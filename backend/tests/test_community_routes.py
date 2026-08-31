from __future__ import annotations

from contextlib import asynccontextmanager
import json
import shutil
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import community_publish, models
from app.routes import community


@pytest.fixture(autouse=True)
def _clean_publication_journal():
  shutil.rmtree(community_publish._journal_root(), ignore_errors=True)
  yield
  shutil.rmtree(community_publish._journal_root(), ignore_errors=True)


def _local_app(db):
  app = models.App(
    id=42,
    slug="pocket-list",
    name="Pocket List",
    description="A small shared list.",
    jsx_source="export default function App() { return null }",
    compiled_path="/tmp/pocket-list.js",
    source_dir="/data/apps/pocket-list",
  )
  db.add(app)
  db.commit()
  return app


@pytest.mark.asyncio
async def test_store_inherits_only_sanitized_local_github_identity(monkeypatch):
  monkeypatch.setattr(community.github_auth, "read_state", lambda: {
    "token": "must-never-cross-the-route",
    "login": "octo-owner",
    "scopes": ["repo", "workflow"],
    "device_code": "also-private",
  })

  assert await community.community_github_status(None) == {
    "connected": True,
    "login": "octo-owner",
  }


@pytest.mark.asyncio
async def test_store_github_identity_is_empty_when_contribute_is_disconnected(monkeypatch):
  monkeypatch.setattr(community.github_auth, "read_state", lambda: None)

  assert await community.community_github_status(None) == {
    "connected": False,
    "login": "",
  }


def test_store_github_surface_requires_both_app_grants(
  client, db, owner_token, monkeypatch,
):
  from app.auth import create_access_token

  app = models.App(
    name="Store-shaped caller",
    description="",
    jsx_source="export default function App() { return null }",
    source_dir="/tmp/store-shaped-caller",
    slug="store-shaped-caller",
    manifest_url="https://example.test/store-shaped-caller/mobius.json",
    cross_app_access="none",
    share_with_apps="none",
    offline_capable=False,
    manage_apps=True,
    github_access=False,
  )
  db.add(app)
  db.commit()
  token = create_access_token({
    "sub": "test", "scope": "app", "app_id": app.id,
  })
  headers = {"Authorization": f"Bearer {token}"}

  missing_github = client.get("/api/community/github-status", headers=headers)
  assert missing_github.status_code == 403
  assert "github_access" in missing_github.json()["detail"]

  app.github_access = True
  app.manage_apps = False
  db.commit()
  missing_store = client.get("/api/community/github-status", headers=headers)
  assert missing_store.status_code == 403
  assert "manage_apps" in missing_store.json()["detail"]

  app.manage_apps = True
  db.commit()
  monkeypatch.setattr(community.github_auth, "read_state", lambda: {
    "token": "private", "login": "octo-owner",
  })
  ready = client.get("/api/community/github-status", headers=headers)
  assert ready.status_code == 200
  assert ready.json() == {"connected": True, "login": "octo-owner"}


@pytest.mark.asyncio
async def test_publication_lifecycle_reads_through_identity_broker(monkeypatch):
  captured = {}

  async def fake_request(method, path, **kwargs):
    captured.update(method=method, path=path, **kwargs)
    return {"items": []}, 200, {}

  monkeypatch.setattr(community, "_broker_result", fake_request)
  response = await community.list_community_publications(100, 0, None)

  assert response.status_code == 200
  assert captured == {
    "method": "GET",
    "path": "/v1/community/publications",
    "params": {"limit": 100, "offset": 0},
  }


@pytest.mark.asyncio
async def test_authoritative_live_state_reconciles_only_an_older_local_journal(
  monkeypatch,
):
  journal = community_publish.new_publication_journal(
    local_app_id="app:42:pocket-list",
    accepted_commit="a" * 40,
    repository_name="pocket-list",
    repository="octo-owner/pocket-list",
    source_commit_sha="c" * 40,
  )
  journal.updated_at = "2026-08-29T12:00:02+00:00"
  community_publish.write_publication_journal(journal)

  remote = {
    "id": "pub_public_1234",
    "local_app_id": "app:42:pocket-list",
    "status": "live",
    "repository": "octo-owner/pocket-list",
    "repository_url": "https://github.com/octo-owner/pocket-list",
    "commit_sha": "d" * 40,
    "updated_at": "2026-08-29T12:00:01+00:00",
  }

  async def older_remote(*_args, **_kwargs):
    return {"items": [remote]}, 200, {}

  monkeypatch.setattr(community, "_broker_result", older_remote)
  pending = await community.list_community_publications(100, 0, None)
  assert json.loads(pending.body)["items"][0]["status"] == "listing_pending"
  saved = community_publish.read_publication_journal("app:42:pocket-list")
  assert saved is not None

  saved.admission_commit_sha = "d" * 40
  community_publish.write_publication_journal(saved)
  live = await community.list_community_publications(100, 0, None)
  assert json.loads(live.body)["items"][0]["status"] == "live"
  assert community_publish.read_publication_journal("app:42:pocket-list") is None


