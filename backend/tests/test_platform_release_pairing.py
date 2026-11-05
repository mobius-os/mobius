"""An image-only recovery is healthy, not a completed source update.

Containment is proved against the captured served commit, or HEAD once
everything since it activated live; the historical upstream marker never
counts. Only the clone's official refs name a release, never the image itself.
"""

import hashlib

import pytest

from app import platform_update as pu
from app.main import _served_platform_identity
from .test_platform_update import (
  _advance_origin, _git, _local_commit, _served_sha, clone_env,
)


def _running(platform, monkeypatch, image, served):
  monkeypatch.setattr(pu, "current_build_sha", lambda: image)
  # Every stamped image's lineage lists its own build commit, official or not.
  monkeypatch.setattr(pu, "frozen_image_sha", lambda: image)
  monkeypatch.setattr(pu, "image_lineage", lambda: frozenset({image}))
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  if served is not None:
    pu.SERVING_SHA_FILE.write_text(served + "\n")
  monkeypatch.setenv("MOBIUS_SERVING_SOURCE_FILE", str(pu.SERVING_SOURCE_FILE))
  monkeypatch.setenv("MOBIUS_SERVING_SHA_FILE", str(pu.SERVING_SHA_FILE))


def _origin_branch_commit(origin, branch, edits):
  """Push one commit to a non-main origin branch, as a PR or feature build."""
  work = origin.parent / "origin-work"
  _git(work, "pull", "-q", "origin", "main")
  _git(work, "checkout", "-q", "-b", branch)
  for rel, content in edits.items():
    (work / rel).write_text(content)
  _git(work, "add", "-A")
  _git(work, "commit", "-q", "-m", branch)
  _git(work, "push", "-q", "origin", branch)
  sha = _git(work, "rev-parse", "HEAD").stdout.strip()
  _git(work, "checkout", "-q", "main")
  return sha


