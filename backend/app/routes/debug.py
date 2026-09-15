"""Debug and observability endpoints.

Provides structured access to chat logs, active SDK runtimes (Claude +
Codex), starting state, and broadcast state.  All endpoints require
authentication.  The agent uses these when debugging issues instead of
ad-hoc debug endpoints.
"""

import os
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from app import models
from app.allocator import allocator_status
from app.broadcast import (
  broadcast_memory_diagnostics,
  get_all_active_broadcasts,
)
from app.browser_profiles import browser_profile_status
from app.chat import active_sink_memory_diagnostics
from app.chat_writer import writer_memory_diagnostics
from app.config import get_settings
from app.database import database_pool_snapshot, get_db
from app.deps import get_current_owner
from app.text_util import elide
from app.memory_observability import (
  allocation_report,
  gc_diagnostics,
  memory_map_summary,
  memory_status,
  process_inventory,
)
from app.questions import question_memory_diagnostics
from app.resource_pressure import resource_status
from app.runner_registry import RunnerKind, registry
from app.secure_inputs import secure_input_memory_diagnostics

router = APIRouter(prefix="/api/debug", tags=["debug"])

# Path to the flag file written by entrypoint.sh when the SECRET_KEY changed
# between boots. Backend checks for this on startup and surfaces it in
# /api/debug/status so operators know all outstanding JWTs were invalidated.
# The file contains the ISO timestamp of the detection; it is cleared by
# entrypoint.sh on the next boot where the key is stable.
_SECRET_KEY_CHANGED_FLAG = Path(
  os.environ.get("DATA_DIR", "/data")
) / ".secret-key-changed"


def _runtime_memory_ownership(*, include_payloads: bool = True) -> dict:
  """Cheap cardinality/payload diagnostics from each long-lived owner."""
  report = {
    "runner_handles": {
      kind.value: len(registry.handles_by_kind(kind))
      for kind in RunnerKind
    },
    "starting_chats": len(registry.starting_chat_ids()),
    "broadcasts": broadcast_memory_diagnostics(
      include_payloads=include_payloads,
    ),
    "active_sinks": active_sink_memory_diagnostics(
      include_payloads=include_payloads,
    ),
    "writer": writer_memory_diagnostics(),
    "questions": question_memory_diagnostics(),
    "secure_inputs": secure_input_memory_diagnostics(),
  }
  report["payload_sizing"] = {
    "included": include_payloads,
    **(
      {}
      if include_payloads
      else {
        "omitted": True,
        "detail_url": "/api/debug/memory",
      }
    ),
  }
  return report