def test_local_admission_key_binds_the_exact_public_source_intent():
  coordinates = {
    "issuer": "https://www.mobius.you",
    "subject": "user_owner",
    "local_app_id": "app:42:pocket-list",
    "accepted_commit": "a" * 40,
    "repository": "octo-owner/pocket-list",
    "source_commit_sha": "c" * 40,
    "manifest_path": "mobius.json",
    "public_identity": "github",
  }

  original = community._local_admission_key(**coordinates)

  assert community._local_admission_key(**coordinates) == original
  assert community._local_admission_key(
    **{**coordinates, "repository": "octo-owner/another-list"},
  ) != original
  assert community._local_admission_key(
    **{**coordinates, "source_commit_sha": "d" * 40},
  ) != original


@pytest.mark.asyncio
async def test_publish_uses_local_github_token_and_sends_only_public_proof(monkeypatch):
  captured = {}
  github_calls = []

  class Client:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *_):
      return None

  async def fake_github(client, method, path, *, body=None, allow_not_found=False):
    github_calls.append((method, path, body))
    if path == "/user":
      return {"id": 77, "login": "octo-owner"}, 200
    if path == "/repos/octo-owner/pocket-list":
      return {
        "full_name": "octo-owner/pocket-list",
        "private": False,
        "owner": {"id": 77},
      }, 200
    if path.endswith("/git/commits/" + "a" * 40) and method == "GET":
      return {"tree": {"sha": "b" * 40}}, 200
    if "/git/ref/heads/" in path:
      return None, 404
    if path.endswith("/git/commits") and method == "POST":
      return {"sha": "c" * 40}, 201
    if path.endswith("/git/refs") and method == "POST":
      return {"ref": body["ref"]}, 201
    raise AssertionError((method, path, body, allow_not_found))

  async def fake_broker(method, path, **kwargs):
    assert (method, path) == ("GET", "/identity")
    return {
      "issuer": "https://www.mobius.you",
      "subject": "user_owner",
    }, 200, {}

  async def fake_request(method, path, **kwargs):
    captured.update(method=method, path=path, **kwargs)
    return SimpleNamespace(status_code=201)

  monkeypatch.setattr(community.github_auth, "get_token", lambda: "local-only-token")
  monkeypatch.setattr(community.community_broker, "request", fake_broker)
  monkeypatch.setattr(community.httpx, "AsyncClient", lambda **kwargs: Client())
  monkeypatch.setattr(community, "_github_json", fake_github)
  monkeypatch.setattr(community, "_request", fake_request)

  response = await community._publish_existing_github_revision(
    community.ExistingGitHubRevisionIn(
      repository="octo-owner/pocket-list",
      commit_sha="a" * 40,
      manifest_path="mobius.json",
      public_identity="github",
    ),
    "store:publish:0000000000000001",
    local_app_id="app:42:pocket-list",
  )

  assert response.status_code == 201
  assert captured["body"]["github"]["commit_sha"] == "c" * 40
  assert captured["body"]["ownership_proof"] == {
    "kind": "github_commit_v1",
    "parent_sha": "a" * 40,
  }
  assert captured["body"]["local_app_id"] == "app:42:pocket-list"
  commit_body = next(body for method, path, body in github_calls if method == "POST" and path.endswith("/git/commits"))
  assert "[mobius-store-proof:" in commit_body["message"]
  assert "local-only-token" not in repr(captured)


