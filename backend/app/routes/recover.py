"""Recovery page: static HTML, password-authenticated, independent of the
React frontend.  Works even if the agent breaks the shell."""

import io
import os
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import models, recover_auth
from app.config import get_settings
from app.database import get_db
from app.routes.recover_html import dashboard_html, login_html
from app.theme import inject_theme_into_html

router = APIRouter(tags=["recover"])
_limiter = Limiter(key_func=get_remote_address)

# Session tokens come from `recover_auth` (HMAC-signed, no JWT
# library) so recovery works even if `app/auth.py` is broken or
# corrupted by the agent. The cookie name + TTL match what the
# previous JWT-based implementation used; existing recover sessions
# carry over after deploy.
_COOKIE = recover_auth.COOKIE_NAME

# Serializes the restore-shell action. FastAPI runs sync route handlers
# in a threadpool, so two concurrent /recover/action POSTs can run
# _action_restore_shell in parallel — and the dist-swap (rename to
# dist.bak, run build, rename back on failure) is not safe under
# concurrency: thread A's rename clobbers thread B's backup, both end
# up with no dist on a failure path. Lock at the action boundary so
# only one shell restore runs at a time.
_RESTORE_SHELL_LOCK = threading.Lock()


def _themed(html_str: str) -> str:
  """Injects the active theme CSS into a recover page HTML string."""
  return inject_theme_into_html(html_str, get_settings().data_dir)


def _verify_session(request: Request, db: Session) -> models.Owner:
  """Returns the owner if the recovery session cookie is valid."""
  token = request.cookies.get(_COOKIE)
  username = recover_auth.decode_session_token(token)
  if not username:
    raise HTTPException(status_code=401)
  owner = (
    db.query(models.Owner)
    .filter(models.Owner.username == username)
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
  if token and recover_auth.decode_session_token(token):
    return HTMLResponse(_themed(dashboard_html()))
  return HTMLResponse(_themed(login_html()))


def _request_is_https(request: Request) -> bool:
  """Detects whether the inbound request was served over TLS.

  Prefer the actual request scheme over a configured DOMAIN value:
  Möbius commonly runs behind a TLS-terminating reverse proxy (Caddy)
  on a public hostname, but in containers DOMAIN may be set to
  'localhost' or be unset entirely. We want Secure=True whenever the
  browser used HTTPS, regardless of internal routing.
  """
  forwarded = request.headers.get("x-forwarded-proto", "").lower().split(",")[0].strip()
  if forwarded == "https":
    return True
  if forwarded == "http":
    return False
  return request.url.scheme == "https"


@router.post("/recover/auth")
@_limiter.limit("5/minute")
def recover_login(
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
  if not owner or not recover_auth.verify_password(password, owner.hashed_password):
    return HTMLResponse(_themed(login_html(error="Incorrect username or password.")))
  token = recover_auth.create_session_token(owner.username)
  resp = HTMLResponse(_themed(dashboard_html()))
  resp.set_cookie(
    _COOKIE, token,
    httponly=True, samesite="strict", max_age=3600,
    secure=_request_is_https(request),
  )
  return resp


@router.post("/recover/logout")
def recover_logout(request: Request):
  """Clears the recovery session cookie and returns the login page."""
  resp = HTMLResponse(_themed(login_html()))
  resp.delete_cookie(_COOKIE)
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

  Atomicity: Vite's build cleans the outDir before populating it. If
  the build times out or fails partway, `dist` would be left empty
  or partial — the React app would 404 on assets until /app/static
  fallback is consulted. We preserve the current dist as `dist.bak`
  before the build, restore from it on any failure, and only remove
  the backup on success. Worst case, the user sees the same shell
  they had before the action.

  Concurrency: serialized by `_RESTORE_SHELL_LOCK`. Two simultaneous
  POSTs from the recovery page would otherwise race the dist.bak
  swap and end up with no dist on a failure path.
  """
  if not _RESTORE_SHELL_LOCK.acquire(blocking=False):
    return "Another shell restore is already running. Wait for it to finish."
  try:
    return _do_restore_shell(data_dir)
  finally:
    _RESTORE_SHELL_LOCK.release()


def _do_restore_shell(data_dir: Path) -> str:
  """Inner restore-shell implementation. Caller holds _RESTORE_SHELL_LOCK."""
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

  dist_dir = shell_dir / "dist"
  dist_bak = shell_dir / "dist.bak"
  had_dist = dist_dir.exists()
  if had_dist:
    _rm_tree(dist_bak)
    os.rename(dist_dir, dist_bak)

  def _restore_backup() -> None:
    """Restores the previous dist from dist.bak on failure paths."""
    _rm_tree(dist_dir)
    if dist_bak.exists():
      os.rename(dist_bak, dist_dir)

  try:
    result = subprocess.run(
      ["npm", "run", "build"],
      cwd=str(shell_dir),
      capture_output=True,
      text=True,
      timeout=120,
    )
  except subprocess.TimeoutExpired:
    _restore_backup()
    return (
      "Build timed out after 120s — previous shell restored."
      " Try again, or use Factory reset if it keeps failing."
    )
  except OSError as exc:
    _restore_backup()
    return f"Build could not start: {exc}"

  if result.returncode != 0:
    _restore_backup()
    return f"Build failed (previous shell restored): {result.stderr[-600:]}"

  # Success — drop the backup.
  _rm_tree(dist_bak)
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


def _action_restore_backend(data_dir: Path, db: Session) -> str:
  """Restores /app/app/ from /app/app-baked/ via recovery_restore.sh
  then SIGTERMs the parent process so the container supervisor
  restarts uvicorn with the baked code."""
  script = Path("/app/scripts/recovery_restore.sh")
  if not script.is_file():
    return "recovery_restore.sh not found (broken image?)"
  result = subprocess.run(
    [str(script), "backend"],
    capture_output=True, text=True, timeout=60,
  )
  if result.returncode != 0:
    return f"restore_backend failed: {result.stderr[:200]}"
  # Schedule a SIGTERM so uvicorn restarts with the baked code. The
  # HTTP response goes out first; the worker exits after that.
  os.kill(os.getppid(), signal.SIGTERM)
  return "Backend restored from /app/app-baked/. Server is restarting..."


def _action_restore_scripts(data_dir: Path, db: Session) -> str:
  """Restores /app/scripts/ from /app/scripts-baked/. No restart
  needed -- scripts are loaded at invocation time, not at boot."""
  script = Path("/app/scripts/recovery_restore.sh")
  if not script.is_file():
    return "recovery_restore.sh not found (broken image?)"
  result = subprocess.run(
    [str(script), "scripts"],
    capture_output=True, text=True, timeout=60,
  )
  if result.returncode != 0:
    return f"restore_scripts failed: {result.stderr[:200]}"
  return "Scripts restored from /app/scripts-baked/."


# Maps action names to handler functions.  download_backup is handled
# separately because it returns a StreamingResponse, not a plain message.
_ACTION_HANDLERS = {
  "reset_apps": _action_reset_apps,
  "reset_chat": _action_reset_chat,
  "reset_settings": _action_reset_settings,
  "restore_shell": _action_restore_shell,
  "restore_backend": _action_restore_backend,
  "restore_scripts": _action_restore_scripts,
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
    # clear_storage=True injects a small inline script that wipes the
    # React app's localStorage (token, setup-step) and the TanStack
    # Query IndexedDB cache. Without this, the next / load picks up
    # the stale token and renders cached chats from the prior owner.
    resp = HTMLResponse(_themed(login_html(
      error="Factory reset complete.  Set up your account again.",
      clear_storage=True,
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


