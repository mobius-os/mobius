"""Tests for authentication flow."""

import json
import time

import bcrypt


def test_setup_creates_owner(client):
  r = client.post("/api/auth/setup", json={
    "username": "admin",
    "password": "securepassword123",
  })
  assert r.status_code == 200
  assert "access_token" in r.json()


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


def test_login_upgrades_legacy_hash_and_recovery_seed(client, db):
  """A successful legacy login migrates both durable credential copies."""
  from app import auth, models, recovery_seed

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
  seed = json.loads(recovery_seed.OWNER_SEED_PATH.read_text())
  assert seed == {
    "username": "legacy",
    "hashed_password": stored.hashed_password,
  }


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
  """App-scoped JWTs (minted for the news Settings tab, the future
  Reflection Settings tab, recovery chat picker) must read the full
  model list — otherwise the picker silently falls back to one model
  per provider. The endpoint is read-only and the same list is
  already visible to every running mini-app via the CLI runtime,
  so loosening the auth here doesn't widen the surface."""
  # Need a real App row for the app-scoped JWT to resolve.
  r0 = client.post("/api/apps/", headers=auth, json={
    "name": "Picker host",
    "description": "x",
    "jsx_source": "export default function App() { return null }",
  })
  assert r0.status_code == 201, r0.text
  app_id = r0.json()["id"]

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
  r0 = client.post("/api/apps/", headers=auth, json={
    "name": "Status host",
    "description": "x",
    "jsx_source": "export default function App() { return null }",
  })
  assert r0.status_code == 201, r0.text
  app_id = r0.json()["id"]

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
  client, auth,
):
  """Without real Anthropic / Codex credentials the underlying
  `list_models` falls back to KNOWN_MODELS — exercise that path and
  pin the response shape mini-apps depend on (id + name, plus a
  tier on Claude rows)."""
  from app.providers import DEFAULT_VISIBLE_MODELS, KNOWN_MODELS, invalidate_model_cache
  invalidate_model_cache()
  r = client.get("/api/auth/providers/models", headers=auth)
  assert r.status_code == 200
  body = r.json()
  assert set(body) == {"claude", "codex"}
  claude_ids = [m["id"] for m in body["claude"]]
  assert claude_ids == [
    "claude-fable-5", "claude-sonnet-5",
    "claude-opus-4-8", "claude-sonnet-4-6",
  ]
  codex_ids = [m["id"] for m in body["codex"]]
  assert codex_ids == [
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
  ]
  assert set(claude_ids) == DEFAULT_VISIBLE_MODELS["claude"]
  assert set(codex_ids) == DEFAULT_VISIBLE_MODELS["codex"]
  # Claude rows carry a tier derived from the id.
  by_id = {m["id"]: m for m in body["claude"]}
  assert by_id["claude-opus-4-8"]["name"] == "Opus 4.8"
  assert by_id["claude-opus-4-8"]["tier"] == "opus"
  assert by_id["claude-sonnet-4-6"]["tier"] == "sonnet"
  # Codex rows intentionally omit `tier` — the field doesn't apply.
  for row in body["codex"]:
    assert "tier" not in row
    assert "id" in row and "name" in row
  # `available` / `provider` from the shell-facing /api/models response
  # are NOT leaked through; mini-apps see only id + name (+ tier).
  for rows in body.values():
    for row in rows:
      assert set(row).issubset({"id", "name", "tier"})


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
