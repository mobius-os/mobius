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
import time
from pathlib import Path
from typing import AsyncIterator


RECOVERY_LOG_PATH = Path("/data/recovery_chat.jsonl")
CLAUDE_CONFIG_PATH = Path("/data/cli-auth/claude")


def _sse(event: dict) -> str:
  """Encodes an event as a single SSE message line."""
  return f"data: {json.dumps(event, separators=(',', ':'))}\n\n"


def _system_prompt() -> str:
  """Recovery agent instructions. Minimal — agent is here to fix."""
  return (
    "You are running inside the Mobius recovery chat. The user has "
    "reached you here because something in the platform is broken "
    "and they need help fixing it. You have elevated write access "
    "to /app/app/ (backend), /app/scripts/ (utility scripts), and "
    "/data/shell/ (frontend source). Recovery routes themselves "
    "(/app/app/routes/recover*.py, /app/app/recover_chat*.py, "
    "/app/app/recover_auth.py, /app/scripts/entrypoint.sh, "
    "/app/scripts/recovery_restore.sh) are frozen (chmod 444) and "
    "cannot be edited.\n\n"
    "Workflow:\n"
    "1. Read /data/logs/chat.log and use git diff against "
    "/app/app-baked/ or /app/shell-src/ to understand what changed.\n"
    "2. Make the fix.\n"
    "3. Ask the user to click Restart in the recovery chat UI "
    "(triggers POST /recover/restart) so uvicorn reloads the code.\n"
    "4. After verifying the fix, append a Lesson to "
    "/data/shared/agent-experience.md describing what went wrong "
    "and how to avoid it.\n\n"
    "If you cannot fix it, tell the user to use the Restore "
    "buttons in the main recovery page (/recover) -- that is the "
    "last-resort reset using /app/app-baked/ or /app/shell-src/."
  )


def append_log(role: str, content: str) -> None:
  """Appends one message to the recovery log. JSON-per-line so a
  partial write doesn't corrupt earlier entries."""
  RECOVERY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
  entry = {"role": role, "content": content, "ts": time.time()}
  with RECOVERY_LOG_PATH.open("a", encoding="utf-8") as f:
    f.write(json.dumps(entry, separators=(",", ":")) + "\n")


def load_log() -> list[dict]:
  """Returns all logged messages in order, or [] if no log yet."""
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


async def stream_turn(user_message: str) -> AsyncIterator[str]:
  """Spawns the Claude CLI for one turn and yields SSE events."""
  claude_bin = shutil.which("claude")
  if not claude_bin:
    yield _sse({"type": "error", "message": "claude CLI not found in PATH"})
    yield _sse({"type": "done"})
    return

  env = dict(os.environ)
  if CLAUDE_CONFIG_PATH.is_dir():
    env["CLAUDE_CONFIG_DIR"] = str(CLAUDE_CONFIG_PATH)

  cmd = [
    claude_bin,
    "--print",
    "--output-format", "stream-json",
    "--verbose",
    "--include-partial-messages",
    "--dangerously-skip-permissions",
    "--system-prompt", _system_prompt(),
    "--", user_message,
  ]

  proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env=env,
  )

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
  finally:
    if proc.returncode is None:
      proc.kill()
      await proc.wait()

  try:
    assistant_text = "".join(full_assistant_text)
    if assistant_text:
      append_log("assistant", assistant_text)
  except Exception:
    pass

  yield _sse({"type": "done"})
