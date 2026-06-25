"""restart_this_worker — the reliable in-process restart for the Settings
restart buttons. A bare SIGTERM hangs uvicorn on the open chat SSE stream, so
this must ALSO arm a hard-kill fallback; these pin both halves without actually
killing the test process (os.kill is mocked)."""

import os
import signal

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


def test_restart_sigterms_self_and_arms_daemon_force_kill(monkeypatch):
  _FakeTimer.instances = []
  calls = []
  monkeypatch.setattr(ru.os, "kill", lambda pid, sig: calls.append((pid, sig)))
  monkeypatch.setattr(ru.threading, "Timer", _FakeTimer)

  ru.restart_this_worker()

  # The graceful ask: SIGTERM to our own worker.
  assert calls == [(os.getpid(), signal.SIGTERM)]
  # A single force-kill fallback, armed as a daemon and started, so a hung
  # graceful shutdown can't leave the container "Up" with a dead worker.
  assert len(_FakeTimer.instances) == 1
  timer = _FakeTimer.instances[0]
  assert timer.daemon is True
  assert timer.started is True
  assert timer.interval > 0

  # Firing the fallback hard-kills this worker so the container actually cycles.
  timer.fn()
  assert calls[-1] == (os.getpid(), signal.SIGKILL)
