"""App agent roles: the declaration, consent, the agent key's reach, the Agent
Gateway's filtering, and the room each confined agent runs in."""

import importlib.util
import json
import socket
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from app import agent_gateway, agent_roles, app_consent, auth, chat_writer, models
from app.agent_roles import AgentKey, AgentRole, Room
from app.app_capabilities import contract_from_manifest
from app.manifest_contract import ManifestContractError, validate_manifest_contract

FENCE = Path(agent_roles.FENCE_SCRIPT)


def _manifest(**extra):
  return {
    "id": "notes-helper",
    "name": "Notes helper",
    "description": "Keeps notes.",
    "version": "1.0.0",
    "entry": "index.jsx",
    **extra,
  }


def _role(role, *tools, why="Answers questions about your notes."):
  return {"role": role, "tools": list(tools), "why": why}


def _rows(declaration=None, *, embeds_agent=False):
  manifest = _manifest(embeds_agent=embeds_agent)
  if declaration is not None:
    manifest["agent_role"] = declaration
  return app_consent.rows_from_contract(contract_from_manifest(manifest))


def _agent_row(declaration=None, **kwargs):
  rows = [row for row in _rows(declaration, **kwargs) if row.get("group") == "agent"]
  assert len(rows) <= 1
  return rows[0] if rows else None


# --- the declaration -------------------------------------------------------------


@pytest.mark.parametrize(
  "declaration, problem",
  [
    (_role("admin"), "agent_role.role"),
    (_role("viewer", "shell"), "agent_role.tools"),
    (_role("viewer", "web", "web"), "agent_role.tools"),
    (_role("owner", "web"), "declares no tools"),
    (_role("editor", why=""), "agent_role.why"),
    (_role("editor", why="x" * 81), "agent_role.why"),
    ({**_role("editor"), "extra": True}, "role, tools and why"),
  ],
)
def test_a_malformed_agent_role_is_refused_with_its_reason(declaration, problem):
  with pytest.raises(ManifestContractError, match=problem):
    validate_manifest_contract(_manifest(agent_role=declaration))


@pytest.mark.parametrize("role", ["viewer", "editor", "explorer"])
def test_web_may_be_declared_for_any_confined_role(role):
  validate_manifest_contract(_manifest(agent_role=_role(role, "web", "notify")))
  assert contract_from_manifest(_manifest(agent_role=_role(role, "web")))["agent"][
    "role"
  ]["tools"] == ["web"]


def test_an_app_without_a_role_keeps_its_contract_unchanged():
  contract = contract_from_manifest(_manifest())
  assert "role" not in contract["agent"]
  assert agent_roles.role_from_contract(contract) is None


def test_an_unreadable_stored_role_never_gains_reach():
  role = agent_roles.role_from_contract(
    {"agent": {"role": {"role": "superuser", "tools": [], "why": "x"}}}
  )
  assert role == AgentRole("explorer", (), "")
  assert role.confined and role.spec.data is None


# --- consent -------------------------------------------------------------------


def test_each_confined_role_is_one_agent_row_with_the_apps_own_reason():
  row = _agent_row(_role("editor", "web", "notify"))
  assert row["id"] == "agent_role" and row["level"] == "Editor" and not row["critical"]
  assert row["text"] == "Answers questions about your notes."
  assert [(tool["id"], tool["warning"]) for tool in row["tools"]] == [
    ("notify", False),
    ("web", True),
  ]
  assert _agent_row(_role("explorer", "web"))["tools"][0]["warning"] is False


def test_owner_role_and_undeclared_agents_are_the_critical_full_access_row():
  owner = _agent_row(_role("owner", why="Manages your apps."))
  legacy = _agent_row(embeds_agent=True)
  for row in (owner, legacy):
    assert (
      row["id"] == "agent_start"
      and row["critical"]
      and row["level"] == "Owner"
      and row["tools"] == []
    )
  assert owner["text"] == "Manages your apps."
  assert _agent_row() is None


