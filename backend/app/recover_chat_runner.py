"""Minimal CLI runner for the recovery chat.

Deliberately does NOT share code with app.chat, app.providers, or
the SDK runners. Those are the production chat path; if the agent
broke them, recovery still needs to work. This module is small,
frozen (chmod 444 via protected-files.txt), and imports only
stdlib + bcrypt.

What it does:
- Spawns the Claude CLI with --print --output-format stream-json
  via asyncio.create_subprocess_exec (args as list, no shell)
- Parses each stdout JSON line into a small set of events
- Yields them as SSE-formatted strings for the recovery chat page
- Appends each user + assistant turn to /data/recovery_chat.jsonl
  (append-only log; survives if the chats DB schema is broken)

What it does NOT do:
- AskUserQuestion (user can just type)
- Multi-turn resume (the agent reads the log file itself for context)
- Stop / cancel mid-stream (refresh to abandon)
- Per-token typewriter
- Provider switching (Claude only; Codex SDK is in production path)
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import AsyncIterator


RECOVERY_LOG_PATH = Path("/data/recovery_chat.jsonl")
CLAUDE_CONFIG_PATH = Path("/data/cli-auth/claude")
SUBPROCESS_CWD = "/data"

# `_current_run` tracks the in-flight stream_turn so a second
# concurrent request can detect the conflict and return a
# 409-equivalent SSE error rather than queueing. It carries the live
# asyncio subprocess Process so the generator's finally can
# deterministically kill it.
#
# `_run_lock` is a *threading* lock (not asyncio.Lock) because the
# claim/release operations are pure in-memory dict reads/writes — no
# I/O, no awaits. Using a sync lock makes the release path
# uncancellable: real ASGI client-disconnect raises CancelledError
# at the next await point inside `stream_turn`'s finally, and if the
# release used `async with` that CancelledError could abort the
# release before `_current_run = None` runs, wedging the recovery
# chat until server restart. A sync lock has no await, so
# cancellation cannot interleave.
#
# This replaces the old `_STREAM_LOCK` that wrapped the entire SSE
# generator: when a client disconnected, FastAPI stopped consuming
# and the `async with` exit only ran when the generator was GC'd or
# aclose()'d, leaving the lock orphaned and blocking every
# subsequent /stream request.
_current_run: dict | None = None
_run_lock = threading.Lock()


def _claim_run() -> dict | None:
  """Atomically claim the run slot.

  Returns a fresh claim dict on success, or None if a turn is
  already in flight. The claim has `proc: None` initially; the
  caller fills it after spawning the subprocess so the cleanup path
  can find the process to terminate.
  """
  global _current_run
  with _run_lock:
    if _current_run is not None:
      return None
    _current_run = {"proc": None}
    return _current_run


def _release_run(claim: dict) -> None:
  """Release the run slot if `claim` still owns it.

  Pure sync — safe to call from a cancelled task's finally. The
  identity check guards against a misuse that reassigned
  `_current_run` from outside; we'd rather leave a stale slot than
  clobber a different caller's claim.
  """
  global _current_run
  with _run_lock:
    if _current_run is claim:
      _current_run = None


# Module-level threading lock wraps "append one line + derive its
# index" in `append_log`. Without this, two concurrent /send requests
# can both append their line, both count the file (both see N lines),
# and both return the same turn_id — re-opening the multi-tab pairing
# race the turn_id was meant to close. threading.Lock (not
# asyncio.Lock) because the file I/O is synchronous and must work
# across any mix of sync + async callers.
_append_lock = threading.Lock()


# Grace period (seconds) between SIGTERM and SIGKILL during cleanup.
# Short enough that an abandoned stream releases the run-claim quickly
# (next /stream POST is unblocked), long enough for a polite shutdown
# to flush a final stdout line.
_KILL_GRACE_SECONDS = 0.5


# Hard cap on the rendered + in-memory recovery log so a long repair
# session can't degrade the very page you need most. The on-disk
# file keeps growing; only the page render + latest_user_message are
# bounded. Operator can manually truncate the file if needed.
MAX_RENDERED_MESSAGES = 200


def _sse(event: dict) -> str:
  """Encodes an event as a single SSE message line."""
  return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def _system_prompt() -> str:
  """Recovery agent instructions. Minimal — agent is here to fix.

  Updated 2026-05-26 per multi-reviewer findings:
    - `diff -ru` not `git diff`: /app/app-baked/ has no .git
    - cwd is /data/, set in stream_turn's subprocess call
    - read prior turns from the log file before answering
    - no AskUserQuestion (recovery UI has only a textarea)
    - no API calls (no AGENT_TOKEN, no API_BASE_URL set here —
      this is intentionally filesystem-only)
  """
  return (
    "You are running inside the Mobius recovery chat. The user has "
    "reached you here because something in the platform is broken "
    "and they need help fixing it.\n\n"
    "BEFORE answering, run `Read /data/recovery_chat.jsonl` to see "
    "prior turns in this session (each line is a JSON object with "
    "role + content). The CLI is invoked fresh per turn so there is "
    "no in-process memory of earlier exchanges.\n\n"
    "You have filesystem-only access. There is NO $AGENT_TOKEN, NO "
    "$API_BASE_URL, NO $CHAT_ID env var here — the production chat "
    "API plumbing may be broken. Do not try to POST to /api/...\n\n"
    "Do NOT call AskUserQuestion. The recovery UI is a plain "
    "textarea; questions you ask via that tool will hang silently. "
    "Ask in plain prose and wait for the user's next turn.\n\n"
    "Write surface:\n"
    "  /app/app/        backend Python (mobius-writable, EXCEPT the "
    "frozen-island files below). Your cwd is /data/; backend lives "
    "at /app/app/.\n"
    "  /app/scripts/    utility scripts (mobius-writable, EXCEPT "
    "the two .sh files below).\n"
    "  /data/shell/     frontend source + built bundle.\n\n"
    "Frozen island (chmod 444/555 root-owned, edits are blocked at "
    "the OS level — do not waste tool calls trying):\n"
    "  /app/app/main.py                  router wiring\n"
    "  /app/app/routes/__init__.py        router exports\n"
    "  /app/app/auth.py                   production auth\n"
    "  /app/app/database.py               DB engine init\n"
    "  /app/app/routes/recover.py         recovery page\n"
    "  /app/app/routes/recover_html.py    recovery HTML\n"
    "  /app/app/recover_chat.py           this chat's endpoints\n"
    "  /app/app/recover_chat_runner.py    this runner\n"
    "  /app/app/recover_auth.py           recovery auth\n"
    "  /app/scripts/entrypoint.sh         boot\n"
    "  /app/scripts/recovery_restore.sh   restore from baked\n\n"
    "Workflow:\n"
    "1. Read /data/logs/chat.log for the latest error trail.\n"
    "2. To see what changed vs the baked copy, use `diff -ru "
    "/app/app-baked/ /app/app/` (NOT git diff — /app/app-baked/ "
    "has no .git). Same for `/app/shell-src/` vs `/data/shell/`.\n"
    "3. Make the fix in /app/app/ or /app/scripts/ or /data/shell/.\n"
    "4. Tell the user: \"Click the **Restart server** button at the "
    "top of this page.\" That POSTs /recover/restart, which SIGTERMs "
    "uvicorn; the container's restart policy brings it back with "
    "your edits loaded. No need to leave this chat.\n"
    "5. After the partner confirms the fix, append a Lesson to "
    "/data/shared/agent-experience.md describing what went wrong "
    "and how to avoid it.\n\n"
    "If you cannot fix it from here, tell the user: \"Open "
    "/recover in a new tab; click 'Restore backend' (or 'Restore "
    "shell' / 'Restore scripts') — this copies the baked sources "
    "back over the live ones and restarts the server.\""
  )


def append_log(role: str, content: str) -> int:
  """Appends one message to the recovery log and returns its
  zero-based index. The index is the turn_id used by /send +
  /stream to pair a stream response to its specific message,
  closing the multi-tab race where 'latest user message' would
  cross wires.

  JSON-per-line so a partial write doesn't corrupt earlier
  entries. Index counts ALL lines (any role); the runner filters
  by role when reading. Using line count as id is durable: log
  truncation invalidates ids, which is the correct behaviour
  (after reset, prior turn_ids are stale and the next /stream
  will simply 400).

  Append + index-derivation runs under `_append_lock` so two
  concurrent callers cannot both observe the same final line
  count. Without the lock, both would return the same turn_id,
  both /stream POSTs would resolve to the same log row, and the
  multi-tab pairing race the turn_id was meant to close would
  re-open.
  """
  RECOVERY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
  entry = {"role": role, "content": content, "ts": time.time()}
  payload = json.dumps(entry, separators=(",", ":")) + "\n"
  with _append_lock:
    # One open in "a+" mode: append the line and count from the same
    # handle. Seeking to 0 and counting under the lock guarantees no
    # other writer can interleave between the write and the count, so
    # the returned index is always this line's zero-based position.
    try:
      with RECOVERY_LOG_PATH.open("a+", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        f.seek(0)
        total = sum(1 for _ in f)
      return total - 1
    except Exception:
      return -1


def user_message_by_id(turn_id: int) -> str | None:
  """Returns the content of the message at `turn_id` if it exists
  AND is a user message. None otherwise — the /stream handler 400s
  on None so a stale/garbage id surfaces as a clean error rather
  than racing onto the wrong message."""
  if turn_id < 0:
    return None
  if not RECOVERY_LOG_PATH.is_file():
    return None
  with RECOVERY_LOG_PATH.open("r", encoding="utf-8") as f:
    for i, line in enumerate(f):
      if i != turn_id:
        continue
      line = line.strip()
      if not line:
        return None
      try:
        entry = json.loads(line)
        if entry.get("role") != "user":
          return None
        return entry.get("content") or None
      except json.JSONDecodeError:
        return None
  return None


def load_log(limit: int | None = MAX_RENDERED_MESSAGES) -> list[dict]:
  """Returns the most recent `limit` logged messages in order (or
  all if limit is None). Default cap is MAX_RENDERED_MESSAGES so a
  long repair session can't degrade the recovery page render.

  The on-disk file is unbounded; only what we LOAD is capped.
  Operator can manually truncate /data/recovery_chat.jsonl.
  """
  if not RECOVERY_LOG_PATH.is_file():
    return []
  out = []
  with RECOVERY_LOG_PATH.open("r", encoding="utf-8") as f:
    for line in f:
      line = line.strip()
      if not line:
        continue
      try:
        out.append(json.loads(line))
      except json.JSONDecodeError:
        continue
  if limit is not None and len(out) > limit:
    out = out[-limit:]
  return out


def reset_log() -> None:
  """Wipes the recovery log. The Reset button calls this."""
  if RECOVERY_LOG_PATH.is_file():
    RECOVERY_LOG_PATH.unlink()


def latest_user_message() -> str | None:
  """Returns the most recent user-role message in the log, or None."""
  if not RECOVERY_LOG_PATH.is_file():
    return None
  last = None
  with RECOVERY_LOG_PATH.open("r", encoding="utf-8") as f:
    for line in f:
      line = line.strip()
      if not line:
        continue
      try:
        entry = json.loads(line)
        if entry.get("role") == "user":
          last = entry.get("content")
      except json.JSONDecodeError:
        continue
  return last


async def _terminate_proc(proc: asyncio.subprocess.Process) -> None:
  """Polite SIGTERM, brief grace, SIGKILL fallback. Always awaits
  the child so the kernel reaps it and proc.returncode is set.

  Called from stream_turn's finally so cleanup is deterministic
  regardless of how the generator exited: normal completion, client
  disconnect (GeneratorExit at the current await point), or an
  unexpected exception in the streaming loop.
  """
  if proc.returncode is not None:
    return
  try:
    proc.terminate()
  except (ProcessLookupError, OSError):
    # Process already gone, or signal delivery failed (denied by
    # namespace, etc.). Either way, nothing to wait on — return so
    # the caller's cleanup can proceed.
    return
  try:
    await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE_SECONDS)
    return
  except asyncio.TimeoutError:
    pass
  except BaseException:
    # Don't let wait() failure (including CancelledError from the
    # surrounding task being cancelled) block the SIGKILL fallback.
    pass
  try:
    proc.kill()
  except (ProcessLookupError, OSError):
    return
  try:
    await proc.wait()
  except BaseException:
    pass


async def stream_turn(user_message: str) -> AsyncIterator[str]:
  """Spawns the Claude CLI for one turn and yields SSE events.

  Concurrency contract: at most one subprocess runs at a time. A
  second /stream request that arrives while one is in flight gets a
  409-equivalent SSE error event and exits. We deliberately do NOT
  queue across an SSE boundary — that produces a UI where the client
  thinks its turn is live but nothing is streaming.

  The run-claim is held under `_run_lock` only at the boundary (claim
  on entry, release on exit). The subprocess itself runs outside the
  asyncio lock; its lifetime is bound to the generator via the
  try/finally, which fires on normal completion AND on GeneratorExit
  (FastAPI stops consuming when the client disconnects). The old
  design wrapped the whole generator in `async with _STREAM_LOCK`,
  which orphaned the lock on client disconnect until generator GC
  ran the `__aexit__` — blocking every subsequent /stream call in
  the meantime.
  """
  # Claim the run slot atomically. If it's already taken, return a
  # structured SSE error rather than queueing.
  claim = _claim_run()
  if claim is None:
    yield _sse({
      "type": "error",
      "message": "Another recovery turn is in progress.",
    })
    yield _sse({"type": "done"})
    return

  try:
    async for chunk in _stream_turn_impl(user_message, claim):
      yield chunk
  finally:
    # Release the run slot FIRST, before any await. The release is
    # pure sync (threading.Lock + dict assignment), so it can't be
    # interrupted by CancelledError. If we awaited _terminate_proc
    # first and the task got cancelled mid-await, the slot would
    # leak and wedge the recovery chat until restart — the exact
    # failure the round-4 fix was meant to prevent.
    #
    # Once released, a concurrent /stream POST can start a fresh
    # turn. That turn's CLI subprocess is independent of ours; even
    # though our subprocess teardown is still in progress, the OS
    # owns reaping it. Briefly two CLI processes may coexist (ours
    # being killed, theirs starting); the spec is "one turn at a
    # time" and our turn is logically over once cleanup begins.
    _release_run(claim)
    proc = claim.get("proc")
    if proc is not None:
      try:
        await _terminate_proc(proc)
      except BaseException:
        # Don't let subprocess teardown failure propagate. Slot is
        # already released; the kernel will reap the child even if
        # our polite shutdown sequence failed.
        pass


async def _stream_turn_impl(
  user_message: str, claim: dict
) -> AsyncIterator[str]:
  """Spawns the CLI, writes the message to stdin, streams stdout.

  Message goes via stdin (not argv) so long pastes — crash logs,
  full diffs, > 200KB dumps — don't hit Linux's ~128KB argv cap.
  `claude --print --input-format text` with no positional `prompt`
  reads from stdin (input-format text is the CLI default).
  """
  claude_bin = shutil.which("claude")
  if not claude_bin:
    yield _sse(
      {"type": "error", "message": "claude CLI not found in PATH"}
    )
    yield _sse({"type": "done"})
    return

  env = dict(os.environ)
  if CLAUDE_CONFIG_PATH.is_dir():
    env["CLAUDE_CONFIG_DIR"] = str(CLAUDE_CONFIG_PATH)

  # No positional user_message — it goes via stdin below.
  cmd = [
    claude_bin,
    "--print",
    "--input-format", "text",
    "--output-format", "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--dangerously-skip-permissions",
    "--system-prompt", _system_prompt(),
  ]

  # cwd=/data so the agent's relative-path commands in the system
  # prompt resolve consistently. Without this, cwd inherits from
  # uvicorn's launch dir (/app) which contradicts the prompt's
  # `Read /data/recovery_chat.jsonl` references.
  proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env=env,
    cwd=SUBPROCESS_CWD,
  )
  # Publish the live process onto the claim immediately so a
  # GeneratorExit between spawn and stdin-write still tears down.
  claim["proc"] = proc

  # Write the message to stdin and close so the CLI sees EOF and
  # starts processing. drain() handles backpressure for large
  # payloads automatically.
  assert proc.stdin is not None
  try:
    proc.stdin.write(user_message.encode("utf-8"))
    await proc.stdin.drain()
  except (BrokenPipeError, ConnectionResetError):
    # Subprocess died before reading stdin — fall through to read
    # whatever stderr has and surface as an error event.
    pass
  finally:
    try:
      proc.stdin.close()
    except Exception:
      pass

  full_assistant_text = []
  try:
    assert proc.stdout is not None
    while True:
      line = await proc.stdout.readline()
      if not line:
        break
      try:
        event = json.loads(line.decode("utf-8"))
      except (json.JSONDecodeError, UnicodeDecodeError):
        continue

      ev_type = event.get("type")
      if ev_type == "stream_event":
        inner = event.get("event", {})
        if inner.get("type") == "content_block_delta":
          delta = inner.get("delta", {})
          if delta.get("type") == "text_delta":
            text = delta.get("text", "")
            if text:
              full_assistant_text.append(text)
              yield _sse({"type": "text", "content": text})
        elif inner.get("type") == "content_block_start":
          block = inner.get("content_block", {})
          if block.get("type") == "tool_use":
            yield _sse({
              "type": "tool",
              "name": block.get("name", "?"),
            })
      elif ev_type == "result":
        if event.get("is_error"):
          msg = event.get("result", "Agent reported an error")
          yield _sse({"type": "error", "message": str(msg)})

    rc = await proc.wait()
    if rc != 0:
      stderr_b = await proc.stderr.read() if proc.stderr else b""
      err = stderr_b.decode("utf-8", errors="replace").strip()[:500]
      if err:
        yield _sse({"type": "error", "message": f"CLI exit {rc}: {err}"})

  except Exception as exc:
    yield _sse({
      "type": "error",
      "message": f"Recovery runner crashed: {exc!r}",
    })

  try:
    assistant_text = "".join(full_assistant_text)
    if assistant_text:
      append_log("assistant", assistant_text)
  except Exception:
    pass

  yield _sse({"type": "done"})
