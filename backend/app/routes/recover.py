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

import bcrypt

from app import recover_auth
from app.routes.recover_html import dashboard_html, login_html

# Pre-computed bcrypt hash of a random string. When the submitted
# username doesn't exist we still run checkpw against this hash so
# the response time is indistinguishable from a wrong-password response
# for a real account. Without this, an attacker can binary-search
# valid usernames purely from timing (a missing-user response returns
# ~microseconds; a wrong-password response returns ~100ms of bcrypt work).
_DUMMY_HASH = bcrypt.hashpw(b"__dummy_password__", bcrypt.gensalt()).decode()

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


def _db_delete_all_user_content() -> None:
  """Deletes chats, notifications, and push subscriptions. Raw sqlite3 — no ORM.

  Chats are NOT owner-scoped (the chat-list query has no owner filter), so a
  factory reset that left them behind would show the previous owner's
  conversation history to whoever sets the instance up next. A reset is a clean
  slate, so wipe them alongside apps + owners; their on-disk attachments go with
  the `/data/chats` rmtree.
  """
  try:
    with sqlite3.connect(RECOVERY_DB_PATH) as con:
      for table in ("chats", "notifications", "push_subscriptions"):
        con.execute(f"DELETE FROM {table}")
      con.commit()
  except sqlite3.Error:
    pass  # best-effort; factory_reset also wipes /data/chats on disk

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


# The /recover POST routes are deliberately EXEMPT from reject_cross_site: they
# authenticate via the separate recovery session-cookie surface (recover_auth
# HMAC cookie), not the owner JWT, so the JWT-oriented CSRF guard doesn't apply.
@router.post("/recover/auth")
@_limiter.limit("5/minute")
def recover_login(
  request: Request,
  username: str = Form(...),
  password: str = Form(...),
):
  """Authenticates and sets a recovery session cookie."""
  pw_hash = _owner_password_hash(username)
  # Always run bcrypt regardless of whether the username exists.
  # Skipping the hash work when the user is absent leaks existence via
  # a ~100ms timing difference; checking against a dummy hash makes
  # both failure paths take the same bcrypt time.
  candidate_hash = pw_hash if pw_hash else _DUMMY_HASH
  if not recover_auth.verify_password(password, candidate_hash) or not pw_hash:
    return HTMLResponse(login_html(error="Incorrect username or password."))
  token = recover_auth.create_session_token(username)
  resp = HTMLResponse(dashboard_html())
  resp.set_cookie(
    _COOKIE, token,
    httponly=True, samesite="strict", max_age=3600,
    secure=_request_is_https(request),
    # Scope to the recovery surface so the cookie is NOT attached to ordinary
    # same-origin requests (the shell, /api/*, mini-app fetches) — it is only
    # sent on /recover/*, shrinking the replay surface to the recovery routes
    # themselves (which the password re-auth above then gates).
    path="/recover",
  )
  return resp


@router.post("/recover/logout")
def recover_logout(request: Request):
  """Clears the recovery session cookie and returns the login page."""
  resp = HTMLResponse(login_html())
  resp.delete_cookie(_COOKIE, path="/recover")
  # Also clear any pre-fix cookie issued with the default Path=/ (a browser that
  # logged in before this deploy still holds one); without this, logout would
  # leave that legacy cookie valid until its 1h expiry.
  resp.delete_cookie(_COOKIE, path="/")
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
  _db_delete_all_user_content()
  # agent-browser-profiles holds per-chat Chromium profiles (IndexedDB,
  # localStorage, cookies) written during agent-browser runs — prior-owner
  # session data that must not survive a reset into the next owner's instance.
  for subdir in [
    "compiled", "apps", "shared", "logs", "cli-auth", "chats",
    "agent-browser-profiles",
  ]:
    _rm_tree(data_dir / subdir)
    (data_dir / subdir).mkdir(parents=True, exist_ok=True)
  # Delete identity secrets so the next boot regenerates them cleanly.
  # A factory reset must produce a completely clean slate: different
  # secrets mean a new identity so prior sessions/tokens from the old
  # owner cannot be reused on the fresh instance. Next boot's entrypoint
  # auto-generates new values for .secret-key and service-token.txt;
  # recover_auth._recovery_secret_bytes() generates .recovery-secret on
  # first use.
  for name in [".secret-key", ".recovery-secret", "service-token.txt"]:
    p = data_dir / name
    try:
      p.unlink(missing_ok=True)
    except OSError:
      pass
  # Recovery chat history is the prior owner's private data — a new owner
  # must not see it. Wipe the whole directory; it's recreated on demand.
  _rm_tree(data_dir / "recovery")


