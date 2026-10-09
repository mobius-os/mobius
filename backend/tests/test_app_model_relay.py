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
  assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
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
  with pytest.raises(ValueError, match="does not belong to provider 'app-4242'"):
    await claude_sdk_runner.run_claude_sdk_turn(
      **{**turn, "agent_settings": {"model": "claude-opus-4-8"}},
      provider_id="app-4242",
    )



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


@pytest.mark.asyncio
@pytest.mark.parametrize("launch", ["turn", "compaction"])
async def test_messages_sdk_child_overrides_ambient_auth_and_routing(
  monkeypatch, tmp_path, launch,
):
  import os

  from app import claude_sdk_runner, compaction
  from tests.test_claude_sdk_runner import _ChatBus

  # Replace, rather than read, the process environment: no real credentials
  # are inspected and no subprocess is started by this regression.
  competing = {
    "OPENAI_API_KEY": "synthetic-openai-key",
    "OTHER_API_TOKEN": "synthetic-other-token",
    "OTHER_AUTH_TOKEN": "synthetic-other-auth",
    "CLAUDE_CODE_OAUTH_TOKEN": "synthetic-owner-oauth",
    "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR": "123",
    "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR": "124",
    "ANTHROPIC_AUTH_TOKEN": "synthetic-owner-auth",
    "ANTHROPIC_CUSTOM_HEADERS": "Authorization: synthetic-owner-header",
    "ANTHROPIC_MODEL": "ambient/model",
    "CLAUDE_CODE_SUBAGENT_MODEL": "ambient/helper",
    "ANTHROPIC_SMALL_FAST_MODEL_AWS_REGION": "ambient-region",
    "CLAUDE_CODE_USE_BEDROCK": "1",
    "CLAUDE_CODE_USE_VERTEX": "1",
    "CLAUDE_CODE_USE_FOUNDRY": "1",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS": "1",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD": "1",
    "CLAUDE_CODE_USE_MANTLE": "1",
    "CLAUDE_CODE_USE_GATEWAY": "1",
    "CLAUDE_CODE_GATEWAY_TOKEN": "synthetic-gateway-token",
    "CLAUDE_CODE_GATEWAY_TOKEN_FILE_DESCRIPTOR": "125",
    "CLAUDE_CODE_SESSION_ACCESS_TOKEN": "synthetic-session-token",
    "CLAUDE_CODE_OAUTH_REFRESH_TOKEN": "synthetic-refresh-token",
  }
  monkeypatch.setattr(os, "environ", {
    **competing,
    "ANTHROPIC_API_KEY": "synthetic-owner-key",
    "ANTHROPIC_BASE_URL": "https://ambient.example.com",
    "CLAUDE_AGENT_SDK_SKIP_VERSION_CHECK": "1",
    "AGENT_TOKEN": "synthetic-control-token",
    "MOBIUS_RUN_TOKEN": "synthetic-run",
    "API_BASE_URL": "http://127.0.0.1:9",
  })
  adapter = providers.AppModelProvider(4242, _manifest()["model_provider"])
  monkeypatch.setitem(providers.PROVIDERS, "app-4242", adapter)
  captured = {}

  async def fake_open_process(*args, **kwargs):
    captured.update(kwargs["env"])
    raise RuntimeError("synthetic probe stopped before process creation")

  monkeypatch.setattr(
    "claude_agent_sdk._internal.transport.subprocess_cli.anyio.open_process",
    fake_open_process,
  )
  if launch == "turn":
    await claude_sdk_runner.run_claude_sdk_turn(
      user_message="hi", session_id=None,
      base_env=adapter.build_env({}, str(tmp_path)), cwd=str(tmp_path),
      chat_id="relay-env", skill_text="system", bc=_ChatBus(),
      agent_settings={"model": "example/flash"}, provider_id="app-4242",
    )
  else:
    with pytest.raises(Exception, match="synthetic probe stopped"):
      await compaction._run_claude_summarize_turn(
        "summarize", data_dir=str(tmp_path), provider_id="app-4242",
        model="example/flash", effort=None,
      )

  assert captured
  assert all(captured.get(name) == "" for name in competing)
  assert captured["ANTHROPIC_API_KEY"] == providers.model_relay_token("app-4242")
  assert captured["ANTHROPIC_BASE_URL"] == adapter._relay_base_url()
  assert captured["AGENT_TOKEN"] == "synthetic-control-token"
  assert captured["MOBIUS_RUN_TOKEN"] == "synthetic-run"
  assert captured["API_BASE_URL"] == "http://127.0.0.1:9"
  assert captured["CLAUDE_CONFIG_DIR"].startswith(str(tmp_path))
  assert captured["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "example/flash"


@pytest.mark.asyncio
async def test_shared_relay_pool_rejects_cookies_sequentially_and_concurrently(monkeypatch):
  import asyncio

  seen = []

  async def upstream(request):
    seen.append(request.headers.get("cookie"))
    await asyncio.sleep(0)
    return httpx.Response(200, headers={
      "set-cookie": "session=synthetic-session; Domain=example.com; Path=/",
    }, content=b"{}")

  real_client = httpx.AsyncClient
  monkeypatch.setattr(model_relay, "_http_client", None)
  monkeypatch.setattr(model_relay.httpx, "AsyncClient", lambda **kwargs: real_client(
    transport=httpx.MockTransport(upstream), **kwargs,
  ))
  pool = model_relay._client()
  try:
    await pool.post("https://provider-a.example.com/v1/messages")
    await pool.post("https://provider-b.example.com/responses")
    await asyncio.gather(*(
      pool.post(f"https://provider-a.example.com/connection-{number}")
      for number in range(8)
    ))
    assert seen == [None] * 10
    assert not list(pool.cookies.jar)
    assert model_relay._client() is pool
  finally:
    await model_relay.close_model_relay_client()
  assert pool.is_closed
  assert model_relay._http_client is None
  await model_relay.close_model_relay_client()
  replacement = model_relay._client()
  assert replacement is not pool
  assert not replacement.is_closed
  await model_relay.close_model_relay_client()


@pytest.fixture
def relay_connection(monkeypatch, tmp_path):
  """Synthetic accepted connection without a real upstream or credential."""
  adapter = providers.AppModelProvider(4242, _manifest()["model_provider"])
  monkeypatch.setitem(providers.PROVIDERS, "app-4242", adapter)
  monkeypatch.setattr(providers, "sync_app_model_providers", lambda *_: None)
  monkeypatch.setattr(adapter, "check_auth", lambda *_: None)
  monkeypatch.setattr("app.app_secret_crypto.decrypt_app_secret", lambda *_: REAL_KEY)

  async def receive():
    return {"type": "http.request", "body": b'{}', "more_body": False}

  request = Request({
    "type": "http", "method": "POST", "scheme": "http",
    "path": "/api/model-relay/app-4242/v1/messages", "query_string": b"",
    "headers": [(b"x-api-key", providers.model_relay_token("app-4242").encode())],
    "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000),
  }, receive)
  return adapter, request


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["anthropic_messages", "responses"])
async def test_relay_preserves_retry_metadata_but_not_upstream_transport_headers(
  monkeypatch, relay_connection, protocol,
):
  import gzip

  adapter, request = relay_connection
  adapter.protocol = protocol
  metadata = {
    "retry-after": "60", "retry-after-ms": "60000", "x-should-retry": "true",
    "request-id": "synthetic-request", "x-request-id": "synthetic-request",
    "anthropic-ratelimit-requests-remaining": "0", "x-ratelimit-reset-tokens": "60s",
  }
  body = b'{"error":"try later"}'
  async with httpx.AsyncClient(transport=httpx.MockTransport(
    lambda _: httpx.Response(429, headers={
      **metadata, "content-type": "application/json",
      "content-encoding": "gzip", "set-cookie": "session=synthetic",
      "connection": "keep-alive", "x-unapproved": "private",
    }, content=gzip.compress(body)),
  )) as pool:
    monkeypatch.setattr(model_relay, "_http_client", pool)
    response = await model_relay._relay("app-4242", request, protocol)
    assert response.status_code == 429
    assert dict(response.headers) == {"content-type": "application/json", **metadata}
    assert b"".join([chunk async for chunk in response.body_iterator]) == body


