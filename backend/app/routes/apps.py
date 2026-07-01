"""Routes for managing the mini-app registry."""

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, UTC
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import activity, app_git, fs_locks, icon_cache, models, schemas, theme
from app.storage_io import delete_content_type_tree, read_capped_body
from app.broadcast import get_system_broadcast
from app.routes.notify import publish_app_built_to_owning_chat
from app.compiler import compile_jsx, recompile_app_bundle
from app.config import get_settings
from app.database import get_db
from app.deps import (
  get_current_owner, get_current_owner_or_app, get_principal, Principal,
  get_owner_or_app_with_manage_apps, reject_cross_site, resolve_owner_or_app,
)
from app.http_caching import strip_range
from app.resource_access import live_app, live_app_or_404
from app.timeutil import now_naive_utc, SOFT_DELETE_TTL

router = APIRouter(prefix="/api/apps", tags=["apps"])

# Tombstoned apps are hard-purged this long after uninstall. Aliases the single
# shared SOFT_DELETE_TTL (app.timeutil) — the same window chat soft-delete uses,
# so the two recovery windows can't drift. The agent recovers within the window
# by reinstalling (store apps) or POST /{id}/recover (any app). See feature 110.
APP_SOFT_DELETE_TTL = SOFT_DELETE_TTL

log = logging.getLogger("mobius.apps")


def _slugify_for_source_dir(name: str) -> str:
  """Same slug shape register_app.py / the storage layout uses.
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


def _derive_source_dir(data_dir: str, name: str) -> str:
  """Default source_dir when a caller doesn't provide one.
  Mirrors register_app.py's `/data/apps/<slug>/` convention so the
  watcher's exact-match lookup always finds the app."""
  return str(Path(data_dir) / "apps" / _slugify_for_source_dir(name))


def _validate_source_dir(source_dir: str, data_dir: str) -> str:
  """Validates a caller-supplied source_dir, returning its resolved path.

  A source dir must be an IMMEDIATE child of /data/apps with a non-numeric
  basename — the exact shape every production source dir already has (built
  apps and installs both land at /data/apps/<slug>; register_app passes the
  dirname of a jsx file the agent wrote under /data/apps/<slug>). Enforcing it
  here is free defense-in-depth on the create/patch/register_app inputs, which
  arrive verbatim:
    - `resolved.parent != apps_root` rejects traversal and arbitrary
      locations (`/data/apps/../etc` resolves to /etc, whose parent isn't
      apps_root) so source_dir can't point the run-job script runner or the
      uninstall rmtree at a path outside the app source tree, and
    - a purely-numeric basename would collide with the per-app STORAGE tree
      /data/apps/<id> (storage is keyed by the integer app id): a write to
      that app's storage could clobber this app's source, and uninstall's
      rmtree could delete the other app's storage tree.
  Raises 400 on either violation. `.resolve()` collapses symlinks and `..`
  before the containment check.
  """
  apps_root = (Path(data_dir) / "apps").resolve()
  # resolve() can raise on a pathological path (e.g. a symlink loop). Surface
  # that as a clean 400, not a 500 (Codex review round-7 #3 robustness caveat).
  try:
    resolved = Path(source_dir).resolve()
  except (OSError, RuntimeError):
    raise HTTPException(status_code=400, detail="Invalid source_dir.")
  if resolved.parent != apps_root:
    raise HTTPException(
      status_code=400,
      detail="source_dir must be an immediate child of /data/apps.",
    )
  if resolved.name.isdigit():
    raise HTTPException(
      status_code=400,
      detail=(
        "source_dir basename must not be purely numeric — bare integers "
        "are reserved for the per-app storage path /data/apps/<id>."
      ),
    )
  return str(resolved)


def _reject_if_source_dir_taken(
  db: Session, source_dir: str, exclude_id: int | None
) -> None:
  """Reject (409) if another app already claims this source dir.

  The caller holds ``fs_locks.source_dir_lock(source_dir)``, so the check +
  the subsequent assignment are atomic against a concurrent create/patch.
  Two apps sharing one source tree is ambiguous for the file watcher and makes
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


def _safe_to_rmtree_source(
  resolved: Path, apps_root: Path, db: Session, exclude_id: int
) -> bool:
  """Whether uninstall may recursively delete this resolved source dir.

  Only an IMMEDIATE, non-numeric child of /data/apps that NO OTHER app row
  still resolves to. Refuses to delete:
    - a nested descendant (parent != apps_root) — a legacy/invalid row whose
      source_dir points deep into /data/apps could otherwise rmtree a path
      inside another app's tree,
    - a /data/apps/<integer> per-app storage tree, and
    - a directory a SIBLING app row shares — removing it when one app is
      uninstalled would break the other.
  Production source dirs are always a unique /data/apps/<slug>, so this only
  ever stops cleanup for pathological legacy rows.
  """
  if resolved.parent != apps_root or resolved.name.isdigit():
    return False
  others = (
    db.query(models.App)
    .filter(models.App.id != exclude_id, models.App.source_dir.isnot(None))
    .all()
  )
  for other in others:
    try:
      if Path(other.source_dir).resolve() == resolved:
        return False
    except OSError:
      continue
  return True


def _drop_cron_and_rmtree(resolved: Path) -> None:
  """Drop the resolved source tree's cron entry + rmtree it (no DB access).

  Pure-filesystem so it can run via ``asyncio.to_thread`` off the sole event
  loop — ``_unregister_cron`` shells out to crontab (can block seconds) and
  ``rmtree`` is unbounded. The caller has ALREADY
  decided it's safe (``_safe_to_rmtree_source``, which needs the DB) while
  holding ``source_dir_lock``, and keeps holding it across this call so the
  check and the removal stay atomic. Drops the cron even when the tree is gone
  — a live entry can outlive a partial cleanup. Swallows filesystem errors.
  """
  from app.install import _unregister_cron
  try:
    _unregister_cron(resolved)
    if resolved.is_dir():
      shutil.rmtree(resolved, ignore_errors=True)
  except OSError:
    pass


def _disable_init_cron_replay(resolved: Path) -> None:
  """Move a source tree's ``init-cron.sh`` aside so boot replay skips it.

  ``entrypoint.sh`` re-runs EVERY ``/data/apps/*/init-cron.sh`` on boot, which
  would resurrect a tombstoned scheduled app's crontab entry that
  ``_drop_cron_only`` just removed. Renaming the script to
  ``init-cron.sh.tombstoned`` puts it outside the entrypoint's
  ``*/init-cron.sh`` glob while keeping it on disk so ``recover`` can rename it
  back and reinstall the schedule. Swallows ``OSError`` like its siblings.
  """
  try:
    os.replace(
      resolved / "init-cron.sh", resolved / "init-cron.sh.tombstoned"
    )
  except OSError:
    pass


def _reenable_init_cron_replay(resolved: Path) -> None:
  """Re-arm a recovered app's cron: undo ``_disable_init_cron_replay``.

  Renames ``init-cron.sh.tombstoned`` back to ``init-cron.sh`` (so the next
  boot replays it too) and runs it once now to reinstall the crontab entry the
  tombstone dropped — recovery must re-establish every side-effect the delete
  tore down. Swallows ``OSError``; the ``init-cron.sh`` run is best-effort
  (``check=False``) and bounded.
  """
  try:
    os.replace(
      resolved / "init-cron.sh.tombstoned", resolved / "init-cron.sh"
    )
  except OSError:
    return
  try:
    subprocess.run(
      ["bash", str(resolved / "init-cron.sh")], timeout=10, check=False
    )
  except OSError:
    pass


def _drop_cron_only(resolved: Path) -> None:
  """Unregister a source tree's cron WITHOUT removing the tree.

  The soft-delete (tombstone) path: a tombstoned app must stop running its
  scheduled jobs, but its source — including the job.sh — has to survive so a
  reinstall/recover can re-register the schedule. Drops the live crontab entry
  AND moves ``init-cron.sh`` aside (``_disable_init_cron_replay``) so the boot
  replay in ``entrypoint.sh`` can't resurrect the schedule. Pure-filesystem so
  it runs via ``asyncio.to_thread`` (``_unregister_cron`` shells out to
  crontab). Swallows errors like ``_drop_cron_and_rmtree``.
  """
  from app.install import _unregister_cron
  try:
    _unregister_cron(resolved)
  except OSError:
    pass
  _disable_init_cron_replay(resolved)


def _resolve_app_source_dir(app_source_dir, app_name, settings) -> Path | None:
  """Resolve an app's source tree: the stored source_dir, else a name-based
  fallback for legacy rows. Returns None when neither resolves."""
  if app_source_dir:
    try:
      return Path(app_source_dir).resolve()
    except OSError:
      return None
  if app_name and re.fullmatch(r"[a-zA-Z0-9_-]+", app_name):
    try:
      return (Path(settings.data_dir) / "apps" / app_name).resolve()
    except OSError:
      return None
  return None


async def _hard_delete_app(db: Session, app: models.App) -> None:
  """Permanently remove an app's DB row, compiled bundle, source tree, and
  id-keyed storage tree — the pre-110 destructive uninstall, now reached only by
  the TTL purge of tombstoned rows.

  The CALLER must already hold ``install_uninstall_lock`` AND
  ``app_storage_lock(app.id)`` (the order ``delete_app`` documents), so a
  replacement app can't reuse the freed integer id and then have its storage
  deleted by this cleanup.
  """
  compiled_path = app.compiled_path
  app_name = app.name
  app_source_dir = app.source_dir
  deleted_app_id = app.id

  # Delete the row first so a partial filesystem cleanup leaves the registry
  # coherent — stale files are harmless orphans, a row pointing at missing
  # files is a live 404.
  db.delete(app)
  db.commit()
  get_system_broadcast().publish(
    {"type": "app_updated", "appId": str(deleted_app_id)}
  )

  if compiled_path:
    try:
      Path(compiled_path).unlink(missing_ok=True)
    except OSError:
      pass  # best effort — a stale compiled file is harmless

  settings = get_settings()
  apps_root = (Path(settings.data_dir) / "apps").resolve()
  resolved_source = _resolve_app_source_dir(app_source_dir, app_name, settings)
  if resolved_source is not None:
    async with fs_locks.source_dir_lock(str(resolved_source)):
      if _safe_to_rmtree_source(resolved_source, apps_root, db, deleted_app_id):
        await asyncio.to_thread(_drop_cron_and_rmtree, resolved_source)
  storage_dir = apps_root / str(deleted_app_id)
  if storage_dir.is_dir():
    await asyncio.to_thread(shutil.rmtree, storage_dir, ignore_errors=True)


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
  base = _slugify_for_source_dir(name)
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


def ensure_slug(db: Session, app: models.App) -> str:
  """Returns the app's slug, populating it on first call for legacy rows.

  Apps created before the slug column existed have NULL slug. Lazy
  backfill on first standalone-route access keeps the migration
  pure-additive and avoids guessing slugs we might not be able to
  validate at migration time (uniqueness needs a transaction).
  """
  if app.slug:
    return app.slug
  app.slug = allocate_unique_slug(db, app.name, exclude_id=app.id)
  db.commit()
  return app.slug


@router.get("/", response_model=list[schemas.AppOut])
async def list_apps(
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Returns all LIVE registered mini-apps (tombstoned ones are hidden).

  Pinned apps sort first (newest pin at top of the pinned group),
  then unpinned apps by creation time (oldest first — the drawer's
  apps list has historically been stable-ordered). See `Chat.pinned_at`
  for the same contract on chats.

  Piggybacks the TTL purge of tombstoned apps onto this list call, the way
  `list_chats` does. The pre-check is lock-free so the hot drawer path pays
  nothing in the common case; only when a stale tombstone actually exists do we
  take `install_uninstall_lock` to serialize the hard-delete against a
  concurrent reinstall/recover — otherwise the purge could delete a row the
  reinstall is reviving, re-opening the slug-flip race (feature 110).
  """
  cutoff = now_naive_utc() - APP_SOFT_DELETE_TTL
  has_stale = (
    db.query(models.App.id)
    .filter(models.App.deleted_at.isnot(None), models.App.deleted_at < cutoff)
    .first()
  )
  if has_stale:
    async with fs_locks.install_uninstall_lock():
      stale = (
        db.query(models.App)
        .filter(
          models.App.deleted_at.isnot(None), models.App.deleted_at < cutoff
        )
        .all()
      )
      for app in stale:
        async with fs_locks.app_storage_lock(app.id):
          await _hard_delete_app(db, app)
  return (
    db.query(models.App)
    .filter(models.App.deleted_at.is_(None))
    .order_by(
      models.App.pinned_at.is_(None),
      models.App.pinned_at.desc(),
      models.App.created_at,
    )
    .all()
  )


