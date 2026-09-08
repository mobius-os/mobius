"""Durable continuation (design §2.4): parse/drain → park → sweep → resume.

Locks in the contracts of the limit-park feature:

  (a) Reset-time parsing is lenient: structured value → text parse →
      30-minute fallback, clamped, and it NEVER raises.
  (b) A limit kill PARKS the run row (parked_until + park_reason) and
      clears the per-chat marker; ownership is identity-keyed like
      FinishRun (a superseded run never parks onto a fresh marker).
  (c) Latest-run-wins: the park probe honors a park only while the chat's
      newest run row is the parked one, and a fresh StartTurn closes a stale
      park (no orphaned notify/auto-resume).
  (d) The reset sweep makes at most one notification attempt per park, keeps
      an opted park
      retryable until its continuation starts, skips future parks, stands down
      while draining, and resolves deleted chats silently.
  (e) Auto-resume is policy-controlled (off = notify only). Provider-limit
      retries ignore unrelated live work and launch with a short stagger,
      while an accepted planned restart relaunches the exact previously-live
      set in prompt batches that do not wait for earlier turns to finish. Each
      resumed turn combines its preserved queue + a "continue" into one
      continuation.
  (f) The parks are observable: /api/debug/status lists parked runs.
  (g) A planned restart reuses the same exact-run state with a due-now time;
      crashes, unanswered questions, and app-owned work stay manual.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app import chat as chat_mod
from app import models
from app.chat_writer import (
  Barrier,
  FinishRun,
  ParkRun,
  PrepareAutoResume,
  PromotePending,
  ResolvePark,
  StartTurn,
  _tail_open_question_id,
  get_writer,
)
from app.database import SessionLocal
from app.chat_transcript import materialized_messages
from app.runner_registry import RunnerKind, registry
from app.memory_recall import EMPTY_RECALL_BINDING


NOW = datetime(2026, 7, 10, 22, 0, 0)


def _async_notify(callback):
  async def wrapped(*args, **kwargs):
    return callback(*args, **kwargs)

  return wrapped


class _Sink:
  def __init__(self):
    self.events = []

  def publish(self, event):
    self.events.append(event)


class _Handle:
  def __init__(self, chat_id: str):
    self.chat_id = chat_id
    self.kind = RunnerKind.CLAUDE_SDK

  async def stop(self, timeout: float = 2.0) -> bool:
    del timeout
    registry.unregister(self.chat_id, self.kind)
    return True


def _drain_writer():
  get_writer().submit(Barrier()).result(timeout=5)


def _seed_chat(
  chat_id: str, *, pending=None, deleted=False,
  auto_resume=False, auto_restart=True, messages=None,
):
  db = SessionLocal()
  try:
    db.add(models.Chat(
      id=chat_id,
      title="t",
      messages=(
        messages
        if messages is not None
        else [{"role": "user", "content": "do work", "ts": 1}]
      ),
      pending_messages=pending or [],
      session_id="sess",
      provider="claude",
      auto_resume_on_limit=auto_resume,
      auto_resume_on_restart=auto_restart,
      deleted_at=(
        datetime.now(UTC).replace(tzinfo=None) if deleted else None
      ),
    ))
    db.commit()
  finally:
    db.close()


def _seed_run(chat_id: str, token: str, *, status="running",
              parked_until=None, park_reason=None, started_offset=0,
              initiated_by_app_id=None, restart_nonce=None):
  db = SessionLocal()
  try:
    db.add(models.ChatRun(
      id=token,
      chat_id=chat_id,
      status=status,
      provider="claude",
      initiated_by_app_id=initiated_by_app_id,
      started_at=(
        datetime.now(UTC).replace(tzinfo=None)
        + timedelta(seconds=started_offset)
      ),
      parked_until=parked_until,
      park_reason=park_reason,
      restart_nonce=restart_nonce,
    ))
    db.commit()
  finally:
    db.close()


def _run_row(token: str):
  db = SessionLocal()
  try:
    run = db.query(models.ChatRun).filter(models.ChatRun.id == token).first()
    if run is None:
      return None
    return {
      "status": run.status,
      "parked_until": run.parked_until,
      "park_reason": run.park_reason,
      "restart_nonce": run.restart_nonce,
      "ended_at": run.ended_at,
      "initiated_by_app_id": run.initiated_by_app_id,
    }
  finally:
    db.close()


def _chat_row(chat_id: str):
  db = SessionLocal()
  try:
    row = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    from app.run_state import has_running_run
    return {
      "running_status": "running" if has_running_run(db, chat_id) else None,
      "messages": materialized_messages(row),
      "pending": list(row.pending_messages or []),
    }
  finally:
    db.close()


# -- (a) reset-time parsing ----------------------------------------------------

def test_park_fields_structured_datetime_aware():
  aware = datetime(2026, 7, 11, 1, 40, tzinfo=UTC)
  target, reason = chat_mod._limit_park_fields(
    {"rate_limit_resets_at": aware}, "usage limit reached", now=NOW,
  )
  assert target == datetime(2026, 7, 11, 1, 40)
  assert reason == "usage_limit"


def test_park_fields_structured_epoch_seconds():
  epoch = int(datetime(2026, 7, 11, 1, 40, tzinfo=UTC).timestamp())
  target, _ = chat_mod._limit_park_fields(
    {"rate_limit_resets_at": epoch}, None, now=NOW,
  )
  assert target == datetime(2026, 7, 11, 1, 40)


def test_park_fields_structured_iso_string():
  target, _ = chat_mod._limit_park_fields(
    {"rate_limit_resets_at": "2026-07-11T01:40:00Z"}, None, now=NOW,
  )
  assert target == datetime(2026, 7, 11, 1, 40)


def test_park_fields_text_clock_rolls_to_next_occurrence():
  # 1:40am has already passed at NOW (22:00), so the park rolls to tomorrow.
  target, reason = chat_mod._limit_park_fields(
    {}, "You've hit your weekly limit · resets 1:40am", now=NOW,
  )
  assert target == datetime(2026, 7, 11, 1, 40)
  assert reason == "usage_limit"


def test_park_fields_text_relative_duration():
  target, reason = chat_mod._limit_park_fields(
    {}, "Server is temporarily limiting requests. Try again in 30 minutes.",
    now=NOW,
  )
  assert target == NOW + timedelta(minutes=30)
  assert reason == "rate_limit"


def test_park_fields_text_iso_timestamp():
  target, _ = chat_mod._limit_park_fields(
    {}, "Rate limited. Resets at 2026-07-11T01:40:00Z.", now=NOW,
  )
  assert target == datetime(2026, 7, 11, 1, 40)


def test_park_fields_fallback_on_unparseable_text():
  target, reason = chat_mod._limit_park_fields(
    {}, "429 too many requests", now=NOW,
  )
  assert target == NOW + chat_mod.PARK_FALLBACK_DELAY
  assert reason == "rate_limit"


def test_park_fields_clamps_past_reset_to_min_delay():
  past = datetime(2026, 7, 10, 1, 0, tzinfo=UTC)
  target, _ = chat_mod._limit_park_fields(
    {"rate_limit_resets_at": past}, None, now=NOW,
  )
  assert target == NOW + timedelta(seconds=60)


def test_park_fields_clamps_absurd_future_and_never_raises():
  target, _ = chat_mod._limit_park_fields(
    {"rate_limit_resets_at": "9999-01-01T00:00:00Z"}, None, now=NOW,
  )
  assert target == NOW + timedelta(days=7)
  # A hostile structured value must degrade to the fallback, not raise.
  target, reason = chat_mod._limit_park_fields(
    {"rate_limit_resets_at": object()}, None, now=NOW,
  )
  assert target == NOW + chat_mod.PARK_FALLBACK_DELAY
  assert reason == "rate_limit"


# -- the shared exit classifier ------------------------------------------------

def test_limit_exit_publishes_enriched_event_and_kwargs():
  sink = _Sink()
  kwargs = chat_mod._limit_exit(
    sink,
    {"api_error_status": 429,
     "error": "You've hit your weekly limit · resets 1:40am"},
    "You've hit your weekly limit · resets 1:40am",
  )
  assert kwargs["limit_reached"] is True
  assert isinstance(kwargs["parked_until"], datetime)
  event = sink.events[-1]
  assert event["type"] == "error"
  assert event["resumable"] is True
  # The block carries ONE pause descriptor; the DB kwargs keep the raw fields.
  # Explicit-UTC ISO so the client's Date() renders the right local time.
  assert event["pause"]["resets_at"].endswith("+00:00")
  assert event["pause"]["kind"] == kwargs["park_reason"]


def test_limit_exit_non_limit_error_stays_plain():
  sink = _Sink()
  kwargs = chat_mod._limit_exit(sink, {"error": "syntax error"}, "syntax error")
  assert kwargs == {"limit_reached": False}
  assert sink.events[-1] == {"type": "error", "message": "syntax error"}


def test_limit_exit_resume_incomplete_publishes_calm_resumable_note():
  """A steer-interrupted turn (error defused to None, resume_incomplete set)
  publishes a calm, resumable "Paused" note — never a red error block."""
  sink = _Sink()
  kwargs = chat_mod._limit_exit(
    sink,
    {"error": None, "terminal_status": "interrupted", "resume_incomplete": True},
    None,
  )
  assert kwargs == {"limit_reached": False}
  note = sink.events[-1]
  assert note["type"] == "error"
  assert note["resumable"] is True
  # A `pause` descriptor makes the card render in the calm "Paused" family, not
  # the danger-red error styling; the kind is distinct from a restart pause.
  assert note["pause"]["kind"] == "interrupted"
  assert "resets_at" not in note["pause"]
  assert "Resume" in note["message"]


def test_limit_exit_bare_429_synthesizes_the_card_block():
  """A 429 terminal with NO error text still persists a parked card block."""
  sink = _Sink()
  kwargs = chat_mod._limit_exit(sink, {"api_error_status": 429}, None)
  assert kwargs["limit_reached"] is True
  assert sink.events and sink.events[-1]["type"] == "error"
  assert sink.events[-1]["message"]


# -- (b) the ParkRun / ResolvePark actor commands ------------------------------

def test_park_run_parks_row_and_clears_marker():
  cid = "park-basic"
  _seed_chat(cid)
  _seed_run(cid, "rt-park-basic")
  until = datetime(2026, 7, 11, 1, 40)

  get_writer().submit(ParkRun(
    chat_id=cid, run_token="rt-park-basic",
    parked_until=until, park_reason="usage_limit",
  )).result(timeout=5)

  row = _run_row("rt-park-basic")
  assert row["status"] == "parked"
  assert row["parked_until"] == until
  assert row["park_reason"] == "usage_limit"
  assert row["ended_at"] is not None
  # The per-chat marker is cleared: the turn is over, the chat is not busy.
  assert _chat_row(cid)["running_status"] is None


def _pending_question_id(chat_id: str):
  db = SessionLocal()
  try:
    return db.query(models.Chat).filter(
      models.Chat.id == chat_id
    ).first().pending_question_id
  finally:
    db.close()


def _open_question_transcript(qid: str):
  return [
    {"role": "user", "content": "help me choose", "ts": 1},
    {"role": "assistant", "ts": 2, "blocks": [
      {"type": "text", "content": "Here are the options."},
      {"type": "question", "question_id": qid,
       "questions": [{"question": "Which one?"}]},
    ]},
  ]


def test_restart_park_restores_open_question_marker_from_transcript():
  """A resumable park re-establishes the open-question marker from the parked
  transcript.

  Regression for the restart-resume-past-an-unanswered-question bug (chat
  0f08c0c3): drain-for-restart calls Finalize (which clears the marker) and
  THEN ParkRun. ParkRun owns the "resumable pause keeps the open question"
  invariant, so it must re-derive the marker even though it enters with the
  marker already None. Without it the resume gate saw None and continued past
  the grayed-out card.
  """
  cid = "park-restore-open-q"
  token = "rt-park-restore-open-q"
  _seed_chat(cid, messages=_open_question_transcript("restart-q"))
  _seed_run(cid, token)
  assert _pending_question_id(cid) is None  # the post-Finalize state

  get_writer().submit(ParkRun(
    chat_id=cid, run_token=token,
    parked_until=datetime(2026, 7, 11, 1, 40),
    park_reason="restart", restart_nonce="nonce-restore-1234",
  )).result(timeout=5)

  assert _run_row(token)["status"] == "parked"
  assert _pending_question_id(cid) == "restart-q"


def test_park_run_leaves_marker_none_when_transcript_has_no_open_question():
  """An ordinary park (tail is normal output) must not invent a marker."""
  cid = "park-no-open-q"
  token = "rt-park-no-open-q"
  _seed_chat(cid, messages=[
    {"role": "user", "content": "do work", "ts": 1},
    {"role": "assistant", "ts": 2,
     "blocks": [{"type": "text", "content": "All done."}]},
  ])
  _seed_run(cid, token)

  get_writer().submit(ParkRun(
    chat_id=cid, run_token=token,
    parked_until=datetime(2026, 7, 11, 1, 40),
    park_reason="restart", restart_nonce="nonce-noq-1234",
  )).result(timeout=5)

  assert _run_row(token)["status"] == "parked"
  assert _pending_question_id(cid) is None


def test_tail_open_question_id_derivation():
  """The shared rule ParkRun and the boot backfill both use."""
  qid = "q-tail"
  assert _tail_open_question_id(_open_question_transcript(qid)) == qid
  # Answered -> not open.
  assert _tail_open_question_id([
    {"role": "assistant", "blocks": [
      {"type": "question", "question_id": qid, "answers": {"Which one?": "A"}}]},
  ]) is None
  # A trailing user message means the assistant is no longer the tail.
  assert _tail_open_question_id(
    _open_question_transcript(qid) + [{"role": "user", "content": "hi"}]
  ) is None
  # Oversized id rejected (the column is VARCHAR(64)).
  assert _tail_open_question_id([
    {"role": "assistant", "blocks": [
      {"type": "question", "question_id": "x" * 65}]},
  ]) is None
  assert _tail_open_question_id([]) is None
  assert _tail_open_question_id(None) is None


def test_restart_park_carries_one_shot_intent_nonce():
  cid = "park-restart-nonce"
  token = "rt-park-restart-nonce"
  _seed_chat(cid)
  _seed_run(cid, token)

  get_writer().submit(ParkRun(
    chat_id=cid,
    run_token=token,
    parked_until=datetime(2026, 7, 11, 1, 40),
    park_reason="restart",
    restart_nonce="nonce-park-1234",
  )).result(timeout=5)

  row = _run_row(token)
  assert row["status"] == "parked"
  assert row["restart_nonce"] == "nonce-park-1234"


def test_restart_park_idempotency_requires_the_same_nonce():
  cid = "park-restart-idempotency"
  token = "rt-park-restart-idempotency"
  _seed_chat(cid)
  _seed_run(
    cid,
    token,
    status="parked",
    parked_until=datetime(2026, 7, 11, 1, 40),
    park_reason="restart",
    restart_nonce="nonce-first-1234",
  )

  same = get_writer().submit(ParkRun(
    chat_id=cid,
    run_token=token,
    parked_until=datetime(2026, 7, 11, 1, 40),
    park_reason="restart",
    restart_nonce="nonce-first-1234",
  )).result(timeout=5)
  different = get_writer().submit(ParkRun(
    chat_id=cid,
    run_token=token,
    parked_until=datetime(2026, 7, 11, 1, 40),
    park_reason="restart",
    restart_nonce="nonce-second-1234",
  )).result(timeout=5)

  assert same is True
  assert different is False
  assert _run_row(token)["restart_nonce"] == "nonce-first-1234"


def test_park_run_missing_exact_row_is_a_noop():
  cid = "park-missing-exact-row"
  _seed_chat(cid)

  parked = get_writer().submit(ParkRun(
    chat_id=cid, run_token="rt-does-not-exist",
    parked_until=datetime(2026, 7, 11, 1, 40), park_reason="restart",
  )).result(timeout=5)

  assert parked is False
  assert _chat_row(cid)["running_status"] is None


def test_park_run_superseded_owner_completes_without_parking():
  """A dying run whose marker a fresh turn took must NOT park (a stale park
  would fire a spurious notify) — its own row closes 'completed' and the
  fresh turn's marker survives, mirroring FinishRun ownership."""
  cid = "park-superseded"
  _seed_chat(cid)
  # A fresh StartTurn claims the marker under rt-new (records ownership).
  get_writer().submit(StartTurn(
    chat_id=cid, run_token="rt-new",
    user_msg={"role": "user", "content": "hi", "ts": 2},
    title_source="hi", default_provider="claude",
  )).result(timeout=5)
  # The dying run's row (created before the fresh claim in real flow).
  _seed_run(cid, "rt-old")

  get_writer().submit(ParkRun(
    chat_id=cid, run_token="rt-old",
    parked_until=datetime(2026, 7, 11, 1, 40), park_reason="rate_limit",
  )).result(timeout=5)

  assert _run_row("rt-old")["status"] == "completed"
  assert _run_row("rt-old")["parked_until"] is None
  assert _run_row("rt-new")["status"] == "running"
  assert _chat_row(cid)["running_status"] == "running"


