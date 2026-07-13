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
from pathlib import Path

from sqlalchemy.orm import Session

from app import models

log = logging.getLogger("moebius.chat")
_MAX_FRAGMENT_BYTES = 256 * 1024


def _safe_fragment_path(source_dir: str | None, basename: str | None) -> Path | None:
  """Return a confined root-level markdown path, tolerating legacy/corrupt rows."""
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
  return Path(source_dir) / basename


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
      models.App.system_prompt_file.isnot(None),
    )
    .order_by(models.App.id.asc())
    .all()
  )
  fragments: list[str] = []
  for app in rows:
    path = _safe_fragment_path(app.source_dir, app.system_prompt_file)
    if path is None:
      log.warning("invalid system-prompt fragment row for app id=%s", app.id)
      continue
    try:
      raw = path.read_bytes()
      if len(raw) > _MAX_FRAGMENT_BYTES:
        log.warning("system-prompt fragment too large for app id=%s", app.id)
        continue
      fragment = raw.decode("utf-8").strip()
    except (OSError, UnicodeError) as exc:
      log.warning(
        "could not read system-prompt fragment for app id=%s: %r", app.id, exc
      )
      continue
    if not fragment:
      continue
    fragments.append(
      f"<!-- installed system app: {app.slug or app.id} -->\n{fragment}"
    )
  if not fragments:
    return base
  return base.rstrip() + "\n\n" + "\n\n".join(fragments) + "\n"