@router.post(
  "/install",
  response_model=schemas.AppInstallOut,
  status_code=201,
  dependencies=[Depends(reject_cross_site)],
)
async def install_app(
  body: schemas.AppInstall,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_owner_or_app_with_manage_apps),
):
  """Atomic install (or in-place update) of an app from a `mobius.json`.

  See `app.install.install_from_manifest` for the lifecycle: fetch
  manifest → fetch entry JSX + icon + seed files → compile → write
  source_dir → seed storage, all inside one DB transaction with
  filesystem rollback on failure. Cron registration happens after the
  commit; failures are non-fatal and returned as warnings.

  Returns the new (or updated) App row plus the install `mode` and
  any non-fatal `warnings` (e.g. icon 404, cron deferred).
  """
  # Late import to avoid circular import — install.py reads from
  # routes/apps.py at module top.
  from app.install import install_from_manifest
  # Serialize the whole install against any concurrent uninstall — both are
  # app-lifecycle operations over the same /data/apps trees, and letting them
  # overlap lets one delete what the other just wrote
  # (fs_locks.install_uninstall_lock has the full rationale).
  async with fs_locks.install_uninstall_lock():
    app, mode, warnings, manifest, conflict_paths, divergence = (
      await install_from_manifest(
        db,
        manifest_url=body.manifest_url,
        manifest=body.manifest,
        raw_base=body.raw_base,
        source="store",
      )
    )
  # Notify the Shell to refetch its app list so a new install (or an
  # in-place update) shows up in the drawer without a page reload.
  # Published only on the success path: install_from_manifest raises
  # HTTPException on any pre-commit failure, so reaching this line
  # means the DB row is durable. Cron-registration warnings are
  # collected into `warnings` and do not block the event — the app
  # IS installed at this point.
  get_system_broadcast().publish(
    {"type": "app_updated", "appId": str(app.id)}
  )
  # A conflicting update leaves the app on its current version with a real
  # working-tree merge (markers + MERGE_HEAD) on disk; the merge is NOT
  # auto-resolved. Whether to involve the agent is the owner's call, not ours:
  # the store surfaces the conflict (mode + conflict_paths, below) and the owner
  # opts in via its click-gated "Resolve in chat" affordance, which opens the
  # resolver chat itself. We deliberately do NOT auto-spawn a resolver here —
  # doing so preempted the owner's choice and raced a duplicate chat against the
  # store's own.
  return schemas.AppInstallOut(
    id=app.id,
    name=app.name,
    description=app.description,
    compiled_path=app.compiled_path,
    chat_id=app.chat_id,
    source_dir=app.source_dir,
    pinned_at=app.pinned_at,
    cross_app_access=app.cross_app_access,
    share_with_apps=app.share_with_apps,
    offline_capable=app.offline_capable,
    manage_apps=app.manage_apps,
    slug=app.slug,
    manifest_url=app.manifest_url,
    theme_color=app.theme_color,
    background_color=app.background_color,
    display=app.display,
    offline_contract=app.offline_contract,
    created_at=app.created_at,
    updated_at=app.updated_at,
    mode=mode,
    version=manifest.get("version", "unknown"),
    warnings=warnings,
    conflict_paths=conflict_paths,
    divergence=divergence,
  )


