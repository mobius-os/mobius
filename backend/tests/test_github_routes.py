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
from datetime import timedelta
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import Response

from app import app_git, github_auth, github_contributions, models, source_status
from app import auth as auth_mod
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


@pytest.fixture(autouse=True)
def _prepared_review_publication_unavailable(monkeypatch):
  """Keep legacy local-review tests offline unless they assert remote truth.

  The checks-refresh query retains its existing mocked HTTP path; only the new
  repository-head document gets the graceful unavailable result by default.
  Focused publication tests replace this wrapper with exact nodes below.
  """
  original = github_routes._github_graphql_json

  async def offline_repository_heads(token, query, variables):
    if "fragment repositoryHead" in query:
      return None
    return await original(token, query, variables)

  monkeypatch.setattr(
    github_routes, "_github_graphql_json", offline_repository_heads,
  )


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
  # workflow is not opt-in: every device-flow connect must clear
  # has_full_pr_access() (which requires it), so the default request includes it.
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
    "/api/github/connect/start", headers=auth, json={},
  )

  assert r.status_code == 200, r.text
  assert seen["scope"] == ["public_repo workflow"]
  assert r.json()["requested_scopes"] == ["public_repo", "workflow"]


def test_connect_start_private_opt_in_requests_repo_scope(
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
    "/api/github/connect/start", headers=auth, json={"private_repos": True},
  )

  assert r.status_code == 200, r.text
  # `repo` is a strict superset of `public_repo`, so the private opt-in requests
  # it alone alongside workflow.
  assert seen["scope"] == ["repo workflow"]
  assert r.json()["requested_scopes"] == ["repo", "workflow"]


@pytest.mark.parametrize(
  ("scopes", "expected"),
  [
    (("repo", "workflow"), True),
    (("repo",), True),
    (("public_repo", "workflow"), False),
    (("public_repo",), False),
    ((), False),
  ],
)
def test_private_repo_access_requires_repo_scope(scopes, expected):
  assert github_routes.has_private_repo_access(scopes) is expected


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


def test_source_diff_is_project_bound_bounded_and_stale_safe(
  client, owner_token,
):
  from app import models
  from app.database import SessionLocal

  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  session = SessionLocal()
  try:
    row = session.query(models.App).filter(models.App.id == app_id).one()
    source = Path(row.source_dir)
  finally:
    session.close()
  head = subprocess.run(
    ["git", "-C", str(source), "rev-parse", "HEAD"],
    check=True, capture_output=True, text=True,
  ).stdout.strip()
  comparison = subprocess.run(
    ["git", "-C", str(source), "rev-parse", "upstream"],
    check=True, capture_output=True, text=True,
  ).stdout.strip()
  (source / "new-source.js").write_text(
    "export const reviewed = true\n", encoding="utf-8",
  )
  headers = {"Authorization": f"Bearer {app_token}"}

  response = client.get(
    "/api/github/source-diff",
    params={
      "project": f"app:{app_id}",
      "head": head,
      "comparison": comparison,
    },
    headers=headers,
  )

  assert response.status_code == 200, response.text
  body = response.json()
  assert body["project"] == f"app:{app_id}"
  assert body["head_sha"] == head
  assert "diff --git a/new-source.js b/new-source.js" in body["diff"]
  assert "+export const reviewed = true" in body["diff"]
  assert str(source) not in response.text

  stale = client.get(
    "/api/github/source-diff",
    params={
      "project": f"app:{app_id}",
      "head": "0" * 40,
      "comparison": comparison,
    },
    headers=headers,
  )
  assert stale.status_code == 409
  assert stale.json()["detail"]["code"] == "source_snapshot_changed"


def test_source_diff_requires_github_access_for_app_tokens(
  client, owner_token,
):
  _, denied_token = _app_token(client, owner_token, github_access=False)
  denied = client.get(
    "/api/github/source-diff",
    params={"project": "platform", "head": "0" * 40},
    headers={"Authorization": f"Bearer {denied_token}"},
  )
  assert denied.status_code == 403


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


@pytest.mark.parametrize("landing", ["fast_forward", "merge_commit"])
def test_connect_published_app_reuses_reviewed_local_row_idempotently(
  client, owner_token, monkeypatch, landing,
):
  """The after-merge action links identity without replacing app data.

  Installs record GitHub's merge commit, which only equals the reviewed head
  for a fast-forward landing. A retry after a real merge commit must still
  take the full proof path so a pending conflict receipt is honoured.
  """
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
  if landing == "merge_commit":
    reviewed_tree = subprocess.run(
      ["git", "-C", str(source), "rev-parse", f"{reviewed}^{{tree}}"],
      check=True, capture_output=True, text=True,
    ).stdout.strip()
    landed = subprocess.run(
      [
        "git", "-C", str(source),
        "-c", f"user.name={app_git._GIT_NAME}",
        "-c", f"user.email={app_git._GIT_EMAIL}",
        "commit-tree", reviewed_tree, "-p", reviewed,
        "-m", "Merge pull request #1",
      ],
      check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert landed != reviewed
  else:
    landed = reviewed
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
    source, diff_digest, upstream_sha=landed,
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
      f"{landed}/"
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
    github_routes, "_merged_upstream_sha", lambda *_args: landed,
  )
  monkeypatch.setattr(app_git, "fetch_origin_commit", lambda *_args: landed)
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
      f"{landed}#manifest-id=connect"
    )
    row.version = "0.1.0"
    row.capability_contract = contract
    row.upstream_commit = landed
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
    upstream_commit=landed,
    manifest=manifest,
    raw_base=candidate.raw_base,
    capability_digest=capability_digest,
    candidate_digest=candidate.candidate_digest,
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
  # The live receipt format proves a conflict is pending but does not persist
  # path details; those remain available in the resolver UI itself.
  assert recovered.json()["connection"]["conflict_paths"] == []
  assert installs == 1

  install.stage_pending_conflict_update(
    source,
    app_id=target["id"],
    upstream_commit=landed,
    manifest=manifest,
    raw_base=candidate.raw_base,
    capability_digest=capability_digest,
    candidate_digest="c" * 64,
  )
  interrupted = json.loads(record_path.read_text(encoding="utf-8"))
  interrupted.pop("publication_connection", None)
  atomic_write(record_path, json.dumps(interrupted))

  rejected = client.post(
    f"/api/github/contributions/{contribute_id}/publish-connect/connect-app",
    headers=app_auth,
  )
  assert rejected.status_code == 409, rejected.text
  assert rejected.json()["detail"] == (
    "The installed app no longer matches this reviewed publication."
  )
  assert installs == 1
  assert "publication_connection" not in json.loads(record_path.read_text())

  pending_receipt = (
    source / ".git" / "mobius-pending-update" / "receipt.json"
  )
  atomic_write(pending_receipt, "{invalid receipt\n")
  invalid_receipt = client.post(
    f"/api/github/contributions/{contribute_id}/publish-connect/connect-app",
    headers=app_auth,
  )
  assert invalid_receipt.status_code == 409, invalid_receipt.text
  assert invalid_receipt.json()["detail"] == (
    "The installed app has an invalid pending update receipt."
  )
  assert installs == 1
  assert "publication_connection" not in json.loads(record_path.read_text())

  session = SessionLocal()
  try:
    row = session.query(models.App).filter(models.App.id == target["id"]).one()
    assert row.slug == "connect"
    assert row.manifest_url.endswith(f"{landed}#manifest-id=connect")
  finally:
    session.close()


def _already_linked_publication(client, owner_token, record_id):
  """A canonical app installed at a newer commit plus its stale merged record.

  The record deliberately omits the old Git/package proof: the installation
  already owns the exact canonical identity, so only the ledger is stale.
  """
  from app import models
  from app.database import SessionLocal

  contribute_id, contribute_token = _app_token(
    client, owner_token, github_access=True,
  )
  target = create_local_app(
    client,
    {"Authorization": f"Bearer {owner_token}"},
    name="Already linked",
    source_dir=Path(get_settings().data_dir) / "apps" / "already-linked",
    manifest_extra={"id": "already-linked"},
  )
  canonical = (
    "https://raw.githubusercontent.com/mobius-os/"
    "app-already-linked/main#manifest-id=already-linked"
  )
  session = SessionLocal()
  try:
    session.query(models.App).filter(models.App.id == target["id"]).update({
      "manifest_url": canonical,
      "version": "2.0.0",
      "upstream_commit": "b" * 40,
    })
    session.commit()
  finally:
    session.close()

  record_path = (
    Path(get_settings().data_dir) / "apps" / str(contribute_id)
    / "contributions" / f"{record_id}.json"
  )
  record_path.parent.mkdir(parents=True, exist_ok=True)
  atomic_write(record_path, json.dumps({
    "id": record_id,
    "type": "pr",
    "status": "merged",
    "repo": "mobius-os/app-already-linked",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-already-linked",
      "after_merge": {
        "action": "connect_app",
        "app_id": target["id"],
        "manifest_url": (
          "https://raw.githubusercontent.com/mobius-os/"
          "app-already-linked/main/mobius.json"
        ),
      },
    },
  }))
  return contribute_id, contribute_token, target, record_path, canonical


def _assert_settled_connection(response, record_path, target, canonical):
  assert response.status_code == 200, response.text
  connection = response.json()["connection"]
  assert connection == {
    "status": "connected",
    "app_id": target["id"],
    "manifest_url": canonical,
    "version": "2.0.0",
    "connected_at": connection["connected_at"],
    "conflict_paths": [],
  }
  stored = json.loads(record_path.read_text(encoding="utf-8"))
  assert stored["publication_connection"] == connection


def test_connect_published_app_settles_a_proof_less_record_via_identity(
  client, owner_token, monkeypatch,
):
  """A record without reviewed proof settles through its ledger identity alone."""
  from app import install

  record_id = "already-linked-identity"
  contribute_id, contribute_token, target, record_path, canonical = (
    _already_linked_publication(client, owner_token, record_id)
  )

  async def must_not_fetch(_url):
    pytest.fail("an already-linked newer app replayed its historical package")

  def must_not_lookup(*_args):
    pytest.fail("identity-only settlement asked GitHub for a merge commit")

  monkeypatch.setattr(install, "fetch_install_candidate", must_not_fetch)
  monkeypatch.setattr(github_routes, "_merged_upstream_sha", must_not_lookup)

  response = client.post(
    f"/api/github/contributions/{contribute_id}/{record_id}/connect-app",
    headers={"Authorization": f"Bearer {contribute_token}"},
  )

  _assert_settled_connection(response, record_path, target, canonical)


def test_connect_published_app_settles_a_superseded_reviewed_package(
  client, owner_token, monkeypatch,
):
  """A complete proof whose merge commit the app has moved past settles too."""
  from app import install

  record_id = "already-linked-superseded"
  contribute_id, contribute_token, target, record_path, canonical = (
    _already_linked_publication(client, owner_token, record_id)
  )
  spec = github_contributions.PublicationHandoffSpec(
    contribution_id=record_id,
    target_app_id=target["id"],
    repo_slug="mobius-os/app-already-linked",
    manifest_url=(
      "https://raw.githubusercontent.com/mobius-os/"
      "app-already-linked/main/mobius.json"
    ),
    manifest_id="already-linked",
    source_repo=Path(target["source_dir"]),
    reviewed_base_sha="9" * 40,
    reviewed_head_sha="a" * 40,
    reviewed_source_sha="a" * 40,
    diff_sha256="1" * 64,
    package_digest="2" * 64,
    capability_digest="3" * 64,
  )
  monkeypatch.setattr(
    github_routes, "publication_handoff_spec", lambda _record, _db: spec,
  )
  monkeypatch.setattr(
    github_routes, "_merged_upstream_sha", lambda *_args: "c" * 40,
  )

  async def must_not_fetch(_url):
    pytest.fail("an already-linked newer app replayed its historical package")

  monkeypatch.setattr(install, "fetch_install_candidate", must_not_fetch)

  response = client.post(
    f"/api/github/contributions/{contribute_id}/{record_id}/connect-app",
    headers={"Authorization": f"Bearer {contribute_token}"},
  )

  _assert_settled_connection(response, record_path, target, canonical)


def test_connect_published_app_rejects_an_unsigned_bot_merge_commit(
  client, owner_token,
):
  contribute_id, contribute_token = _app_token(
    client, owner_token, github_access=True,
  )
  record_id = "publish-forged-bot-merge"
  record_path = (
    Path(get_settings().data_dir) / "apps" / str(contribute_id)
    / "contributions" / f"{record_id}.json"
  )
  record_path.parent.mkdir(parents=True, exist_ok=True)
  atomic_write(record_path, json.dumps({
    "id": record_id,
    "type": "pr",
    "status": "merged",
    "submission_mode": "mobius-bot",
    "merge_commit_sha": "a" * 40,
  }))

  response = client.post(
    f"/api/github/contributions/{contribute_id}/{record_id}/connect-app",
    headers={"Authorization": f"Bearer {contribute_token}"},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"].startswith(
    "The saved Möbius relay merge result is invalid."
  )


def test_relay_merge_authority_uses_only_the_signed_result(monkeypatch):
  from app.routes import contribution_relay

  app_id = 17
  record_id = "publish-signed-bot-merge"
  merge_sha = "a" * 40
  record = {
    "id": record_id,
    "type": "pr",
    "submission_mode": "mobius-bot",
    "public_identity": "anonymous",
    "relay_contribution_id": "ctr_1234567890abcdef1234567890abcdef",
    "relay_revision": 2,
    "relay_request_sha256": "b" * 64,
    "relay_payload_sha256": "c" * 64,
    "relay_idempotency_key": contribution_relay._idempotency_key(
      app_id, record_id, 2,
    ),
    "merge_commit_sha": merge_sha,
    "relay_terminal_status": "merged",
    "last_land_head_sha": "d" * 40,
    "checks": {"merge_commit_sha": "e" * 40},
  }
  record["relay_attempt_input_sha256"] = (
    contribution_relay._relay_input_fingerprint(record)
  )
  record["relay_owner_claim_sha256"] = (
    contribution_relay._owner_claim_witness(
      app_id, record_id, record["relay_attempt_input_sha256"],
    )
  )
  record["relay_attempt_witness_sha256"] = contribution_relay._attempt_witness(
    app_id, record_id, record,
  )
  record["relay_result_witness_sha256"] = contribution_relay._result_witness(
    app_id, record_id, record,
  )

  assert github_routes._relay_merge_authority(
    app_id, record_id, record,
  ) == (True, "merged", merge_sha)
  assert github_routes._relay_merge_authority(
    app_id, record_id, {
      **record,
      "status": "closed",
      "relay_status": "closed",
    },
  ) == (True, "merged", merge_sha)

  for changed in (
    {**record, "submission_mode": "github"},
    {**record, "plan": {"repo": "mobius-os/changed"}},
  ):
    with pytest.raises(HTTPException) as exc_info:
      github_routes._relay_merge_authority(app_id, record_id, changed)
    assert exc_info.value.status_code == 409

  without_result_id = {**record}
  without_result_id.pop("relay_contribution_id")
  with pytest.raises(HTTPException) as exc_info:
    github_routes._relay_merge_authority(
      app_id, record_id, without_result_id,
    )
  assert exc_info.value.status_code == 409

  downgraded = {
    key: value for key, value in record.items()
    if not key.startswith("relay_")
  }
  downgraded.pop("submission_mode")
  downgraded.pop("public_identity")
  assert github_routes._relay_merge_authority(
    app_id, record_id, downgraded,
  ) == (False, None, None)

  def authoritative_lookup(candidate, _repo):
    assert candidate["last_land_head_sha"] is None
    assert candidate["merge_commit_sha"] is None
    assert candidate["checks"]["merge_commit_sha"] is None
    return "f" * 40

  monkeypatch.setattr(
    github_routes, "_merged_upstream_sha", authoritative_lookup,
  )
  assert github_routes._personal_merge_commit(
    downgraded, Path("/tmp/review"),
  ) == "f" * 40


@pytest.mark.parametrize(
  "mismatch",
  ["manifest_id", "package_digest", "capability_digest"],
)
def test_connect_published_app_rejects_package_mismatch_before_activation(
  client, owner_token, monkeypatch, mismatch,
):
  """Every digest-bound package gate fails before source or row mutation."""
  from app import install, models
  from app.app_capabilities import contract_and_digest
  from app.database import SessionLocal

  contribute_id, contribute_token = _app_token(
    client, owner_token, github_access=True,
  )
  auth = {"Authorization": f"Bearer {owner_token}"}
  target = create_local_app(
    client,
    auth,
    name="Mismatch",
    source_dir=Path(get_settings().data_dir) / "apps" / "mismatch",
    manifest_extra={"id": "mismatch"},
  )
  record_id = f"publish-mismatch-{mismatch}"
  record_path = (
    Path(get_settings().data_dir) / "apps" / str(contribute_id)
    / "contributions" / f"{record_id}.json"
  )
  record_path.parent.mkdir(parents=True, exist_ok=True)
  atomic_write(record_path, json.dumps({"id": record_id}))

  manifest = {
    "id": "mismatch",
    "name": "Mismatch",
    "version": "1.0.0",
    "entry": "index.jsx",
  }
  contract, capability_digest = contract_and_digest(manifest)
  candidate = install.InstallCandidate(
    manifest=manifest,
    raw_base="https://raw.githubusercontent.com/mobius-os/app-mismatch/abc/",
    entry_bytes=b"export default function App(){return null}\n",
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
  package_digest = install.install_candidate_content_digest(candidate)
  expected = {
    "manifest_id": "mismatch",
    "package_digest": package_digest,
    "capability_digest": capability_digest,
  }
  expected[mismatch] = "0" * 64 if mismatch != "manifest_id" else "other"
  spec = github_contributions.PublicationHandoffSpec(
    contribution_id=record_id,
    target_app_id=target["id"],
    repo_slug="mobius-os/app-mismatch",
    manifest_url=(
      "https://raw.githubusercontent.com/mobius-os/"
      "app-mismatch/main/mobius.json"
    ),
    source_repo=Path(target["source_dir"]),
    reviewed_base_sha="b" * 40,
    reviewed_head_sha="a" * 40,
    reviewed_source_sha="a" * 40,
    diff_sha256="1" * 64,
    **expected,
  )
  monkeypatch.setattr(
    github_routes, "publication_handoff_spec", lambda _record, _db: spec,
  )
  monkeypatch.setattr(
    github_routes, "_merged_upstream_sha", lambda *_args: "a" * 40,
  )

  async def fetched(_url):
    return candidate

  async def must_not_install(*_args, **_kwargs):
    pytest.fail("mismatched package reached installation")

  monkeypatch.setattr(install, "fetch_install_candidate", fetched)
  monkeypatch.setattr(install, "install_from_manifest", must_not_install)
  broadcasts = []
  monkeypatch.setattr(
    github_routes,
    "get_system_broadcast",
    lambda: SimpleNamespace(publish=broadcasts.append),
  )
  session = SessionLocal()
  try:
    before = session.query(models.App).filter(
      models.App.id == target["id"],
    ).one()
    original = (
      before.manifest_url,
      before.version,
      json.loads(json.dumps(before.capability_contract)),
    )
  finally:
    session.close()

  response = client.post(
    f"/api/github/contributions/{contribute_id}/{record_id}/connect-app",
    headers={"Authorization": f"Bearer {contribute_token}"},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"] == (
    "The merged app package does not match the exact revision you reviewed."
  )
  assert broadcasts == []
  session = SessionLocal()
  try:
    row = session.query(models.App).filter(models.App.id == target["id"]).one()
    assert (row.manifest_url, row.version, row.capability_contract) == original
  finally:
    session.close()


@pytest.mark.parametrize(
  ("drift_field", "drift_value"),
  [
    ("target_app_id", 999_999),
    ("source_repo", Path("/tmp/changed-reviewed-source")),
    ("reviewed_head_sha", "c" * 40),
    ("diff_sha256", "d" * 64),
    ("package_digest", "e" * 64),
    ("capability_digest", "f" * 64),
  ],
)
def test_connect_published_app_rejects_full_handoff_drift_after_install(
  client, owner_token, monkeypatch, drift_field, drift_value,
):
  """An installed package cannot be attached to a concurrently changed review."""
  from app import app_git, install, models
  from app.app_capabilities import capability_digest

  contribute_id, contribute_token = _app_token(
    client, owner_token, github_access=True,
  )
  target = create_local_app(
    client,
    {"Authorization": f"Bearer {owner_token}"},
    name="Concurrent publication",
    source_dir=(
      Path(get_settings().data_dir) / "apps" / "concurrent-publication"
    ),
  )
  record_id = f"publication-drift-{drift_field.replace('_', '-')}"
  record_path = (
    Path(get_settings().data_dir) / "apps" / str(contribute_id)
    / "contributions" / f"{record_id}.json"
  )
  record_path.parent.mkdir(parents=True, exist_ok=True)
  atomic_write(record_path, json.dumps({"id": record_id, "version": 1}))

  reviewed_head = "a" * 40
  installed_contract = {"reviewed_publication": True}
  original_spec = github_contributions.PublicationHandoffSpec(
    contribution_id=record_id,
    target_app_id=target["id"],
    source_repo=Path(target["source_dir"]),
    repo_slug="mobius-os/app-concurrent-publication",
    manifest_url=(
      "https://raw.githubusercontent.com/mobius-os/"
      "app-concurrent-publication/main/mobius.json"
    ),
    manifest_id="concurrent-publication",
    reviewed_base_sha="b" * 40,
    reviewed_head_sha=reviewed_head,
    reviewed_source_sha=reviewed_head,
    diff_sha256="1" * 64,
    package_digest="2" * 64,
    capability_digest=capability_digest(installed_contract),
  )
  spec_reads = []

  def handoff_spec(current_record, _db):
    spec_reads.append(dict(current_record))
    if current_record.get("concurrent_drift"):
      return replace(original_spec, **{drift_field: drift_value})
    return original_spec

  monkeypatch.setattr(github_routes, "publication_handoff_spec", handoff_spec)
  def merged_upstream(current_record, _repo):
    if current_record.get("concurrent_drift") == "reviewed_head_sha":
      return drift_value
    return reviewed_head

  monkeypatch.setattr(github_routes, "_merged_upstream_sha", merged_upstream)
  monkeypatch.setattr(app_git, "fetch_origin_commit", lambda *_args: reviewed_head)
  def verify_landed(repo, **kwargs):
    expected = original_spec
    if drift_field == "reviewed_head_sha" and json.loads(
      record_path.read_text(),
    ).get("concurrent_drift"):
      expected = replace(original_spec, reviewed_head_sha=drift_value)
    return (
      Path(repo) == expected.source_repo
      and kwargs == {
        "diff_sha256": expected.diff_sha256,
        "contribution_id": expected.contribution_id,
        "base_sha": expected.reviewed_base_sha,
        "head_sha": expected.reviewed_head_sha,
        "source_sha": expected.reviewed_source_sha,
        "upstream_sha": (
          drift_value if expected is not original_spec else reviewed_head
        ),
      }
    )

  monkeypatch.setattr(
    app_git, "verify_landed_equivalent_change", verify_landed,
  )
  candidate = SimpleNamespace(
    manifest={"id": original_spec.manifest_id},
    capability_digest=original_spec.capability_digest,
    raw_base=(
      "https://raw.githubusercontent.com/mobius-os/"
      f"app-concurrent-publication/{reviewed_head}/"
    ),
    candidate_digest="4" * 64,
  )

  async def fetch_candidate(_url):
    return candidate

  monkeypatch.setattr(install, "fetch_install_candidate", fetch_candidate)
  monkeypatch.setattr(
    install,
    "install_candidate_content_digest",
    lambda _candidate: original_spec.package_digest,
  )
  installs = []

  async def install_and_drift(db, **_kwargs):
    installs.append(True)
    app = db.query(models.App).filter(models.App.id == target["id"]).one()
    app.manifest_url = (
      f"{original_spec.manifest_url}#manifest-id={original_spec.manifest_id}"
    )
    app.upstream_commit = reviewed_head
    app.capability_contract = installed_contract
    db.commit()
    db.refresh(app)
    atomic_write(record_path, json.dumps({
      "id": record_id,
      "version": 2,
      "concurrent_drift": drift_field,
    }))
    return SimpleNamespace(
      app=app,
      mode="update",
      conflict_paths=[],
    )

  monkeypatch.setattr(install, "install_from_manifest", install_and_drift)

  response = client.post(
    f"/api/github/contributions/{contribute_id}/{record_id}/connect-app",
    headers={"Authorization": f"Bearer {contribute_token}"},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"] == (
    "This contribution changed while the app was being connected."
  )
  assert installs == [True]
  assert len(spec_reads) == 2
  stored = json.loads(record_path.read_text())
  assert stored["id"] == record_id
  assert stored["concurrent_drift"] == drift_field
  assert "publication_connection" not in stored

  retry = client.post(
    f"/api/github/contributions/{contribute_id}/{record_id}/connect-app",
    headers={"Authorization": f"Bearer {contribute_token}"},
  )

  assert retry.status_code == 409, retry.text
  assert installs == [True]
  assert len(spec_reads) >= 3
  retried = json.loads(record_path.read_text())
  assert retried["concurrent_drift"] == drift_field
  assert "publication_connection" not in retried


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
  # Route tests historically modelled every `prepared` PR as already reviewed.
  # Keep that intent explicit now that publication requires a verdict pinned to
  # the immutable prepared head; gate-specific tests override this field.
  head_sha = str((record.get("plan") or {}).get("head_sha") or "")
  if (
    record.get("type") == "pr"
    and record.get("status") == "prepared"
    and re.fullmatch(r"[0-9a-f]{40}", head_sha)
  ):
    record.setdefault("quality_review", {
      "state": "all_clear",
      "reviewed_head_sha": head_sha,
      "reviewed_at": "2026-07-09T00:00:00Z",
    })
  base = Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
  base.mkdir(parents=True, exist_ok=True)
  atomic_write(base / f"{record_id}.json", json.dumps(record))
  if diff_text:
    atomic_write(base / f"{record_id}.diff", diff_text)


def _allow_synthetic_source_provenance(monkeypatch) -> None:
  """Let downstream publication tests keep their deliberately fake repos."""
  for module in (github_routes, github_contributions):
    monkeypatch.setattr(
      module,
      "_assert_pending_equivalence_preflight",
      lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
      module,
      "_assert_pending_equivalence_before_publication",
      lambda *_args, **_kwargs: None,
    )
  # Synthetic GitHub fixtures predate the authoritative branch-lease read and
  # intentionally model an absent unpublished topic branch.
  monkeypatch.setattr(
    "app.github_contribution_git._authoritative_upstream_branch_sha",
    lambda *_args, **_kwargs: None,
  )


def _all_clear_review(head_sha: str) -> dict:
  """The exact-head review precondition for tests exercising public work."""
  return {
    "state": "all_clear",
    "reviewed_head_sha": head_sha,
    "reviewed_at": "2026-08-30T00:00:00Z",
  }


def _write_personal_draft(app_id, record_id="personal-draft", number=58):
  head = "a" * 40
  base = "b" * 40
  repo_path = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
  (repo_path / ".git").mkdir(parents=True)
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "draft",
    "publication_stage": "draft",
    "number": number,
    "url": f"https://github.com/mobius-os/app-demo/pull/{number}",
    "branch": "feat/existing-review",
    "head_repository": "octocat/app-demo",
    "last_submit_push_sha": head,
    "last_submit_upstream_branch": "main",
    "submitted_at": "2026-08-29T08:00:00Z",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "repo_path": str(repo_path),
      "branch": "feat/existing-review",
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": hashlib.sha256(b"reviewed").hexdigest(),
    },
    "quality_review": {
      "state": "all_clear",
      "reviewed_head_sha": head,
      "reviewed_at": "2026-08-29T07:59:00Z",
    },
  }
  _write_contribution(app_id, record_id, record)
  return record


def _personal_pr_live(record, *, draft, head_sha=None, auto_merge=None):
  return {
    "node_id": "PR_kwDO_ready_58",
    "html_url": record["url"],
    "state": "open",
    "draft": draft,
    "auto_merge": auto_merge,
    "head": {
      "ref": record["plan"]["branch"],
      "sha": head_sha or record["last_submit_push_sha"],
      "repo": {"full_name": record["head_repository"]},
    },
    "base": {
      "ref": record["last_submit_upstream_branch"],
      "repo": {"full_name": record["repo"]},
    },
  }


@pytest.mark.asyncio
async def test_ready_action_lock_serializes_one_record():
  first = github_routes._serialize_ready_action(71, "same-record")
  second = github_routes._serialize_ready_action(71, "same-record")
  await first.__anext__()
  waiting = asyncio.create_task(second.__anext__())
  await asyncio.sleep(0)
  assert not waiting.done()

  await first.aclose()
  await asyncio.wait_for(waiting, timeout=1)
  await second.aclose()


def test_publication_requires_all_clear_review_on_exact_head():
  from app.github_contributions import _require_all_clear_review

  base = "b" * 40
  head = "a" * 40
  record = {"plan": {"base_sha": base, "head_sha": head}}
  with pytest.raises(HTTPException, match="complete agent review"):
    _require_all_clear_review(record)
  record["quality_review"] = {
    "state": "all_clear", "reviewed_head_sha": "b" * 40,
  }
  with pytest.raises(HTTPException, match="complete agent review"):
    _require_all_clear_review(record)
  record["quality_review"]["reviewed_head_sha"] = head
  _require_all_clear_review(record)


def test_mark_ready_mutates_once_after_exact_live_identity(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record = _write_personal_draft(app_id)
  monkeypatch.setattr(
    "app.github_contributions.shutil.which", lambda name: f"/bin/{name}",
  )
  calls = []
  reads = 0

  def fake_gh(_repo_path, *args, check=True):
    nonlocal reads
    calls.append(args)
    if args[:2] == ("api", f"repos/{record['repo']}/pulls/{record['number']}"):
      reads += 1
      return _cp(json.dumps(_personal_pr_live(record, draft=reads == 1)))
    if args[:2] == ("api", "graphql"):
      return _cp(json.dumps({"data": {
        "markPullRequestReadyForReview": {"pullRequest": {
          "id": "PR_kwDO_ready_58",
          "isDraft": False,
          "headRefOid": record["last_submit_push_sha"],
          "url": record["url"],
        }},
      }}))
    pytest.fail(f"unexpected gh call: {args}")

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/ready",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"expected_head_sha": record["last_submit_push_sha"]},
  )

  assert response.status_code == 200, response.text
  ready = response.json()["record"]
  assert ready["status"] == "open"
  assert ready["publication_stage"] == "ready"
  assert ready["last_ready_head_sha"] == record["last_submit_push_sha"]
  assert "readying" not in ready
  assert [call[:2] for call in calls] == [
    ("api", f"repos/{record['repo']}/pulls/{record['number']}"),
    ("api", "graphql"),
    ("api", f"repos/{record['repo']}/pulls/{record['number']}"),
  ]
  mutation = calls[1]
  assert "markPullRequestReadyForReview" in mutation[mutation.index("-f") + 1]
  assert "pullRequestId=PR_kwDO_ready_58" in mutation


@pytest.mark.parametrize("mutation_failure", ["timeout", "nonzero"])
def test_mark_ready_lost_response_recovers_by_read_without_second_mutation(
  client, owner_token, monkeypatch, mutation_failure,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record = _write_personal_draft(
    app_id, f"personal-draft-recovery-{mutation_failure}",
  )
  monkeypatch.setattr(
    "app.github_contributions.shutil.which", lambda name: f"/bin/{name}",
  )
  calls = []
  recovering = False

  def fake_gh(_repo_path, *args, check=True):
    calls.append(args)
    if args[:2] == ("api", f"repos/{record['repo']}/pulls/{record['number']}"):
      return _cp(json.dumps(_personal_pr_live(record, draft=not recovering)))
    if args[:2] == ("api", "graphql"):
      if mutation_failure == "timeout":
        raise subprocess.TimeoutExpired(["gh", "api", "graphql"], timeout=30)
      return _cp("", "network response ended early", returncode=1)
    pytest.fail(f"unexpected gh call: {args}")

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  url = f"/api/github/contributions/{app_id}/{record['id']}/ready"
  headers = {"Authorization": f"Bearer {app_token}"}
  payload = {"expected_head_sha": record["last_submit_push_sha"]}

  first = client.post(url, headers=headers, json=payload)
  assert first.status_code == 503, first.text
  assert first.json()["detail"]["code"] == "ready_unconfirmed"
  assert first.json()["detail"]["record"]["readying"]["expected_head_sha"] == (
    record["last_submit_push_sha"]
  )

  recovering = True
  second = client.post(url, headers=headers, json=payload)
  assert second.status_code == 200, second.text
  assert second.json()["record"]["publication_stage"] == "ready"
  assert "readying" not in second.json()["record"]
  assert sum(call[:2] == ("api", "graphql") for call in calls) == 1


def test_mark_ready_recovery_releases_a_changed_live_target(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record = _write_personal_draft(app_id, "personal-draft-recovery-drift")
  record["readying"] = {
    "version": 1,
    "repo": record["repo"],
    "number": record["number"],
    "url": record["url"],
    "head_repository": record["head_repository"],
    "head_branch": record["plan"]["branch"],
    "base_branch": record["last_submit_upstream_branch"],
    "expected_head_sha": record["last_submit_push_sha"],
    "started_at": "2026-08-29T08:01:00Z",
  }
  _write_contribution(app_id, record["id"], record)
  monkeypatch.setattr(
    "app.github_contributions.shutil.which", lambda name: f"/bin/{name}",
  )
  calls = []

  def fake_gh(_repo_path, *args, check=True):
    calls.append(args)
    return _cp(json.dumps(_personal_pr_live(
      record, draft=True, head_sha="c" * 40,
    )))

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/ready",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"expected_head_sha": record["last_submit_push_sha"]},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "ready_target_changed"
  stored = json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id) /
     "contributions" / f"{record['id']}.json").read_text()
  )
  assert "readying" not in stored
  assert stored["last_ready_error_code"] == "ready_target_changed"
  assert len(calls) == 1


def test_mark_ready_rejects_changed_head_and_relay_without_github(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record = _write_personal_draft(app_id, "personal-draft-stale")
  calls = []
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda *_args, **_kwargs: calls.append(True) or _cp(""),
  )
  headers = {"Authorization": f"Bearer {app_token}"}

  stale = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/ready",
    headers=headers,
    json={"expected_head_sha": "c" * 40},
  )
  assert stale.status_code == 409, stale.text
  assert "changed after the Ready action was shown" in stale.json()["detail"]

  record["id"] = "relay-draft"
  record["submission_mode"] = "mobius-bot"
  record["relay_contribution_id"] = "ctr_1234567890abcdef1234567890abcdef"
  _write_contribution(app_id, record["id"], record)
  relay = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/ready",
    headers=headers,
    json={"expected_head_sha": record["last_submit_push_sha"]},
  )
  assert relay.status_code == 409, relay.text
  assert "relay supports" in relay.json()["detail"]
  assert calls == []


@pytest.mark.parametrize("field", ["base", "head", "quality"])
def test_mark_ready_rejects_legacy_abbreviated_review_oid_without_github(
  client, owner_token, monkeypatch, field,
):
  """Ready never upgrades a legacy prefix into a public-action identity."""
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record = _write_personal_draft(app_id, f"ready-abbreviated-{field}")
  if field == "base":
    record["plan"]["base_sha"] = record["plan"]["base_sha"][:12]
  elif field == "head":
    record["plan"]["head_sha"] = record["plan"]["head_sha"][:12]
  else:
    record["quality_review"]["reviewed_head_sha"] = (
      record["quality_review"]["reviewed_head_sha"][:12]
    )
  _write_contribution(app_id, record["id"], record)
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda *_args, **_kwargs: pytest.fail("abbreviated Ready must not call GitHub"),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/ready",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"expected_head_sha": record["last_submit_push_sha"]},
  )

  assert response.status_code == 409, response.text
  assert "full canonical" in response.json()["detail"] or (
    "all-clear review" in response.json()["detail"]
  )


def test_mark_ready_rejects_live_identity_drift_without_mutation(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record = _write_personal_draft(app_id, "personal-draft-live-drift")
  monkeypatch.setattr(
    "app.github_contributions.shutil.which", lambda name: f"/bin/{name}",
  )
  calls = []

  def fake_gh(_repo_path, *args, check=True):
    calls.append(args)
    if args[:2] == ("api", f"repos/{record['repo']}/pulls/{record['number']}"):
      return _cp(json.dumps(_personal_pr_live(
        record, draft=True, head_sha="c" * 40,
      )))
    pytest.fail("identity drift must stop before a mutation")

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/ready",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"expected_head_sha": record["last_submit_push_sha"]},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "ready_target_changed"
  stored = json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id) /
     "contributions" / f"{record['id']}.json").read_text()
  )
  assert stored["status"] == "draft"
  assert "readying" not in stored
  assert len(calls) == 1


def test_mark_ready_refuses_to_trigger_an_armed_auto_merge(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record = _write_personal_draft(app_id, "personal-draft-auto-merge")
  monkeypatch.setattr(
    "app.github_contributions.shutil.which", lambda name: f"/bin/{name}",
  )
  calls = []

  def fake_gh(_repo_path, *args, check=True):
    calls.append(args)
    if args[:2] == ("api", f"repos/{record['repo']}/pulls/{record['number']}"):
      return _cp(json.dumps(_personal_pr_live(
        record,
        draft=True,
        auto_merge={"merge_method": "squash"},
      )))
    pytest.fail("an armed auto-merge must stop before the Ready mutation")

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/ready",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"expected_head_sha": record["last_submit_push_sha"]},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "ready_auto_merge_enabled"
  assert len(calls) == 1


def test_assign_review_names_one_exact_pr(client, owner_token, monkeypatch):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  calls = []

  def fake_gh(repo_path, *args, check=True):
    calls.append((Path(repo_path), args))
    if args[-1] == "repos/mobius-os/mobius":
      return _cp('{"permissions":{"triage":true}}')
    if args[-1] == "repos/mobius-os/mobius/pulls/42":
      return _cp('{"state":"open","head":{"sha":"abc"}}')
    return _cp('{"assignees":[{"login":"octocat"}]}')

  monkeypatch.setattr("app.routes.github._gh", fake_gh)
  response = client.post(
    f"/api/github/contributions/{app_id}/assign-review",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"repo": "mobius-os/mobius", "number": 42},
  )
  assert response.status_code == 200, response.text
  assert response.json() == {
    "assigned": True,
    "login": "octocat",
    "repo": "mobius-os/mobius",
    "number": 42,
  }
  assert calls[-1][1] == (
    "api", "--method", "POST", "repos/mobius-os/mobius/issues/42/assignees",
    "-f", "assignees[]=octocat",
  )


