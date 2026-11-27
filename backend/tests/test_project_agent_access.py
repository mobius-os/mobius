"""Project agent access: a shared project's chats run locked down in the app
agent room (agent_roles); a private project's keep full access.

A locked-down chat runs every turn in an Edit room on the project's folder,
with the web and questions to the owner, and a key that is the owner's
run-bound bearer narrowed to the room's allow-list. Möbius's own Git there
never starts a program the repository's settings name (project_git)."""

import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from app import (
  agent_roles, auth as tokens, chat_writer, models, project_agent_access, schemas,
)
from app.agent_roles import AgentKey, AgentRole, Room
from app.project_agent_access import SHARED_ROLE

FENCE = Path(agent_roles.FENCE_SCRIPT)


def _project(client, auth_headers, name="Shared project"):
  response = client.post(
    "/api/projects", headers=auth_headers, json={"name": name, "template_id": "blank"},
  )
  assert response.status_code == 200, response.text
  return response.json()


def _share(client, auth_headers, project_id):
  """Invite someone and have them join; returns their member id."""
  invite = client.post(
    f"/api/projects/{project_id}/invites", headers=auth_headers,
    json={"invitee_name": "Sam", "role": "editor"},
  )
  assert invite.status_code == 200, invite.text
  secret = urlsplit(invite.json()["join_url"]).fragment
  joined = client.post(
    "/api/projects/invites/redeem", json={"invite": secret, "display_name": "Sam"},
  )
  assert joined.status_code == 200, joined.text
  return joined.json()["member_id"]


def _unshare(client, auth_headers, project_id, member_id):
  response = client.delete(f"/api/projects/{project_id}/members/{member_id}", headers=auth_headers)
  assert response.status_code == 204, response.text


def _chat_with_run(db, project_id, provider="claude"):
  chat = chat_writer.create_chat(
    id=str(uuid.uuid4()), title="Project chat", messages=[], project_id=project_id,
  )
  db.add(chat)
  run = models.ChatRun(
    id=str(uuid.uuid4()), root_run_id="r", chat_id=chat.id, status="running",
    provider=provider,
  )
  run.root_run_id = run.id
  db.add(run)
  db.commit()
  return chat, run


def _room_key(db, project_id, role, *tools, provider="claude"):
  owner = db.query(models.Owner).first()
  chat, run = _chat_with_run(db, project_id, provider)
  key = tokens.create_agent_token(
    chat.id, owner.username, owner.token_epoch, run_id=run.id,
    grant=AgentKey.claims_for(AgentRole(role, tools, ""), provider, project=project_id),
  )
  return key, chat, run


def test_a_private_project_keeps_full_access_and_a_shared_one_locks_down(client, auth, db):
  project = _project(client, auth)
  row = db.get(models.Project, project["id"])
  assert project_agent_access.role_for(db, row) is None
  assert project_agent_access.grant(db, row) == "project:admin"
  member = _share(client, auth, project["id"])
  assert project_agent_access.role_for(db, row) == SHARED_ROLE
  assert SHARED_ROLE.role == "edit" and set(SHARED_ROLE.tools) == {"web", "ask"}
  _unshare(client, auth, project["id"], member)
  assert project_agent_access.role_for(db, row) is None


def test_a_running_project_key_follows_the_projects_sharing(client, auth, db):
  project = _project(client, auth)
  member = _share(client, auth, project["id"])
  key, _chat, _run = _room_key(db, project["id"], "edit", "ask", "web", provider="codex")
  bearer = {"Authorization": f"Bearer {key}"}
  assert client.get("/api/agent-gateway/v1/models", headers=bearer).status_code == 200
  assert client.get("/api/apps/", headers=bearer).status_code == 403
  _unshare(client, auth, project["id"], member)
  assert client.get("/api/agent-gateway/v1/models", headers=bearer).status_code == 401
  # The owner's own bearer is still not a room key at the gateway.
  assert client.get("/api/agent-gateway/v1/models", headers=auth).status_code == 403


def test_a_project_key_stops_working_with_its_run(client, auth, db):
  project = _project(client, auth)
  _share(client, auth, project["id"])
  key, _chat, run = _room_key(db, project["id"], "edit", "ask", "web", provider="codex")
  run.status = "completed"
  db.commit()
  bearer = {"Authorization": f"Bearer {key}"}
  assert client.get("/api/agent-gateway/v1/models", headers=bearer).status_code == 401


