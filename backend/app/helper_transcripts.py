"""Read one helper agent's own conversation on behalf of its parent chat.

A helper row in the parent chat (a Claude Agent/Task helper or a Codex
multi-agent child) carries only a provider task id. Two questions sit behind
"open this helper":

* **Which helper is it?** A task id is admitted only when the append-only
  ``agent_lifecycle_events`` table recorded it for *this* chat. That table is
  the authority boundary: a task id never becomes a file path unless the
  parent chat demonstrably spawned that helper.
* **What did it say and do?** Each provider already persists the helper's
  complete conversation. Claude writes a sidechain transcript beside the parent
  session; Codex writes the child thread's own rollout. Either format is
  projected into the ordinary chat block vocabulary (``text`` and ``tool``
  blocks) so the frontend reuses its existing renderers.

The provider files are read-only evidence. Reads are bounded because a
long-lived Codex rollout can reach hundreds of megabytes: only the newest
``TAIL_BYTES`` are parsed and only the newest ``MAX_BLOCKS`` blocks returned,
with ``truncated`` telling the viewer.
"""

from __future__ import annotations

import glob
import json
import re
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app import models
from app.agent_lifecycle import stable_activation_id
from app.config import get_settings
from app.tool_summaries import summarize_tool_input


TAIL_BYTES = 16 * 1024 * 1024
MAX_BLOCKS = 400
TEXT_CHARS = 20_000
TOOL_INPUT_CHARS = 2_000
TOOL_OUTPUT_CHARS = 4_000

# Provider ids become path components, so they must stay a single safe name.
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,160}$")


def read_helper_conversation(
  db: Session, chat_id: str, task_id: str,
) -> dict | None:
  """``{"provider", "blocks", "truncated"}`` for a helper this chat spawned.

  ``None`` when the chat never recorded ``task_id`` or the provider's record
  of the conversation is gone.
  """
  delegation = db.query(models.Delegation).filter(
    models.Delegation.id == task_id,
    models.Delegation.parent_chat_id == chat_id,
  ).first()
  if delegation is not None:
    return _delegation_conversation(db, delegation)
  helper = _recorded_helper(db, chat_id, task_id)
  if helper is None:
    return None
  if helper.provider == "claude":
    path = _claude_sidechain_path(
      helper.provider_session_id, helper.provider_agent_id,
    )
  elif helper.provider == "codex":
    path = _codex_rollout_path(helper.provider_agent_id)
  else:
    path = None
  if path is None:
    return None
  try:
    records, skipped = _tail_records(path)
  except OSError:
    return None
  if helper.provider == "claude":
    blocks = _claude_blocks(records)
  else:
    blocks = _codex_blocks(records, from_start=not skipped)
  dropped = len(blocks) > MAX_BLOCKS
  return {
    "provider": helper.provider,
    "blocks": blocks[-MAX_BLOCKS:],
    "truncated": skipped or dropped,
  }


def _delegation_conversation(db: Session, delegation: models.Delegation) -> dict | None:
  """A Möbius helper's own chat, in the same block shape as a transcript.

  Its tasks read as ``{"role": "user", "content"}``, its replies as
  ``{"role": "assistant", "content"}``, and tool blocks as stored; hidden
  product messages (result carriers, wakes) are not its conversation. A
  running turn's live steps are included.
  """
  from app.chat_transcript import materialized_messages

  child = db.query(models.Chat).filter(
    models.Chat.id == delegation.child_chat_id,
    models.Chat.deleted_at.is_(None),
  ).first()
  if child is None:
    return None
  blocks: list[dict] = []
  for message in materialized_messages(child):
    if not isinstance(message, dict) or message.get("hidden"):
      continue
    if message.get("role") == "user":
      content = message.get("content")
      if isinstance(content, str) and content.strip():
        blocks.append({"role": "user", "content": content})
      continue
    if message.get("role") != "assistant":
      continue
    for block in message.get("blocks") or []:
      if not isinstance(block, dict):
        continue
      if block.get("type") == "tool":
        blocks.append(block)
      elif block.get("type") == "text" and str(block.get("content") or "").strip():
        blocks.append({"role": "assistant", "content": block["content"]})
  return {
    "provider": delegation.provider,
    "child_chat_id": delegation.child_chat_id,
    "blocks": blocks[-MAX_BLOCKS:],
    "truncated": len(blocks) > MAX_BLOCKS,
  }