def test_assign_review_rejects_invalid_repo(client, owner_token):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  response = client.post(
    f"/api/github/contributions/{app_id}/assign-review",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"repo": "invalid", "number": 1},
  )
  assert response.status_code == 422


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
  source = data_dir / "apps" / f"{record_id}-source"
  source.mkdir(parents=True)
  subprocess.run(["git", "init", "-b", "main"], cwd=source, check=True,
                 capture_output=True)
  subprocess.run(["git", "config", "user.name", "octocat"], cwd=source,
                 check=True)
  subprocess.run([
    "git", "config", "user.email", "42+octocat@users.noreply.github.com",
  ], cwd=source, check=True)
  (source / "index.jsx").write_text("export default 2\n")
  subprocess.run(["git", "add", "index.jsx"], cwd=source, check=True)
  subprocess.run(["git", "commit", "-m", "installed source"], cwd=source,
                 check=True, capture_output=True)
  source_sha = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=source, text=True,
  ).strip()
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Reviewed fix",
    "branch": "fix/demo-review",
    "quality_review": _all_clear_review(head),
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "title": "Reviewed fix",
      "body_draft": "Reviewed fix body.",
      "branch": "fix/demo-review",
      "repo_path": str(repo),
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
      "source_repo_path": str(source),
      "source_sha": source_sha,
    },
  }
  _write_contribution(app_id, record_id, record, diff_text)
  return repo, record, diff_text


def _agent_run_headers(db, chat_id: str, run_id: str) -> dict[str, str]:
  db.add(models.ChatRun(
    id=run_id,
    root_run_id=run_id,
    chat_id=chat_id,
    status="running",
    provider="codex",
  ))
  db.commit()
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(
    chat_id,
    owner.username,
    owner.token_epoch,
    run_id=run_id,
    expires_delta=timedelta(minutes=5),
  )
  return {"Authorization": f"Bearer {token}"}


def _prepared_continuity_route(
  client,
  owner_token,
  db,
  record_id: str,
) -> dict:
  """Create one exact overlapping review plus its active source-chat bearer."""
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  review_repo, record, diff_text = _prepared_real_review(app_id, record_id)
  chat_response = client.post(
    "/api/chats",
    json={"title": "Source review"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert chat_response.status_code == 200, chat_response.text
  chat_id = chat_response.json()["id"]
  record["chat_id"] = chat_id
  record["quality_review"] = _all_clear_review(record["plan"]["head_sha"])
  _write_contribution(app_id, record_id, record, diff_text)

  source_repo = Path(record["plan"]["source_repo_path"])
  (source_repo / "index.jsx").write_text("export default 3\n")
  subprocess.run(["git", "add", "index.jsx"], cwd=source_repo, check=True)
  subprocess.run(
    ["git", "commit", "-m", "reviewed local refinement"],
    cwd=source_repo,
    check=True,
    capture_output=True,
  )
  reviewed_through = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=source_repo, text=True,
  ).strip()
  source_advance = app_git._canonical_diff(
    source_repo,
    record["plan"]["source_sha"],
    reviewed_through,
    read_only=True,
  )
  assert source_advance is not None
  body = {
    "base_sha": record["plan"]["base_sha"],
    "head_sha": record["plan"]["head_sha"],
    "source_sha": record["plan"]["source_sha"],
    "diff_sha256": record["plan"]["diff_sha256"],
    "reviewed_through_sha": reviewed_through,
    "source_advance_diff_sha256": hashlib.sha256(source_advance).hexdigest(),
    "review_identity_sha256": github_contributions._reviewed_source_identity(
      record,
    ),
  }
  auth = _agent_run_headers(db, chat_id, f"run-{record_id}")
  return {
    "app_id": app_id,
    "app_token": app_token,
    "record_id": record_id,
    "review_repo": review_repo,
    "source_repo": source_repo,
    "record": record,
    "body": body,
    "auth": auth,
    "url": f"/api/github/contributions/{app_id}/{record_id}/source-continuity",
  }


def _remove_reviewed_change_from_source(record: dict, *, commit: bool = True) -> None:
  source = Path(record["plan"]["source_repo_path"])
  (source / "index.jsx").write_text("export default 1\n")
  if commit:
    subprocess.run(["git", "add", "index.jsx"], cwd=source, check=True)
    subprocess.run(
      ["git", "commit", "-m", "remove reviewed behavior"],
      cwd=source,
      check=True,
      capture_output=True,
    )


def _add_source_proof_stack_child(
  app_id: int,
  parent: dict,
  *,
  status: str,
) -> list[str]:
  """Complete a real two-layer chain sharing one installed source checkout."""
  stack_id = "source-proof"
  parent_branch = f"stack/{stack_id}/01-parent"
  repo = Path(parent["plan"]["repo_path"])
  subprocess.run(
    ["git", "branch", parent_branch, parent["plan"]["head_sha"]],
    cwd=repo,
    check=True,
    capture_output=True,
  )
  parent["branch"] = parent_branch
  parent["plan"]["branch"] = parent_branch
  parent["plan"]["stack"] = {
    "id": stack_id,
    "position": 1,
    "total": 2,
    "parent_record_id": "",
    "base_branch": "main",
  }
  child = json.loads(json.dumps(parent))
  child_id = f"{parent['id']}-child"
  child_branch = f"stack/{stack_id}/02-child"
  subprocess.run(
    ["git", "checkout", "-q", "-b", child_branch, parent["plan"]["head_sha"]],
    cwd=repo,
    check=True,
  )
  (repo / "child.jsx").write_text("export default 'child'\n")
  subprocess.run(["git", "add", "child.jsx"], cwd=repo, check=True)
  subprocess.run([
    "git", "commit", "-m", "reviewed child", "-m",
    "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>",
  ], cwd=repo, check=True, capture_output=True)
  child_head = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=repo, text=True,
  ).strip()
  child_diff = subprocess.check_output([
    "git", "-c", "core.quotePath=false", "diff", "--no-ext-diff",
    "--no-color", "--binary", "--full-index", "--src-prefix=a/",
    "--dst-prefix=b/", f"{parent['plan']['head_sha']}..{child_head}",
  ], cwd=repo, text=True)
  subprocess.run(["git", "checkout", "-q", parent_branch], cwd=repo, check=True)
  child.update({
    "id": child_id,
    "status": status,
    "branch": child_branch,
    "number": 92,
    "url": "https://github.com/mobius-os/app-demo/pull/92",
  })
  child["plan"].update({
    "branch": child_branch,
    "base_sha": parent["plan"]["head_sha"],
    "head_sha": child_head,
    "diff_sha256": hashlib.sha256(child_diff.encode()).hexdigest(),
    "stack": {
      "id": stack_id,
      "position": 2,
      "total": 2,
      "parent_record_id": parent["id"],
      "base_branch": parent_branch,
    },
  })
  child["quality_review"] = _all_clear_review(child_head)
  _write_contribution(app_id, child_id, child, child_diff)
  return [parent["id"], child_id]


def test_review_status_catches_local_drift_before_send(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  repo, record, _diff = _prepared_real_review(app_id, "review-health")
  source = Path(record["plan"]["source_repo_path"])
  reviewed_head = record["plan"]["head_sha"]
  source_refs_before = subprocess.check_output(
    ["git", "show-ref"], cwd=source, text=True,
  )
  assert subprocess.run(
    ["git", "cat-file", "-e", f"{reviewed_head}^{{commit}}"],
    cwd=source, check=False, capture_output=True,
  ).returncode != 0
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
    "publication": {
      "state": "unavailable",
      "code": "publication_unavailable",
      "message": "GitHub freshness could not be checked right now.",
    },
  }]
  # A standalone app review needs a cross-repository proof. Previewing it must
  # not import reviewed objects or leave a durable ref in the installed app.
  assert subprocess.check_output(
    ["git", "show-ref"], cwd=source, text=True,
  ) == source_refs_before
  assert subprocess.run(
    ["git", "cat-file", "-e", f"{reviewed_head}^{{commit}}"],
    cwd=source, check=False, capture_output=True,
  ).returncode != 0

  (repo / "index.jsx").write_text("export default 3\n")
  stale = client.get(
    f"/api/github/contributions/{app_id}/review-status", headers=headers,
  )
  assert stale.status_code == 200, stale.text
  assert stale.json()["needs_refresh"] == 1
  assert stale.json()["records"][0]["code"] == "working_changes"
  # Read-only means the review check neither commits nor discards the edit.
  assert (repo / "index.jsx").read_text() == "export default 3\n"


def test_review_status_requires_an_installed_source_for_standalone_review(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, diff_text = _prepared_real_review(app_id, "review-no-source")
  record["plan"].pop("source_repo_path")
  record["plan"].pop("source_sha")
  _write_contribution(app_id, record["id"], record, diff_text)

  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  assert response.json()["ready"] == 0
  assert response.json()["records"][0]["code"] == "missing_source_provenance"


@pytest.mark.parametrize("field", ["base", "head", "quality"])
def test_send_rejects_abbreviated_review_oids_before_claim(
  client, owner_token, monkeypatch, field,
):
  """Legacy short hashes need refresh and never enter submitting state."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, diff_text = _prepared_real_review(
    app_id, f"abbreviated-{field}",
  )
  if field == "base":
    record["plan"]["base_sha"] = record["plan"]["base_sha"][:12]
  elif field == "head":
    short = record["plan"]["head_sha"][:12]
    record["plan"]["head_sha"] = short
    record["quality_review"]["reviewed_head_sha"] = short
  else:
    record["quality_review"]["reviewed_head_sha"] = (
      record["quality_review"]["reviewed_head_sha"][:12]
    )
  _write_contribution(app_id, record["id"], record, diff_text)
  monkeypatch.setattr(
    github_routes, "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail("abbreviated review must not publish"),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/submit",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"publication_stage": "draft"},
  )

  assert response.status_code == 409, response.text
  stored = json.loads((
    Path(get_settings().data_dir) / "apps" / str(app_id) /
    "contributions" / f"{record['id']}.json"
  ).read_text())
  assert stored["status"] == "prepared"


def test_reviewed_commit_resolution_rejects_abbreviation_before_git_lookup(
  tmp_path, monkeypatch,
):
  """A locally resolvable prefix is not the immutable identity reviewed."""
  monkeypatch.setattr(
    "app.github_contribution_git._git",
    lambda *_args, **_kwargs: pytest.fail("short oid must fail before rev-parse"),
  )
  with pytest.raises(ContributionSubmitError, match="full canonical"):
    github_contributions._git_ops._resolve_reviewed_commit(
      tmp_path, "a" * 12, "head sha",
    )


def test_reviewed_commit_resolution_requires_raw_oid_to_match_resolved_identity(
  tmp_path, monkeypatch,
):
  reviewed = "a" * 40
  monkeypatch.setattr(
    "app.github_contribution_git._git",
    lambda *_args, **_kwargs: _cp("b" * 40 + "\n"),
  )
  with pytest.raises(ContributionSubmitError, match="resolved incorrectly"):
    github_contributions._git_ops._resolve_reviewed_commit(
      tmp_path, reviewed, "head sha",
    )


def test_submit_requires_source_provenance_without_a_prior_status_read(
  client, owner_token, monkeypatch,
):
  """A direct Send cannot bypass the installed-source ownership boundary."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, diff_text = _prepared_real_review(app_id, "send-no-source")
  record["plan"].pop("source_repo_path")
  record["plan"].pop("source_sha")
  _write_contribution(app_id, record["id"], record, diff_text)
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail("missing provenance must never publish"),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/submit",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"publication_stage": "draft"},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "missing_source_provenance"


def test_submit_rechecks_current_source_after_ready_status(
  client, owner_token, monkeypatch,
):
  """A source change between Ready and Send returns to review, without push."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, diff_text = _prepared_real_review(app_id, "send-source-moved")
  headers = {"Authorization": f"Bearer {app_token}"}
  ready = client.get(
    f"/api/github/contributions/{app_id}/review-status", headers=headers,
  )
  assert ready.status_code == 200, ready.text
  assert ready.json()["ready"] == 1

  # These fields are app-writable. Even a plausible forged lost-response
  # journal cannot authorize publication without matching public GitHub state.
  record.update({
    "last_submit_stage": "pushed",
    "last_submit_push_sha": record["plan"]["head_sha"],
    "last_submit_upstream_branch": "main",
    "head_repository": "octocat/app-demo",
  })
  _write_contribution(app_id, record["id"], record, diff_text)
  monkeypatch.setattr(
    github_contributions, "_find_existing_pr", lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_contributions, "_existing_branch_pr", lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda *_args, **_kwargs: "c" * 40,
  )
  _remove_reviewed_change_from_source(record)
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail("moved source must never publish"),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/submit",
    headers=headers,
    json={"publication_stage": "draft"},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "source_provenance_mismatch"
  assert not github_routes.contribution_runtime.personal_attempt_path(
    app_id, record["id"],
  ).exists()


def test_submit_rejects_dirty_installed_source_before_push(
  client, owner_token, monkeypatch,
):
  """Uncommitted served bytes cannot be ignored in favor of source HEAD."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, diff_text = _prepared_real_review(app_id, "send-source-dirty")
  record.update({
    "last_submit_stage": "pushed",
    "last_submit_push_sha": record["plan"]["head_sha"],
    "last_submit_upstream_branch": "main",
    "head_repository": "octocat/app-demo",
  })
  _write_contribution(app_id, record["id"], record, diff_text)
  monkeypatch.setattr(
    github_contributions, "_find_existing_pr", lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_contributions, "_existing_branch_pr", lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda *_args, **_kwargs: "c" * 40,
  )
  _remove_reviewed_change_from_source(record, commit=False)
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail("dirty source must never publish"),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/submit",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"publication_stage": "draft"},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "source_provenance_mismatch"


def test_source_continuity_route_requires_the_exact_active_source_chat(
  client, owner_token, db,
):
  fixture = _prepared_continuity_route(
    client, owner_token, db, "continuity-route-auth",
  )
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  assert client.post(
    fixture["url"], headers=owner_auth, json=fixture["body"],
  ).status_code == 403
  assert client.post(
    fixture["url"],
    headers={"Authorization": f"Bearer {fixture['app_token']}"},
    json=fixture["body"],
  ).status_code == 403

  other_chat = client.post(
    "/api/chats",
    json={"title": "Different source"},
    headers=owner_auth,
  ).json()["id"]
  other_auth = _agent_run_headers(db, other_chat, "run-continuity-other")
  wrong_chat = client.post(
    fixture["url"], headers=other_auth, json=fixture["body"],
  )
  assert wrong_chat.status_code == 403
  assert "source chat" in wrong_chat.json()["detail"]

  accepted = client.post(
    fixture["url"], headers=fixture["auth"], json=fixture["body"],
  )
  assert accepted.status_code == 200, accepted.text


def test_source_continuity_route_rejects_dirty_installed_source(
  client, owner_token, db,
):
  fixture = _prepared_continuity_route(
    client, owner_token, db, "continuity-route-dirty",
  )
  (fixture["source_repo"] / "uncommitted.txt").write_text("not reviewed\n")

  response = client.post(
    fixture["url"], headers=fixture["auth"], json=fixture["body"],
  )

  assert response.status_code == 409, response.text
  assert "uncommitted changes" in response.json()["detail"]["message"]
  assert app_git.prepublication_source_continuity(
    fixture["source_repo"],
    base_sha=fixture["body"]["base_sha"],
    head_sha=fixture["body"]["head_sha"],
    source_sha=fixture["body"]["source_sha"],
    current_source_sha=fixture["body"]["reviewed_through_sha"],
    diff_sha256=fixture["body"]["diff_sha256"],
    contribution_id=fixture["record_id"],
    review_identity_sha256=fixture["body"]["review_identity_sha256"],
  ) is None


def test_source_continuity_route_rejects_stale_review_checkout(
  client, owner_token, db,
):
  fixture = _prepared_continuity_route(
    client, owner_token, db, "continuity-route-stale-review",
  )
  (fixture["review_repo"] / "later.txt").write_text("unreviewed revision\n")
  subprocess.run(
    ["git", "add", "later.txt"], cwd=fixture["review_repo"], check=True,
  )
  subprocess.run(
    ["git", "commit", "-m", "later private revision"],
    cwd=fixture["review_repo"],
    check=True,
    capture_output=True,
  )

  response = client.post(
    fixture["url"], headers=fixture["auth"], json=fixture["body"],
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "branch_moved"


@pytest.mark.parametrize(
  "field,replacement",
  [
    ("reviewed_through_sha", lambda fixture: fixture["body"]["source_sha"]),
    ("source_advance_diff_sha256", lambda _fixture: "0" * 64),
  ],
  ids=["stale-source-head", "source-advance-digest"],
)
def test_source_continuity_route_rejects_stale_source_or_digest(
  client, owner_token, db, field, replacement,
):
  fixture = _prepared_continuity_route(
    client, owner_token, db, f"continuity-route-{field}",
  )
  body = {**fixture["body"], field: replacement(fixture)}

  response = client.post(
    fixture["url"], headers=fixture["auth"], json=body,
  )

  assert response.status_code == 409, response.text
  assert "changed" in str(response.json()["detail"]).lower() or (
    "differs" in str(response.json()["detail"]).lower()
  )


def test_source_continuity_route_binds_the_locked_canonical_review_identity(
  client, owner_token, db,
):
  fixture = _prepared_continuity_route(
    client, owner_token, db, "continuity-route-identity",
  )
  wrong_body = {
    **fixture["body"],
    "review_identity_sha256": "f" * 64,
  }
  wrong_identity = client.post(
    fixture["url"], headers=fixture["auth"], json=wrong_body,
  )
  assert wrong_identity.status_code == 409, wrong_identity.text

  fixture["record"]["plan"]["body_draft"] = "App-writable drift."
  _write_contribution(
    fixture["app_id"], fixture["record_id"], fixture["record"],
  )
  stale_envelope = client.post(
    fixture["url"], headers=fixture["auth"], json=fixture["body"],
  )
  assert stale_envelope.status_code == 409, stale_envelope.text
  assert "changed before continuity" in stale_envelope.json()["detail"]


def test_source_continuity_identity_freezes_review_inputs_not_status_mirrors():
  record = {
    "id": "identity-envelope",
    "type": "pr",
    "repo": "mobius-os/mobius",
    "status": "prepared",
    "title": "Reviewed title",
    "branch": "fix/identity-envelope",
    "chat_id": "source-chat",
    "chat_ids": ["source-chat"],
    "plan": {
      "action": "pr",
      "repo": "mobius-os/mobius",
      "body_draft": "Reviewed body.",
      "head_sha": "a" * 40,
    },
    "quality_review": {
      "state": "all_clear",
      "reviewed_head_sha": "a" * 40,
    },
    "updated_at": "2026-09-01T00:00:00Z",
    "checks": {"state": "pending"},
  }
  identity = github_contributions._reviewed_source_identity(record)

  operational = json.loads(json.dumps(record))
  operational["status"] = "submitting"
  operational["submitter"] = "contribute-update-button"
  operational["submit_started_at"] = "2026-09-02T00:00:00Z"
  operational["updated_at"] = "2026-09-02T00:00:00Z"
  operational["checks"] = {"state": "success"}
  assert github_contributions._reviewed_source_identity(operational) == identity

  for mutate in (
    lambda item: item["plan"].update(body_draft="Changed body."),
    lambda item: item["chat_ids"].append("new-source-chat"),
    lambda item: item["quality_review"].update(state="changes_needed"),
  ):
    changed = json.loads(json.dumps(record))
    mutate(changed)
    assert github_contributions._reviewed_source_identity(changed) != identity


def test_manifest_identity_projection_is_review_bound_and_forwarded(
  client, owner_token, monkeypatch,
):
  app_id, _app_token_value = _app_token(
    client, owner_token, github_access=True,
  )
  _repo, record, _diff_text = _prepared_real_review(
    app_id, "manifest-identity-projection",
  )
  projection = {
    "adapter": "github_app_manifest_identity_v1",
    "path": "mobius.json",
    "fields": {
      "author": "mobius-os",
      "homepage": "https://github.com/mobius-os/app-kanban",
    },
  }
  record["repo"] = "mobius-os/app-kanban"
  record["plan"]["repo"] = "mobius-os/app-kanban"
  record["plan"]["source_projection"] = projection
  reviewed_identity = github_contributions._reviewed_source_identity(record)
  seen = []

  def prove(_repo, **kwargs):
    seen.append(kwargs["source_projection"])
    return kwargs["source_projection"].adapter

  monkeypatch.setattr(
    app_git, "preview_pending_equivalent_change", prove,
  )

  assert github_contributions._assert_pending_equivalence_preflight(
    record,
  ) == "github_app_manifest_identity_v1"
  assert [item.as_dict() for item in seen] == [projection]

  changed = json.loads(json.dumps(record))
  changed["plan"]["source_projection"]["fields"]["homepage"] = (
    "https://github.com/mobius-os/app-other"
  )
  assert github_contributions._reviewed_source_identity(
    changed,
  ) != reviewed_identity
  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._assert_pending_equivalence_preflight(changed)
  assert caught.value.code == "missing_source_provenance"


def test_source_continuity_route_is_idempotent_and_restart_durable(
  client, owner_token, db,
):
  fixture = _prepared_continuity_route(
    client, owner_token, db, "continuity-route-restart",
  )
  first = client.post(
    fixture["url"], headers=fixture["auth"], json=fixture["body"],
  )
  assert first.status_code == 200, first.text
  subprocess.run(
    ["git", "pack-refs", "--all"], cwd=fixture["source_repo"], check=True,
  )

  second = client.post(
    fixture["url"], headers=fixture["auth"], json=fixture["body"],
  )
  assert second.status_code == 200, second.text
  assert second.json() == first.json()
  refs = subprocess.check_output(
    [
      "git", "for-each-ref", "--format=%(refname)",
      "refs/mobius/equivalences/reviewed-source/",
    ],
    cwd=fixture["source_repo"],
    text=True,
  ).splitlines()
  assert len(refs) == 1


@pytest.mark.asyncio
async def test_source_continuity_route_serializes_concurrent_exact_retries(
  client, owner_token, db,
):
  fixture = _prepared_continuity_route(
    client, owner_token, db, "continuity-route-concurrent",
  )

  transport = httpx.ASGITransport(app=client.app)
  async with httpx.AsyncClient(
    transport=transport, base_url="http://testserver",
  ) as async_client:
    responses = await asyncio.gather(*(
      async_client.post(
        fixture["url"], headers=fixture["auth"], json=fixture["body"],
      )
      for _index in range(2)
    ))

  assert [response.status_code for response in responses] == [200, 200], [
    response.text for response in responses
  ]
  assert responses[0].json() == responses[1].json()
  refs = subprocess.check_output(
    [
      "git", "for-each-ref", "--format=%(refname)",
      "refs/mobius/equivalences/reviewed-source/",
    ],
    cwd=fixture["source_repo"],
    text=True,
  ).splitlines()
  assert len(refs) == 1



def _prepared_resolution_route(client, owner_token, db, record_id):
  fixture = _prepared_continuity_route(client, owner_token, db, record_id)
  # Unlike later-edit continuity, this captured source never contained the
  # upstream-shaped patch exactly: the local variant needs explicit review.
  record = fixture["record"]
  source_sha = fixture["body"]["reviewed_through_sha"]
  record["plan"]["source_sha"] = source_sha
  _write_contribution(fixture["app_id"], record_id, record)
  fixture["body"].update(
    source_sha=source_sha,
    source_advance_diff_sha256=hashlib.sha256(b"").hexdigest(),
    review_identity_sha256=github_contributions._reviewed_source_identity(record),
  )
  resolution = app_git.preview_source_resolution(
    fixture["source_repo"],
    base_sha=record["plan"]["base_sha"],
    head_sha=record["plan"]["head_sha"],
    source_sha=source_sha,
    diff_sha256=record["plan"]["diff_sha256"],
    review_source_dir=fixture["review_repo"],
  )
  assert resolution is not None
  assert resolution.conflict_paths == ("index.jsx",)
  fixture["body"]["source_resolution_sha256"] = resolution.diff_sha256
  return fixture


def test_source_resolution_requires_explicit_review_before_publication(
  client, owner_token, db, monkeypatch,
):
  fixture = _prepared_resolution_route(
    client, owner_token, db, "source-resolution-explicit",
  )
  record = fixture["record"]
  monkeypatch.setattr(github_contributions._git_ops, "_gh", lambda *a, **kw:
    pytest.fail("Source reconciliation must not contact GitHub"))
  with pytest.raises(ContributionSubmitError, match="durable source"):
    github_contributions._assert_pending_equivalence_preflight(record)

  implicit_body = {k: v for k, v in fixture["body"].items()
                   if k != "source_resolution_sha256"}
  rejected = client.post(fixture["url"], headers=fixture["auth"], json=implicit_body)
  assert rejected.status_code == 409, rejected.text

  accepted = client.post(fixture["url"], headers=fixture["auth"], json=fixture["body"])
  assert accepted.status_code == 200, accepted.text
  assert github_contributions._assert_pending_equivalence_preflight(record) == (
    "reviewed_source_resolution"
  )
  pending_ref = github_contributions._record_pending_equivalence(record)
  assert pending_ref
  witness = app_git._read_equivalent_change(fixture["source_repo"], pending_ref)
  assert witness.proof_mode == "reviewed_source_resolution"
  assert witness.source_sha == fixture["body"]["reviewed_through_sha"]
  assert record["status"] == "prepared"


@pytest.mark.parametrize("changed", ["resolution", "source", "review", "dirty", "owner"])
def test_source_resolution_keeps_exact_review_and_authority_guards(
  client, owner_token, db, changed,
):
  fixture = _prepared_resolution_route(
    client, owner_token, db, "source-resolution-" + changed,
  )
  body = dict(fixture["body"])
  headers = fixture["auth"]
  if changed == "resolution":
    body["source_resolution_sha256"] = "0" * 64
  elif changed == "source":
    body["reviewed_through_sha"] = fixture["record"]["plan"]["base_sha"]
  elif changed == "review":
    fixture["record"]["plan"]["body_draft"] = "A changed public proposal"
    _write_contribution(fixture["app_id"], fixture["record_id"], fixture["record"])
  elif changed == "dirty":
    (fixture["source_repo"] / "unreviewed.txt").write_text("owner draft\n")
  elif changed == "owner":
    headers = {"Authorization": f"Bearer {owner_token}"}
  response = client.post(fixture["url"], headers=headers, json=body)
  assert response.status_code == (403 if changed == "owner" else 409), response.text
  with pytest.raises(ContributionSubmitError):
    github_contributions._assert_pending_equivalence_preflight(fixture["record"])


def test_source_resolution_retries_survive_restart_but_not_later_source_edits(
  client, owner_token, db,
):
  fixture = _prepared_resolution_route(
    client, owner_token, db, "source-resolution-restart",
  )
  for _ in range(2):
    response = client.post(fixture["url"], headers=fixture["auth"], json=fixture["body"])
    assert response.status_code == 200, response.text
    subprocess.run(["git", "pack-refs", "--all"], cwd=fixture["source_repo"], check=True)
  assert github_contributions._assert_pending_equivalence_preflight(fixture["record"]) == (
    "reviewed_source_resolution"
  )
  _remove_reviewed_change_from_source(fixture["record"])
  with pytest.raises(ContributionSubmitError):
    github_contributions._assert_pending_equivalence_preflight(fixture["record"])
  assert github_contributions._record_pending_equivalence(fixture["record"]) is None

def test_agent_reviewed_continuity_bridges_overlap_into_pending_provenance(
  client, owner_token,
):
  """Send can reuse exact original proof after a reviewed same-path advance."""
  app_id, _app_token_value = _app_token(
    client, owner_token, github_access=True,
  )
  _repo, record, _diff_text = _prepared_real_review(
    app_id, "reviewed-source-continuity",
  )
  record["quality_review"] = _all_clear_review(record["plan"]["head_sha"])
  source = Path(record["plan"]["source_repo_path"])
  (source / "index.jsx").write_text("export default 3\n")
  subprocess.run(["git", "add", "index.jsx"], cwd=source, check=True)
  subprocess.run(
    ["git", "commit", "-m", "reviewed local refinement"],
    cwd=source,
    check=True,
    capture_output=True,
  )
  reviewed_through = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=source, text=True,
  ).strip()

  with pytest.raises(ContributionSubmitError) as strict:
    github_contributions._assert_pending_equivalence_preflight(record)
  assert strict.value.code == "source_provenance_mismatch"
  witness = github_contributions._record_prepublication_source_continuity(
    record,
    reviewed_through_sha=reviewed_through,
  )
  assert witness
  assert github_contributions._assert_pending_equivalence_preflight(
    record,
  ) == "reviewed_source_continuity"

  pending = github_contributions._record_pending_equivalence(record)
  assert pending
  recorded = app_git._read_equivalent_change(source, pending)
  assert recorded is not None
  assert recorded.source_sha == record["plan"]["source_sha"]
  assert not app_git.ref_exists(source, witness)


def test_agent_reviewed_continuity_survives_publication_claim_state(
  client, owner_token,
):
  """The guarded prepared -> submitting claim keeps the reviewed witness valid."""
  app_id, _app_token_value = _app_token(
    client, owner_token, github_access=True,
  )
  _repo, record, _diff_text = _prepared_real_review(
    app_id, "reviewed-source-claim-state",
  )
  record["quality_review"] = _all_clear_review(record["plan"]["head_sha"])
  source = Path(record["plan"]["source_repo_path"])
  (source / "index.jsx").write_text("export default 3\n")
  subprocess.run(["git", "add", "index.jsx"], cwd=source, check=True)
  subprocess.run(
    ["git", "commit", "-m", "reviewed local refinement"],
    cwd=source,
    check=True,
    capture_output=True,
  )
  reviewed_through = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=source, text=True,
  ).strip()
  assert github_contributions._record_prepublication_source_continuity(
    record,
    reviewed_through_sha=reviewed_through,
  )

  claimed = json.loads(json.dumps(record))
  claimed.update({
    "status": "submitting",
    "submitter": "contribute-update-button",
    "submit_started_at": "2026-09-02T00:00:00Z",
    "updated_at": "2026-09-02T00:00:00Z",
  })
  assert github_contributions._assert_pending_equivalence_preflight(
    claimed,
  ) == "reviewed_source_continuity"


def test_agent_reviewed_continuity_fails_after_a_later_reviewed_path_revert(
  client, owner_token,
):
  """A later removal cannot ride an older active-agent attestation."""
  app_id, _app_token_value = _app_token(
    client, owner_token, github_access=True,
  )
  _repo, record, _diff_text = _prepared_real_review(
    app_id, "reviewed-source-reverted",
  )
  record["quality_review"] = _all_clear_review(record["plan"]["head_sha"])
  source = Path(record["plan"]["source_repo_path"])
  (source / "index.jsx").write_text("export default 3\n")
  subprocess.run(["git", "add", "index.jsx"], cwd=source, check=True)
  subprocess.run(
    ["git", "commit", "-m", "reviewed local refinement"],
    cwd=source,
    check=True,
    capture_output=True,
  )
  reviewed_through = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=source, text=True,
  ).strip()
  assert github_contributions._record_prepublication_source_continuity(
    record,
    reviewed_through_sha=reviewed_through,
  )

  (source / "index.jsx").write_text("export default 1\n")
  subprocess.run(["git", "add", "index.jsx"], cwd=source, check=True)
  subprocess.run(
    ["git", "commit", "-m", "revert reviewed behavior"],
    cwd=source,
    check=True,
    capture_output=True,
  )

  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._assert_pending_equivalence_preflight(record)
  assert caught.value.code == "source_provenance_mismatch"
  assert github_contributions._record_pending_equivalence(record) is None


def test_publication_preflight_reconciles_only_authoritative_public_state(
  monkeypatch,
):
  """Ledger fields nominate recovery; a public proof alone authorizes it."""
  calls = []
  monkeypatch.setattr(
    github_contributions,
    "_assert_pending_equivalence_preflight",
    lambda record: calls.append(record) or "exact_tree",
  )
  record = {"id": "exact-retry"}
  public = github_contributions.PublicReconciliation(
    head_repository="octocat/app-demo",
    pr_url="https://github.com/mobius-os/app-demo/pull/42",
    pr_number=42,
    publication_stage="draft",
  )
  monkeypatch.setattr(
    github_contributions,
    "_authoritative_public_reconciliation",
    lambda _record: public,
  )

  assert github_contributions._assert_pending_equivalence_before_publication(
    record,
  ) == "public_reconciliation"
  assert calls == []

  monkeypatch.setattr(
    github_contributions,
    "_authoritative_public_reconciliation",
    lambda _record: None,
  )
  assert github_contributions._assert_pending_equivalence_before_publication(
    record,
  ) == "exact_tree"
  assert calls == [record]


@pytest.mark.parametrize("phase", ["branch_published", "pr_ambiguous", "complete"])
@pytest.mark.parametrize("shape", ["ordinary", "stack", "successor"])
def test_postmutation_receipt_rechecks_reverted_source_when_public_head_is_gone(
  tmp_path, monkeypatch, phase, shape,
):
  """A signed effect is not provenance after GitHub loses the reviewed head."""
  app_id = 41
  record_id = f"{shape}-{phase}"
  plan = {
    "action": "pr" if shape == "ordinary" else "pr_update",
    "repo": "mobius-os/app-demo",
    "branch": f"fix/{record_id}",
    "head_sha": "a" * 40,
    "diff_sha256": "b" * 64,
    "title": "Reviewed",
    "body_draft": "Reviewed body.",
  }
  if shape == "stack":
    plan["stack"] = {
      "id": "reviewed-stack", "position": 2, "total": 2,
      "base_branch": "fix/parent",
    }
  elif shape == "successor":
    plan["successor"] = {
      "old_head_sha": "c" * 40,
      "old_base_branch": "fix/parent",
      "old_base_sha": "d" * 40,
      "base_branch": "main",
    }
  record = {
    "id": record_id,
    "type": "pr",
    "status": "submitting",
    "repo": "mobius-os/app-demo",
    "branch": plan["branch"],
    "number": 58,
    "head_repository": "octocat/app-demo",
    "submitter": "contribute-update-button",
    "plan": plan,
  }
  record["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256(record)
  )
  record_path = tmp_path / f"{record_id}.json"
  record_path.write_text(json.dumps(record))
  owner = github_routes._PersonalAttemptOwner(
    app_id=app_id,
    record_id=record_id,
    record_path=record_path,
    claimed=record,
    action_input={"action": f"update_{shape}"},
  )
  owner.event(phase, {"action": "push", "head_sha": "a" * 40}, {
    "last_submit_stage": "pushed",
    "last_submit_push_sha": "a" * 40,
    "head_repository": "octocat/app-demo",
  })
  monkeypatch.setattr(
    github_contributions, "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: None,
  )
  source_checks = []

  def reverted_source(candidate):
    source_checks.append(candidate["id"])
    raise ContributionSubmitError(
      "The installed source reverted.", code="source_provenance_mismatch",
    )

  monkeypatch.setattr(
    github_contributions, "_assert_pending_equivalence_preflight",
    reverted_source,
  )

  with pytest.raises(ContributionSubmitError) as caught:
    github_routes._assert_personal_publication_source(record, owner)

  assert caught.value.code == "source_provenance_mismatch"
  assert source_checks == [record_id]
  owner.settle()


def test_autopilot_publication_identity_survives_lease_rotation_but_not_drift():
  """Run ids authorize DB work; immutable target/content authorize replay."""
  class Row:
    target_repo = "mobius-os/app-demo"
    target_pr_number = 58
    target_head_repository = "octocat/app-demo"
    target_branch = "fix/autopilot-prior"

  class Body:
    run_id = "old-run"
    head_sha = "a" * 40
    diff_sha256 = "b" * 64

  first = github_routes._autopilot_update_action_input(Row(), Body())
  Body.run_id = "new-run"
  assert github_routes._autopilot_update_action_input(Row(), Body()) == first
  assert "run_id" not in first

  Body.diff_sha256 = "c" * 64
  assert github_routes._autopilot_update_action_input(Row(), Body()) != first
  Body.diff_sha256 = "b" * 64
  Row.target_branch = "fix/different"
  assert github_routes._autopilot_update_action_input(Row(), Body()) != first