def _asks_again(before, after):
  accepted = {
    r["id"]: {"rank": r["rank"], "scope": set(r["scope"] or []), "limits": {}}
    for r in _rows(before)
  }
  return [r["id"] for r in _rows(after) if not app_consent._covered(r, accepted)]


def test_updates_ask_again_only_when_they_add_reach():
  assert _asks_again(_role("viewer"), _role("editor")) == ["agent_role"]
  assert _asks_again(_role("viewer"), _role("viewer", "web")) == ["agent_role"]
  assert _asks_again(_role("explorer", "web"), _role("viewer", "web")) == ["agent_role"]
  assert _asks_again(_role("editor", "web"), _role("viewer")) == []
  assert _asks_again(_role("viewer", "web"), _role("explorer", "web")) == []
  assert _asks_again(_role("viewer"), _role("viewer", why="Reworded reason.")) == []


# --- the agent key -------------------------------------------------------------


def _key(role, *tools, app_id=7, provider="claude"):
  return AgentKey(app_id, role, frozenset(tools), provider)


@pytest.mark.parametrize(
  "key, method, path, allowed",
  [
    (_key("viewer"), "GET", "/api/storage/apps/7/notes.json", True),
    (_key("viewer"), "PUT", "/api/storage/apps/7/notes.json", False),
    (_key("editor"), "PUT", "/api/storage/apps/7/notes.json", True),
    (_key("editor"), "GET", "/api/storage/apps-list/7/", True),
    (_key("editor"), "GET", "/api/storage/apps/8/notes.json", False),
    (_key("explorer"), "GET", "/api/storage/apps/7/notes.json", False),
    (_key("viewer"), "POST", "/api/agent-gateway/v1/messages", True),
    (_key("viewer"), "GET", "/api/proxy", False),
    (_key("viewer", "web"), "GET", "/api/proxy", True),
    (_key("viewer"), "POST", "/api/notifications/send", False),
    (_key("viewer", "notify"), "POST", "/api/notifications/send", True),
    (_key("editor", "web", "notify"), "POST", "/api/app-chats", False),
    (_key("editor", "web", "notify"), "GET", "/api/apps/", False),
    (_key("editor", "web", "notify"), "GET", "/api/secrets/7/token", False),
  ],
)
def test_an_agent_key_reaches_only_its_roles_allow_list(key, method, path, allowed):
  assert key.may_call(method, path) is allowed


def _agent_key_for(db, app, *, role, tools=(), provider="claude"):
  owner = db.query(models.Owner).first()
  chat = chat_writer.create_chat(
    id=str(uuid.uuid4()), title="App chat", messages=[], created_by_app_id=app.id
  )
  db.add(chat)
  run = models.ChatRun(
    id=str(uuid.uuid4()),
    root_run_id="r",
    chat_id=chat.id,
    status="running",
    provider=provider,
  )
  run.root_run_id = run.id
  db.add(run)
  db.commit()
  key = auth.create_app_agent_token(
    app.id,
    owner.username,
    owner.token_epoch,
    app_nonce=app.token_nonce,
    consent=app_consent.session_id(app),
    chat_id=chat.id,
    run_id=run.id,
    grant=AgentKey.claims_for(AgentRole(role, tuple(tools), "why"), provider),
  )
  return key, run


def _local_app(db, slug, declaration):
  row = models.App(
    name=slug.title(),
    description="",
    jsx_source="",
    compiled_path="",
    source_dir=f"/tmp/{slug}",
    slug=slug,
    capability_contract=contract_from_manifest(_manifest(agent_role=declaration)),
  )
  db.add(row)
  db.commit()
  db.refresh(row)
  return row


def test_the_middleware_refuses_an_agent_key_outside_its_allow_list(
  client, owner_token, db
):
  app = _local_app(db, "notes", _role("viewer"))
  key, _run = _agent_key_for(db, app, role="viewer")
  bearer = {"Authorization": f"Bearer {key}"}
  refused = client.get("/api/apps/", headers=bearer)
  assert refused.status_code == 403 and "Viewer" in refused.json()["detail"]
  assert client.post("/api/app-chats", json={}, headers=bearer).status_code == 403
  assert client.get("/api/apps/", params={"token": key}).status_code == 403
  # The owner's own key is untouched by the allow-list.
  assert (
    client.get(
      "/api/apps/", headers={"Authorization": f"Bearer {owner_token}"}
    ).status_code
    == 200
  )


