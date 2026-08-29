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
import shutil
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import Response

from app import github_auth, github_pre_pr_checks, source_status
from app.contribution_errors import ContributionSubmitError
from app.config import get_settings
from app.database import checked_out_connections
from app.storage_io import atomic_write
from test_app_fixtures import create_local_app

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
  scopes=("public_repo", "workflow"), source="device",
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


def _app_token(
  client,
  owner_token,
  *,
  github_access=False,
  github_connect=False,
):
  """Create an app with independently reviewable GitHub grants."""
  app_id = create_local_app(
    client, {"Authorization": f"Bearer {owner_token}"},
    name="contribute-test", description="t",
  )["id"]
  if github_access or github_connect:
    # Set the column directly — the plain create path doesn't parse the
    # permission (that's the install path); the gate reads the row at
    # request time regardless (deps.get_owner_or_app_with_github_access).
    from app import models
    from app.database import SessionLocal
    s = SessionLocal()
    try:
      app = s.query(models.App).filter(models.App.id == app_id).first()
      app.github_access = bool(github_access)
      app.github_connect = bool(github_connect)
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


def _poll(client, auth, attempt_id):
  return client.post(
    "/api/github/connect/poll",
    headers=auth,
    json={"attempt_id": attempt_id},
  )


def _patch_device_flow(**changes):
  flow = github_auth.get_device_flow()
  assert flow is not None
  flow.update(changes)
  github_auth.set_device_flow(flow)
  return flow


# --- connect/start ----------------------------------------------------


def test_connect_start_requires_client_id(client, auth, monkeypatch):
  _set_client_id(monkeypatch, None)
  r = client.post("/api/github/connect/start", headers=auth)
  assert r.status_code == 409
  assert "GITHUB_OAUTH_CLIENT_ID" in r.json()["detail"]


@pytest.mark.asyncio
async def test_disconnected_start_never_publishes_attempt():
  class GoneRequest:
    async def is_disconnected(self):
      return True

  with pytest.raises(HTTPException) as caught:
    await github_routes._start_device_attempt(GoneRequest())

  assert getattr(caught.value, "status_code", None) == 499
  assert github_auth.get_device_flow() is None


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
  assert body["attempt_id"]
  assert body["expires_at"] > 0
  assert github_auth.get_device_flow()["device_code"] == "DEV"


def test_connect_start_bounds_invalid_provider_durations(
  client, auth, monkeypatch,
):
  _set_client_id(monkeypatch, "cid-123")

  def handler(request):
    if str(request.url) == _DEVICE_CODE_URL:
      return httpx.Response(200, json={
        "device_code": "DEV",
        "user_code": "WXYZ-1234",
        "verification_uri": "https://github.com/login/device",
        "interval": "not-a-number",
        "expires_in": 999999,
      })
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  response = client.post("/api/github/connect/start", headers=auth)

  assert response.status_code == 200
  assert response.json()["interval"] == 5
  assert response.json()["expires_in"] == 1800
  assert github_auth.get_device_flow()["interval"] == 5


def test_connect_start_always_requests_full_pr_access(
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
  # Older Contribute builds sent this selector. It is intentionally ignored:
  # callers can no longer create a partial connection.
  r = client.post(
    "/api/github/connect/start", headers=auth, json={"workflow": False},
  )

  assert r.status_code == 200, r.text
  assert seen["scope"] == ["public_repo workflow"]
  assert r.json()["requested_scopes"] == ["public_repo", "workflow"]


@pytest.mark.parametrize(
  ("scopes", "expected"),
  [
    (("public_repo", "workflow"), True),
    (("repo", "workflow"), True),
    (("public_repo",), False),
    (("workflow",), False),
    ((), False),
  ],
)
def test_full_pr_access_is_one_explicit_scope_contract(scopes, expected):
  assert github_routes.has_full_pr_access(scopes) is expected


def test_connect_start_app_with_github_connect(
  client, owner_token, monkeypatch,
):
  """The Contribute app drives connect from its own UI: a github_connect
  app token is accepted on the connect flow, not just the owner JWT."""
  _set_client_id(monkeypatch, "cid-123")
  _, app_token = _app_token(
    client,
    owner_token,
    github_access=True,
    github_connect=True,
  )

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


def test_connect_start_app_with_data_only_grant_forbidden(
  client, owner_token, monkeypatch,
):
  _set_client_id(monkeypatch, "cid-123")
  _, app_token = _app_token(client, owner_token, github_access=True)
  r = client.post("/api/github/connect/start",
                  headers={"Authorization": f"Bearer {app_token}"})
  assert r.status_code == 403
  assert "github_connect" in r.json()["detail"]


# --- connect/poll -----------------------------------------------------


def test_poll_unknown_attempt_is_explicit(client, auth):
  r = _poll(client, auth, "missing-attempt")
  assert r.status_code == 404
  assert "no longer exists" in r.json()["detail"]


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
                            headers={
                              "x-oauth-scopes": "public_repo, workflow, read:org",
                            })
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)

  start = client.post("/api/github/connect/start", headers=auth)
  assert start.status_code == 200
  attempt_id = start.json()["attempt_id"]

  # Poll before GitHub's interval elapses — answered pending WITHOUT hitting
  # the token endpoint (the server paces so an eager frontend can't trip
  # slow_down escalation).
  r = _poll(client, auth, attempt_id)
  assert r.json()["status"] == "pending"
  assert r.json()["attempt_id"] == attempt_id
  assert r.json()["retry_after"] > 0
  assert calls["access_token"] == 0

  # authorization_pending — interval unchanged.
  _patch_device_flow(next_poll_at=0)
  r = _poll(client, auth, attempt_id)
  assert r.json()["status"] == "pending"
  assert calls["access_token"] == 1
  assert github_auth.get_device_flow()["interval"] == 5

  # slow_down — interval bumps to max(payload 7, prev 5 + 5) = 10.
  _patch_device_flow(next_poll_at=0)
  r = _poll(client, auth, attempt_id)
  assert r.json()["status"] == "pending"
  assert calls["access_token"] == 2
  assert github_auth.get_device_flow()["interval"] == 10

  # success — credentials and terminal attempt state are persisted.
  _patch_device_flow(next_poll_at=0)
  r = _poll(client, auth, attempt_id)
  assert r.json()["status"] == "complete"
  assert r.json()["login"] == "octocat"
  assert calls["user"] == 1
  assert github_auth.get_device_flow()["status"] == "complete"
  assert "device_code" not in github_auth.get_device_flow()

  # Both credential files exist at 0600.
  for path in (github_auth.STATE_PATH, github_auth.HOSTS_PATH):
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
  state = json.loads(github_auth.STATE_PATH.read_text())
  assert state["token"] == "gh-secret-xyz"
  assert state["login"] == "octocat"
  assert state["token_source"] == "device"
  assert state["scopes"] == ["public_repo", "workflow", "read:org"]

  # Git identity attributes commits to the connected user.
  def _git_get(key):
    return subprocess.run(
      ["git", "config", "--global", "--get", key],
      capture_output=True, text=True,
    ).stdout.strip()

  assert _git_get("user.name") == "octocat"
  assert _git_get("user.email") == "42+octocat@users.noreply.github.com"


def test_device_token_survives_user_lookup_retry(
  client, auth, monkeypatch,
):
  _set_client_id(monkeypatch, "cid-123")
  calls = {"token": 0, "user": 0}

  def handler(request):
    url = str(request.url)
    if url == _DEVICE_CODE_URL:
      return httpx.Response(200, json={
        "device_code": "DEV", "user_code": "AB-12",
        "verification_uri": "https://github.com/login/device",
        "interval": 5, "expires_in": 900,
      })
    if url == _ACCESS_TOKEN_URL:
      calls["token"] += 1
      return httpx.Response(200, json={"access_token": "gh-recoverable"})
    if url == "https://api.github.com/user":
      calls["user"] += 1
      if calls["user"] == 1:
        return httpx.Response(503, json={"message": "temporarily unavailable"})
      return httpx.Response(
        200,
        json={"login": "octocat", "id": 42},
        headers={"x-oauth-scopes": "public_repo, workflow"},
      )
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  attempt_id = client.post(
    "/api/github/connect/start", headers=auth,
  ).json()["attempt_id"]
  flow = github_auth.get_device_flow()
  flow["next_poll_at"] = 0
  github_auth.set_device_flow(flow)

  failed_lookup = _poll(client, auth, attempt_id)
  assert failed_lookup.status_code == 502
  flow = github_auth.get_device_flow()
  assert flow["pending_token"] == "gh-recoverable"
  assert "device_code" not in flow

  flow["next_poll_at"] = 0
  github_auth.set_device_flow(flow)
  completed = _poll(client, auth, attempt_id)

  assert completed.status_code == 200
  assert completed.json()["status"] == "complete"
  assert calls == {"token": 1, "user": 2}
  assert github_auth.read_state()["token"] == "gh-recoverable"
  assert "pending_token" not in github_auth.get_device_flow()


def test_device_flow_rejects_a_partial_scope_grant(
  client, auth, monkeypatch,
):
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
      return httpx.Response(200, json={"access_token": "gh-partial"})
    if url == "https://api.github.com/user":
      return httpx.Response(
        200,
        json={"login": "octocat", "id": 42},
        headers={"x-oauth-scopes": "public_repo"},
      )
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  attempt_id = client.post(
    "/api/github/connect/start", headers=auth,
  ).json()["attempt_id"]
  _patch_device_flow(next_poll_at=0)

  result = _poll(client, auth, attempt_id)

  assert result.status_code == 200
  assert result.json()["status"] == "failed"
  assert result.json()["reason"] == "insufficient_scopes"
  assert "full PR access" in result.json()["message"]
  assert github_auth.read_state() is None
  assert "pending_token" not in github_auth.get_device_flow()


@pytest.mark.parametrize("reason", ["expired_token", "access_denied"])
def test_poll_failure_preserves_terminal_state(client, auth, monkeypatch, reason):
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
  start = client.post("/api/github/connect/start", headers=auth)
  assert start.status_code == 200
  attempt_id = start.json()["attempt_id"]
  _patch_device_flow(next_poll_at=0)
  r = _poll(client, auth, attempt_id)
  assert r.json()["status"] == "failed"
  assert r.json()["reason"] == reason
  assert github_auth.get_device_flow()["status"] == "failed"
  assert "device_code" not in github_auth.get_device_flow()


def test_device_attempt_is_reloaded_from_durable_state(
  client, auth, monkeypatch,
):
  _set_client_id(monkeypatch, "cid-123")

  def handler(request):
    if str(request.url) == _DEVICE_CODE_URL:
      return httpx.Response(200, json={
        "device_code": "DEV", "user_code": "AB-12",
        "verification_uri": "https://github.com/login/device",
        "interval": 5, "expires_in": 900,
      })
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  start = client.post("/api/github/connect/start", headers=auth)
  attempt_id = start.json()["attempt_id"]
  assert stat.S_IMODE(github_auth.DEVICE_FLOW_PATH.stat().st_mode) == 0o600

  recovered = github_auth.get_device_flow()
  assert recovered["attempt_id"] == attempt_id
  assert recovered["device_code"] == "DEV"
  assert recovered["status"] == "waiting"

  recovered["user_code"] = "UPDATED-BY-OTHER-WORKER"
  github_auth._write_0600(
    github_auth.DEVICE_FLOW_PATH,
    json.dumps(recovered),
  )
  assert (
    github_auth.get_device_flow()["user_code"]
    == "UPDATED-BY-OTHER-WORKER"
  )


def test_new_attempt_supersedes_stale_tab(client, auth, monkeypatch):
  _set_client_id(monkeypatch, "cid-123")
  device_codes = iter(("DEV-OLD", "DEV-NEW"))

  def handler(request):
    if str(request.url) == _DEVICE_CODE_URL:
      device_code = next(device_codes)
      return httpx.Response(200, json={
        "device_code": device_code, "user_code": "AB-12",
        "verification_uri": "https://github.com/login/device",
        "interval": 5, "expires_in": 900,
      })
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  old_id = client.post(
    "/api/github/connect/start", headers=auth,
  ).json()["attempt_id"]
  new_id = client.post(
    "/api/github/connect/start", headers=auth,
  ).json()["attempt_id"]

  stale = _poll(client, auth, old_id)
  assert stale.status_code == 404
  assert github_auth.get_device_flow()["attempt_id"] == new_id
  assert github_auth.get_device_flow()["device_code"] == "DEV-NEW"


def test_cancel_targets_exact_attempt(client, auth, monkeypatch):
  _set_client_id(monkeypatch, "cid-123")

  def handler(request):
    if str(request.url) == _DEVICE_CODE_URL:
      return httpx.Response(200, json={
        "device_code": "DEV", "user_code": "AB-12",
        "verification_uri": "https://github.com/login/device",
        "interval": 5, "expires_in": 900,
      })
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  attempt_id = client.post(
    "/api/github/connect/start", headers=auth,
  ).json()["attempt_id"]

  cancelled = client.post(
    "/api/github/connect/cancel",
    headers=auth,
    json={"attempt_id": attempt_id},
  )

  assert cancelled.status_code == 200
  assert cancelled.json()["status"] == "cancelled"
  assert github_auth.get_device_flow()["status"] == "cancelled"
  assert "device_code" not in github_auth.get_device_flow()


def test_expired_attempt_never_calls_github(client, auth, monkeypatch):
  _set_client_id(monkeypatch, "cid-123")

  def handler(request):
    if str(request.url) == _DEVICE_CODE_URL:
      return httpx.Response(200, json={
        "device_code": "DEV", "user_code": "AB-12",
        "verification_uri": "https://github.com/login/device",
        "interval": 5, "expires_in": 900,
      })
    return _fail(request)

  _install_mock_transport(monkeypatch, handler)
  attempt_id = client.post(
    "/api/github/connect/start", headers=auth,
  ).json()["attempt_id"]
  flow = github_auth.get_device_flow()
  flow["expires_at"] = 0
  github_auth.set_device_flow(flow)

  expired = _poll(client, auth, attempt_id)

  assert expired.status_code == 200
  assert expired.json()["status"] == "expired"
  assert expired.json()["reason"] == "expired_token"
  assert "device_code" not in github_auth.get_device_flow()


def test_legacy_pasted_token_connection_is_retired(client, auth):
  response = client.post(
    "/api/github/connect/token",
    json={"token": "ghp_not_sent_anywhere"},
    headers=auth,
  )

  assert response.status_code == 404


def test_connection_mutation_lock_is_exclusive_and_survives_disconnect():
  first = github_auth.try_acquire_connection_lock()
  assert first is not None
  try:
    assert github_auth.try_acquire_connection_lock() is None
    github_auth.clear_credentials()
    assert github_auth.CONNECTION_LOCK_PATH.exists()
  finally:
    github_auth.release_connection_lock(first)

  second = github_auth.try_acquire_connection_lock()
  assert second is not None
  github_auth.release_connection_lock(second)


