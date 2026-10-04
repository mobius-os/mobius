"""Compatibility levels for one-way storage steps.

See ONE_WAY_UPGRADES_DESIGN.md. Two integers describe this source:

- ``COMPAT_LEVEL`` is the highest one-way step this code understands. The
  database records a *floor*; code whose level is below it must not serve.
- ``REQUIRED_IMAGE_LEVEL`` is the highest step this code registers. The running
  image's baked fallback (``/app/platform-baked``) must understand at least
  that much, or a later failure of this source would fall back to code that
  cannot read the data. This module refuses such a pairing at import time,
  which is exactly what the entrypoint's import probe exercises, so the
  entrypoint takes its existing revert/baked-fallback path before anything
  opens the database.

Raise both only in a release that registers a new one-way step. That release
must also advance ``backend/runtime/one_way_capability.json``, whose image-owned
path makes every updater ship it together with its matching image.

``app.main`` imports this module before anything that can open the database,
so it uses only the standard library.
"""

from __future__ import annotations

import ast
import logging
import os
from pathlib import Path

COMPAT_LEVEL = 1
REQUIRED_IMAGE_LEVEL = 1

BAKED_ROOT = Path("/app/platform-baked")
BAKED_COMPAT_PATH = BAKED_ROOT / "backend" / "app" / "compat.py"

# Set only by the update validators, in the child process that imports a
# reviewed image-requiring candidate on the old image. The entrypoint scrubs
# both before every probe and before uvicorn; serve-time startup re-checks
# without them (``serve_time_image_verdict``).
CANDIDATE_MARKER_ENV = "MOBIUS_CANDIDATE_VALIDATION"
CANDIDATE_LEVEL_ENV = "MOBIUS_CANDIDATE_IMAGE_LEVEL"
# Honoured only inside the isolated test runtime (wt-pytest sets both flags).
TEST_LEVEL_ENV = "MOBIUS_TEST_IMAGE_LEVEL"
# Honoured only when there is no baked checkout at all (development outside
# the image).
DEV_LEVEL_ENV = "MOBIUS_DEV_IMAGE_LEVEL"

log = logging.getLogger(__name__)


class ImageBelowSourceError(RuntimeError):
  """This source needs a newer image than the one it is running on."""


def _checked_level(value: object, name: str) -> int:
  # bool is an int subclass; a level must be a literal non-negative integer.
  if type(value) is not int or value < 0:
    raise ValueError(f"{name} must be a non-negative integer, got {value!r}")
  return value


def declared_level(source: str, name: str = "COMPAT_LEVEL") -> int | None:
  """Parse ``NAME = <int>`` from compat.py source text without importing it.

  Returns None when the name is not assigned at module level; raises
  ValueError when it is assigned anything but one literal non-negative
  integer. Python keeps the last of several assignments, so a duplicate is
  ambiguous and rejected rather than read as the first.
  """
  found: list[int] = []
  for node in ast.parse(source).body:
    if isinstance(node, ast.Assign):
      targets, value = node.targets, node.value
    elif isinstance(node, ast.AnnAssign) and node.value is not None:
      targets, value = [node.target], node.value
    else:
      continue
    if any(isinstance(t, ast.Name) and t.id == name for t in targets):
      if not isinstance(value, ast.Constant):
        raise ValueError(f"{name} must be a literal integer")
      found.append(_checked_level(value.value, name))
  if len(found) > 1:
    raise ValueError(f"{name} is assigned more than once")
  return found[0] if found else None


def baked_image_level(path: Path = BAKED_COMPAT_PATH) -> int:
  """The baked fallback's COMPAT_LEVEL; 0 for a baked tree that predates it.

  An unreadable or malformed file counts as 0: the lowest level is the one
  that can only make the check stricter.
  """
  try:
    level = declared_level(path.read_text(encoding="utf-8"))
  except FileNotFoundError:
    return 0
  except (OSError, SyntaxError, ValueError, UnicodeDecodeError) as exc:
    log.warning("baked compat level unreadable (%s); treating it as 0", exc)
    return 0
  return 0 if level is None else level


def _env_level(name: str) -> int | None:
  raw = os.environ.get(name)
  if raw is None:
    return None
  if not raw.isascii() or not raw.isdigit():
    raise ImageBelowSourceError(f"{name} must be a non-negative integer, got {raw!r}")
  return int(raw)


def _isolated_test_runtime() -> bool:
  return (
    os.environ.get("MOBIUS_TEST_RUNTIME") == "1"
    and os.environ.get("MOBIUS_TEST_DATABASE_ISOLATED") == "1"
  )


def image_level(*, allow_candidate: bool, baked_root: Path = BAKED_ROOT) -> tuple[int, str]:
  """Return the level the running image provides, and where it came from."""
  if allow_candidate and os.environ.get(CANDIDATE_MARKER_ENV) == "1":
    candidate = _env_level(CANDIDATE_LEVEL_ENV)
    if candidate is not None:
      return candidate, "candidate"
  if _isolated_test_runtime():
    test_level = _env_level(TEST_LEVEL_ENV)
    # Tests run inside an arbitrary container; unless a test pins a level,
    # assume the image matches the source under test.
    return (COMPAT_LEVEL if test_level is None else test_level), "test"
  if baked_root.is_dir():
    return baked_image_level(baked_root / "backend" / "app" / "compat.py"), "baked"
  dev_level = _env_level(DEV_LEVEL_ENV)
  if dev_level is not None:
    return dev_level, "dev"
  return 0, "absent"


def check_declarations() -> None:
  _checked_level(COMPAT_LEVEL, "COMPAT_LEVEL")
  _checked_level(REQUIRED_IMAGE_LEVEL, "REQUIRED_IMAGE_LEVEL")
  if COMPAT_LEVEL < REQUIRED_IMAGE_LEVEL:
    raise ValueError("COMPAT_LEVEL must be at least REQUIRED_IMAGE_LEVEL")


def _refusal(level: int, source: str) -> str:
  hint = ""
  if source == "absent":
    hint = f" Outside the image, set {DEV_LEVEL_ENV} to the level you are testing."
  return (
    f"This Möbius source needs an image at compatibility level "
    f"{REQUIRED_IMAGE_LEVEL}, but the {source} level is {level}. Finish the "
    f"image update, or return to the previous source.{hint}"
  )


def assert_image_supports_source() -> None:
  """Import-time check: refuse this source on an image that is too old."""
  check_declarations()
  level, source = image_level(allow_candidate=True)
  if level < REQUIRED_IMAGE_LEVEL:
    raise ImageBelowSourceError(_refusal(level, source))


def serve_time_image_verdict() -> dict | None:
  """Re-check before the database opens, ignoring candidate overrides.

  Returns a degraded-boot detail when the serving process runs on an image
  that is too old, otherwise None. A served boot must never inherit the
  validators' candidate level; seeing it here means something leaked it.
  """
  leaked = [name for name in (CANDIDATE_MARKER_ENV, CANDIDATE_LEVEL_ENV) if name in os.environ]
  if leaked:
    log.critical("ignoring update-validation variables in a served boot: %s", ", ".join(leaked))
  try:
    level, source = image_level(allow_candidate=False)
  except ImageBelowSourceError as exc:
    return {"required_image_level": REQUIRED_IMAGE_LEVEL, "message": str(exc)}
  if level < REQUIRED_IMAGE_LEVEL:
    return {
      "required_image_level": REQUIRED_IMAGE_LEVEL,
      "image_level": level,
      "image_level_source": source,
      "message": _refusal(level, source),
    }
  return None
