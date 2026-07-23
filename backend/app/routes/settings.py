# backend/app/routes/settings.py
"""Settings API: read/write owner-level configuration.

`routes/__init__.py` is frozen and only mounts the module's top-
level `router` symbol. We expose a single outer router with no
prefix that composes the three concerns (owner settings, model
registry, model-picker prefs) via `include_router`. Adding a new
owner-scoped surface = define a sub-router in this file and
`outer_router.include_router(...)` it below.
"""

import functools
import json
import logging
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app import models, providers
from app.config import get_settings as get_app_settings
from app.database import get_db
from app.deps import (
  get_current_owner,
  get_current_owner_or_app,
  get_owner_app_or_chat_embed_for_models,
  reject_cross_site,
)
from app.schemas import (
  ModelPrefsUpdate,
  ModelRegistryResponse,
  SettingsUpdate,
)

logger = logging.getLogger(__name__)

# Bound the version shell-out so a wedged CLI can never hang the
# settings request. `--version` is a near-instant probe; two seconds is
# generous headroom.
_VERSION_TIMEOUT = 2.0

# Release dates for the installed CLI versions, keyed by bare semver, so the
# Settings row can read "2.1.183 (2026-06-19)" instead of the raw CLI banner.
#
# Captured at image-build time by the Dockerfile (`npm view <pkg>@<v> time`,
# right after the global installs) into the JSON file below — keyed by the
# versions actually installed. A CLI pin bump therefore refreshes the date
# automatically, with no hand-maintained map to keep in lockstep and no test
# to satisfy. Read once and cached; every failure mode (the file absent on a
# dev checkout, or a build that couldn't reach the npm registry) degrades to
# an empty map, and the row then shows the bare version — never an error.
_CLI_RELEASE_DATES_PATH = "/app/cli-release-dates.json"


@functools.lru_cache(maxsize=1)
def _cli_release_dates() -> dict[str, str]:
  try:
    with open(_CLI_RELEASE_DATES_PATH) as f:
      data = json.load(f)
    if isinstance(data, dict):
      return {str(k): str(v) for k, v in data.items()}
  except (OSError, ValueError):
    pass
  return {}

# Bare semver inside a CLI's `--version` banner. Tolerant of both
# shapes the pinned CLIs print:
#   claude → "2.1.173 (Claude Code)"   → captures "2.1.173"
#   codex  → "codex-cli 0.134.0"       → captures "0.134.0"
# The trailing \S* keeps a pre-release/build suffix (e.g. "1.0.0-rc.1")
# attached to the version while still dropping the surrounding prose.
_SEMVER_RE = re.compile(r"\d+\.\d+\.\d+\S*")


def _format_cli_version(raw: str | None) -> str | None:
  """Normalize a CLI `--version` banner to "<version> (<date>)".

  Parses the bare semver out of the banner (dropping the codex-cli
  prefix and the "(Claude Code)" suffix), then appends the build-captured
  release date from `_cli_release_dates()`. Falls back to the bare
  version when the date is unknown, and passes None straight through
  (CLI absent / unresponsive) — the date lookup never blocks the row.
  """
  if raw is None:
    return None
  match = _SEMVER_RE.search(raw)
  # No recognizable semver (unexpected banner shape) → surface the raw
  # string rather than dropping the row entirely.
  if match is None:
    return raw
  version = match.group(0)
  date = _cli_release_dates().get(version)
  return f"{version} ({date})" if date else version


def _background_choice_payload(choice, *, include_enabled: bool = False) -> dict | None:
  if choice is None or choice.provider is None:
    return None
  out = {"provider": choice.provider, "model": None, "effort": None}
  if isinstance(choice.model, str) and choice.model.strip():
    out["model"] = choice.model.strip()
  if isinstance(choice.effort, str) and choice.effort.strip():
    out["effort"] = choice.effort.strip()
  if include_enabled:
    out["enabled"] = choice.enabled is not False
  return out


