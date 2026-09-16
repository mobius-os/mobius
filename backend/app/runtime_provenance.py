"""Prove that the image-owned protected runtime matches the served source tree.

``/data/platform`` owns the desired platform generation, while ``/app/runtime``
contains the root-started modules that only an image replacement can activate.
Git ancestry and BUILD_SHA prove the two generations independently; neither
proves that these protected bytes agree.  This module provides that missing,
read-only comparison for version diagnostics, Settings activation status, and
deployment cutover checks.

Only image-owned modules are compared.  A module the frozen launcher starts
from the served checkout is authoritative there, so its served and image copies
are allowed to differ; treating that as a mismatch would report a permanent,
meaningless "stale".
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal, TypedDict

from app import platform_activation


RuntimeParityState = Literal["current", "stale", "unavailable"]

# Protected-runtime modules the frozen launcher starts from the served checkout.
# Keep in sync with ``SERVED_MODULES`` in
# ``backend/runtime/served_runtime_launcher.py``.
SERVED_RUNTIME_MODULES = ("identity_broker.py",)

# The served/image ``BROKER_ROUTE_EPOCH`` marker, read from bytes without
# importing the privileged module. Mirrors the launcher's own gate.
_ROUTE_EPOCH_RE = re.compile(rb"^BROKER_ROUTE_EPOCH\s*=\s*(\d+)", re.MULTILINE)

ServedRuntimeState = Literal["behind", "current", "unavailable"]


class ServedRuntimeModule(TypedDict):
  """Route-epoch comparison for one served protected-runtime module."""

  module: str
  state: ServedRuntimeState
  served_epoch: int | None
  image_epoch: int | None


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


def _image_owned(relative: Path) -> bool:
  """Whether this runtime module must still come verbatim from the image."""
  return platform_activation.runtime_module_is_image_owned(relative.as_posix())


def _snapshot(root: Path, *, include: Callable[[Path], bool]) -> _TreeSnapshot:
  """Hash a tree without following links or including Python bytecode."""
  if not root.is_dir():
    raise FileNotFoundError(root)

  files: dict[str, str] = {}
  invalid: list[str] = []
  for path in sorted(root.rglob("*")):
    relative = path.relative_to(root)
    if _ignored(relative) or not include(relative):
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
    files[name] = hashlib.sha256(path.read_bytes()).hexdigest()

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
    source = _snapshot(source_root, include=_image_owned)
  except (OSError, UnicodeError):
    return {
      "state": "unavailable",
      "source_sha256": None,
      "deployed_sha256": None,
      "mismatched_paths": [],
    }

  target_root = deployed_root if deployed_root is not None else _deployed_root()
  try:
    deployed = _snapshot(target_root, include=_image_owned)
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


def _module_route_epoch(path: Path) -> int | None:
  """Route epoch declared by a runtime module, or None when absent/unreadable."""
  try:
    source = path.read_bytes()
  except OSError:
    return None
  match = _ROUTE_EPOCH_RE.search(source)
  return int(match.group(1)) if match else None


def served_runtime_status(
  source_root: Path,
  deployed_root: Path | None = None,
) -> list[ServedRuntimeModule]:
  """Report whether each served protected-runtime module is behind the image.

  Unlike ``protected_runtime_status`` (which excludes these launcher-started
  modules because their bytes may legitimately differ), this compares only the
  declared route epoch, so a served broker that predates a route the running app
  needs surfaces as ``behind`` instead of a misleading ``current``. ``image_epoch``
  missing means there is nothing to prove against — ``unavailable``.
  """
  image_root = deployed_root if deployed_root is not None else _deployed_root()
  results: list[ServedRuntimeModule] = []
  for name in SERVED_RUNTIME_MODULES:
    served_epoch = _module_route_epoch(Path(source_root) / name)
    image_epoch = _module_route_epoch(image_root / name)
    if image_epoch is None:
      state: ServedRuntimeState = "unavailable"
    elif served_epoch is None or served_epoch < image_epoch:
      state = "behind"
    else:
      state = "current"
    results.append({
      "module": name,
      "state": state,
      "served_epoch": served_epoch,
      "image_epoch": image_epoch,
    })
  return results


def activation_paths(status: RuntimeParity) -> list[str]:
  """Translate a stale parity result into canonical platform source paths."""
  if status["state"] != "stale":
    return []
  if not status["mismatched_paths"]:
    return ["backend/runtime"]
  return [f"backend/runtime/{path}" for path in status["mismatched_paths"]]
