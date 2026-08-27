from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app import models
from app.routes import community


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
async def test_local_publication_preview_is_bound_to_the_accepted_listing(
  monkeypatch,
):
  app = SimpleNamespace(
    id=42,
    slug="pocket-list",
    name="Pocket List",
    source_dir="/data/apps/pocket-list",
    updated_at="2026-08-27 12:00:00",
  )

  class Query:
    def filter(self, *_):
      return self

    def first(self):
      return app

  class DB:
    def query(self, *_):
      return Query()

  @asynccontextmanager
  async def source_lock(_):
    yield

  listing = {
    "tagline": "Small and exact.",
    "description": "The accepted listing.",
    "icon": "icon.png",
    "hero": None,
    "screenshots": [{
      "src": "static/store/screen.png",
      "alt": "Pocket List's main screen.",
    }],
    "featured": False,
  }
  files = [{"path": "mobius.json", "content_base64": "e30="}]
  monkeypatch.setattr(community.fs_locks, "source_dir_lock", source_lock)
  monkeypatch.setattr(
    community, "build_public_snapshot", lambda _: ("a" * 40, files),
  )
  monkeypatch.setattr(community, "public_store_listing", lambda value: listing)

  preview = await community.preview_local_app_publication(42, DB(), None)

  assert preview["accepted_commit"] == "a" * 40
  assert preview["repository_name"] == "pocket-list"
  assert preview["asset_base"] == "/app-assets/by-id/42/"
  assert preview["listing"] is listing


@pytest.mark.asyncio
async def test_publication_lifecycle_reads_through_identity_broker(monkeypatch):
  captured = {}

  async def fake_request(method, path, **kwargs):
    captured.update(method=method, path=path, **kwargs)
    return SimpleNamespace(status_code=200)

  monkeypatch.setattr(community, "_request", fake_request)
  response = await community.list_community_publications(100, 0, None)

  assert response.status_code == 200
  assert captured == {
    "method": "GET",
    "path": "/v1/community/publications",
    "params": {"limit": 100, "offset": 0},
  }


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
async def test_local_app_publish_creates_public_github_source_then_lists_exact_commit(monkeypatch):
  github_calls = []
  listed = {}
  app = SimpleNamespace(
    id=42,
    slug="pocket-list",
    name="Pocket List",
    description="A small shared list.",
    source_dir="/data/apps/pocket-list",
  )

  class Query:
    def filter(self, *_):
      return self

    def first(self):
      return app

  class DB:
    def query(self, *_):
      return Query()

  class Client:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *_):
      return None

  @asynccontextmanager
  async def source_lock(_):
    yield

  async def fake_broker(method, path, **kwargs):
    assert (method, path) == ("GET", "/identity")
    return {"issuer": "https://www.mobius.you", "subject": "user_owner"}, 200, {}

  async def fake_github(client, method, path, *, body=None, allow_not_found=False):
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

  async def fake_list(body, key, *, local_app_id=""):
    listed.update(body=body, key=key, local_app_id=local_app_id)
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
  monkeypatch.setattr(community, "public_store_listing", lambda _: {})
  monkeypatch.setattr(community.httpx, "AsyncClient", lambda **kwargs: Client())
  monkeypatch.setattr(community, "_github_json", fake_github)
  monkeypatch.setattr(community, "_publish_existing_github_revision", fake_list)

  response = await community.publish_local_app_to_github(
    community.PublishLocalGitHubAppIn(
      app_id=42,
      repository_name="pocket-list",
      confirm_source_public=True,
      public_identity="github",
    ),
    DB(),
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
async def test_local_app_publish_reuses_identical_managed_main_without_new_write(monkeypatch):
  github_calls = []
  app = SimpleNamespace(
    id=42,
    slug="pocket-list",
    name="Pocket List",
    description="A small shared list.",
    source_dir="/data/apps/pocket-list",
  )
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

  class Query:
    def filter(self, *_):
      return self

    def first(self):
      return app

  class DB:
    def query(self, *_):
      return Query()

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

  async def fake_list(body, key, *, local_app_id=""):
    assert body.commit_sha == "c" * 40
    assert local_app_id == "app:42:pocket-list"
    return SimpleNamespace(status_code=201, headers={})

  monkeypatch.setattr(community.github_auth, "get_token", lambda: "local-only-token")
  monkeypatch.setattr(community.community_broker, "request", fake_broker)
  monkeypatch.setattr(community.fs_locks, "source_dir_lock", source_lock)
  monkeypatch.setattr(
    community, "build_public_snapshot",
    lambda _: ("a" * 40, [{"path": "mobius.json", "mode": "100644", "content_base64": "e30="}]),
  )
  monkeypatch.setattr(community, "public_store_listing", lambda _: {})
  monkeypatch.setattr(community.httpx, "AsyncClient", lambda **kwargs: Client())
  monkeypatch.setattr(community, "_github_json", fake_github)
  monkeypatch.setattr(community, "_publish_existing_github_revision", fake_list)

  await community.publish_local_app_to_github(
    community.PublishLocalGitHubAppIn(
      app_id=42,
      repository_name="pocket-list",
      confirm_source_public=True,
    ),
    DB(),
    "owner",
    "store:publish-local:000000000002",
  )

  assert not any(method != "GET" for method, _, _ in github_calls)


@pytest.mark.asyncio
async def test_local_app_update_only_fast_forwards_its_managed_main(monkeypatch):
  github_calls = []
  app = SimpleNamespace(
    id=42,
    slug="pocket-list",
    name="Pocket List",
    description="A small shared list.",
    source_dir="/data/apps/pocket-list",
  )
  repository_marker = community._local_repository_marker(
    issuer="https://www.mobius.you",
    subject="user_owner",
    repository="octo-owner/pocket-list",
    local_app_id="app:42:pocket-list",
  )

  class Query:
    def filter(self, *_):
      return self

    def first(self):
      return app

  class DB:
    def query(self, *_):
      return Query()

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

  async def fake_list(body, key, *, local_app_id=""):
    assert body.commit_sha == "e" * 40
    assert local_app_id == "app:42:pocket-list"
    return SimpleNamespace(status_code=201, headers={})

  monkeypatch.setattr(community.github_auth, "get_token", lambda: "local-only-token")
  monkeypatch.setattr(community.community_broker, "request", fake_broker)
  monkeypatch.setattr(community.fs_locks, "source_dir_lock", source_lock)
  monkeypatch.setattr(
    community, "build_public_snapshot",
    lambda _: ("a" * 40, [{"path": "mobius.json", "mode": "100644", "content_base64": "e30="}]),
  )
  monkeypatch.setattr(community, "public_store_listing", lambda _: {})
  monkeypatch.setattr(community.httpx, "AsyncClient", lambda **kwargs: Client())
  monkeypatch.setattr(community, "_github_json", fake_github)
  monkeypatch.setattr(community, "_publish_existing_github_revision", fake_list)

  await community.publish_local_app_to_github(
    community.PublishLocalGitHubAppIn(
      app_id=42,
      repository_name="pocket-list",
      confirm_source_public=True,
    ),
    DB(),
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
