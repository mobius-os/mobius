"""Fast, bounded measurement of one installed app's allocated storage."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def _allocated_tree_bytes(path: Path, seen: set[tuple[int, int]]) -> int:
  total = 0
  stack = [path]
  while stack:
    current = stack.pop()
    try:
      info = current.lstat()
    except OSError:
      continue
    identity = (info.st_dev, info.st_ino)
    if identity in seen:
      continue
    seen.add(identity)
    total += int(getattr(info, "st_blocks", 0)) * 512
    if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
      continue
    try:
      with os.scandir(current) as entries:
        stack.extend(Path(entry.path) for entry in entries)
    except OSError:
      continue
  return total


def _owned_path(root: Path, candidate: str, *, lane: str) -> Path | None:
  try:
    path = Path(candidate)
    if not path.is_absolute():
      return None
    resolved = path.resolve(strict=False)
    parent = (root / lane).resolve()
    if resolved.parent != parent:
      return None
    if lane == "apps" and resolved.name.isdigit():
      return None
    return path
  except (OSError, RuntimeError, TypeError, ValueError):
    return None


def app_footprint_bytes(data_dir: str | Path, app: dict) -> int:
  """Measure only the storage lanes owned by one app, without following links."""
  root = Path(data_dir)
  try:
    app_id = int(app["id"])
  except (KeyError, TypeError, ValueError):
    return 0
  paths = [root / "apps" / str(app_id), root / "app-secrets" / str(app_id)]
  source = _owned_path(root, str(app.get("source_dir") or ""), lane="apps")
  compiled = _owned_path(
    root, str(app.get("compiled_path") or ""), lane="compiled",
  )
  if source is not None:
    paths.append(source)
  if compiled is not None:
    paths.append(compiled)
  seen: set[tuple[int, int]] = set()
  return sum(_allocated_tree_bytes(path, seen) for path in paths)
