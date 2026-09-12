"""Release reviewed Autopilot blockers under an unchanged owner grant.

The private review record supplies a repair signal, never consent: only an
enabled, platform-blocked grant can recover. Explicit and legacy disabled
grants remain disabled. Recovery does not push a private update, retarget a
grant, or bypass the live PR checks on subsequent public actions.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, update

from app import contribution_autopilot as autopilot, fs_locks, models
from app.contribution_records import read_record, record_paths, write_record
from app.database import SessionLocal
from app.timeutil import now_naive_utc

log = logging.getLogger(__name__)


def _has_reviewed_resolution(row: models.ContributionAutopilot, record: dict) -> bool:
  """Require a fresh, exact published-head review, not just a dismissed alert."""
  if (
    record.get("id") != row.record_id
    or record.get("repo") != row.target_repo
    or record.get("type") != "pr"
    or record.get("status") not in {"open", "draft"}
    or record.get("needs_attention")
    or record.get("attention")
    or row.blocked_at is None
  ):
    return False
  plan = record.get("plan")
  review = record.get("quality_review")
  if not isinstance(plan, dict) or not isinstance(review, dict):
    return False
  expected = (
    row.target_repo, row.target_pr_number, row.target_head_repository,
    row.target_branch, row.target_repo_path,
  )
  actual = (
    plan.get("repo") or record.get("repo"), record.get("number"),
    record.get("head_repository") or plan.get("head_repository"),
    plan.get("branch") or record.get("branch"), plan.get("repo_path"),
  )
  if any(value in (None, "") for value in expected) or actual != expected:
    return False
  reviewed_head = review.get("reviewed_head_sha")
  if (
    review.get("state") != "all_clear"
    or not row.granted_head_sha
    or not reviewed_head
    # The publisher may normalize attribution without changing the reviewed
    # diff. Keep the same public-head equivalence as _personal_ready_target;
    # the DB grant, not this marker, still pins the actual published head.
    or reviewed_head not in {
      row.granted_head_sha, plan.get("attribution_normalized_from"),
    }
    or plan.get("head_sha") != row.granted_head_sha
  ):
    return False
  try:
    reviewed_at = datetime.fromisoformat(
      str(review.get("reviewed_at") or "").replace("Z", "+00:00")
    )
  except ValueError:
    return False
  if reviewed_at.tzinfo is None:
    reviewed_at = reviewed_at.replace(tzinfo=UTC)
  reviewed_at = reviewed_at.astimezone(UTC).replace(tzinfo=None)
  return row.blocked_at <= reviewed_at <= now_naive_utc() + timedelta(minutes=5)


def _recover_one(app_id: int, record_id: str) -> bool:
  """Called in a worker while the app's record lock is held."""
  with SessionLocal() as db:
    row = autopilot.get_row(db, app_id, record_id)
    if row is None or not row.enabled or row.state != "blocked":
      return False
    path, _ = record_paths(app_id, record_id)
    record = read_record(path)
    if not _has_reviewed_resolution(row, record):
      return False
    chat = db.get(models.Chat, row.followup_chat_id) if row.followup_chat_id else None
    if chat is not None and chat.pending_question_id:
      return False
    values = {
      **autopilot._release_values(),
      "rounds_used": 0,
      "consecutive_failures": 0,
      "updated_at": now_naive_utc(),
    }
    if row.attention_key:
      values["last_handled_attention_key"] = row.attention_key
    if row.claimed_event_at:
      values["last_handled_event_at"] = max(
        row.claimed_event_at, row.last_handled_event_at or "",
      )
    changed = db.execute(
      update(models.ContributionAutopilot)
      .where(
        models.ContributionAutopilot.app_id == app_id,
        models.ContributionAutopilot.record_id == record_id,
        models.ContributionAutopilot.enabled.is_(True),
        models.ContributionAutopilot.state == "blocked",
        models.ContributionAutopilot.blocked_at == row.blocked_at,
        models.ContributionAutopilot.granted_head_sha == row.granted_head_sha,
        ~exists().where(
          models.Chat.id == models.ContributionAutopilot.followup_chat_id,
          models.Chat.pending_question_id.isnot(None),
        ),
      )
      .values(**values)
    )
    if changed.rowcount != 1:
      db.rollback()
      return False
    autopilot.stage_followup_drawer_hidden(db, row, True)
    db.commit()
    db.refresh(row)
    record["autopilot"] = autopilot.mirror_block(row)
    write_record(path, record)
    return True


async def recover_resolved_blocks() -> int:
  """Join the existing recovery pass; no new timer or background agent."""
  def blocked_records():
    with SessionLocal() as db:
      return db.query(
        models.ContributionAutopilot.app_id,
        models.ContributionAutopilot.record_id,
      ).filter(
        models.ContributionAutopilot.enabled.is_(True),
        models.ContributionAutopilot.state == "blocked",
      ).all()

  recovered = 0
  for app_id, record_id in await asyncio.to_thread(blocked_records):
    try:
      async with fs_locks.app_storage_lock(app_id):
        recovered += await asyncio.to_thread(_recover_one, app_id, record_id)
    except Exception:
      # One missing/deleted/malformed review must not strand healthy siblings.
      log.warning("Autopilot recovery failed for %s/%s", app_id, record_id, exc_info=True)
  return recovered
