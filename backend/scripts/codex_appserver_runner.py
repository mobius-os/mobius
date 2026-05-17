#!/usr/bin/env python3
"""Driver for the Codex `app-server` JSON-RPC protocol.

Spawns `codex app-server`, performs the initialize / thread/start /
turn/start handshake, then translates each notification into a Möbius
event line on stdout.  Mobius's chat.py reads our stdout line-by-line
exactly as it does for `codex exec --json` — the CodexProvider just
points at this script.

Why a wrapper script (not in providers.py): chat.py spawns the
subprocess and `async for raw in proc.stdout` reads lines.  Doing
JSON-RPC requires writing to the subprocess's stdin AFTER spawn, and
the wrapper keeps that orchestration out of the async event loop in
chat.py.  Chat.py stays simple: spawn → read lines → parse_line.

Args:
  --prompt FILE     Path to file containing the user prompt (use a
                    file because long prompts blow past argv limits).
  --session-id ID   Optional: resume an existing thread.
  --model NAME      Optional: override codex model.
  --cwd DIR         Working directory for the codex thread.
  --base-instructions FILE
                    Optional: read system prompt from this file
                    (Möbius's skill / AGENTS.md).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

# Make the app module importable when this script runs inside the
# container (where /app is the working dir for the API but scripts
# live at /app/scripts).
_HERE = Path(__file__).resolve().parent
_APP_ROOT = _HERE.parent
if str(_APP_ROOT) not in sys.path:
  sys.path.insert(0, str(_APP_ROOT))

from app.codex_appserver import translate_notification  # noqa: E402


# How long to wait for initialize / thread responses before giving up.
_INIT_TIMEOUT_SECS = 30
# Hard cap on a single turn (defense against runaway).  chat.py also
# has its own timeout; this is a safety net for stuck protocol state.
_TURN_TIMEOUT_SECS = int(os.environ.get("CODEX_TURN_TIMEOUT_SECS", "1800"))


def _emit(event: dict) -> None:
  """Writes a Möbius event as a single JSON line on stdout."""
  sys.stdout.write(json.dumps(event) + "\n")
  sys.stdout.flush()


def _emit_error(message: str) -> None:
  _emit({"type": "error", "message": message})
  _emit({"type": "done", "cost_usd": 0})


def _drain_stderr(proc: subprocess.Popen) -> None:
  """Discards stderr so the pipe doesn't fill and deadlock writes."""
  if proc.stderr is None:
    return
  for _ in iter(proc.stderr.readline, b""):
    pass


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser()
  parser.add_argument("--prompt", required=True,
                      help="Path to file containing prompt text.")
  parser.add_argument("--session-id", default=None,
                      help="Thread ID to resume.")
  parser.add_argument("--model", default=None,
                      help="Codex model override.")
  parser.add_argument("--cwd", default=os.getcwd(),
                      help="Working directory for the thread.")
  parser.add_argument("--base-instructions", default=None,
                      help="Path to system prompt file.")
  args = parser.parse_args(argv)

  # Unlink the prompt file immediately after reading. CodexProvider
  # writes one prompt file per run with a UUID name; without unlink
  # they accumulate in /data/chats/{chat_id}/ indefinitely AND retain
  # full user prompts outside Möbius's DB retention model (a transcript
  # residue hazard). The try/finally ensures the unlink fires even if
  # the read raises (e.g., file vanished between mkdir and read) so a
  # failing read still cleans up its stub. missing_ok handles
  # double-cleanup safely.
  try:
    prompt_text = Path(args.prompt).read_text(encoding="utf-8")
  except OSError as exc:
    _emit_error(f"codex runner: cannot read prompt file: {exc}")
    Path(args.prompt).unlink(missing_ok=True)
    return 1
  Path(args.prompt).unlink(missing_ok=True)

  base_instructions = None
  if args.base_instructions:
    # Same rationale as the prompt: unlink the per-run instructions
    # file so it doesn't accumulate, even if the read raises.
    try:
      base_instructions = Path(args.base_instructions).read_text(
        encoding="utf-8",
      )
    except OSError:
      # Non-fatal: skill file missing in local dev.  Continue with
      # whatever Codex's defaults are.
      base_instructions = None
    Path(args.base_instructions).unlink(missing_ok=True)

  # Spawn codex app-server.  The auth (CODEX_HOME) is inherited from
  # the parent (chat.py sets it via CodexProvider.build()'s env).
  #
  # start_new_session=True puts the child in its own process group, so
  # _cleanup() can `killpg` the whole tree even if codex spawns helper
  # processes. Without this, a SIGKILL on the runner would orphan
  # codex (and its helpers), wasting CPU and tokens.
  try:
    proc = subprocess.Popen(
      ["codex", "app-server"],
      stdin=subprocess.PIPE,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      bufsize=0,
      start_new_session=True,
    )
  except FileNotFoundError:
    _emit_error("codex CLI not found on PATH.")
    return 1
  except OSError as exc:
    _emit_error(f"codex runner: failed to spawn app-server: {exc}")
    return 1

  def _cleanup(p: subprocess.Popen, why: str = "") -> None:
    """Terminate → wait → force-kill the entire process group.

    Called from every early-exit path so a hung initialize/thread/turn
    can never leave codex app-server (or its helpers) alive after the
    runner returns.
    """
    if p.poll() is not None:
      return
    try:
      pgid = os.getpgid(p.pid)
    except (ProcessLookupError, OSError):
      pgid = None
    try:
      if pgid is not None:
        os.killpg(pgid, signal.SIGTERM)
      else:
        p.terminate()
    except (ProcessLookupError, OSError):
      pass
    try:
      p.wait(timeout=2)
      return
    except subprocess.TimeoutExpired:
      pass
    try:
      if pgid is not None:
        os.killpg(pgid, signal.SIGKILL)
      else:
        p.kill()
    except (ProcessLookupError, OSError):
      pass
    try:
      p.wait(timeout=1)
    except subprocess.TimeoutExpired:
      pass

  # When chat.py kills the runner (Stop button, timeout, restart),
  # SIGTERM arrives here. Install a handler that propagates to the
  # codex process group so app-server + helpers terminate cleanly
  # instead of being orphaned with their tokens still draining.
  def _on_term(signum, _frame):  # noqa: ARG001
    _cleanup(proc, why=f"signal {signum}")
    # Re-raise as the default handler to exit the runner promptly.
    os._exit(143)
  try:
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
  except (ValueError, OSError):
    # Signals only work on the main thread; we are on the main thread
    # here, but be defensive in case of unusual interpreter setups.
    pass

  threading.Thread(
    target=_drain_stderr, args=(proc,), daemon=True,
  ).start()

  # JSON-RPC ids — incremented per request so we can match responses
  # by id.  Notifications have no id and are streamed to translate.
  next_id = {"v": 0}
  responses: dict[int, dict] = {}
  done = threading.Event()
  turn_id_holder: dict[str, str | None] = {"v": None}

  def send(method: str, params: dict | None = None) -> int:
    next_id["v"] += 1
    req_id = next_id["v"]
    msg = {
      "jsonrpc": "2.0",
      "id": req_id,
      "method": method,
      "params": params or {},
    }
    line = json.dumps(msg) + "\n"
    try:
      proc.stdin.write(line.encode("utf-8"))
      proc.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
      _emit_error(f"codex runner: failed to send {method}: {exc}")
      done.set()
      raise
    return req_id

  def wait_response(req_id: int, timeout: float) -> dict | None:
    """Blocks until the reader thread records the response for req_id."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      if done.is_set():
        return None
      resp = responses.pop(req_id, None)
      if resp is not None:
        return resp
      time.sleep(0.02)
    return None

  def reader_loop() -> None:
    """Reads stdout, parses JSON-RPC, dispatches to responses or
    translates notifications to Möbius events on our stdout.

    Sets `done` when turn/completed arrives, when the subprocess
    closes stdout, or on a translated 'done' / 'error' event.
    """
    assert proc.stdout is not None
    for raw in iter(proc.stdout.readline, b""):
      line = raw.decode("utf-8", errors="replace").strip()
      if not line:
        continue
      try:
        msg = json.loads(line)
      except json.JSONDecodeError:
        # app-server should never emit non-JSON lines, but tolerate it.
        continue

      if isinstance(msg, dict) and "id" in msg and "method" not in msg:
        # JSON-RPC response.
        responses[msg["id"]] = msg
        continue

      # Notification — translate to Möbius events.
      events = translate_notification(msg)
      for event in events:
        # Capture turn id for potential interrupt support.
        if event.get("type") == "session_init":
          # session_init only fires once per process; record it but
          # also emit so chat.py persists it.
          pass
        _emit(event)
        if event.get("type") in ("done", "error"):
          done.set()
          return
    # stdout closed without emitting done — surface that.
    if not done.is_set():
      _emit_error("codex app-server exited unexpectedly")
      done.set()

  threading.Thread(target=reader_loop, daemon=True).start()

  # 1. initialize
  try:
    init_id = send("initialize", {
      "clientInfo": {"name": "mobius", "version": "0.1"},
      "capabilities": {},
    })
  except (BrokenPipeError, OSError):
    _cleanup(proc)
    return 1
  init_resp = wait_response(init_id, _INIT_TIMEOUT_SECS)
  if init_resp is None:
    _emit_error("codex runner: initialize timed out")
    _cleanup(proc)
    return 1
  if "error" in init_resp:
    _emit_error(
      f"codex initialize error: {init_resp['error'].get('message', 'unknown')}",
    )
    _cleanup(proc)
    return 1

  # 2. thread/start or thread/resume
  if args.session_id:
    thread_method = "thread/resume"
    thread_params: dict = {"threadId": args.session_id}
  else:
    thread_method = "thread/start"
    thread_params = {
      "cwd": args.cwd,
      "sandbox": "danger-full-access",
      "approvalPolicy": "never",
    }
    if base_instructions:
      thread_params["baseInstructions"] = base_instructions
    if args.model:
      thread_params["model"] = args.model

  try:
    thread_req_id = send(thread_method, thread_params)
  except (BrokenPipeError, OSError):
    _cleanup(proc)
    return 1
  thread_resp = wait_response(thread_req_id, _INIT_TIMEOUT_SECS)
  if thread_resp is None:
    _emit_error(f"codex runner: {thread_method} timed out")
    _cleanup(proc)
    return 1
  if "error" in thread_resp:
    _emit_error(
      f"codex {thread_method} error: "
      f"{thread_resp['error'].get('message', 'unknown')}",
    )
    _cleanup(proc)
    return 1

  # Pull thread id out of the response.  For thread/start the
  # `thread/started` notification will also carry it (and trigger
  # session_init via the translator), but we still need it for
  # turn/start.
  thread_result = thread_resp.get("result") or {}
  thread_id = (
    (thread_result.get("thread") or {}).get("id")
    or args.session_id
  )
  if not thread_id:
    _emit_error("codex runner: no thread id in start response")
    _cleanup(proc)
    return 1

  # If resuming, the runner won't see a thread/started notification,
  # so emit session_init ourselves so chat.py keeps tracking it.
  if args.session_id:
    _emit({"type": "session_init", "session_id": thread_id})

  # 3. turn/start
  turn_params: dict = {
    "threadId": thread_id,
    "input": [{"type": "text", "text": prompt_text}],
  }
  if args.model:
    turn_params["model"] = args.model
  try:
    turn_req_id = send("turn/start", turn_params)
  except (BrokenPipeError, OSError):
    _cleanup(proc)
    return 1
  turn_resp = wait_response(turn_req_id, _INIT_TIMEOUT_SECS)
  if turn_resp is None:
    _emit_error("codex runner: turn/start timed out")
    _cleanup(proc)
    return 1
  if "error" in turn_resp:
    _emit_error(
      "codex turn/start error: "
      f"{turn_resp['error'].get('message', 'unknown')}",
    )
    _cleanup(proc)
    return 1
  turn_result = turn_resp.get("result") or {}
  turn_id_holder["v"] = (turn_result.get("turn") or {}).get("id")

  # 4. Wait for the reader thread to see turn/completed or an error.
  if not done.wait(timeout=_TURN_TIMEOUT_SECS):
    _emit_error(
      f"codex runner: turn timed out after {_TURN_TIMEOUT_SECS}s",
    )
    _cleanup(proc)
    return 1

  # Clean shutdown.  Closing stdin signals the server we're done.
  try:
    if proc.stdin and not proc.stdin.closed:
      proc.stdin.close()
  except OSError:
    pass
  try:
    proc.wait(timeout=5)
  except subprocess.TimeoutExpired:
    _cleanup(proc)

  return 0


if __name__ == "__main__":
  sys.exit(main())