@pytest.mark.asyncio
async def test_local_app_publish_creates_public_github_source_then_lists_exact_commit(
  monkeypatch, db,
):
  github_calls = []
  listed = {}
  source_locked = False
  _local_app(db)

  class Client:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *_):
      return None

  @asynccontextmanager
  async def source_lock(_):
    nonlocal source_locked
    source_locked = True
    try:
      yield
    finally:
      source_locked = False

  async def fake_broker(method, path, **kwargs):
    assert (method, path) == ("GET", "/identity")
    return {"issuer": "https://www.mobius.you", "subject": "user_owner"}, 200, {}

  async def fake_github(client, method, path, *, body=None, allow_not_found=False):
    assert source_locked is False
    github_calls.append((method, path, body))
    if path == "/user":
      return {"id": 77, "login": "octo-owner"}, 200
    if path == "/repos/octo-owner/pocket-list":
      return None, 404
    if path == "/user/repos":
      return {
        "full_name": "octo-owner/pocket-list",
        "private": False,
        "owner": {"id": 77},
      }, 201
    if path.endswith("/git/ref/heads/main"):
      return None, 404
    if path.endswith("/git/blobs"):
      return {"sha": format(len(github_calls), "040x")}, 201
    if path.endswith("/git/trees"):
      return {"sha": "b" * 40}, 201
    if path.endswith("/git/commits"):
      return {"sha": "c" * 40}, 201
    if path.endswith("/git/refs"):
      return {"ref": body["ref"]}, 201
    raise AssertionError((method, path, body, allow_not_found))

  async def fake_prepare(body, key, *, local_app_id=""):
    listed.update(body=body, key=key, local_app_id=local_app_id)
    return {
      "github": {
        "repository": body.repository,
        "commit_sha": "f" * 40,
        "manifest_path": body.manifest_path,
      },
      "local_app_id": local_app_id,
    }, key

  async def fake_host(*_args, **_kwargs):
    return SimpleNamespace(status_code=201, headers={})

  monkeypatch.setattr(community.github_auth, "get_token", lambda: "local-only-token")
  monkeypatch.setattr(community.community_broker, "request", fake_broker)
  monkeypatch.setattr(community.fs_locks, "source_dir_lock", source_lock)
  monkeypatch.setattr(
    community,
    "build_public_snapshot",
    lambda _: (
      "a" * 40,
      [{
        "path": "mobius.json",
        "mode": "100644",
        "content_base64": "e30=",
      }],
    ),
  )
  monkeypatch.setattr(community.httpx, "AsyncClient", lambda **kwargs: Client())
  monkeypatch.setattr(community, "_github_json", fake_github)
  monkeypatch.setattr(community, "_prepare_existing_github_revision", fake_prepare)
  monkeypatch.setattr(community, "_request", fake_host)

  response = await community.publish_local_app_to_github(
    community.PublishLocalGitHubAppIn(
      app_id=42,
      repository_name="pocket-list",
      confirm_source_public=True,
      public_identity="github",
    ),
    db,
    "owner",
    "store:publish-local:000000000001",
  )

  assert response.status_code == 201
  assert listed["body"].repository == "octo-owner/pocket-list"
  assert listed["body"].commit_sha == "c" * 40
  assert listed["key"].startswith("store:publish-local:")
  assert listed["local_app_id"] == "app:42:pocket-list"
  source_commit = next(
    body for method, path, body in github_calls
    if method == "POST" and path.endswith("/git/commits")
  )
  assert "[mobius-store-repository:" in source_commit["message"]
  assert "[mobius-store-source:" in source_commit["message"]
  assert source_commit["parents"] == []
  assert "local-only-token" not in repr(listed)