def test_an_agent_key_resolves_to_its_grant_and_expires_with_its_run(
  client, owner_token, db
):
  app = _local_app(db, "notes", _role("editor", "web"))
  key, run = _agent_key_for(db, app, role="editor", tools=("web",), provider="codex")
  bearer = {"Authorization": f"Bearer {key}"}
  assert client.get("/api/agent-gateway/v1/models", headers=bearer).status_code == 200
  owner = {"Authorization": f"Bearer {owner_token}"}
  assert client.get("/api/agent-gateway/v1/models", headers=owner).status_code == 403
  run.status = "completed"
  db.commit()
  assert client.get("/api/agent-gateway/v1/models", headers=bearer).status_code == 401


# --- the Agent Gateway ---------------------------------------------------------


def _body(**fields):
  return json.dumps(fields).encode()


def test_the_gateway_drops_provider_run_tools_the_app_did_not_declare():
  raw = _body(
    tools=[
      {"type": "custom", "name": "exec"},
      {"name": "Bash", "input_schema": {}},
      {"type": "web_search_20250305", "name": "web_search"},
      {"type": "code_execution_20250825"},
      {"type": "mcp", "server_url": "https://x.test"},
    ],
    mcp_servers=[{"url": "https://x.test"}],
    input=[
      {
        "type": "message",
        "additional_tools": [{"type": "image_generation"}, {"type": "function"}],
      }
    ],
  )
  body, removed = agent_gateway.prepare_body(_key("editor"), raw)
  sent = json.loads(body)
  assert [t.get("type", "") for t in sent["tools"]] == ["custom", ""]
  assert sent["input"][0]["additional_tools"] == [{"type": "function"}]
  assert "mcp_servers" not in sent
  assert set(removed) == {
    "mcp_servers",
    "web_search_20250305",
    "code_execution_20250825",
    "mcp",
    "image_generation",
  }
  kept, _ = agent_gateway.prepare_body(_key("editor", "web"), raw)
  assert any(t.get("type") == "web_search_20250305" for t in json.loads(kept)["tools"])


def test_a_request_forcing_a_removed_tool_fails_instead_of_inventing_a_result():
  raw = _body(
    tools=[{"type": "web_search_20250305", "name": "web_search"}],
    tool_choice={"type": "tool", "name": "web_search"},
  )
  with pytest.raises(agent_gateway.GatewayError) as refused:
    agent_gateway.prepare_body(_key("viewer"), raw)
  assert refused.value.status == 403


@pytest.mark.parametrize(
  "key, path, status",
  [
    (_key("viewer", provider="claude"), "v1/messages", None),
    (_key("viewer", provider="claude"), "v1/responses", 404),
    (_key("viewer", provider="claude"), "v1/files", 404),
    (_key("viewer", provider="codex"), "v1/responses", None),
    (_key("viewer", provider="codex"), "v1/alpha/search", 403),
    (_key("viewer", "web", provider="mobius"), "v1/alpha/search", None),
    (_key("viewer", provider="mobius-app-9"), "v1/responses", 404),
  ],
)
def test_the_gateway_serves_only_the_runs_own_model_endpoints(key, path, status):
  if status is None:
    assert agent_gateway.endpoint(key, path) == path
  else:
    with pytest.raises(agent_gateway.GatewayError) as refused:
      agent_gateway.endpoint(key, path)
    assert refused.value.status == status


# --- the room ------------------------------------------------------------------


def _room(tmp_path, role, *tools):
  return Room.for_run(
    AgentRole(role, tools, "why"),
    scratch=tmp_path / "scratch",
    storage_dir=tmp_path / "apps" / "7",
    api_base_url="http://localhost:8080",
  )


