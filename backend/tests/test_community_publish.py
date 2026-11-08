import base64
import io
import json
import os
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

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


def _png(color=(40, 90, 180), size=(8, 8), image_format="PNG"):
  output = io.BytesIO()
  Image.new("RGB", size, color).save(output, format=image_format)
  return output.getvalue()


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
    _public_file("icon.png", _png((1, 1, 1))),
    _public_file("static/store/hero.png", _png((2, 2, 2))),
    _public_file("static/store/screen.png", _png((3, 3, 3))),
  ]

  listing = public_store_listing(files)

  assert listing["hero"] == {"path": "static/store/hero.png"}
  assert listing["screenshots"][0]["src"] == "static/store/screen.png"
  assert "static_assets" not in manifest


_PNG = b"\x89PNG\r\n\x1a\n"
_ACCEPTED_SCREEN = _png((4, 4, 4))
_SCREEN = _png((5, 5, 5))


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
  (repo / "icon.png").write_bytes(_png((6, 6, 6)))
  (repo / "static" / "store").mkdir(parents=True)
  screenshot = repo / "static" / "store" / "screen.png"
  screenshot.write_bytes(_ACCEPTED_SCREEN)
  _git(repo, "add", "mobius.json", "icon.png", "static/store/screen.png")
  _git(repo, "commit", "-m", "accepted listing")
  accepted = _git(repo, "rev-parse", "HEAD")
  app.source_commit = accepted
  screenshot.write_bytes(b"editable-draft")

  assert read_public_store_asset(
    app, accepted, "static/store/screen.png",
  ) == (_ACCEPTED_SCREEN, "image/png")

  with pytest.raises(CommunityPublicationError) as unavailable:
    read_public_store_asset(app, accepted, "index.jsx")
  assert unavailable.value.code == "listing_asset_unavailable"

  with pytest.raises(CommunityPublicationError) as changed:
    read_public_store_asset(app, "f" * 40, "static/store/screen.png")
  assert changed.value.code == "accepted_revision_changed"


