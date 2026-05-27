"""Minimal CLI runner for the recovery chat.

Deliberately does NOT share code with app.chat, app.providers, or
the SDK runners. Those are the production chat path; if the agent
broke them, recovery still needs to work. This module is small,
frozen (chmod 444 via protected-files.txt), and imports only
stdlib.

What it does:
- Spawns either the Claude CLI (`claude --print --output-format
  stream-json`) or the Codex CLI (`codex exec --json`) via
  asyncio.create_subprocess_exec (args as list, no shell)
- Parses each stdout JSON line into a small set of events
- Yields them as SSE-formatted strings for the recovery chat page
- Appends each user + assistant turn to /data/recovery_chat.jsonl
  (append-only log; survives if the chats DB schema is broken)

What it does NOT do:
- AskUserQuestion (user can just type)
- Multi-turn resume (the agent reads the log file itself for context)
- Stop / cancel mid-stream (refresh to abandon)
- Per-token typewriter (Claude path streams text deltas; Codex
  emits the assistant message in one chunk at turn end)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path
from typing import AsyncIterator


# Multi-chat layout: one jsonl file per chat under RECOVERY_CHATS_DIR.
# First line of each file is a metadata record: `{"_meta": {...}}`.
# Subsequent lines are role/content entries appended at runtime.
# chat_id is a short hex id (12 chars) generated when the chat is
# created; the file is `<chat_id>.jsonl`.
#
# RECOVERY_LOG_PATH is the LEGACY single-file path. If present at
# list_chats() time it gets migrated into the chats dir as
# `legacy.jsonl` so prior recovery history isn't orphaned.
RECOVERY_LOG_PATH = Path("/data/recovery_chat.jsonl")
RECOVERY_CHATS_DIR = Path("/data/recovery/chats")
CLAUDE_CONFIG_PATH = Path("/data/cli-auth/claude")
CODEX_CONFIG_PATH = Path("/data/cli-auth/codex")

# chat_id must be alphanumeric (plus dash/underscore) to prevent path
# traversal in chat_log_path. 64-char cap matches what the create_chat
# generator produces (12 chars); we allow longer for backward
# compatibility with manually-named files like "legacy".
_CHAT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SUBPROCESS_CWD = "/data"

# Supported providers for the recovery chat. Keep ordered: the default
# resolution prefers the first available entry.
SUPPORTED_PROVIDERS: tuple[str, ...] = ("claude", "codex")


def provider_status() -> dict[str, bool]:
  """Returns {provider_name: is_configured} for each supported provider.

  A provider is "configured" if its credential directory has the
  expected auth file. `.credentials.json` for Claude, `auth.json`
  for Codex — these are what the respective CLIs read at spawn time,
  so their presence is a reasonable proxy for "the CLI will start
  without an interactive login prompt." False negative is possible
  if the file exists but is corrupted; the spawn will then error
  and the user sees a meaningful message.
  """
  return {
    "claude": (CLAUDE_CONFIG_PATH / ".credentials.json").is_file(),
    "codex": (CODEX_CONFIG_PATH / "auth.json").is_file(),
  }


def default_provider() -> str:
  """Returns the first configured provider, preferring claude.

  Falls back to the first SUPPORTED_PROVIDERS entry when nothing is
  configured (the spawn will then fail with 'claude CLI not found'
  or 'auth missing' — a meaningful error for the user).
  """
  status = provider_status()
  for name in SUPPORTED_PROVIDERS:
    if status.get(name):
      return name
  return SUPPORTED_PROVIDERS[0]

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


def _claim_run(chat_id: str | None = None) -> dict | None:
  """Atomically claim the run slot.

  Returns a fresh claim dict on success, or None if a turn is
  already in flight. The claim has `proc: None` initially; the
  caller fills it after spawning the subprocess so the cleanup path
  can find the process to terminate.

  `chat_id` is recorded on the claim so `terminate_active_run_for`
  can decide whether the active subprocess belongs to a specific
  chat (e.g. when that chat is deleted mid-stream).
  """
  global _current_run
  with _run_lock:
    if _current_run is not None:
      return None
    _current_run = {"proc": None, "chat_id": chat_id}
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


def terminate_active_run_for(chat_id: str) -> bool:
  """Kills the active recovery subprocess IF it's for `chat_id`.

  Called from delete_chat — if the user deletes a chat that
  currently has a rescue agent running on it, the agent's output
  has nowhere to land (the log file is being unlinked), so kill
  it. Other chats' rescue agents are left alone.

  Returns True if a matching run was killed, False if there was
  nothing to terminate or the active run is for a different chat.

  Atomicity: we set `claim["cancelled"]`, read `claim["proc"]`, and
  clear `_current_run` all under the same lock acquisition. This
  matters during the spawn-startup window where `_claim_run` has
  installed the claim but the subprocess hasn't been attached yet.
  In that window the spawn task does its OWN `claim["cancelled"]`
  check under the same lock before publishing `proc`; whichever
  path acquires the lock first wins, and the other path either
  kills the just-published proc (delete-after-publish) or skips
  publishing entirely (delete-before-publish). Codex review caught
  the original race where claim["proc"] was None at delete time and
  the not-yet-attached subprocess still started and ran against a
  deleted chat.
  """
  global _current_run
  with _run_lock:
    claim = _current_run
    if claim is None or claim.get("chat_id") != chat_id:
      return False
    claim["cancelled"] = True
    proc = claim.get("proc")
    _current_run = None
  if proc is not None:
    try:
      proc.kill()
    except (ProcessLookupError, OSError):
      pass
  return True


def terminate_active_run() -> bool:
  """Kills the active recovery subprocess (if any) and frees the slot.

  Called from destructive admin actions (factory reset, restart)
  where we want the running rescue agent to STOP — not just be
  walled out of future endpoints — because it may still be in the
  middle of a tool call that writes to disk.

  Codex review caught the gap: `_require_session` re-checks the
  owner row before each HTTP request, but a stream that's ALREADY
  running keeps its subprocess alive. After a factory reset that
  blows away credentials and data, the in-flight rescue agent
  retains elevated write access until it naturally exits.

  Returns True if a run was active and got terminated, False if
  there was nothing to kill. Pure sync — the subprocess kill is
  fire-and-forget (the OS reaps it); the stream generator's own
  finally will release the slot when its next await wakes up.
  """
  global _current_run
  with _run_lock:
    claim = _current_run
  if claim is None:
    return False
  proc = claim.get("proc")
  if proc is not None:
    try:
      proc.kill()
    except (ProcessLookupError, OSError):
      pass
  # Force-clear the slot so a new request can start immediately, even
  # if the stream generator's own cleanup hasn't run yet. Belt-and-
  # braces — the generator's finally also clears it.
  with _run_lock:
    if _current_run is claim:
      _current_run = None
  return True


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


def _system_prompt(chat_id: str | None = None) -> str:
  """Recovery agent instructions. Minimal — agent is here to fix.

  `chat_id` is interpolated into the "read prior turns" instruction
  so the agent knows which per-chat log to read. Multi-chat moved
  the log from a single /data/recovery_chat.jsonl to per-chat files
  at /data/recovery/chats/<chat_id>.jsonl; older versions of this
  prompt hardcoded the legacy path and silently broke multi-turn
  context after the migration (Claude review caught it).

  Updated 2026-05-26 per multi-reviewer findings:
    - `diff -ru` not `git diff`: /app/app-baked/ has no .git
    - cwd is /data/, set in stream_turn's subprocess call
    - read prior turns from the log file before answering
    - no AskUserQuestion (recovery UI has only a textarea)
    - no API calls (no AGENT_TOKEN, no API_BASE_URL set here —
      this is intentionally filesystem-only)
  Updated 2026-05-26 (later): take chat_id, interpolate the per-
  chat log path so the agent reads the right file.
  """
  if chat_id:
    prior_turns = (
      f"BEFORE answering, run `Read /data/recovery/chats/{chat_id}.jsonl` "
      "to see prior turns in this session. Each line is a JSON object "
      "with role + content; the FIRST line is a `_meta` record (provider "
      "+ created_at) — skip it. The CLI is invoked fresh per turn so "
      "there is no in-process memory of earlier exchanges."
    )
  else:
    # Defensive fallback for ad-hoc invocations without a chat_id
    # (tests, internal tooling). The HTTP layer always supplies
    # one. Don't emit a `Read <glob>` instruction here — the Read
    # tool would treat the glob as a literal filename and waste a
    # tool call on a guaranteed file-not-found. Tell the agent to
    # list the directory instead. Codex review noted this fallback
    # is unreachable from production but the prior wording could
    # mislead any non-HTTP caller.
    prior_turns = (
      "Prior turns in this session live under /data/recovery/chats/ "
      "as one .jsonl per chat. List that directory to find the right "
      "file. Each line in a chat file is a JSON object with role + "
      "content; the FIRST line is a `_meta` record — skip it."
    )
  return (
    "You are running inside the Mobius recovery chat. The user has "
    "reached you here because something in the platform is broken "
    "and they need help fixing it.\n\n"
    + prior_turns + "\n\n"
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
    "  /app/app/config.py                 env settings\n"
    "  /app/app/models.py                 SQLAlchemy table defs\n"
    "  /app/app/routes/recover.py         recovery page\n"
    "  /app/app/routes/recover_html.py    recovery HTML\n"
    "  /app/app/recover_chat.py           this chat's endpoints\n"
    "  /app/app/recover_chat_runner.py    this runner\n"
    "  /app/app/recover_auth.py           recovery auth\n"
    "  /app/app/recover_oauth.py          recovery OAuth\n"
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


# ---------------------------------------------------------------------
# Multi-chat layout
# ---------------------------------------------------------------------
# Each recovery chat is a single jsonl file under RECOVERY_CHATS_DIR.
# The first line is a metadata record: `{"_meta": {provider, created_at, ...}}`.
# Subsequent lines are message entries: `{"role": "user|assistant",
# "content": "...", "ts": ...}`.
#
# Why a file-per-chat:
#  - Atomic appends per chat (no cross-chat lock contention)
#  - Simple list = ls the directory
#  - Reset a single chat without affecting others
#  - Backup / inspect with cat or `jq`
#
# Why the metadata first line (not a separate meta.json):
#  - One file per chat keeps the on-disk layout obvious
#  - The first line is read-only after creation; no append-time
#    coordination needed
#  - turn_id is the line's zero-based position (so the meta line is
#    turn_id=0; the first user message is turn_id=1). The runner
#    skips _meta on reads.


def _validate_chat_id(chat_id: str) -> None:
  """Raises ValueError on a chat_id that doesn't match the allowed
  shape. The shape is restrictive enough to prevent path traversal
  (no '..', no '/'). Allowed: alphanumeric, dash, underscore.
  """
  if not isinstance(chat_id, str) or not _CHAT_ID_RE.match(chat_id):
    raise ValueError(f"invalid chat_id: {chat_id!r}")


def chat_log_path(chat_id: str) -> Path:
  """Returns the on-disk path for `chat_id`'s log file.

  Validates chat_id to prevent path traversal. Caller is responsible
  for checking the file exists if that's required — this function
  just computes the path.
  """
  _validate_chat_id(chat_id)
  return RECOVERY_CHATS_DIR / f"{chat_id}.jsonl"


def _read_meta(path: Path) -> dict | None:
  """Reads the first line of a chat file and returns its `_meta` dict.

  Returns None if the file is missing, empty, the first line isn't
  valid JSON, or the JSON doesn't carry a `_meta` key. Used by both
  list_chats (to render metadata) and get_chat_provider.
  """
  if not path.is_file():
    return None
  try:
    with path.open("r", encoding="utf-8") as f:
      first = f.readline().strip()
    if not first:
      return None
    data = json.loads(first)
  except (json.JSONDecodeError, OSError):
    return None
  if isinstance(data, dict) and isinstance(data.get("_meta"), dict):
    return data["_meta"]
  return None


def _migrate_legacy_log() -> None:
  """Moves the pre-multi-chat single-log file into the chats dir.

  Called by list_chats() so the migration is lazy — no startup hook,
  no entrypoint change. Idempotent: if `legacy.jsonl` already exists
  in the chats dir, the legacy file is left alone (the user's
  decision to delete or keep it). The migrated file gets a synthetic
  `_meta` prepended with provider="claude" (the only option the
  legacy runner supported) and migrated_from_legacy=True so the UI
  can label it distinctively if it wants to.
  """
  if not RECOVERY_LOG_PATH.is_file():
    return
  RECOVERY_CHATS_DIR.mkdir(parents=True, exist_ok=True)
  target = RECOVERY_CHATS_DIR / "legacy.jsonl"
  if target.exists():
    return
  # Atomic write: copy to <target>.partial first, then rename onto
  # target only if the copy succeeded. Without this, a mid-copy OSError
  # (disk full, etc.) would leave a partial legacy.jsonl that future
  # `list_chats()` calls treat as "already migrated" — silently skipping
  # the rest of the legacy history forever. Codex caught this in review.
  tmp = RECOVERY_CHATS_DIR / "legacy.jsonl.partial"
  try:
    mtime = RECOVERY_LOG_PATH.stat().st_mtime
    meta = {
      "_meta": {
        "provider": "claude",
        "created_at": mtime,
        "migrated_from_legacy": True,
      }
    }
    with RECOVERY_LOG_PATH.open("r", encoding="utf-8") as src, \
        tmp.open("w", encoding="utf-8") as dst:
      dst.write(json.dumps(meta, separators=(",", ":")) + "\n")
      shutil.copyfileobj(src, dst)
    # Atomic rename within the same directory — either the new
    # legacy.jsonl exists complete or it doesn't.
    tmp.replace(target)
    RECOVERY_LOG_PATH.unlink()
  except OSError:
    # Best-effort migration. Clean up the partial so the next call
    # can re-attempt with a fresh copy rather than thinking it's done.
    try:
      tmp.unlink()
    except OSError:
      pass


def list_chats() -> list[dict]:
  """Returns a list of chat metadata dicts, most-recently-touched first.

  Each entry: {chat_id, provider, created_at, mtime, migrated_from_legacy?}.
  Lazily migrates the legacy single-log if it exists, so callers
  always see the unified view.
  """
  _migrate_legacy_log()
  if not RECOVERY_CHATS_DIR.is_dir():
    return []
  out: list[dict] = []
  for p in RECOVERY_CHATS_DIR.glob("*.jsonl"):
    meta = _read_meta(p)
    if not meta:
      # File missing _meta — skip rather than crash. A malformed file
      # could be the result of an aborted create_chat or manual edit.
      continue
    out.append({
      "chat_id": p.stem,
      "provider": meta.get("provider"),
      "created_at": meta.get("created_at"),
      "migrated_from_legacy": meta.get("migrated_from_legacy", False),
      "mtime": p.stat().st_mtime,
    })
  out.sort(key=lambda m: m.get("mtime") or 0, reverse=True)
  return out


def create_chat(provider: str) -> str:
  """Creates a new chat with the given provider, returns its chat_id.

  chat_id is a 12-char hex string (48 bits of entropy — unguessable
  for any practical attack; 1 in 2.8e14 collision per chat). The
  created file has a single line: the `_meta` record. Subsequent
  append_log calls add user/assistant messages.
  """
  if provider not in SUPPORTED_PROVIDERS:
    raise ValueError(
      f"unsupported provider: {provider}; expected one of {SUPPORTED_PROVIDERS}"
    )
  RECOVERY_CHATS_DIR.mkdir(parents=True, exist_ok=True)
  # Retry on the astronomically-unlikely collision so we never
  # silently overwrite an existing chat.
  for _ in range(5):
    chat_id = secrets.token_hex(6)
    path = RECOVERY_CHATS_DIR / f"{chat_id}.jsonl"
    if path.exists():
      continue
    meta = {
      "_meta": {"provider": provider, "created_at": time.time()}
    }
    # Open with O_CREAT|O_EXCL via Python's "x" mode so a parallel
    # collision raises FileExistsError rather than silently clobber.
    try:
      with path.open("x", encoding="utf-8") as f:
        f.write(json.dumps(meta, separators=(",", ":")) + "\n")
      return chat_id
    except FileExistsError:
      continue
  raise RuntimeError("could not allocate unique chat_id after retries")


def delete_chat(chat_id: str) -> bool:
  """Deletes a chat's log file. Returns True if the file existed.

  Used by the recovery UI's "delete chat" affordance. Validates
  chat_id (path traversal defense). Failures other than 'missing'
  raise — the UI surfaces those as errors so the user knows the
  chat wasn't actually deleted.
  """
  path = chat_log_path(chat_id)
  if not path.is_file():
    return False
  path.unlink()
  return True


def get_chat_provider(chat_id: str) -> str | None:
  """Returns the provider name a chat was created with, or None.

  None means the chat doesn't exist or its _meta line is missing.
  The HTTP layer uses this to decide whether to default the picker
  to a specific provider when opening an existing chat.
  """
  meta = _read_meta(chat_log_path(chat_id))
  if not meta:
    return None
  return meta.get("provider")


# ---------------------------------------------------------------------
# Per-chat log operations — all take chat_id as the first argument.
# turn_id is the message's zero-based line index, which means turn_id=0
# is always the _meta line (skipped on user-message lookups) and the
# first real user message has turn_id=1.
# ---------------------------------------------------------------------


def append_log(chat_id: str, role: str, content: str) -> int:
  """Appends a message to `chat_id`'s log; returns its turn_id (line index).

  turn_id is the line's zero-based position in the file, used to pair
  /send and /stream requests so a multi-tab user can't mis-route a
  response.

  The append + index-derivation runs under `_append_lock` so two
  concurrent callers cannot both observe the same final line count.
  Without the lock, both would return the same turn_id and both
  /stream POSTs would resolve to the same log row.

  Note: the lock is GLOBAL, not per-chat. Concurrent appends across
  different chats serialize, which is fine — recovery is single-
  owner and traffic is low. Per-chat locks would add complexity
  for no real benefit.
  """
  path = chat_log_path(chat_id)
  if not path.is_file():
    raise ValueError(f"chat {chat_id} not found")
  entry = {"role": role, "content": content, "ts": time.time()}
  payload = json.dumps(entry, separators=(",", ":")) + "\n"
  with _append_lock:
    try:
      with path.open("a+", encoding="utf-8") as f:
        f.write(payload)
        f.flush()
        f.seek(0)
        total = sum(1 for _ in f)
      return total - 1
    except Exception:
      return -1


def user_message_by_id(chat_id: str, turn_id: int) -> str | None:
  """Returns the content of the message at `turn_id` if it's a user
  message in this chat. None for any other case (missing, wrong role,
  _meta line, malformed). The /stream handler 400s on None so the
  client sees a clean error rather than streaming the wrong message.
  """
  if turn_id < 0:
    return None
  path = chat_log_path(chat_id)
  if not path.is_file():
    return None
  with path.open("r", encoding="utf-8") as f:
    for i, line in enumerate(f):
      if i != turn_id:
        continue
      line = line.strip()
      if not line:
        return None
      try:
        entry = json.loads(line)
      except json.JSONDecodeError:
        return None
      if entry.get("role") != "user":
        # _meta lines and assistant lines both hit this branch.
        return None
      return entry.get("content") or None
  return None


def load_log(
  chat_id: str, limit: int | None = MAX_RENDERED_MESSAGES,
) -> list[dict]:
  """Returns the chat's messages in order, capped at `limit`.

  Skips the _meta first line (it's not a user/assistant message).
  Default cap is MAX_RENDERED_MESSAGES so a long repair session
  can't degrade the recovery page render.
  """
  path = chat_log_path(chat_id)
  if not path.is_file():
    return []
  out: list[dict] = []
  with path.open("r", encoding="utf-8") as f:
    for line in f:
      line = line.strip()
      if not line:
        continue
      try:
        entry = json.loads(line)
      except json.JSONDecodeError:
        continue
      # Skip the metadata line; it isn't a chat message.
      if isinstance(entry, dict) and "_meta" in entry and "role" not in entry:
        continue
      out.append(entry)
  if limit is not None and len(out) > limit:
    out = out[-limit:]
  return out


def reset_log(chat_id: str) -> None:
  """Truncates the chat to just its _meta line (keeps provider association).

  Used by the per-chat "Reset" button so the user can clear the
  conversation while keeping the same chat slot. To delete the chat
  entirely, call delete_chat instead.
  """
  path = chat_log_path(chat_id)
  if not path.is_file():
    return
  meta = _read_meta(path)
  if meta is None:
    # No recoverable metadata — fall back to a default so the file
    # remains a valid chat after reset.
    meta = {"provider": "claude", "created_at": time.time()}
  with path.open("w", encoding="utf-8") as f:
    f.write(json.dumps({"_meta": meta}, separators=(",", ":")) + "\n")


def latest_user_message(chat_id: str) -> str | None:
  """Returns the most recent user message in the chat, or None.

  Used as a fallback when /stream is invoked without a turn_id
  (legacy clients). The current client always sends turn_id, so
  this is rarely hit in practice.
  """
  path = chat_log_path(chat_id)
  if not path.is_file():
    return None
  last = None
  with path.open("r", encoding="utf-8") as f:
    for line in f:
      line = line.strip()
      if not line:
        continue
      try:
        entry = json.loads(line)
      except json.JSONDecodeError:
        continue
      if entry.get("role") == "user":
        last = entry.get("content")
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


async def stream_turn(
  user_message: str,
  provider: str | None = None,
  chat_id: str | None = None,
) -> AsyncIterator[str]:
  """Spawns the rescue CLI for one turn and yields SSE events.

  `provider` is 'claude' or 'codex' (or None → default_provider()).
  `chat_id` identifies which chat's log gets the assistant entry;
  None means don't persist (used by ad-hoc / test paths). When
  provided, the spawn function appends the final assistant text via
  `append_log(chat_id, "assistant", ...)`.

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
  claim = _claim_run(chat_id=chat_id)
  if claim is None:
    yield _sse({
      "type": "error",
      "message": "Another recovery turn is in progress.",
    })
    yield _sse({"type": "done"})
    return

  try:
    chosen = provider or default_provider()
    async for chunk in _stream_turn_impl(user_message, claim, chosen, chat_id):
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
  user_message: str, claim: dict, provider: str, chat_id: str | None,
) -> AsyncIterator[str]:
  """Dispatches to the per-provider spawn function and forwards events."""
  if provider == "codex":
    async for ev in _spawn_codex(user_message, claim, chat_id):
      yield ev
    return
  # Default: Claude. Unknown provider names also fall through to Claude
  # so a typo doesn't silently produce zero output.
  async for ev in _spawn_claude(user_message, claim, chat_id):
    yield ev


