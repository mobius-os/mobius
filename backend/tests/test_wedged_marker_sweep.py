"""Finished-run marker recovery (`sweep_wedged_runs`) + limit classifier.

The sweep closes runs orphaned by a completed-but-unclosed turn WITHOUT
a process restart (a FAILED_LEAVE_MARKER terminal, or the late-promote gap),
which boot reconciliation would otherwise only fix on the next restart. It must
reap ONLY a definitively-finished turn — never a live turn, and never the
is_alive-false terminal window where `_complete_turn` is still finalizing (that
window is distinguished by a still-running broadcast).
"""

import asyncio
from datetime import UTC, datetime, timedelta

from app import chat as chat_mod
from app import models
from app.broadcast import create_broadcast
from app.chat_writer import Barrier, get_writer
from app.database import SessionLocal, engine
from app.runner_registry import registry
from sqlalchemy import event


def _drain_writer():
  get_writer().submit(Barrier()).result(timeout=5)


def _seed(chat_id, *, age_secs=200, pending=None,
          messages=None, live_assistant=None, with_run=True):
  db = SessionLocal()
  try:
    started = datetime.now(UTC).replace(tzinfo=None) - timedelta(
      seconds=age_secs
    )
    c = models.Chat(
      id=chat_id, title="t", messages=messages or [],
      live_assistant=live_assistant, pending_messages=pending or [],
      session_id="sess", provider="claude",
    )
    db.add(c)
    if with_run:
      db.add(models.ChatRun(
        id=f"rt-{chat_id}", chat_id=chat_id, status="running",
        provider="claude",
        started_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(
          seconds=age_secs
        ),
      ))
    db.commit()
  finally:
    db.close()


def _state(chat_id):
  db = SessionLocal()
  try:
    c = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    run = db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == chat_id,
    ).order_by(
      models.ChatRun.started_at.desc(),
      models.ChatRun.id.desc(),
    ).first()
    return (
      (
        run.status
        if run is not None and run.status in models.NONTERMINAL_RUN_STATUSES
        else None
      ),
      list(c.pending_messages or []),
      list(c.messages or []),
      c.live_assistant,
    )
  finally:
    db.close()


def _run_outcome(chat_id):
  db = SessionLocal()
  try:
    row = db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == chat_id,
    ).one()
    return row.status
  finally:
    db.close()


def _sweep():
  db = SessionLocal()
  try:
    return asyncio.run(chat_mod.sweep_wedged_runs(db))
  finally:
    db.close()


def test_sweep_recovers_orphaned_turn_and_preserves_queue():
  _seed(
    "wedged-1",
    age_secs=200,
    pending=[{"id": "p1", "ts": 1, "text": "hi"}],
    messages=[{"role": "user", "content": "help", "ts": 10}],
  )
  swept = _sweep()
  _drain_writer()
  assert "wedged-1" in swept
  status, pending, messages, live = _state("wedged-1")
  assert status is None
  assert _run_outcome("wedged-1") == "interrupted"
  assert live is None
  assert [message["role"] for message in messages] == ["user", "assistant"]
  assert messages[-1]["blocks"] == [{
    "type": "error",
    "message": "This response could not be saved. You can resume the turn.",
    "resumable": True,
  }]
  # The queue is preserved for the next-send stale-pending self-heal.
  assert len(pending) == 1


def test_sweep_materializes_live_reply_before_interruption_marker():
  _seed(
    "wedged-live",
    messages=[{"role": "user", "content": "help", "ts": 10}],
    live_assistant={
      "role": "assistant",
      "content": "Partial answer",
      "ts": 11,
      "blocks": [{"type": "text", "content": "Partial answer"}],
    },
  )
  assert "wedged-live" in _sweep()
  _drain_writer()
  status, _pending, messages, live = _state("wedged-live")
  assert status is None
  assert live is None
  assert messages[-1]["ts"] == 11
  assert messages[-1]["content"] == "Partial answer"
  assert [block["type"] for block in messages[-1]["blocks"]] == [
    "text", "error",
  ]


def test_sweep_keeps_unanswered_question_terminal_without_second_resume():
  _seed(
    "wedged-question",
    messages=[{"role": "user", "content": "choose", "ts": 10}],
    live_assistant={
      "role": "assistant",
      "content": "",
      "ts": 11,
      "blocks": [{
        "type": "question",
        "id": "direction",
        "question": "Which direction?",
        "options": [{"label": "A"}, {"label": "B"}],
      }],
    },
  )
  assert "wedged-question" in _sweep()
  _drain_writer()
  _status, _pending, messages, _live = _state("wedged-question")
  blocks = messages[-1]["blocks"]
  assert [block["type"] for block in blocks] == ["error", "question"]
  assert "resumable" not in blocks[0]
  assert blocks[1]["id"] == "direction"