@pytest.mark.asyncio
@pytest.mark.parametrize("before_body", [False, True])
async def test_relay_disconnect_closes_upstream_even_inside_cancelled_scope(
  monkeypatch, relay_connection, before_body,
):
  import anyio

  _, request = relay_connection
  first_chunk = anyio.Event()
  closed = []

  class Stream(httpx.AsyncByteStream):
    async def __aiter__(self):
      yield b"first"
      await anyio.sleep_forever()

    async def aclose(self):
      # Socket cleanup can itself suspend. A cancelled Starlette task must
      # still finish it, not merely enter the response iterator's finally.
      await anyio.sleep(0)
      closed.append(True)

  async with httpx.AsyncClient(transport=httpx.MockTransport(
    lambda _: httpx.Response(200, stream=Stream()),
  )) as pool:
    monkeypatch.setattr(model_relay, "_http_client", pool)
    response = await model_relay._relay("app-4242", request, "anthropic_messages")

    async def receive():
      await first_chunk.wait()
      return {"type": "http.disconnect"}

    async def send(message):
      if before_body and message["type"] == "http.response.start":
        first_chunk.set()
        await anyio.sleep_forever()
      if message["type"] == "http.response.body":
        first_chunk.set()

    await response(request.scope, receive, send)
  assert closed == [True]


