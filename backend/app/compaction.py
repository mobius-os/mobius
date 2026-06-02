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

The summarize turn deliberately reuses the Claude SDK (the chat's own
provider is irrelevant: the summary is plain text either way, and Claude
is always the platform's baseline provider). It is a fresh, tool-free,
non-streaming query — NOT the live chat session — so it never touches the
chat's `session_id`, run markers, or broadcast.
"""

from __future__ import annotations

import logging

log = logging.getLogger("moebius.chat")

# Cap how much transcript we feed the summarizer. A very long chat would
# otherwise blow the prompt budget; the tail carries the most relevant
# "where we are now" context for continuing the work, so we keep the most
# recent characters rather than the oldest.
_MAX_TRANSCRIPT_CHARS = 60_000

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
  await client.connect()
  try:
    await client.query(prompt)
    async for msg in client.receive_response():
      if isinstance(msg, AssistantMessage):
        for block in msg.content:
          if isinstance(block, TextBlock):
            parts.append(block.text)
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
