from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "frontend-deps.sh"
WT_NPM_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "wt-npm.sh"


def test_wt_npm_is_executable():
  assert os.access(WT_NPM_SCRIPT, os.X_OK)


def _status(frontend: Path) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    [
      "bash",
      "-c",
      'source "$1"; mobius_frontend_deps_status "$2"',
      "bash",
      str(SCRIPT),
      str(frontend),
    ],
    env=os.environ,
    text=True,
    capture_output=True,
    check=False,
  )


def _write_project(
  frontend: Path,
  *,
  locked_version: str = "1.1.0",
  installed_version: str | None = "1.1.0",
) -> None:
  frontend.mkdir(parents=True, exist_ok=True)
  (frontend / "package.json").write_text(json.dumps({
    "name": "dependency-proof",
    "version": "1.0.0",
    "dependencies": {"fixture-package": "^1.0.0"},
  }))
  (frontend / "package-lock.json").write_text(json.dumps({
    "name": "dependency-proof",
    "version": "1.0.0",
    "lockfileVersion": 3,
    "requires": True,
    "packages": {
      "": {
        "name": "dependency-proof",
        "version": "1.0.0",
        "dependencies": {"fixture-package": "^1.0.0"},
      },
      "node_modules/fixture-package": {"version": locked_version},
    },
  }))
  modules = frontend / "node_modules"
  modules.mkdir()
  if installed_version is not None:
    package = modules / "fixture-package"
    package.mkdir()
    (package / "package.json").write_text(json.dumps({
      "name": "fixture-package",
      "version": installed_version,
    }))


def test_frontend_dependency_status_accepts_exact_lock_symlink(tmp_path: Path):
  source = tmp_path / "source"
  review = tmp_path / "review"
  _write_project(source)
  _write_project(review, installed_version=None)
  (review / "node_modules").rmdir()
  (review / "node_modules").symlink_to(source / "node_modules")

  result = _status(review)

  assert result.returncode == 0
  assert result.stdout.strip() == "ready"


def test_frontend_dependency_status_runs_npm_at_canonical_install_root(
  tmp_path: Path,
):
  source = tmp_path / "source"
  review = tmp_path / "review"
  _write_project(source)
  _write_project(review, installed_version=None)
  (review / "node_modules").rmdir()
  (review / "node_modules").symlink_to(source / "node_modules")
  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  observed = tmp_path / "npm-cwd"
  fake_npm = fake_bin / "npm"
  fake_npm.write_text(
    "#!/bin/sh\n"
    "pwd > \"$FRONTEND_DEPS_NPM_CWD\"\n"
    "exit 0\n"
  )
  fake_npm.chmod(0o755)

  result = subprocess.run(
    [
      "bash",
      "-c",
      'source "$1"; mobius_frontend_deps_status "$2"',
      "bash",
      str(SCRIPT),
      str(review),
    ],
    env={
      **os.environ,
      "PATH": f"{fake_bin}:{os.environ['PATH']}",
      "FRONTEND_DEPS_NPM_CWD": str(observed),
    },
    text=True,
    capture_output=True,
    check=False,
  )

  assert result.returncode == 0
  assert result.stdout.strip() == "ready"
  assert observed.read_text().strip() == str(source.resolve())


def test_frontend_dependency_status_rejects_different_lock_symlink(tmp_path: Path):
  source = tmp_path / "source"
  review = tmp_path / "review"
  _write_project(source, locked_version="1.2.6", installed_version="1.2.6")
  _write_project(review, locked_version="1.2.4", installed_version=None)
  (review / "node_modules").rmdir()
  (review / "node_modules").symlink_to(source / "node_modules")

  result = _status(review)

  assert result.returncode == 3
  assert result.stdout.strip() == "lock-mismatch"


def test_frontend_dependency_status_rejects_review_manifest_drift(
  tmp_path: Path,
):
  source = tmp_path / "source"
  review = tmp_path / "review"
  _write_project(source)
  _write_project(review, installed_version=None)
  (review / "package.json").write_text(json.dumps({
    "name": "dependency-proof",
    "version": "1.0.0",
    "dependencies": {
      "fixture-package": "^1.0.0",
      "unlocked-package": "^2.0.0",
    },
  }))
  (review / "node_modules").rmdir()
  (review / "node_modules").symlink_to(source / "node_modules")

  result = _status(review)

  assert result.returncode == 3
  assert result.stdout.strip() == "lock-mismatch"


