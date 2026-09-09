"""One anonymous storage capability for every public Möbius surface.

The bearer decides the namespace. A hosted mini-app token grants the reviewed
``public/`` scope in that app's ordinary storage tree; a published-site token
grants read-only access to the exact generation-bound Project artifact data.
Compatibility routes in ``public_apps`` and ``published`` call the same
operations so old shared pages keep working without maintaining a second
storage implementation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request, Response
from slowapi import Limiter

from app import fs_locks, storage_io
from app.artifact_data import MAX_ARTIFACT_READ_BYTES
from app.config import get_settings
from app.database import SessionLocal
from app.deps import resolve_public_app_token
from app.publication import _TOKEN_RE, resolve_active_publication
# The owner lane owns path safety, body decoding, listing and the CAS write
# core; the anonymous lane reuses them rather than re-deriving any of them.
from app.routes import storage as storage_routes


router = APIRouter(prefix="/api/public-storage", tags=["public-storage"])

PUBLIC_STORAGE_READ_ROOT = "public"
PUBLIC_WRITE_MAX_VALUE_BYTES = 64 * 1024
PUBLIC_WRITE_SUBTREE_MAX_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True)
class PublicStorageGrant:
  kind: str
  app_id: int
  root: str
  readable: bool
  read_root: str
  write_prefix: str | None
  binding: tuple


def _bearer(authorization: str | None) -> str:
  scheme, _, token = (authorization or "").partition(" ")
  if scheme.lower() != "bearer" or not token:
    raise HTTPException(401, "Valid public storage token required.")
  return token


def _request_key(request: Request) -> str:
  """One bounded bucket per bearer without retaining the bearer in the key."""
  authorization = request.headers.get("authorization", "")
  return hashlib.sha256(authorization.encode("utf-8")).hexdigest()[:24]


def _not_published_site(request: Request) -> bool:
  authorization = request.headers.get("authorization", "")
  scheme, _, token = authorization.partition(" ")
  return scheme.lower() != "bearer" or not _TOKEN_RE.fullmatch(token or "")


_limiter = Limiter(key_func=_request_key, key_style="endpoint")
_read_limit = _limiter.shared_limit("600/minute", scope="public-storage-read")
_published_read_limit = _limiter.shared_limit(
  "60/minute",
  scope="published-public-storage-read",
  exempt_when=_not_published_site,
)
_write_limit = _limiter.shared_limit("120/minute", scope="public-storage-write")
# Legacy hosted-app URLs share these exact buckets so compatibility cannot be
# used to multiply a token's anonymous storage budget.
legacy_public_app_read_limit = _read_limit
legacy_public_app_write_limit = _write_limit


def _resolve_grant(token: str, *, expected_app_id: int | None = None) -> PublicStorageGrant:
  """Resolve either public bearer into one exact app-storage namespace."""
  db = SessionLocal()
  try:
    if _TOKEN_RE.fullmatch(token or ""):
      record = resolve_active_publication(db, get_settings(), token)
      if record is None or record.project_id is None:
        raise HTTPException(401, "Valid public storage token required.")
      if expected_app_id is not None and record.app_id != expected_app_id:
        raise HTTPException(401, "Valid public storage token required.")
      return PublicStorageGrant(
        kind="published_site",
        app_id=record.app_id,
        root=f"artifact-data/{record.project_id}",
        readable=True,
        read_root="",
        write_prefix=None,
        binding=record.binding(),
      )

    access = resolve_public_app_token(token, db, expected_app_id=expected_app_id)
    return PublicStorageGrant(
      kind="public_app",
      app_id=access.app_id,
      root="",
      readable=access.storage["read"],
      read_root=PUBLIC_STORAGE_READ_ROOT,
      write_prefix=access.storage["write_prefix"],
      binding=(access.app_id, access.app.public_token_nonce),
    )
  finally:
    db.close()


def _same_live_grant(token: str, grant: PublicStorageGrant) -> PublicStorageGrant:
  """Re-resolve under the app storage lock: the anonymous lane's uninstall /
  freed-id-reuse guard, matching the owner lane's `_recheck_app_identity`."""
  current = _resolve_grant(token, expected_app_id=grant.app_id)
  if current.kind != grant.kind or current.binding != grant.binding:
    raise HTTPException(401, "Public storage session is no longer valid.")
  return current