@router.post("/recover/action")
@_limiter.limit("10/minute")
def recover_action(
  request: Request,
  action: str = Form(...),
  password: str = Form(""),
):
  """Executes a recovery action.

  Three actions are live: `download_backup` streams a zip,
  `factory_reset` wipes state and bounces to the setup wizard, and
  `reinstall_store` re-runs the first-boot store bootstrap (idempotent
  — a no-op when the store is already installed). Each has a button in
  the dashboard (see `recover_html.py`). Everything else the dashboard
  used to expose (the deeper reset/restore knobs) is now the recovery
  chat agent's job — see /recover/chat.
  """
  username = _verify_session(request)
  data_dir = _DATA_DIR

  # The recovery cookie ALONE must not authorize the secret-bearing or
  # destructive actions. It is HttpOnly + SameSite=Strict + path-scoped to
  # /recover, but a same-origin mini-app (its iframe runs with
  # sandbox="... allow-same-origin") can still attach the cookie to a
  # credentialed fetch and replay it within the 1-hour window — silently
  # exfiltrating the secrets backup (download_backup) or wiping the instance
  # (factory_reset). Gate those two on a fresh owner-password re-entry, which a
  # replaying app cannot supply (it never sees the password). reinstall_store is
  # idempotent and non-secret, so it stays cookie-only.
  if action in ("download_backup", "factory_reset"):
    pw_hash = _owner_password_hash(username)
    # Run bcrypt either way (dummy hash when the row is somehow gone) so the
    # response time doesn't distinguish a wrong password from a missing owner.
    candidate_hash = pw_hash if pw_hash else _DUMMY_HASH
    # Reject an EMPTY submission outright: a replayed cookie-only POST defaults
    # `password` to "", and verify_password("", hash_of_"") is True — so without
    # this an owner who configured an empty password could still be exploited.
    if (
      not password
      or not pw_hash
      or not recover_auth.verify_password(password, candidate_hash)
    ):
      return HTMLResponse(
        dashboard_html(msg="Incorrect password — the action was not performed."),
        status_code=401,
      )

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
    resp.delete_cookie(_COOKIE, path="/recover")
    resp.delete_cookie(_COOKIE, path="/")  # also clear any legacy Path=/ cookie
    return resp

  if action == "reinstall_store":
    msg = _action_reinstall_store()
    return HTMLResponse(dashboard_html(msg=msg))

  return HTMLResponse(dashboard_html(msg="Unknown action."))


def _action_reinstall_store() -> str:
  """Re-runs the first-boot store bootstrap and returns a status msg.

  Idempotent by design — `ensure_store_installed` is keyed on
  `manifest_url`, so calling it when the store is already installed
  is a no-op (logs + returns), and calling it after the user
  uninstalled the store reinstalls it without needing a container
  restart.

  The bootstrap function swallows its own failures (a GitHub blip
  must not crash lifespan), so we look at the DB state after the
  call to tell the user what actually happened: a row appeared (new
  install), a row was already there (no-op), or neither (install
  attempt failed — point them at logs). Imports are lazy: recovery
  endpoints intentionally avoid `app.bootstrap` / `app.database` at
  module import so the recovery surface keeps loading even when the
  install / models / config chain is broken. The reinstall action
  is the deliberate exception — it CAN'T work if those modules are
  broken, but reaching it requires the surrounding recovery page to
  render first, so the lazy import preserves the contract.
  """
  try:
    from app.bootstrap import (
      BOOTSTRAP_STORE_MANIFEST_URL,
      ensure_store_installed,
    )
    from app.database import SessionLocal
    from app import models
  except Exception as exc:  # noqa: BLE001
    return f"App store install failed during import: {exc}"

  import asyncio

  db = SessionLocal()
  try:
    # Installs store the canonical key (`<base>#manifest-id=<id>`, /mobius.json
    # stripped), not the bare URL — match that prefix.
    from app.install import _canonical_base
    _store_like = (
      _canonical_base(BOOTSTRAP_STORE_MANIFEST_URL) + "#manifest-id=%"
    )
    pre_existing = (
      db.query(models.App)
      .filter(models.App.manifest_url.like(_store_like))
      .first()
    )
    try:
      asyncio.run(ensure_store_installed(db))
    except Exception as exc:  # noqa: BLE001
      # ensure_store_installed already catches its own exceptions and
      # logs them. Belt-and-braces in case a future refactor changes
      # that contract — the recovery dashboard must not 500.
      return f"App store install failed: {exc}"
    # Refresh the session so we see any row install_from_manifest
    # committed via its own session.
    db.expire_all()
    post = (
      db.query(models.App)
      .filter(models.App.manifest_url.like(_store_like))
      .first()
    )
    if post is None:
      return (
        "App store install attempted but no row was created. "
        "Check /data/logs for the bootstrap error."
      )
    if pre_existing is not None:
      return "App store was already installed — no action taken."
    return "App store installed."
  finally:
    db.close()


