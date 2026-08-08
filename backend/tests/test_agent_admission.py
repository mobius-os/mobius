"""Contracts for the global agent-turn admission cap."""

import asyncio

import pytest

import app.agent_admission as adm


def _set_ceilings(main: int, child: int) -> None:
  adm._main_sem = asyncio.Semaphore(main)
  adm._child_sem = asyncio.Semaphore(child)


def test_main_bucket_bounds_concurrent_turns():
  async def _run():
    _set_ceilings(2, 1)
    active = 0
    peak = 0
    release = asyncio.Event()

    async def turn():
      nonlocal active, peak
      async with adm.turn_slot(delegated=False):
        active += 1
        peak = max(peak, active)
        await release.wait()
        active -= 1

    tasks = [asyncio.create_task(turn()) for _ in range(5)]
    await asyncio.sleep(0.05)  # let all five contend for the two slots
    assert active == 2  # only two admitted; the rest are parked on acquire
    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2

  try:
    asyncio.run(_run())
  finally:
    adm.reset_for_tests()


def test_child_bucket_is_separate_so_parent_blocking_on_child_cannot_deadlock():
  async def _run():
    # Main pool has a single slot. A parent holds it and then blocks in-turn on
    # a delegated child. The child draws from the SEPARATE child bucket, so it
    # can start even though main is saturated. One shared bucket would deadlock.
    _set_ceilings(1, 1)
    child_ran = asyncio.Event()

    async def child():
      async with adm.turn_slot(delegated=True):
        child_ran.set()

    async def parent():
      async with adm.turn_slot(delegated=False):
        await child()

    await asyncio.wait_for(parent(), timeout=2.0)
    assert child_ran.is_set()

  try:
    asyncio.run(_run())
  finally:
    adm.reset_for_tests()


def test_slot_is_released_on_exception():
  async def _run():
    _set_ceilings(1, 1)
    with pytest.raises(ValueError):
      async with adm.turn_slot(delegated=False):
        raise ValueError("boom")
    # The single slot must be free again — a fresh acquire returns at once.
    async with asyncio.timeout(1.0):
      async with adm.turn_slot(delegated=False):
        pass

  try:
    asyncio.run(_run())
  finally:
    adm.reset_for_tests()


def test_cancelling_a_parked_waiter_does_not_leak_a_slot():
  async def _run():
    _set_ceilings(1, 1)
    holder_in = asyncio.Event()
    holder_release = asyncio.Event()

    async def holder():
      async with adm.turn_slot(delegated=False):
        holder_in.set()
        await holder_release.wait()

    async def waiter():
      async with adm.turn_slot(delegated=False):
        pass

    h = asyncio.create_task(holder())
    await holder_in.wait()
    w = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)  # waiter parks on acquire (slot held by holder)
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
      await w
    # A cancelled acquire must not have taken (or leaked) the slot.
    holder_release.set()
    await h
    async with asyncio.timeout(1.0):
      async with adm.turn_slot(delegated=False):
        pass

  try:
    asyncio.run(_run())
  finally:
    adm.reset_for_tests()


def test_reset_rebuilds_from_settings(monkeypatch):
  async def _run():
    adm.reset_for_tests()

    class _Stub:
      max_concurrent_agent_turns = 1
      max_concurrent_delegated_turns = 1

    monkeypatch.setattr("app.config.get_settings", lambda: _Stub())
    # First acquire builds the semaphores from the stubbed ceiling (1).
    held = asyncio.Event()
    release = asyncio.Event()

    async def first():
      async with adm.turn_slot(delegated=False):
        held.set()
        await release.wait()

    t = asyncio.create_task(first())
    await held.wait()
    second_started = asyncio.Event()

    async def second():
      async with adm.turn_slot(delegated=False):
        second_started.set()

    s = asyncio.create_task(second())
    await asyncio.sleep(0.05)
    assert not second_started.is_set()  # ceiling of 1 blocks the second
    release.set()
    await asyncio.gather(t, s)

  try:
    asyncio.run(_run())
  finally:
    adm.reset_for_tests()
