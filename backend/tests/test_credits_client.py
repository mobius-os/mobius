"""A Möbius paired with a credits service: pairing, reads, and app charges."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi import HTTPException

from app import app_consent, credits_client, deps, models
from app.auth import create_access_token, create_agent_token, create_app_token
from app.browser_access import BrowserLineage, redeem_invitation
from app.config import get_settings
from tests.browser_access_fixtures import link_grant


class FakeService:
  """Records calls and answers like the mobius.you credits service."""

  def __init__(self):
    self.calls = []
    self.answers = {}

  def __call__(self, method, url, json=None, headers=None, **_kwargs):
    self.calls.append({"method": method, "url": url, "json": json, "headers": headers})
    path = url.split("/api/billing/", 1)[1]
    status, body = self.answers.get(path, (200, {"balance": "0.00"}))
    return httpx.Response(status, json=body, request=httpx.Request(method, url))


@pytest.fixture
def service(monkeypatch, tmp_path):
  monkeypatch.setattr(get_settings(), "data_dir", str(tmp_path))
  fake = FakeService()
  monkeypatch.setattr(credits_client.httpx, "request", fake)
  return fake


@pytest.fixture
def paired(service):
  credits_client.pair("https://www.mobius.you/credits/", "mbc_example_key")
  return service


def _app(db, slug, *, spend_credits=("ana/prompts",), package_id=None, store=True,
         **grants):
  row = models.App(
    slug=slug, source_dir=f"/tmp/mobius-tests/{slug}", name=slug,
    description="", jsx_source="export default function App(){}",
    compiled_path="/tmp/app.js", package_id=package_id,
    manifest_url=f"https://store.example/{slug}/mobius.json" if store else None,
    capability_contract={"data": {"spend_credits": list(spend_credits), **grants}},
  )
  if store:
    assert app_consent.accept(row, app_consent.request_digest(app_consent.current_rows(row)))
  db.add(row)
  db.commit()
  db.refresh(row)
  return row


def _as(db, app, service):
  owner = db.query(models.Owner).first()
  token = create_app_token(app.id, owner.username, owner.token_epoch, app.token_nonce,
                           consent=app_consent.session_id(app), service=service)
  return {"Authorization": f"Bearer {token}"}


TOPUP_ID = "12345678-1234-4567-89ab-123456789abc"


CHARGE = {"seller": "ana", "app": "prompts", "action": "run",
          "idempotency_key": "run-00000001", "max_price_micro": 500_000}


def test_an_unpaired_installation_is_told_so(client, auth, service):
  body = client.get("/api/identity/credits", headers=auth).json()
  assert body["mode"] == "unpaired" and body["balance"] is None
  assert not service.calls


class FakeLauncher:
  """The identity broker's route to the launcher, as the credits client calls it."""

  def __init__(self, status=200, body=None):
    self.status = status
    self.body = body if body is not None else {
      "service_url": "https://www.mobius.you/credits", "key": "mbc_from_launcher"}
    self.posts = []

  def __call__(self, *, timeout):
    launcher = self

    class Client:
      def __enter__(self):
        return self

      def __exit__(self, *_exc):
        return False

      def post(self, path):
        launcher.posts.append(path)
        return httpx.Response(launcher.status, json=launcher.body,
                              request=httpx.Request("POST", "http://broker" + path))

    return Client()


@pytest.fixture
def launcher(monkeypatch):
  """This Möbius was launched from mobius.you and can reach its launcher."""
  monkeypatch.setattr(get_settings(), "mobius_sso_issuer", "https://www.mobius.you")
  monkeypatch.setattr(get_settings(), "mobius_sso_instance_id", "mob_example")
  fake = FakeLauncher()
  monkeypatch.setattr(credits_client, "broker_client", fake)
  return fake


def test_a_launched_installation_pairs_with_its_owners_wallet_without_a_key(
  client, auth, service, launcher
):
  before = client.get("/api/identity/credits", headers=auth).json()
  assert before["mode"] == "unpaired" and before["can_pair_with_account"] is True

  paired = client.post("/api/identity/credits/pair/account", headers=auth)

  assert paired.status_code == 200 and paired.json() == {"paired": True}
  assert launcher.posts == ["/managed/api/instance/v1/credits/key"]
  # The key is checked at the service before it is kept, exactly like a pasted one.
  assert service.calls[-1]["url"] == "https://www.mobius.you/credits/api/billing/balance"
  assert service.calls[-1]["headers"]["Authorization"] == "Bearer mbc_from_launcher"
  assert credits_client.load_credentials()["key"] == "mbc_from_launcher"
  assert client.get("/api/identity/credits", headers=auth).json()["mode"] == "customer"


def test_a_self_hosted_installation_is_not_offered_account_pairing(
  client, auth, service, monkeypatch
):
  monkeypatch.setattr(credits_client, "broker_client", FakeLauncher())
  body = client.get("/api/identity/credits", headers=auth).json()
  assert body["can_pair_with_account"] is False

  refused = client.post("/api/identity/credits/pair/account", headers=auth)

  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "not_managed"
  assert credits_client.broker_client.posts == []
  assert credits_client.load_credentials() is None


