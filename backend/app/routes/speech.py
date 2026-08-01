"""Authenticated streaming speech for owner-installed mini-apps."""

from __future__ import annotations

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import models
from app.deps import Principal, get_db, get_principal
from app.routes.apps import live_app_or_404
from app.speech_runtime import (
  SUPPORTED_LANGUAGES,
  SpeechRuntimeError,
  ensure_runtime,
  invalidate_runtime,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/apps", tags=["speech"])

_VOICE_FOR_LANGUAGE = {
  "english": "alba",
  "french_24l": "estelle",
  "german_24l": "juergen",
  "spanish_24l": "lola",
  "portuguese_24l": "rafael",
  "italian_24l": "giovanni",
}
_SPEECH_LOCK = asyncio.Lock()


class SpeechRequest(BaseModel):
  text: str = Field(min_length=1, max_length=50_000)
  language: str = Field(default="english", max_length=32)


def _authorize_app(db: Session, principal: Principal, app_id: int) -> models.App:
  app = live_app_or_404(db, app_id)
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(
      status_code=403,
      detail="An app token may only create speech for its own app.",
    )
  return app


@router.post("/{app_id}/speech")
async def stream_speech(
  app_id: int,
  body: SpeechRequest,
  principal: Principal = Depends(get_principal),
  db: Session = Depends(get_db),
):
  """Stream Kyutai Pocket TTS WAV bytes without storing an audio artifact."""
  _authorize_app(db, principal, app_id)
  text = " ".join(body.text.split())
  if not text:
    raise HTTPException(status_code=400, detail="Speech text cannot be empty.")
  if body.language not in SUPPORTED_LANGUAGES:
    raise HTTPException(status_code=400, detail="Unsupported speech language.")

  await _SPEECH_LOCK.acquire()
  client: httpx.AsyncClient | None = None
  upstream: httpx.Response | None = None
  try:
    origin = await asyncio.to_thread(ensure_runtime, body.language)
    client = httpx.AsyncClient(
      timeout=httpx.Timeout(None, connect=10.0),
      follow_redirects=False,
    )
    request = client.build_request(
      "POST",
      f"{origin}/tts",
      data={
        "text": text,
        "voice_url": _VOICE_FOR_LANGUAGE[body.language],
      },
    )
    upstream = await client.send(request, stream=True)
    if upstream.status_code != 200:
      detail = (await upstream.aread())[:500].decode("utf-8", errors="replace")
      await upstream.aclose()
      await client.aclose()
      _SPEECH_LOCK.release()
      raise HTTPException(
        status_code=502,
        detail=detail or "Pocket TTS could not create speech.",
      )
  except SpeechRuntimeError as exc:
    if client is not None:
      await client.aclose()
    _SPEECH_LOCK.release()
    raise HTTPException(status_code=503, detail=str(exc)) from exc
  except HTTPException:
    raise
  except Exception as exc:
    if upstream is not None:
      await upstream.aclose()
    if client is not None:
      await client.aclose()
    _SPEECH_LOCK.release()
    log.warning("Speech upstream failed before streaming: %s", exc)
    raise HTTPException(status_code=502, detail="Speech is unavailable right now.") from exc

  async def body_stream():
    completed = False
    try:
      async for chunk in upstream.aiter_raw():
        yield chunk
      completed = True
    except asyncio.CancelledError:
      raise
    finally:
      await upstream.aclose()
      await client.aclose()
      if _SPEECH_LOCK.locked():
        _SPEECH_LOCK.release()
      # Kyutai's official server generates in a worker thread. If the phone
      # disconnects, stop that isolated process so an abandoned long report
      # cannot keep consuming CPU or overlap the next request.
      if not completed:
        await asyncio.to_thread(invalidate_runtime)

  return StreamingResponse(
    body_stream(),
    media_type="audio/wav",
    headers={
      "Cache-Control": "no-store",
      "Content-Disposition": "inline; filename=digest.wav",
    },
  )
