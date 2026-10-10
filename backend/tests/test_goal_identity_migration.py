"""0086 keeps legacy identities and attributes only provable ownership."""
from contextlib import contextmanager
import sqlite3
from time import perf_counter

import pytest

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError, OperationalError

from app import models
from app.schema_migrations import _goal_execution_identity


@contextmanager
def _sqlite_statements(eng):
  """Observe DBAPI statements, including raw SQL and each executemany step."""
  statements = []

  def start(dbapi, _record, _proxy):
    dbapi.set_trace_callback(statements.append)

  def stop(dbapi, _record):
    if dbapi is not None:
      dbapi.set_trace_callback(None)

  event.listen(eng, "checkout", start)
  event.listen(eng, "checkin", stop)
  try:
    yield statements
  finally:
    event.remove(eng, "checkout", start)
    event.remove(eng, "checkin", stop)


def _db(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'identity.db'}")
  with eng.begin() as db:
    db.execute(text("""CREATE TABLE chat_goals (
      id TEXT PRIMARY KEY, chat_id TEXT, status TEXT)"""))
    db.execute(text("""CREATE TABLE chat_runs (
      id TEXT PRIMARY KEY, root_run_id TEXT, chat_id TEXT, goal_id TEXT)"""))
    db.execute(text("""CREATE TABLE delegations (
      id TEXT PRIMARY KEY, parent_chat_id TEXT, child_chat_id TEXT,
      parent_root_run_id TEXT NOT NULL, task_key TEXT NOT NULL,
      app_id INTEGER, source_work_id TEXT,
      CONSTRAINT uq_delegations_parent_root_task UNIQUE (parent_root_run_id, task_key))"""))
    db.execute(text("CREATE INDEX ix_delegations_child ON delegations (child_chat_id)"))
    db.execute(text("""CREATE TABLE chat_waits (
      id TEXT PRIMARY KEY, chat_id TEXT, created_by_run_id TEXT,
      kind TEXT, goal_id TEXT, root_run_id TEXT)"""))
    db.execute(text("""INSERT INTO chat_goals VALUES
      ('A','owner','completed'),('B','owner','open'),
      ('C','owner','completed'),('N','other','open')"""))
    db.execute(text("""INSERT INTO chat_runs VALUES
      ('runA','rootA','owner','A'),('runB','shared','owner','B'),
      ('runN','nested','child','N'),('runC','rootC','owner','C'),
      ('runAmb1','amb','owner','A'),
      ('runAmb2','amb','owner','B')"""))
    db.execute(text("""INSERT INTO delegations VALUES
      ('direct','owner','child','A','task',NULL,NULL),
      ('nested','child','grandchild','nested','task',NULL,NULL),
      ('ambiguous','owner','amb-child','amb','task',NULL,NULL),
      ('source','owner','source-child','A','other',NULL,'source-id'),
      ('app','owner','app-child','A','app',1,NULL),
      ('cycle1','cycle-two','cycle-one','none1','task',NULL,NULL),
      ('cycle2','cycle-one','cycle-two','none2','task',NULL,NULL)"""))
    db.execute(text("""INSERT INTO chat_waits VALUES
      ('ordinary','owner','runA','timer',NULL,NULL),
      ('activation','owner','runA','platform_activation','B','preserved'),
      ('already','owner','runA','command','B','preserved')"""))
  return eng


def test_0086_backfill_retry_and_null_safe_uniqueness(tmp_path):
  eng = _db(tmp_path)
  _goal_execution_identity(eng)
  _goal_execution_identity(eng)
  columns = {c['name'] for c in inspect(eng).get_columns('delegations')}
  assert 'goal_id' in columns
  with eng.begin() as db:
    ownership = dict(db.execute(text("SELECT id, goal_id FROM delegations")).all())
    assert ownership['direct'] == 'A'
    assert ownership['nested'] == 'A'
    assert all(ownership[name] is None for name in
               ('ambiguous', 'source', 'app', 'cycle1', 'cycle2'))
    assert db.execute(text("SELECT completion_run_id FROM chat_goals WHERE id='A'" )).scalar() is None
    assert db.execute(text("SELECT completion_run_id FROM chat_goals WHERE id='C'" )).scalar() == 'runC'
    assert db.execute(text("SELECT goal_id, root_run_id FROM chat_waits WHERE id='ordinary'" )).one() == ('A', 'rootA')
    assert db.execute(text("SELECT goal_id, root_run_id FROM chat_waits WHERE id='activation'" )).one() == ('B', 'preserved')
    assert db.execute(text("SELECT goal_id, root_run_id FROM chat_waits WHERE id='already'" )).one() == ('B', 'preserved')
    assert 'ix_delegations_child' in {i['name'] for i in inspect(eng).get_indexes('delegations')}
    db.execute(text("""INSERT INTO delegations VALUES
      ('new-goal','owner','fresh','A','task',NULL,NULL,'B')"""))
    try:
      db.execute(text("""INSERT INTO delegations VALUES
        ('duplicate','owner','dup','A','task',NULL,NULL,'A')"""))
    except IntegrityError:
      pass
    else:
      raise AssertionError('same Goal/root/task must remain unique')
    try:
      db.execute(text("""INSERT INTO delegations VALUES
        ('duplicate-null','owner','dup-null','amb','task',NULL,NULL,NULL)"""))
    except IntegrityError:
      pass
    else:
      raise AssertionError('Goal-less root/task must remain unique')


