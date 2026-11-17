"""App agent roles: the declaration, consent, the agent key's reach, the Agent
Gateway's filtering, and the room each confined agent runs in."""

import asyncio
import base64
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from app import (
  agent_gateway,
  agent_roles,
  app_consent,
  auth,
  chat_writer,
  models,
  schemas,
)
from app.agent_roles import AgentKey, AgentRole, Room
from app.app_capabilities import contract_from_manifest
from app.config import get_settings
from app.manifest_contract import ManifestContractError, validate_manifest_contract
from app.runner_registry import RunnerKind, registry

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
        "additional_tools": [
          {"type": "image_generation"},
          {"type": "function"},
        ],
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


_REMOTE = "https://elsewhere.test/FAKE-APP-DATA"


@pytest.mark.parametrize(
  "block",
  [
    {"type": "image", "source": {"type": "url", "url": _REMOTE}},
    {"type": "document", "source": {"type": "url", "url": _REMOTE}},
    {"type": "input_image", "image_url": _REMOTE},
    {"type": "input_image", "image_url": {"url": _REMOTE}},
    {"type": "input_file", "file_url": _REMOTE},
  ],
)
def test_the_provider_never_fetches_a_web_address_for_the_agent(block):
  nested = {"type": "tool_result", "content": [block]}
  for key in (_key("viewer"), _key("viewer", "web")):
    with pytest.raises(agent_gateway.GatewayError) as refused:
      agent_gateway.prepare_body(key, _body(messages=[{"content": [nested]}]))
    assert refused.value.status == 403