def test_credential_state_is_committed_only_after_cli_view(
  monkeypatch,
):
  real_write = github_auth._write_0600

  def fail_canonical(path, content):
    if path == github_auth.STATE_PATH:
      raise OSError("state disk failure")
    real_write(path, content)

  monkeypatch.setattr(github_auth, "_write_0600", fail_canonical)

  with pytest.raises(OSError, match="state disk failure"):
    github_auth.write_credentials(
      token="gh-not-committed",
      login="octocat",
      user_id=42,
      scopes=["public_repo"],
      source="device",
    )

  assert github_auth.HOSTS_PATH.exists()
  assert github_auth.read_state() is None
  assert github_auth.get_token() is None


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
  assert "classic_token_url" not in body
  assert "classic_workflow_token_url" not in body
  assert "gh_version" in body
  assert body["active_attempt"] is None
  assert "token" not in body


def test_status_requires_connection_grant_not_data_grant(
  client, owner_token, monkeypatch,
):
  _set_client_id(monkeypatch, "cid-123")
  _, data_token = _app_token(
    client,
    owner_token,
    github_access=True,
  )
  denied = client.get(
    "/api/github/status",
    headers={"Authorization": f"Bearer {data_token}"},
  )
  assert denied.status_code == 403
  assert "github_connect" in denied.json()["detail"]

  _, connect_token = _app_token(
    client,
    owner_token,
    github_connect=True,
  )
  allowed = client.get(
    "/api/github/status",
    headers={"Authorization": f"Bearer {connect_token}"},
  )
  assert allowed.status_code == 200


def test_status_exposes_resumable_attempt_without_secrets(client, auth):
  github_auth.set_device_flow({
    "attempt_id": "resume-123",
    "status": "waiting",
    "device_code": "device-secret",
    "pending_token": "token-secret",
    "interval": 5,
    "next_poll_at": time.time() + 5,
    "created_at": time.time(),
    "expires_at": time.time() + 300,
    "requested_scopes": ["public_repo", "workflow"],
    "user_code": "ABCD-EFGH",
    "verification_uri": "https://github.com/login/device",
  })

  body = client.get("/api/github/status", headers=auth).json()

  assert body["connected"] is False
  assert body["active_attempt"]["attempt_id"] == "resume-123"
  assert body["active_attempt"]["user_code"] == "ABCD-EFGH"
  assert body["active_attempt"]["verification_uri"].endswith("/device")
  assert body["active_attempt"]["expires_in"] > 0
  serialized = json.dumps(body)
  assert "device-secret" not in serialized
  assert "token-secret" not in serialized


def test_status_device_flow_unavailable_without_client_id(
  client, auth, monkeypatch,
):
  _set_client_id(monkeypatch, None)
  r = client.get("/api/github/status", headers=auth)
  assert r.json()["device_flow_available"] is False


def test_status_connected_never_echoes_token(client, auth):
  secret = _write_token(token="gh-super-secret", login="octocat",
                        scopes=("public_repo", "read:org"), source="device")
  r = client.get("/api/github/status", headers=auth)
  assert r.status_code == 200
  body = r.json()
  assert body["connected"] is True
  assert body["login"] == "octocat"
  assert body["scopes"] == ["public_repo", "read:org"]
  assert body["token_source"] == "device"
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


def test_source_status_projects_local_distribution_manifest_identity(
  client, owner_token, auth, monkeypatch,
):
  from app import models
  from app.database import SessionLocal

  app_id, _ = _app_token(client, owner_token)
  source_dir = Path(get_settings().data_dir) / "apps" / "published-source"
  source_dir.mkdir(parents=True, exist_ok=True)
  distribution_url = (
    "https://raw.githubusercontent.com/example/published/main/mobius.json"
  )
  session = SessionLocal()
  try:
    session.query(models.App).filter(models.App.id == app_id).update({
      "source_dir": str(source_dir),
      "published_manifest_url": distribution_url,
    })
    session.commit()
  finally:
    session.close()

  monkeypatch.setattr(source_status, "build_platform_status", lambda: {
    "key": "platform", "available": True,
  })

  def inspect(app):
    assert app["manifest_url"] is None
    assert app["published_manifest_url"] == distribution_url
    return {"key": f"app:{app['id']}", "name": app["name"]}

  monkeypatch.setattr(source_status, "build_app_status", inspect)

  response = client.get("/api/github/source-status", headers=auth)

  assert response.status_code == 200, response.text
  assert response.json()["apps"][0]["key"] == f"app:{app_id}"


def test_connect_published_app_reuses_reviewed_local_row_idempotently(
  client, owner_token, monkeypatch,
):
  """The after-merge action links identity without replacing app data."""
  from app import app_git, install, models
  from app.app_capabilities import contract_and_digest
  from app.database import SessionLocal

  contribute_id, contribute_token = _app_token(
    client, owner_token, github_access=True,
  )
  auth = {"Authorization": f"Bearer {owner_token}"}
  target = create_local_app(
    client,
    auth,
    name="Connect",
    source_dir=Path(get_settings().data_dir) / "apps" / "connect",
    manifest_extra={
      "id": "connect",
      "permissions": {"connect_manage": True},
    },
  )
  source = Path(target["source_dir"])
  subprocess.run(
    [
      "git", "-C", str(source), "remote", "add", "origin",
      "https://github.com/mobius-os/app-connect.git",
    ],
    check=True,
  )
  base = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  (source / "index.jsx").write_text(
    "export default function App(){return <div>reviewed</div>}\n",
    encoding="utf-8",
  )
  reviewed = app_git.commit_local(source, "reviewed publication")
  assert reviewed
  reviewed_diff = app_git._canonical_diff(source, base, reviewed)
  assert reviewed_diff is not None
  diff_digest = hashlib.sha256(reviewed_diff).hexdigest()
  assert app_git.record_pending_equivalent_change(
    source,
    base_sha=base,
    head_sha=reviewed,
    source_sha=reviewed,
    diff_sha256=diff_digest,
    contribution_id="publish-connect",
  )
  assert app_git.mark_equivalent_change_landed(
    source, diff_digest, upstream_sha=reviewed,
  )

  record = {
    "id": "publish-connect",
    "type": "pr",
    "status": "merged",
    "repo": "mobius-os/app-connect",
    "number": 1,
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-connect",
      "source_repo_path": str(source),
      "source_sha": reviewed,
      "base_sha": base,
      "head_sha": reviewed,
      "diff_sha256": diff_digest,
      "after_merge": {
        "action": "connect_app",
        "app_id": target["id"],
        "manifest_url": (
          "https://raw.githubusercontent.com/mobius-os/"
          "app-connect/main/mobius.json"
        ),
      },
    },
  }
  record_path = (
    Path(get_settings().data_dir) / "apps" / str(contribute_id)
    / "contributions" / "publish-connect.json"
  )
  record_path.parent.mkdir(parents=True, exist_ok=True)
  atomic_write(record_path, json.dumps(record))

  tree = app_git.read_ref_tree(source, reviewed)
  manifest = json.loads(tree["mobius.json"])
  contract, capability_digest = contract_and_digest(manifest)
  candidate = install.InstallCandidate(
    manifest=manifest,
    raw_base=(
      "https://raw.githubusercontent.com/mobius-os/app-connect/"
      f"{reviewed}/"
    ),
    entry_bytes=tree["index.jsx"],
    icon_processed=None,
    icon_warning=None,
    bundled_job=None,
    static_assets={},
    source_files={},
    seeds={},
    capability_contract=contract,
    capability_digest=capability_digest,
    candidate_digest="a" * 64,
    source_review_digest="b" * 64,
  )
  monkeypatch.setattr(
    github_routes, "_merged_upstream_sha", lambda *_args: reviewed,
  )
  monkeypatch.setattr(app_git, "fetch_origin_commit", lambda *_args: reviewed)
  async def fetched(_url):
    return candidate
  monkeypatch.setattr(install, "fetch_install_candidate", fetched)

  installs = 0
  async def connected(db, **kwargs):
    nonlocal installs
    installs += 1
    row = db.query(models.App).filter(models.App.id == target["id"]).one()
    row.manifest_url = (
      "https://raw.githubusercontent.com/mobius-os/app-connect/"
      f"{reviewed}#manifest-id=connect"
    )
    row.version = "0.1.0"
    row.capability_contract = contract
    row.upstream_commit = reviewed
    db.commit()
    db.refresh(row)
    return install.InstallResult(
      app=row,
      mode="update",
      warnings=[],
      manifest=manifest,
      conflict_paths=[],
      divergence="clean_merge",
      reconciliation=app_git.ReconciliationReceipt(),
    )
  monkeypatch.setattr(install, "install_from_manifest", connected)

  app_auth = {"Authorization": f"Bearer {contribute_token}"}
  first = client.post(
    f"/api/github/contributions/{contribute_id}/publish-connect/connect-app",
    headers=app_auth,
  )
  assert first.status_code == 200, first.text
  assert first.json()["connection"]["status"] == "connected"
  assert first.json()["connection"]["app_id"] == target["id"]
  assert installs == 1

  second = client.post(
    f"/api/github/contributions/{contribute_id}/publish-connect/connect-app",
    headers=app_auth,
  )
  assert second.status_code == 200, second.text
  assert second.json()["connection"]["app_id"] == target["id"]
  assert installs == 1

  # Simulate the narrow crash window after a conflicting handoff committed
  # the app identity and pending receipt but before its ledger mirror landed.
  install.stage_pending_conflict_update(
    source,
    app_id=target["id"],
    upstream_commit=reviewed,
    manifest=manifest,
    raw_base=candidate.raw_base,
    capability_digest=capability_digest,
    candidate_digest="c" * 64,
    conflict_paths=["index.jsx"],
  )
  interrupted = json.loads(record_path.read_text(encoding="utf-8"))
  interrupted.pop("publication_connection", None)
  atomic_write(record_path, json.dumps(interrupted))

  recovered = client.post(
    f"/api/github/contributions/{contribute_id}/publish-connect/connect-app",
    headers=app_auth,
  )
  assert recovered.status_code == 200, recovered.text
  assert recovered.json()["connection"]["status"] == "connected_conflict"
  assert recovered.json()["connection"]["conflict_paths"] == ["index.jsx"]
  assert installs == 1

  session = SessionLocal()
  try:
    row = session.query(models.App).filter(models.App.id == target["id"]).one()
    assert row.slug == "connect"
    assert row.manifest_url.endswith(f"{reviewed}#manifest-id=connect")
  finally:
    session.close()


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
  github_auth.set_device_flow({
    "attempt_id": "pending-disconnect",
    "status": "waiting",
    "device_code": "DEV",
  })
  assert github_auth.GH_AUTH_DIR.exists()
  r = client.delete("/api/github/connect", headers=auth)
  assert r.status_code == 200
  assert r.json() == {"ok": True}
  assert not github_auth.GH_AUTH_DIR.exists()
  assert github_auth.get_device_flow() is None


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
  baseline = checked_out_connections()
  checked_out = []

  async def fake_forward(_client, _request):
    checked_out.append(checked_out_connections())
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


def test_reviewed_pr_labels_are_bounded_to_the_visible_two():
  assert github_routes._reviewed_pr_labels({
    "labels": [" bug ", "area: ui", "hidden-third"],
  }) == ["bug", "area: ui"]
  assert github_routes._reviewed_pr_labels({
    "labels": ["bug", "BUG", "area: ui"],
  }) == ["bug"]
  assert github_routes._reviewed_pr_labels({
    "labels": [None, "", "bug", "area: ui", "hidden-third"],
  }) == ["bug", "area: ui"]
  assert github_routes._reviewed_pr_labels({"labels": "bug"}) == []


def test_pr_labels_apply_only_existing_names_and_preserve_missing(
  monkeypatch, tmp_path,
):
  calls = []

  def fake_gh(repo, *args, check=True):
    calls.append(args)
    if "--paginate" in args:
      return _cp("bug\narea: ui\n")
    return _cp("[]")

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  patch = github_routes._apply_reviewed_pr_labels(
    tmp_path,
    "mobius-os/mobius",
    123,
    ["Bug", "area: backend"],
  )

  assert patch["last_submit_labels_requested"] == ["Bug", "area: backend"]
  assert patch["last_submit_labels_applied"] == ["bug"]
  assert patch["last_submit_labels_missing"] == ["area: backend"]
  assert "Some reviewed labels" in patch["last_submit_labels_note"]
  apply_call = calls[-1]
  assert apply_call[:3] == ("api", "--method", "POST")
  assert "labels[]=bug" in apply_call
  assert "labels[]=area: backend" not in apply_call


def test_pr_label_permission_failure_does_not_fail_an_open_pr(
  monkeypatch, tmp_path,
):
  def fake_gh(repo, *args, check=True):
    if "--paginate" in args:
      return _cp("bug\n")
    return _cp("forbidden", returncode=1)

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  patch = github_routes._apply_reviewed_pr_labels(
    tmp_path,
    "someone/example",
    7,
    ["bug"],
  )

  assert patch["last_submit_labels_applied"] == []
  assert "did not confirm" in patch["last_submit_labels_note"]


@pytest.mark.parametrize(
  "label_failure",
  [
    subprocess.TimeoutExpired(["gh", "api"], timeout=30),
    OSError("gh could not start"),
  ],
  ids=["apply-timeout", "apply-launch-error"],
)
def test_pr_label_apply_transport_failure_is_nonfatal(
  monkeypatch, tmp_path, label_failure,
):
  def fake_gh(repo, *args, check=True):
    if "--paginate" in args:
      return _cp("bug\n")
    raise label_failure

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  patch = github_routes._apply_reviewed_pr_labels(
    tmp_path,
    "someone/example",
    7,
    ["bug"],
  )

  assert patch["last_submit_labels_requested"] == ["bug"]
  assert patch["last_submit_labels_applied"] == []
  assert "pull request is open" in patch["last_submit_labels_note"]


def _write_contribution(app_id, record_id, record, diff_text=""):
  base = Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
  base.mkdir(parents=True, exist_ok=True)
  atomic_write(base / f"{record_id}.json", json.dumps(record))
  if diff_text:
    atomic_write(base / f"{record_id}.diff", diff_text)


def _prepared_real_review(app_id, record_id):
  """Build one exact local review checkout under the route's allowlist."""
  data_dir = Path(get_settings().data_dir)
  repo = data_dir / "contrib" / record_id / "worktree"
  repo.mkdir(parents=True)
  subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                 capture_output=True)
  subprocess.run(["git", "config", "user.name", "octocat"], cwd=repo,
                 check=True)
  subprocess.run([
    "git", "config", "user.email", "42+octocat@users.noreply.github.com",
  ], cwd=repo, check=True)
  (repo / "index.jsx").write_text("export default 1\n")
  subprocess.run(["git", "add", "index.jsx"], cwd=repo, check=True)
  subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True,
                 capture_output=True)
  base = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo,
                                 text=True).strip()
  subprocess.run(["git", "checkout", "-b", "fix/demo-review"], cwd=repo,
                 check=True, capture_output=True)
  (repo / "index.jsx").write_text("export default 2\n")
  subprocess.run(["git", "add", "index.jsx"], cwd=repo, check=True)
  subprocess.run([
    "git", "commit", "-m", "reviewed fix", "-m",
    "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>",
  ], cwd=repo, check=True, capture_output=True)
  head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo,
                                 text=True).strip()
  diff_text = subprocess.check_output([
    "git", "-c", "core.quotePath=false", "diff", "--no-ext-diff",
    "--no-color", "--binary", "--full-index", "--src-prefix=a/",
    "--dst-prefix=b/", f"{base}..{head}",
  ], cwd=repo, text=True)
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Reviewed fix",
    "branch": "fix/demo-review",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "branch": "fix/demo-review",
      "repo_path": str(repo),
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
    },
  }
  _write_contribution(app_id, record_id, record, diff_text)
  return repo, record, diff_text


