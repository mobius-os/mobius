"""Gemini image generation route."""

import asyncio
import base64
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi import Path as FastPath
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from app import models
from app.auth import decrypt_api_key
from app.auth_helpers import TokenSource, get_auth_token_source
from app.config import get_settings
from app.database import get_db
from app.deps import (
  get_current_owner, reject_cross_site, resolve_media_or_header_owner,
)
from app.path_utils import validate_path_within_base
from app.resource_access import get_active_chat_or_404
from app.storage_io import app_dir_usage

# Per-chat total cap for generated images. Each image is ~100-500 KB
# for flash-model output; 100 MB accommodates hundreds of generations
# while bounding the blast radius on the memory-tight host.
_MAX_CHAT_MEDIA_BYTES = 100 * 1024 * 1024  # 100 MB per chat media dir

# Chat IDs are dashed UUID4 strings produced by str(uuid.uuid4()).
# Rejecting early prevents using a crafted chat_id as a filesystem path
# component to escape the chats/ subtree.
_CHAT_ID_RE = re.compile(
  r'^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$',
  re.IGNORECASE,
)


def _validate_chat_id(chat_id: str) -> None:
  """Raises 400 if chat_id doesn't look like a UUID4."""
  if not _CHAT_ID_RE.match(chat_id):
    raise HTTPException(status_code=400, detail="Invalid chat id.")

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chats", tags=["generate"])

_GEMINI_BASE = (
  "https://generativelanguage.googleapis.com/v1beta/models/"
)

# Cheapest flash model only — keeps per-image cost low (~$0.04/image).
_IMAGE_MODELS = [
  "gemini-2.5-flash-image",
]

_MAX_RETRIES = 3


_ALLOWED_ASPECT_RATIOS = {"1:1", "16:9", "9:16", "4:3", "3:4"}


class GenerateRequest(BaseModel):
  prompt: str
  aspect_ratio: str = "1:1"

  @field_validator("aspect_ratio")
  @classmethod
  def validate_aspect_ratio(cls, v: str) -> str:
    if v not in _ALLOWED_ASPECT_RATIOS:
      raise ValueError(
        f"aspect_ratio must be one of {sorted(_ALLOWED_ASPECT_RATIOS)}"
      )
    return v


# The generate endpoint uses get_auth_token from app.auth_helpers
# because the image serve endpoint must accept ?token= for <img> tags
# that cannot set Authorization headers.


async def _call_gemini(
  api_key: str, prompt: str, aspect_ratio: str,
) -> tuple[bytes, str]:
  """Calls Gemini image generation with retries on transient errors."""
  payload = {
    "contents": [{"parts": [{"text": prompt}]}],
    "generationConfig": {
      "responseModalities": ["TEXT", "IMAGE"],
      "imageConfig": {"aspectRatio": aspect_ratio},
    },
  }

  last_error = None
  async with httpx.AsyncClient() as client:
    for model in _IMAGE_MODELS:
      log.info("Trying image generation with model: %s", model)
      url = f"{_GEMINI_BASE}{model}:generateContent"
      for attempt in range(_MAX_RETRIES):
        try:
          resp = await client.post(
            url,
            json=payload,
            headers={"x-goog-api-key": api_key},
            timeout=60.0,
          )
        except httpx.TimeoutException:
          last_error = "Gemini request timed out."
          continue

        if resp.status_code == 200:
          data = resp.json()
          for part in data.get("candidates", [{}])[0] \
              .get("content", {}).get("parts", []):
            if "inlineData" in part:
              return base64.b64decode(part["inlineData"]["data"]), model
          last_error = "Gemini returned no image in response."
          break  # no point retrying if response was 200 but no image

        if resp.status_code == 429:
          body = resp.text or ""
          if "limit: 0" in body or "quota" in body.lower():
            # Budget/quota exhausted — no point retrying.
            raise HTTPException(
              status_code=402,
              detail="Gemini API quota exhausted. Check your billing.",
            )
          # Transient rate limit — wait and retry.
          wait = 2 ** attempt
          log.warning("Gemini 429 on %s, retrying in %ds", model, wait)
          last_error = "Gemini rate limit exceeded."
          await asyncio.sleep(wait)
          continue

        # Other errors — don't retry.
        last_error = resp.text[:300] if resp.text else f"HTTP {resp.status_code}"
        log.warning("Gemini %d on %s: %s", resp.status_code, model, last_error)
        break

  raise HTTPException(status_code=502, detail=f"Image generation failed: {last_error}")


