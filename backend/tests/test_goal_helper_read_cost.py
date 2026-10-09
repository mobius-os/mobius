"""Goal trees preserve helper truth without one latest-run read per node."""

from contextlib import contextmanager
from datetime import datetime, timedelta

from app.chat_writer import create_chat
import pytest
from sqlalchemy import event

from app import delegations, goal_plans, models
from app.database import SessionLocal
from tests.goal_fixtures import goal_run


@contextmanager
def _selects(db):
  statements = []

  def capture(_conn, _cursor, statement, *_args):
    if statement.lstrip().upper().startswith("SELECT"):
      statements.append(statement)

  event.listen(db.get_bind(), "before_cursor_execute", capture)
  try:
    yield statements
  finally:
    event.remove(db.get_bind(), "before_cursor_execute", capture)


def _helper(db, parent_id, key, *, status=None, root_id="goal-root", **flags):
  child_id = f"child-{key}"
  db.add(create_chat(id=child_id, title=key, messages=[]))
  db.flush()
  row = models.Delegation(
    id=f"helper-{key}", parent_chat_id=parent_id, parent_root_run_id=root_id,
    task_key=key, child_chat_id=child_id, provider="codex", scope="write",
    cwd="/data", prompt_sha256="0" * 64, **flags,
  )
  db.add(row)
  if status is not None:
    db.add(models.ChatRun(
      id=f"run-{key}", chat_id=child_id, provider="codex", status=status,
      started_at=datetime(2026, 10, 2, 12),
      activity_delivery_json={"large": "not needed by a Goal card" * 100},
    ))
  db.flush()
  return row


def test_status_batch_preserves_all_single_helper_rules_in_one_lightweight_read(
  db, chat,
):
  now = datetime(2026, 10, 2, 12)
  cases = [
    (None, {}, "starting"),
    *[(status, {}, status) for status in (
      "running", "completed", "failed", "stopped", "interrupted", "custom",
    )],
    ("resume_pending", {}, "resuming"),
    ("parked", {}, "paused"),
    ("parked_notified", {}, "paused"),
    ("running", {"cancelled_at": now}, "cancelled"),
    ("running", {"interrupted_at": now, "cancelled_at": now}, "interrupted"),
    *[(None, {"source_work_status": status, "source_work_result": "Saved"}, status)
      for status in ("accepted", "retrying", "needs_review", "completed")],
    (None, {"source_work_status": "failed"}, "starting"),
  ]
  ids = []
  expected = {}
  for index, (status, flags, shown) in enumerate(cases):
    row = _helper(db, chat.id, str(index), status=status, **flags)
    ids.append(row.id)
    expected[row.id] = shown
  db.commit()
  with SessionLocal() as cold:
    rows = cold.query(models.Delegation).filter(models.Delegation.id.in_(ids)).all()
    with _selects(cold) as statements:
      actual = delegations.delegation_statuses(cold, rows)
    assert actual == expected
    assert len(statements) == 1
    assert "chats.messages" not in statements[0]
    assert "activity_delivery_json" not in statements[0]
    assert "continuation_json" not in statements[0]
    assert "goal_plan_json" not in statements[0]
    assert actual == {
      row.id: delegations.derived_status(cold, row, load_result=False)[0]
      for row in rows
    }


def test_batch_selects_exact_latest_run_and_never_borrows_another_chat(db, chat):
  row = _helper(db, chat.id, "tie", status="completed")
  missing = _helper(db, chat.id, "missing")
  db.add_all([
    models.ChatRun(
      id="z-tied", chat_id=row.child_chat_id, status="parked_notified",
      started_at=datetime(2026, 10, 2, 12),
    ),
    models.ChatRun(
      id="zzz-older", chat_id=row.child_chat_id, status="failed",
      started_at=datetime(2026, 10, 2, 11),
    ),
    models.ChatRun(
      id="unrelated-newest", chat_id=chat.id, status="running",
      started_at=datetime(2026, 10, 2, 13),
    ),
  ])
  db.commit()
  rows = db.query(models.Delegation).all()
  assert delegations.delegation_statuses(db, rows) == {
    row.id: "paused", missing.id: "starting",
  }
  db.add(models.ChatRun(
    id="follow-up", chat_id=row.child_chat_id, status="running",
    started_at=datetime(2026, 10, 2, 14),
  ))
  db.commit()
  rows = db.query(models.Delegation).all()
  assert delegations.delegation_statuses(db, rows)[row.id] == "running"


def test_empty_status_batch_does_not_read_database(db):
  with _selects(db) as statements:
    assert delegations.delegation_statuses(db, []) == {}
  assert not statements


@pytest.mark.parametrize("width", [1, 24, 100])
def test_goal_tree_read_count_is_independent_of_helper_width(db, chat, width):
  parent_id = chat.id
  physical = goal_run(
    db, id="goal-root", root_run_id="goal-root", chat_id=parent_id, status="running",
    goal_id="goal", goal_objective="Preserve helper ownership",
  )
  db.add(physical)
  db.flush()
  for index in range(width):
    _helper(db, parent_id, f"wide-{index}", status="running")
  db.commit()
  with SessionLocal() as cold:
    physical = cold.get(models.ChatRun, "goal-root")
    root = cold.get(models.ChatGoal, "goal")
    with _selects(cold) as statements:
      tree = goal_plans._delegation_tree(cold, physical, root)
    assert len(tree) == width
    assert {node["status"] for node in tree} == {"running"}
    # Goal attempt roots, direct children, descendant frontier, then statuses.
    assert len(statements) == 4
    assert not any("chats.messages" in sql for sql in statements)


def test_nested_tree_keeps_latest_task_attempt(db, chat):
  parent_id = chat.id
  physical = goal_run(
    db, id="goal-root", root_run_id="goal-root", chat_id=parent_id, status="running",
    goal_id="goal", goal_objective="Keep exact ownership",
  )
  db.add(physical)
  db.flush()
  old = _helper(db, parent_id, "old", status="failed")
  old.task_key = "audit"
  old.created_at = datetime(2026, 10, 2, 12)
  db.add(models.ChatRun(
    id="resumed-root", root_run_id="resumed-root", chat_id=parent_id, goal_id="goal",
    goal_objective="Keep exact ownership", status="running",
    started_at=datetime(2026, 10, 2, 13),
  ))
  current = _helper(
    db, parent_id, "current", status="completed", root_id="resumed-root",
  )
  current.task_key = "audit"
  current.created_at = old.created_at + timedelta(seconds=1)
  child = _helper(db, current.child_chat_id, "nested", status="resume_pending")
  db.commit()
  physical = db.get(models.ChatRun, "goal-root")
  root = db.get(models.ChatGoal, "goal")
  current_tree = goal_plans._delegation_tree(db, physical, root)
  assert [node["id"] for node in current_tree] == [current.id]
  assert current_tree[0]["status"] == "completed"
  assert current_tree[0]["children"][0]["id"] == child.id
  assert current_tree[0]["children"][0]["status"] == "resuming"
