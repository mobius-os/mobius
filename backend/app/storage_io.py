"""Shared filesystem helpers for per-app and shared storage.

Lives apart from ``routes/storage.py`` so the installer (``install.py``) can
reuse the SAME atomic write the storage API uses when it seeds an app's initial
files — otherwise a seed would be written non-atomically and a concurrent read
could observe it torn.
"""

import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from fastapi import HTTPException, Request

# Hard cap on a single storage object — enforced on BOTH the PUT request body
# and the file served back. Möbius runs on a memory-tight host (recurring OOM),
# so an app writing or reading an unbounded blob would threaten the whole
# instance. 50 MB is far above any real per-key app payload (notes, reports,
# save files) while still bounding the blast radius.
MAX_STORAGE_BYTES = 50 * 1024 * 1024

# Hard cap on the TOTAL on-disk bytes a single mini-app may store. The per-blob
# cap above bounds one object; this bounds the app's whole tree so one app
# (buggy or runaway) can't fill `/data` and take the disk-tight host down for
# every other app. Generous — a few hundred max-blobs — so no legitimate app
# (media library, save files, offline cache) hits it in normal use; it's a
# blast-radius bound, not a usage budget. Per the data-layer philosophy this is
# a backstop, not a wall: the owner's agent can raise it in code if a real app
# needs more headroom.
MAX_APP_STORAGE_BYTES = 1024 * 1024 * 1024


def file_version_token(file_path: Path) -> str:
  """Returns an opaque version token for a storage file.

  The storage route computes this only for callers that opt into CAS/version
  headers. mtime_ns + size is cheap, stable enough under the per-app write
  lock, and changes after atomic_write's os.replace() installs the new inode.
  """
  st = file_path.stat()
  return f'"{st.st_mtime_ns:x}-{st.st_size:x}"'


def etag_matches(token: str, if_match: str) -> bool:
  """Reports whether a client's If-Match refers to the version in `token`.

  A transcoding reverse proxy rewrites the ETag it forwards so the tag stays
  unique per content-encoding: Caddy's `encode` turns a strong `"<tok>"` into
  `"<tok>-gzip"` on a compressed read. A client echoing that suffixed tag back
  in If-Match would never strong-equal our un-suffixed version token, so every
  CAS write to a compressible file would 412 forever — strip a trailing
  `-<encoding>` suffix before comparing. Otherwise follow RFC 9110 §13.1.1:
  `*` matches any existing representation, a weak (`W/`) tag never
  strong-matches, and comma-separated candidates match if any one does.
  """
  value = if_match.strip()
  if value == "*":
    return True
  for raw in value.split(","):
    tag = raw.strip()
    if tag.startswith("W/"):
      continue
    for encoding in ("gzip", "br", "zstd", "deflate"):
      if tag.endswith(f'-{encoding}"'):
        tag = tag[: -(len(encoding) + 2)] + '"'
        break
    if tag == token:
      return True
  return False


def atomic_write(file_path: Path, content: str | bytes) -> None:
  """Writes content to file_path atomically — no torn or interleaved reads.

  A reader (or the listing-based completion poll a mini-app runs after a job)
  must never observe a half-written file, and two concurrent writers to the
  same path must not interleave bytes into one corrupt file. Write the full
  body to a uniquely-named temp file in the SAME directory, fsync it, then
  ``os.replace()`` onto the target — a same-filesystem rename is atomic on
  POSIX, so a reader sees either the old file or the new one, never a
  truncation. A crash mid-write leaves only the temp file; the target is never
  partial.
  """
  file_path.parent.mkdir(parents=True, exist_ok=True)
  data = content.encode("utf-8") if isinstance(content, str) else content
  # Unique temp name (mkstemp) so concurrent writers to the same path don't
  # collide on the temp file itself. mkstemp creates 0600; chmod to 0644 so the
  # file is readable the same way a normal umask-022 write would leave it.
  fd, tmp = tempfile.mkstemp(
    dir=file_path.parent, prefix=f".{file_path.name}.", suffix=".tmp"
  )
  try:
    with os.fdopen(fd, "wb") as f:
      f.write(data)
      f.flush()
      os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, file_path)
  except BaseException:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise


async def read_capped_body(request: Request, cap: int = MAX_STORAGE_BYTES) -> bytes:
  """Reads a request body, refusing once it crosses `cap` bytes.

  A declared Content-Length over the cap is rejected before a byte is read;
  then the body is streamed chunk-by-chunk and aborted the instant the running
  total exceeds the cap. So a runaway (or lying-Content-Length) upload can't
  buffer an unbounded body into memory and OOM the tight host (Codex review
  round-8 #3, round-9 #4). Shared by the storage PUT and the icon upload — any
  route that reads a raw body should use this instead of `request.body()`.
  """
  cl = request.headers.get("content-length")
  if cl is not None:
    try:
      declared = int(cl)
    except ValueError:
      declared = None
    if declared is not None and declared > cap:
      raise HTTPException(status_code=413, detail="Request body too large.")
  chunks: list[bytes] = []
  total = 0
  async for chunk in request.stream():
    total += len(chunk)
    if total > cap:
      raise HTTPException(status_code=413, detail="Request body too large.")
    chunks.append(chunk)
  return b"".join(chunks)


