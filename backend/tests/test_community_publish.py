import base64
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from app import app_git, community_publish
from app.community_publish import (
  CommunityPublicationError,
  build_public_snapshot,
  public_store_listing,
  read_public_store_asset,
)


def _git(repo, *args):
  env = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
  }
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    env=env,
    capture_output=True,
    text=True,
    check=True,
  ).stdout.strip()


def _public_file(path, content=b""):
  return {
    "path": path,
    "mode": "100644",
    "content_base64": base64.b64encode(content).decode("ascii"),
  }


def test_listing_art_is_direct_versioned_source_not_a_runtime_asset():
  manifest = {
    "id": "pocket-list",
    "name": "Pocket List",
    "description": "A small list.",
    "version": "1.0.0",
    "entry": "index.jsx",
    "icon": "icon.png",
    "store": {
      "tagline": "Small and exact.",
      "description": "A calm list for the things that matter.",
      "hero": "static/store/hero.png",
      "screenshots": [{
        "src": "static/store/screen.png",
        "alt": "Pocket List showing three items.",
      }],
    },
  }
  files = [
    _public_file("mobius.json", json.dumps(manifest).encode()),
    _public_file("index.jsx", b"export default function App() {}"),
    _public_file("icon.png", b"icon"),
    _public_file("static/store/hero.png", b"hero"),
    _public_file("static/store/screen.png", b"screen"),
  ]

  listing = public_store_listing(files)

  assert listing["hero"] == {"path": "static/store/hero.png"}
  assert listing["screenshots"][0]["src"] == "static/store/screen.png"
  assert "static_assets" not in manifest


