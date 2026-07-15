"""Tests for the GitHub connection + read-surface routes (routes/github.py).

The upstream GitHub calls are mocked with httpx.MockTransport (the
test_model_registry.py idiom — respx is not installed), so no test touches
the network. Two harness notes:

- The router owns its own slowapi Limiter; conftest only disables the app +
  auth limiters, so connect/start's 3/min ceiling would 429 the suite by the
  fourth test. Disable it explicitly at import.
- The autouse _isolate_git_env fixture pins GIT_CONFIG_GLOBAL=/dev/null;
  write_credentials sets the git identity via `git config --global`, so the
  identity test re-points GIT_CONFIG_GLOBAL at a tmp file and reads it back.
"""

import asyncio
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.responses import Response

from app import github_auth, source_status
from app.config import get_settings
from app.database import engine
from app.storage_io import atomic_write

# The github router's Limiter is a separate instance from app.state.limiter,
# so conftest's disable doesn't reach it (see module docstring).
from app.routes.github import _limiter as _github_limiter
from app.routes import github as github_routes

_github_limiter.enabled = False


# --- fixtures + helpers -----------------------------------------------


@pytest.fixture(autouse=True)
def _github_state():
  """Clears the on-disk credential dir + in-flight device flow around each
  test — conftest.fresh_db wipes apps/ and shared/ but not cli-auth/ — and
  resets the settings cache so per-test GITHUB_OAUTH_CLIENT_ID takes."""
  import shutil
  github_auth.set_device_flow(None)
  shutil.rmtree(github_auth.GH_AUTH_DIR, ignore_errors=True)
  get_settings.cache_clear()
  yield
  github_auth.set_device_flow(None)
  shutil.rmtree(github_auth.GH_AUTH_DIR, ignore_errors=True)
  get_settings.cache_clear()


def _set_client_id(monkeypatch, value):
  """Sets GITHUB_OAUTH_CLIENT_ID and drops the lru_cache so the next
  get_settings() reflects it. None means "device flow disabled", which is
  an EXPLICIT empty env var — config.py ships a public default client id,
  so merely unsetting the var would leave device flow available."""
  monkeypatch.setenv("GITHUB_OAUTH_CLIENT_ID", value if value is not None else "")
  get_settings.cache_clear()


def _install_mock_transport(monkeypatch, handler):
  """Route every httpx.AsyncClient request through `handler` (an
  httpx.MockTransport route) — the test_model_registry.py idiom, no network,
  no respx."""
  real = httpx.AsyncClient

  def factory(*args, **kwargs):
    kwargs["transport"] = httpx.MockTransport(handler)
    return real(*args, **kwargs)

  monkeypatch.setattr(httpx, "AsyncClient", factory)


def _write_token(
  *, token="gh-tok-abc", login="octocat", user_id=42,
  scopes=("public_repo",), source="pat",
):
  """Writes a connected-state file directly (the get_token() read source)."""
  os.makedirs(github_auth.GH_AUTH_DIR, exist_ok=True)
  github_auth.STATE_PATH.write_text(json.dumps({
    "token": token,
    "login": login,
    "user_id": user_id,
    "scopes": list(scopes),
    "token_source": source,
    "connected_at": "2026-07-06T00:00:00+00:00",
  }))
  return token


def _app_token(client, owner_token, *, github_access=False):
  """Creates an app (optionally granting github_access on the row) and
  returns (app_id, app_scoped_token)."""
  r = client.post("/api/apps/", json={
    "name": "contribute-test",
    "description": "t",
    "jsx_source": "export default function App(){ return <div>hi</div> }",
  }, headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 201, r.text
  app_id = r.json()["id"]
  if github_access:
    # Set the column directly — the plain create path doesn't parse the
    # permission (that's the install path); the gate reads the row at
    # request time regardless (deps.get_owner_or_app_with_github_access).
    from app import models
    from app.database import SessionLocal
    s = SessionLocal()
    try:
      app = s.query(models.App).filter(models.App.id == app_id).first()
      app.github_access = True
      s.commit()
    finally:
      s.close()
  r = client.post("/api/auth/app-token", json={"app_id": app_id},
                  headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 200, r.text
  return app_id, r.json()["token"]


_DEVICE_CODE_URL = "https://github.com/login/device/code"
_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"


def _fail(request):
  """A handler leaf that fails loudly on an unexpected upstream call — a
  591 body a route would surface, so a bypassed guard shows up as a wrong
  status rather than a silent pass."""
  return httpx.Response(591, json={"unexpected": str(request.url)})


# --- connect/start ----------------------------------------------------


def test_connect_start_requires_client_id(client, auth, monkeypatch):
  _set_client_id(monkeypatch, None)
  r = client.post("/api/github/connect/start", headers=auth)
  assert r.status_code == 409
  assert "GITHUB_OAUTH_CLIENT_ID" in r.json()["detail"]


def test_connect_start_returns_user_code(client, auth, monkeypatch):
  _set_client_id(monkeypatch, "cid-123")

  def handler(request):
    if str(request.url) == _DEVICE_CODE_URL:
      return httpx.Response(200, json={
        "device_code": "DEV", "user_code": "WXYZ-1234",
        "verification_uri": "https://github.com/login/device",
        "interval": 5, "expires_in": 900,
      })
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  r = client.post("/api/github/connect/start", headers=auth)
  assert r.status_code == 200, r.text
  body = r.json()
  assert body["user_code"] == "WXYZ-1234"
  assert body["verification_uri"] == "https://github.com/login/device"
  assert body["interval"] == 5
  assert github_auth.get_device_flow()["device_code"] == "DEV"


def test_connect_start_can_explicitly_request_workflow_scope(
  client, auth, monkeypatch,
):
  _set_client_id(monkeypatch, "cid-123")
  seen = {}

  def handler(request):
    if str(request.url) == _DEVICE_CODE_URL:
      seen.update(parse_qs(request.content.decode()))
      return httpx.Response(200, json={
        "device_code": "DEV", "user_code": "WXYZ-1234",
        "verification_uri": "https://github.com/login/device",
        "interval": 5, "expires_in": 900,
      })
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  r = client.post(
    "/api/github/connect/start", headers=auth, json={"workflow": True},
  )

  assert r.status_code == 200, r.text
  assert seen["scope"] == ["public_repo workflow"]
  assert r.json()["requested_scopes"] == ["public_repo", "workflow"]


def test_connect_start_app_with_github_access(
  client, owner_token, monkeypatch,
):
  """The Contribute app drives connect from its own UI: a github_access
  app token is accepted on the connect flow, not just the owner JWT."""
  _set_client_id(monkeypatch, "cid-123")
  _, app_token = _app_token(client, owner_token, github_access=True)

  def handler(request):
    if str(request.url) == _DEVICE_CODE_URL:
      return httpx.Response(200, json={
        "device_code": "DEV", "user_code": "WXYZ-1234",
        "verification_uri": "https://github.com/login/device",
        "interval": 5, "expires_in": 900,
      })
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  r = client.post("/api/github/connect/start",
                  headers={"Authorization": f"Bearer {app_token}"})
  assert r.status_code == 200, r.text
  assert r.json()["user_code"] == "WXYZ-1234"


def test_connect_start_app_without_github_access_forbidden(
  client, owner_token, monkeypatch,
):
  _set_client_id(monkeypatch, "cid-123")
  _, app_token = _app_token(client, owner_token, github_access=False)
  r = client.post("/api/github/connect/start",
                  headers={"Authorization": f"Bearer {app_token}"})
  assert r.status_code == 403
  assert "github_access" in r.json()["detail"]


# --- connect/poll -----------------------------------------------------


def test_poll_no_flow_returns_none(client, auth):
  r = client.post("/api/github/connect/poll", headers=auth)
  assert r.status_code == 200
  assert r.json() == {"status": "none"}


def test_device_flow_happy_path(client, auth, monkeypatch, tmp_path):
  """start → poll-before-interval (no upstream) → pending → slow_down bumps
  the interval → success writes BOTH files 0600 and the git identity."""
  _set_client_id(monkeypatch, "cid-123")
  # Re-point the global git config so the identity write lands in a file we
  # can read back (the autouse fixture pins it at /dev/null otherwise).
  gitconfig = tmp_path / "gitconfig"
  monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))

  calls = {"access_token": 0, "user": 0}
  access_seq = [
    {"error": "authorization_pending"},
    {"error": "slow_down", "interval": 7},
    {"access_token": "gh-secret-xyz"},
  ]

  def handler(request):
    url = str(request.url)
    if url == _DEVICE_CODE_URL:
      return httpx.Response(200, json={
        "device_code": "DEV", "user_code": "WXYZ-1234",
        "verification_uri": "https://github.com/login/device",
        "interval": 5, "expires_in": 900,
      })
    if url == _ACCESS_TOKEN_URL:
      body = access_seq[calls["access_token"]]
      calls["access_token"] += 1
      return httpx.Response(200, json=body)
    if url == "https://api.github.com/user":
      calls["user"] += 1
      assert request.headers.get("authorization") == "Bearer gh-secret-xyz"
      return httpx.Response(200, json={"login": "octocat", "id": 42},
                            headers={"x-oauth-scopes": "public_repo, read:org"})
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)

  assert client.post("/api/github/connect/start", headers=auth).status_code == 200

  # Poll before GitHub's interval elapses — answered pending WITHOUT hitting
  # the token endpoint (the server paces so an eager frontend can't trip
  # slow_down escalation).
  r = client.post("/api/github/connect/poll", headers=auth)
  assert r.json() == {"status": "pending"}
  assert calls["access_token"] == 0

  # authorization_pending — interval unchanged.
  github_auth.get_device_flow()["next_poll_at"] = 0
  r = client.post("/api/github/connect/poll", headers=auth)
  assert r.json() == {"status": "pending"}
  assert calls["access_token"] == 1
  assert github_auth.get_device_flow()["interval"] == 5

  # slow_down — interval bumps to max(payload 7, prev 5 + 5) = 10.
  github_auth.get_device_flow()["next_poll_at"] = 0
  r = client.post("/api/github/connect/poll", headers=auth)
  assert r.json() == {"status": "pending"}
  assert calls["access_token"] == 2
  assert github_auth.get_device_flow()["interval"] == 10

  # success — credentials persisted, flow cleared.
  github_auth.get_device_flow()["next_poll_at"] = 0
  r = client.post("/api/github/connect/poll", headers=auth)
  assert r.json() == {"status": "complete", "login": "octocat"}
  assert calls["user"] == 1
  assert github_auth.get_device_flow() is None

  # Both credential files exist at 0600.
  for path in (github_auth.STATE_PATH, github_auth.HOSTS_PATH):
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
  state = json.loads(github_auth.STATE_PATH.read_text())
  assert state["token"] == "gh-secret-xyz"
  assert state["login"] == "octocat"
  assert state["token_source"] == "device"
  assert state["scopes"] == ["public_repo", "read:org"]

  # Git identity attributes commits to the connected user.
  def _git_get(key):
    return subprocess.run(
      ["git", "config", "--global", "--get", key],
      capture_output=True, text=True,
    ).stdout.strip()

  assert _git_get("user.name") == "octocat"
  assert _git_get("user.email") == "42+octocat@users.noreply.github.com"


