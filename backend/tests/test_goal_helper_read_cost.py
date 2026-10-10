"""Goal trees preserve helper truth without one latest-run read per node."""

from contextlib import contextmanager
import json
from datetime import datetime, timedelta

from app.chat_writer import create_chat
from app.transcript_rows import replace_all
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from app import delegations, goal_plans, models, transcript_rows
from app.database import Base
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
    _helper(db, parent_id, f"wide-{index}", status="running", goal_id="goal")
  db.commit()
  with SessionLocal() as cold:
    physical = cold.get(models.ChatRun, "goal-root")
    root = cold.get(models.ChatGoal, "goal")
    with _selects(cold) as statements:
      tree = goal_plans._delegation_tree(cold, physical, root)
    assert len(tree) == width
    assert {node["status"] for node in tree} == {"running"}
    # Direct children, descendant frontier, then statuses: immutable Goal
    # ownership no longer needs a run-root lookup.
    assert len(statements) == 3
    assert not any("chats.messages" in sql for sql in statements)


def test_nested_tree_keeps_latest_task_attempt_and_can_show_all_attempts(db, chat):
  parent_id = chat.id
  physical = goal_run(
    db, id="goal-root", root_run_id="goal-root", chat_id=parent_id, status="running",
    goal_id="goal", goal_objective="Keep exact ownership",
  )
  db.add(physical)
  db.flush()
  old = _helper(db, parent_id, "old", status="failed", goal_id="goal")
  old.task_key = "audit"
  old.created_at = datetime(2026, 10, 2, 12)
  db.add(models.ChatRun(
    id="resumed-root", root_run_id="resumed-root", chat_id=parent_id, goal_id="goal",
    goal_objective="Keep exact ownership", status="running",
    started_at=datetime(2026, 10, 2, 13),
  ))
  current = _helper(
    db, parent_id, "current", status="completed", root_id="resumed-root",
    goal_id="goal",
  )
  current.task_key = "audit"
  current.created_at = old.created_at + timedelta(seconds=1)
  child = _helper(db, current.child_chat_id, "nested", status="resume_pending",
                  goal_id="goal")
  db.commit()
  physical = db.get(models.ChatRun, "goal-root")
  root = db.get(models.ChatGoal, "goal")
  current_tree = goal_plans._delegation_tree(db, physical, root)
  assert [node["id"] for node in current_tree] == [current.id]
  assert current_tree[0]["status"] == "completed"
  assert current_tree[0]["children"][0]["id"] == child.id
  assert current_tree[0]["children"][0]["status"] == "resuming"
  all_tree = goal_plans._delegation_tree(db, physical, root, all_attempts=True)
  assert [node["id"] for node in all_tree] == [old.id, current.id]


@pytest.mark.parametrize('width', [1, 24])
@pytest.mark.parametrize('status', ['running', 'needs_review', 'needs_input'])
def test_per_turn_helper_context_batches_status_reads_and_keeps_questions(
  db, chat, width, status,
):
  parent_id = chat.id
  db.add(models.ChatRun(id='goal-root', root_run_id='goal-root',
                       chat_id=parent_id, provider='codex', status='running'))
  for index in range(width):
    key = f'context-{index}'
    row = _helper(db, parent_id, key,
                  status={'needs_review': 'failed', 'needs_input': 'completed'}.get(status, status))
    if status == 'needs_review':
      replace_all(db, db.get(models.Chat, row.child_chat_id), [{
        'id': f'run-{key}', 'role': 'assistant', 'blocks': [{'type': 'error',
        'message': delegations.REVIEW_REQUIRED_MARKER + ': Review retained work'}]}])
    if status == 'needs_input':
      db.add(models.DelegationQuestion(id=f'q-{key}', delegation_id=row.id,
          child_chat_id=row.child_chat_id, root_run_id=f'run-{key}',
          asking_run_id=f'run-{key}', answer_run_id=f'answer-{key}',
          question='Which option?', options_json=['one', 'two']))
  db.commit()
  with SessionLocal() as cold:
    with _selects(cold) as statements:
      items = delegations.own_helper_statuses(cold, parent_id, 'goal-root')
    assert len(items) == width
    assert {item['status'] for item in items} == {status}
    assert len(statements) <= (3 if status == 'running' else 4)
    if status != 'needs_review':
      assert not any('chats.messages' in statement for statement in statements)
    if status == 'needs_input':
      assert all(item['question']['question'] == 'Which option?' for item in items)


def test_batch_assistant_reader_prefers_unconverted_legacy_over_stale_rows(db, chat):
  converted = {'id': 'converted-run', 'role': 'assistant', 'result': 'Row report'}
  stale = {'id': 'legacy-run', 'role': 'assistant', 'result': 'Stale row report'}
  previous = {'id': 'legacy-run', 'role': 'assistant', 'result': 'Legacy report'}
  legacy_chat = create_chat(id='mixed-legacy', title='Legacy', messages=[stale])
  db.add(legacy_chat)
  db.flush()
  replace_all(db, chat, [converted])
  db.commit()
  # Simulate a previous-image write: the trigger removes this chat's marker,
  # making its new legacy bytes authoritative while its old rows remain.
  db.execute(text('UPDATE chats SET messages = :body WHERE id = :id'), {
    'body': json.dumps([previous]), 'id': legacy_chat.id,
  })
  db.commit()
  chat_id, legacy_id = chat.id, legacy_chat.id
  with _selects(db) as statements:
    bodies = transcript_rows.assistant_bodies_by_chat(db, [chat_id, legacy_id])
  assert bodies == {chat_id: [converted], legacy_id: [previous]}
  assert len(statements) == 1


def test_batch_assistant_reader_never_names_dropped_legacy_column(tmp_path):
  from app.schema_migrations import _add_transcript_rows, _create_chat_search_tables
  other = create_engine(f"sqlite:///{tmp_path / 'release2-helpers.db'}")
  Base.metadata.create_all(other)
  with other.begin() as conn:
    conn.exec_driver_sql('ALTER TABLE chats DROP COLUMN messages')
  _create_chat_search_tables(other)
  _add_transcript_rows(other)
  Session = sessionmaker(bind=other)
  with Session() as db:
    chat = create_chat(id='no-legacy-helper', title='Helper', messages=[
      {'id': 'run', 'role': 'assistant', 'result': 'Row report'},
    ])
    db.add(chat)
    db.commit()
    chat_id = chat.id
    with _selects(db) as statements:
      assert transcript_rows.assistant_bodies_by_chat(db, [chat_id]) == {
        chat_id: [{'id': 'run', 'role': 'assistant', 'result': 'Row report'}],
      }
    assert len(statements) == 1
    assert all('chats.messages' not in statement for statement in statements)
  other.dispose()
