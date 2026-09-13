"""Release reviewed Autopilot blockers under an unchanged owner grant.

The private review record supplies a repair signal, never consent: only an
enabled, platform-blocked grant can recover. Explicit and legacy disabled
grants remain disabled. Recovery does not push a private update, retarget a
grant, or bypass the live PR checks on subsequent public actions.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import exists, update

from app import app_services, contribution_autopilot as autopilot, fs_locks, models
from app.contribution_records import read_record, record_paths, write_record
from app.database import SessionLocal
from app.timeutil import now_naive_utc

log = logging.getLogger(__name__)


async def _recover_one(app_id: int, record_id: str) -> bool:
  """Ask Contribute to validate its record, then CAS-release core authority."""
  with SessionLocal() as db:
    row = autopilot.get_row(db, app_id, record_id)
    app = db.get(models.App, app_id)
    owner = db.query(models.Owner).first()
    if (
      row is None or not row.enabled or row.state != "blocked"
      or row.blocked_at is None or app is None or owner is None
    ):
      return False
    path, _ = record_paths(app_id, record_id)
    record = read_record(path)
    grant = {
      "record_id": row.record_id,
      "target_repo": row.target_repo,
      "target_pr_number": row.target_pr_number,
      "target_head_repository": row.target_head_repository,
      "target_branch": row.target_branch,
      "target_repo_path": row.target_repo_path,
      "granted_head_sha": row.granted_head_sha,
      "blocked_at": row.blocked_at.replace(tzinfo=UTC).isoformat(),
    }
    db.expunge(app)
    db.expunge(owner)
  verdict = await app_services.invoke_policy(
    app, owner, "autopilot/reviewed-resolution",
    {
      "grant": grant,
      "record": record,
      "now": datetime.now(UTC).isoformat(),
    },
  )
  if verdict.get("eligible") is not True:
    return False

  with SessionLocal() as db:
    row = autopilot.get_row(db, app_id, record_id)
    if row is None or not row.enabled or row.state != "blocked":
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
        models.ContributionAutopilot.target_repo == grant["target_repo"],
        models.ContributionAutopilot.target_pr_number == grant["target_pr_number"],
        models.ContributionAutopilot.target_head_repository
          == grant["target_head_repository"],
        models.ContributionAutopilot.target_branch == grant["target_branch"],
        models.ContributionAutopilot.target_repo_path == grant["target_repo_path"],
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
        recovered += await _recover_one(app_id, record_id)
    except Exception:
      # One missing/deleted/malformed review must not strand healthy siblings.
      log.warning("Autopilot recovery failed for %s/%s", app_id, record_id, exc_info=True)
  return recovered
