"""Durable continuation (design §2.4): parse/drain → park → sweep → resume.

Locks in the six contracts of the limit-park feature:

  (a) Reset-time parsing is lenient: structured value → text parse →
      30-minute fallback, clamped, and it NEVER raises.
  (b) A limit kill PARKS the run row (parked_until + park_reason) and
      clears the per-chat marker; ownership is identity-keyed like
      ClearRunStatus (a superseded run never parks onto a fresh marker).
  (c) Latest-run-wins: the park probe + stall exemption honor a park only
      while the chat's newest run row is the parked one, and a fresh
      StartTurn closes a stale park (no orphaned notify/auto-resume).
  (d) The reset sweep makes at most one notification attempt per park, keeps
      an opted park
      retryable until its continuation starts, skips future parks, stands down
      while draining, and resolves deleted chats silently.
  (e) Auto-resume is opt-in (off = notify only) and strictly serial: one
      park per tick, none while any turn is live; the resumed turn combines
      the preserved queue + a "continue" into one continuation.
  (f) The parks are observable: /api/debug/status lists parked runs.
  (g) A planned restart reuses the same exact-run state with a due-now time;
      crashes, unanswered questions, and app-owned work stay manual.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from app import chat as chat_mod
from app import models
from app.chat_writer import (
  Barrier,
  ClearRunStatus,
  ParkRun,
  PrepareAutoResume,
  PromotePending,
  ResolvePark,
  RollbackAutoResume,
  StartTurn,
  get_writer,
)
from app.database import SessionLocal
from app.chat_transcript import materialized_messages
from app.runner_registry import RunnerKind, registry


NOW = datetime(2026, 7, 10, 22, 0, 0)


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
  chat_id: str, *, pending=None, run_status=None, deleted=False,
  auto_resume=False, messages=None,
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
      run_status=run_status,
      run_started_at=(
        datetime.now(UTC).replace(tzinfo=None) if run_status else None
      ),
      deleted_at=(
        datetime.now(UTC).replace(tzinfo=None) if deleted else None
      ),
    ))
    db.commit()
  finally:
    db.close()


def _seed_run(chat_id: str, token: str, *, status="running",
              parked_until=None, park_reason=None, started_offset=0,
              initiated_by_app_id=None):
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
      "ended_at": run.ended_at,
    }
  finally:
    db.close()


def _chat_row(chat_id: str):
  db = SessionLocal()
  try:
    row = db.query(models.Chat).filter(models.Chat.id == chat_id).first()
    return {
      "run_status": row.run_status,
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
  _seed_chat(cid, run_status="running")
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
  assert _chat_row(cid)["run_status"] is None


def test_park_run_missing_exact_row_keeps_generic_marker():
  """Losing the exact identity must fail safe into manual crash recovery."""
  cid = "park-missing-exact-row"
  _seed_chat(cid, run_status="running")

  parked = get_writer().submit(ParkRun(
    chat_id=cid, run_token="rt-does-not-exist",
    parked_until=datetime(2026, 7, 11, 1, 40), park_reason="restart",
  )).result(timeout=5)

  assert parked is False
  assert _chat_row(cid)["run_status"] == "running"


def test_park_run_superseded_owner_completes_without_parking():
  """A dying run whose marker a fresh turn took must NOT park (a stale park
  would fire a spurious notify) — its own row closes 'completed' and the
  fresh turn's marker survives, mirroring ClearRunStatus ownership."""
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
  assert _chat_row(cid)["run_status"] == "running"


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


def test_auto_resume_rollback_cannot_unwind_a_newer_successor():
  cid = "park-stale-rollback"
  park_token = "rt-stale-rollback-park"
  promoted_token = "rt-stale-rollback-promoted"
  successor_token = "rt-stale-rollback-successor"
  queued = {
    "role": "user", "content": "continue", "ts": 5,
    "cid": f"limit-resume-{park_token}",
  }
  _seed_chat(cid, pending=[queued])
  _seed_run(cid, park_token, status="resume_pending", started_offset=-30)
  get_writer().submit(PromotePending(
    chat_id=cid, run_token=promoted_token,
  )).result(timeout=5)
  get_writer().submit(StartTurn(
    chat_id=cid,
    run_token=successor_token,
    user_msg={
      "role": "user", "content": "new owner turn", "ts": 10,
      "cid": "new-owner-turn",
    },
    title_source="new owner turn",
    default_provider="claude",
  )).result(timeout=5)

  rolled_back = get_writer().submit(RollbackAutoResume(
    chat_id=cid,
    run_token=park_token,
    promoted_run_token=promoted_token,
    promoted_pending=[queued],
  )).result(timeout=5)
  assert rolled_back is False
  assert _run_row(successor_token)["status"] == "running"
  state = _chat_row(cid)
  assert state["run_status"] == "running"
  assert state["messages"][-1]["cid"] == "new-owner-turn"

  get_writer().submit(ClearRunStatus(
    chat_id=cid, run_token=successor_token,
  )).result(timeout=5)


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
    # A NEWER running row (a fresh turn) hides the stale park immediately,
    # so the stall watchdog can never be wrongly exempted by it.
    _seed_run(cid, "rt-latest-new", status="running", started_offset=60)
    db.expire_all()
    assert chat_mod._parked_until_for_chat(db, cid) is None
  finally:
    db.close()


