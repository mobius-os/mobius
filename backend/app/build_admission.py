"""One cross-process lease for every Mobius-owned JavaScript build.

Several processes can start a native JS build on the same box: uvicorn's
mini-app Rolldown compile, the warm frontend watcher, ``rebuild_shell.sh``,
frontend package scripts, and the in-container app validator. Each can peak at
hundreds of megabytes of native memory, so two at once OOM a small instance.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

from app.resource_pressure import MIB, assess_memory_pressure


class BuildAdmissionUnavailable(RuntimeError):
  """A transient resource condition prevented a native JavaScript build."""


class BuildLeaseUnavailable(BuildAdmissionUnavailable):
  """No lease became free within the wait budget."""


class ViteBuildDeferred(BuildAdmissionUnavailable):
  """Vite lacks enough measured cgroup headroom to start safely."""


# One Vite build is capped at 180s, so a wait longer than that means a builder
# is stuck rather than merely busy. Failing then is better than hanging.
_LEASE_WAIT_SECS = 240.0
_LEASE_POLL_SECS = 0.1

# Exact 512 MiB cgroup canaries showed Vite drive memory.current to the hard
# limit and receive an OOM kill when admitted with less headroom, even while
# working-set ratio and PSI still read "normal". This is a starting reserve,
# not a claim that Vite itself retains 512 MiB: it also protects the serving
# process and concurrent user work while the native bundler peaks.
VITE_BUILD_MIN_HEADROOM_BYTES = 512 * MIB


def vite_build_admitted(memory: dict | None = None) -> bool:
  """Whether a new Vite process can start without a known cgroup OOM risk.

  Missing or unlimited cgroup telemetry stays fail-open for developer hosts.
  A finite cgroup must have both a healthy pressure state and the canary-backed
  absolute reserve; ratios alone hide dangerous low-capacity states.
  """
  assessment = assess_memory_pressure(memory)
  if assessment["state"] in {"constrained", "critical"}:
    return False
  headroom = assessment.get("headroom_bytes")
  return not isinstance(headroom, int) or (
    headroom >= VITE_BUILD_MIN_HEADROOM_BYTES
  )


def require_vite_build_admission(memory: dict | None = None) -> None:
  """Raise a retryable error instead of starting a known-unsafe Vite build."""
  assessment = assess_memory_pressure(memory)
  state = assessment["state"]
  headroom = assessment.get("headroom_bytes")
  too_little_headroom = (
    isinstance(headroom, int)
    and headroom < VITE_BUILD_MIN_HEADROOM_BYTES
  )
  if state not in {"constrained", "critical"} and not too_little_headroom:
    return
  # Say exactly which condition refused the build. A single headroom-shaped
  # template once produced "5260 MiB headroom; 512 MiB is required" while the
  # real trigger was a PSI spike — a self-contradiction that misdirects
  # whoever is debugging a stalled rebuild.
  causes: list[str] = []
  if state in {"constrained", "critical"}:
    signals = (assessment.get("reason") or {}).get("signals") or []
    named = "; ".join(signals) if signals else f"memory pressure is {state}"
    causes.append(f"memory pressure is {state} ({named})")
  if too_little_headroom:
    causes.append(
      f"only {headroom // MIB} MiB cgroup headroom;"
      f" {VITE_BUILD_MIN_HEADROOM_BYTES // MIB} MiB is required to start"
    )
  raise ViteBuildDeferred(
    "Vite build deferred: " + " and ".join(causes) +
    ". Retry after memory pressure falls."
  )


@contextlib.contextmanager
def build_lease(
  *,
  blocking: bool = True,
  timeout: float = _LEASE_WAIT_SECS,
  data_dir: str | Path | None = None,
  fail_open: bool = True,
):
  """Serialize every Mobius-owned JS build across processes."""
  try:
    if data_dir is None:
      # Imported here so the stdlib-only app validator keeps working in a
      # developer checkout that has neither settings nor pydantic installed.
      from app.config import get_settings

      data_dir = get_settings().data_dir
    path = Path(data_dir) / "run" / "build.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
  except Exception as exc:
    if not fail_open:
      raise BuildAdmissionUnavailable(
        f"cannot open the shared JavaScript build lease: {exc}"
      ) from exc
    yield  # No reachable runtime directory means no competing Mobius process.
    return
  deadline = time.monotonic() + timeout
  try:
    while True:
      try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
      except BlockingIOError:
        if not blocking or time.monotonic() >= deadline:
          raise BuildLeaseUnavailable("another JS build holds the lease")
        time.sleep(_LEASE_POLL_SECS)
    yield
  finally:
    os.close(fd)  # Closing releases the lock, including on process death.


@contextlib.asynccontextmanager
async def build_lease_async(*, timeout: float = _LEASE_WAIT_SECS):
  """Hold the same lease from the event loop, polling instead of stalling it."""
  deadline = time.monotonic() + timeout
  with contextlib.ExitStack() as stack:
    while True:
      try:
        stack.enter_context(build_lease(blocking=False))
        break
      except BuildLeaseUnavailable:
        if time.monotonic() >= deadline:
          raise
        await asyncio.sleep(_LEASE_POLL_SECS)
    yield


def _cli(argv: list[str] | None = None) -> int:
  """Run one frontend-owned command under the authoritative runtime lease."""
  parser = argparse.ArgumentParser()
  parser.add_argument(
    "--vite", action="store_true",
    help="also require the Vite cgroup headroom reserve",
  )
  parser.add_argument("command", nargs=argparse.REMAINDER)
  args = parser.parse_args(argv)
  command = args.command[1:] if args.command[:1] == ["--"] else args.command
  if not command:
    parser.error("a command is required after --")
  # A source checkout has a sibling backend too, but no running Mobius process
  # to contend with. Keep package tests/builds zero-configuration there. Every
  # real runtime has SECRET_KEY, and managed/container setups may also declare
  # DATA_DIR explicitly.
  runtime_configured = bool(
    os.environ.get("SECRET_KEY") or os.environ.get("DATA_DIR")
  )
  try:
    if runtime_configured:
      # DATA_DIR is sufficient to take the lock without importing pydantic,
      # keeping this CLI authoritative in a minimally provisioned runtime.
      with build_lease(
        data_dir=os.environ.get("DATA_DIR", "/data"),
        fail_open=False,
      ):
        if args.vite:
          require_vite_build_admission()
        result = subprocess.run(command, check=False)
    else:
      result = subprocess.run(command, check=False)
  except BuildAdmissionUnavailable as exc:
    print(str(exc), file=sys.stderr, flush=True)
    return os.EX_TEMPFAIL
  if result.returncode < 0:
    return 128 - result.returncode
  return result.returncode


if __name__ == "__main__":
  raise SystemExit(_cli())
