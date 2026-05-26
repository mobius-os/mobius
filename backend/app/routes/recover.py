"""Recovery page: static HTML, password-authenticated, independent of
the React frontend AND of the agent's import chain.

Self-contained on purpose: uses raw sqlite3 (stdlib) for owner-row
queries, reads DATA_DIR from the environment, and serves un-themed
HTML. We do NOT import app.database / app.models / app.config /
app.theme — those are on the agent's write surface, and the
recovery page exists to be reachable when they're broken.
"""

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

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from slowapi import Limiter
from slowapi.util import get_remote_address

from app import recover_auth
from app.routes.recover_html import dashboard_html, login_html

router = APIRouter(tags=["recover"])
_limiter = Limiter(key_func=get_remote_address)

# Recovery's view of the world — read straight from env vars so we
# don't depend on app.config. Same vars the rest of the app uses,
# so prod/test/overrides stay in sync without an import.
_DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
_DB_URL = os.environ.get("DATABASE_URL", "sqlite:////data/db/ultimate.db")
RECOVERY_DB_PATH = (
  _DB_URL.removeprefix("sqlite:///") if _DB_URL.startswith("sqlite:") else _DB_URL
)


def _owner_password_hash(username: str) -> str | None:
  """Returns the owner's hashed_password if `username` exists, else None.

  Used by /recover/auth (login) to verify the password without
  importing the SQLAlchemy ORM. Raw sqlite3 keeps recovery alive
  even when app.database / app.models is broken.
  """
  if not username:
    return None
  try:
    with sqlite3.connect(RECOVERY_DB_PATH) as con:
      row = con.execute(
        "SELECT hashed_password FROM owner WHERE username = ? LIMIT 1",
        (username,),
      ).fetchone()
      return row[0] if row else None
  except sqlite3.Error:
    return None


def _owner_exists(username: str) -> bool:
  """Returns True iff an Owner row with `username` exists."""
  return _owner_password_hash(username) is not None


def _db_delete_all_apps() -> None:
  """Deletes every row in the `apps` table. Raw sqlite3 — no ORM."""
  try:
    with sqlite3.connect(RECOVERY_DB_PATH) as con:
      con.execute("DELETE FROM apps")
      con.commit()
  except sqlite3.Error:
    pass  # best-effort; factory_reset also wipes /data/apps/ on disk


def _db_delete_all_owners() -> None:
  """Deletes every row in the `owner` table. Raw sqlite3 — no ORM."""
  try:
    with sqlite3.connect(RECOVERY_DB_PATH) as con:
      con.execute("DELETE FROM owner")
      con.commit()
  except sqlite3.Error:
    pass

# Session tokens come from `recover_auth` (HMAC-signed, no JWT
# library) so recovery works even if `app/auth.py` is broken or
# corrupted by the agent. The cookie name + TTL match what the
# previous JWT-based implementation used. NOTE: the cookie FORMAT
# changed (was JWT, now HMAC) so existing /recover sessions from
# pre-upgrade do NOT carry over — users get bounced to the login
# form on their next /recover visit after deploy. One-time minor
# friction; documented in the deploy runbook.
_COOKIE = recover_auth.COOKIE_NAME

# Serializes the restore-shell action. FastAPI runs sync route handlers
# in a threadpool, so two concurrent /recover/action POSTs can run
# _action_restore_shell in parallel — and the dist-swap (rename to
# dist.bak, run build, rename back on failure) is not safe under
# concurrency: thread A's rename clobbers thread B's backup, both end
# up with no dist on a failure path. Lock at the action boundary so
# only one shell restore runs at a time.
_RESTORE_SHELL_LOCK = threading.Lock()


def _verify_session(request: Request) -> str:
  """Returns the owner username if the recovery session cookie is valid.

  Validates the HMAC + expiry AND re-confirms the owner row still
  exists (factory reset deletes the row; the stale cookie's HMAC
  is still valid but the session has to be invalidated). Raises
  401 otherwise.
  """
  token = request.cookies.get(_COOKIE)
  username = recover_auth.decode_session_token(token)
  if not username or not _owner_exists(username):
    raise HTTPException(status_code=401)
  return username


# -- Pages ------------------------------------------------------------

@router.get("/recover", response_class=HTMLResponse)
def recover_page(request: Request):
  """Serves the recovery login form or the recovery dashboard."""
  token = request.cookies.get(_COOKIE)
  if token and recover_auth.decode_session_token(token):
    return HTMLResponse(dashboard_html())
  return HTMLResponse(login_html())


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
):
  """Authenticates and sets a recovery session cookie."""
  pw_hash = _owner_password_hash(username)
  if not pw_hash or not recover_auth.verify_password(password, pw_hash):
    return HTMLResponse(login_html(error="Incorrect username or password."))
  token = recover_auth.create_session_token(username)
  resp = HTMLResponse(dashboard_html())
  resp.set_cookie(
    _COOKIE, token,
    httponly=True, samesite="strict", max_age=3600,
    secure=_request_is_https(request),
  )
  return resp


@router.post("/recover/logout")
def recover_logout(request: Request):
  """Clears the recovery session cookie and returns the login page."""
  resp = HTMLResponse(login_html())
  resp.delete_cookie(_COOKIE)
  return resp


def _action_reset_apps(data_dir: Path) -> str:
  """Deletes all apps from the database and clears compiled output."""
  _db_delete_all_apps()
  _rm_tree(data_dir / "compiled")
  (data_dir / "compiled").mkdir(parents=True, exist_ok=True)
  return "All apps have been reset."