async def _spawn_app_conflict_chat(db: Session, app, conflict_paths) -> str | None:
  """Open a visible chat that drives the agent to resolve an app update merge
  conflict, and notify the owner.

  No longer auto-called on an app update conflict: the store surfaces the
  conflict and the owner opts in to resolution via its click-gated "Resolve in
  chat" affordance (which opens its own resolver chat). Retained as the
  reference pattern the platform's resolver-chat spawn mirrors; a follow-up can
  decide whether to wire it back behind an explicit owner-confirm endpoint or
  delete it once the platform path is unified.

  The conflict is already materialized on disk (markers + MERGE_HEAD) by
  `install.start_conflict_merge`; this just opens the chat that resolves it, the
  same way a person handles a `git pull` conflict. No programmatic resolve API —
  the agent uses ordinary git per the `resolving-app-git.md` skill.

  Dedupe: skip when a resolver chat for this app is already RUNNING, so a
  repeated failing update doesn't pile up duplicate chats (a finished resolver
  doesn't block a fresh one for a later conflict). Returns the chat id, or None
  when skipped/unavailable. Mirrors the canonical spawn sequence in
  `routes/chats_stream.py` (StartTurn actor → run_chat task).
  """
  from app.broadcast import create_broadcast
  from app.chat import (
    current_run_generation, discard_starting, mark_starting, run_chat,
  )
  from app.chat_writer import StartTurn, alloc_run_token, await_ack, get_writer
  from app.push import notify_owner

  title = f"Resolve update conflict — {app.name}"
  running = (
    db.query(models.Chat.id)
    .filter(models.Chat.title == title)
    .filter(models.Chat.run_status == "running")
    .filter(models.Chat.deleted_at.is_(None))
    .first()
  )
  if running is not None:
    return None

  owner = db.query(models.Owner).first()
  if owner is None:
    return None
  provider = owner.provider or "claude"

  files = ", ".join(conflict_paths) if conflict_paths else "the source files"
  content = (
    f"An update to the app **{app.name}** hit a merge conflict — the new "
    f"upstream version and the local edits both changed the same lines, so the "
    f"update can't apply cleanly.\n\n"
    f"The app's source at `/data/apps/{app.slug}/` is mid-merge with conflict "
    f"markers in: {files}. Read `/data/shared/skills/resolving-app-git.md`, "
    f"then resolve the markers there and **save** — the watcher recompiles and "
    f"finalizes the merge for you (the skill covers reading the partner's "
    f"intent, confirming the merge took, and backing out). The app keeps "
    f"serving its previous version until you finish.\n\n"
    f"To back out instead, run `git merge --abort` in that directory — it "
    f"restores the pre-update version."
  )

  chat_id = str(uuid.uuid4())
  chat = models.Chat(
    id=chat_id,
    title=title,
    messages=[],
    pending_messages=[],
    provider=provider,
    created_by_app_id=None,  # visible in the drawer — the owner should see it
  )
  db.add(chat)
  db.commit()

  if not mark_starting(chat_id):
    return chat_id

  try:
    start_gen = current_run_generation(chat_id)
    run_token = alloc_run_token()
    user_msg = {
      "role": "user", "content": content, "ts": int(time.time() * 1000),
    }
    ack = get_writer().submit(StartTurn(
      chat_id=chat_id,
      run_token=run_token,
      user_msg=user_msg,
      title_source=title,
      default_provider=provider,
    ))
    result = await await_ack(ack)
    if current_run_generation(chat_id) == start_gen:
      create_broadcast(chat_id)
      get_system_broadcast().publish(
        {"type": "chat_run_started", "chatId": chat_id}
      )
      asyncio.create_task(run_chat(
        result["history"],
        chat_id=chat_id,
        session_id=result["session_id"],
        provider_id=result["provider"],
        run_gen=start_gen,
        run_token=run_token,
      ))
    else:
      discard_starting(chat_id)
  except Exception:
    discard_starting(chat_id)
    raise
  finally:
    # Notify regardless of whether the run started — the open chat + the
    # on-disk conflict still need the owner's attention.
    try:
      notify_owner(
        db, owner.id,
        title="App update needs conflict resolution",
        body=(
          f"{app.name}: the update conflicts with local edits. Opened a chat "
          f"to resolve it."
        ),
        source_type="app_conflict",
        source_id=chat_id,
        target=f"chat:{chat_id}",
      )
    except Exception as exc:
      log.warning("conflict-resolver notify failed for %s: %r", app.slug, exc)

  return chat_id


def _upstream_parent(repo: Path, upstream_commit: str | None) -> str | None:
  """The previous pristine upstream commit, when the recorded tip has one."""
  if not upstream_commit:
    return None
  proc = app_git._run(repo, "rev-parse", f"{upstream_commit}^", check=False)
  if proc.returncode != 0:
    return None
  return proc.stdout.strip() or None


def _upstream_diff(repo: Path, upstream_commit: str | None) -> str | None:
  """Unified diff introduced by the recorded upstream tip.

  Degrades to None (not a 500) when the recorded commit no longer exists
  in the repo — a DB/git desync from a wiped + re-seeded repo shouldn't
  break the read-only preview.
  """
  if not upstream_commit:
    return None
  parent = _upstream_parent(repo, upstream_commit)
  if not parent:
    proc = app_git._run(
      repo, "show", "--format=", "--no-ext-diff", upstream_commit,
      "--", ".", check=False,
    )
  else:
    proc = app_git._run(
      repo, "diff", "--no-ext-diff", f"{parent}..{upstream_commit}",
      "--", ".", check=False,
    )
  return proc.stdout if proc.returncode == 0 else None


def _upstream_version(repo: Path, upstream_commit: str | None) -> str | None:
  """Version recorded by app_git.record_upstream's commit subject.

  None (not a 500) when the commit is missing — see `_upstream_diff`.
  """
  if not upstream_commit:
    return None
  proc = app_git._run(
    repo, "log", "-1", "--format=%s", upstream_commit, check=False,
  )
  if proc.returncode != 0:
    return None
  match = re.match(r"install v(.+) from .+", proc.stdout.strip())
  return match.group(1) if match else None


def _materialize_conflict_files(
  repo: Path, conflict_paths: list[str],
) -> list[schemas.ConflictFile]:
  """Reads real conflict-marker text from a throwaway worktree."""
  if not conflict_paths:
    return []
  tmp_parent = Path(tempfile.mkdtemp(prefix="mobius-update-preview-"))
  tmp = tmp_parent / "worktree"
  try:
    app_git._run(
      repo, "worktree", "add", "--detach", str(tmp), app_git.LOCAL_BRANCH,
    )
    app_git._run(
      tmp, "merge", "--no-commit", "--no-ff", app_git.UPSTREAM_BRANCH,
      check=False,
    )
    conflicts: list[schemas.ConflictFile] = []
    for rel in conflict_paths:
      path = tmp / rel
      if not path.is_file():
        continue
      conflicts.append(schemas.ConflictFile(
        path=rel,
        merged_with_markers=path.read_text(
          encoding="utf-8", errors="replace",
        ),
      ))
    return conflicts
  finally:
    app_git._run(
      repo, "worktree", "remove", "--force", str(tmp), check=False,
    )
    shutil.rmtree(tmp_parent, ignore_errors=True)


