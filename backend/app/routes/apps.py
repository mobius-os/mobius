"""Routes for managing the mini-app registry."""

import asyncio
import hashlib
import io
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from app import activity, app_git, fs_locks, models, schemas
from app.storage_io import read_capped_body
from app.broadcast import get_system_broadcast
from app.compiler import compile_jsx, recompile_app_bundle
from app.config import get_settings
from app.database import get_db
from app.deps import (
  get_current_owner, get_current_owner_or_app, get_principal, Principal,
  get_owner_or_app_with_manage_apps, reject_cross_site, resolve_owner_or_app,
)

router = APIRouter(prefix="/api/apps", tags=["apps"])


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
  # so a source-dir name is never a bare integer (Codex review #4).
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
      rmtree could delete the other app's storage tree (Codex review #4).
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
  forbid the duplicate at assignment time (Codex review round-9 #3). Compared
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
  still resolves to. Refuses to delete (Codex review #4):
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
  ``rmtree`` is unbounded (Codex review round-7 #4). The caller has ALREADY
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


def allocate_unique_slug(db: Session, name: str, exclude_id: int | None = None) -> str:
  """Returns a slug that isn't taken by any other App row.

  Starts from the name's slug; if it collides, appends -2, -3, ...
  until a free one is found. `exclude_id` lets callers re-allocate
  for an existing row without colliding with itself (e.g. backfill).
  Slugs pin standalone-install identity (manifest `id`) — keep them
  stable across renames so home-screen icons don't orphan.
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
def list_apps(
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Returns all registered mini-apps.

  Pinned apps sort first (newest pin at top of the pinned group),
  then unpinned apps by creation time (oldest first — the drawer's
  apps list has historically been stable-ordered). See `Chat.pinned_at`
  for the same contract on chats.
  """
  return (
    db.query(models.App)
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
  source_dir → seed storage → register cron, all inside one DB
  transaction with filesystem rollback on failure.

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
    created_at=app.created_at,
    updated_at=app.updated_at,
    mode=mode,
    version=manifest.get("version", "unknown"),
    warnings=warnings,
    conflict_paths=conflict_paths,
    divergence=divergence,
  )


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
  # must only read its OWN app's preview — never another app's. The owner
  # (app_id is None) may read any. Mirrors run-job's scope guard.
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(status_code=403, detail="Not your app.")
  app = db.query(models.App).filter(models.App.id == app_id).first()
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")
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


@router.post("/", response_model=schemas.AppOut, status_code=201)
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
  # that uninstall could rmtree the directory (Codex review round-6 #4), and so
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
  return app


@router.get("/{app_id}", response_model=schemas.AppOut)
def get_app(
  app_id: int,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Returns a single mini-app by ID."""
  app = (
    db.query(models.App).filter(models.App.id == app_id).first()
  )
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")
  return app


@router.patch("/{app_id}", response_model=schemas.AppOut)
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
    app = (
      db.query(models.App).populate_existing()
      .filter(models.App.id == app_id).first()
    )
    if not app:
      raise HTTPException(status_code=404, detail="App not found.")
    if body.name is not None:
      app.name = body.name
    if body.description is not None:
      app.description = body.description
    if body.chat_id is not None:
      app.chat_id = body.chat_id
    if new_source_dir is not None:
      app.source_dir = new_source_dir
    if body.pinned is not None:
      from datetime import UTC, datetime
      app.pinned_at = (
        datetime.now(UTC).replace(tzinfo=None) if body.pinned else None
      )
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
      await _recompile_and_commit(app)
    db.refresh(app)
  return app


@router.put("/{app_id}/icon", status_code=204)
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
  # giant direct-API upload can't OOM the host (Codex review round-9 #4).
  body = await read_capped_body(request, cap=12 * 1024 * 1024)
  # Capture the app's identity at authorization; recheck the nonce under the
  # per-app lock so a slow icon upload can't alter a DIFFERENT app that reused
  # this id between authorization and commit — the same id-reuse race fixed for
  # storage PUT/DELETE (Codex review round-10 #3).
  app0 = db.query(models.App).filter(models.App.id == app_id).first()
  if not app0:
    raise HTTPException(404, "App not found.")
  expected_nonce = app0.token_nonce
  # Decode/normalize via the SHARED installer pipeline, which inspects the
  # image header dimensions BEFORE img.load() so a decompression bomb is
  # rejected before it can allocate (Codex review round-10 #4). Done outside
  # the lock — only the DB mutation needs serializing. Lazy import avoids the
  # install.py <-> routes.apps circular import.
  from app.install import _process_icon
  processed = _process_icon(body) if body else None
  async with fs_locks.app_storage_lock(app_id):
    app = (
      db.query(models.App).populate_existing()
      .filter(models.App.id == app_id).first()
    )
    if app is None or app.token_nonce != expected_nonce:
      raise HTTPException(404, "App not found.")
    app.icon_png = processed
    db.commit()
  return Response(status_code=204)


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
  """Permanently deletes a mini-app — DB row, compiled bundle, source tree.

  This is irreversible.  The caller is expected to confirm with the
  partner before invoking (the agent skill spells this out).

  Async so the source-tree and storage-tree cleanup can run under the same
  per-source-dir / per-app fs_locks a concurrent create / patch / storage
  write takes, closing the uninstall-vs-create and uninstall-vs-write races
  (Codex review round-6 #3, #4) on the single uvicorn worker.
  """
  # Take the lifecycle lock (vs a concurrent install) THEN the per-app storage
  # lock (vs a concurrent write), in that order (see fs_locks LOCK ORDERING).
  # The per-app lock is held across the ENTIRE uninstall — row delete THROUGH
  # storage-tree removal. SQLite reuses a freed integer id, so if the lock were
  # taken only around the rmtree, a replacement app could reuse this id, write
  # valid storage, and then have its tree deleted by this cleanup (Codex review
  # round-7 #1). Holding it from before the delete means any write for this id
  # (which also takes this lock) serializes after us, and the rmtree only ever
  # removes THIS app's (empty-or-stale) tree.
  async with (
    fs_locks.install_uninstall_lock(),
    fs_locks.app_storage_lock(app_id),
  ):
    app = (
      db.query(models.App).filter(models.App.id == app_id).first()
    )
    if not app:
      raise HTTPException(status_code=404, detail="App not found.")

    # Capture paths before dropping the DB row.  Delete the row first so
    # a partial filesystem cleanup leaves the registry coherent — stale
    # files are harmless orphans, a DB row pointing at missing files is
    # a live 404.
    compiled_path = app.compiled_path
    app_name = app.name
    app_source_dir = app.source_dir
    deleted_app_id = app.id

    db.delete(app)
    db.commit()

    # Notify the Shell that the app registry changed. The handler in
    # Shell.jsx refetches /api/apps/ and reconciles the drawer; an
    # app_updated for an id that no longer exists simply causes that id
    # to disappear from the next render. Published after the commit so an
    # event never refers to an app the rollback would have kept alive.
    get_system_broadcast().publish(
      {"type": "app_updated", "appId": str(deleted_app_id)}
    )

    # Compiled bundle — one file under /data/compiled/.
    if compiled_path:
      try:
        Path(compiled_path).unlink(missing_ok=True)
      except OSError:
        pass  # best effort — a stale compiled file is harmless

    settings = get_settings()
    apps_root = (Path(settings.data_dir) / "apps").resolve()

    # Source tree under /data/apps/.  Newer apps store the exact source
    # directory; legacy apps fall back to name-based cleanup. Resolve the
    # candidate, then dedup-check + cron-drop + rmtree UNDER the per-source-dir
    # lock so a concurrent create/patch/install can't claim the directory
    # between the shared-dir check and the rmtree (round-6 #4). The blocking
    # cron-drop + rmtree run in a thread so they don't stall the loop while the
    # lock is held (round-7 #4).
    resolved_source = None
    if app_source_dir:
      try:
        resolved_source = Path(app_source_dir).resolve()
      except OSError:
        resolved_source = None
    elif app_name and re.fullmatch(r"[a-zA-Z0-9_-]+", app_name):
      try:
        resolved_source = (
          Path(settings.data_dir) / "apps" / app_name
        ).resolve()
      except OSError:
        resolved_source = None
    if resolved_source is not None:
      async with fs_locks.source_dir_lock(str(resolved_source)):
        if _safe_to_rmtree_source(
          resolved_source, apps_root, db, deleted_app_id
        ):
          await asyncio.to_thread(_drop_cron_and_rmtree, resolved_source)

    # Per-app STORAGE tree under /data/apps/<numeric-id>/ — distinct from the
    # slug-keyed SOURCE tree above. /api/storage/apps/{id}/... writes land here
    # keyed by the integer id (Codex review #1). rmtree in a thread (round-7
    # #4); still under the outer per-app lock.
    storage_dir = apps_root / str(deleted_app_id)
    if storage_dir.is_dir():
      await asyncio.to_thread(shutil.rmtree, storage_dir, ignore_errors=True)


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
  layout in `app.install`) and job_name comes from the manifest's
  `schedule.job` field (default "fetch.sh"). Both candidates are
  tried so apps installed before the manifest convention solidified
  still work.

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
  app = db.query(models.App).filter(models.App.id == app_id).first()
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")
  if not app.source_dir:
    raise HTTPException(
      status_code=400, detail="App has no source_dir; cannot locate job.",
    )
  source_dir = Path(app.source_dir)
  # Try the conventional names: fetch.sh (current app-news convention)
  # and job.sh (the install-from-manifest default). First hit wins.
  job_path = None
  for candidate in ("fetch.sh", "job.sh"):
    p = source_dir / candidate
    if p.is_file():
      job_path = p
      break
  if job_path is None:
    raise HTTPException(
      status_code=400,
      detail="No job script found (looked for fetch.sh, job.sh).",
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
  cache directive as the 200 it stands in for (the SW's
  cacheWillUpdate gate keys on that header)."""
  match = request.headers.get("if-none-match")
  if match and etag in [v.strip() for v in match.split(",")]:
    headers = {"ETag": etag}
    if offline:
      headers["X-Mobius-Offline"] = "1"
    return Response(status_code=304, headers=headers)
  return None


def _frame_etag(app: models.App, frame_path: Path) -> str | None:
  """Validator for the `/frame` response, combining the app's
  `updated_at` with the shared runtime-frame file's mtime.

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
  here. The frame file is small, so hashing per request is cheap."""
  parts: list[str] = []
  if app.updated_at:
    parts.append(str(int(app.updated_at.timestamp() * 1_000_000)))
  try:
    parts.append(hashlib.sha256(frame_path.read_bytes()).hexdigest()[:16])
  except OSError:
    pass
  if not parts:
    return None
  return 'W/"' + "-".join(parts) + '"'


@router.get("/{app_id}/frame")
def get_frame(
  app_id: int,
  request: Request,
  db: Session = Depends(get_db),
):
  """Serves the mini-app runtime frame HTML.

  Token-free as of 2026-04-27: the parent shell injects the auth
  token and the current theme via `postMessage` after the iframe
  loads, instead of having them server-templated into the body.

  Cache freshness model (2026-05-25 refactor): URL is stable per
  app_id (no `?v=` query). Response carries an ETag derived from
  `app.updated_at` and `Cache-Control: no-cache`. Browsers send
  `If-None-Match` on every navigation; we return 304 with empty
  body when the app hasn't been updated, or 200 with the fresh
  frame when it has. This removed the SW cache-first interception
  for this route — the browser HTTP cache + ETag validation handle
  it natively, which means the agent's fresh-Chromium tests and
  the user's persistent-PWA cache converge on identical behavior
  (the previous `?v=` counter was an in-memory value that reset on
  reload, leaving the user pinned to whatever broken module they
  first cached).

  Frame is intentionally public — it's just the runtime shell
  (importmap, error UI, postMessage init script). Actual app
  modules at `/api/apps/{id}/module` still require a token. An
  attacker embedding this frame in their own page would receive
  the iframe's `frame-ready` postMessage on their parent window,
  but the iframe's origin check (against `window.location.origin`)
  rejects any reply from a non-Möbius origin, so no token can be
  coerced into the frame.
  """
  app = db.query(models.App).filter(models.App.id == app_id).first()
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

  etag = _frame_etag(app, frame_path)
  if etag:
    not_modified = _not_modified_if_match(request, etag, app.offline_capable)
    if not_modified is not None:
      return not_modified

  html = frame_path.read_text(encoding="utf-8")

  # Per-app server-side substitutions. The TOKEN (per-session) and
  # THEME (per-user-edit) are intentionally NOT substituted
  # server-side — the parent shell sends them via postMessage after
  # iframe load.
  html = html.replace(
    "var _FRAME_APP_ID = 'unknown'",
    f"var _FRAME_APP_ID = {json.dumps(str(app_id))}",
  )
  html = html.replace(
    "var _FRAME_CHAT_ID = ''",
    f"var _FRAME_CHAT_ID = {json.dumps(app.chat_id or '')}",
  )

  headers = {"Cache-Control": "no-cache"}
  if etag:
    headers["ETag"] = etag
  # Tells the service worker this app is safe to cache for offline use
  # (Tier 4a). The SW's offlineCapableHandler caches frame/module only when
  # this header is present, so non-offline_capable apps keep their network-only
  # behavior. The SW serves them connectivity-aware: network-first when
  # known-online (fresh app code on an edit), cache-first when not (instant
  # offline). Cacheability is a function of server state, not a client-pushed
  # list — consistent with the ETag freshness model.
  if app.offline_capable:
    headers["X-Mobius-Offline"] = "1"

  # app_open: emit on the 200 path only — the 304 short-circuit above
  # already returned for cache-revalidating loads, which would
  # otherwise double-count every time the iframe checks freshness on
  # a navigation back. Best-effort: a log failure must not block the
  # frame response (activity.log_event swallows its own OSError).
  activity.log_event(
    "app_open", app_id=app.id, slug=ensure_slug(db, app),
  )
  return HTMLResponse(html, headers=headers)


@router.get("/{app_id}/module")
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
  app = (
    db.query(models.App).filter(models.App.id == app_id).first()
  )
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
  # See get_frame — marks an offline_capable app's module cacheable by
  # the service worker (Tier 4a).
  if app.offline_capable:
    headers["X-Mobius-Offline"] = "1"
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
  app = (
    db.query(models.App).filter(models.App.id == app_id).first()
  )
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")

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
