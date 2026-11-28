"""Offering Connect access to someone by their Möbius @handle.

Both Möbius instances are this one test server: outbound peer requests loop
back into the same app, so delivery, verification, acceptance and redemption
all run their real endpoints. Only the runner process is simulated: it pairs
by calling the recipient's real ``/api/connect/pair``.
"""
from urllib.parse import urlsplit

import httpx
import pytest

from app import connect_outbound, models
from app.config import get_settings
from app.routes import connect as connect_routes
from app.routes import connect_offers as offers

HOST = "ana.example"


@pytest.fixture
def peers(client, monkeypatch):
  monkeypatch.setattr(get_settings(), "frontend_origin", f"https://{HOST}")
  directory = {"ana": [HOST], "bob": [HOST], "carol": ["carol.example"], "mallory": ["mallory.example"]}
  state = {
    "me": "ana", "calls": [], "paired": [], "lose_answer": {}, "directory": directory,
    "profiles": {}, "revoked": [],
  }

  async def resolve(_db, _owner_id, handle):
    if handle not in directory:
      raise offers.HTTPException(404, "No one has claimed that mobius.you handle.")
    return directory[handle]

  async def own_handle(_db, _owner):
    return state["me"]

  async def federation(method, url, *, json=None, timeout_seconds=10.0, **_):
    parts = urlsplit(url)
    state["calls"].append((parts.netloc, parts.path))
    if parts.netloc != HOST:
      raise httpx.ConnectError("unreachable")
    as_bob = parts.path.endswith("/inbox")
    if as_bob:
      state["me"] = "bob"
    try:
      response = client.request(method, parts.path, json=json)
    finally:
      if as_bob:
        state["me"] = "ana"
    suffix = "/" + parts.path.rsplit("/", 1)[1]
    if state["lose_answer"].get(suffix, 0) > 0:
      state["lose_answer"][suffix] -= 1
      raise httpx.ReadTimeout("the answer was lost")
    return response

  async def pair(label, base_url, code, agent=False, *, source=None, profile_id=None):
    assert source and profile_id
    if state.get("during_pair"):
      state["during_pair"]()
    # The runner redeems the code with the recipient's real pairing endpoint.
    answer = client.post("/api/connect/pair", json={"code": code})
    if answer.status_code != 200:
      raise connect_outbound.OutboundConnectError("The other Möbius did not accept that command.")
    state["paired"].append((label, base_url, code, agent))
    state["profiles"][profile_id] = "active"
    return {"id": profile_id, "label": label}

  async def revoke(profile_id):
    state["revoked"].append(profile_id)
    state["profiles"].pop(profile_id, None)
    return True

  monkeypatch.setattr(offers, "resolve_handle_hosts", resolve)
  monkeypatch.setattr(offers, "owner_handle", own_handle)
  monkeypatch.setattr(offers, "federation_request", federation)
  monkeypatch.setattr(connect_outbound, "create_paired_profile", pair)
  monkeypatch.setattr(connect_outbound, "revoke_profile", revoke)
  monkeypatch.setattr(connect_outbound, "profile_status", lambda pid: state["profiles"].get(pid))

  async def complete(pid):
    return state["profiles"].get(pid) == "active"

  monkeypatch.setattr(connect_outbound, "complete_interrupted_pairing", complete)
  monkeypatch.setattr(offers, "_redeeming", set())
  monkeypatch.setattr(offers, "_accepting", set())
  for limiter in (offers.inbox_limiter, offers.peer_limiter, offers.redeem_limiter):
    limiter.reset()
  return state


def send(client, auth, handle="@Bob", agent=True):
  return client.post("/api/connect/offers", headers=auth, json={"handle": handle, "agent": agent})


def received(client, auth):
  return client.get("/api/connect/offers", headers=auth).json()["received"]


def test_offer_is_verified_and_accepting_pairs_the_recipients_new_machine(client, auth, db, peers):
  sent = send(client, auth)
  assert sent.status_code == 200, sent.text
  assert sent.json()["handle"] == "bob" and sent.json()["delivered_to"] == HOST
  assert (HOST, "/api/connect/offers/check") in peers["calls"]

  [offer] = received(client, auth)
  assert offer["from_handle"] == "ana" and offer["agent"] is True
  note = db.query(models.Notification).filter_by(source_id=offer["id"]).one()
  assert note.title == "@ana offered you access to their Möbius"

  peers["me"] = "bob"
  accepted = client.post(f"/api/connect/offers/received/{offer['id']}/accept", headers=auth)
  assert accepted.status_code == 200, accepted.text
  assert accepted.json()["status"] == "accepted"

  [(label, base_url, _code, agent)] = peers["paired"]
  assert (label, base_url, agent) == ("@bob", f"https://{HOST}", True)
  machines = [h for h in connect_routes._list_hosts() if h["name"] == "@ana’s Möbius"]
  assert len(machines) == 1 and machines[0]["token_sha256"]
  # The offer is spent: it can't be described or redeemed again.
  assert client.get("/api/connect/offers", headers=auth).json() == {"sent": [], "received": []}
  secret_check = [r for r in offers._all("sent") if r["status"] == "connected"]
  assert len(secret_check) == 1
  # Accepting again only repeats the answer.
  again = client.post(f"/api/connect/offers/received/{offer['id']}/accept", headers=auth)
  assert again.status_code == 200 and len(peers["paired"]) == 1


