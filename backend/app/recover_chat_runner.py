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
SUBPROCESS_CWD = "/data"

# Module-level lock serializes stream_turn so a double-click on Send
# (or a network-retry-driven reconnect) doesn't spawn two CLI
# processes that race to write the assistant entry. The lock is
# instantiated lazily because asyncio.Lock requires a running event
# loop at construction time on older Python versions.
_STREAM_LOCK: asyncio.Lock | None = None


def _get_stream_lock() -> asyncio.Lock:
  global _STREAM_LOCK
  if _STREAM_LOCK is None:
    _STREAM_LOCK = asyncio.Lock()
  return _STREAM_LOCK


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


def append_log(role: str, content: str) -> None:
  """Appends one message to the recovery log. JSON-per-line so a
  partial write doesn't corrupt earlier entries."""
  RECOVERY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
  entry = {"role": role, "content": content, "ts": time.time()}
  with RECOVERY_LOG_PATH.open("a", encoding="utf-8") as f:
    f.write(json.dumps(entry, separators=(",", ":")) + "\n")


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


async def stream_turn(user_message: str) -> AsyncIterator[str]:
  """Spawns the Claude CLI for one turn and yields SSE events.

  Serialized via _STREAM_LOCK so a double-click on Send (or a
  network reconnect that fires a duplicate POST) cannot spawn two
  concurrent CLI processes that interleave output and race to
  append assistant entries to the log.
  """
  async with _get_stream_lock():
    async for chunk in _stream_turn_locked(user_message):
      yield chunk


async def _stream_turn_locked(user_message: str) -> AsyncIterator[str]:
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

  # cwd=/data so the agent's relative-path commands in the system
  # prompt resolve consistently. Without this, cwd inherits from
  # uvicorn's launch dir (/app) which contradicts the prompt's
  # `Read /data/recovery_chat.jsonl` references.
  proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env=env,
    cwd=SUBPROCESS_CWD,
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
