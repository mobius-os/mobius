"""Routes for per-app and shared file storage.

PUT body shapes:

  1. JSON inner object for `.json` files (`Content-Type:
     application/json`). Body IS the data:
       PUT /api/storage/apps/7/notes.json
       {"title": "hi", "items": [1, 2, 3]}
     Server stringifies and writes `{"title":"hi","items":[1,2,3]}`.

  2. JSON envelope for non-JSON files (`Content-Type:
     application/json`):
       PUT /api/storage/apps/7/notes.txt
       {"content": "plain text body"}
     Server writes the inner string as-is.

  3. Raw text for non-JSON files (`Content-Type: text/*`):
       PUT /api/storage/apps/7/notes.txt
       plain text body
     Server decodes the request body as UTF-8 and writes it directly.

  4. Raw bytes for any path (`Content-Type: application/octet-stream`,
     or any other non-JSON, non-text MIME type):
       PUT /api/storage/apps/7/blob.bin
       <bytes>
     Server writes the bytes directly.

For JSON requests, a body that is exactly `{"content": "<str>"}` is
treated as the envelope; anything else is the inner object and is only
accepted for `.json` paths.
"""

import base64
import datetime
import heapq
import json
import logging
import mimetypes
import os
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import activity, fs_locks, models
from app.config import get_settings
from app.database import get_db
from app import storage_io
from app.storage_io import (
  app_dir_usage,
  atomic_write,
  delete_content_type,
  delete_content_type_tree,
  move_content_type,
  read_capped_body,
  read_content_type,
  write_content_type,
)
from app.deps import (
  Principal,
  get_current_owner,
  get_current_owner_or_app,
  get_principal,
  reject_cross_site,
)
from app.path_utils import validate_path_within_base

router = APIRouter(prefix="/api/storage", tags=["storage"])

_log = logging.getLogger(__name__)
_SAFE_RE = re.compile(r"^[\w.\-\/]+$")


_LEVELS = {"none": 0, "read": 1, "write": 2}


def _check_cross_app(
  db: Session, principal: Principal, target_app_id: int, mode: str
) -> models.App:
  """Enforces declared cross-app access on /api/storage/apps/{id}/...

  Owner tokens always pass. App tokens accessing their OWN app always
  pass. App tokens accessing a DIFFERENT app pass only when BOTH:
    - the caller's `cross_app_access` permits the mode, AND
    - the target's `share_with_apps` permits the mode.

  Subject-side is the primary check (threat model: "one app is
  compromised; what stops it from ransacking the others"); object-
  side is defense-in-depth.

  mode: 'read' or 'write'.
  """
  # The target app must EXIST for any storage access — owner, own-app, or
  # cross-app. Without this, a token (or an owner) addressing a deleted or
  # never-created app id could read, recreate, list, or delete an orphan
  # /data/apps/<id> storage tree. Load it once up front so
  # every branch below can assume the row is real.
  target = (
    db.query(models.App).filter(models.App.id == target_app_id).first()
  )
  if not target:
    raise HTTPException(status_code=404, detail="App not found.")
  if principal.app_id is None:
    return target  # owner token
  if principal.app_id == target_app_id:
    return target  # app accessing its own data
  need = _LEVELS[mode]
  caller = (
    db.query(models.App).filter(models.App.id == principal.app_id).first()
  )
  caller_level = _LEVELS.get(
    (caller.cross_app_access or "none").lower() if caller else "none", 0
  )
  if caller_level < need:
    raise HTTPException(
      status_code=403,
      detail=(
        f"This app's cross_app_access is "
        f"'{(caller.cross_app_access if caller else 'none')}' — "
        f"insufficient for {mode}."
      ),
    )
  target_level = _LEVELS.get(
    (target.share_with_apps or "none").lower(), 0
  )
  if target_level < need:
    raise HTTPException(
      status_code=403,
      detail=(
        f"App {target_app_id} share_with_apps is "
        f"'{target.share_with_apps}' — insufficient for {mode}."
      ),
    )
  return target


