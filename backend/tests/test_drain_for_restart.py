"""Drain-gated restart (design §2.2, DrainForRestart path).

Locks in the four contracts that distinguish a restart-drain from a Stop:

  (a) DrainForRestart PRESERVES pending_messages + the run marker, while
      stop_chat_for CLEARS the queue (contrast).
  (b) The drain persists the "paused for a platform update" note WITHOUT losing
      the accumulated partial blocks.
  (c) Boot reconcile marks the paused note resumable (no double note) and the
      boot notify fires exactly once.
  (d) A send arriving while draining QUEUES instead of starting a turn.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from app import chat as chat_mod
from app import models
from app.broadcast import create_broadcast
from app.chat_writer import Barrier, get_writer
from app.database import SessionLocal
from app.runner_registry import RunnerKind, registry


class _Handle:
  def __init__(self, chat_id: str, *, stops: bool = True):
    self.chat_id = chat_id
    self.kind = RunnerKind.CLAUDE_SDK
    self.stop_calls = 0
    self._stops = stops

  async def stop(self, timeout: float = 2.0) -> bool:
    del timeout
    self.stop_calls += 1
    if self._stops:
      registry.unregister(self.chat_id, self.kind)
    return self._stops


def _drain_writer():
  get_writer().submit(Barrier()).result(timeout=5)


def _seed(chat_id: str, *, pending=None, messages=None, run_status="running"):
  db = SessionLocal()
  try:
    started = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=30)
    db.add(models.Chat(
      id=chat_id,
      title="t",
      messages=messages or [{"role": "user", "content": "do work", "ts": 1}],
      pending_messages=pending or [],
      session_id="sess",
      provider="claude",
      run_status=run_status,
      run_started_at=started if run_status else None,
    ))
    if run_status == "running":
      db.add(models.ChatRun(
        id=f"rt-{chat_id}",
        chat_id=chat_id,
        status="running",
        provider="claude",
        started_at=started,
      ))
    db.commit()
  finally:
    db.close()


def _chat(chat_id: str):
  db = SessionLocal()
  try:
    row = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    return {
      "messages": list(row.messages or []),
      "pending": list(row.pending_messages or []),
      "run_status": row.run_status,
    }
  finally:
    db.close()


def _live_turn(chat_id: str, *, pending=None, partial="partial answer"):
  """A live turn with a registered handle + sink, mid-stream (a real partial
  accumulated INTO the sink so the drain's finalize can preserve it)."""
  _seed(chat_id, pending=pending)
  bc = create_broadcast(chat_id)
  sink = chat_mod._ChatEventSink(bc, chat_id, run_token=f"rt-{chat_id}")
  chat_mod.register_active_sink(chat_id, sink)
  if partial:
    sink.publish({"type": "text", "content": partial})
  handle = _Handle(chat_id)
  registry.register(handle)
  return bc, sink, handle


def _run_drain():
  return asyncio.run(chat_mod.drain_all_for_restart())


# -- (b) the drain persists the paused note + preserves partials --------------

def test_drain_persists_paused_note_and_preserves_partials():
  _, _, handle = _live_turn("drain-note-1")

  drained = _run_drain()
  _drain_writer()

  assert drained == ["drain-note-1"]
  assert handle.stop_calls == 1
  state = _chat("drain-note-1")
  blocks = state["messages"][-1]["blocks"]
  # The streamed partial survives...
  assert any(
    b.get("type") == "text" and b.get("content") == "partial answer"
    for b in blocks
  )
  # ...and the paused note is appended.
  assert any(
    b.get("type") == "error"
    and b.get("message") == chat_mod.PAUSED_FOR_RESTART_MESSAGE
    for b in blocks
  )


# -- (a) DrainForRestart preserves the queue; stop_chat_for clears it ---------

def test_drain_preserves_pending_while_stop_clears_it():
  queued = [{"role": "user", "content": "queued", "ts": 2}]
  _live_turn("drain-keep", pending=list(queued))
  _live_turn("stop-clear", pending=list(queued))

  # Drain-for-restart: queue + run marker intact.
  _run_drain()
  _drain_writer()
  drained_state = _chat("drain-keep")
  assert drained_state["pending"] == queued
  assert drained_state["run_status"] == "running"  # marker LEFT for reconcile

  # Stop: queue collapsed (frontend resends; backend clears). The drain above
  # left the process-wide gate set (in production only the restart ends it), so
  # reset it here — Stop-during-drain deliberately preserves the queue, and
  # this half of the contrast is about a NORMAL worker's Stop.
  chat_mod.draining = False
  db = SessionLocal()
  try:
    asyncio.run(chat_mod.stop_chat_for("stop-clear", db=db))
  finally:
    db.close()
  _drain_writer()
  assert _chat("stop-clear")["pending"] == []


def test_drain_does_not_promote_the_queue():
  """The queue must NOT be promoted at drain time (that would start a turn while
  the worker is shutting down). The bumped generation makes the turn-end drain
  read STALE_NO_ACTION; here we assert the queue is left untouched."""
  _, sink, _ = _live_turn(
    "drain-nopromote", pending=[{"role": "user", "content": "q", "ts": 2}]
  )
  gen_before = chat_mod.current_run_generation("drain-nopromote")

  _run_drain()
  _drain_writer()

  # Generation bumped (so a racing turn-end drain would read STALE_NO_ACTION).
  assert chat_mod.current_run_generation("drain-nopromote") == gen_before + 1
  # Queue preserved; the chat is flagged as drained-for-restart so its runner's
  # finally leaves the marker set.
  assert _chat("drain-nopromote")["pending"] == [
    {"role": "user", "content": "q", "ts": 2}
  ]
  assert "drain-nopromote" in chat_mod._restart_draining_chats


# -- (c) boot reconcile marks the note resumable (no double note) + notify once


def test_reconcile_marks_paused_note_resumable_without_double_note():
  cid = "reco-drained"
  _seed(cid, messages=[
    {"role": "user", "content": "hi", "ts": 1},
    {"role": "assistant", "ts": 2, "content": "partial", "blocks": [
      {"type": "text", "content": "partial"},
      {"type": "error", "message": chat_mod.PAUSED_FOR_RESTART_MESSAGE},
    ]},
  ])

  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()

  assert cid in reconciled
  state = _chat(cid)
  assert state["run_status"] is None
  blocks = state["messages"][-1]["blocks"]
  errors = [b for b in blocks if b.get("type") == "error"]
  # Exactly ONE error note (no second interrupted note stacked on the drain's),
  # and it is now resumable.
  assert len(errors) == 1
  assert errors[0]["message"] == chat_mod.PAUSED_FOR_RESTART_MESSAGE
  assert errors[0]["resumable"] is True
  # The upgrade also stamps the benign pause descriptor so a drain note
  # persisted before it existed (or whose live event never landed) renders
  # in the calm "Paused" family, not danger-red.
  assert errors[0]["pause"] == {"kind": "restart"}


def test_reconcile_crash_note_is_resumable():
  cid = "reco-crash"
  # No assistant content — a crash mid-turn before anything streamed.
  _seed(cid, messages=[{"role": "user", "content": "hi", "ts": 1}])

  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()

  assert cid in reconciled
  blocks = _chat(cid)["messages"][-1]["blocks"]
  note = next(b for b in blocks if b.get("type") == "error")
  assert note["resumable"] is True


def test_reconcile_question_tail_note_is_not_resumable():
  """When the interrupted turn ends on an unanswered question, the question
  card is the tail affordance — answering it resumes the turn. The inserted
  wait-note must NOT carry resumable, or a Resume button would compete with
  the card and send a visible 'continue' instead of the answer."""
  cid = "reco-question"
  _seed(cid, messages=[
    {"role": "user", "content": "hi", "ts": 1},
    {"role": "assistant", "ts": 2, "content": "", "blocks": [
      {"type": "text", "content": "thinking"},
      {"type": "question", "id": "q1", "text": "Which one?"},
    ]},
  ])

  db = SessionLocal()
  try:
    reconciled = chat_mod.reconcile_interrupted_chats(db)
  finally:
    db.close()

  assert cid in reconciled
  blocks = _chat(cid)["messages"][-1]["blocks"]
  # The question stays the tail block, and the inserted note is inert.
  assert blocks[-1].get("type") == "question"
  note = next(b for b in blocks if b.get("type") == "error")
  assert "answer is still needed" in note["message"]
  assert not note.get("resumable")


def test_notify_after_reconcile_fires_once(owner_token, monkeypatch):
  del owner_token  # fixture creates the owner row notify_after_reconcile needs
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner",
    lambda db, owner_id, **kw: calls.append(kw) or "notif-id",
  )

  db = SessionLocal()
  try:
    result = chat_mod.notify_after_reconcile(db, ["c1", "c2", "c3"])
  finally:
    db.close()

  assert result == "notif-id"
  assert len(calls) == 1
  assert "resume" in calls[0]["body"].lower()


