"""restart_this_worker — the reliable, drain-gated in-process restart behind the
Settings + platform "Restart" buttons (design §2.2). A bare SIGTERM hangs uvicorn
on the open chat SSE stream, so this must ALSO arm a hard-kill fallback; and it
must drain live turns first so a restart never simply kills a turn. These pin all
three halves (drain → SIGTERM → armed SIGKILL) without killing the test process
(os.kill is mocked) and without a real event loop turn (drain is stubbed)."""

import asyncio
import os
import signal

from app import chat as chat_mod
import app.restart_util as ru


class _FakeTimer:
  instances = []

  def __init__(self, interval, fn):
    self.interval = interval
    self.fn = fn
    self.daemon = None
    self.started = False
    _FakeTimer.instances.append(self)

  def start(self):
    self.started = True


def test_restart_drains_then_sigterms_and_arms_force_kill(monkeypatch):
  _FakeTimer.instances = []
  calls = []
  drained = {"n": 0}
  monkeypatch.setattr(ru.os, "kill", lambda pid, sig: calls.append((pid, sig)))
  monkeypatch.setattr(ru.threading, "Timer", _FakeTimer)

  async def _fake_drain(timeout=0):
    drained["n"] += 1
    return []

  monkeypatch.setattr(chat_mod, "drain_all_for_restart", _fake_drain)
  # Start from a clean gate so the assertion below is meaningful.
  chat_mod.draining = False

  asyncio.run(ru.restart_this_worker())

  # The drain ran, and the gate was set so mid-restart sends queue.
  assert drained["n"] == 1
  assert chat_mod.draining is True
  # The graceful ask: SIGTERM to our own worker, AFTER the drain.
  assert calls == [(os.getpid(), signal.SIGTERM)]
  # A single force-kill fallback, armed as a daemon and started, so a hung
  # graceful shutdown can't leave the container "Up" with a dead worker. Its
  # window covers the drain budget + the post-SIGTERM grace floor.
  assert len(_FakeTimer.instances) == 1
  timer = _FakeTimer.instances[0]
  assert timer.daemon is True
  assert timer.started is True
  assert timer.interval == chat_mod.DRAIN_TIMEOUT + ru._FORCE_KILL_AFTER_SECONDS

  # Firing the fallback hard-kills this worker so the container actually cycles.
  timer.fn()
  assert calls[-1] == (os.getpid(), signal.SIGKILL)


def test_restart_sigterms_even_when_drain_fails(monkeypatch):
  """A drain failure must never block the restart — SIGTERM + backstop still fire."""
  _FakeTimer.instances = []
  calls = []
  monkeypatch.setattr(ru.os, "kill", lambda pid, sig: calls.append((pid, sig)))
  monkeypatch.setattr(ru.threading, "Timer", _FakeTimer)

  async def _boom(timeout=0):
    raise RuntimeError("drain exploded")

  monkeypatch.setattr(chat_mod, "drain_all_for_restart", _boom)

  asyncio.run(ru.restart_this_worker())

  assert calls == [(os.getpid(), signal.SIGTERM)]
  assert len(_FakeTimer.instances) == 1
