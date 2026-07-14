"""Capture immutable per-chat prompts from installed-app fragments.

An app opts into this privileged surface by declaring a root-level
``system_prompt`` markdown file in its manifest.  The installer stores only the
validated basename. At chat start, composition reads the bytes from live app
rows and stores the exact result as a content-addressed snapshot. Installation,
update, and soft-uninstall therefore affect chats started afterwards; an
already-started chat keeps the prompt it began with.

Möbius is single-owner software: installing an app is the trust decision.  This
module does not invent a second allowlist that can drift from the owner's app
catalog.  Fragments are instructions, so manifests and the install UI should
continue to make the declaration visible.
"""

from __future__ import annotations

import logging
import hashlib
import os
import stat
from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

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


def read_prompt_snapshot(chat: models.Chat, db: Session) -> str | None:
  """Return the immutable prompt already selected for ``chat``, if any."""
  snapshot_id = chat.system_prompt_snapshot_id
  if not snapshot_id:
    return None
  row = db.get(models.SystemPromptSnapshot, snapshot_id)
  if row is None:
    # Recomposition here would silently change an established chat's
    # instructions. Treat a broken reference as durable-state corruption.
    raise RuntimeError(
      f"missing system prompt snapshot {snapshot_id} for chat {chat.id}"
    )
  content = row.content
  if hashlib.sha256(content.encode("utf-8")).hexdigest() != snapshot_id:
    raise RuntimeError(
      f"corrupt system prompt snapshot {snapshot_id} for chat {chat.id}"
    )
  return content


def prompt_for_chat(
  chat: models.Chat,
  base: str,
  db: Session,
  *,
  persist: bool,
) -> str:
  """Return a chat-stable prompt, capturing live app fragments once.

  Empty/unstarted chats have no snapshot yet. ``persist=False`` is a read-only
  preview for observability; the first real turn passes ``persist=True`` and
  records the exact composed bytes before invoking a provider.
  """
  existing = read_prompt_snapshot(chat, db)
  if existing is not None:
    return existing
  composed = compose_system_prompt(base, db)
  if not persist:
    return composed
  digest = hashlib.sha256(composed.encode("utf-8")).hexdigest()
  if db.get(models.SystemPromptSnapshot, digest) is None:
    try:
      # Different chats can start concurrently with identical prompt bytes.
      # A SAVEPOINT contains the unique-key race without rolling back the chat
      # turn's outer transaction; the winner's row is then reused below.
      with db.begin_nested():
        db.add(models.SystemPromptSnapshot(id=digest, content=composed))
        db.flush()
    except IntegrityError:
      pass
  row = db.get(models.SystemPromptSnapshot, digest)
  if row is None or row.content != composed:
    raise RuntimeError("could not persist immutable system prompt snapshot")
  chat.system_prompt_snapshot_id = digest
  db.flush()
  return composed


def backfill_started_chat_prompt_snapshots(
  db: Session,
  base_for_chat: Callable[[models.Chat], str],
) -> int:
  """Freeze the effective prompt for chats started before this schema landed.

  New chats snapshot inside the turn transition. During the rollout only,
  established rows initially have a NULL snapshot; capturing them at boot
  prevents a later app install/uninstall from changing those existing chats
  before their next message. Empty drafts deliberately remain live previews.
  """
  run_chat_ids = {
    row[0]
    for row in db.query(models.ChatRun.chat_id).distinct().all()
  }
  rows = (
    db.query(models.Chat)
    .filter(models.Chat.system_prompt_snapshot_id.is_(None))
    .order_by(models.Chat.created_at.asc(), models.Chat.id.asc())
    .all()
  )
  captured = 0
  for chat in rows:
    started = bool(
      chat.messages
      or chat.pending_messages
      or chat.session_id
      or chat.run_status
      or chat.id in run_chat_ids
    )
    if not started:
      continue
    prompt_for_chat(chat, base_for_chat(chat), db, persist=True)
    captured += 1
  return captured
