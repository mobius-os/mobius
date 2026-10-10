"""Codex restores the Goal brief in the same turn right after it compacts."""

import asyncio
import importlib.util
import json
import os
import shutil
import threading
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.chat_writer import create_chat

from app import codex_sdk_runner, helper_hosts
from app.codex_sdk_runner import (
  ActiveCodexTurn, _codex_goal_brief_hook_override, _codex_owner_card_hook_override,
  _codex_platform_hook_overrides, _codex_platform_hook_thread_config,
)
from app.goals import CompactionBriefRefresh

BRIEF = "FRESH_GOAL_BRIEF"
DELIVERED = "Context was compacted; current Goal brief follows (read_goal re-reads it).\n" + BRIEF


def _hook():
  path = Path(__file__).resolve().parents[1] / "scripts" / "codex_goal_brief_hook.py"
  spec = importlib.util.spec_from_file_location("codex_goal_brief_hook", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_brief_hook_runs_only_after_compaction_inline_and_for_every_turn():
  handler_group = tomllib.loads(_codex_goal_brief_hook_override())["hooks"]["SessionStart"][0]
  assert handler_group["matcher"] == "^compact$"
  handler = handler_group["hooks"][0]
  assert "codex_goal_brief_hook.py" in handler["command"]
  assert handler.get("async", False) is False
  # Spilling to a file would make the model fetch what turn start gives inline.
  assert handler["additionalContextLimit"] == 0
  assert _codex_platform_hook_overrides() == [
    _codex_goal_brief_hook_override(), _codex_owner_card_hook_override(),
  ]


def _metadata(override, *, key, command=None):
  from openai_codex.generated.v2_all import HookMetadata
  ((event, groups),) = tomllib.loads(override)["hooks"].items()
  group = groups[0]
  return HookMetadata.model_validate({
    "handlerType": "command", "command": command or group["hooks"][0]["command"],
    "currentHash": f"{key}-hash", "displayOrder": 0, "enabled": True,
    "eventName": event[0].lower() + event[1:], "isManaged": False, "key": key,
    "matcher": group["matcher"], "source": "sessionFlags",
    "sourcePath": "/tmp/config.toml", "timeoutSec": 15, "trustStatus": "untrusted",
  })


def _listing(monkeypatch, hooks):
  class Client:
    async def request(self, method, params, response_model):
      return SimpleNamespace(data=[SimpleNamespace(hooks=hooks)])
  monkeypatch.setattr(codex_sdk_runner, "control_client", lambda _: Client())


def test_thread_trust_covers_each_exact_platform_hook_and_nothing_else(monkeypatch):
  brief, card = _codex_goal_brief_hook_override(), _codex_owner_card_hook_override()
  _listing(monkeypatch, [
    _metadata(brief, key="brief"), _metadata(card, key="card"),
    _metadata(brief, key="lookalike", command="unreviewed-command"),
  ])
  result = asyncio.run(_codex_platform_hook_thread_config(
    object(), {"HooksListResponse": object}, "/work", {}, [card], optional=[brief],
  ))
  assert result["hooks.state"] == {
    "brief": {"trusted_hash": "brief-hash"}, "card": {"trusted_hash": "card-hash"},
  }
  # The owner-card hook stays required, exactly as before.
  _listing(monkeypatch, [_metadata(brief, key="brief")])
  with pytest.raises(RuntimeError, match="PostToolUse"):
    asyncio.run(_codex_platform_hook_thread_config(
      object(), {"HooksListResponse": object}, "/work", {}, [card], optional=[brief],
    ))


def test_undiscovered_brief_hook_only_loses_in_turn_restore(monkeypatch):
  brief, card = _codex_goal_brief_hook_override(), _codex_owner_card_hook_override()
  _listing(monkeypatch, [_metadata(card, key="card")])
  result = asyncio.run(_codex_platform_hook_thread_config(
    object(), {"HooksListResponse": object}, "/work", {}, [card], optional=[brief],
  ))
  # Left untrusted, Codex never runs it; the next turn's brief covers it.
  assert result["hooks.state"] == {"card": {"trusted_hash": "card-hash"}}

  class Broken:
    async def request(self, *_args, **_kwargs):
      raise RuntimeError("hooks/list unavailable")
  monkeypatch.setattr(codex_sdk_runner, "control_client", lambda _: Broken())
  config = {"mcp_servers": {}}
  assert asyncio.run(_codex_platform_hook_thread_config(
    object(), {"HooksListResponse": object}, "/work", config, [], optional=[brief],
  )) == config
  with pytest.raises(RuntimeError, match="unavailable"):
    asyncio.run(_codex_platform_hook_thread_config(
      object(), {"HooksListResponse": object}, "/work", config, [card], optional=[brief],
    ))


def _run_turn(monkeypatch, *, delegated, refresh, discovery):
  from tests.test_codex_sdk_runner import (
    _FakeBroadcast, _FakeThread, _FakeTurnCompletedNotification, _FakeTurnHandle, _fake_sdk,
  )
  completed = SimpleNamespace(id="turn", usage=None, error=None)
  thread = _FakeThread("thread-a", _FakeTurnHandle([SimpleNamespace(
    method="turn/completed", payload=_FakeTurnCompletedNotification(completed),
  )]))
  captured = {"turn_thread": thread}

  class FakeAsyncCodex:
    def __init__(self, config=None):
      captured["config"] = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_args):
      return None

    async def thread_start(self, **kwargs):
      captured["thread"] = kwargs
      return thread

  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: {**_fake_sdk(FakeAsyncCodex), "HooksListResponse": object})
  monkeypatch.setattr(codex_sdk_runner, "_codex_platform_hook_thread_config", discovery)
  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="work", session_id=None, base_env={}, cwd="/tmp", chat_id="chat",
    bc=_FakeBroadcast(), pending_questions={}, db=None,
    run_policy=SimpleNamespace() if delegated else None, goal_brief_refresh=refresh,
  ))
  return result, captured


