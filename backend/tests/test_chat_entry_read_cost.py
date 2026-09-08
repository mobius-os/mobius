"""Chat entry keeps large transcript and historical-plan reads demand driven."""

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import event
from sqlalchemy.orm import load_only

from app import goal_plans, models
from app.chat_writer import ReplaceTranscript, get_writer
from app.database import SessionLocal, engine


@contextmanager
def _selects():
  statements = []

  def capture(_conn, _cursor, statement, _parameters, _context, _many):
    if statement.lstrip().upper().startswith("SELECT"):
      statements.append(" ".join(statement.lower().split()).split(" from ", 1)[0])

  event.listen(engine, "before_cursor_execute", capture)
  try:
    yield statements
  finally:
    event.remove(engine, "before_cursor_execute", capture)


def _transcript_reads(statements):
  return [sql for sql in statements if "chats.messages" in sql]


def _new_chat(client, auth, messages=None):
  response = client.post("/api/chats", headers=auth, json={
    "title": "Read cost regression", "messages": messages or [],
  })
  assert response.status_code == 200, response.text
  return response.json()["id"]


def _run(
  db, chat_id, *, run_id="root", goal_id="goal", status="completed",
  start=0, end=5, root_id=None, task_status=None,
):
  base = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
  run = models.ChatRun(
    id=run_id, root_run_id=root_id or run_id, chat_id=chat_id,
    goal_id=goal_id, goal_objective="Preserve every answer", provider="codex",
    status=status, started_at=base + timedelta(seconds=start),
    ended_at=base + timedelta(seconds=end),
  )
  if task_status:
    run.goal_plan_json = {"version": 1, "tasks": [{
      "id": "verify", "title": "Verify", "status": task_status,
      "depends_on": [],
    }]}
    run.goal_plan_revision = 1
  db.add(run)
  db.commit()
  return run


def test_cold_goal_runtime_without_question_never_reads_transcript(
  client, auth, db,
):
  chat_id = _new_chat(client, auth)
  _run(db, chat_id, status="running")
  with _selects() as statements:
    response = client.get(f"/api/chats/{chat_id}/runtime", headers=auth)
  assert response.status_code == 200, response.text
  assert not _transcript_reads(statements)
  assert not any("chats.live_assistant" in sql for sql in statements)
  with SessionLocal() as cold, _selects() as handoff_statements:
    assert goal_plans.goal_handoff_owner_kind(cold, chat_id, "goal") is None
  assert not _transcript_reads(handoff_statements)
  assert not any("chats.pending_messages" in sql for sql in handoff_statements)


@pytest.mark.parametrize("preload", ["cold", "loaded", "raiseload"])
def test_settled_question_keeps_exact_author_without_reloading_loaded_history(
  client, auth, db, preload,
):
  chat_id = _new_chat(client, auth, [{
    "id": "root", "role": "assistant", "content": "Choose", "ts": 1,
    "blocks": [{
      "type": "question", "question_id": "card",
      "response_mode": "continuation", "questions": [],
    }],
  }])
  _run(db, chat_id)
  _run(
    db, chat_id, run_id="unrelated", goal_id="another", status="running",
    start=10, end=15,
  )
  row = db.get(models.Chat, chat_id)
  row.pending_question_id = "card"
  db.commit()
  with SessionLocal() as cold:
    held = None
    if preload == "loaded":
      held = cold.get(models.Chat, chat_id)
      assert held.messages
    elif preload == "raiseload":
      held = cold.query(models.Chat).options(
        load_only(models.Chat.id, raiseload=True),
      ).one()
    with _selects() as statements:
      assert goal_plans.goal_handoff_owner_kind(
        cold, chat_id, "goal",
      ) == "owner_question"
    assert len(_transcript_reads(statements)) == (0 if preload == "loaded" else 1)
    assert goal_plans.goal_handoff_owner_kind(cold, chat_id, "another") is None


@pytest.mark.parametrize("start,end,expected", [
  (0, 2, False), (3, 3, False), (0, 3, False),
  (3, 4, True), (0, None, True),
])
def test_resumed_goal_only_hydrates_plan_at_final_anchor(
  client, auth, db, monkeypatch, start, end, expected,
):
  messages = [
    {"role": "user", "content": "start"},
    {"role": "assistant", "content": "first", "ts": 1787486401000},
    {"role": "user", "content": "continue"},
    {"role": "assistant", "content": "final", "ts": 1787486411000},
  ]
  chat_id = _new_chat(client, auth, messages)
  _run(db, chat_id, task_status="completed")
  _run(db, chat_id, run_id="resume", root_id="root", start=10, end=15)
  spy = Mock(wraps=goal_plans.serialize_plan)
  monkeypatch.setattr(goal_plans, "serialize_plan", spy)
  with SessionLocal() as cold, _selects() as statements:
    result = goal_plans.terminal_goal_summaries_by_message_index(
      cold, chat_id, messages, message_start=start, message_end=end,
    )
  assert spy.call_count == int(expected)
  assert list(result) == ([3] if expected else [])
  assert not _transcript_reads(statements)
  if not expected:
    assert not any("chat_runs.goal_plan_json" in sql for sql in statements)
  else:
    assert result[3][0]["plan"]["summary"]["can_complete"] is True


