"""Recognize Memory-app recall lookups so a turn can cite what it remembered.

The memory app is an ordinary installed app: the agent consults it by running
its recall entry point through Bash, and the notes it read come back as
ordinary tool output. Without this module that lookup is indistinguishable from any
other shell command, so the owner cannot tell "it remembered something" from
"it ran housekeeping" — nor, more importantly, "it looked and found nothing"
from "it never looked".

Detection is deliberately two-phase and keyed off the tool's own lifecycle:

* ``recall_from_command`` accepts only the simple absolute invocation of a
  BOUND provider path (see ``app.memory_provider``). It deliberately rejects
  shell composition rather than trying to partially parse Bash.
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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

# Recall metadata rides inline on the SSE event, the persisted tool block, and
# the compacted activity summary — the same budget the web-source citations
# live within. Memory transports full selected nodes in ordinary tool output;
# its final receipt carries only bounded path/title metadata. These ceilings
# keep a malformed or hostile stdout from inflating every transcript read.
MAX_RECALL_NOTES = 12
MAX_RECALL_QUERY_CHARS = 600
MAX_RECALL_TITLE_CHARS = 120
MAX_RECALL_EXCERPT_CHARS = 300
MAX_RECALL_PATH_CHARS = 256
# Public because the transcript read boundary uses the same bound when it
# recovers a legacy lookup from a large-output sidecar. Keeping one ceiling
# means the database never materializes an old Memory search's complete note
# bodies merely to read the small structured receipt printed at the end.
MAX_RECALL_RESULT_SCAN_CHARS = 262_144
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
_LOGIN_SHELL_RE = re.compile(r"^(?:.*/)?bash$")
_CONTROL_TOKEN_RE = re.compile(r"^[;&|()<>]+$")


@dataclass(frozen=True, slots=True)
class RecallBinding:
  """The script paths whose recall receipts this platform will honor.

  Immutable, and passed explicitly rather than read from a module global: a
  caller that cannot say where its binding came from has no business minting a
  citation. ``app.memory_provider`` builds it from installed-app state, so this
  module never learns any app's location.
  """

  by_path: Mapping[str, str]
  tool_names: tuple[str, ...]

  @property
  def is_empty(self) -> bool:
    return not self.by_path

  def app_slug_for(self, token: str) -> str | None:
    return self.by_path.get(token)

  @classmethod
  def of(cls, pairs: Iterable[tuple[str, str]]) -> "RecallBinding":
    """Build from (absolute script path, app slug) pairs; first entry wins.

    Pure: ``tool_names`` is derived with ``PurePosixPath`` and never stats the
    filesystem, so the protocol module stays stdlib-only and independently
    testable. Path construction lives in ``memory_provider``.
    """
    by_path: dict[str, str] = {}
    names: list[str] = []
    for path, slug in pairs:
      if not isinstance(path, str) or not path or not isinstance(slug, str):
        continue
      if path in by_path:
        continue
      by_path[path] = slug
      name = PurePosixPath(path).name
      if name and name not in names:
        names.append(name)
    return cls(by_path=by_path, tool_names=tuple(names))


EMPTY_RECALL_BINDING = RecallBinding(by_path={}, tool_names=())

# `MOBIUS_MEMORY_RESULT_V1:` is embedded in durable ToolOutput bytes that the
# read path re-parses on every chat load. Renaming it would retroactively break
# history — that is partner DATA, and exactly the compatibility exception the
# design principles carve out, not laziness. An app-neutral prefix is what a
# protocol v2 is for; shipping one now would be machinery no problem has earned.
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


def _unwrap_login_shell(tokens: list[str]) -> list[str] | None:
  """Unwrap the exact ``/bin/bash -lc <command>`` used by Codex exec.

  Codex's command item records the host wrapper rather than only the inner
  command. Treating that wrapper as arbitrary shell composition made every
  real Codex Memory read invisible even though plain synthetic commands passed
  the recognizer tests. The wrapper is accepted only at exact arity; the inner
  text then goes through the same conservative token/control checks as a plain
  invocation.
  """
  if len(tokens) != 3 or not _LOGIN_SHELL_RE.fullmatch(tokens[0]):
    return tokens
  if tokens[1] != "-lc":
    return None
  return _simple_command_tokens(tokens[2])


def _recall_invocation(
  tokens: list[str], binding: RecallBinding,
) -> tuple[str, str] | None:
  """Return the app slug and question for the documented Memory command.

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
  direct = binding.app_slug_for(head)
  if direct is not None:
    return (
      (direct, tokens[index + 1]) if len(tokens) == index + 3 else None
    )
  if not _INTERPRETER_RE.match(head):
    return None
  for script_index, token in enumerate(tokens[index + 1:], start=index + 1):
    if token.startswith("-"):
      continue
    slug = binding.app_slug_for(token)
    if slug is not None and len(tokens) == script_index + 3:
      return slug, tokens[script_index + 1]
    return None
  return None