def _prepared_platform_review(app_id, record_id):
  """Build the supported standalone platform variant of a real review."""
  repo, record, diff_text = _prepared_real_review(app_id, record_id)
  record = {
    **record,
    "repo": "mobius-os/mobius",
    "plan": {**record["plan"], "repo": "mobius-os/mobius"},
  }
  _write_contribution(app_id, record_id, record, diff_text)
  return repo, record, diff_text


def test_run_pre_pr_checks_persists_exact_run_and_blocks_duplicates(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat", user_id=42, scopes=("public_repo", "workflow"))
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, _record, _diff = _prepared_platform_review(
    app_id, "pre-pr-check-success",
  )

  requested_at = "2026-08-02T14:00:00Z"

  def fake_dispatch(record, diff_path):
    assert record["pre_pr_checks"]["state"] == "dispatching"
    assert diff_path.name == "pre-pr-check-success.diff"
    assert "requested_at" not in record["pre_pr_checks"]
    return ({
      "state": "queued",
      "run_id": 734,
      "url": "https://github.com/octocat/mobius/actions/runs/734",
      "fork_repo": "octocat/mobius",
      "branch": "fix/demo-review",
      "head_sha": record["plan"]["head_sha"],
      "workflow": "test.yml",
      "requested_at": requested_at,
      "observed_at": requested_at,
    }, {"last_submit_push_sha": record["plan"]["head_sha"]})

  monkeypatch.setattr(
    github_pre_pr_checks, "dispatch_pre_pr_checks", fake_dispatch,
  )
  headers = {"Authorization": f"Bearer {app_token}"}
  response = client.post(
    f"/api/github/contributions/{app_id}/pre-pr-check-success/pre-pr-checks",
    headers=headers,
  )
  assert response.status_code == 200, response.text
  record = response.json()["record"]
  assert record["status"] == "prepared"
  assert record["pre_pr_checks"]["state"] == "queued"
  assert record["pre_pr_checks"]["run_id"] == 734
  assert record["pre_pr_checks"]["request_id"]
  assert record["last_submit_push_sha"] == record["plan"]["head_sha"]

  duplicate = client.post(
    f"/api/github/contributions/{app_id}/pre-pr-check-success/pre-pr-checks",
    headers=headers,
  )
  assert duplicate.status_code == 409
  assert "already" in duplicate.json()["detail"].lower()


def test_run_pre_pr_checks_keeps_recoverable_failure_on_the_record(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat", user_id=42, scopes=("public_repo", "workflow"))
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, _diff = _prepared_platform_review(
    app_id, "pre-pr-check-error",
  )

  requested_at = "2026-08-02T14:00:00Z"

  def fake_dispatch(_record, _diff_path):
    raise ContributionSubmitError(
      "GitHub could not start Tests.",
      status_code=409,
      code="pre_pr_checks_dispatch_failed",
      record_patch={
        "last_submit_push_sha": record["plan"]["head_sha"],
        "pre_pr_checks": {
          "state": "error",
          "message": "GitHub could not start Tests.",
          "requested_at": requested_at,
          "observed_at": requested_at,
        },
      },
    )

  monkeypatch.setattr(
    github_pre_pr_checks, "dispatch_pre_pr_checks", fake_dispatch,
  )
  response = client.post(
    f"/api/github/contributions/{app_id}/pre-pr-check-error/pre-pr-checks",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 409, response.text
  stored = response.json()["detail"]["record"]
  assert stored["status"] == "prepared"
  assert stored["pre_pr_checks"]["state"] == "error"
  assert stored["pre_pr_checks"]["request_id"]
  assert stored["last_submit_push_sha"] == record["plan"]["head_sha"]


def test_run_pre_pr_checks_settles_an_invalid_checkout_claim(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42, scopes=("public_repo", "workflow"))
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, diff_text = _prepared_platform_review(
    app_id, "pre-pr-check-invalid-path",
  )
  record["plan"]["repo_path"] = ""
  _write_contribution(app_id, "pre-pr-check-invalid-path", record, diff_text)

  response = client.post(
    f"/api/github/contributions/{app_id}/pre-pr-check-invalid-path/pre-pr-checks",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 409, response.text
  stored = response.json()["detail"]["record"]
  assert stored["status"] == "prepared"
  assert stored["pre_pr_checks"]["state"] == "error"
  assert "durable repo_path" in stored["pre_pr_checks"]["message"]


def test_refresh_pre_pr_checks_persists_the_exact_terminal_run(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat", user_id=42, scopes=("public_repo", "workflow"))
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, diff_text = _prepared_platform_review(
    app_id, "pre-pr-check-refresh",
  )
  record["pre_pr_checks"] = {
    "state": "in_progress",
    "request_id": "request-1",
    "run_id": 735,
    "url": "https://github.com/octocat/mobius/actions/runs/735",
    "fork_repo": "octocat/mobius",
    "branch": record["plan"]["branch"],
    "head_sha": record["plan"]["head_sha"],
  }
  _write_contribution(app_id, "pre-pr-check-refresh", record, diff_text)

  def fake_refresh(_record):
    return {
      **_record["pre_pr_checks"],
      "state": "completed",
      "conclusion": "success",
      "completed_at": "2026-08-02T15:00:00Z",
    }

  monkeypatch.setattr(
    github_pre_pr_checks, "refresh_pre_pr_check", fake_refresh,
  )
  headers = {"Authorization": f"Bearer {app_token}"}
  url = f"/api/github/contributions/{app_id}/pre-pr-checks/refresh"
  response = client.post(url, headers=headers)
  assert response.status_code == 200, response.text
  assert response.json()["refreshed"][0]["pre_pr_checks"]["conclusion"] == (
    "success"
  )

  repeat = client.post(url, headers=headers)
  assert repeat.status_code == 200
  assert repeat.json() == {"refreshed": []}


def test_send_waits_while_pre_pr_checks_are_active(client, owner_token):
  _write_token(login="octocat", user_id=42, scopes=("public_repo", "workflow"))
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, diff_text = _prepared_platform_review(
    app_id, "pre-pr-check-send-lock",
  )
  record["pre_pr_checks"] = {
    "state": "queued",
    "request_id": "request-2",
    "run_id": 736,
  }
  _write_contribution(app_id, "pre-pr-check-send-lock", record, diff_text)

  response = client.post(
    f"/api/github/contributions/{app_id}/pre-pr-check-send-lock/submit",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 409
  assert "starting or running" in response.json()["detail"].lower()

  stored = json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id) /
     "contributions" / "pre-pr-check-send-lock.json").read_text()
  )
  assert stored["status"] == "prepared"
  assert stored["pre_pr_checks"]["state"] == "queued"


def test_review_status_catches_local_drift_before_send(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  repo, _record, _diff = _prepared_real_review(app_id, "review-health")
  headers = {"Authorization": f"Bearer {app_token}"}

  ready = client.get(
    f"/api/github/contributions/{app_id}/review-status", headers=headers,
  )
  assert ready.status_code == 200, ready.text
  assert ready.json()["ready"] == 1
  assert ready.json()["records"] == [{
    "id": "review-health",
    "state": "ready",
    "code": "ready",
    "message": "Still matches the exact source you reviewed.",
  }]

  (repo / "index.jsx").write_text("export default 3\n")
  stale = client.get(
    f"/api/github/contributions/{app_id}/review-status", headers=headers,
  )
  assert stale.status_code == 200, stale.text
  assert stale.json()["needs_refresh"] == 1
  assert stale.json()["records"][0]["code"] == "working_changes"
  # Read-only means the review check neither commits nor discards the edit.
  assert (repo / "index.jsx").read_text() == "export default 3\n"


def test_review_status_releases_db_before_git_inspection(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  _prepared_real_review(app_id, "review-pool")
  baseline = checked_out_connections()
  observed = []
  original = github_routes._inspect_prepared_review

  def inspect(record, diff_path, github_state):
    observed.append(checked_out_connections())
    return original(record, diff_path, github_state)

  monkeypatch.setattr(github_routes, "_inspect_prepared_review", inspect)
  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  assert observed == [baseline]


def test_review_status_skips_oversized_records_without_loading_them(
  client, owner_token,
):
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  record_path = (
    Path(get_settings().data_dir) / "apps" / str(app_id) /
    "contributions" / "oversized.json"
  )
  record_path.parent.mkdir(parents=True, exist_ok=True)
  record_path.write_text(json.dumps({
    "id": "oversized",
    "type": "pr",
    "status": "prepared",
    "padding": "x" * (64 * 1024),
  }))

  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  assert response.json()["records"] == []


def test_review_status_catches_noncanonical_stored_diff(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  _repo, record, _diff = _prepared_real_review(app_id, "review-diff-shape")
  abbreviated = "diff --git a/index.jsx b/index.jsx\nindex 123..456 100644\n"
  record["plan"]["diff_sha256"] = hashlib.sha256(abbreviated.encode()).hexdigest()
  _write_contribution(app_id, "review-diff-shape", record, abbreviated)

  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 200, response.text
  assert response.json()["records"][0]["code"] == "diff_mismatch"


def test_review_status_rejects_a_fingerprinted_nonancestor_base(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  repo, record, _diff = _prepared_real_review(app_id, "review-ancestry")
  original_base = record["plan"]["base_sha"]
  head = record["plan"]["head_sha"]
  tree = subprocess.check_output(
    ["git", "rev-parse", f"{original_base}^{{tree}}"], cwd=repo, text=True,
  ).strip()
  sibling = subprocess.check_output([
    "git", "commit-tree", tree, "-p", original_base, "-m", "sibling base",
  ], cwd=repo, text=True).strip()
  reviewed = subprocess.check_output([
    "git", "-c", "core.quotePath=false", "diff", "--no-ext-diff",
    "--no-color", "--binary", "--full-index", "--src-prefix=a/",
    "--dst-prefix=b/", f"{sibling}..{head}",
  ], cwd=repo, text=True)
  record["plan"]["base_sha"] = sibling
  record["plan"]["diff_sha256"] = hashlib.sha256(reviewed.encode()).hexdigest()
  _write_contribution(app_id, "review-ancestry", record, reviewed)

  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  assert response.json()["records"][0]["code"] == "invalid_ancestry"


def test_review_status_requires_refresh_after_stack_parent_merges(
  client, owner_token,
):
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  stack_id = "merged-review"
  parent_branch = f"stack/{stack_id}/01-parent"
  child_branch = f"stack/{stack_id}/02-child"
  parent_head = "a" * 40
  common = {
    "type": "pr", "repo": "mobius-os/app-demo",
  }
  parent = {
    **common,
    "id": "merged-parent", "status": "merged", "branch": parent_branch,
    "plan": {
      "action": "pr", "repo": "mobius-os/app-demo",
      "branch": parent_branch, "base_sha": "b" * 40,
      "head_sha": parent_head,
      "stack": {
        "id": stack_id, "position": 1, "total": 2,
        "parent_record_id": "", "base_branch": "main",
      },
    },
  }
  child = {
    **common,
    "id": "private-child", "status": "prepared", "branch": child_branch,
    "plan": {
      "action": "pr", "repo": "mobius-os/app-demo",
      "branch": child_branch,
      "repo_path": str(Path(get_settings().data_dir) / "contrib" / "unused"),
      "base_sha": parent_head, "head_sha": "c" * 40,
      "diff_sha256": "d" * 64,
      "stack": {
        "id": stack_id, "position": 2, "total": 2,
        "parent_record_id": "merged-parent", "base_branch": parent_branch,
      },
    },
  }
  _write_contribution(app_id, parent["id"], parent)
  _write_contribution(app_id, child["id"], child)

  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 200, response.text
  assert response.json()["records"][0]["code"] == "parent_merged"


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


def test_push_topic_branch_does_not_retry_deterministic_rejections(
  tmp_path, monkeypatch,
):
  from app.routes.github import _push_topic_branch

  calls = []
  sleeps = []
  monkeypatch.setattr(
    "app.github_contribution_git._git",
    lambda _repo, *args, **kwargs: calls.append(args) or _cp(
      stderr="remote: error: GH006: Protected branch update failed",
      returncode=1,
    ),
  )
  monkeypatch.setattr("app.github_contributions.time.sleep", sleeps.append)

  error = _push_topic_branch(tmp_path, "fix/demo")

  assert "Protected branch" in error
  assert len(calls) == 1
  assert sleeps == []


def test_push_topic_branch_briefly_retries_transient_transport_errors(
  tmp_path, monkeypatch,
):
  from app.routes.github import _push_topic_branch

  outcomes = iter((
    _cp(stderr="fatal: unable to access: HTTP 503", returncode=1),
    _cp(stderr="fatal: connection reset by peer", returncode=1),
    _cp(),
  ))
  sleeps = []
  monkeypatch.setattr(
    "app.github_contribution_git._git",
    lambda _repo, *args, **kwargs: next(outcomes),
  )
  monkeypatch.setattr("app.github_contributions.time.sleep", sleeps.append)

  assert _push_topic_branch(tmp_path, "fix/demo") is None
  assert sleeps == [0.5, 1.0]


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
    "app.github_contribution_git._upstream_default_branch",
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

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
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
  assert sum(call[:1] == ("fetch",) for call in git_calls) == 1


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
    "app.github_contribution_git._upstream_default_branch",
    lambda _repo, _slug: "main",
  )

  def fake_git(repo_path, *args, check=True):
    if args[:2] == ("rev-parse", "--verify"):
      return _cp(fork_sha + "\n")
    if args[:2] == ("merge-base", "--is-ancestor"):
      return _cp(returncode=1)
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
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
    "app.github_contribution_git._upstream_default_branch",
    lambda _repo, _slug: "main",
  )

  def fake_git(repo_path, *args, check=True):
    if args[:2] == ("rev-parse", "--verify"):
      return _cp(next(tips) + "\n")
    if args[:2] == ("merge-base", "--is-ancestor"):
      assert args[2:] == (upstream_sha, ahead_sha)
      return _cp(returncode=0)
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
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


def test_sync_owner_fork_verifies_fast_forward(
  tmp_path, monkeypatch,
):
  from app.routes.github import _sync_owner_fork

  repo = tmp_path / "repo"
  repo.mkdir()
  gh_calls = []

  def fake_gh(repo_path, *args, check=True):
    gh_calls.append(args)
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  monkeypatch.setattr(
    "app.github_contributions._inspect_owner_fork_default_branch",
    lambda *_args, **_kwargs: {
      "last_submit_fork_branch": "main",
      "last_submit_fork_sha": "d" * 40,
      "last_submit_fork_sync": "current",
    },
  )

  patch = _sync_owner_fork(
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


@pytest.mark.parametrize(
  "operation",
  [
    lambda tmp_path: github_routes._submit_prepared_pr(
      {}, tmp_path / "reviewed.diff",
    ),
    lambda _tmp_path: github_routes._preflight_prepared_stack([]),
    lambda _tmp_path: github_routes._land_reviewed_stack([]),
  ],
)
def test_reviewed_writes_reject_partial_connections_before_git(
  tmp_path, monkeypatch, operation,
):
  _write_token(scopes=("public_repo",))
  monkeypatch.setattr(
    "app.github_contributions.shutil.which", lambda name: f"/bin/{name}",
  )

  with pytest.raises(ContributionSubmitError) as failure:
    operation(tmp_path)

  assert failure.value.status_code == 409
  assert "full PR access" in failure.value.message


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

  for status in ("merged", "closed", "superseded", "commented", "abandoned"):
    candidate = data_dir / "contrib" / f"terminal-cleanup-{status}" / "repo"
    (candidate / ".git").mkdir(parents=True)
    (candidate / "index.jsx").write_text("hello")
    record = {
      "status": status,
      "plan": {"repo_path": str(candidate)},
    }
    assert _cleanup_terminal_staging_checkout(record) is True
    assert not candidate.exists()

  live_repo = data_dir / "apps" / "terminal-cleanup-live"
  (live_repo / ".git").mkdir(parents=True)
  record["plan"]["repo_path"] = str(live_repo)
  assert _cleanup_terminal_staging_checkout(record) is False
  assert live_repo.exists()


def test_contribution_lifecycle_persists_equivalence_in_live_linked_repo():
  """Send records the local witness; merged cleanup promotes it before delete."""
  from app import app_git
  from app.routes.github import (
    _cleanup_terminal_staging_checkout,
    _record_pending_equivalence,
    _settle_equivalence,
  )

  data_dir = Path(get_settings().data_dir)
  live = data_dir / "apps" / "equivalence-live"
  review = data_dir / "contrib" / "equivalence-review" / "worktree"
  live.mkdir(parents=True)
  subprocess.run(["git", "init", "-qb", "main", str(live)], check=True)
  subprocess.run(["git", "-C", str(live), "config", "user.name", "Test"], check=True)
  subprocess.run(
    ["git", "-C", str(live), "config", "user.email", "test@example.invalid"],
    check=True,
  )
  (live / "index.jsx").write_text("base\n")
  subprocess.run(["git", "-C", str(live), "add", "index.jsx"], check=True)
  subprocess.run(["git", "-C", str(live), "commit", "-qm", "base"], check=True)
  base = subprocess.check_output(
    ["git", "-C", str(live), "rev-parse", "HEAD"], text=True,
  ).strip()
  (live / "index.jsx").write_text("reviewed\n")
  subprocess.run(["git", "-C", str(live), "commit", "-qam", "reviewed"], check=True)
  head = subprocess.check_output(
    ["git", "-C", str(live), "rev-parse", "HEAD"], text=True,
  ).strip()
  review.parent.mkdir(parents=True)
  subprocess.run(
    ["git", "-C", str(live), "worktree", "add", "-qb", "fix/review", str(review), head],
    check=True,
  )
  diff = app_git._canonical_diff(review, base, head)
  assert diff is not None
  digest = hashlib.sha256(diff).hexdigest()
  record = {
    "id": "equivalence-review",
    "status": "open",
    "plan": {
      "repo_path": str(review),
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": digest,
    },
  }

  pending = _record_pending_equivalence(record)
  assert pending and app_git.ref_exists(live, pending)
  # Simulate GitHub's squash commit: a new identity with the reviewed tree.
  tree = subprocess.check_output(
    ["git", "-C", str(live), "rev-parse", f"{head}^{{tree}}"], text=True,
  ).strip()
  upstream = subprocess.check_output(
    [
      "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
      "-C", str(live), "commit-tree", tree, "-p", base, "-m", "squash",
    ],
    text=True,
  ).strip()
  record["status"] = "merged"
  landed = _settle_equivalence(record, upstream)
  assert landed and app_git.ref_exists(live, landed)
  assert not app_git.ref_exists(live, pending)

  assert _cleanup_terminal_staging_checkout(record) is True
  assert not review.exists()
  assert app_git.ref_exists(live, landed)


def test_merged_legacy_record_reconstructs_witness_after_worktree_cleanup():
  """A pre-witness linked review remains attributable after its worktree left."""
  from app import app_git
  from app.routes.github import _settle_equivalence

  data_dir = Path(get_settings().data_dir)
  live = data_dir / "apps" / "equivalence-legacy-live"
  review = data_dir / "contrib" / "equivalence-legacy-review" / "worktree"
  live.mkdir(parents=True)
  subprocess.run(["git", "init", "-qb", "main", str(live)], check=True)
  subprocess.run(["git", "-C", str(live), "config", "user.name", "Test"], check=True)
  subprocess.run(
    ["git", "-C", str(live), "config", "user.email", "test@example.invalid"],
    check=True,
  )
  (live / "index.jsx").write_text("base\n")
  subprocess.run(["git", "-C", str(live), "add", "index.jsx"], check=True)
  subprocess.run(["git", "-C", str(live), "commit", "-qm", "base"], check=True)
  base = subprocess.check_output(
    ["git", "-C", str(live), "rev-parse", "HEAD"], text=True,
  ).strip()
  (live / "index.jsx").write_text("reviewed\n")
  subprocess.run(["git", "-C", str(live), "commit", "-qam", "reviewed"], check=True)
  source = subprocess.check_output(
    ["git", "-C", str(live), "rev-parse", "HEAD"], text=True,
  ).strip()
  review.parent.mkdir(parents=True)
  subprocess.run(
    ["git", "-C", str(live), "worktree", "add", "-qb", "fix/legacy",
     str(review), source],
    check=True,
  )
  diff = app_git._canonical_diff(review, base, source)
  assert diff is not None
  digest = hashlib.sha256(diff).hexdigest()
  tree = subprocess.check_output(
    ["git", "-C", str(live), "rev-parse", f"{source}^{{tree}}"], text=True,
  ).strip()
  upstream = subprocess.check_output(
    [
      "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
      "-C", str(live), "commit-tree", tree, "-p", base, "-m", "squash",
    ],
    text=True,
  ).strip()
  subprocess.run(
    ["git", "-C", str(live), "worktree", "remove", "--force", str(review)],
    check=True,
  )
  assert not review.exists()

  record = {
    "id": "equivalence-legacy-review",
    "status": "merged",
    "plan": {
      "repo_path": str(review),
      "source_repo_path": str(live),
      "source_sha": source,
      "base_sha": base,
      "head_sha": source,
      "diff_sha256": digest,
    },
  }
  landed = _settle_equivalence(record, upstream)

  assert landed and app_git.ref_exists(live, landed)
  witness = app_git._read_equivalent_change(live, landed)
  assert witness is not None
  assert witness.source_sha == source
  assert witness.upstream_sha == upstream


def test_standalone_app_review_persists_equivalence_in_installed_repo():
  """A no-origin app's disposable review clone cannot own the only witness."""
  from app import app_git
  from app.routes.github import (
    _cleanup_terminal_staging_checkout,
    _record_pending_equivalence,
    _settle_equivalence,
  )

  data_dir = Path(get_settings().data_dir)
  live = data_dir / "apps" / "equivalence-standalone-live"
  review = data_dir / "contrib" / "equivalence-standalone" / "worktree"
  for repo in (live, review):
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-qb", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(
      ["git", "-C", str(repo), "config", "user.email", "test@example.invalid"],
      check=True,
    )

  (review / "index.jsx").write_text("base\n")
  subprocess.run(["git", "-C", str(review), "add", "index.jsx"], check=True)
  subprocess.run(["git", "-C", str(review), "commit", "-qm", "review base"], check=True)
  base = subprocess.check_output(
    ["git", "-C", str(review), "rev-parse", "HEAD"], text=True,
  ).strip()
  (review / "index.jsx").write_text("reviewed\n")
  subprocess.run(["git", "-C", str(review), "commit", "-qam", "review head"], check=True)
  head = subprocess.check_output(
    ["git", "-C", str(review), "rev-parse", "HEAD"], text=True,
  ).strip()

  # The installed synthetic app has the same accepted content but unrelated
  # commit identities. It does not contain either reviewed commit beforehand.
  (live / "index.jsx").write_text("reviewed\n")
  subprocess.run(["git", "-C", str(live), "add", "index.jsx"], check=True)
  subprocess.run(
    ["git", "-C", str(live), "commit", "-qm", "installed synthetic source"],
    check=True,
  )
  source_sha = subprocess.check_output(
    ["git", "-C", str(live), "rev-parse", "HEAD"], text=True,
  ).strip()
  live_tree = subprocess.check_output(
    ["git", "-C", str(live), "rev-parse", "HEAD^{tree}"], text=True,
  ).strip()
  replayed_source = subprocess.check_output(
    [
      "git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
      "-C", str(live), "commit-tree", live_tree, "-m", "store replay",
    ],
    text=True,
  ).strip()
  subprocess.run(
    [
      "git", "-C", str(live), "update-ref", "refs/heads/main",
      replayed_source, source_sha,
    ],
    check=True,
  )
  subprocess.run(["git", "-C", str(live), "reset", "--hard", "-q"], check=True)
  assert app_git.ref_is_ancestor(live, source_sha, replayed_source) is False
  assert subprocess.run(
    ["git", "-C", str(live), "cat-file", "-e", f"{head}^{{commit}}"],
    check=False,
  ).returncode != 0

  diff = app_git._canonical_diff(review, base, head)
  assert diff is not None
  digest = hashlib.sha256(diff).hexdigest()
  record = {
    "id": "equivalence-standalone",
    "status": "open",
    "plan": {
      "repo_path": str(review),
      "source_repo_path": str(live),
      "source_sha": source_sha,
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": digest,
    },
  }

  pending = _record_pending_equivalence(record)
  assert pending and app_git.ref_exists(live, pending)
  assert not app_git.ref_exists(review, pending)
  # The exact reviewed commits were imported without moving the installed app.
  assert app_git.head_sha(live, "HEAD") == replayed_source
  recorded = app_git._read_equivalent_change(live, pending)
  assert recorded is not None and recorded.source_sha == replayed_source
  assert subprocess.run(
    ["git", "-C", str(live), "cat-file", "-e", f"{head}^{{commit}}"],
    check=False,
  ).returncode == 0

  record["status"] = "merged"
  landed = _settle_equivalence(record)
  assert landed and app_git.ref_exists(live, landed)
  assert _cleanup_terminal_staging_checkout(record) is True
  assert not review.exists()
  assert app_git.ref_exists(live, landed)


def test_cleanup_terminal_staging_checkout_unregisters_linked_worktree():
  from app.routes.github import _cleanup_terminal_staging_checkout

  data_dir = Path(get_settings().data_dir)
  owner = data_dir / "contrib" / "terminal-cleanup-owner"
  checkout = data_dir / "contrib" / "terminal-cleanup-linked" / "worktree"
  owner.mkdir(parents=True)
  subprocess.run(["git", "init", "-q", str(owner)], check=True)
  subprocess.run(["git", "-C", str(owner), "config", "user.name", "Test"], check=True)
  subprocess.run(
    ["git", "-C", str(owner), "config", "user.email", "test@example.invalid"],
    check=True,
  )
  (owner / "tracked.txt").write_text("base\n")
  subprocess.run(["git", "-C", str(owner), "add", "tracked.txt"], check=True)
  subprocess.run(["git", "-C", str(owner), "commit", "-qm", "base"], check=True)
  checkout.parent.mkdir(parents=True)
  subprocess.run(
    ["git", "-C", str(owner), "worktree", "add", "-qb", "fix/cleanup-test", str(checkout)],
    check=True,
  )

  record = {
    "status": "merged",
    "plan": {"repo_path": str(checkout)},
  }
  assert _cleanup_terminal_staging_checkout(record) is True
  assert not checkout.exists()
  listed = subprocess.run(
    ["git", "-C", str(owner), "worktree", "list", "--porcelain"],
    check=True,
    capture_output=True,
    text=True,
  ).stdout
  assert str(checkout) not in listed


def test_cleanup_terminal_staging_checkout_removes_stale_missing_admin():
  from app.routes.github import _cleanup_terminal_staging_checkout

  data_dir = Path(get_settings().data_dir)
  checkout = data_dir / "contrib" / "terminal-cleanup-missing-admin" / "worktree"
  missing_admin = data_dir / "contrib" / "missing-owner" / ".git" / "worktrees" / "worktree"
  checkout.mkdir(parents=True)
  (checkout / ".git").write_text(f"gitdir: {missing_admin}\n")
  (checkout / "review.txt").write_text("stale\n")

  record = {
    "status": "closed",
    "plan": {"repo_path": str(checkout)},
  }
  assert _cleanup_terminal_staging_checkout(record) is True
  assert not checkout.exists()


def test_cleanup_terminal_staging_checkout_preserves_recycled_worktree_slot():
  from app.routes.github import _cleanup_terminal_staging_checkout

  data_dir = Path(get_settings().data_dir)
  owner = data_dir / "contrib" / "terminal-cleanup-recycled-owner"
  stale = data_dir / "contrib" / "terminal-cleanup-recycled-old" / "worktree"
  current = data_dir / "contrib" / "terminal-cleanup-recycled-new" / "worktree"
  owner.mkdir(parents=True)
  subprocess.run(["git", "init", "-qb", "main", str(owner)], check=True)
  subprocess.run(["git", "-C", str(owner), "config", "user.name", "Test"], check=True)
  subprocess.run(
    ["git", "-C", str(owner), "config", "user.email", "test@example.invalid"],
    check=True,
  )
  (owner / "tracked.txt").write_text("base\n")
  subprocess.run(["git", "-C", str(owner), "add", "tracked.txt"], check=True)
  subprocess.run(["git", "-C", str(owner), "commit", "-qm", "base"], check=True)

  stale.parent.mkdir(parents=True)
  subprocess.run(
    ["git", "-C", str(owner), "worktree", "add", "-qb", "fix/stale", str(stale)],
    check=True,
  )
  admin_dir = Path((stale / ".git").read_text().split(":", 1)[1].strip())
  shutil.rmtree(admin_dir)

  current.parent.mkdir(parents=True)
  subprocess.run(
    ["git", "-C", str(owner), "worktree", "add", "-qb", "fix/current", str(current)],
    check=True,
  )
  assert Path((current / ".git").read_text().split(":", 1)[1].strip()) == admin_dir

  record = {
    "status": "merged",
    "plan": {"repo_path": str(stale)},
  }
  assert _cleanup_terminal_staging_checkout(record) is True
  assert not stale.exists()
  assert current.exists()
  assert (admin_dir / "gitdir").read_text().strip() == str(current / ".git")
  listed = subprocess.run(
    ["git", "-C", str(owner), "worktree", "list", "--porcelain"],
    check=True,
    capture_output=True,
    text=True,
  ).stdout
  assert str(current) in listed


def test_cleanup_terminal_staging_checkout_preserves_reciprocal_outside_owner():
  from app.routes.github import _cleanup_terminal_staging_checkout

  data_dir = Path(get_settings().data_dir)
  checkout = data_dir / "contrib" / "terminal-cleanup-outside-owner" / "worktree"
  admin_dir = data_dir / "shared" / "outside-owner-admin"
  outside_owner = data_dir / "shared" / "outside-owner.git"
  checkout.mkdir(parents=True)
  admin_dir.mkdir(parents=True)
  outside_owner.mkdir(parents=True)
  (checkout / ".git").write_text(f"gitdir: {admin_dir}\n")
  (admin_dir / "gitdir").write_text(f"{checkout / '.git'}\n")
  (admin_dir / "commondir").write_text(f"{outside_owner}\n")
  (outside_owner / "sentinel").write_text("keep\n")

  record = {
    "status": "closed",
    "plan": {"repo_path": str(checkout)},
  }
  assert _cleanup_terminal_staging_checkout(record) is False
  assert checkout.exists()
  assert (outside_owner / "sentinel").read_text() == "keep\n"


def test_cleanup_terminal_staging_checkout_rejects_repo_symlink_alias():
  from app.routes.github import _cleanup_terminal_staging_checkout

  data_dir = Path(get_settings().data_dir)
  target = data_dir / "contrib" / "terminal-cleanup-alias-target" / "repo"
  alias = data_dir / "contrib" / "terminal-cleanup-alias"
  (target / ".git").mkdir(parents=True)
  (target / "sentinel").write_text("keep\n")
  alias.symlink_to(target)

  record = {
    "status": "closed",
    "plan": {"repo_path": str(alias)},
  }
  assert _cleanup_terminal_staging_checkout(record) is False
  assert alias.is_symlink()
  assert (target / "sentinel").read_text() == "keep\n"


def test_cleanup_terminal_staging_checkout_is_idempotent_after_removal():
  from app.routes.github import _cleanup_terminal_staging_checkout

  data_dir = Path(get_settings().data_dir)
  checkout = data_dir / "contrib" / "terminal-cleanup-idempotent" / "repo"
  (checkout / ".git").mkdir(parents=True)
  record = {
    "status": "abandoned",
    "plan": {"repo_path": str(checkout)},
  }

  assert _cleanup_terminal_staging_checkout(record) is True
  assert _cleanup_terminal_staging_checkout(record) is True


def test_cleanup_terminal_staging_checkout_retries_separate_git_dir_partial_failure(
  monkeypatch,
):
  from app.routes.github import _cleanup_terminal_staging_checkout

  data_dir = Path(get_settings().data_dir)
  root = data_dir / "contrib" / "terminal-cleanup-separated"
  checkout = root / "worktree"
  git_dir = root / "git"
  root.mkdir(parents=True)
  subprocess.run(
    ["git", "init", "-q", f"--separate-git-dir={git_dir}", str(checkout)],
    check=True,
  )

  record = {
    "status": "closed",
    "plan": {"repo_path": str(checkout)},
  }
  real_rmtree = shutil.rmtree
  checkout_failed = False

  def fail_checkout_once(path, *args, **kwargs):
    nonlocal checkout_failed
    if Path(path) == checkout and not checkout_failed:
      checkout_failed = True
      raise OSError("simulated checkout removal failure")
    return real_rmtree(path, *args, **kwargs)

  monkeypatch.setattr(shutil, "rmtree", fail_checkout_once)
  with pytest.raises(OSError, match="simulated checkout removal failure"):
    _cleanup_terminal_staging_checkout(record)
  assert checkout.exists()
  assert not git_dir.exists()

  assert _cleanup_terminal_staging_checkout(record) is True
  assert not checkout.exists()
  assert not git_dir.exists()


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

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)

  fork_slug = _ensure_owner_fork_remote(repo, "mobius-os/app-demo", "octocat")

  assert fork_slug == "octocat/app-demo-1"
  assert (
    "remote", "set-url", "origin",
    "https://github.com/mobius-os/app-demo.git",
  ) in git_calls
  assert ("repo", "fork", "--remote", "--remote-name", "fork") in gh_calls
  assert all("mobius-os/app-demo" not in call for call in gh_calls)


def test_ensure_owner_fork_remote_never_trusts_a_cached_remote(
  tmp_path, monkeypatch,
):
  from app.routes.github import _ensure_owner_fork_remote

  repo = tmp_path / "repo"
  repo.mkdir()
  git_calls = []
  gh_calls = []
  reforked = False

  def fake_git(repo_path, *args, check=True):
    nonlocal reforked
    git_calls.append(args)
    if args == ("remote", "get-url", "fork"):
      # A cached remote is present but names a fork the owner deleted; after
      # re-resolving, the remote points at the live fork.
      if reforked:
        return _cp("https://github.com/octocat/mobius.git\n")
      return _cp("https://github.com/octocat/stale-fork.git\n")
    if args == ("remote", "get-url", "origin"):
      return _cp("https://github.com/mobius-os/mobius.git\n")
    return _cp("")

  def fake_gh(repo_path, *args, check=True):
    nonlocal reforked
    gh_calls.append(args)
    if args == ("repo", "fork", "--remote", "--remote-name", "fork"):
      reforked = True
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)

  fork_slug = _ensure_owner_fork_remote(repo, "mobius-os/mobius", "octocat")

  assert fork_slug == "octocat/mobius"
  # The cached remote is dropped and re-resolved through gh, never returned
  # directly — so a deleted fork heals instead of failing the push.
  assert ("remote", "remove", "fork") in git_calls
  assert ("repo", "fork", "--remote", "--remote-name", "fork") in gh_calls


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


@pytest.mark.parametrize(
  "failure_kind",
  ["timeout", "launch-error"],
)
def test_submit_contribution_keeps_accepted_pr_open_on_label_transport_failure(
  client, owner_token, monkeypatch, failure_kind,
):
  label_failure = (
    subprocess.TimeoutExpired(["gh", "api"], timeout=30)
    if failure_kind == "timeout"
    else OSError("gh could not start")
  )
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = f"rec-pr-label-{failure_kind}"
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
      "labels": ["bug"],
    },
  }
  _write_contribution(app_id, record_id, record, diff_text)

  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")
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
  baseline_checked_out = checked_out_connections()
  upstream_pool_counts = []

  def fake_gh(repo_path, *args, check=True):
    nonlocal fork_ready
    upstream_pool_counts.append(checked_out_connections())
    gh_calls.append(args)
    if args[:2] == ("repo", "fork"):
      fork_ready = True
      return _cp("")
    if args[:2] == ("pr", "list"):
      return _cp("[]")
    if args[:2] == ("pr", "create"):
      return _cp("https://github.com/mobius-os/app-demo/pull/42\n")
    if args[:2] == ("api", "--paginate"):
      raise label_failure
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)

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
  assert body["record"]["last_submit_labels_requested"] == ["bug"]
  assert body["record"]["last_submit_labels_applied"] == []
  assert "pull request is open" in body["record"]["last_submit_labels_note"]
  assert ("repo", "fork", "--remote", "--remote-name", "fork") in gh_calls
  assert not any(call[:2] == ("remote", "set-url") for call in git_calls)
  create_call = next(call for call in gh_calls if call[:2] == ("pr", "create"))
  assert "--draft" not in create_call
  assert "octocat:fix/demo-polish" in create_call
  assert create_call[-2:] == ("--base", "main")
  assert ("push", "fork", "HEAD:refs/heads/fix/demo-polish") in git_calls
  assert sum(call[:1] == ("fetch",) for call in git_calls) == 1
  # Exactly one pre-publication truth check (`--state all`), and no
  # ambiguous-create recovery probe on this clean-create path.
  assert sum(
    call[:2] == ("pr", "list") and "all" in call for call in gh_calls
  ) == 1
  assert not any(
    call[:2] == ("pr", "list") and "all" not in call for call in gh_calls
  )
  assert ("checkout", "-q", "develop") in git_calls
  assert upstream_pool_counts
  assert set(upstream_pool_counts) == {baseline_checked_out}

  stored = json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id) /
     "contributions" / f"{record_id}.json").read_text()
  )
  assert stored["status"] == "open"
  assert stored["number"] == 42
  assert stored["head_repository"] == "octocat/app-demo-1"
  assert stored["last_submit_labels_requested"] == ["bug"]
  assert stored["last_submit_labels_applied"] == []
  assert stored["last_submit_labels_note"] == body["record"]["last_submit_labels_note"]


