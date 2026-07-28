"""Focused operations on the owner-visible ``/data`` safety-net repository.

This is distinct from :mod:`app.app_git`, which owns each mini-app's private
source history. Callers here preserve owner-authored shared data immediately
before a managed lifecycle operation replaces or removes it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


_REDIRECTING_GIT_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE")


def snapshot_path(
  data_dir: Path,
  relative_path: str,
  commit_message: str,
) -> tuple[bool, str]:
  """Make one path durable in ``data_dir``'s git history before mutation.

  A clean path is already durable and succeeds without a new commit. ``--only``
  prevents unrelated staged changes from entering the snapshot commit or being
  disturbed by it. The caller must leave the path untouched when ``ok`` is
  false.
  """
  env = {
    key: value
    for key, value in os.environ.items()
    if key not in _REDIRECTING_GIT_ENV
  }
  base = [
    "git", "-C", str(data_dir),
    "-c", "user.name=Mobius", "-c", "user.email=mobius@localhost",
  ]

  def run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
      [*base, *args],
      capture_output=True,
      text=True,
      timeout=30,
      env=env,
    )

  def failure_reason(proc: subprocess.CompletedProcess) -> str:
    lines = (proc.stderr or proc.stdout or "").strip().splitlines()
    return lines[0] if lines else f"git exited {proc.returncode}"

  status = run("status", "--porcelain", "--", relative_path)
  if status.returncode != 0:
    return False, failure_reason(status)
  if not status.stdout.strip():
    return True, "already committed"

  add = run("add", "--", relative_path)
  if add.returncode != 0:
    return False, failure_reason(add)

  commit = run(
    "commit",
    "--only",
    "-m",
    commit_message,
    "--",
    relative_path,
  )
  if commit.returncode != 0:
    return False, failure_reason(commit)
  return True, "committed"