@pytest.mark.parametrize("status, body, balance", [
  (503, {"error": {"code": "credits_unavailable", "message": "unavailable"}}, (200, {})),
  (200, {"service_url": "https://www.mobius.you/credits"}, (200, {})),
  (200, {"service_url": "https://www.mobius.you/credits", "key": "mbc_from_launcher"},
   (401, {"detail": {"code": "invalid_api_key", "message": "no"}})),
], ids=["launcher-unavailable", "no-key", "service-refuses-key"])
def test_account_pairing_that_does_not_complete_leaves_the_installation_unpaired(
  client, auth, service, launcher, status, body, balance
):
  launcher.status, launcher.body = status, body
  service.answers["balance"] = balance

  refused = client.post("/api/identity/credits/pair/account", headers=auth)

  assert refused.status_code >= 400
  assert credits_client.load_credentials() is None


def test_pairing_keeps_the_service_path_and_stores_the_key_privately(
  client, auth, service, tmp_path
):
  paired = client.post(
    "/api/identity/credits/pair",
    json={"operator_url": "https://www.mobius.you/credits/", "key": "mbc_example_key"},
    headers=auth)
  assert paired.status_code == 200, paired.text
  assert service.calls[-1]["url"] == "https://www.mobius.you/credits/api/billing/balance"
  assert service.calls[-1]["headers"]["Authorization"] == "Bearer mbc_example_key"
  stored = tmp_path / "credits-client" / "credentials.json"
  assert stored.stat().st_mode & 0o777 == 0o600
  assert "mbc_example_key" not in stored.read_text()
  assert "mobius.you" not in stored.read_text()
  assert [p.name for p in stored.parent.iterdir()] == ["credentials.json"]
  assert client.get("/api/identity/credits", headers=auth).json()["mode"] == "customer"

  assert client.post("/api/identity/credits/unpair", headers=auth).json() == {"paired": False}
  assert not stored.exists()


def test_a_key_the_service_refuses_is_not_stored(client, auth, service, tmp_path):
  service.answers["balance"] = (401, {"detail": {
    "code": "invalid_api_key", "message": "Invalid API key."}})
  refused = client.post(
    "/api/identity/credits/pair",
    json={"operator_url": "https://www.mobius.you/credits", "key": "mbc_revoked"},
    headers=auth)
  # Not 401: that would read as the caller's own Möbius sign-in expiring.
  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "invalid_api_key"
  assert not (tmp_path / "credits-client" / "credentials.json").exists()


def test_an_address_without_a_credits_service_is_a_bad_gateway(client, auth, service):
  service.answers["balance"] = (404, {"detail": "Not Found"})
  refused = client.post(
    "/api/identity/credits/pair",
    json={"operator_url": "https://example.com", "key": "mbc_example_key"}, headers=auth)
  assert refused.status_code == 502 and refused.json()["detail"]["code"] == "not_found"


def _attacker_rewrites(stored, **fields):
  """What an app able to write the file could do: keep what it cannot forge
  (the sealed part) and change the rest."""
  stored.write_text(json.dumps({**json.loads(stored.read_text()), **fields}))


ATTACKER = "https://collector.attacker.example"


def _the_key_went_only_to_the_paired_service(service):
  assert service.calls
  for call in service.calls:
    assert call["url"].startswith("https://www.mobius.you/credits/"), call["url"]


def test_a_credentials_file_pointed_at_another_address_never_sends_the_key_there(
  client, auth, paired, tmp_path
):
  stored = tmp_path / "credits-client" / "credentials.json"
  _attacker_rewrites(stored, operator_url=ATTACKER, service_url=ATTACKER)
  paired.calls.clear()
  body = client.get("/api/identity/credits", headers=auth).json()
  assert body["service_host"] == "www.mobius.you"
  _the_key_went_only_to_the_paired_service(paired)


def _key_sealed_beside_a_clear_address(_current):
  sealed_key = credits_client._fernet().encrypt(b"mbc_example_key").decode()
  return {"operator_url": ATTACKER, "sealed_key": sealed_key}


def _key_in_the_clear(_current):
  return {"operator_url": ATTACKER, "key": "mbc_example_key"}


def _sealed_pairing_altered(current):
  sealed = current["sealed"]
  middle = len(sealed) // 2
  changed = "A" if sealed[middle] != "A" else "B"
  return {"sealed": sealed[:middle] + changed + sealed[middle + 1:]}


@pytest.mark.parametrize("tamper", [
  _key_sealed_beside_a_clear_address, _key_in_the_clear, _sealed_pairing_altered,
])
def test_a_credentials_file_this_installation_did_not_seal_reads_as_unpaired(
  client, auth, paired, tmp_path, tamper
):
  stored = tmp_path / "credits-client" / "credentials.json"
  stored.write_text(json.dumps(tamper(json.loads(stored.read_text()))))
  paired.calls.clear()
  assert credits_client.load_credentials() is None
  assert client.get("/api/identity/credits", headers=auth).json()["mode"] == "unpaired"
  assert client.post("/api/identity/credits/withdraw", headers=auth).status_code == 409
  assert not paired.calls


def test_a_key_sealed_under_another_installation_secret_reads_as_unpaired(
  paired, monkeypatch
):
  assert credits_client.load_credentials()["key"] == "mbc_example_key"
  monkeypatch.setattr(get_settings(), "secret_key", "a-different-installation-secret-0")
  assert credits_client.load_credentials() is None
  assert credits_client.summary()["mode"] == "unpaired"


