"""A Möbius paired with a credits service: pairing, reads, and app charges."""

import httpx
import pytest
from fastapi import HTTPException

from app import credits_client, deps, models
from app.auth import create_app_token
from app.browser_access import BrowserLineage
from app.config import get_settings


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


def _app(db, slug, *, spend_credits=("ana/prompts",)):
  row = models.App(
    slug=slug, source_dir=f"/tmp/mobius-tests/{slug}", name=slug,
    description="", jsx_source="export default function App(){}",
    compiled_path="/tmp/app.js",
    capability_contract={"data": {"spend_credits": list(spend_credits)}},
  )
  db.add(row)
  db.commit()
  db.refresh(row)
  return row


def _as(db, app, service):
  owner = db.query(models.Owner).first()
  token = create_app_token(app.id, owner.username, owner.token_epoch, app.token_nonce,
                           service=service)
  return {"Authorization": f"Bearer {token}"}


CHARGE = {"seller": "ana", "app": "prompts", "action": "run",
          "idempotency_key": "run-00000001", "max_price_micro": 500_000}


def test_an_unpaired_installation_is_told_so(client, auth, service):
  body = client.get("/api/identity/credits", headers=auth).json()
  assert body["mode"] == "unpaired" and body["balance"] is None
  assert not service.calls


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
])
def test_pairing_refuses_a_wrong_key_or_plain_http(client, auth, service, url, key):
  refused = client.post("/api/identity/credits/pair",
                        json={"operator_url": url, "key": key}, headers=auth)
  assert refused.status_code == 422 and not service.calls


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
  paired.answers["topups"] = (200, {"topup_id": "t1", "checkout_url": "https://checkout"})
  started = client.post("/api/identity/credits/topup", headers=auth, json={
    "amount_cents": 1000, "waiver_accepted": True, "return_url": "https://me.example/x"})
  assert started.json() == {"checkout_url": "https://checkout", "topup_id": "t1"}
  assert paired.calls[-1]["json"] == {
    "amount_cents": 1000, "waiver_accepted": True, "return_url": "https://me.example/x"}

  requests = [
    ("get", "/api/identity/credits/topups/t1", None, "GET", "topups/t1"),
    ("get", "/api/identity/credits/transactions?limit=500", None,
     "GET", "transactions?limit=200"),
    ("post", "/api/identity/credits/model-funding",
     {"amount_micro": 5_000_000, "idempotency_key": "fund-0001"}, "POST", "model-funding"),
    ("get", "/api/identity/credits/prices/ana?app_slug=prompts", None,
     "GET", "prices/ana?app_slug=prompts"),
    ("put", "/api/identity/credits/prices/prompts/run", {"price_micro": 250_000},
     "PUT", "priced-actions/prompts/run"),
    ("post", "/api/identity/credits/connect", {"return_url": "https://me.example/y"},
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
  assert paired.calls[-4]["json"] == {"return_url": "https://me.example/y"}


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
  assert paired.calls[-1]["json"] == {**CHARGE, "buyer_app": "granted"}


def test_an_app_pays_only_the_sellers_its_grant_lists(client, auth, db, paired):
  app = _app(db, "granted")
  calls = len(paired.calls)
  for other in ({"seller": "bob"}, {"app": "other-app"}):
    refused = client.post("/api/identity/credits/charges", json={**CHARGE, **other},
                          headers=_as(db, app, "private"))
    assert refused.status_code == 403
    assert refused.json()["detail"]["code"] == "seller_not_allowed"
  assert len(paired.calls) == calls


def test_a_shared_browser_guest_cannot_read_the_owners_balance_or_history(db):
  from app.routes import identity
  owner = db.query(models.Owner).first()
  guest = deps.Principal(owner=owner, app_id=None, browser=BrowserLineage("grant-1"))
  with pytest.raises(HTTPException) as refused:
    identity._refuse_shared_browser_guest(principal=guest)
  assert refused.value.status_code == 403
  private = {"/credits", "/credits/transactions", "/credits/topups/{topup_id}"}
  routes = {route.path.removeprefix(identity.router.prefix): route
            for route in identity.router.routes if hasattr(route, "dependant")}
  for path in private:
    calls = [dep.call for dep in routes[path].dependant.dependencies]
    assert identity._refuse_shared_browser_guest in calls, path


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
