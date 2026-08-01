"""Lazy process boundary for the optional official Pocket TTS runtime.

Pocket TTS carries PyTorch and model weights that should not live inside the
Möbius API process.  This module starts Kyutai's unmodified ``pocket-tts
serve`` command only when speech is requested, keeps the selected language in
memory between requests, and swaps the single resident model when the owner
changes language.  The isolated venv and Hugging Face cache both live under
``/data/shared/pocket-tts`` so ordinary server restarts and image rebuilds do
not repeat the one-time download.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from threading import RLock

from app.config import get_settings

SUPPORTED_LANGUAGES = {
  "english",
  "french_24l",
  "german_24l",
  "spanish_24l",
  "portuguese_24l",
  "italian_24l",
}

_PORT = int(os.environ.get("MOBIUS_POCKET_TTS_PORT", "8791"))
_START_LOCK = RLock()
_PROCESS: subprocess.Popen | None = None
_PACKAGE = "pocket-tts==2.1.0"


class SpeechRuntimeError(RuntimeError):
  """The isolated speech runtime could not be started or contacted."""


def _root() -> Path:
  return Path(get_settings().data_dir) / "shared" / "pocket-tts"


def _python() -> Path:
  return _root() / "venv" / "bin" / "python"


def _ensure_installed() -> None:
  """Create the pinned, persistent runtime on the first explicit speech use."""
  if _python().is_file():
    return
  root = _root()
  root.mkdir(parents=True, exist_ok=True)
  staging = root / "venv.installing"
  shutil.rmtree(staging, ignore_errors=True)
  log_path = root / "install.log"
  try:
    with log_path.open("ab") as log_file:
      subprocess.run(
        [sys.executable, "-m", "venv", str(staging)],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        check=True,
        timeout=120,
      )
      subprocess.run(
        [str(staging / "bin" / "python"), "-m", "pip", "install", _PACKAGE],
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        check=True,
        timeout=1800,
      )
    staging.replace(root / "venv")
  except (OSError, subprocess.SubprocessError) as exc:
    shutil.rmtree(staging, ignore_errors=True)
    raise SpeechRuntimeError(
      "Pocket TTS could not be installed. See the speech install log."
    ) from exc


def _state_path() -> Path:
  return _root() / "runtime.json"


def _health() -> bool:
  try:
    with urllib.request.urlopen(
      f"http://127.0.0.1:{_PORT}/health", timeout=0.8,
    ) as response:
      return response.status == 200
  except Exception:
    return False


def _read_state() -> dict:
  try:
    data = json.loads(_state_path().read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}
  except (FileNotFoundError, OSError, ValueError):
    return {}


def _write_state(pid: int, language: str) -> None:
  path = _state_path()
  path.parent.mkdir(parents=True, exist_ok=True)
  temp = path.with_suffix(".tmp")
  temp.write_text(
    json.dumps({"pid": pid, "language": language, "port": _PORT}),
    encoding="utf-8",
  )
  temp.replace(path)


def _runtime_pid(pid: object) -> int | None:
  if not isinstance(pid, int) or pid <= 1:
    return None
  try:
    command = (Path("/proc") / str(pid) / "cmdline").read_bytes()
  except OSError:
    return None
  expected_python = str(_python()).encode()
  if expected_python not in command or b"pocket_tts" not in command or b"serve" not in command:
    return None
  return pid


def _stop_pid(pid: int | None) -> None:
  if pid is None:
    return
  try:
    os.killpg(pid, signal.SIGTERM)
  except (ProcessLookupError, PermissionError):
    try:
      os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
      return
  deadline = time.monotonic() + 5
  while time.monotonic() < deadline:
    if not (Path("/proc") / str(pid)).exists():
      return
    time.sleep(0.1)
  try:
    os.killpg(pid, signal.SIGKILL)
  except (ProcessLookupError, PermissionError):
    pass


def invalidate_runtime() -> None:
  """Stop a generation that lost its client before allowing another request."""
  global _PROCESS
  with _START_LOCK:
    state = _read_state()
    _stop_pid(_runtime_pid(state.get("pid")))
    _PROCESS = None
    _state_path().unlink(missing_ok=True)


def ensure_runtime(language: str, timeout_seconds: int = 300) -> str:
  """Return the loopback origin for a healthy server using ``language``."""
  global _PROCESS
  if language not in SUPPORTED_LANGUAGES:
    raise SpeechRuntimeError("Unsupported speech language.")

  with _START_LOCK:
    _ensure_installed()
    python = _python()
    if not python.is_file():
      raise SpeechRuntimeError("Pocket TTS installation did not produce a runtime.")

    state = _read_state()
    if (
      state.get("language") == language
      and state.get("port") == _PORT
      and _runtime_pid(state.get("pid")) is not None
      and _health()
    ):
      return f"http://127.0.0.1:{_PORT}"

    # A previous API process may have left the deliberately independent model
    # server alive. Reuse it only when its pinned state matches; otherwise stop
    # that exact verified Pocket TTS command before loading the new language.
    _stop_pid(_runtime_pid(state.get("pid")))
    _state_path().unlink(missing_ok=True)

    root = _root()
    cache = root / "cache"
    root.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    log_path = root / "pocket-tts.log"
    env = os.environ.copy()
    env.update({
      "HF_HOME": str(cache),
      "PYTHONUNBUFFERED": "1",
    })
    command = [
      str(python), "-m", "pocket_tts", "serve",
      "--host", "127.0.0.1", "--port", str(_PORT),
      "--language", language,
    ]
    with log_path.open("ab") as log_file:
      _PROCESS = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
      )
    _write_state(_PROCESS.pid, language)

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
      if _PROCESS.poll() is not None:
        _state_path().unlink(missing_ok=True)
        raise SpeechRuntimeError(
          "Pocket TTS stopped while loading. See the speech runtime log."
        )
      if _health():
        return f"http://127.0.0.1:{_PORT}"
      time.sleep(0.4)

    invalidate_runtime()
    raise SpeechRuntimeError("Pocket TTS did not become ready in time.")