def _recheck_app_identity(db: Session, app_id: int, expected_nonce) -> None:
  """Re-verify, UNDER the per-app lock, that app_id is still the SAME app it
  was at authorization time.

  A plain existence check isn't enough: SQLite reuses a freed integer id, so
  between a slow PUT/DELETE's authorization and its locked filesystem mutation,
  the app can be uninstalled and a DIFFERENT app can reuse the id — the old
  request would then write/delete inside the replacement's storage tree. The
  per-app `token_nonce` rotates with the row, so a mismatch (or a missing row)
  means the original app is gone and we must not touch the tree (Codex review
  round-9 #1). `populate_existing()` forces a fresh DB read past the session's
  identity map so a concurrent uninstall's committed delete/recreate is seen.
  """
  row = (
    db.query(models.App)
    .populate_existing()
    .filter(models.App.id == app_id)
    .first()
  )
  if row is None or row.token_nonce != expected_nonce:
    raise HTTPException(status_code=404, detail="App not found.")


def _resolve(base: Path, rel: str) -> Path:
  """Returns a path within base, raising 400 on traversal attempts.

  Layers a strict character-set whitelist over the shared
  validate_path_within_base check. The whitelist rejects spaces,
  quotes, control bytes, and other shell-metacharacters before the
  resolution step ever sees them, which keeps mini-app storage paths
  to the same shape (`[\\w.\\-/]+`) the file watcher and slug logic
  elsewhere already assume.
  """
  if not _SAFE_RE.match(rel):
    raise HTTPException(status_code=400, detail="Invalid path.")
  if ".." in Path(rel).parts:
    raise HTTPException(status_code=400, detail="Path traversal not allowed.")
  resolved = validate_path_within_base(rel, base)
  # Reject a symlink ANYWHERE in the path. validate_path_within_base resolves
  # symlinks before its containment check, so an in-tree symlink (target also
  # under base) passes containment — yet listings already omit symlinks
  # (_list_entry), so letting read/PUT/DELETE follow one is an inconsistent,
  # surprising policy (a DELETE through a link removes the TARGET, not the
  # link). Walk the literal, unresolved path and reject any existing symlink
  # component so the resolve-based routes match the listing's no-symlink
  # contract. is_symlink() is lstat-based (never follows)
  # and is False for not-yet-created components, so a write that creates new
  # dirs/files is unaffected.
  walk = base
  for part in Path(rel).parts:
    walk = walk / part
    if walk.is_symlink():
      raise HTTPException(
        status_code=400, detail="Symlinks are not allowed in storage paths."
      )
  return resolved


# Text MIME types that are safe to read as UTF-8.
_TEXT_PREFIXES = ("text/", "application/json", "application/xml")

# Text/JSON files at or below this size are read into memory and served inline
# (PlainTextResponse) for low latency; larger ones (and all binaries) stream
# from disk via FileResponse so a big read never buffers whole.
_INLINE_READ_MAX = 256 * 1024

# Listing page size: the default when the caller omits `?limit`, and
# the hard cap so a single request can't be made to walk an unbounded
# directory in one shot.
_LIST_DEFAULT_LIMIT = 100
_LIST_MAX_LIMIT = 500


def _is_listable_dirent(entry: os.DirEntry) -> bool:
  """Whether a directory entry may appear in a listing.

  Mirrors `_list_entry`'s skip rules WITHOUT a stat (the DirEntry's cached
  type answers `is_symlink`; the name answers the whitelist), so the
  paginator can exclude symlinks and unsafe names DURING the scan instead
  of after selecting them — see `_list_directory_page`.
  """
  try:
    if entry.is_symlink():
      return False
  except OSError:
    return False
  return bool(_SAFE_RE.match(entry.name))


