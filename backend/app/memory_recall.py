"""Recognize Memory-app recall lookups so a turn can cite what it remembered.

The Memory app is an ordinary installed app: the agent consults it by running
``memory_search.py`` through Bash, and the notes it read come back as ordinary
tool output. Without this module that lookup is indistinguishable from any
other shell command, so the owner cannot tell "it remembered something" from
"it ran housekeeping" — nor, more importantly, "it looked and found nothing"
from "it never looked".

Detection is deliberately two-phase and keyed off the tool's own lifecycle:

* ``recall_from_command`` accepts only the simple absolute invocation documented
  by the Memory skill. It deliberately rejects shell composition rather than
  trying to partially parse Bash.
* ``recall_from_result`` reads the Memory app's bounded structured result line
  and is only called for a tool already identified by the first phase.

The structured line is printed last, so head+tail carving preserves it. Human
prose and the legacy ``FILES:`` line remain useful to the agent, but neither is
parsed for product state. Missing, malformed, or contradictory result metadata
is an explicit failed lookup, never a successful note-less recall.
"""

from __future__ import annotations

import json
import re
import shlex

# Recall metadata rides inline on the SSE event, the persisted tool block, and
# the compacted activity summary — the same budget the web-source citations
# live within. Memory transports full selected nodes in ordinary tool output;
# its final receipt carries only bounded path/title metadata. These ceilings
# keep a malformed or hostile stdout from inflating every transcript read.
MAX_RECALL_NOTES = 12
MAX_RECALL_TITLE_CHARS = 120
MAX_RECALL_EXCERPT_CHARS = 300
MAX_RECALL_PATH_CHARS = 256
_MAX_OUTPUT_SCAN_CHARS = 262_144
_MAX_SECTION_LINES_SCANNED = 256

RECALL_SEARCHING = "searching"
RECALL_HIT = "hit"
RECALL_EMPTY = "empty"
RECALL_FAILED = "failed"

# The command summary is the verbatim Bash command. Accept only Memory's
# documented simple absolute invocation. This is intentionally a narrow
# protocol, not a growing shell grammar: composition, redirection, substitution,
# and relative scripts all yield no observability marker while the command
# itself continues to run normally.
_MAX_COMMAND_SCAN_CHARS = 8192
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_INTERPRETER_RE = re.compile(r"^(?:.*/)?python[0-9.]*$")
_SCRIPT_RE = re.compile(
  r"^/data/apps/(?P<app_slug>memory(?:-[0-9]+)?)/memory_search\.py$"
)
_CONTROL_TOKEN_RE = re.compile(r"^[;&|()<>]+$")

_RESULT_PREFIX = "MOBIUS_MEMORY_RESULT_V1:"
_RESULT_RE = re.compile(
  rf"^{re.escape(_RESULT_PREFIX)}(?P<payload>\{{.*\}})[ \t]*$",
  re.MULTILINE,
)

# A citation path is only ever a repository-relative markdown pointer. Refusing
# anything else keeps traversal, absolute paths, and control characters out of
# a value the client turns into a deep link.
_PATH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*\.md$")
_NOTE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MAX_NOTE_ID_CHARS = 128


def _clean(value: str, limit: int) -> str:
  """Collapse whitespace and bound a label taken from tool output."""
  if not isinstance(value, str):
    return ""
  # Slice before normalizing so a pathological line cannot allocate another
  # full-size string merely to produce a short label.
  return re.sub(r"\s+", " ", value[: limit * 2]).strip()[:limit]


def _note_id(path: str) -> str:
  """The graph node id for a citation path: its file stem."""
  tail = path.rsplit("/", 1)[-1]
  return tail[:-3] if tail.endswith(".md") else tail


def _safe_note_id(value: object, path: str) -> str:
  """Keep the graph's real node id, with a path-stem fallback for old apps.

  A graph id is not required to equal its markdown filename. The Memory app
  opens nodes by id, so replacing a valid structured id with the path stem
  makes a well-formed citation navigate to nowhere whenever those differ.
  """
  if isinstance(value, str):
    candidate = value.strip()
    if (
      candidate
      and len(candidate) <= _MAX_NOTE_ID_CHARS
      and _NOTE_ID_RE.fullmatch(candidate)
    ):
      return candidate
  return _note_id(path)


def _title_from_path(path: str) -> str:
  """A readable fallback when the titled section line was carved away."""
  return _note_id(path).replace("-", " ").replace("_", " ").strip()


def _safe_path(value: str) -> str:
  if not isinstance(value, str):
    return ""
  candidate = value.strip()
  if not candidate or len(candidate) > MAX_RECALL_PATH_CHARS:
    return ""
  if ".." in candidate or candidate.startswith("/"):
    return ""
  return candidate if _PATH_RE.match(candidate) else ""