def test_resolve_park_is_idempotent():
  cid = "park-resolve"
  _seed_chat(cid)
  _seed_run(cid, "rt-resolve", status="parked",
            parked_until=datetime(2026, 7, 10, 1, 0))

  first = get_writer().submit(
    ResolvePark(chat_id=cid, run_token="rt-resolve")
  ).result(timeout=5)
  second = get_writer().submit(
    ResolvePark(chat_id=cid, run_token="rt-resolve")
  ).result(timeout=5)

  assert first is True
  assert second is False
  assert _run_row("rt-resolve")["status"] == "parked_notified"


def test_prepare_auto_resume_is_retryable_and_notification_is_one_shot():
  cid = "park-auto-prepare"
  _seed_chat(cid)
  _seed_run(cid, "rt-auto-prepare", status="parked")

  first = get_writer().submit(PrepareAutoResume(
    chat_id=cid, run_token="rt-auto-prepare",
  )).result(timeout=5)
  second = get_writer().submit(PrepareAutoResume(
    chat_id=cid, run_token="rt-auto-prepare",
  )).result(timeout=5)

  assert first == {"active": True, "notify": True}
  assert second == {"active": True, "notify": False}
  assert _run_row("rt-auto-prepare")["status"] == "resume_pending"


def test_prepare_and_resolve_retire_a_stale_nonlatest_park():
  """A delayed sweep command must never revive or notify an older park."""
  cid = "park-stale-command"
  _seed_chat(cid)
  _seed_run(cid, "rt-old-park", status="parked", started_offset=-30)
  _seed_run(cid, "rt-new-run", status="running", started_offset=30)

  prepared = get_writer().submit(PrepareAutoResume(
    chat_id=cid, run_token="rt-old-park",
  )).result(timeout=5)
  assert prepared == {"active": False, "notify": False}
  assert _run_row("rt-old-park")["status"] == "completed"

  # The same latest-run fence applies to ResolvePark. Re-seed a second stale
  # park so the two commands are independently covered.
  _seed_run(cid, "rt-old-notify", status="parked", started_offset=-20)
  resolved = get_writer().submit(ResolvePark(
    chat_id=cid, run_token="rt-old-notify",
  )).result(timeout=5)
  assert resolved is False
  assert _run_row("rt-old-notify")["status"] == "completed"


# -- (c) latest-run-wins + supersession ----------------------------------------

def test_parked_probe_latest_run_wins():
  cid = "park-latest"
  _seed_chat(cid)
  until = datetime(2026, 7, 11, 1, 40)
  _seed_run(cid, "rt-latest-old", status="parked", parked_until=until,
            started_offset=-60)

  db = SessionLocal()
  try:
    # Parked row is the latest → the park is honored.
    assert chat_mod._parked_until_for_chat(db, cid) == until
    # A NEWER running row (a fresh turn) hides the stale park immediately.
    _seed_run(cid, "rt-latest-new", status="running", started_offset=60)
    db.expire_all()
    assert chat_mod._parked_until_for_chat(db, cid) is None
  finally:
    db.close()


def test_fresh_start_turn_closes_stale_park():
  """The owner resuming a parked chat themselves (any new send → StartTurn)
  cancels the stale park, so no spurious reset notify fires later."""
  cid = "park-cancel"
  _seed_chat(cid)
  _seed_run(cid, "rt-stale-park", status="parked",
            parked_until=datetime(2026, 7, 11, 1, 40), started_offset=-60)

  get_writer().submit(StartTurn(
    chat_id=cid, run_token="rt-after-park",
    user_msg={"role": "user", "content": "continue", "ts": 2},
    title_source="continue", default_provider="claude",
  )).result(timeout=5)

  assert _run_row("rt-stale-park")["status"] == "interrupted"
  db = SessionLocal()
  try:
    assert chat_mod._parked_until_for_chat(db, cid) is None
  finally:
    db.close()


