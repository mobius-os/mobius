"""Inviting someone to a live Project by Möbius @handle.

Both Möbius instances are this one test server: every outbound peer request is
looped back into the same app, so the sender, the receiver's verification, and
the acceptance all run their real endpoints.
"""
import asyncio
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
import pytest

from app import models
from app.config import get_settings
from app.main import app as api
from app.routes import project_handle_invites as invites
from app.timeutil import now_naive_utc

HOST = "ana.example"


@pytest.fixture
def peers(client, monkeypatch):
  """Loop peer HTTPS calls into this app and stub the account directory."""
  monkeypatch.setattr(get_settings(), "frontend_origin", f"https://{HOST}")
  directory = {"ana": [HOST], "bob": [HOST], "mallory": ["mallory.example"]}
  state = {
    "me": "ana", "calls": [], "fail_delivery": False, "directory": directory,
    "redeem_answer": None,
    # Path suffix -> how many answers to drop after the peer has acted.
    "lose_answer": {},
  }

  async def resolve(_db, _owner_id, handle):
    if handle not in directory:
      raise invites.HTTPException(404, "No one has claimed that mobius.you handle.")
    return directory[handle]

  async def own_handle(_db, _owner):
    return state["me"]

  async def federation(method, url, *, json=None, timeout_seconds=10.0, **_):
    parts = urlsplit(url)
    state["calls"].append((parts.netloc, parts.path))
    if parts.netloc == "dead.example":
      # What federation_request raises for a host that doesn't resolve.
      raise invites.HTTPException(400, "Cannot resolve host 'dead.example'")
    if parts.path.endswith("/redeem") and state["redeem_answer"] is not None:
      return httpx.Response(200, json=state["redeem_answer"])
    if parts.path.endswith("/redeem") and state.get("redeem_unreachable"):
      raise httpx.ConnectError("unreachable")
    if parts.netloc != HOST or (state["fail_delivery"] and parts.path.endswith("/inbox")):
      raise httpx.ConnectError("unreachable")
    if parts.path.endswith("/inbox"):
      # The receiver answers as the invitee.
      state["me"] = "bob"
    try:
      response = client.request(method, parts.path, json=json)
    finally:
      if parts.path.endswith("/inbox"):
        state["me"] = "ana"
    suffix = "/" + parts.path.rsplit("/", 1)[1]
    if state["lose_answer"].get(suffix, 0) > 0:
      state["lose_answer"][suffix] -= 1
      raise httpx.ReadTimeout("the answer was lost")
    return response

  monkeypatch.setattr(invites, "resolve_handle_hosts", resolve)
  monkeypatch.setattr(invites, "owner_handle", own_handle)
  monkeypatch.setattr(invites, "federation_request", federation)
  invites.inbox_limiter.reset()
  return state


def make_project(client, auth, name="Shared garden"):
  response = client.post("/api/projects", headers=auth, json={"name": name, "template_id": "blank"})
  assert response.status_code == 200, response.text
  return response.json()


def send(client, auth, project, handle="@Bob", role="editor"):
  return client.post("/api/project-invites/send", headers=auth, json={
    "project_id": project["id"], "handle": handle, "role": role,
  })


def test_handle_invite_arrives_verified_notifies_and_accepting_joins_as_handle(client, auth, db, peers):
  project = make_project(client, auth)
  sent = send(client, auth, project)
  assert sent.status_code == 200, sent.text
  assert sent.json()["handle"] == "bob" and sent.json()["delivered_to"] == HOST
  assert "join_url" not in sent.json()
  assert (HOST, "/api/project-invites/check") in peers["calls"]

  received = client.get("/api/project-invites", headers=auth).json()
  assert len(received) == 1
  invite = received[0]
  assert invite["from_handle"] == "ana"
  assert invite["project_name"] == "Shared garden"
  assert invite["role"] == "editor" and invite["status"] == "pending"
  row = db.get(models.ReceivedProjectInvite, invite["id"])
  assert row.invite_secret_sealed and len(row.invite_secret_sealed) > 43

  note = db.query(models.Notification).filter_by(source_id=invite["id"]).one()
  assert note.title == "@ana invited you to a project"
  assert note.target == "/shell/?projects=1"

  # The sender sees the invitation as pending under the invitee's handle.
  collaboration = client.get(f"/api/projects/{project['id']}/collaboration", headers=auth).json()
  assert [item["invitee_name"] for item in collaboration["invites"]] == ["@bob"]

  peers["me"] = "bob"
  accepted = client.post(f"/api/project-invites/{invite['id']}/accept", headers=auth)
  assert accepted.status_code == 200, accepted.text
  body = accepted.json()
  assert body["status"] == "accepted"
  assert body["open_url"].startswith(f"https://{HOST}/shared/project/{project['id']}#access=")
  member = db.query(models.ProjectMember).filter_by(project_id=project["id"]).one()
  assert member.display_name == "@bob" and member.role == "editor"
  # The shared project says how long this access lasts.
  row = db.get(models.ReceivedProjectInvite, invite["id"])
  db.refresh(row)
  assert abs(row.access_expires_at - (now_naive_utc() + timedelta(days=30))) < timedelta(minutes=1)

  opened = client.post(f"/api/project-invites/{invite['id']}/open", headers=auth)
  assert opened.status_code == 200
  token = opened.json()["url"].split("#access=", 1)[1]
  detail = client.get(f"/api/projects/{project['id']}", headers={"Authorization": f"Bearer {token}"})
  assert detail.status_code == 200, detail.text

  # Accepting twice is harmless and never spends a second invitation.
  assert client.post(f"/api/project-invites/{invite['id']}/accept", headers=auth).status_code == 200
  assert db.query(models.ProjectMember).filter_by(project_id=project["id"]).count() == 1


