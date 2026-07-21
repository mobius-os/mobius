"""Owner-facing filesystem + git oversight API.

Mini-apps see only their own `/api/storage/apps/<id>/...` scope and cannot
read the broader container filesystem. This router gives the OWNER a view of
the whole `/data` tree plus per-repo git status, so the Editor app can
visualise the file structure, show what the agent has changed, and let the
owner edit files directly. Writes are bounded to what the `mobius` process can
write — `/data` — which is exactly the surface the agent itself has; platform
code stays root-owned and read-only.

Auth accepts the owner or an app token carrying the explicit live
`filesystem_access` capability. The Editor is the canonical holder. The grant
does not weaken this router's root confinement, secret deny-list, symlink
containment, or size caps; it removes the need to expose the owner JWT to an app
frame while keeping the privileged action auditable and revocable per app.

Safety, proportional to a single-owner tool:
- a configurable read root (default `/data`) — never the whole container; a
  separate write root pinned to `/data` so widening the view never widens write.
- a deny-list for secrets (cli-auth, the service token, the JWT key, the raw
  DB, .env); read/write/git of a denied path 403s, `tree` omits it and reports
  the count in `redacted`.
- `validate_path_within_base` for containment (resolves `..`/symlinks/abs
  injection); listings omit symlinks; reads/writes are size-capped.
- a write to a root-owned (platform) path surfaces as a clean 403, not a 500.
- git via subprocess under an isolated env (scrub GIT_* + pin
  GIT_CEILING_DIRECTORIES) so a status op can't walk up into /data's own repo.
"""

import base64
import datetime
import mimetypes
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from app import models
from app.config import get_settings
from app.deps import get_owner_or_app_with_filesystem_access, reject_cross_site
from app.path_utils import validate_path_within_base

router = APIRouter(prefix="/api/fs", tags=["fs"])

# Single-level listing page sizes — the FS can be huge, so the tree is never
# walked whole; the UI expands one directory per request (a real file
# explorer). Mirrors storage.py's listing bounds.
_LIST_DEFAULT_LIMIT = 200
_LIST_MAX_LIMIT = 1000

# Read/write caps. Inline small text; stream larger reads up to the hard cap;
# refuse above it so a stray multi-GB file can't pressure the 7.6 GB host.
_INLINE_READ_MAX = 256 * 1024
_READ_MAX = 5 * 1024 * 1024
_WRITE_MAX = 5 * 1024 * 1024

# Upper bound on the immediate-child count reported for a directory when
# `tree?counts=1` is requested — scandir stops here and the count clamps to
# this value so a directory with millions of entries can't turn a cheap badge
# into an unbounded scan. The UI renders a capped count as "10000+".
_CHILD_COUNT_CAP = 10000

# Caps on the recursive `/du` walk. `/data` holds huge trees (.git packs,
# node_modules, compiled bundles); an unbounded walk over one pins CPU/IO and
# blows the request budget, so the walk stops at whichever of these limits it
# reaches first and reports `truncated: true` to say the totals are a lower
# bound. Eight wall-clock seconds keeps the endpoint responsive; 200k entries
# caps a pathological single directory before the wall-clock even fires. (The
# walk is bounded two further ways it need not size: it never follows a
# directory symlink, so it can't cycle, and it prunes the deny-listed
# secrets.)
_DU_TIME_BUDGET_S = 8.0
_DU_ENTRY_CAP = 200_000

_GIT_TIMEOUT = 30
_GIT_LIST_CAP = 200  # per-category status list cap; counts stay exact

