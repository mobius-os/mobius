"""App model providers reach their company only through the loopback model relay."""

import json
from pathlib import Path

import httpx
import pytest
from starlette.requests import Request

from app import models, providers
from app.app_capabilities import contract_from_manifest
from app.config import get_settings
from app.manifest_contract import ManifestContractError, validate_manifest_contract
from app.routes import model_relay
from app.routes.secrets import _write_secret

REAL_KEY = "test-only-provider-key"


def _manifest(**provider_changes):
  return {
    "id": "messages-connect", "name": "Messages Connect", "version": "1.0.0",
    "description": "Connect Messages models", "entry": "index.jsx",
    "model_provider": {
      "name": "Example", "base_url": "https://models.example.com",
      "secret_name": "api_key", "protocol": "anthropic_messages",
      "default_model": "example/flash",
      "models": [{"id": "example/flash", "label": "Flash"}, {"id": "example/pro", "label": "Pro"}],
      **provider_changes,
    },
  }


def test_messages_protocol_is_a_declared_contract_value():
  validate_manifest_contract(_manifest())
  with pytest.raises(ManifestContractError):
    validate_manifest_contract(_manifest(protocol="chat_completions"))
  broker = _manifest(transport="identity_broker", base_url="http://127.0.0.1:8765/v1")
  broker["model_provider"].pop("secret_name")
  broker.update(id="identity", permissions={"identity_manage": True})
  with pytest.raises(ManifestContractError):
    validate_manifest_contract(broker)


def test_messages_provider_runs_on_claude_engine_without_the_real_key(tmp_path):
  data_dir = get_settings().data_dir
  adapter = providers.AppModelProvider(4242, _manifest()["model_provider"])
  _write_secret(Path(data_dir) / "app-secrets" / "4242" / "api_key", REAL_KEY)

  env = adapter.build_env(
    {"ANTHROPIC_API_KEY": "owner-key", "CLAUDE_CODE_OAUTH_TOKEN": "owner-oauth"}, data_dir, "chat-1",
  )

  assert providers.provider_runtime_kind(adapter) == "claude_sdk"
  assert env["ANTHROPIC_BASE_URL"].endswith("/api/model-relay/app-4242")
  assert env["ANTHROPIC_API_KEY"] == providers.model_relay_token("app-4242")
  assert REAL_KEY not in "\n".join(env.values())
  assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
  assert env["CLAUDE_CONFIG_DIR"] == str(
    Path(data_dir) / "apps" / "4242" / "model-runtime" / "claude"
  )
  assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "example/flash"
  assert providers.model_relay_token("app-4242") != providers.model_relay_token("app-4243")