def test_inline_content_still_reaches_the_model():
  inline = [
    {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
    {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    {"type": "tool_use", "input": {"url": _REMOTE, "image_url": _REMOTE}},
  ]
  body, _ = agent_gateway.prepare_body(_key("viewer"), _body(input=inline))
  assert json.loads(body)["input"] == inline


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
    chat_dir=tmp_path / "chat",
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
  assert room.cwd == (Path(data) if flag else room.home)
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
  viewer = _room(tmp_path, "viewer")
  mobius = viewer.codex_overrides(
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
  assert (viewer.config / "catalog.json").read_text() == '{"models": []}'


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


# TCP Fast Open connects inside a send call, which neither Landlock's connect
# rule nor the connect() guard sees; every send call that can carry it must be
# refused. Then every socket other than TCP over IPv4/IPv6 and route netlink.
_FAST_OPEN_PROBE = (
  "import ctypes\n"
  "FO, libc = 0x20000000, ctypes.CDLL(None, use_errno=True)\n"
  "class Iov(ctypes.Structure):\n"
  "  _fields_ = [('base', ctypes.c_char_p), ('len', ctypes.c_size_t)]\n"
  "class Msg(ctypes.Structure):\n"
  "  _fields_ = [('name', ctypes.c_void_p), ('namelen', ctypes.c_uint),\n"
  "    ('iov', ctypes.POINTER(Iov)), ('iovlen', ctypes.c_size_t),\n"
  "    ('control', ctypes.c_void_p), ('controllen', ctypes.c_size_t),\n"
  "    ('flags', ctypes.c_int), ('sent', ctypes.c_uint)]\n"
  "def mmsg():\n"
  "  s = socket.socket()\n"
  "  addr = ctypes.create_string_buffer(\n"
  "    b'\\x02\\x00' + int(q).to_bytes(2, 'big') + bytes([127, 0, 0, 1]) + bytes(8))\n"
  "  iov = Iov(b'FAKE-APP-DATA', 13)\n"
  "  msg = Msg(ctypes.addressof(addr), 16, ctypes.pointer(iov), 1, None, 0, 0, 0)\n"
  "  if libc.sendmmsg(s.fileno(), ctypes.byref(msg), 1, FO) < 0:\n"
  "    raise OSError(ctypes.get_errno(), 'sendmmsg')\n"
  "print(attempt(lambda: socket.socket().sendto(b'x', FO, ('127.0.0.1', int(q)))),\n"
  "      attempt(lambda: socket.socket().sendmsg([b'x'], [], FO, ('127.0.0.1', int(q)))),\n"
  "      attempt(mmsg),\n"
  "      attempt(lambda: socket.socket(socket.AF_UNIX)),\n"
  "      attempt(lambda: socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 4)),\n"
  "      attempt(lambda: socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM)),\n"
  "      attempt(lambda: socket.socket(socket.AF_INET, socket.SOCK_SEQPACKET)),\n"
  "      attempt(lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM, 132)),\n"
  "      attempt(lambda: socket.socket(socket.AF_NETLINK, socket.SOCK_RAW).close()))\n"
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
    "      attempt(lambda: os.listdir('/opt')),\n"
    "      os.environ.get('LEAKED', 'absent'))\n" + _FAST_OPEN_PROBE
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
  other.setblocking(False)
  with pytest.raises(BlockingIOError):  # nothing reached the closed address
    other.accept()
  door.close()
  other.close()
  assert result.returncode == 0, result.stderr
  first, second = result.stdout.splitlines()
  assert first.split() == [
    "ok",
    "PermissionError",
    "PermissionError",
    "ok",
    "PermissionError",
    "PermissionError",
    "PermissionError",
    "absent",
  ]
  assert second.split() == ["PermissionError"] * 8 + ["ok"], second


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
  key = AgentKey(7, "explorer", frozenset({"web"}), "codex", "chat-1")
  gateway_routes._record_search(key, body)
  [(event, fields)] = events
  assert event == "app_agent_web" and fields["kind"] == "search"
  assert (fields["app_id"], fields["chat_id"]) == (7, "chat-1")
  assert (
    "linux kernel" in fields["commands"] and "https://kernel.org/" in fields["commands"]
  )


# --- review findings: each is a boundary a confined agent must not cross -------


def _fence(*args, env=None):
  return subprocess.run(
    [sys.executable, "-I", str(FENCE), *args],
    capture_output=True,
    text=True,
    timeout=30,
    env=env,
  )


@pytest.mark.skipif(
  '"available": true' not in _room_available(), reason="host cannot build rooms"
)
def test_moebius_never_writes_where_the_agent_may_have_planted_a_link(tmp_path):
  room = _room(tmp_path, "viewer")
  victim = tmp_path / "outside.txt"
  victim.write_text("FAKE-SECRET")
  plant = "import os, sys; [os.symlink(sys.argv[1], p) for p in sys.argv[2:]]"
  planted = _fence(
    "--home",
    str(room.home),
    "--",
    sys.executable,
    "-I",
    "-c",
    plant,
    str(victim),
    str(room.home / ".codex" / "catalog.json"),
    str(room.home / "catalog.json"),
  )
  assert planted.returncode == 0, planted.stderr
  catalog = tmp_path / "catalog.json"
  catalog.write_text('{"models": []}')
  room.codex_overrides(
    ['model_provider="mobius_trial"', f'model_catalog_json="{catalog}"']
  )
  assert victim.read_text() == "FAKE-SECRET"
  assert room.home not in room.config.parents and room.config != room.home


def test_a_narrower_role_never_finds_what_a_wider_one_left(tmp_path):
  viewer = _room(tmp_path, "viewer", "web")
  (viewer.home / "cache.txt").write_text("FAKE-SECRET")
  explorer = _room(tmp_path, "explorer", "web")
  assert explorer.home != viewer.home and not (explorer.home / "cache.txt").exists()


def test_a_running_agents_key_follows_the_apps_current_role(client, owner_token, db):
  app = _local_app(db, "notes", _role("editor"))
  key, _run = _agent_key_for(db, app, role="editor")
  bearer = {"Authorization": f"Bearer {key}"}
  path = f"/api/storage/apps/{app.id}/notes.json"
  assert client.put(path, json={"n": 1}, headers=bearer).status_code == 204
  app.capability_contract = contract_from_manifest(
    _manifest(agent_role=_role("viewer"))
  )
  db.commit()
  assert client.put(path, json={"n": 2}, headers=bearer).status_code == 401
  assert client.get(path, headers=bearer).status_code == 401


def test_revoking_consent_stops_the_apps_running_agents(
  client, owner_token, db, monkeypatch
):
  app = _local_app(db, "notes", _role("editor"))
  app.manifest_url = "https://example.test/notes/mobius.json"
  app.accepted_consent = app_consent.platform_acceptance(app.capability_contract)
  db.commit()
  _key, run = _agent_key_for(db, app, role="editor")
  stopped = []

  async def stop(chat_id, db=None, **_kw):
    stopped.append(chat_id)
    return chat_module.ChatStopOutcome(True)

  from app import chat as chat_module
  from app.runner_registry import registry

  monkeypatch.setattr(chat_module, "_stop_chat_for_locked", stop)
  monkeypatch.setattr(registry, "all_alive_chat_ids", lambda: {run.chat_id, "other"})
  owner = {"Authorization": f"Bearer {owner_token}"}
  assert client.delete(f"/api/apps/{app.id}/consent", headers=owner).status_code == 200
  assert stopped == [run.chat_id]


def test_dropping_an_accepted_role_asks_for_full_access(db):
  app = _local_app(db, "notes", _role("viewer"))
  app.manifest_url = "https://example.test/notes/mobius.json"
  app.accepted_consent = app_consent.platform_acceptance(app.capability_contract)
  assert app_consent.status(app) == "accepted"
  app.capability_contract = contract_from_manifest(_manifest())
  assert [row["id"] for row in app_consent.needed_rows(app)] == ["agent_start"]
  assert app_consent.accept(
    app, app_consent.request_digest(app_consent.current_rows(app))
  )
  assert app_consent.status(app) == "accepted"


def test_owner_to_a_confined_role_is_a_narrowing():
  assert _asks_again(_role("owner"), _role("viewer", "web")) == []
  assert _asks_again(None, _role("viewer")) == ["agent_role"]


def test_an_update_shows_only_the_reach_it_adds(db):
  app = _local_app(db, "notes", _role("viewer", "notify"))
  app.manifest_url = "https://example.test/notes/mobius.json"
  app.accepted_consent = app_consent.platform_acceptance(app.capability_contract)
  app.capability_contract = contract_from_manifest(
    _manifest(agent_role=_role("editor", "notify", "web")),
  )
  [row] = app_consent.build_request(db, app)["rows"]
  assert [tool["id"] for tool in row["tools"]] == ["web"]
  assert row["note"] == "Previously Viewer."


@pytest.mark.parametrize(
  "tool",
  [
    {"type": "shell", "environment": {"type": "container_auto"}},
    {"type": "shell"},
    {"type": "function", "name": "run", "container": {"type": "auto"}},
  ],
)
def test_tools_the_provider_would_run_in_its_own_container_are_dropped(tool):
  body, removed = agent_gateway.prepare_body(_key("editor", "web"), _body(tools=[tool]))
  assert "tools" not in json.loads(body) and removed
  local = {"type": "shell", "environment": {"type": "local"}}
  kept, _ = agent_gateway.prepare_body(_key("editor"), _body(tools=[local]))
  assert json.loads(kept)["tools"] == [local]


def test_forcing_a_removed_hosted_tool_by_type_fails_openly():
  raw = _body(tools=[{"type": "web_search"}], tool_choice={"type": "web_search"})
  with pytest.raises(agent_gateway.GatewayError) as refused:
    agent_gateway.prepare_body(_key("viewer"), raw)
  assert refused.value.status == 403
  allowed, _ = agent_gateway.prepare_body(_key("viewer", "web"), raw)
  assert json.loads(allowed)["tool_choice"] == {"type": "web_search"}


@pytest.mark.parametrize(
  "declaration",
  [
    {"role": [], "why": "x"},
    {"role": "viewer", "tools": [{}], "why": "x"},
  ],
)
def test_a_wrongly_typed_agent_role_is_a_manifest_error(declaration):
  with pytest.raises(ManifestContractError):
    validate_manifest_contract(_manifest(agent_role=declaration))


def test_a_room_needs_signal_scoping(monkeypatch):
  spec = importlib.util.spec_from_file_location("fence_under_test", FENCE)
  fence = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(fence)
  monkeypatch.setattr(fence, "landlock_abi", lambda: 5)
  assert fence.availability()["available"] is False


def test_pages_open_only_through_moebius_where_each_is_recorded():
  hosted_fetch = {"type": "web_fetch_20250910", "name": "web_fetch"}
  search = {"type": "web_search_20250305", "name": "web_search"}
  body, removed = agent_gateway.prepare_body(
    _key("explorer", "web"), _body(tools=[hosted_fetch, search])
  )
  assert json.loads(body)["tools"] == [search] and removed == ["web_fetch_20250910"]


def test_a_grants_session_home_is_durable_and_its_own(tmp_path):
  viewer, editor = _room(tmp_path, "viewer"), _room(tmp_path, "editor")
  assert viewer.home.parent == editor.home.parent == tmp_path / "chat" / "agent-rooms"
  assert viewer.home != editor.home
  assert "--tmp" in viewer.fence_args() and viewer.tmp.parent == tmp_path / "scratch"


def test_revoking_reports_agents_that_did_not_stop(
  client, owner_token, db, monkeypatch
):
  from app import chat as chat_module
  from app.runner_registry import registry

  app = _local_app(db, "notes", _role("editor"))
  app.manifest_url = "https://example.test/notes/mobius.json"
  app.accepted_consent = app_consent.platform_acceptance(app.capability_contract)
  db.commit()
  _key, run = _agent_key_for(db, app, role="editor")

  async def unable_to_stop(chat_id, db=None, **_kw):
    return chat_module.ChatStopOutcome(False)

  monkeypatch.setattr(chat_module, "_stop_chat_for_locked", unable_to_stop)
  monkeypatch.setattr(registry, "all_alive_chat_ids", lambda: {run.chat_id})
  owner = {"Authorization": f"Bearer {owner_token}"}
  refused = client.delete(f"/api/apps/{app.id}/consent", headers=owner)
  assert refused.status_code == 503
  db.refresh(app)
  assert app_consent.status(app) == "required"


@pytest.mark.asyncio
async def test_only_runs_whose_grant_changed_are_stopped(owner_token, db, monkeypatch):
  from app import chat as chat_module
  from app.runner_registry import registry

  app = _local_app(db, "notes", _role("viewer"))
  keep, _ = _agent_key_for(db, app, role="viewer")
  chats = [c.id for c in db.query(models.Chat).filter_by(created_by_app_id=app.id)]
  agent_roles.record_run_grant(chats[0], agent_roles.effective_grant(app))
  stopped = []

  async def stop(chat_id, db=None, **_kw):
    stopped.append(chat_id)
    return chat_module.ChatStopOutcome(True)

  monkeypatch.setattr(chat_module, "_stop_chat_for_locked", stop)
  monkeypatch.setattr(registry, "all_alive_chat_ids", lambda: set(chats))
  assert await agent_roles.stop_runs_outside_grant(db, app.id) and stopped == []
  app.capability_contract = contract_from_manifest(
    _manifest(agent_role=_role("explorer"))
  )
  db.commit()
  assert await agent_roles.stop_runs_outside_grant(db, app.id) and stopped == chats


# Lifecycle scenarios from the independent review, with real processes.


@contextmanager
def running_writer(chat_id, tmp_path, storage_dir=None):
  home = tmp_path / "home"
  home.mkdir()
  data = storage_dir or tmp_path / "data"
  data.mkdir(parents=True, exist_ok=True)
  after = data / "after-change.txt"
  code = 'import sys,pathlib; print("ready",flush=True); sys.stdin.readline(); pathlib.Path(sys.argv[1]).write_text("write-after-change")'
  proc = subprocess.Popen(
    [
      sys.executable,
      "-I",
      str(agent_roles.FENCE_SCRIPT),
      "--home",
      str(home),
      "--rw",
      str(data),
      "--",
      sys.executable,
      "-I",
      "-c",
      code,
      str(after),
    ],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    start_new_session=True,
  )

  class Writer:
    kind = RunnerKind.CODEX_SDK

    async def stop(self, timeout=2):
      if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        await asyncio.to_thread(proc.wait, timeout=timeout)
      return True

    async def force_stop(self, timeout=5):
      return await self.stop(timeout)

  handle = Writer()
  handle.chat_id = chat_id
  registry.register(handle)
  try:
    assert proc.stdout.readline().strip() == "ready"
    yield proc, after
  finally:
    registry.unregister(chat_id, RunnerKind.CODEX_SDK)
    if proc.poll() is None:
      os.killpg(proc.pid, signal.SIGTERM)
      proc.communicate(timeout=5)


def test_applying_a_narrower_role_stops_a_running_agent(
  client, owner_token, db, tmp_path
):
  from tests.test_app_apply import _source, _apply

  source = _source("review-narrow-running")
  manifest = json.loads((source / "mobius.json").read_text())
  manifest["agent_role"] = _role("editor")
  (source / "mobius.json").write_text(json.dumps(manifest))
  owner = {"Authorization": f"Bearer {owner_token}"}
  initial = _apply(client, owner, source)
  assert initial.status_code == 200, initial.text
  app = db.get(models.App, int(initial.json()["app"]["id"]))
  _key, run = _agent_key_for(db, app, role="editor")
  from app.config import get_settings

  storage_dir = Path(get_settings().data_dir) / "apps" / str(app.id)
  with running_writer(run.chat_id, tmp_path, storage_dir) as (proc, after):
    manifest["agent_role"] = _role("viewer")
    (source / "mobius.json").write_text(json.dumps(manifest))
    changed = _apply(client, owner, source)
    assert changed.status_code == 200, changed.text
    if proc.poll() is None:
      proc.communicate("continue\n", timeout=10)
    assert not after.exists(), "Editor process kept writing after the app became Viewer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
  "provider_id,provider_name", [("claude", "Claude Code"), ("codex", "Codex")]
)
@pytest.mark.parametrize("role", ["viewer", "editor", "explorer"])
async def test_a_confined_agent_never_inherits_history_from_another_grant(
  db, owner_token, monkeypatch, provider_id, provider_name, role
):
  from app import chat as chat_module, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_writer import StartTurn, get_writer
  from tests.test_retired_provider_sessions import _Provider

  app = _local_app(db, "review-history", _role("owner"))
  chat_id = f"review-history-{provider_id}-{role}"
  marker = "FAKE-SECRET-FROM-UNRELATED-APP"
  chat = chat_writer.create_chat(
    id=chat_id,
    title="former owner chat",
    created_by_app_id=app.id,
    provider=provider_id,
    session_id="old-owner-session",
    messages=[
      {"role": "user", "content": "Read another app.", "ts": 1},
      {"role": "assistant", "content": marker, "ts": 2},
    ],
  )
  db.add(chat)
  db.commit()
  app.capability_contract = contract_from_manifest(_manifest(agent_role=_role(role)))
  db.commit()
  calls = []

  async def fake_runner(**kwargs):
    calls.append(kwargs)
    return {
      "session_id": "fresh-restricted-session",
      "cost_usd": 0.0,
      "error": None,
    }

  async def fake_complete(**kwargs):
    kwargs["db"].close()
    return chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED

  async def fake_metrics(**kwargs):
    return None

  monkeypatch.setattr(
    chat_module, "registered_provider", lambda _: _Provider(provider_name)
  )
  monkeypatch.setattr(chat_module, "_complete_turn", fake_complete)
  monkeypatch.setattr(chat_module, "_record_run_metrics", fake_metrics)
  if provider_id == "claude":
    from app import claude_sdk_runner

    monkeypatch.setattr(claude_sdk_runner, "run_claude_sdk_turn", fake_runner)
    monkeypatch.setattr(claude_sdk_runner, "_resumable", lambda *a, **kw: False)
  else:
    from app import codex_sdk_runner

    monkeypatch.setattr(codex_sdk_runner, "run_codex_sdk_turn", fake_runner)
  run_id = f"run-{chat_id}"
  get_writer().submit(
    StartTurn(
      chat_id=chat_id,
      run_token=run_id,
      user_msg={
        "role": "user",
        "content": "Continue safely.",
        "ts": 3,
        "cid": f"m-{chat_id}",
      },
      title_source="Continue safely.",
      default_provider=provider_id,
    )
  ).result(timeout=5)
  create_broadcast(chat_id)
  try:
    await chat_module._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="Continue safely.")],
      chat_id=chat_id,
      session_id="old-owner-session",
      provider_id=provider_id,
      run_token=run_id,
    )
  finally:
    remove_broadcast(chat_id)
  assert len(calls) == 1
  prompt = str(calls[0].get("user_message", "")) + str(
    calls[0].get("resumed_context", "")
  )
  assert marker not in prompt