@pytest.mark.parametrize("phase", ["armed", "normalizing", "push_pending"])
def test_autopilot_pre_effect_receipt_is_replayable_across_run_ids_only_exactly(
  tmp_path, phase,
):
  """A lease rotation neither strands nor broadens a signed publication."""
  record = {
    "id": f"autopilot-{phase}", "type": "pr", "status": "open",
    "repo": "mobius-os/app-demo", "branch": "fix/autopilot",
    "number": 58, "head_repository": "octocat/app-demo",
    "plan": {
      "action": "pr", "repo": "mobius-os/app-demo",
      "branch": "fix/autopilot", "head_sha": "a" * 40,
      "diff_sha256": "b" * 64,
    },
  }
  record["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256(record)
  )
  record_path = tmp_path / f"{record['id']}.json"
  record_path.write_text(json.dumps(record))

  class Row:
    target_repo = record["repo"]
    target_pr_number = 58
    target_head_repository = record["head_repository"]
    target_branch = record["branch"]

  class Body:
    run_id = "old-run"
    head_sha = "a" * 40
    diff_sha256 = "b" * 64

  action = github_routes._autopilot_update_action_input(Row(), Body())
  legacy_action = {**action, "run_id": "old-run"}
  old = github_routes._PersonalAttemptOwner(
    app_id=42, record_id=record["id"], record_path=record_path,
    claimed=record, action_input=legacy_action,
  )
  if phase == "armed":
    old.arm_claim()
  else:
    old.event(phase, {"action": phase}, {})

  Body.run_id = "new-run"
  assert github_routes._autopilot_update_action_input(Row(), Body()) == action
  resumed = github_routes._PersonalAttemptOwner(
    app_id=42, record_id=record["id"], record_path=record_path,
    claimed=record, action_input=action,
  )
  resumed.arm_claim()
  assert resumed.replay_phase() == phase

  Body.diff_sha256 = "c" * 64
  drifted = github_routes._PersonalAttemptOwner(
    app_id=42, record_id=record["id"], record_path=record_path,
    claimed=record,
    action_input=github_routes._autopilot_update_action_input(Row(), Body()),
  )
  with pytest.raises(
    ContributionSubmitError, match="recovery receipt|different publication",
  ):
    drifted.arm_claim()
  resumed.settle()


def test_signed_personal_attempt_retains_exact_outcome_on_record_drift(
  tmp_path,
):
  """A public result is signed beside, never merged into, a changed plan."""
  path = tmp_path / "attempt.json"
  record = {
    "id": "attempt",
    "type": "pr",
    "status": "submitting",
    "repo": "mobius-os/app-demo",
    "branch": "fix/attempt",
    "submitter": "contribute-button",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "branch": "fix/attempt",
      "head_sha": "a" * 40,
      "diff_sha256": "b" * 64,
      "title": "Reviewed",
      "body_draft": "Exact body",
    },
    "quality_review": {
      "state": "all_clear", "reviewed_head_sha": "a" * 40,
    },
  }
  record["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256(record)
  )
  path.write_text(json.dumps(record))
  owner = github_routes._PersonalAttemptOwner(
    app_id=7,
    record_id="attempt",
    record_path=path,
    claimed=record,
    action_input={"action": "submit", "publication_stage": "draft"},
  )
  request = {
    "action": "push", "repo": "mobius-os/app-demo",
    "head_repository": "octocat/app-demo", "branch": "fix/attempt",
    "head_sha": "a" * 40, "base_branch": "main",
  }
  owner.event("armed", request, {})
  assert owner.permits_exact_replay() is True

  drifted = json.loads(path.read_text())
  drifted["plan"] = {**drifted["plan"], "title": "Unreviewed replacement"}
  path.write_text(json.dumps(drifted))
  pushed_patch = {
    "last_submit_stage": "pushed",
    "last_submit_push_sha": "a" * 40,
    "head_repository": "octocat/app-demo",
  }
  with pytest.raises(ContributionSubmitError) as caught:
    owner.event("branch_published", request, pushed_patch)

  assert caught.value.code == "publication_claim_changed"
  assert json.loads(path.read_text())["plan"]["title"] == "Unreviewed replacement"
  receipt = github_routes._read_personal_attempt(
    path, app_id=7, record_id="attempt",
  )
  assert receipt["phase"] == "branch_published"
  assert receipt["record_patch"] == pushed_patch
  assert receipt["claim_input"]["plan"]["title"] == "Reviewed"


def test_signed_personal_attempt_replays_derived_patch_and_rejects_forgery(
  tmp_path,
):
  """An exact retry survives its truthful patch; hostile edits void the HMAC."""
  path = tmp_path / "retry.json"
  record = {
    "id": "retry", "type": "pr", "status": "submitting",
    "repo": "mobius-os/app-demo", "branch": "fix/retry",
    "submitter": "contribute-button",
    "plan": {
      "action": "pr", "repo": "mobius-os/app-demo",
      "branch": "fix/retry", "head_sha": "a" * 40,
      "diff_sha256": "b" * 64, "title": "Reviewed",
      "body_draft": "Exact body",
    },
    "quality_review": {
      "state": "all_clear", "reviewed_head_sha": "a" * 40,
    },
  }
  record["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256(record)
  )
  path.write_text(json.dumps(record))
  action = {"action": "submit", "publication_stage": "draft"}
  owner = github_routes._PersonalAttemptOwner(
    app_id=8, record_id="retry", record_path=path,
    claimed=record, action_input=action,
  )
  patch = {"head_repository": "octocat/app-demo"}
  owner.event("armed", {
    "action": "push", "head_sha": "a" * 40,
  }, patch)

  retried = {**record, **patch}
  retried["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256(retried)
  )
  path.write_text(json.dumps(retried))
  retry_owner = github_routes._PersonalAttemptOwner(
    app_id=8, record_id="retry", record_path=path,
    claimed=retried, action_input=action,
  )
  assert retry_owner.permits_exact_replay() is True

  receipt_path = github_routes.contribution_runtime.personal_attempt_path(
    8, "retry",
  )
  forged = json.loads(receipt_path.read_text())
  forged["effective_request"]["head_sha"] = "c" * 40
  receipt_path.write_text(json.dumps(forged))
  assert github_routes._read_personal_attempt(
    path, app_id=8, record_id="retry",
  ) is None
  assert retry_owner.permits_exact_replay() is False


def test_signed_personal_attempt_cleans_only_after_matching_durable_write(
  tmp_path,
):
  path = tmp_path / "complete.json"
  record = {
    "id": "complete", "type": "pr", "status": "submitting",
    "repo": "mobius-os/app-demo", "branch": "fix/complete",
    "submitter": "contribute-button",
    "plan": {
      "action": "pr", "repo": "mobius-os/app-demo",
      "branch": "fix/complete", "head_sha": "a" * 40,
      "diff_sha256": "b" * 64, "title": "Reviewed",
      "body_draft": "Exact body",
    },
    "quality_review": {
      "state": "all_clear", "reviewed_head_sha": "a" * 40,
    },
  }
  record["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256(record)
  )
  path.write_text(json.dumps(record))
  owner = github_routes._PersonalAttemptOwner(
    app_id=9, record_id="complete", record_path=path, claimed=record,
    action_input={"action": "submit", "publication_stage": "draft"},
  )
  owner.event("complete", {
    "action": "reconcile_pr", "url": "https://github.com/x/y/pull/1",
  }, {"last_submit_push_sha": "a" * 40})
  receipt_path = github_routes.contribution_runtime.personal_attempt_path(
    9, "complete",
  )
  assert receipt_path.is_file()
  owner.assert_current()
  path.write_text(json.dumps({**record, "status": "draft"}))
  owner.settle()
  assert not receipt_path.exists()


@pytest.mark.parametrize(
  ("plan_action", "submitter", "action_input"),
  [
    ("pr", "contribute-button", {
      "action": "submit", "publication_stage": "draft", "autopilot": False,
    }),
    ("pr_update", "contribute-update-button", {"action": "update_existing"}),
  ],
)
def test_preclaim_receipt_recovers_crash_before_ledger_transition(
  tmp_path, plan_action, submitter, action_input,
):
  """The receipt binds prepared and submitting inputs across the write gap."""
  path = tmp_path / f"preclaim-{plan_action}.json"
  prepared = {
    "id": f"preclaim-{plan_action}", "type": "pr", "status": "prepared",
    "repo": "mobius-os/app-demo", "branch": "fix/preclaim",
    "plan": {"action": plan_action, "repo": "mobius-os/app-demo",
             "branch": "fix/preclaim", "head_sha": "a" * 40},
  }
  claimed = {
    **prepared, "status": "submitting", "submitter": submitter,
    "submit_started_at": "2026-09-01T00:00:00Z",
  }
  claimed["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256(claimed)
  )
  path.write_text(json.dumps(prepared))
  owner = github_routes._PersonalAttemptOwner(
    app_id=17, record_id=prepared["id"], record_path=path,
    claimed=claimed, action_input=action_input, preclaim_record=prepared,
  )
  owner.arm_claim(preclaim=True)
  assert json.loads(path.read_text())["status"] == "prepared"
  assert github_routes._personal_resume_allowed(
    app_id=17, record_id=prepared["id"], record_path=path,
    action_input=action_input,
  ) is True
  restored = json.loads(path.read_text())
  assert restored["status"] == "submitting"
  assert restored["submitter"] == submitter


def test_complete_receipt_cleanup_precedes_new_action_identity(tmp_path):
  path = tmp_path / "complete-new-action.json"
  record = {
    "id": "complete-new-action", "type": "pr", "status": "open",
    "repo": "mobius-os/app-demo", "branch": "fix/complete",
    "url": "https://github.com/mobius-os/app-demo/pull/7", "number": 7,
    "head_repository": "octocat/app-demo", "publication_stage": "draft",
    "plan": {"action": "pr", "repo": "mobius-os/app-demo",
             "branch": "fix/complete", "head_sha": "a" * 40},
  }
  path.write_text(json.dumps(record))
  claimed = {**record, "status": "submitting", "submitter": "contribute-button"}
  owner = github_routes._PersonalAttemptOwner(
    app_id=21, record_id=record["id"], record_path=path, claimed=claimed,
    action_input={"action": "submit", "publication_stage": "draft"},
  )
  with pytest.raises(ContributionSubmitError):
    owner.event("complete", {"action": "reconcile_pr"}, {
      "url": record["url"], "number": 7,
      "head_repository": record["head_repository"],
      "publication_stage": "draft",
    })
  assert github_routes._personal_resume_allowed(
    app_id=21, record_id=record["id"], record_path=path,
    action_input={"action": "update_existing"},
  ) is False
  assert not github_routes.contribution_runtime.personal_attempt_path(
    21, record["id"],
  ).exists()


def test_pre_v7_submitting_row_reopens_and_ignores_adjacent_app_receipt(
  tmp_path,
):
  """Unsigned legacy rows require fresh approval; app files cannot resume."""
  record_id = "legacy-personal-submit"
  app_id = 31
  path = tmp_path / f"{record_id}.json"
  record = {
    "id": record_id,
    "type": "pr",
    "status": "submitting",
    "submitter": "contribute-button",
    "submit_started_at": "2026-08-30T00:00:00Z",
    "personal_submit_input_sha256": "f" * 64,
    "repo": "mobius-os/app-demo",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "branch": "fix/legacy",
      "head_sha": "a" * 40,
    },
  }
  path.write_text(json.dumps(record))
  action = {
    "action": "submit", "publication_stage": "draft", "autopilot": False,
  }
  adjacent = path.with_name(f".{path.stem}.personal-submit.json")
  adjacent.write_text(json.dumps(github_routes._signed_personal_attempt({
    "version": 1,
    "app_id": app_id,
    "record_id": record_id,
    "claim_sha256": "forged-app-claim",
    "claim_input": record,
    "preclaim_input": None,
    "action_input": action,
    "phase": "branch_published",
    "effective_request": {"action": "push"},
    "record_patch": {"last_submit_stage": "pushed"},
    "updated_at": "2026-08-30T00:00:00Z",
  })))

  assert github_routes._personal_resume_allowed(
    app_id=app_id,
    record_id=record_id,
    record_path=path,
    action_input=action,
  ) is False

  restored = json.loads(path.read_text())
  assert restored["status"] == "prepared"
  assert "submitter" not in restored
  assert "submit_started_at" not in restored
  assert "personal_submit_input_sha256" not in restored
  assert adjacent.is_file()
  assert not github_routes.contribution_runtime.personal_attempt_path(
    app_id, record_id,
  ).exists()


def test_pre_v7_submitting_row_upgrades_only_exact_public_pr_recovery(
  tmp_path, monkeypatch,
):
  record_id = "legacy-public-pr"
  app_id = 32
  path = tmp_path / f"{record_id}.json"
  head = "a" * 40
  record = {
    "id": record_id,
    "type": "pr",
    "status": "submitting",
    "submitter": "contribute-button",
    "repo": "mobius-os/app-demo",
    "branch": "fix/legacy-public",
    "last_submit_stage": "pushed",
    "last_submit_push_sha": head,
    "head_repository": "octocat/app-demo",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "branch": "fix/legacy-public",
      "head_sha": head,
    },
  }
  path.write_text(json.dumps(record))
  monkeypatch.setattr(
    github_routes,
    "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: github_contributions.PublicReconciliation(
      head_repository="octocat/app-demo",
      pr_url="https://github.com/mobius-os/app-demo/pull/91",
      pr_number=91,
      publication_stage="draft",
    ),
  )
  action = {
    "action": "submit", "publication_stage": "draft", "autopilot": False,
  }

  assert github_routes._personal_resume_allowed(
    app_id=app_id,
    record_id=record_id,
    record_path=path,
    action_input=action,
  ) is True

  assert json.loads(path.read_text())["status"] == "submitting"
  receipt = github_routes._read_personal_attempt(
    path, app_id=app_id, record_id=record_id,
  )
  assert receipt["phase"] == "pr_ambiguous"
  assert receipt["record_patch"]["url"].endswith("/pull/91")
  assert receipt["record_patch"]["last_submit_push_sha"] == head


def test_pre_v7_pr_update_upgrades_only_when_reviewed_text_is_already_live(
  tmp_path, monkeypatch,
):
  record_id = "legacy-public-update"
  app_id = 33
  path = tmp_path / f"{record_id}.json"
  head = "a" * 40
  record = {
    "id": record_id,
    "type": "pr",
    "status": "submitting",
    "submitter": "contribute-update-button",
    "repo": "mobius-os/app-demo",
    "branch": "fix/legacy-update",
    "number": 92,
    "url": "https://github.com/mobius-os/app-demo/pull/92",
    "last_submit_stage": "pushed",
    "last_submit_push_sha": head,
    "head_repository": "octocat/app-demo",
    "plan": {
      "action": "pr_update",
      "repo": "mobius-os/app-demo",
      "branch": "fix/legacy-update",
      "head_sha": head,
      "title": "Desired title",
      "body_draft": "Desired body.",
      "pr_metadata": {
        "old_title": "Desired title", "old_body": "Desired body.",
      },
    },
  }
  path.write_text(json.dumps(record))
  monkeypatch.setattr(
    github_routes,
    "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: github_contributions.PublicReconciliation(
      head_repository="octocat/app-demo",
      pr_url=record["url"],
      pr_number=92,
      publication_stage="draft",
    ),
  )
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: {
      "error": None,
      "head_sha": head,
      "base_branch": "main",
      "base_sha": "b" * 40,
      "title": "Desired title",
      "body": "Desired body.",
    },
  )

  assert github_routes._personal_resume_allowed(
    app_id=app_id,
    record_id=record_id,
    record_path=path,
    action_input={"action": "update_existing"},
  ) is True
  receipt = github_routes._read_personal_attempt(
    path, app_id=app_id, record_id=record_id,
  )
  assert receipt["phase"] == "pr_ambiguous"


def test_stack_cleanup_precedes_complete_receipt_action_comparison(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_ids = ["stale-complete-stack-01", "stale-complete-stack-02"]
  for index, record_id in enumerate(record_ids, 1):
    record = {
      "id": record_id, "type": "pr", "status": "prepared",
      "repo": "mobius-os/app-demo", "branch": f"stack/stale/0{index}",
      "url": f"https://github.com/mobius-os/app-demo/pull/{index}",
      "number": index, "head_repository": "octocat/app-demo",
      "publication_stage": "draft",
      "plan": {"action": "pr", "repo": "mobius-os/app-demo",
               "branch": f"stack/stale/0{index}", "head_sha": "a" * 40},
    }
    _write_contribution(app_id, record_id, record, "reviewed")
    path, _ = github_routes._record_paths(app_id, record_id)
    owner = github_routes._PersonalAttemptOwner(
      app_id=app_id, record_id=record_id, record_path=path, claimed=record,
      action_input={"action": "old_standalone_action"},
    )
    owner.event("complete", {"action": "old"}, {
      "url": record["url"], "number": index,
      "head_repository": record["head_repository"],
      "publication_stage": "draft",
    })

  def observe_cleanup(**_kwargs):
    for record_id in record_ids:
      path, _ = github_routes._record_paths(app_id, record_id)
      assert not github_routes.contribution_runtime.personal_attempt_path(
        app_id, record_id,
      ).exists()
    raise HTTPException(status_code=418, detail="cleanup observed")

  monkeypatch.setattr(github_routes, "_claim_stack_records", observe_cleanup)
  response = client.post(
    f"/api/github/contributions/{app_id}/submit-stack",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"record_ids": record_ids, "publication_stage": "draft"},
  )
  assert response.status_code == 418, response.text
  assert response.json()["detail"] == "cleanup observed"


def test_autopilot_claim_cleans_complete_receipt_before_new_action(tmp_path):
  path = tmp_path / "autopilot-complete.json"
  record = {
    "id": "autopilot-complete", "type": "pr", "status": "open",
    "repo": "mobius-os/app-demo", "branch": "fix/autopilot",
    "url": "https://github.com/mobius-os/app-demo/pull/9", "number": 9,
    "head_repository": "octocat/app-demo", "publication_stage": "draft",
    "plan": {"action": "pr", "repo": "mobius-os/app-demo",
             "branch": "fix/autopilot", "head_sha": "a" * 40},
  }
  record["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256(record)
  )
  path.write_text(json.dumps(record))
  old_owner = github_routes._PersonalAttemptOwner(
    app_id=22, record_id=record["id"], record_path=path, claimed=record,
    action_input={"action": "update_existing"},
  )
  old_owner.event("complete", {"action": "old"}, {
    "url": record["url"], "number": 9,
    "head_repository": record["head_repository"],
    "publication_stage": "draft",
  })

  action = {
    "action": "autopilot_update", "run_id": "round-1",
    "head_sha": "a" * 40, "diff_sha256": "b" * 64,
  }
  new_owner = github_routes._PersonalAttemptOwner(
    app_id=22, record_id=record["id"], record_path=path, claimed=record,
    action_input=action,
  )
  new_owner.arm_claim()
  receipt = github_routes._read_personal_attempt(
    path, app_id=22, record_id=record["id"],
  )
  assert receipt["phase"] == "armed"
  assert receipt["action_input"] == action


@pytest.mark.parametrize("phase", ["branch_published", "pr_ambiguous", "complete"])
def test_submit_route_resumes_signed_public_phase_without_republishing(
  client, owner_token, monkeypatch, phase,
):
  """A restart consumes the receipt and settles only an exact recovered PR."""
  _write_token(login="octocat")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = f"route-restart-{phase}"
  head = "a" * 40
  repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  record = {
    "id": record_id, "type": "pr", "status": "submitting",
    "repo": "mobius-os/app-demo", "branch": "fix/restart",
    "submitter": "contribute-button",
    "quality_review": _all_clear_review(head),
    "plan": {
      "action": "pr", "repo": "mobius-os/app-demo",
      "repo_path": str(repo), "branch": "fix/restart",
      "base_sha": "b" * 40, "head_sha": head,
      "diff_sha256": "c" * 64, "title": "Restart safe",
      "body_draft": "Exact body",
    },
  }
  record["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256(record)
  )
  _write_contribution(app_id, record_id, record, "reviewed")
  record_path = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "contributions" / f"{record_id}.json"
  )
  action = {
    "action": "submit", "publication_stage": "draft", "autopilot": False,
  }
  owner = github_routes._PersonalAttemptOwner(
    app_id=app_id, record_id=record_id, record_path=record_path,
    claimed=record, action_input=action,
  )
  normalized_head = "d" * 40
  normalized_plan = {
    **record["plan"], "head_sha": normalized_head,
    "attribution_normalized_from": head,
  }
  patch = {
    "plan": normalized_plan, "head_sha": normalized_head,
    "last_submit_stage": "pushed", "last_submit_push_sha": normalized_head,
    "head_repository": "octocat/app-demo",
  }
  owner.event(phase, {
    "action": "reconcile_pr", "head_sha": normalized_head,
    "head_repository": "octocat/app-demo", "branch": "fix/restart",
  }, patch)

  seen = []
  monkeypatch.setattr(
    github_routes, "_assert_personal_publication_source", lambda *_args: "exact",
  )
  monkeypatch.setattr(github_routes, "_equivalence_source_repo", lambda _r: None)
  monkeypatch.setattr(
    github_routes, "_record_pending_equivalence_locked",
    lambda *_args, **_kwargs: None,
  )

  def reconcile(recovered, _diff, **kwargs):
    seen.append((recovered, kwargs))
    assert recovered["last_submit_push_sha"] == normalized_head
    assert recovered["plan"]["attribution_normalized_from"] == head
    assert kwargs["prior_attempt_phase"] == phase
    return (
      "https://github.com/mobius-os/app-demo/pull/91", 91,
      {**patch, "url": "https://github.com/mobius-os/app-demo/pull/91",
       "number": 91},
    )

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", reconcile)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 200, response.text
  assert len(seen) == 1
  assert response.json()["record"]["number"] == 91
  assert not github_routes.contribution_runtime.personal_attempt_path(
    app_id, record_id,
  ).exists()


def test_submit_route_armed_receipt_does_not_bypass_reverted_source(
  client, owner_token, monkeypatch,
):
  """Intent alone cannot turn a stale installed-source proof into recovery."""
  _write_token(login="octocat")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "route-armed-source-revert"
  head = "a" * 40
  repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
  (repo / ".git").mkdir(parents=True)
  record = {
    "id": record_id, "type": "pr", "status": "submitting",
    "repo": "mobius-os/app-demo", "branch": "fix/reverted",
    "submitter": "contribute-button", "quality_review": _all_clear_review(head),
    "plan": {"action": "pr", "repo": "mobius-os/app-demo",
             "repo_path": str(repo), "branch": "fix/reverted",
             "head_sha": head, "title": "Reverted", "body_draft": "Body"},
  }
  record["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256(record)
  )
  _write_contribution(app_id, record_id, record)
  record_path = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "contributions" / f"{record_id}.json"
  )
  owner = github_routes._PersonalAttemptOwner(
    app_id=app_id, record_id=record_id, record_path=record_path, claimed=record,
    action_input={"action": "submit", "publication_stage": "draft", "autopilot": False},
  )
  owner.event("armed", {"action": "push", "head_sha": head}, {})
  monkeypatch.setattr(github_routes, "_equivalence_source_repo", lambda _r: None)
  called = []
  monkeypatch.setattr(
    github_routes, "_assert_pending_equivalence_before_publication",
    lambda _r: (_ for _ in ()).throw(
      ContributionSubmitError("installed source reverted", code="source_provenance_mismatch")
    ),
  )
  monkeypatch.setattr(
    github_routes, "_submit_prepared_pr", lambda *_a, **_k: called.append(True),
  )
  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 409
  assert not called
  # A definite local rejection durably returns the record to prepared and may
  # then clear its still-armed (never-public) receipt.
  assert not github_routes.contribution_runtime.personal_attempt_path(
    app_id, record_id,
  ).exists()


def test_update_route_resumes_signed_branch_without_repeating_publication(
  client, owner_token, monkeypatch,
):
  """An ordinary PR update resumes its exact published head after restart."""
  _write_token(login="octocat")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "route-update-restart"
  record = _prepared_existing_pr_update(app_id, record_id)
  record.update({"status": "submitting", "submitter": "contribute-update-button"})
  record["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256(record)
  )
  _write_contribution(app_id, record_id, record, "reviewed")
  record_path = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "contributions" / f"{record_id}.json"
  )
  head = record["plan"]["head_sha"]
  owner = github_routes._PersonalAttemptOwner(
    app_id=app_id, record_id=record_id, record_path=record_path, claimed=record,
    action_input={"action": "update_existing"},
  )
  patch = {
    "last_submit_stage": "pushed", "last_submit_push_sha": head,
    "head_repository": record["head_repository"],
  }
  owner.event("branch_published", {
    "action": "reconcile_pr", "head_sha": head,
  }, patch)
  monkeypatch.setattr(github_routes, "_equivalence_source_repo", lambda _r: None)
  monkeypatch.setattr(
    github_routes, "_assert_personal_publication_source", lambda *_a: "exact",
  )
  monkeypatch.setattr(
    github_routes, "_autopilot_live_target",
    lambda *_a, **_k: {
      "head_sha": "b" * 40, "base_branch": "main", "error": "",
      "title": record["plan"]["pr_metadata"]["old_title"],
      "body": record["plan"]["pr_metadata"]["old_body"],
    },
  )
  monkeypatch.setattr(
    github_routes, "_assert_reviewed_update_contains_live_head",
    lambda *_a, **_k: None,
  )
  calls = []

  def reconcile(recovered, _diff, **kwargs):
    calls.append(kwargs)
    assert recovered["last_submit_push_sha"] == head
    assert kwargs["prior_attempt_phase"] == "branch_published"
    return record["url"], record["number"], patch

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", reconcile)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update-existing",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 200, response.text
  assert len(calls) == 1
  assert not github_routes.contribution_runtime.personal_attempt_path(
    app_id, record_id,
  ).exists()


def test_submit_stack_rejects_forged_receipt_for_submitting_layer(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, _ = _app_token(client, owner_token, github_access=True)
  stack_id = "forged-restart-stack"
  ids = ["forged-restart-01", "forged-restart-02"]
  heads = ["a" * 40, "c" * 40]
  records = []
  for index, record_id in enumerate(ids):
    base = "b" * 40 if index == 0 else heads[0]
    branch = f"stack/{stack_id}/0{index + 1}-layer"
    repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
    (repo / ".git").mkdir(parents=True)
    record = {
      "id": record_id, "type": "pr", "status": "submitting",
      "repo": "mobius-os/mobius", "branch": branch,
      "submitter": "contribute-stack-button",
      "quality_review": _all_clear_review(heads[index]),
      "plan": {
        "action": "pr", "repo": "mobius-os/mobius", "repo_path": str(repo),
        "branch": branch, "base_sha": base, "head_sha": heads[index],
        "diff_sha256": "d" * 64, "title": f"Layer {index + 1}",
        "body_draft": "Body", "stack": {
          "id": stack_id, "position": index + 1, "total": 2,
          "parent_record_id": "" if index == 0 else ids[0],
          "base_branch": "main" if index == 0 else f"stack/{stack_id}/01-layer",
        },
      },
    }
    record["personal_submit_input_sha256"] = (
      github_contributions._personal_publication_input_sha256(record)
    )
    _write_contribution(app_id, record_id, record, "reviewed")
    records.append(record)
  action = {"action": "submit_stack", "record_ids": ids, "publication_stage": "draft"}
  for record in records:
    path = (
      Path(get_settings().data_dir) / "apps" / str(app_id)
      / "contributions" / f"{record['id']}.json"
    )
    owner = github_routes._PersonalAttemptOwner(
      app_id=app_id, record_id=record["id"], record_path=path,
      claimed=record, action_input=action,
    )
    owner.event("armed", {"action": "claim"}, {})
  forged_path = github_routes.contribution_runtime.personal_attempt_path(
    app_id, ids[1],
  )
  forged = json.loads(forged_path.read_text())
  forged["effective_request"]["action"] = "forged"
  forged_path.write_text(json.dumps(forged))
  called = []
  monkeypatch.setattr(
    github_routes, "_preflight_prepared_stack", lambda *_a, **_k: called.append(True),
  )
  response = client.post(
    f"/api/github/contributions/{app_id}/submit-stack",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"record_ids": ids, "publication_stage": "draft"},
  )
  assert response.status_code == 409
  assert not called


def test_public_reconciliation_requires_exact_github_branch_or_pr(
  tmp_path, monkeypatch,
):
  """A forged post-push journal is inert unless GitHub proves its exact tip."""
  _write_token(login="octocat", user_id=42)
  repo = tmp_path / "review"
  (repo / ".git").mkdir(parents=True)
  head = "a" * 40
  record = {
    "id": "forged-recovery",
    "repo": "mobius-os/app-demo",
    "branch": "fix/exact-retry",
    "last_submit_stage": "pushed",
    "last_submit_push_sha": head,
    "last_submit_upstream_branch": "main",
    "head_repository": "octocat/app-demo",
    "plan": {
      "repo": "mobius-os/app-demo",
      "repo_path": str(repo),
      "branch": "fix/exact-retry",
      "head_sha": head,
    },
  }
  monkeypatch.setattr(
    github_contributions, "_safe_repo_path", lambda _raw: repo,
  )
  monkeypatch.setattr(
    github_contributions, "_find_existing_pr", lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_contributions, "_existing_branch_pr", lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda *_args, **_kwargs: "b" * 40,
  )

  assert github_contributions._authoritative_public_reconciliation(
    record,
  ) is None

  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda _repo, slug, branch: (
      head if (slug, branch) == ("octocat/app-demo", "fix/exact-retry") else None
    ),
  )
  branch_recovery = github_contributions._authoritative_public_reconciliation(
    record,
  )
  assert branch_recovery == github_contributions.PublicReconciliation(
    head_repository="octocat/app-demo",
  )

  url = "https://github.com/mobius-os/app-demo/pull/42"
  monkeypatch.setattr(
    github_contributions, "_find_existing_pr",
    lambda *_args, **_kwargs: url,
  )
  monkeypatch.setattr(
    github_contributions, "_confirm_existing_pr_update",
    lambda *_args, **_kwargs: (url, "draft"),
  )
  pr_recovery = github_contributions._authoritative_public_reconciliation(
    record,
  )
  assert pr_recovery == github_contributions.PublicReconciliation(
    head_repository="octocat/app-demo",
    pr_url=url,
    pr_number=42,
    publication_stage="draft",
  )


def test_rejected_push_journal_cannot_bypass_source_recheck_on_retry(
  client, owner_token, monkeypatch,
):
  """A pre-push SHA patch is not a post-push recovery authorization."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, _diff_text = _prepared_real_review(
    app_id, "rejected-push-retry",
  )
  calls = []

  def reject(record_arg, _diff_path, **_kwargs):
    calls.append(record_arg["id"])
    raise ContributionSubmitError(
      "GitHub rejected this push.",
      record_patch={"last_submit_push_sha": record["plan"]["head_sha"]},
    )

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", reject)
  headers = {"Authorization": f"Bearer {app_token}"}
  first = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/submit",
    headers=headers,
    json={"publication_stage": "draft"},
  )
  assert first.status_code == 409, first.text
  assert first.json()["detail"]["record"]["last_submit_push_sha"] == (
    record["plan"]["head_sha"]
  )
  assert "last_submit_stage" not in first.json()["detail"]["record"]

  _remove_reviewed_change_from_source(record)
  second = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/submit",
    headers=headers,
    json={"publication_stage": "draft"},
  )

  assert second.status_code == 409, second.text
  assert second.json()["detail"]["code"] == "source_provenance_mismatch"
  assert calls == [record["id"]]


def test_existing_pr_update_rechecks_current_source_before_push(
  client, owner_token, monkeypatch,
):
  """Update PR cannot bypass provenance after its source has reverted."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, diff_text = _prepared_real_review(app_id, "update-source-moved")
  record.update({
    "number": 58,
    "url": "https://github.com/mobius-os/app-demo/pull/58",
    "head_repository": "octocat/app-demo",
    "submitted_at": "2026-08-30T12:00:00Z",
  })
  record["plan"]["action"] = "pr_update"
  record["plan"]["title"] = record["title"]
  record["plan"]["body_draft"] = "Reviewed fix body."
  record["plan"]["pr_metadata"] = {
    "old_title": record["plan"]["title"],
    "old_body": record["plan"]["body_draft"],
  }
  _write_contribution(app_id, record["id"], record, diff_text)
  _remove_reviewed_change_from_source(record)
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: {
      "error": None,
      "head_sha": record["plan"]["base_sha"],
      "base_branch": "main",
      "title": record["plan"]["pr_metadata"]["old_title"],
      "body": record["plan"]["pr_metadata"]["old_body"],
    },
  )
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail("reverted source must never update"),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/update-existing",
    headers={"Authorization": f"Bearer {app_token}"},
    json={},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "source_provenance_mismatch"


def test_autopilot_update_rechecks_current_source_before_push(
  client, owner_token, monkeypatch,
):
  """A follow-up round cannot publish a head absent from installed source."""
  from app import contribution_autopilot
  from app.database import SessionLocal

  _write_token(login="octocat", user_id=42)
  app_id, _app_token_value = _app_token(
    client, owner_token, github_access=True,
  )
  repo, record, diff_text = _prepared_real_review(app_id, "autopilot-source-moved")
  record.update({
    "status": "open",
    "number": 58,
    "url": "https://github.com/mobius-os/app-demo/pull/58",
    "head_repository": "octocat/app-demo",
  })
  record["plan"]["action"] = "pr_update"
  record["plan"]["pr_metadata"] = {
    "old_title": record["plan"]["title"],
    "old_body": record["plan"]["body_draft"],
  }
  _write_contribution(app_id, record["id"], record, diff_text)
  session = SessionLocal()
  try:
    contribution_autopilot.stamp_grant(
      session,
      app_id,
      record["id"],
      head_sha=record["plan"]["head_sha"],
      target_repo=record["repo"],
      target_pr_number=58,
      target_head_repository=record["head_repository"],
      target_branch=record["branch"],
      target_repo_path=str(repo.resolve()),
    )
    verdict = contribution_autopilot.claim_for_round(
      session,
      app_id,
      record["id"],
      attention_key="review:source-moved",
      event_at="2026-08-31T12:00:00Z",
    )
    run_id = verdict["run_id"]
  finally:
    session.close()
  _remove_reviewed_change_from_source(record)
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: {
      "error": None,
      "head_sha": record["plan"]["base_sha"],
      "base_branch": "main",
      "base_sha": record["plan"]["base_sha"],
      "title": record["title"],
      "body": "Reviewed fix body.",
    },
  )
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail("reverted source must never update"),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/update",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={
      "run_id": run_id,
      "head_sha": record["plan"]["head_sha"],
      "diff_sha256": record["plan"]["diff_sha256"],
    },
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "source_provenance_mismatch"
  assert not github_routes.contribution_runtime.personal_attempt_path(
    app_id, record["id"],
  ).exists()


def test_autopilot_update_reconciles_exact_ambiguous_push_after_source_moves(
  client, owner_token, monkeypatch,
):
  """A lost push response saves its head and retries without orphaning the PR."""
  from app import contribution_autopilot
  from app.database import SessionLocal

  _write_token(login="octocat", user_id=42)
  app_id, _app_token_value = _app_token(
    client, owner_token, github_access=True,
  )
  repo, record, diff_text = _prepared_real_review(
    app_id, "autopilot-ambiguous-retry",
  )
  record.update({
    "status": "open",
    "number": 58,
    "url": "https://github.com/mobius-os/app-demo/pull/58",
    "head_repository": "octocat/app-demo",
  })
  record["plan"]["action"] = "pr_update"
  record["plan"]["pr_metadata"] = {
    "old_title": record["plan"]["title"],
    "old_body": record["plan"]["body_draft"],
  }
  _write_contribution(app_id, record["id"], record, diff_text)
  session = SessionLocal()
  try:
    contribution_autopilot.stamp_grant(
      session,
      app_id,
      record["id"],
      head_sha=record["plan"]["head_sha"],
      target_repo=record["repo"],
      target_pr_number=58,
      target_head_repository=record["head_repository"],
      target_branch=record["branch"],
      target_repo_path=str(repo.resolve()),
    )
    verdict = contribution_autopilot.claim_for_round(
      session,
      app_id,
      record["id"],
      attention_key="review:ambiguous-push",
      event_at="2026-08-31T12:30:00Z",
    )
    run_id = verdict["run_id"]
  finally:
    session.close()
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: {
      "error": None,
      "head_sha": record["plan"]["head_sha"],
      "base_branch": "main",
      "base_sha": record["plan"]["base_sha"],
      "title": record["title"],
      "body": "Reviewed fix body.",
    },
  )
  monkeypatch.setattr(
    github_contributions,
    "_authoritative_public_reconciliation",
    lambda candidate, **_kwargs: (
      github_contributions.PublicReconciliation(
        head_repository="octocat/app-demo",
        pr_url=record["url"],
        pr_number=58,
        publication_stage="draft",
      )
      if candidate.get("last_submit_stage") == "pushed"
      else None
    ),
  )
  attempts = []

  def submit(_record, _diff_path, **_kwargs):
    attempts.append(_record.get("last_submit_push_sha"))
    if len(attempts) == 1:
      patch = {
        "last_submit_push_sha": record["plan"]["head_sha"],
        "last_submit_stage": "pushed",
      }
      _kwargs["attempt_event"](
        "branch_published",
        {"action": "push", "head_sha": record["plan"]["head_sha"]},
        patch,
      )
      raise ContributionSubmitError(
        "GitHub did not confirm the branch update.",
        status_code=503,
        code="update_unconfirmed",
        record_patch=patch,
      )
    return (
      record["url"],
      58,
      {"last_submit_push_sha": record["plan"]["head_sha"]},
    )

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", submit)
  request_body = {
    "run_id": run_id,
    "head_sha": record["plan"]["head_sha"],
    "diff_sha256": record["plan"]["diff_sha256"],
  }
  first = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/update",
    headers={"Authorization": f"Bearer {owner_token}"},
    json=request_body,
  )

  assert first.status_code == 503, first.text
  saved = json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id)
     / "contributions" / f"{record['id']}.json").read_text()
  )
  assert saved["last_submit_push_sha"] == record["plan"]["head_sha"]

  _remove_reviewed_change_from_source(record)
  second = client.post(
    f"/api/github/contributions/{app_id}/{record['id']}/update",
    headers={"Authorization": f"Bearer {owner_token}"},
    json=request_body,
  )

  assert second.status_code == 200, second.text
  assert attempts == [None, record["plan"]["head_sha"]]


