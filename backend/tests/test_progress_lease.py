"""ProgressLease regime + renewal logic (no real DB writes)."""

import types
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import database, models, progress_lease
from app.progress_lease import (
  MODEL_IDLE_TTL,
  SUSPEND_TTL,
  TOOL_TTL,
  ProgressLease,
  lease_expired,
)


@pytest.fixture
def writes(monkeypatch):
  """Capture lease writes instead of touching SQLite."""
  captured = []
  monkeypatch.setattr(
    progress_lease, "_write_lease",
    lambda chat_id, deadline: captured.append((chat_id, deadline)),
  )
  return captured


def _tool_use(tool_use_id, name):
  return types.SimpleNamespace(id=tool_use_id, name=name)


def _tool_result(tool_use_id):
  return types.SimpleNamespace(tool_use_id=tool_use_id)


def _msg(*blocks):
  return types.SimpleNamespace(content=list(blocks))


def test_default_regime_is_model_idle(writes):
  lease = ProgressLease("c1")
  assert lease.current_ttl() == MODEL_IDLE_TTL


def test_outstanding_cli_tool_uses_tool_ttl(writes):
  lease = ProgressLease("c1")
  lease.note_message(_msg(_tool_use("t1", "Bash")), is_root=True)
  assert lease.current_ttl() == TOOL_TTL
  # Result clears it back to model-idle.
  lease.note_message(_msg(_tool_result("t1")), is_root=True)
  assert lease.current_ttl() == MODEL_IDLE_TTL


def test_long_tool_suspends_lease(writes):
  lease = ProgressLease("c1")
  lease.note_message(_msg(_tool_use("t1", "TaskOutput")), is_root=True)
  assert lease.current_ttl() == SUSPEND_TTL
  lease.note_message(_msg(_tool_result("t1")), is_root=True)
  assert lease.current_ttl() == MODEL_IDLE_TTL


def test_long_tool_wins_over_concurrent_cli_tool(writes):
  lease = ProgressLease("c1")
  lease.note_message(
    _msg(_tool_use("t1", "Bash"), _tool_use("t2", "TaskOutput")), is_root=True,
  )
  assert lease.current_ttl() == SUSPEND_TTL
  # Bash returns first: still suspended by the outstanding long tool.
  lease.note_message(_msg(_tool_result("t1")), is_root=True)
  assert lease.current_ttl() == SUSPEND_TTL
  # Long tool returns: back to model-idle.
  lease.note_message(_msg(_tool_result("t2")), is_root=True)
  assert lease.current_ttl() == MODEL_IDLE_TTL


def test_non_root_messages_do_not_change_regime(writes):
  lease = ProgressLease("c1")
  # A subagent sidechain tool_use must not set the ROOT regime.
  lease.note_message(_msg(_tool_use("t1", "TaskOutput")), is_root=False)
  assert lease.current_ttl() == MODEL_IDLE_TTL


def test_start_forces_a_write_then_throttles(writes):
  lease = ProgressLease("c1")
  lease.start()
  assert len(writes) == 1
  # A second progress message within the throttle window does not write again.
  lease.note_message(_msg(), is_root=True)
  assert len(writes) == 1


def test_start_writes_model_idle_deadline(writes):
  before = datetime.now(UTC).replace(tzinfo=None)
  lease = ProgressLease("c1")
  lease.start()
  _chat_id, deadline = writes[-1]
  expected = before + timedelta(seconds=MODEL_IDLE_TTL)
  # Allow a small execution delta.
  assert abs((deadline - expected).total_seconds()) < 5


def test_floor_ttl_raises_model_and_tool_regimes(writes):
  # Codex path: no tool parsing, so a floor keeps a silent tool from tripping.
  lease = ProgressLease("c1", floor_ttl=TOOL_TTL)
  assert lease.current_ttl() == TOOL_TTL  # model-idle floored up
  # A floor below a regime TTL never lowers it.
  low = ProgressLease("c2", floor_ttl=1.0)
  assert low.current_ttl() == MODEL_IDLE_TTL



def test_lease_write_never_moves_deadline_backwards(tmp_path, monkeypatch):
  engine = create_engine(f"sqlite:///{tmp_path / 'lease.db'}")
  models.Base.metadata.create_all(engine)
  sessions = sessionmaker(bind=engine)
  monkeypatch.setattr(database, "SessionLocal", sessions)
  now = datetime.now(UTC).replace(tzinfo=None)
  with sessions() as db:
    db.add(models.Chat(id="c1", title="lease"))
    db.add(models.ChatRun(
      id="rt-c1", chat_id="c1", status="running",
      progress_expires_at=now + timedelta(minutes=10),
    ))
    db.commit()
  progress_lease._write_lease("c1", now + timedelta(minutes=5))
  with sessions() as db:
    assert db.get(models.ChatRun, "rt-c1").progress_expires_at == now + timedelta(minutes=10)
  progress_lease._write_lease("c1", now + timedelta(minutes=15))
  with sessions() as db:
    assert db.get(models.ChatRun, "rt-c1").progress_expires_at == now + timedelta(minutes=15)


def test_lease_expired_helper():
  now = datetime.now(UTC).replace(tzinfo=None)
  past = types.SimpleNamespace(progress_expires_at=now - timedelta(seconds=1))
  future = types.SimpleNamespace(progress_expires_at=now + timedelta(seconds=60))
  null = types.SimpleNamespace(progress_expires_at=None)
  assert lease_expired(past, now)
  assert not lease_expired(future, now)
  # NULL lease is never "expired" — the caller keeps its legacy fallback.
  assert not lease_expired(null, now)
