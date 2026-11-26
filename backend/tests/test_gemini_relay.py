"""App providers declared `gemini` run on the Codex engine through a translating relay."""

import json

import httpx
import pytest

from app import providers
from app.gemini_request import qualified_tool_names, to_gemini_request
from app.gemini_stream import ResponsesStream
from app.manifest_contract import validate_manifest_contract
from app.routes import model_relay

REAL_KEY = "test-only-gemini-key"
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"

TOOLS = [
  {"type": "function", "name": "exec_command", "description": "Run a command",
   "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
  {"type": "namespace", "name": "multi_agent_v1", "description": "Sub-agents", "tools": [
    {"type": "function", "name": "close_agent", "parameters": {"type": "object", "properties": {}}},
  ]},
  {"type": "custom", "name": "apply_patch"},
]


def _declaration(**changes):
  return {
    "name": "Gemini", "base_url": BASE_URL, "secret_name": "api_key", "protocol": "gemini",
    "default_model": "gemini-test", "models": [{"id": "gemini-test", "label": "Gemini Test"}],
    **changes,
  }


def _events(chunks: list[bytes]) -> list[dict]:
  return [json.loads(c.decode().split("data: ", 1)[1]) for c in chunks]


def _answer(stream: ResponsesStream, *chunks: dict) -> list[dict]:
  events = stream.start()
  for chunk in chunks:
    events += stream.feed(chunk)
  return _events(events + stream.finish())


def test_gemini_is_a_declared_protocol():
  validate_manifest_contract({
    "id": "gemini", "name": "Gemini", "version": "0.1.0", "description": "Gemini models",
    "entry": "index.jsx", "model_provider": _declaration(),
  })


def test_gemini_provider_runs_on_the_codex_engine():
  adapter = providers.AppModelProvider(4242, _declaration())
  assert adapter.runtime_kind == "codex_sdk"


def test_request_moves_leading_developer_text_to_system_instruction():
  body = to_gemini_request({"input": [
    {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]},
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "later"}]},
  ], "reasoning": {"effort": "xhigh"}})
  assert body["systemInstruction"] == {"parts": [{"text": "rules"}]}
  assert body["contents"] == [{"role": "user", "parts": [{"text": "hi"}, {"text": "later"}]}]
  assert body["generationConfig"]["thinkingConfig"] == {"includeThoughts": True, "thinkingLevel": "xhigh"}


def test_function_and_namespace_tools_become_declarations():
  body = to_gemini_request({"input": [], "tools": TOOLS})
  names = [d["name"] for d in body["tools"][0]["functionDeclarations"]]
  assert names == ["exec_command", "multi_agent_v1.close_agent"]
  assert body["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"]["properties"] == {
    "cmd": {"type": "string"},
  }


def test_text_streams_as_one_assistant_message_with_usage():
  stream = ResponsesStream("gemini-test", {})
  events = _answer(
    stream,
    {"candidates": [{"content": {"parts": [{"text": "plan", "thought": True}]}}]},
    {"candidates": [{"content": {"parts": [{"text": "Hel"}]}}]},
    {"candidates": [{"content": {"parts": [{"text": "lo"}]}, "finishReason": "STOP"}],
     "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2, "thoughtsTokenCount": 3,
                       "totalTokenCount": 12}},
  )
  assert [e["delta"] for e in events if e["type"] == "response.output_text.delta"] == ["Hel", "lo"]
  done = events[-1]["response"]
  assert done["status"] == "completed"
  assert done["output"][0]["content"][0]["text"] == "Hello"
  assert done["output"][1]["summary"] == [{"type": "summary_text", "text": "plan"}]
  assert done["usage"]["input_tokens"] == 7 and done["usage"]["output_tokens"] == 5