@router.get("/status")
def debug_status(
  request: Request,
  _owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Returns active agent runtimes, broadcasts, and starting state.

  `active_sdk_clients` and `active_sdk_sessions` list the SDK-backed
  runtimes (Claude via claude-agent-sdk, Codex via openai-codex).
  Completion monitors should treat a chat as "running" if it appears
  in `active_sdk_clients`, `active_sdk_sessions`, or `starting`.

  `reconciliation_failed` is True when the startup chat reconciliation
  step threw an exception. A failed reconciliation means interrupted
  chats may still show as "running" in the UI after a crash — the
  operator should investigate and restart. The field is absent (or
  False) when reconciliation succeeded.

  `media_migration_failed` follows the same absent-when-healthy contract. When
  present, old chat image paths may require recovery before they render.

  Runtime payload sizing is deliberately omitted from this cheap endpoint.
  The response says so under ``runtime_memory.payload_sizing`` and points to
  ``/api/debug/memory``; query options do not turn the status probe into the
  detailed report.
  """
  sdk_clients = [
    {"chat_id": handle.chat_id}
    for handle in registry.handles_by_kind(RunnerKind.CLAUDE_SDK)
  ]
  sdk_sessions = [
    {"chat_id": handle.chat_id}
    for handle in registry.handles_by_kind(RunnerKind.CODEX_SDK)
  ]

  broadcasts = []
  for bc in get_all_active_broadcasts():
    broadcasts.append({
      "chat_id": bc.chat_id,
      "running": bc.running,
      "event_count": len(bc.event_log),
      "subscriber_count": len(bc.subscribers),
    })

  # app.state.reconciliation_failed is set by lifespan() when the
  # startup reconciliation throws. Absent (getattr default False)
  # when reconciliation succeeded so the field is stable to check.
  reconciliation_failed = getattr(request.app.state, "reconciliation_failed", False)
  media_migration_failed = getattr(
    request.app.state, "media_migration_failed", False,
  )

  # Provider-limit parks (design §2.4). A parked chat has NO live handle and
  # NO broadcast — its turn ended — so it appears in none of the lists above;
  # this is the surface that makes a park observable (the same fields the
  # reset sweep keys on). Empty list when nothing is parked.
  parked_runs = [
    {
      "chat_id": run.chat_id,
      "run_id": run.id,
      # Distinguish an untouched park from an opted-in park whose automatic
      # continuation is still waiting/retrying. Without this, operators cannot
      # tell whether the reset sweep has claimed the row at all.
      "status": run.status,
      "parked_until": (
        run.parked_until.isoformat() if run.parked_until else None
      ),
      "park_reason": run.park_reason,
    }
    for run in (
      db.query(models.ChatRun)
      .filter(models.ChatRun.status.in_(models.CONTINUATION_RUN_STATUSES))
      # id.asc() tiebreak keeps the listing stable across reads when two
      # rows share a started_at (same rationale as the latest-run probe in
      # chat._parked_until_for_chat).
      .order_by(models.ChatRun.started_at.asc(), models.ChatRun.id.asc())
      .all()
    )
  ]

  result = {
    "active_sdk_clients": sdk_clients,
    "active_sdk_sessions": sdk_sessions,
    "starting": list(registry.starting_chat_ids()),
    "broadcasts": broadcasts,
    "parked_runs": parked_runs,
    "database_pool": database_pool_snapshot(),
  }
  try:
    from app.frontend_watcher import watcher_health
    result["frontend_watcher"] = watcher_health()
  except Exception:
    result["frontend_watcher"] = {"status": "unavailable", "running": False}
  result["allocator"] = allocator_status()
  result["browser_profiles"] = browser_profile_status()
  # These are on-demand /proc reads plus bounded owner counters. Nothing
  # samples continuously merely because observability exists.
  memory = memory_status()
  result["memory"] = memory
  result["resources"] = resource_status(
    get_settings().data_dir,
    memory=memory["cgroup"],
  )
  result["runtime_memory"] = _runtime_memory_ownership(include_payloads=False)
  try:
    from app.public_app_transport import public_app_usage_snapshot
    result["public_apps"] = public_app_usage_snapshot()
  except Exception:
    result["public_apps"] = {}
  if reconciliation_failed:
    result["reconciliation_failed"] = True
  if media_migration_failed:
    result["media_migration_failed"] = True

  # Surface the SECRET_KEY drift flag written by entrypoint.sh.
  # Present (with the detection timestamp as a string) when the key changed
  # between boots; absent when the key is stable. Lets operators discover
  # accidental drift via the API rather than having to tail container logs.
  if _SECRET_KEY_CHANGED_FLAG.exists():
    try:
      timestamp = _SECRET_KEY_CHANGED_FLAG.read_text().strip()
    except OSError:
      timestamp = "unknown"
    result["secret_key_changed"] = timestamp

  settings = get_settings()
  # Phase 4 upgrade-available notice: set when the baked image SHA changed
  # from the recorded one. Cleared when they match again.
  _upgrade_flag = Path(settings.data_dir) / ".platform-upgrade-available"
  if _upgrade_flag.exists():
    try:
      result["platform_upgrade_available"] = _upgrade_flag.read_text().strip()
    except OSError:
      result["platform_upgrade_available"] = True

  # F1 non-destructive migration notice: set when first clone-model boot found
  # an existing /data/platform (old overlay shape) and moved it aside to a
  # timestamped .pre-clone quarantine instead of deleting it. Surfaces the
  # quarantine path so the owner can migrate the preserved edits. Absent-when-
  # false, like the flags above, so the golden_debug_status test is unaffected.
  _pre_clone_flag = Path(settings.data_dir) / ".platform-pre-clone-active"
  if _pre_clone_flag.exists():
    try:
      result["platform_pre_clone_active"] = _pre_clone_flag.read_text().strip()
    except OSError:
      result["platform_pre_clone_active"] = True


  return result


@router.get("/capacity")
def debug_capacity(
  _owner: models.Owner = Depends(get_current_owner),
  refresh: bool = Query(default=False),
):
  """Operator capacity view: per-domain attribution + forecast + alert tier.

  During the 2026-09-01 incident an operator had no line-item view of WHICH
  domain filled /data (the culprit — an inactive Codex telemetry DB under
  cli-auth — was structurally un-attributable through the file-explorer du,
  which deny-lists cli-auth). This is that view: mount headroom for host / and
  /data, size-only per-domain attribution (cli-auth by byte totals only), the
  8/5/2 GiB alert tier, and the time-to-exhaustion forecast.

  Reads the snapshot cached by the periodic monitor so the probe stays cheap
  (no synchronous subtree walk). ``refresh=1`` forces a fresh domain-aware
  sample on demand (an explicit, bounded operator opt-in).
  """
  from app.capacity import capacity_snapshot, latest_snapshot, set_latest_snapshot
  from app.capacity_history import forecast, load_samples

  data_dir = get_settings().data_dir
  if refresh:
    snapshot = capacity_snapshot(data_dir, include_domains=True)
    snapshot["forecast"] = forecast(load_samples(data_dir, limit=1000))
    set_latest_snapshot(snapshot)
    return snapshot
  cached = latest_snapshot()
  if cached is not None:
    return cached
  # The periodic monitor has not run yet (fresh boot): a cheap mount-only view.
  return capacity_snapshot(data_dir, include_domains=False)


@router.get("/memory")
def debug_memory(
  _owner: models.Owner = Depends(get_current_owner),
  deep: bool = Query(default=False),
  allocation_limit: int = Query(default=25, ge=0, le=100),
  process_limit: int = Query(default=20, ge=0, le=100),
):
  """Return an explicit memory investigation report.

  Ordinary status reads stay cheap. This endpoint additionally walks cgroup
  processes and, when requested, GC-tracked object types. If opt-in
  tracemalloc was enabled before process import, ``allocation_limit`` source
  locations are included; otherwise its report explains that tracing is off.
  """
  from app.oom_diagnostics import recent_oom_events

  return {
    **memory_status(include_checkpoints=True),
    "memory_maps": memory_map_summary(),
    "processes": process_inventory(limit=process_limit),
    "runtime_memory": _runtime_memory_ownership(),
    "gc": gc_diagnostics(deep=deep),
    "allocations": allocation_report(limit=allocation_limit),
    "oom_events": recent_oom_events(get_settings().data_dir, limit=10),
  }


@router.get("/logs")
def debug_logs(
  _owner: models.Owner = Depends(get_current_owner),
  lines: int = Query(default=100, ge=1, le=5000),
  chat_id: str | None = Query(default=None),
):
  """Returns the last N lines from the chat log, optionally filtered by
  chat_id.  Reads from the end of the file efficiently."""
  settings = get_settings()
  log_path = Path(settings.data_dir) / "logs" / "chat.log"
  if not log_path.exists():
    return {"lines": [], "total_size": 0}

  total_size = log_path.stat().st_size

  # Read the last chunk of the file (generous buffer to get enough lines).
  buf_size = min(total_size, lines * 500)
  with open(log_path, "rb") as f:
    f.seek(max(0, total_size - buf_size))
    tail = f.read().decode("utf-8", errors="replace")

  all_lines = tail.strip().split("\n")

  if chat_id:
    all_lines = [l for l in all_lines if chat_id in l]

  # Bound each entry independently so one serialized payload cannot dominate
  # an otherwise line-bounded diagnostic response.
  result = [elide(line, 4000)[0] for line in all_lines[-lines:]]
  return {"lines": result, "total_size": total_size}
