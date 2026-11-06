"""Hermetic end-to-end proof that a failing PreToolUse hook fails closed.

Möbius's identity/env PreToolUse hook (``app.claude_helper_host``) sets
``on_failure="block"``. This drives the bundled Claude Code CLI through
claude_agent_sdk against a local fake Anthropic Messages API that asks for one
Bash call, with a hook that raises. Under "block" the command never runs; the
unset policy is recorded too, as the fail-open baseline the policy replaces.

No real model or credential is used: the CLI environment is rebuilt from an
allowlist with HOME and CLAUDE_CONFIG_DIR in a temp dir.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sdk = pytest.importorskip("claude_agent_sdk")

CLI = Path(sdk.__file__).parent / "_bundled" / "claude"
if not CLI.exists():  # pragma: no cover - environment dependent
  pytest.skip("bundled Claude Code CLI is absent", allow_module_level=True)


class _FakeModel:
  """First turn asks for one Bash call; any later turn ends with text."""

  def __init__(self, command: str):
    self.command = command
    self.tool_results: list[dict] = []
    self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
    threading.Thread(target=self.server.serve_forever, daemon=True).start()

  @property
  def url(self) -> str:
    return f"http://127.0.0.1:{self.server.server_address[1]}"

  def blocks(self, body) -> list[dict]:
    if "PROBE-FAIL-CLOSED" not in json.dumps(body.get("system") or ""):
      return [{"type": "text", "text": "OK"}]
    last = (body.get("messages") or [{}])[-1]
    content = last.get("content") if isinstance(last.get("content"), list) else []
    results = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"]
    if results:
      self.tool_results.extend(results)
      return [{"type": "text", "text": "DONE"}]
    return [{"type": "tool_use", "id": "toolu_probe_001", "name": "Bash",
             "input": {"command": self.command, "description": "probe"}}]

  def _handler(self):
    fake = self

    class Handler(BaseHTTPRequestHandler):
      protocol_version = "HTTP/1.1"

      def log_message(self, *args):
        pass

      def _send(self, data: bytes, content_type: str):
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

      def do_GET(self):
        self._send(json.dumps({"data": [], "has_more": False}).encode(), "application/json")

      def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        body = json.loads(raw or b"{}")
        path = self.path.split("?")[0]
        if path.endswith("/count_tokens"):
          return self._send(b'{"input_tokens": 10}', "application/json")
        if not path.endswith("/v1/messages"):
          return self._send(b"{}", "application/json")
        blocks = fake.blocks(body)
        stop = "tool_use" if blocks[0]["type"] == "tool_use" else "end_turn"
        usage = {"input_tokens": 10, "output_tokens": 5,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        message = {"id": "msg_probe", "type": "message", "role": "assistant",
                   "model": body.get("model") or "claude-fake", "content": [],
                   "stop_reason": None, "stop_sequence": None, "usage": usage}
        if not body.get("stream"):
          message.update(content=blocks, stop_reason=stop)
          return self._send(json.dumps(message).encode(), "application/json")
        events = [("message_start", {"type": "message_start", "message": message})]
        for index, block in enumerate(blocks):
          if block["type"] == "text":
            start = {"type": "text", "text": ""}
            delta = {"type": "text_delta", "text": block["text"]}
          else:
            start = {**block, "input": {}}
            delta = {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
          events += [
            ("content_block_start", {"type": "content_block_start", "index": index,
                                     "content_block": start}),
            ("content_block_delta", {"type": "content_block_delta", "index": index,
                                     "delta": delta}),
            ("content_block_stop", {"type": "content_block_stop", "index": index}),
          ]
        events += [
          ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 5},
                             "delta": {"stop_reason": stop, "stop_sequence": None}}),
          ("message_stop", {"type": "message_stop"}),
        ]
        self._send("".join(
          f"event: {name}\ndata: {json.dumps(payload)}\n\n" for name, payload in events
        ).encode(), "text/event-stream")

    return Handler


@pytest.fixture
def probe(monkeypatch, tmp_path):
  marker = tmp_path / "ran"
  fake = _FakeModel(f"touch {marker}")
  for key in list(os.environ):
    if key not in ("PATH", "LANG", "LC_ALL", "TERM", "TZ"):
      monkeypatch.delenv(key, raising=False)
  home = tmp_path / "home"
  (home / ".claude").mkdir(parents=True)
  for key, value in {
    "HOME": str(home), "CLAUDE_CONFIG_DIR": str(home / ".claude"),
    "TMPDIR": str(tmp_path), "ANTHROPIC_API_KEY": "sk-ant-fake-probe",
    "ANTHROPIC_BASE_URL": fake.url,
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
    "DISABLE_ERROR_REPORTING": "1", "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1",
  }.items():
    monkeypatch.setenv(key, value)
  yield fake, marker, tmp_path
  fake.server.shutdown()
  fake.server.server_close()


async def _run_with_raising_hook(cwd: Path, on_failure) -> list[str]:
  from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, HookMatcher
  from claude_agent_sdk.types import PermissionResultAllow

  calls: list[str] = []

  async def raising_hook(data, tool_use_id, context):
    calls.append(data.get("tool_name"))
    raise RuntimeError("identity lookup failed")

  async def allow_all(name, tool_input, context):
    return PermissionResultAllow(updated_input=tool_input)

  options = ClaudeAgentOptions(
    system_prompt="PROBE-FAIL-CLOSED", cwd=str(cwd), cli_path=str(CLI),
    permission_mode="default", can_use_tool=allow_all, model="claude-fake",
    hooks={"PreToolUse": [HookMatcher(
      matcher=None, hooks=[raising_hook], on_failure=on_failure,
    )]},
  )
  async with ClaudeSDKClient(options) as client:
    await client.query("Run the probe.")
    async for _message in client.receive_response():
      pass
  return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("on_failure,runs", [("block", False), (None, True)],
                         ids=["block", "unset-fails-open"])
async def test_raising_pre_tool_use_hook_blocks_only_under_block_policy(probe, on_failure, runs):
  fake, marker, cwd = probe
  calls = await asyncio.wait_for(_run_with_raising_hook(cwd, on_failure), timeout=90)
  assert calls == ["Bash"]
  assert marker.exists() is runs
  assert len(fake.tool_results) == 1
  if not runs:
    assert fake.tool_results[0].get("is_error") is True
