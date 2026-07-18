"""Serve generation-bound published sites and their public artifact data.

A Web Studio project publishes its built static site (build/site/) to a
snapshot under DATA_DIR/published/<token>/; this serves it at a stable,
unguessable token URL — the owner's shareable "live preview". The token is
per-project (stable across re-publishes, stored in the project's build/ dir).

Registered shares are authorized by ``published-meta`` and the live app
generation on every request.  Pre-registry snapshots remain readable only as a
compatibility exception until their next publish/revoke lifecycle event.
"""

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from slowapi import Limiter
from sqlalchemy.orm import Session

from app import fs_locks
from app.artifact_data import (
  ArtifactDataError, artifact_file_path, read_json_file,
)
from app.config import get_settings
from app.database import SessionLocal, get_db
from app.publication import (
  InvalidPublicationRegistry,
  _TOKEN_RE,
  published_root,
  read_publication_record,
  resolve_active_publication,
)

published_router = APIRouter(tags=["published"])

log = logging.getLogger("mobius.published")
_legacy_warned_tokens: set[str] = set()


def _public_token_key(request: Request) -> str:
  return request.path_params.get("token", "invalid")


_public_data_limiter = Limiter(key_func=_public_token_key)


def _published_root() -> Path:
  return published_root(get_settings())


def _not_found() -> HTTPException:
  return HTTPException(status_code=404, detail="Not found.")


def _serve(token: str, path: str, db: Session | None = None):
  if not _TOKEN_RE.fullmatch(token or ""):
    raise _not_found()
  settings = get_settings()
  try:
    record = read_publication_record(settings, token)
  except InvalidPublicationRegistry:
    raise _not_found()
  if record is not None:
    own_db = db is None
    active_db = db or SessionLocal()
    try:
      if resolve_active_publication(active_db, settings, token) is None:
        raise _not_found()
    finally:
      if own_db:
        active_db.close()
  root = _published_root()
  literal_base = root / token
  if root.is_symlink() or literal_base.is_symlink():
    raise _not_found()
  base = literal_base.resolve()
  if not base.is_dir():
    raise _not_found()
  if record is None and token not in _legacy_warned_tokens:
    _legacy_warned_tokens.add(token)
    log.warning(
      "serving legacy publication without registry entry: token=%s", token,
    )
  rel = Path(path or "index.html")
  literal = literal_base
  for part in rel.parts:
    literal = literal / part
    if literal.is_symlink():
      raise _not_found()
  target = literal.resolve()
  # A resolved path outside the exact token root never reaches SPA fallback.
  if base != target and base not in target.parents:
    raise _not_found()
  if target.is_dir():
    if target.is_symlink() or (target / "index.html").is_symlink():
      raise _not_found()
    target = target / "index.html"
  if not target.is_file() or target.is_symlink():
    idx = base / "index.html"
    if not idx.is_file() or idx.is_symlink():
      raise _not_found()
    target = idx
  resp = FileResponse(str(target))
  resp.headers["X-Content-Type-Options"] = "nosniff"
  resp.headers["Cache-Control"] = "no-cache"
  return resp


@published_router.get(
  "/api/published-sites/{token}/data/{key}",
  include_in_schema=False,
)
@_public_data_limiter.limit("60/minute")
async def read_published_artifact_data(
  token: str,
  key: str,
  request: Request,
  db: Session = Depends(get_db),
):
  """Return one JSON value through a generation-bound public capability."""
  if not _TOKEN_RE.fullmatch(token or ""):
    raise _not_found()
  settings = get_settings()
  record = resolve_active_publication(db, settings, token)
  if record is None or record.project_id is None:
    raise _not_found()
  async with fs_locks.app_storage_lock(record.app_id):
    current = resolve_active_publication(db, settings, token)
    if current is None or current.binding() != record.binding():
      raise _not_found()
    try:
      _artifact_root, file_path = artifact_file_path(
        settings, current.app_id, current.project_id, key,
      )
      value = read_json_file(file_path)
    except ArtifactDataError:
      raise _not_found()
  response = JSONResponse(value)
  response.headers["X-Content-Type-Options"] = "nosniff"
  response.headers["Cache-Control"] = "no-cache"
  return response

@published_router.get("/sites/{token}/{path:path}", include_in_schema=False)
def serve_published(
  token: str,
  path: str = "",
  db: Session = Depends(get_db),
):
  return _serve(token, path, db)


@published_router.get("/sites/{token}", include_in_schema=False)
def serve_published_root(token: str, db: Session = Depends(get_db)):
  return _serve(token, "", db)


# The routes/__init__ _load() scaffold returns `mod.router`; expose it.
router = published_router