def test_owner_message_queues_behind_future_limit_park(
  client, auth, db, monkeypatch,
):
  """Normal owner input preserves the parked run; Try now is explicit."""
  cid = "park-owner-queue"
  _seed_chat(cid, auto_resume=True)
  with SessionLocal() as setup_db:
    setup_db.get(models.Chat, cid).agent_settings_json = {
      "model": "claude-opus-4-8",
    }
    setup_db.commit()
  parked_until = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
  _seed_run(
    cid, "rt-park-owner-queue", status="parked",
    parked_until=parked_until, park_reason="usage_limit",
  )
  scheduled = []
  monkeypatch.setattr(
    "app.routes.chats_stream._schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  assert chat_mod.is_chat_running(cid) is False

  response = client.post(
    f"/api/chats/{cid}/messages",
    json={"content": "also check the weekly limit", "cid": "queued-owner"},
    headers=auth,
  )

  assert response.status_code == 202, response.text
  assert response.json()["status"] == "queued"
  assert scheduled == []
  assert _run_row("rt-park-owner-queue")["status"] == "parked"
  assert _chat_row(cid)["pending"] == [{
    "role": "user",
    "content": "also check the weekly limit",
    "ts": response.json()["ts"],
    "cid": "queued-owner",
  }]


def test_owner_message_also_waits_behind_a_transient_rate_limit(
  client, auth, db, monkeypatch,
):
  del db
  cid = "park-owner-rate-queue"
  _seed_chat(cid, auto_resume=True)
  with SessionLocal() as setup_db:
    setup_db.get(models.Chat, cid).agent_settings_json = {
      "model": "claude-opus-4-8",
    }
    setup_db.commit()
  _seed_run(
    cid, "rt-park-owner-rate-queue", status="parked",
    parked_until=(
      datetime.now(UTC).replace(tzinfo=None) + timedelta(minutes=5)
    ),
    park_reason="rate_limit",
  )
  scheduled = []
  monkeypatch.setattr(
    "app.routes.chats_stream._schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )

  response = client.post(
    f"/api/chats/{cid}/messages",
    json={"content": "keep this with the parked turn", "cid": "rate-owner"},
    headers=auth,
  )

  assert response.status_code == 202, response.text
  assert response.json()["status"] == "queued"
  assert scheduled == []
  assert _run_row("rt-park-owner-rate-queue")["status"] == "parked"


def test_notify_only_park_does_not_capture_an_owner_message(
  client, auth, db, monkeypatch,
):
  del db
  del monkeypatch
  cid = "park-owner-notify-only"
  _seed_chat(cid, auto_resume=False)
  with SessionLocal() as setup_db:
    setup_db.get(models.Chat, cid).agent_settings_json = {
      "model": "claude-opus-4-8",
    }
    setup_db.commit()
  _seed_run(
    cid, "rt-park-owner-notify-only", status="parked",
    parked_until=(
      datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    ),
    park_reason="usage_limit",
  )
  response = client.post(
    f"/api/chats/{cid}/messages",
    json={"content": "start a new turn now", "cid": "notify-owner"},
    headers=auth,
  )

  assert response.status_code == 202, response.text
  assert response.json()["status"] == "started"
  assert _run_row("rt-park-owner-notify-only")["status"] == "interrupted"
  assert _chat_row(cid)["pending"] == []
  chat_mod.discard_starting(cid)


def test_manual_try_now_drains_messages_queued_behind_future_limit_park(
  client, auth, monkeypatch,
):
  cid = "park-owner-try-now"
  _seed_chat(cid, auto_resume=True)
  with SessionLocal() as setup_db:
    setup_db.get(models.Chat, cid).agent_settings_json = {
      "model": "claude-opus-4-8",
    }
    setup_db.commit()
  _seed_run(
    cid, "rt-park-owner-try-now", status="parked",
    parked_until=(
      datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    ),
    park_reason="usage_limit",
  )
  with SessionLocal() as setup_db:
    parked = setup_db.get(models.ChatRun, "rt-park-owner-try-now")
    parked.root_run_id = parked.id
    parked.goal_objective = "Finish the durable Goal"
    parked.goal_id = parked.id
    parked.goal_plan_json = {
      "tasks": [{"id": "finish", "status": "running"}],
    }
    setup_db.commit()
  first = client.post(
    f"/api/chats/{cid}/messages",
    json={"content": "preserve this context", "cid": "queued-context"},
    headers=auth,
  )
  assert first.status_code == 202, first.text
  scheduled = []
  monkeypatch.setattr(
    "app.routes.chats_stream._schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  assert chat_mod.is_chat_running(cid) is False

  response = client.post(
    f"/api/chats/{cid}/messages",
    json={
      "content": "continue", "cid": "manual-early-retry",
      "hidden": True, "continuation": "manual",
    },
    headers=auth,
  )

  assert response.status_code == 202, response.text
  assert response.json()["status"] == "started"
  assert len(scheduled) == 1
  assert "preserve this context" in scheduled[0]["next_user"]["content"]
  assert scheduled[0]["next_user"]["kind"] == "continuation"
  assert scheduled[0]["next_user"]["continuation_reason"] == "manual"
  assert _run_row("rt-park-owner-try-now")["status"] == "completed"
  assert _chat_row(cid)["pending"] == []
  with SessionLocal() as db:
    successor = db.query(models.ChatRun).filter(
      models.ChatRun.id == scheduled[0]["run_token"],
    ).one()
    assert successor.root_run_id == "rt-park-owner-try-now"
    assert successor.goal_id == "rt-park-owner-try-now"
    assert successor.goal_objective == "Finish the durable Goal"


@pytest.mark.parametrize(
  ("kind", "result_cid"),
  [
    ("wait_result", "wait-result-manual-product"),
    ("delegation_result", "delegation-result-manual-product"),
  ],
)
def test_manual_try_now_preserves_product_result_identity_and_claims_latch(
  client, auth, monkeypatch, kind, result_cid,
):
  from app.broadcast import remove_broadcast
  from app.continuations import product_result_run_token

  cid = f"park-manual-{kind}"
  _seed_chat(cid, auto_resume=True)
  _seed_run(
    cid,
    f"rt-{kind}-park",
    status="parked",
    parked_until=(
      datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    ),
    park_reason="usage_limit",
  )
  result = {
    "role": "user",
    "content": f"hidden {kind} receipt",
    "hidden": True,
    "kind": kind,
    "source_work_id": f"source-{kind}",
    "cid": result_cid,
    "ts": 1,
  }
  with SessionLocal() as setup_db:
    target = setup_db.get(models.Chat, cid)
    target.agent_settings_json = {"model": "claude-opus-4-8"}
    target.pending_messages = [result]
    setup_db.commit()
  expected_run = product_result_run_token(cid, result)
  claimed_waits = []
  claimed_wakes = []

  monkeypatch.setattr(
    "app.chat_waits.claim_scheduled_wait_result",
    lambda chat_id, next_user: claimed_waits.append((chat_id, next_user)),
  )
  monkeypatch.setattr(
    "app.delegations.claim_scheduled_parent_wake",
    lambda chat_id, next_user: claimed_wakes.append((chat_id, next_user)),
  )

  def schedule(**kwargs):
    from app.chat_waits import claim_scheduled_wait_result
    from app.delegations import claim_scheduled_parent_wake

    claim_scheduled_wait_result(cid, kwargs["next_user"])
    claim_scheduled_parent_wake(cid, kwargs["next_user"])
    return True

  monkeypatch.setattr(
    "app.routes.chats_stream._schedule_continuation", schedule,
  )

  response = client.post(
    f"/api/chats/{cid}/messages",
    json={
      "content": "continue",
      "cid": f"manual-{kind}",
      "hidden": True,
      "continuation": "manual",
    },
    headers=auth,
  )

  assert response.status_code == 202, response.text
  assert response.json()["status"] == "started"
  with SessionLocal() as check_db:
    successor = check_db.get(models.ChatRun, expected_run)
    assert successor is not None
  for claimed in (claimed_waits, claimed_wakes):
    assert len(claimed) == 1
    assert claimed[0][0] == cid
    assert claimed[0][1]["kind"] == kind
    assert claimed[0][1]["_run_token"] == expected_run
  assert _chat_row(cid)["pending"] == []
  chat_mod.discard_starting(cid)
  remove_broadcast(cid)


def test_park_run_strict_tokenless_finishes_all_running_rows():
  cid = "park-tokenless"
  _seed_chat(cid)
  _seed_run(cid, "rt-park-tokenless")

  asyncio.run(chat_mod._park_run_strict(
    cid, "", datetime(2026, 7, 11, 1, 40), "rate_limit",
  ))
  _drain_writer()

  assert _chat_row(cid)["running_status"] is None


# -- (d) the reset sweep -------------------------------------------------------

def _due_park(
  cid: str, token: str, *, pending=None, deleted=False, auto_resume=False,
  auto_restart=True, park_reason=None, messages=None, restart_nonce=None,
  initiated_by_app_id=None,
):
  _seed_chat(
    cid, pending=pending, deleted=deleted, auto_resume=auto_resume,
    auto_restart=auto_restart, messages=messages,
  )
  _seed_run(cid, token, status="parked",
            parked_until=datetime.now(UTC).replace(tzinfo=None)
            - timedelta(minutes=1), park_reason=park_reason,
            restart_nonce=restart_nonce,
            initiated_by_app_id=initiated_by_app_id)


def _delegated_limit_park(
  cid: str,
  token: str,
  *,
  status: str = "resume_pending",
  pending=None,
) -> int:
  """Seed one exact active Delegation-owned provider-limit attempt."""
  _seed_chat(cid, pending=pending, auto_resume=False)
  _seed_run(
    cid,
    token,
    status=status,
    parked_until=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
    park_reason="usage_limit",
  )
  with SessionLocal() as db:
    app = models.App(
      slug=f"limit-{cid}", source_dir=f"/tmp/mobius-tests/{cid}",
      name="Subagents", description="", jsx_source="",
    )
    db.add(app)
    db.flush()
    parent_id = f"{cid}-parent"
    db.add(models.Chat(
      id=parent_id, title="Parent", messages=[], provider="codex",
    ))
    child = db.get(models.Chat, cid)
    child.created_by_app_id = app.id
    physical = db.get(models.ChatRun, token)
    physical.initiated_by_app_id = app.id
    db.add(models.Delegation(
      id=f"delegation-{cid}", app_id=app.id,
      parent_chat_id=parent_id, parent_root_run_id="parent-root",
      task_key="bounded", child_chat_id=cid, provider="claude",
      model="claude-opus-4-8", effort="low", scope="read", cwd="/data",
      prompt_sha256="digest",
    ))
    db.commit()
    return int(app.id)


def _set_pending_question(cid: str, question_id: str | None) -> None:
  """Set the chat's durable open-question marker (what QuestionCommit does)."""
  db = SessionLocal()
  try:
    chat = db.query(models.Chat).filter(models.Chat.id == cid).first()
    chat.pending_question_id = question_id
    db.commit()
  finally:
    db.close()


def _run_sweep_result():
  db = SessionLocal()
  try:
    return asyncio.run(chat_mod.sweep_reset_parks(db))
  finally:
    db.close()


def _run_sweep():
  return list(_run_sweep_result().resolved)


def _pause_finish_run(monkeypatch):
  """Hold cancellation inside its stop-to-latch window for race tests."""
  entered = asyncio.Event()
  release = asyncio.Event()
  original = chat_mod._finish_run

  async def paused_finish(*args, **kwargs):
    entered.set()
    await release.wait()
    return await original(*args, **kwargs)

  monkeypatch.setattr(chat_mod, "_finish_run", paused_finish)
  return entered, release


def test_sweep_notifies_once_and_resolves(owner_token, monkeypatch):
  del owner_token  # fixture creates the Owner row the notify needs
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: calls.append(kw) or "notif-id"),
  )
  _due_park("sweep-once", "rt-sweep-once")

  resolved = _run_sweep()

  assert resolved == ["sweep-once"]
  assert len(calls) == 1
  assert "reset" in calls[0]["body"].lower()
  assert calls[0]["source_id"] == "sweep-once"
  assert _run_row("rt-sweep-once")["status"] == "parked_notified"

  # A second tick finds nothing parked — the notify is attempted at most once.
  assert _run_sweep() == []
  assert len(calls) == 1


def test_sweep_skips_future_parks(owner_token, monkeypatch):
  del owner_token
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: calls.append(kw) or "notif-id"),
  )
  _seed_chat("sweep-future")
  _seed_run("sweep-future", "rt-sweep-future", status="parked",
            parked_until=datetime.now(UTC).replace(tzinfo=None)
            + timedelta(hours=1))

  assert _run_sweep() == []
  assert calls == []
  assert _run_row("rt-sweep-future")["status"] == "parked"


def test_sweep_stands_down_while_draining(owner_token, monkeypatch):
  del owner_token
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: calls.append(kw) or "notif-id"),
  )
  _due_park("sweep-drain", "rt-sweep-drain")

  chat_mod.draining = True
  try:
    assert _run_sweep() == []
  finally:
    chat_mod.draining = False
  assert calls == []
  assert _run_row("rt-sweep-drain")["status"] == "parked"


def test_sweep_resolves_deleted_chat_without_notify(owner_token, monkeypatch):
  del owner_token
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: calls.append(kw) or "notif-id"),
  )
  _due_park("sweep-deleted", "rt-sweep-deleted", deleted=True)

  resolved = _run_sweep()

  assert resolved == ["sweep-deleted"]
  assert calls == []
  assert _run_row("rt-sweep-deleted")["status"] == "parked_notified"


def test_sweep_processes_a_bounded_batch(owner_token, monkeypatch):
  del owner_token
  monkeypatch.setattr(chat_mod, "CONTINUATION_SWEEP_BATCH_SIZE", 2)
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: "notif-id"),
  )
  for suffix in ("a", "b", "c"):
    _due_park(f"sweep-batch-{suffix}", f"rt-sweep-batch-{suffix}")

  first = _run_sweep()

  assert len(first) == 2
  remaining = {
    token
    for token in ("rt-sweep-batch-a", "rt-sweep-batch-b", "rt-sweep-batch-c")
    if _run_row(token)["status"] == "parked"
  }
  assert len(remaining) == 1
  assert _run_sweep() == [next(iter(remaining)).removeprefix("rt-")]