def test_overlapping_pairings_never_share_a_temporary_file(service, tmp_path, monkeypatch):
  real_replace, temporaries = credits_client.os.replace, []

  def record(source, target):
    temporaries.append(source)
    real_replace(source, target)

  monkeypatch.setattr(credits_client.os, "replace", record)
  credits_client.pair("https://www.mobius.you/credits", "mbc_first_key")
  credits_client.pair("https://www.mobius.you/credits", "mbc_second_key")
  assert len(set(temporaries)) == 2
  assert credits_client.load_credentials()["key"] == "mbc_second_key"

  def fail(_source, _target):
    raise OSError("disk full")

  monkeypatch.setattr(credits_client.os, "replace", fail)
  with pytest.raises(OSError):
    credits_client.pair("https://www.mobius.you/credits", "mbc_third_key")
  folder = tmp_path / "credits-client"
  assert [p.name for p in folder.iterdir()] == ["credentials.json"]
  assert credits_client.load_credentials()["key"] == "mbc_second_key"


@pytest.mark.parametrize(("url", "key"), [
  ("https://www.mobius.you/credits", "sk_live_wrong_shape"),
  ("http://credits.example", "mbc_example_key"),
  ("https://credits.example:abc", "mbc_example_key"),
  ("https://credits.example:99999", "mbc_example_key"),
  ("https://[credits.example", "mbc_example_key"),
  ("https://name:secret@credits.example", "mbc_example_key"),
  ("https://name@credits.example", "mbc_example_key"),
])
def test_pairing_refuses_a_wrong_key_or_plain_http(client, auth, service, url, key):
  refused = client.post("/api/identity/credits/pair",
                        json={"operator_url": url, "key": key}, headers=auth)
  assert refused.status_code == 422 and not service.calls


def test_an_address_the_http_client_cannot_use_is_refused_not_a_crash(
  client, auth, service, monkeypatch
):
  def invalid(*_args, **_kwargs):
    raise httpx.InvalidURL("Invalid port")

  monkeypatch.setattr(credits_client.httpx, "request", invalid)
  refused = client.post("/api/identity/credits/pair", headers=auth, json={
    "operator_url": "https://credits.example", "key": "mbc_example_key"})
  assert refused.status_code == 422, refused.text


def test_summary_passes_the_service_payout_fields_through(client, auth, paired):
  payouts = {"earned_micro": 2_000_000, "withdrawable_micro": 1_000_000,
             "keepable_micro": 1_500_000, "withdrawal_status": "pending",
             "destination": "…ab12", "cooldown_ends_at": "2026-11-05T00:00:00+00:00"}
  paired.answers["payouts"] = (200, payouts)
  body = client.get("/api/identity/credits", headers=auth).json()
  assert body["service_host"] == "www.mobius.you" and body["error"] is None
  assert body["earnings"] == payouts


def test_a_charge_only_key_reads_its_balance_without_an_error(client, auth, paired):
  paired.answers["payouts"] = (403, {"detail": {
    "code": "key_scope", "message": "this key may only read and pay for calls"}})
  body = client.get("/api/identity/credits", headers=auth).json()
  assert body["balance"] == {"balance": "0.00"}
  assert body["earnings"] is None and body["error"] is None


def test_an_unreachable_service_is_an_error_not_a_zero(client, auth, paired, monkeypatch):
  def unreachable(*_args, **_kwargs):
    raise httpx.ConnectError("down")

  monkeypatch.setattr(credits_client.httpx, "request", unreachable)
  body = client.get("/api/identity/credits", headers=auth).json()
  assert body["balance"] is None
  assert body["error"]["message"] == "Could not reach www.mobius.you."


def test_owner_actions_reach_the_service_routes(client, auth, paired):
  checkout = "https://checkout.stripe.com/c/pay/cs_test_a1#fid=x"
  paired.answers["topups"] = (200, {"topup_id": TOPUP_ID, "checkout_url": checkout})
  paired.answers["payouts/connect"] = (
    200, {"connect_url": "https://connect.stripe.com/setup/e/acct_1/abc"})
  started = client.post("/api/identity/credits/topup", headers=auth, json={
    "amount_cents": 1000, "waiver_accepted": True})
  assert started.json() == {"checkout_url": checkout, "topup_id": TOPUP_ID}
  assert paired.calls[-1]["json"] == {
    "amount_cents": 1000, "waiver_accepted": True}

  requests = [
    ("get", f"/api/identity/credits/topups/{TOPUP_ID}", None, "GET", f"topups/{TOPUP_ID}"),
    ("get", "/api/identity/credits/transactions?limit=500", None,
     "GET", "transactions?limit=200"),
    ("post", "/api/identity/credits/model-funding",
     {"amount_micro": 5_000_000, "idempotency_key": "fund-0001"}, "POST", "model-funding"),
    ("get", "/api/identity/credits/prices/ana?app_slug=prompts", None,
     "GET", "prices/ana?app_slug=prompts"),
    ("put", "/api/identity/credits/prices/prompts/run", {"price_micro": 250_000},
     "PUT", "priced-actions/prompts/run"),
    ("post", "/api/identity/credits/connect", None,
     "POST", "payouts/connect"),
    ("post", "/api/identity/credits/convert", None, "POST", "payouts/convert"),
    ("post", "/api/identity/credits/withdraw", None, "POST", "payouts/withdraw"),
    ("post", "/api/identity/credits/disconnect", None, "POST", "payouts/disconnect"),
  ]
  for verb, route, body, method, path in requests:
    answer = getattr(client, verb)(route, headers=auth, **({"json": body} if body else {}))
    assert answer.status_code == 200, (route, answer.text)
    call = paired.calls[-1]
    assert (call["method"], call["url"].split("/api/billing/", 1)[1]) == (method, path)
  assert paired.calls[-5]["json"] == {
    "price_micro": 250_000, "description": None, "enabled": True}
  assert paired.calls[-4]["json"] == {}