def test_preview_asset_reads_the_accepted_tree_not_the_editable_worktree(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  manifest = json.loads((repo / "mobius.json").read_text())
  manifest["icon"] = "icon.png"
  manifest["store"] = {
    "tagline": "Small and exact.",
    "description": "A calm list for the things that matter.",
    "screenshots": [{
      "src": "static/store/screen.png",
      "alt": "Pocket List showing three items.",
    }],
  }
  (repo / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  (repo / "icon.png").write_bytes(b"accepted-icon")
  (repo / "static" / "store").mkdir(parents=True)
  screenshot = repo / "static" / "store" / "screen.png"
  screenshot.write_bytes(b"accepted-screen")
  _git(repo, "add", "mobius.json", "icon.png", "static/store/screen.png")
  _git(repo, "commit", "-m", "accepted listing")
  accepted = _git(repo, "rev-parse", "HEAD")
  app.source_commit = accepted
  screenshot.write_bytes(b"editable-draft")

  assert read_public_store_asset(
    app, accepted, "static/store/screen.png",
  ) == b"accepted-screen"

  with pytest.raises(CommunityPublicationError) as unavailable:
    read_public_store_asset(app, accepted, "index.jsx")
  assert unavailable.value.code == "listing_asset_unavailable"

  with pytest.raises(CommunityPublicationError) as changed:
    read_public_store_asset(app, "f" * 40, "static/store/screen.png")
  assert changed.value.code == "accepted_revision_changed"


def test_app_git_accepts_store_art_but_excludes_runtime_static_assets(tmp_path):
  repo = tmp_path / "app"
  repo.mkdir()
  app_git.ensure_repo(repo)
  manifest = {
    "id": "pocket-list",
    "name": "Pocket List",
    "description": "A small list.",
    "version": "1.0.0",
    "entry": "index.jsx",
    "icon": "icon.png",
    "store": {
      "tagline": "Small and exact.",
      "description": "A calm list for the things that matter.",
      "screenshots": [{
        "src": "static/store/screen.png",
        "alt": "Pocket List showing three items.",
      }],
    },
  }
  (repo / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  (repo / "index.jsx").write_text(
    "export default function App() { return null }\n", encoding="utf-8",
  )
  (repo / "icon.png").write_bytes(b"icon")
  (repo / "static" / "store").mkdir(parents=True)
  (repo / "static" / "store" / "screen.png").write_bytes(b"screen")
  (repo / "static" / "runtime").mkdir()
  (repo / "static" / "runtime" / "bundle.js").write_bytes(b"generated")
  (repo / ".mobius-static-assets.json").write_text(
    json.dumps(["runtime/bundle.js"]), encoding="utf-8",
  )

  snapshot = app_git.snapshot_worktree(repo)
  accepted = app_git.commit_worktree_tree(repo, snapshot, "apply app")
  assert accepted is not None
  app = SimpleNamespace(
    id=41, source_dir=str(repo), source_commit=accepted, compiled_path="bundle.js",
  )

  resolved, files = build_public_snapshot(app)
  listing = public_store_listing(files)
  published_paths = {item["path"] for item in files}

  assert resolved == accepted
  assert listing["screenshots"][0]["src"] == "static/store/screen.png"
  assert "static/store/screen.png" in published_paths
  assert "static/runtime/bundle.js" not in published_paths
  assert ".mobius-static-assets.json" not in published_paths


def test_listing_art_must_use_the_existing_local_static_route():
  manifest = {
    "id": "pocket-list",
    "name": "Pocket List",
    "description": "A small list.",
    "version": "1.0.0",
    "entry": "index.jsx",
    "icon": "icon.png",
    "store": {
      "tagline": "Small and exact.",
      "description": "A calm list for the things that matter.",
      "screenshots": [{"src": "listing/screen.png", "alt": "Pocket List"}],
    },
  }
  files = [
    _public_file("mobius.json", json.dumps(manifest).encode()),
    _public_file("icon.png", b"icon"),
    _public_file("listing/screen.png", b"screen"),
  ]

  with pytest.raises(CommunityPublicationError) as raised:
    public_store_listing(files)

  assert raised.value.code == "listing_incomplete"


def _app_repo(tmp_path, *, source="export default function App(){return null}"):
  repo = tmp_path / "app"
  repo.mkdir()
  _git(repo, "init", "-b", "main")
  (repo / "mobius.json").write_text(json.dumps({
    "id": "test-app",
    "name": "Test app",
    "description": "A test.",
    "version": "1.0.0",
    "entry": "index.jsx",
    "source_files": ["index.jsx"],
  }))
  (repo / "index.jsx").write_text(source)
  _git(repo, "add", "mobius.json", "index.jsx")
  _git(repo, "commit", "-m", "accepted app")
  commit = _git(repo, "rev-parse", "HEAD")
  app = SimpleNamespace(
    id=41, source_dir=str(repo), source_commit=commit, compiled_path="bundle.js",
  )
  return repo, app, commit


def test_snapshot_reads_only_accepted_commit_not_editable_draft(tmp_path):
  repo, app, commit = _app_repo(tmp_path)
  (repo / "index.jsx").write_text("const draft = 'not accepted'")
  (repo / ".env").write_text("GH_TOKEN=not-part-of-accepted-commit")

  resolved, files = build_public_snapshot(app)

  assert resolved == commit
  decoded = {
    item["path"]: __import__("base64").b64decode(item["content_base64"])
    for item in files
  }
  assert set(decoded) == {"index.jsx", "mobius.json"}
  assert b"not accepted" not in decoded["index.jsx"]


def test_snapshot_rejects_tracked_secret_before_broker_upload(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  (repo / "key.txt").write_text("-----BEGIN PRIVATE KEY-----\nnot-a-real-key")
  _git(repo, "add", "key.txt")
  _git(repo, "commit", "-m", "oops")
  app.source_commit = _git(repo, "rev-parse", "HEAD")

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "secret_detected"


def test_snapshot_rejects_tracked_environment_files(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  (repo / ".env.local").write_text("SERVICE_PASSWORD=private", encoding="utf-8")
  _git(repo, "add", "-f", ".env.local")
  _git(repo, "commit", "-m", "tracked environment")
  app.source_commit = _git(repo, "rev-parse", "HEAD")

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "sensitive_path"


def test_snapshot_enforces_the_host_release_size_limit(tmp_path, monkeypatch):
  repo, app, _ = _app_repo(tmp_path, source="export default 1")
  monkeypatch.setattr("app.community_publish.MAX_SOURCE_BYTES", 1)

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "payload_too_large"


def test_snapshot_rejects_symlink_instead_of_following_it(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  (repo / "linked").symlink_to("/etc/passwd")
  _git(repo, "add", "linked")
  _git(repo, "commit", "-m", "symlink")
  app.source_commit = _git(repo, "rev-parse", "HEAD")

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "invalid_file_type"


def test_journal_root_symlink_is_rejected_for_every_operation(
  tmp_path, monkeypatch,
):
  outside = tmp_path / "outside"
  outside.mkdir()
  root = tmp_path / "community-publications"
  root.symlink_to(outside, target_is_directory=True)
  monkeypatch.setattr(community_publish, "_journal_root", lambda: root)
  journal = community_publish.new_publication_journal(
    local_app_id="app:42:pocket-list",
    accepted_commit="a" * 40,
    repository_name="pocket-list",
    repository="octo-owner/pocket-list",
    source_commit_sha="c" * 40,
  )

  for operation in (
    lambda: community_publish.read_publication_journal(journal.local_app_id),
    community_publish.list_publication_journals,
    lambda: community_publish.write_publication_journal(journal),
    lambda: community_publish.delete_publication_journal(journal.local_app_id),
  ):
    with pytest.raises(CommunityPublicationError) as raised:
      operation()
    assert raised.value.code == "publication_journal_invalid"


def test_partial_success_journal_contains_only_bounded_public_state(
  tmp_path, monkeypatch,
):
  root = tmp_path / "community-publications"
  monkeypatch.setattr(community_publish, "_journal_root", lambda: root)
  journal = community_publish.new_publication_journal(
    local_app_id="app:42:pocket-list",
    accepted_commit="a" * 40,
    repository_name="pocket-list",
    repository="octo-owner/pocket-list",
    source_commit_sha="c" * 40,
  )

  community_publish.write_publication_journal(journal)

  raw = next(root.glob("*.json")).read_text()
  assert set(json.loads(raw)) == {
    "accepted_commit",
    "admission_code",
    "admission_commit_sha",
    "admission_message",
    "admission_retryable",
    "admission_status_code",
    "created_at",
    "id",
    "local_app_id",
    "repository",
    "repository_name",
    "source_commit_sha",
    "state",
    "updated_at",
  }
  assert "token" not in raw.casefold()
  assert community_publish.read_publication_journal(
    "app:42:pocket-list",
  ) == journal

  journal.admission_message = "x" * 401
  with pytest.raises(CommunityPublicationError) as raised:
    community_publish.write_publication_journal(journal)
  assert raised.value.code == "publication_journal_invalid"
