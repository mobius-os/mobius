"""Canonical, owner-reviewable capability contracts for app manifests.

The manifest is the author's declaration; this module turns it into one small,
versioned object that every install surface can render and bind to the eventual
install.  Keeping the contract server-derived prevents the App Store and the
installer from slowly learning different meanings for privileged fields.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any


CONTRACT_SCHEMA = 4


# Host-mediated browser capabilities. These are deliberately separate from
# server API permissions: a runtime capability crosses the opaque iframe
# boundary through the shell, while a server permission gates an authenticated
# HTTP route. Both appear in the same install-review contract below.
#
# Capability ids are stable names. Each capability evolves independently via
# its own integer version, so adding (say) camera v2 never forces every storage
# or microphone consumer onto a new global runtime version.
RUNTIME_CAPABILITY_DEFINITIONS: dict[str, dict[str, Any]] = {
  "device.asset-cache": {
    "version": 1,
    "kind": "session",
    "title": "Store large files on this device",
    "description": (
      "Download verified app assets into this browser's private storage."
    ),
    "risk": "storage",
    "lifecycle": "active_frame",
    "default_limits": {
      "max_bytes": 256 * 1024 * 1024,
      "max_asset_bytes": 256 * 1024 * 1024,
      "max_chunk_bytes": 8 * 1024 * 1024,
    },
    "hard_limits": {
      "max_bytes": (1 * 1024 * 1024, 2 * 1024 * 1024 * 1024),
      "max_asset_bytes": (1 * 1024 * 1024, 1 * 1024 * 1024 * 1024),
      "max_chunk_bytes": (256 * 1024, 16 * 1024 * 1024),
    },
  },
  "device.speech-models": {
    "version": 1,
    "kind": "session",
    "title": "Manage local speech models",
    "description": (
      "Download and select verified speech models shared by apps on this device."
    ),
    "risk": "storage",
    "lifecycle": "active_frame",
    "default_limits": {
      "max_bytes": 256 * 1024 * 1024,
      "max_asset_bytes": 256 * 1024 * 1024,
      "max_chunk_bytes": 8 * 1024 * 1024,
    },
    "hard_limits": {
      "max_bytes": (1 * 1024 * 1024, 2 * 1024 * 1024 * 1024),
      "max_asset_bytes": (1 * 1024 * 1024, 1 * 1024 * 1024 * 1024),
      "max_chunk_bytes": (256 * 1024, 16 * 1024 * 1024),
    },
  },
  "media.speech": {
    "version": 1,
    "kind": "session",
    "title": "Generate speech",
    "description": "Turn text into audio with a speech model saved on this device.",
    "risk": "device",
    "lifecycle": "background",
    "default_limits": {"max_text_chars": 50_000},
    "hard_limits": {"max_text_chars": (1, 250_000)},
  },
  "media.microphone.capture": {
    "version": 1,
    "kind": "session",
    "title": "Record audio",
    "description": "Use the device microphone while this app is visible.",
    "risk": "device",
    "lifecycle": "active_frame",
    "default_limits": {"max_duration_ms": 30_000},
    "hard_limits": {"max_duration_ms": (100, 60_000)},
  },
}


def normalize_runtime_capabilities(manifest: dict[str, Any]) -> dict[str, Any]:
  """Validate and normalize manifest-declared host capabilities.

  Unknown names or versions fail closed: an install surface cannot honestly
  review a capability whose semantics this platform does not know. Optional
  provider/plugin capability catalogs can extend this registry in the future;
  they must supply the same stable definition shape before install review.
  """
  requested = manifest.get("capabilities")
  if requested is None:
    requested = {}
  if not isinstance(requested, dict):
    raise ValueError("Manifest `capabilities` must be an object.")

  normalized: dict[str, Any] = {}
  for capability_id in sorted(requested):
    raw = requested[capability_id]
    definition = RUNTIME_CAPABILITY_DEFINITIONS.get(capability_id)
    if definition is None:
      raise ValueError(f"Unknown capability `{capability_id}`.")
    if not isinstance(raw, dict):
      raise ValueError(
        f"Manifest capability `{capability_id}` must be an object."
      )

    version = raw.get("version")
    if version != definition["version"]:
      raise ValueError(
        f"Capability `{capability_id}` requires version "
        f"{definition['version']}."
      )
    reason = raw.get("reason")
    if reason is not None and (
      not isinstance(reason, str) or not reason.strip() or len(reason) > 240
    ):
      raise ValueError(
        f"Capability `{capability_id}` reason must be 1-240 characters."
      )

    raw_limits = raw.get("limits") or {}
    if not isinstance(raw_limits, dict):
      raise ValueError(
        f"Capability `{capability_id}` limits must be an object."
      )
    unknown_limits = set(raw_limits) - set(definition["hard_limits"])
    if unknown_limits:
      raise ValueError(
        f"Capability `{capability_id}` has unknown limits: "
        + ", ".join(sorted(unknown_limits))
        + "."
      )
    limits = dict(definition["default_limits"])
    for key, value in raw_limits.items():
      if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
          f"Capability `{capability_id}` limit `{key}` must be a number."
        )
      low, high = definition["hard_limits"][key]
      if value < low or value > high:
        raise ValueError(
          f"Capability `{capability_id}` limit `{key}` must be between "
          f"{low} and {high}."
        )
      limits[key] = int(value)

    normalized[capability_id] = {
      "version": definition["version"],
      "kind": definition["kind"],
      "title": definition["title"],
      "description": definition["description"],
      "risk": definition["risk"],
      "lifecycle": definition["lifecycle"],
      "reason": reason.strip() if isinstance(reason, str) else None,
      "limits": limits,
    }
  return normalized


def local_manifest_runtime_fields(manifest: dict[str, Any]) -> dict[str, Any]:
  """Return the local-manifest fields owned by the live app runtime.

  Explicit local apply consumes this projection for both creation and updates,
  so one parser prevents those paths interpreting the same declaration
  differently.
  """
  if not isinstance(manifest, dict):
    raise ValueError("mobius.json must contain a JSON object.")
  capabilities = manifest.get("capabilities")
  if capabilities is None:
    capabilities = {}
  if not isinstance(capabilities, dict):
    raise ValueError("mobius.json `capabilities` must be an object.")
  # Validate names, versions, reasons, and limits now; callers still need the
  # author declaration rather than the host-enriched normalized contract.
  normalize_runtime_capabilities(manifest)
  fields: dict[str, Any] = {"capabilities": capabilities}
  if "offline_capable" in manifest:
    offline_capable = manifest["offline_capable"]
    if not isinstance(offline_capable, bool):
      raise ValueError(
        "mobius.json `offline_capable` must be true or false."
      )
    fields["offline_capable"] = offline_capable
  return fields


def contract_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
  """Return the normalized capability contract for a validated manifest."""
  perms = manifest.get("permissions") or {}
  schedule = manifest.get("schedule") or {}
  requested_logs = perms.get("chat_log_access", "none")
  # Both readable tiers use structural redaction. The higher tier widens only
  # the lifecycle scope to recoverable soft-deleted chats; there is no raw/full
  # transcript capability.
  effective_logs = (
    requested_logs
    if requested_logs in ("summary", "summary_with_deleted")
    else "none"
  )
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
          "scope": "chats_started_while_installed",
          "activation": "chat_start",
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
        "redaction": "structural" if effective_logs != "none" else "none",
      },
      "filesystem_api": bool(perms.get("filesystem_access", False)),
      "shared_memory": perms.get("shared_memory", "none"),
      "cross_app_access": perms.get("cross_app_access", "none"),
      "share_with_apps": perms.get("share_with_apps", "none"),
      "manage_apps": bool(perms.get("manage_apps", False)),
      "manage_skills": bool(perms.get("manage_skills", False)),
      "github_access": bool(perms.get("github_access", False)),
      "github_connect": bool(perms.get("github_connect", False)),
    },
    "background": (
      {
        "job": job,
        "mode": "scheduled" if cron else "on_demand",
        "cron": cron,
        "user_configurable": bool(schedule.get("user_configurable", False)),
        "initialize_on_install": bool(schedule.get("initialize_on_install", False)),
      }
      if job else None
    ),
    "offline": {
      "capable": bool(manifest.get("offline_capable", False)),
      "contract": manifest.get("offline") or None,
    },
    "runtime": normalize_runtime_capabilities(manifest),
  }


def runtime_declaration_from_contract(
  contract: dict[str, Any] | None,
) -> dict[str, Any]:
  """Recover the author-controlled part of a normalized runtime contract.

  Normalized contracts contain host copy and lifecycle metadata in addition to
  the declaration.  Local app metadata updates must not feed that host-owned
  material back through the public declaration parser, so retain only the
  version, reason, and reviewed limits.
  """
  runtime = contract.get("runtime", {}) if isinstance(contract, dict) else {}
  if not isinstance(runtime, dict):
    return {}
  declaration: dict[str, Any] = {}
  for capability_id, value in runtime.items():
    if not isinstance(value, dict):
      continue
    declaration[capability_id] = {
      "version": value.get("version"),
      "reason": value.get("reason"),
      "limits": dict(value.get("limits") or {}),
    }
  return declaration


def contract_from_app_state(
  app: Any,
  *,
  capabilities: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Build an accurate contract for an owner-authored local app.

  Store installs derive their complete contract from the reviewed manifest.
  Local apps are created and edited directly by their owner, so their durable
  database state is authoritative for server permissions while ``mobius.json``
  is authoritative for host-mediated runtime capabilities.
  """
  if capabilities is None:
    capabilities = runtime_declaration_from_contract(
      getattr(app, "capability_contract", None),
    )
  manifest = {
    "system_app": bool(getattr(app, "system_app", False)),
    "system_prompt": getattr(app, "system_prompt_file", None),
    "embeds_agent": bool(getattr(app, "embeds_agent", False)),
    "permissions": {
      "chat_log_access": getattr(app, "chat_log_access", "none"),
      "filesystem_access": bool(getattr(app, "filesystem_access", False)),
      "cross_app_access": getattr(app, "cross_app_access", "none"),
      "share_with_apps": getattr(app, "share_with_apps", "none"),
      "manage_apps": bool(getattr(app, "manage_apps", False)),
      "manage_skills": bool(getattr(app, "manage_skills", False)),
      "github_access": bool(getattr(app, "github_access", False)),
      "github_connect": bool(getattr(app, "github_connect", False)),
    },
    "offline_capable": bool(getattr(app, "offline_capable", False)),
    "offline": getattr(app, "offline_contract", None),
    "capabilities": capabilities,
  }
  return contract_from_manifest(manifest)


def contract_with_chat_log_access(
  contract: dict[str, Any] | None,
  access: str,
) -> dict[str, Any] | None:
  """Update only the accepted chat-log scope in a reviewed contract.

  Store contracts contain package-owned facts (skills, background work,
  runtime limits, and more) that an owner permission change must preserve.
  Rebuilding one from the App row would silently discard those facts.  This
  narrow immutable projection keeps the complete reviewed contract intact
  while recording the owner's explicit requested/effective scope choice.
  """
  if not isinstance(contract, dict):
    return None
  updated = deepcopy(contract)
  data = updated.get("data")
  if not isinstance(data, dict):
    data = {}
    updated["data"] = data
  chat_logs = data.get("chat_logs")
  if not isinstance(chat_logs, dict):
    chat_logs = {}
    data["chat_logs"] = chat_logs
  chat_logs["requested"] = access
  chat_logs["effective"] = access
  chat_logs["redaction"] = "structural" if access != "none" else "none"
  return updated


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
