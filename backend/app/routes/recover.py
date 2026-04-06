"""Recovery page: static HTML, password-authenticated, independent of the
React frontend.  Works even if the agent breaks the shell."""

import io
import os
import shutil
import sqlite3
import subprocess
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import auth, models
from app.config import get_settings
from app.database import get_db
from app.routes.recover_html import dashboard_html, login_html
from app.theme import inject_theme_into_html

router = APIRouter(tags=["recover"])
_limiter = Limiter(key_func=get_remote_address)

# Session tokens are short-lived, stored in a cookie.  They are separate
# from JWT tokens so recovery works even if JWT logic is broken.
_COOKIE = "moebius_recover"


def _themed(html_str: str) -> str:
  """Injects the active theme CSS into a recover page HTML string."""
  return inject_theme_into_html(html_str, get_settings().data_dir)


def _verify_session(request: Request, db: Session) -> models.Owner:
  """Returns the owner if the recovery session cookie is valid."""
  token = request.cookies.get(_COOKIE)
  if not token:
    raise HTTPException(status_code=401)
  payload = auth.decode_access_token(token)
  if not payload:
    raise HTTPException(status_code=401)
  owner = (
    db.query(models.Owner)
    .filter(models.Owner.username == payload.get("sub"))
    .first()
  )
  if not owner:
    raise HTTPException(status_code=401)
  return owner


# -- Pages ------------------------------------------------------------

@router.get("/recover", response_class=HTMLResponse)
def recover_page(request: Request, db: Session = Depends(get_db)):
  """Serves the recovery login form or the recovery dashboard."""
  token = request.cookies.get(_COOKIE)
  if token and auth.decode_access_token(token):
    return HTMLResponse(_themed(dashboard_html()))
  return HTMLResponse(_themed(login_html()))


@router.post("/recover/auth")
@_limiter.limit("5/minute")
def recover_auth(
  request: Request,
  username: str = Form(...),
  password: str = Form(...),
  db: Session = Depends(get_db),
):
  """Authenticates and sets a recovery session cookie."""
  owner = (
    db.query(models.Owner)
    .filter(models.Owner.username == username)
    .first()
  )
  if not owner or not auth.verify_password(password, owner.hashed_password):
    return HTMLResponse(_themed(login_html(error="Incorrect username or password.")))
  token = auth.create_access_token({"sub": owner.username})
  resp = HTMLResponse(_themed(dashboard_html()))
  # Only set the secure flag when not running on localhost — local dev uses HTTP.
  is_secure = get_settings().domain not in ("localhost", "127.0.0.1", "")
  resp.set_cookie(
    _COOKIE, token,
    httponly=True, samesite="strict", max_age=3600,
    secure=is_secure,
  )
  return resp


def _action_reset_apps(data_dir: Path, db: Session) -> str:
  """Deletes all apps from the database and clears compiled output."""
  db.query(models.App).delete()
  db.commit()
  _rm_tree(data_dir / "compiled")
  (data_dir / "compiled").mkdir(parents=True, exist_ok=True)
  return "All apps have been reset."


def _action_reset_chat(data_dir: Path, db: Session) -> str:
  """Clears the debug log file at /data/logs/chat.log.

  This does not affect chat history — conversations are stored server-side
  in SQLite and are unaffected by this action.
  """
  log_file = data_dir / "logs" / "chat.log"
  if log_file.exists():
    log_file.write_text("", encoding="utf-8")
  return "Debug chat log cleared."


def _action_reset_settings(data_dir: Path, db: Session) -> str:
  """Clears CLI auth credentials so the user can re-authenticate."""
  _rm_tree(data_dir / "cli-auth")
  (data_dir / "cli-auth").mkdir(parents=True, exist_ok=True)
  return "CLI auth cleared.  Sign in again via the setup wizard."


def _action_restore_shell(data_dir: Path, db: Session) -> str:
  """Rebuilds the frontend from the original source baked into the image.

  Restores /data/shell/src from /app/shell-src/src, then runs npm build.
  Does not touch the database, compiled mini-apps, or CLI auth.
  """
  shell_dir = data_dir / "shell"
  shell_src = Path("/app/shell-src")
  if not shell_src.exists():
    return "Error: /app/shell-src not found in image."
  # Restore original source files (agent may have modified or corrupted them).
  src_dest = shell_dir / "src"
  _rm_tree(src_dest)
  shutil.copytree(shell_src / "src", src_dest)
  shutil.copy2(shell_src / "package.json", shell_dir / "package.json")
  shutil.copy2(shell_src / "vite.config.js", shell_dir / "vite.config.js")
  # Ensure node_modules are present (image has them in shell-src).
  if not (shell_dir / "node_modules").exists():
    shutil.copytree(shell_src / "node_modules", shell_dir / "node_modules")
  # Run the build.
  result = subprocess.run(
    ["npm", "run", "build"],
    cwd=str(shell_dir),
    capture_output=True,
    text=True,
    timeout=120,
  )
  if result.returncode != 0:
    return f"Build failed: {result.stderr[-600:]}"
  return "Shell restored and rebuilt from original source. Reload the app."