def _list_entry(child: Path, prefix: str, mime_override=None) -> dict | None:
  """Builds one listing entry for an immediate child of a directory.

  `prefix` is the request's prefix (relative to the app dir), used to
  compose each entry's `path`. Directories report no size of their own
  (the byte count is the file's; a directory's is meaningless here) and
  no mime_type; files carry both. `modified_at` is ISO-8601 UTC with a
  trailing `Z`, derived from the child's mtime.

  `mime_override`, when given, is called with the entry's scope-relative
  path and returns the stored sidecar MIME (or None). A stored type wins
  over the filename guess so a listing reports the same type a read would
  serve — chiefly for extensionless or custom-MIME blobs.

  Returns None for any child the listing must not surface, so the caller
  filters it out: a symlink (following it with stat() could leak the
  mtime/size of a target outside the storage tree, and a read of that
  path is rejected anyway), a name the read/PUT whitelist rejects (so
  every listed entry round-trips back through get()/put()), or one that
  can't be stat'd (e.g. a dangling link). One bad dirent never 500s the
  whole listing.
  """
  if child.is_symlink() or not _SAFE_RE.match(child.name):
    return None
  try:
    stat = child.stat()
  except OSError:
    return None
  modified = datetime.datetime.fromtimestamp(
    stat.st_mtime, tz=datetime.timezone.utc
  )
  # Compose the entry's path from the request prefix so the caller can
  # round-trip it straight back into a storage read without
  # reconstructing the join itself.
  rel = f"{prefix.rstrip('/')}/{child.name}" if prefix else child.name
  is_dir = child.is_dir()
  entry = {
    "name": child.name,
    "path": rel,
    "type": "directory" if is_dir else "file",
    "size": 0 if is_dir else stat.st_size,
    "modified_at": modified.isoformat().replace("+00:00", "Z"),
  }
  if not is_dir:
    stored = mime_override(rel) if mime_override else None
    entry["mime_type"] = stored or mimetypes.guess_type(child.name)[0]
  return entry


def _decode_cursor(cursor: str | None) -> str | None:
  """Decodes the opaque pagination cursor back to the last-seen name.

  The cursor is just the base64 of the last entry's `name` from the
  previous page; an unparseable cursor is treated as no cursor (start
  from the top) rather than an error, so a stale or hand-edited cursor
  degrades to a fresh listing instead of a 400.
  """
  if not cursor:
    return None
  try:
    return base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
  except Exception:
    return None


def _encode_cursor(name: str) -> str:
  """Encodes a last-seen name into the opaque next-page cursor."""
  return base64.urlsafe_b64encode(name.encode("utf-8")).decode("ascii")


def _list_directory_page(
  dir_path: Path, prefix: str, limit: int, cursor: str | None,
  mime_override=None,
) -> tuple[list[dict], str | None]:
  """Returns one keyset page `(entries, next_cursor)` of a directory.

  Shared by the app and shared listing routes so both enforce the SAME
  contract: symlinks and names that can't round-trip
  through `_resolve` are excluded (a listing never advertises a child a
  read/PUT would reject), one un-stat-able dirent drops out instead of
  500-ing the page, and pagination is identical.

  Selection uses a bounded heap: the smallest `limit`+1 VALID child names
  strictly greater than the decoded cursor, walked in O(n log limit) time
  and O(limit) memory — it never materializes or sorts the whole directory
  per page, so a large directory can't be turned into a repeated expensive
  full scan. Validity (not a symlink, name passes the
  whitelist) is checked DURING the scan, so excluded entries don't consume
  page slots — a page returns `limit` real entries and the cursor advances
  past the skipped ones, rather than selecting raw dirents and filtering
  after (which produced short/empty pages while valid entries remained
  further along — Codex review #5). `limit` is clamped to `_LIST_MAX_LIMIT`.
  """
  limit = max(1, min(limit, _LIST_MAX_LIMIT))
  after = _decode_cursor(cursor)
  try:
    scan = os.scandir(dir_path)
  except OSError:
    return [], None
  with scan:
    # Filter to listable entries (cheap: dirent type cache + name regex, no
    # stat) BEFORE selection, so symlinks/unsafe names never occupy a page
    # slot. nsmallest then keeps only limit+1 in a heap → O(limit) memory
    # even for a directory of millions; the +1 tells us a next page exists
    # without a second pass.
    candidates = (
      Path(e.path) for e in scan
      if (after is None or e.name > after) and _is_listable_dirent(e)
    )
    page_plus = heapq.nsmallest(limit + 1, candidates, key=lambda c: c.name)
  has_more = len(page_plus) > limit
  page = page_plus[:limit]
  # _list_entry still runs (it does the stat for size/mtime and re-checks the
  # same predicates); a dirent that vanished between scan and stat drops out.
  entries = [
    e for e in (_list_entry(c, prefix, mime_override) for c in page) if e
  ]
  next_cursor = _encode_cursor(page[-1].name) if has_more and page else None
  return entries, next_cursor


