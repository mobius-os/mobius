from pathlib import Path

from app.codex_rollouts import rollout_paths
from app import viewed_images

THREAD = "01a11cfa-03f1-72d1-9114-e7be8db73825"


def _write(path: Path) -> Path:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.touch()
  return path


def _live(home: Path, day: str = "08") -> Path:
  return _write(
    home / "sessions" / "2026" / "10" / day / f"rollout-2026-10-{day}T19-25-09-{THREAD}.jsonl"
  )


def _archived(home: Path) -> Path:
  return _write(home / "archived_sessions" / f"rollout-2026-10-01T08-00-00-{THREAD}.jsonl")


def test_a_home_with_glob_characters_is_a_literal_directory(tmp_path):
  home = tmp_path / "[prod]" / "codex"
  rollout = _live(home)
  # Unescaped, "[prod]" would match a single character and never this home.
  _live(tmp_path / "p" / "codex")

  assert rollout_paths(home, THREAD) == [rollout]
  assert viewed_images.rollout_path(home, THREAD) == rollout


def test_archived_rollouts_follow_live_ones_only_when_asked(tmp_path):
  archived = _archived(tmp_path)
  assert rollout_paths(tmp_path, THREAD) == []
  assert rollout_paths(tmp_path, THREAD, archived=True) == [archived]

  live = _live(tmp_path)
  assert rollout_paths(tmp_path, THREAD, archived=True) == [live, archived]


def test_an_image_binding_needs_exactly_one_live_rollout(tmp_path):
  _live(tmp_path, "08")
  _live(tmp_path, "09")
  assert len(rollout_paths(tmp_path, THREAD)) == 2
  assert viewed_images.rollout_path(tmp_path, THREAD) is None


def test_unsafe_thread_ids_match_nothing(tmp_path):
  _live(tmp_path)
  for thread_id in ("", "*", "?", "[0-9]", "../x", f"{THREAD}/..", None):
    assert rollout_paths(tmp_path, thread_id, archived=True) == []
