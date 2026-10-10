"""Complete package checks stay byte-exact without a second retained tree."""
import hashlib
import io
import json
import subprocess

import pytest
from PIL import Image

from app import app_git, install


def _commit(repo, tree):
  for path, content in tree.items():
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
  subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
  subprocess.run(
    ["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
     "user.email=fixture@example.invalid", "commit", "-qm", "fixture"],
    check=True,
  )
  return app_git.head_sha(repo, "HEAD")


def _icon(color):
  output = io.BytesIO()
  Image.new("RGB", (32, 32), color).save(output, format="PNG")
  return output.getvalue()


def _tree():
  manifest = {
    "id": "stream-package", "name": "Stream Package", "version": "1.0.0",
    "description": "Public synthetic fixture", "entry": "index.jsx",
    "source_files": ["cards.js"], "icon": "icon.png",
    "static_assets": {"assets/large.bin": "large.bin"},
    "storage_seeds": {"state.json": "seed.json", "inline.json": {"value": 1}},
    "schedule": {"job": "fetch.sh"},
    "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  }
  return {
    "mobius.json": json.dumps(manifest).encode(),
    "index.jsx": b"export default () => <div>fixture</div>;",
    "cards.js": b"export const cards = ['one'];",
    "icon.png": _icon("red"),
    "large.bin": b"\x00\xff\x80fixture\n" * 24000,
    "seed.json": b'{"value": 2}',
    "fetch.sh": b"#!/bin/sh\necho fixture\n",
    "unrelated.txt": b"not an install input",
  }


@pytest.fixture
def repo(tmp_path):
  subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
  return tmp_path


def test_streamed_package_matches_complete_binary_digest(repo):
  tree = _tree()
  commit = _commit(repo, tree)
  expected = install.package_content_digest_from_tree(tree)
  assert install.package_content_digest_from_git(repo, commit) == expected
  assert app_git.read_ref_tree(repo, commit) == tree


@pytest.mark.parametrize("field", ["manifest", "capability", "entry", "source", "icon", "static", "seed", "inline", "job"])
def test_every_declared_input_still_changes_the_exact_digest(repo, field):
  tree = _tree()
  before = install.package_content_digest_from_tree(tree)
  if field in ("manifest", "capability", "inline"):
    manifest = json.loads(tree["mobius.json"])
    if field == "manifest":
      manifest["name"] = "Changed"
    elif field == "capability":
      manifest["permissions"]["manage_apps"] = True
    else:
      manifest["storage_seeds"]["inline.json"] = {"value": 3}
    tree["mobius.json"] = json.dumps(manifest).encode()
  elif field == "icon":
    tree["icon.png"] = _icon("blue")
  else:
    path = {"entry": "index.jsx", "source": "cards.js", "static": "large.bin", "seed": "seed.json", "job": "fetch.sh"}[field]
    tree[path] += b"\nchanged\n"
  commit = _commit(repo, tree)
  after = install.package_content_digest_from_tree(tree)
  assert after != before
  assert install.package_content_digest_from_git(repo, commit) == after


@pytest.mark.parametrize("missing", ["index.jsx", "cards.js", "large.bin", "seed.json", "fetch.sh", "icon.png"])
def test_missing_declared_files_fail_closed_in_both_paths(repo, missing):
  tree = _tree()
  tree.pop(missing)
  commit = _commit(repo, tree)
  with pytest.raises(install.PackageContentError):
    install.package_content_digest_from_tree(tree)
  with pytest.raises(install.PackageContentError):
    install.package_content_digest_from_git(repo, commit)


@pytest.mark.parametrize("body", [b"broken json", b'{"id":"invalid"}'])
def test_invalid_manifest_is_not_treated_as_a_legacy_baseline(repo, body):
  tree = _tree()
  tree["mobius.json"] = body
  commit = _commit(repo, tree)
  with pytest.raises(install.PackageContentError):
    install.package_content_digest_from_git(repo, commit)


def test_only_manifest_absence_selects_real_legacy_comparison(repo):
  tree = _tree()
  tree.pop("mobius.json")
  commit = _commit(repo, tree)
  assert install.package_content_digest_from_git(repo, commit) is None
  assert app_git.read_ref_tree(repo, commit) == tree


def test_hashing_never_materializes_opaque_package_bodies(repo, monkeypatch):
  tree = _tree()
  commit = _commit(repo, tree)
  original = app_git.GitTreeBlob.read_bytes
  parsed_sizes = []
  def parsed(blob):
    parsed_sizes.append(blob.size)
    assert blob.size < 4096, "Opaque asset body must stream, not materialize"
    return original(blob)
  monkeypatch.setattr(app_git.GitTreeBlob, "read_bytes", parsed)
  assert install.package_content_digest_from_git(repo, commit) == install.package_content_digest_from_tree(tree)
  assert len(parsed_sizes) == 3  # manifest, schedule syntax, normalized icon


def test_blob_chunks_are_bounded_binary_faithful_and_scope_owned(repo):
  tree = {"empty": b"", "name with\nnewline.bin": b"\x00\xff\x80" * 100000}
  commit = _commit(repo, tree)
  with app_git.open_ref_tree(repo, commit) as opened:
    blob = opened["name with\nnewline.bin"]
    digest = hashlib.sha256()
    count = 0
    for chunk in blob.chunks():
      assert 0 < len(chunk) <= 65536
      digest.update(chunk)
      count += len(chunk)
    assert count == len(tree["name with\nnewline.bin"])
    assert digest.digest() == hashlib.sha256(tree["name with\nnewline.bin"]).digest()
    assert list(opened["empty"].chunks()) == []
    assert opened["empty"].read_bytes() == b""
  with pytest.raises(ValueError):
    blob.read_bytes()


def test_exception_during_digest_closes_the_shared_spool(repo, monkeypatch):
  commit = _commit(repo, _tree())
  seen = []
  original = app_git.GitTreeBlob.chunks
  def broken(blob):
    seen.append(blob._stream)
    raise RuntimeError("synthetic consumer failure")
    yield from original(blob)
  monkeypatch.setattr(app_git.GitTreeBlob, "chunks", broken)
  with pytest.raises(RuntimeError, match="synthetic consumer failure"):
    install.package_content_digest_from_git(repo, commit)
  assert seen and all(stream.closed for stream in seen)


def test_immutable_commit_not_later_working_tree_bytes_is_compared(repo):
  tree = _tree()
  commit = _commit(repo, tree)
  (repo / "cards.js").write_bytes(b"unaccepted local change")
  assert install.package_content_digest_from_git(repo, commit) == install.package_content_digest_from_tree(tree)


@pytest.mark.parametrize("unavailable", ["mobius.json", "large.bin"])
def test_unavailable_declared_blob_is_not_a_legacy_baseline(repo, unavailable):
  tree = _tree()
  commit = _commit(repo, tree)
  oid = subprocess.check_output(
    ["git", "-C", str(repo), "rev-parse", f"{commit}:{unavailable}"], text=True,
  ).strip()
  (repo / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
  # Full materialization keeps its established missing-object omission.
  assert unavailable not in app_git.read_ref_tree(repo, commit)
  with pytest.raises(install.PackageContentError):
    install.package_content_digest_from_git(repo, commit)


def test_unavailable_undeclared_blob_does_not_hide_a_valid_package(repo):
  tree = _tree()
  commit = _commit(repo, tree)
  oid = subprocess.check_output(
    ["git", "-C", str(repo), "rev-parse", f"{commit}:unrelated.txt"], text=True,
  ).strip()
  (repo / ".git" / "objects" / oid[:2] / oid[2:]).unlink()
  assert install.package_content_digest_from_git(repo, commit) == install.package_content_digest_from_tree(tree)


@pytest.mark.parametrize("framing", ["wrong-oid", "short-header", "non-blob", "negative-size", "truncated"])
def test_malformed_batch_framing_fails_closed_and_closes_spool(repo, monkeypatch, framing):
  commit = _commit(repo, {"file.bin": b"body"})
  original_run = subprocess.run
  spools = []

  def malformed(args, **kwargs):
    if args[-1] != "--batch":
      return original_run(args, **kwargs)
    oid = kwargs["input"].strip()
    spools.append(kwargs["stdout"])
    bodies = {
      "wrong-oid": b"0" * len(oid) + b" blob 4\nbody\n",
      "short-header": oid + b" blob\n",
      "non-blob": oid + b" tree 4\nbody\n",
      "negative-size": oid + b" blob -1\n",
      "truncated": oid + b" blob 4\nbo",
    }
    kwargs["stdout"].write(bodies[framing])
    return subprocess.CompletedProcess(args, 0)

  monkeypatch.setattr(app_git.subprocess, "run", malformed)
  with pytest.raises(RuntimeError):
    with app_git.open_ref_tree(repo, commit):
      pytest.fail("Malformed Git output must not expose a partial tree")
  assert spools and all(spool.closed for spool in spools)


def test_batch_process_failure_closes_spool(repo, monkeypatch):
  commit = _commit(repo, {"file.bin": b"body"})
  original_run = subprocess.run
  spools = []

  def failed(args, **kwargs):
    if args[-1] != "--batch":
      return original_run(args, **kwargs)
    spools.append(kwargs["stdout"])
    raise subprocess.CalledProcessError(128, args)

  monkeypatch.setattr(app_git.subprocess, "run", failed)
  with pytest.raises(subprocess.CalledProcessError):
    with app_git.open_ref_tree(repo, commit):
      pytest.fail("Failed Git output must not expose a partial tree")
  assert spools and all(spool.closed for spool in spools)


def test_gitlinks_are_not_materialized_as_files(repo):
  commit = _commit(repo, {"file.bin": b"body"})
  subprocess.run(
    ["git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
     f"160000,{commit},linked-submodule"], check=True,
  )
  subprocess.run(
    ["git", "-C", str(repo), "-c", "user.name=Fixture", "-c",
     "user.email=fixture@example.invalid", "commit", "-qm", "gitlink"],
    check=True,
  )
  commit = app_git.head_sha(repo, "HEAD")
  with app_git.open_ref_tree(repo, commit) as opened:
    assert list(opened) == ["file.bin"]
  assert app_git.read_ref_tree(repo, commit) == {"file.bin": b"body"}


def test_multiple_destinations_consume_shared_blob_bytes_in_full(repo):
  tree = _tree()
  manifest = json.loads(tree["mobius.json"])
  manifest["static_assets"]["assets/alias.bin"] = "large.bin"
  tree["mobius.json"] = json.dumps(manifest).encode()
  commit = _commit(repo, tree)
  assert install.package_content_digest_from_git(repo, commit) == install.package_content_digest_from_tree(tree)


def test_refused_icon_digests_like_install_instead_of_blocking_update_checks(repo):
  """Install skips an icon it refuses; the update-check digest must agree."""
  tree = _tree()
  tree["icon.png"] = b"not an image"
  commit = _commit(repo, tree)
  installed = install._read_git_candidate_inputs(tree, strict=True)
  assert installed.icon_processed is None
  assert installed.icon_warning
  digest = install.package_content_digest_from_tree(tree)
  assert digest == (json.loads(tree["mobius.json"])["id"], installed.content_digest())
  assert install.package_content_digest_from_git(repo, commit) == digest


@pytest.mark.parametrize("limit", ["manifest", "package"])
def test_installed_tree_over_admission_limits_digests_with_refused_icon_semantics(
  repo, monkeypatch, limit,
):
  tree = _tree()
  tree["icon.png"] = b"not an image"
  candidate = install._read_git_candidate_inputs(tree, strict=True)
  expected = (candidate.manifest["id"], candidate.content_digest())
  if limit == "manifest":
    tree["mobius.json"] += b" " * install._MANIFEST_MAX_BYTES
  else:
    monkeypatch.setattr(install, "_PACKAGE_MAX_BYTES", 1)
  commit = _commit(repo, tree)
  assert install.package_content_digest_from_tree(tree) == expected
  assert install.package_content_digest_from_git(repo, commit) == expected
  with pytest.raises(install.PackageTooLarge):
    install.read_git_install_candidate(repo, commit, "https://example.test/app/")