def test_notify_after_reconcile_noop_when_nothing_reconciled(owner_token, monkeypatch):
  del owner_token
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner",
    lambda db, owner_id, **kw: calls.append(kw) or "notif-id",
  )

  db = SessionLocal()
  try:
    assert chat_mod.notify_after_reconcile(db, []) is None
  finally:
    db.close()
  assert calls == []


# -- (d) a send while draining queues instead of starting ---------------------

def test_send_while_draining_queues_instead_of_starting(client, auth):
  cid = str(uuid.uuid4())
  db = SessionLocal()
  try:
    db.add(models.Chat(id=cid, title="t", messages=[]))
    db.commit()
  finally:
    db.close()

  chat_mod.draining = True
  try:
    r = client.post(
      f"/api/chats/{cid}/messages",
      headers=auth,
      json={"content": "hello while draining"},
    )
  finally:
    chat_mod.draining = False

  assert r.status_code == 202
  assert r.json()["status"] == "queued"
  # No turn was started; the send sits in the durable queue for post-restart.
  assert not chat_mod.is_chat_running(cid)
  assert len(_chat(cid)["pending"]) == 1


def test_force_steer_while_draining_queues_too(client, auth):
  """force_steer must not pierce the drain gate: a steer accepted while the
  drain is interrupting a handle can buffer into the dying runner's
  continuation (or, with no running turn, fall through to a fresh StartTurn).
  During the restart window every send queues — steer included."""
  cid = str(uuid.uuid4())
  db = SessionLocal()
  try:
    db.add(models.Chat(id=cid, title="t", messages=[]))
    db.commit()
  finally:
    db.close()

  chat_mod.draining = True
  try:
    r = client.post(
      f"/api/chats/{cid}/messages",
      headers=auth,
      json={"content": "steer while draining", "force_steer": True},
    )
  finally:
    chat_mod.draining = False

  assert r.status_code == 202
  assert r.json()["status"] == "queued"
  assert not chat_mod.is_chat_running(cid)
  assert len(_chat(cid)["pending"]) == 1