# --- more tools: ask and other apps' tools -------------------------------------


def test_ask_reaches_only_the_runs_own_chat_cards():
  key = AgentKey(7, "viewer", frozenset({"ask"}), "claude", "chat-1", "run-1")
  assert key.may_call("POST", "/api/chats/chat-1/question")
  assert key.may_call("POST", "/api/chats/chat-1/approval")
  assert key.may_call("POST", "/api/chats/chat-1/turn-end")
  assert not key.may_call("POST", "/api/chats/chat-2/question")
  assert not key.may_call("POST", "/api/chats/chat-1/restart-request")
  assert not AgentKey(7, "viewer", frozenset(), "claude", "chat-1").may_call(
    "POST", "/api/chats/chat-1/question"
  )


def test_a_confined_key_is_an_agent_run_of_its_own_chat(client, owner_token, db):
  from app.deps import get_agent_run_principal

  app = _local_app(db, "notes", _role("viewer", "ask"))
  key, run = _agent_key_for(db, app, role="viewer", tools=("ask",))
  principal = get_agent_run_principal(key, db)
  assert (principal.app_id, principal.chat_id, principal.run_id) == (
    app.id,
    run.chat_id,
    run.id,
  )


def test_ask_makes_the_room_end_its_turn_on_cards(tmp_path):
  room = _room(tmp_path, "editor", "ask")
  assert room.asks_owner and room.control_tools == (
    "request_question",
    "request_approval",
  )
  assert "MOBIUS_RUN_TOKEN" in agent_roles.ROOM_ENV
  assert "request_approval" in agent_roles.system_prompt(
    room.role, app_id=7, app_name="Notes", room=room, app_prompt=None
  )