@pytest.mark.asyncio
async def test_retryable_host_failure_survives_reload_and_resumes_without_republishing(
  monkeypatch, db,
):
  _local_app(db)
  source_calls = []
  host_calls = []

  async def fake_broker(method, path, **kwargs):
    assert (method, path) == ("GET", "/identity")
    return {"issuer": "https://www.mobius.you", "subject": "user_owner"}, 200, {}

  async def fake_source(**kwargs):
    source_calls.append(kwargs)
    return "octo-owner/pocket-list", "c" * 40

  async def fake_prepare(body, key, *, local_app_id=""):
    assert local_app_id == "app:42:pocket-list"
    return {
      "github": {
        "repository": body.repository,
        "commit_sha": "f" * 40,
        "manifest_path": body.manifest_path,
      },
      "public_identity": "github",
      "ownership_proof": {"kind": "github_commit_v1", "parent_sha": body.commit_sha},
      "local_app_id": local_app_id,
    }, key

  async def fake_host(method, path, *, body, idempotency_key):
    assert (method, path) == ("POST", "/v1/community/apps")
    row = community_publish.read_publication_journal("app:42:pocket-list")
    assert row is not None
    assert row.state == "listing_pending"
    assert row.accepted_commit == "a" * 40
    assert row.repository == "octo-owner/pocket-list"
    assert row.source_commit_sha == "c" * 40
    assert row.admission_commit_sha == "f" * 40
    host_calls.append((body, idempotency_key))
    if len(host_calls) == 1:
      raise HTTPException(
        503,
        {"code": "community_unavailable", "message": "Host maintenance is in progress."},
        headers={"Retry-After": "15"},
      )
    return SimpleNamespace(status_code=201, headers={})

  monkeypatch.setattr(community.github_auth, "get_token", lambda: "local-only-token")
  monkeypatch.setattr(community.community_broker, "request", fake_broker)
  monkeypatch.setattr(
    community, "build_public_snapshot",
    lambda _: ("a" * 40, [{"path": "mobius.json"}]),
  )
  monkeypatch.setattr(community, "_publish_local_source", fake_source)
  monkeypatch.setattr(community, "_prepare_existing_github_revision", fake_prepare)
  monkeypatch.setattr(community, "_request", fake_host)
  request = community.PublishLocalGitHubAppIn(
    app_id=42,
    repository_name="pocket-list",
    confirm_source_public=True,
  )

  with pytest.raises(HTTPException) as raised:
    await community.publish_local_app_to_github(
      request, db, None, "store:publish-local:retryable-0001",
    )

  assert raised.value.status_code == 503
  assert raised.value.headers == {"Retry-After": "15"}
  assert raised.value.detail["code"] == "community_unavailable"
  assert raised.value.detail["message"] == "Host maintenance is in progress."
  assert raised.value.detail["retryable"] is True
  assert raised.value.detail["publication"]["status"] == "listing_pending"

  row = community_publish.read_publication_journal("app:42:pocket-list")
  assert row is not None
  assert row.state == "listing_pending"
  assert row.admission_code == "community_unavailable"
  assert row.admission_retryable is True

  async def unavailable_registry(*_args, **_kwargs):
    raise HTTPException(
      503,
      {"code": "community_unavailable", "message": "Host is still unavailable."},
    )

  monkeypatch.setattr(community, "_broker_result", unavailable_registry)
  publication_state = await community.list_community_publications(100, 0, None)
  payload = json.loads(publication_state.body)
  assert payload["items"][0]["status"] == "listing_pending"
  assert payload["items"][0]["repository_url"] == "https://github.com/octo-owner/pocket-list"
  assert payload["items"][0]["admission"] == {
    "code": "community_unavailable",
    "message": "Host maintenance is in progress.",
    "status_code": 503,
    "retryable": True,
  }
  assert payload["registry_unavailable"]["code"] == "community_unavailable"

  response = await community.publish_local_app_to_github(
    request, db, None, "store:publish-local:retryable-0002",
  )

  assert response.status_code == 201
  assert len(source_calls) == 1
  assert len(host_calls) == 2
  assert host_calls[0][1] == host_calls[1][1]
  assert host_calls[1][0]["ownership_proof"]["parent_sha"] == "c" * 40
  live = community_publish.read_publication_journal("app:42:pocket-list")
  assert live is not None
  assert live.state == "live"
  assert live.admission_code == ""