@pytest.mark.parametrize(
  ("field", "source_value", "review_value"),
  [
    ("overrides", {"fixture-package": "1.1.0"}, {"fixture-package": "1.2.0"}),
    ("workspaces", ["packages/*"], ["packages/*", "tools/*"]),
  ],
)
def test_frontend_dependency_status_rejects_unlocked_install_field_drift(
  tmp_path: Path,
  field: str,
  source_value: object,
  review_value: object,
):
  source = tmp_path / "source"
  review = tmp_path / "review"
  _write_project(source)
  _write_project(review, installed_version=None)
  for frontend, value in ((source, source_value), (review, review_value)):
    manifest = json.loads((frontend / "package.json").read_text())
    manifest[field] = value
    (frontend / "package.json").write_text(json.dumps(manifest))
  (review / "node_modules").rmdir()
  (review / "node_modules").symlink_to(source / "node_modules")

  result = _status(review)

  assert result.returncode == 3
  assert result.stdout.strip() == "lock-mismatch"


def test_frontend_dependency_status_reports_missing_tree(tmp_path: Path):
  frontend = tmp_path / "frontend"
  frontend.mkdir()

  result = _status(frontend)

  assert result.returncode == 2
  assert result.stdout.strip() == "missing"


def test_frontend_dependency_status_reports_incomplete_tree(tmp_path: Path):
  frontend = tmp_path / "frontend"
  _write_project(frontend, installed_version=None)

  result = _status(frontend)

  assert result.returncode == 4
  assert result.stdout.strip() == "incomplete"


def test_frontend_dependency_status_rejects_semver_valid_installed_drift(
  tmp_path: Path,
):
  frontend = tmp_path / "frontend"
  _write_project(
    frontend,
    locked_version="1.1.0",
    installed_version="1.2.0",
  )

  result = _status(frontend)

  assert result.returncode == 3
  assert result.stdout.strip() == "lock-mismatch"


def _git(repo: Path, *args: str) -> None:
  subprocess.run(
    ["git", "-C", str(repo), *args],
    check=True,
    text=True,
    capture_output=True,
  )


def _worktree_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
  primary = tmp_path / "primary"
  review = tmp_path / "review"
  primary.mkdir()
  _git(primary, "init", "-b", "main")
  _git(primary, "config", "user.name", "Test")
  _git(primary, "config", "user.email", "test@example.com")
  _write_project(primary / "frontend")
  _git(primary, "add", "frontend/package.json", "frontend/package-lock.json")
  _git(primary, "commit", "-m", "fixture")
  _git(primary, "worktree", "add", "-b", "review", str(review), "main")

  fake_bin = tmp_path / "bin"
  fake_bin.mkdir()
  fake_node = fake_bin / "node"
  fake_node.write_text("#!/bin/sh\nprintf 'ready\\n'\nexit 0\n")
  fake_node.chmod(0o755)
  return primary, review, fake_bin


def test_wt_npm_does_not_truncate_lock_symlink_target(tmp_path: Path):
  _, review, fake_bin = _worktree_fixture(tmp_path)
  lock_target = tmp_path / "must-not-be-truncated"
  lock_target.write_text("durable data\n")
  lock_path = subprocess.run(
    [
      "git",
      "-C",
      str(review),
      "rev-parse",
      "--path-format=absolute",
      "--git-path",
      "mobius-wt-npm.lock",
    ],
    check=True,
    text=True,
    capture_output=True,
  ).stdout.strip()
  Path(lock_path).symlink_to(lock_target)
  fake_npm = fake_bin / "npm"
  fake_npm.write_text("#!/bin/sh\nexit 0\n")
  fake_npm.chmod(0o755)

  result = subprocess.run(
    ["bash", str(WT_NPM_SCRIPT), "test"],
    cwd=review,
    env={
      **os.environ,
      "PATH": f"{fake_bin}:{os.environ['PATH']}",
    },
    text=True,
    capture_output=True,
    check=False,
  )

  assert result.returncode == 0, result.stderr
  assert lock_target.read_text() == "durable data\n"


@pytest.mark.parametrize("npm_exit", [0, 7])
def test_wt_npm_borrows_exact_primary_dependencies_and_releases_them(
  tmp_path: Path,
  npm_exit: int,
):
  primary, review, fake_bin = _worktree_fixture(tmp_path)
  fake_npm = fake_bin / "npm"
  fake_npm.write_text(
    "#!/bin/sh\n"
    "test -L node_modules || exit 91\n"
    "readlink -f node_modules > \"$WT_NPM_OBSERVED\"\n"
    "exit \"${WT_NPM_EXIT:-0}\"\n"
  )
  fake_npm.chmod(0o755)
  observed = tmp_path / "observed"
  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
    "WT_NPM_OBSERVED": str(observed),
    "WT_NPM_EXIT": str(npm_exit),
  }

  result = subprocess.run(
    ["bash", str(WT_NPM_SCRIPT), "test"],
    cwd=review,
    env=env,
    text=True,
    capture_output=True,
    check=False,
  )

  assert result.returncode == npm_exit, result.stderr
  assert "borrowing exact-lock dependencies" in result.stderr
  assert observed.read_text().strip() == str(
    (primary / "frontend" / "node_modules").resolve()
  )
  assert not (review / "frontend" / "node_modules").exists()
  assert not list((review / "frontend").glob(".node_modules.borrow.*"))