@pytest.mark.parametrize(
  "name, ok",
  [
    ("memory.search", True),
    ("web-studio.build_site", True),
    ("Memory.search", False),
    ("memory.", False),
    (".search", False),
    ("memory search", False),
  ],
)
def test_another_apps_tool_is_declared_as_app_dot_tool(name, ok):
  manifest = _manifest(agent_role=_role("explorer", name))
  if ok:
    validate_manifest_contract(manifest)
  else:
    with pytest.raises(ManifestContractError):
      validate_manifest_contract(manifest)


def test_another_apps_tool_counts_as_private_data_beside_web():
  row = _agent_row(_role("explorer", "memory.search", "web"))
  tools = {tool["id"]: tool for tool in row["tools"]}
  assert tools["web"]["warning"] is True and tools["memory.search"]["warning"] is False
  assert "search tool of your memory app" in tools["memory.search"]["text"]
  assert "tool:memory.search" in row["scope"]


def test_another_apps_tool_routes_open_only_when_declared():
  declared = AgentKey(7, "explorer", frozenset({"memory.search"}), "claude", "c", "r")
  assert declared.app_tools == {("memory", "search")}
  assert declared.may_call("POST", "/api/agent/app-tools/call")
  assert declared.may_call("GET", "/api/agent/app-tools/")
  assert not _key("explorer", "web").may_call("POST", "/api/agent/app-tools/call")