@pytest.mark.asyncio
async def test_model_relay_client_is_closed_by_application_lifespan(monkeypatch):
  from types import SimpleNamespace
  from unittest.mock import AsyncMock

  from app import main, startup

  # Exercise the real lifespan's shutdown with inert startup/supervisors.
  monkeypatch.setattr("app.helper_hosts.end_orphaned_hosts", lambda: None)
  monkeypatch.setattr(startup, "run_startup_plan", AsyncMock(return_value=
    startup.DatabaseBootResult(failure_reason="synthetic degraded boot"),
  ))
  monkeypatch.setattr(main, "_set_database_boot_state", lambda _: None)
  monkeypatch.setattr(main, "record_memory_checkpoint", lambda _: None)
  monkeypatch.setattr("app.runtime_supervisors.RuntimeSupervisors", lambda **_: SimpleNamespace(
    start_process_services=AsyncMock(), stop=AsyncMock(),
  ))
  monkeypatch.setattr("app.public_app_transport.close_public_fetch_clients", AsyncMock())
  monkeypatch.setattr("app.saved_secure_inputs.shutdown", AsyncMock())
  monkeypatch.setattr("app.helper_hosts.MANAGER.close_all", AsyncMock())
  monkeypatch.setattr(main.activity, "flush_request_errors", lambda: None)
  pool = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
  monkeypatch.setattr(model_relay, "_http_client", pool)
  async with main.lifespan(SimpleNamespace(state=SimpleNamespace())):
    assert not pool.is_closed
  assert pool.is_closed
  assert model_relay._http_client is None


@pytest.mark.parametrize("protocol", ["anthropic_messages", "responses"])
def test_app_run_durable_and_terminal_cost_are_both_unknown(
  client, auth, db, monkeypatch, protocol,
):
  import asyncio

  from app import chat as chat_mod, schemas
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_writer import StartTurn, alloc_run_token, get_writer

  app = models.App(
    name="Cost Connection", slug="cost-connect", description="",
    source_dir="/tmp/cost-connect",
    capability_contract=contract_from_manifest(_manifest(protocol=protocol)),
  )
  db.add(app)
  db.commit()
  provider_id = f"app-{app.id}"
  providers.sync_app_model_providers(get_settings().data_dir, force=True)
  adapter = providers.PROVIDERS[provider_id]
  monkeypatch.setattr(adapter, "check_auth", lambda _: None)
  chat_id = client.post("/api/chats", json={"title": "cost"}, headers=auth).json()["id"]
  row = db.get(models.Chat, chat_id)
  row.provider = provider_id
  row.agent_settings_json = {"model": "example/flash"}
  db.commit()
  usage = {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15}
  reached = []

  async def runner(**kwargs):
    reached.append(kwargs["provider_id"])
    return {"session_id": None, "cost_usd": 12.34, "usage_metrics": usage, "error": None}

  monkeypatch.setattr(
    "app.claude_sdk_runner.run_claude_sdk_turn" if protocol == "anthropic_messages"
    else "app.codex_sdk_runner.run_codex_sdk_turn", runner,
  )
  broadcast = create_broadcast(chat_id)
  events = []
  monkeypatch.setattr(broadcast, "publish", lambda event: events.append(event))
  run_token = alloc_run_token()
  get_writer().submit(StartTurn(
    chat_id=chat_id, run_token=run_token,
    user_msg={"role": "user", "content": "hi", "ts": 1, "cid": "synthetic-cost-message"},
    title_source="hi", default_provider=provider_id,
  )).result(timeout=5)
  try:
    asyncio.run(chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="hi")],
      chat_id=chat_id, session_id=None, provider_id=provider_id,
      run_gen=chat_mod.current_run_generation(chat_id), run_token=run_token,
    ))
    assert reached == [provider_id]
    assert [event["cost_usd"] for event in events if event["type"] == "done"] == [None]
    db.expire_all()
    run = db.get(models.ChatRun, run_token)
    assert run.cost_usd is None
    assert run.input_tokens == 12
    assert run.output_tokens == 3
  finally:
    remove_broadcast(chat_id)
    providers.invalidate_model_cache()


