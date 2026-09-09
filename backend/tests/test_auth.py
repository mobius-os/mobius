"""Tests for authentication flow."""

import base64
import hashlib
import json
import time
import urllib.parse
from datetime import timedelta

import bcrypt
from test_app_fixtures import create_local_app


def configure_managed_sso(monkeypatch):
  from app.config import get_settings

  settings = get_settings()
  monkeypatch.setattr(settings, "mobius_sso_issuer", "http://launcher.test")
  monkeypatch.setattr(settings, "mobius_sso_instance_id", "mob_testinstance")
  monkeypatch.setattr(settings, "mobius_sso_client_secret", "s" * 48)
  monkeypatch.setattr(settings, "frontend_origin", "http://testserver")
  return settings


def _mobius_login_handoff(db, *, epoch=0):
  from app import auth as auth_service, models
  from app.timeutil import now_naive_utc

  owner = models.Owner(
    username="mobius-owner",
    hashed_password="unused",
    auth_mode="mobius",
    sso_subject="mobius-subject",
    token_epoch=epoch,
  )
  db.add(owner)
  db.flush()
  jti = "test-mobius-login-handoff"
  db.add(models.MobiusLoginHandoffGrant(
    token_hash=hashlib.sha256(jti.encode()).hexdigest(),
    owner_id=owner.id,
    owner_epoch=epoch,
    expires_at=now_naive_utc() + timedelta(seconds=60),
  ))
  db.commit()
  token = auth_service.create_access_token({
    "scope": "mobius_login_handoff",
    "sub": owner.username,
    "jti": jti,
  }, expires_delta=timedelta(seconds=60))
  return owner, token



def test_setup_creates_owner(client):
  r = client.post("/api/auth/setup", json={
    "username": "admin",
    "password": "securepassword123",
  })
  assert r.status_code == 200
  assert "access_token" in r.json()


def test_managed_sso_handoff_cannot_cross_into_shell_install_protocol(client):
  from app import auth as auth_service

  handoff = auth_service.create_access_token({"scope": "mobius_sso_handoff"})
  client.cookies.set(
    "mobius_shell_install",
    handoff,
    path="/api/auth/shell-install-pass/redeem",
  )

  assert client.post("/api/auth/shell-install-pass/redeem").status_code == 401


def test_self_hosted_setup_status_stays_local(client):
  status = client.get("/api/auth/setup/status")

  assert status.status_code == 200
  assert status.json() == {"configured": False, "auth_mode": "local"}


def test_managed_mode_closes_local_first_owner_setup(client, monkeypatch):
  configure_managed_sso(monkeypatch)

  status = client.get("/api/auth/setup/status")
  setup = client.post("/api/auth/setup", json={
    "username": "attacker",
    "password": "not-the-owner",
  })

  assert status.json() == {"configured": False, "auth_mode": "mobius"}
  assert setup.status_code == 403
  assert "Managed sign-in" in setup.json()["detail"]


def test_setup_rejects_duplicate(client):
  client.post("/api/auth/setup", json={
    "username": "admin",
    "password": "securepassword123",
  })
  # Owner already exists, so a second setup is rejected.
  r = client.post("/api/auth/setup", json={
    "username": "admin2",
    "password": "anotherpassword",
  })
  assert r.status_code == 400


def test_mobius_login_handoff_is_durably_one_use(client, db):
  from app import models

  _owner, handoff = _mobius_login_handoff(db)
  client.cookies.set(
    "mobius_login_handoff",
    handoff,
    path="/api/auth/mobius/login/session",
  )

  first = client.post("/api/auth/mobius/login/session")
  assert first.status_code == 200
  assert "access_token" in first.json()

  # Restore the captured cookie: the browser deletes its copy after success,
  # but a replay must still fail from durable server-side state.
  client.cookies.set(
    "mobius_login_handoff",
    handoff,
    path="/api/auth/mobius/login/session",
  )
  replay = client.post("/api/auth/mobius/login/session")
  assert replay.status_code == 401
  db.expire_all()
  grant = db.query(models.MobiusLoginHandoffGrant).one()
  assert grant.consumed_at is not None


