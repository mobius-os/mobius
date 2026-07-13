"""Cross-provider chat compaction: a portable plain-text summary.

Native SDK compaction is within-provider only — a Claude session summary
is not a valid Codex thread seed and vice versa. To switch the PROVIDER
of a chat without losing the work so far, we run a one-shot summarize turn
that emits PORTABLE plain text (no provider-specific session handle): a
"compacted chat" the next provider's first turn can replay as an ordinary
prompt prefix.

This module owns just the summarize STEP — produce the text. Storing the
result (through the writer actor) and prepending it to the next turn are
the caller's job (`routes/chats.py` stores; the provider-switch wiring is
a frontend follow-up). Keeping the summarize call here, behind one async
function, means the compaction endpoint can be tested hermetically by
monkeypatching `summarize_chat` — no live provider, no SDK subprocess.

The preferred source is the chat's own cumulative ``## Summary`` note: that is
maintained every turn, is provider-neutral, and has no platform length cap. The
one-shot Claude summarizer below remains a compatibility fallback for legacy
chats that do not yet have a summary note.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger("moebius.chat")

# Cap how much transcript we feed the summarizer. A very long chat would
# otherwise blow the prompt budget; the tail carries the most relevant
# "where we are now" context for continuing the work, so we keep the most
# recent characters rather than the oldest.
_MAX_TRANSCRIPT_CHARS = 60_000

# Timeout for the SDK client connect() call. A stuck DNS lookup or hung
# subprocess launch should fail fast rather than blocking the compaction
# endpoint indefinitely.
_CONNECT_TIMEOUT_SECS = 30.0

# Overall timeout for the entire summarize receive loop. Prevents an
# unresponsive Claude CLI from holding the compaction endpoint open until
# an upstream proxy idle-timeout (typically 300–600 s) forcibly closes it.
# 180 s is well under common proxy idle windows and long enough for any
# realistic summarize response.
_RECEIVE_TIMEOUT_SECS = 180.0

# The summarize instruction. The output is replayed verbatim as the prefix
# of the next provider's first turn, so it must be self-contained prose a
# fresh agent (possibly a different provider, with no session history) can
# pick up from — goal, decisions, current state, and the next step.
_SUMMARIZE_PROMPT = (
  "You are compacting a chat so the work can continue on a DIFFERENT AI "
  "provider that has NONE of this conversation's history. Write a portable, "
  "self-contained plain-text briefing the next agent can read cold and "
  "immediately keep working from. Cover: the user's goal, the key decisions "
  "and constraints agreed so far, the current state of the work (files "
  "touched, what is done), and the next concrete step. Be specific and "
  "factual; do not invent anything not present in the transcript. Output "
  "ONLY the briefing prose — no preamble, no markdown headers, no fences.\n\n"
  "--- TRANSCRIPT ---\n"
)


def load_cumulative_summary(data_dir: str, chat_id: str) -> str | None:
  """Read the chat-maintained unbounded ``## Summary`` section, if present."""
  path = Path(data_dir) / "shared" / "memory" / "chats" / chat_id / "index.md"
  try:
    lines = path.read_text(encoding="utf-8").splitlines()
  except OSError:
    return None
  start: int | None = None
  for index, line in enumerate(lines):
    if line.strip().lower() == "## summary":
      start = index + 1
      break
  if start is None:
    return None
  body: list[str] = []
  for line in lines[start:]:
    if line.strip().startswith("## "):
      break
    body.append(line)
  summary = "\n".join(body).strip()
  return summary or None