def test_redeem_pairs_only_with_the_invitees_own_mobius(client, auth, peers):
  secret = "s" * 43
  offers._save("sent", {
    "id": "co_" + "1" * 16, "handle": "bob", "agent": True,
    "secret_sha256": offers._digest(secret), "status": "pending",
    "created_at": 1, "expires_at": 4_000_000_000,
  })
  stolen = client.post("/api/connect/offers/redeem", json={
    "offer": secret, "host": "mallory.example", "pairing_code": "ABCD-EFGH",
  })
  assert stolen.status_code == 403
  assert peers["paired"] == []


def test_inbox_refuses_offers_from_another_host_or_for_someone_else(client, auth, peers):
  forged = client.post("/api/connect/offers/inbox", json={
    "format": offers.FORMAT, "from_handle": "mallory",
    "offer_url": f"https://{HOST}{offers.OFFER_PATH}#{'x' * 43}",
  })
  assert forged.status_code == 403
  # A live offer @ana made for @carol can't land in @bob's Connect.
  secret = "c" * 43
  offers._save("sent", {
    "id": "co_" + "2" * 16, "handle": "carol", "agent": False,
    "secret_sha256": offers._digest(secret), "status": "pending",
    "created_at": 1, "expires_at": 4_000_000_000,
  })
  peers["me"] = "bob"
  misdirected = client.post("/api/connect/offers/inbox", json={
    "format": offers.FORMAT, "from_handle": "ana",
    "offer_url": f"https://{HOST}{offers.OFFER_PATH}#{secret}",
  })
  assert misdirected.status_code == 403
  assert "someone else" in misdirected.json()["detail"]
  assert received(client, auth) == []


def test_a_lost_accept_answer_still_finishes_once(client, auth, peers):
  assert send(client, auth).status_code == 200
  [offer] = received(client, auth)
  peers["me"] = "bob"
  peers["lose_answer"]["/redeem"] = 1
  accepted = client.post(f"/api/connect/offers/received/{offer['id']}/accept", headers=auth)
  assert accepted.status_code == 200, accepted.text
  assert len(peers["paired"]) == 1


def test_a_withdrawn_offer_cannot_be_accepted_and_leaves_no_machine(client, auth, peers):
  sent = send(client, auth).json()
  [offer] = received(client, auth)
  assert client.delete(f"/api/connect/offers/sent/{sent['id']}", headers=auth).status_code == 200
  peers["me"] = "bob"
  refused = client.post(f"/api/connect/offers/received/{offer['id']}/accept", headers=auth)
  assert refused.status_code == 410
  assert not [h for h in connect_routes._list_hosts() if h["name"] == "@ana’s Möbius"]
  assert peers["paired"] == []


def test_declining_removes_the_offer(client, auth, peers):
  assert send(client, auth).status_code == 200
  [offer] = received(client, auth)
  assert client.post(f"/api/connect/offers/received/{offer['id']}/decline", headers=auth).status_code == 200
  assert received(client, auth) == []


def test_sender_needs_a_handle_and_cannot_offer_to_themselves(client, auth, peers):
  assert send(client, auth, handle="@ana").status_code == 400
  peers["me"] = None
  assert send(client, auth).status_code == 409


def test_withdrawing_during_pairing_undoes_the_access(client, auth, peers):
  sent = send(client, auth).json()
  [offer] = received(client, auth)

  def withdraw():
    assert client.delete(f"/api/connect/offers/sent/{sent['id']}", headers=auth).status_code == 200

  peers["during_pair"] = withdraw
  peers["me"] = "bob"
  client.post(f"/api/connect/offers/received/{offer['id']}/accept", headers=auth)
  # The recipient's machine paired, but the granter revoked its new access at
  # once (a real revoke also tells the recipient, which then forgets it).
  assert len(peers["paired"]) == 1 and len(peers["revoked"]) == 1
  assert peers["profiles"] == {}
  assert offers._all("sent") == []


def test_a_withdrawal_during_delivery_is_not_undone(client, auth, peers, monkeypatch):
  deliver = offers._deliver

  async def withdrawn_while_delivering(host, envelope, timeout):
    outcome = await deliver(host, envelope, timeout)
    [record] = offers._all("sent")
    assert client.delete(f"/api/connect/offers/sent/{record['id']}", headers=auth).status_code == 200
    return outcome

  monkeypatch.setattr(offers, "_deliver", withdrawn_while_delivering)
  sent = send(client, auth)
  assert sent.status_code == 200 and sent.json()["status"] == "withdrawn"
  assert offers._all("sent") == []