@pytest.mark.asyncio
async def test_permanent_host_failure_preserves_actionable_outcome(monkeypatch, db):
  _local_app(db)
  source_calls = 0
  host_calls = 0

  async def fake_broker(method, path, **kwargs):
    assert (method, path) == ("GET", "/identity")
    return {"issuer": "https://www.mobius.you", "subject": "user_owner"}, 200, {}

  async def fake_source(**_kwargs):
    nonlocal source_calls
    source_calls += 1
    return "octo-owner/pocket-list", "c" * 40

  async def fake_prepare(body, key, *, local_app_id=""):
    return {
      "github": {
        "repository": body.repository,
        "commit_sha": "f" * 40,
        "manifest_path": body.manifest_path,
      },
      "local_app_id": local_app_id,
    }, key

  async def reject_manifest(*_args, **_kwargs):
    nonlocal host_calls
    host_calls += 1
    raise HTTPException(
      422,
      {"code": "manifest_rejected", "message": "The Store category is invalid."},
    )

  monkeypatch.setattr(community.github_auth, "get_token", lambda: "local-only-token")
  monkeypatch.setattr(community.community_broker, "request", fake_broker)
  monkeypatch.setattr(
    community, "build_public_snapshot",
    lambda _: ("a" * 40, [{"path": "mobius.json"}]),
  )
  monkeypatch.setattr(community, "_publish_local_source", fake_source)
  monkeypatch.setattr(community, "_prepare_existing_github_revision", fake_prepare)
  monkeypatch.setattr(community, "_request", reject_manifest)

  with pytest.raises(HTTPException) as raised:
    await community.publish_local_app_to_github(
      community.PublishLocalGitHubAppIn(
        app_id=42,
        repository_name="pocket-list",
        confirm_source_public=True,
      ),
      db,
      None,
      "store:publish-local:permanent-0001",
    )

  assert raised.value.status_code == 422
  assert raised.value.detail["code"] == "manifest_rejected"
  assert raised.value.detail["message"] == "The Store category is invalid."
  assert raised.value.detail["retryable"] is False
  row = community_publish.read_publication_journal("app:42:pocket-list")
  assert row is not None
  assert row.state == "failed"
  assert row.admission_status_code == 422
  assert row.admission_retryable is False

  with pytest.raises(HTTPException) as repeated:
    await community.publish_local_app_to_github(
      community.PublishLocalGitHubAppIn(
        app_id=42,
        repository_name="pocket-list",
        confirm_source_public=True,
      ),
      db,
      None,
      "store:publish-local:permanent-0002",
    )
  assert repeated.value.status_code == 422
  assert repeated.value.detail["code"] == "manifest_rejected"
  assert repeated.value.detail["retryable"] is False
  assert source_calls == 1
  assert host_calls == 1


@pytest.mark.asyncio
async def test_local_app_publish_reuses_identical_managed_main_without_new_write(
  monkeypatch, db,
):
  github_calls = []
  _local_app(db)
  repository_marker = community._local_repository_marker(
    issuer="https://www.mobius.you",
    subject="user_owner",
    repository="octo-owner/pocket-list",
    local_app_id="app:42:pocket-list",
  )
  release_marker = community._local_release_marker(
    repository_marker=repository_marker,
    accepted_commit="a" * 40,
  )

  class Client:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *_):
      return None

  @asynccontextmanager
  async def source_lock(_):
    yield

  async def fake_broker(method, path, **kwargs):
    return {"issuer": "https://www.mobius.you", "subject": "user_owner"}, 200, {}

  async def fake_github(client, method, path, *, body=None, allow_not_found=False):
    github_calls.append((method, path, body))
    if path == "/user":
      return {"id": 77, "login": "octo-owner"}, 200
    if path == "/repos/octo-owner/pocket-list":
      return {
        "full_name": "octo-owner/pocket-list",
        "private": False,
        "owner": {"id": 77},
      }, 200
    if path.endswith("/git/ref/heads/main"):
      return {"object": {"sha": "c" * 40}}, 200
    if path.endswith("/commits/" + "c" * 40):
      return {
        "commit": {"message": f"Managed\n\n{repository_marker}\n{release_marker}"},
      }, 200
    raise AssertionError((method, path, body, allow_not_found))

  async def fake_prepare(body, key, *, local_app_id=""):
    assert body.commit_sha == "c" * 40
    assert local_app_id == "app:42:pocket-list"
    return {
      "github": {
        "repository": body.repository,
        "commit_sha": "f" * 40,
        "manifest_path": body.manifest_path,
      },
      "local_app_id": local_app_id,
    }, key

  async def fake_host(*_args, **_kwargs):
    return SimpleNamespace(status_code=201, headers={})

  monkeypatch.setattr(community.github_auth, "get_token", lambda: "local-only-token")
  monkeypatch.setattr(community.community_broker, "request", fake_broker)
  monkeypatch.setattr(community.fs_locks, "source_dir_lock", source_lock)
  monkeypatch.setattr(
    community, "build_public_snapshot",
    lambda _: ("a" * 40, [{"path": "mobius.json", "mode": "100644", "content_base64": "e30="}]),
  )
  monkeypatch.setattr(community.httpx, "AsyncClient", lambda **kwargs: Client())
  monkeypatch.setattr(community, "_github_json", fake_github)
  monkeypatch.setattr(community, "_prepare_existing_github_revision", fake_prepare)
  monkeypatch.setattr(community, "_request", fake_host)

  await community.publish_local_app_to_github(
    community.PublishLocalGitHubAppIn(
      app_id=42,
      repository_name="pocket-list",
      confirm_source_public=True,
    ),
    db,
    "owner",
    "store:publish-local:000000000002",
  )

  assert not any(method != "GET" for method, _, _ in github_calls)