@pytest.mark.parametrize("reason", ["expired_token", "access_denied"])
def test_poll_failure_clears_state(client, auth, monkeypatch, reason):
  _set_client_id(monkeypatch, "cid-123")

  def handler(request):
    url = str(request.url)
    if url == _DEVICE_CODE_URL:
      return httpx.Response(200, json={
        "device_code": "DEV", "user_code": "AB-12",
        "verification_uri": "https://github.com/login/device",
        "interval": 5, "expires_in": 900,
      })
    if url == _ACCESS_TOKEN_URL:
      return httpx.Response(200, json={"error": reason})
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  assert client.post("/api/github/connect/start", headers=auth).status_code == 200
  github_auth.get_device_flow()["next_poll_at"] = 0
  r = client.post("/api/github/connect/poll", headers=auth)
  assert r.json() == {"status": "failed", "reason": reason}
  assert github_auth.get_device_flow() is None


# --- connect/token (classic PAT) --------------------------------------


def test_connect_token_rejects_fine_grained(client, auth):
  r = client.post("/api/github/connect/token",
                  json={"token": "github_pat_11ABCDEF_secret"}, headers=auth)
  assert r.status_code == 400
  detail = r.json()["detail"]
  assert "fine-grained" in detail
  # The rejection is actionable: it links straight to the classic-token
  # creation page with the required scope pre-filled, and says why.
  assert "https://github.com/settings/tokens/new" in detail
  assert "scopes=public_repo" in detail


def test_connect_token_rejects_missing_scope(client, auth, monkeypatch):
  def handler(request):
    if str(request.url) == "https://api.github.com/user":
      return httpx.Response(200, json={"login": "octocat", "id": 42},
                            headers={"x-oauth-scopes": "read:user, gist"})
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  r = client.post("/api/github/connect/token",
                  json={"token": "ghp_noscope"}, headers=auth)
  assert r.status_code == 400
  detail = r.json()["detail"]
  # The scopes the token DID have are echoed back.
  assert "read:user" in detail and "gist" in detail


def test_connect_token_happy_path(client, auth, monkeypatch):
  def handler(request):
    if str(request.url) == "https://api.github.com/user":
      assert request.headers.get("authorization") == "Bearer ghp_classic123"
      return httpx.Response(200, json={"login": "octocat", "id": 42},
                            headers={"x-oauth-scopes": "repo"})
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  r = client.post("/api/github/connect/token",
                  json={"token": "ghp_classic123"}, headers=auth)
  assert r.status_code == 200, r.text
  assert r.json() == {"login": "octocat"}
  state = json.loads(github_auth.STATE_PATH.read_text())
  assert state["token"] == "ghp_classic123"
  assert state["token_source"] == "pat"
  assert stat.S_IMODE(github_auth.STATE_PATH.stat().st_mode) == 0o600


# --- status -----------------------------------------------------------


def test_status_disconnected(client, auth, monkeypatch):
  _set_client_id(monkeypatch, "cid-123")
  r = client.get("/api/github/status", headers=auth)
  assert r.status_code == 200
  body = r.json()
  assert body["connected"] is False
  assert body["login"] is None
  assert body["scopes"] == []
  assert body["token_source"] is None
  assert body["device_flow_available"] is True
  assert "scopes=public_repo" in body["classic_token_url"]
  assert "workflow" in body["classic_workflow_token_url"]
  assert "gh_version" in body
  assert "token" not in body


def test_status_device_flow_unavailable_without_client_id(
  client, auth, monkeypatch,
):
  _set_client_id(monkeypatch, None)
  r = client.get("/api/github/status", headers=auth)
  assert r.json()["device_flow_available"] is False


def test_status_connected_never_echoes_token(client, auth):
  secret = _write_token(token="gh-super-secret", login="octocat",
                        scopes=("public_repo", "read:org"), source="pat")
  r = client.get("/api/github/status", headers=auth)
  assert r.status_code == 200
  body = r.json()
  assert body["connected"] is True
  assert body["login"] == "octocat"
  assert body["scopes"] == ["public_repo", "read:org"]
  assert body["token_source"] == "pat"
  # INV1: the token never appears anywhere in the payload.
  assert "token" not in body
  assert secret not in json.dumps(body)


def test_source_status_is_fetch_free_and_available_to_owner(client, auth):
  r = client.get("/api/github/source-status", headers=auth)
  assert r.status_code == 200, r.text
  body = r.json()
  assert body["schema"] == 1
  assert body["fetch_free"] is True
  assert body["platform"]["key"] == "platform"
  assert body["apps"] == []
  serialized = json.dumps(body)
  assert "source_dir" not in serialized
  assert "manifest_url" not in serialized


def test_source_status_releases_db_before_waiting_on_repository_locks(
  monkeypatch,
):
  class EmptyQuery:
    def filter(self, *args):
      return self

    def order_by(self, *args):
      return self

    def all(self):
      return []

  class FakeSession:
    closed = False

    def query(self, *args):
      return EmptyQuery()

    def close(self):
      self.closed = True

  db = FakeSession()

  async def checked_to_thread(function, *args):
    assert db.closed, "repository inspection started with DB still checked out"
    assert function is source_status.build_platform_status
    return {"key": "platform", "available": True}

  monkeypatch.setattr(github_routes.asyncio, "to_thread", checked_to_thread)
  result = asyncio.run(github_routes.github_source_status(None, db))

  assert result["platform"] == {"key": "platform", "available": True}
  assert result["apps"] == []


def test_source_status_requires_github_access_for_app_tokens(
  client, owner_token,
):
  _, denied_token = _app_token(client, owner_token, github_access=False)
  denied = client.get(
    "/api/github/source-status",
    headers={"Authorization": f"Bearer {denied_token}"},
  )
  assert denied.status_code == 403

  _, allowed_token = _app_token(client, owner_token, github_access=True)
  allowed = client.get(
    "/api/github/source-status",
    headers={"Authorization": f"Bearer {allowed_token}"},
  )
  assert allowed.status_code == 200, allowed.text


def test_source_status_keeps_healthy_apps_when_one_checkout_fails(
  client, owner_token, auth, monkeypatch,
):
  from app import models
  from app.database import SessionLocal

  good_id, _ = _app_token(client, owner_token)
  bad_id, _ = _app_token(client, owner_token)
  app_root = Path(get_settings().data_dir) / "apps"
  good_dir = app_root / "good-source"
  bad_dir = app_root / "bad-source"
  good_dir.mkdir(parents=True, exist_ok=True)
  bad_dir.mkdir(parents=True, exist_ok=True)
  session = SessionLocal()
  try:
    session.query(models.App).filter(models.App.id == good_id).update({
      "name": "Good source", "source_dir": str(good_dir),
    })
    session.query(models.App).filter(models.App.id == bad_id).update({
      "name": "Bad source", "source_dir": str(bad_dir),
    })
    session.commit()
  finally:
    session.close()

  monkeypatch.setattr(source_status, "build_platform_status", lambda: {
    "key": "platform", "available": True,
  })

  def inspect(app):
    if app["id"] == bad_id:
      raise RuntimeError("damaged checkout")
    return {"key": f'app:{app["id"]}', "name": app["name"]}

  monkeypatch.setattr(source_status, "build_app_status", inspect)
  response = client.get("/api/github/source-status", headers=auth)

  assert response.status_code == 200, response.text
  assert response.json()["apps"] == [{
    "key": f"app:{good_id}", "name": "Good source",
  }]


# --- disconnect -------------------------------------------------------


def test_disconnect_removes_dir(client, auth):
  _write_token()
  assert github_auth.GH_AUTH_DIR.exists()
  r = client.delete("/api/github/connect", headers=auth)
  assert r.status_code == 200
  assert r.json() == {"ok": True}
  assert not github_auth.GH_AUTH_DIR.exists()


# --- REST passthrough (GET-only, read-only by construction) -----------


def test_rest_get_injects_auth_and_forwards_query(client, auth, monkeypatch):
  _write_token(token="gh-rest-tok")

  def handler(request):
    if request.url.host == "api.github.com" and request.method == "GET":
      assert request.headers.get("authorization") == "Bearer gh-rest-tok"
      assert "per_page=5" in request.url.query.decode()
      return httpx.Response(200, json={"full_name": "mobius-os/app-tasks"},
                            headers={"x-ratelimit-remaining": "4321"})
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  r = client.get("/api/github/api/repos/mobius-os/app-tasks?per_page=5",
                 headers=auth)
  assert r.status_code == 200
  assert r.json()["full_name"] == "mobius-os/app-tasks"
  assert r.headers["X-RateLimit-Remaining"] == "4321"


def test_rest_requires_connection(client, auth):
  r = client.get("/api/github/api/user", headers=auth)
  assert r.status_code == 401
  assert "not connected" in r.json()["detail"].lower()


def test_rest_non_get_not_served(client, auth, monkeypatch):
  # Only GET is registered on the passthrough (read-only by construction).
  # main.py's `/api/{path:path}` catch-all fully matches every method, so an
  # unregistered method on an /api path resolves to that 404 rather than a
  # 405 — either way the POST never reaches the passthrough. The _fail
  # transport would surface a 591 if it somehow did forward upstream.
  _install_mock_transport(monkeypatch, _fail)
  r = client.post("/api/github/api/user", headers=auth)
  assert r.status_code == 404


def test_rest_app_without_github_access_forbidden(client, owner_token):
  _write_token()
  _, app_token = _app_token(client, owner_token, github_access=False)
  r = client.get("/api/github/api/user",
                 headers={"Authorization": f"Bearer {app_token}"})
  assert r.status_code == 403
  assert "github_access" in r.json()["detail"]


def test_rest_app_with_github_access_ok(client, owner_token, monkeypatch):
  _write_token(token="gh-app-tok")
  _, app_token = _app_token(client, owner_token, github_access=True)

  def handler(request):
    if request.url.host == "api.github.com":
      assert request.headers.get("authorization") == "Bearer gh-app-tok"
      return httpx.Response(200, json={"login": "octocat"})
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  r = client.get("/api/github/api/user",
                 headers={"Authorization": f"Bearer {app_token}"})
  assert r.status_code == 200
  assert r.json()["login"] == "octocat"