# Paths relative to the FS root that the viewer never reads/writes/git's
# (403) and omits from listings (reported in `redacted`). Mostly secrets;
# `.storage-meta` is the one non-secret entry — it's the storage layer's
# internal MIME-sidecar tree (routes/storage.py), kept out of the owner's
# File Explorer so it can't be mistaken for app data or hand-edited (the card
# 085 contract that sidecars never leak into listings or agent edits). The DB
# stays *listable* (size visible) but not raw-readable — the /recover backup
# is the right channel for a consistent copy.
_DENY_RELPATHS = (
  "cli-auth",
  "service-token.txt",
  ".secret-key",
  ".recovery-secret",
  ".recovery-owner.json",
  ".env",
  "db/ultimate.db",
  ".storage-meta",
  # First-boot claim gate (app.setup_claim): the one-time setup token, and its
  # non-secret consumed marker. The token must never leak via the file API; the
  # marker is runtime state, kept out like .storage-meta so it can't be
  # hand-edited into a fail-closed lockout.
  ".setup-claim",
  ".setup-consumed",
)
# Defense in depth: a secret-shaped filename anywhere in the tree is denied,
# in case one is copied outside its canonical home. `.recovery-secret` is the
# HMAC key that signs recovery session cookies (forging it grants the recovery
# surface); `.recovery-owner.json` holds the owner's password hash for the
# DB-independent recovery fallback.
_SECRET_NAMES = {
  ".env", ".secret-key", ".recovery-secret", ".recovery-owner.json",
  ".credentials.json", "service-token.txt",
  # The first-boot setup claim is a one-time takeover secret — deny a file of
  # that name anywhere in the tree, in case a copy lands outside its root home.
  ".setup-claim",
}


def _fs_root() -> Path:
  """The root the viewer is confined to. Defaults to the data dir; a
  `fs_view_root` setting can widen it later without a code change, but it
  ships narrow (`/data` holds everything the owner cares about and avoids
  /app, /etc, /proc noise + risk)."""
  s = get_settings()
  root = getattr(s, "fs_view_root", "") or s.data_dir
  return Path(root).resolve()


def _write_root() -> Path:
  """Writes are pinned to the data dir regardless of how wide the read view
  is — the `mobius` process can only write `/data` anyway, so this matches the
  OS boundary and keeps a widened read view from ever widening write."""
  return Path(get_settings().data_dir).resolve()


def _rel_to_root(resolved: Path, root: Path) -> str:
  try:
    return resolved.relative_to(root).as_posix()
  except ValueError:
    return ""


def _is_denied(resolved: Path, root: Path) -> bool:
  """True when the resolved path is a secret we never expose."""
  rel = _rel_to_root(resolved, root)
  if rel == "":
    return False  # the root itself
  for d in _DENY_RELPATHS:
    # `d + "-"` denies a file's sidecars alongside the file itself — chiefly
    # SQLite's WAL-mode companions db/ultimate.db-wal / -shm / -journal, which
    # hold live pages of the very rows db/ultimate.db protects (password hash,
    # encrypted keys, chats). Without this the sidecars read raw + list openly.
    if rel == d or rel.startswith(d + "/") or rel.startswith(d + "-"):
      return True
  return any(part in _SECRET_NAMES for part in Path(rel).parts)


def _resolve(path: str, root: Path) -> Path:
  """Resolve a user path under the root with containment + a NUL guard.

  Raises 400 on traversal/NUL; the caller handles 403/404."""
  if path and "\x00" in path:
    raise HTTPException(status_code=400, detail="Invalid path.")
  rel = (path or "").lstrip("/")
  return validate_path_within_base(rel, root)


def _entry(child: Path, rel_prefix: str) -> dict | None:
  """One listing entry for an immediate child, or None to omit it.

  Symlinks are omitted (following them with stat() could leak a target's
  mtime/size outside the root, and a read of one is refused anyway). Unlike
  storage's listing we do NOT reject names with spaces/unicode — a read-only
  viewer must surface real-world filenames; containment is enforced by
  `validate_path_within_base`, not a name whitelist."""
  if child.is_symlink():
    return None
  try:
    stat = child.stat()
  except OSError:
    return None
  is_dir = child.is_dir()
  modified = datetime.datetime.fromtimestamp(
    stat.st_mtime, tz=datetime.timezone.utc
  ).isoformat().replace("+00:00", "Z")
  rel = f"{rel_prefix.rstrip('/')}/{child.name}" if rel_prefix else child.name
  entry = {
    "name": child.name,
    "path": rel,
    "type": "directory" if is_dir else "file",
    "size": 0 if is_dir else stat.st_size,
    "modified_at": modified,
  }
  if is_dir:
    # A cheap probe so the UI can badge git repos + decide whether to call
    # /git, without a recursive walk.
    entry["is_git_repo"] = (child / ".git").exists()
  else:
    mime, _ = mimetypes.guess_type(child.name)
    entry["mime_type"] = mime
  return entry