def test_stall_exemption_reports_parked():
  cid = "park-exempt"
  _seed_chat(cid)
  future = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
  _seed_run(cid, "rt-exempt", status="parked", parked_until=future)

  db = SessionLocal()
  try:
    assert chat_mod._stall_exemption(db, cid) == "parked"
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


def test_park_run_strict_tokenless_falls_back_to_marker_clear():
  cid = "park-tokenless"
  _seed_chat(cid, run_status="running")

  asyncio.run(chat_mod._park_run_strict(
    cid, "", datetime(2026, 7, 11, 1, 40), "rate_limit",
  ))
  _drain_writer()

  assert _chat_row(cid)["run_status"] is None


# -- (d) the reset sweep -------------------------------------------------------

def _due_park(
  cid: str, token: str, *, pending=None, deleted=False, auto_resume=False,
  park_reason=None, messages=None,
):
  _seed_chat(
    cid, pending=pending, deleted=deleted, auto_resume=auto_resume,
    messages=messages,
  )
  _seed_run(cid, token, status="parked",
            parked_until=datetime.now(UTC).replace(tzinfo=None)
            - timedelta(minutes=1), park_reason=park_reason)


def _run_sweep():
  db = SessionLocal()
  try:
    return asyncio.run(chat_mod.sweep_reset_parks(db))
  finally:
    db.close()


def test_sweep_notifies_once_and_resolves(owner_token, monkeypatch):
  del owner_token  # fixture creates the Owner row the notify needs
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner",
    lambda db, owner_id, **kw: calls.append(kw) or "notif-id",
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
    "app.push.notify_owner",
    lambda db, owner_id, **kw: calls.append(kw) or "notif-id",
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
    "app.push.notify_owner",
    lambda db, owner_id, **kw: calls.append(kw) or "notif-id",
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
    "app.push.notify_owner",
    lambda db, owner_id, **kw: calls.append(kw) or "notif-id",
  )
  _due_park("sweep-deleted", "rt-sweep-deleted", deleted=True)

  resolved = _run_sweep()

  assert resolved == ["sweep-deleted"]
  assert calls == []
  assert _run_row("rt-sweep-deleted")["status"] == "parked_notified"


# -- (e) auto-resume -----------------------------------------------------------

def test_sweep_auto_resume_off_by_default(owner_token, monkeypatch):
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner",
    lambda db, owner_id, **kw: "notif-id",
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


def test_sweep_auto_resume_on_starts_one_serial_continue(
  owner_token, monkeypatch,
):
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner",
    lambda db, owner_id, **kw: "notif-id",
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
    assert promoted["_messages"][-1]["kind"] == "auto_continuation"
    assert promoted["_messages"][-1]["continuation_reason"] == "usage_limit"
    state = _chat_row("sweep-auto")
    assert state["pending"] == []
    assert state["run_status"] == "running"  # PromotePending set the marker
  finally:
    # _schedule_continuation was stubbed, so release the claim it would have
    # handed to the spawned turn.
    chat_mod.discard_starting("sweep-auto")


