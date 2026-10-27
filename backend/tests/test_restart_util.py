"""restart_this_worker — the reliable, drain-gated in-process restart behind the
Settings + platform "Restart" buttons (design §2.2). The normal path asks the
frozen supervisor to acknowledge the exact intent and terminate pid 1; a direct
SIGTERM is only the fail-closed handshake fallback. Every path arms a hard-kill
backstop and drains first. These tests pin those boundaries without killing the
test process (os.kill is mocked)."""

import asyncio
import json
import os
from pathlib import Path
import signal

import pytest

from app import chat as chat_mod
from app import platform_boot
from app import restart_ledger
import app.restart_util as ru


@pytest.fixture(autouse=True)
def _unready_boots(monkeypatch, tmp_path):
  """Keep every restart away from the instance's real boot record."""
  record = tmp_path / "unready-boots.json"
  monkeypatch.setattr(platform_boot, "UNREADY_BOOTS", record)
  return record


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


def _write_probe_fixture(
  root: Path, *, broken: bool, selftest: str | None = None,
) -> None:
  app = root / "backend" / "app"
  routes = app / "routes"
  routes.mkdir(parents=True, exist_ok=True)
  (app / "__init__.py").write_text("", encoding="utf-8")
  (app / "main.py").write_text("from app import routes\n", encoding="utf-8")
  verdict = (
    "raise RuntimeError('dangling restart contract')"
    if broken else "return None"
  )
  (routes / "__init__.py").write_text(
    "def require_all_routers_loaded():\n"
    f"  {verdict}\n",
    encoding="utf-8",
  )
  selftest_path = app / "startup_selftest.py"
  if selftest is None:
    selftest_path.unlink(missing_ok=True)
  else:
    selftest_path.write_text(selftest, encoding="utf-8")


def test_restart_source_validation_matches_the_boot_router_verdict(
  monkeypatch, tmp_path,
):
  monkeypatch.setenv("MOBIUS_PLATFORM_DIR", str(tmp_path))
  _write_probe_fixture(tmp_path, broken=True)

  try:
    ru.validate_restart_source()
  except ru.RestartSourceInvalid as exc:
    assert "dangling restart contract" in str(exc)
  else:
    raise AssertionError("broken router verdict was accepted")

  _write_probe_fixture(tmp_path, broken=False)
  ru.validate_restart_source()


def test_restart_source_validation_rejects_a_failing_startup_selftest(
  monkeypatch, tmp_path,
):
  monkeypatch.setenv("MOBIUS_PLATFORM_DIR", str(tmp_path))
  _write_probe_fixture(tmp_path, broken=False, selftest=(
    "import app.main\nraise RuntimeError('provider resolution broke')\n"
  ))

  try:
    ru.validate_restart_source()
  except ru.RestartSourceInvalid as exc:
    assert "provider resolution broke" in str(exc)
  else:
    raise AssertionError("failing startup selftest was accepted")


def test_restart_source_validation_runs_the_router_verdict_after_the_selftest(
  monkeypatch, tmp_path,
):
  monkeypatch.setenv("MOBIUS_PLATFORM_DIR", str(tmp_path))
  _write_probe_fixture(tmp_path, broken=True, selftest="import app.main\n")

  try:
    ru.validate_restart_source()
  except ru.RestartSourceInvalid as exc:
    assert "dangling restart contract" in str(exc)
  else:
    raise AssertionError("broken router verdict was accepted")


def test_restart_source_without_selftest_gets_import_only_validation(
  monkeypatch, tmp_path,
):
  monkeypatch.setenv("MOBIUS_PLATFORM_DIR", str(tmp_path))
  _write_probe_fixture(tmp_path, broken=False)
  # Not imported by app.main: only a present selftest would reach it.
  (tmp_path / "backend/app/providers.py").write_text(
    "raise RuntimeError('not part of import-only')\n", encoding="utf-8",
  )

  ru.validate_restart_source()


_GAP = {
  "packages": [{
    "name": "claude-agent-sdk", "declared": "==0.2.165",
    "installed": "0.2.161", "kind": "version", "origin": "official_newer",
  }],
  "inputs": ["backend/requirements.lock"], "origin": "official_newer",
  "declared_by": "c" * 40, "fix": {"action": "replace_container", "target_sha": "c" * 40},
  "blocking": False,
}


def _baked_image(root: Path, monkeypatch, *, exit_code: int | None, gap=None) -> None:
  """An image checkout whose boot answers ``check-packages`` (``exit_code``),
  or an older one that only knows activate|revert|guard (None)."""
  app = root / "app"
  app.mkdir(parents=True)
  (app / "__init__.py").write_text("", encoding="utf-8")
  if exit_code is None:
    body = "import sys\nprint('usage: activate|revert|guard', file=sys.stderr)\nraise SystemExit(2)\n"
  else:
    body = (
      "import json, sys\n"
      "PACKAGE_CHECK_PROTOCOL = 1\n"
      "assert sys.argv[1] == \"check-packages\"\n"
      f"print(json.dumps({gap!r}))\n"
      f"raise SystemExit({exit_code})\n"
    )
  (app / "platform_boot.py").write_text(body, encoding="utf-8")
  monkeypatch.setattr(ru, "BAKED_BACKEND", root)


def test_this_images_boot_declares_its_package_check(monkeypatch):
  """The marker restart admission reads is in this image's own boot module,
  and an older boot module without it keeps the strict fallback."""
  monkeypatch.setattr(ru, "BAKED_BACKEND", Path(ru.__file__).resolve().parents[1])
  assert ru.image_boot_checks_packages()

  monkeypatch.setattr(ru, "BAKED_BACKEND", Path("/nonexistent-baked-backend"))
  assert not ru.image_boot_checks_packages()


