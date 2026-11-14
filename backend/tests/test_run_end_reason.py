"""Every interrupted run says why it ended."""
import logging
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app import models
from app.chat_event_sink import _pause_note
from app.chat_writer import (
  ChatWriterActor, FinishRun, RecoverWedgedRun, StartTurn, create_chat,
)
from app.database import SessionLocal
from app.schema_migrations import _add_run_end_reason

APP = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def actor():
  a = ChatWriterActor(session_factory=SessionLocal)
  a.start()
  try:
    yield a
  finally:
    a.stop(timeout=5)


def _seed(chat_id, *runs):
  with SessionLocal() as db:
    db.add(create_chat(id=chat_id, title="t", messages=[], provider="claude"))
    for run_id, status in runs:
      db.add(models.ChatRun(id=run_id, chat_id=chat_id, status=status, provider="claude"))
    db.commit()


def _run(run_id):
  with SessionLocal() as db:
    run = db.get(models.ChatRun, run_id)
    return run.status, run.end_reason


def test_a_new_turn_records_that_it_superseded_an_open_run(actor):
  _seed("er-supersede", ("rt-old", "running"))
  actor.submit(StartTurn(
    chat_id="er-supersede", run_token="rt-new",
    user_msg={"role": "user", "content": "next", "ts": 1}, title_source="next",
  )).result(timeout=5)
  assert _run("rt-old") == ("interrupted", "superseded")
  assert _run("rt-new") == ("running", None)


def test_finish_records_the_given_reason_only_for_interruptions(actor):
  _seed("er-finish", ("rt-card", "running"), ("rt-done", "running"))
  actor.submit(FinishRun(
    chat_id="er-finish", run_token="rt-card",
    terminal_status="interrupted", end_reason="owner_input",
  )).result(timeout=5)
  actor.submit(FinishRun(
    chat_id="er-finish", run_token="rt-done",
    terminal_status="completed", end_reason="owner_input",
  )).result(timeout=5)
  assert _run("rt-card") == ("interrupted", "owner_input")
  assert _run("rt-done") == ("completed", None)


def test_recovery_of_a_wedged_run_is_labelled_recovered(actor):
  _seed("er-wedged", ("rt-wedged", "running"))
  actor.submit(RecoverWedgedRun(
    chat_id="er-wedged", run_token="rt-wedged",
    interruption_block=_pause_note("This response could not be saved."),
  )).result(timeout=5)
  assert _run("rt-wedged") == ("interrupted", "recovered")


def test_an_unexplained_interruption_is_logged(caplog):
  _seed("er-unknown", ("rt-unknown", "running"))
  with caplog.at_level(logging.WARNING, logger="app.models"):
    with SessionLocal() as db:
      db.get(models.ChatRun, "rt-unknown").status = "interrupted"
      db.commit()
  assert "rt-unknown interrupted without a known end_reason" in caplog.text


def test_older_reasonless_rows_can_change_without_a_warning(caplog):
  _seed("er-history", ("rt-history", "interrupted"))  # as if pre-0098
  caplog.clear()
  with caplog.at_level(logging.WARNING, logger="app.models"):
    with SessionLocal() as db:
      db.get(models.ChatRun, "rt-history").note_recovery_attempted = True
      db.commit()
  assert "rt-history" not in caplog.text


def test_every_reason_the_code_writes_is_in_the_shared_vocabulary():
  written = set()
  for path in APP.rglob("*.py"):
    source = path.read_text(encoding="utf-8")
    written.update(re.findall(r'end_reason\s*=\s*"([a-z_]+)"', source))
    written.update(re.findall(r'end_reason: str = "([a-z_]+)"', source))
    # Conditional reasons, e.g. end_reason=("a" if ... else "b").
    for expr in re.findall(r'end_reason=\(([^)]*)\)', source):
      written.update(re.findall(r'"([a-z_]+)"', expr))
  assert written, "no end_reason literals found"
  assert written <= models.RUN_END_REASONS, written - models.RUN_END_REASONS
  assert {"owner_input", "activation", "superseded", "server_restart"} <= written


def test_migration_adds_the_column_once_to_an_existing_database(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE chat_runs (id VARCHAR(64) PRIMARY KEY, status VARCHAR(16))"))
    conn.execute(text("INSERT INTO chat_runs VALUES ('old', 'interrupted')"))
  _add_run_end_reason(eng)
  _add_run_end_reason(eng)
  assert "end_reason" in {c["name"] for c in inspect(eng).get_columns("chat_runs")}
  with eng.connect() as conn:
    assert conn.execute(text("SELECT end_reason FROM chat_runs")).scalar() is None