def _action_reset_chat(data_dir: Path) -> str:
  """Clears the debug log file at /data/logs/chat.log.

  This does not affect chat history — conversations are stored server-side
  in SQLite and are unaffected by this action.
  """
  log_file = data_dir / "logs" / "chat.log"
  if log_file.exists():
    log_file.write_text("", encoding="utf-8")
  return "Debug chat log cleared."


def _action_reset_settings(data_dir: Path) -> str:
  """Clears CLI auth credentials so the user can re-authenticate."""
  _rm_tree(data_dir / "cli-auth")
  (data_dir / "cli-auth").mkdir(parents=True, exist_ok=True)
  return "CLI auth cleared.  Sign in again via the setup wizard."


def _action_restore_shell(data_dir: Path) -> str:
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


def _action_factory_reset(data_dir: Path) -> None:
  """Deletes everything: apps, owner, storage, compiled files.

  Also terminates any in-flight recovery rescue agent — a running
  subprocess would otherwise retain elevated write access until it
  naturally exits, even though /recover endpoints will start
  rejecting its session cookie immediately. Codex review caught
  this gap.

  Returns None to signal that the caller should redirect to the
  login page rather than the dashboard.
  """
  # Kill the in-flight rescue agent BEFORE deleting state so the
  # subprocess can't write to files we're about to wipe (race-
  # tolerant, not race-free — best-effort, the proc.kill() is fast).
  try:
    from app import recover_chat_runner
    recover_chat_runner.terminate_active_run()
  except Exception:
    # Don't let the kill fail block the reset itself.
    pass
  _db_delete_all_apps()
  _db_delete_all_owners()
  for subdir in ["compiled", "apps", "shared", "logs", "cli-auth"]:
    _rm_tree(data_dir / subdir)
    (data_dir / subdir).mkdir(parents=True, exist_ok=True)


_RECOVER_PENDING_FILE = Path("/data/.recover-pending")

# Modes the entrypoint's recovery_restore.sh knows how to handle.
# Keep in sync with the case-statement in scripts/recovery_restore.sh
# (`backend`, `scripts`, `shell-dist`, `shell-src`). A typo'd mode
# would silently boot-loop into restore_status="unknown-mode", so
# the validation here is the guardrail that turns a silent
# misconfiguration into a loud caller-visible error.
_VALID_MODES = frozenset({"backend", "scripts", "shell-dist", "shell-src"})


def _defer_restore(mode: str) -> None:
  """Writes a flag file then SIGTERMs uvicorn. The container restart
  policy brings uvicorn back; entrypoint.sh reads the flag and runs
  recovery_restore.sh AS ROOT before starting uvicorn.

  Running the restore from entrypoint (not from this route handler)
  is load-bearing: the route runs as `mobius` (uvicorn dropped
  privilege), but `cp -a` from /app/<X>-baked/ to /app/<X>/ must
  preserve root ownership on protected files for the frozen-island
  invariant to hold. Mobius cannot `chown root:root`. Entrypoint
  CAN — it runs as root before the `su -s mobius` exec at the end.

  Raises ValueError on an unknown mode — entrypoint.sh would silently
  fall through to restore_status="unknown-mode" and the container
  would reboot into the same broken state. Better to reject the
  typo at the call site than to ship a boot loop.
  """
  if mode not in _VALID_MODES:
    raise ValueError(
      f"invalid restore mode {mode!r}; expected one of {sorted(_VALID_MODES)}"
    )
  _RECOVER_PENDING_FILE.write_text(mode)
  import threading
  threading.Timer(0.2, lambda: os.kill(os.getpid(), signal.SIGTERM)).start()


def _action_restore_backend(data_dir: Path) -> str:
  """Schedules restore of /app/app/ from /app/app-baked/ at next
  boot, then SIGTERMs uvicorn. Docker restart policy (unless-stopped
  on prod) recreates uvicorn; entrypoint.sh sees /data/.recover-pending
  and runs recovery_restore.sh AS ROOT before starting uvicorn."""
  _defer_restore("backend")
  return "Backend restore scheduled. Server is restarting..."


def _action_restore_scripts(data_dir: Path) -> str:
  """Schedules restore of /app/scripts/ from /app/scripts-baked/."""
  _defer_restore("scripts")
  return "Scripts restore scheduled. Server is restarting..."


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
):
  """Executes a recovery action."""
  _verify_session(request)
  data_dir = _DATA_DIR

  if action == "download_backup":
    return _create_backup(data_dir)

  if action == "factory_reset":
    _action_factory_reset(data_dir)
    # clear_storage=True injects a small inline script that wipes the
    # React app's localStorage (token, setup-step) and the TanStack
    # Query IndexedDB cache. Without this, the next / load picks up
    # the stale token and renders cached chats from the prior owner.
    resp = HTMLResponse(login_html(
      error="Factory reset complete.  Set up your account again.",
      clear_storage=True,
    ))
    resp.delete_cookie(_COOKIE)
    return resp

  handler = _ACTION_HANDLERS.get(action)
  if handler is None:
    return HTMLResponse(dashboard_html(msg="Unknown action."))

  msg = handler(data_dir)
  return HTMLResponse(dashboard_html(msg=msg))


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


def _create_backup(data_dir: Path) -> StreamingResponse:
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


