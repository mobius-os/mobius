"""One cross-process lease admits one Mobius-owned JavaScript build."""

import asyncio
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