def test_restart_park_auto_continues_with_product_marker(
  owner_token, monkeypatch,
):
  del owner_token
  notifications = []
  monkeypatch.setattr(
    "app.push.notify_owner",
    lambda *args, **kwargs: notifications.append(kwargs) or "notif-id",
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  cid = "restart-auto"
  token = f"rt-{cid}"
  _due_park(cid, token, auto_resume=True, park_reason="restart")

  try:
    assert _run_sweep() == [cid]
    assert len(scheduled) == 1
    marker = scheduled[0]["next_user"]["_messages"][-1]
    assert marker["role"] == "user"
    assert marker["content"] == "continue"
    assert marker["kind"] == "auto_continuation"
    assert marker["continuation_reason"] == "restart"
    assert marker["cid"] == f"restart-resume-{token}"
    assert notifications[0]["title"] == "Möbius restarted"
    assert "limit" not in notifications[0]["body"].lower()
  finally:
    chat_mod.discard_starting(cid)


def test_restart_park_waiting_on_question_stays_manual(
  owner_token, monkeypatch,
):
  del owner_token
  notifications = []
  monkeypatch.setattr(
    "app.push.notify_owner",
    lambda *args, **kwargs: notifications.append(kwargs) or "notif-id",
  )
  resumes = []

  async def _fake_resume(*args, **kwargs):
    resumes.append((args, kwargs))
    return True

  monkeypatch.setattr(chat_mod, "_auto_resume_chat", _fake_resume)
  cid = "restart-question"
  _due_park(
    cid,
    f"rt-{cid}",
    auto_resume=True,
    park_reason="restart",
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
            "questions": [{"question": "Which one?"}],
          },
        ],
      },
    ],
  )

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
    "app.push.notify_owner",
    lambda *args, **kwargs: notifications.append(kwargs) or "notif-id",
  )
  cid = "restart-policy-off"
  _due_park(cid, f"rt-{cid}", auto_resume=False, park_reason="restart")

  assert _run_sweep() == [cid]
  assert _run_row(f"rt-{cid}")["status"] == "interrupted"
  assert notifications[0]["title"] == "Möbius restarted"
  assert "limit" not in notifications[0]["body"].lower()


def test_sweep_auto_resume_defers_while_any_turn_is_live(
  owner_token, monkeypatch,
):
  del owner_token
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner",
    lambda db, owner_id, **kw: calls.append(kw) or "notif-id",
  )
  _due_park("sweep-serial", "rt-sweep-serial", auto_resume=True)
  # An unrelated live turn: strictly-serial auto-resume must wait for it.
  other = f"live-{uuid.uuid4()}"
  handle = _Handle(other)
  registry.register(handle)
  try:
    assert _run_sweep() == []
  finally:
    registry.unregister(other, handle.kind)
  # Untouched: still parked, not notified — deferred to a later tick, never
  # lost.
  assert calls == []
  assert _run_row("rt-sweep-serial")["status"] == "parked"


def test_live_turn_defers_opted_chat_but_not_notify_only_chat(
  owner_token, monkeypatch,
):
  """One chat's opt-in must not hold another chat's reset notice hostage."""
  del owner_token
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner",
    lambda db, owner_id, **kw: calls.append(kw) or "notif-id",
  )
  _due_park("sweep-opted", "rt-sweep-opted", auto_resume=True)
  _due_park("sweep-notify", "rt-sweep-notify")
  other = f"live-{uuid.uuid4()}"
  handle = _Handle(other)
  registry.register(handle)
  try:
    assert _run_sweep() == ["sweep-notify"]
  finally:
    registry.unregister(other, handle.kind)

  assert _run_row("rt-sweep-opted")["status"] == "parked"
  assert _run_row("rt-sweep-notify")["status"] == "parked_notified"
  assert [call["source_id"] for call in calls] == ["sweep-notify"]


def test_auto_resume_race_after_notify_stays_retryable(
  owner_token, monkeypatch,
):
  """A turn starting during the notify window must not consume auto-resume."""
  del owner_token
  cid = "sweep-notify-race"
  blocker_id = f"live-{uuid.uuid4()}"
  blocker = _Handle(blocker_id)
  notifications = []

  def _notify(*args, **kwargs):
    del args
    notifications.append(kwargs)
    registry.register(blocker)
    return "notif-id"

  monkeypatch.setattr("app.push.notify_owner", _notify)
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kw: scheduled.append(kw),
  )
  _due_park(cid, f"rt-{cid}", auto_resume=True)

  try:
    assert _run_sweep() == []
    assert _run_row(f"rt-{cid}")["status"] == "resume_pending"
    assert len(notifications) == 1
  finally:
    registry.unregister(blocker_id, blocker.kind)

  try:
    assert _run_sweep() == [cid]
    assert len(scheduled) == 1
    assert len(notifications) == 1
    assert _run_row(f"rt-{cid}")["status"] == "completed"
  finally:
    chat_mod.discard_starting(cid)


def test_sweep_starts_only_one_of_two_opted_chats(owner_token, monkeypatch):
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner", lambda *args, **kwargs: "notif-id",
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