def test_mobius_login_handoff_honors_owner_epoch_revocation(client, db):
  owner, handoff = _mobius_login_handoff(db)
  owner.token_epoch += 1
  db.commit()
  client.cookies.set(
    "mobius_login_handoff",
    handoff,
    path="/api/auth/mobius/login/session",
  )

  response = client.post("/api/auth/mobius/login/session")

  assert response.status_code == 401


def test_mobius_login_secret_cannot_cross_into_shell_install_protocol(client, db):
  from app import auth as auth_service

  _owner, handoff = _mobius_login_handoff(db)
  jti = auth_service.decode_access_token(handoff)["jti"]
  client.cookies.set(
    "mobius_shell_install",
    jti,
    path="/api/auth/shell-install-pass/redeem",
  )

  assert client.post("/api/auth/shell-install-pass/redeem").status_code == 401


def test_login_success(client):
  client.post("/api/auth/setup", json={
    "username": "admin",
    "password": "securepassword123",
  })
  r = client.post("/api/auth/token", data={
    "username": "admin",
    "password": "securepassword123",
  })
  assert r.status_code == 200
  assert "access_token" in r.json()


def test_login_wrong_password(client):
  client.post("/api/auth/setup", json={
    "username": "admin",
    "password": "securepassword123",
  })
  r = client.post("/api/auth/token", data={
    "username": "admin",
    "password": "wrongpassword",
  })
  assert r.status_code == 401


def test_login_upgrades_legacy_hash(client, db):
  """A successful legacy login migrates the authoritative owner credential."""
  from app import auth, models

  password = "a" * 100
  legacy_hash = bcrypt.hashpw(
    password.encode()[:72], bcrypt.gensalt(rounds=4)
  ).decode()
  db.add(models.Owner(username="legacy", hashed_password=legacy_hash))
  db.commit()

  response = client.post("/api/auth/token", data={
    "username": "legacy",
    "password": password,
  })

  assert response.status_code == 200
  db.expire_all()
  stored = db.query(models.Owner).filter_by(username="legacy").one()
  assert stored.hashed_password.startswith(auth.PASSWORD_HASH_PREFIX)
  assert auth.verify_password(password, stored.hashed_password) is True


