"""PostToolUse recognizes receipts but leaves card/run authority to the server."""

import importlib.util
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from app import codex_sdk_runner
from app.codex_sdk_runner import _codex_owner_card_hook_override


def _hook():
  path = Path(__file__).resolve().parents[1] / "scripts" / "codex_owner_card_hook.py"
  spec = importlib.util.spec_from_file_location("codex_owner_card_hook", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


@pytest.mark.parametrize("wrapper", ["mcp", "shell"])
def test_hook_waits_for_server_validation_before_returning(monkeypatch, wrapper):
  hook = _hook()
  monkeypatch.setenv("CHAT_ID", "this-chat")
  receipt = {"state": "waiting_for_owner", "question_id": "this-card", "next_action": "End"}
  response = {"content": [{"type": "text", "text": json.dumps(receipt)}]}
  if wrapper == "shell":
    response = {"stdout": json.dumps(receipt), "interrupted": False}
  calls = []

  def end(*args):
    calls.append(args)
    return {"ended": True}

  monkeypatch.setattr(hook, "_agent_api_call", end)
  hook.finish_card_tool({"hook_event_name": "PostToolUse", "tool_response": response})
  assert calls == [("POST", "/api/chats/this-chat/owner-card-end", {"question_id": "this-card"})]


@pytest.mark.parametrize("payload", [
  {"hook_event_name": "PreToolUse"},
  {"hook_event_name": "PostToolUse", "tool_response": {"state": {"draft": {}}}},
  {"hook_event_name": "PostToolUse", "agent_id": "native-child", "tool_response": {
    "state": "waiting_for_owner", "question_id": "child-card", "next_action": "End",
  }},
])
def test_hook_does_not_end_turns_from_unrelated_results(monkeypatch, payload):
  hook = _hook()
  calls = []
  monkeypatch.setattr(hook, "_agent_api_call", lambda *args: calls.append(args))
  hook.finish_card_tool(payload)
  assert calls == []


def test_hook_config_is_synchronous_and_does_not_rewrite_tool_feedback():
  import tomllib
  config = tomllib.loads(_codex_owner_card_hook_override())
  handler = config["hooks"]["PostToolUse"][0]["hooks"][0]
  assert handler["type"] == "command"
  assert "codex_owner_card_hook.py" in handler["command"]
  assert handler.get("async", False) is False


def test_thread_trust_names_only_our_exact_hook_without_mutating_connector_config(monkeypatch):
  import asyncio
  import tomllib
  expected = tomllib.loads(_codex_owner_card_hook_override())["hooks"]["PostToolUse"][0]
  from openai_codex.generated.v2_all import HookMetadata

  def hook_metadata(key, command):
    return HookMetadata.model_validate({
      "handlerType": "command", "command": command,
      "currentHash": "our-definition-hash", "displayOrder": 0,
      "enabled": True, "eventName": "postToolUse", "isManaged": False,
      "key": key, "matcher": expected["matcher"],
      "source": "sessionFlags", "sourcePath": "/tmp/config.toml",
      "timeoutSec": 15, "trustStatus": "untrusted",
    })

  ours = hook_metadata("our-key", expected["hooks"][0]["command"])
  other = hook_metadata("other-key", "unreviewed-command")

  class Client:
    async def request(self, method, params, response_model):
      assert method == "hooks/list"
      assert params == {"cwds": ["/work"]}
      return SimpleNamespace(data=[SimpleNamespace(hooks=[ours, other])])

  monkeypatch.setattr(codex_sdk_runner, "control_client", lambda _: Client())
  original = {"mcp_servers": {"calendar": {"url": "https://example.invalid"}}}
  result = asyncio.run(codex_sdk_runner._codex_owner_card_hook_thread_config(
    object(), {"HooksListResponse": object}, "/work", original,
  ))
  assert result["mcp_servers"] == original["mcp_servers"]
  assert "hooks" not in original
  assert result["hooks.state"] == {"our-key": {"trusted_hash": "our-definition-hash"}}
