"""Runtime liveness watchdog (`sweep_wedged_run_markers`) + limit classifier.

The sweep clears run markers orphaned by a completed-but-uncleared turn WITHOUT
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
from app.database import SessionLocal
from app.runner_registry import registry


def _drain_writer():
  get_writer().submit(Barrier()).result(timeout=5)


def _seed(chat_id, *, age_secs=200, run_status="running", pending=None,
          with_run=True):
  db = SessionLocal()
  try:
    started = None
    if run_status is not None:
      started = datetime.now(UTC).replace(tzinfo=None) - timedelta(
        seconds=age_secs
      )
    c = models.Chat(
      id=chat_id, title="t", messages=[], pending_messages=pending or [],
      session_id="sess", provider="claude",
      run_status=run_status, run_started_at=started,
    )
    db.add(c)
    if with_run and run_status is not None:
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
    return c.run_status, list(c.pending_messages or [])
  finally:
    db.close()


def _sweep():
  db = SessionLocal()
  try:
    return asyncio.run(chat_mod.sweep_wedged_run_markers(db))
  finally:
    db.close()


def test_sweep_clears_orphaned_marker_and_preserves_queue():
  _seed("wedged-1", age_secs=200, pending=[{"id": "p1", "ts": 1, "text": "hi"}])
  swept = _sweep()
  _drain_writer()
  assert "wedged-1" in swept
  status, pending = _state("wedged-1")
  assert status is None, "orphaned marker should be cleared"
  # The queue is preserved for the next-send stale-pending self-heal.
  assert len(pending) == 1


def test_sweep_skips_recent_turn():
  # Younger than the floor: a just-started turn whose state hasn't settled.
  _seed("recent-1", age_secs=5)
  swept = _sweep()
  _drain_writer()
  assert "recent-1" not in swept
  assert _state("recent-1")[0] == "running"


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
  # No ChatRun to identity-key the clear on — leave it for boot reconcile
  # rather than risk a tokenless clear wiping a racing fresh run.
  _seed("norun-1", age_secs=200, with_run=False)
  swept = _sweep()
  _drain_writer()
  assert "norun-1" not in swept
  assert _state("norun-1")[0] == "running"


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