def test_sweep_skips_recent_turn():
  # Younger than the floor: a just-started turn whose state hasn't settled.
  _seed("recent-1", age_secs=5)
  swept = _sweep()
  _drain_writer()
  assert "recent-1" not in swept
  assert _state("recent-1")[0] == "running"


def test_wedged_candidate_query_does_not_load_transcripts():
  _seed("wedged-projection", age_secs=200)
  db = SessionLocal()
  try:
    chat = db.get(models.Chat, "wedged-projection")
    chat.messages = [{"role": "assistant", "content": "x" * 1_000_000}]
    db.commit()
  finally:
    db.close()

  statements = []

  def capture(_conn, _cursor, statement, _parameters, _context, _many):
    if (
      "FROM chat_runs JOIN chats" in statement
      and "chat_runs.started_at <" in statement
    ):
      statements.append(statement)

  event.listen(engine, "before_cursor_execute", capture)
  try:
    _sweep()
  finally:
    event.remove(engine, "before_cursor_execute", capture)
  _drain_writer()

  assert len(statements) == 1
  projection = statements[0].split("FROM chat_runs", 1)[0]
  assert "chat_runs.id" in projection
  assert "chats.messages" not in projection


def test_sweep_skips_live_turn():
  _seed("live-1", age_secs=200)
  registry.mark_starting("live-1")  # is_alive() -> True
  try:
    swept = _sweep()
  finally:
    registry.reset_for_tests()
  _drain_writer()
  assert "live-1" not in swept
  assert _state("live-1")[0] == "running"


def test_sweep_skips_turn_with_running_broadcast():
  # The is_alive-false-but-still-finalizing terminal window: _complete_turn has
  # not yet called bc.mark_completed(), so the broadcast is still running.
  _seed("finalizing-1", age_secs=200)
  bc = create_broadcast("finalizing-1")
  assert bc.running
  try:
    swept = _sweep()
  finally:
    bc.mark_completed()
  _drain_writer()
  assert "finalizing-1" not in swept
  assert _state("finalizing-1")[0] == "running"


def test_sweep_reaps_after_broadcast_completed():
  # Same chat once its broadcast has completed (turn truly done) is reaped.
  _seed("done-bc-1", age_secs=200)
  bc = create_broadcast("done-bc-1")
  bc.mark_completed()
  swept = _sweep()
  _drain_writer()
  assert "done-bc-1" in swept
  assert _state("done-bc-1")[0] is None


def test_sweep_skips_when_no_run_record():
  # No durable run means there is no recovery candidate.
  _seed("norun-1", age_secs=200, with_run=False)
  swept = _sweep()
  _drain_writer()
  assert "norun-1" not in swept
  assert _state("norun-1")[0] is None


def test_limit_error_text_classifier():
  f = chat_mod._is_limit_error_text
  assert f("Error: rate limit exceeded")
  assert f("usage limit reached, resets at ...")
  assert f("HTTP 429 Too Many Requests")
  assert f("model overloaded, try again")
  assert f("quota exceeded")
  assert not f("some ordinary failure")
  assert not f("connection reset by peer")
  assert not f(None)
  assert not f("")


def test_limit_classifier_matches_real_prod_strings():
  # The actual Anthropic limit strings seen in prod chat.log — bug C is
  # pointless if these don't classify (they lack the bare "rate limit" marker).
  f = chat_mod._is_limit_error_text
  assert f("You've hit your weekly limit · resets Jul 4, 3am (UTC)")
  assert f("You've hit your session limit · resets 2:20am (UTC)")
  assert f(
    "API Error: Server is temporarily limiting requests "
    "(not your usage limit) · Rate limited"
  )
  # A generic error that merely mentions "limit" but isn't a rate/usage kill
  # must NOT park (no reset window, no rate/usage/weekly/session marker).
  assert not f("ValueError: list index out of range (limit check)")
  assert not f("Execution interrupted.")


def test_limit_terminal_classifier_uses_api_error_status():
  f = chat_mod._is_limit_terminal
  assert f({"api_error_status": 429, "error": None})
  assert f({"api_error_status": None, "error": "rate limit hit"})
  assert not f({"api_error_status": 200, "error": "some other error"})
  assert not f({"error": None})
