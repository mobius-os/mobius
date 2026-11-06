"""Branching a chat from a reply: what the new chat holds and what its agent sees."""

import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

import pytest

from app import chat as chat_mod, chat_branches, models, schemas, transcript_rows
from app.broadcast import create_broadcast
from app.chat_branches import claude_fork_point, codex_fork_point
from app.chat_writer import StartTurn, alloc_run_token, get_writer
from app.config import get_settings


def _chat(client, auth, db, messages, **fields):
  chat_id = client.post("/api/chats", json={"title": "Trip plan"}, headers=auth).json()["id"]
  client.put(f"/api/chats/{chat_id}", json={"messages": messages}, headers=auth)
  row = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  for key, value in fields.items():
    setattr(row, key, value)
  db.commit()
  return chat_id


CONVERSATION = [
  {"role": "user", "content": "Plan a trip", "cid": "u1", "ts": 1},
  {"id": "a1", "role": "assistant", "content": "Rome or Paris?", "ts": 2},
  {"role": "user", "content": "Rome", "cid": "u2", "ts": 3},
  {"id": "a2", "role": "assistant", "content": "Rome it is.", "ts": 4},
]


def test_branch_copies_rows_through_the_reply_and_leaves_source_unchanged(client, auth, db):
  source_id = _chat(client, auth, db, CONVERSATION, session_id="source-session",
                    agent_settings_json={"model": "claude-opus-4-8", "effort": "high",
                                         "system_prompt": "app only"})

  response = client.post(f"/api/chats/{source_id}/branch", json={"through_id": "a1"}, headers=auth)

  assert response.status_code == 200, response.text
  branch_id = response.json()["id"]
  assert branch_id != source_id
  assert response.json()["title"] == "Branch · Trip plan"
  db.expire_all()
  branch = db.query(models.Chat).filter(models.Chat.id == branch_id).one()
  source = db.query(models.Chat).filter(models.Chat.id == source_id).one()
  assert [m["content"] for m in transcript_rows.read_all(db, branch)] == ["Plan a trip", "Rome or Paris?"]
  assert len(transcript_rows.read_all(db, source)) == 4
  assert branch.session_id is None
  assert branch.provider == source.provider
  assert branch.agent_settings_json == {"model": "claude-opus-4-8", "effort": "high"}
  assert branch.created_by_app_id is None


def test_branch_refuses_an_owner_message_or_unknown_key(client, auth, db):
  source_id = _chat(client, auth, db, CONVERSATION)
  for key in ("u1", "missing"):
    response = client.post(f"/api/chats/{source_id}/branch", json={"through_id": key}, headers=auth)
    assert response.status_code == 409
  assert db.query(models.Chat).count() == 1


def test_branch_brings_attached_files_and_full_tool_outputs(client, auth, db):
  data_dir = Path(get_settings().data_dir)
  messages = [
    {"role": "user", "content": "Read this", "cid": "u1", "ts": 1,
     "attachments": [{"name": "notes.txt"}]},
    {"id": "a1", "role": "assistant", "content": "Done", "ts": 2,
     "blocks": [{"type": "tool", "tool": "Read", "tool_use_id": "toolu_1"}]},
    {"role": "user", "content": "And this", "cid": "u2", "ts": 3,
     "attachments": [{"name": "later.txt"}]},
    {"id": "a2", "role": "assistant", "content": "Ok", "ts": 4,
     "blocks": [{"type": "tool", "tool": "Read", "tool_use_id": "toolu_2"}]},
  ]
  source_id = _chat(client, auth, db, messages, uploads=[
    {"name": "notes.txt", "claimed": True}, {"name": "later.txt", "claimed": True},
  ])
  uploads = data_dir / "chats" / source_id / "uploads"
  uploads.mkdir(parents=True, exist_ok=True)
  (uploads / "notes.txt").write_text("hello")
  (uploads / "later.txt").write_text("later")
  db.add_all([
    models.ToolOutput(chat_id=source_id, tool_use_id="toolu_1", output="full one"),
    models.ToolOutput(chat_id=source_id, tool_use_id="toolu_2", output="full two"),
  ])
  db.commit()

  branch_id = client.post(
    f"/api/chats/{source_id}/branch", json={"through_id": "a1"}, headers=auth,
  ).json()["id"]

  assert (data_dir / "chats" / branch_id / "uploads" / "notes.txt").read_text() == "hello"
  assert not (data_dir / "chats" / branch_id / "uploads" / "later.txt").exists()
  assert client.get(f"/api/chats/{branch_id}/tool-output/toolu_1", headers=auth).text == "full one"
  assert client.get(f"/api/chats/{branch_id}/tool-output/toolu_2", headers=auth).status_code == 404
  db.expire_all()
  branch = db.query(models.Chat).filter(models.Chat.id == branch_id).one()
  assert branch.uploads == [{"name": "notes.txt", "claimed": True}]


