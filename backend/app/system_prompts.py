"""Compose the base agent prompt with live installed-app fragments.

An app opts into this privileged surface by declaring a root-level
``system_prompt`` markdown file in its manifest.  The installer stores only the
validated basename; composition reads the bytes from the app's installed source
tree and, critically, selects only live rows.  Soft-uninstall therefore removes
the fragment by construction even though the app source and user data remain.

Möbius is single-owner software: installing an app is the trust decision.  This
module does not invent a second allowlist that can drift from the owner's app
catalog.  Fragments are instructions, so manifests and the install UI should
continue to make the declaration visible.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

from sqlalchemy.orm import Session

from app import models
from app.config import get_settings

log = logging.getLogger("moebius.chat")
_MAX_FRAGMENT_BYTES = 256 * 1024


def _read_safe_fragment(
  source_dir: str | None,
  basename: str | None,
) -> str | None:
  """Read one confined regular file through a pinned directory descriptor."""
  if not source_dir or not basename or not isinstance(basename, str):
    return None
  if (
    not basename.endswith(".md")
    or basename == ".md"
    or Path(basename).name != basename
    or basename.startswith(".")
    or ".." in basename
  ):
    return None
  source = Path(source_dir)
  dir_fd = None
  file_fd = None
  try:
    apps_root = (Path(get_settings().data_dir) / "apps").resolve(strict=True)
    # Installed source trees are immediate, real directories below apps/.  A
    # corrupt row or post-install symlink must not turn a prompt fragment into
    # an arbitrary host-file read.
    if source.is_symlink():
      return None
    resolved_source = source.resolve(strict=True)
    if resolved_source.parent != apps_root or not resolved_source.is_dir():
      return None
    dir_fd = os.open(
      resolved_source,
      os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    file_fd = os.open(
      basename,
      os.O_RDONLY | os.O_NOFOLLOW,
      dir_fd=dir_fd,
    )
    info = os.fstat(file_fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_FRAGMENT_BYTES:
      return None
    chunks: list[bytes] = []
    remaining = _MAX_FRAGMENT_BYTES + 1
    while remaining > 0:
      chunk = os.read(file_fd, min(65_536, remaining))
      if not chunk:
        break
      chunks.append(chunk)
      remaining -= len(chunk)
    raw = b"".join(chunks)
    if len(raw) > _MAX_FRAGMENT_BYTES:
      return None
    fragment = raw.decode("utf-8").strip()
    return fragment or None
  except (OSError, RuntimeError, UnicodeError):
    return None
  finally:
    if file_fd is not None:
      os.close(file_fd)
    if dir_fd is not None:
      os.close(dir_fd)


def compose_system_prompt(base: str, db: Session) -> str:
  """Append prompt fragments from live installed apps in stable id order.

  Missing or unreadable fragments are skipped rather than taking chat down.
  When no live app contributes a readable fragment, the return value is exactly
  ``base`` so landing the mechanism is behavior-neutral.
  """
  rows = (
    db.query(models.App)
    .filter(
      models.App.deleted_at.is_(None),
      models.App.system_app.is_(True),
      models.App.system_prompt_file.isnot(None),
    )
    .order_by(models.App.id.asc())
    .all()
  )
  fragments: list[str] = []
  for app in rows:
    fragment = _read_safe_fragment(app.source_dir, app.system_prompt_file)
    if fragment is None:
      log.warning("invalid system-prompt fragment row for app id=%s", app.id)
      continue
    source_label = str(Path(app.source_dir).resolve())
    fragments.append(
      f"<!-- installed system app: {app.slug or app.id}; "
      f"source_dir: {source_label} -->\n{fragment}"
    )
  if not fragments:
    return base
  return base.rstrip() + "\n\n" + "\n\n".join(fragments) + "\n"