def _pkey(role, *tools):
  return AgentKey(0, role, frozenset(tools), "claude", "c", "r", "p1")


@pytest.mark.parametrize(
  "key, method, path, allowed",
  [
    (_pkey("edit"), "POST", "/api/agent-gateway/v1/messages", True),
    # A project's files are its room's folder, never another route.
    (_pkey("edit"), "PUT", "/api/storage/apps/0/notes.json", False),
    (_pkey("edit"), "PUT", "/api/projects/p1/file", False),
    (_pkey("edit"), "PUT", "/api/projects/p1/agent-access", False),
    (_pkey("read"), "GET", "/api/proxy", False),
    (_pkey("read", "web"), "GET", "/api/proxy", True),
    (_pkey("read", "notify"), "POST", "/api/notifications/send", True),
    (_pkey("read", "ask"), "POST", "/api/chats/c/question", True),
    (_pkey("read", "ask"), "POST", "/api/chats/other/question", False),
    (_pkey("edit", "web", "notify", "ask"), "GET", "/api/apps/", False),
  ],
)
def test_a_project_key_reaches_only_its_allow_list(key, method, path, allowed):
  assert key.may_call(method, path) is allowed


def test_only_a_bearer_naming_its_project_is_a_room_key():
  plain = {"sub": "owner", "agent_chat": "c", "agent_run": "r", "agent_role": "read"}
  assert AgentKey.from_claims(plain) is None
  key = AgentKey.from_claims({**plain, "agent_project": "p1", "agent_tools": ["ask"]})
  assert key is not None and key.project == "p1" and key.app_id == 0
  assert key.subject == "project:p1" and key.tools == frozenset({"ask"})
  # An app's key never names a project, whatever its claims say.
  app_key = AgentKey.from_claims({**plain, "scope": "app", "app_id": 4, "agent_project": "p1"})
  assert app_key.project == "" and app_key.subject == "app:4"


def test_the_project_prompt_states_its_reach_and_carries_the_project_context(tmp_path):
  from app.agent_roles import Room
  role = AgentRole("read", ("ask",), "")
  room = Room.for_run(role, chat_dir=tmp_path / "chat", scratch=tmp_path / "s", storage_dir=tmp_path / "files")
  prompt = project_agent_access.system_prompt(
    role, project_name="Game", room=room, context="<project_context>x</project_context>", custom=None,
  )
  assert "Read access" in prompt and str(tmp_path / "files") in prompt
  assert "not change them" in prompt and "no internet access" in prompt
  assert "<project_context>" in prompt and "request_approval" in prompt
  empty = project_agent_access.system_prompt(
    AgentRole("tools", (), ""), project_name="Game",
    room=Room.for_run(AgentRole("tools", (), ""), chat_dir=tmp_path / "c2", scratch=tmp_path / "s2", storage_dir=tmp_path / "f2"),
    context="<project_context>secret</project_context>", custom=None,
  )
  assert "secret" not in empty and "empty folder" in empty


async def _run_turn(chat_id, monkeypatch):
  from app import chat as chat_module, chat_queue, claude_sdk_runner
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_writer import StartTurn, get_writer
  from tests.test_retired_provider_sessions import _Provider

  calls = []

  async def fake_runner(**kwargs):
    calls.append(kwargs)
    return {"session_id": "s", "cost_usd": 0.0, "error": None}

  async def fake_complete(**kwargs):
    kwargs["db"].close()
    return chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED

  async def fake_metrics(**kwargs):
    return None

  monkeypatch.setattr(chat_module, "registered_provider", lambda _: _Provider("Claude Code"))
  monkeypatch.setattr(chat_module, "_complete_turn", fake_complete)
  monkeypatch.setattr(chat_module, "_record_run_metrics", fake_metrics)
  monkeypatch.setattr(claude_sdk_runner, "run_claude_sdk_turn", fake_runner)
  run_id = f"run-{chat_id}"
  get_writer().submit(StartTurn(
    chat_id=chat_id, run_token=run_id,
    user_msg={"role": "user", "content": "Look.", "ts": 1, "cid": f"m-{chat_id}"},
    title_source="Look.", default_provider="claude",
  )).result(timeout=5)
  create_broadcast(chat_id)
  try:
    await chat_module._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content="Look.")],
      chat_id=chat_id, session_id=None, provider_id="claude", run_token=run_id,
    )
  finally:
    remove_broadcast(chat_id)
  [call] = calls
  return call