def _first_turn_prompt(client, auth, db, monkeypatch, chat_id, text, *, runner=None):
  monkeypatch.setattr("app.providers.ClaudeProvider.check_auth", lambda self, _d: None)
  monkeypatch.setattr("app.providers.ClaudeProvider.ensure_auth", lambda self, _d: asyncio.sleep(0))
  captured = {}

  async def _runner(**kwargs):
    captured.update(kwargs)
    return {"session_id": "new-session", "cost_usd": 0.0, "error": None}

  monkeypatch.setattr("app.claude_sdk_runner.run_claude_sdk_turn", runner or _runner)
  db.query(models.Chat).filter(models.Chat.id == chat_id).one().provider = "claude"
  db.commit()
  create_broadcast(chat_id)
  run_token = alloc_run_token()
  get_writer().submit(StartTurn(
    chat_id=chat_id, run_token=run_token,
    user_msg={"role": "user", "content": text, "ts": 10, "cid": f"next-{chat_id}"},
    title_source=text, default_provider="claude",
  )).result(timeout=5)
  asyncio.run(chat_mod._run_chat_impl(
    messages=[schemas.ChatMessage(role="user", content=text)],
    chat_id=chat_id, session_id=None, provider_id="claude",
    run_gen=chat_mod.current_run_generation(chat_id), run_token=run_token,
  ))
  return captured


def test_branch_first_turn_is_seeded_with_the_copied_conversation(client, auth, db, monkeypatch):
  source_id = _chat(client, auth, db, CONVERSATION)
  branch_id = client.post(
    f"/api/chats/{source_id}/branch", json={"through_id": "a1"}, headers=auth,
  ).json()["id"]

  captured = _first_turn_prompt(client, auth, db, monkeypatch, branch_id, "Paris instead")

  prompt = captured["user_message"]
  assert captured["session_id"] is None
  assert "<resumed_context>" in prompt
  assert prompt.index("Plan a trip") < prompt.index("Rome or Paris?") < prompt.index("Paris instead")
  # Only the branch's own turns: nothing after the branch point leaks in.
  assert "Rome it is." not in prompt


def test_first_turn_of_a_new_chat_gets_no_seeded_history(client, auth, db, monkeypatch):
  chat_id = client.post("/api/chats", json={"title": "Fresh"}, headers=auth).json()["id"]

  captured = _first_turn_prompt(client, auth, db, monkeypatch, chat_id, "Hello")

  assert "<resumed_context>" not in captured["user_message"]


def test_sessionless_chat_that_is_not_a_branch_gets_no_seeded_history(client, auth, db, monkeypatch):
  # Earlier replies without a session (an imported or provider-less history)
  # are not a branch: only `branch_origin` makes a first turn replay them.
  chat_id = _chat(client, auth, db, CONVERSATION)

  captured = _first_turn_prompt(client, auth, db, monkeypatch, chat_id, "And then?")

  assert captured["fork_from"] is None
  assert "<resumed_context>" not in captured["user_message"]
  assert "Rome or Paris?" not in captured["user_message"]


# ── Branch origin and exact provider forks ───────────────────────────────

T0 = datetime(2026, 11, 5, 1, 0, 0)
T1 = datetime(2026, 11, 5, 1, 0, 30)


def _iso(seconds):
  return f"2026-11-05T01:00:{seconds:02d}.000Z"


def _claude_transcript(data_dir, session_id, entries):
  folder = Path(data_dir) / "cli-auth" / "claude" / "projects" / ("-" + str(data_dir).strip("/").replace("/", "-"))
  folder.mkdir(parents=True, exist_ok=True)
  (folder / f"{session_id}.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n")


def _assistant(uid, seconds, *, tool=False, sidechain=False):
  content = [{"type": "tool_use", "id": "toolu_x"}] if tool else [{"type": "text", "text": "x"}]
  return {"type": "assistant", "uuid": uid, "timestamp": _iso(seconds),
          "isSidechain": sidechain, "message": {"content": content}}


