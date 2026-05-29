# backend/app/routes/settings.py
"""Settings API: read/write owner-level configuration.

`routes/__init__.py` is frozen and only mounts the module's top-
level `router` symbol. We expose a single outer router with no
prefix that composes the three concerns (owner settings, model
registry, model-picker prefs) via `include_router`. Adding a new
owner-scoped surface = define a sub-router in this file and
`outer_router.include_router(...)` it below.
"""

from pathlib import Path

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app import models, providers
from app.auth import encrypt_api_key
from app.config import get_settings as get_app_settings
from app.database import get_db
from app.deps import get_current_owner
from app.schemas import (
  ModelPrefsUpdate,
  ModelRegistryResponse,
  SettingsUpdate,
)

# Outer composer — this is what routes/__init__.py picks up. The
# real surfaces live on the three child routers below.
router = APIRouter()

# Owner-level settings (Gemini key, provider preference).
settings_router = APIRouter(prefix="/api/settings", tags=["settings"])


@settings_router.get("")
def get_settings_view(
  owner: models.Owner = Depends(get_current_owner),
) -> dict:
  """Returns which optional integrations are configured."""
  data_dir = get_app_settings().data_dir
  codex_creds = Path(data_dir) / "cli-auth" / "codex" / "auth.json"
  return {
    "gemini_configured": owner.gemini_api_key_enc is not None,
    "codex_authenticated": codex_creds.exists(),
    "provider": owner.provider or "claude",
  }


@settings_router.post("")
def update_settings(
  body: SettingsUpdate,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
) -> dict:
  """Saves updated settings. Pass empty string to clear a key."""
  if body.gemini_api_key is not None:
    if body.gemini_api_key == "":
      owner.gemini_api_key_enc = None
    else:
      owner.gemini_api_key_enc = encrypt_api_key(body.gemini_api_key)
  if body.provider is not None:
    owner.provider = body.provider
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
  _: models.Owner = Depends(get_current_owner),
) -> ModelRegistryResponse:
  """Returns the registry of available models per provider.

  Live data is cached for 5 minutes per provider. A fetch failure
  falls back to KNOWN_MODELS for that provider — the other
  provider's live data still flows.
  """
  data_dir = get_app_settings().data_dir
  registry = await providers.list_models(data_dir, force_refresh=refresh)
  return ModelRegistryResponse(providers=registry)


owner_router = APIRouter(prefix="/api/owner", tags=["owner"])


@owner_router.get("/model-prefs")
def get_model_prefs(
  owner: models.Owner = Depends(get_current_owner),
) -> dict:
  """Returns the owner's model-picker preferences.

  Default shape is `{"hidden_ids": []}` — absent prefs and empty
  prefs are equivalent (the picker shows every registry entry).
  """
  prefs = owner.model_prefs_json or {}
  hidden = prefs.get("hidden_ids") or []
  # Defensive normalize: any persisted non-string falls out here so
  # the client never sees a malformed entry.
  return {"hidden_ids": [s for s in hidden if isinstance(s, str)]}


@owner_router.patch("/model-prefs")
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


# Compose: a single outer router so routes/__init__.py's frozen
# `_load("settings")` picks up all three surfaces.
router.include_router(settings_router)
router.include_router(models_router)
router.include_router(owner_router)