def _bare_chat(db, project_id):
  chat = chat_writer.create_chat(
    id=str(uuid.uuid4()), title="Project chat", messages=[], project_id=project_id,
  )
  db.add(chat)
  db.commit()
  return chat


def _bare_chat(db, project_id):
  chat = chat_writer.create_chat(
    id=str(uuid.uuid4()), title="Project chat", messages=[], project_id=project_id,
  )
  db.add(chat)
  db.commit()
  return chat


@pytest.mark.asyncio
async def test_a_shared_project_chat_runs_locked_down_on_the_project_folder(
  client, auth, db, monkeypatch,
):
  project = _project(client, auth, name="Pixel game")
  _share(client, auth, project["id"])
  chat = _bare_chat(db, project["id"])
  call = await _run_turn(chat.id, monkeypatch)
  room = call["app_room"]
  root = (Path(os.environ["DATA_DIR"]) / db.get(models.Project, project["id"]).root_path).resolve()
  assert room is not None and room.role == SHARED_ROLE
  assert room.data == root and call["cwd"] == str(root)
  assert any(a == "--rw" and b == str(root) for a, b in zip(room.fence_args(), room.fence_args()[1:]))
  claims = tokens.decode_access_token(call["base_env"]["AGENT_TOKEN"])
  assert claims.get("scope") is None and claims["agent_project"] == project["id"]
  assert claims["agent_role"] == "edit" and claims["agent_tools"] == ["ask", "web"]
  assert "Edit access" in call["skill_text"] and "Pixel game" in call["skill_text"]
  assert "cannot install packages" in call["skill_text"]


@pytest.mark.asyncio
async def test_a_private_project_chat_keeps_the_ordinary_runtime(
  client, auth, db, monkeypatch,
):
  project = _project(client, auth)
  chat = _bare_chat(db, project["id"])
  call = await _run_turn(chat.id, monkeypatch)
  assert call["app_room"] is None and "Edit access" not in call["skill_text"]


@pytest.mark.asyncio
async def test_sharing_a_project_stops_only_runs_under_the_other_access(
  client, auth, db, monkeypatch,
):
  from app import chat as chat_module
  from app.runner_registry import registry

  project = _project(client, auth)
  other = _project(client, auth, name="Other")
  row = db.get(models.Project, project["id"])
  chats = [_bare_chat(db, project["id"]).id, _bare_chat(db, other["id"]).id]
  monkeypatch.setattr(registry, "all_alive_chat_ids", lambda: set(chats))
  agent_roles.record_run_grant(chats[0], project_agent_access.grant(db, row))
  agent_roles.record_run_grant(
    chats[1], project_agent_access.grant(db, db.get(models.Project, other["id"])),
  )
  stopped = []

  async def stop(chat_id, db=None, **_kw):
    stopped.append(chat_id)
    return chat_module.ChatStopOutcome(True)

  monkeypatch.setattr(chat_module, "_stop_chat_for_locked", stop)
  assert await project_agent_access.stop_runs_outside_grant(db, project["id"]) and stopped == []
  # Joining stops it by itself (sharing_changed); a sweep after agrees.
  _share(client, auth, project["id"])
  assert stopped == [chats[0]]
  assert await project_agent_access.stop_runs_outside_grant(db, project["id"])
  assert set(stopped) == {chats[0]}


def test_joining_and_leaving_a_project_stops_its_running_chats(client, auth, monkeypatch):
  changed = []
  monkeypatch.setattr(project_agent_access, "sharing_changed", changed.append)
  project = _project(client, auth)
  member = _share(client, auth, project["id"])
  assert changed == [project["id"]]
  _unshare(client, auth, project["id"], member)
  assert changed == [project["id"], project["id"]]