def test_claude_fork_point_is_the_runs_last_main_reply_entry(tmp_path):
  sid = str(uuid.uuid4())
  _claude_transcript(tmp_path, sid, [
    _assistant("before-run", 0) | {"timestamp": "2026-11-05T00:59:59.000Z"},
    _assistant("calls-tool", 5, tool=True),
    _assistant("final", 20),
    _assistant("helper", 25, sidechain=True),
    _assistant("next-run", 31),
  ])
  config = tmp_path / "cli-auth" / "claude"
  assert claude_fork_point(config, str(tmp_path), sid, T0, T1) == "final"


def test_claude_run_that_ended_on_a_tool_call_has_no_fork_point(tmp_path):
  sid = str(uuid.uuid4())
  _claude_transcript(tmp_path, sid, [_assistant("text", 5), _assistant("calls-tool", 20, tool=True)])
  assert claude_fork_point(tmp_path / "cli-auth" / "claude", str(tmp_path), sid, T0, T1) is None


def test_codex_fork_point_is_the_last_turn_the_run_started(tmp_path):
  tid = str(uuid.uuid4())
  folder = tmp_path / "sessions" / "2026" / "11" / "05"
  folder.mkdir(parents=True)
  (folder / f"rollout-2026-11-05T01-00-00-{tid}.jsonl").write_text("\n".join(json.dumps(e) for e in [
    {"timestamp": "2026-11-05T00:59:00.000Z", "type": "event_msg", "payload": {"type": "task_started", "turn_id": "older"}},
    {"timestamp": _iso(2), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "this-run"}},
    {"timestamp": _iso(40), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "later"}},
  ]) + "\n")
  assert codex_fork_point(tmp_path, tid, T0, T1) == "this-run"


def _forkable_source(client, auth, db, *, transcript=True):
  data_dir = get_settings().data_dir
  sid = str(uuid.uuid4())
  messages = [
    {"role": "user", "content": "Plan a trip", "cid": "u1", "ts": 1},
    {"id": "run-a1", "role": "assistant", "content": "Rome or Paris?", "ts": 2},
    {"role": "user", "content": "Rome", "cid": "u2", "ts": 3},
    {"id": "run-a2", "role": "assistant", "content": "Rome it is.", "ts": 4},
  ]
  source_id = _chat(client, auth, db, messages, provider="claude", session_id=sid)
  for run_id in ("run-a1", "run-a2"):
    db.add(models.ChatRun(id=run_id, root_run_id=run_id, chat_id=source_id, status="completed",
                          provider="claude", provider_session_id=sid, started_at=T0, ended_at=T1))
  db.commit()
  if transcript:
    _claude_transcript(data_dir, sid, [_assistant("cut-here", 10)])
  return source_id, sid


def test_branch_records_its_origin_and_the_exact_fork_point(client, auth, db):
  source_id, sid = _forkable_source(client, auth, db)

  branch_id = client.post(f"/api/chats/{source_id}/branch", json={"through_id": "run-a1"}, headers=auth).json()["id"]

  db.expire_all()
  origin = db.query(models.Chat).filter(models.Chat.id == branch_id).one().branch_origin
  assert origin == {"chat_id": source_id, "title": "Trip plan", "through_count": 2,
                    "fork": {"provider": "claude", "session_id": sid, "at": "cut-here"}}
  # The app gets what the divider shows, never the provider fork point.
  detail = client.get(f"/api/chats/{branch_id}", headers=auth).json()
  assert detail["branch_origin"] == {"chat_id": source_id, "title": "Trip plan",
                                     "through_count": 2, "available": True}


def test_branch_first_turn_forks_the_source_session_instead_of_replaying_text(client, auth, db, monkeypatch):
  source_id, sid = _forkable_source(client, auth, db)
  branch_id = client.post(f"/api/chats/{source_id}/branch", json={"through_id": "run-a1"}, headers=auth).json()["id"]

  captured = _first_turn_prompt(client, auth, db, monkeypatch, branch_id, "Paris instead")

  assert captured["fork_from"] == {"session_id": sid, "at": "cut-here"}
  assert captured["session_id"] is None
  assert "<resumed_context>" not in captured["user_message"]


