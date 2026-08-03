import asyncio
import logging
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

  await supervisors.start()

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
  assert len(broadcast.subscribers) == 1

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