def test_github_capability_releases_db_before_upstream_request(
  client, owner_token, monkeypatch,
):
  """A fan-out of slow GitHub reads must consume sockets, not the DB pool."""
  _write_token(token="gh-app-tok")
  _, app_token = _app_token(client, owner_token, github_access=True)
  baseline = engine.pool.checkedout()
  checked_out = []

  async def fake_forward(_client, _request):
    checked_out.append(engine.pool.checkedout())
    return Response(content=b'{}', media_type="application/json")

  monkeypatch.setattr(github_routes, "_forward_capped", fake_forward)
  r = client.get(
    "/api/github/api/user",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert r.status_code == 200
  assert checked_out == [baseline]


def test_rest_owner_ok(client, auth, monkeypatch):
  _write_token(token="gh-owner-tok")

  def handler(request):
    if request.url.host == "api.github.com":
      return httpx.Response(200, json={"login": "octocat"})
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  r = client.get("/api/github/api/user", headers=auth)
  assert r.status_code == 200


def test_rest_rejects_path_escape(client, auth, monkeypatch):
  _write_token()

  # If the guard were bypassed the request would reach evil.com and the
  # handler would answer 591 — so a wrong host shows up as a wrong status.
  _install_mock_transport(monkeypatch, _fail)
  r = client.get("/api/github/api/https://evil.com/steal", headers=auth)
  assert r.status_code == 400
  assert "api.github.com" in r.json()["detail"]


# --- GraphQL (read-only: mutations/subscriptions rejected, INV2) ------


def _graphql_ok_handler(seen):
  def handler(request):
    if str(request.url) == "https://api.github.com/graphql":
      seen["body"] = json.loads(request.content)
      assert request.headers.get("authorization") == "Bearer gh-gql-tok"
      return httpx.Response(200, json={"data": {"viewer": {"login": "octocat"}}})
    return _fail(request)

  return handler


def test_graphql_query_ok_and_forwards_variables(client, auth, monkeypatch):
  _write_token(token="gh-gql-tok")
  seen = {}
  _install_mock_transport(monkeypatch, _graphql_ok_handler(seen))
  r = client.post("/api/github/graphql", headers=auth, json={
    "query": "query($n:Int!){ viewer { login } rateLimit { cost } }",
    "variables": {"n": 3},
  })
  assert r.status_code == 200
  assert r.json()["data"]["viewer"]["login"] == "octocat"
  # Variables are forwarded verbatim to GitHub.
  assert seen["body"]["variables"] == {"n": 3}


def test_graphql_plain_mutation_rejected(client, auth, monkeypatch):
  _write_token(token="gh-gql-tok")
  # No upstream call should happen — the guard rejects before forwarding.
  _install_mock_transport(monkeypatch, _fail)
  r = client.post("/api/github/graphql", headers=auth, json={
    "query": "mutation { addStar(input:{starrableId:\"x\"}) { clientMutationId } }",
  })
  assert r.status_code == 400
  assert "read-only" in r.json()["detail"]


def test_graphql_mutation_hidden_after_comment_rejected(
  client, auth, monkeypatch,
):
  # Stripping the #-comment must not let the REAL mutation slip past the
  # scan — the keyword after the comment is still caught.
  _write_token(token="gh-gql-tok")
  _install_mock_transport(monkeypatch, _fail)
  query = (
    "query { viewer { login } }  # innocuous trailing note\n"
    "mutation { addReaction(input:{}) { clientMutationId } }"
  )
  r = client.post("/api/github/graphql", headers=auth, json={"query": query})
  assert r.status_code == 400


def test_graphql_mutation_as_string_literal_allowed(client, auth, monkeypatch):
  # "mutation" inside a string value is data, not an operation — the guard
  # must NOT trip, and the query must forward.
  _write_token(token="gh-gql-tok")
  seen = {}
  _install_mock_transport(monkeypatch, _graphql_ok_handler(seen))
  r = client.post("/api/github/graphql", headers=auth, json={
    "query": (
      'query { search(query: "is:issue mutation in:title", '
      'type: ISSUE, first: 1) { issueCount } }'
    ),
  })
  assert r.status_code == 200
  assert seen["body"]["query"].count("mutation") == 1


# --- contribution submit (approval button path) -----------------------


def _write_contribution(app_id, record_id, record, diff_text=""):
  base = Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
  base.mkdir(parents=True, exist_ok=True)
  atomic_write(base / f"{record_id}.json", json.dumps(record))
  if diff_text:
    atomic_write(base / f"{record_id}.diff", diff_text)


def _cp(stdout="", stderr="", returncode=0):
  return subprocess.CompletedProcess(["mock"], returncode, stdout, stderr)


_UPSTREAM_SHA = "d" * 40


def _submit_preflight_response(args, *, merge_conflict: bool = False):
  if (
    len(args) >= 3 and
    args[:2] == ("rev-parse", "--verify") and
    args[2].startswith("refs/mobius-submit/upstream-")
  ):
    return _cp(_UPSTREAM_SHA + "\n")
  if (
    len(args) >= 3 and
    args[:2] == ("rev-parse", "--verify") and
    args[2].startswith("refs/mobius-submit/fork-")
  ):
    # Existing submit tests model a fork that is already current. Dedicated
    # sync tests below exercise stale, ahead, and diverged fork tips.
    return _cp(_UPSTREAM_SHA + "\n")
  if args[:1] == ("merge-tree",):
    return _cp(returncode=1 if merge_conflict else 0)
  return None


def test_inspect_owner_fork_reports_strictly_behind_without_mutation(
  tmp_path, monkeypatch,
):
  from app.routes.github import _inspect_owner_fork_default_branch

  repo = tmp_path / "repo"
  repo.mkdir()
  stale = "c" * 40
  current = "d" * 40
  git_calls = []
  gh_calls = []

  monkeypatch.setattr(
    "app.routes.github._upstream_default_branch",
    lambda _repo, _slug: "main",
  )

  def fake_git(repo_path, *args, check=True):
    git_calls.append(args)
    if args[:2] == ("rev-parse", "--verify"):
      return _cp(stale + "\n")
    if args[:2] == ("merge-base", "--is-ancestor"):
      if args[2:] == (current, stale):
        return _cp(returncode=1)
      if args[2:] == (stale, current):
        return _cp(returncode=0)
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)
  monkeypatch.setattr(
    "app.routes.github._gh",
    lambda _repo, *args, **kwargs: gh_calls.append(args) or _cp(""),
  )

  patch = _inspect_owner_fork_default_branch(
    repo,
    "octocat/app-demo",
    upstream_branch="main",
    upstream_sha=current,
  )

  assert patch["last_submit_fork_sync"] == "strictly-behind"
  assert patch["last_submit_fork_sha"] == stale
  assert gh_calls == []
  assert sum(call[:1] == ("fetch",) for call in git_calls) == 2


def test_inspect_owner_fork_accepts_updated_topic_as_upstream_carrier(
  tmp_path, monkeypatch,
):
  from app.routes.github import _inspect_owner_fork_default_branch

  repo = tmp_path / "repo"
  repo.mkdir()
  stale = "c" * 40
  upstream = "d" * 40
  carrier_tip = "e" * 40

  monkeypatch.setattr(
    "app.routes.github._upstream_default_branch",
    lambda _repo, _slug: "main",
  )

  def fake_git(repo_path, *args, check=True):
    if args[:2] == ("rev-parse", "--verify"):
      return _cp(stale + "\n")
    if args[:2] == ("merge-base", "--is-ancestor"):
      if args[2:] == (upstream, stale):
        return _cp(returncode=1)
      if args[2:] == (stale, upstream):
        return _cp(returncode=0)
      if args[2:] == (upstream, carrier_tip):
        return _cp(returncode=0)
    if args[:2] == ("for-each-ref", "--format=%(refname)%00%(objectname)"):
      prefix = args[2].rstrip("/")
      return _cp(f"{prefix}/fix/review\x00{carrier_tip}\n")
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)

  patch = _inspect_owner_fork_default_branch(
    repo,
    "octocat/app-demo",
    upstream_branch="main",
    upstream_sha=upstream,
  )

  assert patch["last_submit_fork_sync"] == "contains-upstream"
  assert patch["last_submit_fork_carrier_branch"] == "fix/review"
  assert patch["last_submit_fork_carrier_sha"] == carrier_tip


def test_inspect_owner_fork_leaves_diverged_default_branch_untouched(
  tmp_path, monkeypatch,
):
  from app.routes.github import (
    ContributionSubmitError,
    _inspect_owner_fork_default_branch,
  )

  repo = tmp_path / "repo"
  repo.mkdir()
  fork_sha = "c" * 40
  upstream_sha = "d" * 40
  gh_calls = []

  monkeypatch.setattr(
    "app.routes.github._upstream_default_branch",
    lambda _repo, _slug: "main",
  )

  def fake_git(repo_path, *args, check=True):
    if args[:2] == ("rev-parse", "--verify"):
      return _cp(fork_sha + "\n")
    if args[:2] == ("merge-base", "--is-ancestor"):
      return _cp(returncode=1)
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)
  monkeypatch.setattr(
    "app.routes.github._gh",
    lambda _repo, *args, **kwargs: gh_calls.append(args) or _cp(""),
  )

  with pytest.raises(ContributionSubmitError) as exc:
    _inspect_owner_fork_default_branch(
      repo,
      "octocat/app-demo",
      upstream_branch="main",
      upstream_sha=upstream_sha,
    )

  assert "diverged" in exc.value.message
  assert exc.value.record_patch["last_submit_fork_sync"] == "diverged"
  assert gh_calls == []


def test_inspect_owner_fork_reports_current_or_ahead_branch(
  tmp_path, monkeypatch,
):
  from app.routes.github import _inspect_owner_fork_default_branch

  repo = tmp_path / "repo"
  repo.mkdir()
  upstream_sha = "d" * 40
  ahead_sha = "e" * 40
  tips = iter((upstream_sha, ahead_sha))
  gh_calls = []

  monkeypatch.setattr(
    "app.routes.github._upstream_default_branch",
    lambda _repo, _slug: "main",
  )

  def fake_git(repo_path, *args, check=True):
    if args[:2] == ("rev-parse", "--verify"):
      return _cp(next(tips) + "\n")
    if args[:2] == ("merge-base", "--is-ancestor"):
      assert args[2:] == (upstream_sha, ahead_sha)
      return _cp(returncode=0)
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)
  monkeypatch.setattr(
    "app.routes.github._gh",
    lambda _repo, *args, **kwargs: gh_calls.append(args) or _cp(""),
  )

  current = _inspect_owner_fork_default_branch(
    repo,
    "octocat/app-demo",
    upstream_branch="main",
    upstream_sha=upstream_sha,
  )
  ahead = _inspect_owner_fork_default_branch(
    repo,
    "octocat/app-demo",
    upstream_branch="main",
    upstream_sha=upstream_sha,
  )

  assert current["last_submit_fork_sync"] == "current"
  assert ahead["last_submit_fork_sync"] == "contains-upstream"
  assert gh_calls == []