def test_inbox_rejects_an_invite_whose_host_is_not_the_senders(client, auth, db, peers):
  peers["calls"].clear()
  response = client.post("/api/project-invites/inbox", json={
    "format": "mobius-project-invite-v1",
    "from_handle": "mallory",
    "invite_url": f"https://{HOST}/project-invite#" + "a" * 43,
  })
  assert response.status_code == 403
  assert db.query(models.ReceivedProjectInvite).count() == 0
  assert peers["calls"] == []


def test_inbox_rejects_an_invitation_the_sender_cannot_confirm(client, auth, db, peers):
  response = client.post("/api/project-invites/inbox", json={
    "format": "mobius-project-invite-v1",
    "from_handle": "ana",
    "invite_url": f"https://{HOST}/project-invite#" + "b" * 43,
  })
  assert response.status_code == 422
  assert db.query(models.ReceivedProjectInvite).count() == 0
  assert db.query(models.Notification).count() == 0


@pytest.mark.parametrize("url", [
  f"http://{HOST}/project-invite#" + "c" * 43,
  f"https://{HOST}/elsewhere#" + "c" * 43,
  f"https://{HOST}/project-invite?x=1#" + "c" * 43,
  f"https://{HOST}/project-invite#short",
])
def test_inbox_rejects_malformed_invitation_links(client, peers, url):
  response = client.post("/api/project-invites/inbox", json={
    "format": "mobius-project-invite-v1", "from_handle": "ana", "invite_url": url,
  })
  assert response.status_code == 422


def test_undeliverable_invite_is_withdrawn(client, auth, db, peers):
  project = make_project(client, auth)
  peers["fail_delivery"] = True
  response = send(client, auth, project)
  assert response.status_code == 502
  assert "@bob" in response.json()["detail"]
  row = db.query(models.ProjectInvite).filter_by(project_id=project["id"]).one()
  assert row.revoked_at is not None


def test_self_and_unknown_handles_are_refused_before_creating_an_invite(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project, handle="@ana").status_code == 400
  assert send(client, auth, project, handle="@nobody").status_code == 404
  assert send(client, auth, project, handle="not a handle!").status_code == 400
  assert db.query(models.ProjectInvite).count() == 0


def test_decline_hides_and_remove_deletes(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project).status_code == 200
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  declined = client.post(f"/api/project-invites/{invite_id}/decline", headers=auth)
  assert declined.status_code == 200 and declined.json()["status"] == "declined"
  assert client.get("/api/project-invites", headers=auth).json() == []
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 409
  assert db.get(models.ReceivedProjectInvite, invite_id).invite_secret_sealed is None
  assert client.delete(f"/api/project-invites/{invite_id}", headers=auth).status_code == 204
  assert db.get(models.ReceivedProjectInvite, invite_id) is None


def test_owner_only_endpoints_require_the_owner(client, peers):
  assert client.get("/api/project-invites").status_code == 401
  assert client.post("/api/project-invites/send", json={"project_id": "x", "handle": "bob"}).status_code == 401


def test_one_sender_cannot_crowd_the_inbox(client, auth, db, peers):
  now = now_naive_utc()
  for index in range(invites.MAX_PENDING_PER_SENDER):
    db.add(models.ReceivedProjectInvite(
      id=str(uuid.uuid4()), delivery_key=f"{index:064x}", from_handle="ana",
      from_host=HOST, project_name="Waiting", role="editor", received_at=now,
      expires_at=now + timedelta(days=1), status="pending",
    ))
  db.commit()
  project = make_project(client, auth)
  refused = send(client, auth, project)
  assert refused.status_code == 502
  assert "@ana already has 10 invitations waiting here" in refused.json()["detail"]
  assert db.query(models.ReceivedProjectInvite).count() == invites.MAX_PENDING_PER_SENDER