def _child_count(subdir: Path) -> int | None:
  """Number of immediate entries in `subdir`, bounded by `_CHILD_COUNT_CAP`.

  Counting stops at the cap and returns the cap value (the UI renders it as
  "10000+") so a huge directory can't turn a cheap badge into an unbounded
  scan. Returns None when the directory can't be scanned (e.g. a permission
  error) so the caller omits the field rather than failing the whole page."""
  n = 0
  try:
    with os.scandir(subdir) as it:
      for _ in it:
        n += 1
        if n >= _CHILD_COUNT_CAP:
          return _CHILD_COUNT_CAP
  except OSError:
    return None
  return n


@router.get("/disk")
def fs_disk(_owner: models.Owner = Depends(get_owner_or_app_with_filesystem_access)):
  """Disk usage of the HOST filesystem that backs the data dir, in bytes.

  A single `statvfs` on the data-dir mount — no path parameter, no walk. This
  reports the underlying HOST volume holding `/data` (total/used/free), NOT a
  Möbius-imposed quota or the size of the viewable tree: everything else on
  that mount (the OS, other containers, images) counts toward `used`. The
  Editor surfaces it so the owner can see how much room the volume has
  left."""
  data_dir = get_settings().data_dir
  usage = shutil.disk_usage(data_dir)
  return {"total": usage.total, "used": usage.used, "free": usage.free,
          "path": data_dir}


@router.get("/du")
def fs_du(
  path: str = Query("", description="path relative to the FS root"),
  _owner: models.Owner = Depends(get_owner_or_app_with_filesystem_access),
):
  """Recursive disk usage of a directory subtree — "what is eating space".

  Sums the sizes of every file under `path` (immediate + all descendants)
  and counts files and subdirectories, so the Editor can show a folder's
  real weight the way a file manager's "recursive size" does. A denied path
  403s; a missing / non-directory path returns zeros (like `tree`).

  The walk is BOUNDED, because `/data` holds huge trees (.git, node_modules)
  an unbounded walk would pin CPU/IO on. It never follows a directory symlink
  (`os.walk(..., followlinks=False)`, so it can't cycle), prunes the
  deny-listed secret subtrees from both descent and the totals, and stops
  after `_DU_TIME_BUDGET_S` seconds or `_DU_ENTRY_CAP` visited entries. When
  any of those made the totals a lower bound — a cap fired or a secret was
  pruned — `truncated` is true; it is false only when the whole subtree was
  summed."""
  root = _fs_root()
  resolved = _resolve(path, root)
  if _is_denied(resolved, root):
    raise HTTPException(status_code=403, detail="Path not viewable.")
  rel = _rel_to_root(resolved, root)
  if not resolved.is_dir():
    return {"path": rel, "bytes": 0, "files": 0, "dirs": 0, "truncated": False}

  total_bytes = 0
  files = 0
  dirs = 0
  visited = 0
  truncated = False
  deadline = time.monotonic() + _DU_TIME_BUDGET_S
  stop = False

  # `os.walk` is scandir-backed, so classifying an entry as dir-vs-file costs
  # no extra stat; only a file's SIZE needs one. `topdown=True` lets us prune
  # `dirnames` in place to skip descent into symlinked or denied subtrees.
  for dirpath, dirnames, filenames in os.walk(
    resolved, topdown=True, followlinks=False
  ):
    cur = Path(dirpath)
    kept = []
    for name in dirnames:
      child = cur / name
      # A directory symlink is never followed — it could point outside the
      # root or back up the tree and cycle — and it isn't counted (a symlink
      # holds no real data of its own here). A denied subtree is a secret we
      # never total, and pruning it makes the totals a lower bound, so it
      # flips `truncated`.
      if child.is_symlink():
        continue
      if _is_denied(child, root):
        truncated = True
        continue
      kept.append(name)
    dirnames[:] = kept
    dirs += len(kept)
    visited += len(kept)

    for name in filenames:
      visited += 1
      # The caps must bite mid-directory: a single directory can hold
      # millions of entries, so a per-directory check alone wouldn't bound
      # the stat cost within one huge folder.
      if visited > _DU_ENTRY_CAP or time.monotonic() > deadline:
        truncated = True
        stop = True
        break
      f = cur / name
      # One `lstat` per file: it never follows the link, so it both detects a
      # symlink (skip it — its target's size lives elsewhere) and yields the
      # size for a real file. A per-entry OSError (unreadable file, races a
      # delete) skips just that entry rather than failing the whole request.
      try:
        st = f.stat(follow_symlinks=False)
      except OSError:
        continue
      if stat.S_ISLNK(st.st_mode):
        continue
      if _is_denied(f, root):
        truncated = True
        continue
      total_bytes += st.st_size
      files += 1

    if stop:
      break
    # Also check between directories, so a deep tree of empty (file-less)
    # dirs can't run past the wall-clock without ever entering the file loop.
    if visited > _DU_ENTRY_CAP or time.monotonic() > deadline:
      truncated = True
      break

  return {"path": rel, "bytes": total_bytes, "files": files, "dirs": dirs,
          "truncated": truncated}