def test_sync_owner_fork_with_workflow_scope_verifies_fast_forward(
  tmp_path, monkeypatch,
):
  from app.routes.github import _sync_owner_fork_with_workflow_scope

  repo = tmp_path / "repo"
  repo.mkdir()
  gh_calls = []

  def fake_gh(repo_path, *args, check=True):
    gh_calls.append(args)
    return _cp("")

  monkeypatch.setattr("app.routes.github._gh", fake_gh)
  monkeypatch.setattr(
    "app.routes.github._inspect_owner_fork_default_branch",
    lambda *_args, **_kwargs: {
      "last_submit_fork_branch": "main",
      "last_submit_fork_sha": "d" * 40,
      "last_submit_fork_sync": "current",
    },
  )

  patch = _sync_owner_fork_with_workflow_scope(
    repo,
    "octocat/app-demo",
    upstream_branch="main",
    upstream_sha="d" * 40,
  )

  assert gh_calls == [(
    "api", "--method", "POST",
    "repos/octocat/app-demo/merge-upstream",
    "-f", "branch=main",
  )]
  assert patch["last_submit_fork_sync"] == "fast-forwarded"


def test_build_fork_compatible_topic_preserves_exact_reviewed_merge(tmp_path):
  from app.routes.github import (
    _build_fork_compatible_topic_commit,
    _reviewed_branch_diff,
  )

  repo = tmp_path / "repo"
  repo.mkdir()

  def git(*args, input_text=None):
    return subprocess.run(
      ["git", "-C", str(repo), *args],
      input=input_text,
      capture_output=True,
      text=True,
      check=True,
    ).stdout.strip()

  git("init", "-b", "main")
  (repo / ".github" / "workflows").mkdir(parents=True)
  (repo / ".github" / "workflows" / "test.yml").write_text("old workflow\n")
  (repo / "app.py").write_text("old\n")
  git("add", ".")
  git(
    "-c", "user.name=owner", "-c", "user.email=owner@example.com",
    "commit", "-m", "fork base",
  )
  fork_sha = git("rev-parse", "HEAD")

  (repo / ".github" / "workflows" / "test.yml").write_text("new workflow\n")
  git("add", ".github/workflows/test.yml")
  git(
    "-c", "user.name=owner", "-c", "user.email=owner@example.com",
    "commit", "-m", "upstream workflow change",
  )
  upstream_sha = git("rev-parse", "HEAD")
  git("checkout", "-b", "reviewed")
  (repo / "app.py").write_text("reviewed\n")
  git("add", "app.py")
  git(
    "-c", "user.name=owner", "-c", "user.email=owner@example.com",
    "commit", "-m", "Reviewed fix", "-m", (
      "Co-authored-by: Möbius Agent "
      "<mobius-agent@users.noreply.github.com>"
    ),
  )
  reviewed_sha = git("rev-parse", "HEAD")
  reviewed_diff = _reviewed_branch_diff(repo, upstream_sha, reviewed_sha)
  diff_path = tmp_path / "reviewed.diff"
  diff_path.write_bytes(reviewed_diff)
  expected_diff = hashlib.sha256(reviewed_diff).hexdigest()

  push_sha = _build_fork_compatible_topic_commit(
    repo,
    branch="reviewed",
    fork_sha=fork_sha,
    upstream_sha=upstream_sha,
    diff_path=diff_path,
    expected_diff=expected_diff,
    author_name="owner",
    author_email="owner@example.com",
  )

  assert git("rev-parse", "--abbrev-ref", "HEAD") == "reviewed"
  assert git("rev-parse", f"{push_sha}^") == fork_sha
  assert git("diff", "--name-only", f"{fork_sha}..{push_sha}") == "app.py"
  merged_tree = git("merge-tree", "--write-tree", upstream_sha, push_sha)
  assert hashlib.sha256(
    _reviewed_branch_diff(repo, upstream_sha, merged_tree)
  ).hexdigest() == expected_diff
  assert git("status", "--porcelain") == ""


def test_build_fork_compatible_topic_does_not_reset_if_detach_fails(
  tmp_path, monkeypatch,
):
  from app.routes.github import (
    ContributionSubmitError,
    _build_fork_compatible_topic_commit,
  )

  repo = tmp_path / "repo"
  repo.mkdir()
  calls = []

  def fake_git(repo_path, *args, check=True):
    calls.append(args)
    if args[:3] == ("log", "-1", "--format=%B"):
      return _cp(
        "Reviewed fix\n\nCo-authored-by: Möbius Agent "
        "<mobius-agent@users.noreply.github.com>\n"
      )
    if args[:3] == ("checkout", "-q", "--detach"):
      raise ContributionSubmitError("detach failed")
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)

  with pytest.raises(ContributionSubmitError, match="detach failed"):
    _build_fork_compatible_topic_commit(
      repo,
      branch="reviewed",
      fork_sha="a" * 40,
      upstream_sha="b" * 40,
      diff_path=tmp_path / "reviewed.diff",
      expected_diff="c" * 64,
      author_name="owner",
      author_email="owner@example.com",
    )

  assert not any(call[:2] == ("reset", "--hard") for call in calls)


def test_safe_repo_path_accepts_durable_contribution_roots():
  from app.routes.github import _safe_repo_path

  data_dir = Path(get_settings().data_dir)

  assert _safe_repo_path(str(data_dir / "apps" / "notes")) == (
    data_dir / "apps" / "notes"
  ).resolve()
  assert _safe_repo_path(str(data_dir / "platform")) == (
    data_dir / "platform"
  ).resolve()
  assert _safe_repo_path(str(data_dir / "platform" / ".worktrees" / "fix")) == (
    data_dir / "platform" / ".worktrees" / "fix"
  ).resolve()
  assert _safe_repo_path(str(data_dir / "contributions" / "rec" / "repo")) == (
    data_dir / "contributions" / "rec" / "repo"
  ).resolve()
  assert _safe_repo_path(str(data_dir / "contrib" / "mobius-fix-x")) == (
    data_dir / "contrib" / "mobius-fix-x"
  ).resolve()
  assert _safe_repo_path(
    str(data_dir / "contrib" / "audit-20260710-1617" / "scroll-intent-return")
  ) == (
    data_dir / "contrib" / "audit-20260710-1617" / "scroll-intent-return"
  ).resolve()


def test_safe_repo_path_rejects_non_durable_locations(tmp_path):
  from app.routes.github import ContributionSubmitError, _safe_repo_path

  with pytest.raises(ContributionSubmitError) as exc:
    _safe_repo_path(str(tmp_path / "repo"))

  assert "durable contribution folders" in exc.value.message
  assert "nothing was sent to GitHub" in exc.value.message

  data_dir = Path(get_settings().data_dir)

  # Component-wise ancestry, not string-prefix: a sibling dir sharing the
  # "contrib" prefix must not ride the allowlist.
  with pytest.raises(ContributionSubmitError):
    _safe_repo_path(str(data_dir / "contribXX" / "repo"))

  # A symlink under an allowed root resolves BEFORE the ancestry check, so it
  # cannot smuggle in a repo that really lives outside /data.
  outside = tmp_path / "outside-repo"
  outside.mkdir()
  contrib = data_dir / "contrib"
  contrib.mkdir(parents=True, exist_ok=True)
  link = contrib / "escape"
  link.symlink_to(outside)
  with pytest.raises(ContributionSubmitError):
    _safe_repo_path(str(link))


def test_cleanup_terminal_staging_checkout_only_removes_disposable_clone():
  from app.routes.github import _cleanup_terminal_staging_checkout

  data_dir = Path(get_settings().data_dir)
  disposable = data_dir / "contrib" / "terminal-cleanup" / "repo"
  (disposable / ".git").mkdir(parents=True)
  (disposable / "index.jsx").write_text("hello")
  record = {
    "status": "open",
    "plan": {"repo_path": str(disposable)},
  }
  assert _cleanup_terminal_staging_checkout(record) is False
  assert disposable.exists()
  record["status"] = "merged"
  assert _cleanup_terminal_staging_checkout(record) is True
  assert not disposable.exists()

  live_repo = data_dir / "apps" / "terminal-cleanup-live"
  (live_repo / ".git").mkdir(parents=True)
  record["plan"]["repo_path"] = str(live_repo)
  assert _cleanup_terminal_staging_checkout(record) is False
  assert live_repo.exists()


def test_ensure_owner_fork_remote_runs_in_repo_after_pinning_origin(
  tmp_path, monkeypatch,
):
  from app.routes.github import _ensure_owner_fork_remote

  repo = tmp_path / "repo"
  repo.mkdir()
  git_calls = []
  gh_calls = []
  fork_ready = False

  def fake_git(repo_path, *args, check=True):
    nonlocal fork_ready
    git_calls.append(args)
    if args == ("remote", "get-url", "fork"):
      if fork_ready:
        return _cp("https://github.com/octocat/app-demo-1.git\n")
      return _cp(returncode=1)
    if args == ("remote", "get-url", "origin"):
      return _cp("https://github.com/someone-else/app-demo.git\n")
    if args == (
      "remote", "set-url", "origin",
      "https://github.com/mobius-os/app-demo.git",
    ):
      return _cp("")
    return _cp("")

  def fake_gh(repo_path, *args, check=True):
    nonlocal fork_ready
    gh_calls.append(args)
    if args == ("repo", "fork", "--remote", "--remote-name", "fork"):
      fork_ready = True
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)
  monkeypatch.setattr("app.routes.github._gh", fake_gh)

  fork_slug = _ensure_owner_fork_remote(repo, "mobius-os/app-demo", "octocat")

  assert fork_slug == "octocat/app-demo-1"
  assert (
    "remote", "set-url", "origin",
    "https://github.com/mobius-os/app-demo.git",
  ) in git_calls
  assert ("repo", "fork", "--remote", "--remote-name", "fork") in gh_calls
  assert all("mobius-os/app-demo" not in call for call in gh_calls)


def _commit_metadata(
  sha,
  *,
  name="octocat",
  email="42+octocat@users.noreply.github.com",
  tree="reviewed-tree",
):
  return _cp(
    f"{sha}\x00{tree}\x00{name}\x00{email}\x00{name}\x00{email}"
    "\x002026-07-10T03:12:02+00:00\n"
  )


