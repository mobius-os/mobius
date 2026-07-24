"""restart_this_worker — the reliable, drain-gated in-process restart behind the
Settings + platform "Restart" buttons (design §2.2). The normal path asks the
frozen supervisor to acknowledge the exact intent and terminate pid 1; a direct
SIGTERM is only the fail-closed handshake fallback. Every path arms a hard-kill
backstop and drains first. These tests pin those boundaries without killing the
test process (os.kill is mocked)."""

import asyncio
import os
import signal

from app import chat as chat_mod
from app import restart_ledger
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


def test_restart_drains_then_requests_supervisor_and_arms_force_kill(monkeypatch):
  _FakeTimer.instances = []
  calls = []
  drained = {"n": 0}
  monkeypatch.setattr(ru.os, "kill", lambda pid, sig: calls.append((pid, sig)))
  monkeypatch.setattr(ru.threading, "Timer", _FakeTimer)
  requests = []
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "new_nonce", lambda: "nonce-12345678")
  monkeypatch.setattr(
    restart_ledger, "request_restart",
    lambda **kwargs: requests.append(kwargs),
  )

  async def _fake_drain(timeout=0, *, restart_nonce=""):
    assert restart_nonce == "nonce-12345678"
    drained["n"] += 1
    return [{"chat_id": "chat-12345678", "run_token": "run-12345678"}]

  monkeypatch.setattr(chat_mod, "drain_all_for_restart", _fake_drain)
  # Start from a clean gate so the assertion below is meaningful.
  chat_mod.draining = False

  asyncio.run(ru.restart_this_worker())

  # The drain ran, and the gate was set so mid-restart sends queue.
  assert drained["n"] == 1
  assert chat_mod.draining is True
  # Only the frozen root-owned poller may acknowledge and terminate pid 1.
  assert calls == []
  assert requests == [{
    "boot_id": "boot-12345678",
    "nonce": "nonce-12345678",
    "runs": [{"chat_id": "chat-12345678", "run_token": "run-12345678"}],
  }]
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


def test_restart_request_survives_drain_failure(monkeypatch):
  _FakeTimer.instances = []
  calls = []
  monkeypatch.setattr(ru.os, "kill", lambda pid, sig: calls.append((pid, sig)))
  monkeypatch.setattr(ru.threading, "Timer", _FakeTimer)
  requests = []
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "new_nonce", lambda: "nonce-12345678")
  monkeypatch.setattr(
    restart_ledger, "request_restart",
    lambda **kwargs: requests.append(kwargs),
  )

  async def _boom(timeout=0, *, restart_nonce=""):
    del timeout, restart_nonce
    raise RuntimeError("drain exploded")

  monkeypatch.setattr(chat_mod, "drain_all_for_restart", _boom)

  asyncio.run(ru.restart_this_worker())

  assert calls == []
  assert requests == [{
    "boot_id": "boot-12345678",
    "nonce": "nonce-12345678",
    "runs": [],
  }]
  assert len(_FakeTimer.instances) == 1


def test_restart_handshake_failure_restarts_without_authorization(monkeypatch):
  _FakeTimer.instances = []
  calls = []
  monkeypatch.setattr(ru.os, "kill", lambda pid, sig: calls.append((pid, sig)))
  monkeypatch.setattr(ru.threading, "Timer", _FakeTimer)
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "new_nonce", lambda: "nonce-12345678")

  async def _fake_drain(timeout=0, *, restart_nonce=""):
    del timeout, restart_nonce
    return [{"chat_id": "chat-12345678", "run_token": "run-12345678"}]

  def _request_fails(**kwargs):
    del kwargs
    raise OSError("volume unavailable")

  monkeypatch.setattr(chat_mod, "drain_all_for_restart", _fake_drain)
  monkeypatch.setattr(restart_ledger, "request_restart", _request_fails)

  asyncio.run(ru.restart_this_worker())

  assert calls == [(os.getpid(), signal.SIGTERM)]