def test_review_status_rejects_an_ambiguous_conflict_projection(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  data_dir = Path(get_settings().data_dir)
  record_id = "review-resolved-projection"
  review = data_dir / "contrib" / record_id / "worktree"
  source = data_dir / "apps" / f"{record_id}-source"
  for repo in (review, source):
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "config", "user.name", "octocat"], cwd=repo,
                   check=True)
    subprocess.run([
      "git", "config", "user.email", "42+octocat@users.noreply.github.com",
    ], cwd=repo, check=True)

  (review / "index.jsx").write_text("base\n")
  (review / "stable.js").write_text("base\n")
  subprocess.run(["git", "add", "index.jsx", "stable.js"], cwd=review,
                 check=True)
  subprocess.run(["git", "commit", "-m", "base"], cwd=review, check=True,
                 capture_output=True)
  base = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=review, text=True,
  ).strip()
  subprocess.run(["git", "checkout", "-b", "fix/resolved"], cwd=review,
                 check=True, capture_output=True)
  (review / "index.jsx").write_text("reviewed\n")
  (review / "stable.js").write_text("reviewed\n")
  subprocess.run(["git", "add", "index.jsx", "stable.js"], cwd=review,
                 check=True)
  subprocess.run([
    "git", "commit", "-m", "reviewed projection", "-m",
    "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>",
  ], cwd=review, check=True, capture_output=True)
  head = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=review, text=True,
  ).strip()
  diff_text = subprocess.check_output([
    "git", "-c", "core.quotePath=false", "diff", "--no-ext-diff",
    "--no-color", "--binary", "--full-index", "--src-prefix=a/",
    "--dst-prefix=b/", f"{base}..{head}",
  ], cwd=review, text=True)

  # Matching path sets do not prove that a locally resolved conflict contains
  # the reviewed change. The installed source must fail closed.
  (source / "index.jsx").write_text("local precursor\n")
  (source / "stable.js").write_text("base\n")
  subprocess.run(["git", "add", "index.jsx", "stable.js"], cwd=source,
                 check=True)
  subprocess.run(["git", "commit", "-m", "source parent"], cwd=source,
                 check=True, capture_output=True)
  (source / "index.jsx").write_text("resolved locally\n")
  (source / "stable.js").write_text("reviewed\n")
  subprocess.run(["git", "add", "index.jsx", "stable.js"], cwd=source,
                 check=True)
  subprocess.run(["git", "commit", "-m", "source resolution"], cwd=source,
                 check=True, capture_output=True)
  source_sha = subprocess.check_output(
    ["git", "rev-parse", "HEAD"], cwd=source, text=True,
  ).strip()
  record = {
    "id": record_id,
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "prepared",
    "title": "Resolved projection",
    "branch": "fix/resolved",
    "plan": {
      "action": "pr",
      "repo": "mobius-os/app-demo",
      "branch": "fix/resolved",
      "repo_path": str(review),
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
      "source_repo_path": str(source),
      "source_sha": source_sha,
    },
  }
  _write_contribution(app_id, record_id, record, diff_text)

  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._assert_pending_equivalence_preflight(record)
  assert caught.value.code == "source_provenance_mismatch"
  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 200, response.text
  assert response.json()["needs_refresh"] == 1
  assert response.json()["records"][0]["code"] == "source_provenance_mismatch"


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


def test_repository_head_query_batches_distinct_targets_with_variables():
  query, variables, aliases = github_routes._build_repository_heads_query([
    "mobius-os/mobius",
    "mobius-os/app-contribute",
    "MOBIUS-OS/MOBIUS",
  ])

  assert aliases == {
    "repo0": "mobius-os/mobius",
    "repo1": "mobius-os/app-contribute",
  }
  assert variables == {
    "repo0o": "mobius-os", "repo0n": "mobius",
    "repo1o": "mobius-os", "repo1n": "app-contribute",
  }
  assert query.count("repository(owner:") == 2
  assert "mobius-os/mobius" not in query
  assert "pullRequest" not in query


def test_review_status_batches_publication_heads_after_releasing_all_locks(
  client, owner_token, monkeypatch,
):
  # These separately created fixtures model one repository's identical base.
  # Commit identity must not depend on finishing all three within one second.
  monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-01-01T00:00:00+00:00")
  monkeypatch.setenv("GIT_COMMITTER_DATE", "2026-01-01T00:00:00+00:00")
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  first_repo, first, first_diff = _prepared_real_review(app_id, "first-target")
  _second_repo, second, second_diff = _prepared_real_review(
    app_id, "second-target",
  )
  second["repo"] = "mobius-os/app-other"
  second["plan"]["repo"] = "mobius-os/app-other"
  _write_contribution(app_id, second["id"], second, second_diff)
  _third_repo, third, third_diff = _prepared_real_review(app_id, "same-target")
  _write_contribution(app_id, third["id"], third, third_diff)

  held = {"app": 0, "source": 0}
  real_app_lock = github_routes.fs_locks.app_storage_lock
  real_source_lock = github_routes.fs_locks.source_dir_lock

  @asynccontextmanager
  async def observed_app_lock(requested_app_id):
    async with real_app_lock(requested_app_id):
      held["app"] += 1
      try:
        yield
      finally:
        held["app"] -= 1

  @asynccontextmanager
  async def observed_source_lock(source_dir):
    async with real_source_lock(source_dir):
      held["source"] += 1
      try:
        yield
      finally:
        held["source"] -= 1

  calls = []
  baseline = checked_out_connections()

  async def repository_heads(_token, query, variables):
    assert held == {"app": 0, "source": 0}
    assert checked_out_connections() == baseline
    calls.append((query, variables))
    nodes = {}
    for index in range(2):
      repo = f"{variables[f'repo{index}o']}/{variables[f'repo{index}n']}"
      expected = (
        first["plan"]["base_sha"]
        if repo == "mobius-os/app-demo"
        else second["plan"]["base_sha"]
      )
      nodes[f"repo{index}"] = {
        "defaultBranchRef": {
          "name": "main", "target": {"oid": expected},
        },
      }
    return nodes

  monkeypatch.setattr(
    github_routes.fs_locks, "app_storage_lock", observed_app_lock,
  )
  monkeypatch.setattr(
    github_routes.fs_locks, "source_dir_lock", observed_source_lock,
  )
  monkeypatch.setattr(github_routes, "_github_graphql_json", repository_heads)

  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  assert len(calls) == 1
  assert calls[0][0].count("repository(owner:") == 2
  assert response.json()["publication"] == {
    "ready": 3, "needs_refresh": 0, "unavailable": 0,
  }
  assert {
    row["id"]: row["publication"]["state"]
    for row in response.json()["records"]
  } == {
    "first-target": "ready",
    "same-target": "ready",
    "second-target": "ready",
  }
  assert held == {"app": 0, "source": 0}
  assert first_repo.exists()


@pytest.mark.parametrize(("remote", "state", "code"), [
  ("advanced", "needs_refresh", "outdated_default_head"),
  ("missing", "needs_refresh", "target_repository_missing"),
  ("unavailable", "unavailable", "publication_unavailable"),
])
def test_review_status_reports_publication_freshness_without_changing_local_ready(
  client, owner_token, monkeypatch, remote, state, code,
):
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, _diff = _prepared_real_review(app_id, f"remote-{remote}")

  async def repository_head(_token, _query, _variables):
    if remote == "unavailable":
      return None
    if remote == "missing":
      return {"repo0": None}
    return {"repo0": {"defaultBranchRef": {
      "name": "main", "target": {"oid": "f" * 40},
    }}}

  monkeypatch.setattr(github_routes, "_github_graphql_json", repository_head)
  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  row = response.json()["records"][0]
  assert row["state"] == "ready"
  assert row["publication"]["state"] == state
  assert row["publication"]["code"] == code
  if remote == "advanced":
    assert row["publication"]["head_sha"] != record["plan"]["base_sha"]


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


def test_review_status_ignores_prepared_comment_drafts(
  client, owner_token,
):
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  record = {
    "id": "comment-draft",
    "type": "issue_comment",
    "status": "prepared",
    "repo": "mobius-os/mobius",
    "plan": {
      "action": "issue_comment",
      "repo": "mobius-os/mobius",
      "target_url": "https://github.com/mobius-os/mobius/issues/1",
      "body_draft": "Prepared feedback.",
    },
  }
  _write_contribution(app_id, record["id"], record)

  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  assert response.json()["records"] == []


def test_review_status_keeps_recent_stack_together_past_filename_scan_cap(
  client, owner_token, monkeypatch,
):
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  contribution_dir = (
    Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
  )
  contribution_dir.mkdir(parents=True, exist_ok=True)
  old_time = time.time() - 3600
  for index in range(499):
    path = contribution_dir / f"middle-{index:03d}.json"
    path.write_text(json.dumps({
      "id": f"middle-{index:03d}",
      "type": "pr",
      "status": "merged",
    }))
    os.utime(path, (old_time, old_time))

  stack_id = "recent-stack"
  parent_id = "aaa-recent-parent"
  child_id = "zzz-recent-child"
  parent_branch = f"stack/{stack_id}/01-parent"
  child_branch = f"stack/{stack_id}/02-child"
  parent_head = "b" * 40
  repo_path = str(Path(get_settings().data_dir) / "contrib" / "unused")
  common = {"type": "pr", "repo": "mobius-os/mobius", "status": "prepared"}
  parent = {
    **common,
    "id": parent_id,
    "branch": parent_branch,
    "plan": {
      "action": "pr", "repo": "mobius-os/mobius",
      "repo_path": repo_path, "branch": parent_branch,
      "base_sha": "a" * 40, "head_sha": parent_head,
      "stack": {
        "id": stack_id, "position": 1, "total": 2,
        "parent_record_id": "", "base_branch": "main",
      },
    },
  }
  child = {
    **common,
    "id": child_id,
    "branch": child_branch,
    "plan": {
      "action": "pr", "repo": "mobius-os/mobius",
      "repo_path": repo_path, "branch": child_branch,
      "base_sha": parent_head, "head_sha": "c" * 40,
      "stack": {
        "id": stack_id, "position": 2, "total": 2,
        "parent_record_id": parent_id, "base_branch": parent_branch,
      },
    },
  }
  _write_contribution(app_id, parent_id, parent)
  _write_contribution(app_id, child_id, child)

  monkeypatch.setattr(
    github_routes,
    "_inspect_prepared_review",
    lambda record, _diff_path, _github_state: {
      "id": record["id"], "state": "ready", "code": "ready",
      "message": "Still matches the exact source you reviewed.",
    },
  )
  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  assert response.json()["ready"] == 2
  assert {item["id"] for item in response.json()["records"]} == {
    parent_id, child_id,
  }


def test_review_status_accepts_reviewed_updates_for_an_existing_pr_stack(
  client, owner_token, monkeypatch,
):
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  stack_id = "existing-stack-update"
  parent_id = "existing-stack-parent"
  child_id = "existing-stack-child"
  parent_branch = f"stack/{stack_id}/01-parent"
  child_branch = f"stack/{stack_id}/02-child"
  parent_head = "b" * 40
  repo_path = str(Path(get_settings().data_dir) / "contrib" / "unused")
  common = {"type": "pr", "repo": "mobius-os/mobius", "status": "prepared"}
  parent = {
    **common,
    "id": parent_id,
    "branch": parent_branch,
    "plan": {
      "action": "pr_update", "repo": "mobius-os/mobius",
      "repo_path": repo_path, "branch": parent_branch,
      "base_sha": "a" * 40, "head_sha": parent_head,
      "stack": {
        "id": stack_id, "position": 1, "total": 2,
        "parent_record_id": "", "base_branch": "main",
      },
    },
  }
  child = {
    **common,
    "id": child_id,
    "branch": child_branch,
    "plan": {
      "action": "pr_update", "repo": "mobius-os/mobius",
      "repo_path": repo_path, "branch": child_branch,
      "base_sha": parent_head, "head_sha": "c" * 40,
      "stack": {
        "id": stack_id, "position": 2, "total": 2,
        "parent_record_id": parent_id, "base_branch": parent_branch,
      },
    },
  }
  _write_contribution(app_id, parent_id, parent)
  _write_contribution(app_id, child_id, child)
  monkeypatch.setattr(
    github_routes,
    "_inspect_prepared_review",
    lambda record, _diff_path, _github_state: {
      "id": record["id"], "state": "ready", "code": "ready",
      "message": "Still matches the exact source you reviewed.",
    },
  )

  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  assert response.json()["ready"] == 2
  # The new-stack submission validator keeps its narrower default. Existing
  # PR updates remain available only through the dedicated Update PR route.
  with pytest.raises(ContributionSubmitError):
    github_routes._validate_stack_records([parent, child])


def test_review_status_filters_hidden_journals_before_record_cap(
  client, owner_token, monkeypatch,
):
  """Private dotfiles cannot crowd a real contribution out of the snapshot."""
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  contribution_dir = (
    Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
  )
  contribution_dir.mkdir(parents=True, exist_ok=True)
  for index in range(501):
    (contribution_dir / f".{index:03d}-journal.json").write_text("{}")
  record = {
    "id": "visible-review",
    "type": "pr",
    "status": "prepared",
    "repo": "mobius-os/app-demo",
    "plan": {"action": "pr", "repo": "mobius-os/app-demo"},
  }
  visible_path = contribution_dir / "visible-review.json"
  visible_path.write_text(json.dumps(record))
  old_time = time.time() - 3600
  os.utime(visible_path, (old_time, old_time))
  real_stat = Path.stat

  def guarded_stat(path, *args, **kwargs):
    if path.parent == contribution_dir and path.name.startswith("."):
      pytest.fail("hidden journals must be filtered before stat and cap")
    return real_stat(path, *args, **kwargs)

  monkeypatch.setattr(Path, "stat", guarded_stat)
  seen = []
  async def inspect(candidate, *_args):
    seen.append(candidate["id"])
    return {"id": candidate["id"], "state": "ready"}

  monkeypatch.setattr(
    github_routes, "_inspect_prepared_review_locked", inspect,
  )

  response = client.get(
    f"/api/github/contributions/{app_id}/review-status",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  assert seen == ["visible-review"]
  assert response.json()["records"] == [
    {
      "id": "visible-review",
      "state": "ready",
      "publication": {
        "state": "unavailable",
        "code": "publication_unavailable",
        "message": "GitHub freshness could not be checked right now.",
      },
    },
  ]


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


def test_upstream_merge_preflight_retries_one_transient_fetch(tmp_path, monkeypatch):
  from app.github_contribution_git import _assert_merges_with_upstream

  repo = tmp_path / "repo"
  repo.mkdir()
  calls = []
  fetches = iter((
    _cp(stderr="fatal: unable to access: HTTP 503", returncode=1),
    _cp(),
  ))
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_default_branch",
    lambda *_args: "main",
  )

  def fake_git(_repo, *args, check=True):
    calls.append(args)
    if args[:1] == ("fetch",):
      return next(fetches)
    if args[:2] == ("rev-parse", "--verify"):
      return _cp(_UPSTREAM_SHA + "\n")
    return _cp()

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)

  patch = _assert_merges_with_upstream(
    repo, "mobius-os/app-demo", "fix/demo",
  )

  assert patch == {
    "last_submit_upstream_branch": "main",
    "last_submit_upstream_sha": _UPSTREAM_SHA,
  }
  assert sum(call[:1] == ("fetch",) for call in calls) == 2
  assert sum(call[:1] == ("merge-tree",) for call in calls) == 1


def test_upstream_merge_preflight_recovers_one_fetch_timeout(tmp_path, monkeypatch):
  from app.github_contribution_git import _assert_merges_with_upstream

  repo = tmp_path / "repo"
  repo.mkdir()
  fetch_attempts = 0
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_default_branch",
    lambda *_args: "main",
  )

  def fake_git(_repo, *args, check=True):
    nonlocal fetch_attempts
    if args[:1] == ("fetch",):
      fetch_attempts += 1
      if fetch_attempts == 1:
        raise subprocess.TimeoutExpired(["git", "fetch"], timeout=30)
      return _cp()
    if args[:2] == ("rev-parse", "--verify"):
      return _cp(_UPSTREAM_SHA + "\n")
    return _cp()

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)

  patch = _assert_merges_with_upstream(
    repo, "mobius-os/app-demo", "fix/demo",
  )

  assert patch["last_submit_upstream_sha"] == _UPSTREAM_SHA
  assert fetch_attempts == 2


def test_upstream_merge_preflight_preserves_persistent_fetch_diagnosis(
  tmp_path, monkeypatch,
):
  from app.github_contribution_git import _assert_merges_with_upstream

  repo = tmp_path / "repo"
  repo.mkdir()
  calls = []
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_default_branch",
    lambda *_args: "main",
  )

  def fake_git(_repo, *args, check=True):
    calls.append(args)
    if args[:1] == ("fetch",):
      return _cp(
        stderr="\x1b[31mfatal: Could not resolve host: github.com\x1b[0m",
        returncode=1,
      )
    return _cp()

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)

  with pytest.raises(ContributionSubmitError) as raised:
    _assert_merges_with_upstream(repo, "mobius-os/app-demo", "fix/demo")

  error = raised.value
  assert error.code == "upstream_fetch_unavailable"
  assert error.record_patch == {"last_submit_upstream_branch": "main"}
  assert error.detail == "fatal: Could not resolve host: github.com"
  assert "Try Send again" in error.message
  assert "Nothing was published" in error.message
  assert sum(call[:1] == ("fetch",) for call in calls) == 2
  assert not any(call[:1] == ("merge-tree",) for call in calls)


def test_upstream_merge_preflight_does_not_retry_deterministic_fetch_rejection(
  tmp_path, monkeypatch,
):
  from app.github_contribution_git import _assert_merges_with_upstream

  repo = tmp_path / "repo"
  repo.mkdir()
  calls = []
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_default_branch",
    lambda *_args: "main",
  )

  def fake_git(_repo, *args, check=True):
    calls.append(args)
    if args[:1] == ("fetch",):
      return _cp(stderr="fatal: repository not found", returncode=128)
    return _cp()

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)

  with pytest.raises(ContributionSubmitError) as raised:
    _assert_merges_with_upstream(repo, "mobius-os/app-demo", "fix/demo")

  assert raised.value.code == "upstream_fetch_failed"
  assert raised.value.detail == "fatal: repository not found"
  assert sum(call[:1] == ("fetch",) for call in calls) == 1


def test_merge_error_patch_keeps_transport_diagnosis():
  from app.github_contribution_git import _merge_error_patch

  original = ContributionSubmitError(
    "GitHub was temporarily unreachable.",
    record_patch={"last_submit_upstream_branch": "main"},
    code="upstream_fetch_unavailable",
    detail="fatal: Could not resolve host: github.com",
  )

  merged = _merge_error_patch(original, {"head_sha": "a" * 40})

  assert merged.code == original.code
  assert merged.detail == original.detail
  assert merged.record_patch == {
    "head_sha": "a" * 40,
    "last_submit_upstream_branch": "main",
  }


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


def test_rate_limited_transport_is_surfaced_instead_of_retried(
  tmp_path, monkeypatch,
):
  """A 429 carries Retry-After, so an instant second attempt is wasted."""
  from app.routes.github import _push_topic_branch

  calls = []
  sleeps = []
  monkeypatch.setattr(
    "app.github_contribution_git._git",
    lambda _repo, *args, **kwargs: calls.append(args) or _cp(
      stderr="fatal: unable to access: The requested URL returned error: 429",
      returncode=1,
    ),
  )
  monkeypatch.setattr("app.github_contributions.time.sleep", sleeps.append)

  error = _push_topic_branch(tmp_path, "fix/demo")

  assert "429" in error
  assert len(calls) == 1
  assert sleeps == []


def test_push_topic_branch_uses_the_exact_authoritative_remote_lease(
  tmp_path, monkeypatch,
):
  calls = []
  monkeypatch.setattr(
    "app.github_contribution_git._git",
    lambda _repo, *args, **_kwargs: calls.append(args) or _cp(""),
  )
  old = "b" * 40

  assert github_contributions._push_topic_branch(
    tmp_path, "fix/demo", "HEAD", old,
  ) is None
  assert calls == [(
    "push", f"--force-with-lease=refs/heads/fix/demo:{old}",
    "fork", "HEAD:refs/heads/fix/demo",
  )]


def test_push_topic_branch_surfaces_transient_result_for_authoritative_recovery(
  tmp_path, monkeypatch,
):
  from app.routes.github import _push_topic_branch

  outcomes = iter((_cp(
    stderr="fatal: unable to access: HTTP 503", returncode=1,
  ),))
  sleeps = []
  monkeypatch.setattr(
    "app.github_contribution_git._git",
    lambda _repo, *args, **kwargs: next(outcomes),
  )
  monkeypatch.setattr("app.github_contributions.time.sleep", sleeps.append)

  assert "HTTP 503" in _push_topic_branch(tmp_path, "fix/demo")
  assert sleeps == []


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
  assert _safe_repo_path(str(data_dir / "contrib" / "mobius-fix-x")) == (
    data_dir / "contrib" / "mobius-fix-x"
  ).resolve()
  assert _safe_repo_path(str(data_dir / "contributions" / "legacy" / "repo")) == (
    data_dir / "contributions" / "legacy" / "repo"
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


def test_safe_equivalence_source_path_accepts_only_primary_standalone_checkout():
  from app.routes.github import (
    ContributionSubmitError,
    _safe_equivalence_source_path,
  )

  data_dir = Path(get_settings().data_dir)
  source = data_dir / "worktrees" / "standalone-source"
  source.mkdir(parents=True)
  subprocess.run(["git", "init", "-qb", "main", str(source)], check=True)
  subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
  subprocess.run(
    ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
    check=True,
  )
  (source / "tracked.txt").write_text("source\n")
  subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
  subprocess.run(["git", "-C", str(source), "commit", "-qm", "source"], check=True)

  assert _safe_equivalence_source_path(str(source)) == source.resolve()

  linked = data_dir / "worktrees" / "linked-review"
  subprocess.run(
    ["git", "-C", str(source), "worktree", "add", "-qb", "fix/linked", str(linked)],
    check=True,
  )
  with pytest.raises(ContributionSubmitError) as linked_error:
    _safe_equivalence_source_path(str(linked))
  assert "primary checkout" in linked_error.value.message

  nested = data_dir / "worktrees" / "group" / "nested-source"
  nested.mkdir(parents=True)
  subprocess.run(["git", "init", "-qb", "main", str(nested)], check=True)
  with pytest.raises(ContributionSubmitError) as nested_error:
    _safe_equivalence_source_path(str(nested))
  assert "direct checkout" in nested_error.value.message


def test_contribution_lifecycle_persists_equivalence_in_standalone_source():
  """A direct /data/worktrees source owns the same durable merge witness."""
  from app import app_git
  from app.routes.github import _record_pending_equivalence

  data_dir = Path(get_settings().data_dir)
  source = data_dir / "worktrees" / "equivalence-standalone-source"
  review = data_dir / "contrib" / "equivalence-standalone-review" / "worktree"
  source.mkdir(parents=True)
  subprocess.run(["git", "init", "-qb", "main", str(source)], check=True)
  subprocess.run(["git", "-C", str(source), "config", "user.name", "Test"], check=True)
  subprocess.run(
    ["git", "-C", str(source), "config", "user.email", "test@example.invalid"],
    check=True,
  )
  (source / "tracked.txt").write_text("base\n")
  subprocess.run(["git", "-C", str(source), "add", "tracked.txt"], check=True)
  subprocess.run(["git", "-C", str(source), "commit", "-qm", "base"], check=True)
  base = app_git.head_sha(source, "HEAD")
  (source / "tracked.txt").write_text("reviewed\n")
  subprocess.run(["git", "-C", str(source), "commit", "-qam", "reviewed"], check=True)
  head = app_git.head_sha(source, "HEAD")
  review.parent.mkdir(parents=True)
  subprocess.run(
    [
      "git", "-C", str(source), "worktree", "add", "-qb",
      "fix/review", str(review), head,
    ],
    check=True,
  )
  diff = app_git._canonical_diff(review, base, head)
  assert diff is not None
  digest = hashlib.sha256(diff).hexdigest()
  record = {
    "id": "equivalence-standalone-review",
    "status": "open",
    "plan": {
      "repo_path": str(review),
      "base_sha": base,
      "head_sha": head,
      "diff_sha256": digest,
    },
  }

  pending = _record_pending_equivalence(record)

  assert pending and app_git.ref_exists(source, pending)
  assert app_git.primary_worktree_path(review) == source.resolve()


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


def test_cleanup_terminal_staging_checkout_unlocks_and_unregisters_linked_worktree():
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
  subprocess.run(
    [
      "git", "-C", str(owner), "worktree", "lock",
      "--reason", "durable contribution review", str(checkout),
    ],
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
def test_submit_contribution_keeps_accepted_ready_pr_on_label_transport_failure(
  client, owner_token, monkeypatch, failure_kind,
):
  label_failure = (
    subprocess.TimeoutExpired(["gh", "api"], timeout=30)
    if failure_kind == "timeout"
    else OSError("gh could not start")
  )
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = f"rec-pr-label-{failure_kind}"
  repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
    if args == ("api", "repos/mobius-os/app-demo/pulls/42"):
      return _cp(json.dumps({
        "state": "open",
        "draft": False,
        "html_url": "https://github.com/mobius-os/app-demo/pull/42",
        "head": {
          "ref": "fix/demo-polish",
          "sha": head,
          "repo": {"full_name": "octocat/app-demo-1"},
        },
        "base": {"ref": "main"},
      }))
    if args[:2] == ("api", "--paginate"):
      raise label_failure
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)

  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"publication_stage": "ready"},
  )
  assert r.status_code == 200, r.text
  body = r.json()
  assert body["url"] == "https://github.com/mobius-os/app-demo/pull/42"
  assert body["number"] == 42
  assert body["record"]["status"] == "open"
  assert body["record"]["publication_stage"] == "ready"
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
  assert (
    "push", "--force-with-lease=refs/heads/fix/demo-polish:",
    "fork", "HEAD:refs/heads/fix/demo-polish",
  ) in git_calls
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
  assert stored["publication_stage"] == "ready"
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
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = f"rec-pr-create-{failure_kind}-{existing_mode}"
  repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
    if args == ("api", "repos/mobius-os/app-demo/pulls/42"):
      return _cp(json.dumps({
        "state": "open",
        "draft": True,
        "html_url": "https://github.com/mobius-os/app-demo/pull/42",
        "head": {
          "ref": "fix/demo-polish",
          "sha": head,
          "repo": {"full_name": "octocat/app-demo-1"},
        },
        "base": {"ref": "main"},
      }))
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
    assert stored["status"] == "draft"
    assert stored["publication_stage"] == "draft"
    assert stored["url"].endswith("/pull/42")
    assert stored["last_submit_push_sha"] == head
    assert stored["last_submit_labels_applied"] == ["bug"]
  else:
    assert response.status_code == 409, response.text
    assert stored["status"] == "prepared"
    assert stored["last_submit_stage"] == "pushed"
    assert stored["last_submit_push_sha"] == head
    assert "url" not in stored


def test_existing_pr_update_confirmation_reads_the_known_pr_directly(
  tmp_path, monkeypatch,
):
  from app.github_contributions import _confirm_existing_pr_update

  expected = "a" * 40
  expected_base = "b" * 40
  calls = []

  def fake_gh(repo_path, *args, check=True):
    calls.append(args)
    head = "c" * 40 if len(calls) == 1 else expected
    return _cp(json.dumps({
      "html_url": "https://github.com/mobius-os/app-demo/pull/58",
      "state": "open",
      "head": {
        "ref": "feat/existing-review",
        "sha": head,
        "repo": {"full_name": "octocat/app-demo"},
      },
      # GitHub's PR payload is a PR snapshot, not an authoritative current
      # branch ref. A stale value must not reject an unchanged reviewed parent.
      "base": {"ref": "main", "sha": "d" * 40},
      "draft": True,
    }))

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda _repo, _upstream, _branch: expected_base,
  )
  monkeypatch.setattr("app.github_contributions.time.sleep", lambda _seconds: None)

  confirmed = _confirm_existing_pr_update(
    tmp_path,
    "mobius-os/app-demo",
    58,
    expected_head_repository="octocat/app-demo",
    expected_head_sha=expected,
    branch="feat/existing-review",
    base_branch="main",
    expected_base_sha=expected_base,
  )

  assert confirmed == (
    "https://github.com/mobius-os/app-demo/pull/58", "draft",
  )
  assert calls == [
    ("api", "repos/mobius-os/app-demo/pulls/58"),
    ("api", "repos/mobius-os/app-demo/pulls/58"),
  ]


def test_existing_pr_update_confirmation_rejects_metadata_drift(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr("app.github_contributions._PR_VISIBILITY_RETRIES", 1)
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda *_args, **_kwargs: _cp(json.dumps({
      "html_url": "https://github.com/mobius-os/app-demo/pull/58",
      "state": "open",
      "title": "Maintainer title",
      "body": "Reviewed body.\n",
      "head": {
        "ref": "feat/existing-review",
        "sha": "a" * 40,
        "repo": {"full_name": "octocat/app-demo"},
      },
      "base": {"ref": "main", "sha": "b" * 40},
      "draft": True,
    })),
  )

  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._confirm_existing_pr_update(
      tmp_path,
      "mobius-os/app-demo",
      58,
      expected_head_repository="octocat/app-demo",
      expected_head_sha="a" * 40,
      branch="feat/existing-review",
      base_branch="main",
      expected_title="Reviewed title",
      expected_body="Reviewed body.\n",
    )

  assert caught.value.code == "review_refresh_needed"
  assert "title or body changed" in caught.value.message


def test_existing_pr_update_confirmation_rejects_moved_stack_base(
  tmp_path, monkeypatch,
):
  from app.github_contributions import _confirm_existing_pr_update

  monkeypatch.setattr("app.github_contributions._PR_VISIBILITY_RETRIES", 1)
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda *_args, **_kwargs: _cp(json.dumps({
      "html_url": "https://github.com/mobius-os/app-demo/pull/58",
      "state": "open",
      "head": {
        "ref": "feat/existing-review",
        "sha": "a" * 40,
        "repo": {"full_name": "octocat/app-demo"},
      },
      # The snapshot can still show the reviewed commit after the live base
      # branch has moved.
      "base": {"ref": "stack/demo/01-parent", "sha": "b" * 40},
      "draft": True,
    })),
  )
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda _repo, _upstream, _branch: "d" * 40,
  )

  with pytest.raises(ContributionSubmitError) as caught:
    _confirm_existing_pr_update(
      tmp_path,
      "mobius-os/app-demo",
      58,
      expected_head_repository="octocat/app-demo",
      expected_head_sha="a" * 40,
      branch="feat/existing-review",
      base_branch="stack/demo/01-parent",
      expected_base_sha="b" * 40,
    )

  assert caught.value.code == "review_refresh_needed"
  assert "parent branch moved" in caught.value.message


@pytest.mark.parametrize(
  "lookup_failure",
  [
    None,
    OSError("offline"),
    subprocess.TimeoutExpired(["gh", "api"], 30),
  ],
)
def test_existing_pr_update_confirmation_stops_when_stack_base_is_unreadable(
  tmp_path, monkeypatch, lookup_failure,
):
  from app.github_contributions import _confirm_existing_pr_update

  monkeypatch.setattr("app.github_contributions._PR_VISIBILITY_RETRIES", 1)
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda *_args, **_kwargs: _cp(json.dumps({
      "html_url": "https://github.com/mobius-os/app-demo/pull/58",
      "state": "open",
      "head": {
        "ref": "feat/existing-review",
        "sha": "a" * 40,
        "repo": {"full_name": "octocat/app-demo"},
      },
      "base": {"ref": "stack/demo/01-parent", "sha": "b" * 40},
      "draft": True,
    })),
  )
  def unreadable(_repo, _upstream, _branch):
    if lookup_failure is not None:
      raise lookup_failure
    return None

  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha", unreadable,
  )

  with pytest.raises(ContributionSubmitError) as caught:
    _confirm_existing_pr_update(
      tmp_path,
      "mobius-os/app-demo",
      58,
      expected_head_repository="octocat/app-demo",
      expected_head_sha="a" * 40,
      branch="feat/existing-review",
      base_branch="stack/demo/01-parent",
      expected_base_sha="b" * 40,
    )

  assert caught.value.status_code == 503
  assert caught.value.code == "update_unconfirmed"
  assert "did not confirm" in caught.value.message


def test_submit_contribution_normalizes_fallback_author_before_push(
  client, owner_token, monkeypatch,
):
  monkeypatch.setattr(
    github_contributions,
    "_confirm_existing_pr_update",
    lambda _repo, upstream, number, **_kwargs: (
      f"https://github.com/{upstream}/pull/{number}", "draft",
    ),
  )
  _write_token(login="octocat", user_id=42)
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-fallback-author"
  repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
  mismatched_head = False
  crash_after_amend = True
  parent = "e" * 40
  commit_message = "Polish demo\n\n"

  def current_head():
    if not normalized:
      return old_head
    return ("d" * 40) if mismatched_head else new_head

  def fake_git(repo_path, *args, check=True):
    nonlocal normalized, crash_after_amend
    git_calls.append(args)
    if (preflight := _submit_preflight_response(args)) is not None:
      return preflight
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp("main\n")
    if args == ("status", "--porcelain"):
      return _cp("")
    if args == ("rev-parse", "fix/demo-polish"):
      return _cp(current_head() + "\n")
    if args == ("rev-parse", "HEAD"):
      return _cp(current_head() + "\n")
    if args == ("rev-parse", "--verify", f"{base}^{{commit}}"):
      return _cp(base + "\n")
    if args == ("rev-parse", "--verify", f"{old_head}^{{commit}}"):
      return _cp(old_head + "\n")
    if args == ("rev-parse", "--verify", f"{new_head}^{{commit}}"):
      return _cp(new_head + "\n")
    if args in {
      ("show", "-s", "--format=%P", old_head),
      ("show", "-s", "--format=%P", new_head),
    }:
      return _cp(parent + "\n")
    if args in {
      ("show", "-s", "--format=%B", old_head),
      ("show", "-s", "--format=%B", new_head),
    }:
      return _cp(commit_message)
    if args == ("show", "-s", "--format=%cI", new_head):
      return _cp("2026-07-10T03:12:02+00:00\n")
    if args == ("cat-file", "-p", old_head):
      return _cp(
        f"tree reviewed-tree\nparent {parent}\n"
        "author Mobius Agent <agent@mobius> 1783653122 +0000\n"
        "committer Mobius Agent <agent@mobius> 1783653122 +0000\n\n"
        f"{commit_message}"
      )
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
        return _commit_metadata(
          current_head(),
        )
      return _commit_metadata(
        old_head,
        name="Mobius Agent",
        email="agent@mobius",
      )
    if args == (
      "update-ref", "refs/heads/fix/demo-polish", new_head, old_head,
    ):
      normalized = True
      if crash_after_amend:
        crash_after_amend = False
        raise RuntimeError("injected crash after ref update")
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

  def fake_run_cmd(argv, **_kwargs):
    if argv[-2:] == ["var", "GIT_AUTHOR_IDENT"]:
      return _cp("octocat <42+octocat@users.noreply.github.com> 1783653122 +0000\n")
    if argv[-2:] == ["var", "GIT_COMMITTER_IDENT"]:
      return _cp("octocat <42+octocat@users.noreply.github.com> 1783653122 +0000\n")
    if "hash-object" in argv:
      return _cp(new_head + "\n")
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  monkeypatch.setattr("app.github_contribution_git._run_cmd", fake_run_cmd)

  first = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert first.status_code == 500, first.text
  record_path = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "contributions" / f"{record_id}.json"
  )
  receipt = github_routes._read_personal_attempt(
    record_path, app_id=app_id, record_id=record_id,
  )
  assert receipt is not None and receipt["phase"] == "normalizing"
  assert json.loads(record_path.read_text())["plan"]["head_sha"] == old_head

  mismatched_head = True
  rejected = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert rejected.status_code == 409, rejected.text
  assert github_routes._read_personal_attempt(
    record_path, app_id=app_id, record_id=record_id,
  )["phase"] == "normalizing"
  mismatched_head = False
  r = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 200, r.text
  body = r.json()
  assert body["record"]["head_sha"] == new_head
  assert body["record"]["plan"]["head_sha"] == new_head
  assert body["record"]["plan"]["attribution_normalized_from"] == old_head
  assert (
    "push", "--force-with-lease=refs/heads/fix/demo-polish:",
    "fork", "HEAD:refs/heads/fix/demo-polish",
  ) in git_calls
  assert not github_routes.contribution_runtime.personal_attempt_path(
    app_id, record_id,
  ).exists()


def _normalization_repo(tmp_path: Path, *, linked: bool = False) -> tuple[Path, str]:
  source = tmp_path / "normalization-source"
  subprocess.run(["git", "init", "-qb", "main", str(source)], check=True)
  subprocess.run(
    ["git", "-C", str(source), "config", "user.name", "Mobius Agent"],
    check=True,
  )
  subprocess.run(
    ["git", "-C", str(source), "config", "user.email", "agent@mobius"],
    check=True,
  )
  (source / "base.txt").write_text("base\n")
  subprocess.run(["git", "-C", str(source), "add", "base.txt"], check=True)
  subprocess.run(
    ["git", "-C", str(source), "commit", "-qm", "base"], check=True,
  )
  base = subprocess.run(
    ["git", "-C", str(source), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True,
  ).stdout.strip()
  if linked:
    repo = tmp_path / "normalization-linked"
    subprocess.run(
      [
        "git", "-C", str(source), "worktree", "add", "-qb",
        "fix/normalize", str(repo),
      ],
      check=True,
    )
  else:
    repo = source
    subprocess.run(
      ["git", "-C", str(repo), "checkout", "-qb", "fix/normalize"],
      check=True,
    )
  return repo, base


def _commit_reviewed_change(repo: Path, name: str = "reviewed.txt") -> str:
  (repo / name).write_text("reviewed\n")
  subprocess.run(["git", "-C", str(repo), "add", name], check=True)
  subprocess.run(
    ["git", "-C", str(repo), "commit", "-qm", "Reviewed change"],
    check=True,
  )
  return subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True,
  ).stdout.strip()


