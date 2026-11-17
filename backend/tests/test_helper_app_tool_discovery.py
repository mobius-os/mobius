"""Exercise helper discovery over MCP before a child has made a tool call."""

from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import subprocess
import threading

import pytest

from app import claude_helper_host, connectors, helper_hosts
from app.platform_tools import CONTROL_SERVER_NAME, claude_control_servers, codex_turn_mcp_config
from tests.test_app_tools import _app


def _exchange(server, env, messages):
  result = subprocess.run(
    [server["command"], *server["args"]],
    env={**env, **server.get("env", {})},
    input="".join(json.dumps(message) + "\n" for message in messages),
    text=True, capture_output=True, check=False, timeout=10,
  )
  assert result.returncode == 0, result.stderr
  return {reply["id"]: reply for reply in map(json.loads, result.stdout.splitlines())}


@pytest.mark.parametrize("installed", [True, False])
def test_claude_shared_host_discovers_reviewed_tools_before_any_child_identity(
  db, tmp_path, monkeypatch, installed,
):
  # This is the same split + options factory used at Claude host startup.
  if installed:
    _app(db, "gmail")
  host_env, _turn_env = helper_hosts.split_env({
    "API_BASE_URL": "http://127.0.0.1:9", "AGENT_TOKEN": "child-a-bearer",
    "CHAT_ID": "child-a", "MOBIUS_RUN_TOKEN": "child-a-run",
  })
  host_env[helper_hosts.HOST_MARKER_ENV] = "host-fixture"
  captured = {}

  @contextmanager
  def config_handle(_plan, *, extra_servers):
    captured.update(extra_servers)
    yield None

  monkeypatch.setattr(connectors, "claude_mcp_config_handle", config_handle)
  factory = claude_helper_host._host_options(
    key=helper_hosts.HostKey("parent", "claude", str(tmp_path), "setup"),
    host_env=host_env, skill_text="", connector_plan=None,
    skills_enabled=False, model="test-model", supports_effort=False,
  )
  with ExitStack() as stack:
    host = claude_helper_host.ClaudeHelperHost(
      helper_hosts.HostKey("parent", "claude", str(tmp_path), "setup"),
      options_factory=factory, session_file=tmp_path / "session.json",
    )
    options = factory(host, None, stack)
    assert "AGENT_TOKEN" not in options.env
    assert "child-a-bearer" not in repr(captured)
    replies = _exchange(captured["mobius_control"], options.env, [{
      "jsonrpc": "2.0", "id": 1, "method": "tools/list",
    }])
    tools = replies[1]["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert ("gmail_log_friction" in names) is installed
    assert "app_tools_unavailable" not in names
    snapshot = Path(captured[CONTROL_SERVER_NAME]["env"]["MOBIUS_APP_TOOL_CATALOGUE_FILE"])
    assert json.loads(snapshot.read_text()) == {"tools": [{
      "name": "gmail_log_friction",
      "description": "Record what made this work harder than it should have been.",
      "inputSchema": {"type": "object", "properties": {"friction": {"type": "string"}},
                      "required": ["friction"]},
    }] if installed else []}
  assert not snapshot.exists()


APP_TOOL = {
  "name": "gmail_read", "description": "Read test mail.",
  "inputSchema": {"type": "object"},
}


@contextmanager
def _catalogue_api(listing=None, status=200):
  """Local protocol fixture: exact callers only; no installed service is run."""
  calls = []

  class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
      pass

    def respond(self, code, data):
      self.send_response(code)
      self.end_headers()
      self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
      token = self.headers.get("Authorization")
      calls.append(("GET", token, None))
      assert self.path == "/api/agent/app-tools/"
      if token not in ("Bearer child-a-bearer", "Bearer child-b-bearer"):
        return self.respond(403, {"detail": "Exact child identity required."})
      self.respond(status, listing)

    def do_POST(self):
      token = self.headers.get("Authorization")
      payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
      calls.append(("POST", token, payload))
      assert self.path == "/api/agent/app-tools/call"
      expected = f"Bearer {payload['arguments']['child']}-bearer"
      if token != expected:
        return self.respond(403, {"detail": "Wrong child identity."})
      self.respond(200, {"result": payload["arguments"]["child"], "is_error": False})

  server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
  thread = threading.Thread(target=server.serve_forever)
  thread.start()
  try:
    yield f"http://127.0.0.1:{server.server_port}", calls
  finally:
    server.shutdown()
    server.server_close()
    thread.join()


def _list_message():
  return {"jsonrpc": "2.0", "id": "list", "method": "tools/list"}


def test_codex_startup_loads_only_its_turn_identity_for_discovery(tmp_path):
  with _catalogue_api({"tools": [APP_TOOL]}) as (base, calls):
    host_env, turn_env = helper_hosts.split_env({
      "API_BASE_URL": base, "AGENT_TOKEN": "child-b-bearer", "CHAT_ID": "child-b",
    })
    env_file = helper_hosts.TurnEnvFile(tmp_path, "b", turn_env)
    config = helper_hosts.codex_turn_thread_config(
      codex_turn_mcp_config(None, control_enabled=True),
      turn_env=turn_env, env_file=env_file,
    )
    replies = _exchange(config["mcp_servers"][CONTROL_SERVER_NAME], host_env, [_list_message()])
    assert APP_TOOL in replies["list"]["result"]["tools"]
    assert calls == [("GET", "Bearer child-b-bearer", None)]
    assert "AGENT_TOKEN" not in host_env


@pytest.mark.parametrize("listing,status", [
  ({"tools": []}, 200),
  ({"detail": "Catalogue denied."}, 403),
  ({"detail": "Catalogue unavailable."}, 503),
  ({}, 200),
  ({"tools": [APP_TOOL, {"name": "broken"}]}, 200),
  ({"tools": "not-a-list"}, 200),
])
def test_discovery_distinguishes_valid_empty_from_denied_unavailable_and_malformed(listing, status):
  with _catalogue_api(listing, status) as (base, _calls):
    replies = _exchange(claude_control_servers(enabled=True)[CONTROL_SERVER_NAME], {
      "API_BASE_URL": base, "AGENT_TOKEN": "child-a-bearer",
    }, [_list_message(), {
      "jsonrpc": "2.0", "id": "diagnostic", "method": "tools/call",
      "params": {"name": "app_tools_unavailable", "arguments": {}},
    }])
  tools = replies["list"]["result"]["tools"]
  diagnostic = [tool for tool in tools if tool["name"] == "app_tools_unavailable"]
  valid_empty = listing == {"tools": []} and status == 200
  assert bool(diagnostic) is not valid_empty
  if diagnostic:
    assert "not an empty catalogue" in diagnostic[0]["description"]
    if status != 200:
      assert f"Refused ({status})" in diagnostic[0]["description"]
    else:
      assert "malformed" in diagnostic[0]["description"]
    assert diagnostic[0]["_meta"] == {"anthropic/alwaysLoad": True}
    assert "gmail_read" not in [tool["name"] for tool in tools]
  assert replies["diagnostic"]["result"]["isError"] is True


@pytest.mark.parametrize("snapshot", [None, "missing", "not json", '{"tools": false}'])
def test_missing_identity_or_unreadable_snapshot_is_visible_not_empty(tmp_path, snapshot):
  env = {"API_BASE_URL": "http://127.0.0.1:9", "MOBIUS_HELPER_HOST": "host"}
  if snapshot is not None:
    path = tmp_path / "catalogue.json"
    if snapshot != "missing":
      path.write_text(snapshot)
    env["MOBIUS_APP_TOOL_CATALOGUE_FILE"] = str(path)
  replies = _exchange(claude_control_servers(enabled=True)[CONTROL_SERVER_NAME], env, [_list_message()])
  tools = replies["list"]["result"]["tools"]
  assert any(tool["name"] == "app_tools_unavailable" for tool in tools)


@pytest.mark.asyncio
async def test_shared_catalogue_cannot_authorize_calls_or_lend_one_childs_bearer_to_another(tmp_path):
  from tests.test_helper_hosts import _claude_host, _turn, _Started

  host = _claude_host(tmp_path)
  turns = {}
  for child in ("child-a", "child-b"):
    directory = tmp_path / child
    directory.mkdir()
    turn = _turn(directory, dispatch_id=child)
    turn.env_file = helper_hosts.TurnEnvFile(directory, child, {
      "AGENT_TOKEN": f"{child}-bearer", "CHAT_ID": child,
    })
    host._turn_by_tool_use[child] = turn
    host._on_task_started(_Started(child, child))
    turns[child] = turn

  # A model's forged path must be replaced by the host-owned identity hook.
  messages = []
  for child in turns:
    decision = await host.pre_tool_use({
      "agent_id": child, "tool_name": "mcp__mobius_control__gmail_read",
      "tool_input": {"child": child, "_mobius_caller_env_file": str(turns["child-a"].env_file.path)},
    }, f"call-{child}", None)
    stamped = decision["hookSpecificOutput"]["updatedInput"]
    assert stamped["_mobius_caller_env_file"] == str(turns[child].env_file.path)
    messages.append({"jsonrpc": "2.0", "id": child, "method": "tools/call", "params": {
      "name": "gmail_read", "arguments": stamped,
    }})

  snapshot = tmp_path / "catalogue.json"
  snapshot.write_text(json.dumps({"tools": [APP_TOOL]}))
  messages.append({"jsonrpc": "2.0", "id": "unscoped", "method": "tools/call", "params": {
    "name": "gmail_read", "arguments": {"child": "child-a"},
  }})
  with _catalogue_api({"tools": [APP_TOOL]}) as (base, calls):
    replies = _exchange(claude_control_servers(enabled=True)[CONTROL_SERVER_NAME], {
      "API_BASE_URL": base, "MOBIUS_HELPER_HOST": "host",
      "MOBIUS_APP_TOOL_CATALOGUE_FILE": str(snapshot),
    }, [_list_message(), *messages])
  assert APP_TOOL in replies["list"]["result"]["tools"]
  for child in turns:
    assert replies[child]["result"] == {
      "content": [{"type": "text", "text": child}], "isError": False,
    }
  assert replies["unscoped"]["result"]["isError"] is True
  posts = [(token, payload["arguments"]) for method, token, payload in calls if method == "POST"]
  assert sorted(posts, key=lambda row: row[0]) == [
    ("Bearer child-a-bearer", {"child": "child-a"}),
    ("Bearer child-b-bearer", {"child": "child-b"}),
  ]


@pytest.mark.parametrize("listing,status,reason", [
  ({"detail": "Run is no longer authorized."}, 403, "Refused (403)"),
  ({"tools": []}, 200, "Tool is unavailable"),
])
def test_shared_snapshot_never_substitutes_for_live_call_authority(tmp_path, listing, status, reason):
  snapshot = tmp_path / "catalogue.json"
  snapshot.write_text(json.dumps({"tools": [APP_TOOL]}))
  env_file = helper_hosts.TurnEnvFile(tmp_path, "caller", {"AGENT_TOKEN": "child-a-bearer"})
  with _catalogue_api(listing, status) as (base, calls):
    replies = _exchange(claude_control_servers(enabled=True)[CONTROL_SERVER_NAME], {
      "API_BASE_URL": base, "MOBIUS_HELPER_HOST": "host",
      "MOBIUS_APP_TOOL_CATALOGUE_FILE": str(snapshot),
    }, [_list_message(), {
      "jsonrpc": "2.0", "id": "call", "method": "tools/call",
      "params": {"name": "gmail_read", "arguments": {
        "_mobius_caller_env_file": str(env_file.path),
      }},
    }])
  assert APP_TOOL in replies["list"]["result"]["tools"]
  assert replies["call"]["result"]["isError"] is True
  assert reason in replies["call"]["result"]["content"][0]["text"]
  assert calls == [("GET", "Bearer child-a-bearer", None)]


def test_discovery_transport_timeout_is_visible_to_the_agent(monkeypatch):
  from tests.test_platform_tools import _control_module

  control = _control_module()
  monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:9")
  monkeypatch.setenv("AGENT_TOKEN", "test-bearer")

  def timeout(*_args, **_kwargs):
    raise TimeoutError("timed out")

  monkeypatch.setattr(control, "urlopen", timeout)
  tools = control._dispatch_message(_list_message())["result"]["tools"]
  diagnostic = next(tool for tool in tools if tool["name"] == "app_tools_unavailable")
  assert "request timed out" in diagnostic["description"]
