#!/usr/bin/env python3
"""Small stdio MCP server for run-bound Möbius control operations.

This server deliberately uses only the Python standard library.  It exposes a
single platform-owned tool, so importing the general FastMCP stack for every
active Claude and Codex turn would spend substantially more memory than the
tool itself needs.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, TextIO


SERVER_NAME = "Möbius control"
SERVER_VERSION = "1.0.0"
LATEST_PROTOCOL_VERSION = "2025-11-25"
SUPPORTED_PROTOCOL_VERSIONS = {
  "2024-11-05",
  "2025-03-26",
  "2025-06-18",
  LATEST_PROTOCOL_VERSION,
}
TOOL_NAME = "promote_goal"
TOOL_DESCRIPTION = (
  "Promote the current ordinary top-level owner turn into a durable, "
  "platform-owned Goal after the goal-planning criteria are satisfied. "
  "Use at task start or when an owner choice, investigation, or discovery "
  "turns bounded work into a multi-stage outcome. Do not use for questions, "
  "honest one-turn work, or delegated children."
)


def _goal_promote_module() -> ModuleType:
  path = Path(__file__).with_name("goal_promote.py")
  spec = importlib.util.spec_from_file_location("mobius_goal_promote", path)
  if spec is None or spec.loader is None:  # pragma: no cover - import invariant
    raise RuntimeError("goal promotion helper is unavailable")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


_GOALS = _goal_promote_module()


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
  return {
    "tools": [{
      "name": TOOL_NAME,
      "description": TOOL_DESCRIPTION,
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
    }],
  }


def _call_tool(params: Any) -> dict[str, Any]:
  if not isinstance(params, dict) or params.get("name") != TOOL_NAME:
    return _tool_result("Unknown tool.", is_error=True)
  arguments = params.get("arguments")
  if not isinstance(arguments, dict):
    return _tool_result("Tool arguments must be an object.", is_error=True)
  objective = arguments.get("objective")
  if not isinstance(objective, str) or not objective.strip():
    return _tool_result("objective must be a non-empty string.", is_error=True)
  try:
    return _tool_result(_promote_goal(objective.strip()))
  except Exception as exc:  # Tool failures are data; keep the MCP server alive.
    return _tool_result(str(exc) or "Goal promotion failed.", is_error=True)


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