def link_invite(client, auth, project, invitee_name=None):
  response = client.post(f"/api/projects/{project['id']}/invites", headers=auth, json={
    "invitee_name": invitee_name, "role": "editor",
  })
  assert response.status_code == 200, response.text
  return response.json()


def deliver(client, url, sender="ana"):
  return client.post("/api/project-invites/inbox", json={
    "format": "mobius-project-invite-v1", "from_handle": sender, "invite_url": url,
  })


def test_an_unresolvable_host_is_skipped_for_the_next_one(client, auth, db, peers):
  peers["directory"]["bob"] = ["dead.example", HOST]
  project = make_project(client, auth)
  sent = send(client, auth, project)
  assert sent.status_code == 200, sent.text
  assert sent.json()["delivered_to"] == HOST


def test_an_invite_no_host_accepts_is_revoked_even_when_every_host_is_unresolvable(client, auth, db, peers):
  peers["directory"]["bob"] = ["dead.example"]
  project = make_project(client, auth)
  response = send(client, auth, project)
  assert response.status_code == 502
  assert "resolve" not in response.json()["detail"]
  row = db.query(models.ProjectInvite).filter_by(project_id=project["id"]).one()
  assert row.revoked_at is not None


def test_only_an_invitation_made_for_this_person_reaches_their_inbox(client, auth, db, peers):
  project = make_project(client, auth)
  # A private link, or an invitation meant for someone else, may be held by
  # anyone; pushing it into this inbox must not read as "@ana invited you".
  for name in (None, "@bob"):
    response = deliver(client, link_invite(client, auth, project, name)["join_url"])
    assert response.status_code == 403, response.text
  assert db.query(models.ReceivedProjectInvite).count() == 0
  assert db.query(models.Notification).count() == 0


def test_repeated_delivery_stores_and_notifies_once(client, auth, db, peers):
  project = make_project(client, auth)
  url = link_invite(client, auth, project, "@ana")["join_url"]
  assert deliver(client, url).status_code == 200
  assert deliver(client, url).status_code == 200
  assert db.query(models.ReceivedProjectInvite).count() == 1
  assert db.query(models.Notification).count() == 1


def test_an_invitation_about_to_expire_is_refused(client, auth, db, peers):
  project = make_project(client, auth)
  invite = link_invite(client, auth, project, "@ana")
  db.get(models.ProjectInvite, invite["id"]).expires_at = now_naive_utc() + timedelta(minutes=5)
  db.commit()
  response = deliver(client, invite["join_url"])
  assert response.status_code == 422
  assert db.query(models.ReceivedProjectInvite).count() == 0


def test_check_describes_only_a_live_invitation_and_is_never_cached(client, auth, db, peers):
  project = make_project(client, auth)
  invite = link_invite(client, auth, project, "@ana")
  secret = invite["join_url"].split("#", 1)[1]
  checked = client.post("/api/project-invites/check", json={"invite": secret})
  assert checked.status_code == 200
  assert checked.headers["cache-control"] == "no-store"
  assert checked.json()["invitee"] == "@ana"
  db.get(models.ProjectInvite, invite["id"]).revoked_at = now_naive_utc()
  db.commit()
  assert client.post("/api/project-invites/check", json={"invite": secret}).status_code == 410


def test_accepting_a_withdrawn_invitation_marks_it_expired(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project).status_code == 200
  db.query(models.ProjectInvite).filter_by(project_id=project["id"]).one().revoked_at = now_naive_utc()
  db.commit()
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  peers["me"] = "bob"
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 410
  listed = client.get("/api/project-invites", headers=auth).json()[0]
  assert listed["status"] == "pending" and listed["expired"] is True


def test_a_malformed_redeem_answer_leaves_the_invitation_pending(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project).status_code == 200
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  peers["me"] = "bob"
  peers["redeem_answer"] = {"access_token": "short", "project": {"id": "../x"}}
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 502
  assert db.get(models.ReceivedProjectInvite, invite_id).status == "pending"


def test_the_inbox_is_rate_limited(client, peers):
  url = f"https://{HOST}/project-invite#short"
  codes = [deliver(client, url).status_code for _ in range(31)]
  assert codes[:30] == [422] * 30 and codes[30] == 429