def test_signed_namespaced_call_round_trips_through_replay():
  """The engine replays output items as input; the call returns to Gemini
  under its Gemini name with the signature it was issued with."""
  stream = ResponsesStream("gemini-test", qualified_tool_names(TOOLS))
  events = _answer(stream, {"candidates": [{"content": {"parts": [{
    "functionCall": {"name": "multi_agent_v1.close_agent", "args": {"target": "a1"}},
    "thoughtSignature": "sig-1",
  }]}}]})
  call, reasoning = events[-1]["response"]["output"]
  assert (call["namespace"], call["name"], json.loads(call["arguments"])) == (
    "multi_agent_v1", "close_agent", {"target": "a1"},
  )
  replay = [call, reasoning, {"type": "function_call_output", "call_id": call["call_id"], "output": "ok"}]
  contents = to_gemini_request({"input": replay})["contents"]
  assert contents == [
    {"role": "model", "parts": [{"functionCall": {
      "name": "multi_agent_v1.close_agent", "args": {"target": "a1"}}, "thoughtSignature": "sig-1"}]},
    {"role": "user", "parts": [{"functionResponse": {
      "name": "multi_agent_v1.close_agent", "response": {"output": "ok"}}}]},
  ]


def test_blocked_prompt_fails_the_response():
  events = _answer(ResponsesStream("gemini-test", {}), {"promptFeedback": {"blockReason": "SAFETY"}})
  assert events[-1]["type"] == "response.failed"
  assert events[-1]["response"]["error"]["code"] == "content_filter"


@pytest.mark.parametrize("endpoint, allowed", [
  ("/models/gemini-test:streamGenerateContent", True),
  ("/models/../../v1/files:streamGenerateContent", False),
  ("/responses", False),
])
def test_the_key_is_checked_for_the_concrete_model_path(monkeypatch, endpoint, allowed):
  from app import key_vault
  keys = [{"name": "gemini", "multiline": False}]
  monkeypatch.setattr(key_vault, "resolve_key_in", lambda *_: "gemini")
  monkeypatch.setattr(key_vault, "_grant_of", lambda *_: {"sites": [
    {"host": "generativelanguage.googleapis.com", "methods": ["POST"], "paths": ["/v1beta/models/*"]},
  ]})
  key = key_vault.model_provider_key_in(keys, 1, "a" * 32, "api_key", BASE_URL, "gemini", endpoint)
  assert (key == "gemini") is allowed


def _relay_client(client, monkeypatch, upstream):
  adapter = providers.AppModelProvider(4242, _declaration())
  monkeypatch.setitem(providers.PROVIDERS, "app-4242", adapter)
  monkeypatch.setattr(providers, "_app_providers_fresh", lambda: True)
  monkeypatch.setattr(model_relay, "_is_direct_loopback", lambda _request: True)
  checked = []

  def read_relay_key(_data_dir, *, endpoint=None, **_):
    checked.append(endpoint)
    return providers.RelayKeyRead(REAL_KEY, None)
  monkeypatch.setattr(adapter, "read_relay_key", read_relay_key)
  monkeypatch.setattr(model_relay, "_http_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))

  def post(body):
    return client.post("/api/model-relay/app-4242/v1/responses", json=body,
                       headers={"authorization": f"Bearer {adapter.relay_token()}"})
  return post, checked


def test_relay_translates_to_gemini_with_the_key_for_that_model(client, monkeypatch):
  seen = []

  def upstream(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    chunk = {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]}
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=f"data: {json.dumps(chunk)}\n\n".encode())
  post, checked = _relay_client(client, monkeypatch, upstream)
  response = post({"model": "gemini-test", "input": [{"type": "message", "role": "user", "content": "yo"}]})

  assert response.status_code == 200
  assert checked == ["/models/gemini-test:streamGenerateContent"]
  assert str(seen[0].url) == f"{BASE_URL}/models/gemini-test:streamGenerateContent?alt=sse"
  assert seen[0].headers["x-goog-api-key"] == REAL_KEY and "authorization" not in seen[0].headers
  assert json.loads(seen[0].content)["contents"] == [{"role": "user", "parts": [{"text": "yo"}]}]
  events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
  assert events[-1]["type"] == "response.completed"
  assert events[-1]["response"]["output"][0]["content"][0]["text"] == "hi"


def test_relay_refuses_undeclared_models_and_passes_google_errors(client, monkeypatch):
  def upstream(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, headers={"retry-after": "7"},
                          json={"error": {"code": 429, "message": "Quota exceeded"}})
  post, checked = _relay_client(client, monkeypatch, upstream)

  assert post({"model": "other-model", "input": []}).status_code == 404
  assert checked == []
  limited = post({"model": "gemini-test", "input": []})
  assert limited.status_code == 429 and limited.headers["retry-after"] == "7"
  assert limited.json()["error"]["message"] == "Quota exceeded"


