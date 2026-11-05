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

  response = client.post(f"/api/chats/{source_id}/branch", json={"through": "a1"}, headers=auth)

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
    response = client.post(f"/api/chats/{source_id}/branch", json={"through": key}, headers=auth)
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
    f"/api/chats/{source_id}/branch", json={"through": "a1"}, headers=auth,
  ).json()["id"]

  assert (data_dir / "chats" / branch_id / "uploads" / "notes.txt").read_text() == "hello"
  assert not (data_dir / "chats" / branch_id / "uploads" / "later.txt").exists()
  assert client.get(f"/api/chats/{branch_id}/tool-output/toolu_1", headers=auth).text == "full one"
  assert client.get(f"/api/chats/{branch_id}/tool-output/toolu_2", headers=auth).status_code == 404
  db.expire_all()
  branch = db.query(models.Chat).filter(models.Chat.id == branch_id).one()
  assert branch.uploads == [{"name": "notes.txt", "claimed": True}]


def _first_turn_prompt(client, auth, db, monkeypatch, chat_id, text):
  monkeypatch.setattr("app.providers.ClaudeProvider.check_auth", lambda self, _d: None)
  monkeypatch.setattr("app.providers.ClaudeProvider.ensure_auth", lambda self, _d: asyncio.sleep(0))
  captured = {}

  async def _runner(**kwargs):
    captured.update(kwargs)
    return {"session_id": "new-session", "cost_usd": 0.0, "error": None}

  monkeypatch.setattr("app.claude_sdk_runner.run_claude_sdk_turn", _runner)
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
    f"/api/chats/{source_id}/branch", json={"through": "a1"}, headers=auth,
  ).json()["id"]

  captured = _first_turn_prompt(client, auth, db, monkeypatch, branch_id, "Paris instead")

  prompt = captured["user_message"]
  assert captured["session_id"] is None
  assert "<resumed_context>" in prompt
  assert prompt.index("Plan a trip") < prompt.index("Rome or Paris?") < prompt.index("Paris instead")
  # Only the branch's own turns: nothing after the branch point leaks in.
  assert "Rome it is." not in prompt
  # The current request is the turn itself, not part of the seeded history.
  assert prompt.count("Paris instead") == 1


def test_first_turn_of_a_new_chat_gets_no_seeded_history(client, auth, db, monkeypatch):
  chat_id = client.post("/api/chats", json={"title": "Fresh"}, headers=auth).json()["id"]

  captured = _first_turn_prompt(client, auth, db, monkeypatch, chat_id, "Hello")

  assert "<resumed_context>" not in captured["user_message"]


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

  branch_id = client.post(f"/api/chats/{source_id}/branch", json={"through": "run-a1"}, headers=auth).json()["id"]

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
  branch_id = client.post(f"/api/chats/{source_id}/branch", json={"through": "run-a1"}, headers=auth).json()["id"]

  captured = _first_turn_prompt(client, auth, db, monkeypatch, branch_id, "Paris instead")

  assert captured["fork_from"] == {"session_id": sid, "at": "cut-here"}
  assert captured["session_id"] is None
  assert "<resumed_context>" not in captured["user_message"]


def test_branch_without_a_forkable_session_falls_back_to_text_history(client, auth, db, monkeypatch):
  source_id, _sid = _forkable_source(client, auth, db, transcript=False)
  branch_id = client.post(f"/api/chats/{source_id}/branch", json={"through": "run-a1"}, headers=auth).json()["id"]

  captured = _first_turn_prompt(client, auth, db, monkeypatch, branch_id, "Paris instead")

  assert captured["fork_from"] is None
  assert "Rome or Paris?" in captured["user_message"]


def test_text_history_notes_the_tools_a_reply_used():
  from app.chat_context import _with_tool_activity

  row = {"role": "assistant", "content": "Done", "blocks": [
    {"type": "text", "content": "Checking."},
    {"type": "tool", "tool": "WebSearch", "input": {"query": "rome weather"}},
    {"type": "text", "content": "Sunny."},
  ]}
  assert _with_tool_activity(row)["content"] == 'Checking.\n\n[used WebSearch: {"query": "rome weather"}]\n\nSunny.'


def test_text_history_keeps_the_answer_of_a_reply_with_only_tool_blocks():
  from app.chat_context import _with_tool_activity

  row = {"role": "assistant", "content": "Done", "blocks": [
    {"type": "tool", "tool": "Read", "input": "notes.txt"},
  ]}
  assert _with_tool_activity(row)["content"] == "[used Read: notes.txt]\n\nDone"


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
    client.post(f"/api/chats/{source_id}/branch", json={"through": "a1"}, headers=auth)
  assert set((data_dir / "chats").iterdir()) == before
  assert db.query(models.Chat).count() == 1
