"""Key vault lifecycle: the server keeps one vault running, and a vault never
outlives the server that started it or leaves its socket behind.

The supervisor tests run stand-in vault programs with short timings; the
process tests run the real vault program in its own process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import httpx
import pytest

from app import key_vault


VAULT_PROGRAM = key_vault.VAULT_PROGRAM


def _wait_for(condition, timeout: float = 10.0) -> bool:
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    if condition():
      return True
    time.sleep(0.05)
  return False


# -- the supervisor -------------------------------------------------------------

@pytest.fixture
def supervised(monkeypatch, tmp_path):
  """Point the supervisor at ``tmp_path`` and a stand-in vault program."""
  monkeypatch.setattr(key_vault, "get_settings", lambda: types.SimpleNamespace(data_dir=str(tmp_path)))
  program = tmp_path / "stand-in-vault.py"
  monkeypatch.setattr(key_vault, "VAULT_PROGRAM", program)

  def run(source: str) -> asyncio.Task:
    program.write_text(source)
    return asyncio.create_task(key_vault.supervise_key_vault())
  return run


# Each start is counted; the third runs long enough to count as healthy.
_EXITS_AT_ONCE = """
import pathlib, sys, time
data = pathlib.Path(sys.argv[sys.argv.index("--data") + 1]).parent
with open(data / "starts", "a") as starts:
  starts.write("x")
if len((data / "starts").read_text()) == 3:
  time.sleep(1.6)
sys.exit(3)
"""


def test_a_vault_that_exits_is_restarted_with_backoff_that_resets_after_a_healthy_run(
  supervised, monkeypatch, tmp_path, caplog,
):
  monkeypatch.setattr(key_vault, "_RESTART_BACKOFF_SECS", (0.01, 0.02, 0.03))
  monkeypatch.setattr(key_vault, "_HEALTHY_RUN_SECS", 1.5)
  caplog.set_level(logging.WARNING, logger="app.key_vault")
  starts = tmp_path / "starts"

  async def scenario():
    supervisor = supervised(_EXITS_AT_ONCE)
    assert await asyncio.to_thread(
      _wait_for, lambda: starts.exists() and len(starts.read_text()) >= 7, 20,
    )
    supervisor.cancel()
    with pytest.raises(asyncio.CancelledError):
      await supervisor

  asyncio.run(scenario())
  restarts = [r.args for r in caplog.records if r.msg.startswith("key vault exited")]
  assert [code for code, _ in restarts[:6]] == [3] * 6
  # Backoff grows to its last step and stays there; the long third run
  # counted as healthy, so the next failure starts from the first step again.
  assert [delay for _, delay in restarts[:6]] == [0.01, 0.02, 0.01, 0.02, 0.03, 0.03]


_RUNS_UNTIL_STOPPED = """
import os, pathlib, signal, sys, time
data = pathlib.Path(sys.argv[sys.argv.index("--data") + 1]).parent
if (data / "ignore-term").exists():
  signal.signal(signal.SIGTERM, signal.SIG_IGN)
(data / "pid").write_text(str(os.getpid()))
time.sleep(60)
"""


@pytest.mark.parametrize("ignores_terminate", [False, True])
def test_stopping_the_supervisor_stops_its_vault(supervised, monkeypatch, tmp_path, ignores_terminate):
  """Cancelling the supervisor (server shutdown) ends the vault: asked to
  stop first, then killed once the grace period runs out."""
  monkeypatch.setattr(key_vault, "_STOP_GRACE_SECS", 0.3)
  if ignores_terminate:
    (tmp_path / "ignore-term").touch()
  pid_file = tmp_path / "pid"

  async def scenario():
    supervisor = supervised(_RUNS_UNTIL_STOPPED)
    assert await asyncio.to_thread(_wait_for, lambda: pid_file.exists() and pid_file.read_text())
    pid = int(pid_file.read_text())
    started = time.monotonic()
    supervisor.cancel()
    with pytest.raises(asyncio.CancelledError):
      await supervisor
    return pid, time.monotonic() - started

  pid, took = asyncio.run(scenario())
  # Reaped by the supervisor, so the pid is gone, not a zombie.
  with pytest.raises(ProcessLookupError):
    os.kill(pid, 0)
  assert took < 5


# -- the vault process ----------------------------------------------------------

@pytest.fixture
def vault_folder():
  # Unix socket paths are limited to ~108 bytes; pytest's tmp_path can exceed it.
  folder = Path(tempfile.mkdtemp(prefix="kvl-"))
  yield folder / "vault"
  shutil.rmtree(folder, ignore_errors=True)


def _answers(socket_path: Path) -> bool:
  try:
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path)), timeout=2) as client:
      return client.get("http://vault/health").status_code == 200
  except (httpx.TransportError, OSError):
    return False


def _gone(pid: int) -> bool:
  """Exited: no such process, or one only its new parent has yet to reap."""
  try:
    return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] == "Z"
  except (FileNotFoundError, ProcessLookupError):
    return True


# Starts the vault as its own child, prints the vault's pid, and waits.
_PARENT = """
import os, subprocess, sys
vault = subprocess.Popen([sys.executable, "-I", sys.argv[1], "--data", sys.argv[2],
                          "--parent", str(os.getpid())])
print(vault.pid, flush=True)
sys.stdin.read()
"""


@pytest.mark.parametrize("socket_replaced", [False, True])
def test_the_vault_exits_when_its_parent_goes_away_and_removes_only_its_own_socket(
  vault_folder, socket_replaced,
):
  socket_path = vault_folder / "vault.sock"
  parent = subprocess.Popen(
    [sys.executable, "-c", _PARENT, str(VAULT_PROGRAM), str(vault_folder)],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
  )
  vault_pid = int(parent.stdout.readline())
  try:
    assert _wait_for(lambda: _answers(socket_path))
    if socket_replaced:
      # Something else now owns the path; the exiting vault must leave it.
      socket_path.rename(vault_folder / "vault.sock.old")
      socket_path.write_text("not the vault's")
    parent.kill()
    parent.wait()
    assert _wait_for(lambda: _gone(vault_pid), 8)
  finally:
    if not _gone(vault_pid):
      os.kill(vault_pid, signal.SIGKILL)
    parent.kill()
    parent.wait()
  if socket_replaced:
    assert socket_path.read_text() == "not the vault's"
  else:
    assert not socket_path.exists()


def test_a_stale_socket_is_replaced_when_the_vault_starts(vault_folder):
  """A socket left by a vault that died without cleaning up does not keep the
  next one from listening; that one removes its socket on a normal stop."""
  vault_folder.mkdir(mode=0o700)
  socket_path = vault_folder / "vault.sock"
  stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
  stale.bind(str(socket_path))
  stale.close()
  assert not _answers(socket_path)
  vault = subprocess.Popen([
    sys.executable, "-I", str(VAULT_PROGRAM), "--data", str(vault_folder), "--parent", str(os.getpid()),
  ])
  try:
    assert _wait_for(lambda: _answers(socket_path))
    assert oct(socket_path.stat().st_mode & 0o777) == oct(0o600)
    vault.terminate()
    assert vault.wait(timeout=8) == 0
  finally:
    if vault.poll() is None:
      vault.kill()
      vault.wait()
  assert not socket_path.exists()
