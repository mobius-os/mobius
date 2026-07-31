"""chat_runs lifecycle tests (077 Step 3) — delete, purge, and the orphan sweep.

These cover the gaps the expanded review surfaced: a soft-delete leaving a stale
"running" run record, a hard purge orphaning run records (no FK cascade on
SQLite), and the boot orphan sweep masking a destructive reconcile that failed.
"""

from datetime import UTC, datetime

from app import chat as chat_mod
from app import models
from app.chat_retention import purge_expired_chat_tombstones
from app.chat_writer import Barrier, get_writer
from app.database import SessionLocal
from app.timeutil import SOFT_DELETE_TTL


def _seed_chat(chat_id, *, messages=None, deleted_at=None):
  db = SessionLocal()
  try:
    c = models.Chat(
      id=chat_id, title="t", messages=messages or [], pending_messages=[],
      session_id="sess", provider="claude",
    )
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
    from app.run_state import has_running_run
    return None if c is None else {
      "running": has_running_run(db, chat_id), "deleted_at": c.deleted_at,
    }
  finally:
    db.close()


def _drain():
  get_writer().submit(Barrier()).result(timeout=5)


def test_soft_delete_closes_the_running_run_record(client, auth):
  """Deleting a chat closes its durable run instead of leaving it for boot."""
  _seed_chat("del-live")
  _seed_run("rt-del", "del-live", status="running")
  # Not in the registry, so is_chat_running is False — delete skips
  # stop_chat_for and reaches the tokenless FinishRun directly.
  r = client.delete("/api/chats/del-live", headers=auth)
  assert r.status_code == 204
  _drain()
  assert _runs("del-live")["rt-del"] != "running", "run record must be closed"
  state = _chat_state("del-live")
  assert state["running"] is False
  assert state["deleted_at"] is not None, "chat is soft-deleted"


def test_hard_purge_deletes_orphaned_run_records():
  """The tombstone lifecycle purge hard-deletes the Chat row; its run records must
  go with it (no FK cascade on SQLite) rather than orphaning + growing the
  table unbounded."""
  old = datetime.now(UTC).replace(tzinfo=None) - SOFT_DELETE_TTL - (
    SOFT_DELETE_TTL  # comfortably past the cutoff
  )
  _seed_chat("purge-me", deleted_at=old)
  _seed_run("rt-p1", "purge-me", status="completed")
  _seed_run("rt-p2", "purge-me", status="interrupted")
  db = SessionLocal()
  try:
    purge_expired_chat_tombstones(db)
  finally:
    db.close()
  assert _chat_state("purge-me") is None, "chat row hard-deleted"
  assert _runs("purge-me") == {}, "run records purged with the chat, not orphaned"


def _seed_session_link(provider, session_id, chat_id):
  db = SessionLocal()
  try:
    db.add(models.ChatSessionLink(
      provider=provider, session_id=session_id, chat_id=chat_id,
    ))
    db.commit()
  finally:
    db.close()


def _session_links(chat_id):
  db = SessionLocal()
  try:
    return [
      r.session_id for r in db.query(models.ChatSessionLink)
      .filter(models.ChatSessionLink.chat_id == chat_id).all()
    ]
  finally:
    db.close()


def test_hard_purge_deletes_session_links():
  """A chat's append-only session->chat link rows (subagent observability) must
  be purged with the chat — same no-FK-cascade lifecycle as chat_runs, else the
  table grows unbounded and the endpoint returns links to a dead chat."""
  old = datetime.now(UTC).replace(tzinfo=None) - SOFT_DELETE_TTL - SOFT_DELETE_TTL
  _seed_chat("purge-links", deleted_at=old)
  _seed_session_link("claude", "sess-purge-1", "purge-links")
  _seed_session_link("codex", "sess-purge-2", "purge-links")
  db = SessionLocal()
  try:
    purge_expired_chat_tombstones(db)
  finally:
    db.close()
  assert _chat_state("purge-links") is None, "chat row hard-deleted"
  assert _session_links("purge-links") == [], "session links purged with the chat"


def test_orphan_sweep_does_not_mask_a_failed_destructive_reconcile(monkeypatch):
  """A failed destructive reconcile must leave its run open for retry."""
  _seed_chat(
    "recon-fail",
    messages=[
      {"role": "user", "content": "hi", "ts": 1},
      {"role": "assistant", "blocks": [], "ts": 2},
    ],
  )
  _seed_run("rt-rf", "recon-fail", status="running")

  # Force the destructive per-chat finalize to raise so its branch rolls back,
  # leaving the durable run open.
  def _boom(_blocks):
    raise RuntimeError("simulated finalize failure")

  monkeypatch.setattr(chat_mod, "finalize_blocks", _boom)

  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()

  assert "recon-fail" not in reconciled, "destructive reconcile failed"
  assert _chat_state("recon-fail")["running"] is True
  assert _runs("recon-fail")["rt-rf"] == "running", (
    "orphan sweep must NOT flip a record whose chat is still authoritatively "
    "running — that would mask the failed reconcile"
  )
