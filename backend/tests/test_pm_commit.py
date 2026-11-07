"""The /data commit helper owns only the paths its caller declares."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

from app.main import _install_pm_commit_launcher


SCRIPT = Path(__file__).parents[1] / "scripts" / "pm-commit"
PREFLIGHT_SCRIPT = (
  Path(__file__).parents[1] / "scripts" / "preflight-restart-source"
)


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


def test_backend_commit_requires_restart_startup_preflight(tmp_path):
  work, start = repo(tmp_path)
  app = work / "backend" / "app"
  scripts = work / "backend" / "scripts"
  app.mkdir(parents=True)
  scripts.mkdir(parents=True)
  changed = app / "changed.py"
  changed.write_text("VALUE = 1\n")
  preflight = scripts / "preflight-restart-source"
  preflight.write_text("#!/bin/sh\necho broken-source >&2\nexit 17\n")
  preflight.chmod(0o755)

  refused = run(
    work, "--from", start, "broken backend", "--", "backend/app/changed.py",
  )

  assert refused.returncode == 3
  assert "broken-source" in refused.stderr
  assert "startup preflight; commit refused" in refused.stderr
  assert git(work, "rev-parse", "HEAD") == start

  preflight.write_text("#!/bin/sh\nexit 0\n")
  accepted = run(
    work, "--from", start, "bootable backend", "--", "backend/app/changed.py",
  )
  assert accepted.returncode == 0, accepted.stderr
  assert git(work, "show", "HEAD:backend/app/changed.py") == "VALUE = 1"


def test_restart_preflight_resolves_its_checkout_outside_the_repo(tmp_path):
  result = subprocess.run(
    [str(PREFLIGHT_SCRIPT)], cwd=tmp_path,
    env={**os.environ, "MOBIUS_PLATFORM_DIR": ""},
    capture_output=True, text=True,
  )

  assert result.returncode == 0, result.stderr


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


def test_launcher_follows_live_helper_updates_without_reinstall(tmp_path):
  source = tmp_path / "platform" / "pm-commit"
  target = tmp_path / "data" / ".pm-commit"
  source.parent.mkdir()
  target.parent.mkdir()
  target.write_text("stale image copy\n")

  source.write_text("#!/bin/sh\nprintf 'first:%s\\n' \"$1\"\n")
  source.chmod(0o755)
  assert _install_pm_commit_launcher(source, target)
  assert subprocess.run(
    [str(target), "one argument"], check=True, capture_output=True, text=True,
  ).stdout == "first:one argument\n"

  source.write_text("#!/bin/sh\nprintf 'second:%s\\n' \"$1\"\n")
  assert subprocess.run(
    [str(target), "same launcher"], check=True, capture_output=True, text=True,
  ).stdout == "second:same launcher\n"
  assert not _install_pm_commit_launcher(source, target)


def test_launcher_preserves_seed_when_live_helper_is_missing(tmp_path):
  source = tmp_path / "platform" / "pm-commit"
  target = tmp_path / "data" / ".pm-commit"
  target.parent.mkdir()
  target.write_text("usable image copy\n")

  with pytest.raises(FileNotFoundError):
    _install_pm_commit_launcher(source, target)

  assert target.read_text() == "usable image copy\n"


def _root_shell(tmp_path):
  """Fake a root shell over a repository owned by "owner" (uid 1000).

  ``stat`` reports that owner even when the tests themselves run as root,
  where the temporary repository really is root-owned.
  """
  fake = tmp_path / "bin"
  fake.mkdir()
  # Root until the helper has switched users, then the owner.
  (fake / "id").write_text('#!/bin/sh\n[ -n "$AS_OWNER" ] && echo 1000 || echo 0\n')
  (fake / "stat").write_text(
    '#!/bin/sh\n'
    'case "$2" in %u) echo 1000 ;; %U) echo owner ;; *) exec /usr/bin/stat "$@" ;; esac\n'
  )
  (fake / "runuser").write_text(
    '#!/bin/sh\nprintf "%s\\n" "$@" > "$RUNUSER_LOG"\nshift 3\nexec env AS_OWNER=1 "$@"\n'
  )
  for name in ("id", "stat", "runuser"):
    (fake / name).chmod(0o755)
  return fake


@pytest.mark.parametrize("relative_root, relative_helper", [
  (False, False), (True, False), (False, True),
])
def test_a_root_shell_commits_as_the_repository_owner(
  tmp_path, relative_root, relative_helper,
):
  """A Recovery-style root shell is refused by Git ("dubious ownership") and
  would leave root-owned objects, so the helper re-runs as the owner, from the
  same helper even when root named it by a path relative to another folder."""
  work = tmp_path / "repo"
  work.mkdir()
  work, start = repo(work)
  fake = _root_shell(tmp_path)
  log = tmp_path / "runuser.log"
  (work / "owned.txt").write_text("repaired\n")
  if relative_helper:
    caller_dir, helper = SCRIPT.parents[1], "scripts/pm-commit"
  else:
    caller_dir, helper = tmp_path, str(SCRIPT)

  result = subprocess.run(
    [helper, "--from", start, "save a recovery repair", "--", "owned.txt"],
    cwd=str(caller_dir),
    env={
      **os.environ, "PM_COMMIT_ROOT": "repo" if relative_root else str(work),
      "RUNUSER_LOG": str(log), "PATH": f"{fake}:{os.environ['PATH']}",
    },
    capture_output=True,
    text=True,
  )

  assert result.returncode == 0, result.stderr
  assert log.read_text().splitlines()[:5] == [
    "-u", "owner", "--", str(SCRIPT), "--from",
  ]
  assert git(work, "log", "-1", "--format=%s") == "save a recovery repair"