def _background_agents_payload(update, existing: dict) -> dict:
  fields_set = getattr(update, "model_fields_set", set())
  if "providers" in fields_set:
    rows = []
    seen = set()
    for choice in update.providers or []:
      row = _background_choice_payload(choice, include_enabled=True)
      if row is None or row["provider"] in seen:
        continue
      rows.append(row)
      seen.add(row["provider"])
    enabled = [row for row in rows if row.get("enabled") is not False]
    if not enabled:
      raise HTTPException(
        status_code=422,
        detail="At least one background provider must be selected.",
      )
    return {
      "providers": rows,
      "primary": {
        k: v for k, v in enabled[0].items() if k != "enabled"
      },
      "fallback": (
        {k: v for k, v in enabled[1].items() if k != "enabled"}
        if len(enabled) > 1 else None
      ),
    }

  primary = existing.get("primary")
  if "primary" in fields_set:
    primary = (
      _background_choice_payload(update.primary)
      or existing.get("primary")
    )
  fallback = existing.get("fallback")
  if "fallback" in fields_set:
    fallback = _background_choice_payload(update.fallback)

  rows_by_provider = {
    row.get("provider"): dict(row)
    for row in existing.get("providers") or []
    if isinstance(row, dict) and row.get("provider")
  }
  enabled_provider_ids = []
  for row in (primary, fallback):
    if not isinstance(row, dict):
      continue
    provider = row.get("provider")
    if provider in enabled_provider_ids:
      continue
    rows_by_provider[provider] = {**row, "enabled": True}
    enabled_provider_ids.append(provider)

  rows = []
  seen = set()
  for provider in enabled_provider_ids:
    row = rows_by_provider.get(provider)
    if row is not None:
      rows.append(row)
      seen.add(provider)
  for row in existing.get("providers") or []:
    if not isinstance(row, dict):
      continue
    provider = row.get("provider")
    if provider in seen:
      continue
    rows.append({**row, "enabled": False})
    seen.add(provider)
  return {
    "providers": rows,
    "primary": primary,
    "fallback": fallback,
  }


def _agent_settings_payload(agent_settings) -> dict:
  out: dict[str, object] = {}
  if agent_settings is None:
    return out
  fields_set = getattr(agent_settings, "model_fields_set", set())
  if "model" in fields_set:
    model = agent_settings.model
    out["model"] = model.strip() if isinstance(model, str) and model.strip() else None
  if "effort" in fields_set:
    effort = agent_settings.effort
    out["effort"] = effort.strip() if isinstance(effort, str) and effort.strip() else None
  if "effort_by_provider" in fields_set:
    raw = agent_settings.effort_by_provider
    if isinstance(raw, dict):
      out["effort_by_provider"] = {
        str(k): str(v) for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, str) and v.strip()
      }
    else:
      out["effort_by_provider"] = None
  return out


def _autopilot_budget_view(data_dir: str) -> dict:
  """The resolved (defaults-filled) autopilot budget for the settings UI."""
  from app import agent_budget
  return agent_budget.read_budget_setting(data_dir)


def _autopilot_budget_payload(update, existing) -> dict:
  """Merge an autopilot-budget update over the existing block, clamped.

  Only fields the client actually sent are changed (partial update); the rest
  fall back to the stored value or the module defaults.
  """
  from app import agent_budget
  base = existing if isinstance(existing, dict) else {}
  fields_set = getattr(update, "model_fields_set", set())
  percent = base.get("percent", agent_budget.DEFAULT_PERCENT)
  tokens = base.get("weekly_tokens", agent_budget.DEFAULT_WEEKLY_TOKENS)
  if "percent" in fields_set and update.percent is not None:
    try:
      percent = max(0.0, min(100.0, float(update.percent)))
    except (TypeError, ValueError):
      pass
  if "weekly_tokens" in fields_set and update.weekly_tokens is not None:
    try:
      tokens = max(0, int(update.weekly_tokens))
    except (TypeError, ValueError):
      pass
  return {"percent": percent, "weekly_tokens": tokens}


# Outer composer — this is what routes/__init__.py picks up. The
# real surfaces live on the three child surfaces below.
router = APIRouter()

# Owner-level settings.
settings_router = APIRouter(prefix="/api/settings", tags=["settings"])


