"""Routes for managing the mini-app registry."""

import asyncio
import hashlib
import io
import json
import re
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from app import auth, models, schemas
from app.compiler import compile_jsx
from app.config import get_settings
from app.database import get_db
from app.deps import (
  get_current_owner, get_current_owner_or_app, get_principal, Principal,
  reject_cross_site,
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
  return slug or "app"


def _derive_source_dir(data_dir: str, name: str) -> str:
  """Default source_dir when a caller doesn't provide one.
  Mirrors register_app.py's `/data/apps/<slug>/` convention so the
  watcher's exact-match lookup always finds the app."""
  return str(Path(data_dir) / "apps" / _slugify_for_source_dir(name))


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


@router.post("/install", response_model=schemas.AppInstallOut, status_code=201)
async def install_app(
  body: schemas.AppInstall,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
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
  app, mode, warnings, manifest = await install_from_manifest(
    db,
    manifest_url=body.manifest_url,
    manifest=body.manifest,
    raw_base=body.raw_base,
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
    slug=app.slug,
    manifest_url=app.manifest_url,
    created_at=app.created_at,
    updated_at=app.updated_at,
    mode=mode,
    version=manifest.get("version", "unknown"),
    warnings=warnings,
  )


@router.post("/", response_model=schemas.AppOut, status_code=201)
async def create_app(
  body: schemas.AppCreate,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
):
  """Creates and compiles a new mini-app from JSX source."""
  # Always set source_dir. The file watcher resolves edits via exact
  # source_dir match — apps with NULL source_dir are invisible to
  # auto-recompile and the partner gets the silent "save doesn't
  # land" failure mode. Derive from the name slug (same convention
  # register_app.py uses) when the caller didn't provide one.
  source_dir = body.source_dir or _derive_source_dir(
    get_settings().data_dir, body.name
  )
  app = models.App(
    name=body.name,
    description=body.description,
    jsx_source=body.jsx_source,
    chat_id=body.chat_id,
    source_dir=source_dir,
    cross_app_access=body.cross_app_access,
    share_with_apps=body.share_with_apps,
    offline_capable=body.offline_capable,
    slug=allocate_unique_slug(db, body.name),
    # manifest_url stays NULL on this route. Only the install endpoint
    # may set it — it's the identity key for install-vs-update
    # discrimination. See AppCreate's docstring for the threat model.
  )
  db.add(app)
  db.flush()  # assigns app.id without committing
  try:
    compiled = await compile_jsx(app.id, body.jsx_source)
  except RuntimeError as exc:
    # Roll back explicitly to avoid leaving the SQLite WAL connection in a
    # dirty transaction state, which can cause "database is locked" errors
    # on subsequent writes.
    db.rollback()
    raise HTTPException(status_code=422, detail=str(exc))
  app.compiled_path = compiled
  db.commit()
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
  """Partially updates a mini-app, recompiling if source changed."""
  app = (
    db.query(models.App).filter(models.App.id == app_id).first()
  )
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")
  if body.name is not None:
    app.name = body.name
  if body.description is not None:
    app.description = body.description
  if body.jsx_source is not None:
    app.jsx_source = body.jsx_source
    try:
      compiled = await compile_jsx(app.id, body.jsx_source)
    except RuntimeError as exc:
      db.rollback()
      raise HTTPException(status_code=422, detail=str(exc))
    app.compiled_path = compiled
  if body.chat_id is not None:
    app.chat_id = body.chat_id
  if body.source_dir is not None:
    app.source_dir = body.source_dir
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
  db.commit()
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
  body = await request.body()
  # 12 MB cap on the wire — phone camera photos routinely run 5-8 MB.
  # The standalone shell downscales client-side before upload, so
  # well-behaved clients never approach this; the cap is the safety
  # net for direct-API uploads of giant originals.
  if len(body) > 12 * 1024 * 1024:
    raise HTTPException(413, "Icon too large (max 12 MB).")
  app = (
    db.query(models.App).filter(models.App.id == app_id).first()
  )
  if not app:
    raise HTTPException(404, "App not found.")
  if not body:
    app.icon_png = None
    db.commit()
    return Response(status_code=204)
  from PIL import Image
  try:
    img = Image.open(io.BytesIO(body))
    img.load()
  except Exception:
    raise HTTPException(415, "Not a valid image.")
  # Preserve alpha for PNG / WebP uploads — transparent icons are
  # common (logos with no background) and flattening to RGB renders
  # a hard black square on most home screens.
  if img.mode not in ("RGB", "RGBA"):
    has_alpha = "A" in img.mode or "transparency" in img.info
    img = img.convert("RGBA" if has_alpha else "RGB")
  # Center-square-crop before resize. Non-square inputs would
  # otherwise stretch when the standalone icon route resizes to
  # the requested manifest size (192/512), producing distorted
  # icons. Cropping preserves the most likely subject.
  w, h = img.size
  if w != h:
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
  img.thumbnail((1024, 1024), Image.LANCZOS)
  buf = io.BytesIO()
  img.save(buf, format="PNG", optimize=True)
  app.icon_png = buf.getvalue()
  db.commit()
  return Response(status_code=204)


@router.delete("/{app_id}", status_code=204)
def delete_app(
  app_id: int,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
):
  """Permanently deletes a mini-app — DB row, compiled bundle, source tree.

  This is irreversible.  The caller is expected to confirm with the
  partner before invoking (the agent skill spells this out).
  """
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

  db.delete(app)
  db.commit()

  # Compiled bundle — one file under /data/compiled/.
  if compiled_path:
    try:
      Path(compiled_path).unlink(missing_ok=True)
    except OSError:
      pass  # best effort — a stale compiled file is harmless

  # Source tree under /data/apps/.  Newer apps store the exact source
  # directory; legacy apps fall back to name-based cleanup.
  settings = get_settings()
  apps_root = (Path(settings.data_dir) / "apps").resolve()
  if app_source_dir:
    source_dir = Path(app_source_dir)
    try:
      resolved = source_dir.resolve()
      if (resolved.is_dir()
          and str(resolved).startswith(str(apps_root) + "/")):
        shutil.rmtree(resolved, ignore_errors=True)
    except OSError:
      pass
  elif app_name and re.fullmatch(r"[a-zA-Z0-9_-]+", app_name):
    source_dir = Path(settings.data_dir) / "apps" / app_name
    try:
      resolved = source_dir.resolve()
      if (resolved.is_dir()
          and str(resolved).startswith(str(apps_root) + "/")):
        shutil.rmtree(resolved, ignore_errors=True)
    except OSError:
      pass


@router.post(
  "/{app_id}/run-job",
  status_code=202,
  dependencies=[Depends(reject_cross_site)],
)
def run_app_job(
  app_id: int,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
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

  Owner-only — passes the same defense-in-depth CSRF guard the other
  state-changing endpoints (settings, model-prefs) use.
  """
  from datetime import UTC, datetime
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
  # (Tier 4a). The SW NetworkFirst-caches frame/module only when this
  # header is present, so non-offline_capable apps keep their current
  # network-only behavior. Cacheability is a function of server state,
  # not a client-pushed list — consistent with the ETag freshness model.
  if app.offline_capable:
    headers["X-Mobius-Offline"] = "1"
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
  # same-origin decision" for the broader trust model.
  if not token or not auth.decode_access_token(token):
    raise HTTPException(
      status_code=401, detail="Valid token required."
    )
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