def test_the_app_tools_routes_serve_a_confined_agent_only_its_declared_tools(
  client, owner_token, db, monkeypatch
):
  from app import app_tools

  # foo-bar.baz and foo.bar_baz share the agent-facing name foo_bar_baz; only
  # the declared identity is served.
  tools = [
    app_tools.AppTool(
      app_id=i,
      exposed_name=exposed,
      name=name,
      description="d",
      input_schema={"type": "object"},
      app_slug=slug,
    )
    for i, (slug, name, exposed) in enumerate(
      [
        ("memory", "search", "memory_search"),
        ("notes", "delete", "notes_delete"),
        ("foo-bar", "baz", "foo_bar_baz"),
      ]
    )
  ]
  monkeypatch.setattr(app_tools, "live_app_tools", lambda db: tools)
  app = _local_app(db, "reader", _role("explorer", "memory.search", "foo.bar_baz"))
  key, _run = _agent_key_for(
    db, app, role="explorer", tools=("memory.search", "foo.bar_baz")
  )
  bearer = {"Authorization": f"Bearer {key}"}
  listed = client.get("/api/agent/app-tools/", headers=bearer)
  assert [tool["name"] for tool in listed.json()["tools"]] == ["memory_search"]
  for name in ("notes_delete", "foo_bar_baz"):
    refused = client.post(
      "/api/agent/app-tools/call", json={"name": name}, headers=bearer
    )
    assert refused.status_code == 403


