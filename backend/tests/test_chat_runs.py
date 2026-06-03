"""Durable per-run record tests (persistence redesign 077 Step 3).

`chat_runs` is the per-turn successor to the single `Chat.run_status` column:
one row per turn keyed by run_token, dual-written with run_status (in the same
actor commit) so the two never diverge, closed terminal on a clean turn end and
marked interrupted by boot reconciliation when a process died mid-turn.

These drive the REAL `get_writer()` actor (the conftest `fresh_db` fixture
starts one bound to the test DB) and the real `reconcile_interrupted_chats`, so
they cover the wired dual-write + reconciliation maintenance, not a mock.
"""

from app import chat as chat_mod
from app import models
from app.chat_writer import (
  AppendPending, Barrier, ClearRunStatus, PromotePending, StartTurn, get_writer,
)
from app.database import SessionLocal


def _seed_chat(chat_id, messages=None, pending=None, run_status=None):
  from datetime import UTC, datetime

  db = SessionLocal()
  try:
    chat = models.Chat(
      id=chat_id, title="t",
      messages=messages if messages is not None else [],
      pending_messages=pending if pending is not None else [],
      session_id="sess", provider="claude",
    )
    if run_status is not None:
      chat.run_status = run_status
      chat.run_started_at = datetime.now(UTC)
    db.add(chat)
    db.commit()
  finally:
    db.close()
  return chat_id


def _seed_run(run_id, chat_id, status="running"):
  from datetime import UTC, datetime

  db = SessionLocal()
  try:
    db.add(models.ChatRun(
      id=run_id, chat_id=chat_id, status=status, provider="claude",
      started_at=datetime.now(UTC),
    ))
    db.commit()
  finally:
    db.close()


def _runs(chat_id):
  """Return {run_id: (status, ended_is_set)} for a chat's run records."""
  db = SessionLocal()
  try:
    rows = (
      db.query(models.ChatRun)
      .filter(models.ChatRun.chat_id == chat_id)
      .all()
    )
    return {r.id: (r.status, r.ended_at is not None) for r in rows}
  finally:
    db.close()


def _run_status(chat_id):
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    return chat.run_status
  finally:
    db.close()


def _drain():
  get_writer().submit(Barrier()).result(timeout=5)


def _start(chat_id, run_token):
  get_writer().submit(StartTurn(
    chat_id=chat_id, run_token=run_token,
    user_msg={"role": "user", "content": "hi", "ts": 1},
    title_source="hi", default_provider="claude",
  )).result(timeout=5)


# -- dual-write on start --------------------------------------------------
def test_start_turn_opens_a_running_run_record():
  _seed_chat("r1")
  _start("r1", "rt-1")
  _drain()
  runs = _runs("r1")
  assert runs == {"rt-1": ("running", False)}
  assert _run_status("r1") == "running"


# -- clean close ----------------------------------------------------------
def test_clear_run_status_completes_the_run_record():
  _seed_chat("r2")
  _start("r2", "rt-2")
  get_writer().submit(
    ClearRunStatus(chat_id="r2", run_token="rt-2")
  ).result(timeout=5)
  _drain()
  runs = _runs("r2")
  assert runs["rt-2"] == ("completed", True)
  assert _run_status("r2") is None


# -- continuation handoff -------------------------------------------------
def test_promote_closes_prior_run_and_opens_the_continuation():
  _seed_chat("r3")
  _start("r3", "rt-3a")
  # Queue a follow-up, then promote it as the continuation under a new token.
  get_writer().submit(AppendPending(
    chat_id="r3", run_token="rt-3a",
    user_msg={"role": "user", "content": "next", "ts": 2},
  )).result(timeout=5)
  get_writer().submit(
    PromotePending(chat_id="r3", run_token="rt-3b")
  ).result(timeout=5)
  _drain()
  runs = _runs("r3")
  # The prior run is closed completed; the continuation is the live record.
  assert runs["rt-3a"] == ("completed", True)
  assert runs["rt-3b"] == ("running", False)
  # The per-chat marker stays set across the handoff (the continuation runs).
  assert _run_status("r3") == "running"


# -- identity-keyed dying-run clear ---------------------------------------
def test_dying_run_clear_closes_own_record_but_keeps_successor_marker():
  """A fresh turn took the marker; the dying run's late tokened clear must
  not wipe the successor's run_status, and must not touch the successor's
  run record — only its own (which the fresh start already superseded)."""
  _seed_chat("r4")
  _start("r4", "rt-4a")          # rt-4a owns the marker
  _start("r4", "rt-4b")          # fresh turn supersedes: rt-4a → interrupted
  # The dying rt-4a now issues its late clear. owner is rt-4b, so the marker
  # is the successor's — the clear must no-op on it.
  get_writer().submit(
    ClearRunStatus(chat_id="r4", run_token="rt-4a")
  ).result(timeout=5)
  _drain()
  runs = _runs("r4")
  assert runs["rt-4a"][0] == "interrupted"  # superseded by the fresh start
  assert runs["rt-4b"] == ("running", False)  # successor untouched
  assert _run_status("r4") == "running", "successor's marker must survive"


# -- tokenless clear closes everything still running ----------------------
def test_tokenless_clear_closes_all_running_records():
  _seed_chat("r5")
  _start("r5", "rt-5")
  # A tokenless clear (Stop with no live handle / reconciliation handoff)
  # takes the chat idle and closes every still-running record.
  get_writer().submit(
    ClearRunStatus(chat_id="r5", run_token="")
  ).result(timeout=5)
  _drain()
  assert _runs("r5")["rt-5"] == ("completed", True)
  assert _run_status("r5") is None


# -- reconciliation maintains the record ----------------------------------
def test_reconcile_marks_interrupted_run_record():
  """An interrupted turn (run_status running + a running run record, no live
  registry entry) is reconciled: transcript finalized, marker cleared, and the
  run record moved to interrupted in the same pass."""
  _seed_chat(
    "r6", messages=[{"role": "user", "content": "hi", "ts": 1}],
    run_status="running",
  )
  _seed_run("rt-6", "r6", status="running")
  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "r6" in reconciled
  assert _runs("r6")["rt-6"][0] == "interrupted"
  assert _run_status("r6") is None


def test_reconcile_orphan_sweep_closes_record_without_run_status():
  """A run record left running whose run_status was already cleared (a dropped
  close, not an interruption) is closed by the non-destructive orphan sweep —
  the record converges, but the transcript is NOT touched (run_status, the
  authoritative trigger, said the chat was idle)."""
  _seed_chat(
    "r7", messages=[{"role": "user", "content": "hi", "ts": 1}],
    run_status=None,
  )
  _seed_run("rt-7", "r7", status="running")
  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()
  assert "r7" not in reconciled, "no destructive recovery without run_status"
  assert _runs("r7")["rt-7"][0] == "interrupted"
  # Transcript untouched: still the lone user message, no interruption note.
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == "r7").first()
    assert len(chat.messages) == 1
    assert chat.messages[0]["role"] == "user"
  finally:
    db.close()