@pytest.mark.parametrize("page", [
  "javascript:alert(document.cookie)",
  "data:text/html,<script>alert(1)</script>",
  "http://checkout.stripe.com/c/pay/cs_test_a1",
  "https://checkout.stripe.com.example.net/c/pay",
  "https://checkout.stripe.com@example.net/c/pay",
  "https://checkout.stripe.com:8443/c/pay",
  "https://checkout.stripe.com:bad/c/pay",
  "https://[checkout.stripe.com/c/pay",
  "https://connect.stripe.com/setup/e/acct_1/abc",
  "",
  None,
])
def test_a_checkout_page_that_is_not_stripes_is_never_passed_on(client, auth, paired, page):
  paired.answers["topups"] = (200, {"topup_id": TOPUP_ID, "checkout_url": page})
  refused = client.post("/api/identity/credits/topup", headers=auth, json={
    "amount_cents": 1000, "waiver_accepted": True})
  assert refused.status_code == 502
  assert refused.json()["detail"]["code"] == "unexpected_page"


@pytest.mark.parametrize("page", [
  "javascript:alert(1)",
  "https://checkout.stripe.com/c/pay/cs_test_a1",
  "https://connect.stripe.com.example.net/setup",
])
def test_a_payout_setup_page_that_is_not_stripes_is_never_passed_on(
  client, auth, paired, page,
):
  paired.answers["payouts/connect"] = (200, {"connect_url": page})
  refused = client.post("/api/identity/credits/connect", headers=auth, json={})
  assert refused.status_code == 502
  assert refused.json()["detail"]["code"] == "unexpected_page"


def test_owner_actions_before_pairing_say_so(client, auth, service):
  refused = client.post("/api/identity/credits/withdraw", headers=auth)
  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "not_paired"
  assert not service.calls


def test_only_an_app_service_with_spend_credits_can_charge(client, auth, db, paired):
  granted, ungranted = _app(db, "granted"), _app(db, "ungranted", spend_credits=())
  for headers in (_as(db, granted, None), _as(db, granted, "public"),
                  _as(db, ungranted, "private"), auth):
    assert client.post("/api/identity/credits/charges", json=CHARGE,
                       headers=headers).status_code == 403
  paired.answers["charges"] = (200, {"charge_id": "c1", "amount": "0.50"})
  paid = client.post("/api/identity/credits/charges", json=CHARGE,
                     headers=_as(db, granted, "private"))
  assert paid.status_code == 200 and paid.json()["charge_id"] == "c1"
  # The service learns which local app paid, for the owner's history.
  assert paired.calls[-1]["json"] == {
    **CHARGE, "buyer_app": "granted",
    "idempotency_key": credits_client.app_scoped_key(
      f"app:{granted.id}", CHARGE["idempotency_key"]),
  }


def test_an_app_pays_only_the_sellers_its_grant_lists(client, auth, db, paired):
  app = _app(db, "granted")
  calls = len(paired.calls)
  for other in ({"seller": "bob"}, {"app": "other-app"}):
    refused = client.post("/api/identity/credits/charges", json={**CHARGE, **other},
                          headers=_as(db, app, "private"))
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "seller_not_allowed"
  assert len(paired.calls) == calls


@pytest.mark.parametrize("through_frame", [False, True], ids=["agent", "app-frame"])
def test_a_service_cannot_launder_helper_lineage_into_spending_credits(
  client, auth, db, paired, through_frame,
):
  from tests.test_app_services import SERVICE_TOKEN, _service_agent_token, _service_app

  app = _service_app(db, service_bytes=SERVICE_TOKEN)
  app.manifest_url = "https://store.example/service-test/mobius.json"
  app.capability_contract = {
    **app.capability_contract, "data": {"spend_credits": ["ana/prompts"]},
  }
  assert app_consent.accept(app, app_consent.request_digest(app_consent.current_rows(app)))
  db.commit()
  helper = _service_agent_token(db, delegated=True)
  paired.answers["charges"] = (200, {"charge_id": "c1"})
  for headers, delegated in (({"Authorization": f"Bearer {helper}"}, True), (auth, False)):
    if through_frame:
      frame = client.post("/api/auth/app-token", headers=headers, json={"app_id": app.id})
      assert frame.status_code == 200, frame.text
      headers = {"Authorization": f"Bearer {frame.json()['token']}"}
    invocation = client.get(f"/api/apps/{app.id}/service/token", headers=headers)
    assert invocation.status_code == 200, invocation.text
    token = invocation.json()["token"]
    principal = deps.get_principal(token, db)
    assert principal.app_is_service
    assert principal.delegation_id == ("service-helper" if delegated else None)
    calls = len(paired.calls)
    charged = client.post("/api/identity/credits/charges", json=CHARGE,
                          headers={"Authorization": f"Bearer {token}"})
    assert charged.status_code == (403 if delegated else 200), charged.text
    assert len(paired.calls) == calls + (0 if delegated else 1)
    if delegated:
      assert "Delegated" in charged.json()["detail"]
    else:
      assert charged.json()["charge_id"] == "c1"


def test_an_app_that_spends_credits_can_read_the_price_it_pays(client, auth, db, paired):
  granted, ungranted = _app(db, "granted"), _app(db, "ungranted", spend_credits=())
  path = "/api/identity/credits/prices/ana?app_slug=prompts"
  paired.answers["prices/ana?app_slug=prompts"] = (200, {"prices": [{"price_micro": 250_000}]})
  for headers in (_as(db, granted, "private"), _as(db, granted, None)):
    answer = client.get(path, headers=headers)
    assert answer.status_code == 200
    assert answer.json() == {"prices": [{"price_micro": 250_000}]}
  calls = len(paired.calls)
  refused = client.get(path, headers=_as(db, ungranted, "private"))
  assert refused.status_code == 403 and "spend_credits" in refused.json()["detail"]
  assert len(paired.calls) == calls