@pytest.mark.parametrize(
  ("failure_kind", "existing_mode"),
  [
    ("timeout", "match"),
    ("launch-error", "match"),
    ("timeout", "absent"),
    ("launch-error", "absent"),
    ("timeout", "stale-head"),
    ("timeout", "wrong-head"),
    ("timeout", "wrong-owner"),
  ],
)
def test_submit_contribution_recovers_ambiguous_create_by_exact_pushed_head(
  client, owner_token, monkeypatch, failure_kind, existing_mode,
):
  """A lost create response retries exact reads and never creates twice."""
  create_failure = (
    subprocess.TimeoutExpired(["gh", "pr", "create"], timeout=30)
    if failure_kind == "timeout"
    else OSError("gh could not start")
  )
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = f"rec-pr-create-{failure_kind}-{existing_mode}"
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
      "labels": ["bug"],
    },
  }
  _write_contribution(app_id, record_id, record, diff_text)

  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")
  monkeypatch.setattr(
    "app.github_contribution_git._assert_fresh",
    lambda *_args, **_kwargs: (base, head, record["plan"]["diff_sha256"]),
  )
  monkeypatch.setattr("app.github_contribution_git._assert_coauthor_trailer", lambda *_args: None)
  monkeypatch.setattr("app.github_contribution_git._assert_clean_worktree", lambda *_args: None)
  monkeypatch.setattr(
    "app.github_contribution_git._normalize_head_attribution",
    lambda *_args, **_kwargs: {},
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_merges_with_upstream",
    lambda *_args, **_kwargs: {
      "last_submit_upstream_branch": "main",
      "last_submit_upstream_sha": base,
    },
  )
  monkeypatch.setattr(
    "app.github_contributions._ensure_owner_fork_remote",
    lambda *_args, **_kwargs: "octocat/app-demo-1",
  )
  monkeypatch.setattr(
    "app.github_contributions._push_topic_branch",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr("app.github_contributions.time.sleep", lambda _seconds: None)

  git_calls = []

  def fake_git(repo_path, *args, check=True):
    git_calls.append(args)
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp("develop\n")
    if args == ("rev-parse", "HEAD"):
      return _cp(head + "\n")
    return _cp("")

  gh_calls = []

  def fake_gh(repo_path, *args, check=True):
    gh_calls.append(args)
    if args[:2] == ("pr", "create"):
      raise create_failure
    if args[:2] == ("pr", "list"):
      if existing_mode == "absent":
        return _cp("[]")
      recovery_probe = sum(
        call[:2] == ("pr", "list") and "all" not in call
        for call in gh_calls
      )
      stale = existing_mode == "stale-head" and recovery_probe == 1
      found_head = head if existing_mode != "wrong-head" and not stale else "c" * 40
      return _cp(json.dumps([{
        "url": "https://github.com/mobius-os/app-demo/pull/42",
        "headRefName": "fix/demo-polish",
        "headRefOid": found_head,
        "headRepositoryOwner": {
          "login": "someone-else" if existing_mode == "wrong-owner" else "octocat",
        },
      }]))
    if args[:2] == ("api", "--paginate"):
      return _cp("bug\n")
    if args[:3] == ("api", "--method", "POST"):
      return _cp("[]")
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)

  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  creates = [call for call in gh_calls if call[:2] == ("pr", "create")]
  # The pre-publication truth check is a separate `--state all` lookup; the
  # ambiguous-create RECOVERY probe queries open PRs only. Count them apart.
  preflights = [
    call for call in gh_calls
    if call[:2] == ("pr", "list") and "all" in call
  ]
  probes = [
    call for call in gh_calls
    if call[:2] == ("pr", "list") and "all" not in call
  ]
  assert len(creates) == 1, "an ambiguous response must never trigger a second create"
  assert len(preflights) == 1
  expected_probes = (
    1 if existing_mode == "match"
    else 2 if existing_mode == "stale-head"
    else 3
  )
  assert len(probes) == expected_probes
  assert creates[0][-2:] == ("--base", "main")
  assert "url,headRefName,headRefOid,headRepositoryOwner" in probes[0]
  assert probes[0][probes[0].index("--head") + 1] == "fix/demo-polish"
  assert "octocat:fix/demo-polish" not in probes[0]
  assert probes[0][probes[0].index("--base") + 1] == "main"
  assert ("checkout", "-q", "develop") in git_calls

  stored = json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id) /
     "contributions" / f"{record_id}.json").read_text()
  )
  if existing_mode in {"match", "stale-head"}:
    assert response.status_code == 200, response.text
    assert response.json()["url"].endswith("/pull/42")
    assert stored["status"] == "open"
    assert stored["url"].endswith("/pull/42")
    assert stored["last_submit_push_sha"] == head
    assert stored["last_submit_labels_applied"] == ["bug"]
  else:
    assert response.status_code == 409, response.text
    assert stored["status"] == "prepared"
    assert stored["last_submit_stage"] == "pushed"
    assert stored["last_submit_push_sha"] == head
    assert "url" not in stored


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
  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")

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

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)

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
  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")

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

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)

  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 200, r.text
  assert ("remote", "remove", "fork") in git_calls
  assert ("repo", "fork", "--remote", "--remote-name", "fork") in gh_calls
  assert not any(call[:2] == ("remote", "set-url") for call in git_calls)
  assert ("push", "fork", "HEAD:refs/heads/fix/demo-polish") in git_calls