def test_preview_asset_type_follows_the_bytes_not_the_name(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  manifest = json.loads((repo / "mobius.json").read_text())
  manifest["store"] = {"screenshots": [{"src": "static/store/shot.png", "alt": "A"}]}
  (repo / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  (repo / "static" / "store").mkdir(parents=True)
  jpeg = _png((9, 9, 9), image_format="JPEG")
  (repo / "static" / "store" / "shot.png").write_bytes(jpeg)
  _git(repo, "add", "mobius.json", "static/store/shot.png")
  _git(repo, "commit", "-m", "jpeg under a png name")
  app.source_commit = accepted = _git(repo, "rev-parse", "HEAD")

  assert read_public_store_asset(app, accepted, "static/store/shot.png") == (
    jpeg, "image/jpeg",
  )


def test_incomplete_listing_cannot_make_other_source_public(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  manifest = json.loads((repo / "mobius.json").read_text())
  manifest["store"] = {
    "tagline": "Draft", "description": "Incomplete draft",
    "hero": "private-notes.txt",
    "screenshots": [{"src": "static/store/screen.png", "alt": "Screen"}],
  }
  (repo / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  (repo / "private-notes.txt").write_text("not public", encoding="utf-8")
  (repo / "static" / "store").mkdir(parents=True)
  (repo / "static" / "store" / "screen.png").write_bytes(_SCREEN)
  _git(repo, "add", "mobius.json", "private-notes.txt", "static/store/screen.png")
  _git(repo, "commit", "-m", "draft listing")
  accepted = _git(repo, "rev-parse", "HEAD")
  app.source_commit = accepted

  with pytest.raises(CommunityPublicationError) as private:
    read_public_store_asset(app, accepted, "private-notes.txt")
  assert private.value.code == "listing_asset_unavailable"
  assert read_public_store_asset(app, accepted, "static/store/screen.png") == (
    _SCREEN, "image/png",
  )


def test_preview_serves_only_images_even_for_the_declared_icon(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  manifest = json.loads((repo / "mobius.json").read_text())
  # A Store-managed app's local manifest is never validated by apply, so its
  # icon can name any tracked file.
  manifest["icon"] = "index.jsx"
  manifest["store"] = {"screenshots": [{"src": "static/store/big.png", "alt": "A"}]}
  (repo / "mobius.json").write_text(json.dumps(manifest), encoding="utf-8")
  (repo / "static" / "store").mkdir(parents=True)
  (repo / "static" / "store" / "big.png").write_bytes(
    _PNG + b"x" * community_publish.STORE_LISTING_IMAGE_MAX_BYTES,
  )
  _git(repo, "add", "mobius.json", "static/store/big.png")
  _git(repo, "commit", "-m", "listing")
  accepted = _git(repo, "rev-parse", "HEAD")
  app.source_commit = accepted

  for path in ("index.jsx", "static/store/big.png"):
    with pytest.raises(CommunityPublicationError) as refused:
      read_public_store_asset(app, accepted, path)
    assert refused.value.code == "listing_asset_unavailable"


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
  (repo / "icon.png").write_bytes(_png())
  (repo / "static" / "store").mkdir(parents=True)
  (repo / "static" / "store" / "screen.png").write_bytes(_png((7, 7, 7)))
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
    _public_file("icon.png", _png((1, 1, 1))),
    _public_file("listing/screen.png", _png((3, 3, 3))),
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
  monkeypatch.setattr("app.community_publish.PACKAGE_MAX_BYTES", 1)

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "payload_too_large"


def test_snapshot_bounds_what_install_writes_not_only_the_tree(
  tmp_path, monkeypatch,
):
  """Install writes every static-asset destination separately, so pointing
  many destinations at one file must not let an accepted snapshot fail to
  install. Publication bounds the same declared sum install charges."""
  repo, app, _ = _app_repo(tmp_path)
  manifest = json.loads((repo / "mobius.json").read_text())
  manifest["static_assets"] = {f"copy-{n}.bin": "data.bin" for n in range(8)}
  (repo / "mobius.json").write_text(json.dumps(manifest))
  (repo / "data.bin").write_bytes(b"x" * 1000)
  _git(repo, "add", "mobius.json", "data.bin")
  _git(repo, "commit", "-m", "fan out one file")
  app.source_commit = _git(repo, "rev-parse", "HEAD")
  tree_bytes = sum(
    len(path.read_bytes()) for path in repo.iterdir() if path.is_file()
  )
  monkeypatch.setattr("app.community_publish.PACKAGE_MAX_BYTES", tree_bytes + 1000)

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "payload_too_large"
  assert "declares" in raised.value.detail


def test_snapshot_refuses_a_manifest_install_would_refuse(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  manifest = json.loads((repo / "mobius.json").read_text())
  manifest["source_files"] = ["index.jsx"]
  (repo / "mobius.json").write_text(json.dumps(manifest))
  _git(repo, "add", "mobius.json")
  _git(repo, "commit", "-m", "entry listed as a module")
  app.source_commit = _git(repo, "rev-parse", "HEAD")

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "invalid_manifest"


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


def test_listing_preview_can_inspect_legacy_source_but_publication_requires_manifest(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  _git(repo, "rm", "mobius.json")
  _git(repo, "commit", "-m", "legacy source")
  app.source_commit = _git(repo, "rev-parse", "HEAD")

  commit, files, problems = community_publish.read_public_snapshot(app)
  assert commit == app.source_commit and problems == []
  assert files and not any(item["path"] == "mobius.json" for item in files)
  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)
  assert raised.value.code == "invalid_manifest"


def _commit_files(repo, app, files):
  for path, content in files.items():
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
  _git(repo, "add", "-f", "--", *files)
  _git(repo, "commit", "-m", "more files")
  app.source_commit = _git(repo, "rev-parse", "HEAD")


def test_a_tree_over_the_file_limit_reads_only_what_the_review_needs(
  tmp_path, monkeypatch,
):
  repo, app, _ = _app_repo(tmp_path)
  _commit_files(repo, app, {
    f"src/file{index}.js": b"export {}" for index in range(community_publish.MAX_SOURCE_FILES + 50)
  })
  reads = []
  real_blob = community_publish._blob
  monkeypatch.setattr(
    community_publish, "_blob",
    lambda repo, entry: reads.append(entry.path) or real_blob(repo, entry),
  )

  _, files, problems = community_publish.read_public_snapshot(app)

  assert [problem.code for problem in problems] == ["payload_too_large"]
  assert [item["path"] for item in files] == ["mobius.json"]
  assert set(reads) == {"mobius.json"} and len(reads) <= 2


def test_snapshot_publishes_file_names_install_accepts(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  names = [
    "Screenshot (1).png", "café.mp3", "[id].jsx", "a~b.md",
  ]
  _commit_files(repo, app, {name: b"ordinary source" for name in names})

  _, files = build_public_snapshot(app)

  assert set(names) <= {item["path"] for item in files}


@pytest.mark.parametrize("folder", ["secrets", "credentials"])
def test_snapshot_publishes_private_sounding_folders_without_secret_content(tmp_path, folder):
  repo, app, _ = _app_repo(tmp_path)
  path = f"{folder}/ordinary.txt"
  _commit_files(repo, app, {path: b"ordinary source"})

  _, files = build_public_snapshot(app)

  assert path in {item["path"] for item in files}


# What a private folder typically holds is refused by its own name, whatever
# folder it sits in: the folder name is not what makes it private.
@pytest.mark.parametrize("folder", ["", "src/", "secrets/", "credentials/", "config/"])
@pytest.mark.parametrize("name", [
  ".env", ".env.production", "prod.env", ".netrc", ".npmrc", "id_ed25519",
  "credentials.json", "secrets.json", "service-account.json", ".secret-key",
  "service-token.txt", "tls.key", "release.jks",
  "debug.keystore", "trust.bks", "keys.jceks", "client.p12", "client.pfx",
  "deploy.ppk", "vault.kdbx",
])
def test_snapshot_refuses_credential_files_by_name_in_any_folder(tmp_path, folder, name):
  repo, app, _ = _app_repo(tmp_path)
  path = f"{folder}{name}"
  _commit_files(repo, app, {path: b"\x30\x82 opaque"})

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "sensitive_path"
  assert path in raised.value.detail


@pytest.mark.parametrize("name, content", [
  ("server_private", b"-----BEGIN PRIVATE KEY-----\nMIIE"),
  ("tls_private", b"-----BEGIN EC PRIVATE KEY-----\nMHcC"),
  ("deploy_key", b"-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl"),
  ("signing.asc", b"-----BEGIN PGP PRIVATE KEY BLOCK-----\n"),
  ("token.json", b'{"token": "ya29.' + b"a0Af" * 10 + b'"}'),
  ("client_secret.json", b'{"installed": {"client_secret": "GOCSPX-' + b"A" * 28 + b'"}}'),
])
def test_snapshot_refuses_recognizable_credentials_in_private_folders(tmp_path, name, content):
  repo, app, _ = _app_repo(tmp_path)
  _commit_files(repo, app, {f"secrets/{name}": content})

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "secret_detected"


def test_content_scan_reads_tokens_inside_a_binary_database(tmp_path):
  import sqlite3

  database = tmp_path / "vault.db"
  with sqlite3.connect(database) as connection:
    connection.execute("create table keys (value text)")
    connection.execute("insert into keys values (?)", ("ghp_" + "A" * 36,))
  connection.close()
  repo, app, _ = _app_repo(tmp_path)
  _commit_files(repo, app, {"secrets/vault.db": database.read_bytes()})

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "secret_detected"


def test_snapshot_names_the_path_it_refuses_and_why(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  _commit_files(repo, app, {".github/workflows/ci.yml": b"on: push\n"})

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "sensitive_path"
  assert ".github/workflows/ci.yml" in raised.value.detail
  assert "token scope" in raised.value.detail


def test_snapshot_refuses_control_characters_and_names_the_path(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  _commit_files(repo, app, {"bad\nname.js": b"export {}"})

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "invalid_path"
  assert "'bad\\nname.js'" in raised.value.detail


def test_snapshot_refuses_names_that_collide_on_other_checkouts(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  _commit_files(repo, app, {"café.md": b"a", "café.md": b"b"})

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "invalid_path"


def test_content_scan_still_catches_credentials_in_any_directory(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  _commit_files(repo, app, {
    "config/config.js": b"export const key = 'ghp_" + b"A" * 36 + b"';",
  })

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "secret_detected"


@pytest.mark.parametrize("folder", ["config", "settings"])
def test_content_scan_catches_ascii_credentials_in_invalid_utf8(tmp_path, folder):
  repo, app, _ = _app_repo(tmp_path)
  _commit_files(repo, app, {
    f"{folder}/config.txt": b"\xff\nTOKEN=ghp_" + b"A" * 36,
  })

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "secret_detected"


@pytest.mark.parametrize("character", [
  "\u2028", "\u2029", "\u200e", "\u200f", "\u061c", "\u200b", "\u200c",
  "\u200d", "\u2060", "\u180e", "\u2062", "\ufeff",
  "\u00a0", "\u3000", "\u115f", "\u1160", "\u3164", "\uffa0", "\u2800",
])
def test_snapshot_refuses_invisible_filename_characters(tmp_path, character):
  repo, app, _ = _app_repo(tmp_path)
  _commit_files(repo, app, {f"look{character}alike.js": b"export {}"})

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "invalid_path"


@pytest.mark.parametrize("character", [
  "\u00ad", "\u034f", "\u0378", "\ue000", "\ud800",
  "\u17b4", "\u17b5", "\u180b", "\u180c", "\u180d", "\u180f",
  "\ufe00", "\ufe0f", "\U000e0100", "\U000e01ef",
])
def test_publication_path_refuses_ignorable_and_nonpublic_code_points(character):
  from app.community_publish import _validate_path

  with pytest.raises(CommunityPublicationError) as raised:
    _validate_path(f"look{character}alike.js")
  assert raised.value.code == "invalid_path"


@pytest.mark.parametrize("path", [" leading.js", "trailing.js ", "dir /file", "dir/ file"])
def test_publication_path_refuses_space_padded_segments(path):
  from app.community_publish import _validate_path

  with pytest.raises(CommunityPublicationError) as raised:
    _validate_path(path)
  assert raised.value.code == "invalid_path"
  assert "leading or trailing spaces" in raised.value.detail


def test_publication_path_accepts_visible_unicode_and_internal_spaces():
  from app.community_publish import _validate_path

  _validate_path("café folder/日本語 [1] e\u0301.js")


def test_snapshot_allows_slack_placeholder(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  _commit_files(repo, app, {"README.md": b"Set xoxb-your-token-here in the example."})

  _, files = build_public_snapshot(app)
  assert "README.md" in {item["path"] for item in files}


def test_snapshot_publishes_a_secrets_manager_ui_and_credential_setup_docs(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  sources = {
    "secrets/SecretsPanel.jsx": (
      b"export function SecretsPanel() {\n"
      b"  return <input type='password' placeholder='sk_test_...' />;\n}\n"
    ),
    "secrets/vault.js": b"export const fields = ['api_key', 'password'];",
    "credentials/README.md": (
      b"Paste a webhook such as https://hooks.slack.com/services/"
      b"T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX and a bot token "
      b"like 123456789:YOUR_BOT_TOKEN.\n"
    ),
  }
  _commit_files(repo, app, sources)

  _, files = build_public_snapshot(app)

  assert set(sources) <= {item["path"] for item in files}


def test_snapshot_applies_the_same_icon_rule_as_apply(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  manifest = json.loads((repo / "mobius.json").read_text())
  manifest["icon"] = "icon.png"
  _commit_files(repo, app, {
    "mobius.json": json.dumps(manifest).encode(),
    "icon.png": b"not an image",
  })

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "invalid_icon"
  assert "icon.png" in raised.value.detail


@pytest.mark.parametrize("credential", [
  "-----BEGIN ENCRYPTED PRIVATE KEY-----",
  "-----BEGIN DSA PRIVATE KEY-----",
  "-----BEGIN PGP PRIVATE KEY BLOCK-----",
  "AIza" + "A" * 35,
  "sk-ant-" + "A" * 20,
  "sk-proj-" + "A" * 20,
  "rk_live_" + "A" * 20,
  "sk_live_" + "A" * 20,
  "sk_test_" + "A" * 20,
  "rk_test_" + "A" * 20,
  "sk-" + "A" * 20 + "T3BlbkFJ" + "A" * 20,
  "sk-svcacct-" + "A" * 40 + "T3BlbkFJ" + "A" * 40,
  "xoxb-1234567890-1234567890-" + "A" * 24,
  "https://hooks.slack.com/services/T0123ABCD/B0123ABCD/" + "a1B2" * 6,
  "123456789:AA" + "a1B2_-c" * 4 + "a1B2_",
])
@pytest.mark.parametrize("folder", ["", "src/", "secrets/"])
def test_snapshot_refuses_additional_recognizable_credential_patterns(
  tmp_path, credential, folder,
):
  repo, app, _ = _app_repo(tmp_path)
  _commit_files(repo, app, {f"{folder}config.js": f"const key = '{credential}';".encode()})

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "secret_detected"
  assert credential not in raised.value.detail


def test_snapshot_refuses_declared_icon_missing_from_snapshot(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  manifest = json.loads((repo / "mobius.json").read_text())
  manifest["icon"] = "missing.png"
  _commit_files(repo, app, {"mobius.json": json.dumps(manifest).encode()})

  with pytest.raises(CommunityPublicationError) as raised:
    build_public_snapshot(app)

  assert raised.value.code == "invalid_icon"
  assert "missing.png" in raised.value.detail


def test_public_certificates_publish_and_private_keys_in_pem_are_refused(tmp_path):
  repo, app, _ = _app_repo(tmp_path)
  _commit_files(repo, app, {"certs/cacert.pem": b"-----BEGIN CERTIFICATE-----\nMIIB\n"})
  build_public_snapshot(app)

  (tmp_path / "second").mkdir()
  repo, app, _ = _app_repo(tmp_path / "second")
  _commit_files(repo, app, {"certs/server.pem": b"-----BEGIN PRIVATE KEY-----\nMIIE"})
  with pytest.raises(CommunityPublicationError):
    build_public_snapshot(app)


def test_openai_marker_scan_stays_linear_on_long_runs(tmp_path):
  import time
  from app import community_publish
  text = "sk-" * 200_000
  started = time.monotonic()
  assert not any(pattern.search(text) for pattern in community_publish._SECRET_PATTERNS)
  # The unbounded prefix took about a minute here; the bounded one is instant.
  assert time.monotonic() - started < 5
