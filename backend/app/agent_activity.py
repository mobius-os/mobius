"""Generic, app-owned presentation for manifest-declared agent commands."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath

RESULT_PREFIX = "MOBIUS_APP_ACTIVITY_V1:"
MAX_RESULT_SCAN_CHARS = 262_144
_MAX_COMMAND_CHARS = 8192
_MAX_RESOURCES = 128
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_INTERPRETER_RE = re.compile(r"^(?:.*/)?python[0-9.]*$")
_LOGIN_SHELL_RE = re.compile(r"^(?:.*/)?bash$")
_CONTROL_TOKEN_RE = re.compile(r"^[;&|()<>]+$")
_RESULT_RE = re.compile(
  rf"^{re.escape(RESULT_PREFIX)}(?P<payload>\{{.*\}})[ \t]*$", re.MULTILINE,
)
_BACKGROUND_RE = re.compile(
  r"\ACommand running in background with ID: (?P<task_id>[A-Za-z0-9_-]{1,64})\."
  r" Output is being written to: (?P<path>/[^\s]+?\.output)\."
)
_EXIT_TRAILER_RE = re.compile(r"\[exited with code (?P<code>-?\d+)\]\s*\Z")
# One app operation can span several calls when its output is paged to fit a
# provider's tool-output limit. Receipts from the same app that share this key
# are one operation: the chat shows them as a single row (activityGrouping.js).
_OPERATION_KEY_RE = re.compile(r"[A-Za-z0-9._:-]{1,160}")


def _text(value: object, limit: int) -> str:
  if not isinstance(value, str):
    return ""
  return re.sub(r"\s+", " ", value[: limit * 2]).strip()[:limit]


@dataclass(frozen=True, slots=True)
class ActivityCommand:
  app_slug: str
  app_name: str
  activity_id: str
  argument_count: int
  running_label: str


@dataclass(frozen=True, slots=True)
class AgentActivityBinding:
  by_path: Mapping[str, ActivityCommand]
  tool_names: tuple[str, ...]

  @property
  def is_empty(self) -> bool:
    return not self.by_path

  @classmethod
  def of(
    cls, pairs: Iterable[tuple[str, ActivityCommand]],
  ) -> "AgentActivityBinding":
    by_path: dict[str, ActivityCommand] = {}
    names: list[str] = []
    for path, command in pairs:
      if not isinstance(path, str) or not path or path in by_path:
        continue
      by_path[path] = command
      name = PurePosixPath(path).name
      if name and name not in names:
        names.append(name)
    return cls(by_path=by_path, tool_names=tuple(names))


EMPTY_AGENT_ACTIVITY_BINDING = AgentActivityBinding(by_path={}, tool_names=())


def _tokens(command: str) -> list[str] | None:
  # Quoted newlines are still outside the one-command contract; reject them
  # before shlex erases that distinction.
  if "\n" in command or "\r" in command:
    return None
  try:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    values = list(lexer)
  except ValueError:
    return None
  if not values or any(_CONTROL_TOKEN_RE.fullmatch(value) for value in values):
    return None
  if any("`" in value or "$(" in value for value in values):
    return None
  return values


def _unwrap(tokens: list[str]) -> list[str] | None:
  if len(tokens) != 3 or not _LOGIN_SHELL_RE.fullmatch(tokens[0]):
    return tokens
  return _tokens(tokens[2]) if tokens[1] == "-lc" else None


def _declared_command(
  tokens: list[str], binding: AgentActivityBinding,
) -> ActivityCommand | None:
  index = 0
  while index < len(tokens) and _ENV_ASSIGN_RE.match(tokens[index]):
    index += 1
  if index >= len(tokens):
    return None
  declared = binding.by_path.get(tokens[index])
  if declared is not None:
    expected = index + 1 + declared.argument_count
    return declared if len(tokens) == expected else None
  if not _INTERPRETER_RE.match(tokens[index]):
    return None
  # Keep interpreter recognition as narrow as direct script execution. Python
  # options have different arities (`-W value`, `-X value`, `-c code`, ...), so
  # skipping option-looking tokens can authenticate a path consumed as an
  # option value even though that script never runs.
  script_index = index + 1
  if script_index >= len(tokens):
    return None
  declared = binding.by_path.get(tokens[script_index])
  if declared is None:
    return None
  expected = script_index + 1 + declared.argument_count
  return declared if len(tokens) == expected else None


def activity_from_command(
  command: object, binding: AgentActivityBinding,
) -> dict | None:
  """Authenticate a declared simple invocation and return its running card."""
  if (
    not isinstance(command, str)
    or not command
    or len(command) > _MAX_COMMAND_CHARS
    or binding.is_empty
    or not any(name in command for name in binding.tool_names)
  ):
    return None
  tokens = _tokens(command)
  tokens = _unwrap(tokens) if tokens else None
  declared = _declared_command(tokens, binding) if tokens else None
  if declared is None:
    return None
  return {
    "status": "running",
    "app_slug": declared.app_slug,
    "app_name": declared.app_name,
    "activity_id": declared.activity_id,
    "label": declared.running_label,
  }


def _identity(pending: object, settled: dict) -> dict:
  if isinstance(pending, dict):
    for key in ("app_slug", "app_name", "activity_id"):
      value = pending.get(key)
      if isinstance(value, str) and value:
        settled[key] = value
  return settled


def _failed(pending: object) -> dict:
  return _identity(pending, {"status": "failed", "label": "Activity failed"})


def activity_without_receipt(pending: object) -> dict:
  return _identity(pending, {
    "status": "succeeded", "label": "Completed", "receipt_missing": True,
  })


def _intent(value: object) -> str:
  if not isinstance(value, str):
    return ""
  candidate = value.strip()
  if (
    not candidate
    or len(candidate) > 512
    or any(ord(character) < 32 for character in candidate)
  ):
    return ""
  return candidate


def _resources(value: object) -> list[dict[str, str]]:
  if not isinstance(value, list):
    return []
  resources: list[dict[str, str]] = []
  seen: set[tuple[str, str]] = set()
  for raw in value[:_MAX_RESOURCES]:
    if not isinstance(raw, dict):
      continue
    label = _text(raw.get("label"), 160)
    intent = _intent(raw.get("intent"))
    key = (label, intent)
    if not label or key in seen:
      continue
    seen.add(key)
    resource = {"label": label}
    summary = _text(raw.get("summary"), 400)
    if summary and summary.casefold() != label.casefold():
      resource["summary"] = summary
    if intent:
      resource["intent"] = intent
    resources.append(resource)
  return resources


def activity_from_result(
  pending: object, text: object, exit_code: object = None,
) -> dict:
  """Validate the app receipt while retaining host-authenticated identity."""
  if isinstance(exit_code, bool):
    exit_code = None
  process_failed = isinstance(exit_code, int) and exit_code != 0
  if not isinstance(text, str) or not text.strip():
    return _failed(pending) if process_failed else activity_without_receipt(pending)
  matches = list(_RESULT_RE.finditer(text[-MAX_RESULT_SCAN_CHARS:]))
  if not matches:
    return _failed(pending)
  try:
    payload = json.loads(matches[-1].group("payload"))
  except (TypeError, ValueError, json.JSONDecodeError):
    return _failed(pending)
  if (
    not isinstance(payload, dict)
    or not isinstance(pending, dict)
    or payload.get("activity_id") != pending.get("activity_id")
    or payload.get("status") not in {"succeeded", "empty", "failed"}
    or (process_failed and payload.get("status") != "failed")
  ):
    return _failed(pending)
  label = _text(payload.get("label"), 160)
  if not label:
    return _failed(pending)
  settled: dict = {"status": payload["status"], "label": label}
  for key, limit in (("detail", 600), ("warning", 600)):
    value = _text(payload.get(key), limit)
    if value:
      settled[key] = value
  resources = _resources(payload.get("resources"))
  if resources:
    settled["resources"] = resources
  operation_key = payload.get("operation_key")
  if isinstance(operation_key, str) and _OPERATION_KEY_RE.fullmatch(operation_key):
    settled["operation_key"] = operation_key
  return _identity(pending, settled)


def background_dispatch(text: object) -> dict | None:
  if not isinstance(text, str) or len(text) > _MAX_COMMAND_CHARS:
    return None
  match = _BACKGROUND_RE.match(text.strip())
  if not match:
    return None
  task_id = match.group("task_id")
  path = match.group("path")
  if PurePosixPath(path).name != f"{task_id}.output":
    return None
  return {"task_id": task_id, "output_path": path}


def defer_activity(pending: object, dispatch: dict) -> dict:
  label = pending.get("label") if isinstance(pending, dict) else None
  return _identity(pending, {
    "status": "running", "label": label or "Working", **dispatch,
  })


def background_output_path(pending: object, scratch_root: object) -> str | None:
  if not isinstance(pending, dict) or not isinstance(scratch_root, str):
    return None
  task_id = pending.get("task_id")
  path = pending.get("output_path")
  if not isinstance(task_id, str) or not isinstance(path, str):
    return None
  root = PurePosixPath(scratch_root)
  candidate = PurePosixPath(path)
  if (
    not candidate.is_absolute()
    or ".." in candidate.parts
    or candidate.name != f"{task_id}.output"
    or candidate.parent.name != "tasks"
    or not candidate.is_relative_to(root)
  ):
    return None
  return str(candidate)


def activity_from_task_output(
  pending: object, text: object, task_status: object = None,
) -> dict:
  # Unlike a provider's optional foreground aggregate, this file is the
  # authoritative completed background capture. Missing bytes therefore mean
  # the task never produced a trustworthy receipt, not a receipt-less success.
  if not isinstance(text, str) or not text.strip():
    return _failed(pending)
  exit_code: int | None = None
  trailer = _EXIT_TRAILER_RE.search(text[-64:])
  if trailer:
    exit_code = int(trailer.group("code"))
  if exit_code is None and isinstance(task_status, str):
    if task_status not in ("done", "completed"):
      exit_code = 1
  return activity_from_result(pending, text, exit_code)