def test_a_confined_agent_is_offered_only_its_declared_app_tools(monkeypatch):
  control = _control_module()
  monkeypatch.setenv("MOBIUS_AGENT_TOOLS", "")
  monkeypatch.setenv("MOBIUS_AGENT_APP_TOOLS", "memory_search")
  listing = {
    "tools": [
      {"name": n, "description": "d", "inputSchema": {"type": "object"}}
      for n in ("memory_search", "notes_delete")
    ]
  }
  monkeypatch.setattr(control, "_agent_api_call", lambda *a, **k: listing)
  assert [tool["name"] for tool in control._tools_list_result()["tools"]] == [
    "memory_search"
  ]
  room = AgentRole("explorer", ("memory.search",), "why")
  assert room.app_tools == ("memory_search",) and room.reads_private_data


@pytest.mark.asyncio
async def test_a_replacement_run_admitted_during_the_sweep_keeps_running(
  owner_token, db, monkeypatch
):
  from app import chat as chat_module
  from app import chat_queue

  app = _local_app(db, "notes", _role("editor"))
  _agent_key_for(db, app, role="editor")
  [chat_id] = [c.id for c in db.query(models.Chat).filter_by(created_by_app_id=app.id)]
  agent_roles.record_run_grant(chat_id, "editor@stale")
  stopped = []

  async def stop(chat_id, db=None, **_kw):
    stopped.append(chat_id)
    return chat_module.ChatStopOutcome(True)

  monkeypatch.setattr(chat_module, "_stop_chat_for_locked", stop)
  monkeypatch.setattr(registry, "all_alive_chat_ids", lambda: {chat_id})
  async with chat_queue.get_transition_lock(chat_id):
    sweep = asyncio.create_task(agent_roles.stop_runs_outside_grant(db, app.id))
    await asyncio.sleep(0.05)
    agent_roles.record_run_grant(chat_id, agent_roles.effective_grant(app))
  assert await sweep and stopped == []


# --- helpers ------------------------------------------------------------------


def test_helpers_reach_only_the_helper_routes_when_declared():
  key = AgentKey(7, "viewer", frozenset({"helpers"}), "claude", "chat-1", "run-1")
  for method, path in [
    ("POST", "/api/delegations"),
    ("GET", "/api/delegations"),
    ("GET", "/api/delegations/capabilities"),
    ("GET", "/api/delegations/d1"),
    ("POST", "/api/delegations/d1/messages"),
    ("POST", "/api/delegations/d1/cancel"),
    ("POST", "/api/delegations/d1/result-read"),
  ]:
    assert key.may_call(method, path), path
  assert not key.may_call("POST", "/api/delegations/d1/retry")
  assert not _key("viewer").may_call("POST", "/api/delegations")


def test_a_confined_agent_starts_helpers_only_under_its_own_chat_on_a_room_runtime(
  client, owner_token, db
):
  app = _local_app(db, "notes", _role("viewer", "helpers"))
  key, run = _agent_key_for(db, app, role="viewer", tools=("helpers",))
  bearer = {"Authorization": f"Bearer {key}"}
  body = {
    "app_id": None,
    "parent_chat_id": "someone-elses-chat",
    "task_key": "t",
    "prompt": "Look.",
    "provider": "claude",
  }
  assert client.post("/api/delegations", json=body, headers=bearer).status_code == 403
  body |= {"parent_chat_id": run.chat_id, "provider": "app-404"}
  assert client.post("/api/delegations", json=body, headers=bearer).status_code == 422