def _cli_version(cmd: str) -> str | None:
  """Best-effort installed version of a CLI, or None if unavailable.

  Resolves the binary on PATH first so we never shell out blind, then
  runs `<cmd> --version` under a short timeout. Every failure mode — the
  binary missing, a non-zero exit, a hang, or any OS error — degrades to
  None so the settings request can't break on a flaky CLI. Returns the
  trimmed first line of stdout (the CLIs print one line).
  """
  if shutil.which(cmd) is None:
    return None
  try:
    result = subprocess.run(
      [cmd, "--version"],
      capture_output=True,
      text=True,
      timeout=_VERSION_TIMEOUT,
    )
  except (subprocess.SubprocessError, OSError) as exc:
    logger.warning("%s --version failed: %s", cmd, exc)
    return None
  if result.returncode != 0:
    return None
  first_line = result.stdout.strip().splitlines()
  return first_line[0].strip() if first_line else None


@settings_router.get("")
def get_settings_view(
  owner: models.Owner = Depends(get_current_owner),
) -> dict:
  """Returns which optional integrations are configured.

  `claude_version` / `codex_version` are probed live on each request
  (the CLIs can be upgraded in place), and are None when the CLI isn't
  installed or doesn't respond — the UI renders those read-only.
  """
  data_dir = get_app_settings().data_dir
  codex_creds = Path(data_dir) / "cli-auth" / "codex" / "auth.json"
  configured_provider = (
    owner.provider if owner.provider in providers.PROVIDERS
    else providers.DEFAULT_PROVIDER
  )
  provider = providers.resolve_default_provider(data_dir, owner.provider)
  agent_settings = providers.effective_agent_settings(
    data_dir, None, provider
  )
  if provider != configured_provider and agent_settings.get("model") is None:
    agent_settings = {
      **agent_settings,
      "model": providers.DEFAULT_MODELS.get(provider),
    }
  return {
    "codex_authenticated": codex_creds.exists(),
    "provider": provider,
    "agent_settings": agent_settings,
    "background_agents": providers.background_agent_settings(
      data_dir, provider
    ),
    "skills_enabled": providers.skills_enabled(data_dir),
    "autopilot_budget": _autopilot_budget_view(data_dir),
    "claude_version": _format_cli_version(_cli_version("claude")),
    "codex_version": _format_cli_version(_cli_version("codex")),
  }


@settings_router.post("", dependencies=[Depends(reject_cross_site)])
def update_settings(
  body: SettingsUpdate,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
) -> dict:
  """Saves updated settings."""
  if body.provider is not None:
    owner.provider = body.provider
  # `skills_enabled` lives in the shared agent-settings.json (the
  # Owner model is frozen / chmod 444), so it's persisted outside the
  # DB transaction. Merge into the existing file so we don't clobber
  # model/effort defaults the picker wrote there.
  if (
    body.skills_enabled is not None
    or body.background_agents is not None
    or body.agent_settings is not None
    or body.autopilot_budget is not None
  ):
    data_dir = get_app_settings().data_dir

    def merge_settings(current: dict) -> dict:
      if body.skills_enabled is not None:
        current["skills_enabled"] = bool(body.skills_enabled)
      if body.agent_settings is not None:
        current.update(_agent_settings_payload(body.agent_settings))
      if body.background_agents is not None:
        provider = providers.resolve_default_provider(data_dir, owner.provider)
        existing = providers.background_agent_settings(data_dir, provider)
        current["background_agents"] = _background_agents_payload(
          body.background_agents,
          existing,
        )
      if body.autopilot_budget is not None:
        current["autopilot_budget"] = _autopilot_budget_payload(
          body.autopilot_budget, current.get("autopilot_budget"),
        )
      return current

    if not providers.update_agent_settings(data_dir, merge_settings):
      db.rollback()
      raise HTTPException(
        status_code=500,
        detail="Could not save agent settings to disk. The previous settings are unchanged.",
      )
  db.commit()
  return {"ok": True}


# ─── Model registry ─────────────────────────────────────────────────
#
# Mounted under the same router so the model surface stays adjacent
# to the rest of owner-level preferences. The picker only needs the
# combined list across providers; the upstream fetcher already does
# per-provider isolation (failure on one falls back to KNOWN_MODELS
# for that provider without affecting the other).

