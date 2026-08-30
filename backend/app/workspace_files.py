"""Confined, bounded reads shared by Project and app-source workspaces."""

from __future__ import annotations

import hashlib
import mimetypes
from collections import deque
from datetime import datetime
from pathlib import Path


READ_MAX = 10 * 1024 * 1024
LIST_LIMIT = 1000
# Directory contents are owner data and can be adversarially large.  Bound the
# number of filesystem entries one request will inspect as well as the number
# it returns; `truncated` tells the caller when either ceiling was reached.
LIST_SCAN_LIMIT = 10_000
# Git repositories may store their control data as either a directory or a
# gitfile (for example in linked worktrees and submodules). Workspaces expose
# source files, never repository metadata, so reserve the name independent of
# the on-disk entry type.
GIT_METADATA_NAMES = frozenset({".git"})
TEXT_MEDIA_TYPES = frozenset({
  "application/json",
  "application/javascript",
  "application/x-latex",
  "application/x-tex",
  "image/svg+xml",
})
TEXT_SUFFIXES = frozenset({
  ".bash", ".c", ".cc", ".conf", ".cpp", ".css", ".csv", ".env",
  ".go", ".h", ".hpp", ".htm", ".html", ".ini", ".java", ".js",
  ".json", ".jsx", ".log", ".md", ".mjs", ".py", ".rs", ".sh",
  ".sql", ".svg", ".tex", ".toml", ".ts", ".tsx", ".txt", ".xml",
  ".yaml", ".yml",
})


class InvalidWorkspacePath(ValueError):
  """The requested relative path escaped or was malformed."""


class UnavailableWorkspacePath(PermissionError):
  """The path crosses a symlink or an intentionally hidden directory."""


def resolve_path(
  root: Path,
  path: str,
  *,
  hidden_dirs: frozenset[str] = frozenset(),
) -> Path:
  """Resolve one relative path without exposing links or hidden internals."""
  if "\x00" in (path or ""):
    raise InvalidWorkspacePath("Invalid path.")
  root = root.resolve()
  relative = Path((path or "").lstrip("/"))
  if relative.is_absolute() or any(part == ".." for part in relative.parts):
    raise InvalidWorkspacePath("Invalid path.")
  if any(part in hidden_dirs for part in relative.parts):
    raise UnavailableWorkspacePath("Path is not available in this workspace.")
  candidate = root / relative
  cursor = root
  for part in relative.parts:
    cursor = cursor / part
    if cursor.is_symlink():
      raise UnavailableWorkspacePath(
        "Symbolic links are not available in this workspace.",
      )
  try:
    target = candidate.resolve()
    target.relative_to(root)
  except (OSError, RuntimeError, ValueError) as exc:
    raise InvalidWorkspacePath("Invalid path.") from exc
  return target


def _entry(root: Path, child: Path) -> dict | None:
  try:
    stat = child.stat()
  except OSError:
    return None
  directory = child.is_dir()
  return {
    "name": child.name,
    "path": child.relative_to(root).as_posix(),
    "type": "directory" if directory else "file",
    "size": 0 if directory else stat.st_size,
    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
    "mime_type": None if directory else mimetypes.guess_type(child.name)[0],
  }


def list_entries(
  root: Path,
  directory: Path,
  *,
  path: str = "",
  recursive: bool = False,
  hidden_dirs: frozenset[str] = frozenset(),
  hidden_root_dirs: frozenset[str] = frozenset(),
) -> dict:
  """List a directory in stable folder-first order with a shared hard cap."""
  root = root.resolve()
  if not directory.exists():
    return {"path": path, "entries": [], "truncated": False}
  if not directory.is_dir():
    raise NotADirectoryError("Path is not a directory.")

  scanned = 0

  def children(folder: Path) -> tuple[list[Path], bool]:
    nonlocal scanned
    values: list[Path] = []
    hit_scan_limit = False
    try:
      for value in folder.iterdir():
        if scanned >= LIST_SCAN_LIMIT:
          hit_scan_limit = True
          break
        scanned += 1
        values.append(value)
      return sorted(
        values,
        key=lambda value: (not value.is_dir(), value.name.lower()),
      ), hit_scan_limit
    except OSError:
      return [], hit_scan_limit

  def excluded(child: Path) -> bool:
    if child.is_symlink():
      return True
    if child.name in hidden_dirs:
      return True
    return (
      child.is_dir()
      and child.name in hidden_root_dirs
      and child.parent.resolve() == root
    )

  entries: list[dict] = []
  truncated = False
  if recursive:
    queue = deque([directory])
    while queue and not truncated:
      folder = queue.popleft()
      folder_children, hit_scan_limit = children(folder)
      for child in folder_children:
        if excluded(child):
          continue
        if child.is_dir():
          queue.append(child)
          continue
        if len(entries) >= LIST_LIMIT:
          truncated = True
          break
        row = _entry(root, child)
        if row is not None:
          entries.append(row)
      if hit_scan_limit:
        truncated = True
  else:
    folder_children, hit_scan_limit = children(directory)
    for child in folder_children:
      if excluded(child):
        continue
      if len(entries) >= LIST_LIMIT:
        truncated = True
        break
      row = _entry(root, child)
      if row is not None:
        entries.append(row)
    truncated = truncated or hit_scan_limit
  return {"path": path, "entries": entries, "truncated": truncated}


def read_file(target: Path, path: str) -> tuple[dict | None, str]:
  """Return a UTF-8 payload when possible, otherwise its media type."""
  if not target.is_file():
    raise FileNotFoundError(path)
  if target.stat().st_size > READ_MAX:
    raise OverflowError("File is too large to open in this workspace.")
  media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
  if (
    media_type.startswith("text/")
    or media_type in TEXT_MEDIA_TYPES
    or target.suffix.lower() in TEXT_SUFFIXES
  ):
    try:
      with target.open("rb") as handle:
        raw = handle.read(READ_MAX + 1)
      if len(raw) > READ_MAX:
        raise OverflowError("File is too large to open in this workspace.")
      return {
        "path": path,
        "content": raw.decode("utf-8"),
        "mime_type": media_type,
        "revision": hashlib.sha256(raw).hexdigest(),
      }, media_type
    except UnicodeDecodeError:
      pass
  return None, media_type


def file_revision(target: Path) -> str | None:
  """Content identity for one regular file, or ``None`` when it is absent."""
  if not target.is_file():
    return None
  digest = hashlib.sha256()
  with target.open("rb") as handle:
    for chunk in iter(lambda: handle.read(128 * 1024), b""):
      digest.update(chunk)
  return digest.hexdigest()