def test_normalize_attribution_pins_exact_oid_in_linked_worktree(tmp_path):
  """The signed pre-ref witness names the only acceptable normalized commit."""
  repo, base = _normalization_repo(tmp_path, linked=True)
  old_head = _commit_reviewed_change(repo)
  expected_diff = hashlib.sha256(
    github_contributions._git_ops._reviewed_branch_diff(repo, base, old_head)
  ).hexdigest()
  record = {"plan": {"head_sha": old_head}}
  witnessed = []

  patch = github_contributions._git_ops._normalize_head_attribution(
    repo,
    "fix/normalize",
    author_name="octocat",
    author_email="42+octocat@users.noreply.github.com",
    base_sha=base,
    expected_diff=expected_diff,
    record=record,
    before_amend=witnessed.append,
  )

  new_head = subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True,
  ).stdout.strip()
  assert patch["head_sha"] == new_head
  assert witnessed[0]["expected_normalized_head_sha"] == new_head
  assert subprocess.run(
    ["git", "-C", str(repo), "status", "--porcelain"],
    capture_output=True, text=True, check=True,
  ).stdout == ""
  assert github_contributions._recover_normalized_attribution(
    record,
    repo,
    "fix/normalize",
    {
      "previous_head_sha": old_head,
      "expected_normalized_head_sha": new_head,
    },
  )["head_sha"] == new_head


def test_normalize_attribution_preserves_all_ordered_parents(tmp_path):
  repo, _base = _normalization_repo(tmp_path)
  source = repo
  subprocess.run(
    ["git", "-C", str(source), "checkout", "-qb", "side", "main"],
    check=True,
  )
  _commit_reviewed_change(source, "side.txt")
  subprocess.run(
    ["git", "-C", str(source), "checkout", "-q", "fix/normalize"],
    check=True,
  )
  _commit_reviewed_change(source, "main.txt")
  subprocess.run(
    ["git", "-C", str(source), "merge", "--no-ff", "-qm", "merge", "side"],
    check=True,
  )
  old_head = subprocess.run(
    ["git", "-C", str(source), "rev-parse", "HEAD"],
    capture_output=True, text=True, check=True,
  ).stdout.strip()
  old_parents = subprocess.run(
    ["git", "-C", str(source), "show", "-s", "--format=%P", old_head],
    capture_output=True, text=True, check=True,
  ).stdout.strip().split()
  assert len(old_parents) == 2
  base = old_parents[0]
  expected_diff = hashlib.sha256(
    github_contributions._git_ops._reviewed_branch_diff(source, base, old_head)
  ).hexdigest()
  witnessed = []
  github_contributions._git_ops._normalize_head_attribution(
    source,
    "fix/normalize",
    author_name="octocat",
    author_email="42+octocat@users.noreply.github.com",
    base_sha=base,
    expected_diff=expected_diff,
    record={"plan": {"head_sha": old_head}},
    before_amend=witnessed.append,
  )
  new_parents = subprocess.run(
    ["git", "-C", str(source), "show", "-s", "--format=%P", "HEAD"],
    capture_output=True, text=True, check=True,
  ).stdout.strip().split()
  assert witnessed[0]["parents"] == old_parents
  assert new_parents == old_parents


@pytest.mark.parametrize("tamper", ["alternate-date", "continuation"])
def test_normalization_recovery_rejects_distinct_raw_commit_oid(tmp_path, tamper):
  """Parsed metadata equivalence never substitutes for the signed exact OID."""
  repo, base = _normalization_repo(tmp_path)
  old_head = _commit_reviewed_change(repo)
  expected_diff = hashlib.sha256(
    github_contributions._git_ops._reviewed_branch_diff(repo, base, old_head)
  ).hexdigest()
  witnessed = []
  github_contributions._git_ops._normalize_head_attribution(
    repo,
    "fix/normalize",
    author_name="octocat",
    author_email="42+octocat@users.noreply.github.com",
    base_sha=base,
    expected_diff=expected_diff,
    record={"plan": {"head_sha": old_head}},
    before_amend=witnessed.append,
  )
  expected_head = witnessed[0]["expected_normalized_head_sha"]
  raw = subprocess.run(
    ["git", "-C", str(repo), "cat-file", "-p", expected_head],
    capture_output=True, text=True, check=True,
  ).stdout
  if tamper == "alternate-date":
    tampered = re.sub(
      r"(?m)^((?:author|committer) .* )([0-9]+)( [+-][0-9]{4})$",
      lambda match: (
        match.group(1) + "0" + match.group(2) + match.group(3)
      ),
      raw,
    )
  else:
    tampered = raw.replace("\n\n", "\n hidden continuation\n\n", 1)
  tampered_head = subprocess.run(
    [
      "git", "-C", str(repo), "hash-object", "--literally", "-t", "commit",
      "-w", "--stdin",
    ],
    input=tampered, capture_output=True, text=True, check=True,
  ).stdout.strip()
  assert tampered_head != expected_head
  subprocess.run(
    [
      "git", "-C", str(repo), "update-ref", "refs/heads/fix/normalize",
      tampered_head, expected_head,
    ],
    check=True,
  )
  expected_meta = github_contributions._git_ops._head_commit_metadata(
    repo, expected_head,
  )
  tampered_meta = github_contributions._git_ops._head_commit_metadata(
    repo, "fix/normalize",
  )
  assert {
    key: value for key, value in expected_meta.items() if key != "sha"
  } == {
    key: value for key, value in tampered_meta.items() if key != "sha"
  }
  with pytest.raises(ContributionSubmitError, match="changed during attribution"):
    github_contributions._recover_normalized_attribution(
      {"plan": {"head_sha": old_head}},
      repo,
      "fix/normalize",
      {
        "previous_head_sha": old_head,
        "expected_normalized_head_sha": expected_head,
      },
    )


def test_submit_contribution_replaces_stale_fork_remote_before_push(
  client, owner_token, monkeypatch,
):
  monkeypatch.setattr(
    github_contributions,
    "_confirm_existing_pr_update",
    lambda _repo, upstream, number, **_kwargs: (
      f"https://github.com/{upstream}/pull/{number}", "draft",
    ),
  )
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-stale-fork"
  repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
  assert (
    "push", "--force-with-lease=refs/heads/fix/demo-polish:",
    "fork", "HEAD:refs/heads/fix/demo-polish",
  ) in git_calls


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
  monkeypatch.setattr(
    github_contributions,
    "_confirm_existing_pr_update",
    lambda _repo, upstream, number, **_kwargs: (
      f"https://github.com/{upstream}/pull/{number}", "draft",
    ),
  )
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
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
    repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
      "quality_review": {
        "state": "all_clear",
        "reviewed_head_sha": head_sha,
      },
    }
    _write_contribution(app_id, record_id, record, diff_text)

  baseline = checked_out_connections()
  preflight_pool_counts = []

  def fake_preflight(rows, **_kwargs):
    preflight_pool_counts.append(checked_out_connections())

  monkeypatch.setattr(
    "app.routes.github._preflight_prepared_stack",
    fake_preflight,
  )
  calls = []

  def fake_submit(
    record, diff_path, *, direct_base_branch=None, publication_stage="draft",
    **_kwargs,
  ):
    record_path = (
      Path(get_settings().data_dir) / "apps" / str(app_id)
      / "contributions" / f"{record['id']}.json"
    )
    receipt = github_routes._read_personal_attempt(
      record_path, app_id=app_id, record_id=record["id"],
    )
    assert receipt is not None and receipt["phase"] == "armed"
    calls.append((
      record["id"], direct_base_branch, diff_path.name, publication_stage,
    ))
    number = 70 + len(calls)
    return (
      f"https://github.com/mobius-os/mobius/pull/{number}",
      number,
      {
        "last_submit_mode": "stack",
        "last_submit_base_branch": direct_base_branch,
        "publication_stage": publication_stage,
      },
    )

  monkeypatch.setattr("app.routes.github._submit_prepared_pr", fake_submit)

  r = client.post(
    f"/api/github/contributions/{app_id}/submit-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids, "publication_stage": "ready"},
  )

  assert r.status_code == 200, r.text
  assert calls == [
    (record_ids[0], "main", f"{record_ids[0]}.diff", "ready"),
    (
      record_ids[1], f"stack/{stack_id}/01-stream",
      f"{record_ids[1]}.diff", "ready",
    ),
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
  monkeypatch.setattr(
    github_contributions,
    "_confirm_existing_pr_update",
    lambda _repo, upstream, number, **_kwargs: (
      f"https://github.com/{upstream}/pull/{number}", "draft",
    ),
  )
  from app.routes.github import ContributionSubmitError

  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
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
    repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
      "quality_review": {
        "state": "all_clear",
        "reviewed_head_sha": head_sha,
      },
    }
    _write_contribution(app_id, record_id, record, diff_text)

  monkeypatch.setattr(
    "app.routes.github._preflight_prepared_stack",
    lambda rows, **_kwargs: None,
  )
  calls = []

  def fake_submit(
    record, diff_path, *, direct_base_branch=None, publication_stage="draft",
    **_kwargs,
  ):
    calls.append(record["id"])
    if len(calls) == 1:
      return (
        "https://github.com/mobius-os/mobius/pull/81",
        81,
        {
          "last_submit_mode": "stack",
          "publication_stage": publication_stage,
        },
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
    "draft", "prepared",
  ]
  assert detail["records"][1]["last_submit_error"] == (
    "Child PR could not be opened."
  )


def test_submit_contribution_stack_rechecks_current_source_before_push(
  client, owner_token, monkeypatch,
):
  """The batch path proves every installed source before its first push."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, diff_text = _prepared_real_review(app_id, "stack-source-moved")
  record_ids = _add_source_proof_stack_child(
    app_id, record, status="prepared",
  )
  _write_contribution(app_id, record["id"], record, diff_text)
  _remove_reviewed_change_from_source(record)
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_default_branch",
    lambda *_args: "main",
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_upstream_push_permission",
    lambda *_args: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_merges_with_upstream",
    lambda *_args: {},
  )
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail("reverted source must never push"),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/submit-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids, "publication_stage": "draft"},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "source_provenance_mismatch"


def test_submit_contribution_stack_rechecks_each_child_before_first_push(
  client, owner_token, monkeypatch,
):
  """A valid parent cannot hide a child absent from the installed source."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, diff_text = _prepared_real_review(app_id, "stack-child-moved")
  record_ids = _add_source_proof_stack_child(
    app_id, record, status="prepared",
  )
  _write_contribution(app_id, record["id"], record, diff_text)
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_default_branch",
    lambda *_args: "main",
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_upstream_push_permission",
    lambda *_args: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_merges_with_upstream",
    lambda *_args: {},
  )
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail("invalid child must block every push"),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/submit-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids, "publication_stage": "draft"},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "source_provenance_mismatch"


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
  repo = Path(get_settings().data_dir) / "contrib" / "merged-retry" / "repo"
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
    repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
  monkeypatch.setattr(
    github_contributions,
    "_confirm_existing_pr_update",
    lambda _repo, upstream, number, **_kwargs: (
      f"https://github.com/{upstream}/pull/{number}", "draft",
    ),
  )
  from app.routes.github import _submit_prepared_pr

  _write_token(login="octocat")
  record_id = "direct-stack-layer"
  repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
  monkeypatch.setattr(
    "app.github_contribution_git._authoritative_upstream_branch_sha",
    lambda *_args, **_kwargs: None,
  )
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
    "push", f"--force-with-lease=refs/heads/{branch}:",
    "https://github.com/mobius-os/app-demo.git",
    f"HEAD:refs/heads/{branch}",
  ) in git_calls
  create = next(call for call in gh_calls if call[:2] == ("pr", "create"))
  assert create[create.index("-H") + 1] == branch
  assert "--draft" in create
  assert create[-2:] == ("--base", "main")
  assert not any(call[:2] == ("repo", "fork") for call in gh_calls)


@pytest.mark.parametrize("upstream_repo", ["octocat/app-demo", "mobius-os/app-demo"])
def test_push_permission_uses_same_repo_branch_without_fork(
  tmp_path, monkeypatch, upstream_repo,
):
  from app.routes.github import _submit_prepared_pr

  _write_token(login="octocat")
  repo = tmp_path / "owner-repo"
  (repo / ".git").mkdir(parents=True)
  branch = "fix/owner-repo-change"
  base = "b" * 40
  head = "a" * 40
  diff_text = "diff --git a/model.py b/model.py\n+reviewed\n"
  diff_path = tmp_path / "owner.diff"
  diff_path.write_text(diff_text)
  record = {
    "id": "owner-repo-change",
    "type": "pr",
    "repo": upstream_repo,
    "status": "submitting",
    "title": "Owner repository change",
    "branch": branch,
    "plan": {
      "action": "pr",
      "repo": upstream_repo,
      "title": "Owner repository change",
      "body_draft": "Reviewed owner repository change.",
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
    "app.github_contribution_git._assert_head_attribution",
    lambda *_args, **_kwargs: None,
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
  pushes = []
  monkeypatch.setattr(
    "app.github_contributions._push_branch",
    lambda _repo, remote, pushed_branch, source, _expected_remote_sha: (
      pushes.append((remote, pushed_branch, source)) or None
    ),
  )
  monkeypatch.setattr(
    "app.github_contributions._ensure_owner_fork_remote",
    lambda *_args, **_kwargs: pytest.fail("owner repositories must not be forked"),
  )

  def fake_git(_repo, *args, check=True):
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp(branch + "\n")
    if args == ("rev-parse", "HEAD"):
      return _cp(head + "\n")
    return _cp("")

  gh_calls = []

  def fake_gh(_repo, *args, check=True):
    gh_calls.append(args)
    if args[:2] == ("api", f"repos/{upstream_repo}"):
      return _cp("true\n")
    if args[:2] == ("pr", "list"):
      return _cp("[]")
    if args[:2] == ("pr", "create"):
      return _cp(f"https://github.com/{upstream_repo}/pull/74\n")
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  monkeypatch.setattr(
    "app.github_contribution_git._authoritative_upstream_branch_sha",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    "app.github_contributions._confirm_existing_pr_update",
    lambda *_args, **_kwargs: (
      f"https://github.com/{upstream_repo}/pull/74", "draft",
    ),
  )

  url, number, patch = _submit_prepared_pr(record, diff_path)

  assert url.endswith("/pull/74")
  assert number == 74
  assert pushes == [
    (f"https://github.com/{upstream_repo}.git", branch, "HEAD"),
  ]
  assert patch["head_repository"] == upstream_repo
  assert patch["last_submit_mode"] == "upstream-repo"
  assert patch["last_pushed_branch"] == branch
  create = next(call for call in gh_calls if call[:2] == ("pr", "create"))
  assert create[create.index("-H") + 1] == branch
  assert create[-2:] == ("--base", "main")

def _reviewed_metadata_update() -> dict:
  repo_path = (
    Path(get_settings().data_dir) / "contrib" / "metadata-update" / "worktree"
  )
  (repo_path / ".git").mkdir(parents=True, exist_ok=True)
  return {
    "id": "metadata-update",
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "branch": "feat/existing-review",
    "plan": {
      "action": "pr_update",
      "repo": "mobius-os/app-demo",
      "title": "Reviewed title",
      "body_draft": "Reviewed body.\n",
      "branch": "feat/existing-review",
      "repo_path": str(repo_path),
      "head_sha": "a" * 40,
      "pr_metadata": {
        "old_title": "Reviewed title",
        "old_body": "Reviewed body.\n",
      },
    },
  }


def test_personal_pr_text_is_published_as_exact_reviewed_bytes():
  record = {
    "title": "Fallback title",
    "plan": {
      "title": "Exact title",
      "body_draft": "\nExact body with preserved surrounding lines.\n",
    },
  }

  assert github_contributions._exact_reviewed_pr_text(record) == (
    "Exact title", "\nExact body with preserved surrounding lines.\n",
  )


def test_personal_pr_title_rejects_text_github_would_normalize():
  with pytest.raises(ContributionSubmitError):
    github_contributions._exact_reviewed_pr_text({
      "plan": {"title": " Padded title", "body_draft": "Body"},
    })


def test_existing_pr_metadata_requires_exact_reviewed_desired_text():
  record = _reviewed_metadata_update()
  assert github_contributions._assert_reviewed_existing_pr_metadata(
    record,
    live_title="Reviewed title",
    live_body="Reviewed body.\n",
  ) == "desired"


@pytest.mark.parametrize("witness", [None, "mismatch", "missing_plan_title"])
def test_existing_pr_metadata_requires_one_exact_reviewed_witness(witness):
  record = _reviewed_metadata_update()
  if witness is None:
    record["plan"].pop("pr_metadata")
  elif witness == "missing_plan_title":
    record["title"] = record["plan"].pop("title")
  else:
    record["plan"]["pr_metadata"]["old_body"] = "Earlier reviewed body.\n"

  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._assert_reviewed_existing_pr_metadata(
      record,
      live_title="Reviewed title",
      live_body="Reviewed body.\n",
    )

  assert caught.value.code == "review_refresh_needed"
  assert "witness" in caught.value.message


@pytest.mark.parametrize(
  ("title", "body"),
  [
    ("Old title", "Old body.\n"),
    ("Maintainer title", "Maintainer body."),
  ],
)
def test_existing_pr_metadata_mismatch_blocks_without_edit(title, body):
  """Neither a reviewed old witness nor later drift authorizes a text edit."""
  record = _reviewed_metadata_update()

  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._assert_reviewed_existing_pr_metadata(
      record,
      live_title=title,
      live_body=body,
    )

  assert caught.value.code == "review_refresh_needed"


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
      "pr_metadata": {
        "old_title": "Refine the existing contribution",
        "old_body": "Reviewed existing contribution update.",
      },
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
    "app.github_contribution_git._assert_head_attribution",
    lambda *_args, **_kwargs: None,
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
    "app.github_contribution_git._has_upstream_push_permission",
    lambda *_args: False,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_upstream_push_permission",
    lambda *_args: pytest.fail("a fork-backed PR must not push upstream"),
  )
  monkeypatch.setattr(
    "app.github_contributions._push_branch",
    lambda *_args: pytest.fail("a fork-backed PR must not push upstream"),
  )
  monkeypatch.setattr(
    "app.github_contribution_git._authoritative_upstream_branch_sha",
    lambda *_args, **_kwargs: None,
  )

  def fake_git(_repo, *args, check=True):
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp(branch + "\n")
    if args == ("rev-parse", "HEAD"):
      return _cp(head + "\n")
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  return record, diff_path, branch, head


def test_existing_pr_update_uses_verified_fork_even_with_upstream_permission(
  tmp_path, monkeypatch,
):
  from app.routes.github import _submit_prepared_pr

  record, diff_path, branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._has_upstream_push_permission",
    lambda *_args: True,
  )
  fork_calls = []
  monkeypatch.setattr(
    "app.github_contributions._ensure_owner_fork_remote",
    lambda _repo, upstream, login: (
      fork_calls.append((upstream, login)) or "octocat/app-demo"
    ),
  )
  monkeypatch.setattr(
    "app.github_contribution_git._authoritative_upstream_branch_sha",
    lambda *_args, **_kwargs: head,
  )
  pushes = []
  monkeypatch.setattr(
    "app.github_contributions._push_topic_branch",
    lambda _repo, pushed_branch, source, _expected_remote_sha: (
      pushes.append((pushed_branch, source)) or None
    ),
  )
  confirmations = []

  def confirm(
    _repo,
    upstream,
    number,
    *,
    expected_head_repository,
    expected_head_sha,
    branch,
    base_branch,
    expected_base_sha,
    expected_title,
    expected_body,
  ):
    confirmations.append((
      upstream,
      number,
      expected_head_repository,
      branch,
      expected_head_sha,
      base_branch,
      expected_base_sha,
      expected_title,
      expected_body,
    ))
    return "https://github.com/mobius-os/app-demo/pull/58", "draft"

  monkeypatch.setattr(
    "app.github_contributions._confirm_existing_pr_update", confirm,
  )

  url, number, patch = _submit_prepared_pr(
    record,
    diff_path,
    expected_existing_pr_number=58,
    expected_existing_head_repository="octocat/app-demo",
    expected_existing_head_sha=head,
  )

  assert url.endswith("/pull/58")
  assert number == 58
  assert fork_calls == [("mobius-os/app-demo", "octocat")]
  assert pushes == [(branch, "HEAD")]
  assert confirmations == [
    (
      "mobius-os/app-demo", 58, "octocat/app-demo", branch, head, "main",
      None, record["plan"]["title"], record["plan"]["body_draft"],
    ),
    (
      "mobius-os/app-demo", 58, "octocat/app-demo", branch, head, "main",
      None, record["plan"]["title"], record["plan"]["body_draft"],
    ),
  ]
  assert patch["head_repository"] == "octocat/app-demo"
  assert patch["last_submit_push_sha"] == head
  assert patch["last_pushed_branch"] == f"octocat:{branch}"
  assert patch["publication_stage"] == "draft"


def test_push_pending_receipt_recovers_accepted_push_after_crash(
  tmp_path, monkeypatch,
):
  """The intent is signed first; retry trusts only GitHub's exact remote tip."""
  record, diff_path, branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  record["plan"]["action"] = "pr"
  remote = {"published": False}
  pushes = []
  events = []

  def reconcile(_record, **_kwargs):
    if not remote["published"]:
      return None
    return github_contributions.PublicReconciliation(
      head_repository="octocat/app-demo",
      pr_url=None,
      pr_number=None,
      publication_stage="draft",
    )

  def push(_repo, pushed_branch, source, expected_remote_sha):
    assert pushed_branch == branch
    assert source == "HEAD"
    assert expected_remote_sha is None
    pushes.append(head)
    remote["published"] = True
    return None

  crash = {"armed": True}

  def attempt_event(phase, request, patch):
    if phase == "branch_published" and crash["armed"]:
      crash["armed"] = False
      raise RuntimeError("crash after GitHub accepted the push")
    events.append((phase, request, dict(patch or {})))

  monkeypatch.setattr(
    github_contributions, "_authoritative_public_reconciliation", reconcile,
  )
  monkeypatch.setattr(
    github_contributions, "_existing_branch_pr", lambda *_a, **_k: None,
  )
  monkeypatch.setattr(
    github_contributions,
    "_ensure_owner_fork_remote",
    lambda *_a, **_k: "octocat/app-demo",
  )
  monkeypatch.setattr(github_contributions, "_push_topic_branch", push)
  monkeypatch.setattr(
    github_contributions, "_apply_reviewed_pr_labels", lambda *_a, **_k: {},
  )
  monkeypatch.setattr(
    github_contributions,
    "_confirm_existing_pr_update",
    lambda _repo, upstream, number, **_kwargs: (
      f"https://github.com/{upstream}/pull/{number}", "draft",
    ),
  )
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda _repo, *args, **_kwargs: (
      _cp("https://github.com/mobius-os/app-demo/pull/92\n")
      if args[:2] == ("pr", "create") else _cp("")
    ),
  )

  with pytest.raises(RuntimeError, match="accepted the push"):
    github_contributions._submit_prepared_pr(
      record,
      diff_path,
      attempt_event=attempt_event,
    )
  pending = next(event for event in events if event[0] == "push_pending")
  assert pending[2]["last_submit_push_sha"] == head
  assert pushes == [head]

  retried_record = {**record, **pending[2]}
  url, number, _patch = github_contributions._submit_prepared_pr(
    retried_record,
    diff_path,
    attempt_event=attempt_event,
    prior_attempt_phase="push_pending",
    prior_attempt_receipt={
      "phase": "push_pending",
      "effective_request": pending[1],
      "record_patch": pending[2],
    },
  )
  assert url.endswith("/pull/92")
  assert number == 92
  assert pushes == [head]


@pytest.mark.parametrize("same_repo", [False, True], ids=["fork", "same-repo"])
def test_push_pending_no_effect_replays_once_behind_its_exact_remote_lease(
  tmp_path, monkeypatch, same_repo,
):
  """A hard death after intent but before push resumes without blind force."""
  record, diff_path, branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  record["plan"]["action"] = "pr"
  events = []
  pushes = []
  die = {"now": True}

  monkeypatch.setattr(
    github_contributions, "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_contributions, "_existing_branch_pr", lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_contributions, "_ensure_owner_fork_remote",
    lambda *_args, **_kwargs: "octocat/app-demo",
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_head_attribution",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_upstream_push_permission",
    lambda *_args, **_kwargs: None,
  )

  def event(phase, request, patch):
    events.append((phase, request, dict(patch or {})))
    if phase == "push_pending" and die["now"]:
      die["now"] = False
      raise RuntimeError("hard death before git push")

  def pushed(*args):
    pushes.append(args)
    return None

  monkeypatch.setattr(github_contributions, "_push_branch", pushed)
  monkeypatch.setattr(github_contributions, "_push_topic_branch", pushed)
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda _repo, *args, **_kwargs: (
      _cp("https://github.com/mobius-os/app-demo/pull/92\n")
      if args[:2] == ("pr", "create") else _cp("")
    ),
  )
  monkeypatch.setattr(
    github_contributions, "_confirm_existing_pr_update",
    lambda _repo, upstream, number, **_kwargs: (
      f"https://github.com/{upstream}/pull/{number}", "draft",
    ),
  )
  monkeypatch.setattr(
    github_contributions, "_apply_reviewed_pr_labels",
    lambda *_args, **_kwargs: {},
  )

  with pytest.raises(RuntimeError, match="before git push"):
    github_contributions._submit_prepared_pr(
      record, diff_path,
      direct_base_branch="main" if same_repo else None,
      attempt_event=event,
    )
  pending = next(item for item in events if item[0] == "push_pending")
  assert pending[1]["expected_remote_sha"] is None
  assert pushes == []

  url, number, _patch = github_contributions._submit_prepared_pr(
    {**record, **pending[2]},
    diff_path,
    direct_base_branch="main" if same_repo else None,
    attempt_event=event,
    prior_attempt_phase="push_pending",
    prior_attempt_receipt={
      "phase": "push_pending",
      "effective_request": pending[1],
      "record_patch": pending[2],
    },
  )
  assert url.endswith("/pull/92") and number == 92
  assert len(pushes) == 1
  # Both push owners receive the exact observed absence as their lease.
  assert pushes[0][-1] is None


def test_push_pending_no_effect_rejects_remote_reset_without_repush(
  tmp_path, monkeypatch,
):
  """A concurrent branch write invalidates the old receipt's exact lease."""
  record, diff_path, _branch, _head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  record["plan"]["action"] = "pr"
  events = []

  monkeypatch.setattr(
    github_contributions, "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_contributions, "_existing_branch_pr", lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_contributions, "_ensure_owner_fork_remote",
    lambda *_args, **_kwargs: "octocat/app-demo",
  )

  def die_after_intent(phase, request, patch):
    events.append((phase, request, dict(patch or {})))
    if phase == "push_pending":
      raise RuntimeError("hard death")

  with pytest.raises(RuntimeError, match="hard death"):
    github_contributions._submit_prepared_pr(
      record, diff_path, attempt_event=die_after_intent,
    )
  pending = next(item for item in events if item[0] == "push_pending")
  monkeypatch.setattr(
    "app.github_contribution_git._authoritative_upstream_branch_sha",
    lambda *_args, **_kwargs: "c" * 40,
  )
  monkeypatch.setattr(
    github_contributions, "_push_topic_branch",
    lambda *_args, **_kwargs: pytest.fail("concurrent drift must not be pushed"),
  )

  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._submit_prepared_pr(
      {**record, **pending[2]},
      diff_path,
      prior_attempt_phase="push_pending",
      prior_attempt_receipt={
        "phase": "push_pending",
        "effective_request": pending[1],
        "record_patch": pending[2],
      },
    )
  assert caught.value.code == "review_refresh_needed"
  assert "changed after the prior push attempt" in caught.value.message


@pytest.mark.parametrize("phase", ["branch_published", "pr_ambiguous", "complete"])
def test_postmutation_remote_reset_rechecks_source_inside_publication_owner(
  tmp_path, monkeypatch, phase,
):
  """A public read is not a lease permitting a later receipt-backed repush."""
  record, diff_path, _branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  record["plan"]["action"] = "pr"
  record.update({
    "last_submit_stage": "pushed",
    "last_submit_push_sha": head,
    "head_repository": "octocat/app-demo",
  })
  monkeypatch.setattr(
    github_contributions,
    "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: None,
  )
  source_checks = []

  def reverted_source(candidate):
    source_checks.append(candidate["id"])
    raise ContributionSubmitError(
      "The installed source reverted.", code="source_provenance_mismatch",
    )

  monkeypatch.setattr(
    github_contributions, "_assert_pending_equivalence_preflight",
    reverted_source,
  )
  monkeypatch.setattr(
    github_contributions,
    "_ensure_owner_fork_remote",
    lambda *_args, **_kwargs: pytest.fail("reverted source must not repush"),
  )

  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._submit_prepared_pr(
      record,
      diff_path,
      prior_attempt_phase=phase,
      prior_attempt_receipt={
        "phase": phase,
        "effective_request": {"action": "push"},
        "record_patch": {},
      },
    )

  assert caught.value.code == "source_provenance_mismatch"
  assert source_checks == [record["id"]]


def test_definite_pr_create_rejection_keeps_branch_retryable(
  tmp_path, monkeypatch,
):
  """A proven create rejection retries create, never republishes the branch."""
  record, diff_path, branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  record["plan"]["action"] = "pr"
  remote = {"published": False}
  pushes = []
  creates = []
  events = []

  monkeypatch.setattr(
    github_contributions,
    "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: (
      github_contributions.PublicReconciliation(
        head_repository="octocat/app-demo",
      )
      if remote["published"] else None
    ),
  )
  monkeypatch.setattr(
    github_contributions, "_existing_branch_pr", lambda *_a, **_k: None,
  )
  monkeypatch.setattr(
    github_contributions, "_find_existing_pr", lambda *_a, **_k: None,
  )
  monkeypatch.setattr(
    github_contributions,
    "_confirm_existing_pr_update",
    lambda _repo, upstream, number, **_kwargs: (
      f"https://github.com/{upstream}/pull/{number}", "draft",
    ),
  )
  monkeypatch.setattr(
    github_contributions,
    "_ensure_owner_fork_remote",
    lambda *_a, **_k: "octocat/app-demo",
  )

  def push(_repo, pushed_branch, source, _expected_remote_sha):
    pushes.append((pushed_branch, source))
    remote["published"] = True
    return None

  def gh(_repo, *args, **_kwargs):
    if args[:2] != ("pr", "create"):
      return _cp("")
    creates.append(args)
    if len(creates) == 1:
      return _cp("", returncode=1, stderr="GraphQL: title is invalid")
    return _cp("https://github.com/mobius-os/app-demo/pull/92\n")

  monkeypatch.setattr(github_contributions, "_push_topic_branch", push)
  monkeypatch.setattr("app.github_contribution_git._gh", gh)
  monkeypatch.setattr(
    github_contributions, "_apply_reviewed_pr_labels", lambda *_a, **_k: {},
  )

  def event(phase, request, patch):
    events.append((phase, request, dict(patch or {})))

  with pytest.raises(ContributionSubmitError):
    github_contributions._submit_prepared_pr(
      record, diff_path, attempt_event=event,
    )
  assert events[-1][0] == "branch_published"
  assert not any(phase == "pr_ambiguous" for phase, *_rest in events)
  branch_patch = events[-1][2]

  url, number, _patch = github_contributions._submit_prepared_pr(
    {**record, **branch_patch},
    diff_path,
    attempt_event=event,
    prior_attempt_phase="branch_published",
    prior_attempt_receipt={
      "phase": "branch_published",
      "effective_request": events[-1][1],
      "record_patch": branch_patch,
    },
  )

  assert url.endswith("/pull/92")
  assert number == 92
  assert pushes == [(branch, "HEAD")]
  assert len(creates) == 2
  assert head == branch_patch["last_submit_push_sha"]


def test_definite_push_rejection_rearms_signed_attempt(
  tmp_path, monkeypatch,
):
  """A proven rejection does not leave a false accepted-push recovery marker."""
  record, diff_path, branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  record["plan"]["action"] = "pr"
  events = []
  monkeypatch.setattr(
    github_contributions,
    "_authoritative_public_reconciliation",
    lambda *_a, **_k: None,
  )
  monkeypatch.setattr(
    github_contributions, "_existing_branch_pr", lambda *_a, **_k: None,
  )
  monkeypatch.setattr(
    github_contributions,
    "_ensure_owner_fork_remote",
    lambda *_a, **_k: "octocat/app-demo",
  )
  monkeypatch.setattr(
    github_contributions,
    "_push_topic_branch",
    lambda *_a, **_k: "remote rejected: protected branch",
  )

  with pytest.raises(ContributionSubmitError, match="would not accept"):
    github_contributions._submit_prepared_pr(
      record,
      diff_path,
      attempt_event=lambda phase, request, patch: events.append(
        (phase, request, dict(patch or {}))
      ),
    )

  assert [phase for phase, _request, _patch in events] == [
    "push_pending", "armed",
  ]
  assert events[0][1]["branch"] == branch
  assert events[0][2]["last_submit_push_sha"] == head
  assert events[1][1]["action"] == "push_rejected"
  assert "last_submit_stage" not in events[1][2]


def test_exact_existing_pr_retry_reconciles_before_duplicate_branch_guard(
  tmp_path, monkeypatch,
):
  """A lost create response settles its exact PR without another push."""
  record, diff_path, _branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  url = "https://github.com/mobius-os/app-demo/pull/42"
  record.update({
    "last_submit_stage": "pushed",
    "last_submit_push_sha": head,
    "last_submit_upstream_branch": "main",
    "head_repository": "octocat/app-demo",
  })
  monkeypatch.setattr(
    github_contributions,
    "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: github_contributions.PublicReconciliation(
      head_repository="octocat/app-demo",
      pr_url=url,
      pr_number=42,
      publication_stage="draft",
    ),
  )
  monkeypatch.setattr(
    github_contributions,
    "_existing_branch_pr",
    lambda *_args, **_kwargs: pytest.fail(
      "an exact recovery must precede the duplicate branch refusal",
    ),
  )
  monkeypatch.setattr(
    github_contributions,
    "_ensure_owner_fork_remote",
    lambda *_args, **_kwargs: pytest.fail("recovery must not recreate a fork"),
  )
  monkeypatch.setattr(
    github_contributions,
    "_push_topic_branch",
    lambda *_args, **_kwargs: pytest.fail("recovery must not push twice"),
  )
  monkeypatch.setattr(
    github_contributions, "_apply_reviewed_pr_labels", lambda *_args: {},
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_merges_with_upstream",
    lambda *_args, **_kwargs: pytest.fail(
      "an already-public exact PR must reconcile before current upstream fetch"
    ),
  )

  returned_url, number, patch = github_contributions._submit_prepared_pr(
    record, diff_path,
  )

  assert (returned_url, number) == (url, 42)
  assert patch["last_submit_stage"] == "pushed"
  assert patch["last_submit_push_sha"] == head


def test_numbered_pr_recovery_falls_back_to_exact_remote_branch(
  tmp_path, monkeypatch,
):
  """Known-PR metadata lag suppresses a duplicate push from branch truth."""
  record, _diff_path, branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  record.update({
    "number": 42,
    "url": "https://github.com/mobius-os/app-demo/pull/42",
    "head_repository": "octocat/app-demo",
    "last_submit_stage": "pushed",
    "last_submit_push_sha": head,
    "last_submit_base_branch": "main",
  })
  monkeypatch.setattr(
    github_contributions, "_confirm_existing_pr_update", lambda *_a, **_k: None,
  )
  calls = []
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda _repo, slug, remote_branch: (
      calls.append((slug, remote_branch)) or head
    ),
  )

  recovery = github_contributions._authoritative_public_reconciliation(
    record,
    expected_pr_number=42,
    expected_head_repository="octocat/app-demo",
  )

  assert recovery == github_contributions.PublicReconciliation(
    head_repository="octocat/app-demo",
  )
  assert calls == [("octocat/app-demo", branch)]


def test_public_reconciliation_transport_error_fails_closed(
  tmp_path, monkeypatch,
):
  """An unreadable remote branch never upgrades an app-writable journal."""
  record, _diff_path, _branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  record.update({
    "number": 42,
    "url": "https://github.com/mobius-os/app-demo/pull/42",
    "head_repository": "octocat/app-demo",
    "last_submit_stage": "pushed",
    "last_submit_push_sha": head,
    "last_submit_base_branch": "main",
  })
  monkeypatch.setattr(
    github_contributions, "_confirm_existing_pr_update", lambda *_a, **_k: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda *_args, **_kwargs: (_ for _ in ()).throw(
      subprocess.TimeoutExpired("gh api", 1),
    ),
  )
  assert github_contributions._authoritative_public_reconciliation(
    record,
    expected_pr_number=42,
    expected_head_repository="octocat/app-demo",
  ) is None