def test_0086_fresh_schema_is_already_current(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
  models.Base.metadata.create_all(eng)
  before = {i['name'] for i in inspect(eng).get_indexes('delegations')}
  _goal_execution_identity(eng)
  after = {i['name'] for i in inspect(eng).get_indexes('delegations')}
  assert before == after
  assert {'uq_delegations_goal_root_task',
          'uq_delegations_no_goal_root_task'} <= after


def test_0086_retry_does_not_adopt_a_new_goal_or_rewrite_answer_identities(tmp_path):
  eng = _db(tmp_path)
  with eng.begin() as db:
    db.execute(text("INSERT INTO chat_runs VALUES ('plain','plain','owner',NULL)"))
    db.execute(text("INSERT INTO chat_waits VALUES ('plain-wait','owner','plain','timer',NULL,NULL)"))
    db.execute(text("""CREATE TABLE delegation_questions (
      id TEXT PRIMARY KEY, delegation_id TEXT REFERENCES delegations(id),
      root_run_id TEXT, answer_run_id TEXT, answer_cid TEXT)"""))
    db.execute(text("""INSERT INTO delegation_questions VALUES
      ('question-id','direct','asking-root','reserved-answer-run','reserved-answer-cid')"""))
  _goal_execution_identity(eng)
  with eng.begin() as db:
    assert db.execute(text("SELECT goal_id, root_run_id FROM chat_waits WHERE id='plain-wait'")).one() == (None, 'plain')
    db.execute(text("UPDATE chat_runs SET goal_id='B' WHERE id='plain'"))
    before = db.execute(text("SELECT * FROM delegation_questions")).all()
  _goal_execution_identity(eng)
  with eng.connect() as db:
    assert db.execute(text("SELECT goal_id, root_run_id FROM chat_waits WHERE id='plain-wait'")).one() == (None, 'plain')
    assert db.execute(text("SELECT * FROM delegation_questions")).all() == before
    assert db.execute(text("PRAGMA foreign_key_check")).all() == []
  eng.dispose()


def test_0086_snapshot_keeps_old_and_new_unowned_helpers_unowned(tmp_path):
  eng = _db(tmp_path)
  with eng.begin() as db:
    db.execute(text("INSERT INTO chat_runs VALUES ('plain','plain','owner',NULL)"))
    db.execute(text("""INSERT INTO delegations VALUES
      ('old-unowned','owner','old-child','plain','old',NULL,NULL)"""))
  _goal_execution_identity(eng)
  with eng.begin() as db:
    assert db.execute(text(
      "SELECT goal_id FROM goal_identity_0086_snapshot "
      "WHERE delegation_id='old-unowned'"
    )).scalar() is None
    db.execute(text("""INSERT INTO delegations VALUES
      ('new-unowned','owner','new-child','plain','new',NULL,NULL,NULL)"""))
    db.execute(text("""INSERT INTO chat_waits VALUES
      ('new-wait','owner','plain','timer',NULL,NULL)"""))
    db.execute(text("UPDATE chat_runs SET goal_id='B' WHERE id='plain'"))
  _goal_execution_identity(eng)
  with eng.connect() as db:
    assert db.execute(text(
      "SELECT id, goal_id FROM delegations WHERE id IN "
      "('old-unowned','new-unowned') ORDER BY id"
    )).all() == [('new-unowned', None), ('old-unowned', None)]
    assert db.execute(text(
      "SELECT delegation_id, goal_id FROM goal_identity_0086_snapshot "
      "WHERE delegation_id IN ('old-unowned','new-unowned') "
      "ORDER BY delegation_id"
    )).all() == [('new-unowned', None), ('old-unowned', None)]
    assert db.execute(text(
      "SELECT goal_id, root_run_id FROM chat_waits WHERE id='new-wait'"
    )).one() == (None, None)


def test_0086_backfill_large_shape_has_bounded_queries_and_time(tmp_path):
  eng = _db(tmp_path)
  with eng.begin() as db:
    db.execute(text("INSERT INTO chat_goals VALUES (:id,'owner','completed')"),
               [{"id": f"g{i}"} for i in range(10_000)])
    db.execute(text("INSERT INTO chat_runs VALUES (:id,:root,'owner',:goal)"),
               [{"id": f"r{i}", "root": f"root{i}",
                 "goal": f"g{i}" if i < 10_000 else None} for i in range(50_000)])
    db.execute(text("""INSERT INTO delegations VALUES
      (:id,'owner',:child,:goal,'worker',NULL,NULL)"""),
               [{"id": f"helper{i}", "child": f"child{i}", "goal": f"g{i}"}
                for i in range(10_000)])
  with _sqlite_statements(eng) as statements:
    started = perf_counter()
    _goal_execution_identity(eng)
    elapsed = perf_counter() - started
  # Trace the real SQL boundary, including schema rebuild and both 10k-row
  # writes: SQLAlchemy's executemany callback hides per-row statements.
  assert elapsed < 5, f"0086 large-shaped backfill took {elapsed:.2f}s"
  assert len(statements) < 100, f"0086 executed {len(statements)} SQLite statements"
  with eng.connect() as db:
    assert db.execute(text(
      "SELECT COUNT(*) FROM chat_goals WHERE id LIKE 'g%' AND completion_run_id IS NOT NULL"
    )).scalar() == 10_000
    assert db.execute(text(
      "SELECT COUNT(*) FROM delegations WHERE id LIKE 'helper%' AND goal_id IS NOT NULL"
    )).scalar() == 10_000


def test_0086_snapshot_marker_cannot_collide_with_historical_helper_ids(tmp_path):
  eng = _db(tmp_path)
  with eng.begin() as db:
    db.execute(text("""INSERT INTO delegations VALUES
      ('__complete__','owner','marker-child','A','marker',NULL,NULL),
      ('0086','owner','number-child','A','number',NULL,NULL)"""))
  _goal_execution_identity(eng)
  _goal_execution_identity(eng)
  with eng.connect() as db:
    assert db.execute(text(
      "SELECT id, goal_id FROM delegations WHERE id IN ('__complete__','0086') ORDER BY id"
    )).all() == [('0086', 'A'), ('__complete__', 'A')]
    assert db.execute(text(
      "SELECT record_kind, delegation_id, goal_id FROM goal_identity_0086_snapshot "
      "WHERE delegation_id IN ('__complete__','0086') ORDER BY record_kind, delegation_id"
    )).all() == [('delegation', '0086', 'A'), ('delegation', '__complete__', 'A'),
                ('migration', '0086', None)]


def test_0086_backfill_deep_chain_without_recursion(tmp_path):
  eng = _db(tmp_path)
  with eng.begin() as db:
    db.execute(text("""INSERT INTO delegations
      (id,parent_chat_id,child_chat_id,parent_root_run_id,task_key,app_id,source_work_id)
      VALUES (:id,:parent,:child,:root,'chain',NULL,NULL)"""), [
        {"id": f"chain{i}", "parent": "child" if i == 0 else f"chain-chat{i-1}",
         "child": f"chain-chat{i}", "root": f"chain-root{i}"}
        for i in range(2500)
      ])
  _goal_execution_identity(eng)
  with eng.connect() as db:
    assert db.execute(text(
      "SELECT goal_id FROM delegations WHERE id='chain2499'"
    )).scalar() == 'A'


@pytest.mark.parametrize('failure_at', ['DROP TABLE delegations', 'UPDATE chat_waits',
                                      'CREATE INDEX IF NOT EXISTS ix_chat_goals_completion_run_id'])
def test_0086_failure_rolls_back_schema_copy_ownership_and_snapshot(tmp_path, failure_at):
  eng = _db(tmp_path)
  with eng.connect() as db:
    db.exec_driver_sql('PRAGMA foreign_keys=ON')
    db.commit()
    before = db.execute(text('SELECT * FROM delegations ORDER BY id')).all()

  def fail(_conn, _cursor, statement, _parameters, _context, _many):
    if statement.startswith(failure_at):
      raise RuntimeError('injected migration failure')

  event.listen(eng, 'before_cursor_execute', fail)
  try:
    with _sqlite_statements(eng) as statements:
      with pytest.raises(RuntimeError, match='injected migration failure'):
        _goal_execution_identity(eng)
  finally:
    event.remove(eng, 'before_cursor_execute', fail)
  assert 'ROLLBACK' in statements
  assert statements.count('BEGIN IMMEDIATE') == 1
  assert 'COMMIT' not in statements
  with eng.connect() as db:
    assert db.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1
    assert db.execute(text('SELECT * FROM delegations ORDER BY id')).all() == before
    assert 'goal_id' not in {c['name'] for c in inspect(db).get_columns('delegations')}
    assert 'completion_run_id' not in {c['name'] for c in inspect(db).get_columns('chat_goals')}
    assert not {'goal_identity_0086_snapshot', 'delegations__0086'} & set(inspect(db).get_table_names())
    assert db.exec_driver_sql("SELECT name FROM sqlite_temp_master WHERE type='table'").all() == []
    assert db.execute(text("SELECT goal_id, root_run_id FROM chat_waits WHERE id='ordinary'")).one() == (None, None)
    assert 'uq_delegations_parent_root_task' in db.exec_driver_sql(
      "SELECT sql FROM sqlite_master WHERE name='delegations'").scalar()
  # Retry from the rolled-back legacy schema still establishes correct ownership.
  _goal_execution_identity(eng)
  with eng.connect() as db:
    assert db.execute(text("SELECT goal_id FROM delegations WHERE id='nested'")).scalar() == 'A'
    assert db.exec_driver_sql('PRAGMA foreign_keys').scalar() == 1


def test_0086_writer_lock_precedes_schema_and_source_reads_and_commit_is_atomic(tmp_path):
  eng = _db(tmp_path)
  checked = []
  writer = sqlite3.connect(tmp_path / 'identity.db', timeout=0)

  def assert_locked(conn, _cursor, statement, _parameters, _context, _many):
    if statement.startswith('ALTER TABLE chat_goals') or statement.startswith('SELECT id, chat_id, status FROM chat_goals'):
      assert conn.connection.driver_connection.in_transaction
      with pytest.raises(sqlite3.OperationalError, match='locked'):
        writer.execute("UPDATE chat_runs SET goal_id='B' WHERE id='runA'")
      writer.rollback()
      checked.append(statement)

  event.listen(eng, 'before_cursor_execute', assert_locked)
  try:
    with _sqlite_statements(eng) as statements:
      _goal_execution_identity(eng)
  finally:
    event.remove(eng, 'before_cursor_execute', assert_locked)
  assert len(checked) == 2
  assert statements.count('BEGIN IMMEDIATE') == 1
  assert statements.count('COMMIT') == 1
  begin = statements.index('BEGIN IMMEDIATE')
  commit = statements.index('COMMIT')
  for index, statement in enumerate(statements):
    if statement.startswith(('SELECT', 'ALTER', 'CREATE', 'INSERT', 'DROP', 'UPDATE', 'WITH')):
      assert begin < index < commit
  writer.execute("UPDATE chat_runs SET goal_id='B' WHERE id='runA'")
  writer.commit()
  writer.close()
  _goal_execution_identity(eng)
  with eng.connect() as db:
    assert db.execute(text("SELECT goal_id FROM delegations WHERE id='nested'")).scalar() == 'A'


def test_0086_existing_writer_blocks_before_any_schema_or_snapshot_read(tmp_path):
  eng = _db(tmp_path)
  with eng.connect() as db:
    db.exec_driver_sql('PRAGMA busy_timeout=0')
    db.commit()
  writer = sqlite3.connect(tmp_path / 'identity.db', timeout=0)
  writer.execute('BEGIN IMMEDIATE')
  try:
    with _sqlite_statements(eng) as statements:
      with pytest.raises(OperationalError, match='locked'):
        _goal_execution_identity(eng)
    assert not any(s.startswith(('SELECT', 'ALTER', 'CREATE', 'INSERT', 'UPDATE', 'DROP')) for s in statements)
  finally:
    writer.rollback()
    writer.close()
  _goal_execution_identity(eng)


def test_0086_retry_does_not_load_or_index_mutable_source_tables(tmp_path):
  eng = _db(tmp_path)
  _goal_execution_identity(eng)
  with _sqlite_statements(eng) as statements:
    _goal_execution_identity(eng)
  assert not any(s.startswith(('SELECT id, chat_id, status FROM chat_goals',
                              'SELECT id, root_run_id, chat_id, goal_id FROM chat_runs',
                              'SELECT id, parent_chat_id')) for s in statements)
  assert len(statements) < 100
  assert statements.count('BEGIN IMMEDIATE') == statements.count('COMMIT') == 1