def recall_from_command(command: object, binding: RecallBinding) -> dict | None:
  """Return a pending recall marker when this command RUNS a memory lookup.

  Called at tool-input time so the live turn can name what it is doing while
  the lookup is still in flight. Returning ``None`` means "not a memory
  lookup", which is also the safe answer for a missing or oversized command
  summary, an empty binding — and, deliberately, for any command that merely
  names the script.
  """
  if not isinstance(command, str) or not command:
    return None
  if len(command) > _MAX_COMMAND_SCAN_CHARS:
    return None
  if binding.is_empty:
    return None
  # Cheap reject before tokenizing: the overwhelming majority of commands are
  # not memory lookups and should cost one substring scan. The names come from
  # the binding, so this stays a prefilter and never a second source of truth.
  if not any(name in command for name in binding.tool_names):
    return None
  tokens = _simple_command_tokens(command)
  tokens = _unwrap_login_shell(tokens) if tokens else None
  invocation = _recall_invocation(tokens, binding) if tokens else None
  if not invocation:
    return None
  app_slug, raw_query = invocation
  query = _clean(raw_query, MAX_RECALL_QUERY_CHARS)
  return {
    "status": RECALL_SEARCHING,
    "app_slug": app_slug,
    **({"query": query} if query else {}),
  }


def recall_from_result(text: object, exit_code: object = None) -> dict:
  """Validate a known Memory command's final structured result."""
  if isinstance(exit_code, bool):
    exit_code = None
  if isinstance(exit_code, int) and exit_code != 0:
    return {"status": RECALL_FAILED}
  if not isinstance(text, str) or not text.strip():
    return {"status": RECALL_FAILED}

  body = text[-MAX_RECALL_RESULT_SCAN_CHARS:]
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


def _with_pending_context(pending: object, settled: dict) -> dict:
  if not isinstance(pending, dict):
    return settled
  app_slug = pending.get("app_slug")
  query = pending.get("query")
  if isinstance(app_slug, str):
    settled["app_slug"] = app_slug
  if isinstance(query, str) and query:
    settled["query"] = query
  if settled.get("status") == RECALL_HIT and isinstance(app_slug, str):
    settled["notes"] = [
      {**note, "app_slug": app_slug} for note in settled.get("notes", [])
    ]
  return settled


def settle_recall(
  pending: object,
  text: object,
  exit_code: object = None,
) -> dict:
  """Settle one command-identified lookup and retain its product context."""
  return _with_pending_context(pending, recall_from_result(text, exit_code))


# Claude Code answers a `run_in_background` Bash call with this fixed
# placeholder instead of the command's stdout; the real output lands in the
# named file and a task_done follows. The lookup is still in flight at that
# moment, so the placeholder must never be read as Memory's (missing) result.
_BACKGROUND_DISPATCH_RE = re.compile(
  r"\ACommand running in background with ID: (?P<task_id>[A-Za-z0-9_-]{1,64})\."
  r" Output is being written to: (?P<path>/[^\s]+?\.output)\."
)
_TASK_OUTPUT_TRAILER_RE = re.compile(r"\[exited with code (?P<code>-?\d+)\]\s*\Z")