def _recorded_helper(
  db: Session, chat_id: str, task_id: str,
) -> models.AgentLifecycleEvent | None:
  """The runner-recorded lifecycle row this chat holds for ``task_id``.

  A Claude row's task id is the provider agent id itself. A Codex row's task
  id is a per-activation id (Codex can resume one child several times in a
  turn), which the table stores only as the opaque ``stable_activation_id``;
  recompute that digest for each Codex helper the chat recorded and match it.
  """
  events = models.AgentLifecycleEvent
  runner_rows = db.query(events).filter(
    events.chat_id == chat_id, events.source == "runner",
  )
  direct = (
    runner_rows.filter(events.provider_agent_id == task_id)
    .order_by(events.id.asc()).first()
  )
  if direct is not None:
    return direct
  candidates = (
    db.query(events.agent_id, events.chat_run_id)
    .filter(
      events.chat_id == chat_id,
      events.source == "runner",
      events.provider == "codex",
    )
    .distinct()
    .all()
  )
  if not candidates:
    return None
  digests = {
    stable_activation_id(agent_id, chat_run_id, task_id)
    for agent_id, chat_run_id in candidates
  }
  return (
    runner_rows.filter(events.activation_id.in_(digests))
    .order_by(events.id.asc()).first()
  )


def _provider_home(name: str) -> Path:
  # The same directories providers.build_env hands the provider CLIs.
  return Path(get_settings().data_dir) / "cli-auth" / name


def _claude_sidechain_path(session_id: str | None, agent_id: str) -> Path | None:
  """Claude writes ``projects/<cwd>/<session>/subagents/agent-<id>.jsonl``."""
  if not session_id or not _SAFE_ID.match(session_id) or not _SAFE_ID.match(agent_id):
    return None
  matches = glob.glob(str(
    _provider_home("claude") / "projects" / "*" / session_id / "subagents"
    / f"agent-{agent_id}.jsonl"
  ))
  return Path(matches[0]) if matches else None


def _codex_rollout_path(thread_id: str) -> Path | None:
  """Codex writes ``sessions/YYYY/MM/DD/rollout-<time>-<thread>.jsonl``."""
  if not _SAFE_ID.match(thread_id):
    return None
  home = _provider_home("codex")
  for pattern in (
    home / "sessions" / "*" / "*" / "*" / f"rollout-*-{thread_id}.jsonl",
    home / "archived_sessions" / f"rollout-*-{thread_id}.jsonl",
  ):
    matches = glob.glob(str(pattern))
    if matches:
      return Path(matches[0])
  return None


def _tail_records(
  path: Path, tail_bytes: int | None = None,
) -> tuple[list[dict], bool]:
  """Parse the newest JSONL records; ``True`` when older bytes were skipped."""
  tail_bytes = TAIL_BYTES if tail_bytes is None else tail_bytes
  size = path.stat().st_size
  skipped = size > tail_bytes
  with path.open("rb") as handle:
    if skipped:
      handle.seek(size - tail_bytes)
      handle.readline()  # discard the partial first line
    raw = handle.read()
  records = []
  for line in raw.splitlines():
    try:
      record = json.loads(line)
    except ValueError:
      continue  # the provider may be mid-write on the final line
    if isinstance(record, dict):
      records.append(record)
  return records, skipped


def _clip(text: str, limit: int) -> str:
  return text if len(text) <= limit else text[:limit].rstrip() + "\n…"


def _joined_text(parts: Any, *types: str) -> str:
  if isinstance(parts, str):
    return parts
  return "\n".join(
    part["text"] for part in parts if isinstance(part, dict)
    and part.get("type") in types and isinstance(part.get("text"), str)
  ) if isinstance(parts, list) else ""


