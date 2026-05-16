# backend/app/routes/settings.py
"""Settings API: read/write owner-level configuration."""

from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models
from app.auth import encrypt_api_key
from app.config import get_settings as get_app_settings
from app.database import get_db
from app.deps import get_current_owner

router = APIRouter(prefix="/api/settings", tags=["settings"])


class SettingsUpdate(BaseModel):
  gemini_api_key: str | None = None
  provider: str | None = None


@router.get("")
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


@router.post("")
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
  if body.provider is not None and body.provider in ("claude", "codex"):
    owner.provider = body.provider
  db.commit()
  return {"ok": True}
