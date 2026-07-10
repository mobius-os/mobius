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

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import httpx
import pytest

from app import github_auth
from app.config import get_settings
from app.storage_io import atomic_write

# The github router's Limiter is a separate instance from app.state.limiter,
# so conftest's disable doesn't reach it (see module docstring).
from app.routes.github import _limiter as _github_limiter

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
  assert "fine-grained" in r.json()["detail"]


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


def test_submit_contribution_creates_draft_pr_from_prepared_record(
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

  def fake_git(repo_path, *args, check=True):
    git_calls.append(args)
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
    if args == ("remote", "get-url", "fork"):
      return _cp(returncode=1)
    return _cp("")

  gh_calls = []

  def fake_gh(repo_path, *args, check=True):
    gh_calls.append(args)
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
  assert body["record"]["status"] == "draft"
  assert body["record"]["url"] == body["url"]
  assert (
    "repo", "fork", "mobius-os/app-demo",
    "--remote", "--remote-name", "fork",
  ) in gh_calls
  assert any(call[:2] == ("pr", "create") for call in gh_calls)
  assert ("push", "fork", "HEAD:refs/heads/fix/demo-polish") in git_calls
  assert ("checkout", "-q", "develop") in git_calls

  stored = json.loads(
    (Path(get_settings().data_dir) / "apps" / str(app_id) /
     "contributions" / f"{record_id}.json").read_text()
  )
  assert stored["status"] == "draft"
  assert stored["number"] == 42


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
    if args == ("remote", "get-url", "fork"):
      return _cp("")
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