def _serve_file(file_path: Path, stored_mime: str | None = None):
  """Serve a file with the right content type, STREAMING large reads.

  Binary types and any file past the inline threshold are served via
  FileResponse, which streams from disk (no in-process buffer). Only small
  text/JSON is read into memory (PlainTextResponse) so the common small-doc
  read stays inline + low-latency. This bounds the per-read memory to the
  threshold rather than the full file size — a 50 MB doc no longer buffers
  whole on the memory-tight host.

  `stored_mime` (from the MIME sidecar) OVERRIDES the filename guess when
  present — that's how a cold read of an extensionless or custom-MIME blob
  serves the type the app declared instead of `text/plain`. The text/binary
  branch decision uses the effective type so a sidecar marking a file as,
  say, `image/png` streams it as binary even if the name looks text-y.
  """
  mime = stored_mime or mimetypes.guess_type(file_path.name)[0]
  is_text = mime is None or mime.startswith(tuple(_TEXT_PREFIXES))
  try:
    too_big = file_path.stat().st_size > _INLINE_READ_MAX
  except OSError:
    raise HTTPException(status_code=404, detail="File not found.")
  if not is_text or too_big:
    return FileResponse(
      file_path, media_type=(mime or "application/octet-stream")
    )
  try:
    text = file_path.read_text(encoding="utf-8")
  except UnicodeDecodeError:
    return FileResponse(
      file_path, media_type=(mime or "application/octet-stream")
    )
  return PlainTextResponse(text, media_type=(mime or "text/plain"))


def _is_envelope(body) -> bool:
  """True iff body is the legacy `{"content": "<string>"}` shape."""
  return (
    isinstance(body, dict)
    and set(body.keys()) == {"content"}
    and isinstance(body["content"], str)
  )


async def _decode_write_body(
  request: Request, file_path: Path
) -> tuple[str | bytes, str | None]:
  """Decodes a PUT body into `(content, stored_mime)`.

  `content` is the text or bytes to write; `stored_mime` is the
  Content-Type to record in the MIME sidecar, or None to record none
  (clearing any prior sidecar). Only a raw-bytes write carries a
  stored_mime — that's the case where the served type must come from the
  app's declared `setBlob` Content-Type rather than the filename guess
  (an extensionless or custom-MIME blob). Text and JSON writes leave it
  None: their type is recovered from the extension (`text/plain`,
  `application/json`), so a sidecar would only add a stale override.

  Accepts JSON inner objects for `.json` files, the legacy JSON
  envelope for text files, raw text for non-JSON files, and raw bytes
  for any path.
  """
  raw = await read_capped_body(request)
  content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
  is_json_path = file_path.suffix.lower() == ".json"

  if content_type != "application/json":
    if content_type.startswith("text/"):
      if is_json_path:
        raise HTTPException(
          status_code=415,
          detail="Raw text writes are only accepted for non-JSON paths.",
        )
      try:
        return raw.decode("utf-8"), None
      except UnicodeDecodeError:
        raise HTTPException(
          status_code=400,
          detail="Text storage writes must be valid UTF-8.",
        )
    # Raw bytes: keep the declared MIME so a cold read serves the app's
    # type. A blank content-type records nothing (read falls back to the
    # extension guess).
    return raw, (content_type or None)

  try:
    body = json.loads(raw)
  except json.JSONDecodeError:
    raise HTTPException(status_code=400, detail="Invalid JSON body.")

  # For .json paths the body IS the document — never sniff for the
  # legacy envelope. Otherwise a mini-app that legitimately stores
  # `{"content": "..."}` (single-field forms, markdown notes, etc.)
  # gets silently unwrapped: the file ends up containing the raw
  # string instead of the JSON object, and the next read returns a
  # string where the app expected a dict. The envelope shape is only
  # meaningful when the path is non-JSON (the envelope was added so
  # text files could be PUT via a JSON body, not the other way).
  if is_json_path:
    return json.dumps(body, ensure_ascii=False), None

  if _is_envelope(body):
    return body["content"], None

  raise HTTPException(
    status_code=400,
    detail=(
      "Non-JSON paths require an envelope body: "
      "{\"content\": \"<text>\"}."
    ),
  )