@pytest.mark.parametrize("delegated", [True, False])
@pytest.mark.parametrize("has_refresh", [True, False])
def test_only_a_turn_with_a_goal_brief_refresher_trusts_the_brief_hook(
  monkeypatch, delegated, has_refresh,
):
  calls = []

  async def discovery(codex, sdk, cwd, config, hook_overrides, *, optional=()):
    calls.append((list(hook_overrides), list(optional)))
    return config

  refresh = CompactionBriefRefresh(lambda: BRIEF) if has_refresh else None
  result, captured = _run_turn(
    monkeypatch, delegated=delegated, refresh=refresh, discovery=discovery,
  )
  assert result["error"] is None
  brief, card = _codex_goal_brief_hook_override(), _codex_owner_card_hook_override()
  required = [card]
  optional = [brief] if has_refresh else []
  # Both root and delegated turns require the exact turn-end hook.
  assert calls == ([(required, optional)] if required or optional else [])
  # Launch definitions stay static (shared helper hosts are keyed without
  # them); only thread trust makes Codex run a hook.
  overrides = captured["config"].kwargs["config_overrides"]
  assert [o for o in overrides if o.startswith("hooks.")] == (
    _codex_platform_hook_overrides()
  )


def test_failed_turn_end_hook_discovery_fails_the_delegated_turn_closed(monkeypatch):
  class Broken:
    async def request(self, *_args, **_kwargs):
      raise RuntimeError("hooks/list unavailable")
  monkeypatch.setattr(codex_sdk_runner, "control_client", lambda _: Broken())
  result, captured = _run_turn(
    monkeypatch, delegated=True, refresh=CompactionBriefRefresh(lambda: BRIEF),
    discovery=_codex_platform_hook_thread_config,
  )
  assert "hooks/list unavailable" in result["error"]
  assert "thread" not in captured


def test_goal_less_coordinator_trusts_lazy_hook_without_changing_turn_input(monkeypatch):
  calls = []

  async def discovery(codex, sdk, cwd, config, required, *, optional=()):
    calls.append(list(optional))
    return config

  refresh = CompactionBriefRefresh(lambda: "")
  result, captured = _run_turn(monkeypatch, delegated=False, refresh=refresh, discovery=discovery)
  assert result["error"] is None
  assert calls == [[_codex_goal_brief_hook_override()]]
  # The hook is a lazy context read, not an extra model turn or prompt item.
  assert captured["turn_thread"].turn_args == ("work",)

  async def compact():
    active = ActiveCodexTurn(SimpleNamespace(id="thread-a"), object(), chat_id="chat",
                             goal_brief_refresh=refresh)
    return await active.goal_brief_after_compaction("thread-a")

  assert asyncio.run(compact()) == ""
  assert not refresh.stale


def _active(thread_id="thread-a", load=lambda: BRIEF):
  refresh = CompactionBriefRefresh(load)
  return ActiveCodexTurn(SimpleNamespace(id=thread_id), object(), chat_id="chat",
                         goal_brief_refresh=refresh), refresh


