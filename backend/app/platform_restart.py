"""Typed, platform-owned Restart cards and loaded-source activation proof.

The action is deliberately narrower than a generic approval executor.  A card
binds one source boot to the exact committed bytes that a server restart can
load.  Its activation wait is satisfied only by a later, ready boot whose
captured platform tree has those bytes (including explicit absence for deleted
paths).  A durable execution claim precedes the restart side effect and is
never replayed after an ambiguous process death.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import models
from app.boot_source import supported_restart_path as _supported_restart_path
from app.timeutil import now_naive_utc


ACTIVATION_WAIT_KIND = "platform_activation"
REQUIREMENT_VERSION = 1
# A response normally hands its committed claim to restart_util immediately.
# If even failure-settlement persistence fails, the existing wait supervisor
# closes that handoff after two minutes; it never retries the restart itself.
RESTART_HANDOFF_DEADLINE_SECONDS = 120


class RestartRequirementError(RuntimeError):
  """The current source cannot be represented by one safe restart card."""


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    capture_output=True,
    text=True,
    timeout=30,
    check=False,
  )


def _path_manifest(repo: Path, revision: str, path: str) -> dict[str, str]:
  """Return committed blob identity, or an explicit absence proof."""
  entry = _git(repo, "ls-tree", revision, "--", path)
  if entry.returncode != 0:
    raise RestartRequirementError("could_not_read_committed_source")
  raw = entry.stdout.strip()
  if not raw:
    return {"state": "absent"}
  try:
    left, listed_path = raw.split("\t", 1)
    mode, kind, object_id = left.split(" ", 2)
  except ValueError as exc:
    raise RestartRequirementError("invalid_committed_source_entry") from exc
  if listed_path != path or kind != "blob" or mode not in ("100644", "100755"):
    raise RestartRequirementError("restart_requirement_is_not_a_file")
  blob = subprocess.run(
    ["git", "-C", str(repo), "cat-file", "blob", object_id],
    capture_output=True,
    timeout=30,
    check=False,
  )
  if blob.returncode != 0:
    raise RestartRequirementError("could_not_read_committed_source_blob")
  return {
    "state": "file",
    "mode": mode,
    "sha256": hashlib.sha256(blob.stdout).hexdigest(),
  }


def committed_manifest(
  repo: Path, revision: str, paths: list[str],
) -> dict[str, dict[str, str]]:
  if not revision or not paths:
    raise RestartRequirementError("restart_requirement_has_no_source")
  return {
    path: _path_manifest(repo, revision, path)
    for path in sorted(set(paths))
  }


def _canonical_hash(value: object) -> str:
  payload = json.dumps(
    value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
  ).encode("utf-8")
  return hashlib.sha256(payload).hexdigest()


def build_restart_requirement(repo: Path | None = None) -> dict:
  """Derive the exact current restart-loadable committed source requirement.

  This rejects a baked process, uncommitted runtime source, broad/unknown
  backend sentinels. Other activation planes (frontend builds, dependencies,
  and image changes) remain outside this proof. The result contains no
  caller-authored path or command.
  """
  from app import platform_activation, platform_update, restart_ledger

  repo = repo or platform_update.PLATFORM_REPO
  source_boot_id = restart_ledger.current_boot_id()
  served = platform_update._served_platform_sha()
  if not source_boot_id or not served:
    raise RestartRequirementError("platform_source_is_not_loaded")
  head_proc = _git(repo, "rev-parse", "HEAD")
  head = head_proc.stdout.strip() if head_proc.returncode == 0 else ""
  if not head:
    raise RestartRequirementError("platform_head_is_unavailable")

  dirty = _git(
    repo, "status", "--porcelain", "--untracked-files=all", "--",
    "backend/app", "backend/scripts/pm-commit", "skill/core.md",
  )
  if dirty.returncode != 0 or dirty.stdout.strip():
    raise RestartRequirementError("restart_source_must_be_committed")

  changed = platform_update._activation_paths_between(repo, served, head)
  pending = platform_update._pending_activation_paths(
    repo, served_to_head=changed,
  )
  restart_paths: set[str] = set()
  unsupported: list[str] = []
  for path in pending:
    impact = platform_activation.classify_activation([path])
    if impact["level"] != platform_activation.ActivationLevel.SERVER_RESTART.value:
      continue
    if _supported_restart_path(path):
      restart_paths.add(path)
      continue
    # A directory sentinel can be expanded only from the exact served→HEAD
    # diff.  Guessing a whole directory manifest would turn an old legacy
    # marker into authority for unrelated future files.
    expanded = [
      candidate for candidate in changed
      if candidate == path or candidate.startswith(path.rstrip("/") + "/")
    ]
    if expanded and all(_supported_restart_path(candidate) for candidate in expanded):
      restart_paths.update(expanded)
    else:
      unsupported.append(path)
  if unsupported:
    raise RestartRequirementError("restart_requirement_is_not_exact")
  if not restart_paths:
    raise RestartRequirementError("no_restart_loadable_changes")

  files = committed_manifest(repo, head, sorted(restart_paths))
  identity = {
    "version": REQUIREMENT_VERSION,
    "source_boot_id": source_boot_id,
    "target_sha": head,
    "files": files,
  }
  return {
    **identity,
    "action_id": f"platform-restart:{_canonical_hash(identity)}",
    "paths": sorted(restart_paths),
  }


def requirement_matches_current_source(requirement: object) -> bool:
  """Re-derive the action immediately before claim; never trust card JSON."""
  if not isinstance(requirement, dict):
    return False
  try:
    current = build_restart_requirement()
  except RestartRequirementError:
    return False
  return current == requirement


def requirement_matches_snapshot(requirement: object, snapshot) -> bool:
  if not isinstance(requirement, dict) or snapshot is None:
    return False
  expected = requirement.get("files")
  loaded = snapshot.loaded_files_json
  return bool(
    snapshot.service_ready
    and snapshot.source_kind == "platform"
    and snapshot.boot_id != requirement.get("source_boot_id")
    and isinstance(expected, dict)
    and isinstance(loaded, dict)
    and all(loaded.get(path) == value for path, value in expected.items())
  )


def activation_wait_verdict(
  db: Session, row: models.ChatWait,
) -> tuple[Literal["met", "pending", "failed"], str]:
  """Evaluate one typed wait only from a later immutable boot snapshot."""
  requirement = row.condition_json
  if (
    not isinstance(requirement, dict)
    or requirement.get("version") != REQUIREMENT_VERSION
    or not isinstance(requirement.get("files"), dict)
    or not requirement.get("source_boot_id")
    or not isinstance(requirement.get("action_id"), str)
    or not requirement["action_id"].startswith("platform-restart:")
    or any(
      not isinstance(path, str) or not _supported_restart_path(path)
      for path in requirement["files"]
    )
  ):
    return "failed", "The saved activation requirement is invalid."
  execution = db.get(
    models.PlatformRestartExecution, requirement.get("action_id") or "",
  )
  if execution is not None and execution.status == "source_changed":
    return "failed", "The committed source changed before restart admission."
  if execution is not None and execution.status == "claimed":
    from app import restart_ledger, restart_util
    if (
      execution.source_boot_id == restart_ledger.current_boot_id()
      and execution.claimed_at is not None
      and (now_naive_utc() - execution.claimed_at).total_seconds()
        >= RESTART_HANDOFF_DEADLINE_SECONDS
      and not restart_util.restart_admission_in_progress()
    ):
      # A conditional transition races admission safely: either admission wins
      # and we do nothing, or it sees a settled claim and cannot dispatch.
      settle_undispatched_execution(execution.action_id)
      db.refresh(execution)
  if execution is not None and execution.status == "uncertain":
    from app import restart_ledger
    if execution.source_boot_id == restart_ledger.current_boot_id():
      return "failed", "The Restart response ended before dispatch could be confirmed. No action was replayed."
  snapshots = (
    db.query(models.PlatformBootSnapshot)
    .filter(models.PlatformBootSnapshot.captured_at >= row.created_at)
    .filter(
      models.PlatformBootSnapshot.boot_id != requirement["source_boot_id"],
    )
    .order_by(models.PlatformBootSnapshot.captured_at.desc())
    .all()
  )
  if not snapshots:
    return "pending", ""
  latest = snapshots[0]
  if requirement_matches_snapshot(requirement, latest):
    return "met", "Required committed source bytes are loaded and ready."
  if latest.service_ready:
    if execution is not None and execution.status == "uncertain":
      if latest.source_kind != "platform":
        return "failed", "The dispatched restart reached a baked fallback boot."
      return "failed", "The dispatched restart did not load the approved source bytes."
    # A Settings/other-card restart for different changes is not evidence that
    # this independent wait failed. Keep it armed for a later matching boot.
    return "pending", ""
  return "pending", ""


def capture_ready_boot_snapshot(
  db: Session, *, boot_id: str, repo: Path | None = None,
) -> models.PlatformBootSnapshot:
  """Capture one post-migration, post-writer source proof for all waiters."""
  from app import platform_update
  from app.main import service_readiness
  from app.boot_source import BOOT_SOURCE_INPUTS

  repo = repo or platform_update.PLATFORM_REPO
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

  waits = (
    db.query(models.ChatWait)
    .filter(models.ChatWait.kind == ACTIVATION_WAIT_KIND)
    .filter(models.ChatWait.status.in_(("armed", "met", "expired", "failed")))
    .filter(models.ChatWait.resume_delivered_at.is_(None))
    .all()
  )
  unsettled_executions = db.query(models.PlatformRestartExecution).filter(
    models.PlatformRestartExecution.status.in_(("claimed", "admitted")),
    models.PlatformRestartExecution.source_boot_id != boot_id,
  ).all()
  paths = sorted({
    path
    for wait in waits
    if isinstance(wait.condition_json, dict)
    for path in (wait.condition_json.get("files") or {})
    if isinstance(path, str) and _supported_restart_path(path)
  } | {
    path
    for execution in unsettled_executions
    if isinstance(execution.requirement_json, dict)
    for path in (execution.requirement_json.get("files") or {})
    if isinstance(path, str) and _supported_restart_path(path)
  })
  loaded = {}
  if source_kind == "platform" and source_sha and paths:
    try:
      if (
        BOOT_SOURCE_INPUTS.source_kind != source_kind
        or BOOT_SOURCE_INPUTS.source_sha != source_sha
      ):
        raise ValueError("boot_source_identity_changed")
      loaded = BOOT_SOURCE_INPUTS.unchanged_manifest(repo, paths)
    except (ValueError, OSError):
      service_ready = False

  snapshot = db.get(models.PlatformBootSnapshot, boot_id)
  if snapshot is not None:
    return snapshot
  snapshot = models.PlatformBootSnapshot(boot_id=boot_id)
  db.add(snapshot)
  snapshot.source_kind = source_kind
  snapshot.source_sha = source_sha
  snapshot.loaded_files_json = loaded
  snapshot.service_ready = service_ready
  snapshot.captured_at = now_naive_utc()

  # A prior claim is evidence that one dispatch may already have happened. A
  # later boot settles it from source proof or labels it uncertain; neither
  # branch replays the side effect.
  for execution in unsettled_executions:
    if requirement_matches_snapshot(execution.requirement_json, snapshot):
      execution.status = "activated"
      execution.activated_boot_id = boot_id
    else:
      execution.status = "uncertain"
    execution.settled_at = now_naive_utc()
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


def settle_undispatched_execution(action_id: str) -> bool:
  """Close a response handoff that ended without side-effect admission.

  Called in the owning response's finally block and on construction failure.
  Only a claimed action is known not to have entered restart_util; admitted
  actions belong to boot reconciliation and must never be reclassified here.
  Existing activation wait checks expose uncertainty without another scheduler.
  """
  from app.database import SessionLocal

  with SessionLocal() as db:
    count = db.query(models.PlatformRestartExecution).filter(
      models.PlatformRestartExecution.action_id == action_id,
      models.PlatformRestartExecution.status == "claimed",
    ).update({
      models.PlatformRestartExecution.status: "uncertain",
      models.PlatformRestartExecution.settled_at: now_naive_utc(),
    }, synchronize_session=False)
    db.commit()
    return bool(count)


def admit_execution_if_current(action_id: str) -> bool:
  """Admit a claimed card only while its exact source still matches.

  This check runs synchronously inside restart_util after in-process
  singleflight and immediately before drain. A changed checkout settles the
  wait visibly and performs no restart; an admitted/ambiguous row is never
  replayed.
  """
  from app.database import SessionLocal

  with SessionLocal() as db:
    row = db.get(models.PlatformRestartExecution, action_id)
    if row is None or row.status != "claimed":
      return False
    current = requirement_matches_current_source(row.requirement_json)
    now = now_naive_utc()
    # Source derivation does I/O. Re-check claimed in the UPDATE so an expired
    # response handoff cannot be resurrected after that I/O yields the DB.
    values = ({
      models.PlatformRestartExecution.status: "admitted",
      models.PlatformRestartExecution.admitted_at: now,
    } if current else {
      models.PlatformRestartExecution.status: "source_changed",
      models.PlatformRestartExecution.settled_at: now,
    })
    changed = db.query(models.PlatformRestartExecution).filter(
      models.PlatformRestartExecution.action_id == action_id,
      models.PlatformRestartExecution.status == "claimed",
    ).update(values, synchronize_session=False)
    if not changed:
      db.rollback()
      return False
    if not current:
      wait = db.get(models.ChatWait, row.wait_id)
      if (
        wait is not None
        and wait.kind == ACTIVATION_WAIT_KIND
        and wait.status == "armed"
        and wait.resume_delivered_at is None
      ):
        wait.status = "failed"
        wait.last_checked_at = now
        wait.last_output = (
          "Committed restart source changed before side-effect admission."
        )
    db.commit()
    return current


def activation_barrier_for_chat(db: Session, chat_id: str) -> bool:
  """Whether A still owns the chat ahead of every queued B."""
  return db.query(models.ChatWait.id).filter(
    models.ChatWait.chat_id == chat_id,
    models.ChatWait.kind == ACTIVATION_WAIT_KIND,
    models.ChatWait.status.in_(("armed", "met", "expired", "failed")),
    models.ChatWait.resume_delivered_at.is_(None),
  ).first() is not None


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
    "required_paths": sorted((requirement.get("files") or {}).keys()),
    "declaring_run_id": row.created_by_run_id,
    "root_run_id": row.root_run_id,
    "goal_id": row.goal_id,
  }, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
  if outcome == "met":
    lead = (
      "Möbius verified that the exact committed platform source required by "
      "this work is loaded in a ready server. Continue the interrupted work "
      "before handling later queued owner messages."
    )
  else:
    lead = (
      "Möbius could not prove that the exact approved platform source loaded. "
      "No Restart action was replayed. Inspect current source and request a "
      "fresh specific approval only if a restart is still required, then "
      "report the concrete uncertainty before later queued work."
    )
  return f"{lead}\n<platform_activation>{body}</platform_activation>"