def background_dispatch_from_result(text: object) -> dict | None:
  """Return ``{"task_id", "output_path"}`` when ``text`` is the background placeholder."""
  if not isinstance(text, str) or len(text) > _MAX_COMMAND_SCAN_CHARS:
    return None
  match = _BACKGROUND_DISPATCH_RE.match(text.strip())
  if not match:
    return None
  task_id = match.group("task_id")
  path = match.group("path")
  if PurePosixPath(path).name != f"{task_id}.output":
    return None
  return {"task_id": task_id, "output_path": path}


def defer_recall_to_task(pending: object, dispatch: dict) -> dict:
  """Keep a lookup `searching` while its background task owns the result."""
  deferred = {"status": RECALL_SEARCHING, **dispatch}
  return _with_pending_context(pending, deferred)


def background_recall_path(pending: object, scratch_root: object) -> str | None:
  """The task output file a deferred lookup may be settled from, or None.

  The path was quoted back from tool output, so it is honored only when it is
  this chat's own scratch task file. Anything else settles as failed rather
  than reading an arbitrary file.
  """
  if not isinstance(pending, dict) or not isinstance(scratch_root, str):
    return None
  task_id = pending.get("task_id")
  path = pending.get("output_path")
  if not isinstance(task_id, str) or not isinstance(path, str):
    return None
  root = PurePosixPath(scratch_root)
  candidate = PurePosixPath(path)
  if not candidate.is_absolute() or ".." in candidate.parts:
    return None
  if candidate.name != f"{task_id}.output" or candidate.parent.name != "tasks":
    return None
  if not candidate.is_relative_to(root):
    return None
  return str(candidate)


def settle_recall_from_task_output(
  pending: object, text: object, task_status: object = None,
) -> dict:
  """Settle a deferred lookup from its background task's captured output.

  Claude Code appends ``[exited with code N]`` to the file; that trailer is the
  command's own exit status. Without it, a task that did not end ``done`` is
  a failure and a completed one is judged on the structured result line.
  """
  exit_code: int | None = None
  if isinstance(text, str):
    trailer = _TASK_OUTPUT_TRAILER_RE.search(text[-64:])
    if trailer:
      exit_code = int(trailer.group("code"))
  if exit_code is None and isinstance(task_status, str):
    if task_status not in ("done", "completed"):
      exit_code = 1
  return settle_recall(pending, text, exit_code)


def recall_from_tool_block(
  block: object, binding: RecallBinding,
) -> dict | None:
  """Return explicit or recoverable recall metadata for a stored tool block.

  Early Codex transcripts contain the exact Memory command and structured
  receipt but no ``recall`` field because the provider recorded its standard
  login-shell wrapper. Recovering at the read projection preserves that real
  owner history without rewriting ``Chat.messages``.
  """
  if not isinstance(block, dict) or block.get("type") != "tool":
    return None
  recall = block.get("recall")
  if isinstance(recall, dict):
    return recall
  pending = recall_from_command(block.get("input"), binding)
  if pending is None or block.get("status") == "running":
    return None

  output = block.get("output")
  exit_code = block.get("output_exit_code")
  # Historical large outputs are removed from the ordinary chat payload once
  # their durable sidecar exists. An absent inline excerpt therefore means
  # "deferred", not "Memory failed". The sidecar-aware transcript boundary
  # enriches these blocks before compaction; callers without that sidecar must
  # leave the lookup unclassified rather than manufacture an error. A real
  # non-zero process exit is still enough to report failure without stdout.
  failed_exit = (
    isinstance(exit_code, int)
    and not isinstance(exit_code, bool)
    and exit_code != 0
  )
  if not failed_exit and (not isinstance(output, str) or not output.strip()):
    return None
  return settle_recall(
    pending,
    output,
    exit_code,
  )