@pytest.mark.asyncio
async def test_each_compaction_hook_delivers_the_brief_exactly_once():
  # Codex runs the hook only after it compacts: its arrival is the signal.
  active, refresh = _active()
  assert not refresh.stale
  assert await active.goal_brief_after_compaction("thread-a") == DELIVERED
  assert not refresh.stale
  assert await active.goal_brief_after_compaction("thread-a") == DELIVERED
  assert not refresh.stale


@pytest.mark.asyncio
async def test_an_earlier_unserved_need_is_not_delivered_twice_by_a_later_hook():
  loads = iter([RuntimeError("db busy"), BRIEF, BRIEF])

  def load():
    value = next(loads)
    if isinstance(value, Exception):
      raise value
    return value

  active, refresh = _active(load=load)
  assert await active.goal_brief_after_compaction("thread-a") == ""
  assert refresh.stale
  # The next compaction's hook serves its own need and absorbs the earlier
  # one: one brief, not two, and nothing left over for a third call.
  assert await active.goal_brief_after_compaction("thread-a") == DELIVERED
  assert not refresh.stale


@pytest.mark.asyncio
async def test_no_refresher_means_no_brief():
  active = ActiveCodexTurn(SimpleNamespace(id="thread-a"), object(), chat_id="chat")
  assert await active.goal_brief_after_compaction("thread-a") == ""


@pytest.mark.asyncio
async def test_compaction_hook_for_another_thread_or_ending_turn_is_refused():
  active, refresh = _active()
  with pytest.raises(LookupError):
    await active.goal_brief_after_compaction("thread-b")
  stopping, stopping_refresh = _active()
  stopping._interrupt_requested = True
  with pytest.raises(LookupError):
    await stopping.goal_brief_after_compaction("thread-a")
  active.mark_finished()
  with pytest.raises(LookupError):
    await active.goal_brief_after_compaction("thread-a")
  assert not refresh.stale and not stopping_refresh.stale


def test_hook_ignores_other_session_starts_and_unknown_identity(monkeypatch):
  hook = _hook()
  calls = []
  monkeypatch.setattr(hook, "_agent_api_call", lambda *a: calls.append(a) or {"context": "x"})
  monkeypatch.setenv("CHAT_ID", "chat")
  monkeypatch.setenv("AGENT_TOKEN", "token")
  for source in ("startup", "resume", "clear"):
    assert hook.compaction_context({"hook_event_name": "SessionStart", "source": source,
                                    "session_id": "thread-a"}) == ""
  assert calls == []
  monkeypatch.delenv("CHAT_ID")
  monkeypatch.delenv("AGENT_TOKEN")
  monkeypatch.setenv(helper_hosts.THREAD_ENV_LINKS_ENV, "/nonexistent")
  for thread_id in ("../escape", "thread-a"):
    assert hook.compaction_context({"hook_event_name": "SessionStart", "source": "compact",
                                    "session_id": thread_id}) == ""
  assert calls == []


def test_host_hook_uses_only_its_threads_own_turn_identity(monkeypatch, tmp_path):
  hook = _hook()
  links = tmp_path / "links"
  ours = helper_hosts.TurnEnvFile(tmp_path / "a", "a", {"CHAT_ID": "child-a", "AGENT_TOKEN": "ta"})
  theirs = helper_hosts.TurnEnvFile(tmp_path / "b", "b", {"CHAT_ID": "child-b", "AGENT_TOKEN": "tb"})
  ours.link_thread(links, "thread-a")
  theirs.link_thread(links, "thread-b")
  calls = []
  monkeypatch.setattr(hook, "_agent_api_call",
                      lambda *a: calls.append((a, os.environ["AGENT_TOKEN"])) or {"context": "c"})
  for name in ("CHAT_ID", "AGENT_TOKEN"):
    monkeypatch.delenv(name, raising=False)
  monkeypatch.setenv(helper_hosts.THREAD_ENV_LINKS_ENV, str(links))
  assert hook.compaction_context({"hook_event_name": "SessionStart", "source": "compact",
                                  "session_id": "thread-a"}) == "c"
  assert calls == [(("POST", "/api/chats/child-a/goal-brief/compaction",
                     {"thread_id": "thread-a"}), "ta")]
  # A finished turn takes its link and secret file with it, and never a later
  # turn's link for the same thread.
  later = helper_hosts.TurnEnvFile(tmp_path / "c", "c", {"CHAT_ID": "child-a"})
  later.link_thread(links, "thread-a")
  ours.remove()
  assert os.readlink(links / "thread-a.env") == str(later.path)
  theirs.remove()
  assert not (links / "thread-b.env").exists() and not theirs.path.exists()


