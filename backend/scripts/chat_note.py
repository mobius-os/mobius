#!/usr/bin/env python3
"""Ensure a chat's memory note exists + is current — the platform backstop.

The agent is instructed (core.md) to maintain `chats/<id>/index.md` every turn —
a growing summary + the partner's facts & intent + the one-line gist that IS the
chat name. But it does so VARIABLY: on some turns it jumps straight to answering
and skips the note. This runner is the turn-end guarantee the platform fires when
the agent left the note missing or stale, so EVERY substantive chat ends up with
a current note regardless of agent compliance.

Tool-free by design (the anti-exfil pattern — see the agent-tool-scope memory):
the summarizer subagent gets the transcript in its PROMPT and runs with NO tools
(it only PRODUCES the note text). THIS script does the privileged writes — the
note file and the title PATCH. So a prompt-injected chat can't make the subagent
write outside the note or exfiltrate anything.

Usage: chat_note.py <chat_id>
Exit 0 ok (or nothing-to-do) · 2 bad args. Best-effort: never raises into the
caller — a failed note must never break or slow the turn that triggered it.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.request
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB = DATA_DIR / "db" / "ultimate.db"
MEMORY_DIR = DATA_DIR / "shared" / "memory"
CLAUDE_CONFIG_DIR = DATA_DIR / "cli-auth" / "claude"
CLI_PATH = "/usr/local/bin/claude"
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
SERVICE_TOKEN_FILE = DATA_DIR / "service-token.txt"

# A fast model is right for a per-skipped-turn backstop; override if needed.
MODEL = os.environ.get("CHAT_NOTE_MODEL", "claude-sonnet-4-6")
TIMEOUT_SECS = int(os.environ.get("CHAT_NOTE_TIMEOUT", "120"))
# Trim the transcript fed to the summarizer so a long chat can't blow the prompt.
MAX_TRANSCRIPT_BYTES = 12000

SYSTEM_PROMPT = """\
You write the MEMORY NOTE for one Möbius chat. You are given the chat transcript
and (if it already exists) the current note. Produce the UPDATED note and NOTHING
else — no preamble, no code fences, no commentary.

The note is the chat's durable memory. Its exact shape:

---
type: chat
description: <one-line gist of the chat in the partner's own words — this IS the
chat's name, e.g. "dialing in a sour espresso shot", not "chat 12">
---
## Summary
<a couple of paragraphs: what this chat is about and what it has produced (an app
built, a decision made, a preference learned), recency-biased for a long chat>

## Facts & intent
- <each durable fact the partner gave — a preference, constraint, identity,
  environment, project, or working-style detail>
- intent: <what the partner is ultimately trying to do>

Rules:
- GROW, never shrink: if a current note is given, fold the new transcript content
  INTO it and reorganize for coherence — keep everything that's still true, add
  what's new. Never drop facts (the nightly pass consolidates later).
