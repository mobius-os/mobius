import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from app.broadcast import SystemBroadcast
from app.runtime_supervisors import RuntimeSupervisors


def _supervisors():
  return RuntimeSupervisors(
    settings=SimpleNamespace(data_dir="/tmp"),
    logger=logging.getLogger("test.runtime-supervisors"),
    restart_authorization=None,
    restart_fallback_chats=[],
  )


class _EmptySession:
  def __enter__(self):
    return object()

  def __exit__(self, *_args):
    return False


@pytest.mark.asyncio
async def test_stop_cancels_and_observes_every_named_task():
  supervisors = _supervisors()
  stopped = []

  async def resident(name):
    try:
      await asyncio.Event().wait()
    finally:
      stopped.append(name)

  supervisors._spawn("first", resident("first"))
  supervisors._spawn("second", resident("second"))
  await asyncio.sleep(0)

  task_names = {task.get_name() for task in supervisors._tasks.values()}
  assert task_names == {"mobius:first", "mobius:second"}

  await supervisors.stop()

  assert set(stopped) == {"first", "second"}
  assert supervisors._tasks == {}


@pytest.mark.asyncio
async def test_start_fails_open_when_chat_supervisor_wiring_breaks(monkeypatch):
  supervisors = _supervisors()
  frontend_started = False

  async def frontend():
    nonlocal frontend_started
    frontend_started = True

  async def broken_chat_wiring():
    raise RuntimeError("broken wiring")

  monkeypatch.setattr(supervisors, "_start_frontend_watcher", frontend)
  monkeypatch.setattr(supervisors, "_start_chat_supervisors", broken_chat_wiring)

  await supervisors.start_process_services()
  await supervisors.start_database_services()

  assert frontend_started is True
  assert supervisors._tasks == {}


@pytest.mark.asyncio
async def test_reset_park_subscription_exists_only_while_its_task_runs(
  monkeypatch,
):
  import app.broadcast as broadcast_module
  import app.chat as chat_module
  import app.runtime_supervisors as supervisors_module

  broadcast = SystemBroadcast()

  async def no_chats(*_args, **_kwargs):
    return chat_module.ContinuationSweepResult()

  monkeypatch.setattr(broadcast_module, "get_system_broadcast", lambda: broadcast)
  monkeypatch.setattr(supervisors_module, "SessionLocal", _EmptySession)
  monkeypatch.setattr(chat_module, "sweep_reset_parks", no_chats)

  supervisors = _supervisors()
  await supervisors._start_chat_supervisors()

  # Task creation alone must not allocate a process-lifetime subscriber. An
  # immediate shutdown can cancel a coroutine before its first instruction.
  assert broadcast.subscribers == []

  await asyncio.sleep(0)
  # Reset-park recovery and exact scratch-release hints each own one bounded
  # subscription for the lifetime of their supervisor task.
  assert len(broadcast.subscribers) == 2

  await supervisors.stop()
  assert broadcast.subscribers == []


@pytest.mark.asyncio
async def test_restart_backlog_gets_prompt_followup_without_turn_completion(
  monkeypatch,
):
  import app.broadcast as broadcast_module
  import app.chat as chat_module
  import app.runtime_supervisors as supervisors_module

  broadcast = SystemBroadcast()

  sweep_authorizations = []

  async def paced_sweep(*_args, **_kwargs):
    sweep_authorizations.append(_kwargs.get("restart_authorization"))
    if len(sweep_authorizations) == 1:
      return chat_module.ContinuationSweepResult(
        ("first", "second"), restart_deferred=True,
      )
    return chat_module.ContinuationSweepResult(("third",))

  monkeypatch.setattr(broadcast_module, "get_system_broadcast", lambda: broadcast)
  monkeypatch.setattr(supervisors_module, "SessionLocal", _EmptySession)
  monkeypatch.setattr(supervisors_module, "RESTART_BACKLOG_DRAIN_INTERVAL_SECS", 0)
  monkeypatch.setattr(chat_module, "sweep_reset_parks", paced_sweep)

  supervisors = _supervisors()
  supervisors.restart_authorization = "accepted-restart"
  await supervisors._start_chat_supervisors()
  for _ in range(10):
    if len(sweep_authorizations) >= 2:
      break
    await asyncio.sleep(0)

  assert sweep_authorizations == ["accepted-restart", "accepted-restart"]
  await supervisors.stop()


