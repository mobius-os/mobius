"""Routes for managing the mini-app registry."""

import json
import re
import shutil
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy.orm import Session

from app import auth, models, schemas
from app.compiler import compile_jsx
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_owner, get_current_owner_or_app

router = APIRouter(prefix="/api/apps", tags=["apps"])


@router.get("/", response_model=list[schemas.AppOut])
def list_apps(
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Returns all registered mini-apps ordered by creation time."""
  return (
    db.query(models.App).order_by(models.App.created_at).all()
  )


@router.post("/", response_model=schemas.AppOut, status_code=201)
async def create_app(
  body: schemas.AppCreate,
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner),
):
  """Creates and compiles a new mini-app from JSX source."""
  app = models.App(
    name=body.name,
    description=body.description,
    jsx_source=body.jsx_source,
    chat_id=body.chat_id,
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

  db.delete(app)
  db.commit()

  # Compiled bundle — one file under /data/compiled/.
  if compiled_path:
    try:
      Path(compiled_path).unlink(missing_ok=True)
    except OSError:
      pass  # best effort — a stale compiled file is harmless

  # Source tree under /data/apps/<slug>/.  Only delete directories whose
  # name is a URL-safe slug, to avoid path-traversal via a tampered name
  # field.  If the agent used a non-slug name, the source tree is left.
  if app_name and re.fullmatch(r"[a-zA-Z0-9_-]+", app_name):
    settings = get_settings()
    source_dir = Path(settings.data_dir) / "apps" / app_name
    try:
      resolved = source_dir.resolve()
      apps_root = (Path(settings.data_dir) / "apps").resolve()
      if (resolved.is_dir()
          and str(resolved).startswith(str(apps_root) + "/")):
        shutil.rmtree(resolved, ignore_errors=True)
    except OSError:
      pass


@router.get("/{app_id}/frame")
def get_frame(
  app_id: int,
  v: int = 0,
  db: Session = Depends(get_db),
):
  """Serves the mini-app runtime frame HTML.

  Token-free as of 2026-04-27: the parent shell injects the auth
  token and the current theme via `postMessage` after the iframe
  loads, instead of having them server-templated into the body.
  This makes the frame HTML cacheable across sessions: same
  (app_id, version) ↔ same response bytes ↔ SW cache hit. With the
  cache, opening any previously-visited app on a cold PWA start
  saves the round-trip to the server.

  Frame is intentionally public — it's just the runtime shell
  (importmap, error UI, postMessage init script). Actual app
  modules at `/api/apps/{id}/module` still require a token. An
  attacker embedding this frame in their own page would receive
  the iframe's `frame-ready` postMessage on their parent window,
  but the iframe's origin check (against `_FRAME_PARENT_ORIGIN`,
  baked in below) rejects any reply from a non-Möbius origin, so
  no token can be coerced into the frame.
  """
  app = db.query(models.App).filter(models.App.id == app_id).first()
  if not app or not app.compiled_path:
    raise HTTPException(status_code=404, detail="App not found.")
  compiled = Path(app.compiled_path)
  if not compiled.exists():
    raise HTTPException(status_code=404, detail="Compiled module missing.")

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

  # Per-app server-side substitutions. These are stable per-app so
  # they don't break cacheability of the response. The TOKEN
  # (per-session) and THEME (per-user-edit) are intentionally NOT
  # substituted server-side — the parent shell sends them via
  # postMessage after iframe load. See app-frame.html init script.
  html = html.replace(
    "var _FRAME_APP_ID = 'unknown'",
    f"var _FRAME_APP_ID = {json.dumps(str(app_id))}",
  )
  html = html.replace(
    "var _FRAME_CHAT_ID = ''",
    f"var _FRAME_CHAT_ID = {json.dumps(app.chat_id or '')}",
  )
  html = html.replace(
    "var _FRAME_PARENT_ORIGIN = 'UNSET'",
    f"var _FRAME_PARENT_ORIGIN = {json.dumps(get_settings().frontend_origin)}",
  )

  # When versioned, treat as immutable — the agent bumps `v` on every
  # update so cache invalidation is automatic. The SW also caches this
  # cache-first; long-lived since URL changes on app update.
  cache_header = (
    "public, max-age=31536000, immutable"
    if v
    else "no-cache"
  )
  return HTMLResponse(html, headers={"Cache-Control": cache_header})


@router.get("/{app_id}/module")
def get_module(
  app_id: int,
  token: str | None = None,
  v: int | None = None,
  db: Session = Depends(get_db),
):
  """Serves the compiled JS module for a mini-app.

  Accepts a token query parameter so that the app-frame iframe
  can load the module without custom request headers.

  Caching: when a version (`v`) is supplied, the URL is treated as
  immutable — the client may cache the response indefinitely. The
  agent bumps the app version on every update via the `app_updated`
  event flow, which changes the URL and invalidates the cache
  naturally. Without `v`, fall back to no-cache so legacy clients
  never get stuck on stale modules.
  """
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
  cache_header = (
    "public, max-age=31536000, immutable"
    if v is not None
    else "no-cache"
  )
  return FileResponse(
    path,
    media_type="application/javascript",
    headers={"Cache-Control": cache_header},
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
