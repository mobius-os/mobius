"""Routes for managing the mini-app registry."""

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
from app.deps import get_current_owner, get_current_owner_or_app

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
  db.commit()
  db.refresh(app)
  return app


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


def _etag_for_app(app: models.App) -> str | None:
  """Weak ETag derived from `app.updated_at`. Microsecond precision
  so two updates within the same wall-clock second produce different
  validators — second-precision risks the agent shipping a fix and
  the user's cached browser refusing to revalidate."""
  if not app.updated_at:
    return None
  ts_us = int(app.updated_at.timestamp() * 1_000_000)
  return f'W/"{ts_us}"'


def _not_modified_if_match(request: Request, etag: str) -> Response | None:
  """Returns a 304 Response if the request's If-None-Match matches
  `etag`, else None. The 304 must keep the ETag header so a browser
  re-validating an existing cache entry can keep its validator."""
  match = request.headers.get("if-none-match")
  if match and etag in [v.strip() for v in match.split(",")]:
    return Response(status_code=304, headers={"ETag": etag})
  return None


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

  etag = _etag_for_app(app)
  if etag:
    not_modified = _not_modified_if_match(request, etag)
    if not_modified is not None:
      return not_modified

  # Frame priority: agent-editable copy first, then dev-mode path, then
  # the baked-in fallback. The agent can edit
  # /data/shell/public/app-frame.html directly.
  frame_candidates = [
    Path(get_settings().data_dir) / "shell" / "public" / "app-frame.html",
    Path(__file__).parent.parent.parent.parent
    / "frontend" / "public" / "app-frame.html",
    Path("/app/app-frame.html"),
  ]
  frame_path = next((p for p in frame_candidates if p.exists()), None)
  if frame_path is None:
    raise HTTPException(status_code=404, detail="Frame not found.")

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
    not_modified = _not_modified_if_match(request, etag)
    if not_modified is not None:
      return not_modified

  headers = {"Cache-Control": "no-cache"}
  if etag:
    headers["ETag"] = etag
  return FileResponse(
    path,
    media_type="application/javascript",
    headers=headers,
  )


@router.get("/{app_id}/validate")
def validate_app(
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
      elif "export default" not in js and "export{" not in js:
        issues.append(
          "Compiled JS has no default export — "
          "the component won't mount."
        )
      # Quick syntax check via node --check if available.
      try:
        result = subprocess.run(
          ["node", "--check", str(path)],
          capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
          issues.append(
            f"JS syntax error: {result.stderr.strip()}"
          )
      except FileNotFoundError:
        pass  # node not available — skip this check
      except subprocess.TimeoutExpired:
        issues.append("Syntax check timed out.")

  return {
    "app_id": app.id,
    "name": app.name,
    "valid": len(issues) == 0,
    "issues": issues,
  }