# -- Backup ------------------------------------------------------------

def _backup_db(src_path: Path, dest_path: Path) -> None:
  """Copies the SQLite database using the online backup API for consistency.

  A raw file copy of an open WAL-mode database can produce an inconsistent
  snapshot.  The backup API performs a live, consistent copy regardless of
  concurrent writes.

  We run PRAGMA wal_checkpoint(FULL) on the source before backing up so
  that all committed WAL frames are flushed into the main database file.
  Without this, the backup zip contains a consistent copy of whatever
  frames already made it into the .db file, but any frames still sitting
  only in the .wal sidecar are silently absent from the backup — the
  recipient of the zip sees those commits as lost. The checkpoint is
  best-effort: a FULL checkpoint may not complete if readers are active,
  but it always moves as many frames as possible; the backup API then
  captures a consistent view of the resulting state.
  """
  # Checkpoint WAL into the main file before copying so the backup
  # reflects the most recent committed state.
  src_chk = sqlite3.connect(str(src_path))
  try:
    src_chk.execute("PRAGMA wal_checkpoint(FULL)")
  finally:
    src_chk.close()

  src = sqlite3.connect(str(src_path))
  dst = sqlite3.connect(str(dest_path))
  src.backup(dst)
  dst.close()
  src.close()


def _safe_add_file(zf: zipfile.ZipFile, f: Path, data_dir: Path) -> None:
  """Adds a single file to the zip archive, skipping symlinks and out-of-tree
  paths. Symlinks inside /data can point outside the data directory (e.g.
  a misconfigured app); writing them would either follow the link (leaking
  host files) or archive the dangling name (confusing a restore). Skipping
  is the safe default — the restore recipient doesn't need symlinks."""
  if f.is_symlink():
    return
  try:
    arc_name = str(f.relative_to(data_dir))
  except ValueError:
    # Path is outside data_dir — don't archive it.
    return
  try:
    zf.write(f, arc_name)
  except OSError:
    # Permission denied or file vanished between stat and read — skip
    # rather than aborting the whole backup. The user can investigate
    # the missing file after restore.
    pass


def _create_backup(data_dir: Path) -> StreamingResponse:
  """Creates a ZIP archive of all data needed for a complete restore.

  Includes database, app storage, CLI credentials, identity secrets
  (.secret-key, .recovery-secret, service-token.txt), VAPID keys
  (push/), and recovery chat history (recovery/). Restoring without
  the identity secrets would invalidate every device session, break
  scheduled-task service tokens, and permanently break Web Push —
  the previous incomplete backup left the user in a worse state than
  before. Store the backup file securely.
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

    # App storage (agents' built apps, shared knowledge, compiled bundles).
    for sub in ["apps", "shared", "compiled"]:
      base = data_dir / sub
      if not base.exists():
        continue
      for f in base.rglob("*"):
        if f.is_file():
          _safe_add_file(zf, f, data_dir)

    # CLI auth credentials (OAuth tokens for Claude and other providers).
    cli_auth = data_dir / "cli-auth"
    if cli_auth.exists():
      for f in cli_auth.rglob("*"):
        if f.is_file():
          _safe_add_file(zf, f, data_dir)

    # Identity secrets — critical for a working restore.
    # .secret-key signs all JWTs; .recovery-secret signs recovery cookies;
    # service-token.txt is the long-lived cron/agent service token.
    # Without these the restore target has a different identity than the
    # original: every device session becomes invalid, scheduled tasks stop
    # working, and the recovery surface gets a fresh key (one re-login).
    for name in [".secret-key", ".recovery-secret", "service-token.txt"]:
      p = data_dir / name
      if p.exists() and not p.is_symlink():
        try:
          zf.write(p, name)
        except OSError:
          pass

    # VAPID keys (Web Push). Without these, Web Push subscriptions on
    # the restore target can never be notified — the VAPID key pair is
    # baked into the browser's push subscription at subscribe() time,
    # so a different key means silent delivery failure forever.
    push_dir = data_dir / "push"
    if push_dir.exists():
      for f in push_dir.rglob("*"):
        if f.is_file():
          _safe_add_file(zf, f, data_dir)

    # Recovery chat history — the user's conversation with the rescue
    # agent during a previous incident. Useful context for diagnosis
    # after a restore.
    recovery_dir = data_dir / "recovery"
    if recovery_dir.exists():
      for f in recovery_dir.rglob("*"):
        if f.is_file():
          _safe_add_file(zf, f, data_dir)

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