@router.post(
  "/{chat_id}/generate-image", dependencies=[Depends(reject_cross_site)],
)
async def generate_image(
  body: GenerateRequest,
  chat_id: str,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Calls Gemini to generate an image and saves it under the chat dir."""
  _validate_chat_id(chat_id)
  if not owner.gemini_api_key_enc:
    raise HTTPException(
      status_code=503,
      detail="No Gemini API key configured. Add one in Settings.",
    )

  chat = get_active_chat_or_404(db, chat_id)

  api_key = decrypt_api_key(owner.gemini_api_key_enc)
  image_bytes, model_used = await _call_gemini(
    api_key, body.prompt, body.aspect_ratio,
  )

  settings = get_settings()
  # One dir holds all agent-attached chat media (generated images AND
  # screenshots). The older `generated/` path is still served for embeds in
  # pre-`media/` messages — see the legacy alias route below.
  media_dir = Path(settings.data_dir) / "chats" / chat_id / "media"
  media_dir.mkdir(parents=True, exist_ok=True)

  # Enforce per-chat directory total before writing. Each Gemini image
  # is typically a few hundred KB, so 100 MB supports many generations
  # while preventing a runaway chat from filling the host disk.
  # Count both the new media/ dir and the legacy generated/ dir (old chats may
  # still hold images there) so the per-chat cap covers all served media, not a
  # fresh allowance per directory.
  gen_dir = Path(settings.data_dir) / "chats" / chat_id / "generated"
  dir_used = app_dir_usage(media_dir) + (
    app_dir_usage(gen_dir) if gen_dir.exists() else 0
  )
  if dir_used + len(image_bytes) > _MAX_CHAT_MEDIA_BYTES:
    raise HTTPException(
      status_code=413,
      detail=(
        f"This chat's media directory is full "
        f"({_MAX_CHAT_MEDIA_BYTES // (1024 * 1024)} MB limit per chat). "
        f"The agent can delete old images to free space."
      ),
    )

  filename = f"{uuid.uuid4().hex}.png"
  (media_dir / filename).write_bytes(image_bytes)

  record = {
    "filename": filename,
    "prompt": body.prompt,
    "created_at": datetime.now(UTC).isoformat(),
  }
  chat.generated_images = list(chat.generated_images or []) + [record]
  db.commit()

  return {
    "url": f"/api/chats/{chat_id}/media/{filename}",
    "model": model_used,
  }


def _serve_chat_image(chat_id, filename, subdir, token_src, db):
  """Common auth + path-validation + FileResponse for a chat media file.

  `subdir` is "media" (current) or "generated" (legacy alias kept so embeds in
  old messages still resolve). Owner-only. The token can come from two sources:
  - Authorization header: any valid owner JWT (full-session auth).
  - ?token= query param: ONLY a short-lived media-scoped token minted by
    POST /api/chats/{id}/media-token. Owner JWTs are explicitly rejected on
    this path to prevent the 30-day token from leaking into logs/history.

  App tokens are rejected on both paths.
  """
  _validate_chat_id(chat_id)
  resolve_media_or_header_owner(
    token_src.token, db, chat_id=chat_id, from_query=token_src.from_query,
  )

  settings = get_settings()
  base = Path(settings.data_dir) / "chats" / chat_id / subdir
  file_path = validate_path_within_base(filename, base)

  if not file_path.exists():
    raise HTTPException(status_code=404, detail="Image not found.")

  return FileResponse(str(file_path), media_type="image/png")


@router.get("/{chat_id}/media/{filename}")
def serve_chat_media(
  chat_id: str,
  filename: str = FastPath(...),
  token_src: TokenSource = Depends(get_auth_token_source),
  db: Session = Depends(get_db),
):
  """Serves an agent-attached chat image — screenshots and generated images."""
  return _serve_chat_image(chat_id, filename, "media", token_src, db)


@router.get("/{chat_id}/generated/{filename}")
def serve_generated_image(
  chat_id: str,
  filename: str = FastPath(...),
  token_src: TokenSource = Depends(get_auth_token_source),
  db: Session = Depends(get_db),
):
  """Legacy alias: serves media written under the old `generated/` dir, so
  embeds in pre-`media/` messages keep rendering."""
  return _serve_chat_image(chat_id, filename, "generated", token_src, db)