def test_admission_uses_baked_check_packages_when_available(monkeypatch, tmp_path):
  """A new image serves a version-only gap, so admission follows its verdict
  even where the served strict rule would refuse."""
  from app import platform_update

  monkeypatch.setenv("MOBIUS_PLATFORM_DIR", str(tmp_path / "platform"))
  _write_probe_fixture(tmp_path / "platform", broken=False)
  # The served gap would refuse under the strict rule.
  monkeypatch.setattr(platform_update, "python_package_gap", lambda *_: dict(_GAP))
  _baked_image(tmp_path / "baked-ok", monkeypatch, exit_code=0, gap=_GAP)

  ru.validate_restart_source()

  blocking = {**_GAP, "packages": [{**_GAP["packages"][0], "kind": "missing", "installed": None}],
              "blocking": True}
  _baked_image(tmp_path / "baked-refuses", monkeypatch, exit_code=1, gap=blocking)
  try:
    ru.validate_restart_source()
  except ru.RestartSourceInvalid as exc:
    assert exc.detail == {"reason": "image_packages_behind_source", "gap": blocking}
    assert "claude-agent-sdk: source declares ==0.2.165, this image does not have it" in str(exc)
  else:
    raise AssertionError("a missing distribution was admitted")


def test_admission_falls_back_to_served_strict_check_on_old_image(monkeypatch, tmp_path):
  """An older image refuses every official-newer gap at boot, so admission
  must too, and names the gap."""
  from app import platform_update

  monkeypatch.setenv("MOBIUS_PLATFORM_DIR", str(tmp_path / "platform"))
  _write_probe_fixture(tmp_path / "platform", broken=False)
  monkeypatch.setattr(platform_update, "python_package_gap", lambda *_: dict(_GAP))
  _baked_image(tmp_path / "baked-old", monkeypatch, exit_code=None)

  try:
    ru.validate_restart_source()
  except ru.RestartSourceInvalid as exc:
    assert str(exc) == f"Restart stopped: {platform_update.describe_package_gap(_GAP)}"
    assert exc.detail["gap"] == {**_GAP, "blocking": True}
  else:
    raise AssertionError("the old image's strict rule was not predicted")


def test_last_chance_restart_preflight_does_not_drain_invalid_source(
  monkeypatch,
):
  claimed = []
  monkeypatch.setattr(
    ru, "validate_restart_source",
    lambda: (_ for _ in ()).throw(ru.RestartSourceInvalid("broken")),
  )
  monkeypatch.setattr(
    ru, "_claim_in_process_restart", lambda: claimed.append(True) or True,
  )

  asyncio.run(ru.restart_this_worker())

  assert claimed == []


def test_an_approved_restart_retries_a_commit_set_aside_for_not_starting(
  monkeypatch, _unready_boots,
):
  _unready_boots.write_text(json.dumps({"sha": "abc", "unready_boots": 2}))
  monkeypatch.setattr(ru, "validate_restart_source", lambda: None)
  monkeypatch.setattr(ru, "_claim_in_process_restart", lambda: True)
  monkeypatch.setattr(ru.threading, "Timer", _FakeTimer)

  class _StopAfterAdmission(Exception):
    pass

  async def _stop_after_admission(**_kwargs):
    raise _StopAfterAdmission

  monkeypatch.setattr(ru, "_drain_exact_restart", _stop_after_admission)

  with pytest.raises(_StopAfterAdmission):
    asyncio.run(ru.restart_this_worker())

  assert not _unready_boots.exists()


def test_a_refused_restart_keeps_the_set_aside_commit_set_aside(
  monkeypatch, _unready_boots,
):
  _unready_boots.write_text(json.dumps({"sha": "abc", "unready_boots": 2}))
  monkeypatch.setattr(
    ru, "validate_restart_source",
    lambda: (_ for _ in ()).throw(ru.RestartSourceInvalid("broken")),
  )

  asyncio.run(ru.restart_this_worker())

  assert _unready_boots.exists()


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
  into the shared delivery state. The boot id prevents the still-answering
  old worker from being mistaken for a completed restart."""
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

  assert observed_before_drain == [{
    "type": "server_restarting", "boot_id": "boot-12345678",
  }]
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


def test_competing_restart_cards_share_one_worker_drain_and_dispatch(monkeypatch):
  requests = []
  drains = []
  _FakeTimer.instances = []
  monkeypatch.setattr(ru, "validate_restart_source", lambda: None)
  monkeypatch.setattr(ru.threading, "Timer", _FakeTimer)
  monkeypatch.setattr(ru.os, "kill", lambda *_args: (_ for _ in ()).throw(
    AssertionError("test must never signal a worker")
  ))
  monkeypatch.setattr(restart_ledger, "request_restart", lambda **kwargs: requests.append(kwargs))

  async def drain():
    drains.append(True)
    await asyncio.sleep(0)  # A competing card reaches admission while draining.
    return "boot-multiple-cards", "one-restart-nonce", []

  monkeypatch.setattr(ru, "_drain_exact_restart", drain)

  async def competing_cards():
    await asyncio.gather(ru.restart_this_worker(), ru.restart_this_worker())

  asyncio.run(competing_cards())
  assert drains == [True]
  assert requests == [{"boot_id": "boot-multiple-cards", "nonce": "one-restart-nonce", "runs": []}]
  assert len(_FakeTimer.instances) == 1