def test_branch_without_a_forkable_session_falls_back_to_text_history(client, auth, db, monkeypatch):
  source_id, _sid = _forkable_source(client, auth, db, transcript=False)
  branch_id = client.post(f"/api/chats/{source_id}/branch", json={"through_id": "run-a1"}, headers=auth).json()["id"]

  captured = _first_turn_prompt(client, auth, db, monkeypatch, branch_id, "Paris instead")

  assert captured["fork_from"] is None
  assert "Rome or Paris?" in captured["user_message"]


def test_refused_claude_fork_reruns_the_same_turn_on_the_text_reseed(client, auth, db, monkeypatch):
  source_id, sid = _forkable_source(client, auth, db)
  branch_id = client.post(f"/api/chats/{source_id}/branch", json={"through_id": "run-a1"}, headers=auth).json()["id"]
  calls = []

  async def _runner(**kwargs):
    calls.append(kwargs)
    if kwargs["fork_from"] is not None:
      # What the CLI prints, before any session, when the cut point is gone.
      return {"session_id": None, "cost_usd": 0.0,
              "error": "Command failed with exit code 1: No message found with message.uuid of: cut-here"}
    return {"session_id": "new-session", "cost_usd": 0.0, "error": None}

  _first_turn_prompt(client, auth, db, monkeypatch, branch_id, "Paris instead", runner=_runner)

  assert [call["fork_from"] for call in calls] == [{"session_id": sid, "at": "cut-here"}, None]
  assert "<resumed_context>" not in calls[0]["user_message"]
  assert "<resumed_context>" in calls[1]["user_message"]
  assert "Rome or Paris?" in calls[1]["user_message"]
  db.expire_all()
  branch = db.get(models.Chat, branch_id)
  assert branch.branch_origin["fork"] is None
  assert branch.session_id == "new-session"


@pytest.mark.parametrize("result", [
  {"session_id": None, "error": "You've hit your usage limit · resets 5pm"},
  {"session_id": None, "error": "Invalid API key · Please run /login"},
  # A refusal-like text after the session started is an ordinary turn error.
  {"session_id": "forked-session", "error": "No conversation found in tool output"},
])
def test_ordinary_turn_failure_keeps_the_branch_fork(client, auth, db, monkeypatch, result):
  source_id, sid = _forkable_source(client, auth, db)
  branch_id = client.post(f"/api/chats/{source_id}/branch", json={"through_id": "run-a1"}, headers=auth).json()["id"]
  calls = []

  async def _runner(**kwargs):
    calls.append(kwargs)
    return {"cost_usd": 0.0, **result}

  _first_turn_prompt(client, auth, db, monkeypatch, branch_id, "Paris instead", runner=_runner)

  assert len(calls) == 1
  db.expire_all()
  assert db.get(models.Chat, branch_id).branch_origin["fork"] == {
    "provider": "claude", "session_id": sid, "at": "cut-here",
  }


def _source_with_runs(client, auth, db, messages, run_ids, **fields):
  sid = str(uuid.uuid4())
  source_id = _chat(client, auth, db, messages, provider="claude", session_id=sid, **fields)
  for run_id in run_ids:
    db.add(models.ChatRun(id=run_id, root_run_id=run_id, chat_id=source_id, status="completed",
                          provider="claude", provider_session_id=sid, started_at=T0, ended_at=T1))
  db.commit()
  _claude_transcript(get_settings().data_dir, sid, [_assistant("cut-here", 10)])
  return source_id


def _branch_origin(client, auth, db, source_id, through_id):
  response = client.post(f"/api/chats/{source_id}/branch", json={"through_id": through_id}, headers=auth)
  assert response.status_code == 200, response.text
  db.expire_all()
  return db.get(models.Chat, response.json()["id"]).branch_origin


def test_reply_cut_by_a_mid_turn_message_branches_as_text_not_a_fork(client, auth, db):
  # A steered run keeps its token on the pre-steer reply and continues in
  # `<token>:assistant:1`; its provider session ended after the continuation,
  # so a fork would carry text the branch never showed.
  steered = [
    {"role": "user", "content": "Plan a trip", "cid": "u1", "ts": 1},
    {"id": "run-a1", "role": "assistant", "content": "Looking at Rome", "ts": 2},
    {"role": "user", "content": "Actually Paris", "cid": "u2", "ts": 3},
    {"id": "run-a1:assistant:1", "role": "assistant", "content": "Paris it is.", "ts": 4},
  ]
  source_id = _source_with_runs(client, auth, db, steered, ["run-a1"])

  assert _branch_origin(client, auth, db, source_id, "run-a1")["fork"] is None
  assert _branch_origin(client, auth, db, source_id, "run-a1:assistant:1")["fork"] is None


