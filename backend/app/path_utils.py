"""Filesystem identifier and path validation shared by serving routes.

The three caller routes used to each implement this check with subtly
different techniques (`str().startswith()`, string concatenation,
`is_relative_to()`). That meant a fix for one edge case (e.g. symlink
escape) had to be propagated three times, and inconsistencies in
posture were silent. Centralizing here unifies the check on
`is_relative_to()` — the most correct of the three approaches because
it operates on resolved Path objects and survives symlink escapes that
prefix-string checks miss.
"""

import re
from pathlib import Path

from fastapi import HTTPException

_CHAT_ID_RE = re.compile(
  r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
  re.IGNORECASE,
)


def validate_chat_id(chat_id: str) -> None:
  """Raise HTTP 400 unless ``chat_id`` is a dashed UUID4 string."""
  if not _CHAT_ID_RE.match(chat_id):
    raise HTTPException(status_code=400, detail="Invalid chat id.")


def validate_path_within_base(path: Path | str, base: Path) -> Path:
  """Resolves `path` joined under `base` and asserts containment.

  Returns the resolved absolute Path. Raises HTTPException(400) when
  the resolved path escapes `base` via symlinks, `..` components, or
  absolute-path injection (`Path("/abs")` joined under a base resolves
  to `/abs`, not `base/abs`, and the containment check catches it).

  Args:
    path: User-supplied relative path or Path. May be a string.
    base: Directory the resolved path must live within.

  Returns:
    The resolved absolute Path.

  Raises:
    HTTPException: 400 when the resolved path escapes `base`.
  """
  p = Path(path) if isinstance(path, str) else path
  resolved = (base / p).resolve()
  if not resolved.is_relative_to(base.resolve()):
    raise HTTPException(status_code=400, detail="Invalid path.")
  return resolved