def test_stop_during_drain_preserves_pending():
  """A Stop landing inside the drain window must not clear the queue: the
  worker is about to die, so handing the cleared messages to the frontend to
  re-send races the SIGTERM and can lose them. With the drain gate up, Stop
  still interrupts but reports an empty cleared list (the PM-115 contract:
  the frontend re-sends only what the backend confirms it cleared)."""
  queued = [{"role": "user", "content": "queued", "ts": 2}]
  _live_turn("stop-in-drain", pending=list(queued))

  chat_mod.draining = True
  try:
    db = SessionLocal()
    try:
      _, cleared, _ = asyncio.run(chat_mod.stop_chat_for("stop-in-drain", db=db))
    finally:
      db.close()
  finally:
    chat_mod.draining = False
  _drain_writer()

  assert cleared == []
  assert _chat("stop-in-drain")["pending"] == queued


def test_wedged_sweep_stands_down_while_draining():
  """The wedged-marker sweep must not clear a marker the drain deliberately
  left set (design §2.3 — the sweeps stand down during a drain)."""
  _seed("sweep-drain", run_status="running")
  chat_mod.draining = True
  try:
    db = SessionLocal()
    try:
      swept = asyncio.run(chat_mod.sweep_wedged_run_markers(db))
    finally:
      db.close()
  finally:
    chat_mod.draining = False
  assert swept == []
  assert _chat("sweep-drain")["run_status"] == "running"