def test_stack_layer_cannot_be_sent_through_standalone_endpoint(
  client, owner_token, monkeypatch,
):
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = "stack-standalone-guard"
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/mobius",
    "status": "prepared",
    "title": "Layer 1",
    "branch": "stack/guarded/01-layer",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/mobius",
      "branch": "stack/guarded/01-layer",
      "stack": {
        "id": "guarded",
        "position": 1,
        "total": 2,
        "parent_record_id": "",
        "base_branch": "main",
      },
    },
  }
  _write_contribution(app_id, record_id, record, "reviewed")
  called = False

  def submit(*args, **kwargs):
    nonlocal called
    called = True

  monkeypatch.setattr("app.routes.github._submit_prepared_pr", submit)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 409
  assert "complete chain" in response.json()["detail"]
  assert called is False
  stored = json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id) /
     "contributions" / f"{record_id}.json").read_text()
  )
  assert stored["status"] == "prepared"


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

  baseline = checked_out_connections()
  preflight_pool_counts = []

  def fake_preflight(rows):
    preflight_pool_counts.append(checked_out_connections())

  monkeypatch.setattr(
    "app.routes.github._preflight_prepared_stack",
    fake_preflight,
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
  assert preflight_pool_counts == [baseline]


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


def test_submit_contribution_stack_accepts_public_draft_parent():
  from app.routes.github import _validate_stack_records

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

  validated = _validate_stack_records(records)
  assert [item["record"]["status"] for item in validated] == [
    "draft", "prepared",
  ]


def test_stack_validation_allows_retargeted_public_history():
  from app.routes.github import _validate_stack_records

  stack_id = "retargeted-history"
  records = []
  statuses = ("merged", "open", "prepared")
  heads = ("a" * 40, "b" * 40, "c" * 40)
  for position, status in enumerate(statuses, 1):
    branch = f"stack/{stack_id}/0{position}-layer"
    if position == 1:
      base_sha = "0" * 40
    elif position == 2:
      base_sha = "f" * 40
    else:
      base_sha = heads[position - 2]
    records.append({
      "id": f"retargeted-{position}",
      "type": "pr", "repo": "mobius-os/mobius", "status": status,
      "branch": branch,
      "plan": {
        "action": "pr", "repo": "mobius-os/mobius", "branch": branch,
        "base_sha": base_sha, "head_sha": heads[position - 1],
        "stack": {
          "id": stack_id, "position": position, "total": 3,
          "parent_record_id": "" if position == 1 else f"retargeted-{position - 1}",
          "base_branch": "main" if position == 1 else f"stack/{stack_id}/0{position - 1}-layer",
        },
      },
    })

  validated = _validate_stack_records(records)
  assert [item["record"]["status"] for item in validated] == list(statuses)


def test_stack_preflight_requires_refresh_after_parent_merges(monkeypatch):
  from app.routes.github import ContributionSubmitError, _preflight_prepared_stack

  _write_token(login="octocat")
  repo = Path(get_settings().data_dir) / "contributions" / "merged-retry" / "repo"
  (repo / ".git").mkdir(parents=True)
  stack_id = "merged-retry"
  parent_branch = f"stack/{stack_id}/01-parent"
  rows = [
    {
      "record": {
        "id": "merged-parent", "status": "merged", "repo": "mobius-os/mobius",
        "branch": parent_branch,
        "plan": {
          "repo": "mobius-os/mobius", "branch": parent_branch,
          "head_sha": "a" * 40,
        },
      },
      "stack": {"base_branch": "main"},
    },
    {
      "record": {
        "id": "private-child", "status": "submitting", "repo": "mobius-os/mobius",
        "branch": f"stack/{stack_id}/02-child",
        "plan": {
          "repo": "mobius-os/mobius", "repo_path": str(repo),
          "branch": f"stack/{stack_id}/02-child",
        },
      },
      "stack": {"base_branch": parent_branch},
    },
  ]
  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")
  monkeypatch.setattr("app.github_contribution_git._upstream_default_branch", lambda *args: "main")
  monkeypatch.setattr("app.github_contribution_git._assert_upstream_push_permission", lambda *args: None)

  with pytest.raises(ContributionSubmitError, match="already merged"):
    _preflight_prepared_stack(rows)


def test_stack_preflight_rejects_changed_existing_parent(monkeypatch, tmp_path):
  from app.routes.github import ContributionSubmitError, _assert_upstream_branch_at

  expected = "a" * 40
  changed = "b" * 40

  def fake_gh(repo, *args, check=True):
    assert args == (
      "api",
      "repos/mobius-os/mobius/git/ref/heads/stack%2Fchat%2F01-parent",
      "--jq", ".object.sha",
    )
    return _cp(changed + "\n")

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  with pytest.raises(ContributionSubmitError, match="changed after review"):
    _assert_upstream_branch_at(
      tmp_path, "mobius-os/mobius", "stack/chat/01-parent", expected,
    )


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
  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")
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

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)

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