def _action_factory_reset(data_dir: Path, db: Session) -> None:
  """Deletes everything: apps, owner, storage, compiled files.

  Returns None to signal that the caller should redirect to the login page
  rather than the dashboard.
  """
  db.query(models.App).delete()
  db.query(models.Owner).delete()
  db.commit()
  for subdir in ["compiled", "apps", "shared", "logs", "cli-auth"]:
    _rm_tree(data_dir / subdir)
    (data_dir / subdir).mkdir(parents=True, exist_ok=True)


# Maps action names to handler functions.  download_backup is handled
# separately because it returns a StreamingResponse, not a plain message.
_ACTION_HANDLERS = {
  "reset_apps": _action_reset_apps,
  "reset_chat": _action_reset_chat,
  "reset_settings": _action_reset_settings,
  "restore_shell": _action_restore_shell,
}


@router.post("/recover/action")
@_limiter.limit("10/minute")
def recover_action(
  request: Request,
  action: str = Form(...),
  db: Session = Depends(get_db),
):
  """Executes a recovery action."""
  owner = _verify_session(request, db)
  settings = get_settings()
  data_dir = Path(settings.data_dir)

  if action == "download_backup":
    return _create_backup(db, data_dir)

  if action == "factory_reset":
    _action_factory_reset(data_dir, db)
    resp = HTMLResponse(_themed(login_html(
      error="Factory reset complete.  Set up your account again."
    )))
    resp.delete_cookie(_COOKIE)
    return resp

  handler = _ACTION_HANDLERS.get(action)
  if handler is None:
    return HTMLResponse(_themed(dashboard_html(msg="Unknown action.")))

  msg = handler(data_dir, db)
  return HTMLResponse(_themed(dashboard_html(msg=msg)))


# -- Backup ------------------------------------------------------------

def _backup_db(src_path: Path, dest_path: Path) -> None:
  """Copies the SQLite database using the online backup API for consistency.

  A raw file copy of an open WAL-mode database can produce an inconsistent
  snapshot.  The backup API performs a live, consistent copy regardless of
  concurrent writes.
  """
  src = sqlite3.connect(str(src_path))
  dst = sqlite3.connect(str(dest_path))
  src.backup(dst)
  dst.close()
  src.close()


def _create_backup(db: Session, data_dir: Path) -> StreamingResponse:
  """Creates a ZIP archive of the database and app data.

  Backup includes CLI auth credentials. Store the backup file securely.
  """
  buf = io.BytesIO()
  with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
    # Database — use the online backup API for a consistent WAL-mode snapshot.
    db_path = data_dir / "db" / "ultimate.db"
    if db_path.exists():
      with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_db_path = Path(tmp.name)
      try:
        _backup_db(db_path, tmp_db_path)
        zf.write(tmp_db_path, "db/ultimate.db")
      finally:
        tmp_db_path.unlink(missing_ok=True)
    # App storage.
    for sub in ["apps", "shared", "compiled"]:
      base = data_dir / sub
      if not base.exists():
        continue
      for f in base.rglob("*"):
        if f.is_file():
          zf.write(f, str(f.relative_to(data_dir)))
    # CLI auth credentials (OAuth tokens for Claude and other providers).
    cli_auth = data_dir / "cli-auth"
    if cli_auth.exists():
      for f in cli_auth.rglob("*"):
        if f.is_file():
          zf.write(f, str(f.relative_to(data_dir)))
  buf.seek(0)
  ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
  return StreamingResponse(
    buf,
    media_type="application/zip",
    headers={
      "Content-Disposition": f'attachment; filename="moebius-backup-{ts}.zip"'
    },
  )


# -- Helpers -----------------------------------------------------------

def _force_remove(_func, path, _exc_info):
  """Handles permission errors during rmtree by chmod-ing and retrying."""
  os.chmod(path, 0o755)
  _func(path)


def _rm_tree(path: Path) -> None:
  """Recursively removes a directory tree, forcing past read-only files."""
  if not path.exists():
    return
  shutil.rmtree(path, onexc=_force_remove)