@pytest.mark.parametrize("status,task_status,expected", [
  ("failed", "pending", "failed"), ("completed", "pending", None),
  ("completed", "completed", "completed"),
])
def test_history_preserves_failed_and_unfinished_goal_semantics(
  client, auth, db, status, task_status, expected,
):
  messages = [{"role": "assistant", "content": "answer", "ts": 1787486401000}]
  chat_id = _new_chat(client, auth, messages)
  _run(db, chat_id, status=status, task_status=task_status)
  with SessionLocal() as cold:
    result = goal_plans.terminal_goal_summaries_by_message_index(
      cold, chat_id, messages,
    )
  assert (result[0][0]["status"] if result else None) == expected


def test_edit_diffs_reads_transcript_once_after_writer_fence(client, auth):
  chat_id = _new_chat(client, auth)
  with _selects() as statements:
    response = client.get(f"/api/chats/{chat_id}/edit-diffs", headers=auth)
  assert response.status_code == 200, response.text
  assert len(_transcript_reads(statements)) == 1


def test_edit_diffs_barrier_failure_does_not_read_transcript(
  client, auth, monkeypatch,
):
  from app import chat_writer
  chat_id = _new_chat(client, auth)
  monkeypatch.setattr(
    chat_writer, "get_writer", Mock(side_effect=RuntimeError("unavailable")),
  )
  with _selects() as statements:
    response = client.get(f"/api/chats/{chat_id}/edit-diffs", headers=auth)
  assert response.status_code == 503
  assert not _transcript_reads(statements)


@pytest.mark.parametrize("change", ["transcript", "deletion"])
def test_edit_diffs_rechecks_authoritative_state_after_fence(
  client, auth, monkeypatch, change,
):
  from app.routes import chats
  chat_id = _new_chat(client, auth)
  original = chats._drain_writer_before_sidecar_read

  def fence(db, requested_id, sidecar):
    # Release the request snapshot before simulating a concurrent committed write.
    db.rollback()
    if change == "transcript":
      get_writer().submit(ReplaceTranscript(chat_id=chat_id, messages=[{
        "role": "assistant", "content": "edited", "blocks": [{
          "type": "tool", "tool": "Edit", "tool_use_id": "fresh",
          "edit_preview": {"diff": "+new committed text"},
        }],
      }])).result(timeout=5)
    else:
      with SessionLocal() as writer:
        writer.get(models.Chat, chat_id).deleted_at = datetime.now(UTC)
        writer.commit()
    original(db, requested_id, sidecar)

  monkeypatch.setattr(chats, "_drain_writer_before_sidecar_read", fence)
  response = client.get(f"/api/chats/{chat_id}/edit-diffs", headers=auth)
  if change == "deletion":
    assert response.status_code == 404
  else:
    assert response.status_code == 200, response.text
    assert "+new committed text" in response.text


def test_detail_history_with_many_goals_reads_transcript_only_once(
  client, auth, db, monkeypatch,
):
  messages = [{
    "role": "assistant", "content": f"Answer {i}",
    "ts": 1787486401000 + i * 10000,
  } for i in range(6)]
  chat_id = _new_chat(client, auth, messages)
  for i in range(6):
    _run(
      db, chat_id, run_id=f"run-{i}", goal_id=f"goal-{i}",
      start=i * 10, end=i * 10 + 5,
    )
  spy = Mock(wraps=goal_plans.serialize_plan)
  monkeypatch.setattr(goal_plans, "serialize_plan", spy)
  with _selects() as statements:
    response = client.get(f"/api/chats/{chat_id}?limit=1", headers=auth)
  assert response.status_code == 200, response.text
  assert len(_transcript_reads(statements)) == 1
  body = response.json()
  assert body["offset"] == 5
  assert body["messages"][0]["goal_summaries"][0]["id"] == "goal-5"
  # The current Goal can also be presented separately; older off-page plans cannot.
  assert {call.args[1].goal_id for call in spy.call_args_list} == {"goal-5"}


def test_cold_runtime_keeps_settled_continuation_question_owner(
  client, auth, db,
):
  chat_id = _new_chat(client, auth, [{
    "id": "root", "role": "assistant", "content": "Approval needed", "ts": 1,
    "blocks": [{
      "type": "question", "question_id": "card",
      "response_mode": "continuation", "questions": [],
    }],
  }])
  _run(db, chat_id)
  db.get(models.Chat, chat_id).pending_question_id = "card"
  db.commit()
  with _selects() as statements:
    response = client.get(f"/api/chats/{chat_id}/runtime", headers=auth)
  assert response.status_code == 200, response.text
  body = response.json()
  assert body["pending_question_id"] == "card"
  assert body["goal"]["status"] == "paused"
  assert body["goal"]["wait_kind"] == "owner_question"
  assert len(_transcript_reads(statements)) == 1
