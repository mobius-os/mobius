"""The owner's view of storage and memory for the Settings box.

Cheap enough to read whenever Settings opens: one disk and cgroup reading, the
domain sizes the capacity monitor last measured, one process pass for memory,
and the chats with a live agent turn. ``level`` is set only when the owner
should act, so a healthy server shows no status at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app import models
from app.capacity import data_alert_tier, domain_label, latest_snapshot
from app.memory_observability import process_inventory
from app.resource_cleanup import CLEANUP_TARGETS
from app.resource_pressure import resource_status

# Alert tiers that already warn the owner by push; below them the box stays quiet.
_WARNING_TIERS = {"warning", "critical"}
_MEMORY_GROUPS = {
  "claude": "Agent turns",
  "codex": "Agent turns",
  "browser": "Agent browsers",
  "mobius_server": "Möbius server",
}


def _storage(status: dict[str, Any]) -> dict[str, Any] | None:
  disk = status["facts"]["disk"]
  pressure = status["pressure"]["disk"]
  if not disk.get("available") or pressure["state"] == "unknown":
    return None
  free, total = disk["free_bytes"], disk["total_bytes"]
  level = {"critical": "paused", "constrained": "low"}.get(pressure["state"])
  if level is None and data_alert_tier(free, total) in _WARNING_TIERS:
    level = "getting_low"
  return {
    "free_bytes": free,
    "total_bytes": total,
    "pause_below_bytes": pressure["critical_below_bytes"],
    "level": level,
  }


def _memory_groups() -> list[dict[str, Any]]:
  totals: dict[str, int] = {}
  for group in process_inventory(limit=0).get("groups", []):
    label = _MEMORY_GROUPS.get(group.get("category"), "Other")
    totals[label] = totals.get(label, 0) + int(group.get("pss_bytes") or 0)
  return sorted(
    ({"label": label, "bytes": size} for label, size in totals.items() if size),
    key=lambda row: -row["bytes"],
  )


def _memory(status: dict[str, Any]) -> dict[str, Any] | None:
  facts = status["facts"]["memory"]
  pressure = status["pressure"]["memory"]
  if pressure["state"] == "unknown":
    return None
  limit = int(facts["limit_bytes"])
  return {
    "used_bytes": int(facts["working_set_bytes"]),
    "limit_bytes": limit,
    "pause_at_bytes": int(limit * pressure["critical_at_ratio"]),
    "level": {"critical": "paused", "constrained": "low"}.get(pressure["state"]),
    "groups": _memory_groups(),
  }


def _incomplete(domain: dict[str, Any]) -> bool:
  return bool(domain.get("truncated") or domain.get("timed_out"))


def _usage(snapshot: dict[str, Any]) -> dict[str, Any]:
  """Stored size per cleanup and per kept folder.

  Every cleanup is listed even before the first measurement, because its
  confirm step measures for itself. A folder whose measurement stopped early
  is flagged, never shown as a plain smaller number.
  """
  domains = snapshot.get("domains") or {}
  cleanable_domains = {name for target in CLEANUP_TARGETS.values() for name in target.domains}
  cleanable = [
    {
      "target": target_id,
      "bytes": (
        sum(int(domains[name].get("bytes") or 0) for name in target.domains if name in domains)
        if domains else None
      ),
      "incomplete": any(_incomplete(domains.get(name, {})) for name in target.domains),
    }
    for target_id, target in CLEANUP_TARGETS.items()
  ]
  kept = sorted(
    (
      {
        "id": name,
        "label": domain_label(name),
        "bytes": int(domain.get("bytes") or 0),
        "incomplete": _incomplete(domain),
      }
      for name, domain in domains.items()
      if name not in cleanable_domains and (domain.get("bytes") or _incomplete(domain))
    ),
    key=lambda row: -row["bytes"],
  )
  return {
    "measured_at": snapshot.get("domains_captured_at") if domains else None,
    "cleanable": cleanable,
    "kept": kept,
  }


def _running_turns(db, alive_chat_ids: set[str]) -> list[dict[str, Any]]:
  """Chats with a live agent turn, with what a Stop would also cancel.

  Stopping clears the chat's queued messages; only the chat screen can send
  them again, so Settings needs to know about them before offering Stop.
  """
  if not alive_chat_ids:
    return []
  rows = (
    db.query(models.Chat.id, models.Chat.title, models.Chat.pending_messages)
    .filter(models.Chat.id.in_(alive_chat_ids), models.Chat.deleted_at.is_(None))
    .order_by(models.Chat.activity_at.desc())
    .all()
  )
  helpers = _active_helpers(db, [row.id for row in rows])
  return [
    {
      "chat_id": row.id,
      "title": row.title,
      # Hidden entries are machine results that Stop keeps; only what the
      # owner typed is lost.
      "queued": sum(1 for entry in row.pending_messages or [] if not entry.get("hidden")),
      "helpers": helpers.get(row.id, 0),
    }
    for row in rows
  ]


def _active_helpers(db, parent_chat_ids: list[str]) -> dict[str, int]:
  """Helpers a Stop would cancel, counted by the rule the cancel itself uses."""
  from app.delegations import ACTIVE_DELEGATION_STATUSES, derived_status

  counts: dict[str, int] = {}
  rows = db.query(models.Delegation).filter(
    models.Delegation.parent_chat_id.in_(parent_chat_ids),
    models.Delegation.cancelled_at.is_(None),
  ).all()
  for row in rows:
    if derived_status(db, row, load_result=False)[0] in ACTIVE_DELEGATION_STATUSES:
      counts[row.parent_chat_id] = counts.get(row.parent_chat_id, 0) + 1
  return counts


def resources_overview(data_dir: str | Path, alive_chat_ids: set[str]) -> dict[str, Any]:
  """The whole overview. Blocking, so routes run it in a worker thread.

  ``alive_chat_ids`` is the runner registry read on the event loop.
  """
  from app.database import SessionLocal

  status = resource_status(data_dir)
  with SessionLocal() as db:
    running = _running_turns(db, alive_chat_ids)
  return {
    "storage": _storage(status),
    "memory": _memory(status),
    **_usage(latest_snapshot() or {}),
    "running": running,
  }
