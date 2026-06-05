"""Tests for authentication flow."""


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
  Dreaming Settings tab, recovery chat picker) must read the full
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
  from app.providers import KNOWN_MODELS, invalidate_model_cache
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
  # Full per-provider list, not a one-model FALLBACK_GROUPS stub.
  assert [m["id"] for m in body["claude"]] == KNOWN_MODELS["claude"]
  assert [m["id"] for m in body["codex"]] == KNOWN_MODELS["codex"]


def test_providers_models_returns_known_models_on_missing_creds(
  client, auth,
):
  """Without real Anthropic / Codex credentials the underlying
  `list_models` falls back to KNOWN_MODELS — exercise that path and
  pin the response shape mini-apps depend on (id + name, plus a
  tier on Claude rows)."""
  from app.providers import KNOWN_MODELS, invalidate_model_cache
  invalidate_model_cache()
  r = client.get("/api/auth/providers/models", headers=auth)
  assert r.status_code == 200
  body = r.json()
  assert set(body) == {"claude", "codex"}
  claude_ids = [m["id"] for m in body["claude"]]
  assert claude_ids == KNOWN_MODELS["claude"]
  codex_ids = [m["id"] for m in body["codex"]]
  assert codex_ids == KNOWN_MODELS["codex"]
  # Claude rows carry a tier derived from the id.
  by_id = {m["id"]: m for m in body["claude"]}
  assert by_id["claude-opus-4-8"]["name"] == "Opus 4.8"
  assert by_id["claude-opus-4-8"]["tier"] == "opus"
  assert by_id["claude-sonnet-4-6"]["tier"] == "sonnet"
  assert by_id["claude-haiku-4-5-20251001"]["tier"] == "haiku"
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