def test_a_shared_browser_guest_cannot_spend_the_owners_credits(client, auth, db):
  owner = db.query(models.Owner).first()
  guest = deps.Principal(owner=owner, app_id=1, app_is_service=True,
                         browser=BrowserLineage("grant-1"))
  with pytest.raises(HTTPException) as refused:
    deps.get_credits_spender(principal=guest, db=db)
  assert refused.value.status_code == 403


@pytest.mark.parametrize(("status", "detail"), [
  (402, {"code": "insufficient_credits", "message": "not enough credits",
         "required_micro": 500_000, "available_micro": 0}),
  (409, {"code": "price_changed", "message": "the price is higher than the caller agreed to",
         "price_micro": 750_000, "max_price_micro": 500_000}),
])
def test_a_refused_charge_reaches_the_app_with_its_details(
  client, auth, db, paired, status, detail
):
  app = _app(db, "granted")
  paired.answers["charges"] = (status, {"detail": detail})
  refused = client.post("/api/identity/credits/charges", json=CHARGE,
                        headers=_as(db, app, "private"))
  assert refused.status_code == status
  assert refused.json()["detail"] == detail


def test_charging_before_pairing_says_so(client, auth, db, service):
  app = _app(db, "granted")
  answer = client.post("/api/identity/credits/charges", json=CHARGE,
                       headers=_as(db, app, "private"))
  assert answer.status_code == 409 and answer.json()["detail"]["code"] == "not_paired"
  assert not service.calls


def test_settings_reads_the_amounts_the_service_sells(client, auth, service):
  credits_client.pair("https://www.mobius.you/credits", "mbc_example_key")
  terms = {"currency": "usd", "amounts_cents": [500, 1000], "waiver_required": True}
  service.answers["purchase-terms"] = (200, terms)
  read = client.get("/api/identity/credits/purchase-terms", headers=auth)
  assert read.status_code == 200, read.text
  assert service.calls[-1]["url"] == "https://www.mobius.you/credits/api/billing/purchase-terms"
  pairing = client.get("/api/identity/credits", headers=auth).json()["pairing"]
  assert read.json() == {**terms, "pairing": pairing}


# --- Credits management is its own grant ---------------------------------------

# Every route that reads or moves the owner's money, with a body it accepts.
OWNER_CREDIT_ROUTES = [
  ("GET", "/api/identity/credits", None),
  ("GET", "/api/identity/credits/transactions", None),
  ("GET", "/api/identity/credits/purchase-terms", None),
  ("GET", f"/api/identity/credits/topups/{TOPUP_ID}", None),
  ("GET", "/api/identity/credits/prices/ana", None),
  ("POST", "/api/identity/credits/pair",
   {"operator_url": "https://www.mobius.you/credits", "key": "mbc_other_key"}),
  ("POST", "/api/identity/credits/topup", {"amount_cents": 500}),
  ("POST", "/api/identity/credits/model-funding",
   {"amount_micro": 1_000_000, "idempotency_key": "fund-0001"}),
  ("PUT", "/api/identity/credits/prices/prompts/run", {"price_micro": 250_000}),
  ("POST", "/api/identity/credits/connect", {}),
  ("POST", "/api/identity/credits/convert", None),
  ("POST", "/api/identity/credits/withdraw", None),
  ("POST", "/api/identity/credits/disconnect", None),
  ("POST", "/api/identity/credits/pair/account", None),
  ("POST", "/api/identity/credits/unpair", None),
]


@pytest.mark.parametrize("service_scope", [None, "private"], ids=["frame", "service"])
@pytest.mark.parametrize("has_package_id", [False, True], ids=["local", "local-package"])
def test_local_manifest_credits_permissions_do_not_grant_credits_access(
  client, auth, db, paired, service_scope, has_package_id,
):
  from app.app_capabilities import contract_from_manifest

  app = _app(db, "local-credits", store=False,
             package_id="local-package" if has_package_id else None)
  app.capability_contract = contract_from_manifest({
    "permissions": {"credits_manage": True, "spend_credits": ["ana/prompts"]},
  })
  db.commit()
  assert not app_consent.gated(app)
  headers = _as(db, app, service_scope)
  calls = len(paired.calls)
  routes = [*OWNER_CREDIT_ROUTES,
            ("POST", "/api/identity/credits/charges", CHARGE)]
  for method, path, body in routes:
    refused = client.request(method, path, json=body, headers=headers)
    assert refused.status_code == 403, (path, refused.text)
    if not path.endswith("/charges") or service_scope == "private":
      assert "installed Store app with accepted consent" in refused.json()["detail"]
  assert len(paired.calls) == calls
  assert credits_client.load_credentials()["key"] == "mbc_example_key"


@pytest.mark.parametrize("gate", [deps.get_credits_reader, deps.get_credits_controller,
                                  deps.get_credits_spender, deps.get_credits_price_reader])
def test_credits_gates_require_the_store_contract_to_be_accepted(auth, db, gate):
  app = _app(db, "credits-manager", credits_manage=True)
  owner = db.query(models.Owner).first()
  principal = deps.Principal(owner=owner, app_id=app.id, app_is_service=True)
  assert gate(principal=principal, db=db) is not None
  app.accepted_consent = None
  db.commit()
  with pytest.raises(HTTPException) as refused:
    gate(principal=principal, db=db)
  assert refused.value.status_code == 403
  assert "installed Store app with accepted consent" in refused.value.detail