def _existing_fork_submission(tmp_path, monkeypatch):
  _write_token(login="octocat")
  repo = tmp_path / "existing-fork-pr"
  (repo / ".git").mkdir(parents=True)
  branch = "feat/existing-review"
  base = "b" * 40
  head = "a" * 40
  diff_text = "diff --git a/model.py b/model.py\n+reviewed\n"
  diff_path = tmp_path / "existing.diff"
  diff_path.write_text(diff_text)
  record = {
    "id": "existing-fork-pr",
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "submitting",
    "title": "Refine the existing contribution",
    "branch": branch,
    "plan": {
      "action": "pr_update",
      "repo": "mobius-os/app-demo",
      "title": "Refine the existing contribution",
      "body_draft": "Reviewed existing contribution update.",
      "branch": branch,
      "repo_path": str(repo),
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
    },
  }
  monkeypatch.setattr(
    "app.github_contributions.shutil.which", lambda name: f"/bin/{name}",
  )
  monkeypatch.setattr(
    "app.github_contributions._safe_repo_path", lambda _raw: repo,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_fresh",
    lambda *_args, **_kwargs: (base, head, record["plan"]["diff_sha256"]),
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_clean_worktree", lambda *_args: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_coauthor_trailer", lambda *_args: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._normalize_head_attribution",
    lambda *_args, **_kwargs: {},
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_merges_with_upstream",
    lambda *_args, **_kwargs: {
      "last_submit_upstream_branch": "main",
      "last_submit_upstream_sha": base,
    },
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_upstream_push_permission",
    lambda *_args: pytest.fail("a fork-backed PR must not push upstream"),
  )
  monkeypatch.setattr(
    "app.github_contributions._push_branch",
    lambda *_args: pytest.fail("a fork-backed PR must not push upstream"),
  )

  def fake_git(_repo, *args, check=True):
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp(branch + "\n")
    if args == ("rev-parse", "HEAD"):
      return _cp(head + "\n")
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  return record, diff_path, branch, head


def test_existing_pr_update_uses_its_verified_fork_destination(
  tmp_path, monkeypatch,
):
  from app.routes.github import _submit_prepared_pr

  record, diff_path, branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  fork_calls = []
  monkeypatch.setattr(
    "app.github_contributions._ensure_owner_fork_remote",
    lambda _repo, upstream, login: (
      fork_calls.append((upstream, login)) or "octocat/app-demo"
    ),
  )
  pushes = []
  monkeypatch.setattr(
    "app.github_contributions._push_topic_branch",
    lambda _repo, pushed_branch, source: (
      pushes.append((pushed_branch, source)) or None
    ),
  )
  confirmations = []

  def confirm(
    _repo,
    upstream,
    login,
    pushed_branch,
    *,
    expected_head_sha,
    base_branch,
    same_repo,
  ):
    confirmations.append((
      upstream,
      login,
      pushed_branch,
      expected_head_sha,
      base_branch,
      same_repo,
    ))
    return "https://github.com/mobius-os/app-demo/pull/58"

  monkeypatch.setattr(
    "app.github_contributions._find_existing_pr", confirm,
  )

  url, number, patch = _submit_prepared_pr(
    record,
    diff_path,
    expected_existing_pr_number=58,
    expected_existing_head_repository="octocat/app-demo",
  )

  assert url.endswith("/pull/58")
  assert number == 58
  assert fork_calls == [("mobius-os/app-demo", "octocat")]
  assert pushes == [(branch, "HEAD")]
  assert confirmations == [(
    "mobius-os/app-demo",
    "octocat",
    branch,
    head,
    "main",
    False,
  )]
  assert patch["head_repository"] == "octocat/app-demo"
  assert patch["last_submit_push_sha"] == head
  assert patch["last_pushed_branch"] == f"octocat:{branch}"


def test_existing_pr_update_stops_if_verified_fork_remote_does_not_match(
  tmp_path, monkeypatch,
):
  from app.routes.github import ContributionSubmitError, _submit_prepared_pr

  record, diff_path, _branch, _head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  monkeypatch.setattr(
    "app.github_contributions._ensure_owner_fork_remote",
    lambda *_args, **_kwargs: "octocat/app-demo-1",
  )
  monkeypatch.setattr(
    "app.github_contributions._push_topic_branch",
    lambda *_args: pytest.fail("a mismatched fork must stop before push"),
  )
  monkeypatch.setattr(
    "app.github_contributions._find_existing_pr",
    lambda *_args, **_kwargs: pytest.fail("an unpushed branch cannot confirm"),
  )

  with pytest.raises(ContributionSubmitError) as err:
    _submit_prepared_pr(
      record,
      diff_path,
      expected_existing_pr_number=58,
      expected_existing_head_repository="octocat/app-demo",
    )

  assert "no longer matches" in err.value.message
  assert "Nothing was pushed" in err.value.message


def test_existing_pr_update_rejects_an_unowned_head_repository(
  tmp_path, monkeypatch,
):
  from app.routes.github import ContributionSubmitError, _submit_prepared_pr

  record, diff_path, _branch, _head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  monkeypatch.setattr(
    "app.github_contributions._ensure_owner_fork_remote",
    lambda *_args, **_kwargs: pytest.fail("an unowned PR must stop before fork lookup"),
  )

  with pytest.raises(ContributionSubmitError) as err:
    _submit_prepared_pr(
      record,
      diff_path,
      expected_existing_pr_number=58,
      expected_existing_head_repository="someone-else/app-demo",
    )

  assert "not owned by the connected GitHub account" in err.value.message
  assert "Nothing was pushed" in err.value.message


