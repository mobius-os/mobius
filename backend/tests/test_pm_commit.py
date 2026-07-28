"""The /data commit helper owns only the paths its caller declares."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess


SCRIPT = Path(__file__).parents[1] / "scripts" / "pm-commit"


def git(repo: Path, *args: str) -> str:
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    check=True,
    capture_output=True,
    text=True,
  ).stdout.strip()


def repo(tmp_path: Path) -> tuple[Path, str]:
  git(tmp_path, "init", "-q")
  git(tmp_path, "config", "user.name", "Test Owner")
  git(tmp_path, "config", "user.email", "owner@example.test")
  (tmp_path / "owned.txt").write_text("before\n")
  (tmp_path / "other.txt").write_text("before\n")
  git(tmp_path, "add", "owned.txt", "other.txt")
  git(tmp_path, "commit", "-qm", "initial")
  return tmp_path, git(tmp_path, "rev-parse", "HEAD")


def run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    [str(SCRIPT), *args],
    env={**os.environ, "PM_COMMIT_ROOT": str(repo)},
    capture_output=True,
    text=True,
  )


def test_scoped_commit_preserves_unrelated_staged_and_unstaged_work(tmp_path):
  work, start = repo(tmp_path)
  (work / "owned.txt").write_text("after\n")
  (work / "new.txt").write_text("new\n")
  (work / "other.txt").write_text("someone else\n")
  git(work, "add", "other.txt")

  result = run(work, "--from", start, "own exact paths", "--", "owned.txt", "new.txt")

  assert result.returncode == 0, result.stderr
  assert set(git(work, "show", "--format=", "--name-only", "HEAD").splitlines()) == {
    "new.txt", "owned.txt",
  }
  assert git(work, "diff", "--cached", "--name-only") == "other.txt"
  assert git(work, "show", "HEAD:other.txt") == "before"


def test_scoped_commit_stops_when_same_path_changed_since_task_start(tmp_path):
  work, start = repo(tmp_path)
  (work / "owned.txt").write_text("concurrent\n")
  git(work, "commit", "-qam", "concurrent owner")
  (work / "owned.txt").write_text("my edit\n")

  result = run(work, "--from", start, "must not overwrite", "--", "owned.txt")

  assert result.returncode == 3
  assert "changed since task start" in result.stderr
  assert git(work, "log", "-1", "--pretty=%s") == "concurrent owner"


def test_scoped_commit_requires_start_and_paths(tmp_path):
  work, _ = repo(tmp_path)
  result = run(work, "old sweeping form")
  assert result.returncode == 2
  assert "Usage:" in result.stderr


def test_broad_snapshot_mode_does_not_exist(tmp_path):
  work, _ = repo(tmp_path)
  (work / "owned.txt").write_text("after\n")

  result = run(work, "--all", "snapshot")

  assert result.returncode == 2
  assert git(work, "status", "--short") == "M owned.txt"