def app_dir_usage(app_dir: Path) -> int:
  """Returns the total bytes of regular files under an app's storage tree.

  Walks `app_dir` once, summing the size of every regular file (symlinks are
  not followed — they're rejected at write time and never counted, so the
  number reflects only bytes this app actually owns). A missing tree is 0
  (a fresh app that has written nothing). This is the live usage the per-app
  quota is checked against; it intentionally re-walks rather than caching a
  counter, so it can never drift out of sync with the filesystem the agent or
  a sibling process can also mutate.
  """
  total = 0
  for root, _dirs, files in os.walk(app_dir):
    for name in files:
      try:
        st = os.lstat(os.path.join(root, name))
      except OSError:
        continue
      # Count only regular, non-symlink files. lstat doesn't follow links, so
      # a symlink's own (tiny) entry is what `st` describes; S_ISREG filters
      # it out and avoids ever attributing a link target's size to this app.
      if stat.S_ISREG(st.st_mode):
        total += st.st_size
  return total


# Content-Type sidecars live in a meta tree PARALLEL to the storage tree, never
# inside it — so a `<path>.json` of stored MIME can't leak into an app's own
# listing or be edited/served as if it were app data. The layout mirrors the
# storage path: `<data_dir>/.storage-meta/apps/<id>/<path>.json`.
_META_DIRNAME = ".storage-meta"


def _meta_path(data_dir: str, scope_rel: Path, rel: str) -> Path:
  """The sidecar path for a stored file.

  `scope_rel` is the storage scope relative to data_dir (e.g. `apps/7` or
  `shared`); `rel` is the file's path within that scope. The sidecar is the
  same relative path under the parallel meta root, with `.json` appended — so
  it round-trips one-to-one with the file and is trivially locatable on read,
  write, and delete.
  """
  return Path(data_dir) / _META_DIRNAME / scope_rel / (rel + ".json")


def write_content_type(
  data_dir: str, scope_rel: Path, rel: str, content_type: str | None
) -> None:
  """Records (or clears) the served Content-Type for a stored file.

  Written on every PUT so a COLD read (cache miss / fresh device) of an
  extensionless or custom-MIME blob can serve the type the app declared,
  instead of the server's filename guess. A None/blank type clears any prior
  sidecar — a later read then falls back to the extension guess, which is the
  right behavior for a plain text/JSON write that carried no explicit type.
  Best-effort: a sidecar write failure must never fail the data write it
  annotates (the bytes are the source of truth; the MIME is a hint).
  """
  meta = _meta_path(data_dir, scope_rel, rel)
  if not content_type:
    try:
      meta.unlink()
    except OSError:
      pass
    return
  try:
    atomic_write(meta, json.dumps({"content_type": content_type}))
  except OSError:
    pass


def read_content_type(data_dir: str, scope_rel: Path, rel: str) -> str | None:
  """Returns the stored Content-Type for a file, or None if no sidecar.

  A missing or unparseable sidecar yields None so the caller falls back to
  the extension guess — the sidecar is an override, never a hard dependency.
  """
  meta = _meta_path(data_dir, scope_rel, rel)
  try:
    data = json.loads(meta.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return None
  ct = data.get("content_type") if isinstance(data, dict) else None
  return ct if isinstance(ct, str) and ct else None


def delete_content_type(data_dir: str, scope_rel: Path, rel: str) -> None:
  """Removes a file's sidecar, leaving no stale stored MIME behind.

  Called when the file is deleted so a later write to the SAME path with a
  different type isn't shadowed by the old sidecar. Best-effort — a missing
  sidecar is fine (a file written without an explicit type never had one).
  """
  meta = _meta_path(data_dir, scope_rel, rel)
  try:
    meta.unlink()
  except OSError:
    pass


def delete_content_type_tree(data_dir: str, scope_rel: Path, rel: str) -> None:
  """Removes the sidecar subtree mirroring a deleted storage FOLDER.

  A recursive folder delete must drop every sidecar under it, for the same
  reason a single delete drops its one sidecar — otherwise a later file at a
  path inside the recreated folder would be shadowed by a stale stored MIME.
  The meta tree mirrors the storage path, so the folder's sidecars live at
  `<meta>/<scope>/<rel>` (a directory, no `.json` suffix). Best-effort.
  """
  meta_dir = Path(data_dir) / _META_DIRNAME / scope_rel / rel
  try:
    shutil.rmtree(meta_dir)
  except OSError:
    pass


def move_content_type(
  data_dir: str, scope_rel: Path, src_rel: str, dst_rel: str
) -> None:
  """Moves a sidecar (or sidecar subtree) alongside a storage move/rename.

  Keeps the stored MIME attached to the bytes after a move: the file's
  sidecar follows to the new path so a cold read still serves the right
  type, and the old path is left with no stale sidecar to shadow a future
  write. Handles both a single file's sidecar (`<src>.json` → `<dst>.json`)
  and a folder's sidecar subtree (`<src>/` → `<dst>/`). Best-effort — the
  bytes already moved, the MIME hint must not be able to fail the move.
  """
  meta_root = Path(data_dir) / _META_DIRNAME / scope_rel
  pairs = (
    (_meta_path(data_dir, scope_rel, src_rel),
     _meta_path(data_dir, scope_rel, dst_rel)),
    (meta_root / src_rel, meta_root / dst_rel),
  )
  for src, dst in pairs:
    if not src.exists():
      continue
    try:
      dst.parent.mkdir(parents=True, exist_ok=True)
      # The data move already 409s on an existing destination (routes/storage),
      # so the meta tree should be clear too — but the meta tree is DERIVED and
      # can hold a stale sidecar dir the data side never knew about. shutil.move
      # of a dir ONTO an existing dir NESTS it (`dst/<src-name>/...`) instead of
      # renaming, which orphans every moved sidecar one level too deep and
      # silently drops the moved blobs back to extension-guess MIME. The source
      # is authoritative, so replace a stale dest subtree rather than nest into
      # it. (A file dest is overwritten by shutil.move already — only the dir
      # case nests.)
      if dst.is_dir() and not dst.is_symlink():
        shutil.rmtree(dst, ignore_errors=True)
      shutil.move(str(src), str(dst))
    except OSError:
      pass
