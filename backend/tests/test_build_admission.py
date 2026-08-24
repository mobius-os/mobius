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
  VITE_BUILD_MIN_HEADROOM_BYTES,
  ViteBuildDeferred,
  build_lease,
  build_lease_async,
  require_vite_build_admission,
  vite_build_admitted,
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


def _memory(*, working_set: int, limit: int | None) -> dict:
  return {
    "available": True,
    "working_set_bytes": working_set,
    "limit_bytes": limit,
    "pressure": {
      "some": {"avg60": 0.0},
      "full": {"avg60": 0.0},
    },
  }


def test_vite_admission_needs_absolute_headroom_even_at_a_normal_ratio():
  one_gib = 1024 * 1024 * 1024
  just_below = _memory(
    working_set=one_gib - VITE_BUILD_MIN_HEADROOM_BYTES + 1,
    limit=one_gib,
  )

  assert vite_build_admitted(just_below) is False
  with pytest.raises(ViteBuildDeferred, match="511 MiB cgroup headroom"):
    require_vite_build_admission(just_below)

  at_reserve = _memory(
    working_set=one_gib - VITE_BUILD_MIN_HEADROOM_BYTES,
    limit=one_gib,
  )
  assert vite_build_admitted(at_reserve) is True
  require_vite_build_admission(at_reserve)


def test_vite_admission_fails_open_without_a_finite_cgroup_limit():
  unknown = _memory(working_set=128 * 1024 * 1024, limit=None)

  assert vite_build_admitted(unknown) is True
  require_vite_build_admission(unknown)


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


def test_refusal_names_psi_not_headroom_when_psi_trips():
  """The message must name the tripped condition. A fixed headroom template
  once produced "5260 MiB headroom; 512 MiB is required" during a PSI spike."""
  one_gib = 1024 * 1024 * 1024
  psi_spike = _memory(working_set=128 * 1024 * 1024, limit=one_gib)
  psi_spike["pressure"] = {"some": {"avg60": 2.0}, "full": {"avg60": 0.0}}

  assert vite_build_admitted(psi_spike) is False
  with pytest.raises(ViteBuildDeferred) as exc:
    require_vite_build_admission(psi_spike)
  message = str(exc.value)
  assert "PSI some avg60 2.0" in message
  assert "headroom" not in message


def test_refusal_names_footprint_ratio_when_ratio_trips():
  one_gib = 1024 * 1024 * 1024
  hot = _memory(working_set=int(one_gib * 0.8), limit=one_gib)

  with pytest.raises(ViteBuildDeferred) as exc:
    require_vite_build_admission(hot)
  assert "unreclaimable footprint is 80% of the limit" in str(exc.value)