def test_provider_login_rejects_cross_site_request(client, auth):
  cross = client.post(
    "/api/auth/provider/login",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_provider_code_rejects_cross_site_request(client, auth):
  cross = client.post(
    "/api/auth/provider/code",
    json={"code": "abc123"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_codex_provider_login_rejects_cross_site_request(client, auth):
  cross = client.post(
    "/api/auth/provider/codex/login",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_protected_route_requires_token(client):
  r = client.get("/api/apps/")
  assert r.status_code in (401, 403)


def test_protected_route_with_valid_token(client, owner_token):
  r = client.get("/api/apps/", headers={
    "Authorization": f"Bearer {owner_token}",
  })
  assert r.status_code == 200


def test_providers_models_requires_auth(client):
  """The mini-app model endpoint still rejects anonymous callers."""
  r = client.get("/api/auth/providers/models")
  assert r.status_code in (401, 403)


def test_providers_models_accepts_app_token(client, auth):
  """App-scoped JWTs (minted for the news Settings tab and the future
  Reflection Settings tab) must read the full
  model list — otherwise the picker silently falls back to one model
  per provider. The endpoint is read-only and the same list is
  already visible to every running mini-app via the CLI runtime,
  so loosening the auth here doesn't widen the surface."""
  # Need a real App row for the app-scoped JWT to resolve.
  app_id = create_local_app(
    client, auth, name="Picker host", description="x",
  )["id"]

  from app.auth import create_access_token
  from app.providers import DEFAULT_VISIBLE_MODELS, invalidate_model_cache
  invalidate_model_cache()
  app_token = create_access_token({
    "sub": "test", "scope": "app", "app_id": app_id,
  })
  r = client.get(
    "/api/auth/providers/models",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 200, r.text
  body = r.json()
  # The same curated defaults the owner sees, not a one-model fallback stub.
  assert {m["id"] for m in body["claude"]} == DEFAULT_VISIBLE_MODELS["claude"]
  assert {m["id"] for m in body["codex"]} == DEFAULT_VISIBLE_MODELS["codex"]
  assert len(body["claude"]) > 1 and len(body["codex"]) > 1


def test_providers_status_accepts_app_token(client, auth):
  """Mini-app setup screens need provider connection status with the same
  app-scoped token they use for the model registry."""
  app_id = create_local_app(
    client, auth, name="Status host", description="x",
  )["id"]

  from app.auth import create_access_token
  app_token = create_access_token({
    "sub": "test", "scope": "app", "app_id": app_id,
  })
  r = client.get(
    "/api/auth/providers/status",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 200, r.text
  body = r.json()
  assert "claude" in body
  assert "codex" in body
  assert "configured" in body["claude"]
  assert "authenticated" in body["claude"]
  assert body["claude"]["configured"] is body["claude"]["authenticated"]
  assert body["mobius"]["available"] is False
  assert body["mobius"]["configured"] is False


def test_mobius_provider_is_available_only_with_identity_app_installed(
  client, auth,
):
  from app import models
  from app.database import SessionLocal

  absent = client.get("/api/auth/providers/status", headers=auth).json()
  assert absent["mobius"]["available"] is False

  app = create_local_app(
    client, auth, name="Möbius · You", description="Account",
  )
  with SessionLocal() as session:
    row = session.query(models.App).filter(models.App.id == app["id"]).one()
    row.slug = "identity"
    session.commit()

  installed = client.get("/api/auth/providers/status", headers=auth).json()
  assert installed["mobius"]["available"] is True


def test_providers_status_hides_mobius_trial_from_app_principals(
  client, auth, monkeypatch,
):
  """The owner's Möbius trial balance is owner-only. App/embed principals share
  the endpoint for model-picker availability, so they must see the same
  provider fields without the owner's credit units and grant expiries."""
  from app import models
  from app.database import SessionLocal
  from app.providers import MobiusProvider

  # Install the identity app so mobius reports as available.
  app = create_local_app(
    client, auth, name="Möbius · You", description="Account",
  )
  with SessionLocal() as session:
    row = session.query(models.App).filter(models.App.id == app["id"]).one()
    row.slug = "identity"
    session.commit()

  # Fake a linked subscription carrying a real trial balance.
  balance = {
    "spendable_units": 500,
    "grants": [{"amount": 500, "expires_at": "2026-12-31"}],
  }
  monkeypatch.setattr(MobiusProvider, "check_auth", lambda self, data_dir: None)
  monkeypatch.setattr(MobiusProvider, "trial_status", lambda self: balance)

  # The owner sees the trial balance.
  owner_body = client.get("/api/auth/providers/status", headers=auth).json()
  assert owner_body["mobius"]["available"] is True
  assert owner_body["mobius"]["trial"] == balance

  # An app-scoped principal gets availability but never the balance.
  from app.auth import create_access_token
  app_token = create_access_token(
    {"sub": "test", "scope": "app", "app_id": app["id"]},
  )
  app_body = client.get(
    "/api/auth/providers/status",
    headers={"Authorization": f"Bearer {app_token}"},
  ).json()
  assert app_body["mobius"]["available"] is True
  assert app_body["mobius"]["configured"] is True
  assert "trial" not in app_body["mobius"]


def test_provider_status_exposes_configured_with_legacy_alias(client, auth):
  r = client.get("/api/auth/provider/status", headers=auth)

  assert r.status_code == 200, r.text
  body = r.json()
  assert body["configured"] is body["authenticated"]


def test_providers_status_rejects_empty_claude_oauth_record(
  client, auth, tmp_path, monkeypatch,
):
  """A leftover credential shell is not a connected Claude session.

  Recovery can preserve the scopes/email metadata while clearing unusable
  tokens. File-presence checks used to report that state as connected even
  though the next turn deterministically failed authentication.
  """
  from app.config import get_settings

  data_dir = tmp_path / "data"
  creds = data_dir / "cli-auth" / "claude" / ".credentials.json"
  creds.parent.mkdir(parents=True)
  creds.write_text(json.dumps({
    "claudeAiOauth": {
      "expiresAt": 0,
      "scopes": ["user:inference"],
    },
  }))
  monkeypatch.setattr(get_settings(), "data_dir", str(data_dir))

  r = client.get("/api/auth/providers/status", headers=auth)

  assert r.status_code == 200, r.text
  status = r.json()["claude"]
  assert status["configured"] is False
  assert status["authenticated"] is False
  assert "reconnect" in status["error"].lower()


def test_claude_auth_accepts_current_access_or_refreshable_session(tmp_path):
  from app.providers import ClaudeProvider

  creds = tmp_path / "cli-auth" / "claude" / ".credentials.json"
  creds.parent.mkdir(parents=True)

  creds.write_text(json.dumps({
    "claudeAiOauth": {
      "accessToken": "current-token",
      "expiresAt": int(time.time() * 1000) + 120_000,
    },
  }))
  assert ClaudeProvider().check_auth(str(tmp_path)) is None

  # Older Claude credential documents omit refreshTokenExpiresAt.
  creds.write_text(json.dumps({
    "claudeAiOauth": {
      "accessToken": "expired-token",
      "refreshToken": "refresh-token",
      "expiresAt": 0,
    },
  }))
  assert ClaudeProvider().check_auth(str(tmp_path)) is None

  creds.write_text(json.dumps({
    "claudeAiOauth": {
      "accessToken": "expired-token",
      "refreshToken": "refresh-token",
      "expiresAt": 0,
      "refreshTokenExpiresAt": int(time.time() * 1000) + 120_000,
    },
  }))
  assert ClaudeProvider().check_auth(str(tmp_path)) is None


def test_claude_auth_rejects_expired_unrefreshable_session(tmp_path):
  from app.providers import ClaudeProvider

  creds = tmp_path / "cli-auth" / "claude" / ".credentials.json"
  creds.parent.mkdir(parents=True)
  creds.write_text(json.dumps({
    "claudeAiOauth": {
      "accessToken": "expired-token",
      "expiresAt": 0,
    },
  }))

  error = ClaudeProvider().check_auth(str(tmp_path))

  assert error is not None
  assert "reconnect" in error.lower()

  creds.write_text(json.dumps({
    "claudeAiOauth": {
      "accessToken": "expired-token",
      "refreshToken": "expired-refresh-token",
      "expiresAt": 0,
      "refreshTokenExpiresAt": 0,
    },
  }))

  error = ClaudeProvider().check_auth(str(tmp_path))

  assert error is not None
  assert "reconnect" in error.lower()


def test_claude_auth_rejects_malformed_credentials(tmp_path):
  from app.providers import ClaudeProvider

  creds = tmp_path / "cli-auth" / "claude" / ".credentials.json"
  creds.parent.mkdir(parents=True)
  creds.write_text("{not-json")

  error = ClaudeProvider().check_auth(str(tmp_path))

  assert error is not None
  assert "reconnect" in error.lower()


def test_providers_status_rejects_non_utf8_claude_credentials(
  client, auth, tmp_path, monkeypatch,
):
  from app.config import get_settings

  data_dir = tmp_path / "data"
  creds = data_dir / "cli-auth" / "claude" / ".credentials.json"
  creds.parent.mkdir(parents=True)
  creds.write_bytes(b"\xff\xfe")
  monkeypatch.setattr(get_settings(), "data_dir", str(data_dir))

  r = client.get("/api/auth/providers/status", headers=auth)

  assert r.status_code == 200, r.text
  status = r.json()["claude"]
  assert status["configured"] is False
  assert status["authenticated"] is False
  assert "reconnect" in status["error"].lower()


def test_providers_models_returns_known_models_on_missing_creds(
  client, auth, monkeypatch,
):
  """Without real Anthropic / Codex credentials the underlying
  `list_models` falls back to KNOWN_MODELS — exercise that path and
  pin the response shape mini-apps depend on (id + name, plus a
  tier on Claude rows)."""
  from app import providers
  from app.providers import DEFAULT_VISIBLE_MODELS, KNOWN_MODELS, invalidate_model_cache
  real_fetch = providers._fetch_provider_models

  async def missing_credentials_fetch(provider_id, data_dir):
    if provider_id == "mobius":
      raise RuntimeError("Möbius broker unavailable")
    return await real_fetch(provider_id, data_dir)

  monkeypatch.setattr(providers, "_fetch_provider_models", missing_credentials_fetch)
  invalidate_model_cache()
  r = client.get("/api/auth/providers/models", headers=auth)
  assert r.status_code == 200
  body = r.json()
  assert set(body) == {"claude", "codex", "mobius"}
  claude_ids = [m["id"] for m in body["claude"]]
  assert claude_ids == [
    "claude-fable-5-1",
    "claude-fable-5", "claude-sonnet-5",
    "claude-opus-4-8", "claude-sonnet-4-6",
  ]
  codex_ids = [m["id"] for m in body["codex"]]
  assert codex_ids == [
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
  ]
  assert set(claude_ids) == DEFAULT_VISIBLE_MODELS["claude"]
  assert set(codex_ids) == DEFAULT_VISIBLE_MODELS["codex"]
  assert [m["id"] for m in body["mobius"]] == ["spark", "inkling"]
  assert [m["name"] for m in body["mobius"]] == ["Spark", "Evolve"]
  # Claude rows carry a tier derived from the id.
  by_id = {m["id"]: m for m in body["claude"]}
  assert by_id["claude-opus-4-8"]["name"] == "claude-opus-4-8"
  assert by_id["claude-opus-4-8"]["tier"] == "opus"
  assert by_id["claude-sonnet-4-6"]["tier"] == "sonnet"
  # Codex rows intentionally omit `tier` — the field doesn't apply.
  for row in body["codex"]:
    assert "tier" not in row
    assert "id" in row and "name" in row
  assert [m["name"] for m in body["mobius"]] == ["Spark", "Evolve"]
  # `available` / `provider` from the shell-facing /api/models response
  # are NOT leaked through; mini-apps see only id + name (+ tier).
  for rows in body.values():
    for row in rows:
      assert set(row).issubset({"id", "name", "tier"})


def test_self_host_mobius_callback_uses_single_use_broker_state_without_auth_header(
  client, auth, monkeypatch,
):
  from app.routes import auth as auth_routes

  saved = {}
  broker_calls = []
  instance_id = "mob_self_testinstance"

  async def broker_request(method, route, payload=None):
    broker_calls.append((method, route, payload))
    if route == "/identity":
      return {
        "linked": False,
        "instance_id": instance_id,
        "public_key_jwk": {"kty": "OKP", "crv": "Ed25519", "x": "x" * 43},
        "key_thumbprint": "a" * 64,
      }
    if route == "/identity/oauth/start":
      saved.update(payload)
      return {"saved": True}
    if route == "/identity/oauth/consume":
      if payload["state"] != saved.get("state"):
        return {"pending": None}
      value = dict(saved)
      saved.clear()
      return {"pending": value}
    if route == "/identity/enroll":
      return {"linked": True}
    raise AssertionError(route)

  receipt_payload = base64.urlsafe_b64encode(json.dumps({
    "sub": "mobius-test-account",
    "instance_id": instance_id,
    "iss": auth_routes._MOBIUS_IDENTITY_ISSUER,
    "aud": auth_routes._MOBIUS_RECEIPT_AUDIENCE,
    "exp": time.time() + 300,
  }).encode()).decode().rstrip("=")

  class ExchangeResponse:
    def raise_for_status(self):
      return None

    def json(self):
      return {"enrollment_receipt": f"header.{receipt_payload}.signature"}

  class ExchangeClient:
    def __init__(self, *args, **kwargs):
      pass

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def post(self, *_args, **_kwargs):
      return ExchangeResponse()

  monkeypatch.setattr(auth_routes, "_mobius_broker_request", broker_request)
  start = client.post("/api/auth/provider/mobius/login", headers=auth)
  assert start.status_code == 200
  authorization_url = urllib.parse.urlparse(start.json()["authorization_url"])
  state = urllib.parse.parse_qs(authorization_url.query)["state"][0]
  monkeypatch.setattr(auth_routes.httpx, "AsyncClient", ExchangeClient)

  # A top-level browser redirect has no localStorage bearer header. State is
  # the one-use callback credential and is consumed by the root broker.
  callback = client.get(
    "/api/auth/provider/mobius/callback",
    params={"code": "central-code", "state": state},
    follow_redirects=False,
  )
  assert callback.status_code == 303
  assert callback.headers["location"] == "/settings?section=ai-providers"
  replay = client.get(
    "/api/auth/provider/mobius/callback",
    params={"code": "central-code", "state": state},
    follow_redirects=False,
  )
  assert replay.status_code == 303
  assert replay.headers["location"] == (
    "/settings?section=ai-providers&mobius_enroll_error=1"
  )
  assert sum(route == "/identity/enroll" for _, route, _ in broker_calls) == 1


def test_providers_models_respects_hidden_model_prefs(client, auth):
  """Mini-app pickers use the same visible model list the chat picker
  does, so hiding a model globally should remove it from this endpoint
  too."""
  from app.providers import invalidate_model_cache
  invalidate_model_cache()
  r0 = client.patch(
    "/api/owner/model-prefs",
    headers=auth,
    json={"hidden_ids": ["claude-opus-4-8", "gpt-5.5"]},
  )
  assert r0.status_code == 200, r0.text

  r = client.get("/api/auth/providers/models", headers=auth)
  assert r.status_code == 200, r.text
  body = r.json()
  assert "claude-opus-4-8" not in [m["id"] for m in body["claude"]]
  assert "gpt-5.5" not in [m["id"] for m in body["codex"]]
  assert body["claude"]
  assert body["codex"]


# ---------------------------------------------------------------------------
# CSRF hardening (Task 1): setup endpoint now protected
# ---------------------------------------------------------------------------

def test_setup_rejects_cross_site_request(client):
  """POST /api/auth/setup must reject cross-site requests (Sec-Fetch-Site:
  cross-site). First-boot setup via curl is unaffected because curl does not
  send Sec-Fetch-Site at all, so the guard passes the request through."""
  r = client.post(
    "/api/auth/setup",
    json={"username": "admin", "password": "securepassword123"},
    headers={"Sec-Fetch-Site": "cross-site"},
  )
  assert r.status_code == 403


def test_setup_rejects_opaque_cross_site_request_without_bearer(client):
  """Origin null alone is not the authenticated app-sandbox exception."""
  r = client.post(
    "/api/auth/setup",
    json={"username": "admin", "password": "securepassword123"},
    headers={"Origin": "null", "Sec-Fetch-Site": "cross-site"},
  )
  assert r.status_code == 403


def test_setup_allows_curl_style_request(client):
  """Setup with no Sec-Fetch-Site header (e.g. curl) must still work."""
  r = client.post(
    "/api/auth/setup",
    json={
      "username": "admin",
      "password": "securepassword123",
    },
  )
  assert r.status_code == 200
  assert "access_token" in r.json()


# ---------------------------------------------------------------------------
# Login tracking cap (Task 7): dict eviction on overflow
# ---------------------------------------------------------------------------

def test_login_failure_tracking_caps_at_10k(client):
  """_login_failures must not grow beyond _LOGIN_TRACK_CAP entries so a
  username-enumeration flood can't exhaust the process heap."""
  from app.routes.auth import (
    _LOGIN_TRACK_CAP, _login_failures, _record_login_failure,
  )
  # Snapshot the starting length (other tests may leave entries).
  import app.routes.auth as _auth_mod
  _auth_mod._login_failures = {}
  # Insert one more than the cap — the dict must stay at or below the cap.
  for i in range(_LOGIN_TRACK_CAP + 5):
    _record_login_failure(f"user_{i}")
  assert len(_auth_mod._login_failures) <= _LOGIN_TRACK_CAP


def test_login_cooldown_tracking_caps_at_10k(client):
  """_login_cooldown_until must also be capped to avoid unbounded growth."""
  import app.routes.auth as _auth_mod
  _auth_mod._login_failures = {}
  _auth_mod._login_cooldown_until = {}
  # 30+ failures triggers the longest cooldown and writes to _login_cooldown_until.
  from app.routes.auth import _LOGIN_TRACK_CAP, _record_login_failure
  for i in range(_LOGIN_TRACK_CAP + 5):
    # Directly set failures to 30 so each record_failure call creates a cooldown.
    _auth_mod._login_failures[f"user_{i}"] = 29
    _record_login_failure(f"user_{i}")
  assert len(_auth_mod._login_cooldown_until) <= _LOGIN_TRACK_CAP