@router.get("/tree")
def fs_tree(
  path: str = Query("", description="path relative to the FS root"),
  limit: int = Query(_LIST_DEFAULT_LIMIT),
  cursor: str | None = Query(None, description="opaque pagination cursor"),
  counts: int = Query(0, description="1 = add child_count to dir entries"),
  _owner: models.Owner = Depends(get_owner_or_app_with_filesystem_access),
):
  """List one directory level under the root (lazy, keyset-paginated).

  Directories sort before files, each alphabetical (case-insensitive). The
  cursor is the opaque base64 of the last returned name; pass it back for the
  next page. A non-existent / non-directory path returns empty entries (like
  storage's "enumerating a not-yet-created dir is normal"); a denied path
  403s. `?counts=1` adds a `child_count` to each DIRECTORY entry on the
  returned page (one bounded `scandir` per dir, only the paginated dirs), so
  the UI can badge folder sizes; without it the response is unchanged and no
  per-subdir scan runs."""
  root = _fs_root()
  resolved = _resolve(path, root)
  if _is_denied(resolved, root):
    raise HTTPException(status_code=403, detail="Path not viewable.")
  rel_prefix = _rel_to_root(resolved, root)
  if not resolved.is_dir():
    return {"root": str(root), "path": rel_prefix, "abs_path": str(resolved),
            "entries": [], "next_cursor": None, "redacted": []}

  limit = max(1, min(limit, _LIST_MAX_LIMIT))
  # Offset cursor: the count already returned. The listing is re-scanned and
  # re-sorted deterministically each request, so an offset resumes correctly —
  # and (unlike a name-keyed cursor) it matches the dirs-first, then
  # case-insensitive-name sort without needing to re-encode that whole key.
  offset = 0
  if cursor:
    try:
      offset = max(0, int(base64.urlsafe_b64decode(cursor.encode()).decode()))
    except Exception:
      offset = 0

  redacted: list[str] = []
  rows: list[dict] = []
  try:
    children = list(os.scandir(resolved))
  except OSError:
    children = []
  for de in children:
    child = resolved / de.name
    if _is_denied(child, root):
      redacted.append(de.name)
      continue
    e = _entry(child, rel_prefix)
    if e is not None:
      rows.append(e)

  # Directories first, then files; alphabetical within each, case-insensitive.
  rows.sort(key=lambda r: (r["type"] != "directory", r["name"].lower()))
  page = rows[offset:offset + limit]
  if counts:
    # Opt-in: badge each directory on THIS page with its immediate-child
    # count. Bounded to the page's dirs (never the whole listing), so the cost
    # is one bounded scandir per paginated directory; a dir that errors on
    # scandir simply gets no child_count rather than failing the page.
    for e in page:
      if e["type"] == "directory":
        c = _child_count(resolved / e["name"])
        if c is not None:
          e["child_count"] = c
  next_cursor = None
  if len(rows) > offset + limit:
    nxt = str(offset + limit)
    next_cursor = base64.urlsafe_b64encode(nxt.encode()).decode()

  return {
    "root": str(root),
    "path": rel_prefix,
    "abs_path": str(resolved),
    "entries": page,
    "next_cursor": next_cursor,
    "redacted": redacted,
  }