def test_exact_public_branch_retry_creates_pr_without_repeating_push(
  tmp_path, monkeypatch,
):
  """An exact branch-only lost response resumes at PR creation."""
  record, diff_path, _branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  record.update({
    "last_submit_stage": "pushed",
    "last_submit_push_sha": head,
    "last_submit_upstream_branch": "main",
    "head_repository": "octocat/app-demo",
  })
  monkeypatch.setattr(
    github_contributions,
    "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: github_contributions.PublicReconciliation(
      head_repository="octocat/app-demo",
    ),
  )
  monkeypatch.setattr(
    github_contributions,
    "_existing_branch_pr",
    lambda *_args, **_kwargs: pytest.fail(
      "the authenticated branch recovery already checked public PR truth",
    ),
  )
  monkeypatch.setattr(
    github_contributions,
    "_ensure_owner_fork_remote",
    lambda *_args, **_kwargs: pytest.fail("recovery must not recreate a fork"),
  )
  monkeypatch.setattr(
    github_contributions,
    "_push_topic_branch",
    lambda *_args, **_kwargs: pytest.fail("recovery must not push twice"),
  )
  gh_calls = []

  def fake_gh(_repo, *args, check=True):
    gh_calls.append(args)
    if args[:2] == ("pr", "create"):
      return _cp("https://github.com/mobius-os/app-demo/pull/43\n")
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._gh", fake_gh)
  confirmations = []

  def confirm_created(_repo, upstream, number, **kwargs):
    confirmations.append((upstream, number, kwargs))
    return "https://github.com/mobius-os/app-demo/pull/43", "draft"

  monkeypatch.setattr(
    github_contributions, "_confirm_existing_pr_update", confirm_created,
  )

  returned_url, number, patch = github_contributions._submit_prepared_pr(
    record, diff_path,
  )

  assert (returned_url, number) == (
    "https://github.com/mobius-os/app-demo/pull/43", 43,
  )
  assert len([call for call in gh_calls if call[:2] == ("pr", "create")]) == 1
  assert patch["last_submit_push_sha"] == head
  assert confirmations == [(
    "mobius-os/app-demo",
    43,
    {
      "expected_head_repository": "octocat/app-demo",
      "expected_head_sha": head,
      "branch": "feat/existing-review",
      "base_branch": "main",
    },
  )]


def test_ambiguous_pr_create_receipt_waits_for_exact_read_without_recreate(
  tmp_path, monkeypatch,
):
  record, diff_path, _branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  record.update({
    "last_submit_stage": "pushed",
    "last_submit_push_sha": head,
    "last_submit_upstream_branch": "main",
    "head_repository": "octocat/app-demo",
  })
  monkeypatch.setattr(
    github_contributions,
    "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: github_contributions.PublicReconciliation(
      head_repository="octocat/app-demo",
    ),
  )
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda *_args, **_kwargs: pytest.fail(
      "an ambiguous create receipt must never issue another create"
    ),
  )
  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._submit_prepared_pr(
      record, diff_path, prior_attempt_phase="pr_ambiguous",
    )
  assert caught.value.code == "create_unconfirmed"
  assert caught.value.record_patch["last_submit_push_sha"] == head


def test_ambiguous_push_uses_exact_remote_tip_and_persists_pushed_patch(
  tmp_path, monkeypatch,
):
  """A lost push response advances only after GitHub proves the reviewed SHA."""
  record, diff_path, _branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  monkeypatch.setattr(
    github_contributions,
    "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_contributions, "_existing_branch_pr", lambda *_a, **_k: None,
  )
  monkeypatch.setattr(
    github_contributions,
    "_ensure_owner_fork_remote",
    lambda *_args, **_kwargs: "octocat/app-demo",
  )
  monkeypatch.setattr(
    github_contributions,
    "_push_topic_branch",
    lambda *_args, **_kwargs: "fatal: unable to access: Connection timed out",
  )
  monkeypatch.setattr(
    "app.github_contribution_git._authoritative_upstream_branch_sha",
    lambda *_args, **_kwargs: head,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda _repo, *args, check=True: (
      _cp("https://github.com/mobius-os/app-demo/pull/44\n")
      if args[:2] == ("pr", "create") else _cp("")
    ),
  )
  monkeypatch.setattr(
    github_contributions,
    "_confirm_existing_pr_update",
    lambda *_args, **_kwargs: (
      "https://github.com/mobius-os/app-demo/pull/44", "draft",
    ),
  )
  monkeypatch.setattr(
    github_contributions, "_apply_reviewed_pr_labels", lambda *_a, **_k: {},
  )

  _url, _number, patch = github_contributions._submit_prepared_pr(
    record, diff_path,
  )

  assert patch["last_submit_stage"] == "pushed"
  assert patch["last_submit_push_sha"] == head


def test_create_url_requires_exact_repo_and_direct_pr_confirmation(
  tmp_path, monkeypatch,
):
  """A successful create stdout is not itself publication authority."""
  record, diff_path, _branch, _head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  monkeypatch.setattr(
    github_contributions,
    "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_contributions, "_existing_branch_pr", lambda *_a, **_k: None,
  )
  monkeypatch.setattr(
    github_contributions,
    "_ensure_owner_fork_remote",
    lambda *_args, **_kwargs: "octocat/app-demo",
  )
  monkeypatch.setattr(
    github_contributions, "_push_topic_branch", lambda *_a, **_k: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda _repo, *args, check=True: (
      _cp("https://github.com/octocat/app-demo/pull/45\n")
      if args[:2] == ("pr", "create") else _cp("")
    ),
  )
  confirms = []
  monkeypatch.setattr(
    github_contributions,
    "_confirm_existing_pr_update",
    lambda *_args, **kwargs: confirms.append(kwargs) or None,
  )

  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._submit_prepared_pr(record, diff_path)
  assert "exact expected pull request URL" in caught.value.message
  assert confirms == []


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
    "app.github_contribution_git._authoritative_upstream_branch_sha",
    lambda *_args, **_kwargs: head,
  )
  monkeypatch.setattr(
    "app.github_contributions._push_topic_branch",
    lambda _repo, pushed_branch, source, _expected_remote_sha: (
      pushes.append((pushed_branch, source)) or None
    ),
  )
  confirmations = []

  def confirm(
    _repo,
    upstream,
    number,
    *,
    expected_head_repository,
    expected_head_sha,
    branch,
    base_branch,
    expected_base_sha,
    expected_title,
    expected_body,
  ):
    confirmations.append((
      upstream,
      number,
      expected_head_repository,
      branch,
      expected_head_sha,
      base_branch,
      expected_base_sha,
      expected_title,
      expected_body,
    ))
    return "https://github.com/mobius-os/app-demo/pull/58", "draft"

  monkeypatch.setattr(
    "app.github_contributions._confirm_existing_pr_update", confirm,
  )

  url, number, patch = _submit_prepared_pr(
    record,
    diff_path,
    direct_base_branch="release",
    expected_existing_pr_number=58,
    expected_existing_head_repository="octocat/app-demo",
    expected_existing_head_sha=head,
  )

  assert url.endswith("/pull/58")
  assert number == 58
  assert fork_calls == [("mobius-os/app-demo", "octocat")]
  assert pushes == [(branch, "HEAD")]
  assert confirmations == [
    (
      "mobius-os/app-demo", 58, "octocat/app-demo", branch, head, "release",
      None, record["plan"]["title"], record["plan"]["body_draft"],
    ),
    (
      "mobius-os/app-demo", 58, "octocat/app-demo", branch, head, "release",
      None, record["plan"]["title"], record["plan"]["body_draft"],
    ),
  ]
  assert patch["head_repository"] == "octocat/app-demo"
  assert patch["last_submit_push_sha"] == head
  assert patch["last_submit_base_branch"] == "release"
  assert patch["last_pushed_branch"] == f"octocat:{branch}"
  assert patch["publication_stage"] == "draft"


def test_existing_pr_restack_uses_verified_head_lease(
  tmp_path, monkeypatch,
):
  from app.routes.github import _submit_prepared_pr

  record, diff_path, branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  live_head = "9" * 40
  monkeypatch.setattr(
    "app.github_contribution_git._assert_head_attribution",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    "app.github_contributions._ensure_owner_fork_remote",
    lambda *_args, **_kwargs: "octocat/app-demo",
  )
  monkeypatch.setattr(
    "app.github_contributions._push_topic_branch",
    lambda *_args: pytest.fail("a reviewed restack must use its exact lease"),
  )
  lease_calls = []
  monkeypatch.setattr(
    "app.github_contributions._push_stack_tip_with_lease",
    lambda repo, **kwargs: lease_calls.append((repo, kwargs)),
  )
  monkeypatch.setattr(
    "app.github_contribution_git._authoritative_upstream_branch_sha",
    lambda *_args, **_kwargs: live_head,
  )
  monkeypatch.setattr(
    "app.github_contributions._confirm_existing_pr_update",
    lambda *_args, **_kwargs: (
      "https://github.com/mobius-os/app-demo/pull/58", "draft",
    ),
  )

  _url, number, patch = _submit_prepared_pr(
    record,
    diff_path,
    direct_base_branch="main",
    expected_existing_pr_number=58,
    expected_existing_head_repository="octocat/app-demo",
    expected_existing_head_sha=live_head,
    expected_existing_base_sha=record["plan"]["base_sha"],
    existing_branch_lease_sha=live_head,
  )

  assert number == 58
  assert lease_calls == [(
    Path(record["plan"]["repo_path"]),
    {
      "upstream_repo": "octocat/app-demo",
      "target_branch": branch,
      "expected_base": live_head,
      "landed_sha": head,
    },
  )]
  assert patch["last_submit_push_sha"] == head


def test_existing_pr_restack_preserves_pushed_witness_when_base_moves(
  tmp_path, monkeypatch,
):
  from app.routes.github import ContributionSubmitError, _submit_prepared_pr

  record, diff_path, _branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_head_attribution",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    "app.github_contributions._ensure_owner_fork_remote",
    lambda *_args, **_kwargs: "octocat/app-demo",
  )
  monkeypatch.setattr(
    "app.github_contributions._push_stack_tip_with_lease",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._authoritative_upstream_branch_sha",
    lambda *_args, **_kwargs: "9" * 40,
  )

  confirmations = 0

  def moved_base(*_args, **_kwargs):
    nonlocal confirmations
    confirmations += 1
    if confirmations == 1:
      return "https://github.com/mobius-os/app-demo/pull/58", "draft"
    raise ContributionSubmitError(
      "The reviewed child branch was pushed, but its parent branch moved.",
      code="review_refresh_needed",
      detail="The pull request's base branch moved from the reviewed parent commit.",
    )

  monkeypatch.setattr(
    "app.github_contributions._confirm_existing_pr_update",
    moved_base,
  )

  with pytest.raises(ContributionSubmitError) as caught:
    _submit_prepared_pr(
      record,
      diff_path,
      direct_base_branch="main",
      expected_existing_pr_number=58,
      expected_existing_head_repository="octocat/app-demo",
      expected_existing_head_sha="9" * 40,
      expected_existing_base_sha=record["plan"]["base_sha"],
      existing_branch_lease_sha="9" * 40,
    )

  assert caught.value.code == "review_refresh_needed"
  assert "parent branch moved" in caught.value.message
  assert caught.value.record_patch["last_submit_push_sha"] == head
  assert caught.value.record_patch["last_submit_stage"] == "pushed"


def test_existing_pr_restack_preserves_pushed_witness_when_base_is_unconfirmed(
  tmp_path, monkeypatch,
):
  from app.routes.github import ContributionSubmitError, _submit_prepared_pr

  record, diff_path, _branch, head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_head_attribution",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    "app.github_contributions._ensure_owner_fork_remote",
    lambda *_args, **_kwargs: "octocat/app-demo",
  )
  monkeypatch.setattr(
    "app.github_contributions._push_stack_tip_with_lease",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._authoritative_upstream_branch_sha",
    lambda *_args, **_kwargs: "9" * 40,
  )

  confirmations = 0

  def unconfirmed_base(*_args, **_kwargs):
    nonlocal confirmations
    confirmations += 1
    if confirmations == 1:
      return "https://github.com/mobius-os/app-demo/pull/58", "draft"
    raise ContributionSubmitError(
      "The reviewed child branch was pushed, but GitHub did not confirm its "
      "current parent branch.",
      status_code=503,
      code="update_unconfirmed",
      detail="The current base branch tip could not be verified after the push.",
    )

  monkeypatch.setattr(
    "app.github_contributions._confirm_existing_pr_update",
    unconfirmed_base,
  )

  with pytest.raises(ContributionSubmitError) as caught:
    _submit_prepared_pr(
      record,
      diff_path,
      direct_base_branch="main",
      expected_existing_pr_number=58,
      expected_existing_head_repository="octocat/app-demo",
      expected_existing_head_sha="9" * 40,
      expected_existing_base_sha=record["plan"]["base_sha"],
      existing_branch_lease_sha="9" * 40,
    )

  assert caught.value.status_code == 503
  assert caught.value.code == "update_unconfirmed"
  assert caught.value.record_patch["last_submit_push_sha"] == head
  assert caught.value.record_patch["last_submit_stage"] == "pushed"


def test_existing_pr_branch_lease_requires_a_reviewed_stack_base(
  tmp_path, monkeypatch,
):
  from app.routes.github import ContributionSubmitError, _submit_prepared_pr

  record, diff_path, _branch, _head = _existing_fork_submission(
    tmp_path, monkeypatch,
  )

  with pytest.raises(ContributionSubmitError, match="stack update"):
    _submit_prepared_pr(
      record,
      diff_path,
      expected_existing_pr_number=58,
      expected_existing_head_repository="octocat/app-demo",
      expected_existing_head_sha="9" * 40,
      existing_branch_lease_sha="9" * 40,
    )


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
    "app.github_contributions._confirm_existing_pr_update",
    lambda *_args, **_kwargs: (
      "https://github.com/mobius-os/app-demo/pull/58", "draft"
    ),
  )

  with pytest.raises(ContributionSubmitError) as err:
    _submit_prepared_pr(
      record,
      diff_path,
        expected_existing_pr_number=58,
        expected_existing_head_repository="octocat/app-demo",
        expected_existing_head_sha=record["plan"]["head_sha"],
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
        expected_existing_head_sha=record["plan"]["head_sha"],
    )

  assert "not owned by the connected GitHub account" in err.value.message
  assert "Nothing was pushed" in err.value.message


