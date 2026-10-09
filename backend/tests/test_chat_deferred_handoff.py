"""Read-side compact hold status never supplies lifecycle authority."""
from datetime import UTC, datetime, timedelta

from app.chat_writer import create_chat
import pytest
from sqlalchemy import event

from app import models
from app.goal_plans import presented_deferred_goals
from app.goals import _new_hold
from tests.goal_fixtures import goal_run


def seed(db, chat):
  run = goal_run(db, id=f"held-{chat.id}", chat_id=chat.id, status="stopped",
                 goal_objective="Finish exact outcome", started_at=datetime.now(UTC))
  db.add(run)
  db.flush()
  goal = db.get(models.ChatGoal, run.goal_id)
  goal.hold_json = _new_hold(cause="deferred", actor="agent", source_id=run.id,
                            run_id=run.id, reason="Await the agreed review window")
  db.commit()
  return goal


def read(client, auth, chat):
  row = client.get(f"/api/chats?ids={chat.id}", headers=auth).json()[0]
  detail = client.get(f"/api/chats/{chat.id}", headers=auth).json()
  runtime = client.get(f"/api/chats/{chat.id}/runtime", headers=auth).json()
  assert row["handoff"] == detail["handoff"] == runtime["handoff"]
  assert detail["goal"] == runtime["goal"]
  return row, runtime


@pytest.mark.parametrize("other", [None, "working", "card", "wait", "recovery"])
def test_deferred_chat_status_and_unrelated_work_precedence(client, auth, db, chat, monkeypatch, other):
  goal = seed(db, chat)
  if other in {"working", "card", "recovery"}:
    db.add(models.ChatRun(id="unrelated", chat_id=chat.id,
                         status="running" if other == "working" else "completed",
                         started_at=datetime.now(UTC) + timedelta(seconds=1)))
  if other == "working":
    monkeypatch.setattr("app.routes.chats.is_chat_running", lambda _id: True)
  if other == "card":
    chat.pending_question_id = "unrelated-card"
  if other == "wait":
    db.add(models.ChatWait(id="unrelated-wait", chat_id=chat.id, kind="timer",
                          description="Other work", status="armed",
                          due_at=datetime.now(UTC) + timedelta(days=1),
                          deadline_at=datetime.now(UTC) + timedelta(days=2),
                          next_check_at=datetime.now(UTC) + timedelta(days=1)))
  if other == "recovery":
    db.add(models.ChatRun(id="park", chat_id=chat.id, status="parked", park_reason="restart",
                         started_at=datetime.now(UTC) + timedelta(seconds=2)))
    monkeypatch.setattr("app.routes.chats.continuation_handoff_for_chat",
                        lambda *_: {"kind": "recovery", "reason": "restart"})
  db.commit()
  row, runtime = read(client, auth, chat)
  shown = runtime["goal"]
  assert shown["id"] == goal.id and shown["pause_reason"] == "deferred"
  assert shown["handoff"] == {"kind": "none", "reason": None}
  expected = {"working": "working", "card": "owner_input", "wait": "automatic", "recovery": "recovery"}
  assert row["handoff"]["kind"] == expected.get(other, "on_hold")
  if other is None:
    assert row["handoff"] == {"kind": "on_hold", "reason": "deferred", "goal_id": goal.id,
                               "hold_reason": "Await the agreed review window"}
    assert not row["running"] and not row["waiting"]


@pytest.mark.parametrize("change", ["unknown", "system", "dismissed", "terminal", "new_terminal", "new_unpresentable"])
def test_non_deliberate_or_unpresented_hold_is_not_chat_deferral(client, auth, db, chat, change):
  goal = seed(db, chat)
  if change == "unknown":
    goal.hold_json = None
  elif change == "system":
    goal.hold_json = {**goal.hold_json, "actor": "system"}
  elif change == "dismissed":
    chat.dismissed_goal_id = goal.id
  elif change == "terminal":
    goal.status = "completed"
  else:
    newer = goal_run(db, id="new-goal", chat_id=chat.id, status="completed",
                     goal_objective="New outcome", started_at=datetime.now(UTC) + timedelta(seconds=2))
    if change == "new_unpresentable":
      newer.goal_objective = None
    db.add(newer)
    if change == "new_terminal":
      db.flush()
      db.get(models.ChatGoal, newer.goal_id).status = "completed"
  db.commit()
  row, _ = read(client, auth, chat)
  assert row["handoff"] == {"kind": "none", "reason": None}
  assert not row["running"]  # historical terminal Goal is not chat completion


def test_batch_projection_is_one_scalar_select_without_transcript_or_plan(db, chat):
  goal = seed(db, chat)
  for i in range(8):
    other = create_chat(id=f"batch-{i}", title="Batch")
    db.add(other)
    db.flush()
    seed(db, other)
  chat_id = chat.id
  statements = []
  def capture(_conn, _cursor, statement, *_args):
    statements.append(statement)
  event.listen(db.get_bind(), "before_cursor_execute", capture)
  try:
    projected = presented_deferred_goals(db, [chat_id] + [f"batch-{i}" for i in range(8)])
  finally:
    event.remove(db.get_bind(), "before_cursor_execute", capture)
  assert len(projected) == 9 and projected[chat.id]["id"] == goal.id
  assert len(statements) == 1
  assert "row_number() OVER" in statements[0]
  assert "messages_v1" not in statements[0] and "chat_messages" not in statements[0]
  assert "plan_json" not in statements[0]