@pytest.mark.parametrize("service_scope", [None, "private"], ids=["frame", "service"])
def test_an_app_approved_only_to_manage_identity_cannot_reach_the_owners_credits(
  client, auth, db, paired, service_scope
):
  """Consent to the sign-in link is not consent to see or move money."""
  app = _app(db, "identity-only", spend_credits=(), identity_manage=True)
  headers = _as(db, app, service_scope)
  calls = len(paired.calls)
  for method, path, body in OWNER_CREDIT_ROUTES:
    refused = client.request(method, path, json=body, headers=headers)
    assert refused.status_code == 403, (path, refused.text)
  assert len(paired.calls) == calls
  assert credits_client.load_credentials()["key"] == "mbc_example_key"


@pytest.mark.parametrize("service_scope", [None, "private"], ids=["frame", "service"])
def test_an_app_approved_to_manage_credits_reaches_them(
  client, auth, db, paired, service_scope
):
  app = _app(db, "credits-manager", spend_credits=(), credits_manage=True)
  headers = _as(db, app, service_scope)
  paired.answers["balance"] = (200, {"balance": "2.25"})
  read = client.get("/api/identity/credits", headers=headers)
  assert read.status_code == 200 and read.json()["balance"] == {"balance": "2.25"}
  for method, path, body in OWNER_CREDIT_ROUTES:
    if path.endswith(("/pair", "/pair/account", "/unpair", "/topup", "/connect")):
      continue  # Their answers depend on the service's page; covered above.
    answer = client.request(method, path, json=body, headers=headers)
    assert answer.status_code == 200, (path, answer.text)


# Moving money is an installation-owner control; seeing it is the owner's own.
OWNER_ACTION_ROUTES = [route for route in OWNER_CREDIT_ROUTES if route[0] != "GET"]
OWNER_PRIVATE_ROUTES = [
  route for route in OWNER_CREDIT_ROUTES if not route[1].endswith("/prices/ana")]


def _guest_lineage(db):
  owner = db.query(models.Owner).first()
  grant, invitation = link_grant(db, owner, "Guest")
  _secret, session, _grant, _owner = redeem_invitation(db, invitation)
  return BrowserLineage(grant.id, session.id)


def _guest(db, app=None):
  """A shared-browser guest's bearer, or the bearer of an app it opened."""
  owner = db.query(models.Owner).first()
  lineage = _guest_lineage(db)
  if app is None:
    token = create_access_token(
      {"sub": owner.username}, token_epoch=owner.token_epoch, browser=lineage)
  else:
    token = create_app_token(app.id, owner.username, owner.token_epoch, app.token_nonce,
                             consent=app_consent.session_id(app), browser=lineage)
  return {"Authorization": f"Bearer {token}"}


def _delegated_helper(db, app=None):
  """A delegated helper's bearer, or the bearer of an app it opened."""
  owner = db.query(models.Owner).first()
  helper = {"delegation_id": "credits-helper", "delegation_chat": "credits-helper-chat"}
  if app is None:
    token = create_agent_token("credits-helper-chat", owner.username, owner.token_epoch,
                               **helper)
  else:
    token = create_app_token(app.id, owner.username, owner.token_epoch, app.token_nonce,
                             consent=app_consent.session_id(app), **helper)
  return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("through_app", [False, True], ids=["owner-bearer", "credits-app"])
def test_a_delegated_helper_cannot_move_the_owners_credits(
  client, auth, db, paired, through_app
):
  app = _app(db, "credits-manager", spend_credits=(), credits_manage=True)
  headers = _delegated_helper(db, app if through_app else None)
  # It acts with its parent's tools, so it can still read the balance.
  assert client.get("/api/identity/credits", headers=headers).status_code == 200
  calls = len(paired.calls)
  for method, path, body in OWNER_ACTION_ROUTES:
    refused = client.request(method, path, json=body, headers=headers)
    assert refused.status_code == 403, (path, refused.text)
  assert len(paired.calls) == calls
  assert credits_client.load_credentials()["key"] == "mbc_example_key"


@pytest.mark.parametrize("through_app", [False, True], ids=["owner-bearer", "credits-app"])
def test_a_shared_browser_guest_cannot_see_or_move_the_owners_credits(
  client, auth, db, paired, through_app
):
  app = _app(db, "credits-manager", spend_credits=(), credits_manage=True)
  headers = _guest(db, app if through_app else None)
  calls = len(paired.calls)
  for method, path, body in OWNER_PRIVATE_ROUTES:
    refused = client.request(method, path, json=body, headers=headers)
    assert refused.status_code == 403, (path, refused.text)
  assert len(paired.calls) == calls
  assert credits_client.load_credentials()["key"] == "mbc_example_key"
  # The same bearer is otherwise live: the refusal is the credits rule.
  assert client.get("/api/identity/credits/prices/ana", headers=headers).status_code == 200


def test_every_credits_route_has_exactly_one_gate():
  """A route added without its gate would be open to every app; one with two
  gates would mean one of them is not saying the whole rule."""
  from app.routes import credits
  gates = {deps.get_credits_reader, deps.get_credits_controller,
           deps.get_credits_spender, deps.get_credits_price_reader}
  routes = [route for route in credits.router.routes if hasattr(route, "dependant")]
  assert len(routes) == len(OWNER_CREDIT_ROUTES) + 1  # and the app charge
  for route in routes:
    used = {dep.call for dep in route.dependant.dependencies} & gates
    assert len(used) == 1, (route.path, used)