def build_transcript_text(messages: list[dict]) -> str:
  """Render the chat's messages into the plain text the summarizer reads.

  Each message becomes a `ROLE: content` line. Tool blocks and other
  non-text structure are dropped — the summary cares about the dialogue
  and the assistant's prose, not the raw tool I/O. The result is tail-
  capped to `_MAX_TRANSCRIPT_CHARS` so a long chat can't overflow the
  prompt budget; the tail is kept because the most recent turns carry the
  "where we are now" context most useful for continuing.
  """
  lines: list[str] = []
  for m in messages or []:
    role = (m.get("role") or "user").upper()
    content = m.get("content") or ""
    if not content.strip():
      continue
    lines.append(f"{role}: {content}")
  text = "\n\n".join(lines)
  if len(text) > _MAX_TRANSCRIPT_CHARS:
    text = text[-_MAX_TRANSCRIPT_CHARS:]
  return text


async def summarize_chat(
  messages: list[dict],
  *,
  data_dir: str,
) -> str:
  """Run a one-shot summarize turn and return the portable briefing text.

  Raises `CompactionError` when the chat has nothing to summarize, when the
  provider is not connected, or when the summarize turn produces no text —
  the caller maps that to a non-2xx and does NOT switch provider or store an
  empty block (a failed summarize must never silently lose context).

  This is the one seam tests monkeypatch: the compaction endpoint awaits it,
  so a stub returning canned text makes the route hermetic.
  """
  transcript = build_transcript_text(messages)
  if not transcript.strip():
    raise CompactionError("Nothing to compact — the chat has no content yet.")

  from app.providers import get_provider

  provider = get_provider("claude")
  auth_error = provider.check_auth(data_dir)
  if auth_error is not None:
    raise CompactionError(auth_error)
  # Refresh the OAuth token before spawning the summarize CLI, exactly as the
  # live chat path does (chat.py). The summarize turn is a full CLI subprocess
  # too, so without this it can hand an at-spawn-expired token to the CLI and
  # 401 — the same intermittent failure ensure_auth fixes for chat turns.
  # Best effort: a refresh blip must not block compaction.
  await provider.ensure_auth(data_dir)

  summary = await _run_summarize_turn(
    _SUMMARIZE_PROMPT + transcript, data_dir=data_dir
  )
  summary = (summary or "").strip()
  if not summary:
    raise CompactionError(
      "The summarize turn returned no text; not compacting."
    )
  return summary


async def _run_summarize_turn(prompt: str, *, data_dir: str) -> str:
  """Run a fresh, tool-free Claude SDK query and return its final text.

  Isolated from the live chat runner on purpose: a fresh `ClaudeSDKClient`
  with no `resume`, no tools that mutate state, and no broadcast — so it
  can't touch the chat's session, run markers, or SSE stream. Only the
  assistant's text blocks are collected; everything else is ignored.
  """
  from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient
  from claude_agent_sdk.types import AssistantMessage, TextBlock

  from app.providers import get_provider

  env = get_provider("claude").build_env(base_env={}, data_dir=data_dir)
  options = ClaudeAgentOptions(
    env=env,
    cli_path="/usr/local/bin/claude",
    include_partial_messages=False,
  )
  client = ClaudeSDKClient(options)
  parts: list[str] = []
  try:
    await asyncio.wait_for(client.connect(), timeout=_CONNECT_TIMEOUT_SECS)
  except asyncio.TimeoutError:
    raise CompactionError(
      f"Compaction client connect timed out after {_CONNECT_TIMEOUT_SECS:.0f}s."
    )
  try:
    async with asyncio.timeout(_RECEIVE_TIMEOUT_SECS):
      await client.query(prompt)
      async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
          for block in msg.content:
            if isinstance(block, TextBlock):
              parts.append(block.text)
  except asyncio.TimeoutError:
    raise CompactionError(
      f"Compaction receive loop timed out after {_RECEIVE_TIMEOUT_SECS:.0f}s."
    )
  finally:
    await client.disconnect()
  return "".join(parts)


class CompactionError(Exception):
  """The summarize turn could not produce a usable briefing.

  Raised for an empty chat, a disconnected provider, or a summarize turn
  that returned no text. The compaction endpoint maps it to a 4xx/502 and
  declines to store a block or switch provider — a failed summarize must
  not lose the user's context silently.
  """