@pytest.mark.asyncio
async def test_local_app_update_only_fast_forwards_its_managed_main(
  monkeypatch, db,
):
  github_calls = []
  _local_app(db)
  repository_marker = community._local_repository_marker(
    issuer="https://www.mobius.you",
    subject="user_owner",
    repository="octo-owner/pocket-list",
    local_app_id="app:42:pocket-list",
  )

  class Client:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *_):
      return None

  @asynccontextmanager
  async def source_lock(_):
    yield

  async def fake_broker(method, path, **kwargs):
    return {"issuer": "https://www.mobius.you", "subject": "user_owner"}, 200, {}

  async def fake_github(client, method, path, *, body=None, allow_not_found=False):
    github_calls.append((method, path, body))
    if path == "/user":
      return {"id": 77, "login": "octo-owner"}, 200
    if path == "/repos/octo-owner/pocket-list":
      return {
        "full_name": "octo-owner/pocket-list",
        "private": False,
        "owner": {"id": 77},
      }, 200
    if path.endswith("/git/ref/heads/main"):
      return {"object": {"sha": "d" * 40}}, 200
    if path.endswith("/commits/" + "d" * 40):
      return {"commit": {"message": f"Previous release\n\n{repository_marker}"}}, 200
    if path.endswith("/git/blobs"):
      return {"sha": "1" * 40}, 201
    if path.endswith("/git/trees"):
      return {"sha": "b" * 40}, 201
    if path.endswith("/git/commits"):
      return {"sha": "e" * 40}, 201
    if method == "PATCH" and path.endswith("/git/refs/heads/main"):
      return {"object": {"sha": body["sha"]}}, 200
    raise AssertionError((method, path, body, allow_not_found))

  async def fake_prepare(body, key, *, local_app_id=""):
    assert body.commit_sha == "e" * 40
    assert local_app_id == "app:42:pocket-list"
    return {
      "github": {
        "repository": body.repository,
        "commit_sha": "f" * 40,
        "manifest_path": body.manifest_path,
      },
      "local_app_id": local_app_id,
    }, key

  async def fake_host(*_args, **_kwargs):
    return SimpleNamespace(status_code=201, headers={})

  monkeypatch.setattr(community.github_auth, "get_token", lambda: "local-only-token")
  monkeypatch.setattr(community.community_broker, "request", fake_broker)
  monkeypatch.setattr(community.fs_locks, "source_dir_lock", source_lock)
  monkeypatch.setattr(
    community, "build_public_snapshot",
    lambda _: ("a" * 40, [{"path": "mobius.json", "mode": "100644", "content_base64": "e30="}]),
  )
  monkeypatch.setattr(community.httpx, "AsyncClient", lambda **kwargs: Client())
  monkeypatch.setattr(community, "_github_json", fake_github)
  monkeypatch.setattr(community, "_prepare_existing_github_revision", fake_prepare)
  monkeypatch.setattr(community, "_request", fake_host)

  await community.publish_local_app_to_github(
    community.PublishLocalGitHubAppIn(
      app_id=42,
      repository_name="pocket-list",
      confirm_source_public=True,
    ),
    db,
    "owner",
    "store:publish-local:000000000003",
  )

  source_commit = next(
    body for method, path, body in github_calls
    if method == "POST" and path.endswith("/git/commits")
  )
  update_ref = next(
    body for method, path, body in github_calls
    if method == "PATCH" and path.endswith("/git/refs/heads/main")
  )
  assert source_commit["parents"] == ["d" * 40]
  assert update_ref == {"sha": "e" * 40, "force": False}


@pytest.mark.asyncio
async def test_install_receipt_keeps_exact_revision_available(monkeypatch):
  captured = {}

  async def fake_request(method, path, **kwargs):
    captured.update(method=method, path=path, **kwargs)
    return SimpleNamespace(status_code=201)

  monkeypatch.setattr(community, "_request", fake_request)
  response = await community.record_community_install(
    "app_public_1234",
    "rev_public_1234",
    community.InstallReceiptIn(local_app_id="app:42:shared-notes"),
    None,
    "store:install:0000000000000001",
  )

  assert response.status_code == 201
  assert captured == {
    "method": "POST",
    "path": (
      "/v1/community/apps/app_public_1234/revisions/"
      "rev_public_1234/installs"
    ),
    "body": {"local_app_id": "app:42:shared-notes"},
    "idempotency_key": "store:install:0000000000000001",
  }
