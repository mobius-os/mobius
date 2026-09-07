"""Minimal ASGI entrypoint for the isolated Common public host."""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.common_protocol import ActorVerifier
from app.common_public import CommonPublicStore, create_public_router

SERVICE_NAME = "mobius-social"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _baked_source_sha() -> str:
  """Read the build-owned file beside this module; never trust runtime env."""
  value = Path(__file__).with_name("SOCIAL_SOURCE_SHA").read_text(
    encoding="ascii"
  ).strip()
  if not _SHA_RE.fullmatch(value):
    raise RuntimeError("SOCIAL_SOURCE_SHA is not a baked 40-character Git SHA")
  return value


SOURCE_SHA = _baked_source_sha()


def create_app(data_dir: str | Path | None = None) -> FastAPI:
  """Create an owner-independent public-host application.

  ``data_dir`` is an explicit factory seam for hermetic tests.  Production
  calls this with no argument and reads only ``SOCIAL_DATA_DIR`` (default
  ``/data``); build provenance has no corresponding runtime override.
  """
  configured = Path(
    data_dir if data_dir is not None else os.environ.get("SOCIAL_DATA_DIR", "/data")
  )
  if not configured.is_absolute():
    raise RuntimeError("SOCIAL_DATA_DIR must be an absolute path")
  store = CommonPublicStore(configured)
  verifier = ActorVerifier(configured)
  public_router, write_limiter = create_public_router(store, verifier)

  @asynccontextmanager
  async def lifespan(application: FastAPI):
    store.initialize()
    application.state.initialized = True
    try:
      yield
    finally:
      application.state.initialized = False

  application = FastAPI(
    title="Möbius Social public host",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
  )
  application.state.initialized = False
  application.state.social_store = store
  application.state.actor_verifier = verifier
  application.state.limiter = write_limiter
  application.add_exception_handler(
    RateLimitExceeded, _rate_limit_exceeded_handler
  )

  @application.get("/healthz")
  def healthz():
    if not application.state.initialized:
      raise HTTPException(status_code=503, detail="Service is initializing.")
    return {"status": "ok"}

  @application.get("/version")
  def version():
    return {"service": SERVICE_NAME, "source_sha": SOURCE_SHA}

  application.include_router(public_router)
  return application


app = create_app()