def test_wt_npm_does_not_unlink_replacement_symlink(tmp_path: Path):
  primary, review, fake_bin = _worktree_fixture(tmp_path)
  fake_npm = fake_bin / "npm"
  fake_npm.write_text(
    "#!/bin/sh\n"
    "unlink node_modules\n"
    "ln -s \"$WT_NPM_SHARED_MODULES\" node_modules\n"
    "exit 7\n"
  )
  fake_npm.chmod(0o755)
  shared_modules = (primary / "frontend" / "node_modules").resolve()

  result = subprocess.run(
    ["bash", str(WT_NPM_SCRIPT), "test"],
    cwd=review,
    env={
      **os.environ,
      "PATH": f"{fake_bin}:{os.environ['PATH']}",
      "WT_NPM_SHARED_MODULES": str(shared_modules),
    },
    text=True,
    capture_output=True,
    check=False,
  )

  replacement = review / "frontend" / "node_modules"
  assert result.returncode == 7, result.stderr
  assert replacement.is_symlink()
  assert replacement.resolve() == shared_modules


def test_wt_npm_serializes_borrowers_in_one_worktree(tmp_path: Path):
  _, review, fake_bin = _worktree_fixture(tmp_path)
  fake_npm = fake_bin / "npm"
  fake_npm.write_text(
    "#!/bin/sh\n"
    "test -L node_modules || exit 91\n"
    "if [ \"$1\" = hold ]; then\n"
    "  : > \"$WT_NPM_STARTED\"\n"
    "  while [ ! -e \"$WT_NPM_RELEASE\" ]; do sleep 0.05; done\n"
    "else\n"
    "  : > \"$WT_NPM_PROBED\"\n"
    "fi\n"
    "test -L node_modules || exit 92\n"
    "exit 0\n"
  )
  fake_npm.chmod(0o755)
  started = tmp_path / "started"
  release = tmp_path / "release"
  probed = tmp_path / "probed"
  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
    "WT_NPM_STARTED": str(started),
    "WT_NPM_RELEASE": str(release),
    "WT_NPM_PROBED": str(probed),
  }

  first = subprocess.Popen(
    ["bash", str(WT_NPM_SCRIPT), "hold"],
    cwd=review,
    env=env,
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
  )
  deadline = time.monotonic() + 5
  while not started.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
  assert started.exists()
  second = subprocess.Popen(
    ["bash", str(WT_NPM_SCRIPT), "probe"],
    cwd=review,
    env=env,
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
  )
  time.sleep(0.2)
  assert not probed.exists()
  release.touch()
  _, first_stderr = first.communicate(timeout=5)
  _, second_stderr = second.communicate(timeout=5)

  assert first.returncode == 0, first_stderr
  assert second.returncode == 0, second_stderr
  assert probed.exists()
  assert not (review / "frontend" / "node_modules").exists()


def test_wt_npm_detached_child_does_not_retain_lock(tmp_path: Path):
  _, review, fake_bin = _worktree_fixture(tmp_path)
  fake_npm = fake_bin / "npm"
  fake_npm.write_text(
    "#!/bin/sh\n"
    "if [ \"$1\" = spawn ]; then\n"
    "  (sleep 3) </dev/null >/dev/null 2>&1 &\n"
    "fi\n"
    "exit 0\n"
  )
  fake_npm.chmod(0o755)
  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
  }

  first = subprocess.run(
    ["bash", str(WT_NPM_SCRIPT), "spawn"],
    cwd=review,
    env=env,
    text=True,
    capture_output=True,
    check=False,
  )
  second = subprocess.run(
    ["bash", str(WT_NPM_SCRIPT), "probe"],
    cwd=review,
    env=env,
    text=True,
    capture_output=True,
    check=False,
    timeout=1,
  )

  assert first.returncode == 0, first.stderr
  assert second.returncode == 0, second.stderr
  assert not (review / "frontend" / "node_modules").exists()


def test_wt_npm_releases_borrowed_tree_after_signal(tmp_path: Path):
  _, review, fake_bin = _worktree_fixture(tmp_path)
  fake_npm = fake_bin / "npm"
  fake_npm.write_text(
    "#!/bin/sh\n"
    ": > \"$WT_NPM_STARTED\"\n"
    "while :; do sleep 0.05; done\n"
  )
  fake_npm.chmod(0o755)
  started = tmp_path / "started"
  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
    "WT_NPM_STARTED": str(started),
  }
  process = subprocess.Popen(
    ["bash", str(WT_NPM_SCRIPT), "test"],
    cwd=review,
    env=env,
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    start_new_session=True,
  )
  deadline = time.monotonic() + 5
  while not started.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
  assert started.exists()

  os.killpg(process.pid, signal.SIGTERM)
  _, stderr = process.communicate(timeout=5)

  assert process.returncode == 143, stderr
  assert not (review / "frontend" / "node_modules").exists()