def test_land_contribution_stack_marks_every_layer_merged(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  stack_id = "green-app-stack"
  record_ids = ["green-stack-01", "green-stack-02"]
  base = "b" * 40
  parent_head = "a" * 40
  top_head = "c" * 40
  for position, record_id in enumerate(record_ids, 1):
    repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  stack_id = "red-app-stack"
  ids = ["red-stack-01", "red-stack-02"]
  for position, record_id in enumerate(ids, 1):
    repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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


def test_land_contribution_stack_rechecks_current_source_before_push(
  client, owner_token, monkeypatch,
):
  """Landing cannot reintroduce a reviewed change removed from live source."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _repo, record, diff_text = _prepared_real_review(app_id, "land-source-moved")
  record.update({
    "status": "open",
    "number": 91,
    "url": "https://github.com/mobius-os/app-demo/pull/91",
    "head_repository": "octocat/app-demo",
  })
  record_ids = _add_source_proof_stack_child(app_id, record, status="open")
  _write_contribution(app_id, record["id"], record, diff_text)
  _remove_reviewed_change_from_source(record)
  monkeypatch.setattr(
    github_routes,
    "_land_reviewed_stack",
    lambda *_args: pytest.fail("reverted source must never land"),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/land-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "source_provenance_mismatch"
  assert response.json()["detail"]["records"][0]["status"] == "open"


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


def test_stack_tip_push_keeps_journal_when_transport_and_probe_raise(
  monkeypatch, tmp_path,
):
  """Raised transport errors remain an ambiguous public outcome."""
  from app.routes.github import ContributionSubmitError, _push_stack_tip_with_lease

  monkeypatch.setattr("app.github_contributions._PUSH_RETRIES", 1)
  monkeypatch.setattr(
    "app.github_contribution_git._git",
    lambda *_args, **_kwargs: (_ for _ in ()).throw(
      subprocess.TimeoutExpired("git push", 1),
    ),
  )
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda *_args, **_kwargs: (_ for _ in ()).throw(
      OSError("temporary GitHub transport failure"),
    ),
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


def test_stack_tip_push_timeout_stays_ambiguous_when_probe_is_still_old(
  monkeypatch, tmp_path,
):
  """An early old-tip read cannot prove a timed-out push will not land later."""
  from app.routes.github import ContributionSubmitError, _push_stack_tip_with_lease

  monkeypatch.setattr("app.github_contributions._PUSH_RETRIES", 1)
  monkeypatch.setattr(
    "app.github_contribution_git._git",
    lambda *_args, **_kwargs: (_ for _ in ()).throw(
      subprocess.TimeoutExpired("git push", 1),
    ),
  )
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda *_args, **_kwargs: "b" * 40,
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
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-diff-mismatch"
  repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-merge-conflict"
  repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "rec-pr-push-then-fail"
  repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
    "quality_review": _all_clear_review(head),
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
        Path(get_settings().data_dir) / "contrib" / record_id / "repo"
      ),
          "head_sha": "a" * 40,
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
        Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
      "base_sha": "b" * 40,
      "head_sha": "a" * 40,
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
      "pr_metadata": {
        "old_title": "Refine the existing contribution",
        "old_body": "## Summary\n\nRefines the open contribution.",
      },
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


def _prepared_merged_parent_successor(app_id: int, record_id: str) -> dict:
  """A standalone reviewed successor that reuses the ordinary update action."""
  record = _prepared_existing_pr_update(app_id, record_id)
  record["plan"]["base_sha"] = _SUCCESSOR_TARGET_BASE_SHA
  record["plan"]["head_sha"] = _SUCCESSOR_NEW_HEAD
  record["plan"]["successor"] = {
    "old_head_sha": _SUCCESSOR_OLD_HEAD,
    "old_base_branch": _SUCCESSOR_OLD_BASE_BRANCH,
    "old_base_sha": _SUCCESSOR_OLD_BASE_SHA,
    "base_branch": _SUCCESSOR_TARGET_BASE_BRANCH,
  }
  record["quality_review"]["reviewed_head_sha"] = _SUCCESSOR_NEW_HEAD
  _write_contribution(app_id, record_id, record, "reviewed successor diff")
  return record


def _prepared_existing_pr_update_stack(app_id: int) -> tuple[list[str], list[dict]]:
  stack_id = "existing-update-stack"
  record_ids = [f"{stack_id}-01", f"{stack_id}-02"]
  parent_head = "a" * 40
  specs = [
    (record_ids[0], 1, "main", "", "b" * 40, parent_head, 58),
    (
      record_ids[1], 2, f"stack/{stack_id}/01-parent", record_ids[0],
      parent_head, "c" * 40, 59,
    ),
  ]
  records = []
  for record_id, position, base_branch, parent_id, base_sha, head_sha, number in specs:
    repo_path = (
      Path(get_settings().data_dir) / "contrib" / record_id / "worktree"
    )
    (repo_path / ".git").mkdir(parents=True, exist_ok=True)
    branch = f"stack/{stack_id}/0{position}-" + (
      "parent" if position == 1 else "child"
    )
    diff_text = f"diff --git a/{record_id} b/{record_id}\n+reviewed\n"
    record = {
      "id": record_id,
      "type": "pr",
      "repo": "mobius-os/app-demo",
      "status": "prepared",
      "title": f"Refine stack layer {position}",
      "branch": branch,
      "number": number,
      "url": f"https://github.com/mobius-os/app-demo/pull/{number}",
      "head_repository": "octocat/app-demo",
      "submitted_at": f"2026-08-2{position}T12:00:00Z",
      "plan": {
        "action": "pr_update",
        "repo": "mobius-os/app-demo",
        "title": f"Refine stack layer {position}",
        "body_draft": f"Reviewed stack update {position}.",
        "pr_metadata": {
          "old_title": f"Refine stack layer {position}",
          "old_body": f"Reviewed stack update {position}.",
        },
        "branch": branch,
        "repo_path": str(repo_path),
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
      "quality_review": {
        "state": "all_clear",
        "reviewed_head_sha": head_sha,
        "reviewed_at": "2026-08-24T18:00:00Z",
      },
    }
    _write_contribution(app_id, record_id, record, diff_text)
    records.append(record)
  return record_ids, records


def _reviewed_live_target(record: dict, **fields) -> dict:
  return {
    "error": None,
    "title": record["plan"]["title"],
    "body": record["plan"]["body_draft"],
    **fields,
  }


def test_existing_pr_stack_update_fast_forwards_complete_chain_parent_first(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, originals = _prepared_existing_pr_update_stack(app_id)
  calls = []
  base_reads = []

  monkeypatch.setattr(
    github_routes,
    "_preflight_prepared_stack",
    lambda rows, **_kwargs: calls.append(("preflight", [row["record"]["id"] for row in rows])),
  )
  def current_base(_repo, upstream, branch):
    base_reads.append((upstream, branch))
    return originals[0]["plan"]["head_sha"]

  monkeypatch.setattr(github_routes, "_upstream_branch_sha", current_base)

  def live_target(repo, number, head_repo, branch):
    calls.append(("target", number, branch))
    record = originals[number - 58]
    return _reviewed_live_target(
      record,
      head_sha=str(number - 50) * 40,
      base_branch="main" if number == 58 else originals[0]["branch"],
    )

  monkeypatch.setattr(github_routes, "_autopilot_live_target", live_target)
  monkeypatch.setattr(
    github_routes,
    "_assert_reviewed_update_contains_live_head",
    lambda _repo, live, reviewed: calls.append(("ancestry", live, reviewed)),
  )

  def submit(record, _diff_path, **kwargs):
    calls.append((
      "submit", record["id"], kwargs["direct_base_branch"],
      kwargs["expected_existing_pr_number"],
    ))
    number = int(record["number"])
    return (
      f"https://github.com/mobius-os/app-demo/pull/{number}",
      number,
      {"last_submit_push_sha": record["plan"]["head_sha"]},
    )

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", submit)
  response = client.post(
    f"/api/github/contributions/{app_id}/update-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 200, response.text
  body = response.json()
  assert [record["status"] for record in body["records"]] == ["open", "open"]
  assert [item["number"] for item in body["updated"]] == [58, 59]
  assert [record["submitted_at"] for record in body["records"]] == [
    record["submitted_at"] for record in originals
  ]
  assert [call for call in calls if call[0] == "submit"] == [
    ("submit", record_ids[0], "main", 58),
    ("submit", record_ids[1], originals[0]["branch"], 59),
  ]
  assert base_reads == [("mobius-os/app-demo", originals[0]["branch"])]


def test_existing_pr_stack_update_restacks_child_with_recorded_head_lease(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, records = _prepared_existing_pr_update_stack(app_id)
  public_heads = ["8" * 40, "9" * 40]
  for record, public_head in zip(records, public_heads, strict=True):
    record["last_submit_push_sha"] = public_head
    diff_text = f"diff --git a/{record['id']} b/{record['id']}\n+reviewed\n"
    _write_contribution(app_id, record["id"], record, diff_text)
  monkeypatch.setattr(
    github_routes, "_preflight_prepared_stack", lambda _rows, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_routes,
    "_upstream_branch_sha",
    lambda _repo, _upstream, _branch: records[0]["plan"]["head_sha"],
  )

  def live_target(_repo, number, _head_repo, _branch):
    position = number - 57
    return _reviewed_live_target(
      records[position - 1],
      head_sha=public_heads[position - 1],
      base_branch=(
        "main" if position == 1 else records[0]["branch"]
      ),
      base_sha=(
        records[0]["plan"]["base_sha"]
        if position == 1 else records[0]["plan"]["head_sha"]
      ),
    )

  monkeypatch.setattr(github_routes, "_autopilot_live_target", live_target)

  def ancestry(_repo, *args, check=True):
    assert args[:2] == ("merge-base", "--is-ancestor")
    assert check is False
    return _cp("", returncode=0 if args[2] == public_heads[0] else 1)

  monkeypatch.setattr(github_routes, "_git", ancestry)
  submit_calls = []

  def submit(record, _diff_path, **kwargs):
    submit_calls.append((
      record["id"],
      kwargs.get("existing_branch_lease_sha"),
      kwargs.get("expected_existing_base_sha"),
    ))
    number = int(record["number"])
    return (
      f"https://github.com/mobius-os/app-demo/pull/{number}",
      number,
      {"last_submit_push_sha": record["plan"]["head_sha"]},
    )

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", submit)
  response = client.post(
    f"/api/github/contributions/{app_id}/update-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 200, response.text
  assert submit_calls == [
    (record_ids[0], None, None),
    (record_ids[1], public_heads[1], records[0]["plan"]["head_sha"]),
  ]


def test_existing_pr_stack_update_rejects_moved_parent_tip_before_child_push(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, records = _prepared_existing_pr_update_stack(app_id)
  parent = records[0]
  parent["status"] = "open"
  parent["plan"]["action"] = "pr"
  _write_contribution(
    app_id,
    record_ids[0],
    parent,
    f"diff --git a/{record_ids[0]} b/{record_ids[0]}\n+reviewed\n",
  )
  monkeypatch.setattr(
    github_routes, "_preflight_prepared_stack", lambda _rows, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_routes,
    "_upstream_branch_sha",
    lambda _repo, _upstream, _branch: "d" * 40,
  )
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: _reviewed_live_target(
      records[1],
      head_sha="9" * 40,
      base_branch=parent["branch"],
      base_sha=parent["plan"]["head_sha"],
    ),
  )
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail(
      "a moved reviewed parent must stop before the child push",
    ),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/update-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 409, response.text
  detail = response.json()["detail"]
  assert detail["code"] == "review_refresh_needed"
  assert detail["updated"] == []
  assert "base branch moved" in detail["detail"]
  assert [record["status"] for record in detail["records"]] == [
    "open", "prepared",
  ]


@pytest.mark.parametrize("lookup_error", [False, True])
def test_existing_pr_stack_update_stops_when_parent_tip_cannot_be_verified(
  client, owner_token, monkeypatch, lookup_error,
):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, records = _prepared_existing_pr_update_stack(app_id)
  parent = records[0]
  parent["status"] = "open"
  parent["plan"]["action"] = "pr"
  _write_contribution(
    app_id,
    record_ids[0],
    parent,
    f"diff --git a/{record_ids[0]} b/{record_ids[0]}\n+reviewed\n",
  )
  monkeypatch.setattr(
    github_routes, "_preflight_prepared_stack", lambda _rows, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: _reviewed_live_target(
      records[1],
      head_sha="9" * 40,
      base_branch=parent["branch"],
      base_sha=parent["plan"]["head_sha"],
    ),
  )

  def unreadable(*_args):
    if lookup_error:
      raise OSError("offline")
    return None

  monkeypatch.setattr(github_routes, "_upstream_branch_sha", unreadable)
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail(
      "an unreadable parent ref must stop before the child push",
    ),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/update-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 409, response.text
  detail = response.json()["detail"]
  assert detail["code"] == "review_refresh_needed"
  assert detail["updated"] == []
  assert "could not be verified" in detail["detail"]
  assert [record["status"] for record in detail["records"]] == [
    "open", "prepared",
  ]


def test_update_stack_outer_preflight_failure_settles_every_armed_claim(
  client, owner_token, monkeypatch,
):
  """A proven pre-publication rejection leaves no private attempt stranded."""
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, _records = _prepared_existing_pr_update_stack(app_id)
  monkeypatch.setattr(
    github_routes,
    "_preflight_prepared_stack",
    lambda *_args, **_kwargs: (_ for _ in ()).throw(
      ContributionSubmitError("The reviewed stack is stale.")
    ),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/update-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 409, response.text
  assert [row["status"] for row in response.json()["detail"]["records"]] == [
    "prepared", "prepared",
  ]
  for record_id in record_ids:
    assert not github_routes.contribution_runtime.personal_attempt_path(
      app_id, record_id,
    ).exists()


def test_stack_update_never_acquires_app_lock_inside_source_lock(
  client, owner_token, monkeypatch,
):
  """The route cannot form a cycle with normal app -> source operations."""
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, originals = _prepared_existing_pr_update_stack(app_id)
  source_depth = 0

  @asynccontextmanager
  async def observed_source_lock(_path):
    nonlocal source_depth
    source_depth += 1
    await asyncio.sleep(0)
    try:
      yield
    finally:
      source_depth -= 1

  real_app_lock = github_routes.fs_locks.app_storage_lock

  @asynccontextmanager
  async def observed_app_lock(requested_app_id):
    assert source_depth == 0, (
      "stack persistence must release source before taking app storage"
    )
    async with real_app_lock(requested_app_id):
      yield

  monkeypatch.setattr(
    github_routes.fs_locks, "source_dir_lock", observed_source_lock,
  )
  monkeypatch.setattr(
    github_routes.fs_locks, "app_storage_lock", observed_app_lock,
  )
  monkeypatch.setattr(
    github_routes,
    "_preflight_prepared_stack",
    lambda _rows, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_routes,
    "_assert_personal_publication_source",
    lambda *_args: "exact_tree",
  )
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda _repo, number, _head_repo, _branch: _reviewed_live_target(
      originals[number - 58],
      head_sha=str(number - 50) * 40,
      base_branch="main" if number == 58 else originals[0]["branch"],
    ),
  )
  monkeypatch.setattr(
    github_routes, "_assert_reviewed_update_contains_live_head",
    lambda *_args: None,
  )
  monkeypatch.setattr(
    github_routes,
    "_upstream_branch_sha",
    lambda _repo, _upstream, _branch: originals[0]["plan"]["head_sha"],
  )
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda record, _diff_path, **_kwargs: (
      f"https://github.com/mobius-os/app-demo/pull/{record['number']}",
      int(record["number"]),
      {"last_submit_push_sha": record["plan"]["head_sha"]},
    ),
  )

  async def skip_witness(*_args, **_kwargs):
    return None

  monkeypatch.setattr(
    github_routes, "_record_pending_equivalence_locked", skip_witness,
  )

  started = time.monotonic()
  response = client.post(
    f"/api/github/contributions/{app_id}/update-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 200, response.text
  assert time.monotonic() - started < 5
  assert source_depth == 0


def test_existing_pr_stack_update_accepts_unchanged_public_create_parent(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, originals = _prepared_existing_pr_update_stack(app_id)
  parent = originals[0]
  parent["status"] = "open"
  parent["plan"]["action"] = "pr"
  _write_contribution(
    app_id,
    record_ids[0],
    parent,
    f"diff --git a/{record_ids[0]} b/{record_ids[0]}\n+reviewed\n",
  )
  monkeypatch.setattr(
    github_routes, "_preflight_prepared_stack", lambda _rows, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_routes,
    "_upstream_branch_sha",
    lambda _repo, _upstream, _branch: originals[0]["plan"]["head_sha"],
  )
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda _repo, number, _head_repo, _branch: _reviewed_live_target(
      originals[number - 58],
      head_sha="9" * 40,
      base_branch=originals[0]["branch"],
    ),
  )
  monkeypatch.setattr(
    github_routes,
    "_assert_reviewed_update_contains_live_head",
    lambda *_args: None,
  )
  submitted = []

  def submit(record, _diff_path, **_kwargs):
    submitted.append(record["id"])
    return (
      "https://github.com/mobius-os/app-demo/pull/59",
      59,
      {"last_submit_push_sha": record["plan"]["head_sha"]},
    )

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", submit)
  response = client.post(
    f"/api/github/contributions/{app_id}/update-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 200, response.text
  body = response.json()
  assert submitted == [record_ids[1]]
  assert body["updated"] == [{
    "id": record_ids[1],
    "url": "https://github.com/mobius-os/app-demo/pull/59",
    "number": 59,
  }]
  assert [record["status"] for record in body["records"]] == ["open", "open"]


def test_existing_pr_stack_update_defers_unpublished_child_for_next_phase(
  client, owner_token, monkeypatch,
):
  """One approval updates the public prefix without claiming a new child."""
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, originals = _prepared_existing_pr_update_stack(app_id)
  child = originals[1]
  child["plan"]["action"] = "pr"
  for field in ("number", "url", "head_repository", "submitted_at"):
    child.pop(field, None)
  _write_contribution(
    app_id,
    record_ids[1],
    child,
    f"diff --git a/{record_ids[1]} b/{record_ids[1]}\n+reviewed\n",
  )
  preflight_statuses = []
  monkeypatch.setattr(
    github_routes,
    "_preflight_prepared_stack",
    lambda rows, **_kwargs: preflight_statuses.append([
      row["record"]["status"] for row in rows
    ]),
  )
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: _reviewed_live_target(
      originals[0], head_sha="8" * 40, base_branch="main",
    ),
  )
  monkeypatch.setattr(
    github_routes, "_assert_reviewed_update_contains_live_head",
    lambda *_args: None,
  )
  submitted = []

  def submit(record, _diff_path, **_kwargs):
    submitted.append(record["id"])
    return (
      "https://github.com/mobius-os/app-demo/pull/58",
      58,
      {"last_submit_push_sha": record["plan"]["head_sha"]},
    )

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", submit)
  response = client.post(
    f"/api/github/contributions/{app_id}/update-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 200, response.text
  body = response.json()
  assert submitted == [record_ids[0]]
  assert preflight_statuses == [["submitting", "prepared"]]
  assert [record["status"] for record in body["records"]] == [
    "open", "prepared",
  ]
  assert body["updated"] == [{
    "id": record_ids[0],
    "url": "https://github.com/mobius-os/app-demo/pull/58",
    "number": 58,
  }]
  assert "submit_started_at" not in body["records"][1]


def test_existing_pr_stack_update_never_claims_private_create_layer(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, originals = _prepared_existing_pr_update_stack(app_id)
  parent = originals[0]
  parent["plan"]["action"] = "pr"
  _write_contribution(
    app_id,
    record_ids[0],
    parent,
    f"diff --git a/{record_ids[0]} b/{record_ids[0]}\n+reviewed\n",
  )
  monkeypatch.setattr(
    github_routes,
    "_preflight_prepared_stack",
    lambda _rows, **_kwargs: pytest.fail("an unapproved create layer must not reach preflight"),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/update-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"] == (
    "An existing pull-request update cannot follow a new private stack layer."
  )


def test_new_stack_phase_accepts_settled_existing_pr_update_parent(
  client, owner_token, monkeypatch,
):
  """After the update phase settles, a second approval opens only the child."""
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, originals = _prepared_existing_pr_update_stack(app_id)
  parent, child = originals
  parent["status"] = "open"
  child["plan"]["action"] = "pr"
  for field in ("number", "url", "head_repository", "submitted_at"):
    child.pop(field, None)
  for record_id, record in zip(record_ids, (parent, child), strict=True):
    _write_contribution(
      app_id,
      record_id,
      record,
      f"diff --git a/{record_id} b/{record_id}\n+reviewed\n",
    )
  preflight_statuses = []
  monkeypatch.setattr(
    github_routes,
    "_preflight_prepared_stack",
    lambda rows, **_kwargs: preflight_statuses.append([
      row["record"]["status"] for row in rows
    ]),
  )
  submitted = []

  def submit(record, _diff_path, **_kwargs):
    submitted.append(record["id"])
    return (
      "https://github.com/mobius-os/app-demo/pull/59",
      59,
      {
        "last_submit_push_sha": record["plan"]["head_sha"],
        "publication_stage": "ready",
      },
    )

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", submit)
  response = client.post(
    f"/api/github/contributions/{app_id}/submit-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids, "publication_stage": "ready"},
  )

  assert response.status_code == 200, response.text
  body = response.json()
  assert submitted == [record_ids[1]]
  assert preflight_statuses == [["open", "submitting"]]
  assert [record["status"] for record in body["records"]] == ["open", "open"]
  assert body["submitted"] == [{
    "id": record_ids[1],
    "url": "https://github.com/mobius-os/app-demo/pull/59",
    "number": 59,
  }]


def test_existing_pr_stack_update_rejects_newer_live_head_before_any_push(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, originals = _prepared_existing_pr_update_stack(app_id)
  monkeypatch.setattr(
    github_routes, "_preflight_prepared_stack", lambda _rows, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda _repo, number, _head_repo, _branch: _reviewed_live_target(
      originals[number - 58],
      head_sha=str(number - 50) * 40,
      base_branch="main" if number == 58 else originals[0]["branch"],
    ),
  )

  def nonancestor(_repo, *args, check=True):
    assert args[:2] == ("merge-base", "--is-ancestor")
    assert check is False
    return subprocess.CompletedProcess(args, 1, "", "")

  monkeypatch.setattr(github_routes, "_git", nonancestor)
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail(
      "a non-ancestor review must stop before any push",
    ),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/update-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 409, response.text
  detail = response.json()["detail"]
  assert detail["code"] == "review_refresh_needed"
  assert detail["updated"] == []
  assert [record["status"] for record in detail["records"]] == [
    "prepared", "prepared",
  ]


def test_existing_pr_stack_update_never_rewrites_when_ancestry_check_fails(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, records = _prepared_existing_pr_update_stack(app_id)
  parent = records[0]
  parent["status"] = "open"
  parent["plan"]["action"] = "pr"
  _write_contribution(
    app_id,
    record_ids[0],
    parent,
    f"diff --git a/{record_ids[0]} b/{record_ids[0]}\n+reviewed\n",
  )
  child_live_head = "9" * 40
  child = records[1]
  child["last_submit_push_sha"] = child_live_head
  _write_contribution(
    app_id,
    record_ids[1],
    child,
    f"diff --git a/{record_ids[1]} b/{record_ids[1]}\n+reviewed\n",
  )
  monkeypatch.setattr(
    github_routes, "_preflight_prepared_stack", lambda _rows, **_kwargs: None,
  )
  monkeypatch.setattr(
    github_routes,
    "_upstream_branch_sha",
    lambda _repo, _upstream, _branch: parent["plan"]["head_sha"],
  )
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: _reviewed_live_target(
      child,
      head_sha=child_live_head,
      base_branch=parent["branch"],
      base_sha=parent["plan"]["head_sha"],
    ),
  )
  monkeypatch.setattr(
    github_routes,
    "_git",
    lambda *_args, **_kwargs: _cp("", returncode=128, stderr="bad object"),
  )
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail(
      "an unverified ancestry comparison must never authorize a rewrite",
    ),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/update-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 409, response.text
  detail = response.json()["detail"]
  assert detail["code"] == "review_refresh_needed"
  assert "could not compare" in detail["detail"]
  assert detail["updated"] == []


def test_existing_pr_stack_update_preserves_parent_when_child_fails(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_ids, originals = _prepared_existing_pr_update_stack(app_id)
  monkeypatch.setattr(github_routes, "_preflight_prepared_stack", lambda _rows, **_kwargs: None)
  monkeypatch.setattr(
    github_routes,
    "_upstream_branch_sha",
    lambda _repo, _upstream, _branch: originals[0]["plan"]["head_sha"],
  )
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda _repo, number, _head_repo, _branch: _reviewed_live_target(
      originals[number - 58],
      head_sha=str(number - 50) * 40,
      base_branch="main" if number == 58 else originals[0]["branch"],
    ),
  )
  monkeypatch.setattr(
    github_routes,
    "_assert_reviewed_update_contains_live_head",
    lambda *_args: None,
  )
  calls = []

  def submit(record, _diff_path, **_kwargs):
    calls.append(record["id"])
    if len(calls) == 2:
      raise ContributionSubmitError("Child update was rejected.")
    return (
      "https://github.com/mobius-os/app-demo/pull/58",
      58,
      {"last_submit_push_sha": record["plan"]["head_sha"]},
    )

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", submit)
  response = client.post(
    f"/api/github/contributions/{app_id}/update-stack",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"record_ids": record_ids},
  )

  assert response.status_code == 409, response.text
  detail = response.json()["detail"]
  assert calls == record_ids
  assert [record["status"] for record in detail["records"]] == [
    "open", "prepared",
  ]
  assert detail["records"][1]["last_submit_error"] == (
    "Child update was rejected."
  )
  assert detail["updated"] == [{
    "id": record_ids[0],
    "url": "https://github.com/mobius-os/app-demo/pull/58",
    "number": 58,
  }]


def test_existing_pr_target_returns_the_authoritative_pr_base_snapshot(monkeypatch):
  _write_token(login="octocat")
  live = {
    "state": "open",
    "title": "Reviewed title",
    "body": "Reviewed body",
    "head": {
      "ref": "feat/existing-review",
      "sha": "9" * 40,
      "repo": {"full_name": "octocat/app-demo"},
    },
    "base": {
      "ref": "stack/review/01-parent",
      "sha": "8" * 40,
      "repo": {"full_name": "mobius-os/app-demo"},
    },
  }
  monkeypatch.setattr(github_routes.shutil, "which", lambda _name: "/bin/gh")
  monkeypatch.setattr(
    github_routes.subprocess,
    "run",
    lambda *_args, **_kwargs: _cp(json.dumps(live)),
  )

  target = github_routes._autopilot_live_target(
    "mobius-os/app-demo", 58, "octocat/app-demo", "feat/existing-review",
  )

  assert target == {
    "error": None,
    "head_sha": "9" * 40,
    "base_branch": "stack/review/01-parent",
    "base_sha": "8" * 40,
    "title": "Reviewed title",
    "body": "Reviewed body",
  }


def test_existing_pr_update_uses_owner_approved_exact_target(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  record_id = "existing-pr-update"
  original = _prepared_existing_pr_update(app_id, record_id)
  calls = []

  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda repo, number, head_repo, branch: calls.append(
      ("target", repo, number, head_repo, branch)
    ) or _reviewed_live_target(
      original, head_sha="9" * 40, base_branch="release",
    ),
  )
  monkeypatch.setattr(
    github_routes,
    "_assert_reviewed_update_contains_live_head",
    lambda repo_path, live_head, reviewed_head: calls.append(
      ("ancestry", repo_path.name, live_head, reviewed_head)
    ),
  )

  def submit(
    record,
    diff_path,
    *,
    direct_base_branch=None,
    expected_existing_pr_number=None,
    expected_existing_head_repository=None,
    expected_existing_head_sha=None,
    attempt_event=None,
    prior_attempt_phase=None,
    prior_attempt_receipt=None,
  ):
    calls.append((
      "submit",
      record["status"],
      direct_base_branch,
      expected_existing_pr_number,
      expected_existing_head_repository,
      diff_path.name,
    ))
    return (
      "https://github.com/mobius-os/app-demo/pull/58",
      58,
      {
        "last_submit_push_sha": record["plan"]["head_sha"],
        "publication_stage": "draft",
      },
    )

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", submit)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update-existing",
    headers={"Authorization": f"Bearer {app_token}"},
    json={},
  )

  assert response.status_code == 200, response.text
  updated = response.json()["record"]
  assert updated["status"] == "draft"
  assert updated["publication_stage"] == "draft"
  assert updated["number"] == 58
  assert updated["submitted_at"] == original["submitted_at"]
  assert updated["last_submit_push_sha"] == original["plan"]["head_sha"]
  assert updated["last_updated_pr_at"]
  assert calls == [
    (
      "target", "mobius-os/app-demo", 58,
      "octocat/app-demo", "feat/existing-review",
    ),
    ("ancestry", "worktree", "9" * 40, original["plan"]["head_sha"]),
    (
      "submit", "submitting", "release", 58, "octocat/app-demo",
      f"{record_id}.diff",
    ),
  ]


def test_existing_pr_update_routes_detached_successor_through_state_machine(
  client, owner_token, monkeypatch,
):
  """The existing update endpoint owns both ordinary and successor updates."""
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = "existing-pr-successor"
  original = _prepared_merged_parent_successor(app_id, record_id)
  calls = []
  live_targets = [
    {
      "error": None,
      "head_sha": _SUCCESSOR_OLD_HEAD,
      "base_branch": _SUCCESSOR_OLD_BASE_BRANCH,
      "base_sha": _SUCCESSOR_OLD_BASE_SHA,
      "title": original["plan"]["pr_metadata"]["old_title"],
      "body": original["plan"]["pr_metadata"]["old_body"],
    },
    {
      "error": None,
      "head_sha": _SUCCESSOR_NEW_HEAD,
      "base_branch": _SUCCESSOR_TARGET_BASE_BRANCH,
      "base_sha": _SUCCESSOR_TARGET_BASE_SHA,
      "title": original["plan"]["title"],
      "body": original["plan"]["body_draft"],
    },
  ]
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: live_targets.pop(0),
  )
  monkeypatch.setattr(
    github_routes,
    "_assert_reviewed_update_contains_live_head",
    lambda *_args: pytest.fail("a reviewed successor is intentionally non-fast-forward"),
  )
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail("successors use the owning state machine"),
  )

  def advance(record, diff_path, **kwargs):
    calls.append((record["status"], diff_path.name, kwargs))
    return (
      "https://github.com/mobius-os/app-demo/pull/58",
      58,
      {
        "last_submit_push_sha": record["plan"]["head_sha"],
        "last_submit_base_branch": _SUCCESSOR_TARGET_BASE_BRANCH,
        "publication_stage": "ready",
      },
    )

  monkeypatch.setattr(github_routes, "_advance_merged_parent_successor", advance)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update-existing",
    headers={"Authorization": f"Bearer {app_token}"},
    json={},
  )

  assert response.status_code == 200, response.text
  updated = response.json()["record"]
  assert updated["status"] == "open"
  assert updated["plan"]["action"] == "pr_update"
  assert updated["plan"]["successor"] == original["plan"]["successor"]
  assert len(calls) == 1
  status, diff_name, kwargs = calls[0]
  assert status == "submitting"
  assert diff_name == f"{record_id}.diff"
  assert kwargs["expected_number"] == 58
  assert kwargs["expected_head_repository"] == "octocat/app-demo"
  assert kwargs["live_head_sha"] == _SUCCESSOR_OLD_HEAD
  assert kwargs["live_base_branch"] == _SUCCESSOR_OLD_BASE_BRANCH
  assert callable(kwargs["attempt_event"])
  assert live_targets == []


def test_existing_pr_successor_never_settles_while_public_base_is_old(
  client, owner_token, monkeypatch,
):
  """A pushed head with an ignored retarget remains resumable, never success."""
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = "existing-pr-successor-old-public-base"
  original = _prepared_merged_parent_successor(app_id, record_id)
  live_targets = [
    {
      "error": None,
      "head_sha": _SUCCESSOR_OLD_HEAD,
      "base_branch": _SUCCESSOR_OLD_BASE_BRANCH,
      "base_sha": _SUCCESSOR_OLD_BASE_SHA,
      "title": original["plan"]["pr_metadata"]["old_title"],
      "body": original["plan"]["pr_metadata"]["old_body"],
    },
    {
      "error": None,
      "head_sha": _SUCCESSOR_NEW_HEAD,
      "base_branch": _SUCCESSOR_OLD_BASE_BRANCH,
      "base_sha": _SUCCESSOR_OLD_BASE_SHA,
      "title": original["plan"]["title"],
      "body": original["plan"]["body_draft"],
    },
  ]
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: live_targets.pop(0),
  )
  monkeypatch.setattr(
    github_routes,
    "_advance_merged_parent_successor",
    lambda *_args, **_kwargs: (
      "https://github.com/mobius-os/app-demo/pull/58",
      58,
      {
        "last_submit_push_sha": _SUCCESSOR_NEW_HEAD,
        # Model the field incident: an inner helper claimed success while
        # retaining the obsolete parent branch as submit metadata.
        "last_submit_base_branch": _SUCCESSOR_OLD_BASE_BRANCH,
        "publication_stage": "ready",
      },
    ),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update-existing",
    headers={"Authorization": f"Bearer {app_token}"},
    json={},
  )

  assert response.status_code == 503, response.text
  detail = response.json()["detail"]
  assert detail["code"] == "update_unconfirmed"
  saved = detail["record"]
  assert saved["status"] == "submitting"
  assert saved["last_submit_push_sha"] == _SUCCESSOR_NEW_HEAD
  assert saved["last_successor_base_branch"] == _SUCCESSOR_TARGET_BASE_BRANCH
  assert saved.get("last_submit_base_branch") != _SUCCESSOR_OLD_BASE_BRANCH
  assert live_targets == []


def test_existing_pr_successor_resumes_its_exact_submitting_claim(
  client, owner_token, monkeypatch,
):
  """A post-push checkpoint resumes even after installed source moves."""
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = "existing-pr-successor-resume"
  record = _prepared_merged_parent_successor(app_id, record_id)
  record["status"] = "submitting"
  record["submitter"] = "contribute-update-button"
  record["submit_started_at"] = "2026-08-30T22:00:00Z"
  record["personal_submit_input_sha256"] = (
    github_contributions._personal_publication_input_sha256(record)
  )
  _write_contribution(app_id, record_id, record, "reviewed successor diff")
  record_path = (
    Path(get_settings().data_dir) / "apps" / str(app_id)
    / "contributions" / f"{record_id}.json"
  )
  github_routes._PersonalAttemptOwner(
    app_id=app_id, record_id=record_id, record_path=record_path,
    claimed=record, action_input={"action": "update_existing"},
  ).event(
    "push_pending",
    {
      "action": "advance_successor_branch",
      "previous_head_sha": _SUCCESSOR_OLD_HEAD,
      "head_sha": _SUCCESSOR_NEW_HEAD,
    },
    {
      "last_submit_stage": "pushed",
      "last_submit_push_sha": _SUCCESSOR_NEW_HEAD,
      "head_repository": record["head_repository"],
    },
  )
  monkeypatch.setattr(
    github_routes,
    "_assert_pending_equivalence_preflight",
    lambda *_args: pytest.fail(
      "a signed post-mutation successor resumes from authoritative public state"
    ),
  )
  live_targets = [
    {
      "error": None,
      "head_sha": _SUCCESSOR_NEW_HEAD,
      "base_branch": _SUCCESSOR_OLD_BASE_BRANCH,
      "base_sha": _SUCCESSOR_OLD_BASE_SHA,
      "title": record["plan"]["pr_metadata"]["old_title"],
      "body": record["plan"]["pr_metadata"]["old_body"],
    },
    {
      "error": None,
      "head_sha": _SUCCESSOR_NEW_HEAD,
      "base_branch": _SUCCESSOR_TARGET_BASE_BRANCH,
      "base_sha": _SUCCESSOR_TARGET_BASE_SHA,
      "title": record["plan"]["title"],
      "body": record["plan"]["body_draft"],
    },
  ]
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: live_targets.pop(0),
  )
  seen = []
  monkeypatch.setattr(
    github_routes,
    "_advance_merged_parent_successor",
    lambda claimed, _diff, **_kwargs: (
      seen.append((claimed["status"], claimed["submit_started_at"]))
      or (
        "https://github.com/mobius-os/app-demo/pull/58",
        58,
        {
          "last_submit_push_sha": _SUCCESSOR_NEW_HEAD,
          "publication_stage": "ready",
        },
      )
    ),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update-existing",
    headers={"Authorization": f"Bearer {app_token}"},
    json={},
  )

  assert response.status_code == 200, response.text
  assert response.json()["record"]["status"] == "open"
  assert seen == [("submitting", "2026-08-30T22:00:00Z")]
  assert live_targets == []


@pytest.mark.parametrize("error_code", ["update_unconfirmed", "landing_unconfirmed"])
def test_existing_pr_successor_preserves_ambiguous_public_claim(
  client, owner_token, monkeypatch, error_code,
):
  """A lost public response keeps the prior approval resumable, not re-prepared."""
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = f"existing-pr-successor-{error_code}"
  original = _prepared_merged_parent_successor(app_id, record_id)
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: {
      "error": None,
      "head_sha": _SUCCESSOR_NEW_HEAD,
      "base_branch": _SUCCESSOR_OLD_BASE_BRANCH,
      "base_sha": _SUCCESSOR_OLD_BASE_SHA,
      "title": original["plan"]["pr_metadata"]["old_title"],
      "body": original["plan"]["pr_metadata"]["old_body"],
    },
  )

  def unconfirmed(*_args, **_kwargs):
    raise ContributionSubmitError(
      "GitHub did not confirm the base retarget.",
      status_code=503,
      code=error_code,
      detail="The exact PR read was inconclusive.",
      record_patch={"last_submit_push_sha": _SUCCESSOR_NEW_HEAD},
    )

  monkeypatch.setattr(github_routes, "_advance_merged_parent_successor", unconfirmed)
  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update-existing",
    headers={"Authorization": f"Bearer {app_token}"},
    json={},
  )

  assert response.status_code == 503, response.text
  saved = response.json()["detail"]["record"]
  assert saved["status"] == "submitting"
  assert saved["last_submit_push_sha"] == _SUCCESSOR_NEW_HEAD
  assert saved["last_submit_error_code"] == error_code


def test_existing_pr_successor_preserves_claim_after_unexpected_failure(
  client, owner_token, monkeypatch,
):
  """Unknown failures cannot reopen a possibly completed public action."""
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = "existing-pr-successor-unexpected"
  _prepared_merged_parent_successor(app_id, record_id)
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: (_ for _ in ()).throw(RuntimeError("unexpected read failure")),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update-existing",
    headers={"Authorization": f"Bearer {app_token}"},
    json={},
  )

  assert response.status_code == 500, response.text
  assert response.json()["detail"]["message"] == (
    "GitHub did not confirm whether this reviewed successor update completed."
  )
  saved = response.json()["detail"]["record"]
  assert saved["status"] == "submitting"
  assert saved["last_submit_error_code"] == "update_unconfirmed"
  assert "check" in saved["last_submit_error_detail"].lower()


def test_existing_pr_successor_rejects_residual_stack_before_public_work(
  client, owner_token, monkeypatch,
):
  """Stack detachment is an explicit private preparation, never a runtime rewrite."""
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = "existing-pr-successor-stacked"
  record = _prepared_merged_parent_successor(app_id, record_id)
  record["plan"]["stack"] = {"id": "demo", "position": 1, "total": 2}
  _write_contribution(app_id, record_id, record, "reviewed successor diff")
  monkeypatch.setattr(
    github_routes,
    "_advance_merged_parent_successor",
    lambda *_args, **_kwargs: pytest.fail("a stacked successor must not mutate"),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update-existing",
    headers={"Authorization": f"Bearer {app_token}"},
    json={},
  )

  assert response.status_code == 409, response.text
  stored = json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id)
     / "contributions" / f"{record_id}.json").read_text()
  )
  assert stored["status"] == "prepared"


def test_existing_pr_update_stays_successful_if_followup_metadata_fails(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat")
  _allow_synthetic_source_provenance(monkeypatch)
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  record_id = "existing-pr-update-metadata-failure"
  original = _prepared_existing_pr_update(app_id, record_id)
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: _reviewed_live_target(original, head_sha="9" * 40),
  )
  monkeypatch.setattr(
    github_routes,
    "_assert_reviewed_update_contains_live_head",
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
    "_autopilot_live_target",
    lambda *_args: {"error": "The live branch moved.", "head_sha": None},
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
  assert detail["code"] == "review_refresh_needed"
  assert detail["detail"] == "The live branch moved."
  assert detail["record"]["status"] == "prepared"
  assert pushed == []


def test_reviewed_pr_update_must_contain_the_current_public_head(
  tmp_path, monkeypatch,
):
  live = "9" * 40
  reviewed = "a" * 40
  calls = []
  monkeypatch.setattr(
    github_routes,
    "_git",
    lambda repo_path, *args, check=True: calls.append(args) or _cp("", returncode=1),
  )

  with pytest.raises(ContributionSubmitError) as failure:
    github_routes._assert_reviewed_update_contains_live_head(
      tmp_path, live, reviewed,
    )

  assert failure.value.code == "review_refresh_needed"
  assert "changed after the update was reviewed" in failure.value.message
  assert calls == [("merge-base", "--is-ancestor", live, reviewed)]


@pytest.mark.parametrize("live_state", ["old", "maintainer_drift"])
def test_existing_pr_update_never_edits_mismatched_public_text(
  client, owner_token, monkeypatch, live_state,
):
  """Unconditional GitHub metadata edits cannot be made race-safe."""
  _write_token(login="octocat")
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  _allow_synthetic_source_provenance(monkeypatch)
  record_id = f"existing-pr-text-{live_state}"
  record = _prepared_existing_pr_update(app_id, record_id)
  metadata = record["plan"]["pr_metadata"]
  live_title, live_body = (
    (metadata["old_title"], metadata["old_body"])
    if live_state == "old"
    else ("Maintainer title", "Maintainer body.")
  )
  monkeypatch.setattr(
    github_routes,
    "_autopilot_live_target",
    lambda *_args: {
      "error": None,
      "head_sha": "9" * 40,
      "base_branch": "main",
      "base_sha": "8" * 40,
      "title": live_title,
      "body": live_body,
    },
  )
  monkeypatch.setattr(
    github_routes,
    "_submit_prepared_pr",
    lambda *_args, **_kwargs: pytest.fail(
      "mismatched public text must block before any branch mutation"
    ),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/update-existing",
    headers={"Authorization": f"Bearer {app_token}"},
    json={},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "review_refresh_needed"


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
  record["quality_review"]["reviewed_at"] = "2026-08-27T12:34:56Z"
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
  assert projected["coverage_at"] == "2026-08-27T12:34:56Z"


def test_chat_projection_keeps_an_interrupted_successor_resumable(
  client, owner_token, monkeypatch,
):
  """A durable successor claim stays reviewable after a process restart."""
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  record_id = "existing-pr-successor-card"
  record = _prepared_merged_parent_successor(app_id, record_id)
  record.update({
    "chat_id": "chat-successor-resume",
    "status": "submitting",
    "submitter": "contribute-update-button",
    "submit_started_at": "2026-08-30T22:00:00Z",
  })
  _write_contribution(app_id, record_id, record, "reviewed successor diff")
  inspected = []
  monkeypatch.setattr(
    github_routes,
    "_inspect_prepared_review",
    lambda current, _diff_path, _github_state: (
      inspected.append(current["status"])
      or {
        "id": current["id"], "state": "ready", "code": "ready",
        "message": "Still matches the exact source you reviewed.",
      }
    ),
  )

  response = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-successor-resume",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  projected = response.json()["records"][0]
  assert projected["status"] == "submitting"
  assert projected["action"] == "pr_update"
  assert projected["successor"] is True
  assert projected["quality_review_ready"] is True
  assert projected["review"]["state"] == "ready"
  assert inspected == ["submitting"]


def test_chat_projection_uses_private_review_time_after_the_public_submission(
  client, owner_token, monkeypatch,
):
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  record_id = "existing-pr-newer-private-review"
  record = _prepared_existing_pr_update(app_id, record_id)
  record["chat_id"] = "chat-private-update"
  record["submitted_at"] = "2026-08-27T10:00:00Z"
  record["quality_review"]["reviewed_at"] = "2026-08-27T12:00:00Z"
  record["updated_at"] = "2026-08-27T13:00:00Z"
  _write_contribution(app_id, record_id, record, "reviewed diff")
  monkeypatch.setattr(
    github_routes,
    "_inspect_prepared_review",
    lambda record, _diff_path, _github_state: {
      "id": record["id"], "state": "ready", "code": "ready",
      "message": "Still matches the exact source you reviewed.",
    },
  )

  response = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-private-update",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  # A scheduled metadata write at 13:00 cannot hide edits. The exact-head
  # review at 12:00 is the latest moment that actually incorporated source.
  assert response.json()["records"][0]["coverage_at"] == "2026-08-27T12:00:00Z"


def test_chat_projection_does_not_let_push_time_cover_post_review_edits(
  client, owner_token, monkeypatch,
):
  app_id, app_token = _app_token(
    client, owner_token, github_access=True,
  )
  record_id = "existing-pr-reviewed-before-push"
  record = _prepared_existing_pr_update(app_id, record_id)
  record["chat_id"] = "chat-review-before-push"
  record["quality_review"]["reviewed_at"] = "2026-08-27T10:00:00Z"
  record["submitted_at"] = "2026-08-27T12:00:00Z"
  record["updated_at"] = "2026-08-27T13:00:00Z"
  _write_contribution(app_id, record_id, record, "reviewed diff")
  monkeypatch.setattr(
    github_routes,
    "_inspect_prepared_review",
    lambda record, _diff_path, _github_state: {
      "id": record["id"], "state": "ready", "code": "ready",
      "message": "Still matches the exact source you reviewed.",
    },
  )

  response = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-review-before-push",
    headers={"Authorization": f"Bearer {app_token}"},
  )

  assert response.status_code == 200, response.text
  assert response.json()["records"][0]["coverage_at"] == "2026-08-27T10:00:00Z"


def test_chat_projection_uses_publication_time_only_for_legacy_records():
  assert github_routes._chat_record_coverage_at({
    "submitted_at": "2026-08-27T12:00:00Z",
    "updated_at": "2026-08-27T13:00:00Z",
  }) == "2026-08-27T12:00:00Z"
  assert github_routes._chat_record_coverage_at({
    "updated_at": "2026-08-27T13:00:00Z",
  }) == ""


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
  assert record["source_root"] == ""
  assert record["url"] == ""
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


def test_for_chat_keeps_one_review_attached_to_every_chat_that_refined_it(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _, record = _prepared_for_chat(app_id, "shared-review", "chat-original")
  record["chat_ids"] = [
    "chat-original", " chat-refinement ", "chat-refinement", "", 42,
  ]
  _write_contribution(app_id, "shared-review", record, "")
  headers = {"Authorization": f"Bearer {owner_token}"}

  original = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-original",
    headers=headers,
  )
  refinement = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-refinement",
    headers=headers,
  )
  unrelated = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-unrelated",
    headers=headers,
  )

  assert original.status_code == 200, original.text
  assert refinement.status_code == 200, refinement.text
  assert unrelated.status_code == 200, unrelated.text
  assert [item["id"] for item in original.json()["records"]] == ["shared-review"]
  assert [item["id"] for item in refinement.json()["records"]] == ["shared-review"]
  assert unrelated.json()["records"] == []
  # Other chat identities stay private; the projection exposes only the same
  # publication review and its source-file coverage.
  assert "chat_ids" not in refinement.json()["records"][0]


def test_for_chat_returns_the_complete_lifecycle_without_a_hidden_five_card_cap(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  for index in range(7):
    _prepared_for_chat(
      app_id,
      f"complete-{index}",
      "chat-complete",
      status="open",
      number=index + 1,
      url=f"https://github.com/mobius-os/app-demo/pull/{index + 1}",
      updated_at=f"2026-08-27T10:00:0{index}Z",
    )
  headers = {"Authorization": f"Bearer {owner_token}"}

  r = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-complete",
    headers=headers,
  )
  assert r.status_code == 200, r.text
  records = r.json()["records"]
  assert len(records) == 7
  assert [record["number"] for record in records] == [7, 6, 5, 4, 3, 2, 1]
  assert records[0]["url"].endswith("/7")


def test_for_chat_coverage_keeps_display_bounded_without_losing_file_41(
  client, owner_token, db, monkeypatch,
):
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _, record = _prepared_for_chat(
    app_id,
    "wide-review",
    "chat-wide",
    status="open",
    number=41,
    url="https://github.com/mobius-os/mobius/pull/41",
  )
  record["plan"]["source_repo_path"] = "/data/platform"
  record["quality_review"] = {"reviewed_at": "2026-08-27T12:00:00Z"}
  diff_text = "".join(
    "\n".join([
      f"diff --git a/file-{index:02}.js b/file-{index:02}.js",
      f"--- a/file-{index:02}.js",
      f"+++ b/file-{index:02}.js",
      "@@ -1 +1 @@",
      "-old",
      "+new",
      "",
    ])
    for index in range(1, 42)
  )
  _write_contribution(app_id, "wide-review", record, diff_text)
  owner_headers = {"Authorization": f"Bearer {owner_token}"}

  lifecycle = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-wide",
    headers=owner_headers,
  )
  assert lifecycle.status_code == 200, lifecycle.text
  projected = lifecycle.json()["records"][0]
  assert len(projected["files"]) == 40
  assert "file-41.js" not in projected["files"]

  coverage = client.post(
    f"/api/github/contributions/{app_id}/for-chat/chat-wide/coverage",
    headers=owner_headers,
    json={"paths": ["/data/platform/file-41.js", "/private/not-requested.js"]},
  )
  assert coverage.status_code == 200, coverage.text
  assert coverage.json() == {"coverage": [{
    "path": "/data/platform/file-41.js",
    "coverage_at": "2026-08-27T12:00:00Z",
  }]}
  assert "wide-review" not in coverage.text
  assert "file-01.js" not in coverage.text

  app_request = client.post(
    f"/api/github/contributions/{app_id}/for-chat/chat-wide/coverage",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"paths": ["/data/platform/file-41.js"]},
  )
  assert app_request.status_code == 403

  async def recorded_edit(_db, requested_chat_id):
    assert requested_chat_id == "chat-wide"
    return [{
      "id": "edit-after-file-40",
      "ts": "2026-08-27T11:00:00Z",
      "paths": ["/data/platform/file-41.js"],
    }]

  monkeypatch.setattr(github_routes, "_recorded_chat_edits", recorded_edit)
  snapshot = asyncio.run(
    github_routes._contribution_work_snapshot(db, app_id, "chat-wide")
  )
  # The helper freshness check must use complete contribution coverage too;
  # otherwise this covered edit appears unsorted forever and every click 409s.
  assert len(snapshot["record_views"][0]["files"]) == 40
  assert snapshot["unsorted_entries"] == []
  assert snapshot["unsorted_revision"] == ""


def test_for_chat_coverage_rejects_an_unbounded_path_request(client, owner_token):
  app_id, _ = _app_token(client, owner_token, github_access=True)
  response = client.post(
    f"/api/github/contributions/{app_id}/for-chat/chat-wide/coverage",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"paths": [f"/data/platform/file-{index}.js" for index in range(101)]},
  )
  assert response.status_code == 400
  assert response.json()["detail"] == "At most 100 paths are allowed."


def test_for_chat_coverage_preserves_repo_relative_a_and_b_directories(
  client, owner_token,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _, record = _prepared_for_chat(
    app_id,
    "side-prefix-directories",
    "chat-side-prefix-directories",
    status="open",
  )
  record["quality_review"] = {"reviewed_at": "2026-08-27T12:00:00Z"}
  record["plan"]["source_repo_path"] = "/data/platform"
  diff_text = "\n".join([
    "diff --git a/a/foo.js b/a/foo.js",
    "--- a/a/foo.js",
    "+++ b/a/foo.js",
    "@@ -1 +1 @@",
    "-old a",
    "+new a",
    "diff --git a/b/foo.js b/b/foo.js",
    "--- a/b/foo.js",
    "+++ b/b/foo.js",
    "@@ -1 +1 @@",
    "-old b",
    "+new b",
    "",
  ])
  _write_contribution(app_id, "side-prefix-directories", record, diff_text)

  response = client.post(
    f"/api/github/contributions/{app_id}/for-chat/"
    "chat-side-prefix-directories/coverage",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"paths": [
      "/data/platform/a/foo.js",
      "/data/platform/foo.js",
      "/data/platform/b/foo.js",
    ]},
  )

  assert response.status_code == 200, response.text
  assert response.json() == {"coverage": [
    {
      "path": "/data/platform/a/foo.js",
      "coverage_at": "2026-08-27T12:00:00Z",
    },
    {
      "path": "/data/platform/b/foo.js",
      "coverage_at": "2026-08-27T12:00:00Z",
    },
  ]}


def test_for_chat_coverage_never_trusts_a_hostile_record_source_root(
  client, owner_token,
):
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _, record = _prepared_for_chat(
    app_id,
    "hostile-coverage",
    "chat-hostile",
    status="open",
    number=42,
    url="https://github.com/mobius-os/mobius/pull/42",
  )
  record["plan"]["source_repo_path"] = "/data/shared/memory"
  record["quality_review"] = {"reviewed_at": "2026-08-27T12:00:00Z"}
  _write_contribution(
    app_id,
    "hostile-coverage",
    record,
    "\n".join([
      "diff --git a/private.md b/private.md",
      "--- a/private.md",
      "+++ b/private.md",
      "@@ -1 +1 @@",
      "-old",
      "+new",
      "",
    ]),
  )

  response = client.post(
    f"/api/github/contributions/{app_id}/for-chat/chat-hostile/coverage",
    headers={"Authorization": f"Bearer {owner_token}"},
    json={"paths": ["/data/shared/memory/private.md"]},
  )
  assert response.status_code == 200, response.text
  assert response.json() == {"coverage": []}
  assert "hostile-coverage" not in response.text


def test_for_chat_releases_storage_lock_before_parsing_contribution_history(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _prepared_for_chat(
    app_id,
    "lock-friendly",
    "chat-a",
    status="open",
    number=58,
    url="https://github.com/mobius-os/app-demo/pull/58",
  )
  held = False
  real_lock = github_routes.fs_locks.app_storage_lock
  real_read = github_routes._read_record_tolerant

  @asynccontextmanager
  async def observed_lock(requested_app_id):
    nonlocal held
    async with real_lock(requested_app_id):
      held = True
      try:
        yield
      finally:
        held = False

  def assert_unlocked_history_read(path):
    if path.parent.name == "contributions":
      assert held is False, "ledger JSON parsing must not hold the writer lock"
    return real_read(path)

  monkeypatch.setattr(
    github_routes.fs_locks, "app_storage_lock", observed_lock,
  )
  monkeypatch.setattr(
    github_routes, "_read_record_tolerant", assert_unlocked_history_read,
  )

  response = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 200, response.text
  assert [record["id"] for record in response.json()["records"]] == [
    "lock-friendly",
  ]


def test_chat_settlements_are_temporal_idempotent_and_owner_written(
  client, owner_token,
):
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/github/contributions/{app_id}/for-chat/chat-a/settle"
  path = "/data/platform/frontend/src/example.js"

  first = client.post(url, headers=headers, json={
    "coverage_at": 1_787_800_000_000,
    "items": [{
      "path": path,
      "disposition": "experimental",
      "summary": "Kept as a local experiment.",
    }],
  })
  assert first.status_code == 200, first.text

  # A delayed retry from an older source snapshot cannot roll the decision
  # backwards or replace its newer explanation.
  older = client.post(url, headers=headers, json={
    "coverage_at": 1_787_700_000_000,
    "items": [{
      "path": path,
      "disposition": "duplicate",
      "summary": "Stale retry.",
    }],
  })
  assert older.status_code == 200, older.text
  settlement = older.json()["settlements"][0]
  assert settlement["coverage_at"] == 1_787_800_000_000
  assert settlement["disposition"] == "experimental"
  assert settlement["summary"] == "Kept as a local experiment."

  projected = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  )
  assert projected.status_code == 200, projected.text
  assert projected.json()["settlements"] == older.json()["settlements"]

  denied = client.post(
    url,
    headers={"Authorization": f"Bearer {app_token}"},
    json={"coverage_at": 1_787_800_000_000, "items": [{"path": path}]},
  )
  assert denied.status_code == 403, denied.text


def test_chat_settlements_accept_only_review_worktrees_under_contrib(
  client, owner_token,
):
  app_id, _ = _app_token(client, owner_token, github_access=True)
  headers = {"Authorization": f"Bearer {owner_token}"}
  url = f"/api/github/contributions/{app_id}/for-chat/chat-a/settle"

  accepted = client.post(url, headers=headers, json={
    "coverage_at": 1_787_800_000_000,
    "items": [{
      "path": "/data/contrib/review-one/worktree/backend/app.py",
      "disposition": "duplicate",
      "summary": "Captured by the final review.",
    }],
  })
  assert accepted.status_code == 200, accepted.text

  rejected = client.post(url, headers=headers, json={
    "coverage_at": 1_787_800_000_000,
    "items": [{
      "path": "/data/contrib/review-one/git/config",
      "disposition": "experimental",
    }],
  })
  assert rejected.status_code == 422, rejected.text


def test_chat_action_key_ignores_poll_timestamps_but_changes_with_attention(
  client, owner_token,
):
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _, record = _prepared_for_chat(
    app_id, "attention-key", "chat-a", status="open", needs_attention=True,
    attention={"key": "checks_failed:one", "type": "checks_failed"},
  )
  headers = {"Authorization": f"Bearer {owner_token}"}

  first = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  ).json()["records"][0]["action_key"]
  record["updated_at"] = "2026-08-27T15:00:00Z"
  _write_contribution(app_id, "attention-key", record, "")
  second = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  ).json()["records"][0]["action_key"]
  assert second == first

  record["attention"] = {"key": "checks_failed:two", "type": "checks_failed"}
  _write_contribution(app_id, "attention-key", record, "")
  third = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  ).json()["records"][0]["action_key"]
  assert third != first


def test_chat_action_key_changes_with_successor_authorization(
  client, owner_token,
):
  """Any changed successor mutation invalidates an older confirmation."""
  app_id, _ = _app_token(client, owner_token, github_access=True)
  record_id = "successor-action-key"
  record = _prepared_merged_parent_successor(app_id, record_id)
  record["chat_id"] = "chat-a"
  _write_contribution(app_id, record_id, record, "reviewed successor diff")
  headers = {"Authorization": f"Bearer {owner_token}"}

  first = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  ).json()["records"][0]["action_key"]
  record["plan"]["successor"]["base_branch"] = "release"
  _write_contribution(app_id, record_id, record, "reviewed successor diff")
  second = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  ).json()["records"][0]["action_key"]

  assert second != first


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
  # A lone stack layer is never preflighted: the complete chain is exposed as a
  # separate approval unit only when every linked record is available.
  assert item["review"] is None


def test_for_chat_exposes_the_complete_reviewed_stack_as_one_approval_unit(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  parent_id = "direct-approval-layer-1"
  child_id = "direct-approval-layer-2"
  _, parent = _prepared_for_chat(app_id, parent_id, "older-chat")
  _, child = _prepared_for_chat(app_id, child_id, "chat-a")
  stack_id = "direct-approval"
  parent["plan"]["stack"] = {
    "id": stack_id, "name": "Direct approval", "position": 1, "total": 2,
    "parent_record_id": "", "base_branch": "main",
  }
  child["plan"]["stack"] = {
    "id": stack_id, "name": "Direct approval", "position": 2, "total": 2,
    "parent_record_id": parent_id, "base_branch": parent["plan"]["branch"],
  }
  _write_contribution(app_id, parent_id, parent, "")
  _write_contribution(app_id, child_id, child, "")
  monkeypatch.setattr(
    github_routes,
    "_validate_stack_records",
    lambda records, **_kwargs: [
      {"record": record}
      for record in sorted(records, key=github_routes._chat_stack_position)
    ],
  )
  monkeypatch.setattr(
    github_routes,
    "_inspect_prepared_review",
    lambda record, _diff, _state: {
      "id": record["id"], "state": "ready", "code": "ready",
      "message": "Still matches the exact source you reviewed.",
    },
  )
  headers = {"Authorization": f"Bearer {owner_token}"}

  response = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  )

  assert response.status_code == 200, response.text
  body = response.json()
  assert [record["id"] for record in body["records"]] == [child_id]
  assert len(body["stack_units"]) == 1
  unit = body["stack_units"][0]
  assert unit["id"] == stack_id
  assert unit["name"] == "Direct approval"
  assert [record["id"] for record in unit["records"]] == [parent_id, child_id]
  assert [record["review"]["state"] for record in unit["records"]] == [
    "ready", "ready",
  ]

  # Partial publication must not strand the remaining private child in the
  # other source chat: the public parent still keeps the complete unit here.
  parent.update({
    "chat_id": "chat-a", "status": "draft", "number": 7,
    "url": "https://github.com/mobius-os/mobius/pull/7",
  })
  child["chat_id"] = "older-chat"
  _write_contribution(app_id, parent_id, parent, "")
  _write_contribution(app_id, child_id, child, "")
  partial = client.get(
    f"/api/github/contributions/{app_id}/for-chat/chat-a", headers=headers,
  )
  assert partial.status_code == 200, partial.text
  assert [record["id"] for record in partial.json()["records"]] == [parent_id]
  assert [record["id"] for record in partial.json()["stack_units"][0]["records"]] == [
    parent_id, child_id,
  ]


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

  def fake_submit(record, _diff_path, **_kwargs):
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


def test_submit_persists_precise_upstream_fetch_failure(
  client, owner_token, monkeypatch,
):
  _write_token(login="octocat", user_id=42)
  app_id, _ = _app_token(client, owner_token, github_access=True)
  _prepared_for_chat(app_id, "fetch-diagnosis", "chat-a")

  def fail_submit(*_args, **_kwargs):
    raise ContributionSubmitError(
      "GitHub was temporarily unreachable while Contribute checked upstream "
      "main. Nothing was published. Try Send again; leave feedback only if "
      "it keeps failing.",
      record_patch={"last_submit_upstream_branch": "main"},
      code="upstream_fetch_unavailable",
      detail="fatal: Could not resolve host: github.com",
    )

  monkeypatch.setattr(github_routes, "_submit_prepared_pr", fail_submit)

  response = client.post(
    f"/api/github/contributions/{app_id}/fetch-diagnosis/submit",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 409, response.text
  detail = response.json()["detail"]
  assert detail["code"] == "upstream_fetch_unavailable"
  assert detail["detail"] == "fatal: Could not resolve host: github.com"
  assert detail["record"]["status"] == "prepared"
  assert detail["record"]["last_submit_error_code"] == detail["code"]
  assert detail["record"]["last_submit_error_detail"] == detail["detail"]
  assert detail["record"]["last_submit_upstream_branch"] == "main"


def test_submit_failure_keeps_a_compatible_patch_error_code(tmp_path):
  from app.github_contributions import _mark_submit_failure

  record_path = tmp_path / "compatible-failure.json"
  record_path.write_text(json.dumps({
    "id": "compatible-failure",
    "status": "submitting",
  }))

  failed = _mark_submit_failure(
    app_id=0,
    record_path=record_path,
    message="Rejected payload.",
    record_patch={"last_submit_error_code": "invalid_payload"},
  )

  assert failed["status"] == "prepared"
  assert failed["last_submit_error_code"] == "invalid_payload"
  assert json.loads(record_path.read_text())["last_submit_error_code"] == "invalid_payload"


def test_submit_failure_does_not_reuse_a_previous_attempt_error_code(tmp_path):
  from app.github_contributions import _mark_submit_failure

  record_path = tmp_path / "new-failure.json"
  record_path.write_text(json.dumps({
    "id": "new-failure",
    "status": "submitting",
    "last_submit_error_code": "upstream_fetch_unavailable",
  }))

  failed = _mark_submit_failure(
    app_id=0,
    record_path=record_path,
    message="Rejected payload.",
  )

  assert failed["status"] == "prepared"
  assert "last_submit_error_code" not in failed
  assert "last_submit_error_code" not in json.loads(record_path.read_text())


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
  repo = Path(get_settings().data_dir) / "contrib" / record_id / "repo"
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
  client, owner_token, monkeypatch,
):
  """No recorded upstream means nothing local to compare against."""
  _write_token(login="octocat", user_id=42)
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  _prepared_real_review(app_id, "never-sent")
  monkeypatch.setattr(
    github_contributions,
    "_authoritative_public_reconciliation",
    lambda *_args, **_kwargs: pytest.fail(
      "review status must remain local and perform no GitHub reconciliation"
    ),
  )

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


# --- merged-parent successor transition -------------------------------------
#
# A squash/queue-merged parent (#966) leaves its reviewed child (#967) pointed
# at a base branch that no longer carries that commit. The guarded successor
# action rewrites the child branch to the reviewed successor with an exact
# force-with-lease from the old public head, then retargets the PR base to the
# surviving branch. Both mutations are keyed only on the live PR so every crash
# resumes state-by-state and any drift fails closed with nothing pushed.

_SUCCESSOR_OLD_HEAD = "a" * 40
_SUCCESSOR_NEW_HEAD = "b" * 40
_SUCCESSOR_OLD_BASE_SHA = "c" * 40
_SUCCESSOR_TARGET_BASE_SHA = "d" * 40
_SUCCESSOR_MERGED_BASE_SHA = "e" * 40
_SUCCESSOR_OLD_BASE_BRANCH = "stack/demo/01-parent"
_SUCCESSOR_TARGET_BASE_BRANCH = "main"
_SUCCESSOR_BRANCH = "stack/demo/02-child"


def _successor_submission(tmp_path, monkeypatch, *, stack=None):
  """One reviewed detached ``pr_update`` successor card whose parent already squash-merged."""
  _write_token(login="octocat", user_id=42)
  repo = tmp_path / "successor-pr"
  (repo / ".git").mkdir(parents=True)
  diff_text = "diff --git a/model.py b/model.py\n+reviewed successor\n"
  diff_path = tmp_path / "successor.diff"
  diff_path.write_text(diff_text)
  plan = {
    "action": "pr_update",
    "repo": "mobius-os/app-demo",
    "title": "Rebase the child onto the surviving base",
    "body_draft": "Reviewed merged-parent successor.",
    "pr_metadata": {
      "old_title": "Rebase the child onto the surviving base",
      "old_body": "Reviewed merged-parent successor.",
    },
    "branch": _SUCCESSOR_BRANCH,
    "repo_path": str(repo),
    "base_sha": _SUCCESSOR_TARGET_BASE_SHA,
    "head_sha": _SUCCESSOR_NEW_HEAD,
    "diff_sha256": hashlib.sha256(diff_text.encode()).hexdigest(),
    "successor": {
      "old_head_sha": _SUCCESSOR_OLD_HEAD,
      "old_base_branch": _SUCCESSOR_OLD_BASE_BRANCH,
      "old_base_sha": _SUCCESSOR_OLD_BASE_SHA,
      "base_branch": _SUCCESSOR_TARGET_BASE_BRANCH,
    },
  }
  if stack is not None:
    plan["stack"] = stack
  record = {
    "id": "successor-pr",
    "type": "pr",
    "repo": "mobius-os/app-demo",
    "status": "submitting",
    "title": "Rebase the child onto the surviving base",
    "branch": _SUCCESSOR_BRANCH,
    "url": "https://github.com/mobius-os/app-demo/pull/967",
    "number": 967,
    "head_repository": "mobius-os/app-demo",
    "plan": plan,
  }
  monkeypatch.setattr(
    "app.github_contributions.shutil.which", lambda name: f"/bin/{name}",
  )
  monkeypatch.setattr(
    "app.github_contributions._safe_repo_path", lambda _raw: repo,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._connected_git_identity",
    lambda *_args, **_kwargs: (
      "Octo Cat", "octocat@users.noreply.github.com",
    ),
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_fresh",
    lambda *_args, **_kwargs: (
      _SUCCESSOR_TARGET_BASE_SHA,
      _SUCCESSOR_NEW_HEAD,
      record["plan"]["diff_sha256"],
    ),
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_clean_worktree", lambda *_args: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_coauthor_trailer", lambda *_args: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_head_attribution",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    "app.github_contribution_git._assert_merges_with_upstream",
    lambda *_args, **_kwargs: {
      "last_submit_upstream_branch": _SUCCESSOR_TARGET_BASE_BRANCH,
      "last_submit_upstream_sha": _SUCCESSOR_TARGET_BASE_SHA,
    },
  )
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda _repo, _upstream, branch: (
      _SUCCESSOR_OLD_BASE_SHA
      if branch == _SUCCESSOR_OLD_BASE_BRANCH
      else _SUCCESSOR_TARGET_BASE_SHA
    ),
  )
  monkeypatch.setattr(
    "app.github_contributions._assert_merged_parent_tree_equivalence",
    lambda *_args, **_kwargs: None,
  )

  def fake_git(_repo, *args, check=True):
    if args == ("rev-parse", "--abbrev-ref", "HEAD"):
      return _cp(_SUCCESSOR_BRANCH + "\n")
    if args == ("rev-parse", "HEAD"):
      return _cp(_SUCCESSOR_NEW_HEAD + "\n")
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  return record, diff_path


def _capture_successor_confirmations(
  monkeypatch,
  *,
  url="https://github.com/mobius-os/app-demo/pull/967",
  target_results=None,
):
  confirms = []
  target_results = list(target_results or [None, (url, "ready")])

  def fake_confirm(_repo, _upstream, number, **kwargs):
    confirms.append({"number": number, **kwargs})
    if kwargs.get("base_branch") == _SUCCESSOR_TARGET_BASE_BRANCH:
      if len(target_results) > 1:
        return target_results.pop(0)
      return target_results[0]
    return url, "ready"

  monkeypatch.setattr(
    "app.github_contributions._confirm_existing_pr_update", fake_confirm,
  )
  return confirms


def test_merged_parent_successor_leases_promoted_child_after_squash_merge(
  tmp_path, monkeypatch,
):
  """old head + old base -> force-with-lease rewrite, then base retarget."""
  from app.github_contributions import _advance_merged_parent_successor

  record, diff_path = _successor_submission(tmp_path, monkeypatch)
  lease_calls = []
  monkeypatch.setattr(
    "app.github_contributions._push_stack_tip_with_lease",
    lambda repo, **kwargs: lease_calls.append(kwargs),
  )
  retarget_calls = []

  def fake_retarget(_repo, _upstream, number, *, base_branch):
    retarget_calls.append((number, base_branch))
    return "accepted", ""

  monkeypatch.setattr(
    "app.github_contributions._retarget_pr_base", fake_retarget,
  )
  confirms = _capture_successor_confirmations(monkeypatch)
  events = []

  url, number, patch = _advance_merged_parent_successor(
    record,
    diff_path,
    expected_number=967,
    expected_head_repository="mobius-os/app-demo",
    live_head_sha=_SUCCESSOR_OLD_HEAD,
    live_base_branch=_SUCCESSOR_OLD_BASE_BRANCH,
    attempt_event=lambda phase, request, event_patch: events.append(
      (phase, request, dict(event_patch or {}))
    ),
  )

  assert (url, number) == ("https://github.com/mobius-os/app-demo/pull/967", 967)
  # Public mutation #1: exact force-with-lease from the old public head.
  assert lease_calls == [{
    "upstream_repo": "mobius-os/app-demo",
    "target_branch": _SUCCESSOR_BRANCH,
    "expected_base": _SUCCESSOR_OLD_HEAD,
    "landed_sha": _SUCCESSOR_NEW_HEAD,
  }]
  # Public mutation #2: retarget to the surviving base.
  assert retarget_calls == [(967, _SUCCESSOR_TARGET_BASE_BRANCH)]
  # The old public state is re-read before the lease, the rewrite is confirmed
  # on the old base, and the exact new head+base is confirmed after retarget.
  assert confirms[0]["base_branch"] == _SUCCESSOR_OLD_BASE_BRANCH
  assert confirms[0]["expected_base_sha"] == _SUCCESSOR_OLD_BASE_SHA
  assert confirms[0]["expected_head_sha"] == _SUCCESSOR_OLD_HEAD
  assert confirms[1]["base_branch"] == _SUCCESSOR_OLD_BASE_BRANCH
  assert confirms[1]["expected_head_sha"] == _SUCCESSOR_NEW_HEAD
  assert confirms[-1]["base_branch"] == _SUCCESSOR_TARGET_BASE_BRANCH
  assert confirms[-1]["expected_base_sha"] == _SUCCESSOR_TARGET_BASE_SHA
  assert patch["last_submit_push_sha"] == _SUCCESSOR_NEW_HEAD
  assert patch["last_successor_old_head"] == _SUCCESSOR_OLD_HEAD
  assert patch["last_successor_base_branch"] == _SUCCESSOR_TARGET_BASE_BRANCH
  assert patch["last_successor_base_sha"] == _SUCCESSOR_TARGET_BASE_SHA
  assert patch["publication_stage"] == "ready"
  assert [phase for phase, *_rest in events] == [
    "push_pending", "branch_published", "pr_ambiguous",
  ]
  assert events[0][1]["previous_head_sha"] == _SUCCESSOR_OLD_HEAD
  assert events[0][1]["head_sha"] == _SUCCESSOR_NEW_HEAD
  assert events[-1][1]["base_branch"] == _SUCCESSOR_TARGET_BASE_BRANCH


def test_merged_parent_successor_proves_tree_equivalence_before_mutation(
  tmp_path, monkeypatch,
):
  """A target base that does not carry the merged parent's tree fails closed."""
  from app.github_contributions import _assert_merged_parent_tree_equivalence

  repo = tmp_path / "tree"
  (repo / ".git").mkdir(parents=True)

  def fake_git(_repo, *args, check=True):
    # rev-parse --verify --quiet <sha>^{tree}
    if args[:3] == ("rev-parse", "--verify", "--quiet"):
      spec = args[3]
      if spec.startswith(_SUCCESSOR_OLD_BASE_SHA):
        return _cp("1" * 40 + "\n")
      return _cp("2" * 40 + "\n")
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  with pytest.raises(ContributionSubmitError) as caught:
    _assert_merged_parent_tree_equivalence(
      repo,
      old_base_sha=_SUCCESSOR_OLD_BASE_SHA,
      target_base_sha=_SUCCESSOR_TARGET_BASE_SHA,
    )
  assert caught.value.code == "review_refresh_needed"

  # Matching trees prove the merged parent already reached the target base.
  monkeypatch.setattr(
    "app.github_contribution_git._git",
    lambda _repo, *args, check=True: (
      _cp("e" * 40 + "\n")
      if args[:3] == ("rev-parse", "--verify", "--quiet")
      else _cp("")
    ),
  )
  _assert_merged_parent_tree_equivalence(
    repo,
    old_base_sha=_SUCCESSOR_OLD_BASE_SHA,
    target_base_sha=_SUCCESSOR_TARGET_BASE_SHA,
  )


def test_merged_parent_successor_stops_when_tree_is_unreadable(
  tmp_path, monkeypatch,
):
  """An unresolved merged-parent or target-base tree fails closed, no mutation."""
  from app.github_contributions import _assert_merged_parent_tree_equivalence

  repo = tmp_path / "tree"
  (repo / ".git").mkdir(parents=True)
  monkeypatch.setattr(
    "app.github_contribution_git._git",
    lambda _repo, *args, check=True: _cp("", "bad object", 128),
  )
  with pytest.raises(ContributionSubmitError) as caught:
    _assert_merged_parent_tree_equivalence(
      repo,
      old_base_sha=_SUCCESSOR_OLD_BASE_SHA,
      target_base_sha=_SUCCESSOR_TARGET_BASE_SHA,
    )
  assert caught.value.code == "update_unconfirmed"
  assert caught.value.status_code == 503


def test_merged_parent_successor_accepts_an_advanced_target_with_exact_merge_proof(
  tmp_path, monkeypatch,
):
  """Later target commits are safe when the exact parent merge is in history."""
  from app.github_contributions import _assert_merged_parent_tree_equivalence

  repo = tmp_path / "advanced-tree"
  (repo / ".git").mkdir(parents=True)
  calls = []

  def fake_git(_repo, *args, check=True):
    calls.append(args)
    if args[:3] == ("rev-parse", "--verify", "--quiet"):
      # The old parent and its exact merge commit share one tree. The later
      # target tip deliberately has a different tree and is not compared.
      return _cp("1" * 40 + "\n")
    if args[:2] == ("merge-base", "--is-ancestor"):
      return _cp("")
    return _cp("", "unexpected git call", 128)

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  _assert_merged_parent_tree_equivalence(
    repo,
    old_base_sha=_SUCCESSOR_OLD_BASE_SHA,
    merged_base_sha=_SUCCESSOR_MERGED_BASE_SHA,
    target_base_sha=_SUCCESSOR_TARGET_BASE_SHA,
  )
  assert (
    "merge-base", "--is-ancestor",
    _SUCCESSOR_MERGED_BASE_SHA, _SUCCESSOR_TARGET_BASE_SHA,
  ) in calls
  assert not any(
    args[:3] == ("rev-parse", "--verify", "--quiet")
    and args[3].startswith(_SUCCESSOR_TARGET_BASE_SHA)
    for args in calls
  )


@pytest.mark.parametrize("ancestry_returncode, expected_code", [
  (1, "review_refresh_needed"),
  (128, "update_unconfirmed"),
])
def test_merged_parent_successor_rejects_unproven_advanced_target_ancestry(
  tmp_path, monkeypatch, ancestry_returncode, expected_code,
):
  """A missing or unreadable parent-to-target edge always fails closed."""
  from app.github_contributions import _assert_merged_parent_tree_equivalence

  repo = tmp_path / f"advanced-tree-{ancestry_returncode}"
  (repo / ".git").mkdir(parents=True)

  def fake_git(_repo, *args, check=True):
    if args[:3] == ("rev-parse", "--verify", "--quiet"):
      return _cp("1" * 40 + "\n")
    if args[:2] == ("merge-base", "--is-ancestor"):
      return _cp("", "bad ancestry", ancestry_returncode)
    return _cp("")

  monkeypatch.setattr("app.github_contribution_git._git", fake_git)
  with pytest.raises(ContributionSubmitError) as caught:
    _assert_merged_parent_tree_equivalence(
      repo,
      old_base_sha=_SUCCESSOR_OLD_BASE_SHA,
      merged_base_sha=_SUCCESSOR_MERGED_BASE_SHA,
      target_base_sha=_SUCCESSOR_TARGET_BASE_SHA,
    )
  assert caught.value.code == expected_code


def test_merged_parent_successor_resumes_retarget_without_pushing(
  tmp_path, monkeypatch,
):
  """new head + old base -> skip the push, retarget only."""
  from app.github_contributions import _advance_merged_parent_successor

  record, diff_path = _successor_submission(tmp_path, monkeypatch)
  monkeypatch.setattr(
    "app.github_contributions._push_stack_tip_with_lease",
    lambda *_args, **_kwargs: pytest.fail(
      "a resumed successor at the new head must never push again"
    ),
  )
  retarget_calls = []
  monkeypatch.setattr(
    "app.github_contributions._retarget_pr_base",
    lambda _repo, _upstream, number, *, base_branch: (
      retarget_calls.append((number, base_branch)) or ("accepted", "")
    ),
  )
  confirms = _capture_successor_confirmations(monkeypatch)

  url, number, patch = _advance_merged_parent_successor(
    record,
    diff_path,
    expected_number=967,
    expected_head_repository="mobius-os/app-demo",
    live_head_sha=_SUCCESSOR_NEW_HEAD,
    live_base_branch=_SUCCESSOR_OLD_BASE_BRANCH,
  )

  assert (url, number) == ("https://github.com/mobius-os/app-demo/pull/967", 967)
  assert retarget_calls == [(967, _SUCCESSOR_TARGET_BASE_BRANCH)]
  # A resume first checks whether the retarget already landed, proves the exact
  # old base before one edit, then confirms the target base after it.
  assert [c["base_branch"] for c in confirms] == [
    _SUCCESSOR_TARGET_BASE_BRANCH,
    _SUCCESSOR_OLD_BASE_BRANCH,
    _SUCCESSOR_TARGET_BASE_BRANCH,
  ]
  assert patch["last_submit_push_sha"] == _SUCCESSOR_NEW_HEAD


def test_merged_parent_successor_recovers_a_lost_retarget_response(
  tmp_path, monkeypatch,
):
  """A lost/ambiguous edit response reconciles from the PR read, not a re-edit."""
  from app.github_contributions import _advance_merged_parent_successor

  record, diff_path = _successor_submission(tmp_path, monkeypatch)
  monkeypatch.setattr(
    "app.github_contributions._push_stack_tip_with_lease",
    lambda *_args, **_kwargs: None,
  )
  edit_attempts = []

  def lost_response(_repo, _upstream, number, *, base_branch):
    edit_attempts.append((number, base_branch))
    return "ambiguous", "Timed out waiting for GitHub."

  monkeypatch.setattr(
    "app.github_contributions._retarget_pr_base", lost_response,
  )
  # The PR read is the authority: it shows the retarget already landed.
  confirms = _capture_successor_confirmations(monkeypatch)

  url, number, _patch = _advance_merged_parent_successor(
    record,
    diff_path,
    expected_number=967,
    expected_head_repository="mobius-os/app-demo",
    live_head_sha=_SUCCESSOR_NEW_HEAD,
    live_base_branch=_SUCCESSOR_OLD_BASE_BRANCH,
  )

  assert number == 967
  assert edit_attempts == [(967, _SUCCESSOR_TARGET_BASE_BRANCH)]
  assert confirms[-1]["base_branch"] == _SUCCESSOR_TARGET_BASE_BRANCH


def test_merged_parent_successor_fails_when_retarget_is_unconfirmed(
  tmp_path, monkeypatch,
):
  """An accepted edit that GitHub never exposes fails closed with the witness."""
  from app.github_contributions import _advance_merged_parent_successor

  record, diff_path = _successor_submission(tmp_path, monkeypatch)
  monkeypatch.setattr(
    "app.github_contributions._push_stack_tip_with_lease",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    "app.github_contributions._retarget_pr_base",
    lambda *_args, **_kwargs: ("accepted", ""),
  )
  monkeypatch.setattr(
    "app.github_contributions._confirm_existing_pr_update",
    lambda *_args, **_kwargs: None,
  )

  with pytest.raises(ContributionSubmitError) as caught:
    _advance_merged_parent_successor(
      record,
      diff_path,
      expected_number=967,
      expected_head_repository="mobius-os/app-demo",
      live_head_sha=_SUCCESSOR_NEW_HEAD,
      live_base_branch=_SUCCESSOR_OLD_BASE_BRANCH,
    )
  assert caught.value.code == "update_unconfirmed"
  assert caught.value.status_code == 503
  assert caught.value.record_patch["last_submit_push_sha"] == _SUCCESSOR_NEW_HEAD


def test_merged_parent_successor_settles_when_already_live(
  tmp_path, monkeypatch,
):
  """new head + new base -> settle the ledger only; never push or edit."""
  from app.github_contributions import _advance_merged_parent_successor

  record, diff_path = _successor_submission(tmp_path, monkeypatch)
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda _repo, _upstream, branch: (
      "f" * 40
      if branch == _SUCCESSOR_OLD_BASE_BRANCH
      else _SUCCESSOR_TARGET_BASE_SHA
    ),
  )
  monkeypatch.setattr(
    "app.github_contributions._push_stack_tip_with_lease",
    lambda *_args, **_kwargs: pytest.fail("settle must never push"),
  )
  monkeypatch.setattr(
    "app.github_contributions._retarget_pr_base",
    lambda *_args, **_kwargs: pytest.fail("settle must never retarget"),
  )
  confirms = _capture_successor_confirmations(
    monkeypatch,
    target_results=[("https://github.com/mobius-os/app-demo/pull/967", "ready")],
  )

  url, number, patch = _advance_merged_parent_successor(
    record,
    diff_path,
    expected_number=967,
    expected_head_repository="mobius-os/app-demo",
    live_head_sha=_SUCCESSOR_NEW_HEAD,
    live_base_branch=_SUCCESSOR_TARGET_BASE_BRANCH,
  )

  assert (url, number) == ("https://github.com/mobius-os/app-demo/pull/967", 967)
  assert [c["base_branch"] for c in confirms] == [_SUCCESSOR_TARGET_BASE_BRANCH]
  assert patch["last_successor_base_sha"] == _SUCCESSOR_TARGET_BASE_SHA


