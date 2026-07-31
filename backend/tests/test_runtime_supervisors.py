import asyncio
import logging
from types import SimpleNamespace

import pytest

from app.runtime_supervisors import RuntimeSupervisors


def _supervisors():
  async def lag_sleep(*_args, **_kwargs):
    await asyncio.sleep(3600)

  return RuntimeSupervisors(
    settings=SimpleNamespace(data_dir="/tmp"),
    logger=logging.getLogger("test.runtime-supervisors"),
    restart_authorization=None,
    restart_fallback_chats=[],
    lag_sleep=lag_sleep,
  )


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