def _simple_command_tokens(command: str) -> list[str] | None:
  try:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
  except ValueError:
    return None
  if not tokens or any(_CONTROL_TOKEN_RE.fullmatch(token) for token in tokens):
    return None
  # Reject substitutions/backticks conservatively. They are not part of the
  # documented call and would turn this recognizer back into a shell parser.
  if any("`" in token or "$(" in token for token in tokens):
    return None
  return tokens


def _tokens_search_slug(tokens: list[str]) -> str | None:
  """Return the invoked Memory app slug for the exact documented command.

  Exact arity is a security boundary, not mere tidiness: ``shlex`` treats a
  newline as whitespace, so accepting arbitrary trailing tokens would also
  accept a second shell command whose output could forge the structured result
  line. The supported command is env assignments + Python flags + script +
  query + chat id, and then it must end.
  """
  index = 0
  while index < len(tokens) and _ENV_ASSIGN_RE.match(tokens[index]):
    index += 1
  if index >= len(tokens):
    return None
  head = tokens[index]
  direct = _SCRIPT_RE.fullmatch(head)
  if direct:
    return direct.group("app_slug") if len(tokens) == index + 3 else None
  if not _INTERPRETER_RE.match(head):
    return None
  for script_index, token in enumerate(tokens[index + 1:], start=index + 1):
    if token.startswith("-"):
      continue
    script = _SCRIPT_RE.fullmatch(token)
    if script and len(tokens) == script_index + 3:
      return script.group("app_slug")
    return None
  return None


def recall_from_command(command: object) -> dict | None:
  """Return a pending recall marker when this command RUNS a memory lookup.

  Called at tool-input time so the live turn can name what it is doing while
  the lookup is still in flight. Returning ``None`` means "not a memory
  lookup", which is also the safe answer for a missing or oversized command
  summary — and, deliberately, for any command that merely names the script.
  """
  if not isinstance(command, str) or not command:
    return None
  if len(command) > _MAX_COMMAND_SCAN_CHARS:
    return None
  # Cheap reject before tokenizing: the overwhelming majority of commands are
  # not memory lookups and should cost one substring scan.
  if "memory_search.py" not in command:
    return None
  tokens = _simple_command_tokens(command)
  app_slug = _tokens_search_slug(tokens) if tokens else None
  return (
    {"status": RECALL_SEARCHING, "app_slug": app_slug}
    if app_slug else None
  )


def recall_from_result(text: object, exit_code: object = None) -> dict:
  """Validate a known Memory command's final structured result."""
  if isinstance(exit_code, bool):
    exit_code = None
  if isinstance(exit_code, int) and exit_code != 0:
    return {"status": RECALL_FAILED}
  if not isinstance(text, str) or not text.strip():
    return {"status": RECALL_FAILED}

  body = text[-_MAX_OUTPUT_SCAN_CHARS:]
  matches = list(_RESULT_RE.finditer(body))
  if not matches:
    return {"status": RECALL_FAILED}
  try:
    payload = json.loads(matches[-1].group("payload"))
  except (TypeError, ValueError, json.JSONDecodeError):
    return {"status": RECALL_FAILED}
  if not isinstance(payload, dict):
    return {"status": RECALL_FAILED}

  status = payload.get("status")
  if status == RECALL_FAILED:
    return {"status": RECALL_FAILED}
  if status == RECALL_EMPTY:
    return {"status": RECALL_EMPTY}
  if status != RECALL_HIT or not isinstance(payload.get("notes"), list):
    return {"status": RECALL_FAILED}

  notes: list[dict[str, str]] = []
  seen: set[str] = set()
  for raw_note in payload["notes"][:_MAX_SECTION_LINES_SCANNED]:
    if not isinstance(raw_note, dict):
      continue
    path = _safe_path(raw_note.get("path"))
    if not path or path in seen:
      continue
    seen.add(path)
    title = _clean(raw_note.get("title"), MAX_RECALL_TITLE_CHARS)
    excerpt = _clean(raw_note.get("excerpt"), MAX_RECALL_EXCERPT_CHARS)
    note = {
      "id": _safe_note_id(raw_note.get("id"), path),
      "path": path,
      "title": title or _title_from_path(path) or path,
    }
    if excerpt:
      note["excerpt"] = excerpt
    notes.append(note)
    if len(notes) >= MAX_RECALL_NOTES:
      break
  return (
    {"status": RECALL_HIT, "notes": notes}
    if notes else {"status": RECALL_FAILED}
  )


def merge_recall_notes(
  target: list[dict[str, str]],
  seen: set[str],
  recall: object,
) -> None:
  """Accumulate one block's notes into a deduped, bounded citation list.

  Shared by the transcript compaction rollup so the projection and the live
  block agree on ordering (first occurrence owns the position) and on the cap.
  """
  if not isinstance(recall, dict):
    return
  for note in recall.get("notes") or []:
    if not isinstance(note, dict):
      continue
    path = note.get("path")
    if not isinstance(path, str) or not path or path in seen:
      continue
    seen.add(path)
    target.append(note)
    if len(target) >= MAX_RECALL_NOTES:
      return