def test_compaction_endpoint_serves_only_the_live_run_on_that_thread(
  client, owner_token, db, monkeypatch,
):
  from app import auth as auth_mod, chat_event_sink, models
  from app.runner_registry import registry
  db.add(create_chat(id="codex-chat", title="c", messages=[]))
  db.add(create_chat(id="other-chat", title="o", messages=[]))
  db.add(models.ChatRun(id="run-live", chat_id="codex-chat", status="running", provider="codex"))
  # A still-valid run of this chat that does not own the streaming turn.
  db.add(models.ChatRun(id="run-old", chat_id="codex-chat", status="running", provider="codex"))
  db.add(models.ChatRun(id="run-other", chat_id="other-chat", status="running", provider="codex"))
  db.commit()
  owner = db.query(models.Owner).first()

  def headers(chat_id, run_id):
    return {"Authorization": "Bearer " + auth_mod.create_agent_token(
      chat_id, owner.username, owner.token_epoch, run_id=run_id)}

  calls = []

  class Handle:
    async def goal_brief_after_compaction(self, thread_id):
      calls.append(thread_id)
      if thread_id != "thread-a":
        raise LookupError("This turn does not own that Codex thread.")
      return DELIVERED

  monkeypatch.setattr(chat_event_sink, "get_active_sink",
                      lambda chat_id: SimpleNamespace(run_token="run-live"))
  monkeypatch.setattr(registry, "get_handle", lambda chat_id, kind: Handle())
  url = "/api/chats/codex-chat/goal-brief/compaction"
  ok = client.post(url, json={"thread_id": "thread-a"}, headers=headers("codex-chat", "run-live"))
  assert ok.status_code == 200 and ok.json() == {"context": DELIVERED}
  assert client.post(url, json={"thread_id": "thread-b"},
                     headers=headers("codex-chat", "run-live")).status_code == 409
  assert client.post(url, json={"thread_id": "thread-a"},
                     headers=headers("codex-chat", "run-old")).status_code == 409
  assert client.post(url, json={"thread_id": "thread-a"},
                     headers=headers("other-chat", "run-other")).status_code == 403
  assert calls == ["thread-a", "thread-b"]


def _stream(output, response_id, input_tokens):
  from tests.test_mobius_codex_e2e import _response
  response = _response(response_id, output)
  response["usage"].update(input_tokens=input_tokens, total_tokens=input_tokens + 4)
  rows = [{"type": "response.output_item.done", "output_index": i, "sequence_number": i,
           "item": item} for i, item in enumerate(output)]
  rows.append({"type": "response.completed", "sequence_number": len(output),
               "response": response})
  return b"".join(f"event: {r['type']}\ndata: {json.dumps(r)}\n\n".encode() for r in rows)


def _developer_texts(request):
  return [part.get("text", "") for item in request.get("input", [])
          if item.get("role") == "developer" for part in item.get("content", [])]