async def _spawn_claude(
  user_message: str, claim: dict, chat_id: str | None,
) -> AsyncIterator[str]:
  """Spawns the Claude CLI, writes the message to stdin, streams stdout.

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
    "--system-prompt", _system_prompt(chat_id),
  ]

  # cwd=/data so the agent's relative-path commands in the system
  # prompt resolve consistently. Without this, cwd inherits from
  # uvicorn's launch dir (/app) which contradicts the prompt's
  # `Read /data/recovery/chats/<chat_id>.jsonl` references.
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
  # Atomic with the cancellation check so a delete_chat that landed
  # during the await above doesn't slip past — see the matching
  # comment in terminate_active_run_for.
  with _run_lock:
    if claim.get("cancelled"):
      cancelled_during_spawn = True
    else:
      claim["proc"] = proc
      cancelled_during_spawn = False
  if cancelled_during_spawn:
    try:
      proc.kill()
    except (ProcessLookupError, OSError):
      pass
    yield _sse({"type": "done"})
    return

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
      if chat_id:
        append_log(chat_id, "assistant", assistant_text)
  except Exception:
    pass

  yield _sse({"type": "done"})


async def _spawn_codex(
  user_message: str, claim: dict, chat_id: str | None,
) -> AsyncIterator[str]:
  """Spawns the Codex CLI for one turn and yields SSE events.

  Unlike Claude's stream-json output, `codex exec --json` does not
  emit per-token deltas — it ends a turn with a single
  `item.completed` event whose `item.text` is the full assistant
  message. So this path yields ONE big `text` event at turn end
  rather than streaming chunks. Acceptable for recovery — the user
  is rescuing a broken instance, they don't need typewriter UX.

  Message goes via stdin (the trailing `-` argv) so long pastes
  don't hit Linux's argv cap, mirroring the Claude path.
  """
  codex_bin = shutil.which("codex")
  if not codex_bin:
    yield _sse(
      {"type": "error", "message": "codex CLI not found in PATH"}
    )
    yield _sse({"type": "done"})
    return

  env = dict(os.environ)
  env["CODEX_HOME"] = str(CODEX_CONFIG_PATH)

  cmd = [
    codex_bin, "exec", "--json",
    "--dangerously-bypass-approvals-and-sandbox",
    "--skip-git-repo-check",
    "-",  # explicit stdin marker
  ]

  proc = await asyncio.create_subprocess_exec(
    *cmd,
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.PIPE,
    env=env,
    cwd=SUBPROCESS_CWD,
  )
  # Atomic publish + cancellation check under the run lock; mirrors
  # _spawn_claude. Without this guard a delete_chat that landed
  # during the spawn await would clear _current_run but the not-yet-
  # attached proc would keep running against the deleted chat.
  with _run_lock:
    if claim.get("cancelled"):
      cancelled_during_spawn = True
    else:
      claim["proc"] = proc
      cancelled_during_spawn = False
  if cancelled_during_spawn:
    try:
      proc.kill()
    except (ProcessLookupError, OSError):
      pass
    yield _sse({"type": "done"})
    return

  # Codex `exec --json` has no separate --system-prompt flag — the
  # stdin payload IS the prompt. So prepend the recovery system
  # prompt to the user message, separated by a clear marker so the
  # model treats them as distinct intents. Without this, the Codex
  # rescue agent has no context about the recovery surface (write
  # surface, frozen island, per-chat log path) and behaves like a
  # bare codex session — Claude reviewer flagged this gap.
  assert proc.stdin is not None
  combined = (
    _system_prompt(chat_id)
    + "\n\n---\n\nUser message follows:\n\n"
    + user_message
  )
  try:
    proc.stdin.write(combined.encode("utf-8"))
    await proc.stdin.drain()
  except (BrokenPipeError, ConnectionResetError):
    pass
  finally:
    try:
      proc.stdin.close()
    except Exception:
      pass

  full_assistant_text: list[str] = []
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
      if ev_type == "item.completed":
        item = event.get("item", {}) or {}
        item_type = item.get("type")
        if item_type == "agent_message":
          text = item.get("text", "")
          if text:
            full_assistant_text.append(text)
            yield _sse({"type": "text", "content": text})
        elif item_type in ("tool_use", "command_execution", "commandExecution"):
          # Codex tool events: emit a minimal "tool" event so the UI
          # can show a "▸ Tool: <name>" hint. Best-effort name
          # extraction across the few shapes the CLI emits.
          name = (
            item.get("name")
            or item.get("command")
            or item_type
          )
          yield _sse({"type": "tool", "name": str(name)[:80]})

    rc = await proc.wait()
    if rc != 0:
      stderr_b = await proc.stderr.read() if proc.stderr else b""
      err = stderr_b.decode("utf-8", errors="replace").strip()[:500]
      if err:
        yield _sse(
          {"type": "error", "message": f"CLI exit {rc}: {err}"}
        )

  except Exception as exc:
    yield _sse({
      "type": "error",
      "message": f"Recovery runner crashed: {exc!r}",
    })

  try:
    assistant_text = "".join(full_assistant_text)
    if assistant_text:
      if chat_id:
        append_log(chat_id, "assistant", assistant_text)
  except Exception:
    pass

  yield _sse({"type": "done"})
