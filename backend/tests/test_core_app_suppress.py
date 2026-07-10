"""Compatibility tests for the retired core-app suppression marker."""

from pathlib import Path

from app import core_app_suppress


def _marker(data_dir, slug):
  return Path(data_dir) / "shared" / "suppressed-core-apps" / slug


def test_no_catalog_app_is_suppressible():
  for slug in ("memory", "reflection", "beat-machine", "store", "notes", "", None):
    assert not core_app_suppress.is_suppressible_core_slug(slug)


def test_mark_is_noop_for_historical_platform_app(tmp_path):
  core_app_suppress.mark_suppressed(tmp_path, "memory", app_id=42)
  assert not _marker(tmp_path, "memory").exists()
  assert not core_app_suppress.is_suppressed(tmp_path, "memory")


def test_mark_is_noop_for_ordinary_app(tmp_path):
  core_app_suppress.mark_suppressed(tmp_path, "notes", app_id=7)
  assert not _marker(tmp_path, "notes").exists()
  assert not core_app_suppress.is_suppressed(tmp_path, "notes")


def test_mark_is_noop_for_store(tmp_path):
  # Deleting the store must NOT durably suppress it (that would strand re-add).
  core_app_suppress.mark_suppressed(tmp_path, "store", app_id=1)
  assert not _marker(tmp_path, "store").exists()


def test_clear_removes_marker(tmp_path):
  _marker(tmp_path, "memory").parent.mkdir(parents=True)
  _marker(tmp_path, "memory").write_text("old marker", encoding="utf-8")
  assert core_app_suppress.is_suppressed(tmp_path, "memory")
  core_app_suppress.clear_suppressed(tmp_path, "memory")
  assert not core_app_suppress.is_suppressed(tmp_path, "memory")


def test_clear_is_noop_when_absent(tmp_path):
  # Must not raise for the common recover/install case (no marker present).
  core_app_suppress.clear_suppressed(tmp_path, "memory")
  core_app_suppress.clear_suppressed(tmp_path, "notes")
  core_app_suppress.clear_suppressed(tmp_path, "")


def test_list_suppressed(tmp_path):
  assert core_app_suppress.list_suppressed(tmp_path) == set()
  marker_dir = tmp_path / "shared" / "suppressed-core-apps"
  marker_dir.mkdir(parents=True)
  (marker_dir / "memory").write_text("old", encoding="utf-8")
  (marker_dir / "reflection").write_text("old", encoding="utf-8")
  assert core_app_suppress.list_suppressed(tmp_path) == {"memory", "reflection"}


def test_mark_no_longer_creates_shell_marker(tmp_path):
  core_app_suppress.mark_suppressed(tmp_path, "memory")
  assert not (tmp_path / "shared" / "suppressed-core-apps" / "memory").exists()
