"""One cross-process lease admits one Mobius-owned JavaScript build."""

import asyncio
import fcntl
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.build_admission import (
  BuildLeaseUnavailable,
  build_lease,
  build_lease_async,
)


_HOLD_THE_LEASE = textwrap.dedent("""
    import sys, time
    sys.path.insert(0, sys.argv[1])
    from app.build_admission import build_lease
    with build_lease():
      print("held", flush=True)
      time.sleep(30)
""")


def test_lease_excludes_a_build_running_in_another_process():
  holder = subprocess.Popen(
    [sys.executable, "-c", _HOLD_THE_LEASE,
     str(Path(__file__).resolve().parents[1])],
    stdout=subprocess.PIPE,
    text=True,
  )
  try:
    assert holder.stdout.readline().strip() == "held"
    with pytest.raises(BuildLeaseUnavailable):
      with build_lease(blocking=False):
        pytest.fail("a second build must not start while one is running")
  finally:
    holder.kill()
    holder.wait(timeout=10)

  # The kernel releases the lease with the holder's last descriptor.
  with build_lease(blocking=False):
    pass


def test_a_blocked_build_fails_instead_of_waiting_forever():
  with build_lease():
    with pytest.raises(BuildLeaseUnavailable):
      with build_lease(timeout=0.05):
        pytest.fail("the wait budget must be bounded")


@pytest.mark.asyncio
async def test_awaiting_the_lease_does_not_stall_the_event_loop():
  entered = asyncio.Event()

  async def compile_when_admitted():
    async with build_lease_async(timeout=10):
      entered.set()

  with build_lease():
    task = asyncio.create_task(compile_when_admitted())
    await asyncio.sleep(0.3)  # Other loop work still runs while it waits.
    assert not entered.is_set()

  await asyncio.wait_for(task, timeout=10)


def test_no_reachable_runtime_directory_admits_every_build(
  tmp_path, monkeypatch,
):
  """A developer checkout has no runtime, so nothing to serialize against."""
  not_a_directory = tmp_path / "data"
  not_a_directory.write_text("", encoding="utf-8")
  monkeypatch.setattr(
    "app.config.get_settings",
    lambda: SimpleNamespace(data_dir=str(not_a_directory)),
  )

  with build_lease(blocking=False):
    with build_lease(blocking=False):
      pass


def test_frontend_node_entrypoint_uses_the_python_lease_without_nesting(
  tmp_path,
):
  """One outer lease covers a nested build script without self-deadlocking."""
  repo = Path(__file__).resolve().parents[2]
  helper = repo / "frontend" / "scripts" / "build-admission.mjs"
  child = tmp_path / "child.mjs"
  marker = tmp_path / "entered"
  child.write_text(
    "\n".join((
      "import fs from 'node:fs'",
      f"import {{ enterBuildAdmission }} from {helper.as_uri()!r}",
      "enterBuildAdmission()",
      f"fs.writeFileSync({str(marker)!r}, 'entered')",
    )),
    encoding="utf-8",
  )
  outer = tmp_path / "outer.mjs"
  outer.write_text(
    "\n".join((
      "import { spawnSync } from 'node:child_process'",
      f"import {{ enterBuildAdmission }} from {helper.as_uri()!r}",
      "enterBuildAdmission()",
      f"const result = spawnSync(process.execPath, [{str(child)!r}], "
      "{ stdio: 'inherit', env: process.env })",
      "if (result.error) throw result.error",
      "process.exitCode = result.status ?? 1",
    )),
    encoding="utf-8",
  )
  runtime = tmp_path / "runtime"
  lock_path = runtime / "run" / "build.lock"
  lock_path.parent.mkdir(parents=True)
  lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
  fcntl.flock(lock_fd, fcntl.LOCK_EX)
  env = {
    **os.environ,
    "DATA_DIR": str(runtime),
    "SECRET_KEY": "x" * 32,
  }
  proc = subprocess.Popen(["node", str(outer)], env=env)
  try:
    # If the Node entrypoint bypasses the Python lease, the marker appears.
    with pytest.raises(subprocess.TimeoutExpired):
      proc.wait(timeout=0.4)
    assert not marker.exists()
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    assert proc.wait(timeout=10) == 0
  finally:
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    if proc.poll() is None:
      proc.kill()
      proc.wait(timeout=10)

  assert marker.read_text(encoding="utf-8") == "entered"


def test_all_frontend_native_build_entrypoints_enter_admission():
  scripts = Path(__file__).resolve().parents[2] / "frontend" / "scripts"

  for name in ("safe-build.mjs", "build-runtime.mjs", "build-tts-worker.mjs"):
    source = (scripts / name).read_text(encoding="utf-8")
    assert "from './build-admission.mjs'" in source
    assert "enterBuildAdmission(" in source
