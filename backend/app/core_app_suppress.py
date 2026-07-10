"""Durable owner-suppression of re-seeded core apps.

A "core app" (memory, reflection, beat-machine) is re-installed by
``backend/scripts/install-core-apps.sh`` on every container boot. Without a
durable signal, an owner who deletes such an app sees it resurrected on the next
reboot: the seeder is not tombstone-aware, and the 7-day soft-delete tombstone
eventually TTL-purges, freeing the slug for a fresh re-create. This module
records the owner's decision as a per-slug marker file under
``<data_dir>/shared/suppressed-core-apps/<slug>`` that the boot seeder honors, so
a deleted core app stays gone until the owner brings it back. Two bring-back
paths can clear the marker: ``recover_app`` within the 7-day tombstone window,
or reinstalling from the App Store afterwards when that core app has a catalog
entry (memory IS listed in the store catalog, so this is a real owner-facing
path there — verified 2026-07-06).

File-based on purpose: the marker lives in the ``/data`` volume so it survives
reboots AND the tombstone TTL purge; the boot seeder is a shell script that can
check it with one ``[ -f ]`` test; it is inspectable by the owner/agent; and the
nightly ``/data`` safety-net commit makes it git-undoable. One file per slug
keeps concurrent mark/clear race-free (no read-modify-write on a shared JSON).

``store`` is deliberately NOT suppressible — it is the app-manager; durably
removing it would strand the owner with no way to reinstall anything (and it has
its own every-boot reinstall in ``bootstrap.ensure_store_installed`` that revives
even a tombstoned store).

Note on ``reflection`` (an accepted trade-off, owner call 2026-07-06):
suppressing its app makes the seeder skip the reflection install, which ALSO
skips re-arming its nightly cron (the cron block is gated on a non-empty app id).
That nightly run does memory-graph consolidation, not just the morning brief — so
uninstalling the Reflection app stops BOTH. That is the intended "uninstall the
feature" semantic; the memory graph the agent already built is untouched (only
the nightly consolidation pass stops).

Keying is by slug, matching the seeder's ``[ -f .../<slug> ]`` check. This
assumes the core app holds its canonical slug; if a user app already occupies
``memory`` and core Memory landed on ``memory-2``, deletion would write no marker
— a pre-existing core-vs-user slug-collision fragility, not introduced here.
"""

from pathlib import Path

from .source_dirs import CORE_APP_SLUGS
from .timeutil import now_naive_utc

# The core apps an owner may durably remove. ``store`` is excluded (the
# app-manager). ``reflection`` is included with the accepted trade-off that
# uninstalling it stops its nightly run (brief + graph consolidation) — see the
# module docstring.
SUPPRESSIBLE_CORE_SLUGS = CORE_APP_SLUGS

# Relative to data_dir. Mirrored by the `[ -f ]` check at the top of
# sync_core_app in install-core-apps.sh — keep the two paths in lockstep.
_SUPPRESS_SUBDIR = "shared/suppressed-core-apps"


def is_suppressible_core_slug(slug: str | None) -> bool:
  """True for a core-app slug an owner is allowed to durably remove."""
  return bool(slug) and slug in SUPPRESSIBLE_CORE_SLUGS


def _marker_path(data_dir, slug: str) -> Path:
  return Path(data_dir) / _SUPPRESS_SUBDIR / slug


def mark_suppressed(data_dir, slug: str, *, app_id: int | None = None) -> None:
  """Record that the owner removed core app ``slug`` so the seeder skips it.

  No-op for a non-suppressible slug — ordinary apps are never re-seeded, so they
  need no marker. Best-effort: a write failure must never block the delete it
  accompanies (the tombstone is already committed; a missing marker only means
  the seeder may re-create the app on the next boot, the pre-existing behavior).
  """
  if not is_suppressible_core_slug(slug):
    return
  try:
    path = _marker_path(data_dir, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Existence is the signal; the body is for a human/agent reading it.
    stamp = now_naive_utc().isoformat()
    id_line = f"app_id: {app_id}\n" if app_id is not None else ""
    path.write_text(
      f"suppressed_at: {stamp}\n{id_line}"
      "reason: owner uninstalled this core app; the boot seeder must not "
      "re-create it. Delete this file (or reinstall the app) to restore it.\n",
      encoding="utf-8",
    )
  except OSError:
    pass


def clear_suppressed(data_dir, slug: str) -> None:
  """Remove the suppression marker for ``slug`` — the owner brought it back.

  No-op when there is no marker (the common case for a normal recover/install).
  """
  if not slug:
    return
  try:
    _marker_path(data_dir, slug).unlink(missing_ok=True)
  except OSError:
    pass


def is_suppressed(data_dir, slug: str) -> bool:
  """True when core app ``slug`` is currently owner-suppressed."""
  return bool(slug) and _marker_path(data_dir, slug).exists()


def list_suppressed(data_dir) -> set[str]:
  """The set of currently-suppressed core-app slugs (for observability/tests)."""
  directory = Path(data_dir) / _SUPPRESS_SUBDIR
  try:
    return {p.name for p in directory.iterdir() if p.is_file()}
  except OSError:
    return set()