def test_submit_contribution_creates_review_ready_pr_from_prepared_record(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-1"
  repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  diff_text = "diff --git a/index.jsx b/index.jsx\n+hello\n"
  base = "b" * 40
  head = "a" * 40
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Polish demo",
    "branch": "fix/demo-polish",
    "created_at": "2026-07-09T00:00:00Z",
    "updated_at": "2026-07-09T00:00:00Z",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "title": "Polish demo",
      "body_draft": "## What\n\nPolishes the demo.",
      "branch": "fix/demo-polish",
      "repo_path": str(repo),
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
    },
  }
  _write_contribution(app_id, record_id, record, diff_text)

  monkeypatch.setattr("app.routes.github.shutil.which", lambda name: f"/bin/{name}")
  git_calls = []
  fork_ready = False

  def fake_git(repo_path, *args, check=True):
    nonlocal fork_ready
    git_calls.append(args)
    if (preflight := _submit_preflight_response(args)) is not None:
      return preflight
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp("develop\n")
    if args == ("status", "--porcelain"):
      return _cp("")
    if args == ("rev-parse", "fix/demo-polish"):
      return _cp(head + "\n")
    if args == ("rev-parse", "--verify", f"{base}^{{commit}}"):
      return _cp(base + "\n")
    if args == ("rev-parse", "--verify", f"{head}^{{commit}}"):
      return _cp(head + "\n")
    if args == (
      "-c", "core.quotePath=false",
      "diff",
      "--no-ext-diff",
      "--no-color",
      "--binary",
      "--full-index",
      "--src-prefix=a/",
      "--dst-prefix=b/",
      f"{base}..{head}",
    ):
      return _cp(diff_text)
    if args == ("log", "-1", "--format=%B", "fix/demo-polish"):
      return _cp(
        "Polish demo\n\n"
        "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>\n"
      )
    if args[:3] == ("show", "-s", "--format=%H%x00%T%x00%an%x00%ae%x00%cn%x00%ce%x00%aI"):
      return _commit_metadata(head)
    if args == ("remote", "get-url", "origin"):
      return _cp("https://github.com/mobius-os/app-demo.git\n")
    if args == ("remote", "get-url", "fork"):
      if fork_ready:
        return _cp("https://github.com/octocat/app-demo-1.git\n")
      return _cp(returncode=1)
    return _cp("")

  gh_calls = []

  def fake_gh(repo_path, *args, check=True):
    nonlocal fork_ready
    gh_calls.append(args)
    if args[:2] == ("repo", "fork"):
      fork_ready = True
      return _cp("")
    if args[:2] == ("pr", "list"):
      return _cp("[]")
    if args[:2] == ("pr", "create"):
      return _cp("https://github.com/mobius-os/app-demo/pull/42\n")
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)
  monkeypatch.setattr("app.routes.github._gh", fake_gh)

  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 200, r.text
  body = r.json()
  assert body["url"] == "https://github.com/mobius-os/app-demo/pull/42"
  assert body["number"] == 42
  assert body["record"]["status"] == "open"
  assert body["record"]["url"] == body["url"]
  assert ("repo", "fork", "--remote", "--remote-name", "fork") in gh_calls
  assert not any(call[:2] == ("remote", "set-url") for call in git_calls)
  create_call = next(call for call in gh_calls if call[:2] == ("pr", "create"))
  assert "--draft" not in create_call
  assert "octocat:fix/demo-polish" in create_call
  assert ("push", "fork", "HEAD:refs/heads/fix/demo-polish") in git_calls
  assert ("checkout", "-q", "develop") in git_calls

  stored = json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id) /
     "contributions" / f"{record_id}.json").read_text()
  )
  assert stored["status"] == "open"
  assert stored["number"] == 42
  assert stored["head_repository"] == "octocat/app-demo-1"


def test_submit_contribution_normalizes_fallback_author_before_push(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-fallback-author"
  repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  diff_text = "diff --git a/index.jsx b/index.jsx\n+hello\n"
  base = "b" * 40
  old_head = "a" * 40
  new_head = "c" * 40
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Polish demo",
    "branch": "fix/demo-polish",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "title": "Polish demo",
      "body_draft": "Body",
      "branch": "fix/demo-polish",
      "repo_path": str(repo),
      "base_sha": base,
      "head_sha": old_head,
      "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
    },
  }
  _write_contribution(app_id, record_id, record, diff_text)
  monkeypatch.setattr("app.routes.github.shutil.which", lambda name: f"/bin/{name}")

  git_calls = []
  normalized = False

  def fake_git(repo_path, *args, check=True):
    nonlocal normalized
    git_calls.append(args)
    if (preflight := _submit_preflight_response(args)) is not None:
      return preflight
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp("main\n")
    if args == ("status", "--porcelain"):
      return _cp("")
    if args == ("rev-parse", "fix/demo-polish"):
      return _cp((new_head if normalized else old_head) + "\n")
    if args == ("rev-parse", "HEAD"):
      return _cp((new_head if normalized else old_head) + "\n")
    if args == ("rev-parse", "--verify", f"{base}^{{commit}}"):
      return _cp(base + "\n")
    if args == ("rev-parse", "--verify", f"{old_head}^{{commit}}"):
      return _cp(old_head + "\n")
    if args == (
      "-c", "core.quotePath=false",
      "diff",
      "--no-ext-diff",
      "--no-color",
      "--binary",
      "--full-index",
      "--src-prefix=a/",
      "--dst-prefix=b/",
      f"{base}..{old_head}",
    ):
      return _cp(diff_text)
    if args == (
      "-c", "core.quotePath=false",
      "diff",
      "--no-ext-diff",
      "--no-color",
      "--binary",
      "--full-index",
      "--src-prefix=a/",
      "--dst-prefix=b/",
      f"{base}..{new_head}",
    ):
      return _cp(diff_text)
    if args == ("log", "-1", "--format=%B", "fix/demo-polish"):
      return _cp(
        "Polish demo\n\n"
        "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>\n"
      )
    if args[:3] == ("show", "-s", "--format=%H%x00%T%x00%an%x00%ae%x00%cn%x00%ce%x00%aI"):
      if normalized:
        return _commit_metadata(new_head)
      return _commit_metadata(
        old_head,
        name="Mobius Agent",
        email="agent@mobius",
      )
    if args[:9] == (
      "-c", "user.name=octocat",
      "-c", "user.email=42+octocat@users.noreply.github.com",
      "commit", "--amend", "--no-edit", "--no-gpg-sign", "--author",
    ):
      assert args[9] == "octocat <42+octocat@users.noreply.github.com>"
      normalized = True
      return _cp("")
    if args == ("remote", "get-url", "fork"):
      return _cp("https://github.com/octocat/app-demo.git\n")
    if args[:1] == ("push",):
      assert normalized
      return _cp("")
    return _cp("")

  def fake_gh(repo_path, *args, check=True):
    if args[:2] == ("pr", "list"):
      return _cp("[]")
    if args[:2] == ("pr", "create"):
      return _cp("https://github.com/mobius-os/app-demo/pull/44\n")
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)
  monkeypatch.setattr("app.routes.github._gh", fake_gh)

  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 200, r.text
  body = r.json()
  assert body["record"]["head_sha"] == new_head
  assert body["record"]["plan"]["head_sha"] == new_head
  assert body["record"]["plan"]["attribution_normalized_from"] == old_head
  assert ("push", "fork", "HEAD:refs/heads/fix/demo-polish") in git_calls


def test_submit_contribution_replaces_stale_fork_remote_before_push(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-stale-fork"
  repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  diff_text = "diff --git a/index.jsx b/index.jsx\n+hello\n"
  base = "b" * 40
  head = "a" * 40
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Polish demo",
    "branch": "fix/demo-polish",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "title": "Polish demo",
      "body_draft": "Body",
      "branch": "fix/demo-polish",
      "repo_path": str(repo),
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
    },
  }
  _write_contribution(app_id, record_id, record, diff_text)
  monkeypatch.setattr("app.routes.github.shutil.which", lambda name: f"/bin/{name}")

  git_calls = []
  fork_fixed = False

  def fake_git(repo_path, *args, check=True):
    nonlocal fork_fixed
    git_calls.append(args)
    if (preflight := _submit_preflight_response(args)) is not None:
      return preflight
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp("main\n")
    if args == ("status", "--porcelain"):
      return _cp("")
    if args == ("rev-parse", "fix/demo-polish"):
      return _cp(head + "\n")
    if args == ("rev-parse", "--verify", f"{base}^{{commit}}"):
      return _cp(base + "\n")
    if args == ("rev-parse", "--verify", f"{head}^{{commit}}"):
      return _cp(head + "\n")
    if args == (
      "-c", "core.quotePath=false",
      "diff",
      "--no-ext-diff",
      "--no-color",
      "--binary",
      "--full-index",
      "--src-prefix=a/",
      "--dst-prefix=b/",
      f"{base}..{head}",
    ):
      return _cp(diff_text)
    if args == ("log", "-1", "--format=%B", "fix/demo-polish"):
      return _cp(
        "Polish demo\n\n"
        "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>\n"
      )
    if args[:3] == ("show", "-s", "--format=%H%x00%T%x00%an%x00%ae%x00%cn%x00%ce%x00%aI"):
      return _commit_metadata(head)
    if args == ("remote", "get-url", "origin"):
      return _cp("https://github.com/mobius-os/app-demo.git\n")
    if args == ("remote", "get-url", "fork"):
      if fork_fixed:
        return _cp("git@github.com:octocat/app-demo-1.git\n")
      return _cp("https://github.com/someone-else/app-demo.git\n")
    if args == ("remote", "remove", "fork"):
      return _cp("")
    return _cp("")

  gh_calls = []

  def fake_gh(repo_path, *args, check=True):
    nonlocal fork_fixed
    gh_calls.append(args)
    if args[:2] == ("repo", "fork"):
      fork_fixed = True
      return _cp("")
    if args[:2] == ("pr", "list"):
      return _cp("[]")
    if args[:2] == ("pr", "create"):
      return _cp("https://github.com/mobius-os/app-demo/pull/43\n")
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)
  monkeypatch.setattr("app.routes.github._gh", fake_gh)

  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 200, r.text
  assert ("remote", "remove", "fork") in git_calls
  assert ("repo", "fork", "--remote", "--remote-name", "fork") in gh_calls
  assert not any(call[:2] == ("remote", "set-url") for call in git_calls)
  assert ("push", "fork", "HEAD:refs/heads/fix/demo-polish") in git_calls