def test_a_confined_agent_with_helpers_starts_one_under_its_own_chat(
  client, owner_token, db, monkeypatch
):
  from app.chat_writer import StartTurn, get_writer

  starts = []

  async def fake_start(**kwargs):
    starts.append(kwargs)
    return True

  monkeypatch.setattr("app.routes.delegations.start_programmatic_chat_turn", fake_start)
  app = _local_app(db, "notes", _role("viewer", "helpers"))
  key, run = _agent_key_for(db, app, role="viewer", tools=("helpers",))
  chat_id, run_id = run.chat_id, run.id
  db.delete(run)  # the writer's own turn takes the run's place
  db.commit()
  get_writer().submit(
    StartTurn(
      chat_id=chat_id,
      run_token=run_id,
      user_msg={"role": "user", "content": "Look.", "ts": 1},
    )
  ).result(timeout=5)
  body = {
    "parent_chat_id": chat_id,
    "task_key": "look",
    "prompt": "Look.",
    "provider": "claude",
  }
  bearer = {"Authorization": f"Bearer {key}"}
  created = client.post("/api/delegations", json=body, headers=bearer)
  assert created.status_code == 201, created.text
  row = db.query(models.Delegation).filter_by(parent_chat_id=chat_id).one()
  child = db.get(models.Chat, row.child_chat_id)
  assert (row.app_id, child.created_by_app_id, len(starts)) == (app.id, app.id, 1)
  owner = db.query(models.Owner).first()
  ordinary = auth.create_app_token(
    app.id,
    owner.username,
    owner.token_epoch,
    app_nonce=app.token_nonce,
    consent=app_consent.session_id(app),
  )
  refused = client.post(
    "/api/delegations",
    json=body | {"app_id": app.id, "task_key": "other"},
    headers={"Authorization": f"Bearer {ordinary}"},
  )
  assert refused.status_code in (401, 403), refused.text


def _helper_of(db, app, parent_chat_id, provider="claude"):
  import hashlib

  child = chat_writer.create_chat(
    id=str(uuid.uuid4()),
    title="Helper",
    messages=[],
    created_by_app_id=app.id,
    provider=provider,
  )
  db.add(child)
  db.add(
    models.Delegation(
      id=str(uuid.uuid4()),
      app_id=app.id,
      parent_chat_id=parent_chat_id,
      parent_root_run_id="root",
      task_key=str(uuid.uuid4()),
      child_chat_id=child.id,
      provider=provider,
      model=None,
      effort=None,
      scope="write",
      cwd="/data",
      prompt_sha256=hashlib.sha256(b"Look.").hexdigest(),
    )
  )
  db.commit()
  return child


def test_a_confined_agent_sees_only_its_own_chats_helpers(client, owner_token, db):
  app = _local_app(db, "notes", _role("viewer", "helpers"))
  key, run = _agent_key_for(db, app, role="viewer", tools=("helpers",))
  mine = _helper_of(db, app, run.chat_id)
  _helper_of(db, app, "another-chat-of-the-same-app")
  listed = client.get("/api/delegations", headers={"Authorization": f"Bearer {key}"})
  assert [item["child_chat_id"] for item in listed.json()["items"]] == [mine.id]


@pytest.mark.asyncio
async def test_a_helper_of_a_confined_app_runs_in_a_room_with_the_same_role(
  db, owner_token, monkeypatch
):
  from app import chat as chat_module, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_writer import StartTurn, get_writer
  from app.claude_sdk_runner import run_claude_sdk_turn  # noqa: F401
  from tests.test_retired_provider_sessions import _Provider

  app = _local_app(db, "notes", _role("viewer", "helpers"))
  _key, parent_run = _agent_key_for(db, app, role="viewer", tools=("helpers",))
  child = _helper_of(db, app, parent_run.chat_id)
  calls = []

  async def fake_runner(**kwargs):
    calls.append(kwargs)
    return {"session_id": "helper-session", "cost_usd": 0.0, "error": None}

  async def fake_complete(**kwargs):
    kwargs["db"].close()
    return chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED

  async def fake_metrics(**kwargs):
    return None

  from app import claude_sdk_runner

  monkeypatch.setattr(
    chat_module, "registered_provider", lambda _: _Provider("Claude Code")
  )
  monkeypatch.setattr(chat_module, "_complete_turn", fake_complete)
  monkeypatch.setattr(chat_module, "_record_run_metrics", fake_metrics)
  monkeypatch.setattr(claude_sdk_runner, "run_claude_sdk_turn", fake_runner)
  run_id = f"run-{child.id}"
  get_writer().submit(
    StartTurn(
      chat_id=child.id,
      run_token=run_id,
      user_msg={
        "role": "user",
        "content": "Look.",
        "ts": 1,
        "cid": f"m-{child.id}",
      },
      title_source="Look.",
      default_provider="claude",
    )
  ).result(timeout=5)
  create_broadcast(child.id)
  try:
    await chat_module._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="Look.")],
      chat_id=child.id,
      session_id=None,
      provider_id="claude",
      run_token=run_id,
    )
  finally:
    remove_broadcast(child.id)
  [call] = calls
  room = call["app_room"]
  assert room is not None and room.role.grant == "viewer+helpers"
  claims = auth.decode_access_token(call["base_env"]["AGENT_TOKEN"])
  assert claims["scope"] == "app" and claims["agent_role"] == "viewer"
  assert call["cwd"] == str(room.cwd) and "Viewer role" in call["skill_text"]