def test_normalize_folds_system_turns_into_leading_tool_results():
  tool_use = {"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}
  result = {"type": "tool_result", "tool_use_id": "t1", "content": "ok"}
  payload = {"system": [{"type": "text", "text": "top"}], "messages": [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": [tool_use]},
    {"role": "system", "content": [{"type": "text", "text": "env"}]},
    {"role": "user", "content": [result]},
  ]}

  folded = model_relay.normalize_messages_request(payload)

  assert folded["system"] == payload["system"]
  assert [m["role"] for m in folded["messages"]] == ["user", "assistant", "user"]
  assert folded["messages"][2]["content"] == [result, {"type": "text", "text": "env"}]
  plain = {"messages": [{"role": "user", "content": "hi"}]}
  assert model_relay.normalize_messages_request(plain) is plain


def test_relay_is_only_for_direct_loopback_connections():
  def request(host, headers=()):
    return Request({"type": "http", "headers": list(headers), "client": (host, 1)})

  assert model_relay._is_direct_loopback(request("127.0.0.1"))
  assert not model_relay._is_direct_loopback(request("10.0.0.7"))
  assert not model_relay._is_direct_loopback(
    request("127.0.0.1", [(b"x-forwarded-for", b"203.0.113.9")]),
  )


def test_relay_checks_token_then_forwards_normalized_body_with_real_key(
  client, db, monkeypatch,
):
  app = models.App(
    name="Messages Connect", slug="messages-connect", description="",
    source_dir="/tmp/messages-connect", capability_contract=contract_from_manifest(_manifest()),
  )
  db.add(app)
  db.commit()
  provider_id = f"app-{app.id}"
  data_dir = get_settings().data_dir
  seen = {}

  def upstream(req: httpx.Request) -> httpx.Response:
    seen.update(url=str(req.url), key=req.headers.get("x-api-key"), body=json.loads(req.content))
    return httpx.Response(200, headers={"content-type": "text/event-stream"},
                          content=b"event: message_stop\ndata: {}\n\n")

  monkeypatch.setattr(model_relay, "_is_direct_loopback", lambda _request: True)
  monkeypatch.setattr(model_relay, "_http_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
  path = f"/api/model-relay/{provider_id}/v1/messages?beta=true"
  body = {"model": "example/flash", "messages": [
    {"role": "user", "content": "hi"}, {"role": "system", "content": "env"},
  ]}
  try:
    providers.sync_app_model_providers(data_dir, force=True)
    token = {"x-api-key": providers.model_relay_token(provider_id)}

    assert client.post(path, json=body, headers={"x-api-key": "wrong"}).status_code == 401
    missing_key = client.post(path, json=body, headers=token)
    assert missing_key.status_code == 403
    assert seen == {}

    _write_secret(Path(data_dir) / "app-secrets" / str(app.id) / "api_key", REAL_KEY)
    relayed = client.post(path, json=body, headers=token)

    assert relayed.status_code == 200
    assert relayed.content == b"event: message_stop\ndata: {}\n\n"
    assert seen["url"] == "https://models.example.com/v1/messages?beta=true"
    assert seen["key"] == REAL_KEY
    assert seen["body"]["messages"] == [{"role": "user", "content": [
      {"type": "text", "text": "hi"}, {"type": "text", "text": "env"},
    ]}]
  finally:
    providers.invalidate_model_cache()


@pytest.mark.asyncio
async def test_claude_runner_accepts_a_messages_providers_own_model(monkeypatch):
  from app import claude_sdk_runner
  from tests.test_claude_sdk_runner import _ChatBus

  declaration = _manifest(default_model="runner/only", models=[{"id": "runner/only", "label": "Only"}])
  monkeypatch.setitem(
    providers.PROVIDERS, "app-4242",
    providers.AppModelProvider(4242, declaration["model_provider"]),
  )
  reached = {}

  class _Client:
    def __init__(self, options):
      reached["model"] = options.model

    async def connect(self):
      raise RuntimeError("stop at the SDK boundary")

    async def disconnect(self):
      return None

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _Client)
  turn = dict(
    user_message="hi", session_id=None, base_env={}, cwd="/tmp",
    chat_id="relay-model", skill_text="system", bc=_ChatBus(),
    agent_settings={"model": "runner/only"},
  )

  with pytest.raises(ValueError, match="does not belong to provider 'claude'"):
    await claude_sdk_runner.run_claude_sdk_turn(**turn)
  await claude_sdk_runner.run_claude_sdk_turn(**turn, provider_id="app-4242")
  assert reached["model"] == "runner/only"



def test_responses_relay_strips_null_reasoning_content_only():
  reasoning = {"type": "reasoning", "id": "rs_1", "summary": [], "content": None, "encrypted_content": ""}
  call = {"type": "function_call", "call_id": "c1", "name": "exec", "arguments": "{}", "content": None}
  payload = {"input": [{"type": "message", "role": "user", "content": []}, reasoning, call]}

  cleaned = model_relay.normalize_responses_request(payload)

  assert cleaned["input"][1] == {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": ""}
  assert cleaned["input"][0] == payload["input"][0]
  assert cleaned["input"][2] == call
  untouched = {"input": [{"type": "reasoning", "summary": [], "content": [{"type": "reasoning_text"}]}]}
  assert model_relay.normalize_responses_request(untouched) is untouched


def test_responses_provider_gets_relay_token_not_the_real_key():
  data_dir = get_settings().data_dir
  declaration = _manifest(protocol="responses", base_url="https://models.example.com/v1")["model_provider"]
  adapter = providers.AppModelProvider(4343, declaration)
  _write_secret(Path(data_dir) / "app-secrets" / "4343" / "api_key", REAL_KEY)

  env = adapter.build_env({}, data_dir)
  overrides = "\n".join(adapter.codex_config_overrides())

  assert providers.provider_runtime_kind(adapter) == "codex_sdk"
  assert env["MOBIUS_APP_MODEL_KEY_4343"] == providers.model_relay_token("app-4343")
  assert REAL_KEY not in "\n".join(env.values())
  assert '/api/model-relay/app-4343/v1"' in overrides
  assert "models.example.com" not in overrides


def test_relay_routes_each_protocol_only_to_its_own_providers(client, db, monkeypatch):
  declaration = _manifest(protocol="responses", base_url="https://models.example.com/v1")
  declaration["model_provider"]["models"] = [{"id": "example/responses-only", "label": "R"}]
  declaration["model_provider"]["default_model"] = "example/responses-only"
  app = models.App(
    name="Responses Connect", slug="responses-connect", description="",
    source_dir="/tmp/responses-connect", capability_contract=contract_from_manifest(declaration),
  )
  db.add(app)
  db.commit()
  provider_id = f"app-{app.id}"
  data_dir = get_settings().data_dir
  _write_secret(Path(data_dir) / "app-secrets" / str(app.id) / "api_key", REAL_KEY)
  seen = {}

  def upstream(req: httpx.Request) -> httpx.Response:
    seen.update(url=str(req.url), auth=req.headers.get("authorization"), body=json.loads(req.content))
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=b"data: {}\n\n")

  monkeypatch.setattr(model_relay, "_is_direct_loopback", lambda _request: True)
  monkeypatch.setattr(model_relay, "_http_client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
  bearer = {"authorization": f"Bearer {providers.model_relay_token(provider_id)}"}
  body = {"model": "example/responses-only", "input": [{"type": "reasoning", "summary": [], "content": None}]}
  try:
    providers.sync_app_model_providers(data_dir, force=True)

    wrong_protocol = client.post(f"/api/model-relay/{provider_id}/v1/messages", json=body, headers=bearer)
    assert wrong_protocol.status_code == 404
    relayed = client.post(f"/api/model-relay/{provider_id}/v1/responses", json=body, headers=bearer)

    assert relayed.status_code == 200
    assert seen["url"] == "https://models.example.com/v1/responses"
    assert seen["auth"] == f"Bearer {REAL_KEY}"
    assert seen["body"]["input"] == [{"type": "reasoning", "summary": []}]
  finally:
    providers.invalidate_model_cache()


def test_app_provider_runs_record_only_measured_usage():
  adapter = providers.AppModelProvider(4444, _manifest()["model_provider"])
  zero = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
  real = {"input_tokens": 1200, "output_tokens": 40, "total_tokens": 1240}

  assert adapter.measured_run_accounting(0.0, zero) == (None, None)
  assert adapter.measured_run_accounting(0.31, real) == (None, real)
  assert providers.PROVIDERS["claude"].measured_run_accounting(0.0, zero) == (0.0, zero)
