"""Stable app names used by source trees and standalone installs."""

from pathlib import Path

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app import models, source_dirs


def slugify_for_source_dir(name: str) -> str:
  """Historical source/storage slug shape used by legacy-row backfill.
  Lowercase, alphanum + hyphen, collapsed runs, stripped."""
  slug = "".join(
    ch if ch.isalnum() else "-" for ch in (name or "").lower()
  ).strip("-")
  while "--" in slug:
    slug = slug.replace("--", "-")
  slug = slug or "app"
  # A purely-numeric slug would collide with the numeric-id storage tree:
  # an app named "123" derives source dir /data/apps/123, which is exactly
  # where /api/storage/apps/123/... writes land for app id 123. Prefix it
  # so a source-dir name is never a bare integer.
  if slug.isdigit():
    slug = f"app-{slug}"
  return slug


def allocate_unique_slug(db: Session, name: str, exclude_id: int | None = None) -> str:
  """Returns a slug that isn't taken by any other App row.

  Starts from the name's slug; if it collides, appends -2, -3, ...
  until a free one is found. `exclude_id` lets callers re-allocate
  for an existing row without colliding with itself (e.g. backfill).
  Slugs pin standalone-install identity (manifest `id`) — keep them
  stable across renames so home-screen icons don't orphan.

  Deliberately scans ALL rows including tombstoned (deleted_at IS NOT NULL)
  ones: a soft-deleted app holds its slug until the TTL purge so a
  reinstall-reattach (which revives the SAME slug) can't be blocked by a new
  allocation in the recovery window. Do NOT add a deleted_at filter here — it
  would break that invariant (feature 110).
  """
  base = slugify_for_source_dir(name)
  candidate = base
  suffix = 2
  while True:
    q = db.query(models.App).filter(models.App.slug == candidate)
    if exclude_id is not None:
      q = q.filter(models.App.id != exclude_id)
    if q.first() is None:
      return candidate
    candidate = f"{base}-{suffix}"
    suffix += 1


def validate_source_dir(source_dir: str, data_dir: str) -> str:
  """Validates a caller-supplied source_dir, returning its resolved path.

  App source must be an IMMEDIATE non-numeric child of /data/apps. Everything
  else is rejected so source_dir cannot point job runners, compilers, or
  uninstall cleanup at arbitrary paths.

  Raises 400 on either violation. `.resolve()` collapses symlinks and `..`
  before the containment check.
  """
  # resolve() can raise on a pathological path (e.g. a symlink loop). Surface
  # that as a clean 400, not a 500 (Codex review round-7 #3 robustness caveat).
  try:
    resolved = Path(source_dir).resolve()
  except (OSError, RuntimeError):
    raise HTTPException(status_code=400, detail="Invalid source_dir.")

  kind = source_dirs.source_dir_kind(resolved, data_dir)
  if kind == "app":
    return str(resolved)
  apps_root = source_dirs.apps_root(data_dir)
  core_root = source_dirs.platform_core_root(data_dir)
  if resolved.parent == apps_root and resolved.name.isdigit():
    raise HTTPException(
      status_code=400,
      detail=(
        "source_dir basename must not be purely numeric — bare integers "
        "are reserved for the per-app storage path /data/apps/<id>."
      ),
    )
  if resolved.parent == core_root:
    raise HTTPException(
      status_code=400,
      detail="platform core source_dir is no longer an app source root.",
    )
  raise HTTPException(
    status_code=400,
    detail=(
      "source_dir must be an immediate non-numeric child of /data/apps."
    ),
  )


def reject_if_source_dir_taken(
  db: Session, source_dir: str, exclude_id: int | None
) -> None:
  """Reject (409) if another app already claims this source dir.

  The caller holds ``fs_locks.source_dir_lock(source_dir)``, so the check +
  the subsequent assignment are atomic against a concurrent first apply.
  Two apps sharing one source tree makes explicit application ambiguous and
  uninstall cleanup conservative (it must refuse to rmtree a shared dir), so
  forbid the duplicate at assignment time. Compared
  on RESOLVED paths so a symlinked/relative spelling can't smuggle a duplicate.
  """
  try:
    resolved = Path(source_dir).resolve()
  except (OSError, RuntimeError):
    return  # a pathological path is rejected by _validate_source_dir already
  query = db.query(models.App).filter(models.App.source_dir.isnot(None))
  if exclude_id is not None:
    query = query.filter(models.App.id != exclude_id)
  for other in query.all():
    try:
      other_resolved = Path(other.source_dir).resolve()
    except (OSError, RuntimeError):
      continue
    if other_resolved == resolved:
      raise HTTPException(
        status_code=409,
        detail="source_dir is already used by another app.",
      )
