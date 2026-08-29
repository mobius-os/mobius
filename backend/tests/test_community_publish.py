import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from app import community_publish
from app.community_publish import CommunityPublicationError, build_public_snapshot


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
