import json
import os

import pytest

from app.provider_session_retention import (
  ensure_claude_retention_default,
  sweep_stale_provider_sessions,
)
from app.codex_session_lock import (
  acquire_codex_session_activity,
  try_acquire_codex_session_sweep,
)


def _sessions(tmp_path):
  root = tmp_path / "cli-auth" / "codex" / "sessions"
  root.mkdir(parents=True)
  return root


def test_sweep_removes_only_stale_regular_rollouts(tmp_path):
  root = _sessions(tmp_path)
  stale = root / "2026" / "07" / "rollout-old.jsonl"
  stale.parent.mkdir(parents=True)
  stale.write_text("old")
  recent = root / "2026" / "08" / "rollout-recent.jsonl"
  recent.parent.mkdir(parents=True)
  recent.write_text("recent")
  now = 4_000_000.0
  os.utime(stale, (now - 15 * 86400, now - 15 * 86400))
  os.utime(recent, (now - 13 * 86400, now - 13 * 86400))

  result = sweep_stale_provider_sessions(tmp_path, now=now)

  assert not stale.exists()
  assert recent.read_text() == "recent"
  assert result["removed_files"] == 1
  assert result["reclaimed_bytes"] > 0
  assert not stale.parent.exists()


def test_sweep_ignores_non_rollout_files(tmp_path):
  root = _sessions(tmp_path)
  metadata = root / "session-index.json"
  metadata.write_text("keep")
  os.utime(metadata, (0, 0))

  result = sweep_stale_provider_sessions(
    tmp_path, now=4_000_000, max_age_days=0,
  )

  assert metadata.read_text() == "keep"
  assert result["scanned_files"] == 0


def test_sweep_never_follows_symlinks(tmp_path):
  root = _sessions(tmp_path)
  outside = tmp_path / "outside.jsonl"
  outside.write_text("keep")
  link = root / "linked.jsonl"
  link.symlink_to(outside)

  result = sweep_stale_provider_sessions(tmp_path, now=4_000_000, max_age_days=0)

  assert link.is_symlink()
  assert outside.read_text() == "keep"
  assert result["removed_files"] == 0


def test_sweep_is_bounded(tmp_path):
  root = _sessions(tmp_path)
  for index in range(3):
    path = root / f"rollout-old-{index}.jsonl"
    path.write_text("old")
    os.utime(path, (0, 0))

  result = sweep_stale_provider_sessions(
    tmp_path, now=4_000_000, max_age_days=0, max_files=1,
  )

  assert result["scanned_files"] == 1
  assert result["removed_files"] == 1
  assert result["truncated"] is True


def test_sweep_prioritizes_old_date_buckets(tmp_path):
  root = _sessions(tmp_path)
  recent = root / "2026" / "09" / "03" / "rollout-recent.jsonl"
  stale = root / "2026" / "01" / "01" / "rollout-stale.jsonl"
  recent.parent.mkdir(parents=True)
  stale.parent.mkdir(parents=True)
  recent.write_text("recent")
  stale.write_text("stale")
  now = 4_000_000.0
  os.utime(recent, (now, now))
  os.utime(stale, (0, 0))

  result = sweep_stale_provider_sessions(
    tmp_path, now=now, max_age_days=1, max_files=1,
  )

  assert not stale.exists()
  assert recent.exists()
  assert result["scanned_files"] == 1
  assert result["removed_files"] == 1


def test_sweep_skips_while_an_external_codex_owner_holds_shared_lock(tmp_path):
  root = _sessions(tmp_path)
  stale = root / "rollout-old.jsonl"
  stale.write_text("old")
  os.utime(stale, (0, 0))
  activity = acquire_codex_session_activity(tmp_path)
  try:
    result = sweep_stale_provider_sessions(
      tmp_path, now=4_000_000, max_age_days=0,
    )
  finally:
    activity.release()

  assert result["status"] == "skipped_active"
  assert stale.exists()


def test_exclusive_codex_sweep_lock_is_released(tmp_path):
  ownership = try_acquire_codex_session_sweep(tmp_path)
  assert ownership is not None
  ownership.release()

  next_ownership = try_acquire_codex_session_sweep(tmp_path)
  assert next_ownership is not None
  next_ownership.release()


def test_claude_default_is_added_without_replacing_other_settings(tmp_path):
  path = tmp_path / "cli-auth" / "claude" / "settings.json"
  path.parent.mkdir(parents=True)
  path.write_text(json.dumps({"theme": "dark"}))

  result = ensure_claude_retention_default(tmp_path)

  assert result == {
    "changed": True,
    "retention_days": 14,
    "source": "mobius_default",
  }
  assert json.loads(path.read_text()) == {
    "cleanupPeriodDays": 14,
    "theme": "dark",
  }


def test_claude_explicit_retention_is_preserved(tmp_path):
  path = tmp_path / "cli-auth" / "claude" / "settings.json"
  path.parent.mkdir(parents=True)
  path.write_text(json.dumps({"cleanupPeriodDays": 21}))

  result = ensure_claude_retention_default(tmp_path)

  assert result["changed"] is False
  assert result["retention_days"] == 21
  assert json.loads(path.read_text())["cleanupPeriodDays"] == 21


def test_claude_present_null_retention_is_not_silently_replaced(tmp_path):
  path = tmp_path / "cli-auth" / "claude" / "settings.json"
  path.parent.mkdir(parents=True)
  path.write_text(json.dumps({"cleanupPeriodDays": None}))

  result = ensure_claude_retention_default(tmp_path)

  assert result == {
    "changed": False,
    "retention_days": None,
    "source": "explicit",
  }
  assert json.loads(path.read_text())["cleanupPeriodDays"] is None


def test_claude_malformed_settings_are_never_replaced(tmp_path):
  path = tmp_path / "cli-auth" / "claude" / "settings.json"
  path.parent.mkdir(parents=True)
  path.write_text("not-json")

  with pytest.raises(json.JSONDecodeError):
    ensure_claude_retention_default(tmp_path)

  assert path.read_text() == "not-json"