def test_a_confined_project_approval_claims_only_inside_its_project_and_chat():
  key = _pkey("read", "ask")
  assert f"{key.subject}:chat:c:deploy" == "project:p1:chat:c:deploy"


# --- Möbius's Git in a shared project -------------------------------------------

from app import project_git  # noqa: E402


def _rooms_available():
  return '"available": true' in subprocess.run(
    [sys.executable, str(FENCE), "--check"], capture_output=True, text=True
  ).stdout


def _repo(path: Path) -> Path:
  path.mkdir(parents=True)
  for args in (("init", "-q"), ("config", "user.email", "o@example.com"),
               ("config", "user.name", "Owner")):
    subprocess.run(["git", "-C", str(path), *args], check=True)
  (path / "a.txt").write_text("a\n")
  subprocess.run(["git", "-C", str(path), "add", "a.txt"], check=True)
  subprocess.run(["git", "-C", str(path), "commit", "-qm", "a"], check=True)
  return path


def _plant(repo: Path, key: str, marker: Path) -> None:
  """What an agent in the room could write into the project's .git."""
  subprocess.run(
    ["git", "-C", str(repo), "config", key, f"sh -c 'echo ran > {marker}'"], check=True,
  )


def test_a_private_project_keeps_its_own_git_settings(tmp_path):
  """The owner's own project: Möbius's Git runs exactly as before."""
  repo, marker = _repo(tmp_path / "p"), tmp_path / "ran"
  _plant(repo, "core.fsmonitor", marker)
  project_git.project_status(repo)
  assert marker.exists()


_needs_rooms = pytest.mark.skipif(not _rooms_available(), reason="host cannot build rooms")


@_needs_rooms
@pytest.mark.parametrize("key", ["core.fsmonitor", "core.hooksPath"])
def test_a_shared_projects_planted_git_command_never_runs(tmp_path, key):
  repo, marker = _repo(tmp_path / "p"), tmp_path / "ran"
  if key == "core.hooksPath":
    hooks = repo / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text(f"#!/bin/sh\necho ran > {marker}\n")
    (hooks / "pre-commit").chmod(0o755)
    subprocess.run(["git", "-C", str(repo), "config", key, str(hooks)], check=True)
  else:
    _plant(repo, key, marker)
  (repo / "a.txt").write_text("b\n")
  with project_git.untrusted_settings(True):
    project_git.project_status(repo)
    project_git.commit_project(repo, "change")
  assert not marker.exists()
  assert project_git.project_status(repo)["changes"] == []


@pytest.mark.skipif(not _rooms_available(), reason="host cannot build rooms")
def test_a_shared_projects_git_runs_its_programs_inside_a_room(tmp_path):
  """A filter an agent named runs, but only with the agent's own reach."""
  repo, outside = _repo(tmp_path / "p"), tmp_path / "outside"
  outside.mkdir()
  (repo / ".gitattributes").write_text("*.txt filter=x\n")
  subprocess.run(["git", "-C", str(repo), "config", "filter.x.clean",
                  f"sh -c 'cat; echo ran > {outside}/marker'"], check=True)
  (repo / "a.txt").write_text("c\n")
  with project_git.untrusted_settings(True):
    try:
      project_git.commit_project(repo, "change")
    except project_git.GitProjectError:
      pass
  assert not (outside / "marker").exists()


@pytest.mark.parametrize("failure", ["timeout", "not json", '{"reasons": []}'])
def test_a_room_check_that_cannot_run_refuses_shared_git_and_is_asked_again(
  tmp_path, monkeypatch, failure,
):
  """Only the host's own answer means "no rooms"; one transient failure must
  not take shared projects out of their rooms until the next restart."""
  project_git._rooms_available.cache_clear()
  answers = [
    subprocess.TimeoutExpired("fence", 10) if failure == "timeout" else failure,
    '{"available": false}',
  ]

  def answer(command, **kwargs):
    if "--check" not in command:
      return real_run(command, **kwargs)
    reply = answers.pop(0)
    if isinstance(reply, BaseException):
      raise reply
    return subprocess.CompletedProcess(command, 0, stdout=reply, stderr="")

  real_run = subprocess.run
  monkeypatch.setattr(project_git.subprocess, "run", answer)
  try:
    repo = _repo(tmp_path / "p")
    with project_git.untrusted_settings(True), pytest.raises(
      project_git.GitProjectError, match="confine",
    ):
      project_git._git(repo, "status")
    with project_git.untrusted_settings(True):
      assert project_git._git(repo, "status")[0] == "git"
    assert not answers
  finally:
    project_git._rooms_available.cache_clear()


