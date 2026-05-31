"""Server-side structural redaction for the gated chat-log read API.

Capability B (design §2). A same-origin mini-app holds the owner JWT,
so this is NOT a sandbox — it is the enforceable half of the consent
model: the gated `/api/chat-logs` surface hands an app a *whitelisted,
structurally redacted* view of a chat, never the raw row. The owner-only
`/api/chats/*` routes keep the raw data.

Pure functions, no I/O, so the route stays thin and this is trivially
unit-testable. The design is a WHITELIST, not a blacklist: we copy out
only the fields a summary consumer needs ({role, text}) and drop
everything else by construction. That makes it safe-by-default as the
message shape grows — a new block type or a new augmentation field is
stripped because it isn't on the keep-list, not because someone
remembered to add it to a deny-list.

What gets stripped, and why each matters (design §2 "Redaction"):
  - tool_use / tool_result / `tool` blocks — carry command output, file
    contents, API responses; the richest leak surface.
  - `thinking` blocks — model chain-of-thought, may quote secrets.
  - `question` / `error` blocks — AskUserQuestion option text + raw
    error strings (often include paths/tokens).
  - attachment metadata + generated images — filenames, paths, mime.
  - hidden + pending messages — internal answer-delivery + not-yet-run
    queue; never part of the visible conversation.
  - the upload-metadata / absolute-fs-path augmentation appended to
    user content before storage (see chats_stream._content_with_uploads:
    the `[Files in this session: - name → /data/...]` block). The UI only
    hides it at render; here we cut it from the stored string.
  - chat titles — derived from the first user message, so they can carry
    the same secrets the body does. The list endpoint scrubs them.

Then a secret-scrub regex runs over the surviving text. Framing
(design §2): `summary` is "reduced exposure," not "safe" — a regex
cannot catch a pasted document, an encoded value, or private prose. The
caps + pagination bound how much survives per response.
"""

from __future__ import annotations

import re

# Per-excerpt char cap for the list view, and per-message char cap for
# the single-chat view. Excerpts are a teaser; full-message bodies in
# the detail view are longer but still bounded so one giant pasted blob
# can't blow up a response. Tuned conservatively until a real consumer
# sets its need (design §2 "Tune to the consumer").
EXCERPT_CHARS = 280
MESSAGE_CHARS = 2000

# Per-response message cap for the single-chat view — newest-first slice
# so a 5000-turn chat returns a bounded page, not the whole transcript.
MAX_MESSAGES_PER_CHAT = 200

# Secret-scrub patterns. These catch SHAPES, not a fixed key list, so a
# new provider's token format is covered as long as it rhymes with one
# of these. Ordered longest/most-specific first so e.g. a JWT isn't
# half-eaten by the generic long-token rule. The replacement keeps the
# label so a reader knows something was removed (observability) without
# leaking the value.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
  # JWTs: three base64url segments separated by dots. The Möbius owner
  # token itself is one of these — the single most important thing to
  # never echo back to an app.
  (re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\b"),
   "[redacted-jwt]"),
  # Provider key prefixes (Anthropic sk-ant-, OpenAI sk-, GitHub ghp_/
  # gho_, Google AIza, Slack xox*, Stripe sk_live/rk_live). The trailing
  # run is the secret body.
  (re.compile(r"\b(?:sk-ant-|sk-|rk_live_|sk_live_|ghp_|gho_|ghu_|ghs_|xox[abprs]-|AIza)[A-Za-z0-9_-]{8,}\b"),
   "[redacted-key]"),
  # Bearer tokens in pasted headers/curl: `Authorization: Bearer <tok>`.
  (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._-]{12,}"),
   r"\1 [redacted-token]"),
  # key=value / key: value assignments whose key names a secret.
  (re.compile(r"(?i)\b(api[_-]?key|secret|token|password|passwd|pwd)\b\s*[:=]\s*\S+"),
   r"\1=[redacted]"),
  # Generic high-entropy long token: 32+ chars of base64url/hex with no
  # whitespace. Last so the labelled rules above win on overlap. Bounded
  # length keeps it from eating an entire pasted paragraph that happens
  # to be one long word.
  (re.compile(r"\b[A-Za-z0-9_+/=-]{32,80}\b"),
   "[redacted-token]"),
]


