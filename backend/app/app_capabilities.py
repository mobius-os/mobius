"""Canonical, owner-reviewable capability contracts for app manifests.

The manifest is the author's declaration; this module turns it into one small,
versioned object that every install surface can render and bind to the eventual
install.  Keeping the contract server-derived prevents the App Store and the
installer from slowly learning different meanings for privileged fields.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


CONTRACT_SCHEMA = 1


def contract_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
  """Return the normalized capability contract for a validated manifest."""
  perms = manifest.get("permissions") or {}
  schedule = manifest.get("schedule") or {}
  requested_logs = perms.get("chat_log_access", "none")
  # The only chat-log route is structurally redacted.  A historical ``full``
  # declaration therefore has summary effectiveness, never silent full access.
  effective_logs = "summary" if requested_logs in ("summary", "full") else "none"
  job = schedule.get("job")
  cron = schedule.get("default")
  system_prompt = manifest.get("system_prompt")
  return {
    "schema": CONTRACT_SCHEMA,
    "system_app": bool(manifest.get("system_app", False)),
    "agent": {
      "system_prompt": (
        {
          "file": system_prompt,
          "scope": "all_agent_chats",
          "activation": "next_turn",
        }
        if system_prompt else None
      ),
      "skills": sorted(set(manifest.get("skills") or [])),
      "embeds_agent": bool(manifest.get("embeds_agent", False)),
    },
    "data": {
      "chat_logs": {
        "requested": requested_logs,
        "effective": effective_logs,
        "redaction": "structural" if effective_logs == "summary" else "none",
      },
      "filesystem_api": bool(perms.get("filesystem_access", False)),
      "shared_memory": perms.get("shared_memory", "none"),
      "cross_app_access": perms.get("cross_app_access", "none"),
      "share_with_apps": perms.get("share_with_apps", "none"),
      "manage_apps": bool(perms.get("manage_apps", False)),
      "github_access": bool(perms.get("github_access", False)),
    },
    "background": (
      {
        "job": job,
        "mode": "scheduled" if cron else "on_demand",
        "cron": cron,
        "user_configurable": bool(schedule.get("user_configurable", False)),
        "initialize_on_install": bool(schedule.get("initialize_on_install", False)),
        "agent": bool(perms.get("background_agent", False)),
        # A job is outside the iframe.  This label is intentionally explicit so
        # ``filesystem_api: false`` is never misread as constraining the job.
        "authority": "scoped_system_job" if perms.get("background_agent") else "app_job_process",
      }
      if job else None
    ),
    "offline": {
      "capable": bool(manifest.get("offline_capable", False)),
      "contract": manifest.get("offline") or None,
    },
  }


def canonical_contract_json(contract: dict[str, Any]) -> str:
  return json.dumps(
    contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
  )


def capability_digest(contract: dict[str, Any]) -> str:
  return hashlib.sha256(canonical_contract_json(contract).encode("utf-8")).hexdigest()


def contract_and_digest(manifest: dict[str, Any]) -> tuple[dict[str, Any], str]:
  contract = contract_from_manifest(manifest)
  return contract, capability_digest(contract)


def diff_contracts(
  installed: dict[str, Any] | None,
  candidate: dict[str, Any],
) -> dict[str, list[str] | bool]:
  """Return stable changed capability paths for update review.

  Values are compared at leaf paths.  The UI owns severity/copy; the backend
  only reports precise semantic changes and whether the prior contract was
  unavailable (legacy install).
  """
  if not isinstance(installed, dict):
    return {"unknown_previous": True, "added": [], "removed": [], "changed": []}

  def leaves(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
      out: dict[str, Any] = {}
      for key in sorted(value):
        path = f"{prefix}.{key}" if prefix else key
        out.update(leaves(value[key], path))
      return out
    if isinstance(value, list):
      return {prefix: value}
    return {prefix: value}

  before = leaves(installed)
  after = leaves(candidate)
  added = sorted(k for k in after.keys() - before.keys())
  removed = sorted(k for k in before.keys() - after.keys())
  changed = sorted(k for k in before.keys() & after.keys() if before[k] != after[k])
  return {
    "unknown_previous": False,
    "added": added,
    "removed": removed,
    "changed": changed,
  }