- Only durable, future-useful, partner-specific content. Skip transient chatter.
- Output ONLY the note markdown, starting with the `---` frontmatter line.
"""


def _read_transcript(chat_id: str) -> str:
  try:
    con = sqlite3.connect(str(DB))
    row = con.execute(
      "select messages from chats where id=?", (chat_id,)
    ).fetchone()
    con.close()
  except sqlite3.Error:
    return ""
  raw = row[0] if row and row[0] else ""
  if not raw:
    return ""
  # Render the messages as plain role: text, newest-trimmed to a budget.
  try:
    msgs = json.loads(raw)
  except (ValueError, TypeError):
    return raw[-MAX_TRANSCRIPT_BYTES:]
  lines: list[str] = []
  for m in msgs if isinstance(msgs, list) else []:
    role = m.get("role", "?") if isinstance(m, dict) else "?"
    content = m.get("content") if isinstance(m, dict) else None
    if isinstance(content, list):
      text = " ".join(
        b.get("text", "")
        for b in content
        if isinstance(b, dict) and b.get("type") == "text"
      )
    else:
      text = content or ""
    if text.strip():
      lines.append(f"{role}: {text.strip()}")
  return "\n\n".join(lines)[-MAX_TRANSCRIPT_BYTES:]


def _note_path(chat_id: str) -> Path:
  return MEMORY_DIR / "chats" / chat_id / "index.md"


def _looks_like_note(text: str) -> bool:
  t = text.lstrip()
  return t.startswith("---") and "## Summary" in t


def _clean_note_output(text: str) -> str:
  """Trim model cruft after the note. The summarizer sometimes keeps generating
  past the note — a hallucinated `Human:`/`Assistant:` turn, or a SECOND
  frontmatter block repeating the note. Keep only the first complete note: stop
  at a chat-turn label or at the opening `---` of a repeat (the 3rd `---`, after
  the first note's open + close)."""
  out: list[str] = []
  fences = 0
  for ln in text.lstrip().splitlines():
    s = ln.strip()
    if s.startswith("Human:") or s.startswith("Assistant:"):
      break
    if s == "---":
      fences += 1
      if fences >= 3:  # 1=open, 2=close of THIS note; 3=open of a repeat
        break
    out.append(ln)
  return "\n".join(out).rstrip()


def _build_prompt(transcript: str, existing: str) -> str:
  parts = ["The chat transcript:\n\n", transcript or "(empty)"]
  if existing.strip():
    parts += ["\n\nThe CURRENT note (grow this, never shrink):\n\n", existing]
  parts.append(
    "\n\nProduce the updated memory note now, in the exact format, and nothing else."
  )
  return "".join(parts)


def _patch_title(chat_id: str, description: str) -> None:
  """Best-effort title sync (by_agent so it defers to a manual rename)."""
  try:
    token = SERVICE_TOKEN_FILE.read_text(encoding="utf-8").strip()
  except OSError:
    return
  if not token or not description:
    return
  body = json.dumps({"title": description[:200], "by_agent": True}).encode()
  req = urllib.request.Request(
    f"{API_BASE_URL}/api/chats/{chat_id}",
    data=body,
    method="PATCH",
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
    },
  )
  try:
    urllib.request.urlopen(req, timeout=10).read()
  except Exception:
    pass


def run() -> int:
  if len(sys.argv) < 2 or not sys.argv[1].strip():
    sys.stderr.write("usage: chat_note.py <chat_id>\n")
    return 2
  chat_id = sys.argv[1].strip()

  transcript = _read_transcript(chat_id)
  if not transcript:
    return 0  # nothing to summarize yet
  existing = ""
  note = _note_path(chat_id)
  if note.is_file():
    try:
      existing = note.read_text(encoding="utf-8")
    except OSError:
      existing = ""

  env = dict(os.environ)
  env["CLAUDE_CONFIG_DIR"] = str(CLAUDE_CONFIG_DIR)
  cmd = [
    CLI_PATH,
    "-p",
    _build_prompt(transcript, existing),
    "--output-format",
    "text",
    "--append-system-prompt",
    SYSTEM_PROMPT,
  ]
  if MODEL:
    cmd += ["--model", MODEL]
  try:
    proc = subprocess.run(
      cmd, env=env, capture_output=True, text=True, timeout=TIMEOUT_SECS
    )
  except (subprocess.TimeoutExpired, OSError):
    return 0
  out = _clean_note_output(proc.stdout or "")
  if not _looks_like_note(out):
    # The model didn't return a well-formed note — leave any existing note
    # untouched rather than overwriting it with garbage.
    return 0

  # Privileged write happens HERE (the subagent had no tools).
  try:
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(out + ("\n" if not out.endswith("\n") else ""), encoding="utf-8")
  except OSError:
    return 0

  m = re.search(r"^description:\s*(.+)$", out, re.MULTILINE)
  if m:
    _patch_title(chat_id, m.group(1).strip())
  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(run())
  except Exception:
    # Absolute backstop: never let this surface into the caller.
    raise SystemExit(0)