@pytest.mark.parametrize(
  "role, flag", [("viewer", "--ro"), ("editor", "--rw"), ("explorer", None)]
)
def test_the_rooms_walls_follow_the_role(tmp_path, role, flag):
  room = _room(tmp_path, role)
  args = room.fence_args()
  data = str(tmp_path / "apps" / "7")
  walls = {
    (args[i], args[i + 1]) for i in range(len(args) - 1) if args[i] in ("--ro", "--rw")
  }
  assert {wall for wall in walls if wall[1] == data} == (
    {(flag, data)} if flag else set()
  )
  assert room.cwd == (Path(data) if flag else tmp_path / "scratch" / "room")
  assert args[-1] == "--" and "--door" in args


def test_the_room_never_carries_a_login(tmp_path):
  room = _room(tmp_path, "editor", "web")
  env = room.env("agent-key")
  assert env["ANTHROPIC_AUTH_TOKEN"] == env["AGENT_TOKEN"] == "agent-key"
  assert env["ANTHROPIC_BASE_URL"] == "http://localhost:8080/api/agent-gateway"
  assert "cli-auth" not in json.dumps(env)
  assert env["MOBIUS_AGENT_TOOLS"] == "open_page"
  assert set(env) <= set(agent_roles.ROOM_ENV)


def test_codex_reaches_its_model_through_the_gateway(tmp_path):
  web = _room(tmp_path, "explorer", "web").codex_overrides([])
  assert 'model_provider="mobius_gateway"' in web
  assert (
    'model_providers.mobius_gateway.base_url="http://localhost:8080/api/agent-gateway/v1"'
    in web
  )
  assert 'web_search="live"' in web
  catalog = tmp_path / "catalog.json"
  catalog.write_text('{"models": []}')
  mobius = _room(tmp_path, "viewer").codex_overrides(
    [
      'model_provider="mobius_trial"',
      'model_providers.mobius_trial.base_url="http://127.0.0.1:8765/v1"',
      'model_providers.mobius_trial.env_key="MOBIUS_LOCAL_BROKER_KEY"',
      f'model_catalog_json="{catalog}"',
      'web_search="live"',
    ]
  )
  assert not any(
    "8765" in o or "MOBIUS_LOCAL_BROKER_KEY" in o or str(catalog) in o for o in mobius
  )
  assert 'model_providers.mobius_trial.env_key="AGENT_TOKEN"' in mobius
  assert mobius[-1] == 'web_search="disabled"'
  assert (
    tmp_path / "scratch" / "room" / ".codex" / "catalog.json"
  ).read_text() == '{"models": []}'


def test_the_confined_prompt_states_the_rooms_reach(tmp_path):
  room = _room(tmp_path, "viewer")
  prompt = agent_roles.system_prompt(
    room.role, app_id=7, app_name="Notes", room=room, app_prompt="Be brief."
  )
  assert (
    "Viewer role" in prompt
    and "no internet access" in prompt
    and prompt.endswith("Be brief.")
  )


def _room_available():
  return subprocess.run(
    [sys.executable, str(FENCE), "--check"], capture_output=True, text=True
  ).stdout


