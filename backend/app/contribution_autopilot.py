"""Autopilot lifecycle for Contribute PRs — the platform-owned trust anchor.

After the owner clicks Send with autopilot on, the Contribute app's background
cron (job.sh) detects each new review / failing check / conflict and asks the
platform to respond. This module owns the state that decides whether a response
may run and drives the round: the ``ContributionAutopilot`` DB row is written
ONLY here and by the submit endpoints, so nothing an agent can write (the app
ledger, the diff, the worktree) can authorize an action or forge a claim.

The lifecycle, per (app_id, record_id):

  grant (submit)  ─▶  idle
  /respond  ─▶  claim (state=responding, fresh run_id, 45-min lease) ─▶ spawn round
  agent  ─▶  /update (validated push) / /reply ─▶ /complete ─▶ idle, round logged
  crash   ─▶  lease expires ─▶ next retry marks it stale and reclaims it
  2 consecutive stale/failed rounds, or five-round limit reached ─▶ escalate
  escalate ─▶ human_required attention + owner notification, claim released
  merged / closed ─▶ close_out, autopilot ends

Round identity is the ``run_id`` (a fresh uuid per claim): every agent-called
endpoint must present it, so a zombie agent from a reclaimed round holds a dead
id and can do nothing. The ledger's ``autopilot`` block is a one-way mirror of
this row for the app UI and cron — never read back for enforcement.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import and_, update
from sqlalchemy.orm import Session

from app import fs_locks, models
from app.config import get_settings
from app.timeutil import now_naive_utc

log = logging.getLogger("mobius.contribution_autopilot")

# One round may hold the claim this long before the sweep treats it as crashed.
LEASE_SECONDS = 45 * 60
DEFAULT_MAX_ROUNDS = 5
# Two consecutive non-productive rounds (stale lease or failed spawn) escalate.
FAILURE_ESCALATION_THRESHOLD = 2
# Cap the mirrored/audit round log so a record can't grow without bound.
MAX_ROUND_LOG = 30
MAX_IGNORED_EVENT_URLS = 20

# Outcomes that mean the round did useful work (resets the failure counter and
# advances the round count/cursor). Anything else is a non-productive round.
PRODUCTIVE_OUTCOMES = frozenset({"pushed", "replied"})


# ─────────────────────────── row access ────────────────────────────


def get_row(
  db: Session, app_id: int, record_id: str
) -> models.ContributionAutopilot | None:
  return (
    db.query(models.ContributionAutopilot)
    .filter(
      models.ContributionAutopilot.app_id == app_id,
      models.ContributionAutopilot.record_id == record_id,
    )
    .first()
  )


def stamp_grant(
  db: Session,
  app_id: int,
  record_id: str,
  *,
  head_sha: str | None,
  target_repo: str | None = None,
  target_pr_number: int | None = None,
  target_head_repository: str | None = None,
  target_branch: str | None = None,
  target_repo_path: str | None = None,
  max_rounds: int = DEFAULT_MAX_ROUNDS,
) -> models.ContributionAutopilot:
  """Idempotent grant upsert — the ONLY authorization autopilot consults.

  Called from the submit success path (the request carrying the owner's click).
  A re-send refreshes ``granted_head_sha`` and re-enables a previously paused
  grant, but never wipes the round log or cursor.
  """
  row = get_row(db, app_id, record_id)
  now = now_naive_utc()
  if row is None:
    row = models.ContributionAutopilot(
      app_id=app_id,
      record_id=record_id,
      enabled=True,
      granted_at=now,
      granted_head_sha=head_sha,
      target_repo=target_repo,
      target_pr_number=target_pr_number,
      target_head_repository=target_head_repository,
      target_branch=target_branch,
      target_repo_path=target_repo_path,
      state="idle",
      max_rounds=max_rounds,
      rounds_json=[],
      created_at=now,
      updated_at=now,
    )
    db.add(row)
  else:
    row.enabled = True
    row.granted_at = now
    row.granted_head_sha = head_sha
    row.target_repo = target_repo
    row.target_pr_number = target_pr_number
    row.target_head_repository = target_head_repository
    row.target_branch = target_branch
    row.target_repo_path = target_repo_path
    row.updated_at = now
  db.commit()
  return row


def _lease_expired(row: models.ContributionAutopilot) -> bool:
  return bool(
    row.state == "responding"
    and row.lease_expires_at is not None
    and row.lease_expires_at <= now_naive_utc()
  )


def _release_claim(row: models.ContributionAutopilot) -> None:
  row.state = "idle"
  row.run_id = None
  row.attention_key = None
  row.claimed_event_at = None
  row.round_action = None
  row.round_head_sha = None
  row.claimed_at = None
  row.lease_expires_at = None


# ─────────────────────────── claim / rounds ─────────────────────────


def canonical_event_at(value: object) -> str | None:
  """Return one UTC representation suitable for chronological string compare."""
  raw = str(value or "").strip()
  if not raw:
    return None
  try:
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
  except ValueError as exc:
    raise ValueError("event_at must be an ISO-8601 timestamp") from exc
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone.utc)
  parsed = parsed.astimezone(timezone.utc)
  return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _claim_match(
  app_id: int, record_id: str, run_id: str,
):
  return and_(
    models.ContributionAutopilot.app_id == app_id,
    models.ContributionAutopilot.record_id == record_id,
    models.ContributionAutopilot.enabled.is_(True),
    models.ContributionAutopilot.state == "responding",
    models.ContributionAutopilot.run_id == run_id,
  )


def _release_values() -> dict:
  return {
    "state": "idle",
    "run_id": None,
    "attention_key": None,
    "claimed_event_at": None,
    "round_action": None,
    "round_head_sha": None,
    "claimed_at": None,
    "lease_expires_at": None,
  }


def _append_round_value(row: models.ContributionAutopilot, entry: dict) -> list:
  rounds = list(row.rounds_json or [])
  rounds.append(entry)
  return rounds[-MAX_ROUND_LOG:]


def _nonproductive_entry(
  row: models.ContributionAutopilot, *, outcome: str, summary: str,
) -> dict:
  return {
    "attention_key": row.attention_key,
    "run_id": row.run_id,
    "started_at": row.claimed_at.isoformat() if row.claimed_at else None,
    "finished_at": now_naive_utc().isoformat(),
    "outcome": outcome,
    "summary": summary,
    "head_sha": row.granted_head_sha,
  }


def _reclaim_expired(
  db: Session, row: models.ContributionAutopilot,
) -> tuple[bool, int]:
  """CAS-release one exact expired claim and record its failed round."""
  failures = int(row.consecutive_failures or 0) + 1
  values = {
    **_release_values(),
    "rounds_json": _append_round_value(
      row, _nonproductive_entry(
        row, outcome="stale", summary="Round lease expired.",
      ),
    ),
    "consecutive_failures": failures,
    "updated_at": now_naive_utc(),
  }
  result = db.execute(
    update(models.ContributionAutopilot)
    .where(
      _claim_match(row.app_id, row.record_id, str(row.run_id or "")),
      models.ContributionAutopilot.lease_expires_at
      == row.lease_expires_at,
      models.ContributionAutopilot.lease_expires_at <= now_naive_utc(),
    )
    .values(**values)
  )
  db.commit()
  return result.rowcount == 1, failures


def claim_for_round(
  db: Session,
  app_id: int,
  record_id: str,
  *,
  attention_key: str,
  event_at: str | None,
) -> dict:
  """Attempt to claim the record for one response round.

  Returns a verdict dict with a ``status`` of:
    - ``"granted"`` (+ ``run_id``, ``row``): claim taken, caller may spawn.
    - ``"not_granted"``: no active grant (no row / paused) — classic flow.
    - ``"duplicate"``: this attention was already handled or is in flight.
    - ``"busy"``: a live (non-expired) round holds the claim.
    - ``"escalate"`` (+ ``reason``): five-round limit reached — caller escalates.

  A crashed round (expired lease) is reclaimed here: its stale round is logged
  and the failure counter advanced before the fresh claim is taken.
  """
  claimed_event = canonical_event_at(event_at)
  for _attempt in range(4):
    db.expire_all()
    row = get_row(db, app_id, record_id)
    if row is None or not row.enabled:
      return {"status": "not_granted"}
    if row.last_handled_attention_key == attention_key:
      return {"status": "duplicate"}
    if (
      claimed_event
      and row.last_handled_event_at
      and claimed_event <= row.last_handled_event_at
    ):
      return {"status": "duplicate"}
    if row.state == "responding":
      if not _lease_expired(row):
        return {
          "status": "duplicate"
          if row.attention_key == attention_key else "busy"
        }
      reclaimed, failures = _reclaim_expired(db, row)
      if not reclaimed:
        continue
      if failures >= FAILURE_ESCALATION_THRESHOLD:
        return {"status": "escalate", "reason": "stale_rounds"}
      continue
    if row.rounds_used >= row.max_rounds:
      return {"status": "escalate", "reason": "round_limit"}

    now = now_naive_utc()
    run_id = uuid.uuid4().hex
    cursor_match = (
      models.ContributionAutopilot.last_handled_event_at.is_(None)
      if row.last_handled_event_at is None
      else models.ContributionAutopilot.last_handled_event_at
      == row.last_handled_event_at
    )
    key_match = (
      models.ContributionAutopilot.last_handled_attention_key.is_(None)
      if row.last_handled_attention_key is None
      else models.ContributionAutopilot.last_handled_attention_key
      == row.last_handled_attention_key
    )
    result = db.execute(
      update(models.ContributionAutopilot)
      .where(
        models.ContributionAutopilot.app_id == app_id,
        models.ContributionAutopilot.record_id == record_id,
        models.ContributionAutopilot.enabled.is_(True),
        models.ContributionAutopilot.state == "idle",
        cursor_match,
        key_match,
        models.ContributionAutopilot.rounds_used
        < models.ContributionAutopilot.max_rounds,
      )
      .values(
        state="responding",
        run_id=run_id,
        attention_key=attention_key,
        claimed_event_at=claimed_event,
        round_action=None,
        round_head_sha=None,
        claimed_at=now,
        lease_expires_at=now + timedelta(seconds=LEASE_SECONDS),
        updated_at=now,
      )
    )
    db.commit()
    if result.rowcount == 1:
      db.expire_all()
      return {
        "status": "granted", "run_id": run_id,
        "row": get_row(db, app_id, record_id),
      }
  return {"status": "busy"}


def verify_claim(
  row: models.ContributionAutopilot | None, run_id: str | None
) -> bool:
  """True iff a live round with this exact run_id holds the claim."""
  return bool(
    row is not None
    and row.enabled
    and row.state == "responding"
    and run_id
    and row.run_id == run_id
    and not _lease_expired(row)
  )


def record_action(
  db: Session,
  app_id: int,
  record_id: str,
  *,
  run_id: str,
  action: str,
  head_sha: str | None = None,
  public_event_url: str | None = None,
) -> bool:
  """Bind a successful server-mediated public action to the live claim."""
  if action not in PRODUCTIVE_OUTCOMES:
    return False
  # The agent normally calls action endpoints serially, but the public boundary
  # must not depend on that courtesy. Compare the fields we read so concurrent
  # replies append both exact event URLs and a reply can never overwrite the
  # stronger ``pushed`` outcome. The loser refreshes and retries.
  for _attempt in range(4):
    db.expire_all()
    row = get_row(db, app_id, record_id)
    if not verify_claim(row, run_id):
      return False
    old_action = row.round_action
    old_ignored = row.ignored_event_urls_json
    recorded = (
      "pushed" if action == "pushed" or old_action == "pushed"
      else "replied"
    )
    values = {
      "round_action": recorded,
      "updated_at": now_naive_utc(),
    }
    if head_sha and action == "pushed":
      values["round_head_sha"] = head_sha
    if public_event_url and action == "replied":
      ignored = list(old_ignored or [])
      if public_event_url not in ignored:
        ignored.append(public_event_url)
      values["ignored_event_urls_json"] = ignored[-MAX_IGNORED_EVENT_URLS:]
    action_match = (
      models.ContributionAutopilot.round_action.is_(None)
      if old_action is None
      else models.ContributionAutopilot.round_action == old_action
    )
    ignored_match = (
      models.ContributionAutopilot.ignored_event_urls_json.is_(None)
      if old_ignored is None
      else models.ContributionAutopilot.ignored_event_urls_json == old_ignored
    )
    result = db.execute(
      update(models.ContributionAutopilot)
      .where(
        _claim_match(app_id, record_id, run_id),
        models.ContributionAutopilot.lease_expires_at == row.lease_expires_at,
        action_match,
        ignored_match,
      )
      .values(**values)
    )
    db.commit()
    if result.rowcount == 1:
      return True
  return False


def complete_round(
  db: Session,
  app_id: int,
  record_id: str,
  *,
  run_id: str,
  outcome: str,
  summary: str,
  head_sha: str | None = None,
  event_at: str | None = None,
) -> dict:
  """Finalize a round the agent claims to have completed.

  ``event_at`` is accepted for compatibility but deliberately ignored: the
  cursor advances only to the canonical timestamp stored by the claim.
  Requires the live claim's run_id. Productive outcomes reset the failure
  counter, bump ``rounds_used``, and advance the handled-event cursor; a
  non-productive ``failed`` counts toward escalation. Returns
  ``{"status": "ok"|"stale", "escalate": bool}``.
  """
  row = get_row(db, app_id, record_id)
  if not verify_claim(row, run_id):
    return {"status": "stale", "escalate": False, "productive": False}

  # Public action endpoints, not the caller, decide whether the round did work.
  recorded_outcome = (
    row.round_action if row.round_action in PRODUCTIVE_OUTCOMES else "failed"
  )
  productive = recorded_outcome in PRODUCTIVE_OUTCOMES
  entry = {
    "attention_key": row.attention_key,
    "run_id": run_id,
    "started_at": row.claimed_at.isoformat() if row.claimed_at else None,
    "finished_at": now_naive_utc().isoformat(),
    "outcome": recorded_outcome,
    "summary": str(summary or "")[:2000],
    "head_sha": row.round_head_sha or row.granted_head_sha,
  }
  escalate = False
  values = {
    **_release_values(),
    "rounds_json": _append_round_value(row, entry),
    "updated_at": now_naive_utc(),
  }
  if productive:
    values["consecutive_failures"] = 0
    values["rounds_used"] = int(row.rounds_used or 0) + 1
    if row.round_head_sha:
      values["granted_head_sha"] = row.round_head_sha
    if row.claimed_event_at:
      values["last_handled_event_at"] = row.claimed_event_at
    values["last_handled_attention_key"] = row.attention_key
  else:
    failures = int(row.consecutive_failures or 0) + 1
    values["consecutive_failures"] = failures
    escalate = failures >= FAILURE_ESCALATION_THRESHOLD
  result = db.execute(
    update(models.ContributionAutopilot)
    .where(
      _claim_match(app_id, record_id, run_id),
      models.ContributionAutopilot.lease_expires_at
      == row.lease_expires_at,
    )
    .values(**values)
  )
  db.commit()
  if result.rowcount != 1:
    return {"status": "stale", "escalate": False, "productive": False}
  return {
    "status": "ok", "escalate": escalate, "productive": productive,
  }


def record_spawn_failure(
  db: Session, app_id: int, record_id: str, *, run_id: str, summary: str
) -> bool:
  """A round that could not start (chat busy, provider down). Returns escalate?"""
  row = get_row(db, app_id, record_id)
  if not verify_claim(row, run_id):
    return False
  failures = int(row.consecutive_failures or 0) + 1
  result = db.execute(
    update(models.ContributionAutopilot)
    .where(
      _claim_match(app_id, record_id, run_id),
      models.ContributionAutopilot.lease_expires_at == row.lease_expires_at,
    )
    .values(
      **_release_values(),
      rounds_json=_append_round_value(
        row, _nonproductive_entry(row, outcome="failed", summary=summary),
      ),
      consecutive_failures=failures,
      updated_at=now_naive_utc(),
    )
  )
  db.commit()
  return result.rowcount == 1 and failures >= FAILURE_ESCALATION_THRESHOLD


def release_for_retry(
  db: Session, app_id: int, record_id: str, *, run_id: str,
) -> bool:
  """Drop a just-taken claim without logging a round (spawn declined cleanly)."""
  row = get_row(db, app_id, record_id)
  if not verify_claim(row, run_id):
    return False
  result = db.execute(
    update(models.ContributionAutopilot)
    .where(
      _claim_match(app_id, record_id, run_id),
      models.ContributionAutopilot.lease_expires_at == row.lease_expires_at,
    )
    .values(**_release_values(), updated_at=now_naive_utc())
  )
  db.commit()
  return result.rowcount == 1


def set_enabled(
  db: Session, app_id: int, record_id: str, enabled: bool
) -> models.ContributionAutopilot | None:
  """Owner Pause/Resume. Resume clears escalation state and the round count."""
  row = get_row(db, app_id, record_id)
  if row is None:
    return None
  row.enabled = bool(enabled)
  if enabled:
    row.rounds_used = 0
    row.consecutive_failures = 0
  else:
    # Pause revokes future calls under the live run_id. A public request already
    # executing may still finish, but resume always starts with fresh authority
    # and a zombie round can never become valid again.
    _release_claim(row)
  row.updated_at = now_naive_utc()
  db.commit()
  return row


def close_out(db: Session, app_id: int, record_id: str) -> None:
  """Terminal cleanup once the PR merges/closes: end autopilot, drop the claim."""
  row = get_row(db, app_id, record_id)
  if row is None:
    return
  _release_claim(row)
  row.enabled = False
  row.updated_at = now_naive_utc()
  db.commit()


def escalate(db: Session, app_id: int, record_id: str) -> bool:
  """Atomically pause on escalation; True only for the winning notifier."""
  result = db.execute(
    update(models.ContributionAutopilot)
    .where(
      models.ContributionAutopilot.app_id == app_id,
      models.ContributionAutopilot.record_id == record_id,
      models.ContributionAutopilot.enabled.is_(True),
    )
    .values(
      **_release_values(),
      enabled=False,
      consecutive_failures=0,
      updated_at=now_naive_utc(),
    )
  )
  db.commit()
  return result.rowcount == 1


# ─────────────────────────── ledger mirror ──────────────────────────


def mirror_block(row: models.ContributionAutopilot) -> dict:
  """The display-only ``autopilot`` block the ledger record mirrors.

  Plain data for the app UI + cron. Round summaries stay plain text; the app
  renders them without markdown (they may quote untrusted reviewer text).
  """
  rounds = list(row.rounds_json or [])
  last = rounds[-1] if rounds else None
  return {
    "enabled": bool(row.enabled),
    "granted_at": row.granted_at.isoformat() if row.granted_at else None,
    "state": row.state,
    "rounds_used": int(row.rounds_used or 0),
    "max_rounds": int(row.max_rounds or DEFAULT_MAX_ROUNDS),
    "last_round": last,
    "rounds": rounds,
    "ignored_event_urls": list(row.ignored_event_urls_json or []),
  }


async def mirror_to_ledger(app_id: int, record_id: str) -> None:
  """Write the current DB row's mirror block into the ledger record.

  Best-effort and self-locking: acquires the app storage lock, reads the record,
  overlays only the ``autopilot`` block, writes it back. A missing record (agent
  dropped it) or any IO error is swallowed — the mirror is a cache the next
  transition/cron pass re-writes. Callers MUST NOT already hold the app storage
  lock. Reads the row on its own short-lived session so it never touches the
  caller's transaction.
  """
  from app.database import SessionLocal
  from app.routes import github as gh

  def _write() -> None:
    db = SessionLocal()
    try:
      row = get_row(db, app_id, record_id)
      if row is None:
        return
      block = mirror_block(row)
    finally:
      db.close()
    record_path, _ = gh._record_paths(app_id, record_id)
    try:
      record = gh._read_record(record_path)
    except Exception:
      return
    record["autopilot"] = block
    try:
      gh._write_record(record_path, record)
    except Exception:
      log.debug("mirror write failed app=%s rec=%s", app_id, record_id,
                exc_info=True)

  try:
    async with fs_locks.app_storage_lock(app_id):
      await asyncio.to_thread(_write)
  except Exception:
    log.debug("mirror_to_ledger failed app=%s rec=%s", app_id, record_id,
              exc_info=True)


async def set_ledger_attention(
  app_id: int, record_id: str, attention: dict | None, *, needs_attention: bool,
) -> dict | None:
  """Atomically overlay ``human_required`` attention onto the ledger."""
  from app.routes import github as gh

  def _write() -> dict | None:
    record_path, _ = gh._record_paths(app_id, record_id)
    try:
      record = gh._read_record(record_path)
    except Exception:
      return None
    record["needs_attention"] = bool(needs_attention)
    record["attention"] = attention
    record["updated_at"] = gh._now_iso()
    try:
      gh._write_record(record_path, record)
    except Exception:
      return None
    return record

  async with fs_locks.app_storage_lock(app_id):
    return await asyncio.to_thread(_write)


# ─────────────────────────── chat spawn ─────────────────────────────


def ensure_followup_chat(
  db: Session, app_id: int, record_id: str, *, title: str, provider: str,
) -> str | None:
  """Return the record's dedicated autopilot chat id, creating it once.

  Reuses the stored chat when it still exists and is owner-visible; otherwise
  creates a fresh owner-visible chat and persists its id on the DB row. Returns
  None if there is no autopilot row (should not happen on the claimed path).
  """
  row = get_row(db, app_id, record_id)
  if row is None:
    return None
  if row.followup_chat_id:
    existing = (
      db.query(models.Chat)
      .filter(
        models.Chat.id == row.followup_chat_id,
        models.Chat.deleted_at.is_(None),
        models.Chat.created_by_app_id.is_(None),
      )
      .first()
    )
    if existing is not None:
      if existing.provider != provider:
        existing.provider = provider
        db.commit()
      return existing.id
  chat = models.Chat(
    id=str(uuid.uuid4()),
    title=title,
    messages=[],
    pending_messages=[],
    provider=provider,
    created_by_app_id=None,
  )
  db.add(chat)
  db.commit()
  db.refresh(chat)
  row.followup_chat_id = chat.id
  row.updated_at = now_naive_utc()
  db.commit()
  return chat.id


def resolve_round_provider(db: Session) -> str:
  """Which provider runs follow-up rounds — the owner's background choice."""
  from app import providers
  from app.background_agents import resolve_background_agents

  data_dir = get_settings().data_dir
  choices = resolve_background_agents(data_dir, {})
  primary = choices.get("primary") if isinstance(choices, dict) else None
  if isinstance(primary, dict) and primary.get("provider"):
    return str(primary["provider"])
  owner = db.query(models.Owner).first()
  return providers.resolve_default_provider(
    data_dir, owner.provider if owner else None,
  )


async def spawn_round_turn(
  db: Session, chat_id: str, *, title: str, content: str, provider: str,
) -> bool:
  """Start a follow-up round turn in the dedicated chat.

  Mirrors ``apps._start_conflict_resolver_turn`` but allows a non-empty IDLE
  chat (rounds accumulate in one chat). Refuses only when the chat is missing or
  a turn is already running — the caller then releases the claim and retries next
  cron pass, so rounds never stack into the pending queue.
  """
  from app.broadcast import create_broadcast, get_system_broadcast
  from app.chat import (
    current_run_generation, discard_starting, is_chat_running, mark_starting,
    run_chat,
  )
  from app.chat_writer import StartTurn, alloc_run_token, await_ack, get_writer

  chat = (
    db.query(models.Chat)
    .filter(models.Chat.id == chat_id, models.Chat.deleted_at.is_(None))
    .first()
  )
  if chat is None or chat.run_status == "running" or is_chat_running(chat_id):
    return False
  if not mark_starting(chat_id):
    return False

  try:
    start_gen = current_run_generation(chat_id)
    run_token = alloc_run_token()
    user_msg = {
      "role": "user", "content": content, "ts": int(time.time() * 1000),
    }
    ack = get_writer().submit(StartTurn(
      chat_id=chat_id,
      run_token=run_token,
      user_msg=user_msg,
      title_source=title,
      default_provider=provider,
    ))
    result = await await_ack(ack)
    if current_run_generation(chat_id) != start_gen:
      discard_starting(chat_id)
      return False
    create_broadcast(chat_id)
    get_system_broadcast().publish(
      {"type": "chat_run_started", "chatId": chat_id}
    )
    asyncio.create_task(run_chat(
      result["history"], chat_id=chat_id, session_id=result["session_id"],
      provider_id=result["provider"], run_gen=start_gen, run_token=run_token,
    ))
    return True
  except Exception:
    discard_starting(chat_id)
    raise