def test_branch_refuses_every_row_of_the_run_still_writing(client, auth, db):
  steered = [
    {"role": "user", "content": "Plan a trip", "cid": "u1", "ts": 1},
    {"id": "run-a1", "role": "assistant", "content": "Rome or Paris?", "ts": 2},
    {"role": "user", "content": "Rome", "cid": "u2", "ts": 3},
    {"id": "run-a2", "role": "assistant", "content": "Looking", "ts": 4},
    {"role": "user", "content": "Cheap please", "cid": "u3", "ts": 5},
    {"id": "run-a2:assistant:1", "role": "assistant", "content": "Still looking", "ts": 6},
  ]
  source_id = _chat(client, auth, db, steered, active_assistant_message_id="run-a2")

  for through_id in ("run-a2", "run-a2:assistant:1"):
    response = client.post(f"/api/chats/{source_id}/branch", json={"through_id": through_id}, headers=auth)
    assert response.status_code == 409
    assert "still being written" in response.json()["detail"]
  assert client.post(f"/api/chats/{source_id}/branch", json={"through_id": "run-a1"},
                     headers=auth).status_code == 200


def test_branch_names_a_reply_only_by_its_stored_id(client, auth, db):
  unsaved = [
    {"role": "user", "content": "Plan a trip", "cid": "u1", "ts": 1},
    {"role": "assistant", "content": "Rome or Paris?", "cid": "a1", "ts": 2},
  ]
  source_id = _chat(client, auth, db, unsaved)
  for through_id in ("a1", "assistant-2"):
    response = client.post(f"/api/chats/{source_id}/branch", json={"through_id": through_id}, headers=auth)
    assert response.status_code == 409


def test_app_and_helper_chats_cannot_be_branched(client, auth, db):
  app = models.App(name="Trips", slug="trips", source_dir="/data/apps/trips")
  db.add(app)
  db.commit()
  app_chat = _chat(client, auth, db, CONVERSATION, created_by_app_id=app.id)
  helper_chat = _chat(client, auth, db, CONVERSATION)
  db.add(models.Delegation(
    id="branch-helper", parent_chat_id=app_chat, parent_root_run_id="parent-run",
    task_key="helper", child_chat_id=helper_chat, provider="claude", scope="write",
    cwd="/data", prompt_sha256="0" * 64,
  ))
  db.commit()
  chats_before = db.query(models.Chat).count()

  for source_id in (app_chat, helper_chat):
    response = client.post(f"/api/chats/{source_id}/branch", json={"through_id": "a1"}, headers=auth)
    assert response.status_code == 409
    assert response.json()["detail"] == "Only your own chats can be branched."
  assert db.query(models.Chat).count() == chats_before


def test_branch_copies_only_bare_upload_file_names(client, auth, db):
  data_dir = Path(get_settings().data_dir)
  outside = data_dir / "chats" / "secret.txt"
  outside.parent.mkdir(parents=True, exist_ok=True)
  outside.write_text("not an upload")
  messages = [
    {"role": "user", "content": "Read these", "cid": "u1", "ts": 1,
     "attachments": [{"name": "../secret.txt"}, {"name": "ok.txt"}]},
    {"id": "a1", "role": "assistant", "content": "Done", "ts": 2},
  ]
  source_id = _chat(client, auth, db, messages, uploads=[
    {"name": "../secret.txt", "claimed": True}, {"name": "ok.txt", "claimed": True},
  ])
  uploads = data_dir / "chats" / source_id / "uploads"
  uploads.mkdir(parents=True, exist_ok=True)
  (uploads / "ok.txt").write_text("ok")

  branch_id = client.post(f"/api/chats/{source_id}/branch", json={"through_id": "a1"}, headers=auth).json()["id"]

  db.expire_all()
  assert db.get(models.Chat, branch_id).uploads == [{"name": "ok.txt", "claimed": True}]
  assert sorted(p.name for p in (data_dir / "chats" / branch_id / "uploads").iterdir()) == ["ok.txt"]