def test_wt_npm_pid_signal_waits_for_command_group_before_release(
  tmp_path: Path,
):
  _, review, fake_bin = _worktree_fixture(tmp_path)
  fake_npm = fake_bin / "npm"
  fake_npm.write_text(
    "#!/bin/sh\n"
    "trap ': > \"$WT_NPM_STOPPING\"; sleep 0.3; exit 143' TERM\n"
    "printf '%s\\n' \"$$\" > \"$WT_NPM_CHILD_PID\"\n"
    ": > \"$WT_NPM_STARTED\"\n"
    "while :; do sleep 0.05; done\n"
  )
  fake_npm.chmod(0o755)
  started = tmp_path / "started"
  stopping = tmp_path / "stopping"
  child_pid_path = tmp_path / "child-pid"
  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
    "WT_NPM_STARTED": str(started),
    "WT_NPM_STOPPING": str(stopping),
    "WT_NPM_CHILD_PID": str(child_pid_path),
  }
  process = subprocess.Popen(
    ["bash", str(WT_NPM_SCRIPT), "test"],
    cwd=review,
    env=env,
    text=True,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    start_new_session=True,
  )
  deadline = time.monotonic() + 5
  while not started.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
  assert started.exists()

  os.kill(process.pid, signal.SIGTERM)
  deadline = time.monotonic() + 5
  while not stopping.exists() and time.monotonic() < deadline:
    time.sleep(0.02)
  assert stopping.exists()
  assert process.poll() is None
  assert (review / "frontend" / "node_modules").is_symlink()
  _, stderr = process.communicate(timeout=5)

  assert process.returncode == 143, stderr
  assert not (review / "frontend" / "node_modules").exists()
  child_pid = int(child_pid_path.read_text())
  with pytest.raises(ProcessLookupError):
    os.kill(child_pid, 0)


def test_wt_npm_rejects_dependency_proof_drift(tmp_path: Path):
  primary, review, fake_bin = _worktree_fixture(tmp_path)
  fake_npm = fake_bin / "npm"
  fake_npm.write_text(
    "#!/bin/sh\n"
    "printf '\\n' >> \"$WT_NPM_PRIMARY_LOCK\"\n"
    "exit 0\n"
  )
  fake_npm.chmod(0o755)
  env = {
    **os.environ,
    "PATH": f"{fake_bin}:{os.environ['PATH']}",
    "WT_NPM_PRIMARY_LOCK": str(primary / "frontend" / "package-lock.json"),
  }

  result = subprocess.run(
    ["bash", str(WT_NPM_SCRIPT), "test"],
    cwd=review,
    env=env,
    text=True,
    capture_output=True,
    check=False,
  )

  assert result.returncode == 5, result.stderr
  assert "dependency proof changed" in result.stderr
  assert not (review / "frontend" / "node_modules").exists()


@pytest.mark.parametrize('matching_sibling', [True, False])
def test_wt_npm_reuses_only_exact_lock_registered_sibling(tmp_path, matching_sibling):
  primary, review, fake_bin = _worktree_fixture(tmp_path)
  sibling = tmp_path / 'sibling with spaces'
  _git(primary, 'worktree', 'add', '-b', 'sibling', str(sibling), 'main')
  _write_project(sibling / 'frontend', locked_version='1.2.0', installed_version='1.2.0')
  _write_project(review / 'frontend', locked_version='1.2.0' if matching_sibling else '1.3.0', installed_version=None)
  (review / 'frontend/node_modules').rmdir()
  observed = tmp_path / 'borrowed'
  fake_npm = fake_bin / 'npm'
  fake_npm.write_text('#!/bin/sh\nreadlink -f node_modules > "$WT_NPM_OBSERVED"\n')
  fake_npm.chmod(0o755)
  result = subprocess.run(
    ['bash', str(WT_NPM_SCRIPT), 'test'], cwd=review,
    env={**os.environ, 'PATH': f'{fake_bin}:{os.environ["PATH"]}', 'WT_NPM_OBSERVED': str(observed)},
    text=True, capture_output=True,
  )
  if matching_sibling:
    assert result.returncode == 0, result.stderr
    assert observed.read_text().strip() == str(sibling / 'frontend/node_modules')
  else:
    assert result.returncode == 2, result.stderr
    assert not observed.exists()
  assert not (review / 'frontend/node_modules').exists()
  assert (sibling / 'frontend/node_modules').is_dir()