def _looks_binary(file_path: Path) -> bool:
  try:
    with open(file_path, "rb") as f:
      chunk = f.read(8192)
  except OSError:
    return False
  if b"\x00" in chunk:
    return True
  try:
    chunk.decode("utf-8")
    return False
  except UnicodeDecodeError:
    return True


@router.get("/read")
def fs_read(
  path: str = Query(..., description="path relative to the FS root"),
  meta: int = Query(0, description="1 = return metadata only, no body"),
  head: int = Query(0, description="1 = peek the top of an oversized text file"),
  _owner: models.Owner = Depends(get_owner_or_app_with_filesystem_access),
):
  """Read a single file.

  Text ≤256 KB is returned inline; larger text + binaries stream. Over the
  5 MB cap → 413. `?meta=1` returns size/mime/is_binary/writable without the
  body so the UI can avoid auto-loading a big log or a binary, and know
  whether to offer editing. `?head=1` on a text file over the cap returns the
  first 256 KB (utf-8, errors replaced) as text with `X-Mobius-Truncated: 1`
  and `X-Mobius-Total-Size` headers, so the UI can peek the top of a big log
  instead of refusing it; on a file within the cap `head` changes nothing, and
  a binary over the cap still 413s (no partial binary)."""
  root = _fs_root()
  resolved = _resolve(path, root)
  if _is_denied(resolved, root):
    raise HTTPException(status_code=403, detail="Path not viewable.")
  if not resolved.is_file():
    raise HTTPException(status_code=404, detail="Not a file.")
  size = resolved.stat().st_size
  mime, _ = mimetypes.guess_type(resolved.name)
  is_binary = _looks_binary(resolved)

  if meta:
    return {"name": resolved.name, "size": size, "mime_type": mime,
            "is_binary": is_binary, "writable": _is_writable(resolved),
            "modified_at": datetime.datetime.fromtimestamp(
              resolved.stat().st_mtime, tz=datetime.timezone.utc
            ).isoformat().replace("+00:00", "Z")}

  if size > _READ_MAX:
    if head and not is_binary:
      # Peek the top of an oversized text file rather than refusing it, so the
      # UI can show the first page of a big log. Only the inline cap is read;
      # binaries never take this path (a partial binary is meaningless).
      try:
        with open(resolved, "rb") as f:
          chunk = f.read(_INLINE_READ_MAX)
      except OSError:
        raise HTTPException(status_code=404, detail="Not a file.")
      text = chunk.decode("utf-8", errors="replace")
      return PlainTextResponse(text, headers={
        "X-Mobius-Truncated": "1",
        "X-Mobius-Total-Size": str(size),
      })
    raise HTTPException(
      status_code=413,
      detail=f"File too large to preview ({size // 1024} KB > "
             f"{_READ_MAX // 1024} KB cap). Open it via the agent.",
    )
  if is_binary:
    return FileResponse(resolved, media_type=mime or "application/octet-stream")
  if size <= _INLINE_READ_MAX:
    try:
      text = resolved.read_text(encoding="utf-8", errors="replace")
    except OSError:
      raise HTTPException(status_code=404, detail="Not a file.")
    return PlainTextResponse(text)
  return FileResponse(resolved, media_type="text/plain")


