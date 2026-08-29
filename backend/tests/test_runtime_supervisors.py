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
async def test_stalled_delegation_wake_cannot_block_off_loop_lease_recovery(
  monkeypatch,
):
  import app.broadcast as broadcast_module
  import app.chat as chat_module
  import app.contribution_autopilot as autopilot_module
  import app.delegations as delegations_module
  import app.runtime_supervisors as supervisors_module

  broadcast = SystemBroadcast()
  recovered = asyncio.Event()
  wake_started = asyncio.Event()
  block_wake = asyncio.Event()
  main_thread = threading.get_ident()
  loop = asyncio.get_running_loop()

  class TrackingSession(_EmptySession):
    def __init__(self):
      self.created_on = threading.get_ident()

    def __enter__(self):
      return self

  def session_factory():
    return TrackingSession()

  async def no_chats(*_args, **_kwargs):
    return chat_module.ContinuationSweepResult()

  async def wake_parents(**_kwargs):
    wake_started.set()
    await block_wake.wait()

  def sweep(db):
    assert db.created_on == threading.get_ident()
    assert db.created_on != main_thread
    loop.call_soon_threadsafe(recovered.set)
    return 0

  monkeypatch.setattr(broadcast_module, "get_system_broadcast", lambda: broadcast)
  monkeypatch.setattr(supervisors_module, "SessionLocal", session_factory)
  monkeypatch.setattr(
    supervisors_module, "DELEGATION_WAKE_RECOVERY_INTERVAL_SECS", 0,
  )
  monkeypatch.setattr(
    supervisors_module, "AUTOPILOT_LEASE_RECOVERY_INTERVAL_SECS", 0,
  )
  monkeypatch.setattr(chat_module, "sweep_reset_parks", no_chats)
  monkeypatch.setattr(
    delegations_module, "wake_parents_for_completed_delegations", wake_parents,
  )
  monkeypatch.setattr(autopilot_module, "sweep_expired_leases", sweep)

  supervisors = _supervisors()
  await supervisors._start_chat_supervisors()
  await asyncio.wait_for(wake_started.wait(), timeout=1)
  await asyncio.wait_for(recovered.wait(), timeout=1)

  assert not block_wake.is_set()
  assert "delegation-wake-recovery" in supervisors._tasks
  assert "autopilot-lease-recovery" in supervisors._tasks
  await supervisors.stop()
  assert supervisors._tasks == {}