def test_accepting_while_the_sender_is_unreachable_leaves_it_pending(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project).status_code == 200
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  peers["me"] = "bob"
  peers["redeem_unreachable"] = True
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 502
  assert db.get(models.ReceivedProjectInvite, invite_id).status == "pending"


def test_opening_after_access_expired_is_refused(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project).status_code == 200
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  peers["me"] = "bob"
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 200
  row = db.get(models.ReceivedProjectInvite, invite_id)
  db.refresh(row)
  row.access_expires_at = now_naive_utc() - timedelta(minutes=1)
  db.commit()
  assert client.post(f"/api/project-invites/{invite_id}/open", headers=auth).status_code == 410
  assert client.get("/api/project-invites", headers=auth).json()[0]["access_expired"] is True


def received_row(db, invite_id):
  row = db.get(models.ReceivedProjectInvite, invite_id)
  db.refresh(row)
  return row


def test_a_lost_redeem_answer_is_recovered_by_trying_again(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project).status_code == 200
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  peers["me"] = "bob"
  # The sender creates the membership and spends the invitation, but its
  # answer never arrives.
  peers["lose_answer"]["/redeem"] = 1
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 502
  assert received_row(db, invite_id).status == "pending"
  assert db.query(models.ProjectInvite).filter_by(project_id=project["id"]).one().consumed_at is not None

  # Even past the local expiry, only the sender can say it was spent, so
  # the invitation stays actionable rather than offering only Remove.
  row = received_row(db, invite_id)
  row.expires_at = now_naive_utc() - timedelta(minutes=1)
  db.commit()
  assert client.get("/api/project-invites", headers=auth).json()[0]["expired"] is False
  retried = client.post(f"/api/project-invites/{invite_id}/accept", headers=auth)
  assert retried.status_code == 200, retried.text
  assert retried.json()["status"] == "accepted"
  member = db.query(models.ProjectMember).filter_by(project_id=project["id"]).one()
  assert member.display_name == "@bob"
  row = received_row(db, invite_id)
  assert row.redeem_key_sealed is None and row.invite_secret_sealed is None
  token = client.post(f"/api/project-invites/{invite_id}/open", headers=auth).json()["url"].split("#access=", 1)[1]
  detail = client.get(f"/api/projects/{project['id']}", headers={"Authorization": f"Bearer {token}"})
  assert detail.status_code == 200, detail.text


def test_a_spent_invitation_answers_only_its_own_redeemer(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project).status_code == 200
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  secret = invites._unseal(received_row(db, invite_id).invite_secret_sealed)
  peers["me"] = "bob"
  peers["lose_answer"]["/redeem"] = 1
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 502
  key = invites._unseal(received_row(db, invite_id).redeem_key_sealed)

  # A leaked secret alone finds the invitation spent.
  other = client.post("/api/project-invites/redeem", json={"invite": secret, "redeem_key": "z" * 43})
  assert other.status_code == 410
  replay = client.post("/api/project-invites/redeem", json={"invite": secret, "redeem_key": key})
  assert replay.status_code == 200, replay.text
  # A removed member's key no longer opens anything.
  member = db.query(models.ProjectMember).filter_by(project_id=project["id"]).one()
  member.revoked_at = now_naive_utc()
  db.commit()
  assert client.post("/api/project-invites/redeem", json={"invite": secret, "redeem_key": key}).status_code == 410
  assert db.query(models.ProjectMember).filter_by(project_id=project["id"]).count() == 1

  # Link invitations keep their own single-use redeem.
  link_secret = link_invite(client, auth, project)["join_url"].split("#", 1)[1]
  assert client.post("/api/project-invites/redeem", json={"invite": link_secret, "redeem_key": key}).status_code == 410


def test_a_lost_delivery_answer_is_settled_by_delivering_again(client, auth, db, peers):
  project = make_project(client, auth)
  peers["lose_answer"]["/inbox"] = 1
  sent = send(client, auth, project)
  assert sent.status_code == 200, sent.text
  assert sent.json()["delivered_to"] == HOST and sent.json()["delivery"] == "delivered"
  assert db.query(models.ProjectInvite).filter_by(project_id=project["id"]).one().revoked_at is None
  assert db.query(models.ReceivedProjectInvite).count() == 1
  assert db.query(models.Notification).count() == 1