class _Blocks(list):
  """Chat blocks in order, pairing each tool result with its call."""

  def __init__(self) -> None:
    super().__init__()
    self._open_tools: dict[str, dict] = {}

  def text(self, content: str, *, role: str = "assistant") -> None:
    content = content.strip()
    if not content:
      return
    block = {"type": "text", "content": _clip(content, TEXT_CHARS)}
    if role != "assistant":
      block["role"] = role
    self.append(block)

  def tool(self, call_id: str | None, name: str, summary: str) -> None:
    block = {
      "type": "tool", "tool": name,
      "input": _clip(summary, TOOL_INPUT_CHARS), "output": "", "status": "running",
    }
    if call_id:
      self._open_tools[call_id] = block
    self.append(block)

  def result(self, call_id: str | None, output: str, *, failed: bool = False) -> None:
    block = self._open_tools.pop(call_id, None) if call_id else None
    if block is not None:
      block["output"] = _clip(output, TOOL_OUTPUT_CHARS)
      block["status"] = "error" if failed else "done"


def _claude_blocks(records: Iterable[dict]) -> _Blocks:
  """Project a Claude sidechain transcript (user/assistant message records)."""
  blocks = _Blocks()
  for record in records:
    role = record.get("type")
    message = record.get("message")
    if role not in ("user", "assistant") or not isinstance(message, dict):
      continue
    content = message.get("content")
    if isinstance(content, str):
      blocks.text(content, role=role)
      continue
    for part in content if isinstance(content, list) else []:
      if not isinstance(part, dict):
        continue
      if part.get("type") == "text":
        blocks.text(str(part.get("text") or ""), role=role)
      elif part.get("type") == "tool_use":
        name = str(part.get("name") or "tool")
        blocks.tool(part.get("id"), name, summarize_tool_input(name, part.get("input")))
      elif part.get("type") == "tool_result":
        blocks.result(
          part.get("tool_use_id"),
          _joined_text(part.get("content"), "text"),
          failed=bool(part.get("is_error")),
        )
  return blocks


# Codex runs shell commands through its code-mode ``exec`` tool, whose input is
# a JavaScript snippet; the command string inside it is what the owner reads.
_CODEX_CMD = re.compile(r"""\bcmd\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)""")
_CODEX_SHELL_TOOLS = ("exec", "exec_command", "shell")
_CODEX_TOOL_NAMES = {
  **{name: "Bash" for name in _CODEX_SHELL_TOOLS},
  "apply_patch": "Edit",
  "web_search": "WebSearch",
  "view_image": "ViewImage",
}


def _codex_agent_name(path: Any) -> str:
  """``/root/fix_login`` reads as ``Fix login``; the root is the main agent."""
  leaf = str(path or "").rstrip("/").rsplit("/", 1)[-1]
  if leaf in ("", "root"):
    return "the main agent"
  words = re.sub(r"[_\-]+", " ", leaf).strip()
  return words[:1].upper() + words[1:]


def _codex_tool_label(payload: dict) -> str:
  """The chat's tool vocabulary for one Codex rollout tool call."""
  if payload.get("namespace") == "agents":
    return "Agent"
  name = str(payload.get("name") or "")
  return _CODEX_TOOL_NAMES.get(name, name or "tool")


def _codex_tool_summary(payload: dict) -> str:
  name = payload.get("name")
  raw = payload.get("input") if payload.get("type") == "custom_tool_call" else payload.get("arguments")
  if payload.get("namespace") == "agents":
    # Inter-agent message bodies are provider-encrypted; name only the target.
    try:
      args = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
      args = None
    target = args.get("target") if isinstance(args, dict) else None
    return f"{str(name or 'message').replace('_', ' ')} → {_codex_agent_name(target)}"
  text = raw if isinstance(raw, str) else json.dumps(raw or "")
  if name in _CODEX_SHELL_TOOLS:
    commands = []
    for match in _CODEX_CMD.finditer(text):
      literal = match.group(1)
      try:
        commands.append(json.loads(literal) if literal[0] == '"' else literal[1:-1])
      except ValueError:
        commands.append(literal[1:-1])
    if commands:
      return "\n".join(commands)
  return text