@router.get(
  "/{app_id}/update-preview",
  response_model=schemas.UpdatePreviewOut,
)
async def update_preview(
  app_id: int,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Read-only preview of the recorded upstream update vs local edits."""
  # The preview embeds full conflict-marker source text, so an app token
  # may read its own app's preview. App-manager tokens (the App Store)
  # may read other apps so they can drive conflict-resolution updates.
  # The owner (app_id is None) may read any. Mirrors install/delete's
  # manage_apps trust boundary for app lifecycle operations.
  if principal.app_id is not None and principal.app_id != app_id:
    caller = (
      db.query(models.App)
      .filter(models.App.id == principal.app_id)
      .first()
    )
    if caller is None:
      raise HTTPException(status_code=401, detail="App not found.")
    if not bool(caller.manage_apps):
      raise HTTPException(
        status_code=403,
        detail=(
          "This app needs permissions.manage_apps=true in its manifest "
          "to preview updates for other apps."
        ),
      )
  app = live_app_or_404(db, app_id)
  if not app.source_dir:
    raise HTTPException(status_code=400, detail="App has no source_dir.")
  repo = Path(app.source_dir)
  if not app_git.is_repo(repo):
    raise HTTPException(status_code=400, detail="App is not a git repo.")

  async with fs_locks.source_dir_lock(str(repo)):
    merge = await asyncio.to_thread(app_git.merge_upstream, repo)
    conflict_paths = merge.conflict_paths if merge.status == "conflict" else []
    conflicts = await asyncio.to_thread(
      _materialize_conflict_files, repo, conflict_paths,
    )
    upstream_diff = await asyncio.to_thread(
      _upstream_diff, repo, app.upstream_commit,
    )
    upstream_version = await asyncio.to_thread(
      _upstream_version, repo, app.upstream_commit,
    )
  return schemas.UpdatePreviewOut(
    app_id=app.id,
    status=merge.status,
    upstream_version=upstream_version,
    upstream_commit=app.upstream_commit,
    conflict_paths=conflict_paths,
    conflicts=conflicts,
    upstream_diff=upstream_diff,
  )


@router.post(
  "/",
  response_model=schemas.AppOut,
  status_code=201,
  dependencies=[Depends(reject_cross_site)],
)
async def create_app(
  body: schemas.AppCreate,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
):
  """Creates and compiles a new mini-app from JSX source."""
  # Always set source_dir. The file watcher resolves edits via exact source_dir
  # match — apps with NULL source_dir are invisible to auto-recompile and the
  # partner gets the silent "save doesn't land" failure mode. Derive it from the
  # UNIQUE slug (not the raw name) so two apps with the SAME name get DISTINCT
  # source trees (foo, foo-2) instead of silently sharing /data/apps/foo — the
  # shared-source-dir hazard the uniqueness check below guards (Codex review
  # round-9 #3). A caller-supplied source_dir is validated as-is.
  data_dir = get_settings().data_dir
  slug = allocate_unique_slug(db, body.name)
  source_dir = (
    _validate_source_dir(body.source_dir, data_dir)
    if body.source_dir
    else str(Path(data_dir) / "apps" / slug)
  )
  # Hold the per-source-dir lock across the row commit so this app's source_dir
  # becomes visible to a concurrent uninstall's shared-dir dedup check before
  # that uninstall could rmtree the directory, and so
  # the uniqueness check + assignment are atomic vs another create. One uvicorn
  # worker => this in-process lock fully serializes the two.
  async with fs_locks.source_dir_lock(source_dir):
    _reject_if_source_dir_taken(db, source_dir, exclude_id=None)
    app = models.App(
      name=body.name,
      description=body.description,
      jsx_source=body.jsx_source,
      chat_id=body.chat_id,
      source_dir=source_dir,
      cross_app_access=body.cross_app_access,
      share_with_apps=body.share_with_apps,
      offline_capable=body.offline_capable,
      slug=slug,
      # manifest_url stays NULL on this route. Only the install endpoint
      # may set it — it's the identity key for install-vs-update
      # discrimination. See AppCreate's docstring for the threat model.
    )
    db.add(app)
    db.flush()  # assigns app.id without committing
    # Compile transactionally like every other recompile path: out-of-place to a
    # staging file, swapped into the live bundle only after the commit succeeds,
    # so a commit failure can't leave an orphan live bundle. The app id is
    # brand-new and uncommitted, so no concurrent op can reference it — the
    # lifecycle+app lock recompile_app_bundle normally relies on (to stop an id
    # being reused mid-swap) is moot here, and taking app_storage_lock under the
    # source lock we already hold would invert the documented lock order.
    try:
      await recompile_app_bundle(db, app, body.jsx_source)
    except RuntimeError as exc:
      # Roll back explicitly to avoid leaving the SQLite WAL connection in a
      # dirty transaction state, which can cause "database is locked" errors
      # on subsequent writes.
      db.rollback()
      raise HTTPException(status_code=422, detail=str(exc))
    db.refresh(app)
    get_system_broadcast().publish(
      {"type": "app_updated", "appId": str(app.id)}
    )
    # Fire the chat-scoped `app_built` onto the owning chat's stream so the
    # in-chat "Open <App>" CTA appears for the turn that built/edited it. The
    # global `app_updated` above busts caches everywhere but never reaches the
    # chat-scoped CTA gate. No-ops when the chat has no live broadcast (e.g.
    # an out-of-band edit), so it never plants a spurious CTA. app_watcher
    # emits the same event on a file-write recompile; the client upsert is
    # idempotent (deduped by appId), so a double-emit is harmless.
    publish_app_built_to_owning_chat(db, str(app.id))
  return app


@router.get("/{app_id}", response_model=schemas.AppOut)
def get_app(
  app_id: int,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Returns a single mini-app by ID (404 for a tombstoned one)."""
  app = live_app_or_404(db, app_id)
  return app


@router.patch(
  "/{app_id}",
  response_model=schemas.AppOut,
  dependencies=[Depends(reject_cross_site)],
)
async def update_app(
  app_id: int,
  body: schemas.AppUpdate,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
):
  """Partially updates a mini-app, recompiling if source changed.

  Runs under the lifecycle + per-app lock (documented lifecycle -> app order)
  with the row loaded fresh under the lock, so a PATCH can't race a concurrent
  uninstall + SQLite id reuse and recompile into a REPLACEMENT app's bundle. ALL
  validation (source_dir shape + uniqueness) happens BEFORE the recompile, so a
  conflicting field can't overwrite the live bundle and then fail. The recompile
  goes through ``recompile_app_bundle``, which compiles out-of-place and only
  swaps the live bundle in after the commit succeeds — so a commit failure can
  never leave the new (uncommitted) bundle live.
  """
  data_dir = get_settings().data_dir
  # Validate the source_dir SHAPE up front (cheap, no side effects). The
  # uniqueness check needs the lock + DB and happens below, still before the
  # compile.
  new_source_dir = (
    _validate_source_dir(body.source_dir, data_dir)
    if body.source_dir is not None else None
  )

  async def _recompile_and_commit(app):
    # Everything else is validated by now. With no source change there's
    # nothing to compile, so just persist the field updates.
    if body.jsx_source is None:
      db.commit()
      return
    try:
      await recompile_app_bundle(db, app, body.jsx_source)
    except RuntimeError as exc:
      db.rollback()
      raise HTTPException(status_code=422, detail=str(exc))

  async with (
    fs_locks.install_uninstall_lock(),
    fs_locks.app_storage_lock(app_id),
  ):
    app = live_app_or_404(db, app_id, populate=True)
    if body.name is not None:
      app.name = body.name
    if body.description is not None:
      app.description = body.description
    if body.chat_id is not None:
      app.chat_id = body.chat_id
    if new_source_dir is not None:
      app.source_dir = new_source_dir
    if body.pinned is not None:
      app.pinned_at = now_naive_utc() if body.pinned else None
    if body.share_with_apps is not None:
      app.share_with_apps = body.share_with_apps
    if body.cross_app_access is not None:
      app.cross_app_access = body.cross_app_access
    if body.offline_capable is not None:
      app.offline_capable = body.offline_capable
    # source_dir uniqueness + the recompile/commit run under the per-source-dir
    # lock (when it changed) so the new value is visible to a concurrent
    # uninstall's dedup check, and a conflicting dir is rejected BEFORE the
    # compile touches the live bundle (Codex review round-6 #4, round-12).
    if new_source_dir is not None:
      async with fs_locks.source_dir_lock(new_source_dir):
        _reject_if_source_dir_taken(db, new_source_dir, exclude_id=app_id)
        await _recompile_and_commit(app)
    else:
      if body.jsx_source is not None and app.source_dir:
        async with fs_locks.source_dir_lock(app.source_dir):
          await _recompile_and_commit(app)
      else:
        await _recompile_and_commit(app)
    db.refresh(app)
    get_system_broadcast().publish(
      {"type": "app_updated", "appId": str(app.id)}
    )
    # Fire the chat-scoped `app_built` CTA ONLY on a real rebuild. A
    # metadata-only PATCH (pin/rename/share toggles — `jsx_source is None`)
    # still reaches here (it commits fields without recompiling), and emitting
    # then would plant an "Open <App>" card on whatever turn the owning chat
    # happens to be streaming — a correct link in the wrong turn. Gate on
    # jsx_source so only an actual edit surfaces the CTA. The helper still
    # no-ops without a live broadcast, and the client upsert dedupes by appId.
    if body.jsx_source is not None:
      publish_app_built_to_owning_chat(db, str(app.id))
  return app


@router.put(
  "/{app_id}/icon",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def update_icon(
  app_id: int,
  request: Request,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Owner uploads a custom icon for the app's standalone PWA install.

  Accepts raw PNG / JPEG / WebP bytes (anything Pillow can decode).
  The body is validated, converted to RGB, downscaled to fit
  within 1024x1024 if larger, and re-encoded as PNG before storing
  in `App.icon_png`. The standalone icon endpoint at
  `/apps/<slug>/icon-<N>.png` resizes from this on the fly per
  request size, so one upload covers every icon size the manifest
  declares.

  Authorized for the owner OR for an app-scoped token whose
  `app_id` matches the path — the app can manage its own visual
  identity, but cannot touch a sibling app's icon. The standalone
  install card lives at `/apps/<slug>/` where the page context
  has an app-scoped token in `localStorage['token']` (minted by
  `claim-token` on first render), so requiring owner-only here
  would 403 the upload from the install surface. To revert to
  the auto-generated letter icon, send a zero-byte body.
  """
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(
      status_code=403,
      detail="App token can only modify its own icon.",
    )
  # 12 MB cap on the wire — phone camera photos routinely run 5-8 MB. The
  # standalone shell downscales client-side before upload, so well-behaved
  # clients never approach this. Stream-cap the read (Content-Length precheck +
  # running-total abort) rather than buffering an unbounded body first, so a
  # giant direct-API upload can't OOM the host.
  body = await read_capped_body(request, cap=12 * 1024 * 1024)
  # Capture the app's identity at authorization; recheck the nonce under the
  # per-app lock so a slow icon upload can't alter a DIFFERENT app that reused
  # this id between authorization and commit — the same id-reuse race fixed for
  # storage PUT/DELETE.
  app0 = db.query(models.App).filter(models.App.id == app_id).first()
  if not app0:
    raise HTTPException(404, "App not found.")
  expected_nonce = app0.token_nonce
  # Decode/normalize via the SHARED installer pipeline, which inspects the
  # image header dimensions BEFORE img.load() so a decompression bomb is
  # rejected before it can allocate. Done outside
  # the lock — only the DB mutation needs serializing. Lazy import avoids the
  # install.py <-> routes.apps circular import.
  from app.install import _process_icon
  processed = _process_icon(body) if body else None
  async with fs_locks.app_storage_lock(app_id):
    app = live_app(db, app_id, populate=True)
    if app is None or app.token_nonce != expected_nonce:
      raise HTTPException(404, "App not found.")
    app.icon_png = processed
    db.commit()
  return Response(status_code=204)


def _downscale_icon(png: bytes, size: int) -> bytes:
  """A `size`x`size` PNG downscale of `png`, preserving the install-time
  palette/alpha handling (`install._process_icon` already normalized the
  stored bytes to RGB/RGBA, so a plain LANCZOS resize keeps transparency).

  Only ever downscales: a request for a larger box than the stored icon
  returns the original bytes rather than upscaling a blurrier copy. Any
  decode/encode failure falls back to the full-res bytes — a malformed
  stored icon should still render, just uncompressed."""
  try:
    from PIL import Image
    img = Image.open(io.BytesIO(png))
    img.load()
    if img.width <= size and img.height <= size:
      return png
    img = img.resize((size, size), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
  except Exception:
    return png


# The downscale sizes the icon route will serve. The editor apps render the
# icon as a 28px top-bar logo, so 64 covers HiDPI; the store grid + drawer
# want crisper thumbnails, so 128 is the other supported step. Anything else
# is rejected so the variant cache (keyed on size) can't be flooded with
# arbitrary dimensions.
_ICON_SIZES = frozenset((64, 128))


@router.get("/{app_id}/icon")
async def get_icon(
  app_id: int,
  request: Request,
  db: Session = Depends(get_db),
  size: int | None = None,
):
  """Public read of an app's icon PNG, so a mini-app can render its own logo
  with a plain `<img src="/api/apps/<id>/icon">` (e.g. as its file-drawer
  toggle, mirroring the shell's logo). Public + by-id on purpose: the embedded
  mini-app has its numeric `appId` but not its slug, and the slug-based
  standalone icon route (`/apps/<slug>/icon-<N>.png`) is already public — an app
  icon is not a secret. Returns 404 when the app uses the auto-generated letter
  icon (no stored PNG) so the caller can fall back to its own glyph.

  Icons are hundreds of KB and the store grid renders a dozen at once, so
  the old `Cache-Control: no-cache` made every grid open re-download ~4MB.
  ETag on `updated_at` (same validator family as /module) + a 1h max-age:
  repeat opens are free, and an app update advances the validator so the
  next revalidation picks up the new icon within the hour.

  `?size=` (64 or 128) returns a Pillow-downscaled variant — a full-res
  PNG is wasted bytes when the caller renders it as a 28px top-bar logo or
  a grid thumbnail. The ETag folds the size in so the 64px and the full-res
  responses cache independently; no `size` keeps the original full-res
  bytes (unchanged for existing callers).

  The downscale is memoized in `icon_cache` keyed on the same
  `(app_id, updated_at, size)` the ETag uses, so a warm hit returns bytes
  with no Pillow work, and a cold miss runs the LANCZOS resize off the
  threadpool (this handler is async) — concurrent icon requests no longer
  serialize through a synchronous resize, which was the staggered trickle a
  mini-app saw when its logo and the grid thumbnails all rendered at once.
  The handler is async + `stale-while-revalidate`, so even a revalidation
  that does miss the browser cache is served instantly from the prior bytes
  while the conditional request resolves."""
  if size is not None and size not in _ICON_SIZES:
    raise HTTPException(400, f"size must be one of {sorted(_ICON_SIZES)}.")
  app = live_app(db, app_id, populate=True)
  if app is None or not app.icon_png:
    raise HTTPException(404, "No icon set.")
  ts_us = int(app.updated_at.timestamp() * 1e6) if app.updated_at else 0
  etag = f'W/"{ts_us}-{size}"' if size else f'W/"{ts_us}"'
  headers = {
    "ETag": etag,
    "Cache-Control": "public, max-age=3600, stale-while-revalidate=86400",
  }
  if request.headers.get("if-none-match") == etag:
    return Response(status_code=304, headers=headers)
  if size:
    icon_png = app.icon_png
    content = await icon_cache.get_or_compute(
      app_id=app_id,
      updated_us=ts_us,
      kind="embed",
      size=size,
      compute=lambda: _downscale_icon(icon_png, size),
    )
  else:
    content = app.icon_png
  return Response(content=content, media_type="image/png", headers=headers)


@router.delete(
  "/{app_id}",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def delete_app(
  app_id: int,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_owner_or_app_with_manage_apps),
):
  """Soft-deletes (tombstones) a mini-app — sets deleted_at and drops its cron,
  PRESERVING the source tree and the id-keyed runtime storage tree.

  The app vanishes from the drawer and its module/frame 404, but a reinstall
  (matched by manifest_url) or POST /{id}/recover within APP_SOFT_DELETE_TTL
  revives the SAME id + data instead of orphaning it under a freed integer id.
  The destructive filesystem cleanup is deferred to the TTL purge in list_apps.
  Mirrors chat soft-delete; recovery is agent-driven (feature 110).

  Still async + lock-held: holding install_uninstall_lock serializes the
  tombstone against a concurrent install of the same app, and the per-app
  storage lock matches the order the purge (which DOES rmtree) takes them.
  """
  async with (
    fs_locks.install_uninstall_lock(),
    fs_locks.app_storage_lock(app_id),
  ):
    app = (
      db.query(models.App)
      .filter(models.App.id == app_id, models.App.deleted_at.is_(None))
      .first()
    )
    if not app:
      raise HTTPException(status_code=404, detail="App not found.")

    # Naive UTC to match SQLite's naive storage + the naive TTL comparison in
    # list_apps / recover_app (same contract chats.py documents). Avoids a
    # platform-dependent aware/naive round-trip mismatch.
    app.deleted_at = now_naive_utc()
    app_name = app.name
    app_slug = app.slug
    app_source_dir = app.source_dir
    db.commit()
    # Logical uninstall — pairs with the app_install event so churn analysis
    # (and the nightly digest) sees removals, not just installs. Best-effort,
    # after the tombstone commit.
    activity.log_event("app_uninstall", app_id=app_id, slug=app_slug)

    # The Shell refetches /api/apps/ and the now-tombstoned app drops out
    # (list_apps filters deleted_at IS NULL).
    get_system_broadcast().publish(
      {"type": "app_updated", "appId": str(app_id)}
    )

    # Stop the tombstoned app's scheduled jobs WITHOUT touching its files — the
    # job.sh stays in the preserved source tree so a reinstall/recover can
    # re-register the schedule. Drop cron under the per-source-dir lock, off the
    # loop (crontab shells out).
    settings = get_settings()
    resolved_source = _resolve_app_source_dir(
      app_source_dir, app_name, settings
    )
    if resolved_source is not None:
      async with fs_locks.source_dir_lock(str(resolved_source)):
        await asyncio.to_thread(_drop_cron_only, resolved_source)


@router.delete(
  "/{app_id}/data",
  status_code=204,
  dependencies=[Depends(reject_cross_site)],
)
async def delete_app_data(
  app_id: int,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_owner_or_app_with_manage_apps),
):
  """Wipes an installed app's runtime storage back to empty, KEEPING the app
  installed — the DB row, source tree, compiled bundle, and cron all stay.

  This is a separate, additive action from uninstall: uninstall (delete_app)
  tombstones the row and hides the app; this leaves the app fully live and
  running, just with an empty `/data/apps/<id>` tree. There is no tombstone and
  no recovery window — a data wipe is what the owner asked for, so unlike the
  reversible uninstall it takes effect immediately.

  The wipe holds ``app_storage_lock(app_id)`` — the SAME per-app lock every
  storage write and folder-delete takes (see fs_locks + routes/storage.py) — so
  a concurrent write can't recreate the tree mid-wipe. Taking only this innermost
  storage lock (never the outer install_uninstall_lock) keeps the documented
  lock order intact; we are not touching the source tree, cron, or the id
  allocation that the outer lock protects.
  """
  app = live_app_or_404(db, app_id)

  settings = get_settings()
  apps_root = (Path(settings.data_dir) / "apps").resolve()
  storage_dir = apps_root / str(app.id)
  data_dir = settings.data_dir
  async with fs_locks.app_storage_lock(app.id):
    # Drop the id-keyed runtime tree and its mirrored content-type sidecars.
    # Leaving the dir absent is fine — routes/storage.py recreates it on the
    # next write (atomic_write mkdirs its parent). Passing rel="" targets the
    # whole `<meta>/apps/<id>` sidecar tree (an empty component is dropped in
    # the path join), the sidecar analogue of removing the storage root.
    await asyncio.to_thread(shutil.rmtree, storage_dir, ignore_errors=True)
    delete_content_type_tree(data_dir, Path("apps") / str(app.id), "")

  # Advance updated_at so the iframe cache-buster (versionForApp reads
  # app.updated_at) changes and a currently-open app remounts against its
  # now-empty storage — the wipe touches no mapped column, so onupdate won't
  # fire on its own. Naive UTC to match the App soft-delete write convention.
  app.updated_at = now_naive_utc()
  db.commit()

  # Refetch the drawer and bust any cached iframe so the app reloads against
  # its now-empty storage (Shell's app_updated handler refreshes the list).
  get_system_broadcast().publish(
    {"type": "app_updated", "appId": str(app.id)}
  )


@router.post(
  "/{app_id}/recover",
  dependencies=[Depends(reject_cross_site)],
)
async def recover_app(
  app_id: int,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_owner_or_app_with_manage_apps),
):
  """Restores a soft-deleted app if the TTL window hasn't expired.

  Agent-driven recovery, consistent with chats (POST /api/chats/{id}/recover):
  the agent calls this when the partner asks to undo an uninstall. Store apps can
  also be revived by reinstalling — the install reattaches by manifest_url. The
  id-keyed storage tree was never removed, so the revived app keeps its data.
  Cron IS re-registered on recover for any app that had a scheduled
  ``init-cron.sh`` (the tombstoned replay script is restored and re-run under the
  source-dir lock); reinstalling a store app also re-registers it. See feature
  110.

  Held under install_uninstall_lock — the same lock the TTL purge takes — so a
  recover near the TTL boundary can't race the purge into reviving a row the
  sweep is hard-deleting (or vice versa). Whoever wins the lock leaves a
  consistent state: a purged row → recover 404s; a recovered row → purge's
  under-lock stale re-query no longer matches it.
  """
  async with fs_locks.install_uninstall_lock():
    app = (
      db.query(models.App)
      .filter(models.App.id == app_id, models.App.deleted_at.isnot(None))
      .first()
    )
    if not app:
      raise HTTPException(
        status_code=404, detail="App not found or not deleted."
      )
    if (
      now_naive_utc() - app.deleted_at
    ) >= APP_SOFT_DELETE_TTL:
      raise HTTPException(status_code=410, detail="Recovery window has expired.")
    app.deleted_at = None
    app_name = app.name
    app_source_dir = app.source_dir
    db.commit()

    # Re-arm the schedule the tombstone dropped: rename init-cron.sh back into
    # the entrypoint's replay glob and run it once to reinstall the crontab
    # entry. Recovery must re-establish every side-effect delete tore down.
    # Off the loop under the per-source-dir lock (bash run can block).
    settings = get_settings()
    resolved_source = _resolve_app_source_dir(
      app_source_dir, app_name, settings
    )
    if resolved_source is not None:
      async with fs_locks.source_dir_lock(str(resolved_source)):
        await asyncio.to_thread(_reenable_init_cron_replay, resolved_source)
  # Refetch the drawer (app reappears) and bust any cached iframe for it.
  get_system_broadcast().publish(
    {"type": "app_updated", "appId": str(app_id)}
  )
  return {"ok": True}


def _manifest_job_name(source_dir: Path) -> str | None:
  """The job script the app's `mobius.json` declares under `schedule.job`.

  This is the source of truth for which script a run-job (and the cron
  schedule) should invoke. The legacy probe below only guesses by filename,
  so when an app renames its job (e.g. tandem's `job.sh` -> `generate.sh`)
  a stale sibling left in the tree shadows the new script. Reading the
  manifest immunizes every app against that race: the declared script wins
  regardless of what else happens to sit in the directory.

  Returns the bare filename only when the manifest names a job that is a
  simple filename with no path separators (the same shape `install._validate_manifest`
  enforces) AND that file actually exists on disk — a manifest that points
  at a since-deleted script should fall through to the legacy probe rather
  than 400. Any read/parse error is non-fatal: older apps have no manifest
  on disk, and the probe is the fallback for them.
  """
  manifest_path = source_dir / "mobius.json"
  try:
    manifest = json.loads(manifest_path.read_text())
  except (OSError, ValueError):
    return None
  if not isinstance(manifest, dict):
    return None
  sched = manifest.get("schedule")
  if not isinstance(sched, dict):
    return None
  job = sched.get("job")
  if not isinstance(job, str) or "/" in job or "\\" in job or not job.strip():
    return None
  return job if (source_dir / job).is_file() else None


@router.post(
  "/{app_id}/run-job",
  status_code=202,
  dependencies=[Depends(reject_cross_site)],
)
def run_app_job(
  app_id: int,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Spawns the app's scheduled job script as a non-blocking subprocess.

  Mini-apps cannot shell out themselves — this is the bridge that lets
  a Reports tab's "Generate now" button trigger the same job the cron
  schedule would run. The endpoint returns 202 immediately with a
  started_at timestamp; the job may take 30s+ to complete. Callers
  observe completion by polling the app's storage for newly-written
  output (e.g. `/api/storage/apps/{id}/reports/<date>.json`).

  The job script lives at `<source_dir>/<job_name>` where source_dir
  is the app's on-disk source tree (per the install-from-manifest
  layout in `app.install`). The manifest's `schedule.job` is the
  source of truth and is tried FIRST — the legacy filename probe
  (fetch.sh / job.sh / build.sh) only runs when no manifest declares
  a job, so a stale sibling script can't shadow the script the app
  actually ships (tandem's old job.sh once won over its new
  generate.sh because the probe order, not the manifest, decided).

  Authorized for the owner OR for an app-scoped token whose `app_id`
  matches the path — the News "run now" button fires from inside the
  mini-app iframe, which only holds an app-scoped token, so requiring
  owner-only here would 403 the very caller the endpoint exists for.
  The app can trigger its own job but not a sibling's. The same
  defense-in-depth CSRF guard the other state-changing endpoints
  (settings, model-prefs) use still applies. Mirrors the self-scope
  check on the icon-write route above.
  """
  from datetime import UTC, datetime
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(
      status_code=403,
      detail="App token can only run its own job.",
    )
  app = live_app_or_404(db, app_id)
  if not app.source_dir:
    raise HTTPException(
      status_code=400, detail="App has no source_dir; cannot locate job.",
    )
  source_dir = Path(app.source_dir)
  # The manifest's schedule.job wins. The legacy probe (fetch.sh
  # app-news convention, job.sh install-from-manifest default,
  # build.sh LaTeX/pipeline apps) is the fallback for apps installed
  # before the manifest convention solidified — first hit wins, in
  # priority order.
  job_path = None
  manifest_job = _manifest_job_name(source_dir)
  if manifest_job is not None:
    job_path = source_dir / manifest_job
  else:
    for candidate in ("fetch.sh", "job.sh", "build.sh"):
      p = source_dir / candidate
      if p.is_file():
        job_path = p
        break
  if job_path is None:
    raise HTTPException(
      status_code=400,
      detail="No job script found (looked for fetch.sh, job.sh, build.sh).",
    )
  # Non-blocking. stdout/stderr go to /dev/null so the subprocess
  # doesn't inherit the FastAPI worker's pipes; the job script itself
  # is expected to log to /data/cron-logs/.
  subprocess.Popen(
    ["bash", str(job_path), str(app_id)],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    cwd=str(source_dir),
    close_fds=True,
  )
  return {"started_at": datetime.now(UTC).isoformat()}


@router.post(
  "/{app_id}/schedule",
  dependencies=[Depends(reject_cross_site)],
)
def update_app_schedule(
  app_id: int,
  body: schemas.AppScheduleUpdate,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Updates one app's recurring cron schedule.

  Authorized for the owner OR for the app itself. This is the schedule
  counterpart to run-job: a mini-app settings screen can tune its own
  recurring job, but an app token cannot rewrite a sibling's crontab.
  The scaffold writes both the live crontab and durable init-cron.sh so
  the change survives container restarts.
  """
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(
      status_code=403,
      detail="App token can only update its own schedule.",
    )
  app = live_app_or_404(db, app_id)
  if not app.source_dir:
    raise HTTPException(
      status_code=400, detail="App has no source_dir; cannot locate job.",
    )
  from app.install import _register_cron, _validate_cron_expr
  _validate_cron_expr(body.cron)
  source_dir = Path(app.source_dir)
  job_name = body.job or "fetch.sh"
  if "/" in job_name or "\\" in job_name or not job_name.strip():
    raise HTTPException(status_code=400, detail="Invalid job filename.")
  job_path = source_dir / job_name
  if not job_path.is_file():
    raise HTTPException(status_code=400, detail="Job script not found.")
  slug = app.slug or _slugify_for_source_dir(app.name)
  _register_cron(slug, body.cron, job_path, app_id)
  return {"cron": body.cron, "job": job_name}


def _etag_for_app(app: models.App) -> str | None:
  """Weak ETag derived from `app.updated_at`. Microsecond precision
  so two updates within the same wall-clock second produce different
  validators — second-precision risks the agent shipping a fix and
  the user's cached browser refusing to revalidate."""
  if not app.updated_at:
    return None
  ts_us = int(app.updated_at.timestamp() * 1_000_000)
  return f'W/"{ts_us}"'


def _not_modified_if_match(
  request: Request, etag: str, offline: bool = False
) -> Response | None:
  """Returns a 304 Response if the request's If-None-Match matches
  `etag`, else None. The 304 keeps the ETag header so a browser
  re-validating an existing cache entry can keep its validator, and
  mirrors the X-Mobius-Offline marker so the 304 carries the same
  cache metadata as the 200 it stands in for. The SW's
  appCodeStoreAction policy keys on that header for the gated
  standalone-navigation cache."""
  match = request.headers.get("if-none-match")
  if match and etag in [v.strip() for v in match.split(",")]:
    headers = {"ETag": etag}
    if offline:
      headers["X-Mobius-Offline"] = "1"
    return Response(status_code=304, headers=headers)
  return None


def _frame_etag(
  app: models.App,
  frame_path: Path,
  theme_token: str | None = "",
  frame_rev: str | None = None,
) -> str | None:
  """Validator for the `/frame` response, combining the app's
  `updated_at` with the shared runtime-frame file's content and the
  active theme.

  Unlike the per-app module, the frame serves `app-frame.html` — the
  importmap + runtime shell — which changes INDEPENDENTLY of any app
  row. Keying only on `app.updated_at` (as `_etag_for_app` does) means
  an edit to the frame (e.g. bumping a vendored import path) never
  invalidates an already-installed PWA: it keeps revalidating against
  an unchanged validator, gets a 304, and runs the stale frame forever.
  That is exactly how a dropped `/vendor/three/` path pinned clients to
  a spinner. Folding a hash of the frame's CONTENT in busts every app's
  frame cache on the next load whenever app-frame.html changes.

  Content hash, not mtime: `cp`, bind-mounts, and backup/restore rewrite
  mtimes independently of content, which risks UNDER-invalidation (a
  real content change that keeps its mtime) — the precise failure mode
  here. The frame file is small, so hashing per request is cheap.

  `theme_token` is VESTIGIAL since theme-as-data: the frame no longer has
  the theme server-injected (the client paints it from the __mobius-theme__
  slot + localStorage), so the served frame bytes don't vary by theme and a
  light/dark toggle no longer needs to bust the frame cache. get_frame now
  passes theme_token=None; the parameter is kept (defaulting to "") only so
  any other caller/test stays source-compatible. When non-empty it still
  folds into the validator.

  `frame_rev`: the app-frame.html content hash, already computed once by
  `load_effective_theme` for the same request. Pass it so the frame file
  isn't hashed a SECOND time here — the theme bundle and this ETag share
  one read (both resolve the same candidate list, so the hash is identical;
  see get_frame). When omitted (None), the hash is computed from
  `frame_path` as before, so standalone callers and the unit tests are
  unaffected. An empty rev means the frame was unresolvable — no content
  part, matching the old read-failure fall-through."""
  parts: list[str] = []
  if app.updated_at:
    parts.append(str(int(app.updated_at.timestamp() * 1_000_000)))
  if frame_rev is None:
    try:
      parts.append(hashlib.sha256(frame_path.read_bytes()).hexdigest()[:16])
    except OSError:
      pass
  elif frame_rev:
    parts.append(frame_rev)
  if theme_token:
    parts.append(theme_token)
  if not parts:
    return None
  return 'W/"' + "-".join(parts) + '"'


@router.api_route("/{app_id}/frame", methods=["GET", "HEAD"])
def get_frame(
  app_id: int,
  request: Request,
  db: Session = Depends(get_db),
):
  """Serves the mini-app runtime frame HTML.

  Token-free as of 2026-04-27: the parent shell injects the auth
  token and the current theme via `postMessage` after the iframe
  loads, instead of having them server-templated into the body.

  Cache freshness model: two independent mechanisms COEXIST. The
  compound `_frame_etag` (folding `app.updated_at` with the shared
  frame file's content) plus `Cache-Control: no-cache` drives the
  browser's HTTP-cache revalidation on cold / non-SW paths — the
  browser sends `If-None-Match` and gets a 304 when nothing changed
  or a fresh 200 when `updated_at` advanced or the frame file
  changed. The service worker revalidates frame/module routes against
  the same ETag via `appCodeHandler` in `sw.js`; that cache is ungated
  and applies to every installed app.
  SEPARATELY, `AppCanvas` appends `?v=<app.updated_at>` to the frame
  URL, which the SW keeps as its offline cache key (it strips only
  token/_/install, not `v`), so an app edit changes the SW key and
  forces a fresh load. `v` is purely a client/SW cache-buster — this
  endpoint never reads it.

  Frame is intentionally public — it's just the runtime shell
  (importmap, error UI, postMessage init script). Actual app
  modules at `/api/apps/{id}/module` still require a token. An
  attacker embedding this frame in their own page would receive
  the iframe's `moebius:frame-mounted` postMessage on their parent window,
  but the iframe's origin check (against `window.location.origin`)
  rejects any reply from a non-Möbius origin, so no token can be
  coerced into the frame.
  """
  app = live_app(db, app_id)
  if not app or not app.compiled_path:
    raise HTTPException(status_code=404, detail="App not found.")
  compiled = Path(app.compiled_path)
  if not compiled.exists():
    raise HTTPException(status_code=404, detail="Compiled module missing.")

  # Frame priority: agent-editable copy first, then dev-mode path, then
  # the baked-in fallback. The agent can edit
  # /data/shell/public/app-frame.html directly. Resolve this BEFORE the
  # ETag so the validator reflects the frame file's content (see
  # _frame_etag) — otherwise a changed frame never reaches installed
  # PWAs.
  frame_candidates = [
    Path(get_settings().data_dir) / "shell" / "public" / "app-frame.html",
    Path(__file__).parent.parent.parent.parent
    / "frontend" / "public" / "app-frame.html",
    Path("/app/app-frame.html"),
  ]
  frame_path = next((p for p in frame_candidates if p.exists()), None)
  if frame_path is None:
    raise HTTPException(status_code=404, detail="Frame not found.")

  # The frame is no longer theme-varying: theme-as-data moved theming to the
  # client (the frame's pre-paint IIFE reads the __mobius-theme__ slot +
  # localStorage and paints flash-free; the server no longer injects a
  # <style>). So the validator keys only on app.updated_at + the
  # app-frame.html content hash — NOT the theme. A light/dark toggle no
  # longer needs to bust the frame cache, because the served frame bytes
  # don't change with the theme. Compute the frame content hash directly
  # (theme-independent) and pass theme_token=None.
  frame_rev = theme.frame_content_rev(get_settings().data_dir)
  etag = _frame_etag(app, frame_path, theme_token=None, frame_rev=frame_rev)
  if etag:
    not_modified = _not_modified_if_match(request, etag, app.offline_capable)
    if not_modified is not None:
      return not_modified

  html = frame_path.read_text(encoding="utf-8")

  # Per-app server-side substitution of the app/chat ids the runtime needs.
  html = html.replace(
    "var _FRAME_APP_ID = 'unknown'",
    f"var _FRAME_APP_ID = {json.dumps(str(app_id))}",
  )
  html = html.replace(
    "var _FRAME_CHAT_ID = ''",
    f"var _FRAME_CHAT_ID = {json.dumps(app.chat_id or '')}",
  )

  # Theme-as-data: the frame no longer has the theme server-injected. Its
  # pre-paint IIFE reads the __mobius-theme__ slot (when the server fills
  # one) and the shell's same-origin localStorage to paint --bg / data-theme
  # / color-scheme flash-free from the fallback :root + the persisted owner
  # mode. The parent shell still posts moebius:frame-init/-theme for LIVE
  # swaps without a reload. Removing the injection means the served frame
  # bytes are theme-independent (so the ETag no longer folds the theme).

  headers = {"Cache-Control": "no-cache"}
  if etag:
    headers["ETag"] = etag
  # The X-Mobius-Offline header does not gate frame/module caching: the SW
  # caches code for every installed app via appCodeHandler(OFFLINE_APPS_CACHE,
  # {gated:false}), regardless of this header. It only gates the separate
  # standalone-navigation cache and offline write/open semantics.
  # Offline capability is a function of server state, not a client-pushed list.
  if app.offline_capable:
    headers["X-Mobius-Offline"] = "1"

  # app_open: emit on the GET 200 path only — the 304 short-circuit above
  # already returned for cache-revalidating loads (which would otherwise
  # double-count every freshness check on a navigation back), and a HEAD is
  # an existence probe, not a real open, so it must not count either. Best-
  # effort: a log failure must not block the frame response
  # (activity.log_event swallows its own OSError).
  if request.method != "HEAD":
    activity.log_event(
      "app_open", app_id=app.id, slug=ensure_slug(db, app),
    )
  return HTMLResponse(html, headers=headers)


@router.api_route("/{app_id}/module", methods=["GET", "HEAD"])
def get_module(
  app_id: int,
  request: Request,
  token: str | None = None,
  db: Session = Depends(get_db),
):
  """Serves the compiled JS module for a mini-app.

  Accepts a `token` query parameter so the iframe can load the
  module without custom request headers (dynamic `import()` doesn't
  set an Authorization header).

  Cache freshness: ETag derived from `app.updated_at` (microsecond
  precision) + `Cache-Control: no-cache`. Browser sends
  `If-None-Match` on every fetch; we return 304 when the app hasn't
  changed. Matches the `/frame` route's strategy — see comment
  there for the broader rationale.
  """
  # Apps share modules same as they share storage — every mini-app
  # is authored by the owner's own agent, and a multi-app workflow
  # may legitimately want to import or interop across them. Any
  # valid token (owner or app-scoped) is allowed to fetch any
  # module by id. See CLAUDE.md "Mini-app sandbox — accepted
  # same-origin decision" for the broader trust model. resolve_owner_
  # or_app runs the same decode + revocation check the header deps use,
  # so a signed-out token can't keep pulling module source; the empty-
  # token guard stays explicit to keep the "Valid token required" 401
  # (and to avoid feeding a None token into the JWT decoder).
  if not token:
    raise HTTPException(
      status_code=401, detail="Valid token required."
    )
  resolve_owner_or_app(token, db)
  app = live_app(db, app_id)
  if not app or not app.compiled_path:
    raise HTTPException(status_code=404, detail="Module not found.")
  path = Path(app.compiled_path)
  if not path.exists():
    raise HTTPException(
      status_code=404, detail="Compiled module not found on disk."
    )

  etag = _etag_for_app(app)
  if etag:
    not_modified = _not_modified_if_match(request, etag, app.offline_capable)
    if not_modified is not None:
      return not_modified

  headers = {"Cache-Control": "no-cache"}
  if etag:
    headers["ETag"] = etag
  # See get_frame: X-Mobius-Offline does not gate in-shell module caching.
  # The SW caches modules for every installed app regardless of this header;
  # the header only gates the separate standalone-navigation cache and
  # offline write/open semantics.
  if app.offline_capable:
    headers["X-Mobius-Offline"] = "1"
  # The module is a REVALIDATING response (no-cache + stable ETag), so it
  # must never answer a 206. A `Range: bytes=0-0` probe of a FileResponse
  # would otherwise let Chromium store the 1-byte slice and later serve it
  # as a status-200 full body — a black mini-app until the next app update.
  # Stripping Range here keeps the streamed full-body 200 (see http_caching).
  strip_range(request)
  return FileResponse(
    path,
    media_type="application/javascript",
    headers=headers,
  )


@router.get("/{app_id}/validate")
async def validate_app(
  app_id: int,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
):
  """Validates a compiled mini-app for common issues.

  Checks that the compiled file exists, is parseable JS, exports a
  default, and that the source JSX is present. Returns a report the
  agent can use to decide whether to offer debugging.
  """
  app = live_app_or_404(db, app_id)

  issues = []

  if not app.jsx_source:
    issues.append("No JSX source stored in database.")
  if not app.compiled_path:
    issues.append("No compiled path set — compilation may have failed.")
  else:
    path = Path(app.compiled_path)
    if not path.exists():
      issues.append(
        f"Compiled file missing at {app.compiled_path}."
      )
    else:
      js = path.read_text(encoding="utf-8")
      if not js.strip():
        issues.append("Compiled file is empty.")
      elif not re.search(r"export\s+default\b|export\s*\{[^}]*\bas\s+default\b", js):
        issues.append(
          "Compiled JS has no default export — "
          "the component won't mount."
        )
      # Quick syntax check via node --check if available. Uses
      # asyncio.create_subprocess_exec so the FastAPI event loop
      # stays free while node runs (a blocking subprocess.run here
      # would stall every other request for up to the 5s timeout).
      proc = None
      try:
        proc = await asyncio.create_subprocess_exec(
          "node", "--check", str(path),
          stdout=asyncio.subprocess.PIPE,
          stderr=asyncio.subprocess.PIPE,
        )
        try:
          stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=5,
          )
        except asyncio.TimeoutError:
          # Kill the orphan node process; otherwise it lingers
          # holding the pipe open until the OS reaps it.
          try:
            proc.kill()
            await proc.wait()
          except ProcessLookupError:
            pass
          issues.append("Syntax check timed out.")
        else:
          if proc.returncode != 0:
            stderr = stderr_b.decode("utf-8", errors="replace")
            issues.append(
              f"JS syntax error: {stderr.strip()}"
            )
      except FileNotFoundError:
        pass  # node not available — skip this check

  return {
    "app_id": app.id,
    "name": app.name,
    "valid": len(issues) == 0,
    "issues": issues,
  }



# ---- Publish a project's built static site (feature 136) ----------------
_PUBLISH_PROJECT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PUBLISH_TOKEN_RE = re.compile(r"^[a-f0-9]{16,64}$")


class PublishRequest(BaseModel):
  project_id: str | None = None


def _publish_paths(settings, app, project_id: str | None):
  storage = Path(settings.data_dir) / "apps" / str(app.id)
  base = storage / "projects" / project_id if project_id else storage
  return base / "build" / "site", base / "build" / "publish-token.txt"


@router.post("/{app_id}/publish", dependencies=[Depends(reject_cross_site)])
async def publish_app_site(
  app_id: int,
  body: PublishRequest,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Publish a project's built static site to a stable token URL.

  Snapshots <storage>/[projects/<pid>/]build/site/ to
  <data_dir>/published/<token>/ and returns /sites/<token>/. The token is
  stable per project (kept in the project's build/ dir) so re-publishing
  updates the SAME URL. Owner or the app's own token only.
  """
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(403, "An app may only publish its own site.")
  project_id = (body.project_id or "").strip() or None
  if project_id is not None and not _PUBLISH_PROJECT_RE.match(project_id):
    raise HTTPException(422, "invalid project_id")
  app = live_app_or_404(db, app_id)
  settings = get_settings()
  site_dir, token_file = _publish_paths(settings, app, project_id)
  if not site_dir.is_dir() or not any(site_dir.iterdir()):
    raise HTTPException(400, "No built site to publish — build the project first.")
  token = None
  try:
    existing = token_file.read_text(encoding="utf-8").strip()
    if _PUBLISH_TOKEN_RE.match(existing):
      token = existing
  except OSError:
    pass
  if not token:
    token = uuid.uuid4().hex
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.write_text(token, encoding="utf-8")
  dest = Path(settings.data_dir) / "published" / token

  def _snapshot():
    # Fail closed on symlinks: copytree would otherwise follow a symlink in the
    # (app-controlled) build output and copy its TARGET into the PUBLIC snapshot,
    # exposing arbitrary files at /sites/<token>/. Reject any symlink, and copy
    # with symlinks=True as defense in depth (the serve route's resolve() then
    # confines anything that slips through).
    if site_dir.is_symlink() or any(p.is_symlink() for p in site_dir.rglob("*")):
      raise HTTPException(400, "Built site contains symlinks; refusing to publish.")
    if dest.exists():
      shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(site_dir, dest, symlinks=True)

  await asyncio.to_thread(_snapshot)
  return {"token": token, "url": f"/sites/{token}/"}


@router.delete("/{app_id}/publish", dependencies=[Depends(reject_cross_site)])
async def unpublish_app_site(
  app_id: int,
  project_id: str | None = None,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Take a published site down: remove its snapshot + the stored token."""
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(403, "An app may only unpublish its own site.")
  if project_id and not _PUBLISH_PROJECT_RE.match(project_id):
    raise HTTPException(422, "invalid project_id")
  app = live_app_or_404(db, app_id)
  settings = get_settings()
  _site, token_file = _publish_paths(settings, app, project_id or None)
  try:
    token = token_file.read_text(encoding="utf-8").strip()
  except OSError:
    return {"ok": True}
  if _PUBLISH_TOKEN_RE.match(token or ""):
    dest = Path(settings.data_dir) / "published" / token
    await asyncio.to_thread(shutil.rmtree, dest, ignore_errors=True)
  try:
    token_file.unlink()
  except OSError:
    pass
  return {"ok": True}