# -- (e) auto-resume -----------------------------------------------------------

def test_sweep_auto_resume_off_by_default(owner_token, monkeypatch):
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: "notif-id"),
  )
  resumes = []

  async def _fake_resume(chat_id, provider_id, park_token=None):
    del provider_id, park_token
    resumes.append(chat_id)
    return True

  monkeypatch.setattr(chat_mod, "_auto_resume_chat", _fake_resume)
  _due_park("sweep-noauto", "rt-sweep-noauto")

  assert _run_sweep() == ["sweep-noauto"]
  # Notify-only: the setting is off, so nothing is resumed.
  assert resumes == []


def test_sweep_auto_resume_on_starts_one_staggered_continue(
  owner_token, monkeypatch,
):
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: "notif-id"),
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kw: scheduled.append(kw),
  )
  queued = [{"role": "user", "content": "queued ask", "ts": 5}]
  _due_park(
    "sweep-auto", "rt-sweep-auto", pending=list(queued), auto_resume=True,
  )
  with SessionLocal() as db:
    parked = db.get(models.ChatRun, "rt-sweep-auto")
    db.add(models.ChatRun(
      id="goal-logical-root", root_run_id="goal-logical-root",
      chat_id="sweep-auto", status="completed", provider="claude",
      started_at=parked.started_at - timedelta(seconds=1),
      goal_id="goal-stable-id", goal_objective="Finish the recovery",
    ))
    parked.root_run_id = "goal-logical-root"
    parked.goal_id = "goal-stable-id"
    parked.goal_objective = "Finish the recovery"
    db.commit()

  try:
    resolved = _run_sweep()

    assert resolved == ["sweep-auto"]
    assert _run_row("rt-sweep-auto")["status"] == "completed"
    assert len(scheduled) == 1
    assert scheduled[0]["chat_id"] == "sweep-auto"
    # The preserved queue + the synthetic "continue" were promoted into ONE
    # continuation turn (no per-message limit storm), queue first.
    promoted = scheduled[0]["next_user"]
    assert "queued ask" in promoted["content"]
    assert "continue" in promoted["content"]
    assert promoted["_messages"][-1]["kind"] == "continuation"
    assert promoted["_messages"][-1]["continuation_reason"] == "usage_limit"
    assert promoted["kind"] == "continuation"
    assert promoted["continuation_reason"] == "usage_limit"
    state = _chat_row("sweep-auto")
    assert state["pending"] == []
    assert state["running_status"] == "running"  # PromotePending set the marker
    with SessionLocal() as db:
      resumed = db.query(models.ChatRun).filter(
        models.ChatRun.chat_id == "sweep-auto",
        models.ChatRun.status == "running",
      ).one()
      assert resumed.root_run_id == "goal-logical-root"
      assert resumed.goal_id == "goal-stable-id"
      assert resumed.goal_objective == "Finish the recovery"
  finally:
    # _schedule_continuation was stubbed, so release the claim it would have
    # handed to the spawned turn.
    chat_mod.discard_starting("sweep-auto")


def test_sweep_auto_resumes_an_active_delegation_under_its_app_identity(
  owner_token, monkeypatch,
):
  """Quota suspension must not strand a bounded child behind its parent."""
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: "notif-id"),
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kw: scheduled.append(kw),
  )
  cid = "sweep-delegation"
  token = "rt-sweep-delegation"
  _due_park(cid, token, auto_resume=False)
  with SessionLocal() as db:
    app = models.App(
      slug="limit-resume-delegation",
      source_dir="/tmp/mobius-tests/limit-resume-delegation",
      name="Subagents", description="", jsx_source="",
    )
    db.add(app)
    db.flush()
    app_id = app.id
    db.add(models.Chat(
      id="sweep-delegation-parent", title="Parent", messages=[],
      provider="codex",
    ))
    child = db.get(models.Chat, cid)
    child.created_by_app_id = app_id
    physical = db.get(models.ChatRun, token)
    physical.initiated_by_app_id = app_id
    db.add(models.Delegation(
      id="limit-resume-delegation", app_id=app_id,
      parent_chat_id="sweep-delegation-parent",
      parent_root_run_id="parent-root", task_key="bounded",
      child_chat_id=cid, provider="claude", model="claude-opus-4-8",
      effort="low", scope="read", cwd="/data",
      prompt_sha256="digest",
    ))
    db.commit()

  try:
    assert _run_sweep() == [cid]
    assert len(scheduled) == 1
    assert scheduled[0]["chat_id"] == cid
    assert _run_row(scheduled[0]["run_token"])["initiated_by_app_id"] == app_id
    assert _run_row(token)["status"] == "completed"
  finally:
    chat_mod.discard_starting(cid)


def test_limit_handoff_hidden_result_preserves_goal_root_and_app(monkeypatch):
  """A hidden result is the resumed prompt; Continue stays transcript-only."""
  cid = "limit-hidden-result"
  token = f"rt-{cid}"
  goal_id = "goal-hidden-result"
  hidden = {
    "role": "user",
    "content": "The delegated audit found the exact defect.",
    "ts": 5,
    "cid": "delegation-result-hidden",
    "kind": "delegation_result",
    "source_work_id": goal_id,
    "hidden": True,
  }
  app_id = _delegated_limit_park(cid, token, pending=[hidden])
  with SessionLocal() as db:
    root = db.get(models.ChatRun, token)
    root.goal_id = goal_id
    root.goal_objective = "Finish the lifecycle repair"
    db.commit()
  scheduled = []
  def schedule(**kwargs):
    scheduled.append(kwargs)
    if len(scheduled) == 1:
      chat_mod.discard_starting(cid)
      return False
    return True

  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    schedule,
  )

  try:
    assert not asyncio.run(chat_mod._auto_resume_chat(cid, park_token=token))
    assert asyncio.run(chat_mod._auto_resume_chat(cid, park_token=token))
    assert len(scheduled) == 2
    next_user = scheduled[-1]["next_user"]
    assert next_user["content"] == hidden["content"]
    assert next_user["kind"] == "delegation_result"
    assert next_user["hidden"] is True
    assert [row["cid"] for row in next_user["_messages"]] == [
      hidden["cid"], f"limit-resume-{token}",
    ]
    resumed = _run_row(scheduled[-1]["run_token"])
    assert resumed["initiated_by_app_id"] == app_id
    with SessionLocal() as db:
      physical = db.get(models.ChatRun, scheduled[-1]["run_token"])
      assert physical.root_run_id == token
      assert physical.goal_id == goal_id
      assert physical.goal_objective == "Finish the lifecycle repair"
  finally:
    chat_mod.discard_starting(cid)


def test_cancelled_delegation_cannot_mint_a_limit_successor(monkeypatch):
  """Cancellation that wins before the atomic compare-and-swap stays final."""
  from app.delegations import mark_cancelled

  cid = "limit-cancel-before"
  token = f"rt-{cid}"
  _delegated_limit_park(cid, token)
  with SessionLocal() as db:
    row = db.get(models.Delegation, f"delegation-{cid}")
    mark_cancelled(db, row)
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )

  assert asyncio.run(chat_mod._auto_resume_chat(cid, park_token=token)) is False
  assert scheduled == []
  assert _run_row(chat_mod._auto_resume_run_token(token)) is None


def test_cancel_after_atomic_handoff_stops_and_cannot_reattach(monkeypatch):
  """Cancellation after commit retires the successor before any retry."""
  from app.delegations import cancel_delegation_execution

  cid = "limit-cancel-after"
  token = f"rt-{cid}"
  _delegated_limit_park(cid, token)
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )

  assert asyncio.run(chat_mod._auto_resume_chat(cid, park_token=token))
  resume_token = chat_mod._auto_resume_run_token(token)
  assert _run_row(resume_token)["status"] == "running"
  assert asyncio.run(cancel_delegation_execution(f"delegation-{cid}"))
  assert _run_row(resume_token)["status"] == "stopped"
  assert asyncio.run(chat_mod._auto_resume_chat(cid, park_token=token)) is False
  assert len(scheduled) == 1


def test_cancellation_serializes_with_due_auto_resume(monkeypatch):
  """A due retry cannot cross cancellation's stop-to-latch window."""
  from app.delegations import cancel_delegation_execution

  cid = "limit-cancel-resume-race"
  token = f"rt-{cid}"
  delegation_id = f"delegation-{cid}"
  _delegated_limit_park(cid, token)
  entered_finish, release_finish = _pause_finish_run(monkeypatch)

  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs) or True,
  )

  async def scenario():
    cancelling = asyncio.create_task(
      cancel_delegation_execution(delegation_id)
    )
    await entered_finish.wait()
    resuming = asyncio.create_task(
      chat_mod._auto_resume_chat(cid, park_token=token)
    )
    await asyncio.sleep(0)
    assert not resuming.done()
    release_finish.set()
    assert await cancelling is True
    assert await resuming is False

  asyncio.run(scenario())
  assert scheduled == []
  assert _run_row(token)["status"] == "stopped"
  assert _run_row(chat_mod._auto_resume_run_token(token)) is None
  with SessionLocal() as db:
    assert db.get(models.Delegation, delegation_id).cancelled_at is not None


def test_cancellation_serializes_with_committed_successor_reattach(
  monkeypatch,
):
  """An orphan reattach cannot relaunch between stop and cancellation."""
  from app.delegations import cancel_delegation_execution

  cid = "limit-cancel-reattach-race"
  token = f"rt-{cid}"
  delegation_id = f"delegation-{cid}"
  _delegated_limit_park(cid, token)
  scheduled = []

  def first_schedule(**kwargs):
    scheduled.append(kwargs)
    # Match the real scheduler failure path: its transient starting claim is
    # released while the committed successor remains the durable owner.
    chat_mod.discard_starting(cid)
    return False

  monkeypatch.setattr(chat_mod, "_schedule_continuation", first_schedule)
  assert asyncio.run(chat_mod._auto_resume_chat(cid, park_token=token)) is False
  successor = chat_mod._auto_resume_run_token(token)
  assert _run_row(successor)["status"] == "running"

  entered_finish, release_finish = _pause_finish_run(monkeypatch)
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs) or True,
  )

  async def scenario():
    cancelling = asyncio.create_task(
      cancel_delegation_execution(delegation_id)
    )
    await entered_finish.wait()
    reattaching = asyncio.create_task(
      chat_mod._auto_resume_chat(cid, park_token=token)
    )
    await asyncio.sleep(0)
    assert not reattaching.done()
    release_finish.set()
    assert await cancelling is True
    assert await reattaching is False

  asyncio.run(scenario())
  assert len(scheduled) == 1
  assert _run_row(successor)["status"] == "stopped"
  with SessionLocal() as db:
    assert db.get(models.Delegation, delegation_id).cancelled_at is not None