def test_built_in_gemini_uses_the_owner_key_from_settings(monkeypatch, tmp_path):
  from app import key_vault
  gemini = providers.PROVIDERS["gemini"]
  monkeypatch.setattr(providers, "provider_enabled", lambda *_: True)
  saved = {}
  monkeypatch.setattr(key_vault, "platform_key_sync",
                      lambda name, *, reveal, caller: saved.get(name) and (saved[name] if reveal else name))
  assert gemini.runtime_kind == "codex_sdk" and gemini.protocol == "gemini"
  assert "Settings" in gemini.check_auth(str(tmp_path))
  saved["gemini"] = REAL_KEY
  assert gemini.check_auth(str(tmp_path)) is None
  assert gemini.read_relay_key(str(tmp_path)).key == REAL_KEY
  overrides = gemini.codex_config_overrides()
  assert 'model_provider="gemini"' in overrides
  assert any(o.endswith('/api/model-relay/gemini/v1"') for o in overrides)


@pytest.mark.asyncio
async def test_disconnecting_gemini_removes_its_saved_key(monkeypatch, tmp_path):
  from app import key_vault
  calls = []

  async def vault_call(method, path, payload=None, *, caller=None):
    calls.append((method, path, caller))
    return {}
  monkeypatch.setattr(key_vault, "vault_call", vault_call)
  await providers.disconnect_provider(str(tmp_path), "gemini")
  assert calls == [("DELETE", "/keys/gemini", "owner"), ("DELETE", "/keys/gemini-agent-platform", "owner")]


def test_each_gemini_model_offers_its_documented_thinking_levels():
  """Levels and defaults follow Google's per-model thinking documentation;
  an effort a model lacks is not sent, so Gemini applies its own default."""
  gemini = providers.PROVIDERS["gemini"]
  levels = {row["id"]: (row["effort_levels"], row["default_effort"]) for row in gemini.declaration["models"]}
  assert levels["gemini-4-neon"] == (["low", "medium", "high", "xhigh"], "high")
  assert levels["gemini-4-hydrogen"] == (["low", "medium", "high", "xhigh"], "high")
  assert levels["gemini-3.1-flash-lite"] == (["minimal", "low", "medium", "high"], "minimal")
  assert gemini.resolve_selection("gemini-4-neon", "xhigh") == ("gemini-4-neon", "xhigh")
  assert gemini.resolve_selection("gemini-4-carbon", "xhigh") == ("gemini-4-carbon", None)
  assert to_gemini_request({"input": [], "reasoning": {"effort": "max"}})[
    "generationConfig"]["thinkingConfig"] == {"includeThoughts": True}


def test_an_agent_platform_key_sends_gemini_to_google_cloud(client, monkeypatch):
  """The connection in use picks the key and the API root together."""
  from app import key_vault
  gemini = providers.PROVIDERS["gemini"]
  monkeypatch.setitem(providers.PROVIDERS, "gemini", gemini)
  monkeypatch.setattr(providers, "_app_providers_fresh", lambda: True)
  monkeypatch.setattr(providers, "provider_enabled", lambda *_: True)
  monkeypatch.setattr(model_relay, "_is_direct_loopback", lambda _request: True)
  saved = {"gemini-agent-platform": REAL_KEY}
  monkeypatch.setattr(key_vault, "platform_key_sync",
                      lambda name, *, reveal, caller: saved.get(name) and (saved[name] if reveal else name))
  seen = []

  def upstream(request: httpx.Request) -> httpx.Response:
    seen.append(request)
    chunk = {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]}
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=f"data: {json.dumps(chunk)}\n\n".encode())
  monkeypatch.setattr(model_relay, "_http_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))

  assert gemini.connection_kind("/unused") == "agent_platform"
  response = client.post("/api/model-relay/gemini/v1/responses",
                         json={"model": "gemini-3.5-flash", "input": []},
                         headers={"authorization": f"Bearer {gemini.relay_token()}"})
  assert response.status_code == 200
  assert str(seen[0].url) == (
    "https://aiplatform.googleapis.com/v1/publishers/google/models/gemini-3.5-flash:streamGenerateContent?alt=sse"
  )
  assert seen[0].headers["x-goog-api-key"] == REAL_KEY