def test_the_git_panel_of_a_shared_project_treats_its_settings_as_untrusted(
  client, auth, db, monkeypatch,
):
  project = _project(client, auth)
  seen = []
  real = project_git.project_status

  def status(root, **kwargs):
    seen.append(project_git._untrusted.get())
    return real(root, **kwargs)

  monkeypatch.setattr(project_git, "project_status", status)
  client.get(f"/api/projects/{project['id']}/git/status", headers=auth)
  _share(client, auth, project["id"])
  client.get(f"/api/projects/{project['id']}/git/status", headers=auth)
  assert seen == [False, True]


@pytest.mark.skipif(not _rooms_available(), reason="host cannot build rooms")
def test_one_shared_projects_git_room_ignores_settings_left_in_its_home(tmp_path):
  repo = _repo(tmp_path / "p")
  argv = None
  with project_git.untrusted_settings(True):
    argv = project_git._git(repo, "status")
  assert "GIT_CONFIG_GLOBAL=/dev/null" in argv and argv.index("--") < argv.index("env")


def test_without_rooms_no_agent_runs_so_shared_git_runs_as_usual(tmp_path, monkeypatch):
  monkeypatch.setattr(project_git, "_rooms_available", lambda: False)
  repo = _repo(tmp_path / "p")
  with project_git.untrusted_settings(True):
    assert project_git._git(repo, "status") == ["git", "-C", str(repo), "status"]


@_needs_rooms
def test_a_shared_project_never_runs_network_git_on_its_own_repository(tmp_path):
  repo = _repo(tmp_path / "p")
  for command in ("fetch", "push", "pull", "ls-remote"):
    with project_git.untrusted_settings(True), pytest.raises(project_git.GitProjectError):
      project_git._git(repo, command, "origin")


def _github(monkeypatch, *, fetched: str | None = None) -> list:
  """Stand in for GitHub: record each network command the scratch repository
  runs, and for a fetch land ``fetched`` as origin/main there."""
  from app import github_contribution_git as contribution_git
  real, calls = contribution_git._run_cmd, []

  def run(argv, **kwargs):
    if not any(arg.startswith("https://github.com/") for arg in argv):
      return real(argv, **kwargs)
    calls.append((argv, kwargs["env"]))
    if "fetch" in argv and fetched:
      # GitHub has one branch, main, at ``fetched``; --prune drops the rest.
      scratch = Path(argv[2])
      env = contribution_git._git_env(scratch)
      listed = real(["git", "-C", str(scratch), "for-each-ref", "--format=%(refname)",
                     "refs/remotes/origin/"], cwd=scratch, env=env).stdout.split()
      for ref in listed:
        real(["git", "-C", str(scratch), "update-ref", "-d", ref], cwd=scratch, env=env)
      real(["git", "-C", str(scratch), "update-ref", "refs/remotes/origin/main", fetched],
           cwd=scratch, env=env)
    return subprocess.CompletedProcess(argv, 0, "", "")

  monkeypatch.setattr(contribution_git, "_run_cmd", run)
  return calls


def _connected(path: Path) -> Path:
  repo = _repo(path)
  subprocess.run(["git", "-C", str(repo), "remote", "add", "origin",
                  "https://github.com/owner/project.git"], check=True)
  return repo


def _head(repo: Path) -> str:
  return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                        capture_output=True, text=True, check=True).stdout.strip()


