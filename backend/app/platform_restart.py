"""Typed, platform-owned Restart cards and post-restart continuation.

Pressing **Restart now** always drains active work and restarts once; a restart
simply reloads whatever backend source is currently present. There is no
per-file source proof, no "is this restart still valid" gate, and no execution
state machine — the owner asked for a restart, so Möbius restarts.

What remains here is the *continuation*: after a restart, the interrupted chat
resumes once a later boot becomes ready, so the agent can verify whether its
change loaded. That resume is driven by an ``platform_activation`` ChatWait plus
immutable ready-boot receipts (``PlatformBootSnapshot``), with a bounded
self-heal so the agent is never pinned forever.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.timeutil import now_naive_utc


ACTIVATION_WAIT_KIND = "platform_activation"
CONDITION_VERSION = 1
# After the owner approves a restart, give a later boot this long to record a
# service-ready receipt. If none appears — the boot came up degraded, a failing
# update rolled the source back, or the restart never dispatched — resume the
# agent to verify manually instead of pinning it forever. Generous enough for a
# slow boot or container replacement, bounded enough to self-heal.
ACTIVATION_CONFIRM_DEADLINE_SECONDS = 900


def restart_condition_id(source_boot_id: str, run_id: str) -> str:
  """A stable card/wait identity for the restart offered by one run+boot."""
  digest = hashlib.sha256(
    json.dumps(
      [CONDITION_VERSION, source_boot_id, run_id],
      sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")
  ).hexdigest()
  return f"platform-restart:{digest}"


def pending_restart_paths() -> list[str]:
  """Best-effort list of committed paths a restart would load, for card text.

  This is display-only: it never blocks or refuses a restart. Any error (dirty
  tree, missing HEAD, baked process) yields an empty list and a generic card.
  """
  try:
    from app import platform_activation, platform_update

    repo = platform_update.PLATFORM_REPO
    served = platform_update._served_platform_sha()
    if not served:
      return []
    head = subprocess.run(
      ["git", "-C", str(repo), "rev-parse", "HEAD"],
      capture_output=True, text=True, timeout=30, check=False,
    )
    head_sha = head.stdout.strip() if head.returncode == 0 else ""
    if not head_sha:
      return []
    changed = platform_update._activation_paths_between(repo, served, head_sha)
    pending = platform_update._pending_activation_paths(
      repo, served_to_head=changed,
    )
    restart_level = platform_activation.ActivationLevel.SERVER_RESTART.value
    result = {
      path for path in pending
      if platform_activation.classify_activation([path])["level"]
      == restart_level
    }
    return sorted(result)
  except Exception:
    return []


def activation_wait_verdict(
  db: Session, row: models.ChatWait,
) -> tuple[Literal["met", "pending", "failed"], str]:
  """Wake one typed Restart wait after any later ready boot."""
  requirement = row.condition_json
  if (
    not isinstance(requirement, dict)
    or requirement.get("version") != CONDITION_VERSION
    or not requirement.get("source_boot_id")
    or not isinstance(requirement.get("action_id"), str)
    or not requirement["action_id"].startswith("platform-restart:")
  ):
    return "failed", "The saved activation requirement is invalid."

  ready_boot = (
    db.query(models.PlatformBootSnapshot)
    .filter(models.PlatformBootSnapshot.captured_at >= row.created_at)
    .filter(
      models.PlatformBootSnapshot.boot_id != requirement["source_boot_id"],
    )
    .filter(models.PlatformBootSnapshot.service_ready.is_(True))
    .order_by(models.PlatformBootSnapshot.captured_at.desc())
    .first()
  )
  if ready_boot is not None:
    return "met", "A later ready Möbius boot was observed."

  # Bounded self-heal. Once the owner approved a restart, if no ready boot has
  # confirmed within the window, resume the agent to verify manually — a
  # degraded boot, a rolled-back update, or a restart that never dispatched
  # must never leave the agent waiting on a receipt that will not arrive.
  approved_at = row.action_approved_at
  if (
    approved_at is not None
    and (now_naive_utc() - approved_at).total_seconds()
      >= ACTIVATION_CONFIRM_DEADLINE_SECONDS
  ):
    return "failed", (
      "Möbius did not confirm a ready boot after the restart. Resuming to "
      "verify the update state manually."
    )
  return "pending", ""


def capture_ready_boot_snapshot(
  db: Session, *, boot_id: str,
) -> models.PlatformBootSnapshot:
  """Capture one post-migration, post-writer restart receipt for all waiters."""
  from app import platform_update
  from app.main import service_readiness

  source_kind = "unknown"
  source_sha = None
  try:
    source_kind = platform_update.SERVING_SOURCE_FILE.read_text(
      encoding="utf-8",
    ).strip() or "unknown"
    source_sha = platform_update.SERVING_SHA_FILE.read_text(
      encoding="utf-8",
    ).strip() or None
  except OSError:
    pass
  ready = service_readiness()["ready"]
  # The caller already reached the database phase, but execute a real query in
  # this same transaction so the receipt never infers readiness from ordering.
  database_ready = db.execute(text("SELECT 1")).scalar() == 1
  service_ready = bool(database_ready and ready)

  snapshot = db.get(models.PlatformBootSnapshot, boot_id)
  if snapshot is not None:
    return snapshot
  snapshot = models.PlatformBootSnapshot(boot_id=boot_id)
  db.add(snapshot)
  snapshot.source_kind = source_kind
  snapshot.source_sha = source_sha
  # Kept for the existing durable schema and historical audit rows. Restart
  # continuation deliberately does not compare a per-wait file manifest.
  snapshot.loaded_files_json = {}
  snapshot.service_ready = service_ready
  snapshot.captured_at = now_naive_utc()
  db.commit()
  db.refresh(snapshot)

  # Boot evidence is tiny, but it is process-lifetime data. Keep a bounded
  # recent audit window; active waits only evaluate later snapshots and never
  # require replaying an old one.
  stale_ids = [item[0] for item in (
    db.query(models.PlatformBootSnapshot.boot_id)
    .order_by(
      models.PlatformBootSnapshot.captured_at.desc(),
      models.PlatformBootSnapshot.boot_id.desc(),
    )
    .offset(32)
    .all()
  )]
  if stale_ids:
    db.query(models.PlatformBootSnapshot).filter(
      models.PlatformBootSnapshot.boot_id.in_(stale_ids),
    ).delete(synchronize_session=False)
    db.commit()

  return snapshot


def activation_barrier_wait_id(db: Session, chat_id: str) -> str | None:
  """Return the approved restart wait that still owns chat admission.

  A deferred card keeps its activation monitor armed so a later ready boot
  can resume the interrupted Goal, but no restart is in flight and the owner
  must remain free to continue chatting.  ``action_approved_at`` is the durable
  distinction between that passive monitor and a restart the owner actually
  asked Möbius to execute.
  """
  row = db.query(models.ChatWait.id).filter(
    models.ChatWait.chat_id == chat_id,
    models.ChatWait.kind == ACTIVATION_WAIT_KIND,
    models.ChatWait.status.in_(("armed", "met", "expired", "failed")),
    models.ChatWait.action_approved_at.isnot(None),
    models.ChatWait.resume_delivered_at.is_(None),
  ).first()
  return row[0] if row is not None else None


def restart_action_block(chat, question_id: str | None) -> dict | None:
  """Return only an exact typed Restart card; never fall back to latest."""
  if not question_id:
    return None
  for message in reversed(list(chat.messages or [])):
    if not isinstance(message, dict) or message.get("role") != "assistant":
      continue
    for block in message.get("blocks") or []:
      action = block.get("platform_action") if isinstance(block, dict) else None
      if (
        block.get("type") == "question"
        and block.get("question_id") == question_id
        and isinstance(action, dict)
        and action.get("type") == "restart"
      ):
        return block
  return None


def activation_notice(row: models.ChatWait, outcome: str) -> str:
  requirement = row.condition_json if isinstance(row.condition_json, dict) else {}
  body = json.dumps({
    "wait_id": row.id,
    "outcome": outcome,
    "required_paths": sorted(requirement.get("paths") or []),
    "declaring_run_id": row.created_by_run_id,
    "root_run_id": row.root_run_id,
    "goal_id": row.goal_id,
  }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
  if outcome == "met":
    lead = (
      "Möbius restarted and reached a ready server. Continue the interrupted "
      "work now, verify whether its changes are active, then handle later "
      "queued owner messages."
    )
  else:
    lead = (
      "Möbius restarted but did not confirm a ready server. Inspect the "
      "current source and running state, report the concrete uncertainty, and "
      "request a fresh restart only if one is still required, before later "
      "queued work."
    )
  return f"{lead}\n<platform_activation>{body}</platform_activation>"