def test_notified_park_cancellation_serializes_with_explicit_retry(
  monkeypatch,
):
  """Try-now cannot revive a notified park while cancellation is latching."""
  from app.delegations import cancel_delegation_execution, retry_limit_park

  cid = "limit-cancel-notified-race"
  token = f"rt-{cid}"
  delegation_id = f"delegation-{cid}"
  _delegated_limit_park(cid, token, status="parked_notified")
  entered_finish, release_finish = _pause_finish_run(monkeypatch)

  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs) or True,
  )

  async def retry():
    with SessionLocal() as db:
      row = db.get(models.Delegation, delegation_id)
      return await retry_limit_park(db, row, run_token=token)

  async def scenario():
    cancelling = asyncio.create_task(
      cancel_delegation_execution(delegation_id)
    )
    await entered_finish.wait()
    retrying = asyncio.create_task(retry())
    await asyncio.sleep(0)
    assert not retrying.done()
    release_finish.set()
    assert await cancelling is True
    assert await retrying is False

  asyncio.run(scenario())
  assert scheduled == []
  assert _run_row(token)["status"] == "stopped"
  assert _run_row(chat_mod._auto_resume_run_token(token)) is None
  with SessionLocal() as db:
    row = db.get(models.Delegation, delegation_id)
    chat = db.get(models.Chat, cid)
    assert row.cancelled_at is not None
    assert chat.auto_resume_on_limit is False
    assert chat.auto_resume_on_restart is False


def test_second_quota_hit_gets_a_new_deterministic_attempt(monkeypatch):
  """A resumed child can park again without attaching to its prior retry."""
  cid = "limit-second-hit"
  first_park = f"rt-{cid}"
  app_id = _delegated_limit_park(cid, first_park)
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )

  try:
    assert asyncio.run(chat_mod._auto_resume_chat(cid, park_token=first_park))
    first_retry = chat_mod._auto_resume_run_token(first_park)
    chat_mod.discard_starting(cid)
    with SessionLocal() as db:
      retried = db.get(models.ChatRun, first_retry)
      retried.status = "resume_pending"
      retried.park_reason = "usage_limit"
      retried.parked_until = (
        datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1)
      )
      retried.ended_at = datetime.now(UTC).replace(tzinfo=None)
      db.commit()

    assert asyncio.run(chat_mod._auto_resume_chat(cid, park_token=first_retry))
    second_retry = chat_mod._auto_resume_run_token(first_retry)
    assert second_retry != first_retry
    assert scheduled[-1]["run_token"] == second_retry
    assert _run_row(second_retry)["initiated_by_app_id"] == app_id
    assert _run_row(first_retry)["status"] == "completed"
  finally:
    chat_mod.discard_starting(cid)


def test_limit_auto_resumes_are_staggered_not_blocked_by_live_work(
  owner_token, monkeypatch,
):
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: "notif-id"),
  )
  clock = [100.0]
  monkeypatch.setattr(chat_mod, "_limit_auto_resume_now", lambda: clock[0])
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  first, second = "stagger-a", "stagger-b"
  _due_park(first, f"rt-{first}", auto_resume=True)
  _due_park(second, f"rt-{second}", auto_resume=True)
  unrelated = _Handle(f"unrelated-live-{uuid.uuid4()}")
  registry.register(unrelated)

  try:
    assert len(_run_sweep()) == 1
    assert len(scheduled) == 1
    first_started = scheduled[0]["chat_id"]
    assert first_started in {first, second}

    # A repeated event-driven sweep inside the stagger does not launch the
    # rest of the due batch, even though its durable row stays retryable.
    assert _run_sweep() == []
    assert len(scheduled) == 1

    # Once the short stagger elapses, the next chat starts even while both the
    # unrelated turn and the first continuation are still live.
    clock[0] += chat_mod.LIMIT_AUTO_RESUME_STAGGER_SECS
    assert len(_run_sweep()) == 1
    assert {item["chat_id"] for item in scheduled} == {first, second}
  finally:
    registry.unregister(unrelated.chat_id, unrelated.kind)
    chat_mod.discard_starting(first)
    chat_mod.discard_starting(second)


def test_restart_park_auto_continues_with_product_marker(
  owner_token, monkeypatch,
):
  del owner_token
  notifications = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda *args, **kwargs: notifications.append(kwargs) or "notif-id"),
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  cid = "restart-auto"
  token = f"rt-{cid}"
  nonce = "restart-nonce-auto"
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce",
    lambda: nonce,
  )
  _due_park(
    cid, token, auto_restart=True,
    park_reason="restart", restart_nonce=nonce,
  )

  try:
    assert _run_sweep() == [cid]
    assert len(scheduled) == 1
    marker = scheduled[0]["next_user"]["_messages"][-1]
    assert marker["role"] == "user"
    assert marker["content"] == "continue"
    assert marker["kind"] == "continuation"
    assert marker["continuation_reason"] == "restart"
    assert marker["cid"] == f"restart-resume-{token}"
    state = _chat_row(cid)
    assert state["pending"] == []
    assert state["messages"][-1]["cid"] == f"restart-resume-{token}"
    assert _run_row(token)["status"] == "completed"
    assert notifications[0]["title"] == "Möbius restarted"
    assert "limit" not in notifications[0]["body"].lower()
  finally:
    chat_mod.discard_starting(cid)


def test_restart_consumes_a_hidden_owner_group_without_queueing_continue(
  owner_token, monkeypatch,
):
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async", _async_notify(lambda *args, **kwargs: "notif-id"),
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  cid = "restart-hidden-owner-group"
  token = f"rt-{cid}"
  nonce = "restart-nonce-hidden-owner-group"
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce", lambda: nonce,
  )
  _due_park(
    cid,
    token,
    auto_restart=True,
    park_reason="restart",
    restart_nonce=nonce,
    pending=[{
      "role": "user",
      "content": "hidden recovery payload",
      "ts": 3,
      "cid": "hidden-recovery",
      "hidden": True,
    }],
  )

  try:
    assert _run_sweep() == [cid]
    assert len(scheduled) == 1
    assert scheduled[0]["next_user"]["content"] == "hidden recovery payload"
    assert scheduled[0]["next_user"]["hidden"] is True
    state = _chat_row(cid)
    assert state["pending"] == []
    assert [row["cid"] for row in state["messages"][-2:]] == [
      "hidden-recovery", f"restart-resume-{token}",
    ]
  finally:
    chat_mod.discard_starting(cid)


def test_app_initiated_restart_preserves_attribution_and_continues(
  owner_token, monkeypatch,
):
  """The restart policy applies to app chats without losing ownership."""
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async", _async_notify(lambda *args, **kwargs: "notif-id"),
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  cid = "restart-app-attributed"
  token = f"rt-{cid}"
  nonce = "restart-nonce-app-attributed"
  app_id = 42
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce", lambda: nonce,
  )
  _due_park(
    cid,
    token,
    auto_restart=True,
    park_reason="restart",
    restart_nonce=nonce,
    initiated_by_app_id=app_id,
  )

  try:
    assert _run_sweep() == [cid]
    assert len(scheduled) == 1
    resumed_run = _run_row(scheduled[0]["run_token"])
    assert resumed_run["initiated_by_app_id"] == app_id
    marker = scheduled[0]["next_user"]["_messages"][-1]
    assert marker["kind"] == "continuation"
    assert marker["continuation_reason"] == "restart"
  finally:
    chat_mod.discard_starting(cid)


def test_restart_does_not_absorb_newly_queued_app_work(
  owner_token, monkeypatch,
):
  """A restart restores its exact run, not later unattended app messages."""
  del owner_token
  notifications = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda *args, **kwargs: notifications.append(kwargs) or "notif-id"),
  )
  resumes = []

  async def _fake_resume(*args, **kwargs):
    resumes.append((args, kwargs))
    return True

  monkeypatch.setattr(chat_mod, "_auto_resume_chat", _fake_resume)
  cid = "restart-app-work-queued"
  token = f"rt-{cid}"
  nonce = "restart-nonce-app-work-queued"
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce", lambda: nonce,
  )
  _due_park(
    cid,
    token,
    auto_restart=True,
    park_reason="restart",
    restart_nonce=nonce,
    pending=[{
      "role": "user",
      "content": "new app work",
      "ts": 3,
      "_initiated_by_app_id": 42,
    }],
  )

  assert _run_sweep() == [cid]
  assert resumes == []
  assert _run_row(token)["status"] == "interrupted"
  assert notifications[0]["title"] == "Möbius restarted"


def test_owner_message_queued_after_app_restart_takes_over_attribution(
  owner_token, monkeypatch,
):
  """New owner input outranks the parked app when forming the next run."""
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async", _async_notify(lambda *args, **kwargs: "notif-id"),
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  cid = "restart-app-owner-takeover"
  token = f"rt-{cid}"
  nonce = "restart-nonce-app-owner-takeover"
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce", lambda: nonce,
  )
  _due_park(
    cid,
    token,
    auto_restart=True,
    park_reason="restart",
    restart_nonce=nonce,
    initiated_by_app_id=42,
    pending=[{
      "role": "user",
      "content": "owner follow-up",
      "ts": 3,
    }],
  )

  try:
    assert _run_sweep() == [cid]
    assert len(scheduled) == 1
    resumed_run = _run_row(scheduled[0]["run_token"])
    assert resumed_run["initiated_by_app_id"] is None
    assert "owner follow-up" in scheduled[0]["next_user"]["content"]
  finally:
    chat_mod.discard_starting(cid)


def test_startup_sweep_uses_one_captured_restart_authorization(
  owner_token, monkeypatch,
):
  """The pre-yield pass must not depend on a second ledger read."""
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async", _async_notify(lambda *args, **kwargs: "notif-id"),
  )
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce",
    lambda: (_ for _ in ()).throw(AssertionError("unexpected reread")),
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  cid = "restart-captured-authorization"
  token = f"rt-{cid}"
  nonce = "restart-nonce-captured"
  _due_park(
    cid, token, auto_restart=True,
    park_reason="restart", restart_nonce=nonce,
  )

  db = SessionLocal()
  try:
    result = asyncio.run(chat_mod.sweep_reset_parks(
      db, restart_authorization=nonce,
    ))
  finally:
    db.close()

  try:
    assert list(result.resolved) == [cid]
    assert len(scheduled) == 1
  finally:
    chat_mod.discard_starting(cid)


def test_restart_park_waiting_on_question_stays_manual(
  owner_token, monkeypatch,
):
  del owner_token
  notifications = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda *args, **kwargs: notifications.append(kwargs) or "notif-id"),
  )
  resumes = []

  async def _fake_resume(*args, **kwargs):
    resumes.append((args, kwargs))
    return True

  monkeypatch.setattr(chat_mod, "_auto_resume_chat", _fake_resume)
  cid = "restart-question"
  token = f"rt-{cid}"
  nonce = "restart-nonce-question"
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce",
    lambda: nonce,
  )
  _due_park(
    cid,
    token,
    auto_restart=True,
    park_reason="restart",
    restart_nonce=nonce,
    messages=[
      {"role": "user", "content": "help me choose", "ts": 1},
      {
        "role": "assistant",
        "ts": 2,
        "blocks": [
          {
            "type": "error",
            "message": chat_mod.PAUSED_FOR_RESTART_MESSAGE,
            "pause": {"kind": "restart"},
          },
          {
            "type": "question",
            "question_id": "restart-q",
            "questions": [{"question": "Which one?"}],
          },
        ],
      },
    ],
  )
  # A parked turn waiting on a question carries the durable open-question marker
  # (QuestionCommit set it; ParkRun kept it across the restart). That marker is
  # what keeps auto-resume manual — the answer is the continuation.
  _set_pending_question(cid, "restart-q")

  assert _run_sweep() == [cid]
  assert resumes == []
  assert _run_row(f"rt-{cid}")["status"] == "interrupted"
  assert notifications[0]["title"] == "Möbius restarted"


def test_restart_park_policy_off_resolves_to_manual_interruption(
  owner_token, monkeypatch,
):
  del owner_token
  notifications = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda *args, **kwargs: notifications.append(kwargs) or "notif-id"),
  )
  cid = "restart-policy-off"
  token = f"rt-{cid}"
  nonce = "restart-nonce-policy"
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce",
    lambda: nonce,
  )
  # Usage-limit continuation is deliberately on. The independent restart
  # policy remains off, proving the causes do not share consent.
  _due_park(
    cid, token, auto_resume=True, auto_restart=False,
    park_reason="restart", restart_nonce=nonce,
  )

  assert _run_sweep() == [cid]
  assert _run_row(f"rt-{cid}")["status"] == "interrupted"
  assert notifications[0]["title"] == "Möbius restarted"
  assert "limit" not in notifications[0]["body"].lower()


