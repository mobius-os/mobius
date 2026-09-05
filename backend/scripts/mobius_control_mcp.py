#!/usr/bin/env python3
"""Small stdio MCP server for run-bound Möbius control operations.

This server deliberately uses only the Python standard library. Importing the
general FastMCP stack for every active Claude and Codex turn would spend
substantially more memory than this small control surface needs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO


SERVER_NAME = "Möbius control"
SERVER_VERSION = "1.2.0"
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
  "2024-11-05",
  "2025-03-26",
  "2025-06-18",
  LATEST_PROTOCOL_VERSION,
}
PROMOTE_GOAL_TOOL = "promote_goal"
DECLARE_WAIT_TOOL = "declare_wait"
REQUEST_APPROVAL_TOOL = "request_approval"
REQUEST_QUESTION_TOOL = "request_question"
PROMOTE_GOAL_DESCRIPTION = (
  "Promote the current ordinary top-level owner turn into a durable, "
  "platform-owned Goal after the goal-planning criteria are satisfied. "
  "Use at task start or when an owner choice, investigation, or discovery "
  "turns bounded work into a multi-stage outcome. Do not use for questions, "
  "honest one-turn work, or delegated children."
)
DECLARE_WAIT_DESCRIPTION = (
  "Persist a cross-turn wait so this chat resumes automatically after an "
  "external condition or timer, including across server restarts. Call this "
  "before ending a turn whenever you promise to continue later and no "
  "provider-native task already owns that lifecycle. Supply exactly one of "
  "command or delay_secs. A command must be a read-only check: exit 0 means "
  "met, silent exit 1 means not yet, and any other result wakes the chat as "
  "a failed check. Timers and polling intervals have a 60-second minimum. "
  "The default interval is 300 seconds; the default deadline is one day and "
  "the maximum is seven days. A deadline always wakes the chat instead of "
  "leaving it stuck forever."
)


def _helper_module(filename: str, module_name: str) -> ModuleType:
  path = Path(__file__).with_name(filename)
  spec = importlib.util.spec_from_file_location(module_name, path)
  if spec is None or spec.loader is None:  # pragma: no cover - import invariant
    raise RuntimeError(f"{filename} helper is unavailable")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


_GOALS = _helper_module("goal_promote.py", "mobius_goal_promote")
_WAITS = _helper_module("chat_wait.py", "mobius_chat_wait")
_APPROVALS = _helper_module("owner_approval.py", "mobius_owner_approval")


def _promote_goal(objective: str) -> dict:
  try:
    payload = _GOALS.promote_goal(objective)
  except SystemExit as exc:
    raise RuntimeError(str(exc)) from exc
  return {
    "state": payload["state"],
    "objective": payload["objective"],
    "goal_id": payload["root_run_id"],
    "run_id": payload["run_id"],
  }


def _declare_wait(
  description: str,
  *,
  command: str | None = None,
  delay_secs: int | None = None,
  interval_secs: int | None = None,
  deadline_secs: int | None = None,
) -> dict:
  try:
    return _WAITS.declare_wait(
      description,
      command=command,
      delay_secs=delay_secs,
      interval_secs=interval_secs,
      deadline_secs=deadline_secs,
    )
  except SystemExit as exc:
    raise RuntimeError(str(exc)) from exc


def _response(message_id: Any, result: Any) -> dict[str, Any]:
  return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(
  message_id: Any,
  code: int,
  message: str,
) -> dict[str, Any]:
  return {
    "jsonrpc": "2.0",
    "id": message_id,
    "error": {"code": code, "message": message},
  }


def _tool_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
  text = value if isinstance(value, str) else json.dumps(
    value,
    ensure_ascii=False,
    separators=(",", ":"),
  )
  return {
    "content": [{"type": "text", "text": text}],
    "isError": is_error,
  }


def _initialize_result(params: Any) -> dict[str, Any]:
  requested = params.get("protocolVersion") if isinstance(params, dict) else None
  protocol_version = (
    requested if requested in SUPPORTED_PROTOCOL_VERSIONS
    else LATEST_PROTOCOL_VERSION
  )
  return {
    "protocolVersion": protocol_version,
    "capabilities": {"tools": {"listChanged": False}},
    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    "instructions": (
      "Run-bound platform controls for the current top-level owner turn. "
      "Never use these tools from a delegated child."
    ),
  }


def _tools_list_result() -> dict[str, Any]:
  return {"tools": list(_TOOL_DEFINITIONS.values())}


def _call_promote_goal(arguments: dict[str, Any]) -> dict:
  if set(arguments) != {"objective"}:
    raise ValueError("promote_goal needs exactly one objective")
  objective = arguments.get("objective")
  if not isinstance(objective, str) or not objective.strip():
    raise ValueError("objective must be a non-empty string")
  return _promote_goal(objective.strip())


def _call_request_approval(arguments: dict[str, Any]) -> dict:
  if set(arguments) != {"question", "options"}:
    raise ValueError("request_approval needs question and options")
  try:
    return _APPROVALS.request_approval(**arguments)
  except SystemExit as exc:
    raise RuntimeError(str(exc)) from exc


def _call_request_question(arguments: dict[str, Any]) -> dict:
  if set(arguments) != {"questions"}:
    raise ValueError("request_question needs questions")
  try:
    return _APPROVALS.request_question(**arguments)
  except SystemExit as exc:
    raise RuntimeError(str(exc)) from exc


def _optional_int(arguments: dict[str, Any], name: str) -> int | None:
  value = arguments.get(name)
  if value is None:
    return None
  if isinstance(value, bool) or not isinstance(value, int):
    raise ValueError(f"{name} must be an integer")
  return value


def _call_declare_wait(arguments: dict[str, Any]) -> dict:
  allowed = {
    "description", "command", "delay_secs", "interval_secs", "deadline_secs",
  }
  if not set(arguments).issubset(allowed):
    raise ValueError("declare_wait received unknown arguments")
  description = arguments.get("description")
  if not isinstance(description, str) or not description.strip():
    raise ValueError("description must be a non-empty string")
  command = arguments.get("command")
  if command is not None and not isinstance(command, str):
    raise ValueError("command must be a string")
  return _declare_wait(
    description.strip(),
    command=command,
    delay_secs=_optional_int(arguments, "delay_secs"),
    interval_secs=_optional_int(arguments, "interval_secs"),
    deadline_secs=_optional_int(arguments, "deadline_secs"),
  )


_TOOL_DEFINITIONS = {
  REQUEST_APPROVAL_TOOL: {
    "name": REQUEST_APPROVAL_TOOL,
    "description": (
      "Ask the owner to approve a proposed Möbius action, including a server "
      "restart. This is an application decision, not a sandbox or tool-permission "
      "escalation. Saves an ordinary answerable question card and returns a "
      "receipt immediately, NOT an answer or permission. After success, end "
      "the turn without further text or tools. Put all explanation, preparation and "
      "closeout BEFORE this final call. The owner's answer resumes "
      "the chat; no process needs to wait, and there is no human-answer timeout. "
      "Use this instead of the provider's clarifying-question tool for owner "
      "approvals. Explain the action and its impact in the question and option "
      "descriptions. Include a decline/defer choice. Identical retries within "
      "a turn reuse the same saved card. Never request secrets through this tool. "
      "Background agents leave approvals pending for a live chat instead."
    ),
    "inputSchema": {
      "type": "object",
      "properties": {
        "question": {"type": "string", "minLength": 1, "maxLength": 2000},
        "options": {
          "type": "array", "minItems": 2, "maxItems": 3,
          "items": {
            "type": "object",
            "properties": {
              "label": {"type": "string", "minLength": 1, "maxLength": 100},
              "description": {"type": "string", "minLength": 1, "maxLength": 500},
            },
            "required": ["label", "description"], "additionalProperties": False,
          },
        },
      },
      "required": ["question", "options"], "additionalProperties": False,
    },
  },
  REQUEST_QUESTION_TOOL: {
    "name": REQUEST_QUESTION_TOOL,
    "description": (
      "Ask 1–3 ordinary clarifying questions as the FINAL action of your turn. "
      "Finish useful preparation, explanation and closeout BEFORE this call. "
      "The saved card blocks further work until the owner answers or Stops; "
      "it returns a receipt, NOT an answer. After success end immediately with "
      "no further text or tools. Do not guess, poll or keep a process waiting. "
      "The saved answer resumes the chat even after a restart. Prefer this "
      "over provider-native questions in live owner chats. Use request_approval "
      "for permission; use the sealed secure-input helper for secrets. "
      "Never use in background or scheduled work."
    ),
    "inputSchema": {
      "type": "object", "additionalProperties": False,
      "required": ["questions"],
      "properties": {"questions": {
        "type": "array", "minItems": 1, "maxItems": 3,
        "items": {
          "type": "object", "additionalProperties": False,
          "required": ["id", "header", "question", "options"],
          "properties": {
            "id": {"type": "string"}, "header": {"type": "string"},
            "question": {"type": "string"},
            "options": {"type": "array", "maxItems": 3, "items": {
              "type": "object", "additionalProperties": False,
              "required": ["label", "description"],
              "properties": {"label": {"type": "string"},
                             "description": {"type": "string"}},
            }},
          },
        },
      }},
    },
  },
  PROMOTE_GOAL_TOOL: {
    "name": PROMOTE_GOAL_TOOL,
    "description": PROMOTE_GOAL_DESCRIPTION,
    "inputSchema": {
      "type": "object",
      "properties": {
        "objective": {
          "type": "string",
          "description": "Concise outcome and observable completion condition.",
        },
      },
      "required": ["objective"],
      "additionalProperties": False,
    },
  },
  DECLARE_WAIT_TOOL: {
    "name": DECLARE_WAIT_TOOL,
    "description": DECLARE_WAIT_DESCRIPTION,
    "inputSchema": {
      "type": "object",
      "properties": {
        "description": {
          "type": "string",
          "description": "Plain-language condition this chat will resume for.",
        },
        "command": {
          "type": "string",
          "description": "Read-only shell check with 0/1/error exit semantics.",
        },
        "delay_secs": {
          "type": "integer",
          "description": "Timer delay in seconds, minimum 60.",
        },
        "interval_secs": {
          "type": "integer",
          "description": "Command polling interval in seconds, minimum 60.",
        },
        "deadline_secs": {
          "type": "integer",
          "description": "Wake-up deadline in seconds, maximum 604800.",
        },
      },
      "required": ["description"],
      "additionalProperties": False,
    },
  },
}

_TOOL_HANDLERS = {
  REQUEST_APPROVAL_TOOL: _call_request_approval,
  REQUEST_QUESTION_TOOL: _call_request_question,
  PROMOTE_GOAL_TOOL: _call_promote_goal,
  DECLARE_WAIT_TOOL: _call_declare_wait,
}


def _call_tool(params: Any) -> dict[str, Any]:
  if not isinstance(params, dict):
    return _tool_result("Tool call must be an object.", is_error=True)
  name = params.get("name")
  handler = _TOOL_HANDLERS.get(name) if isinstance(name, str) else None
  if handler is None:
    return _tool_result("Unknown tool.", is_error=True)
  arguments = params.get("arguments")
  if not isinstance(arguments, dict):
    return _tool_result("Tool arguments must be an object.", is_error=True)
  try:
    return _tool_result(handler(arguments))
  except Exception as exc:  # Tool failures are data; keep the MCP server alive.
    return _tool_result(str(exc) or "Tool call failed.", is_error=True)


def _dispatch_message(message: Any) -> dict[str, Any] | None:
  if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
    return _error(None, -32600, "Invalid Request")
  method = message.get("method")
  if not isinstance(method, str):
    return _error(message.get("id"), -32600, "Invalid Request")

  # JSON-RPC notifications never receive a response.  MCP uses this for the
  # initialized/cancelled/progress lifecycle, none of which needs local state.
  if "id" not in message:
    return None

  message_id = message["id"]
  params = message.get("params")
  if method == "initialize":
    return _response(message_id, _initialize_result(params))
  if method == "ping":
    return _response(message_id, {})
  if method == "tools/list":
    return _response(message_id, _tools_list_result())
  if method == "tools/call":
    return _response(message_id, _call_tool(params))
  return _error(message_id, -32601, "Method not found")


def _write_message(stream: TextIO, message: dict[str, Any]) -> None:
  stream.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")))
  stream.write("\n")
  stream.flush()


def serve(input_stream: TextIO, output_stream: TextIO) -> None:
  """Serve newline-delimited JSON-RPC until the provider closes stdin."""
  for raw_line in input_stream:
    if not raw_line.strip():
      continue
    try:
      message = json.loads(raw_line)
    except json.JSONDecodeError:
      _write_message(output_stream, _error(None, -32700, "Parse error"))
      continue
    try:
      response = _dispatch_message(message)
    except Exception:  # Keep a malformed request from terminating the server.
      message_id = message.get("id") if isinstance(message, dict) else None
      response = _error(message_id, -32603, "Internal error")
    if response is not None:
      _write_message(output_stream, response)


if __name__ == "__main__":
  serve(sys.stdin, sys.stdout)