# Separate router instance — the model + prefs endpoints belong
# under different prefixes than /api/settings.
models_router = APIRouter(prefix="/api/models", tags=["models"])


@models_router.get("", response_model=ModelRegistryResponse)
async def list_model_registry(
  refresh: bool = Query(
    default=False,
    description="When true, bypass the 5-minute cache and re-fetch "
    "upstream. Used by the manage-models modal's explicit refresh.",
  ),
  _: models.Owner = Depends(get_owner_app_or_chat_embed_for_models),
) -> ModelRegistryResponse:
  """Returns the registry of available models per provider.

  Live data is cached for 5 minutes per provider. A fetch failure
  falls back to KNOWN_MODELS for that provider — the other
  provider's live data still flows.

  Accepts app-scoped tokens, unlike the rest of this module: the
  registry is read-only and model ids aren't secrets, and mini-apps
  with a model picker (e.g. a per-conversation model selector) need
  the list. Settings reads/writes and owner prefs stay owner-only.
  """
  data_dir = get_app_settings().data_dir
  registry = await providers.list_models(data_dir, force_refresh=refresh)
  return ModelRegistryResponse(providers=registry)


owner_router = APIRouter(prefix="/api/owner", tags=["owner"])


@owner_router.get("/model-prefs")
def get_model_prefs(
  owner: models.Owner = Depends(get_owner_app_or_chat_embed_for_models),
) -> dict:
  """Returns the owner's model-picker preferences.

  Owners without a saved preference receive the curated default hidden set.
  An explicitly saved `{"hidden_ids": []}` is distinct and shows every
  registry entry.
  """
  return {"hidden_ids": providers.hidden_model_ids(owner.model_prefs_json)}


@owner_router.patch("/model-prefs", dependencies=[Depends(reject_cross_site)])
def update_model_prefs(
  body: ModelPrefsUpdate,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
) -> dict:
  """Replaces the owner's model-picker preferences.

  `hidden_ids` is set verbatim — the modal always sends the full
  desired set so a partial-update merge isn't needed. Stale IDs
  (referring to models no longer in the registry) are tolerated:
  they sit in the prefs harmlessly until the next save trims them.
  """
  # Deduplicate while preserving order — the order isn't load-
  # bearing for filtering but a stable list reads better in any
  # debug surface.
  seen: set[str] = set()
  cleaned: list[str] = []
  for entry in body.hidden_ids:
    if entry in seen:
      continue
    seen.add(entry)
    cleaned.append(entry)
  owner.model_prefs_json = {"hidden_ids": cleaned}
  db.commit()
  return {"hidden_ids": cleaned}


@owner_router.get("/walkthrough")
def get_walkthrough(
  owner: models.Owner = Depends(get_current_owner),
) -> dict:
  """Reports whether the post-signup walkthrough should appear.

  The shell calls this once on mount and shows the WalkthroughOverlay
  iff `completed` is False. The timestamp is exposed so future
  analytics can read when the user got onboarded; the shell ignores
  it. Treats absence as "not completed yet" — fresh owners pre-
  migration land here with NULL and see the walkthrough exactly once.
  """
  completed_at = owner.walkthrough_completed_at
  return {
    "completed": completed_at is not None,
    "completed_at": completed_at.isoformat() if completed_at else None,
  }


@owner_router.post(
  "/walkthrough/complete",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def mark_walkthrough_complete(
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
) -> Response:
  """Records walkthrough completion. Idempotent and write-once: a
  second POST is a no-op rather than refreshing the timestamp, so the
  original completion time stays accurate for downstream analytics
  (the model comment commits to that). We don't require a payload —
  completion is a single bit and there's nothing to configure."""
  if owner.walkthrough_completed_at is None:
    owner.walkthrough_completed_at = datetime.now(UTC)
    db.commit()
  return Response(status_code=204)


# Compose: a single outer router so routes/__init__.py's frozen
# `_load("settings")` picks up all the surfaces.
router.include_router(settings_router)
router.include_router(models_router)
router.include_router(owner_router)
