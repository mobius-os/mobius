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

  async def _fake_drain(
    timeout=0, *, restart_nonce="", prepared_runs=None,
  ):
    del timeout, prepared_runs
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


def test_restart_announces_server_restarting_on_the_system_bus(monkeypatch):
  """The drain-gated restart publishes `server_restarting` to the system bus
  before it drains — the ONLY cue that covers a graceful drain (health still
  answers, so client reachability alone shows nothing). The shell mirrors it
  into restartStore and lights its offline dot; see frontend restartStore.js."""
  from app.broadcast import get_system_broadcast

  _FakeTimer.instances = []
  monkeypatch.setattr(ru.os, "kill", lambda pid, sig: None)
  monkeypatch.setattr(ru.threading, "Timer", _FakeTimer)
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "new_nonce", lambda: "nonce-12345678")
  monkeypatch.setattr(restart_ledger, "request_restart", lambda **kwargs: None)

  async def _prepare(nonce):
    del nonce
    return []

  observed_before_drain = []

  async def _fake_drain(timeout=0, *, restart_nonce="", prepared_runs=None):
    del timeout, restart_nonce, prepared_runs
    observed_before_drain.append(q.get_nowait())
    return []

  monkeypatch.setattr(chat_mod, "prepare_restart_intents", _prepare)
  monkeypatch.setattr(chat_mod, "drain_all_for_restart", _fake_drain)
  chat_mod.draining = False

  bus = get_system_broadcast()
  q = bus.subscribe()
  try:
    assert q.empty()
    asyncio.run(ru.restart_this_worker())
  finally:
    bus.unsubscribe(q)

  assert observed_before_drain == [{"type": "server_restarting"}]
  assert q.empty()


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
  prepared = [{
    "chat_id": "chat-prepared-1234",
    "run_token": "run-prepared-1234",
  }]

  async def _prepare(nonce):
    assert nonce == "nonce-12345678"
    return prepared

  monkeypatch.setattr(chat_mod, "prepare_restart_intents", _prepare)

  async def _boom(timeout=0, *, restart_nonce="", prepared_runs=None):
    del timeout, restart_nonce, prepared_runs
    raise RuntimeError("drain exploded")

  monkeypatch.setattr(chat_mod, "drain_all_for_restart", _boom)

  asyncio.run(ru.restart_this_worker())

  assert calls == []
  assert requests == [{
    "boot_id": "boot-12345678",
    "nonce": "nonce-12345678",
    "runs": prepared,
  }]
  assert len(_FakeTimer.instances) == 1


def test_restart_handshake_failure_restarts_without_authorization(monkeypatch):
  _FakeTimer.instances = []
  calls = []
  monkeypatch.setattr(ru.os, "kill", lambda pid, sig: calls.append((pid, sig)))
  monkeypatch.setattr(ru.threading, "Timer", _FakeTimer)
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "new_nonce", lambda: "nonce-12345678")

  async def _fake_drain(
    timeout=0, *, restart_nonce="", prepared_runs=None,
  ):
    del timeout, restart_nonce, prepared_runs
    return [{"chat_id": "chat-12345678", "run_token": "run-12345678"}]

  def _request_fails(**kwargs):
    del kwargs
    raise OSError("volume unavailable")

  monkeypatch.setattr(chat_mod, "drain_all_for_restart", _fake_drain)
  monkeypatch.setattr(restart_ledger, "request_restart", _request_fails)

  asyncio.run(ru.restart_this_worker())

  assert calls == [(os.getpid(), signal.SIGTERM)]


def test_external_cutover_drains_without_requesting_self_restart(monkeypatch):
  _FakeTimer.instances = []
  calls = []
  published = []
  fallback_requests = []
  monkeypatch.setattr(ru.os, "kill", lambda pid, sig: calls.append((pid, sig)))
  monkeypatch.setattr(ru.threading, "Timer", _FakeTimer)
  monkeypatch.setattr(
    restart_ledger, "authorized_cutover_challenge", lambda value: value == "cutover-12345678",
  )
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-12345678")
  monkeypatch.setattr(restart_ledger, "new_nonce", lambda: "nonce-12345678")
  monkeypatch.setattr(
    restart_ledger, "publish_cutover_intent",
    lambda **kwargs: published.append(kwargs),
  )
  monkeypatch.setattr(
    restart_ledger, "request_restart",
    lambda **kwargs: fallback_requests.append(kwargs),
  )

  async def _prepare(_nonce):
    return [{"chat_id": "chat-12345678", "run_token": "run-12345678"}]

  async def _drain(timeout=0, *, restart_nonce="", prepared_runs=None):
    del timeout, restart_nonce
    return prepared_runs or []

  monkeypatch.setattr(chat_mod, "prepare_restart_intents", _prepare)
  monkeypatch.setattr(chat_mod, "drain_all_for_restart", _drain)
  chat_mod.draining = False

  result = asyncio.run(ru.prepare_container_cutover("cutover-12345678"))

  assert result["status"] == "prepared"
  assert result["run_count"] == 1
  assert calls == []
  assert fallback_requests == []
  assert published == [{
    "boot_id": "boot-12345678",
    "nonce": "nonce-12345678",
    "cutover_id": "cutover-12345678",
    "runs": [{"chat_id": "chat-12345678", "run_token": "run-12345678"}],
  }]
  assert len(_FakeTimer.instances) == 1
  watchdog = _FakeTimer.instances[0]
  assert watchdog.interval == ru._CUTOVER_FAILSAFE_SECONDS
  watchdog.fn()
  assert fallback_requests == [{
    "boot_id": "boot-12345678",
    "nonce": "nonce-12345678",
    "runs": [{"chat_id": "chat-12345678", "run_token": "run-12345678"}],
  }]
