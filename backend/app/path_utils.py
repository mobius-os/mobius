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
from urllib.parse import quote

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


def safe_filename(filename: str, *, fallback: str = "download") -> str:
  """Strips directory components and characters that are unsafe to echo.

  Lifted from the upload route so both file-serving surfaces sanitize the
  same way (`routes/uploads.py` keeps a thin wrapper for its own default).
  `\\w` is Unicode-aware, so a non-Latin name survives intact — see
  `attachment_disposition` for why that still needs encoding on the wire.
  """
  name = Path(filename).name
  name = re.sub(r"[^\w.\-]", "_", name)
  if not name or name.startswith("."):
    name = fallback
  return name


def attachment_disposition(filename: str) -> str:
  """Builds an RFC 6266 `Content-Disposition` for a forced download.

  A bare `filename="…"` is latin-1 on the wire, so a name the agent chose in
  the owner's own language (`报告.pdf`, an emoji) raises UnicodeEncodeError
  inside the ASGI layer and 500s the download. Emit an ASCII fallback for old
  clients plus `filename*=UTF-8''…`, which every current browser prefers.
  """
  sanitized = safe_filename(filename)
  ascii_name = sanitized.encode("ascii", "replace").decode("ascii")
  # `"` and `\` would otherwise terminate or escape the quoted-string.
  ascii_name = ascii_name.replace("\\", "_").replace('"', "_")
  encoded = quote(sanitized, safe="")
  return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"