def test_submit_contribution_stack_opens_ordered_incremental_prs(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  stack_id = "chat-reliability"
  base = "b" * 40
  parent_head = "a" * 40
  child_head = "c" * 40
  record_ids = ["stack-chat-01", "stack-chat-02"]
  specs = [
    (record_ids[0], 1, "main", "", base, parent_head, "01-stream"),
    (
      record_ids[1], 2, f"stack/{stack_id}/01-stream", record_ids[0],
      parent_head, child_head, "02-settlement",
    ),
  ]
  for record_id, position, base_branch, parent_id, base_sha, head_sha, suffix in specs:
    repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
    (repo / ".git").mkdir(parents=True)
    diff_text = f"diff --git a/{suffix} b/{suffix}\n+reviewed\n"
    record = {
      "id": record_id,
      "type": "pr",
      "repo": "mobius-os/mobius",
      "status": "prepared",
      "title": f"Layer {position}",
      "branch": f"stack/{stack_id}/{suffix}",
      "plan": {
        "action": "pr",
        "repo": "mobius-os/mobius",
        "title": f"Layer {position}",
        "body_draft": f"Reviewed layer {position}.",
        "branch": f"stack/{stack_id}/{suffix}",
        "repo_path": str(repo),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
        "stack": {
          "id": stack_id,
          "name": "Chat reliability",
          "position": position,
          "total": 2,
          "parent_record_id": parent_id,
          "base_branch": base_branch,
        },
      },
    }
    _write_contribution(app_id, record_id, record, diff_text)

  monkeypatch.setattr(
    "app.routes.github._preflight_prepared_stack",
    lambda rows: None,
  )
  calls = []

  def fake_submit(record, diff_path, *, direct_base_branch=None):
    calls.append((record["id"], direct_base_branch, diff_path.name))
    number = 70 + len(calls)
    return (
      f"https://github.com/mobius-os/mobius/pull/{number}",
      number,
      {
        "last_submit_mode": "stack",
        "last_submit_base_branch": direct_base_branch,
      },
    )

  monkeypatch.setattr("app.routes.github._submit_prepared_pr", fake_submit)

  r = client.post(
    f"/api/github/contributions/{app_id}/submit-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert r.status_code == 200, r.text
  assert calls == [
    (record_ids[0], "main", f"{record_ids[0]}.diff"),
    (record_ids[1], f"stack/{stack_id}/01-stream", f"{record_ids[1]}.diff"),
  ]
  body = r.json()
  assert [record["status"] for record in body["records"]] == ["open", "open"]
  assert [item["number"] for item in body["submitted"]] == [71, 72]
  assert body["records"][1]["last_submit_base_branch"] == (
    f"stack/{stack_id}/01-stream"
  )


def test_submit_contribution_stack_preserves_open_parent_when_child_fails(
  client, owner_token, monkeypatch,
):
  from app.routes.github import ContributionSubmitError

  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  stack_id = "partial-stack"
  record_ids = ["partial-stack-01", "partial-stack-02"]
  parent_head = "a" * 40
  specs = [
    (record_ids[0], 1, "main", "", "b" * 40, parent_head),
    (
      record_ids[1], 2, f"stack/{stack_id}/01-parent", record_ids[0],
      parent_head, "c" * 40,
    ),
  ]
  for record_id, position, base_branch, parent_id, base_sha, head_sha in specs:
    repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
    (repo / ".git").mkdir(parents=True)
    branch = f"stack/{stack_id}/0{position}-" + (
      "parent" if position == 1 else "child"
    )
    diff_text = f"diff --git a/{record_id} b/{record_id}\n+reviewed\n"
    record = {
      "id": record_id,
      "type": "pr",
      "repo": "mobius-os/mobius",
      "status": "prepared",
      "title": f"Layer {position}",
      "branch": branch,
      "plan": {
        "action": "pr",
        "repo": "mobius-os/mobius",
        "title": f"Layer {position}",
        "body_draft": f"Reviewed layer {position}.",
        "branch": branch,
        "repo_path": str(repo),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
        "stack": {
          "id": stack_id,
          "position": position,
          "total": 2,
          "parent_record_id": parent_id,
          "base_branch": base_branch,
        },
      },
    }
    _write_contribution(app_id, record_id, record, diff_text)

  monkeypatch.setattr("app.routes.github._preflight_prepared_stack", lambda rows: None)
  calls = []

  def fake_submit(record, diff_path, *, direct_base_branch=None):
    calls.append(record["id"])
    if len(calls) == 1:
      return (
        "https://github.com/mobius-os/mobius/pull/81",
        81,
        {"last_submit_mode": "stack"},
      )
    raise ContributionSubmitError("Child PR could not be opened.")

  monkeypatch.setattr("app.routes.github._submit_prepared_pr", fake_submit)
  r = client.post(
    f"/api/github/contributions/{app_id}/submit-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert r.status_code == 409, r.text
  detail = r.json()["detail"]
  assert calls == record_ids
  assert detail["submitted"] == [{
    "id": record_ids[0],
    "url": "https://github.com/mobius-os/mobius/pull/81",
    "number": 81,
  }]
  assert [record["status"] for record in detail["records"]] == [
    "open", "prepared",
  ]
  assert detail["records"][1]["last_submit_error"] == (
    "Child PR could not be opened."
  )


def test_submit_contribution_stack_rejects_unapproved_draft_layer():
  from app.routes.github import ContributionSubmitError, _validate_stack_records

  stack_id = "approval-boundary"
  parent_head = "a" * 40
  records = []
  for position, status in ((1, "draft"), (2, "prepared")):
    branch = f"stack/{stack_id}/0{position}-layer"
    records.append({
      "id": f"approval-{position}",
      "type": "pr",
      "repo": "mobius-os/mobius",
      "status": status,
      "branch": branch,
      "plan": {
        "action": "pr",
        "repo": "mobius-os/mobius",
        "branch": branch,
        "base_sha": "b" * 40 if position == 1 else parent_head,
        "head_sha": parent_head if position == 1 else "c" * 40,
        "stack": {
          "id": stack_id,
          "position": position,
          "total": 2,
          "parent_record_id": "" if position == 1 else "approval-1",
          "base_branch": (
            "main" if position == 1 else f"stack/{stack_id}/01-layer"
          ),
        },
      },
    })

  with pytest.raises(ContributionSubmitError, match="ready, open, or already merged"):
    _validate_stack_records(records)


def test_submit_contribution_stack_rejects_broken_parent_link_before_claim(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  stack_id = "broken-chain"
  record_ids = ["broken-01", "broken-02"]
  for position, record_id in enumerate(record_ids, 1):
    repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
    (repo / ".git").mkdir(parents=True)
    base_sha = "b" * 40 if position == 1 else "9" * 40
    head_sha = "a" * 40 if position == 1 else "c" * 40
    branch = f"stack/{stack_id}/0{position}-layer"
    record = {
      "id": record_id,
      "type": "pr",
      "repo": "mobius-os/mobius",
      "status": "prepared",
      "branch": branch,
      "plan": {
        "action": "pr",
        "repo": "mobius-os/mobius",
        "title": "Layer",
        "body_draft": "Body",
        "branch": branch,
        "repo_path": str(repo),
        "base_sha": base_sha,
        "head_sha": head_sha,
        "diff_sha256": "d" * 64,
        "stack": {
          "id": stack_id,
          "position": position,
          "total": 2,
          "parent_record_id": record_ids[0] if position == 2 else "",
          "base_branch": (
            f"stack/{stack_id}/01-layer" if position == 2 else "main"
          ),
        },
      },
    }
    _write_contribution(app_id, record_id, record, "reviewed")

  called = False

  def fake_preflight(_rows):
    nonlocal called
    called = True

  monkeypatch.setattr("app.routes.github._preflight_prepared_stack", fake_preflight)
  r = client.post(
    f"/api/github/contributions/{app_id}/submit-stack",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"record_ids": record_ids},
  )

  assert r.status_code == 409
  assert "not based on its reviewed parent" in r.json()["detail"]
  assert called is False
  for record_id in record_ids:
    stored = json.loads(
      (Path(get_settings().data_dir) / "apps" / str(app_id) /
       "contributions" / f"{record_id}.json").read_text()
    )
    assert stored["status"] == "prepared"


def test_direct_stack_layer_pushes_upstream_and_uses_reviewed_base(
  tmp_path, monkeypatch,
):
  from app.routes.github import _submit_prepared_pr

  _write_token(login="octocat")
  record_id = "direct-stack-layer"
  repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  branch = "stack/demo-flow/01-model"
  base = "b" * 40
  head = "a" * 40
  diff_text = "diff --git a/model.py b/model.py\n+reviewed\n"
  diff_path = tmp_path / "layer.diff"
  diff_path.write_text(diff_text)
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "submitting",
    "title": "Model layer",
    "branch": branch,
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "title": "Model layer",
      "body_draft": "Reviewed model layer.",
      "branch": branch,
      "repo_path": str(repo),
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
    },
  }
  monkeypatch.setattr("app.routes.github.shutil.which", lambda name: f"/bin/{name}")
  git_calls = []

  def fake_git(repo_path, *args, check=True):
    git_calls.append(args)
    if (preflight := _submit_preflight_response(args)) is not None:
      return preflight
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp(branch + "\n")
    if args == ("status", "--porcelain"):
      return _cp("")
    if args == ("rev-parse", branch) or args == ("rev-parse", "HEAD"):
      return _cp(head + "\n")
    if args == ("rev-parse", "--verify", f"{base}^{{commit}}"):
      return _cp(base + "\n")
    if args == ("rev-parse", "--verify", f"{head}^{{commit}}"):
      return _cp(head + "\n")
    if args[-1:] == (f"{base}..{head}",) and "diff" in args:
      return _cp(diff_text)
    if args == ("log", "-1", "--format=%B", branch):
      return _cp(
        "Model layer\n\n"
        "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>\n"
      )
    if args[:3] == (
      "show", "-s", "--format=%H%x00%T%x00%an%x00%ae%x00%cn%x00%ce%x00%aI",
    ):
      return _commit_metadata(head)
    return _cp("")

  gh_calls = []

  def fake_gh(repo_path, *args, check=True):
    gh_calls.append(args)
    if args[:2] == ("repo", "view"):
      return _cp("main\n")
    if args[:2] == ("api", "repos/mobius-os/app-demo"):
      return _cp("true\n")
    if args[:2] == ("pr", "list"):
      return _cp("[]")
    if args[:2] == ("pr", "create"):
      return _cp("https://github.com/mobius-os/app-demo/pull/73\n")
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)
  monkeypatch.setattr("app.routes.github._gh", fake_gh)

  url, number, patch = _submit_prepared_pr(
    record,
    diff_path,
    direct_base_branch="main",
  )

  assert url.endswith("/pull/73")
  assert number == 73
  assert patch["last_submit_mode"] == "stack"
  assert patch["last_submit_base_branch"] == "main"
  assert (
    "push", "https://github.com/mobius-os/app-demo.git",
    f"HEAD:refs/heads/{branch}",
  ) in git_calls
  create = next(call for call in gh_calls if call[:2] == ("pr", "create"))
  assert create[create.index("-H") + 1] == branch
  assert create[-2:] == ("--base", "main")
  assert not any(call[:2] == ("repo", "fork") for call in gh_calls)