@pytest.mark.asyncio
async def test_finished_run_releases_scratch_under_the_chat_start_lock(
  monkeypatch,
):
  import app.agent_scratch as scratch_module
  import app.broadcast as broadcast_module
  import app.chat as chat_module
  import app.chat_queue as chat_queue_module
  import app.runtime_supervisors as supervisors_module

  broadcast = SystemBroadcast()
  released = threading.Event()
  release_calls = []

  async def no_chats(*_args, **_kwargs):
    return chat_module.ContinuationSweepResult()

  def detach_if_idle(chat_id):
    release_calls.append(chat_id)
    released.set()
    return None

  monkeypatch.setattr(broadcast_module, "get_system_broadcast", lambda: broadcast)
  monkeypatch.setattr(supervisors_module, "SessionLocal", _EmptySession)
  monkeypatch.setattr(chat_module, "sweep_reset_parks", no_chats)
  monkeypatch.setattr(scratch_module, "_detach_if_idle", detach_if_idle)

  supervisors = _supervisors()
  await supervisors._start_chat_supervisors()
  await asyncio.sleep(0)

  lock = chat_queue_module.get_lock("chat-1")
  await lock.acquire()
  try:
    broadcast.publish({"type": "chat_scratch_releasable", "chatId": "chat-1"})
    await asyncio.sleep(0)
    assert release_calls == []
  finally:
    lock.release()

  for _ in range(100):
    if released.is_set():
      break
    await asyncio.sleep(0.01)
  assert released.is_set()
  assert release_calls == ["chat-1"]
  await supervisors.stop()


@pytest.mark.asyncio
async def test_malformed_scratch_release_event_is_ignored(
  monkeypatch,
):
  import app.agent_scratch as scratch_module
  import app.broadcast as broadcast_module
  import app.chat as chat_module
  import app.runtime_supervisors as supervisors_module

  broadcast = SystemBroadcast()
  release_calls = []

  async def no_chats(*_args, **_kwargs):
    return chat_module.ContinuationSweepResult()

  async def release_if_idle(chat_id):
    release_calls.append(chat_id)

  monkeypatch.setattr(broadcast_module, "get_system_broadcast", lambda: broadcast)
  monkeypatch.setattr(supervisors_module, "SessionLocal", _EmptySession)
  monkeypatch.setattr(chat_module, "sweep_reset_parks", no_chats)
  monkeypatch.setattr(
    scratch_module, "release_if_idle",
    release_if_idle,
  )

  supervisors = _supervisors()
  await supervisors._start_chat_supervisors()
  await asyncio.sleep(0)
  broadcast.publish({"type": "chat_scratch_releasable"})
  await asyncio.sleep(0)
  assert release_calls == []
  await supervisors.stop()


@pytest.mark.asyncio
async def test_gauntlet_reconciliation_has_periodic_backstop(monkeypatch):
  import app.broadcast as broadcast_module
  import app.chat as chat_module
  import app.gauntlets as gauntlets_module
  import app.runtime_supervisors as supervisors_module

  broadcast = SystemBroadcast()
  reconciled = asyncio.Event()
  calls = 0

  async def no_chats(*_args, **_kwargs):
    return chat_module.ContinuationSweepResult()

  async def reconcile():
    nonlocal calls
    calls += 1
    reconciled.set()
    # Prevent a zero-interval test loop from spinning after proving one tick.
    await asyncio.Event().wait()

  monkeypatch.setattr(broadcast_module, "get_system_broadcast", lambda: broadcast)
  monkeypatch.setattr(supervisors_module, "SessionLocal", _EmptySession)
  monkeypatch.setattr(chat_module, "sweep_reset_parks", no_chats)
  monkeypatch.setattr(
    supervisors_module, "GAUNTLET_RECONCILE_INTERVAL_SECS", 0,
  )
  monkeypatch.setattr(
    gauntlets_module, "reconcile_running_gauntlets", reconcile,
  )

  supervisors = _supervisors()
  await supervisors._start_chat_supervisors()
  await asyncio.wait_for(reconciled.wait(), timeout=1)

  assert calls == 1
  assert "gauntlet-reconcile" in supervisors._tasks
  await supervisors.stop()
  assert supervisors._tasks == {}