@pytest.mark.asyncio
async def test_messages_turn_reads_its_own_effort_catalog_without_claude_discovery(
  monkeypatch, tmp_path,
):
  from app import claude_sdk_runner
  from tests.test_claude_sdk_runner import _ChatBus

  adapter = providers.AppModelProvider(4242, _manifest()["model_provider"])
  monkeypatch.setitem(providers.PROVIDERS, "app-4242", adapter)
  monkeypatch.setattr(providers, "_model_registry_cache", {})

  async def unrelated_models(_data_dir):
    pytest.fail("an app Messages turn must not discover the owner's Claude models")

  monkeypatch.setattr(providers.PROVIDERS["claude"], "fetch_models", unrelated_models)
  captured = []

  class Client:
    def __init__(self, options):
      captured.append(options.effort)

    async def connect(self):
      raise RuntimeError("stop at the SDK boundary")

    async def disconnect(self):
      pass

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", Client)
  await claude_sdk_runner.run_claude_sdk_turn(
    user_message="hi", session_id=None, base_env={}, cwd=str(tmp_path),
    chat_id="relay-effort", skill_text="system", bc=_ChatBus(),
    agent_settings={"model": "example/flash", "effort": "high"},
    provider_id="app-4242",
  )
  assert captured == ["high"]
  assert providers._model_registry_cache == {}


@pytest.mark.parametrize("api_url,env_port,port", [
  ("https://public.example.com/instance", None, 8000),
  ("https://public.example.com", "8123", 8123),
  ("https://public.example.com:9443", None, 9443),
  ("http://localhost:8124", "8123", 8124),
  ("http://127.0.0.1:8125", None, 8125),
  ("http://[::1]:8126", None, 8126),
])
def test_relay_capability_stays_on_loopback_with_a_public_api_origin(
  monkeypatch, tmp_path, api_url, env_port, port,
):
  import os

  monkeypatch.setattr(os, "environ", {} if env_port is None else {"PORT": env_port})
  monkeypatch.setattr(get_settings(), "api_base_url", api_url)
  adapter = providers.AppModelProvider(4242, _manifest()["model_provider"])
  expected = f"http://127.0.0.1:{port}/api/model-relay/app-4242"
  assert adapter.build_env({}, str(tmp_path))["ANTHROPIC_BASE_URL"] == expected
  assert f'model_providers.app_4242.base_url="{expected}/v1"' in adapter.codex_config_overrides()


@pytest.mark.parametrize("api_url,env_port", [
  ("https://public.example.com:bad", "8000"),
  ("https://public.example.com", "65536"),
  ("http://localhost:0", None),
])
def test_relay_rejects_invalid_local_ports(monkeypatch, api_url, env_port):
  import os

  monkeypatch.setattr(os, "environ", {} if env_port is None else {"PORT": env_port})
  monkeypatch.setattr(get_settings(), "api_base_url", api_url)
  adapter = providers.AppModelProvider(4242, _manifest()["model_provider"])
  with pytest.raises(ValueError, match="API port is invalid"):
    adapter._relay_base_url()


def test_responses_sdk_child_scrubs_inherited_credentials_but_preserves_control_env(
  monkeypatch, tmp_path,
):
  import os
  from openai_codex.client import CodexClient, CodexConfig

  # Exercise the real SDK merge, intercepting Popen before any child exists.
  monkeypatch.setattr(os, "environ", {
    "OPENAI_API_KEY": "synthetic-openai-key",
    "OTHER_API_TOKEN": "synthetic-other-token",
    "OTHER_AUTH_TOKEN": "synthetic-other-auth",
    "AGENT_TOKEN": "synthetic-control-token",
    "API_BASE_URL": "http://127.0.0.1:9",
    "MOBIUS_RUN_TOKEN": "synthetic-run",
  })
  adapter = providers.AppModelProvider(4242, _manifest(protocol="responses")["model_provider"])
  captured = {}

  def stop_before_spawn(*args, **kwargs):
    captured.update(kwargs["env"])
    raise RuntimeError("synthetic stop before process creation")

  monkeypatch.setattr("openai_codex.client.subprocess.Popen", stop_before_spawn)
  client = CodexClient(CodexConfig(
    launch_args_override=("synthetic-codex",),
    env=adapter.build_env({"BASE_API_KEY": "synthetic-base-key"}, str(tmp_path)),
  ))
  with pytest.raises(RuntimeError, match="synthetic stop"):
    client.start()
  for name in ("OPENAI_API_KEY", "OTHER_API_TOKEN", "OTHER_AUTH_TOKEN", "BASE_API_KEY"):
    assert captured[name] == ""
  assert captured["MOBIUS_APP_MODEL_KEY_4242"] == providers.model_relay_token("app-4242")
  assert captured["AGENT_TOKEN"] == "synthetic-control-token"
  assert captured["API_BASE_URL"] == "http://127.0.0.1:9"
  assert captured["MOBIUS_RUN_TOKEN"] == "synthetic-run"