def test_submit_contribution_rejects_branch_diff_mismatch(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-diff-mismatch"
  repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  reviewed_diff = "diff --git a/index.jsx b/index.jsx\n+reviewed\n"
  branch_diff = "diff --git a/index.jsx b/index.jsx\n+not-reviewed\n"
  base = "b" * 40
  head = "a" * 40
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Polish demo",
    "branch": "fix/demo-polish",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "title": "Polish demo",
      "body_draft": "Body",
      "branch": "fix/demo-polish",
      "repo_path": str(repo),
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": hashlib.sha256(reviewed_diff.encode()).hexdigest(),
    },
  }
  _write_contribution(app_id, record_id, record, reviewed_diff)
  monkeypatch.setattr("app.routes.github.shutil.which", lambda name: f"/bin/{name}")
  git_calls = []

  def fake_git(repo_path, *args, check=True):
    git_calls.append(args)
    if (preflight := _submit_preflight_response(args)) is not None:
      return preflight
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp("main\n")
    if args == ("status", "--porcelain"):
      return _cp("")
    if args == ("rev-parse", "fix/demo-polish"):
      return _cp(head + "\n")
    if args == ("rev-parse", "--verify", f"{base}^{{commit}}"):
      return _cp(base + "\n")
    if args == ("rev-parse", "--verify", f"{head}^{{commit}}"):
      return _cp(head + "\n")
    if args == (
      "-c", "core.quotePath=false",
      "diff",
      "--no-ext-diff",
      "--no-color",
      "--binary",
      "--full-index",
      "--src-prefix=a/",
      "--dst-prefix=b/",
      f"{base}..{head}",
    ):
      return _cp(branch_diff)
    if args == ("log", "-1", "--format=%B", "fix/demo-polish"):
      return _cp(
        "Polish demo\n\n"
        "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>\n"
      )
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)
  monkeypatch.setattr("app.routes.github._gh", lambda *args, **kwargs: _cp(""))

  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 409
  detail = r.json()["detail"]
  assert "does not match the branch" in detail["message"]
  assert detail["record"]["status"] == "prepared"
  assert not any(call[:1] == ("push",) for call in git_calls)


def test_submit_contribution_rejects_unmergeable_branch_before_push(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-merge-conflict"
  repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  diff_text = "diff --git a/index.jsx b/index.jsx\n+hello\n"
  base = "b" * 40
  head = "a" * 40
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Polish demo",
    "branch": "fix/demo-polish",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "title": "Polish demo",
      "body_draft": "Body",
      "branch": "fix/demo-polish",
      "repo_path": str(repo),
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
    },
  }
  _write_contribution(app_id, record_id, record, diff_text)
  monkeypatch.setattr("app.routes.github.shutil.which", lambda name: f"/bin/{name}")
  git_calls = []

  def fake_git(repo_path, *args, check=True):
    git_calls.append(args)
    if (preflight := _submit_preflight_response(args, merge_conflict=True)) is not None:
      return preflight
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp("main\n")
    if args == ("status", "--porcelain"):
      return _cp("")
    if args == ("rev-parse", "fix/demo-polish"):
      return _cp(head + "\n")
    if args == ("rev-parse", "--verify", f"{base}^{{commit}}"):
      return _cp(base + "\n")
    if args == ("rev-parse", "--verify", f"{head}^{{commit}}"):
      return _cp(head + "\n")
    if args == (
      "-c", "core.quotePath=false",
      "diff",
      "--no-ext-diff",
      "--no-color",
      "--binary",
      "--full-index",
      "--src-prefix=a/",
      "--dst-prefix=b/",
      f"{base}..{head}",
    ):
      return _cp(diff_text)
    if args == ("log", "-1", "--format=%B", "fix/demo-polish"):
      return _cp(
        "Polish demo\n\n"
        "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>\n"
      )
    if args[:3] == ("show", "-s", "--format=%H%x00%T%x00%an%x00%ae%x00%cn%x00%ce%x00%aI"):
      return _commit_metadata(head)
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)
  monkeypatch.setattr("app.routes.github._gh", lambda *args, **kwargs: _cp(""))

  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert r.status_code == 409
  detail = r.json()["detail"]
  assert "no longer merges cleanly" in detail["message"]
  assert detail["record"]["status"] == "prepared"
  assert detail["record"]["last_submit_upstream_branch"] == "main"
  assert detail["record"]["last_submit_upstream_sha"] == _UPSTREAM_SHA
  assert not any(call[:1] == ("push",) for call in git_calls)


def test_submit_contribution_records_public_branch_after_pr_create_failure(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-push-then-fail"
  repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  diff_text = "diff --git a/index.jsx b/index.jsx\n+hello\n"
  base = "b" * 40
  head = "a" * 40
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Polish demo",
    "branch": "fix/demo-polish",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "title": "Polish demo",
      "body_draft": "Body",
      "branch": "fix/demo-polish",
      "repo_path": str(repo),
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
    },
  }
  _write_contribution(app_id, record_id, record, diff_text)
  monkeypatch.setattr("app.routes.github.shutil.which", lambda name: f"/bin/{name}")

  def fake_git(repo_path, *args, check=True):
    if (preflight := _submit_preflight_response(args)) is not None:
      return preflight
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp("main\n")
    if args == ("status", "--porcelain"):
      return _cp("")
    if args == ("rev-parse", "fix/demo-polish"):
      return _cp(head + "\n")
    if args == ("rev-parse", "--verify", f"{base}^{{commit}}"):
      return _cp(base + "\n")
    if args == ("rev-parse", "--verify", f"{head}^{{commit}}"):
      return _cp(head + "\n")
    if args == (
      "-c", "core.quotePath=false",
      "diff",
      "--no-ext-diff",
      "--no-color",
      "--binary",
      "--full-index",
      "--src-prefix=a/",
      "--dst-prefix=b/",
      f"{base}..{head}",
    ):
      return _cp(diff_text)
    if args == ("log", "-1", "--format=%B", "fix/demo-polish"):
      return _cp(
        "Polish demo\n\n"
        "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>\n"
      )
    if args[:3] == ("show", "-s", "--format=%H%x00%T%x00%an%x00%ae%x00%cn%x00%ce%x00%aI"):
      return _commit_metadata(head)
    if args == ("remote", "get-url", "fork"):
      return _cp("https://github.com/octocat/app-demo.git\n")
    if args[:1] == ("push",):
      return _cp("")
    return _cp("")

  def fake_gh(repo_path, *args, check=True):
    if args[:2] == ("pr", "list"):
      return _cp("[]")
    if args[:2] == ("pr", "create"):
      from app.routes.github import ContributionSubmitError
      raise ContributionSubmitError("create failed")
    return _cp("")

  monkeypatch.setattr("app.routes.github._git", fake_git)
  monkeypatch.setattr("app.routes.github._gh", fake_gh)

  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 409
  detail = r.json()["detail"]
  assert "branch was pushed" in detail["message"]
  assert detail["record"]["status"] == "prepared"
  assert detail["record"]["last_submit_stage"] == "pushed"
  assert (
    detail["record"]["last_pushed_branch_url"] ==
    "https://github.com/octocat/app-demo/tree/fix/demo-polish"
  )


def test_submit_contribution_rejects_other_app_scoped_token(
  client, owner_token,
):
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _, other_app_token = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-app-token"
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Polish demo",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "title": "Polish demo",
      "body_draft": "Body",
      "branch": "fix/demo-polish",
      "repo_path": str(
        Path(get_settings().data_dir) / "contributions" / record_id / "repo"
      ),
      "head_sha": "abc123",
    },
  }
  _write_contribution(app_id, record_id, record)

  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {other_app_token}"},
  )
  assert r.status_code == 403
  assert "own storage" in r.json()["detail"]

  stored = json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id) /
     "contributions" / f"{record_id}.json").read_text()
  )
  assert stored["status"] == "prepared"
  assert "last_submit_error" not in stored


def test_submit_contribution_rejects_app_without_github_access(
  client, owner_token,
):
  app_id, app_token = _app_token(client, owner_token, github_access=False)
  record_id = "rec-pr-no-github-access"
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Polish demo",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "title": "Polish demo",
      "body_draft": "Body",
      "branch": "fix/demo-polish",
      "repo_path": str(
        Path(get_settings().data_dir) / "contributions" / record_id / "repo"
      ),
      "head_sha": "abc123",
    },
  }
  _write_contribution(app_id, record_id, record)

  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert r.status_code == 403
  assert "github_access" in r.json()["detail"]


def test_submit_contribution_rolls_back_unready_record(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-unready"
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Polish demo",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "title": "Polish demo",
      "body_draft": "Body",
      "branch": "fix/demo-polish",
      "head_sha": "abc123",
    },
  }
  _write_contribution(app_id, record_id, record)
  monkeypatch.setattr("app.routes.github.shutil.which", lambda name: f"/bin/{name}")

  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 409
  detail = r.json()["detail"]
  assert "repo_path" in detail["message"]
  assert detail["record"]["status"] == "prepared"
  assert "last_submit_error" in detail["record"]

  stored = json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id) /
     "contributions" / f"{record_id}.json").read_text()
  )
  assert stored["status"] == "prepared"
  assert "last_submit_error" in stored


# --- contribution CI feedback loop (checks refresh + classification) ---


_HEAD_SHA = "f" * 40


def _pr_node(
  *, number=4, state="OPEN", is_draft=False, base_ref="main",
  head_sha=_HEAD_SHA, rollup_state="FAILURE", contexts=None,
):
  """A statusCheckRollup GraphQL `pullRequest` node for the mock transport."""
  if contexts is None:
    contexts = [
      {"__typename": "CheckRun", "name": "e2e", "conclusion": "FAILURE",
       "status": "COMPLETED",
       "detailsUrl": "https://github.com/mobius-os/app-demo/runs/e2e"},
      {"__typename": "CheckRun", "name": "core-apps-sync",
       "conclusion": "FAILURE", "status": "COMPLETED",
       "detailsUrl": "https://github.com/mobius-os/app-demo/runs/cas"},
      {"__typename": "CheckRun", "name": "backend", "conclusion": "SUCCESS",
       "status": "COMPLETED",
       "detailsUrl": "https://github.com/mobius-os/app-demo/runs/be"},
    ]
  rollup = None
  if rollup_state is not None or contexts:
    rollup = {"state": rollup_state, "contexts": {"nodes": contexts}}
  return {
    "number": number,
    "state": state,
    "isDraft": is_draft,
    "baseRefName": base_ref,
    "url": f"https://github.com/mobius-os/app-demo/pull/{number}",
    "commits": {"nodes": [{"commit": {
      "oid": head_sha,
      "statusCheckRollup": rollup,
    }}]},
  }


