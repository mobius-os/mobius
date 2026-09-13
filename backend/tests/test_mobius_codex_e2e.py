"""Installed-Codex contract check for the local Möbius trial provider.

This intentionally uses the real ``codex`` binary with a loopback-only fake
Responses server. It catches drift in the custom model catalog, request shape,
stream event shape, and the shell tool-call round trip without contacting a
commercial model provider.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.providers import MobiusProvider


def _response(response_id: str, output: list[dict]) -> dict:
  return {
    "id": response_id,
    "object": "response",
    "created_at": 0,
    "status": "completed",
    "error": None,
    "incomplete_details": None,
    "instructions": None,
    "max_output_tokens": 32768,
    "model": "inkling",
    "output": output,
    "parallel_tool_calls": True,
    "previous_response_id": None,
    "reasoning": {"effort": "medium", "summary": None},
    "store": False,
    "temperature": 1.0,
    "text": {"format": {"type": "text"}},
    "tool_choice": "auto",
    "tools": [],
    "top_p": 1.0,
    "truncation": "disabled",
    "usage": {
      "input_tokens": 12,
      "input_tokens_details": {"cached_tokens": 0},
      "output_tokens": 4,
      "output_tokens_details": {"reasoning_tokens": 0},
      "total_tokens": 16,
    },
  }


def _sse(response: dict) -> bytes:
  item = response["output"][0]
  events = [
    {
      "event": "response.output_item.done",
      "data": {
        "type": "response.output_item.done",
        "sequence_number": 0,
        "output_index": 0,
        "item": item,
      },
    },
    {
      "event": "response.completed",
      "data": {
        "type": "response.completed",
        "sequence_number": 1,
        "response": response,
      },
    },
  ]
  return b"".join(
    f"event: {row['event']}\ndata: {json.dumps(row['data'], separators=(',', ':'))}\n\n".encode()
    for row in events
  )


def test_installed_codex_streams_tool_call_through_trial_catalog(tmp_path):
  codex = shutil.which("codex")
  if codex is None:
    pytest.skip("installed Codex CLI is required for this contract test")

  requests: list[dict] = []
  proof = tmp_path / "codex-tool-proof.txt"

  class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler contract
      size = int(self.headers.get("content-length", "0"))
      payload = json.loads(self.rfile.read(size))
      requests.append(payload)
      has_tool_output = any(
        isinstance(item, dict) and item.get("type") == "function_call_output"
        for item in payload.get("input", [])
      )
      if not has_tool_output:
        tool_name = next(
          (
            tool.get("name")
            for tool in payload["tools"]
            if tool.get("name") in {"exec_command", "shell_command"}
          ),
          None,
        )
        if tool_name is not None:
          item = {
            "id": "fc_mobius_e2e",
            "type": "function_call",
            "status": "completed",
            "arguments": json.dumps(
              {
                "cmd" if tool_name == "exec_command" else "command":
                  f"printf mobius-tool-ok > {proof.name}"
              },
              separators=(",", ":"),
            ),
            "call_id": "call_mobius_e2e",
            "name": tool_name,
          }
          response = _response("resp_tool", [item])
        else:
          response = _response("resp_no_tool", [{
            "id": "msg_no_tool", "type": "message", "role": "assistant",
            "status": "completed", "content": [{
              "type": "output_text", "text": "no-function-tool",
              "annotations": [], "logprobs": [],
            }],
          }])
      else:
        item = {
          "id": "msg_mobius_e2e",
          "type": "message",
          "role": "assistant",
          "status": "completed",
          "content": [
            {
              "type": "output_text",
              "text": "mobius-e2e-complete",
              "annotations": [],
              "logprobs": [],
            }
          ],
        }
        response = _response("resp_final", [item])
      content = _sse(response)
      self.send_response(200)
      self.send_header("content-type", "text/event-stream")
      self.send_header("content-length", str(len(content)))
      self.send_header("connection", "close")
      self.end_headers()
      self.wfile.write(content)

    def log_message(self, _format, *_args):
      return

  try:
    server = ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
  except OSError as exc:
    pytest.skip(f"local broker test port is unavailable: {exc}")
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  try:
    env = MobiusProvider().build_env(
      dict(os.environ), str(tmp_path / "data"), "e2e-chat"
    )
    result = subprocess.run(
      [
        codex,
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-C",
        str(tmp_path),
        "Use the shell tool once, then report completion.",
      ],
      env=env,
      text=True,
      capture_output=True,
      timeout=30,
      check=False,
    )
  finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)

  assert result.returncode == 0, result.stderr
  assert proof.exists(), [
    (tool.get("type"), tool.get("name"))
    for tool in requests[0].get("tools", [])
  ]
  assert proof.read_text() == "mobius-tool-ok"
  assert "mobius-e2e-complete" in result.stdout
  assert len(requests) == 2
  assert all(request["model"] == "inkling" for request in requests)
  assert all(request["stream"] is True for request in requests)
  assert any(
    item.get("type") == "function_call_output"
    for item in requests[1]["input"]
  )