def scrub_secrets(text: str) -> str:
  """Replaces key/token/JWT-shaped substrings with labelled markers.

  Best-effort "reduced exposure," not a guarantee — see module docstring.
  Applied to every surviving text fragment before it leaves the server.
  """
  if not text:
    return text
  for pattern, repl in _SECRET_PATTERNS:
    text = pattern.sub(repl, text)
  return text


# The upload augmentation block appended to user content before storage
# (chats_stream._content_with_uploads). Matches the literal opener
# `[Files in this session:` through its closing bracket, across lines.
# Stripped whole so absolute /data/ paths + filenames never reach an app.
_UPLOAD_AUG_RE = re.compile(
  r"\n*\[Files in this session:.*?\]", re.DOTALL
)


def strip_upload_augmentation(content: str) -> str:
  """Removes the `[Files in this session: ...]` fs-path block from a
  user message's stored content. No-op when absent."""
  if not content:
    return content
  return _UPLOAD_AUG_RE.sub("", content).rstrip()


def _assistant_text(blocks: list, content: str) -> str:
  """Concatenates ONLY type=="text" assistant blocks.

  Whitelist by construction: tool / thinking / question / error blocks
  are dropped because they aren't `text`. Falls back to the message-level
  `content` string (which `build_assistant_message` fills from the same
  text blocks) when `blocks` is absent — legacy rows predate the blocks
  shape.
  """
  if blocks:
    parts = [
      b.get("content", "")
      for b in blocks
      if isinstance(b, dict) and b.get("type") == "text"
    ]
    return "".join(parts)
  return content or ""


def redact_message(msg: dict) -> dict | None:
  """Reduces one stored message to its whitelisted {role, text} form.

  Returns None when the message must be dropped entirely:
    - hidden user messages (answer-delivery internals),
    - any message whose surviving text is empty after stripping (a turn
      that was pure tool calls leaves nothing a summary should show).

  The output carries no blocks, no attachments, no ts, no timezone, no
  viewport — only the two whitelisted fields, each text-scrubbed.
  """
  if not isinstance(msg, dict):
    return None
  if msg.get("hidden"):
    return None
  role = msg.get("role")
  if role == "assistant":
    text = _assistant_text(msg.get("blocks") or [], msg.get("content", ""))
  else:
    # User (or any non-assistant) message: strip the fs-path
    # augmentation, then scrub. Attachments / timezone / viewport on the
    # source dict are simply not copied.
    text = strip_upload_augmentation(msg.get("content", "") or "")
  text = scrub_secrets(text).strip()
  if not text:
    return None
  return {"role": role or "user", "text": text}


def redact_messages(messages: list, *, newest: int = MAX_MESSAGES_PER_CHAT,
                     char_cap: int = MESSAGE_CHARS) -> list[dict]:
  """Whitelisted, capped, newest-`newest` redacted view of a transcript.

  Drops the same blocks/hidden/pending material `redact_message`
  drops, truncates each surviving text to `char_cap`, and returns at
  most `newest` messages (the tail — most-recent — slice). The cap is a
  structural bound so one chat can't return an unbounded body even at
  `summary` tier.
  """
  out: list[dict] = []
  for msg in messages or []:
    red = redact_message(msg)
    if red is None:
      continue
    if len(red["text"]) > char_cap:
      red["text"] = red["text"][:char_cap] + "…"
    out.append(red)
  if newest and len(out) > newest:
    out = out[-newest:]
  return out


def excerpt_for_chat(messages: list, *, char_cap: int = EXCERPT_CHARS) -> str:
  """A short, redacted teaser for the list view.

  Uses the FIRST surviving user/assistant text (chronological) so the
  excerpt reads like the start of the conversation, scrubbed + truncated.
  Empty string when nothing survives redaction.
  """
  for msg in messages or []:
    red = redact_message(msg)
    if red is None:
      continue
    text = red["text"]
    return text[:char_cap] + "…" if len(text) > char_cap else text
  return ""


def count_visible_messages(messages: list) -> int:
  """How many messages survive redaction — the message_count surfaced in
  the list view. Counts the same whitelist `redact_messages` keeps so the
  number matches what a detail fetch would return (modulo the newest cap)."""
  n = 0
  for msg in messages or []:
    if redact_message(msg) is not None:
      n += 1
  return n