# --- One app's idempotency key never replays another's ---------------------------


def test_two_apps_using_the_same_key_are_two_charges_upstream(client, auth, db, paired):
  store_app = _app(db, "store-payer", package_id="pkg-store-payer")
  unpackaged_app = _app(db, "unpackaged-payer")
  paired.answers["charges"] = (200, {"charge_id": "c1"})
  sent = []
  for app in (store_app, unpackaged_app, store_app):
    answer = client.post("/api/identity/credits/charges", json=CHARGE,
                         headers=_as(db, app, "private"))
    assert answer.status_code == 200, answer.text
    sent.append(paired.calls[-1]["json"]["idempotency_key"])
  # Different apps: different upstream keys. The same app retrying: the same.
  assert sent[0] != sent[1]
  assert sent[0] == sent[2]
  assert CHARGE["idempotency_key"] not in sent


def test_a_scoped_key_stays_within_the_services_key_limits():
  for key in ("k" * 8, "x" * 200, "ünïcode-key/with:separators"):
    scoped = credits_client.app_scoped_key("package:" + "p" * 128, key)
    assert 8 <= len(scoped) <= 200 and scoped.isascii()
  assert credits_client.app_scoped_key("app:1", "run-0001") != \
    credits_client.app_scoped_key("app:2", "run-0001")


# --- Purchase terms belong to one pairing --------------------------------------


def test_pairing_again_with_another_key_changes_the_pairing(client, auth, paired):
  first = client.get("/api/identity/credits", headers=auth).json()["pairing"]
  assert first and client.get("/api/identity/credits", headers=auth).json()["pairing"] == first
  credits_client.pair("https://www.mobius.you/credits/", "mbc_another_key")
  second = client.get("/api/identity/credits", headers=auth).json()["pairing"]
  assert second and second != first
  credits_client.pair("https://credits.example.test", "mbc_another_key")
  assert client.get("/api/identity/credits", headers=auth).json()["pairing"] not in (
    first, second)
  assert "mbc_" not in second


def test_a_purchase_on_terms_from_an_earlier_pairing_buys_nothing(client, auth, paired):
  paired.answers["purchase-terms"] = (200, {"currency": "gbp", "amounts_cents": [500]})
  shown = client.get("/api/identity/credits/purchase-terms", headers=auth).json()
  credits_client.pair("https://credits.example.test", "mbc_new_service_key")
  calls = len(paired.calls)
  refused = client.post("/api/identity/credits/topup", headers=auth, json={
    "amount_cents": 500, "pairing": shown["pairing"]})
  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "pairing_changed"
  assert len(paired.calls) == calls

  paired.answers["topups"] = (200, {
    "checkout_url": "https://checkout.stripe.com/c/pay/cs_1", "topup_id": TOPUP_ID})
  current = client.get("/api/identity/credits/purchase-terms", headers=auth).json()
  started = client.post("/api/identity/credits/topup", headers=auth, json={
    "amount_cents": 500, "pairing": current["pairing"]})
  assert started.status_code == 200, started.text
  assert paired.calls[-1]["url"] == "https://credits.example.test/api/billing/topups"


# --- Path pieces cannot turn a public price read into a private account read ----


@pytest.mark.parametrize("segment", ["%2e", "%2e%2e", "Ana", "ana.name"])
@pytest.mark.parametrize("service_scope", [None, "private"], ids=["frame", "service"])
def test_a_spending_app_cannot_escape_the_sellers_price_path(
  client, auth, db, paired, segment, service_scope,
):
  app = _app(db, "price-reader")
  calls = len(paired.calls)
  refused = client.get(f"/api/identity/credits/prices/{segment}",
                       headers=_as(db, app, service_scope))
  assert refused.status_code == 422, refused.text
  assert refused.json()["detail"]["code"] == "invalid_name"
  assert len(paired.calls) == calls


@pytest.mark.parametrize("piece", [".", "..", "../balance", "%2e%2e", "name?x=y",
                                  "name#balance", "name\\balance", "name\n", ""])
@pytest.mark.parametrize("field", ["seller", "topup_id", "app", "action", "app_slug"])
def test_invalid_credits_names_never_send_the_installation_key(paired, piece, field):
  calls = len(paired.calls)
  with pytest.raises(credits_client.CreditsUnavailable) as refused:
    if field == "seller":
      credits_client.prices(piece)
    elif field == "topup_id":
      credits_client.read_topup(piece)
    elif field == "app_slug":
      credits_client.prices("ana", piece)
    else:
      credits_client.publish_price(piece if field == "app" else "prompts",
                                   piece if field == "action" else "run", 1, None, True)
  assert refused.value.status == 422 and refused.value.code == "invalid_name"
  assert len(paired.calls) == calls


@pytest.mark.parametrize("field,piece", [
  ("seller", "a" * 41), ("seller", "Ana"),
  ("app", "a" * 81), ("app", "Prompts"), ("action", "Run"),
  ("topup_id", "not-a-uuid"),
])
def test_credits_names_use_the_services_handle_slug_and_id_formats(paired, field, piece):
  calls = len(paired.calls)
  with pytest.raises(credits_client.CreditsUnavailable) as refused:
    if field == "seller":
      credits_client.prices(piece)
    elif field == "topup_id":
      credits_client.read_topup(piece)
    else:
      credits_client.publish_price(piece if field == "app" else "prompts",
                                   piece if field == "action" else "run", 1, None, True)
  assert refused.value.status == 422
  assert len(paired.calls) == calls