def test_image_only_recovery_is_unconfirmed_until_a_check_fetches_its_release(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  old = _served_sha(platform)
  image = _advance_origin(origin, edits={"release.txt": "new image\n"})
  _running(platform, monkeypatch, image, old)

  status = pu.platform_status(platform)

  assert status["state"] == "source_unconfirmed"
  assert status["available"] is False
  assert status["image_build_applied"] is False
  assert not pu._rev(platform, image)  # status itself did not fetch
  assert status["unfinished_update"] is None  # no manufactured prepared record

  checked = pu.check_for_updates(platform)
  preview = pu.platform_update_preview(platform)

  assert checked["state"] == "available"
  assert checked["checked_target_sha"] == preview["target_sha"] == image
  assert _served_sha(platform) == old  # read-only, no hidden source update


@pytest.mark.parametrize("marker_newer", [False, True])
def test_version_proves_loaded_source_not_saved_marker_or_checkout(
  clone_env, monkeypatch, marker_newer,
):
  origin, platform = clone_env
  old = _served_sha(platform)
  image = _advance_origin(origin, edits={"backend/app/main.py": "VALUE = 42\n"})
  _git(platform, "fetch", "origin")
  _git(platform, "merge", "--ff-only", image)
  if marker_newer:
    _git(platform, "branch", "-f", "upstream", image)
  _running(platform, monkeypatch, image, old)

  identity = _served_platform_identity(str(platform.parent), image)

  assert identity["served_sha"] == old
  assert identity["image_build_applied"] is False


def test_local_merged_source_pairs_even_when_saved_marker_lags(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  old = _served_sha(platform)
  image = _advance_origin(origin, edits={"release.txt": "image\n"})
  _git(platform, "fetch", "origin")
  _git(platform, "merge", "--ff-only", image)
  merged = _local_commit(platform, edits={"local.txt": "keep me\n"})
  _running(platform, monkeypatch, image, merged)

  identity = _served_platform_identity(str(platform.parent), image)
  status = pu.platform_status(platform)

  assert merged != image
  assert pu.recorded_upstream_sha(platform) == old
  assert identity["image_build_applied"] is True
  assert status["image_build_applied"] is True
  assert status["state"] == "up_to_date"


def test_missing_served_identity_cannot_borrow_checkout_pairing(
  clone_env, monkeypatch,
):
  _origin, platform = clone_env
  image = _served_sha(platform)
  _running(platform, monkeypatch, image, None)

  identity = _served_platform_identity(str(platform.parent), image)

  assert identity["image_build_applied"] is False
  status = pu.platform_status(platform)
  assert status["state"] == "source_unconfirmed"
  assert status["available"] is False
  assert status["needs_restart"] is False  # no invented reload instruction


def test_source_apply_completes_pairing_and_preserves_local_edits(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  old = _served_sha(platform)
  _local_commit(platform, edits={"local.txt": "keep committed\n"})
  (platform / "working.txt").write_text("keep uncommitted\n")
  image = _advance_origin(origin, edits={"release.txt": "image\n"})
  _running(platform, monkeypatch, image, old)

  result = pu.reconcile_clone(platform)
  assert result.status == "updated"
  _running(platform, monkeypatch, image, _served_sha(platform))

  assert (platform / "local.txt").read_text() == "keep committed\n"
  assert (platform / "working.txt").read_text() == "keep uncommitted\n"
  assert "working.txt" in _git(platform, "status", "--porcelain").stdout
  status = pu.platform_status(platform)
  assert status["image_build_applied"] is True
  assert status["state"] == "up_to_date"


def test_staged_source_is_not_loaded_until_existing_backend_activation(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  old = _served_sha(platform)
  image = _advance_origin(origin, edits={"backend/app/main.py": "VALUE = 42\n"})
  _git(platform, "fetch", "origin")
  _git(platform, "merge", "--ff-only", image)
  _running(platform, monkeypatch, image, old)

  status = pu.platform_status(platform)

  assert status["image_build_applied"] is False
  assert status["state"] == "restart_needed"
  assert status["needs_restart"] is True


def test_managed_selection_keeps_its_exact_identity_without_claiming_pairing(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  selected = _served_sha(platform)
  image = _advance_origin(origin, edits={"release.txt": "new image\n"})
  _git(platform, "fetch", "origin")
  _running(platform, monkeypatch, image, selected)

  status = pu.platform_status(platform, target_sha=selected)

  assert status["checked_target_sha"] == selected  # never substitute a digest's SHA
  assert status["image_build_applied"] is False
  assert status["state"] == "source_unconfirmed"
  assert status["available"] is False
  assert _served_sha(platform) == selected


def test_older_official_image_does_not_replace_a_newer_fetched_release(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  image = _served_sha(platform)
  newer = _advance_origin(origin, edits={"release.txt": "newer\n"})
  _git(platform, "fetch", "origin")
  _running(platform, monkeypatch, image, image)

  status = pu.platform_status(platform)

  assert status["image_build_applied"] is True
  assert status["checked_target_sha"] == newer
  assert status["state"] == "available"


def test_older_official_image_outside_a_shallow_clone_is_not_the_target(
  clone_env, monkeypatch,
):
  """An image object the clone lacks is never newer just for being missing."""
  origin, platform = clone_env
  newer = _advance_origin(origin, edits={"release.txt": "newer\n"})
  _git(platform, "fetch", "origin")
  _running(platform, monkeypatch, "a" * 40, _served_sha(platform))

  status = pu.platform_status(platform)
  preview = pu.platform_update_preview(platform)

  assert status["checked_target_sha"] == preview["target_sha"] == newer
  assert status["available"] is preview["available"] is True


@pytest.mark.parametrize("fetched", [False, True])
def test_feature_branch_image_never_becomes_the_release(
  clone_env, monkeypatch, fetched,
):
  origin, platform = clone_env
  release = pu.recorded_upstream_sha(platform)
  image = _origin_branch_commit(origin, "feature", {"release.txt": "pr\n"})
  if fetched:
    _git(platform, "fetch", "origin", image)
  _running(platform, monkeypatch, image, _served_sha(platform))

  status = pu.platform_status(platform)
  preview = pu.platform_update_preview(platform)
  checked = pu.check_for_updates(platform)

  main = pu._rev(platform, "origin/main")
  assert status["checked_target_sha"] == preview["target_sha"] == main
  assert checked["checked_target_sha"] == main
  assert preview["available"] is False
  assert status["state"] == checked["state"] == "source_unconfirmed"
  assert pu.recorded_upstream_sha(platform) == release


def test_non_main_commit_in_the_checkout_never_becomes_the_upstream_release(
  clone_env, monkeypatch,
):
  _origin, platform = clone_env
  release = pu.recorded_upstream_sha(platform)
  local = _local_commit(platform, edits={"local.txt": "image built from me\n"})
  _running(platform, monkeypatch, local, local)

  status = pu.check_for_updates(platform)

  assert status["checked_target_sha"] == release
  assert status["state"] == "up_to_date"
  assert pu.recorded_upstream_sha(platform) == release
  assert pu._contained_official_source(platform) == release
  assert pu.applied_release_sha(platform) == release


def test_live_source_update_confirms_the_image_release_without_restart(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  old = _served_sha(platform)
  image = _advance_origin(origin, edits={"frontend/src/release.js": "x\n"})
  _running(platform, monkeypatch, image, old)

  assert pu.reconcile_clone(platform).status == "updated"
  status = pu.platform_status(platform)

  identity = _served_platform_identity(str(platform.parent), image)

  assert pu.SERVING_SHA_FILE.read_text().strip() == old  # no restart happened
  assert status["needs_restart"] is False
  assert status["image_build_applied"] is identity["image_build_applied"] is True
  assert status["state"] == "up_to_date"


@pytest.mark.parametrize("path", ["Dockerfile", "backend/runtime/probe.py"])
def test_image_provided_files_the_image_runs_do_not_hold_back_pairing(
  clone_env, monkeypatch, path,
):
  """Applying an image-only release's source live pairs it: the image already
  runs that release's image-provided files, so no restart is owed."""
  origin, platform = clone_env
  old = _served_sha(platform)
  content = "image provided\n"
  image = _advance_origin(origin, edits={path: content})
  _running(platform, monkeypatch, image, old)
  monkeypatch.setattr(pu, "_build_info", lambda: {
    "sha": image,
    "image_inputs": {path: hashlib.sha256(content.encode()).hexdigest()},
  })

  assert pu.reconcile_clone(platform).status == "updated"
  status = pu.platform_status(platform)
  identity = _served_platform_identity(str(platform.parent), image)

  assert pu.SERVING_SHA_FILE.read_text().strip() == old  # no restart happened
  assert status["image_build_applied"] is identity["image_build_applied"] is True
  assert status["state"] != "source_unconfirmed"


def test_unrelated_update_does_not_claim_to_bring_an_unofficial_image(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  image = _origin_branch_commit(origin, "feature", {"release.txt": "pr\n"})
  _git(platform, "fetch", "origin", image)
  newer = _advance_origin(origin, edits={"other.txt": "official\n"})
  _git(platform, "fetch", "origin")
  _running(platform, monkeypatch, image, _served_sha(platform))

  status = pu.platform_status(platform)

  assert status["checked_target_sha"] == newer
  assert status["available"] is True
  assert status["image_build_applied"] is False
  assert status["update_contains_image_release"] is False


def test_update_that_carries_the_image_release_says_so(clone_env, monkeypatch):
  origin, platform = clone_env
  image = _advance_origin(origin, edits={"release.txt": "image\n"})
  _git(platform, "fetch", "origin")
  _running(platform, monkeypatch, image, _served_sha(platform))

  status = pu.platform_status(platform)

  assert status["available"] is True
  assert status["update_contains_image_release"] is True
