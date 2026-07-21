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
  ArtifactDataError,
  artifact_dir_path,
  artifact_file_path,
  list_artifact_keys,
  read_json_file,
  validate_artifact_key,
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


# Keyed per published site, scoped by VIEW rather than URL. The scope matters:
# slowapi's default key_style="url" folds the request path into the bucket, so
# a caller could mint a fresh 60/minute budget for every {key} it invented,
# which made the limit unbounded in practice.
#
# The key stays the TOKEN rather than the client address on purpose. Public
# traffic reaches this app through the proxy, and main.py deliberately refuses
# to trust X-Forwarded-For for rate limiting (any client could spoof it), so
# get_remote_address resolves to the proxy peer for EVERY public request —
# a "per-client" limit would collapse into one global bucket that a single
# visitor could use to starve every published site. Per-token keeps one site's
# traffic from affecting another; the residual is that heavy traffic to one
# site can throttle that same site, which is the blast radius we want.
_public_data_limiter = Limiter(
  key_func=_public_token_key, key_style="endpoint",
)
# ONE budget for the whole public artifact-data capability. `limit()` scopes per
# HANDLER under key_style="endpoint", so listing and per-key reads would each get
# their own 60/minute; `shared_limit` pins both to one explicit scope instead.
_PUBLIC_DATA_SCOPE = "published-artifact-data"
_public_data_limit = _public_data_limiter.shared_limit(
  "60/minute", scope=_PUBLIC_DATA_SCOPE,
)


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


async def _published_artifact_json(token: str, db: Session, operation):
  """Run ``operation(settings, record)`` for a live published artifact.

  Both public artifact-data reads share this exact envelope: reject a malformed
  token, resolve the ACTIVE generation-bound record (project-scoped), take the
  bound app's storage lock, re-resolve under the lock so a revoke/delete/wipe
  that landed in between wins the race, then run the one filesystem operation
  and stamp the same no-sniff/no-cache headers. Any ArtifactDataError inside the
  lock collapses to a uniform 404 so nothing about the stored layout leaks.
  """
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
      payload = operation(settings, current)
    except ArtifactDataError:
      raise _not_found()
  response = JSONResponse(payload)
  response.headers["X-Content-Type-Options"] = "nosniff"
  response.headers["Cache-Control"] = "no-cache"
  return response


@published_router.get(
  "/api/published-sites/{token}/data",
  include_in_schema=False,
)
@_public_data_limit
async def list_published_artifact_data(
  token: str,
  request: Request,
  db: Session = Depends(get_db),
):
  """List the published artifact's keys through the same public capability.

  Enumeration is server-derived from the directory so a published page never
  depends on a client-maintained index that concurrent writers could desync.
  """
  return await _published_artifact_json(token, db, lambda settings, record: {
    "keys": list_artifact_keys(
      artifact_dir_path(settings, record.app_id, record.project_id),
    ),
  })


@published_router.get(
  "/api/published-sites/{token}/data/{key}",
  include_in_schema=False,
)
@_public_data_limit
async def read_published_artifact_data(
  token: str,
  key: str,
  request: Request,
  db: Session = Depends(get_db),
):
  """Return one JSON value through a generation-bound public capability."""
  # Reject a malformed key before any DB/filesystem work; the token is checked
  # inside the shared envelope. This route is unauthenticated, so cheap rejects
  # stay off the hot path.
  if not validate_artifact_key(key):
    raise _not_found()

  def _read(settings, record):
    _artifact_root, file_path = artifact_file_path(
      settings, record.app_id, record.project_id, key,
    )
    return read_json_file(file_path)

  return await _published_artifact_json(token, db, _read)

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