def test_land_contribution_stack_marks_every_layer_merged(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  stack_id = "green-app-stack"
  record_ids = ["green-stack-01", "green-stack-02"]
  base = "b" * 40
  parent_head = "a" * 40
  top_head = "c" * 40
  for position, record_id in enumerate(record_ids, 1):
    repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
    (repo / ".git").mkdir(parents=True)
    branch = f"stack/{stack_id}/0{position}-layer"
    diff_text = f"diff --git a/layer-{position} b/layer-{position}\n+green\n"
    record = {
      "id": record_id,
      "type": "pr",
      "repo": "mobius-os/app-demo",
      "status": "open",
      "title": f"Layer {position}",
      "branch": branch,
      "number": 90 + position,
      "url": f"https://github.com/mobius-os/app-demo/pull/{90 + position}",
      "plan": {
        "action": "pr",
        "repo": "mobius-os/app-demo",
        "branch": branch,
        "repo_path": str(repo),
        "base_sha": base if position == 1 else parent_head,
        "head_sha": parent_head if position == 1 else top_head,
        "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
        "stack": {
          "id": stack_id,
          "position": position,
          "total": 2,
          "parent_record_id": "" if position == 1 else record_ids[0],
          "base_branch": "main" if position == 1 else f"stack/{stack_id}/01-layer",
        },
      },
    }
    _write_contribution(app_id, record_id, record, diff_text)

  seen = {}

  def fake_land(rows):
    seen["calls"] = seen.get("calls", 0) + 1
    seen["statuses"] = [row["record"]["status"] for row in rows]
    seen["ids"] = [row["record"]["id"] for row in rows]
    seen["journals"] = [
      (
        row["record"]["land_target_branch"],
        row["record"]["land_expected_base_sha"],
        row["record"]["land_head_sha"],
      )
      for row in rows
    ]
    return "main", top_head

  monkeypatch.setattr("app.routes.github._land_reviewed_stack", fake_land)
  response = client.post(
    f"/api/github/contributions/{app_id}/land-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 200, response.text
  assert seen == {
    "calls": 1,
    "statuses": ["landing", "landing"],
    "ids": record_ids,
    "journals": [("main", base, top_head), ("main", base, top_head)],
  }
  body = response.json()
  assert body["target_branch"] == "main"
  assert body["landed_sha"] == top_head
  assert [record["status"] for record in body["records"]] == ["merged", "merged"]
  assert all(record["last_land_mode"] == "atomic-fast-forward" for record in body["records"])

  # A lost HTTP response may repeat the exact request. The durable merged
  # journal makes that retry idempotent; it must never call the pusher again.
  repeated = client.post(
    f"/api/github/contributions/{app_id}/land-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )
  assert repeated.status_code == 200, repeated.text
  assert seen["calls"] == 1
  assert [record["status"] for record in repeated.json()["records"]] == [
    "merged", "merged",
  ]

  storage = (
    Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
  )
  reconcile_statuses = []

  def fake_reconcile(rows):
    reconcile_statuses.append([row["record"]["status"] for row in rows])
    return "main", top_head

  monkeypatch.setattr(
    "app.routes.github._reconcile_stack_landing", fake_reconcile,
  )

  # A process exit while recording success can leave one durable layer merged
  # and its sibling still landing. The next identical request reconciles the
  # shared journal and finishes the record writes without another push.
  first = json.loads((storage / f"{record_ids[0]}.json").read_text())
  second = json.loads((storage / f"{record_ids[1]}.json").read_text())
  second["status"] = "landing"
  atomic_write(storage / f"{record_ids[1]}.json", json.dumps(second))
  mixed_success = client.post(
    f"/api/github/contributions/{app_id}/land-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )
  assert mixed_success.status_code == 200, mixed_success.text
  assert reconcile_statuses[-1] == ["merged", "landing"]
  assert [record["status"] for record in mixed_success.json()["records"]] == [
    "merged", "merged",
  ]

  # A process exit during the initial claim (or while reopening a proven
  # pre-push failure) leaves open/landing. Complete the exact saved journal
  # first, then reconcile upstream just like an all-landing retry.
  first["status"] = "open"
  second["status"] = "landing"
  atomic_write(storage / f"{record_ids[0]}.json", json.dumps(first))
  atomic_write(storage / f"{record_ids[1]}.json", json.dumps(second))
  mixed_claim = client.post(
    f"/api/github/contributions/{app_id}/land-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )
  assert mixed_claim.status_code == 200, mixed_claim.text
  assert reconcile_statuses[-1] == ["landing", "landing"]
  assert [record["status"] for record in mixed_claim.json()["records"]] == [
    "merged", "merged",
  ]


def test_land_contribution_stack_restores_open_records_on_preflight_failure(
  client, owner_token, monkeypatch,
):
  from app.routes.github import ContributionSubmitError

  _write_token(login="octocat")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  stack_id = "red-app-stack"
  ids = ["red-stack-01", "red-stack-02"]
  for position, record_id in enumerate(ids, 1):
    repo = Path(get_settings().data_dir) / "contributions" / record_id / "repo"
    (repo / ".git").mkdir(parents=True)
    parent = "a" * 40
    branch = f"stack/{stack_id}/0{position}-layer"
    record = {
      "id": record_id, "type": "pr", "repo": "mobius-os/app-demo",
      "status": "open", "branch": branch, "number": position,
      "plan": {
        "action": "pr", "repo": "mobius-os/app-demo", "branch": branch,
        "repo_path": str(repo),
        "base_sha": "b" * 40 if position == 1 else parent,
        "head_sha": parent if position == 1 else "c" * 40,
        "stack": {
          "id": stack_id, "position": position, "total": 2,
          "parent_record_id": "" if position == 1 else ids[0],
          "base_branch": "main" if position == 1 else f"stack/{stack_id}/01-layer",
        },
      },
    }
    _write_contribution(app_id, record_id, record, "reviewed")

  monkeypatch.setattr(
    "app.routes.github._land_reviewed_stack",
    lambda rows: (_ for _ in ()).throw(ContributionSubmitError("CI is still running.")),
  )
  response = client.post(
    f"/api/github/contributions/{app_id}/land-stack",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"record_ids": ids},
  )

  assert response.status_code == 409
  assert [record["status"] for record in response.json()["detail"]["records"]] == [
    "open", "open",
  ]
  assert all(
    record["last_land_error"] == "CI is still running."
    for record in response.json()["detail"]["records"]
  )

  # Once the irreversible push has started, an unreadable upstream ref is not
  # proof of failure. Keep the journal claimed so a later request can reconcile
  # the accepted push instead of reopening the stack and risking a duplicate.
  monkeypatch.setattr(
    "app.routes.github._land_reviewed_stack",
    lambda rows: (_ for _ in ()).throw(ContributionSubmitError(
      "Landing result is not confirmed.",
      status_code=503,
      code="landing_unconfirmed",
    )),
  )
  uncertain = client.post(
    f"/api/github/contributions/{app_id}/land-stack",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"record_ids": ids},
  )

  assert uncertain.status_code == 503
  assert uncertain.json()["detail"]["code"] == "landing_unconfirmed"
  assert [record["status"] for record in uncertain.json()["detail"]["records"]] == [
    "landing", "landing",
  ]


def test_stack_landing_requires_every_pr_check_to_be_green(monkeypatch, tmp_path):
  from app.routes.github import ContributionSubmitError, _assert_pr_checks_green

  record = {
    "number": 17,
    "url": "https://github.com/mobius-os/app-demo/pull/17",
  }

  def fake_gh(repo, *args, check=True):
    return _cp(json.dumps({
      "state": "OPEN",
      "isDraft": False,
      "baseRefName": "main",
      "headRefName": "stack/demo/01-layer",
      "headRepositoryOwner": {"login": "mobius-os"},
      "url": record["url"],
      "statusCheckRollup": [{
        "__typename": "CheckRun", "name": "test",
        "status": "IN_PROGRESS", "conclusion": "",
      }],
    }))

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  with pytest.raises(ContributionSubmitError, match="still has CI running"):
    _assert_pr_checks_green(
      tmp_path,
      upstream_repo="mobius-os/app-demo",
      record=record,
      base_branch="main",
      head_branch="stack/demo/01-layer",
    )


def test_stack_landing_accepts_successful_neutral_and_skipped_checks(
  monkeypatch, tmp_path,
):
  from app.routes.github import _assert_pr_checks_green

  record = {
    "number": 18,
    "url": "https://github.com/mobius-os/app-demo/pull/18",
  }

  def fake_gh(repo, *args, check=True):
    return _cp(json.dumps({
      "state": "OPEN",
      "isDraft": False,
      "baseRefName": "main",
      "headRefName": "stack/demo/01-layer",
      "headRepositoryOwner": {"login": "mobius-os"},
      "url": record["url"],
      "statusCheckRollup": [
        {"__typename": "CheckRun", "name": "test", "status": "COMPLETED", "conclusion": "SUCCESS"},
        {"__typename": "CheckRun", "name": "optional", "status": "COMPLETED", "conclusion": "NEUTRAL"},
        {"__typename": "CheckRun", "name": "paths", "status": "COMPLETED", "conclusion": "SKIPPED"},
        {"__typename": "StatusContext", "context": "external", "state": "SUCCESS"},
      ],
    }))

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  _assert_pr_checks_green(
    tmp_path,
    upstream_repo="mobius-os/app-demo",
    record=record,
    base_branch="main",
    head_branch="stack/demo/01-layer",
  )


def test_stack_landing_never_bypasses_protected_branch(monkeypatch, tmp_path):
  from app.routes.github import ContributionSubmitError, _assert_unprotected_landing_target

  calls = []

  def fake_gh(repo, *args, check=True):
    calls.append(args)
    return _cp('{"required_status_checks": {}}')

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  with pytest.raises(ContributionSubmitError, match="is protected"):
    _assert_unprotected_landing_target(tmp_path, "mobius-os/mobius", "main")
  assert len(calls) == 1


def test_stack_tip_push_uses_exact_base_lease(monkeypatch, tmp_path):
  from app.routes.github import _push_stack_tip_with_lease

  calls = []

  def fake_git(repo, *args, check=True):
    calls.append(args)
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  _push_stack_tip_with_lease(
    tmp_path,
    upstream_repo="mobius-os/app-demo",
    target_branch="main",
    expected_base="b" * 40,
    landed_sha="c" * 40,
  )
  assert calls == [(
    "push",
    f"--force-with-lease=refs/heads/main:{'b' * 40}",
    "https://github.com/mobius-os/app-demo.git",
    f"{'c' * 40}:refs/heads/main",
  )]


def test_stack_tip_push_reconciles_a_lost_success_response(monkeypatch, tmp_path):
  from app.routes.github import _push_stack_tip_with_lease

  landed_sha = "c" * 40
  git_calls = []
  gh_calls = []

  def fake_git(repo, *args, check=True):
    git_calls.append(args)
    return _cp("", returncode=1, stderr="remote end hung up unexpectedly")

  def fake_gh(repo, *args, check=True):
    gh_calls.append(args)
    return _cp(landed_sha)

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  _push_stack_tip_with_lease(
    tmp_path,
    upstream_repo="mobius-os/app-demo",
    target_branch="main",
    expected_base="b" * 40,
    landed_sha=landed_sha,
  )

  assert len(git_calls) == 1
  assert gh_calls == [(
    "api", "repos/mobius-os/app-demo/git/ref/heads/main",
    "--jq", ".object.sha",
  )]


def test_stack_tip_push_keeps_journal_when_result_cannot_be_read(
  monkeypatch, tmp_path,
):
  from app.routes.github import ContributionSubmitError, _push_stack_tip_with_lease

  monkeypatch.setattr("app.github_contributions._PUSH_RETRIES", 1)
  monkeypatch.setattr(
    "app.github_contribution_git._git",
    lambda repo, *args, check=True: _cp(
      "", returncode=1, stderr="remote end hung up unexpectedly",
    ),
  )
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda *args, **kwargs: None,
  )

  with pytest.raises(ContributionSubmitError) as caught:
    _push_stack_tip_with_lease(
      tmp_path,
      upstream_repo="mobius-os/app-demo",
      target_branch="main",
      expected_base="b" * 40,
      landed_sha="c" * 40,
    )

  assert caught.value.status_code == 503
  assert caught.value.code == "landing_unconfirmed"


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
  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")
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

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", lambda *args, **kwargs: _cp(""))

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
  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")
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

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", lambda *args, **kwargs: _cp(""))

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
  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")

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

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)

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
  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")

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


def _prepared_existing_pr_update(app_id: int, record_id: str) -> dict:
  repo_path = (
    Path(get_settings().data_dir) / "contrib" / record_id / "worktree"
  )
  (repo_path / ".git").mkdir(parents=True)
  head = "a" * 40
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Refine the existing contribution",
    "branch": "feat/existing-review",
    "number": 58,
    "url": "https://github.com/mobius-os/app-demo/pull/58",
    "head_repository": "octocat/app-demo",
    "submitted_at": "2026-08-20T12:00:00Z",
    "plan": {
      "action": "pr_update",
      "repo": "mobius-os/app-demo",
      "title": "Refine the existing contribution",
      "body_draft": "## Summary\n\nRefines the open contribution.",
      "branch": "feat/existing-review",
      "repo_path": str(repo_path),
      "base_sha": "b" * 40,
      "head_sha": head,
      "diff_sha256": "d" * 64,
    },
    "quality_review": {
      "state": "all_clear",
      "reviewed_head_sha": head,
      "reviewed_at": "2026-08-24T18:00:00Z",
    },
  }
  _write_contribution(app_id, record_id, record, "reviewed diff")
  return record


def test_existing_pr_update_uses_owner_approved_exact_target(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  record_id = "existing-pr-update"
  original = _prepared_existing_pr_update(app_id, record_id)
  calls = []

  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target_error",
    lambda repo, number, head_repo, branch: calls.append(
      ("target", repo, number, head_repo, branch)
    ) or None,
  )

  def submit(
    record,
    diff_path,
    *,
    expected_existing_pr_number=None,
    expected_existing_head_repository=None,
    **_kwargs,
  ):
    calls.append((
      "submit",
      record["status"],
      expected_existing_pr_number,
      expected_existing_head_repository,
      diff_path.name,
    ))
    return (
      "https://github.com/mobius-os/app-demo/pull/58",
      58,
      {"last_submit_push_sha": record["plan"]["head_sha"]},
    )

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", submit)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update-existing",
    headers={"Authorization": f"Bearer {app_token}"},
    json={},
  )

  assert response.status_code == 200, response.text
  updated = response.json()["record"]
  assert updated["status"] == "open"
  assert updated["number"] == 58
  assert updated["submitted_at"] == original["submitted_at"]
  assert updated["last_submit_push_sha"] == original["plan"]["head_sha"]
  assert updated["last_updated_pr_at"]
  assert calls == [
    (
      "target", "mobius-os/app-demo", 58,
      "octocat/app-demo", "feat/existing-review",
    ),
    (
      "submit", "submitting", 58, "octocat/app-demo",
      f"{record_id}.diff",
    ),
  ]


def test_existing_pr_update_stays_successful_if_followup_metadata_fails(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  record_id = "existing-pr-update-metadata-failure"
  original = _prepared_existing_pr_update(app_id, record_id)
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target_error",
    lambda *_args: None,
  )
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda record, _diff_path, **_kwargs: (
      "https://github.com/mobius-os/app-demo/pull/58",
      58,
      {"last_submit_push_sha": record["plan"]["head_sha"]},
    ),
  )

  def fail_metadata(*_args, **_kwargs):
    raise RuntimeError("metadata down")

  monkeypatch.setattr(
    "app.contribution_autopilot.refresh_granted_head",
    fail_metadata,
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update-existing",
    headers={"Authorization": f"Bearer {app_token}"},
    json={},
  )

  assert response.status_code == 200, response.text
  updated = response.json()["record"]
  assert updated["status"] == "open"
  assert updated["submitted_at"] == original["submitted_at"]
  assert updated["last_submit_push_sha"] == original["plan"]["head_sha"]


def test_existing_pr_update_rechecks_target_before_any_push(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  record_id = "existing-pr-drifted"
  _prepared_existing_pr_update(app_id, record_id)
  pushed = []
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target_error",
    lambda *_args: "The live branch moved.",
  )
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pushed.append(True),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update-existing",
    headers={"Authorization": f"Bearer {app_token}"},
    json={},
  )

  assert response.status_code == 409, response.text
  detail = response.json()["detail"]
  assert "changed since this update was prepared" in detail["message"]
  assert detail["detail"] == "The live branch moved."
  assert detail["record"]["status"] == "prepared"
  assert pushed == []


def test_existing_pr_update_is_distinct_from_new_pr_send(
  client, owner_token,
):
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  record_id = "existing-pr-wrong-action"
  _prepared_existing_pr_update(app_id, record_id)

  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 400
  assert "supports pull requests" in response.json()["detail"]


def test_prepared_pr_update_cannot_start_a_new_autopilot_round(
  client, owner_token,
):
  from app import contribution_autopilot
  from app.database import SessionLocal

  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  record_id = "prepared-update-autopilot-guard"
  record = _prepared_existing_pr_update(app_id, record_id)
  session = SessionLocal()
  try:
    contribution_autopilot.stamp_grant(
      session,
      app_id,
      record_id,
      head_sha=record["plan"]["head_sha"],
      target_repo=record["repo"],
      target_pr_number=record["number"],
      target_head_repository=record["head_repository"],
      target_branch=record["branch"],
      target_repo_path=record["plan"]["repo_path"],
    )
  finally:
    session.close()

  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/respond",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"attention": {"key": "review:late-event"}},
  )

  assert response.status_code == 200, response.text
  assert response.json()["status"] == "not_granted"
  session = SessionLocal()
  try:
    row = contribution_autopilot.get_row(session, app_id, record_id)
    assert row is not None
    assert row.state == "idle"
  finally:
    session.close()


def test_chat_projection_marks_exact_reviewed_pr_updates_sendable(
  client, owner_token, monkeypatch,
):
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  record_id = "existing-pr-chat-card"
  record = _prepared_existing_pr_update(app_id, record_id)
  record["chat_id"] = "chat-existing-update"
  _write_contribution(app_id, record_id, record, "reviewed diff")
  monkeypatch.setattr(
    github_routes,
    "_inspect_prepared_review",
    lambda record, _diff_path, _github_state: {
      "id": record["id"],
      "state": "ready",
      "code": "ready",
      "message": "Still matches the exact source you reviewed.",
    },
  )

  response = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-existing-update",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  projected = response.json()["records"][0]
  assert projected["action"] == "pr_update"
  assert projected["quality_review_ready"] is True
  assert projected["review"]["state"] == "ready"


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


def test_refresh_releases_db_before_github_network(
  client, owner_token, monkeypatch,
):
  _write_token(token="gh-checks-tok")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _write_open_pr_record(app_id)
  baseline = checked_out_connections()

  async def fake_graphql(_token, _query, _variables):
    assert checked_out_connections() == baseline
    return {"pr0": {"pullRequest": _pr_node(
      rollup_state="SUCCESS",
      contexts=[],
    )}}

  monkeypatch.setattr(github_routes, "_github_graphql_json", fake_graphql)
  response = client.post(
    f"/api/github/contributions/{app_id}/refresh",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 200, response.text


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


# ── The chat review card's read endpoint ─────────────────────────────────────
# The card lets the owner approve a staged PR in the chat where the work
# happened instead of navigating to the Contribute app. It is a projection over
# the same ledger, so these tests pin what it exposes, what it filters, and that
# it stays scoped to ONE chat.

def _prepared_for_chat(app_id, record_id, chat_id, **overrides):
  repo, record, diff_text = _prepared_real_review(app_id, record_id)
  record["chat_id"] = chat_id
  record["summary"] = "A plain sentence about the improvement."
  record["plan"]["title"] = "Reviewed fix"
  record["plan"]["body_draft"] = "## Summary\n\nThe exact published text.\n"
  record["plan"]["diff_stat"] = "1 file changed, 1 insertion(+), 1 deletion(-)"
  record["plan"]["labels"] = ["bug", "area: ui"]
  record.update(overrides)
  _write_contribution(app_id, record_id, record, diff_text)
  return repo, record


def test_for_chat_returns_only_this_chat_s_prepared_reviews(client, owner_token):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _prepared_for_chat(app_id, "mine", "chat-a")
  _prepared_for_chat(app_id, "someone-elses", "chat-b")
  headers = {"Authorization": f"Bearer {owner_token}"}

  r = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  )
  assert r.status_code == 200, r.text
  body = r.json()
  assert [item["id"] for item in body["records"]] == ["mine"]
  record = body["records"][0]
  # Everything the card needs to show what would be published, and nothing that
  # would let it publish anything itself.
  assert record["title"] == "Reviewed fix"
  assert record["summary"] == "A plain sentence about the improvement."
  assert record["body_draft"] == "## Summary\n\nThe exact published text.\n"
  assert record["files"] == ["index.jsx"]
  assert record["labels"] == ["bug", "area: ui"]
  assert record["diff_stat"].startswith("1 file changed")
  assert record["review"] == {
    "id": "mine",
    "state": "ready",
    "code": "ready",
    "message": "Still matches the exact source you reviewed.",
  }
  assert "diff_sha256" not in record and "repo_path" not in record
  assert body["connected"] is True
  assert body["autopilot_available"] is True
  # No stored preference means the same default the Contribute app applies.
  assert body["autopilot_default"] is True