def _is_writable(resolved: Path) -> bool:
  """Whether the owner can edit this path directly — under the write root,
  not denied, and writable by the process (root-owned platform files are
  read-only here, same as for the agent)."""
  wroot = _write_root()
  try:
    resolved.relative_to(wroot)
  except ValueError:
    return False
  if _is_denied(resolved, _fs_root()):
    return False
  if resolved.exists():
    return os.access(resolved, os.W_OK)
  # New file: writable if the nearest existing parent is.
  parent = resolved.parent
  while not parent.exists() and parent != wroot:
    parent = parent.parent
  return parent.exists() and os.access(parent, os.W_OK)


@router.put("/write", dependencies=[Depends(reject_cross_site)])
def fs_write(
  path: str = Query(..., description="path relative to the data dir"),
  content: str = Body(..., media_type="text/plain"),
  _owner: models.Owner = Depends(get_owner_or_app_with_filesystem_access),
):
  """Write (create or overwrite) a TEXT file under `/data`.

  Owner-only direct edit — the same write surface the agent has. Bounded to
  the data dir; a path outside it, a denied secret, or a root-owned platform
  file is refused (403) rather than crashing. Binary editing is out of scope
  (the Editor edits text/markdown); a binary lands via the agent or upload."""
  wroot = _write_root()
  if path and "\x00" in path:
    raise HTTPException(status_code=400, detail="Invalid path.")
  resolved = validate_path_within_base((path or "").lstrip("/"), wroot)
  # Deny against the WRITE root (always /data), not the read view — a widened
  # fs_view_root must never let a write reach a secret the read view exposes.
  if _is_denied(resolved, wroot):
    raise HTTPException(status_code=403, detail="This file is protected.")
  if resolved.is_dir():
    raise HTTPException(status_code=400, detail="Path is a directory.")
  data = content.encode("utf-8")
  if len(data) > _WRITE_MAX:
    raise HTTPException(status_code=413, detail="File too large to write.")
  try:
    resolved.parent.mkdir(parents=True, exist_ok=True)
    tmp = resolved.with_name(resolved.name + ".fswrite.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, resolved)
  except PermissionError:
    raise HTTPException(
      status_code=403,
      detail="Platform-managed file — read-only. Ask the agent if it must change.",
    )
  except OSError as e:
    raise HTTPException(status_code=400, detail=f"Could not write: {e}")
  st = resolved.stat()
  return {"path": _rel_to_root(resolved, wroot), "size": st.st_size,
          "modified_at": datetime.datetime.fromtimestamp(
            st.st_mtime, tz=datetime.timezone.utc
          ).isoformat().replace("+00:00", "Z")}


@router.delete("/delete", dependencies=[Depends(reject_cross_site)])
def fs_delete(
  path: str = Query(..., description="path relative to the data dir"),
  _owner: models.Owner = Depends(get_owner_or_app_with_filesystem_access),
):
  """Delete a single file under `/data`.

  Owner-only, same bounded surface as fs_write: a path outside the data dir, a
  denied secret, or a root-owned platform file is refused (403) rather than
  acted on. Directories are refused (400) — this is a file delete, not a
  recursive tree removal, so a mistaken path can't wipe a folder."""
  wroot = _write_root()
  if path and "\x00" in path:
    raise HTTPException(status_code=400, detail="Invalid path.")
  resolved = validate_path_within_base((path or "").lstrip("/"), wroot)
  # Deny against the WRITE root (always /data) — same invariant as fs_write.
  if _is_denied(resolved, wroot):
    raise HTTPException(status_code=403, detail="This file is protected.")
  if not resolved.exists():
    raise HTTPException(status_code=404, detail="File not found.")
  if resolved.is_dir():
    raise HTTPException(status_code=400, detail="Path is a directory.")
  try:
    resolved.unlink()
  except PermissionError:
    raise HTTPException(
      status_code=403,
      detail="Platform-managed file — read-only. Ask the agent if it must change.",
    )
  except OSError as e:
    raise HTTPException(status_code=400, detail=f"Could not delete: {e}")
  return {"path": _rel_to_root(resolved, wroot), "deleted": True}


def _git_env(repo: Path) -> dict:
  """Isolated env so a status op can't bleed into /data's own repo (scrub
  inherited GIT_* pointers + pin GIT_CEILING_DIRECTORIES above the repo).
  Mirrors app_git._git_env."""
  env = dict(os.environ)
  for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
              "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE"):
    env.pop(var, None)
  env["GIT_CEILING_DIRECTORIES"] = str(repo.resolve().parent)
  return env


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    capture_output=True, text=True, timeout=_GIT_TIMEOUT,
    check=False, env=_git_env(repo),
  )


