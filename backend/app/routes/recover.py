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
import sqlite3
import tempfile
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
  """Serves the recovery login form or the recovery dashboard.

  The dashboard requires BOTH a valid HMAC cookie AND a live owner
  row. POST endpoints (/recover/action, /recover/auth/logout, etc.)
  re-check owner-existence via _verify_session; the GET should
  match so a factory-reset user with a stale cookie sees the login
  page (consistent), not the dashboard (which would then 401 on
  the next click). Codex reviewer caught this asymmetry.
  """
  token = request.cookies.get(_COOKIE)
  username = recover_auth.decode_session_token(token) if token else None
  if username and _owner_exists(username):
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
  # Also kill any in-flight codex device-auth subprocess. Without
  # this, a user who started "Connect Codex" but never finished
  # could have device-auth complete AFTER the reset, recreating
  # /data/cli-auth/codex/auth.json that we're about to wipe. Codex
  # reviewer caught this gap.
  try:
    from app import recover_oauth
    recover_oauth.terminate_active_codex_login()
  except Exception:
    pass
  _db_delete_all_apps()
  _db_delete_all_owners()
  for subdir in ["compiled", "apps", "shared", "logs", "cli-auth"]:
    _rm_tree(data_dir / subdir)
    (data_dir / subdir).mkdir(parents=True, exist_ok=True)


@router.post("/recover/action")
@_limiter.limit("10/minute")
def recover_action(
  request: Request,
  action: str = Form(...),
):
  """Executes a recovery action.

  Only two actions remain: `download_backup` streams a zip, and
  `factory_reset` wipes state and bounces to the setup wizard.
  Everything else the dashboard used to expose (reset/restore knobs)
  is now the recovery chat agent's job — see /recover/chat.
  """
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

  return HTMLResponse(dashboard_html(msg="Unknown action."))


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