@router.get("/apps/{app_id}/{path:path}")
def read_app_file(
  app_id: int,
  path: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Returns a file from an app's data directory."""
  _check_cross_app(db, principal, app_id, mode="read")
  data_dir = get_settings().data_dir
  base = Path(data_dir) / "apps" / str(app_id)
  file_path = _resolve(base, path)
  # is_file() (not exists()) so a directory path 404s cleanly instead of
  # reaching _serve_file, which would try to read a directory and 500
  #.
  if not file_path.is_file():
    raise HTTPException(status_code=404, detail="File not found.")
  stored = read_content_type(data_dir, Path("apps") / str(app_id), path)
  return _serve_file(file_path, stored)


class _MoveBody(BaseModel):
  """Source + destination relative paths for a storage move/rename."""

  # `from` is a Python keyword, so the field is `from_path` with the wire
  # alias `from` (the FileExplorer sends `{from, to}`).
  from_path: str
  to: str

  model_config = {"populate_by_name": True}

  def __init__(self, **data):
    if "from" in data and "from_path" not in data:
      data["from_path"] = data.pop("from")
    super().__init__(**data)


@router.post(
  "/apps/{app_id}/move",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def move_app_file(
  app_id: int,
  body: _MoveBody,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Moves (renames) a file or directory within an app's storage tree.

  Both `from` and `to` go through the same hardened `_resolve` every other
  storage path does — the character whitelist, the `..` traversal reject,
  the absolute-path / out-of-tree containment check, and the no-symlink-
  component rule — so a move can neither read nor write outside the app's
  own `/data/apps/<id>` dir. Serialized under the per-app storage lock and
  re-verified against the app's `token_nonce` (the same uninstall / freed-
  id-reuse defense `write_app_file` uses), since a move that paused could
  otherwise land in a replacement app's tree.

  Parent directories of the destination are created as needed (a move INTO
  a new folder is a normal FileExplorer action). An existing destination is
  rejected with 409 rather than silently overwritten — the FileExplorer
  asks before clobbering.
  """
  expected_nonce = _check_cross_app(db, principal, app_id, mode="write").token_nonce
  data_dir = get_settings().data_dir
  base = Path(data_dir) / "apps" / str(app_id)
  src = _resolve(base, body.from_path)
  dst = _resolve(base, body.to)
  async with fs_locks.app_storage_lock(app_id):
    _recheck_app_identity(db, app_id, expected_nonce)
    if not src.exists():
      raise HTTPException(status_code=404, detail="Source not found.")
    if dst.exists():
      raise HTTPException(status_code=409, detail="Destination already exists.")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    # Carry the MIME sidecar(s) to the new path so the moved bytes keep their
    # stored type, and the old path keeps no stale sidecar.
    move_content_type(
      data_dir, Path("apps") / str(app_id), body.from_path, body.to
    )
  if activity.should_emit_storage_write(app_id, body.to):
    activity.log_event(
      "storage_write", app_id=app_id, path=body.to, size_delta=0
    )
  return Response(status_code=204)


# Registered BEFORE the catch-all `delete_app_file` so a `DELETE
# /apps/{id}/folder/<path>` matches this recursive-folder route rather than
# being swallowed by the single-file `{path:path}` catch-all (FastAPI
# resolves DELETE routes in registration order, and both share the method).
@router.delete(
  "/apps/{app_id}/folder/{path:path}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def delete_app_folder(
  app_id: int,
  path: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Recursively deletes a directory (and its contents) from an app's tree.

  The per-file `DELETE /apps/{id}/{path}` route only removes a single file;
  the FileExplorer also needs to drop a whole folder. The path goes through
  the same `_resolve` hardening (whitelist, `..` reject, containment, no
  symlink component) so the recursive removal can never escape the app's
  own storage dir. Serialized + nonce-rechecked under the per-app lock like
  every other mutating storage route.

  Deleting the app's storage ROOT (an empty path) is rejected — the root is
  the app's own directory, owned by install/uninstall, not a folder the
  FileExplorer may blow away. A missing path 404s; a path that is a FILE
  rather than a directory 400s (use the per-file DELETE for that).
  """
  expected_nonce = _check_cross_app(db, principal, app_id, mode="write").token_nonce
  data_dir = get_settings().data_dir
  base = Path(data_dir) / "apps" / str(app_id)
  target = _resolve(base, path)
  async with fs_locks.app_storage_lock(app_id):
    _recheck_app_identity(db, app_id, expected_nonce)
    if target.resolve() == base.resolve():
      raise HTTPException(
        status_code=400, detail="Cannot delete the app storage root."
      )
    if not target.exists():
      raise HTTPException(status_code=404, detail="Folder not found.")
    if not target.is_dir():
      raise HTTPException(
        status_code=400, detail="Path is not a directory."
      )
    shutil.rmtree(target)
    # Drop the mirrored sidecar subtree so no stale stored MIME survives a
    # folder removal to shadow a future file recreated at the same path.
    delete_content_type_tree(data_dir, Path("apps") / str(app_id), path)
  if activity.should_emit_storage_write(app_id, path):
    activity.log_event(
      "storage_write", app_id=app_id, path=path, size_delta=0
    )
  return Response(status_code=204)


@router.put(
  "/apps/{app_id}/{path:path}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def write_app_file(
  app_id: int,
  path: str,
  request: Request,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Writes content to a file in an app's data directory."""
  expected_nonce = _check_cross_app(db, principal, app_id, mode="write").token_nonce
  data_dir = get_settings().data_dir
  base = Path(data_dir) / "apps" / str(app_id)
  file_path = _resolve(base, path)
  content, stored_mime = await _decode_write_body(request, file_path)
  new_size = len(
    content.encode("utf-8") if isinstance(content, str) else content
  )
  # Serialize the write against this app's uninstall AND re-verify, under the
  # lock, that this is still the SAME app (its token_nonce is unchanged). A
  # write that paused to read its body could otherwise land after an
  # interleaved uninstall — recreating /data/apps/<id> as an orphan tree, or
  # worse, writing into a DIFFERENT app that reused the freed id (Codex review
  # round-6 #3, round-9 #1). Möbius runs one uvicorn worker, so this in-process
  # lock fully serializes write vs uninstall.
  async with fs_locks.app_storage_lock(app_id):
    _recheck_app_identity(db, app_id, expected_nonce)
    # A directory destination would make the write raise IsADirectory and
    # surface as an opaque 500; reject it explicitly.
    if file_path.is_dir():
      raise HTTPException(status_code=400, detail="Destination is a directory.")
    # Snapshot the pre-write size for size_delta. A missing file is zero; a
    # stat failure (race with delete) is also zero — best-effort, not
    # load-bearing.
    try:
      before_size = file_path.stat().st_size if file_path.is_file() else 0
    except OSError:
      before_size = 0
    # Per-app quota: reject a write that would push the app's total stored
    # bytes over the cap, BEFORE writing — so one runaway app can't fill
    # /data. An overwrite charges only the delta (new minus the bytes it
    # replaces), so rewriting the same key never falsely exhausts the quota.
    # Computed inside the lock so a concurrent same-app write can't both pass
    # the check and overflow. `app_dir_usage` re-walks the tree (it can't
    # drift from a stale counter); the cap is read from the module so a test
    # can shrink it.
    projected = app_dir_usage(base) - before_size + new_size
    if projected > storage_io.MAX_APP_STORAGE_BYTES:
      raise HTTPException(
        status_code=413,
        detail=(
          "App storage quota exceeded — this write would bring the app "
          f"to {projected} bytes, over the "
          f"{storage_io.MAX_APP_STORAGE_BYTES}-byte per-app limit. Delete "
          "unused files or store large media outside per-app storage."
        ),
      )
    atomic_write(file_path, content)
    # Record (or clear) the served MIME sidecar so a cold read of an
    # extensionless or custom-MIME blob returns the app's declared type.
    # Inside the lock so the sidecar can't outlive a racing delete.
    write_content_type(data_dir, Path("apps") / str(app_id), path, stored_mime)
  # storage_write: debounced per (app_id, path) to ≤1 event per
  # minute. Agents writing many small files in a single chat shouldn't
  # flood the log. size_delta uses post-write size minus pre-write
  # size — negative when a file shrank.
  if activity.should_emit_storage_write(app_id, path):
    try:
      after_size = file_path.stat().st_size
    except OSError:
      after_size = 0
    activity.log_event(
      "storage_write",
      app_id=app_id,
      path=path,
      size_delta=after_size - before_size,
    )
  return Response(status_code=204)


@router.delete(
  "/apps/{app_id}/{path:path}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def delete_app_file(
  app_id: int,
  path: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Deletes a file from an app's data directory. 404 if missing."""
  expected_nonce = _check_cross_app(db, principal, app_id, mode="write").token_nonce
  data_dir = get_settings().data_dir
  base = Path(data_dir) / "apps" / str(app_id)
  file_path = _resolve(base, path)
  # Serialize with this app's uninstall + re-verify, under the per-app lock,
  # that this is still the SAME app (token_nonce unchanged), exactly like
  # write_app_file: a delayed DELETE for a reused SQLite id could otherwise
  # unlink a file belonging to the REPLACEMENT app that recycled the id (Codex
  # review round-8 #1, round-9 #1).
  async with fs_locks.app_storage_lock(app_id):
    _recheck_app_identity(db, app_id, expected_nonce)
    if not file_path.exists():
      raise HTTPException(status_code=404, detail="File not found.")
    if not file_path.is_file():
      raise HTTPException(status_code=400, detail="Path is not a file.")
    # Capture size before unlink so size_delta reflects the full removal
    # (negative number = freed bytes).
    try:
      deleted_size = file_path.stat().st_size
    except OSError:
      deleted_size = 0
    file_path.unlink()
    # Drop the MIME sidecar in lockstep so a later write to the same path
    # with a different type isn't shadowed by the stale stored MIME.
    delete_content_type(data_dir, Path("apps") / str(app_id), path)
  if activity.should_emit_storage_write(app_id, path):
    activity.log_event(
      "storage_write",
      app_id=app_id,
      path=path,
      size_delta=-deleted_size,
    )
  return Response(status_code=204)


@router.get("/shared/{path:path}")
def read_shared_file(
  path: str,
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Returns a file from the shared data directory."""
  base = Path(get_settings().data_dir) / "shared"
  file_path = _resolve(base, path)
  # is_file() so a directory path 404s instead of 500-ing in _serve_file
  # — same contract as the per-app read above.
  if not file_path.is_file():
    raise HTTPException(status_code=404, detail="File not found.")
  return _serve_file(file_path)


@router.put(
  "/shared/{path:path}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def write_shared_file(
  path: str,
  request: Request,
  _: models.Owner = Depends(get_current_owner),
):
  """Writes content to a file in the shared data directory.

  Auto-snapshots the prior `theme.css` to `theme.css.bak-<unix-ts>`
  on every overwrite, so a theme that breaks the UI can always be
  rolled back from the recovery page or via `?reset-theme=1`. The
  snapshot is BEST-EFFORT — a snapshot failure must not block the
  agent's write, since the write itself is the recovery target.

  Also emits a `storage_write` activity event with `app_id=0` and
  `scope="shared"` so the dreaming agent sees theme + experience-file
  edits as platform activity. Per-app writes already emit; without
  this branch shared/* changes were invisible.
  """
  settings = get_settings()
  base = Path(settings.data_dir) / "shared"
  file_path = _resolve(base, path)
  # Reject a directory destination explicitly rather than 500-ing on the
  # write below — same contract as the per-app write.
  if file_path.is_dir():
    raise HTTPException(status_code=400, detail="Destination is a directory.")
  # Snapshot pre-write size for size_delta. Same best-effort pattern
  # as the per-app write path: missing file or stat failure = 0.
  try:
    before_size = file_path.stat().st_size if file_path.is_file() else 0
  except OSError:
    before_size = 0
  # Shared storage carries no MIME sidecar (its files — theme.css, skills,
  # memory — are always extensioned text the filename guess recovers), so the
  # stored-mime half of the decode result is discarded here.
  content, _ = await _decode_write_body(request, file_path)
  # Snapshot the prior theme.css before any overwrite. The agent
  # already does this informally; making it automatic means no
  # accidental clobber when the agent forgets.
  if file_path.name == "theme.css" and file_path.parent == base:
    try:
      from app.theme import snapshot_theme_if_present
      snapshot_theme_if_present(settings.data_dir)
    except Exception as exc:
      _log.warning("theme.css snapshot failed: %s", exc)
  atomic_write(file_path, content)
  # storage_write for shared/* — debounced per (0, path) to match the
  # per-app rate-limit. app_id=0 is the documented platform-level
  # sentinel (see activity.py docstring); scope="shared" disambiguates
  # in the consumer (a future per-app event with id=0 would still be
  # distinguishable by absence of the scope field).
  if activity.should_emit_storage_write(0, path):
    try:
      after_size = file_path.stat().st_size
    except OSError:
      after_size = 0
    activity.log_event(
      "storage_write",
      app_id=0,
      scope="shared",
      path=path,
      size_delta=after_size - before_size,
    )
  return Response(status_code=204)


@router.delete(
  "/shared/{path:path}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
def delete_shared_file(
  path: str,
  _: models.Owner = Depends(get_current_owner),
):
  """Deletes a file from the shared data directory. 404 if missing.

  Emits a `storage_write` event with negative size_delta so the
  dreaming agent sees shared-file removals symmetrically with
  creations / updates.
  """
  base = Path(get_settings().data_dir) / "shared"
  file_path = _resolve(base, path)
  if not file_path.exists():
    raise HTTPException(status_code=404, detail="File not found.")
  if not file_path.is_file():
    raise HTTPException(status_code=400, detail="Path is not a file.")
  try:
    deleted_size = file_path.stat().st_size
  except OSError:
    deleted_size = 0
  file_path.unlink()
  if activity.should_emit_storage_write(0, path):
    activity.log_event(
      "storage_write",
      app_id=0,
      scope="shared",
      path=path,
      size_delta=-deleted_size,
    )
  return Response(status_code=204)


@router.get("/apps-list/{app_id}/{prefix:path}")
def list_app_dir(
  app_id: int,
  prefix: str,
  limit: int = _LIST_DEFAULT_LIMIT,
  cursor: str | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Lists the immediate children of an app-storage directory.

  Lives under a separate `apps-list` namespace so the route does not
  collide with the catch-all `GET /apps/{app_id}/{path}` file read.
  Authorization mirrors a file read exactly (`_check_cross_app` in read
  mode), so owner, own-app, and declared cross-app callers all behave
  the same as they would fetching a file.

  Listing is NON-recursive — only direct children of `prefix` are
  returned, each as a `_list_entry` with name, path, type, size,
  modified_at, and (for files) mime_type. Entries sort lexically by
  name, which is also what makes the `cursor` pagination deterministic:
  the cursor is the last name returned, and the next page resumes at the
  first name strictly greater than it.

  A `prefix` that does not resolve to an existing directory returns an
  empty listing rather than a 404 — enumerating a not-yet-created
  directory (the first run of an app before it has written anything) is
  a normal, expected call, not an error. The response is API metadata
  and is NOT subject to the stored-file JSON-envelope rules.
  """
  _check_cross_app(db, principal, app_id, mode="read")
  data_dir = get_settings().data_dir
  base = Path(data_dir) / "apps" / str(app_id)
  # An empty prefix means the app's root dir; _resolve's whitelist
  # rejects the empty string, so short-circuit to base here. A
  # non-empty prefix goes through the same containment + traversal
  # checks every file path does.
  dir_path = base if prefix == "" else _resolve(base, prefix)
  if not dir_path.is_dir():
    return {"entries": [], "next_cursor": None}
  # Resolve each file's stored MIME from its sidecar so the listing reports
  # the same type a read would serve (extensionless/custom-MIME blobs).
  scope = Path("apps") / str(app_id)
  entries, next_cursor = _list_directory_page(
    dir_path, prefix, limit, cursor,
    mime_override=lambda rel: read_content_type(data_dir, scope, rel),
  )
  return {"entries": entries, "next_cursor": next_cursor}


@router.get("/shared-list/{path:path}")
def list_shared_dir(
  path: str,
  limit: int = _LIST_DEFAULT_LIMIT,
  cursor: str | None = None,
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Lists the immediate children of a shared-storage directory.

  Same hardened contract and `{entries, next_cursor}` shape as the
  per-app `apps-list` route: symlinks and unsafe names
  are dropped by `_list_entry`, `OSError` on a racing dirent can't 500 the
  page, and the listing is keyset-paginated. An empty `path` is the shared
  root; a path that doesn't resolve to a directory returns an empty
  listing rather than 404, matching `apps-list` (enumerating a not-yet-
  created directory is a normal call).
  """
  base = Path(get_settings().data_dir) / "shared"
  dir_path = base if path == "" else _resolve(base, path)
  if not dir_path.is_dir():
    return {"entries": [], "next_cursor": None}
  entries, next_cursor = _list_directory_page(dir_path, path, limit, cursor)
  return {"entries": entries, "next_cursor": next_cursor}