def test_merged_parent_successor_settle_rejects_moved_target_base(
  tmp_path, monkeypatch,
):
  """Settle-only recovery remains pinned to the reviewed target-base SHA."""
  from app.github_contributions import _advance_merged_parent_successor

  record, diff_path = _successor_submission(tmp_path, monkeypatch)
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda *_args, **_kwargs: "f" * 40,
  )
  monkeypatch.setattr(
    "app.github_contributions._confirm_existing_pr_update",
    lambda *_args, **_kwargs: pytest.fail("a moved base must not settle"),
  )
  with pytest.raises(ContributionSubmitError) as caught:
    _advance_merged_parent_successor(
      record,
      diff_path,
      expected_number=967,
      expected_head_repository="mobius-os/app-demo",
      live_head_sha=_SUCCESSOR_NEW_HEAD,
      live_base_branch=_SUCCESSOR_TARGET_BASE_BRANCH,
      )
  assert caught.value.code == "review_refresh_needed"


@pytest.mark.parametrize(
  ("live_base_branch", "target_results"),
  [
    (
      _SUCCESSOR_OLD_BASE_BRANCH,
      [None, ("https://github.com/mobius-os/app-demo/pull/967", "ready")],
    ),
    (
      _SUCCESSOR_TARGET_BASE_BRANCH,
      [("https://github.com/mobius-os/app-demo/pull/967", "ready")],
    ),
  ],
)
def test_merged_parent_successor_recovery_revalidates_reviewed_head(
  tmp_path, monkeypatch, live_base_branch, target_results,
):
  """Retarget and settle recovery never substitute public state for review."""
  from app.github_contributions import _advance_merged_parent_successor

  record, diff_path = _successor_submission(tmp_path, monkeypatch)
  validations = []

  def assert_fresh(*_args, **_kwargs):
    validations.append("fresh")
    return (
      _SUCCESSOR_TARGET_BASE_SHA,
      _SUCCESSOR_NEW_HEAD,
      record["plan"]["diff_sha256"],
    )

  monkeypatch.setattr("app.github_contribution_git._assert_fresh", assert_fresh)
  monkeypatch.setattr(
    "app.github_contributions._retarget_pr_base",
    lambda *_args, **_kwargs: ("accepted", ""),
  )
  _capture_successor_confirmations(
    monkeypatch, target_results=target_results,
  )

  _advance_merged_parent_successor(
    record,
    diff_path,
    expected_number=967,
    expected_head_repository="mobius-os/app-demo",
    live_head_sha=_SUCCESSOR_NEW_HEAD,
    live_base_branch=live_base_branch,
  )

  assert validations == ["fresh"]


def test_merged_parent_successor_preserves_witness_when_target_read_raises(
  tmp_path, monkeypatch,
):
  """A recovery-time target lookup exception cannot reopen the public action."""
  from app.github_contributions import _advance_merged_parent_successor

  record, diff_path = _successor_submission(tmp_path, monkeypatch)
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda *_args, **_kwargs: (_ for _ in ()).throw(
      subprocess.TimeoutExpired("gh api", 1),
    ),
  )
  monkeypatch.setattr(
    "app.github_contributions._push_stack_tip_with_lease",
    lambda *_args, **_kwargs: pytest.fail("recovery must never push again"),
  )

  with pytest.raises(ContributionSubmitError) as caught:
    _advance_merged_parent_successor(
      record,
      diff_path,
      expected_number=967,
      expected_head_repository="mobius-os/app-demo",
      live_head_sha=_SUCCESSOR_NEW_HEAD,
      live_base_branch=_SUCCESSOR_OLD_BASE_BRANCH,
    )

  assert caught.value.code == "update_unconfirmed"
  assert caught.value.status_code == 503
  assert caught.value.record_patch["last_submit_push_sha"] == _SUCCESSOR_NEW_HEAD


def test_merged_parent_successor_rejects_moved_old_base_before_mutation(
  tmp_path, monkeypatch,
):
  """The authoritative PR base SHA must still match the reviewed parent."""
  from app.github_contributions import _advance_merged_parent_successor

  record, diff_path = _successor_submission(tmp_path, monkeypatch)
  monkeypatch.setattr(
    "app.github_contribution_git._upstream_branch_sha",
    lambda _repo, _upstream, branch: (
      "f" * 40
      if branch == _SUCCESSOR_OLD_BASE_BRANCH
      else _SUCCESSOR_TARGET_BASE_SHA
    ),
  )
  monkeypatch.setattr(
    "app.github_contributions._push_stack_tip_with_lease",
    lambda *_args, **_kwargs: pytest.fail("a moved old base must not push"),
  )
  monkeypatch.setattr(
    "app.github_contributions._retarget_pr_base",
    lambda *_args, **_kwargs: pytest.fail("a moved old base must not retarget"),
  )
  with pytest.raises(ContributionSubmitError) as caught:
    _advance_merged_parent_successor(
      record,
      diff_path,
      expected_number=967,
      expected_head_repository="mobius-os/app-demo",
      live_head_sha=_SUCCESSOR_OLD_HEAD,
      live_base_branch=_SUCCESSOR_OLD_BASE_BRANCH,
    )
  assert caught.value.code == "review_refresh_needed"


def test_merged_parent_successor_fails_closed_on_unexpected_state(
  tmp_path, monkeypatch,
):
  """Any head/base that is not one of the three known states mutates nothing."""
  from app.github_contributions import _advance_merged_parent_successor

  record, diff_path = _successor_submission(tmp_path, monkeypatch)
  monkeypatch.setattr(
    "app.github_contributions._push_stack_tip_with_lease",
    lambda *_args, **_kwargs: pytest.fail("a drifted PR must never push"),
  )
  monkeypatch.setattr(
    "app.github_contributions._retarget_pr_base",
    lambda *_args, **_kwargs: pytest.fail("a drifted PR must never retarget"),
  )
  monkeypatch.setattr(
    "app.github_contributions._confirm_existing_pr_update",
    lambda *_args, **_kwargs: pytest.fail("a drifted PR must never be confirmed"),
  )

  with pytest.raises(ContributionSubmitError) as caught:
    _advance_merged_parent_successor(
      record,
      diff_path,
      expected_number=967,
      expected_head_repository="mobius-os/app-demo",
      live_head_sha="9" * 40,
      live_base_branch=_SUCCESSOR_OLD_BASE_BRANCH,
      )
  assert caught.value.code == "review_refresh_needed"


def test_merged_parent_successor_never_authorizes_from_ledger_alone(monkeypatch):
  """The classifier keys only on live facts, so a stale ledger cannot promote."""
  from app.github_contributions import (
    _classify_merged_parent_successor,
    _merged_parent_successor_plan,
  )

  journal = _merged_parent_successor_plan({
    "branch": _SUCCESSOR_BRANCH,
    "plan": {
      "action": "pr_update",
      "branch": _SUCCESSOR_BRANCH,
      "head_sha": _SUCCESSOR_NEW_HEAD,
      "base_sha": _SUCCESSOR_TARGET_BASE_SHA,
      "successor": {
        "old_head_sha": _SUCCESSOR_OLD_HEAD,
        "old_base_branch": _SUCCESSOR_OLD_BASE_BRANCH,
        "old_base_sha": _SUCCESSOR_OLD_BASE_SHA,
        "base_branch": _SUCCESSOR_TARGET_BASE_BRANCH,
      },
    },
  })
  assert _classify_merged_parent_successor(
    journal,
    live_head_sha=_SUCCESSOR_OLD_HEAD,
    live_base_branch=_SUCCESSOR_OLD_BASE_BRANCH,
  ) == "push"
  assert _classify_merged_parent_successor(
    journal,
    live_head_sha=_SUCCESSOR_NEW_HEAD,
    live_base_branch=_SUCCESSOR_OLD_BASE_BRANCH,
  ) == "retarget"
  assert _classify_merged_parent_successor(
    journal,
    live_head_sha=_SUCCESSOR_NEW_HEAD,
    live_base_branch=_SUCCESSOR_TARGET_BASE_BRANCH,
  ) == "settle"
  # Base retargeted but head never rewritten is not a reachable resume point.
  with pytest.raises(ContributionSubmitError) as caught:
    _classify_merged_parent_successor(
      journal,
      live_head_sha=_SUCCESSOR_OLD_HEAD,
      live_base_branch=_SUCCESSOR_TARGET_BASE_BRANCH,
    )
  assert caught.value.code == "review_refresh_needed"


def test_merged_parent_successor_plan_requires_distinct_branch_and_head():
  """The durable claim must record a real rewrite AND a real retarget."""
  from app.github_contributions import _merged_parent_successor_plan

  def plan(**overrides):
    base = {
      "action": "pr_update",
      "branch": _SUCCESSOR_BRANCH,
      "head_sha": _SUCCESSOR_NEW_HEAD,
      "base_sha": _SUCCESSOR_TARGET_BASE_SHA,
      "successor": {
        "old_head_sha": _SUCCESSOR_OLD_HEAD,
        "old_base_branch": _SUCCESSOR_OLD_BASE_BRANCH,
        "old_base_sha": _SUCCESSOR_OLD_BASE_SHA,
        "base_branch": _SUCCESSOR_TARGET_BASE_BRANCH,
      },
    }
    base.update(overrides)
    return {"plan": base}

  # Same old/new head is not a rewrite.
  with pytest.raises(ContributionSubmitError):
    _merged_parent_successor_plan(plan(head_sha=_SUCCESSOR_OLD_HEAD))
  # A non-successor action never reaches this owning claim.
  with pytest.raises(ContributionSubmitError):
    _merged_parent_successor_plan({"plan": {"action": "pr_update"}})
  # Same old/target base is not a retarget.
  same_base = {
    "action": "pr_update",
    "branch": _SUCCESSOR_BRANCH,
    "head_sha": _SUCCESSOR_NEW_HEAD,
    "base_sha": _SUCCESSOR_TARGET_BASE_SHA,
    "successor": {
      "old_head_sha": _SUCCESSOR_OLD_HEAD,
      "old_base_branch": _SUCCESSOR_TARGET_BASE_BRANCH,
      "old_base_sha": _SUCCESSOR_OLD_BASE_SHA,
      "base_branch": _SUCCESSOR_TARGET_BASE_BRANCH,
    },
  }
  with pytest.raises(ContributionSubmitError):
    _merged_parent_successor_plan({"plan": same_base})
  # Runtime publication never silently detaches or rewrites stack metadata.
  stacked = plan()["plan"]
  stacked["stack"] = {"id": "demo", "position": 1, "total": 2}
  with pytest.raises(ContributionSubmitError):
    _merged_parent_successor_plan({"plan": stacked})

  advanced = plan()["plan"]
  advanced["successor"]["merged_base_sha"] = _SUCCESSOR_MERGED_BASE_SHA
  assert _merged_parent_successor_plan({"plan": advanced})[
    "merged_base_sha"
  ] == _SUCCESSOR_MERGED_BASE_SHA
  advanced["successor"]["merged_base_sha"] = "not-a-commit"
  with pytest.raises(ContributionSubmitError):
    _merged_parent_successor_plan({"plan": advanced})


def test_retarget_pr_base_attempts_one_mutation_only(tmp_path, monkeypatch):
  """An ambiguous base edit response is never followed by a second mutation."""
  from app.github_contributions import _retarget_pr_base

  attempts = []

  def ambiguous_gh(_repo, *args, check=True):
    attempts.append(args)
    return _cp("", "fatal: unable to access: Connection timed out", 1)

  monkeypatch.setattr("app.github_contribution_git._gh", ambiguous_gh)
  result, error = _retarget_pr_base(
    tmp_path, "mobius-os/app-demo", 967, base_branch="main",
  )
  assert result == "ambiguous"
  assert "timed out" in error.lower()
  assert len(attempts) == 1
  assert attempts[0][:5] == ("pr", "edit", "967", "-R", "mobius-os/app-demo")


def test_retarget_pr_base_distinguishes_deterministic_rejection(
  tmp_path, monkeypatch,
):
  """A validation rejection may re-prepare; a transport failure may not."""
  from app.github_contributions import _retarget_pr_base

  monkeypatch.setattr(
    "app.github_contribution_git._gh",
    lambda *_args, **_kwargs: _cp("", "GraphQL: Base branch is invalid", 1),
  )
  result, error = _retarget_pr_base(
    tmp_path, "mobius-os/app-demo", 967, base_branch="main",
  )
  assert result == "rejected"
  assert "invalid" in error.lower()


def test_merged_parent_successor_plan_leaves_ordinary_update_path_untouched():
  """An ordinary reviewed ``pr_update`` restack never enters the successor claim."""
  from app.github_contributions import _merged_parent_successor_plan

  with pytest.raises(ContributionSubmitError):
    _merged_parent_successor_plan({
      "branch": "feat/existing-review",
      "plan": {"action": "pr_update", "branch": "feat/existing-review"},
    })


def test_assignment_choices_keep_app_scope_and_github_access(client, owner_token, monkeypatch):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  monkeypatch.setattr("app.routes.github._gh", lambda cwd, *args, **kw: _cp(
    '{"permissions":{"admin":true}}' if args[-1] == 'repos/org/repo' else '[{"login":"target"}]'
  ))
  response = client.get(
    f"/api/github/contributions/{app_id}/assignees?repo=org/repo",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 200, response.text
  assert response.json()["can_manage_access"] is True
  assert response.json()["assignees"][0]["login"] == "target"
  other_id, _ = _app_token(client, owner_token, github_access=True)
  response = client.get(
    f"/api/github/contributions/{other_id}/assignees?repo=org/repo",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 403


def test_assign_review_passes_selected_person_and_head_to_owner(client, owner_token, monkeypatch):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=True)
  observed = []
  def assign(gh, cwd, repo, number, login, head, before_write):
    before_write()
    observed.append((repo, number, login, head))
    return {"assigned": True, "login": login, "repo": repo, "number": number}
  monkeypatch.setattr("app.routes.github.contribution_assignments.assign_pull_request", assign)
  response = client.post(
    f"/api/github/contributions/{app_id}/assign-review",
    headers={"Authorization": f"Bearer {app_token}"},
    json={"repo": "org/repo", "number": 7, "assignee": "teammate", "expected_head_sha": "current"},
  )
  assert response.status_code == 200, response.text
  assert observed == [("org/repo", 7, "teammate", "current")]
  assert response.json()["login"] == "teammate"


def test_assignment_choices_reject_missing_github_capability(client, owner_token):
  _write_token(login="octocat")
  app_id, app_token = _app_token(client, owner_token, github_access=False)
  response = client.get(
    f"/api/github/contributions/{app_id}/assignees?repo=org/repo",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 403
