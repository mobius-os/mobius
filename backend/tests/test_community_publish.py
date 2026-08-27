import base64
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from app.community_publish import (
  CommunityPublicationError,
  build_public_snapshot,
  public_store_listing,
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
