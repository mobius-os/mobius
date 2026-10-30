"""An image-only recovery is healthy, not a completed source update.

Containment is proved against the captured served commit; HEAD and the
historical upstream marker may both have advanced since that process started.
"""

import pytest

from app import platform_update as pu
from app.main import _served_platform_identity
from .test_platform_update import (
  _advance_origin, _git, _local_commit, _served_sha, clone_env,
)


def _running(platform, monkeypatch, image, served):
  monkeypatch.setattr(pu, "current_build_sha", lambda: image)
  pu.SERVING_SOURCE_FILE.write_text("platform\n")
  if served is not None:
    pu.SERVING_SHA_FILE.write_text(served + "\n")
  monkeypatch.setenv("MOBIUS_SERVING_SOURCE_FILE", str(pu.SERVING_SOURCE_FILE))
  monkeypatch.setenv("MOBIUS_SERVING_SHA_FILE", str(pu.SERVING_SHA_FILE))


def test_image_only_recovery_does_not_report_source_up_to_date_with_stale_fetch(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  old = _served_sha(platform)
  image = _advance_origin(origin, edits={"release.txt": "new image\n"})
  _git(platform, "fetch", "origin", image)  # exact image object, not tracking ref
  _running(platform, monkeypatch, image, old)

  status = pu.platform_status(platform)

  assert status["state"] == "available"
  assert status["available"] is True
  assert status["checked_target_sha"] == image
  assert status["image_build_applied"] is False
  assert status["unfinished_update"] is None  # no manufactured prepared record
  assert _served_sha(platform) == old  # read-only, no hidden source update


def test_new_image_without_local_git_object_still_owes_source_update(
  clone_env, monkeypatch,
):
  _origin, platform = clone_env
  _running(platform, monkeypatch, "a" * 40, _served_sha(platform))

  status = pu.platform_status(platform)

  assert status["state"] == "available"
  assert status["checked_target_sha"] == "a" * 40
  assert status["image_build_applied"] is False


@pytest.mark.parametrize("marker_newer", [False, True])
def test_version_proves_loaded_source_not_saved_marker_or_checkout(
  clone_env, monkeypatch, marker_newer,
):
  origin, platform = clone_env
  old = _served_sha(platform)
  image = _advance_origin(origin, edits={"release.txt": "image\n"})
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


def test_image_floor_does_not_replace_a_newer_fetched_release(
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


def test_review_targets_the_same_image_floor_as_status(clone_env, monkeypatch):
  origin, platform = clone_env
  old = _served_sha(platform)
  image = _advance_origin(origin, edits={"release.txt": "new image\n"})
  _git(platform, "fetch", "origin", image)
  _running(platform, monkeypatch, image, old)

  preview = pu.platform_update_preview(platform)

  assert preview["target_sha"] == pu.platform_status(platform)["checked_target_sha"] == image
  assert preview["available"] is True
  assert _served_sha(platform) == old


def test_missing_image_object_is_fetched_only_for_preview(clone_env, monkeypatch):
  origin, platform = clone_env
  old = _served_sha(platform)
  image = _advance_origin(origin, edits={"release.txt": "image\n"})
  _running(platform, monkeypatch, image, old)
  assert not pu._rev(platform, image)
  assert pu.platform_status(platform)["available"] is True
  assert not pu._rev(platform, image)  # status itself did not fetch

  preview = pu.platform_update_preview(platform)

  assert preview["target_sha"] == image
  assert pu._rev(platform, image) == image
  assert _served_sha(platform) == old


def test_unavailable_official_image_source_cannot_create_empty_success(
  clone_env, monkeypatch,
):
  _origin, platform = clone_env
  _running(platform, monkeypatch, "a" * 40, _served_sha(platform))

  with pytest.raises(pu.PlatformUpdateError, match="image_release_source_unavailable"):
    pu.platform_update_preview(platform)


def test_replayed_identity_alone_cannot_complete_an_unpaired_replacement(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  old = _served_sha(platform)
  image = _advance_origin(origin, edits={"release.txt": "new image\n"})
  _git(platform, "fetch", "origin")
  _running(platform, monkeypatch, image, old)
  monkeypatch.setattr(pu, "frozen_image_sha", lambda: image)
  record = {
    "state": "swapped", "requires_image": True, "target": image,
    "replayed": old, "replayed_in_boot": None,
  }

  assert pu._serves_replayed(record, platform) is True
  assert pu._serving_settled_update(record, platform) is False


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


def test_paired_replacement_accepts_a_local_merged_descendant(
  clone_env, monkeypatch,
):
  origin, platform = clone_env
  image = _advance_origin(origin, edits={"release.txt": "new image\n"})
  _git(platform, "fetch", "origin")
  _git(platform, "merge", "--ff-only", image)
  merged = _local_commit(platform, edits={"local.txt": "keep me\n"})
  _running(platform, monkeypatch, image, merged)
  monkeypatch.setattr(pu, "frozen_image_sha", lambda: image)
  record = {
    "state": "swapped", "requires_image": True, "target": image,
    "replayed": merged, "replayed_in_boot": None,
  }

  assert merged != image
  assert pu._serving_settled_update(record, platform) is True
  assert (platform / "local.txt").read_text() == "keep me\n"