def _codex_output(payload: dict) -> str:
  """Tool output text, unwrapped from code-mode's command-result envelope."""
  output = payload.get("output")
  if isinstance(output, str):
    return output
  parts = []
  for part in output if isinstance(output, list) else []:
    text = part.get("text") if isinstance(part, dict) else None
    if not isinstance(text, str):
      continue
    try:
      result = json.loads(text)
    except ValueError:
      result = None
    if isinstance(result, dict) and isinstance(result.get("output"), str):
      exit_code = result.get("exit_code")
      suffix = f"\n[exit {exit_code}]" if exit_code not in (None, 0) else ""
      parts.append(result["output"] + suffix)
    elif not text.startswith("Script completed"):
      parts.append(text)
  return "\n".join(parts)


def _codex_task_line(payload: dict) -> str:
  """Plain-language line for an incoming, provider-encrypted agent message."""
  header = _joined_text(payload.get("content"), "input_text", "text")
  fields = {
    key.strip().lower(): value.strip()
    for key, _, value in (line.partition(":") for line in header.splitlines())
    if value.strip()
  }
  sender = _codex_agent_name(fields.get("sender") or payload.get("author"))
  label = "New task" if "NEW_TASK" in fields.get("message type", "NEW_TASK") else "Message"
  if fields.get("task name"):
    label += f": {_codex_agent_name(fields['task name'])}"
  return f"{label} — from {sender}. Codex keeps the message text encrypted."


def _codex_blocks(records: list[dict], *, from_start: bool) -> _Blocks:
  """Project a Codex child rollout (``response_item`` records).

  A spawned Codex thread starts with a copy of recent parent context; its own
  work begins at its first ``task_started`` event. A tail read that skipped
  the file's start is already inside the child's own work.
  """
  blocks = _Blocks()
  started = not from_start
  for record in records:
    payload = record.get("payload")
    if not isinstance(payload, dict):
      continue
    kind = payload.get("type")
    if record.get("type") == "event_msg":
      started = started or kind == "task_started"
      continue
    if not started:
      continue
    if kind == "agent_message":
      blocks.text(_codex_task_line(payload), role="user")
    elif kind == "message" and payload.get("role") == "assistant":
      blocks.text(_joined_text(payload.get("content"), "output_text", "text"))
    elif kind in ("custom_tool_call", "function_call"):
      blocks.tool(
        payload.get("call_id") or payload.get("id"),
        _codex_tool_label(payload), _codex_tool_summary(payload),
      )
    elif kind in ("custom_tool_call_output", "function_call_output"):
      blocks.result(payload.get("call_id"), _codex_output(payload))
  return blocks


# The newest records name a child's current tool and latest answer; a small
# tail keeps a running row's periodic re-read cheap even for a long rollout.
_ACTIVITY_TAIL_BYTES = 256 * 1024


def codex_helper_activity(thread_id: str) -> tuple[str | None, str | None]:
  """``(current tool, latest completed answer)`` of one Codex child thread.

  The parent turn's notification stream carries only lifecycle markers for a
  spawned child, never its tool calls or its answer; the child's own rollout
  records both. An answer belongs to the latest task only, so a newer
  ``task_started`` clears it.
  """
  path = _codex_rollout_path(thread_id)
  if path is None:
    return None, None
  try:
    records, _ = _tail_records(path, _ACTIVITY_TAIL_BYTES)
  except OSError:
    return None, None
  tool = answer = None
  for record in records:
    payload = record.get("payload")
    if not isinstance(payload, dict):
      continue
    kind = payload.get("type")
    if kind in ("custom_tool_call", "function_call"):
      tool = _codex_tool_label(payload)
    elif record.get("type") == "event_msg" and kind == "task_started":
      answer = None
    elif record.get("type") == "event_msg" and kind == "task_complete":
      message = payload.get("last_agent_message")
      answer = message.strip() if isinstance(message, str) and message.strip() else None
  return tool, answer


def codex_helper_row_name(agent_path: Any) -> str | None:
  """A spawned child's owner-facing row name, e.g. ``Fix login``."""
  leaf = str(agent_path or "").rstrip("/").rsplit("/", 1)[-1]
  return None if leaf in ("", "root") else _codex_agent_name(leaf)