def test_auto_resume_spawn_failure_rolls_back_and_retries_once(
  owner_token, monkeypatch,
):
  """A failed task spawn restores the park + exact queue, without re-notify."""
  del owner_token
  cid = "sweep-spawn-rollback"
  park_token = f"rt-{cid}"
  notifications = []
  monkeypatch.setattr(
    "app.push.notify_owner",
    lambda *args, **kwargs: notifications.append(kwargs) or "notif-id",
  )
  queued = {
    "role": "user", "content": "preserve me", "ts": 5,
    "cid": "queued-before-limit",
  }
  _due_park(
    cid, park_token, pending=[queued], auto_resume=True,
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
  assert len(notifications) == 1
  assert [event["type"] for event in system_events] == [
    "chat_run_started", "chat_run_finished",
  ]
  assert _run_row(park_token)["status"] == "resume_pending"
  state = _chat_row(cid)
  assert state["run_status"] is None
  assert [m.get("cid") for m in state["pending"]] == [
    "queued-before-limit", f"limit-resume-{park_token}",
  ]
  assert all(
    m.get("cid") not in {"queued-before-limit", f"limit-resume-{park_token}"}
    for m in state["messages"]
  )

  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kwargs: scheduled.append(kwargs),
  )
  try:
    assert _run_sweep() == [cid]
    assert len(scheduled) == 1
    assert len(notifications) == 1
    assert "preserve me" in scheduled[0]["next_user"]["content"]
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
  assert state["run_status"] is None
  assert state["pending"] == []
  assert _run_row(park_token)["status"] == "completed"
  assert _run_row(promoted_token)["status"] == "interrupted"
  tail_blocks = state["messages"][-1].get("blocks") or []
  assert any(block.get("resumable") for block in tail_blocks)

  # reconcile normally runs before the writer starts. This test drives it
  # against the fixture's live actor, so clear its in-memory owner bookkeeping.
  get_writer().submit(ClearRunStatus(
    chat_id=cid, run_token="",
  )).result(timeout=5)


def test_auto_resume_global_idle_check_is_repeated_at_locked_claim():
  """A different chat starting while this chat waits on its lock wins."""
  cid = "auto-global-claim-race"
  _seed_chat(cid, auto_resume=True)
  other = f"live-{uuid.uuid4()}"
  blocker = _Handle(other)

  async def scenario():
    lock = chat_mod.chat_queue.get_lock(cid)
    await lock.acquire()
    task = asyncio.create_task(
      chat_mod._auto_resume_chat(cid, "claude", park_token="rt-race")
    )
    await asyncio.sleep(0)
    registry.register(blocker)
    lock.release()
    try:
      return await task
    finally:
      registry.unregister(other, blocker.kind)

  assert asyncio.run(scenario()) is False
  assert _chat_row(cid)["pending"] == []
  assert not chat_mod.is_chat_running(cid)


def test_auto_resume_locked_claim_rejects_superseded_park():
  """The selected park can become stale while the sweep waits on the lock."""
  cid = "auto-superseded-locked-claim"
  park_token = f"park-{cid}"
  _seed_chat(cid, auto_resume=True)
  _seed_run(cid, park_token, status="resume_pending", started_offset=-30)
  _seed_run(cid, f"newer-{cid}", status="completed", started_offset=-10)

  assert asyncio.run(
    chat_mod._auto_resume_chat(cid, "claude", park_token=park_token)
  ) is False
  assert _chat_row(cid)["pending"] == []
  assert not chat_mod.is_chat_running(cid)


def test_auto_resume_global_gate_includes_running_terminal_broadcast():
  """Provider unregister is not idle until its broadcast completes."""
  from app.broadcast import create_broadcast

  cid = "auto-terminal-broadcast-gate"
  park_token = f"park-{cid}"
  other = f"terminal-{uuid.uuid4()}"
  _seed_chat(cid, auto_resume=True)
  _seed_run(cid, park_token, status="resume_pending", started_offset=-30)
  broadcast = create_broadcast(other)
  try:
    assert asyncio.run(
      chat_mod._auto_resume_chat(cid, "claude", park_token=park_token)
    ) is False
  finally:
    broadcast.mark_completed()
  assert _chat_row(cid)["pending"] == []
  assert not chat_mod.is_chat_running(cid)


def test_app_initiated_park_never_auto_resumes(owner_token, monkeypatch):
  """App-token turns are background work even though they own a ChatRun."""
  del owner_token
  monkeypatch.setattr(
    "app.push.notify_owner", lambda db, owner_id, **kw: "notif-id",
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
    "app.push.notify_owner", lambda *args, **kwargs: "notif-id",
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
    chat_mod._auto_resume_chat(cid, "claude", park_token="rt-final-check")
  ) is False
  state = _chat_row(cid)
  assert state["pending"] == [app_msg]
  assert state["run_status"] is None
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
    chat_mod._auto_resume_chat(cid, "claude", park_token=park_token)
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
    "app.push.notify_owner",
    lambda db, owner_id, **kw: calls.append(kw) or "notif-id",
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

  _seed_chat(cid, run_status="running")
  _seed_run(cid, f"rt-{cid}")
  bc = create_broadcast(cid)
  sink = chat_mod._ChatEventSink(bc, cid, run_token=f"rt-{cid}")
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
  assert _chat_row(cid)["run_status"] == "running"
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