def test_valid_credits_names_keep_the_service_path(paired):
  credits_client.read_topup(TOPUP_ID)
  assert paired.calls[-1]["url"].endswith(f"/topups/{TOPUP_ID}")
  credits_client.prices("a" * 40, "a" + "._-9" * 19 + "xyz")
  assert paired.calls[-1]["url"].endswith(
    f"/prices/{'a' * 40}?app_slug={'a' + '._-9' * 19 + 'xyz'}")
  credits_client.publish_price("app.v2_3-4", "run.v2_3-4", 1, None, True)
  assert paired.calls[-1]["url"].endswith("/priced-actions/app.v2_3-4/run.v2_3-4")


@pytest.mark.parametrize("service_scope", [None, "private"], ids=["frame", "service"])
def test_a_consented_credits_manager_can_pair_and_unpair(
  client, auth, db, paired, service_scope,
):
  app = _app(db, "credits-manager", spend_credits=(), credits_manage=True)
  headers = _as(db, app, service_scope)
  answer = client.post("/api/identity/credits/pair", headers=headers, json={
    "operator_url": "https://credits.example", "key": "mbc_replacement"})
  assert answer.status_code == 200, answer.text
  assert credits_client.load_credentials()["operator_url"] == "https://credits.example"
  assert credits_client.load_credentials()["key"] == "mbc_replacement"
  assert client.post("/api/identity/credits/unpair", headers=headers).status_code == 200
  assert credits_client.load_credentials() is None


# --- An upstream server error is not proof that a charge failed -----------------


UNKNOWN_CHARGE = {
  "code": "charge_outcome_unknown",
  "message": "The charge outcome is unknown. Retry with the same idempotency key.",
}


@pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
@pytest.mark.parametrize("body", ["Bad Gateway", {"detail": {
  "code": "upstream_failed", "message": "Server failed.", "extra": "not a refusal"}}])
def test_upstream_charge_failures_require_retrying_the_same_key(
  client, auth, db, paired, status, body,
):
  app = _app(db, "granted")
  headers = _as(db, app, "private")
  paired.answers["charges"] = (status, body)
  calls = len(paired.calls)
  unknown = client.post("/api/identity/credits/charges", json=CHARGE, headers=headers)
  assert unknown.status_code == 503, unknown.text
  assert unknown.json()["detail"] == UNKNOWN_CHARGE
  assert len(paired.calls) == calls + 1  # The client never retries a charge itself.
  first = paired.calls[-1]["json"]

  paired.answers["charges"] = (200, {"charge_id": "c1", "replayed": True})
  retried = client.post("/api/identity/credits/charges", json=CHARGE, headers=headers)
  assert retried.status_code == 200 and retried.json()["replayed"] is True
  assert paired.calls[-1]["json"] == first


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout,
                                  httpx.RemoteProtocolError])
def test_charge_transport_failures_report_an_unknown_outcome(
  client, auth, db, paired, monkeypatch, error,
):
  app = _app(db, "granted")

  def fail(*_args, **_kwargs):
    raise error("Lost the response")

  monkeypatch.setattr(credits_client.httpx, "request", fail)
  unknown = client.post("/api/identity/credits/charges", json=CHARGE,
                        headers=_as(db, app, "private"))
  assert unknown.status_code == 503
  assert unknown.json()["detail"] == UNKNOWN_CHARGE


def test_an_unreadable_charge_receipt_also_has_an_unknown_outcome(client, auth, db, paired):
  app = _app(db, "granted")
  paired.answers["charges"] = (200, "not a receipt")
  unknown = client.post("/api/identity/credits/charges", json=CHARGE,
                        headers=_as(db, app, "private"))
  assert unknown.status_code == 503
  assert unknown.json()["detail"] == UNKNOWN_CHARGE


def test_a_definite_missing_charge_endpoint_is_not_an_unknown_charge(
  client, auth, db, paired,
):
  app = _app(db, "granted")
  paired.answers["charges"] = (404, {"detail": "Not Found"})
  answer = client.post("/api/identity/credits/charges", json=CHARGE,
                       headers=_as(db, app, "private"))
  assert answer.status_code == 502
  assert answer.json()["detail"]["code"] == "not_found"


@pytest.mark.parametrize("status", [500, 502, 504])
def test_noncharge_server_errors_keep_the_service_refusal(client, auth, paired, status):
  detail = {"code": "upstream_failed", "message": "Server failed."}
  paired.answers["purchase-terms"] = (status, {"detail": detail})
  answer = client.get("/api/identity/credits/purchase-terms", headers=auth)
  assert answer.status_code == status
  assert answer.json()["detail"] == detail


@pytest.mark.parametrize("path,heading", [
  ("CAPABILITIES.md", "### Paying for priced calls with credits"),
  ("backend/scripts/seed-skills/building-apps.md", "## Priced calls paid with credits"),
])
def test_credits_docs_use_the_supported_pairing_flow(path, heading):
  document = (Path(__file__).resolve().parents[2] / path).read_text()
  section = document.split(heading, 1)[1].split("\n##", 1)[0]
  assert "POST /api/identity/credits/pair" in section
  assert "owner-only" not in section
  assert "credits_manage" in section and "including frames" in section
  assert "charge_outcome_unknown" in section and "5xx" in section
  assert "Settings connect" in section and "planned" in section
  assert "not_paired" in section and "has not connected credits yet" in section
  assert "key_refused" in section and "invalid_api_key" in section
  assert "Möbius · You" not in section