def test_parse_rollup_extracts_jobs_head_and_state():
  from app.routes.github import _parse_rollup

  parsed = _parse_rollup(_pr_node(contexts=[
    {"__typename": "CheckRun", "name": "e2e", "conclusion": "FAILURE",
     "status": "COMPLETED", "detailsUrl": "https://x/runs/e2e"},
    {"__typename": "StatusContext", "context": "legacy-ci", "state": "SUCCESS",
     "targetUrl": "https://x/status/legacy"},
    {"__typename": "CheckRun", "name": "", "conclusion": "SUCCESS"},
  ]))
  assert parsed["pr_state"] == "OPEN"
  assert parsed["head_sha"] == _HEAD_SHA
  assert parsed["base_ref"] == "main"
  assert parsed["rollup_state"] == "FAILURE"
  by_name = {j["name"]: j for j in parsed["jobs"]}
  # Nameless contexts are dropped; both CheckRun and StatusContext normalize.
  assert set(by_name) == {"e2e", "legacy-ci"}
  assert by_name["e2e"]["conclusion"] == "FAILURE"
  assert by_name["e2e"]["url"] == "https://x/runs/e2e"
  assert by_name["legacy-ci"]["conclusion"] == "SUCCESS"
  assert by_name["legacy-ci"]["url"] == "https://x/status/legacy"


def test_parse_rollup_handles_missing_pr_and_empty_rollup():
  from app.routes.github import _parse_rollup

  assert _parse_rollup(None) is None
  assert _parse_rollup("nope") is None
  # PR with no checks reported yet: resolvable, but zero jobs, null state.
  empty = _parse_rollup(_pr_node(rollup_state=None, contexts=[]))
  assert empty["jobs"] == []
  assert empty["rollup_state"] is None
  assert empty["head_sha"] == _HEAD_SHA


def test_classify_jobs_inherited_suspect_unknown():
  from app.routes.github import _classify_jobs

  jobs = [
    {"name": "e2e", "conclusion": "FAILURE"},
    {"name": "core-apps-sync", "conclusion": "FAILURE"},
    {"name": "backend", "conclusion": "SUCCESS"},
  ]
  # core-apps-sync is also red on base → inherited; e2e is green on base →
  # suspect; passing jobs get no classification.
  _classify_jobs(jobs, {"core-apps-sync"})
  assert jobs[0]["classification"] == "suspect-pr-caused"
  assert jobs[1]["classification"] == "inherited"
  assert "classification" not in jobs[2]

  # No base data at all → every failing job is unknown.
  unknown = [{"name": "e2e", "conclusion": "FAILURE"}]
  _classify_jobs(unknown, None)
  assert unknown[0]["classification"] == "unknown"

  # Empty base set (base is green) → the failure is suspect, not inherited.
  suspect = [{"name": "e2e", "conclusion": "FAILURE"}]
  _classify_jobs(suspect, set())
  assert suspect[0]["classification"] == "suspect-pr-caused"


def test_build_pr_checks_query_aliases_and_variables():
  from app.routes.github import _build_pr_checks_query

  query, variables = _build_pr_checks_query([
    ("pr0", "mobius-os", "app-demo", 4),
    ("pr1", "mobius-os", "app-notes", 7),
  ])
  assert variables == {
    "pr0o": "mobius-os", "pr0n": "app-demo", "pr0p": 4,
    "pr1o": "mobius-os", "pr1n": "app-notes", "pr1p": 7,
  }
  assert "pr0: repository(owner: $pr0o, name: $pr0n)" in query
  assert "pullRequest(number: $pr0p)" in query
  assert "pr1: repository(owner: $pr1o, name: $pr1n)" in query
  assert "fragment prChecks on PullRequest" in query
  # No repo slug is interpolated into the query text (injection guard).
  assert "app-demo" not in query


def test_checks_failure_notification_payload_is_self_contained():
  from app.routes.github import _checks_failure_notification

  record = {
    "repo": "mobius-os/app-demo", "number": 4,
    "url": "https://github.com/mobius-os/app-demo/pull/4",
  }
  checks = {
    "head_sha": _HEAD_SHA,
    "jobs": [
      {"name": "e2e", "conclusion": "FAILURE",
       "classification": "suspect-pr-caused",
       "url": "https://github.com/mobius-os/app-demo/runs/e2e"},
      {"name": "core-apps-sync", "conclusion": "FAILURE",
       "classification": "inherited",
       "url": "https://github.com/mobius-os/app-demo/runs/cas"},
      {"name": "backend", "conclusion": "SUCCESS"},
    ],
  }
  n = _checks_failure_notification(record, checks)
  assert n["title"] == "PR checks failing: mobius-os/app-demo#4"
  # repo, PR number, head SHA, per-job name + URL + classification all present.
  assert "mobius-os/app-demo#4" in n["body"]
  assert "fffffff" in n["body"]
  assert "e2e — suspect (PR-caused)" in n["body"]
  assert "core-apps-sync — inherited (also red on upstream main)" in n["body"]
  assert "https://github.com/mobius-os/app-demo/runs/e2e" in n["body"]
  # Passing jobs are not surfaced as failures.
  assert "backend" not in n["body"]
  assert n["target"] == record["url"]
  assert n["actions"][0]["target"] == record["url"]


def _write_open_pr_record(app_id, record_id="rec-open-pr", number=4):
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "open",
    "number": number,
    "url": f"https://github.com/mobius-os/app-demo/pull/{number}",
    "branch": "fix/demo",
    "plan": {"action": "pr", "repo": "mobius-os/app-demo"},
  }
  _write_contribution(app_id, record_id, record)
  return record_id


def _checks_refresh_handler(seen, *, pr_node=None):
  if pr_node is None:
    pr_node = _pr_node()

  def handler(request):
    url = str(request.url)
    if url == "https://api.github.com/graphql" and request.method == "POST":
      seen["graphql"] = json.loads(request.content)
      assert request.headers.get("authorization") == "Bearer gh-checks-tok"
      return httpx.Response(200, json={"data": {"pr0": {"pullRequest": pr_node}}})
    if (
      request.method == "GET"
      and url.startswith(
        "https://api.github.com/repos/mobius-os/app-demo/commits/main/check-runs"
      )
    ):
      seen["base_calls"] = seen.get("base_calls", 0) + 1
      # core-apps-sync is red on main (inherited); e2e is green (suspect).
      return httpx.Response(200, json={"check_runs": [
        {"name": "core-apps-sync", "conclusion": "failure"},
        {"name": "e2e", "conclusion": "success"},
        {"name": "backend", "conclusion": "success"},
      ]})
    return _fail(request)

  return handler


def _stored_checks(app_id, record_id):
  return json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id) /
     "contributions" / f"{record_id}.json").read_text()
  )["checks"]


def _all_notifications():
  from app import models
  from app.database import SessionLocal
  s = SessionLocal()
  try:
    return s.query(models.Notification).all()
  finally:
    s.close()


def test_refresh_requires_github_connection(client, owner_token):
  app_id, _ = _app_token(client, owner_token, github_access=True)
  r = client.post(f"/api/github/contributions/{app_id}/refresh",
                  headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 401
  assert "not connected" in r.json()["detail"].lower()


def test_refresh_no_records_is_noop(client, owner_token, monkeypatch):
  _write_token(token="gh-checks-tok")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  # No upstream call should happen when there are no tracked PRs.
  _install_mock_transport(monkeypatch, _fail)
  r = client.post(f"/api/github/contributions/{app_id}/refresh",
                  headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 200
  assert r.json() == {"refreshed": [], "notified": 0}


def test_refresh_persists_checks_classifies_and_notifies(
  client, owner_token, monkeypatch,
):
  _write_token(token="gh-checks-tok")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = _write_open_pr_record(app_id)
  seen = {}
  _install_mock_transport(monkeypatch, _checks_refresh_handler(seen))

  r = client.post(f"/api/github/contributions/{app_id}/refresh",
                  headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 200, r.text
  body = r.json()
  assert body["notified"] == 1
  assert len(body["refreshed"]) == 1

  # The batched query carried the PR ref as a variable, not interpolated.
  assert seen["graphql"]["variables"]["pr0p"] == 4

  checks = _stored_checks(app_id, record_id)
  assert checks["state"] == "FAILURE"
  assert checks["head_sha"] == _HEAD_SHA
  assert checks["pr_state"] == "OPEN"
  assert checks["base_ref"] == "main"
  assert checks["notified_sha"] == _HEAD_SHA
  by_name = {j["name"]: j for j in checks["jobs"]}
  assert by_name["e2e"]["classification"] == "suspect-pr-caused"
  assert by_name["core-apps-sync"]["classification"] == "inherited"
  # Passing jobs carry no classification.
  assert "classification" not in by_name["backend"]

  notes = _all_notifications()
  assert len(notes) == 1
  assert notes[0].source_type == "app"
  assert notes[0].source_id == str(app_id)
  assert "core-apps-sync — inherited" in notes[0].body
  assert "e2e — suspect" in notes[0].body
  assert notes[0].target == "https://github.com/mobius-os/app-demo/pull/4"


def test_refresh_dedupes_notification_on_unchanged_head(
  client, owner_token, monkeypatch,
):
  _write_token(token="gh-checks-tok")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _write_open_pr_record(app_id)
  seen = {}
  _install_mock_transport(monkeypatch, _checks_refresh_handler(seen))

  first = client.post(f"/api/github/contributions/{app_id}/refresh",
                      headers={"Authorization": f"Bearer {owner_token}"})
  assert first.json()["notified"] == 1
  # Second refresh, same red head SHA — must NOT re-notify (dedupe on
  # checks.notified_sha), and base check-runs are cached per repo per call.
  second = client.post(f"/api/github/contributions/{app_id}/refresh",
                       headers={"Authorization": f"Bearer {owner_token}"})
  assert second.status_code == 200
  assert second.json()["notified"] == 0
  assert len(_all_notifications()) == 1


def test_refresh_skips_non_open_and_success_without_notifying(
  client, owner_token, monkeypatch,
):
  _write_token(token="gh-checks-tok")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = _write_open_pr_record(app_id)
  # All green: checks persist, base branch is never queried, nothing notifies.
  green = _pr_node(rollup_state="SUCCESS", contexts=[
    {"__typename": "CheckRun", "name": "e2e", "conclusion": "SUCCESS",
     "status": "COMPLETED", "detailsUrl": "https://x/runs/e2e"},
  ])
  seen = {}
  _install_mock_transport(monkeypatch, _checks_refresh_handler(seen, pr_node=green))

  r = client.post(f"/api/github/contributions/{app_id}/refresh",
                  headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 200
  assert r.json()["notified"] == 0
  assert seen.get("base_calls", 0) == 0
  checks = _stored_checks(app_id, record_id)
  assert checks["state"] == "SUCCESS"
  assert "notified_sha" not in checks
  assert _all_notifications() == []