@pytest.mark.skipif(
  '"available": true' not in _room_available(), reason="host cannot build rooms"
)
def test_the_room_refuses_everything_outside_its_walls_and_door(tmp_path):
  home, data, outside, code = (
    tmp_path / name for name in ("home", "data", "outside", "code")
  )
  for folder in (home, data, outside, code):
    folder.mkdir()
  (outside / "secret.txt").write_text("FAKE-SECRET")
  door, other = (
    socket.create_server(("127.0.0.1", 0)),
    socket.create_server(("127.0.0.1", 0)),
  )
  probe = code / "probe.py"
  probe.write_text(
    "import os, socket, sys\n"
    "def attempt(fn):\n"
    "  try:\n    fn(); return 'ok'\n  except OSError as exc:\n    return type(exc).__name__\n"
    "d, o, p, q = sys.argv[1:]\n"
    "print(attempt(lambda: open(d + '/mine.txt', 'w').write('x')),\n"
    "      attempt(lambda: os.symlink(o + '/secret.txt', d + '/link')),\n"
    "      attempt(lambda: open(o + '/secret.txt').read()),\n"
    "      attempt(lambda: socket.create_connection(('127.0.0.1', int(p)), 2).close()),\n"
    "      attempt(lambda: socket.create_connection(('127.0.0.1', int(q)), 2).close()),\n"
    "      attempt(lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)),\n"
    "      os.environ.get('LEAKED', 'absent'))\n"
  )
  result = subprocess.run(
    [
      sys.executable,
      str(FENCE),
      "--home",
      str(home),
      "--rw",
      str(data),
      "--ro",
      str(code),
      "--door",
      f"127.0.0.1:{door.getsockname()[1]}",
      "--",
      sys.executable,
      "-I",
      str(probe),
      str(data),
      str(outside),
      str(door.getsockname()[1]),
      str(other.getsockname()[1]),
    ],
    capture_output=True,
    text=True,
    timeout=60,
    env={"PATH": "/usr/bin:/bin", "LEAKED": "yes"},
  )
  door.close()
  other.close()
  assert result.returncode == 0, result.stderr
  assert result.stdout.split() == [
    "ok",
    "PermissionError",
    "PermissionError",
    "ok",
    "PermissionError",
    "PermissionError",
    "absent",
  ]


# --- control tools -------------------------------------------------------------


def _control_module():
  path = Path(__file__).resolve().parents[1] / "scripts" / "mobius_control_mcp.py"
  spec = importlib.util.spec_from_file_location("mobius_control_roles_test", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def test_a_confined_agent_is_offered_only_its_declared_tools(monkeypatch):
  control = _control_module()
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run-1")
  monkeypatch.setenv("MOBIUS_AGENT_TOOLS", "open_page,request_restart_not_a_tool")
  monkeypatch.setattr(
    control, "_agent_api_call", lambda *a, **k: pytest.fail("no app-tool listing")
  )
  assert [tool["name"] for tool in control._tools_list_result()["tools"]] == [
    "open_page"
  ]
  refused = control._call_tool(
    {"name": "notify_owner", "arguments": {"title": "t", "body": "b"}}
  )
  assert refused["isError"]


def test_open_page_returns_readable_text_with_links(monkeypatch):
  control = _control_module()
  parser = control._PageText()
  parser.feed(
    "<html><style>x{}</style><body><h1>Title</h1><p>See <a href='https://a.test/'>docs</a>"
    "</p><script>alert(1)</script></body></html>"
  )
  assert parser.text() == "Title\nSee docs <https://a.test/>"


def test_an_app_with_a_confined_role_cannot_pin_a_runtime_its_room_cannot_start(
  client, owner_token, db
):
  app = _local_app(db, "notes", _role("viewer"))
  app_token = client.post(
    "/api/auth/app-token",
    json={"app_id": app.id},
    headers={"Authorization": f"Bearer {owner_token}"},
  ).json()["token"]
  missing = client.post(
    "/api/app-chats",
    json={"provider": "app-404"},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert missing.status_code == 422


def test_codex_searches_and_opened_pages_are_recorded_per_app(monkeypatch):
  from app.routes import agent_gateway as gateway_routes

  events = []
  monkeypatch.setattr(
    gateway_routes.activity,
    "log_event",
    lambda ev, **fields: events.append((ev, fields)),
  )
  body = _body(
    commands={
      "search_query": [{"q": "linux kernel"}],
      "open": [{"ref_id": "https://kernel.org/"}],
    }
  )
  gateway_routes._record_search(_key("explorer", "web", provider="codex"), body)
  [(event, fields)] = events
  assert (
    event == "app_agent_web" and fields["app_id"] == 7 and fields["kind"] == "search"
  )
  assert (
    "linux kernel" in fields["commands"] and "https://kernel.org/" in fields["commands"]
  )
