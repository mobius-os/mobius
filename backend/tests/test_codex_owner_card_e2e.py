"""Real Codex hook/interrupt ordering with loopback-only fake model responses."""

import asyncio
import json
import os
import shlex
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from app.codex_sdk_runner import (
  _codex_owner_card_hook_override, _codex_owner_card_hook_thread_config,
  _recover_unfinished_codex_messages, _sdk_imports,
)
from app.events import process_event
from app.providers import MobiusProvider
from tests.test_mobius_codex_e2e import _response, _sse


@pytest.mark.parametrize("tool_path,resume", [("shell", False), ("mcp", False), ("mcp", True)])
def test_installed_codex_card_hook_prevents_a_post_receipt_model_request(tmp_path, tool_path, resume):
  if not shutil.which("codex"):
    pytest.skip("installed Codex is required")
  from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
  from openai_codex.types import Personality

  model_requests = []
  card_ends = []
  state = {}
  receipt = {"state": "waiting_for_owner", "question_id": "local-card", "next_action": "End"}
  preceding_reply = "| Operation | Current |\n|---|---|\n| Read | Full history |"
  # An unrelated user hook stays untrusted. This is an isolated test home,
  # never the owner's provider config.
  untrusted_marker = tmp_path / "untrusted-hook-ran"
  codex_home = tmp_path / "codex-home"
  codex_home.mkdir()
  (codex_home / "config.toml").write_text(
    'hooks.PostToolUse=[{matcher=".*",hooks=[{type="command",command='
    + json.dumps("touch " + shlex.quote(str(untrusted_marker))) + '}]}]\n',
  )
  mcp_server = tmp_path / "cards.py"
  mcp_server.write_text(
    "from mcp.server.fastmcp import FastMCP\n"
    "server = FastMCP('cards')\n"
    "@server.tool()\ndef request_question() -> dict:\n"
    + f"  return {receipt!r}\n"
    + "server.run()\n",
  )

  class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
      body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))))
      if self.path.endswith("/owner-card-end"):
        card_ends.append(body)
        asyncio.run_coroutine_threadsafe(
          state["turn"].interrupt(), state["loop"],
        ).result(timeout=10)
        content = b'{"ended":true}'
        content_type = "application/json"
      elif state.get("warming"):
        content = _sse(_response("seed", [{
          "id": "seed-reply", "type": "message", "role": "assistant",
          "phase": "final_answer", "status": "completed",
          "content": [{"type": "output_text", "text": "Ready", "annotations": [], "logprobs": []}],
        }]))
        content_type = "text/event-stream"
      else:
        model_requests.append(body)
        if len(model_requests) == 1:
          names = [t.get("name") for t in body["tools"]]
          tool = (next(n for n in names if n in {"exec_command", "shell_command"})
                  if tool_path == "shell" else "request_question")
          assert tool in names or "mcp__mobius_control" in names, names
          item = {
            "id": "card-tool", "type": "function_call", "status": "completed",
            "call_id": "card-tool-call", "name": tool,
            "arguments": json.dumps({
              "cmd" if tool == "exec_command" else "command":
                "printf '%s' " + shlex.quote(json.dumps(receipt)),
            } if tool_path == "shell" else {}),
          }
          if tool_path == "mcp":
            item["namespace"] = "mcp__mobius_control"
        else:
          item = {
            "id": "leaked-message", "type": "message", "role": "assistant",
            "status": "completed", "phase": "final_answer",
            "content": [{"type": "output_text", "text": "POST-CARD LEAK",
                         "annotations": [], "logprobs": []}],
          }
        output = [item]
        if len(model_requests) == 1:
          # Preserve an already-complete reply before the card/tool event.
          reply = {"id": "preceding-reply", "type": "message", "role": "assistant",
                   "status": "completed", "phase": "commentary",
                   "content": [{"type": "output_text", "text": preceding_reply,
                                "annotations": [], "logprobs": []}]}
          output.insert(0, reply)
        response = _response(f"response-{len(model_requests)}", output)
        data = [{"type": "response.output_item.done", "output_index": i,
                 "sequence_number": i, "item": completed_item}
                for i, completed_item in enumerate(output)]
        data.append({"type": "response.completed", "sequence_number": len(output),
                     "response": response})
        content = b"".join(f"event: {row['type']}\ndata: {json.dumps(row)}\n\n".encode()
                           for row in data)
        content_type = "text/event-stream"
      self.send_response(200)
      self.send_header("content-type", content_type)
      self.send_header("content-length", str(len(content)))
      self.send_header("connection", "close")
      self.end_headers()
      self.wfile.write(content)

    def log_message(self, *_args):
      pass

  server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
  worker = threading.Thread(target=server.serve_forever, daemon=True)
  worker.start()
  base_url = f"http://127.0.0.1:{server.server_port}"
  env = {**os.environ, "CODEX_HOME": str(codex_home),
         "CHAT_ID": "local-chat", "API_BASE_URL": base_url,
         "AGENT_TOKEN": "test-only", "MOBIUS_LOCAL_BROKER_KEY": "test-only"}
  overrides = [
    *MobiusProvider().codex_config_overrides(),
    f'model_providers.mobius_trial.base_url="{base_url}/v1"',
    'model="inkling"', 'web_search="disabled"',
    _codex_owner_card_hook_override(),
  ]

  async def scenario():
    state["loop"] = asyncio.get_running_loop()
    config = CodexConfig(codex_bin=shutil.which("codex"), cwd=str(tmp_path),
                         env=env, config_overrides=overrides)
    if resume:
      # Resume on a fresh app-server, like a later Möbius turn, not an
      # already-loaded thread where Codex may ignore new config.
      state["warming"] = True
      async with AsyncCodex(config=config) as seed:
        seed_thread = await seed.thread_start(model="inkling", sandbox=Sandbox.full_access)
        seed_turn = await seed_thread.turn("Initialize the conversation.")
        async for note in seed_turn.stream():
          if note.method == "turn/completed":
            break
        session_id = seed_thread.id
      state["warming"] = False
    async with AsyncCodex(config=config) as codex:
      from app.codex_sdk_contract import control_client
      from openai_codex.generated.v2_all import HooksListResponse
      state["hooks"] = str(await control_client(codex).request(
        "hooks/list", {"cwds": [str(tmp_path)]}, response_model=HooksListResponse,
      ))
      thread_config = await _codex_owner_card_hook_thread_config(
        codex, {"HooksListResponse": HooksListResponse}, str(tmp_path),
        {"mcp_servers": {"mobius_control": {"command": sys.executable,
                                             "args": [str(mcp_server)]}}},
      )
      kwargs = dict(
        cwd=str(tmp_path), model="inkling", approval_mode=ApprovalMode.deny_all,
        sandbox=Sandbox.full_access, personality=Personality.none,
        config=thread_config,
      )
      thread = (await codex.thread_resume(session_id, **kwargs) if resume
                else await codex.thread_start(**kwargs))
      state["turn"] = await thread.turn("Run the card tool once.")
      async for notification in state["turn"].stream():
        if "hook" in notification.method.lower() or "error" in notification.method.lower():
          state.setdefault("notes", []).append(str(notification))
        if notification.method == "item/completed":
          item = notification.payload.item.root
          if getattr(item, "type", None) == "agentMessage":
            state.setdefault("messages", []).append(item.text)
        if notification.method == "turn/completed":
          class Bus:
            def __init__(self):
              self.blocks = [{"type": "text", "content": "| Operation |",
                              "text_item_id": "preceding-reply"},
                             {"type": "question", "question_id": "local-card"}]

            def publish(self, event):
              process_event(event, self.blocks)

          bus = Bus()
          phases = await _recover_unfinished_codex_messages(
            codex, _sdk_imports(), thread.id, notification.payload.turn,
            {"preceding-reply"}, bus,
          )
          assert phases == {"preceding-reply": "commentary"}
          assert [b["type"] for b in bus.blocks] == ["text", "question"]
          assert bus.blocks[0]["content"] == preceding_reply
          return notification.payload.turn

  try:
    terminal = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
  finally:
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)
  assert card_ends == [{"question_id": "local-card"}], {
    "notes": state.get("notes"), "requests": len(model_requests),
    "terminal": str(terminal),
    "hooks": state.get("hooks"),
  }
  assert len(model_requests) == 1
  assert terminal.status.value == "interrupted"
  assert not untrusted_marker.exists()
  assert preceding_reply in state["messages"]
