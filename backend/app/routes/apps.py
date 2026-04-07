"""Routes for managing the mini-app registry."""

import json
import subprocess
from pathlib import Path
from urllib.parse import urlencode

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
  """Deletes a mini-app from the registry."""
  app = (
    db.query(models.App).filter(models.App.id == app_id).first()
  )
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")
  db.delete(app)
  db.commit()


@router.get("/{app_id}/frame")
def get_frame(
  app_id: int,
  token: str | None = None,
  db: Session = Depends(get_db),
):
  """Serves the mini-app runtime frame with the module inlined.

  The compiled JS is embedded directly in the HTML so the sandboxed
  iframe (no allow-same-origin) doesn't need to make any cross-origin
  requests to load the module.  External imports (react, recharts) come
  from the esm.sh CDN via the importmap and work from any origin.
  """
  if not token or not auth.decode_access_token(token):
    raise HTTPException(
      status_code=401, detail="Valid token required."
    )
  app = db.query(models.App).filter(models.App.id == app_id).first()
  if not app or not app.compiled_path:
    raise HTTPException(status_code=404, detail="App not found.")
  compiled = Path(app.compiled_path)
  if not compiled.exists():
    raise HTTPException(status_code=404, detail="Compiled module missing.")

  frame_path = (
    Path(__file__).parent.parent.parent.parent
    / "frontend" / "public" / "app-frame.html"
  )
  if not frame_path.exists():
    frame_path = Path("/app/app-frame.html")
  if not frame_path.exists():
    raise HTTPException(status_code=404, detail="Frame not found.")

  html = frame_path.read_text(encoding="utf-8")

  # Inject theme CSS (default or override) so mini-apps match the shell.
  from app.theme import inject_theme_into_html
  html = inject_theme_into_html(html, get_settings().data_dir)

  # Inject appId/token into the module script so it can load the component.
  html = html.replace(
    'const params = new URLSearchParams(location.search);',
    'const params = new URLSearchParams("'
    + urlencode({"appId": app_id, "token": token})
    + '");',
  )
  # Inject appId/chatId into the plain script globals so reportError()
  # can route errors back to the correct chat.  These placeholders must
  # match the literals in app-frame.html exactly.
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
  return HTMLResponse(html)


@router.get("/{app_id}/module")
def get_module(
  app_id: int,
  token: str | None = None,
  db: Session = Depends(get_db),
):
  """Serves the compiled JS module for a mini-app.

  Accepts a token query parameter so that the app-frame iframe
  can load the module without custom request headers.
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
  return FileResponse(
    path,
    media_type="application/javascript",
    headers={"Cache-Control": "no-cache"},
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