def test_an_accept_before_the_send_returns_stays_accepted(client, auth, peers, monkeypatch):
  deliver = offers._deliver

  async def accepted_while_delivering(host, envelope, timeout):
    outcome = await deliver(host, envelope, timeout)
    [offer] = received(client, auth)
    peers["me"] = "bob"
    accepted = client.post(f"/api/connect/offers/received/{offer['id']}/accept", headers=auth)
    peers["me"] = "ana"
    assert accepted.status_code == 200, accepted.text
    return outcome

  monkeypatch.setattr(offers, "_deliver", accepted_while_delivering)
  assert send(client, auth).status_code == 200
  [record] = offers._all("sent")
  assert record["status"] == "connected" and record["delivered_to"] == HOST
  assert len(peers["paired"]) == 1


def test_one_offer_delivered_twice_at_once_is_stored_once(client, auth, db, peers, monkeypatch):
  federation = offers.federation_request
  seen = {}

  async def delivered_again_during_check(method, url, *, json=None, **kwargs):
    if url.endswith("/inbox"):
      seen["envelope"] = json
    elif url.endswith("/check") and "again" not in seen:
      seen["again"] = None
      seen["again"] = client.post("/api/connect/offers/inbox", json=seen["envelope"]).status_code
    return await federation(method, url, json=json, **kwargs)

  monkeypatch.setattr(offers, "federation_request", delivered_again_during_check)
  assert send(client, auth).status_code == 200
  assert seen["again"] == 200
  [offer] = received(client, auth)
  assert db.query(models.Notification).filter_by(source_id=offer["id"]).count() == 1
  assert len(offers._all("received")) == 1


def test_decline_is_refused_while_an_accept_is_in_progress(client, auth, peers):
  assert send(client, auth).status_code == 200
  [offer] = received(client, auth)
  offers._accepting.add(offer["id"])
  assert client.post(f"/api/connect/offers/received/{offer['id']}/decline", headers=auth).status_code == 409
  assert client.post(f"/api/connect/offers/received/{offer['id']}/accept", headers=auth).status_code == 409


def test_a_redemption_interrupted_by_a_restart_finishes_or_is_undone(client, auth, peers):
  secret, code = "r" * 43, "ABCD-EFGH"
  base = {
    "handle": "bob", "agent": False, "secret_sha256": offers._digest(secret),
    "status": "redeeming", "created_at": 1, "expires_at": 4_000_000_000,
    "pairing_code_sha256": offers._digest(code),
  }
  # Pairing had completed before the restart: the same code just confirms it.
  offers._save("sent", {**base, "id": "co_" + "3" * 16, "profile_id": "o_" + "3" * 16})
  peers["profiles"]["o_" + "3" * 16] = "active"
  done = client.post("/api/connect/offers/redeem", json={"offer": secret, "host": HOST, "pairing_code": code})
  assert done.json() == {"status": "connected"} and peers["paired"] == []

  # Pairing never confirmed: the half-made profile is revoked before retrying.
  other = "q" * 43
  offers._save("sent", {
    **base, "id": "co_" + "4" * 16, "secret_sha256": offers._digest(other),
    "profile_id": "o_" + "4" * 16,
  })
  peers["profiles"]["o_" + "4" * 16] = "connecting"
  retry = client.post("/api/connect/offers/redeem", json={"offer": other, "host": HOST, "pairing_code": code})
  assert "o_" + "4" * 16 in peers["revoked"]
  assert retry.status_code == 502  # this code names no machine entry here
  assert offers._load("sent", "co_" + "4" * 16)["status"] == "pending"


def test_a_runner_sign_in_saved_before_a_crash_completes_the_profile(tmp_path, monkeypatch):
  import json
  monkeypatch.setattr(connect_outbound, "_profiles_dir", lambda: tmp_path)
  paired, unpaired = "o_" + "5" * 16, "o_" + "6" * 16
  for profile_id in (paired, unpaired):
    connect_outbound._atomic_json(connect_outbound._meta_path(profile_id), {
      "id": profile_id, "base_url": "https://bob.example", "status": "connecting",
    })
  config = connect_outbound._runner_config_path(paired)
  config.parent.mkdir(parents=True, exist_ok=True)
  config.write_text(json.dumps({"url": "https://bob.example", "token": "t", "host_id": "h"}))

  assert connect_outbound._complete_interrupted_pairing(paired) is True
  assert connect_outbound.profile_status(paired) == "active"
  # No saved sign-in: pairing never happened, so the profile is left to revoke.
  assert connect_outbound._complete_interrupted_pairing(unpaired) is False
  assert connect_outbound.profile_status(unpaired) == "connecting"