def test_restart_park_without_current_boot_ack_stays_manual(
  owner_token, monkeypatch,
):
  """OOM after DB park but before supervisor acceptance cannot auto-replay."""
  del owner_token
  notifications = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda *args, **kwargs: notifications.append(kwargs) or "notif-id"),
  )
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce", lambda: None,
  )
  cid = "restart-no-ack"
  token = f"rt-{cid}"
  _due_park(
    cid, token, auto_restart=True, park_reason="restart",
    restart_nonce="unaccepted-nonce-1234",
  )

  assert _run_sweep() == [cid]
  assert _run_row(token)["status"] == "interrupted"
  assert _run_row(token)["restart_nonce"] is None
  assert notifications[0]["title"] == "Möbius restarted"


def test_restart_park_with_no_nonce_never_matches_missing_ack(
  owner_token, monkeypatch,
):
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async", _async_notify(lambda *args, **kwargs: "notif-id"),
  )
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce", lambda: None,
  )
  cid = "restart-no-nonce"
  token = f"rt-{cid}"
  _due_park(
    cid, token, auto_restart=True, park_reason="restart",
    restart_nonce=None,
  )

  assert _run_sweep() == [cid]
  assert _run_row(token)["status"] == "interrupted"


def test_restart_park_rejects_ack_for_the_wrong_nonce(
  owner_token, monkeypatch,
):
  del owner_token
  cid = "restart-wrong-ack"
  token = f"rt-{cid}"
  monkeypatch.setattr(
    "app.push.notify_owner_async", _async_notify(lambda *args, **kwargs: "notif-id"),
  )
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce",
    lambda: "different-nonce-1234",
  )
  _due_park(
    cid,
    token,
    auto_restart=True,
    park_reason="restart",
    restart_nonce="db-nonce-12345678",
  )

  assert _run_sweep() == [cid]
  assert _run_row(token)["status"] == "interrupted"
  assert _run_row(token)["restart_nonce"] is None


def test_restart_spawn_failure_reattaches_deterministic_successor(
  owner_token, monkeypatch,
):
  del owner_token
  cid = "restart-spawn-failure"
  token = f"rt-{cid}"
  nonce = "restart-nonce-spawn"
  monkeypatch.setattr(
    "app.push.notify_owner_async", _async_notify(lambda *args, **kwargs: "notif-id"),
  )
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce",
    lambda: nonce,
  )
  _due_park(
    cid, token, auto_restart=True, park_reason="restart",
    restart_nonce=nonce,
  )
  original_create_broadcast = chat_mod.create_broadcast

  def _spawn_fails(chat_id):
    del chat_id
    raise RuntimeError("spawn failed")

  monkeypatch.setattr(chat_mod, "create_broadcast", _spawn_fails)
  try:
    assert _run_sweep() == []
  finally:
    monkeypatch.setattr(
      chat_mod, "create_broadcast", original_create_broadcast,
    )

  row = _run_row(token)
  assert row["status"] == "completed"
  assert row["restart_nonce"] is None
  resume_token = chat_mod._auto_resume_run_token(token)
  assert _run_row(resume_token)["status"] == "running"
  state = _chat_row(cid)
  assert state["running_status"] == "running"
  assert state["pending"] == []
  assert state["messages"][-1]["cid"] == f"restart-resume-{token}"

  # Both the runtime wedge sweep and cold-start reconciliation preserve the
  # exact no-output successor. The consumed one-shot nonce is not needed to
  # trust a writer-committed continuation with a validated causal envelope.
  with SessionLocal() as db:
    predecessor = db.get(models.ChatRun, token)
    persisted = db.get(models.ChatRun, resume_token)
    predecessor.started_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
      minutes=11,
    )
    persisted.started_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(
      minutes=10,
    )
    db.commit()
  with SessionLocal() as db:
    assert asyncio.run(chat_mod.sweep_wedged_runs(db)) == []
  assert _run_row(resume_token)["status"] == "running"
  with SessionLocal() as db:
    assert chat_mod.safe_auto_resume_startup_orphan(
      db, db.get(models.Chat, cid), db.get(models.ChatRun, resume_token),
    )
    recovered = chat_mod.reconcile_startup_chats(db)
  assert cid not in recovered.manual
  assert _run_row(resume_token)["status"] == "running"

  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  try:
    assert _run_sweep() == [cid]
    assert len(scheduled) == 1
    assert scheduled[0]["run_token"] == resume_token
    assert scheduled[0]["next_user"]["cid"] == f"restart-resume-{token}"
  finally:
    chat_mod.discard_starting(cid)
  assert _chat_row(cid)["pending"] == []


def test_unacknowledged_restart_pending_cannot_bypass_via_idle_sweep(
  monkeypatch,
):
  cid = "restart-idle-bypass"
  token = f"rt-{cid}"
  _seed_chat(
    cid,
    pending=[{
      "role": "user",
      "content": "continue",
      "ts": 1,
      "cid": f"restart-resume-{token}",
      "kind": "auto_continuation",
      "continuation_reason": "restart",
    }],
  )
  _seed_run(
    cid, token, status="interrupted", park_reason="restart",
    restart_nonce=None,
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )

  db = SessionLocal()
  try:
    assert asyncio.run(chat_mod.sweep_idle_pending_chats(db)) == []
  finally:
    db.close()
  assert scheduled == []
  assert _chat_row(cid)["pending"]


def test_owner_send_releases_restart_manual_hold():
  """Manual recovery remains available after automatic replay fails closed."""
  cid = "restart-manual-owner-send"
  token = f"rt-{cid}"
  _seed_chat(cid)
  _seed_run(
    cid, token, status="interrupted", park_reason="restart",
    restart_nonce=None, started_offset=-60,
  )

  db = SessionLocal()
  try:
    assert chat_mod._restart_manual_hold_for_chat(db, cid) is True
  finally:
    db.close()

  get_writer().submit(StartTurn(
    chat_id=cid,
    run_token=f"{token}-manual",
    user_msg={
      "role": "user",
      "content": "continue",
      "ts": 2,
      "cid": f"manual-resume-{token}",
    },
    title_source="continue",
    default_provider="claude",
  )).result(timeout=5)

  db = SessionLocal()
  try:
    assert chat_mod._restart_manual_hold_for_chat(db, cid) is False
  finally:
    db.close()
  assert _run_row(f"{token}-manual")["status"] == "running"


def test_owner_send_drains_preserved_queue_and_releases_restart_hold():
  """The real stale-pending owner-send path is not blocked by the hold."""
  cid = "restart-manual-pending-send"
  token = f"rt-{cid}"
  _seed_chat(
    cid,
    pending=[{
      "role": "user",
      "content": "continue",
      "ts": 1,
      "cid": f"restart-resume-{token}",
      "kind": "auto_continuation",
      "continuation_reason": "restart",
    }],
  )
  _seed_run(
    cid, token, status="interrupted", park_reason="restart",
    restart_nonce=None, started_offset=-60,
  )
  promoted_token = f"{token}-owner"

  result = get_writer().submit(PromotePending(
    chat_id=cid,
    run_token=promoted_token,
  )).result(timeout=5)

  assert result["promoted"]["content"] == "continue"
  assert _chat_row(cid)["pending"] == []
  assert _run_row(promoted_token)["status"] == "running"
  db = SessionLocal()
  try:
    assert chat_mod._restart_manual_hold_for_chat(db, cid) is False
  finally:
    db.close()


def test_sweep_auto_resume_starts_while_unrelated_turn_is_live(
  owner_token, monkeypatch,
):
  del owner_token
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: calls.append(kw) or "notif-id"),
  )
  cid = "sweep-independent"
  _due_park(cid, f"rt-{cid}", auto_resume=True)
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  other = f"live-{uuid.uuid4()}"
  handle = _Handle(other)
  registry.register(handle)
  try:
    assert _run_sweep() == [cid]
  finally:
    registry.unregister(other, handle.kind)
    chat_mod.discard_starting(cid)
  assert len(calls) == 1
  assert len(scheduled) == 1
  assert _run_row(f"rt-{cid}")["status"] == "completed"


def test_live_turn_allows_enabled_and_notify_only_chats_to_resolve(
  owner_token, monkeypatch,
):
  """Unrelated work delays neither continuation nor reset notification."""
  del owner_token
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: calls.append(kw) or "notif-id"),
  )
  _due_park("sweep-opted", "rt-sweep-opted", auto_resume=True)
  _due_park("sweep-notify", "rt-sweep-notify")
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  other = f"live-{uuid.uuid4()}"
  handle = _Handle(other)
  registry.register(handle)
  try:
    assert set(_run_sweep()) == {"sweep-opted", "sweep-notify"}
  finally:
    registry.unregister(other, handle.kind)
    chat_mod.discard_starting("sweep-opted")

  assert _run_row("rt-sweep-opted")["status"] == "completed"
  assert _run_row("rt-sweep-notify")["status"] == "parked_notified"
  assert {call["source_id"] for call in calls} == {
    "sweep-opted", "sweep-notify",
  }
  assert [item["chat_id"] for item in scheduled] == ["sweep-opted"]


def test_notification_side_effects_run_after_resume_is_scheduled(
  owner_token, monkeypatch,
):
  """Slow/active push delivery is never on the continuation critical path."""
  del owner_token
  cid = "sweep-notify-race"
  blocker_id = f"live-{uuid.uuid4()}"
  blocker = _Handle(blocker_id)
  events = []

  def _notify(*args, **kwargs):
    del args
    events.append(("notify", kwargs["source_id"]))
    registry.register(blocker)
    return "notif-id"

  monkeypatch.setattr("app.push.notify_owner_async", _async_notify(_notify))
  scheduled = []
  def _schedule(**kw):
    events.append(("schedule", kw["chat_id"]))
    scheduled.append(kw)

  monkeypatch.setattr(chat_mod, "_schedule_continuation", _schedule)
  _due_park(cid, f"rt-{cid}", auto_resume=True)

  try:
    assert _run_sweep() == [cid]
    assert _run_row(f"rt-{cid}")["status"] == "completed"
    assert events == [("schedule", cid), ("notify", cid)]
  finally:
    registry.unregister(blocker_id, blocker.kind)
    chat_mod.discard_starting(cid)
  assert len(scheduled) == 1


def test_sweep_starts_only_one_of_two_opted_chats(owner_token, monkeypatch):
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async", _async_notify(lambda *args, **kwargs: "notif-id"),
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw),
  )
  _due_park("sweep-auto-a", "rt-sweep-auto-a", auto_resume=True)
  _due_park("sweep-auto-b", "rt-sweep-auto-b", auto_resume=True)

  try:
    resolved = _run_sweep()
    assert len(resolved) == 1
    assert len(scheduled) == 1
    untouched = ({"sweep-auto-a", "sweep-auto-b"} - set(resolved)).pop()
    assert _run_row(f"rt-{untouched}")["status"] == "parked"
  finally:
    for cid in ("sweep-auto-a", "sweep-auto-b"):
      chat_mod.discard_starting(cid)