@pytest.mark.parametrize("identity", ["process", "helper_host", "untrusted"])
def test_installed_codex_restores_brief_in_the_compacted_turn_without_a_model_call(
  tmp_path, monkeypatch, identity,
):
  if not shutil.which("codex"):
    pytest.skip("installed Codex is required")
  from openai_codex import ApprovalMode, AsyncCodex, CodexConfig, Sandbox
  from openai_codex.generated.v2_all import HooksListResponse
  from openai_codex.types import Personality
  from app.providers import MobiusProvider

  model_requests, brief_calls, state = [], [], {}
  turn_token = "turn-only-token"

  class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):
      body = json.loads(self.rfile.read(int(self.headers.get("content-length", "0"))))
      if self.path.endswith("/goal-brief/compaction"):
        brief_calls.append((self.path, self.headers.get("authorization"), body))
        assert self.headers.get("authorization") == f"Bearer {turn_token}"
        context = asyncio.run_coroutine_threadsafe(
          state["active"].goal_brief_after_compaction(body["thread_id"]), state["loop"],
        ).result(timeout=10)
        content, kind = json.dumps({"context": context}).encode(), "application/json"
      else:
        model_requests.append(body)
        n = len(model_requests)
        if n == 1:
          # A tool call whose reported usage forces auto-compaction mid-turn.
          output = [{"id": "tool", "type": "function_call", "status": "completed",
                     "call_id": "tool-call", "name": "exec_command",
                     "arguments": json.dumps({"cmd": "echo working"})}]
          tokens = 900_000
        else:
          output = [{"id": f"reply-{n}", "type": "message", "role": "assistant",
                     "status": "completed", "phase": "final_answer",
                     "content": [{"type": "output_text", "text": f"reply {n}",
                                  "annotations": [], "logprobs": []}]}]
          tokens = 10
        content, kind = _stream(output, f"response-{n}", tokens), "text/event-stream"
      self.send_response(200)
      self.send_header("content-type", kind)
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
  codex_home = tmp_path / "codex-home"
  codex_home.mkdir()
  identity_env = {"CHAT_ID": "local-chat", "AGENT_TOKEN": turn_token}
  # Identity can also be reloaded indirectly by a shell or the control
  # module. Exercise those ambient bridges with synthetic values, never a
  # real caller's files or bearer.
  ambient = tmp_path / 'ambient-identity.env'
  ambient.write_text('export CHAT_ID=wrong-chat\nexport AGENT_TOKEN=ambient-fake-token\n')
  monkeypatch.setenv(helper_hosts.CALLER_ENV_FILE_ENV, str(ambient))
  monkeypatch.setenv('BASH_ENV', str(ambient))
  # The SDK starts Codex with this process's environment plus ``env``; never
  # let this process's own run identity reach the provider or its hook.
  for name in list(os.environ):
    if helper_hosts.is_per_turn_env(name) or name in {
      helper_hosts.CALLER_ENV_FILE_ENV, helper_hosts.THREAD_ENV_LINKS_ENV,
      helper_hosts.HOST_MARKER_ENV, 'BASH_ENV', 'ENV',
    }:
      monkeypatch.delenv(name)
  env = dict(CODEX_HOME=str(codex_home), API_BASE_URL=base_url,
             MOBIUS_LOCAL_BROKER_KEY="test-only")
  links = tmp_path / "thread-env"
  turn_env = None
  if identity in ("process", "untrusted"):
    env.update(identity_env)
  else:
    env[helper_hosts.THREAD_ENV_LINKS_ENV] = str(links)
    turn_env = helper_hosts.TurnEnvFile(tmp_path / "turn", "turn", identity_env)
  hook = _codex_goal_brief_hook_override()
  overrides = [
    *MobiusProvider().codex_config_overrides(),
    f'model_providers.mobius_trial.base_url="{base_url}/v1"',
    'model="inkling"', 'web_search="disabled"', "model_auto_compact_token_limit=1000", hook,
  ]

  async def scenario():
    state["loop"] = asyncio.get_running_loop()
    config = CodexConfig(codex_bin=shutil.which("codex"), cwd=str(tmp_path),
                         env=env, config_overrides=overrides)
    async with AsyncCodex(config=config) as codex:
      # A turn without a Goal brief refresher never trusts the defined hook.
      thread_config = {} if identity == "untrusted" else await (
        _codex_platform_hook_thread_config(
          codex, {"HooksListResponse": HooksListResponse}, str(tmp_path), {}, [],
          optional=[hook],
        )
      )
      thread = await codex.thread_start(
        cwd=str(tmp_path), model="inkling", approval_mode=ApprovalMode.deny_all,
        sandbox=Sandbox.full_access, personality=Personality.none, config=thread_config,
      )
      if turn_env is not None:
        turn_env.link_thread(links, thread.id)
      refresh = CompactionBriefRefresh(lambda: BRIEF)
      state["refresh"] = refresh
      state["active"] = ActiveCodexTurn(thread, None, chat_id="local-chat",
                                        goal_brief_refresh=refresh)
      turn = state["active"].turn = await thread.turn("Work until done.")
      async for note in turn.stream():
        if note.method == "turn/completed":
          state["thread_id"] = thread.id
          return note.payload.turn

  try:
    terminal = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
  finally:
    server.shutdown()
    server.server_close()
    worker.join(timeout=5)
    if turn_env is not None:
      turn_env.remove()
  assert terminal.status.value == "completed"
  compaction = next(i for i, r in enumerate(model_requests)
                    if "CONTEXT CHECKPOINT COMPACTION" in json.dumps(r["input"]))
  if identity == "untrusted":
    # No hook process, no HTTP call, no brief; the compaction still completes.
    assert brief_calls == []
    assert all(DELIVERED not in _developer_texts(r) for r in model_requests)
    assert len(model_requests) == compaction + 2
    return
  assert brief_calls == [("/api/chats/local-chat/goal-brief/compaction",
                          f"Bearer {turn_token}", {"thread_id": state["thread_id"]})]
  # Startup, the tool round trip, and the summary carry no refresh; the
  # compacted turn's very next request does, with no extra request added.
  assert all(DELIVERED not in _developer_texts(r) for r in model_requests[:compaction + 1])
  assert _developer_texts(model_requests[compaction + 1]).count(DELIVERED) == 1
  assert len(model_requests) == compaction + 2
  assert not state["refresh"].stale