def _within(path: str, prefix: str) -> bool:
  if not prefix:
    return True
  return path == prefix or path.startswith(prefix + "/")


def _actual_path(grant: PublicStorageGrant, path: str) -> str:
  clean = str(path or "")
  return f"{grant.root}/{clean}" if grant.root and clean else (grant.root or clean)


def _relative_listing_path(grant: PublicStorageGrant, path: str) -> str:
  if not grant.root:
    return path
  prefix = grant.root + "/"
  return path[len(prefix):] if path.startswith(prefix) else ""


def _assert_read(grant: PublicStorageGrant, path: str) -> None:
  if not grant.readable:
    raise HTTPException(403, "This app does not expose public storage reads.")
  if not _within(path, grant.read_root):
    raise HTTPException(403, "Public reads are limited to the app's public/ folder.")


def _assert_write(grant: PublicStorageGrant, path: str) -> str:
  prefix = grant.write_prefix
  if not prefix:
    raise HTTPException(403, "This public surface does not accept writes.")
  normalized = prefix.rstrip("/")
  if not _within(path, normalized):
    raise HTTPException(403, "Path is outside this app's public write area.")
  return normalized


async def read_public_value(
  token: str,
  path: str,
  *,
  expected_app_id: int | None = None,
):
  grant = _resolve_grant(token, expected_app_id=expected_app_id)
  relative = str(path or "")
  _assert_read(grant, relative)
  settings = get_settings()
  base = Path(settings.data_dir) / "apps" / str(grant.app_id)
  async with fs_locks.app_storage_lock(grant.app_id):
    grant = _same_live_grant(token, grant)
    actual = _actual_path(grant, relative)
    file_path = storage_routes._resolve(base, actual)
    if file_path.is_dir():
      raise HTTPException(400, "Path is a directory.")
    if not file_path.is_file():
      raise HTTPException(404, "Not found.")
    if (
      grant.kind == "published_site"
      and file_path.stat().st_size > MAX_ARTIFACT_READ_BYTES
    ):
      raise HTTPException(404, "Not found.")
    stored_mime = storage_io.read_content_type(
      settings.data_dir, Path("apps") / str(grant.app_id), actual,
    )
    version = storage_io.file_version_token(file_path)
    response = storage_routes._serve_file(file_path, stored_mime)
  response.headers["ETag"] = version
  response.headers["Cache-Control"] = "no-store"
  response.headers["X-Content-Type-Options"] = "nosniff"
  return response


async def list_public_values(
  token: str,
  prefix: str = "",
  *,
  include_content: bool = False,
  limit: int = 100,
  cursor: str | None = None,
  expected_app_id: int | None = None,
):
  grant = _resolve_grant(token, expected_app_id=expected_app_id)
  relative = str(prefix or "")
  _assert_read(grant, relative)
  settings = get_settings()
  base = Path(settings.data_dir) / "apps" / str(grant.app_id)
  async with fs_locks.app_storage_lock(grant.app_id):
    grant = _same_live_grant(token, grant)
    actual = _actual_path(grant, relative)
    directory = storage_routes._resolve(base, actual)
    if not directory.is_dir():
      return {"entries": [], "next_cursor": None}
    scope = Path("apps") / str(grant.app_id)
    entries, next_cursor = storage_routes._list_directory_page(
      directory,
      actual,
      limit,
      cursor,
      mime_override=lambda rel: storage_io.read_content_type(
        settings.data_dir, scope, rel,
      ),
    )
    if include_content:
      storage_routes._include_json_listing_content(entries, base)
    for entry in entries:
      entry["path"] = _relative_listing_path(grant, entry.get("path", ""))
  return {"entries": entries, "next_cursor": next_cursor}