@_needs_rooms
def test_a_local_push_address_planted_in_a_shared_project_never_receives_the_push(
  tmp_path, monkeypatch,
):
  """The room could point origin's push address at a repository it made, whose
  receiving hook would run as the server when the owner presses Push."""
  repo, marker = _connected(tmp_path / "p"), tmp_path / "ran"
  trap = repo / ".git" / "x.git"
  subprocess.run(["git", "init", "-q", "--bare", str(trap)], check=True)
  (trap / "hooks" / "post-receive").write_text(f"#!/bin/sh\necho ran > {marker}\n")
  (trap / "hooks" / "post-receive").chmod(0o755)
  subprocess.run(["git", "-C", str(repo), "config", "remote.origin.pushurl", str(trap)], check=True)
  calls = _github(monkeypatch)
  context = project_git._owned_context(repo)
  with project_git.untrusted_settings(True):
    project_git._push_origin(context, _head(repo), "main")
  assert not marker.exists()
  [(argv, env)] = calls
  assert "https://github.com/owner/project.git" in argv and "origin" not in argv
  assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_ALLOW_PROTOCOL"] == "https"
  assert str(repo) not in argv[2]
  # The push is recorded in the project, and the trap was never used.
  assert _head(repo) == subprocess.run(
    ["git", "-C", str(repo), "rev-parse", "refs/remotes/origin/main"],
    capture_output=True, text=True, check=True).stdout.strip()
  # The same push in a private project goes where the owner's settings say.
  project_git._push_origin(context, _head(repo), "main")
  assert marker.exists()


@_needs_rooms
def test_a_submodule_command_planted_in_a_shared_project_never_runs_on_fetch(
  tmp_path, monkeypatch,
):
  """Fetching could recurse into a submodule whose own settings name a
  command; the project's settings are never read outside a room."""
  repo, marker = _connected(tmp_path / "p"), tmp_path / "ran"
  sub = _repo(tmp_path / "sub")
  subprocess.run(["git", "-C", str(repo), "-c", "protocol.file.allow=always",
                  "submodule", "add", "-q", str(sub), "sub"], check=True)
  subprocess.run(["git", "-C", str(repo), "commit", "-qm", "sub"], check=True)
  modules = repo / ".git" / "modules" / "sub"
  subprocess.run(["git", "-C", str(modules), "config", "remote.origin.url", "ssh://example.com/x"], check=True)
  subprocess.run(["git", "-C", str(modules), "config", "core.sshCommand",
                  f"sh -c 'echo ran > {marker}'"], check=True)
  subprocess.run(["git", "-C", str(repo), "config", "fetch.recurseSubmodules", "yes"], check=True)
  calls = _github(monkeypatch, fetched=_head(repo))
  with project_git.untrusted_settings(True):
    project_git._fetch_origin(project_git._owned_context(repo))
  assert not marker.exists() and len(calls) == 1
  # What GitHub sent reached the project, through the room.
  assert subprocess.run(["git", "-C", str(repo), "rev-parse", "refs/remotes/origin/main"],
                        capture_output=True, text=True, check=True).stdout.strip() == _head(repo)


@_needs_rooms
def test_a_fetch_drops_branches_github_no_longer_has(tmp_path, monkeypatch):
  repo = _connected(tmp_path / "p")
  subprocess.run(["git", "-C", str(repo), "update-ref", "refs/remotes/origin/gone", _head(repo)], check=True)
  _github(monkeypatch, fetched=_head(repo))
  with project_git.untrusted_settings(True):
    project_git._fetch_origin(project_git._owned_context(repo))
  refs = subprocess.run(["git", "-C", str(repo), "for-each-ref", "--format=%(refname)", "refs/remotes/origin/"],
                        capture_output=True, text=True, check=True).stdout.split()
  assert refs == ["refs/remotes/origin/main"]


def test_a_shared_project_on_an_apps_source_reads_it_only(client, auth, db):
  """App Git (Apply, updates) runs there with the folder's own settings, so its
  agent must not be able to write .git; members still edit it."""
  from app.project_agent_access import SHARED_APP_SOURCE_ROLE
  project = _project(client, auth)
  row = db.get(models.Project, project["id"])
  _share(client, auth, project["id"])
  assert project_agent_access.role_for(db, row) == SHARED_ROLE
  from app.routes.projects import _project_root
  app = models.App(name="Notes", description="", jsx_source="", compiled_path="",
                   source_dir=str(_project_root(row)), slug="notes-src")
  db.add(app)
  db.commit()
  assert project_agent_access.role_for(db, row) == SHARED_APP_SOURCE_ROLE
  assert SHARED_APP_SOURCE_ROLE.spec.data == "read"