def test_failed_upload_copy_leaves_no_branch_folder(client, auth, db, monkeypatch):
  data_dir = Path(get_settings().data_dir)
  messages = [
    {"role": "user", "content": "Read these", "cid": "u1", "ts": 1,
     "attachments": [{"name": "one.txt"}, {"name": "two.txt"}]},
    {"id": "a1", "role": "assistant", "content": "Done", "ts": 2},
  ]
  source_id = _chat(client, auth, db, messages, uploads=[
    {"name": "one.txt", "claimed": True}, {"name": "two.txt", "claimed": True},
  ])
  uploads = data_dir / "chats" / source_id / "uploads"
  uploads.mkdir(parents=True, exist_ok=True)
  (uploads / "one.txt").write_text("one")
  (uploads / "two.txt").write_text("two")
  real_copy = chat_branches._link_or_copy

  def _fail_second(source, target):
    if source.name == "two.txt":
      raise OSError("disk full")
    real_copy(source, target)

  monkeypatch.setattr(chat_branches, "_link_or_copy", _fail_second)
  before = set((data_dir / "chats").iterdir())
  with pytest.raises(OSError):
    client.post(f"/api/chats/{source_id}/branch", json={"through_id": "a1"}, headers=auth)
  assert set((data_dir / "chats").iterdir()) == before
  assert db.query(models.Chat).count() == 1


# ── Codex: the branch fork and its fallback ──────────────────────────────

def _codex_turn(monkeypatch, thread_fork):
  """Run one Codex branch first turn whose app-server answers ``thread/fork`` with ``thread_fork``."""
  from types import SimpleNamespace

  from app import codex_sdk_runner
  from tests.test_codex_sdk_runner import (
    _FakeBroadcast, _FakeThread, _FakeTurnCompletedNotification, _FakeTurnHandle, _fake_sdk,
  )

  def _thread(thread_id):
    return _FakeThread(thread_id, _FakeTurnHandle([SimpleNamespace(
      method="turn/completed",
      payload=_FakeTurnCompletedNotification(SimpleNamespace(id="turn-x", usage=None, error=None)),
    )]))

  threads = {"fresh": _thread("fresh-thread"), "forked": _thread("forked-thread")}
  fork_calls = []

  class FakeClient:
    async def thread_fork(self, thread_id, params):
      fork_calls.append((thread_id, params))
      return thread_fork()

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self._client = FakeClient()

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_a):
      return None

    async def thread_start(self, *_a, **_k):
      return threads["fresh"]

  async def _no_platform_hooks(_codex, _sdk, _cwd, config, *_a, **_k):
    return config

  import openai_codex
  monkeypatch.setattr(codex_sdk_runner, "_codex_platform_hook_thread_config", _no_platform_hooks)
  monkeypatch.setattr(openai_codex, "AsyncThread", lambda _codex, thread_id: threads["forked"])
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: _fake_sdk(FakeAsyncCodex))
  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="Paris instead", session_id=None, base_env={}, cwd="/tmp",
    chat_id="branch-chat", bc=_FakeBroadcast(), pending_questions={}, db=None,
    resumed_context="<resumed_context>Rome or Paris?</resumed_context>",
    fork_from={"session_id": "source-thread", "at": "turn-7"},
  ))
  return result, threads, fork_calls


def test_codex_branch_turn_runs_on_the_source_thread_forked_through_the_reply(monkeypatch):
  from types import SimpleNamespace

  result, threads, fork_calls = _codex_turn(
    monkeypatch, lambda: SimpleNamespace(thread=SimpleNamespace(id="forked-thread")),
  )

  assert result["error"] is None
  assert result["session_id"] == "forked-thread"
  [(thread_id, params)] = fork_calls
  assert thread_id == "source-thread"
  assert params["lastTurnId"] == "turn-7"
  assert params["cwd"] == "/tmp"
  # The fork already holds the conversation: no text replay.
  assert threads["forked"].turn_args[0].endswith("Paris instead")
  assert "<resumed_context>" not in threads["forked"].turn_args[0]
  assert threads["fresh"].turn_args is None


def test_codex_branch_fork_failure_of_any_kind_starts_fresh_with_text_history(monkeypatch):
  def _fails():
    raise RuntimeError("app-server: unknown turn turn-7")

  result, threads, _ = _codex_turn(monkeypatch, _fails)

  assert result["error"] is None
  assert result["session_id"] == "fresh-thread"
  assert "<resumed_context>Rome or Paris?</resumed_context>" in threads["fresh"].turn_args[0]
  assert threads["forked"].turn_args is None
