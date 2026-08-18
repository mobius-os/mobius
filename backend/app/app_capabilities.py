"""Canonical, owner-reviewable capability contracts for app manifests.

The manifest is the author's declaration; this module turns it into one small,
versioned object that every install surface can render and bind to the eventual
install.  Keeping the contract server-derived prevents the App Store and the
installer from slowly learning different meanings for privileged fields.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit


CONTRACT_SCHEMA = 5

_PUBLIC_NETWORK_RULE_LIMIT = 16
_PUBLIC_QUERY_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _normalize_public_query(value: Any, *, rule_index: int) -> dict[str, Any]:
  if value is None:
    return {}
  if not isinstance(value, dict) or set(value) - {"allow", "exact", "sha256"}:
    raise ValueError(
      f"Manifest public network rule {rule_index} query must contain only "
      "`allow`, `exact`, and `sha256`."
    )
  allow = value.get("allow", [])
  exact = value.get("exact", {})
  digests = value.get("sha256", {})
  if not isinstance(allow, list) or not all(
    isinstance(name, str) and _PUBLIC_QUERY_NAME.fullmatch(name)
    for name in allow
  ):
    raise ValueError(
      f"Manifest public network rule {rule_index} query.allow must be an "
      "array of bounded parameter names."
    )
  if len(set(allow)) != len(allow):
    raise ValueError(
      f"Manifest public network rule {rule_index} query.allow contains duplicates."
    )

  def normalized_map(raw: Any, *, field: str, digest: bool) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
      raise ValueError(
        f"Manifest public network rule {rule_index} query.{field} must be an object."
      )
    result: dict[str, list[str]] = {}
    for name in sorted(raw):
      accepted = raw[name]
      valid_name = isinstance(name, str) and _PUBLIC_QUERY_NAME.fullmatch(name)
      valid_values = (
        isinstance(accepted, list)
        and 1 <= len(accepted) <= 8
        and all(isinstance(item, str) for item in accepted)
      )
      if not valid_name or not valid_values:
        raise ValueError(
          f"Manifest public network rule {rule_index} query.{field} entries "
          "must map bounded parameter names to 1-8 strings."
        )
      if digest:
        if not all(re.fullmatch(r"[0-9a-f]{64}", item) for item in accepted):
          raise ValueError(
            f"Manifest public network rule {rule_index} query.sha256 values "
            "must be lowercase SHA-256 digests."
          )
      elif not all(len(item) <= 4096 for item in accepted):
        raise ValueError(
          f"Manifest public network rule {rule_index} query.exact values may "
          "contain at most 4096 characters."
        )
      result[name] = sorted(set(accepted))
    return result

  normalized_exact = normalized_map(exact, field="exact", digest=False)
  normalized_digests = normalized_map(digests, field="sha256", digest=True)
  names = set(allow) | set(normalized_exact) | set(normalized_digests)
  if len(names) > 16:
    raise ValueError(
      f"Manifest public network rule {rule_index} may constrain at most 16 "
      "query parameters."
    )
  if len(names) != len(allow) + len(normalized_exact) + len(normalized_digests):
    raise ValueError(
      f"Manifest public network rule {rule_index} query parameter names must "
      "belong to exactly one constraint."
    )
  normalized: dict[str, Any] = {}
  if allow:
    normalized["allow"] = sorted(allow)
  if normalized_exact:
    normalized["exact"] = normalized_exact
  if normalized_digests:
    normalized["sha256"] = normalized_digests
  return normalized


def normalize_public_access(manifest: dict[str, Any]) -> dict[str, Any]:
  """Normalize the network surface available to anonymous app sessions.

  Publication itself is owner state and deliberately does not live in the
  manifest. The package may only declare a bounded set of exact HTTPS origins
  and path prefixes that the public, GET-only fetch capability may reach.
  """
  raw = manifest.get("public_access")
  if raw is None:
    return {"network": []}
  if not isinstance(raw, dict) or set(raw) - {"network"}:
    raise ValueError(
      "Manifest `public_access` must be an object containing only `network`."
    )
  network = raw.get("network", [])
  if not isinstance(network, list):
    raise ValueError("Manifest `public_access.network` must be an array.")
  if len(network) > _PUBLIC_NETWORK_RULE_LIMIT:
    raise ValueError(
      f"Manifest `public_access.network` may contain at most "
      f"{_PUBLIC_NETWORK_RULE_LIMIT} rules."
    )

  normalized: list[dict[str, Any]] = []
  seen: set[tuple[str, str, str]] = set()
  for index, rule in enumerate(network):
    if (
      not isinstance(rule, dict)
      or set(rule) - {"origin", "path_prefix", "query"}
      or not {"origin", "path_prefix"}.issubset(rule)
    ):
      raise ValueError(
        "Each `public_access.network` rule must contain `origin` and "
        "`path_prefix`, with only an optional `query` contract."
      )
    origin = rule.get("origin")
    prefix = rule.get("path_prefix")
    if not isinstance(origin, str) or not isinstance(prefix, str):
      raise ValueError(
        f"Manifest public network rule {index + 1} must use string values."
      )
    try:
      parsed = urlsplit(origin.strip())
      port = parsed.port
    except ValueError as exc:
      raise ValueError(
        f"Manifest public network rule {index + 1} has an invalid origin."
      ) from exc
    if (
      parsed.scheme != "https"
      or not parsed.hostname
      or parsed.username is not None
      or parsed.password is not None
      or parsed.path not in ("", "/")
      or parsed.query
      or parsed.fragment
    ):
      raise ValueError(
        f"Manifest public network rule {index + 1} origin must be an exact "
        "HTTPS origin."
      )
    host = parsed.hostname.lower()
    authority = host if port in (None, 443) else f"{host}:{port}"
    normalized_origin = f"https://{authority}"
    if (
      not prefix.startswith("/")
      or "?" in prefix
      or "#" in prefix
      or len(prefix) > 512
    ):
      raise ValueError(
        f"Manifest public network rule {index + 1} path_prefix must be a "
        "plain absolute path."
      )
    query = _normalize_public_query(rule.get("query"), rule_index=index + 1)
    key = (
      normalized_origin,
      prefix,
      json.dumps(query, sort_keys=True, separators=(",", ":")),
    )
    if key not in seen:
      seen.add(key)
      normalized_rule: dict[str, Any] = {
        "origin": normalized_origin,
        "path_prefix": prefix,
      }
      if query:
        normalized_rule["query"] = query
      normalized.append(normalized_rule)
  return {"network": normalized}


def public_access_declaration_from_contract(
  contract: dict[str, Any] | None,
) -> dict[str, Any]:
  """Recover the normalized public declaration from a stored contract."""
  value = contract.get("public") if isinstance(contract, dict) else None
  network = value.get("network") if isinstance(value, dict) else None
  if not isinstance(network, list):
    return {"network": []}
  return {"network": deepcopy(network)}


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
  "workspace.screen-control": {
    "version": 1,
    "kind": "session",
    "title": "Control this M\u00f6bius screen",
    "description": (
      "Let this app's support chat inspect and control the current M\u00f6bius tab."
    ),
    "risk": "device",
    # The owner may navigate away from the control app while the exact app-owned
    # support chat continues investigating. The browser's capture indicator and
    # M\u00f6bius's active-session stop affordance remain authoritative.
    "lifecycle": "background",
    "default_limits": {},
    "hard_limits": {},
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
  fields: dict[str, Any] = {
    "capabilities": capabilities,
    "public_access": normalize_public_access(manifest),
  }
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
      "connections_manage": bool(perms.get("connections_manage", False)),
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
    "public": normalize_public_access(manifest),
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
  public_access: dict[str, Any] | None = None,
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
  if public_access is None:
    public_access = public_access_declaration_from_contract(
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
      "connections_manage": bool(getattr(app, "connections_manage", False)),
    },
    "offline_capable": bool(getattr(app, "offline_capable", False)),
    "offline": getattr(app, "offline_contract", None),
    "capabilities": capabilities,
    "public_access": public_access,
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


def contract_with_runtime_capabilities(
  contract: dict[str, Any] | None,
  manifest: dict[str, Any],
) -> dict[str, Any] | None:
  """Replace only the runtime capabilities in a reviewed contract.

  Store-installed apps keep package-owned background, data, agent, and offline
  facts when an owner explicitly accepts capabilities from a local source
  revision.  The caller binds that acceptance to a digest; this projection
  merely owns the narrow immutable replacement.
  """
  if not isinstance(contract, dict):
    return None
  updated = deepcopy(contract)
  updated["runtime"] = normalize_runtime_capabilities(manifest)
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