async def write_public_value(
  token: str,
  path: str,
  request: Request,
  *,
  expected_app_id: int | None = None,
):
  grant = _resolve_grant(token, expected_app_id=expected_app_id)
  relative = str(path or "")
  write_root = _assert_write(grant, relative)
  settings = get_settings()
  base = Path(settings.data_dir) / "apps" / str(grant.app_id)
  file_path = storage_routes._resolve(base, _actual_path(grant, relative))
  content, stored_mime = await storage_routes._decode_write_body(request, file_path)
  new_size = len(content.encode("utf-8") if isinstance(content, str) else content)
  if new_size > PUBLIC_WRITE_MAX_VALUE_BYTES:
    raise HTTPException(
      413, f"Public writes are limited to {PUBLIC_WRITE_MAX_VALUE_BYTES} bytes per value.",
    )
  if_match = request.headers.get("if-match")
  if_none_match = request.headers.get("if-none-match")
  async with fs_locks.app_storage_lock(grant.app_id):
    grant = _same_live_grant(token, grant)
    _assert_write(grant, relative)
    actual = _actual_path(grant, relative)
    file_path = storage_routes._resolve(base, actual)
    before_size = storage_routes.check_write_precondition(
      file_path, if_match, if_none_match,
    )
    subtree = base / _actual_path(grant, write_root)
    subtree_before = storage_io.app_dir_usage(subtree) if subtree.is_dir() else 0
    if subtree_before - before_size + new_size > PUBLIC_WRITE_SUBTREE_MAX_BYTES:
      raise HTTPException(413, "This app's public submission area is full.")
    version = storage_routes.commit_write(
      settings.data_dir, grant.app_id, actual, file_path, content, stored_mime,
      before_size=before_size,
    )
  response = Response(status_code=204)
  response.headers["ETag"] = version
  return response


async def delete_public_value(
  token: str,
  path: str,
  *,
  expected_app_id: int | None = None,
):
  grant = _resolve_grant(token, expected_app_id=expected_app_id)
  relative = str(path or "")
  _assert_write(grant, relative)
  settings = get_settings()
  base = Path(settings.data_dir) / "apps" / str(grant.app_id)
  async with fs_locks.app_storage_lock(grant.app_id):
    grant = _same_live_grant(token, grant)
    _assert_write(grant, relative)
    actual = _actual_path(grant, relative)
    file_path = storage_routes._resolve(base, actual)
    storage_routes.remove_file(settings.data_dir, grant.app_id, actual, file_path)
  return Response(status_code=204)


@router.get("")
@_read_limit
@_published_read_limit
async def public_storage_list(
  request: Request,
  prefix: str = "",
  include_content: bool = False,
  limit: int = 100,
  cursor: str | None = None,
  authorization: str | None = Header(default=None),
):
  return await list_public_values(
    _bearer(authorization), prefix,
    include_content=include_content, limit=limit, cursor=cursor,
  )


@router.get("/{path:path}")
@_read_limit
@_published_read_limit
async def public_storage_read(
  path: str,
  request: Request,
  authorization: str | None = Header(default=None),
):
  return await read_public_value(_bearer(authorization), path)


@router.put("/{path:path}", status_code=204)
@_write_limit
async def public_storage_write(
  path: str,
  request: Request,
  authorization: str | None = Header(default=None),
):
  return await write_public_value(_bearer(authorization), path, request)


@router.delete("/{path:path}", status_code=204)
@_write_limit
async def public_storage_delete(
  path: str,
  request: Request,
  authorization: str | None = Header(default=None),
):
  return await delete_public_value(_bearer(authorization), path)
