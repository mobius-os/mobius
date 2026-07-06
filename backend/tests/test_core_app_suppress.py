"""Unit tests for the durable core-app suppression marker."""

from pathlib import Path

from app import core_app_suppress


def _marker(data_dir, slug):
  return Path(data_dir) / "shared" / "suppressed-core-apps" / slug


def test_memory_and_reflection_suppressible_store_and_ordinary_not():
  assert core_app_suppress.is_suppressible_core_slug("memory")
  # Reflection is suppressible (owner call): uninstalling it also stops its
  # nightly run — an accepted trade-off (see the module docstring).
  assert core_app_suppress.is_suppressible_core_slug("reflection")
  # The App Store is the app-manager — never durably suppressible.
  assert not core_app_suppress.is_suppressible_core_slug("store")
  assert not core_app_suppress.is_suppressible_core_slug("notes")
  assert not core_app_suppress.is_suppressible_core_slug("")
  assert not core_app_suppress.is_suppressible_core_slug(None)


def test_mark_creates_marker_for_core_slug(tmp_path):
  core_app_suppress.mark_suppressed(tmp_path, "memory", app_id=42)
  assert _marker(tmp_path, "memory").exists()
  assert core_app_suppress.is_suppressed(tmp_path, "memory")
  body = _marker(tmp_path, "memory").read_text()
  assert "app_id: 42" in body  # observability


def test_mark_is_noop_for_ordinary_app(tmp_path):
  core_app_suppress.mark_suppressed(tmp_path, "notes", app_id=7)
  assert not _marker(tmp_path, "notes").exists()
  assert not core_app_suppress.is_suppressed(tmp_path, "notes")


def test_mark_is_noop_for_store(tmp_path):
  # Deleting the store must NOT durably suppress it (that would strand re-add).
  core_app_suppress.mark_suppressed(tmp_path, "store", app_id=1)
  assert not _marker(tmp_path, "store").exists()


def test_clear_removes_marker(tmp_path):
  core_app_suppress.mark_suppressed(tmp_path, "memory")
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
  core_app_suppress.mark_suppressed(tmp_path, "memory")
  core_app_suppress.mark_suppressed(tmp_path, "reflection")
  # a non-suppressible slug is a no-op and never appears
  core_app_suppress.mark_suppressed(tmp_path, "store")
  assert core_app_suppress.list_suppressed(tmp_path) == {"memory", "reflection"}


def test_marker_path_matches_shell_check(tmp_path):
  # install-core-apps.sh checks `$DATA_DIR/shared/suppressed-core-apps/$slug`.
  # Keep the Python marker path in lockstep with that literal.
  core_app_suppress.mark_suppressed(tmp_path, "memory")
  assert (tmp_path / "shared" / "suppressed-core-apps" / "memory").is_file()
