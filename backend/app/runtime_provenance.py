"""Prove that the protected image runtime matches the served source tree.

``/data/platform`` owns the desired platform generation, while ``/app/runtime``
contains the root-started broker modules that only an image replacement can
activate.  Git ancestry and BUILD_SHA prove the two generations independently;
neither proves that these protected bytes agree.  This module provides that
missing, read-only comparison for version diagnostics, Settings activation
status, and deployment cutover checks.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from typing import Literal, TypedDict


RuntimeParityState = Literal["current", "stale", "unavailable"]


class RuntimeParity(TypedDict):
  """Serializable protected-runtime parity result."""

  state: RuntimeParityState
  source_sha256: str | None
  deployed_sha256: str | None
  mismatched_paths: list[str]


class _TreeSnapshot(TypedDict):
  digest: str
  files: dict[str, str]
  invalid_paths: list[str]


def _deployed_root() -> Path:
  return Path(os.environ.get("MOBIUS_PROTECTED_RUNTIME_DIR", "/app/runtime"))


def _ignored(relative: Path) -> bool:
  return "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}


def _hash_regular_file(path: Path) -> str:
  """Hash one final regular file without following a last-component link."""
  flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
  fd = os.open(path, flags)
  try:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
      raise OSError(f"not a regular file: {path}")
    digest = hashlib.sha256()
    while chunk := os.read(fd, 1024 * 1024):
      digest.update(chunk)
    return digest.hexdigest()
  finally:
    os.close(fd)


def _snapshot(root: Path) -> _TreeSnapshot:
  """Hash a tree without following links or including Python bytecode."""
  if root.is_symlink() or not root.is_dir():
    raise FileNotFoundError(root)

  files: dict[str, str] = {}
  invalid: list[str] = []
  for path in sorted(root.rglob("*")):
    relative = path.relative_to(root)
    if _ignored(relative):
      continue
    name = relative.as_posix()
    if path.is_symlink():
      invalid.append(name)
      continue
    if path.is_dir():
      continue
    if not path.is_file():
      invalid.append(name)
      continue
    files[name] = _hash_regular_file(path)

  tree = hashlib.sha256()
  for name, digest in sorted(files.items()):
    tree.update(name.encode("utf-8"))
    tree.update(b"\0")
    tree.update(digest.encode("ascii"))
    tree.update(b"\n")
  for name in sorted(invalid):
    tree.update(b"invalid\0")
    tree.update(name.encode("utf-8"))
    tree.update(b"\n")
  return {
    "digest": tree.hexdigest(),
    "files": files,
    "invalid_paths": sorted(invalid),
  }


def protected_runtime_status(
  source_root: Path,
  deployed_root: Path | None = None,
) -> RuntimeParity:
  """Compare desired and deployed protected trees; never raise.

  ``unavailable`` means the desired source tree itself could not be read, so
  no deployment conclusion is possible.  A missing/unreadable deployed tree is
  ``stale`` when the source is available: replacement status must not silently
  report current merely because the protected copy disappeared.
  """
  source: _TreeSnapshot
  try:
    source = _snapshot(source_root)
  except (OSError, UnicodeError):
    return {
      "state": "unavailable",
      "source_sha256": None,
      "deployed_sha256": None,
      "mismatched_paths": [],
    }

  target_root = deployed_root if deployed_root is not None else _deployed_root()
  try:
    deployed = _snapshot(target_root)
  except (OSError, UnicodeError):
    return {
      "state": "stale",
      "source_sha256": source["digest"],
      "deployed_sha256": None,
      "mismatched_paths": sorted({
        *source["files"], *source["invalid_paths"],
      }),
    }

  mismatches = {
    name
    for name in set(source["files"]) | set(deployed["files"])
    if source["files"].get(name) != deployed["files"].get(name)
  }
  mismatches.update(source["invalid_paths"])
  mismatches.update(deployed["invalid_paths"])
  return {
    "state": "stale" if mismatches else "current",
    "source_sha256": source["digest"],
    "deployed_sha256": deployed["digest"],
    "mismatched_paths": sorted(mismatches),
  }


def activation_paths(status: RuntimeParity) -> list[str]:
  """Translate a stale parity result into canonical platform source paths."""
  if status["state"] != "stale":
    return []
  if not status["mismatched_paths"]:
    return ["backend/runtime"]
  return [f"backend/runtime/{path}" for path in status["mismatched_paths"]]
