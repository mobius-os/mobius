"""Drain-gated restart (design §2.2, DrainForRestart path).

Locks in the five contracts that distinguish a restart-drain from a Stop:

  (a) DrainForRestart PRESERVES pending_messages and moves the exact run to a
      due restart park, while stop_chat_for CLEARS the queue (contrast).
  (b) The drain persists the "paused for a platform update" note WITHOUT losing
      the accumulated partial blocks.
  (c) Generic boot reconciliation stays manual; display text alone never
      manufactures planned-restart intent.
  (d) A send arriving while draining QUEUES instead of starting a turn.
  (e) Stop/finalize failures retain authenticated exact-run intent, and startup
      converts every opted fallback to the same immediate continuation path.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from app import chat as chat_mod
from app import models
from app.broadcast import create_broadcast
from app.chat_writer import Barrier, get_writer
from app.chat_transcript import materialized_messages
from app.database import SessionLocal
from app.runner_registry import RunnerKind, registry
from app.memory_recall import EMPTY_RECALL_BINDING


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


def _seed(chat_id: str, *, pending=None, messages=None):
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
    ))
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
    from app.run_state import has_running_run
    return {
      "messages": materialized_messages(row),
      "pending": list(row.pending_messages or []),
      "running_status": "running" if has_running_run(db, chat_id) else None,
    }
  finally:
    db.close()


def _run(chat_id: str):
  db = SessionLocal()
  try:
    row = db.query(models.ChatRun).filter(
      models.ChatRun.chat_id == chat_id,
    ).order_by(models.ChatRun.started_at.desc()).first()
    if row is None:
      return None
    return {
      "status": row.status,
      "parked_until": row.parked_until,
      "park_reason": row.park_reason,
      "restart_nonce": row.restart_nonce,
    }
  finally:
    db.close()


def _live_turn(chat_id: str, *, pending=None, partial="partial answer"):
  """A live turn with a registered handle + sink, mid-stream (a real partial
  accumulated INTO the sink so the drain's finalize can preserve it)."""
  _seed(chat_id, pending=pending)
  bc = create_broadcast(chat_id)
  sink = chat_mod._ChatEventSink(bc, chat_id, run_token=f"rt-{chat_id}", recall_binding=EMPTY_RECALL_BINDING)
  chat_mod.register_active_sink(chat_id, sink)
  if partial:
    sink.publish({"type": "text", "content": partial})
  handle = _Handle(chat_id)
  registry.register(handle)
  return bc, sink, handle


def _run_drain():
  return asyncio.run(chat_mod.drain_all_for_restart(
    restart_nonce="restart-nonce-drain",
  ))


# -- (b) the drain persists the paused note + preserves partials --------------

def test_drain_persists_paused_note_and_preserves_partials():
  _, _, handle = _live_turn("drain-note-1")

  drained = _run_drain()
  _drain_writer()

  assert drained == [{
    "chat_id": "drain-note-1",
    "run_token": "rt-drain-note-1",
  }]
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
  # The exact run, not the display text, is the durable restart intent.
  assert _run("drain-note-1")["status"] == "parked"
  assert _run("drain-note-1")["park_reason"] == "restart"
  assert _run("drain-note-1")["parked_until"] is not None
  assert _run("drain-note-1")["restart_nonce"] == "restart-nonce-drain"


def test_drain_pause_survives_claude_interrupt_terminal(monkeypatch):
  """Claude reports a provider error while honoring the drain interrupt."""
  cid = "drain-claude-interrupt"
  _, sink, handle = _live_turn(cid)

  async def _stop_with_claude_terminal(timeout=2.0):
    del timeout
    handle.stop_calls += 1
    # This is the terminal result Claude emits after the drain has already
    # published the authoritative, resumable restart pause.
    chat_mod._limit_exit(sink, {}, "Execution interrupted.")
    registry.unregister(cid, handle.kind)
    return True

  monkeypatch.setattr(handle, "stop", _stop_with_claude_terminal)

  assert _run_drain() == [{
    "chat_id": cid,
    "run_token": f"rt-{cid}",
  }]
  _drain_writer()

  errors = [
    block for block in _chat(cid)["messages"][-1]["blocks"]
    if block.get("type") == "error"
  ]
  assert errors == [{
    "type": "error",
    "message": chat_mod.PAUSED_FOR_RESTART_MESSAGE,
    "resumable": True,
    "pause": {"kind": "restart"},
  }]


def test_drain_finalizes_running_tool_before_clearing_reconcile_marker():
  cid = "drain-running-tool"
  _, sink, _ = _live_turn(cid, partial="")
  sink.publish({
    "type": "tool_start",
    "tool": "Bash",
    "input": "long command",
    "tool_use_id": "tool-drain-1",
  })

  assert _run_drain() == [{
    "chat_id": cid,
    "run_token": f"rt-{cid}",
  }]
  _drain_writer()

  state = _chat(cid)
  tool = next(
    block for block in state["messages"][-1]["blocks"]
    if block.get("type") == "tool"
  )
  assert tool["status"] == "done"
  assert state["running_status"] is None


# -- (a) DrainForRestart preserves the queue; stop_chat_for clears it ---------

def test_drain_preserves_pending_while_stop_clears_it():
  queued = [{"role": "user", "content": "queued", "ts": 2}]
  _live_turn("drain-keep", pending=list(queued))
  _live_turn("stop-clear", pending=list(queued))

  # Drain-for-restart: queue intact; exact run moved to a due restart park.
  _run_drain()
  _drain_writer()
  drained_state = _chat("drain-keep")
  assert drained_state["pending"] == queued
  assert drained_state["running_status"] is None
  assert _run("drain-keep")["status"] == "parked"
  assert _run("drain-keep")["park_reason"] == "restart"

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


def test_drain_park_failure_leaves_authenticated_marker_for_boot_recovery(
  monkeypatch,
):
  cid = "drain-park-failure"
  _live_turn(cid)

  async def _fail_park(*args, **kwargs):
    del args, kwargs
    raise RuntimeError("writer unavailable")

  monkeypatch.setattr(chat_mod, "_park_run_strict", _fail_park)
  assert _run_drain() == [{
    "chat_id": cid,
    "run_token": f"rt-{cid}",
  }]
  _drain_writer()

  assert _chat(cid)["running_status"] == "running"
  assert _run(cid)["status"] == "running"
  assert _run(cid)["restart_nonce"] == "restart-nonce-drain"


def test_drain_stop_timeout_keeps_authenticated_restart_intent():
  """A provider that misses the stop window must still auto-recover on boot."""
  cid = "drain-stop-timeout"
  _seed(cid)
  bc = create_broadcast(cid)
  sink = chat_mod._ChatEventSink(
    bc, cid, run_token=f"rt-{cid}", recall_binding=EMPTY_RECALL_BINDING)
  chat_mod.register_active_sink(cid, sink)
  handle = _Handle(cid, stops=False)
  registry.register(handle)

  assert _run_drain() == [{
    "chat_id": cid,
    "run_token": f"rt-{cid}",
  }]
  _drain_writer()

  assert handle.stop_calls == 1
  assert _chat(cid)["running_status"] == "running"
  run = _run(cid)
  assert run["status"] == "running"
  assert run["restart_nonce"] == "restart-nonce-drain"


def test_drain_interrupts_live_providers_concurrently(monkeypatch):
  """One slow provider must not consume every later chat's stop window."""
  ids = [
    "drain-parallel-a",
    "drain-parallel-b",
    "drain-parallel-c",
  ]
  handles = {}
  for cid in ids:
    _, _sink, handle = _live_turn(cid)
    handles[cid] = handle
  entered = set()
  all_entered = asyncio.Event()
  completed = set()

  for cid, handle in handles.items():
    async def _stop(timeout=2.0, *, _cid=cid, _handle=handle):
      del timeout
      entered.add(_cid)
      if len(entered) == len(ids):
        all_entered.set()
      await asyncio.wait_for(all_entered.wait(), timeout=0.5)
      registry.unregister(_cid, _handle.kind)
      completed.add(_cid)
      return True

    monkeypatch.setattr(handle, "stop", _stop)

  _run_drain()
  assert completed == set(ids)
  assert all(_run(cid)["status"] == "parked" for cid in ids)


def test_drain_terminal_snapshot_failure_keeps_authenticated_restart_intent(
  monkeypatch,
):
  """Transcript serialization failure no longer downgrades to manual."""
  cid = "drain-finalize-failure"
  _, sink, _ = _live_turn(cid)

  async def _fail_finalize():
    raise TypeError("Object is not JSON serializable")

  monkeypatch.setattr(sink, "finalize", _fail_finalize)
  assert _run_drain() == [{
    "chat_id": cid,
    "run_token": f"rt-{cid}",
  }]
  _drain_writer()

  assert _chat(cid)["running_status"] == "running"
  run = _run(cid)
  assert run["status"] == "running"
  assert run["restart_nonce"] == "restart-nonce-drain"


def test_drain_without_exact_run_token_stays_manual():
  cid = "drain-no-token"
  _seed(cid)
  bc = create_broadcast(cid)
  sink = chat_mod._ChatEventSink(bc, cid, run_token=None, recall_binding=EMPTY_RECALL_BINDING)
  chat_mod.register_active_sink(cid, sink)
  handle = _Handle(cid)
  registry.register(handle)

  assert _run_drain() == []
  _drain_writer()

  assert _chat(cid)["running_status"] == "running"
  assert _run(cid)["status"] == "running"


def test_authenticated_restart_fallback_becomes_due_park():
  cid = "reco-authenticated-restart"
  nonce = "restart-nonce-authorized"
  _seed(cid, messages=[
    {"role": "user", "content": "do work", "ts": 1},
    {"role": "assistant", "content": "partial", "ts": 2, "blocks": [
      {"type": "text", "content": "partial"},
      {"type": "error", "message": chat_mod.PAUSED_FOR_RESTART_MESSAGE},
    ]},
  ])
  db = SessionLocal()
  try:
    run = db.query(models.ChatRun).filter(
      models.ChatRun.id == f"rt-{cid}",
    ).one()
    run.restart_nonce = nonce
    db.commit()
    result = chat_mod.reconcile_startup_chats(
      db, restart_authorization=nonce,
    )
  finally:
    db.close()

  assert result.manual == []
  assert result.restart_parks == [cid]
  assert result.restart_waiting == []
  assert _chat(cid)["running_status"] is None
  run = _run(cid)
  assert run["status"] == "parked"
  assert run["park_reason"] == "restart"
  assert run["parked_until"] is not None
  assert run["restart_nonce"] == nonce
  errors = [
    block for block in _chat(cid)["messages"][-1]["blocks"]
    if block.get("type") == "error"
  ]
  assert len(errors) == 1
  assert errors[0]["resumable"] is True


def test_unacknowledged_restart_intent_remains_manual():
  cid = "reco-unacknowledged-restart"
  _seed(cid)
  db = SessionLocal()
  try:
    run = db.query(models.ChatRun).filter(
      models.ChatRun.id == f"rt-{cid}",
    ).one()
    run.restart_nonce = "restart-nonce-unaccepted"
    db.commit()
    result = chat_mod.reconcile_startup_chats(
      db, restart_authorization="different-authorized-nonce",
    )
  finally:
    db.close()

  assert result.manual == [cid]
  assert result.restart_parks == []
  assert result.restart_waiting == []
  run = _run(cid)
  assert run["status"] == "interrupted"
  assert run["restart_nonce"] is None


def test_five_chat_restart_recovers_timeouts_and_finalize_failure(
  monkeypatch,
):
  """Regression for the production incident: all five opted turns resume."""
  nonce = "restart-nonce-five-chat"
  clean_id = "restart-five-clean"
  timeout_ids = [
    "restart-five-timeout-a",
    "restart-five-timeout-b",
    "restart-five-timeout-c",
  ]
  finalize_id = "restart-five-finalize"
  all_ids = [clean_id, *timeout_ids, finalize_id]
  sinks = {}
  handles = {}
  for cid in all_ids:
    _, sink, handle = _live_turn(cid)
    sinks[cid] = sink
    handles[cid] = handle
  for cid in timeout_ids:
    handles[cid]._stops = False

  async def _fail_finalize():
    raise TypeError("legacy path is not JSON serializable")

  monkeypatch.setattr(sinks[finalize_id], "finalize", _fail_finalize)

  covered = asyncio.run(chat_mod.drain_all_for_restart(
    restart_nonce=nonce,
  ))
  assert {item["chat_id"] for item in covered} == set(all_ids)
  assert _run(clean_id)["status"] == "parked"
  for cid in [*timeout_ids, finalize_id]:
    run = _run(cid)
    assert run["status"] == "running"
    assert run["restart_nonce"] == nonce

  # Simulate the new process: no old registry/sink survives the boot.
  registry.reset_for_tests()
  for cid, sink in sinks.items():
    chat_mod.unregister_active_sink(cid, sink)
  chat_mod._restart_draining_chats.clear()
  chat_mod.draining = False

  db = SessionLocal()
  try:
    result = chat_mod.reconcile_startup_chats(
      db, restart_authorization=nonce,
    )
  finally:
    db.close()
  assert set(result.restart_parks) == {*timeout_ids, finalize_id}
  assert result.manual == []
  assert result.restart_waiting == []
  assert all(_run(cid)["status"] == "parked" for cid in all_ids)

  scheduled = []

  async def _record_resume(
    chat_id, park_token=None, *, restart_authorization=None,
  ):
    scheduled.append((chat_id, park_token, restart_authorization))
    return True

  monkeypatch.setattr(chat_mod, "_auto_resume_chat", _record_resume)
  db = SessionLocal()
  try:
    sweep = asyncio.run(chat_mod.sweep_reset_parks(
      db, restart_authorization=nonce,
    ))
  finally:
    db.close()

  assert len(sweep.resolved) == chat_mod.RESTART_AUTO_RESUME_BATCH_SIZE
  assert sweep.restart_deferred is True
  assert {item[0] for item in scheduled} == set(sweep.resolved)
  assert all(item[2] == nonce for item in scheduled)


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
  assert state["running_status"] is None
  assert _run(cid)["status"] == "interrupted"
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
  assert _run(cid)["status"] == "interrupted"
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


def test_authenticated_restart_question_stays_waiting_not_manual():
  """An exact restart must preserve a question without claiming it failed.

  The owner still has to answer the card; neither automatic Continue nor the
  generic "tap to resume" crash notification is a valid continuation.
  """
  cid = "reco-authenticated-question"
  nonce = "restart-nonce-question"
  _seed(cid, messages=[
    {"role": "user", "content": "hi", "ts": 1},
    {"role": "assistant", "ts": 2, "content": "", "blocks": [
      {"type": "text", "content": "I need your approval."},
      {"type": "question", "id": "q1", "text": "Restart now?"},
    ]},
  ])
  db = SessionLocal()
  try:
    run = db.query(models.ChatRun).filter(
      models.ChatRun.id == f"rt-{cid}",
    ).one()
    chat = db.query(models.Chat).filter(models.Chat.id == cid).one()
    run.restart_nonce = nonce
    chat.pending_question_id = "q1"
    db.commit()
    result = chat_mod.reconcile_startup_chats(
      db, restart_authorization=nonce,
    )
  finally:
    db.close()

  assert result.manual == []
  assert result.restart_parks == []
  assert result.restart_waiting == [cid]
  run = _run(cid)
  assert run["status"] == "interrupted"
  assert run["restart_nonce"] is None
  blocks = _chat(cid)["messages"][-1]["blocks"]
  assert blocks[-1]["type"] == "question"
  note = next(block for block in blocks if block["type"] == "error")
  assert "answer is still needed" in note["message"]
  assert not note.get("resumable")


def test_reconcile_restart_note_normalizes_before_open_question():
  """A fallback marker may contain either historical block ordering."""
  cid = "reco-restart-question-order"
  _seed(cid, messages=[
    {"role": "user", "content": "hi", "ts": 1},
    {"role": "assistant", "ts": 2, "content": "", "blocks": [
      {"type": "text", "content": "thinking"},
      {"type": "question", "questions": [{"question": "Which one?"}]},
      {
        "type": "error",
        "message": chat_mod.PAUSED_FOR_RESTART_MESSAGE,
        "resumable": True,
        "pause": {"kind": "restart"},
      },
    ]},
  ])

  db = SessionLocal()
  try:
    assert chat_mod.reconcile_interrupted_chats(db) == [cid]
  finally:
    db.close()

  blocks = _chat(cid)["messages"][-1]["blocks"]
  assert [block["type"] for block in blocks] == [
    "text", "error", "question",
  ]
  assert not blocks[1].get("resumable")
  assert blocks[1]["pause"] == {"kind": "restart"}


def test_historical_restart_note_does_not_mask_a_newer_crash():
  cid = "reco-historical-restart-note"
  _seed(cid, messages=[
    {"role": "user", "content": "hi", "ts": 1},
    {"role": "assistant", "ts": 2, "content": "new work", "blocks": [
      {"type": "error", "message": chat_mod.PAUSED_FOR_RESTART_MESSAGE},
      {"type": "text", "content": "new work after recovery"},
    ]},
  ])

  db = SessionLocal()
  try:
    assert chat_mod.reconcile_interrupted_chats(db) == [cid]
  finally:
    db.close()

  blocks = _chat(cid)["messages"][-1]["blocks"]
  errors = [block for block in blocks if block.get("type") == "error"]
  assert len(errors) == 2
  assert errors[-1]["message"] != chat_mod.PAUSED_FOR_RESTART_MESSAGE
  assert errors[-1]["resumable"] is True


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


# -- bootstrap rejection may reopen only an idle drain ------------------------

def test_cancel_idle_drain_keeps_admission_closed_for_a_live_runner():
  chat_mod.begin_drain()
  registry.register(_Handle("bootstrap-live-runner"))

  assert chat_mod.cancel_idle_drain() is False
  assert chat_mod.is_draining() is True


def test_cancel_idle_drain_keeps_admission_closed_for_a_restart_claim():
  chat_mod.begin_drain()
  chat_mod._restart_draining_chats.add("bootstrap-restart-claim")

  assert chat_mod.cancel_idle_drain() is False
  assert chat_mod.is_draining() is True


def test_cancel_idle_drain_reopens_admission_when_the_worker_is_idle():
  chat_mod.begin_drain()

  assert chat_mod.cancel_idle_drain() is True
  assert chat_mod.is_draining() is False


# -- (d) a send while draining queues instead of starting ---------------------

def test_send_while_draining_queues_instead_of_starting(client, auth):
  cid = str(uuid.uuid4())
  db = SessionLocal()
  try:
    db.add(models.Chat(
      id=cid,
      title="t",
      messages=[],
      agent_settings_json={"model": "claude-opus-4-8"},
    ))
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
    db.add(models.Chat(
      id=cid,
      title="t",
      messages=[],
      agent_settings_json={"model": "claude-opus-4-8"},
    ))
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
      _, cleared = asyncio.run(chat_mod.stop_chat_for("stop-in-drain", db=db))
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
  _seed("sweep-drain")
  chat_mod.draining = True
  try:
    db = SessionLocal()
    try:
      swept = asyncio.run(chat_mod.sweep_wedged_runs(db))
    finally:
      db.close()
  finally:
    chat_mod.draining = False
  assert swept == []
  assert _chat("sweep-drain")["running_status"] == "running"