def test_an_unconfirmed_delivery_keeps_the_invitation_for_its_owner(client, auth, db, peers):
  project = make_project(client, auth)
  peers["lose_answer"]["/inbox"] = 2
  sent = send(client, auth, project)
  assert sent.status_code == 202, sent.text
  assert sent.json()["delivery"] == "unconfirmed" and sent.json()["delivered_to"] is None
  # It did arrive, so it must still work, and its owner can still revoke it.
  assert db.query(models.ProjectInvite).filter_by(project_id=project["id"]).one().revoked_at is None
  collaboration = client.get(f"/api/projects/{project['id']}/collaboration", headers=auth).json()
  assert [item["invitee_name"] for item in collaboration["invites"]] == ["@bob"]
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  peers["me"] = "bob"
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 200


def test_a_server_error_from_the_inbox_is_not_proof_of_non_delivery(client, auth, db, peers, monkeypatch):
  project = make_project(client, auth)

  async def failing(method, url, **_):
    return httpx.Response(503, json={"detail": "busy"})

  monkeypatch.setattr(invites, "federation_request", failing)
  assert send(client, auth, project).status_code == 202
  assert db.query(models.ProjectInvite).filter_by(project_id=project["id"]).one().revoked_at is None


def test_concurrent_deliveries_cannot_exceed_the_per_sender_limit(client, auth, db, peers, monkeypatch):
  count = invites.MAX_PENDING_PER_SENDER + 1
  expires = (now_naive_utc() + timedelta(days=3)).isoformat() + "Z"

  async def run():
    arrived = []
    release = asyncio.Event()

    async def federation(method, url, *, json=None, **_):
      # Every delivery is held at /check until all of them have passed the
      # early limit check.
      arrived.append(url)
      await release.wait()
      return httpx.Response(200, json={
        "format": "mobius-project-invite-v1", "project": {"name": "Waiting"},
        "role": "editor", "expires_at": expires, "invitee": "@ana",
      })

    monkeypatch.setattr(invites, "federation_request", federation)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as peer:
      tasks = [asyncio.create_task(peer.post("/api/project-invites/inbox", json={
        "format": "mobius-project-invite-v1", "from_handle": "bob",
        "invite_url": f"https://{HOST}/project-invite#{index:043d}",
      })) for index in range(count)]
      async with asyncio.timeout(10):
        while len(arrived) < count:
          await asyncio.sleep(0.01)
      release.set()
      return [(await task).status_code for task in tasks]

  codes = asyncio.run(run())
  assert sorted(codes) == [200] * invites.MAX_PENDING_PER_SENDER + [429]
  assert db.query(models.ReceivedProjectInvite).filter_by(from_handle="bob").count() == invites.MAX_PENDING_PER_SENDER
  assert db.query(models.Notification).count() == invites.MAX_PENDING_PER_SENDER


def test_the_senders_refusal_settles_an_invitation_in_doubt(client, auth, db, peers):
  project = make_project(client, auth)
  assert send(client, auth, project).status_code == 200
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  peers["me"] = "bob"
  peers["redeem_unreachable"] = True
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 502
  assert received_row(db, invite_id).redeem_key_sealed is not None
  peers["redeem_unreachable"] = False
  db.query(models.ProjectInvite).filter_by(project_id=project["id"]).one().revoked_at = now_naive_utc()
  db.commit()
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 410
  row = received_row(db, invite_id)
  assert row.redeem_key_sealed is None and row.invite_secret_sealed is None
  assert client.get("/api/project-invites", headers=auth).json()[0]["expired"] is True


def lost_redeem(client, auth, db, peers):
  """Accept once with the answer lost; return the invitation secret and redeem key."""
  project = make_project(client, auth)
  assert send(client, auth, project).status_code == 200
  invite_id = client.get("/api/project-invites", headers=auth).json()[0]["id"]
  secret = invites._unseal(received_row(db, invite_id).invite_secret_sealed)
  peers["me"] = "bob"
  peers["lose_answer"]["/redeem"] = 1
  assert client.post(f"/api/project-invites/{invite_id}/accept", headers=auth).status_code == 502
  return secret, invites._unseal(received_row(db, invite_id).redeem_key_sealed)


def test_replay_ends_with_the_invitation_lifetime(client, auth, db, peers):
  secret, key = lost_redeem(client, auth, db, peers)
  redemption = db.query(models.ProjectInviteRedemption).one()
  redemption.redeemed_at = now_naive_utc() - timedelta(days=8)
  db.commit()
  assert client.post("/api/project-invites/redeem", json={"invite": secret, "redeem_key": key}).status_code == 410


def test_replay_ends_when_the_owner_signs_out_everywhere(client, auth, db, peers):
  secret, key = lost_redeem(client, auth, db, peers)
  owner = db.query(models.Owner).one()
  owner.token_epoch += 1
  db.commit()
  assert client.post("/api/project-invites/redeem", json={"invite": secret, "redeem_key": key}).status_code == 410