def test_sweep_paces_restart_batch_without_waiting_for_live_turns(
  owner_token, monkeypatch,
):
  """A durable remainder advances even while prior recoveries stay live."""
  del owner_token
  nonce = "restart-nonce-batch"
  monkeypatch.setattr(
    "app.restart_ledger.authorized_restart_nonce", lambda: nonce,
  )
  events = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda *args, **kwargs: events.append(
      ("notify", kwargs["source_id"])
    ) or "notif-id"),
  )
  scheduled = []
  def _schedule(**kw):
    events.append(("schedule", kw["chat_id"]))
    scheduled.append(kw)

  monkeypatch.setattr(chat_mod, "_schedule_continuation", _schedule)
  chat_ids = ("restart-batch-a", "restart-batch-b", "restart-batch-c")
  for cid in chat_ids:
    _due_park(
      cid, f"rt-{cid}", auto_restart=True,
      park_reason="restart", restart_nonce=nonce,
    )

  try:
    first = _run_sweep_result()
    assert len(first.resolved) == chat_mod.RESTART_AUTO_RESUME_BATCH_SIZE
    assert first.restart_deferred is True

    # Do not settle/discard either launched turn. The next pass is paced by
    # launches, not by a global live-chat ceiling.
    second = _run_sweep_result()
    assert len(second.resolved) == 1
    assert second.restart_deferred is False
    assert {item["chat_id"] for item in scheduled} == set(chat_ids)
    assert {chat_id for kind, chat_id in events if kind == "notify"} == set(
      chat_ids
    )
    for cid in chat_ids:
      assert _run_row(f"rt-{cid}")["status"] == "completed"
  finally:
    for cid in chat_ids:
      chat_mod.discard_starting(cid)


@pytest.mark.parametrize(
  ("park_reason", "auto_resume", "expected_notifications"),
  [
    ("usage_limit", True, 1),
    ("storage", False, 0),
    ("memory", False, 0),
  ],
)
def test_auto_resume_spawn_failure_reattaches_deterministic_successor_once(
  owner_token, monkeypatch, park_reason, auto_resume,
  expected_notifications,
):
  """Every automatic park leaves one deterministic successor for retry."""
  del owner_token
  cid = "sweep-spawn-rollback"
  park_token = f"rt-{cid}"
  notifications = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda *args, **kwargs: notifications.append(kwargs) or "notif-id"),
  )
  clock = [100.0]
  monkeypatch.setattr(chat_mod, "_limit_auto_resume_now", lambda: clock[0])
  queued = {
    "role": "user", "content": "preserve me", "ts": 5,
    "cid": "queued-before-limit",
  }
  _due_park(
    cid, park_token, pending=[queued], auto_resume=auto_resume,
    park_reason=park_reason,
  )
  original_create_broadcast = chat_mod.create_broadcast

  def _spawn_fails(chat_id):
    del chat_id
    raise RuntimeError("spawn failed")

  monkeypatch.setattr(chat_mod, "create_broadcast", _spawn_fails)
  system_queue = chat_mod.get_system_broadcast().subscribe()
  try:
    assert _run_sweep() == []
    system_events = [system_queue.get_nowait(), system_queue.get_nowait()]
  finally:
    chat_mod.get_system_broadcast().unsubscribe(system_queue)
    monkeypatch.setattr(chat_mod, "create_broadcast", original_create_broadcast)
  assert len(notifications) == expected_notifications
  assert [event["type"] for event in system_events] == [
    "chat_run_started", "chat_run_finished",
  ]
  assert _run_row(park_token)["status"] == "completed"
  resume_token = chat_mod._auto_resume_run_token(park_token)
  assert _run_row(resume_token)["status"] == "running"
  state = _chat_row(cid)
  assert state["running_status"] == "running"
  assert state["pending"] == []
  assert [
    m.get("cid") for m in state["messages"][-2:]
  ] == ["queued-before-limit", f"limit-resume-{park_token}"]

  # The cold-start reconciler recognizes this exact no-output writer orphan
  # and leaves the same physical run for the reset sweep to reschedule.
  with SessionLocal() as db:
    assert chat_mod.safe_auto_resume_startup_orphan(
      db, db.get(models.Chat, cid), db.get(models.ChatRun, resume_token),
    )
    recovered = chat_mod.reconcile_startup_chats(db)
  assert cid not in recovered.manual
  assert _run_row(resume_token)["status"] == "running"

  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  clock[0] += chat_mod.LIMIT_AUTO_RESUME_STAGGER_SECS
  try:
    assert _run_sweep() == [cid]
    assert len(scheduled) == 1
    assert len(notifications) == expected_notifications
    assert "preserve me" in scheduled[0]["next_user"]["content"]
    assert scheduled[0]["run_token"] == resume_token
    state = _chat_row(cid)
    assert [
      m.get("cid") for m in state["messages"]
      if m.get("cid") in {
        "queued-before-limit", f"limit-resume-{park_token}",
      }
    ] == ["queued-before-limit", f"limit-resume-{park_token}"]
  finally:
    chat_mod.discard_starting(cid)


@pytest.mark.parametrize("park_reason", ["storage", "memory"])
def test_app_resource_spawn_failure_reattaches_with_original_attribution(
  owner_token, monkeypatch, park_reason,
):
  """A resource orphan keeps app ownership when no owner work took over."""
  del owner_token
  cid = f"sweep-app-{park_reason}-spawn"
  park_token = f"rt-{cid}"
  app_id = 42
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda *args, **kwargs: "notif-id"),
  )
  clock = [10**9]
  monkeypatch.setattr(chat_mod, "_limit_auto_resume_now", lambda: clock[0])
  _due_park(
    cid,
    park_token,
    auto_resume=False,
    park_reason=park_reason,
    initiated_by_app_id=app_id,
  )
  original_create_broadcast = chat_mod.create_broadcast

  def _spawn_fails(chat_id):
    del chat_id
    raise RuntimeError("spawn failed")

  monkeypatch.setattr(chat_mod, "create_broadcast", _spawn_fails)
  try:
    assert _run_sweep() == []
  finally:
    monkeypatch.setattr(chat_mod, "create_broadcast", original_create_broadcast)

  resume_token = chat_mod._auto_resume_run_token(park_token)
  assert _run_row(park_token)["status"] == "completed"
  assert _run_row(resume_token)["initiated_by_app_id"] == app_id
  with SessionLocal() as db:
    assert chat_mod.safe_auto_resume_startup_orphan(
      db, db.get(models.Chat, cid), db.get(models.ChatRun, resume_token),
    )

  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  clock[0] += chat_mod.LIMIT_AUTO_RESUME_STAGGER_SECS
  try:
    assert _run_sweep() == [cid]
    assert len(scheduled) == 1
    assert scheduled[0]["run_token"] == resume_token
    assert scheduled[0]["next_user"]["continuation_reason"] == park_reason
    assert _run_row(resume_token)["initiated_by_app_id"] == app_id
  finally:
    chat_mod.discard_starting(cid)


def test_post_promote_process_death_recovers_as_manual_resume_boundary():
  """SIGKILL after promote has no durable rollback payload.

  Boot reconciliation resolves that speculative run as an interrupted,
  resumable turn. This intentionally documents the narrow at-most-once window
  rather than claiming the reset sweep can reconstruct and auto-retry it.
  """
  cid = "auto-post-promote-crash"
  park_token = f"rt-{cid}"
  promoted_token = f"promoted-{cid}"
  synthetic = {
    "role": "user", "content": "continue", "ts": 5,
    "cid": f"limit-resume-{park_token}",
  }
  _seed_chat(cid, pending=[synthetic], auto_resume=True)
  _seed_run(
    cid, park_token, status="resume_pending",
    parked_until=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
    started_offset=-30,
  )
  get_writer().submit(PromotePending(
    chat_id=cid, run_token=promoted_token,
  )).result(timeout=5)

  # Simulate the next boot: the in-memory task/rollback payload disappeared,
  # while the promoted run marker survived.
  db = SessionLocal()
  try:
    assert chat_mod.reconcile_interrupted_chats(db) == [cid]
  finally:
    db.close()
  state = _chat_row(cid)
  assert state["running_status"] is None
  assert state["pending"] == []
  assert _run_row(park_token)["status"] == "completed"
  assert _run_row(promoted_token)["status"] == "interrupted"
  tail_blocks = state["messages"][-1].get("blocks") or []
  assert any(block.get("resumable") for block in tail_blocks)

  # reconcile normally runs before the writer starts. This test drives it
  # against the fixture's live actor, so clear its in-memory owner bookkeeping.
  get_writer().submit(FinishRun(
    chat_id=cid, run_token="",
  )).result(timeout=5)


def test_auto_resume_locked_claim_ignores_an_unrelated_live_chat(monkeypatch):
  """Unrelated work must not turn automatic continuation into global idle."""
  cid = "auto-global-claim-race"
  park_token = "rt-race"
  _seed_chat(cid, auto_resume=True)
  _seed_run(cid, park_token, status="resume_pending", started_offset=-30)
  other = f"live-{uuid.uuid4()}"
  blocker = _Handle(other)
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )

  async def scenario():
    lock = chat_mod.chat_queue.get_lock(cid)
    await lock.acquire()
    task = asyncio.create_task(
      chat_mod._auto_resume_chat(cid, park_token=park_token)
    )
    await asyncio.sleep(0)
    registry.register(blocker)
    lock.release()
    try:
      return await task
    finally:
      registry.unregister(other, blocker.kind)

  try:
    assert asyncio.run(scenario()) is True
    assert len(scheduled) == 1
    assert scheduled[0]["chat_id"] == cid
  finally:
    chat_mod.discard_starting(cid)


def test_auto_resume_locked_claim_rejects_superseded_park():
  """The selected park can become stale while the sweep waits on the lock."""
  cid = "auto-superseded-locked-claim"
  park_token = f"park-{cid}"
  _seed_chat(cid, auto_resume=True)
  _seed_run(cid, park_token, status="resume_pending", started_offset=-30)
  _seed_run(cid, f"newer-{cid}", status="completed", started_offset=-10)

  assert asyncio.run(
    chat_mod._auto_resume_chat(cid, park_token=park_token)
  ) is False
  assert _chat_row(cid)["pending"] == []
  assert not chat_mod.is_chat_running(cid)


def test_auto_resume_ignores_an_unrelated_terminal_broadcast(monkeypatch):
  """Another chat finalizing must not delay a due continuation."""
  from app.broadcast import create_broadcast

  cid = "auto-terminal-broadcast-gate"
  park_token = f"park-{cid}"
  other = f"terminal-{uuid.uuid4()}"
  _seed_chat(cid, auto_resume=True)
  _seed_run(cid, park_token, status="resume_pending", started_offset=-30)
  broadcast = create_broadcast(other)
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  try:
    assert asyncio.run(
      chat_mod._auto_resume_chat(cid, park_token=park_token)
    ) is True
  finally:
    broadcast.mark_completed()
    chat_mod.discard_starting(cid)
  assert len(scheduled) == 1
  assert scheduled[0]["chat_id"] == cid


def test_app_initiated_park_never_auto_resumes(owner_token, monkeypatch):
  """App-token turns are background work even though they own a ChatRun."""
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async", _async_notify(lambda db, owner_id, **kw: "notif-id"),
  )
  resumes = []

  async def _fake_resume(chat_id, provider_id, park_token=None):
    del provider_id, park_token
    resumes.append(chat_id)
    return True

  monkeypatch.setattr(chat_mod, "_auto_resume_chat", _fake_resume)
  cid = "sweep-app-background"
  _seed_chat(cid, auto_resume=True)
  _seed_run(
    cid,
    "rt-sweep-app-background",
    status="parked",
    parked_until=datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=1),
    initiated_by_app_id=42,
  )

  assert _run_sweep() == [cid]
  assert resumes == []