def test_screenshot_shows_only_the_apps_own_page_and_counts_as_private_data(
  client, owner_token, db, tmp_path, monkeypatch
):
  from app.routes import agent_gateway as gateway_routes

  fake = tmp_path / "capture.sh"
  fake.write_text('printf "%s" "$2" > "$3.route"; printf "\\x89PNG fake" > "$3"\n')
  monkeypatch.setattr(gateway_routes, "_SCREENSHOT_SCRIPT", fake)
  seen = []
  monkeypatch.setattr(
    gateway_routes, "_capture_env", lambda *a: seen.append(a[2]) or dict(os.environ)
  )
  app = _local_app(db, "notes", _role("viewer", "screenshot"))
  key, _run = _agent_key_for(db, app, role="viewer", tools=("screenshot",))
  shot = client.post(
    "/api/app-agent/screenshot",
    json={"width": 400, "height": 800, "pixelRatio": 3.75},
    headers={"Authorization": f"Bearer {key}"},
  )
  assert shot.status_code == 200, shot.text
  assert base64.b64decode(shot.json()["image"]).startswith(b"\x89PNG")
  [route] = list(Path(get_settings().data_dir).glob("chats/*/media/*.png.route"))
  assert route.read_text() == "/apps/notes/"
  plain, _ = _agent_key_for(db, app, role="viewer")
  refused = client.post(
    "/api/app-agent/screenshot",
    json={},
    headers={"Authorization": f"Bearer {plain}"},
  )
  assert refused.status_code in (401, 403)
  assert (
    _agent_row(_role("explorer", "screenshot", "web"))["tools"][1]["warning"] is True
  )
  assert seen[0].pixelRatio == 3.75


def test_a_confined_approval_claims_only_inside_its_own_app_and_chat(
  client, owner_token, db
):
  from app.agent_work_claims import claim_work
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_writer import StartTurn, get_writer
  from app.chat_event_sink import (
    ChatEventSink,
    register_active_sink,
    unregister_active_sink,
  )

  app = _local_app(db, "notes", _role("viewer", "ask"))
  key, run = _agent_key_for(db, app, role="viewer", tools=("ask",))
  chat_id, run_id = run.chat_id, run.id
  db.delete(run)  # the writer's own turn takes the run's place
  db.commit()
  get_writer().submit(
    StartTurn(
      chat_id=chat_id,
      run_token=run_id,
      user_msg={"role": "user", "content": "Check.", "ts": 1},
    )
  ).result(timeout=5)
  sink = ChatEventSink(create_broadcast(chat_id), chat_id, run_token=run_id)
  register_active_sink(chat_id, sink)
  merge = "github:mobius-os/mobius:pr:1:abc:merge"
  try:
    saved = client.post(
      f"/api/chats/{chat_id}/approval",
      json={
        "question": "Merge it?",
        "work_key": merge,
        "options": [
          {"label": "Yes", "description": "Merge."},
          {"label": "Not now", "description": "Leave it."},
        ],
      },
      headers={"Authorization": f"Bearer {key}"},
    )
  finally:
    unregister_active_sink(chat_id, sink)
    remove_broadcast(chat_id)
  assert saved.status_code == 200, saved.text
  assert saved.json()["state"] == "waiting_for_owner"
  owner = db.query(models.Owner).first()
  other = claim_work(
    db,
    owner_id=owner.id,
    chat_id="owner-chat",
    run_id="owner-run",
    work_key=merge,
    summary="Merge the PR",
  )
  assert other["owner_chat_id"] == "owner-chat"


@pytest.mark.parametrize(
  "before, after, widens",
  [
    (_role("editor", "web"), _role("viewer", "web"), False),
    (_role("owner"), _role("explorer", "notify"), False),
    (_role("viewer"), _role("viewer", why="Reworded reason."), False),
    (_role("viewer"), _role("explorer"), False),
    (_role("viewer"), _role("editor"), True),
    (_role("explorer"), _role("viewer"), True),
    (_role("viewer", "web"), _role("viewer", "notify"), True),
    (_role("editor"), _role("owner"), True),
    (_role("viewer"), None, True),
  ],
)
def test_update_review_ranks_agent_roles(before, after, widens):
  from app.app_capabilities import diff_contracts

  def contract(role):
    return contract_from_manifest(
      _manifest(embeds_agent=True, **({"agent_role": role} if role else {}))
    )

  assert diff_contracts(contract(before), contract(after))["widens"] is widens