def test_diff_file_paths_reads_headers_not_source_that_looks_like_one(tmp_path):
  diff_path = tmp_path / "review.diff"
  diff_path.write_text(
    "diff --git a/real file.jsx b/real file.jsx\n"
    "--- a/real file.jsx\n"
    "+++ b/real file.jsx\n"
    "@@ -1 +1,2 @@\n"
    " keep\n"
    "++++ not-a-reviewed-path.jsx\n",
  )

  assert github_routes._diff_file_paths(diff_path) == ["real file.jsx"]


def test_for_chat_reports_local_drift_so_the_card_can_block_send(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  repo, _record = _prepared_for_chat(app_id, "drifted", "chat-a")
  headers = {"Authorization": f"Bearer {owner_token}"}

  (repo / "index.jsx").write_text("export default 3\n")
  r = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  )
  assert r.status_code == 200, r.text
  review = r.json()["records"][0]["review"]
  assert review["state"] == "needs_refresh"
  assert review["code"] == "working_changes"
  # Read-only: inspecting a review never commits or discards the owner's edit.
  assert (repo / "index.jsx").read_text() == "export default 3\n"


def test_for_chat_keeps_the_sent_lifecycle_with_its_source_chat(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _prepared_for_chat(app_id, "dropped", "chat-a", status="abandoned")
  _prepared_for_chat(
    app_id, "already-open", "chat-a", status="open", number=7,
    url="https://github.com/mobius-os/app-demo/pull/7",
  )
  headers = {"Authorization": f"Bearer {owner_token}"}

  r = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  )
  assert r.status_code == 200, r.text
  # Abandoned work is gone, while a sent contribution stays attached to the
  # conversation that created it. Deeper cross-chat history remains Contribute's.
  records = r.json()["records"]
  assert [item["id"] for item in records] == ["already-open"]
  assert records[0]["status"] == "open"
  assert records[0]["number"] == 7
  assert records[0]["needs_attention"] is False
  assert records[0]["review"] is None


def test_for_chat_honors_the_owner_s_autopilot_default(client, owner_token):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _prepared_for_chat(app_id, "autopilot-default", "chat-a")
  settings_path = (
    Path(get_settings().data_dir) / "apps" / str(app_id) / "settings.json"
  )
  atomic_write(settings_path, json.dumps({"autopilot_default": False}))
  headers = {"Authorization": f"Bearer {owner_token}"}

  r = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  )
  assert r.status_code == 200, r.text
  assert r.json()["autopilot_default"] is False


def test_for_chat_marks_a_stack_layer_so_chat_never_sends_one_alone(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _, record = _prepared_for_chat(app_id, "layer-2", "chat-a")
  record["plan"]["stack"] = {
    "id": "demo", "name": "Demo stack", "position": 2, "total": 3,
    "parent_record_id": "layer-1", "base_branch": "stack/demo/01",
  }
  _write_contribution(app_id, "layer-2", record, "")
  headers = {"Authorization": f"Bearer {owner_token}"}

  r = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  )
  assert r.status_code == 200, r.text
  item = r.json()["records"][0]
  assert item["is_stack"] is True
  assert item["stack"] == {
    "id": "demo", "name": "Demo stack", "position": 2, "total": 3,
  }
  assert "parent_record_id" not in item["stack"]
  assert "base_branch" not in item["stack"]
  # A stack layer is never preflighted here: the whole chain is reviewed and
  # sent together in the app, so the card must not offer a single-layer Send.
  assert item["review"] is None


def test_for_chat_requires_the_owner_or_that_app(client, owner_token):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  other_id, other_token = _app_token(client, owner_token, github_access=True)
  _prepared_for_chat(app_id, "scoped", "chat-a")

  r = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a",
    headers={"Authorization": f"Bearer {other_token}"},
  )
  assert r.status_code == 403, r.text
  assert other_id != app_id

  anon = client.get(f"/api/github/contributions/{app_id}/for-chat/chat-a")
  assert anon.status_code == 401


def test_submit_records_where_the_owner_pressed_send(
  client, owner_token, monkeypatch,
):
  """Provenance only: the ledger says which surface approved the publish."""
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _prepared_for_chat(app_id, "provenance", "chat-a")

  invalid = client.post(
    f"/api/github/contributions/{app_id}/provenance/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"autopilot": False, "submitter": "not-a-real-surface"},
  )
  # An unknown surface is rejected by the schema rather than stored.
  assert invalid.status_code == 422, invalid.text

  def fake_submit(record, _diff_path):
    assert record["submitter"] == "chat-review-card"
    return "https://github.com/mobius-os/app-demo/pull/17", 17, {}

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", fake_submit)
  submitted = client.post(
    f"/api/github/contributions/{app_id}/provenance/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"autopilot": False, "submitter": "chat-review-card"},
  )

  assert submitted.status_code == 200, submitted.text
  record_path, _ = github_routes._record_paths(app_id, "provenance")
  assert json.loads(record_path.read_text())["submitter"] == "chat-review-card"


# ── Pre-publication branch truth check ──────────────────────────────────────
# The reviewed-diff preflights prove WHAT would be sent; _existing_branch_pr
# proves WHETHER it was already sent, from GitHub itself, because the
# agent-writable ledger row can regress (2026-07-29: a merged PR's record was
# rewritten back to `prepared`; one more Send would have force-pushed the
# merged branch and opened a duplicate PR).


def _branch_pr_row(url, state, branch, owner):
  return {
    "url": url,
    "state": state,
    "headRefName": branch,
    "headRepositoryOwner": {"login": owner},
  }


def test_existing_branch_pr_classifies_states(tmp_path, monkeypatch):
  from app.routes.github import _existing_branch_pr

  rows = []

  def fake_gh(repo_path, *args, check=True):
    assert args[:2] == ("pr", "list") and "all" in args
    return _cp(json.dumps(rows))

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  call = lambda: _existing_branch_pr(
    tmp_path, "mobius-os/app-demo", "octocat", "fix/x",
  )

  # Closed-without-merging stays sendable: rework-and-resend is legitimate.
  rows = [_branch_pr_row("https://github.com/mobius-os/app-demo/pull/1",
                         "CLOSED", "fix/x", "octocat")]
  assert call() is None

  # A different owner's or branch's PR never blocks this send.
  rows = [
    _branch_pr_row("https://github.com/mobius-os/app-demo/pull/2",
                   "OPEN", "fix/x", "someone-else"),
    _branch_pr_row("https://github.com/mobius-os/app-demo/pull/3",
                   "OPEN", "fix/other", "octocat"),
  ]
  assert call() is None

  rows = [_branch_pr_row("https://github.com/mobius-os/app-demo/pull/4",
                         "MERGED", "fix/x", "octocat")]
  assert call() == ("https://github.com/mobius-os/app-demo/pull/4", "merged")

  # An open PR outranks a merged one: the message should name the row the
  # send would collide with first.
  rows = [
    _branch_pr_row("https://github.com/mobius-os/app-demo/pull/4",
                   "MERGED", "fix/x", "octocat"),
    _branch_pr_row("https://github.com/mobius-os/app-demo/pull/5",
                   "OPEN", "fix/x", "octocat"),
  ]
  assert call() == ("https://github.com/mobius-os/app-demo/pull/5", "open")


def test_existing_branch_pr_fails_closed_when_lookup_fails(
  tmp_path, monkeypatch,
):
  from app.routes.github import ContributionSubmitError, _existing_branch_pr

  # The lookup failing must STOP the send, not let it proceed blind: the
  # whole point is refusing to trust local state about public reality.
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda repo_path, *args, check=True: _cp("boom", returncode=1),
  )
  with pytest.raises(ContributionSubmitError) as err:
    _existing_branch_pr(tmp_path, "mobius-os/app-demo", "octocat", "fix/x")
  assert "Nothing was pushed" in err.value.message

  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda repo_path, *args, check=True: _cp("not-json"),
  )
  with pytest.raises(ContributionSubmitError):
    _existing_branch_pr(tmp_path, "mobius-os/app-demo", "octocat", "fix/x")


def test_send_refuses_branch_with_existing_pr_before_any_push(
  tmp_path, monkeypatch,
):
  from app.routes.github import ContributionSubmitError, _submit_prepared_pr

  _write_token(login="octocat")
  record_id = "already-sent-guard"
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
  monkeypatch.setattr("app.github_contributions.shutil.which", lambda name: f"/bin/{name}")
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
    if args[:2] == ("pr", "list") and "all" in args:
      # GitHub's truth: this exact branch was already sent and merged.
      return _cp(json.dumps([_branch_pr_row(
        "https://github.com/mobius-os/app-demo/pull/61",
        "MERGED", branch, "mobius-os",
      )]))
    if args[:2] == ("pr", "list"):
      return _cp("[]")
    if args[:2] == ("pr", "create"):
      raise AssertionError("a duplicate send must never reach pr create")
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)

  with pytest.raises(ContributionSubmitError) as err:
    _submit_prepared_pr(record, diff_path, direct_base_branch="main")

  # The refusal names the public truth…
  assert "pull/61" in err.value.message
  assert "merged" in err.value.message
  assert "Nothing was pushed" in err.value.message
  # …and, unlike the pre-guard flow, nothing public was touched: no git push
  # of any kind and no PR creation.
  assert not any("push" in call for call in git_calls)
  assert not any(call[:2] == ("pr", "create") for call in gh_calls)


def _conflicting_upstream_commit(repo, content):
  """Commit a rival change to main, and return its sha.

  The review branch already edited this file, so a different edit to the same
  line is a genuine merge conflict rather than a simulated one.
  """
  subprocess.run(["git", "checkout", "main"], cwd=repo, check=True,
                 capture_output=True)
  (repo / "index.jsx").write_text(content)
  subprocess.run(["git", "add", "index.jsx"], cwd=repo, check=True)
  subprocess.run(["git", "commit", "-m", "upstream moved"], cwd=repo,
                 check=True, capture_output=True)
  sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo,
                                text=True).strip()
  subprocess.run(["git", "checkout", "fix/demo-review"], cwd=repo, check=True,
                 capture_output=True)
  return sha


def _record_upstream(app_id, record_id, sha):
  path = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "contributions" / f"{record_id}.json"
  )
  stored = json.loads(path.read_text())
  stored["last_submit_upstream_sha"] = sha
  path.write_text(json.dumps(stored))


def test_review_status_reports_a_branch_that_no_longer_merges(
  client, owner_token,
):
  """The one verdict local freshness checks cannot reach.

  A conflict is a fact about upstream, so every check about the staged
  checkout still passes and the review would otherwise read "ready".
  """
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  repo, _record, _diff = _prepared_real_review(app_id, "conflicted")
  headers = {"Authorization": f"Bearer {app_token}"}

  assert client.get(
    f"/api/github/contributions/{app_id}/review-status", headers=headers,
  ).json()["records"][0]["state"] == "ready"

  _record_upstream(
    app_id, "conflicted", _conflicting_upstream_commit(repo, "export default 9\n"),
  )

  blocked = client.get(
    f"/api/github/contributions/{app_id}/review-status", headers=headers,
  )
  assert blocked.status_code == 200, blocked.text
  assert blocked.json()["needs_refresh"] == 1
  verdict = blocked.json()["records"][0]
  assert verdict["code"] == "upstream_conflict"
  assert verdict["message"] == (
    github_routes._REVIEW_STATUS_MESSAGES["upstream_conflict"]
  )


def test_refreshing_the_branch_clears_the_conflict_with_nothing_to_reset(
  client, owner_token,
):
  """The property a stored verdict could not have.

  Nothing records that this review was conflicted, so nothing has to remember
  to clear it: merging upstream in is enough, and the next read is honest.
  """
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  repo, _record, _diff = _prepared_real_review(app_id, "healing")
  headers = {"Authorization": f"Bearer {app_token}"}
  upstream = _conflicting_upstream_commit(repo, "export default 9\n")
  _record_upstream(app_id, "healing", upstream)

  assert client.get(
    f"/api/github/contributions/{app_id}/review-status", headers=headers,
  ).json()["records"][0]["code"] == "upstream_conflict"

  # Resolve it exactly as a refresh would, then re-stage the reviewed source.
  subprocess.run(["git", "merge", upstream, "-m", "merge upstream"], cwd=repo,
                 check=False, capture_output=True)
  (repo / "index.jsx").write_text("export default 2\n")
  subprocess.run(["git", "add", "index.jsx"], cwd=repo, check=True)
  subprocess.run([
    "git", "commit", "--no-edit", "-m", "refreshed", "-m",
    "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>",
  ], cwd=repo, check=False, capture_output=True)
  head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo,
                                 text=True).strip()
  path = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "contributions" / "healing.json"
  )
  stored = json.loads(path.read_text())
  base = stored["plan"]["base_sha"]
  diff_text = subprocess.check_output([
    "git", "-c", "core.quotePath=false", "diff", "--no-ext-diff", "--no-color",
    "--binary", "--full-index", "--src-prefix=a/", "--dst-prefix=b/",
    f"{base}..{head}",
  ], cwd=repo, text=True)
  stored["plan"]["head_sha"] = head
  stored["plan"]["diff_sha256"] = hashlib.sha256(diff_text.encode()).hexdigest()
  _write_contribution(app_id, "healing", stored, diff_text)

  healed = client.get(
    f"/api/github/contributions/{app_id}/review-status", headers=headers,
  )
  assert healed.json()["records"][0]["state"] == "ready", healed.text


def test_a_review_that_never_reached_upstream_is_not_inspected_for_conflicts(
  client, owner_token,
):
  """No recorded upstream means nothing local to compare against."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _prepared_real_review(app_id, "never-sent")

  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.json()["records"][0]["state"] == "ready"


def test_a_dirty_checkout_is_named_before_an_upstream_conflict(
  client, owner_token,
):
  """Two things wrong at once: say the one the owner can act on locally."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  repo, _record, _diff = _prepared_real_review(app_id, "both-wrong")
  _record_upstream(
    app_id, "both-wrong", _conflicting_upstream_commit(repo, "export default 9\n"),
  )
  (repo / "index.jsx").write_text("export default 77\n")

  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.json()["records"][0]["code"] == "working_changes"