def test_app_attributed_pending_work_disables_auto_resume(
  owner_token, monkeypatch,
):
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async", _async_notify(lambda *args, **kwargs: "notif-id"),
  )
  resumes = []

  async def _fake_resume(chat_id, provider_id, park_token=None):
    resumes.append((chat_id, provider_id, park_token))
    return True

  monkeypatch.setattr(chat_mod, "_auto_resume_chat", _fake_resume)
  _due_park(
    "sweep-app-queued",
    "rt-sweep-app-queued",
    auto_resume=True,
    pending=[{
      "role": "user", "content": "app work", "ts": 3,
      "_initiated_by_app_id": 9,
    }],
  )

  assert _run_sweep() == ["sweep-app-queued"]
  assert resumes == []
  assert _run_row("rt-sweep-app-queued")["status"] == "parked_notified"


def test_auto_resume_rechecks_app_work_inside_queue_handoff():
  """The final locked check must reject app work that arrived after prepare."""
  cid = "auto-final-app-check"
  app_msg = {
    "role": "user", "content": "late app work", "ts": 7,
    "_initiated_by_app_id": 11,
  }
  _seed_chat(cid, auto_resume=True, pending=[app_msg])

  assert asyncio.run(
    chat_mod._auto_resume_chat(cid, park_token="rt-final-check")
  ) is False
  state = _chat_row(cid)
  assert state["pending"] == [app_msg]
  assert state["running_status"] is None
  assert not chat_mod.is_chat_running(cid)


def test_auto_resume_rechecks_app_run_at_locked_handoff():
  """A direct or stale sweep caller cannot bypass durable attribution."""
  cid = "auto-final-app-run-check"
  park_token = f"rt-{cid}"
  _seed_chat(cid, auto_resume=True)
  _seed_run(
    cid,
    park_token,
    status="resume_pending",
    started_offset=-30,
    initiated_by_app_id=11,
  )

  assert asyncio.run(
    chat_mod._auto_resume_chat(cid, park_token=park_token)
  ) is False
  assert _chat_row(cid)["pending"] == []
  assert _run_row(park_token)["status"] == "resume_pending"
  assert not chat_mod.is_chat_running(cid)


# -- (f) observability ----------------------------------------------------------

def test_debug_status_lists_parked_runs(client, auth):
  cid = "park-debug"
  _seed_chat(cid)
  until = datetime(2026, 7, 11, 1, 40)
  _seed_run(cid, "rt-park-debug", status="parked",
            parked_until=until, park_reason="usage_limit")

  r = client.get("/api/debug/status", headers=auth)

  assert r.status_code == 200
  entry = next(
    item for item in r.json()["parked_runs"] if item["chat_id"] == cid
  )
  assert entry["run_id"] == "rt-park-debug"
  assert entry["status"] == "parked"
  assert entry["parked_until"] == until.isoformat()
  assert entry["park_reason"] == "usage_limit"


# -- adversarial-review fixes (adjudicated 2026-07-11) --------------------------

def test_limit_exit_exception_with_empty_text_still_publishes():
  """H2: an exception exit whose str() is empty (a bare TimeoutError) must
  still persist an error block — otherwise finalize no-ops and the failed
  turn reads as clean."""
  sink = _Sink()
  kwargs = chat_mod._limit_exit(sink, None, "")

  assert kwargs == {"limit_reached": False}
  assert len(sink.events) == 1
  assert sink.events[0]["type"] == "error"
  assert sink.events[0]["message"]  # non-empty fallback text

  # A TERMINAL-result exit with no error text stays silent (a clean turn).
  quiet = _Sink()
  assert chat_mod._limit_exit(quiet, {"error": None}, None) == {
    "limit_reached": False,
  }
  assert quiet.events == []


def test_sweep_skips_notify_when_park_superseded_mid_sweep(
  owner_token, monkeypatch,
):
  """M1: when ResolvePark reports the row was no longer parked (an owner
  send's StartTurn closed it between the sweep's query and the resolve),
  the sweep must not push a 'limit reset' notification at the owner who is
  already driving the chat."""
  del owner_token
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: calls.append(kw) or "notif-id"),
  )

  async def _ack_not_parked(ack, timeout=None):
    del ack, timeout
    return False

  monkeypatch.setattr(chat_mod, "_await_ack", _ack_not_parked)
  _due_park("sweep-superseded", "rt-sweep-superseded")

  assert _run_sweep() == []
  assert calls == []


def test_parked_probe_tiebreak_is_deterministic():
  """L1: two runs sharing a started_at must resolve latest-run-wins by the
  id.desc() tiebreak — stable across reads, never SQLite's arbitrary tie
  order (consecutive sweeps must not flip between 'parked' and 'live')."""
  ts = datetime.now(UTC).replace(tzinfo=None)
  until = ts + timedelta(hours=1)

  def _seed_tie(cid, parked_token, running_token):
    _seed_chat(cid)
    db = SessionLocal()
    try:
      db.add(models.ChatRun(
        id=parked_token, chat_id=cid, status="parked",
        provider="claude", started_at=ts, parked_until=until,
      ))
      db.add(models.ChatRun(
        id=running_token, chat_id=cid, status="running",
        provider="claude", started_at=ts,
      ))
      db.commit()
    finally:
      db.close()

  # Parked row wins the id.desc() tie -> the park is honored.
  _seed_tie("tie-park-wins", "rt-z-park", "rt-a-run")
  # Running row wins the tie -> the park is hidden.
  _seed_tie("tie-run-wins", "rt-a-park", "rt-z-run")

  db = SessionLocal()
  try:
    assert chat_mod._parked_until_for_chat(db, "tie-park-wins") == until
    assert chat_mod._parked_until_for_chat(db, "tie-run-wins") is None
  finally:
    db.close()


def _limit_complete_turn(cid, *, parked_until, monkeypatch=None,
                         park_raises=False, park_returns_false=False):
  """Drive _complete_turn's limit branch with a real bc + sink + seeded run."""
  from app.broadcast import create_broadcast

  _seed_chat(cid)
  _seed_run(cid, f"rt-{cid}")
  bc = create_broadcast(cid)
  sink = chat_mod._ChatEventSink(bc, cid, run_token=f"rt-{cid}", recall_binding=EMPTY_RECALL_BINDING)
  sink.publish({"type": "text", "content": "partial answer"})
  sink.publish(chat_mod._limit_error_event(
    "hit your weekly limit · resets 1:40am", parked_until, "usage_limit",
  ))
  if park_raises:
    async def _boom(*a, **kw):
      raise RuntimeError("park exploded")
    monkeypatch.setattr(chat_mod, "_park_run_strict", _boom)
  elif park_returns_false:
    async def _not_parked(*a, **kw):
      return False
    monkeypatch.setattr(chat_mod, "_park_run_strict", _not_parked)

  db = SessionLocal()
  disposition = asyncio.run(chat_mod._complete_turn(
    bc=bc, sink=sink, db=db, chat_id=cid, run_gen=None,
    provider_id="claude", cost_usd=0, close_browser=False,
    limit_reached=True, parked_until=parked_until,
    park_reason="usage_limit",
  ))
  _drain_writer()
  return disposition


def test_park_failure_degrades_card_and_keeps_resume(monkeypatch):
  """H3: the parked card is published BEFORE ParkRun is durable; when the
  park fails, a follow-up plain resumable error must coalesce onto the same
  block (latest-event-wins) so the persisted card stops claiming a reset
  reminder the sweep will never fire."""
  cid = "park-degrade"
  until = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

  disposition = _limit_complete_turn(
    cid, parked_until=until, monkeypatch=monkeypatch, park_raises=True,
  )

  assert disposition.value == "failed_leave_marker"
  blocks = _chat_row(cid)["messages"][-1]["blocks"]
  errors = [b for b in blocks if b.get("type") == "error"]
  assert len(errors) == 1
  tail = errors[0]
  assert "could not be scheduled" in tail["message"]
  assert tail["resumable"] is True
  # The degraded follow-up carries no pause descriptor, so the coalesced block
  # stops rendering the "resets at …" card.
  assert "pause" not in tail


def test_park_false_result_uses_same_manual_recovery_fallback(monkeypatch):
  cid = "park-false-result"
  until = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

  disposition = _limit_complete_turn(
    cid,
    parked_until=until,
    monkeypatch=monkeypatch,
    park_returns_false=True,
  )

  assert disposition.value == "failed_leave_marker"
  assert _chat_row(cid)["running_status"] == "running"
  tail = _chat_row(cid)["messages"][-1]["blocks"][-1]
  assert "could not be scheduled" in tail["message"]
  assert not tail.get("pause")


def test_limit_park_releases_starting_claim_before_returning():
  """H1: the limit exit must release the send's `_starting` claim inside the
  terminal transition — not leave it held across the done publish + browser
  close — so a Resume tap right after `done` starts a fresh turn instead of
  queueing unpromoted until the next send."""
  cid = "park-release"
  until = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
  assert chat_mod.mark_starting(cid)  # the send's claim, normally held here

  try:
    disposition = _limit_complete_turn(cid, parked_until=until)

    assert disposition.value == "limit_parked"
    assert _run_row(f"rt-{cid}")["status"] == "parked"
    # The claim is gone and the chat reads idle: a Resume tap now takes the
    # fresh StartTurn path immediately.
    assert not chat_mod.is_chat_running(cid)
    assert chat_mod.mark_starting(cid)
  finally:
    chat_mod.discard_starting(cid)

# -- (h) platform-resource parks ----------------------------------------------

def test_sweep_continues_resource_park_without_limit_opt_in(
  owner_token, monkeypatch,
):
  del owner_token
  notified = []
  monkeypatch.setattr(
    "app.push.notify_owner_async",
    _async_notify(lambda db, owner_id, **kw: notified.append(kw) or "n"),
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw),
  )
  monkeypatch.setattr(chat_mod, "_limit_auto_resume_now", lambda: 10**9)
  _due_park(
    "sweep-storage", "rt-sweep-storage",
    auto_resume=False, park_reason="storage",
  )
  try:
    assert _run_sweep() == ["sweep-storage"]
    assert len(scheduled) == 1
    assert scheduled[0]["chat_id"] == "sweep-storage"
    assert notified == []
  finally:
    chat_mod.discard_starting("sweep-storage")


def test_admission_recovery_command_parks_exact_run():
  from app.chat_writer import RecoverWedgedRun

  cid = "defer-storage-park"
  _seed_chat(cid)
  _seed_run(cid, "rt-defer-storage-park")
  due = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=60)
  event = chat_mod._limit_error_event("waiting", due, "storage")
  event.pop("resumable", None)
  ack = get_writer().submit(RecoverWedgedRun(
    chat_id=cid,
    run_token="rt-defer-storage-park",
    interruption_block=event,
    parked_until=due,
    park_reason="storage",
  ))
  assert ack.result(timeout=5) is True

  row = _run_row("rt-defer-storage-park")
  assert row["status"] == "parked"
  assert row["park_reason"] == "storage"
  assert row["parked_until"] == due
  tail = _chat_row(cid)["messages"][-1]
  assert tail["blocks"][-1]["pause"]["kind"] == "storage"


def test_app_initiated_resource_park_preserves_attribution(
  owner_token, monkeypatch,
):
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner_async", _async_notify(lambda *args, **kwargs: "n"),
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw),
  )
  monkeypatch.setattr(chat_mod, "_limit_auto_resume_now", lambda: 10**9)
  cid = "sweep-app-storage"
  app_id = 42
  _due_park(
    cid, f"rt-{cid}", auto_resume=False, park_reason="storage",
    initiated_by_app_id=app_id,
  )
  try:
    assert _run_sweep() == [cid]
    assert len(scheduled) == 1
    resumed = _run_row(scheduled[0]["run_token"])
    assert resumed["initiated_by_app_id"] == app_id
    assert scheduled[0]["next_user"]["_messages"][-1][
      "continuation_reason"
    ] == "storage"
  finally:
    chat_mod.discard_starting(cid)