def _find_repo(start: Path, root: Path) -> Path | None:
  """Nearest ancestor of `start` containing a `.git`, bounded by `root`."""
  cur = start if start.is_dir() else start.parent
  root = root.resolve()
  while True:
    if (cur / ".git").exists():
      return cur
    if cur == root or root not in cur.parents:
      return None
    cur = cur.parent


@router.get("/git")
def fs_git(
  path: str = Query("", description="path within (or pointing at) a repo"),
  _owner: models.Owner = Depends(get_owner_or_app_with_filesystem_access),
):
  """Git status/branch summary for the repo containing `path`.

  Minimal + informative: branch, head sha, upstream + ahead/behind, and
  staged/modified/untracked counts plus capped path lists — enough to see
  "this app has uncommitted edits" at a glance. Read-only (status/rev-parse/
  rev-list mutate nothing; no fetch). 404 when no repo is found between
  `path` and the root."""
  root = _fs_root()
  resolved = _resolve(path, root)
  if _is_denied(resolved, root):
    raise HTTPException(status_code=403, detail="Path not viewable.")
  repo = _find_repo(resolved, root)
  if repo is None:
    raise HTTPException(status_code=404, detail="No git repository here.")

  branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
  head = _git(repo, "rev-parse", "--short", "HEAD").stdout.strip()
  detached = branch == "HEAD"

  ahead = behind = None
  upstream = _git(repo, "rev-parse", "--abbrev-ref",
                  "--symbolic-full-name", "@{upstream}")
  upstream_ref = upstream.stdout.strip() if upstream.returncode == 0 else None
  if upstream_ref:
    rl = _git(repo, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if rl.returncode == 0:
      parts = rl.stdout.split()
      if len(parts) == 2:
        behind, ahead = int(parts[0]), int(parts[1])

  staged: list[dict] = []
  modified: list[dict] = []
  untracked: list[dict] = []
  counts = {"staged": 0, "modified": 0, "untracked": 0}
  st = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
  # -z gives NUL-delimited records; a rename record carries an extra (old)
  # path in the following record — skip it. For a count/summary the leading
  # XY + first path is enough.
  records = [r for r in st.stdout.split("\x00") if r]
  i = 0
  while i < len(records):
    rec = records[i]
    if len(rec) < 3:
      i += 1
      continue
    xy, name = rec[:2], rec[3:]
    if xy[0] in "RC":  # rename/copy: the next record is the old path
      i += 1
    if xy == "??":
      counts["untracked"] += 1
      if len(untracked) < _GIT_LIST_CAP:
        untracked.append({"path": name})
    else:
      if xy[0] not in " ?":
        counts["staged"] += 1
        if len(staged) < _GIT_LIST_CAP:
          staged.append({"path": name, "status": xy[0]})
      if xy[1] not in " ":
        counts["modified"] += 1
        if len(modified) < _GIT_LIST_CAP:
          modified.append({"path": name, "status": xy[1]})
    i += 1

  truncated = (counts["staged"] > len(staged) or counts["modified"] > len(modified)
               or counts["untracked"] > len(untracked))
  return {
    "repo_root": _rel_to_root(repo, root),
    "abs_repo_root": str(repo),
    "branch": branch, "detached": detached, "head_sha": head,
    "upstream": upstream_ref, "ahead": ahead, "behind": behind,
    "counts": counts,
    "staged": staged, "modified": modified, "untracked": untracked,
    "truncated": truncated,
  }
