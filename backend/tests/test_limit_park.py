"""Provider-limit parking (design §2.4): parse → park → sweep → resume.

Locks in the six contracts of the limit-park feature:

  (a) Reset-time parsing is lenient: structured value → text parse →
      30-minute fallback, clamped, and it NEVER raises.
  (b) A limit kill PARKS the run row (parked_until + park_reason) and
      clears the per-chat marker; ownership is identity-keyed like
      ClearRunStatus (a superseded run never parks onto a fresh marker).
  (c) Latest-run-wins: the park probe + stall exemption honor a park only
      while the chat's newest run row is the parked one, and a fresh
      StartTurn closes a stale park (no orphaned notify/auto-resume).
  (d) The reset sweep notifies exactly ONCE per park (resolve-first), skips
      future parks, stands down while draining, and resolves deleted chats
      silently.
  (e) Auto-resume is opt-in (off = notify only) and strictly serial: one
      park per tick, none while any turn is live; the resumed turn combines
      the preserved queue + a "continue" into one continuation.
  (f) The parks are observable: /api/debug/status lists parked runs.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from app import chat as chat_mod
from app import models
from app.chat_writer import (
  Barrier,
  ParkRun,
  ResolvePark,
  StartTurn,
  get_writer,
)
from app.database import SessionLocal
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


def _seed_chat(chat_id: str, *, pending=None, run_status=None, deleted=False):
  db = SessionLocal()
  try:
    db.add(models.Chat(
      id=chat_id,
      title="t",
      messages=[{"role": "user", "content": "do work", "ts": 1}],
      pending_messages=pending or [],
      session_id="sess",
      provider="claude",
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
              parked_until=None, park_reason=None, started_offset=0):
  db = SessionLocal()
  try:
    db.add(models.ChatRun(
      id=token,
      chat_id=chat_id,
      status=status,
      provider="claude",
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
      "messages": list(row.messages or []),
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
  # Explicit-UTC ISO so the client's Date() renders the right local time.
  assert event["parked_until"].endswith("+00:00")
  assert event["park_reason"] == kwargs["park_reason"]


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

def _due_park(cid: str, token: str, *, pending=None, deleted=False):
  _seed_chat(cid, pending=pending, deleted=deleted)
  _seed_run(cid, token, status="parked",
            parked_until=datetime.now(UTC).replace(tzinfo=None)
            - timedelta(minutes=1))


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

  # A second tick finds nothing parked — the notify fires exactly once.
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

  async def _fake_resume(chat_id, provider_id):
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
  monkeypatch.setattr(
    "app.providers.auto_resume_on_limit", lambda data_dir: True,
  )
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **kw: scheduled.append(kw),
  )
  queued = [{"role": "user", "content": "queued ask", "ts": 5}]
  _due_park("sweep-auto", "rt-sweep-auto", pending=list(queued))

  try:
    resolved = _run_sweep()

    assert resolved == ["sweep-auto"]
    assert _run_row("rt-sweep-auto")["status"] == "parked_notified"
    assert len(scheduled) == 1
    assert scheduled[0]["chat_id"] == "sweep-auto"
    # The preserved queue + the synthetic "continue" were promoted into ONE
    # continuation turn (no per-message limit storm), queue first.
    promoted = scheduled[0]["next_user"]
    assert "queued ask" in promoted["content"]
    assert "continue" in promoted["content"]
    state = _chat_row("sweep-auto")
    assert state["pending"] == []
    assert state["run_status"] == "running"  # PromotePending set the marker
  finally:
    # _schedule_continuation was stubbed, so release the claim it would have
    # handed to the spawned turn.
    chat_mod.discard_starting("sweep-auto")


def test_sweep_auto_resume_defers_while_any_turn_is_live(
  owner_token, monkeypatch,
):
  del owner_token
  calls = []
  monkeypatch.setattr(
    "app.push.notify_owner",
    lambda db, owner_id, **kw: calls.append(kw) or "notif-id",
  )
  monkeypatch.setattr(
    "app.providers.auto_resume_on_limit", lambda data_dir: True,
  )
  _due_park("sweep-serial", "rt-sweep-serial")
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
  assert entry["parked_until"] == until.isoformat()
  assert entry["park_reason"] == "usage_limit"
