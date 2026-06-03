"""chat_runs lifecycle tests (077 Step 3) — delete, purge, and the orphan sweep.

These cover the gaps the expanded review surfaced: a soft-delete leaving a stale
"running" run record, a hard purge orphaning run records (no FK cascade on
SQLite), and the boot orphan sweep masking a destructive reconcile that failed.
"""

from datetime import UTC, datetime

from app import chat as chat_mod
from app import models
from app.chat_writer import Barrier, get_writer
from app.database import SessionLocal
from app.routes.chats import SOFT_DELETE_TTL


def _seed_chat(chat_id, *, messages=None, run_status=None, deleted_at=None):
  db = SessionLocal()
  try:
    c = models.Chat(
      id=chat_id, title="t", messages=messages or [], pending_messages=[],
      session_id="sess", provider="claude",
    )
    if run_status is not None:
      c.run_status = run_status
      c.run_started_at = datetime.now(UTC)
    if deleted_at is not None:
      c.deleted_at = deleted_at
    db.add(c)
    db.commit()
  finally:
    db.close()


def _seed_run(run_id, chat_id, status="running"):
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
  db = SessionLocal()
  try:
    return {
      r.id: r.status
      for r in db.query(models.ChatRun)
      .filter(models.ChatRun.chat_id == chat_id).all()
    }
  finally:
    db.close()


def _chat_state(chat_id):
  db = SessionLocal()
  try:
    c = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    return None if c is None else {
      "run_status": c.run_status, "deleted_at": c.deleted_at,
    }
  finally:
    db.close()


def _drain():
  get_writer().submit(Barrier()).result(timeout=5)


def test_soft_delete_closes_the_running_run_record(client, auth):
  """Deleting a chat that carries a live run marker closes its durable run
  record + clears run_status, instead of leaving a stale "running" record for
  the next boot sweep to mop up."""
  _seed_chat("del-live", run_status="running")
  _seed_run("rt-del", "del-live", status="running")
  # Not in the registry, so is_chat_running is False — delete skips
  # stop_chat_for and reaches the tokenless ClearRunStatus directly.
  r = client.delete("/api/chats/del-live", headers=auth)
  assert r.status_code == 204
  _drain()
  assert _runs("del-live")["rt-del"] != "running", "run record must be closed"
  state = _chat_state("del-live")
  assert state["run_status"] is None, "run_status must be cleared on delete"
  assert state["deleted_at"] is not None, "chat is soft-deleted"


def test_hard_purge_deletes_orphaned_run_records(client, auth):
  """The list_chats TTL purge hard-deletes the Chat row; its run records must
  go with it (no FK cascade on SQLite) rather than orphaning + growing the
  table unbounded."""
  old = datetime.now(UTC).replace(tzinfo=None) - SOFT_DELETE_TTL - (
    SOFT_DELETE_TTL  # comfortably past the cutoff
  )
  _seed_chat("purge-me", deleted_at=old)
  _seed_run("rt-p1", "purge-me", status="completed")
  _seed_run("rt-p2", "purge-me", status="interrupted")
  # Listing chats runs the purge sweep.
  r = client.get("/api/chats", headers=auth)
  assert r.status_code == 200
  assert _chat_state("purge-me") is None, "chat row hard-deleted"
  assert _runs("purge-me") == {}, "run records purged with the chat, not orphaned"


def test_orphan_sweep_does_not_mask_a_failed_destructive_reconcile(monkeypatch):
  """If a chat's destructive reconcile FAILS + rolls back (run_status stays
  "running"), the non-destructive orphan sweep must NOT flip its run record to
  "interrupted" — that would diverge the two signals and hide the failure from
  the next boot's destructive retry."""
  _seed_chat(
    "recon-fail",
    messages=[
      {"role": "user", "content": "hi", "ts": 1},
      {"role": "assistant", "blocks": [], "ts": 2},
    ],
    run_status="running",
  )
  _seed_run("rt-rf", "recon-fail", status="running")

  # Force the destructive per-chat finalize to raise so its branch rolls back,
  # leaving run_status=="running" (the failed-reconcile state).
  def _boom(_blocks):
    raise RuntimeError("simulated finalize failure")

  monkeypatch.setattr(chat_mod, "finalize_blocks", _boom)

  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()

  assert "recon-fail" not in reconciled, "destructive reconcile failed"
  assert _chat_state("recon-fail")["run_status"] == "running", (
    "failed reconcile leaves run_status running for next boot"
  )
  assert _runs("recon-fail")["rt-rf"] == "running", (
    "orphan sweep must NOT flip a record whose chat is still authoritatively "
    "running — that would mask the failed reconcile"
  )
