"""Frozen OAuth flow for the recovery surface.

Lets the user CONNECT a provider (Claude or Codex) directly from
the recovery page, without going through the main app's Settings.
Useful when:
- The agent broke `routes/auth.py` so the normal Settings reconnect
  doesn't work.
- The user only has one provider configured and wants to add the
  other for recovery (e.g. has Codex, wants to spin up Claude).
- Existing credentials expired and the main app's auth flow is
  unreachable for any reason.

Deliberately self-contained — uses only stdlib + httpx (already a
dependency) + the recovery cookie auth. Does NOT import from
`routes/auth.py` so a broken agent edit there can't take this
flow down too. The OAuth constants (Anthropic client id, URLs)
are duplicated; they're public values and change rarely, drift
risk is low.

Auth: every endpoint requires the recovery cookie. Without it the
caller gets 401. That cookie is itself acquired by /recover login
(owner username + password), so an attacker would need to know the
owner password to reach this surface.

FROZEN: this file is in protected-files.txt. To change OAuth client
ids or URLs, edit on the host repo and rebuild.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import time
from base64 import urlsafe_b64encode
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import APIRouter, Body, Cookie, HTTPException, Request
from slowapi import Limiter
from slowapi.util import get_remote_address

from app import recover_auth

log = logging.getLogger("moebius.recover_oauth")

router = APIRouter(tags=["recover"])
_limiter = Limiter(key_func=get_remote_address)


# --- Claude OAuth constants -----------------------------------------
# Duplicated from routes/auth.py on purpose. These are public Anthropic
# constants; the duplication ensures the recovery flow keeps working
# even if the agent rewrites routes/auth.py. If Anthropic ever rotates
# the client id, update both places.
_CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
_SCOPES = (
  "org:create_api_key user:profile user:inference "
  "user:sessions:claude_code user:mcp_servers user:file_upload"
)
_PKCE_TIMEOUT = 300  # 5 minutes


# --- Credential paths -----------------------------------------------
_CLAUDE_CONFIG_PATH = Path(
  os.environ.get("DATA_DIR", "/data")
) / "cli-auth" / "claude"
_CODEX_CONFIG_PATH = Path(
  os.environ.get("DATA_DIR", "/data")
) / "cli-auth" / "codex"


# In-flight PKCE state for the Claude flow. Single-owner app, so one
# flow at a time is fine. A second call to /start replaces the prior
# state; a stale /complete after the timeout fails cleanly.
_active_pkce: dict | None = None

# Codex login subprocess + result tracking. Mirrors the structure
# routes/auth.py uses for the main-app Codex login.
_codex_login_procs: dict = {}
_codex_login_status: dict = {}  # "complete" | "failed"


# Recovery DB path resolution — same env-var the rest of the recovery
# surface reads, inlined so we don't import from app.config or
# recover_chat.py. Used by `_owner_exists` below.
_DB_URL = os.environ.get("DATABASE_URL", "sqlite:////data/db/ultimate.db")
_RECOVERY_DB_PATH = (
  _DB_URL.removeprefix("sqlite:///") if _DB_URL.startswith("sqlite:")
  else _DB_URL
)


def _owner_exists(username: str) -> bool:
  """Returns True iff an Owner row with `username` exists in the DB.

  Duplicated from recover_chat.py on purpose — recover_oauth.py is
  its own frozen island and shouldn't import from another frozen
  file just to share this 10-line helper. Uses raw sqlite3 so a
  broken app.database / app.models doesn't take this surface down.
  """
  if not username:
    return False
  try:
    with sqlite3.connect(_RECOVERY_DB_PATH) as con:
      row = con.execute(
        "SELECT 1 FROM owner WHERE username = ? LIMIT 1",
        (username,),
      ).fetchone()
      return row is not None
  except sqlite3.Error:
    return False


def _require_recovery_session(token: Optional[str]) -> None:
  """Raises 401 unless the recovery cookie is valid AND the owner
  still exists.

  Recovery OAuth is gated on the recovery cookie, NOT the main-app
  owner JWT. The cookie is issued by /recover login (owner password)
  so reaching this surface still requires the owner password.

  Owner-existence is re-checked on every call to defeat the
  factory-reset-stale-cookie attack: factory reset deletes the
  owner row, but the cookie's HMAC stays valid for up to 1h. Without
  the re-check, a second tab / stolen cookie could keep writing to
  /data/cli-auth/ post-reset. Codex review caught this.
  """
  username = recover_auth.decode_session_token(token)
  if not username or not _owner_exists(username):
    raise HTTPException(status_code=401)


# --- Claude PKCE helpers --------------------------------------------

def _generate_pkce() -> tuple[str, str]:
  """Returns (code_verifier, code_challenge) for PKCE S256."""
  verifier = secrets.token_urlsafe(43)
  digest = hashlib.sha256(verifier.encode()).digest()
  challenge = urlsafe_b64encode(digest).rstrip(b"=").decode()
  return verifier, challenge


def _extract_provider_code_and_state(raw_code: str) -> tuple[str, str | None]:
  """Extract the provider code and echoed state from pasted input."""
  raw = raw_code.strip()
  parsed = urlparse(raw)
  values: dict = {}
  if parsed.scheme and (parsed.query or parsed.fragment):
    query = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment)
    values = {**query, **fragment}
  elif "=" in raw and ("&" in raw or raw.startswith("code=")):
    values = parse_qs(raw)

  if values:
    code = (values.get("code") or [raw])[0]
    state = (values.get("state") or [None])[0]
    return code, state

  code, _, fragment = raw.partition("#")
  state = None
  if fragment:
    fragment_values = parse_qs(fragment)
    state = (fragment_values.get("state") or [None])[0]
  return code or raw, state


def _write_claude_credentials(token_data: dict) -> None:
  """Transforms the token endpoint response into CLI credential format."""
  for field in ("access_token", "refresh_token", "expires_in"):
    if field not in token_data:
      raise ValueError(f"Token response missing '{field}'")

  _CLAUDE_CONFIG_PATH.mkdir(parents=True, exist_ok=True)

  account = token_data.get("account") or {}
  email = account.get("email_address", "")

  creds = {
    "claudeAiOauth": {
      "accessToken": token_data["access_token"],
      "refreshToken": token_data["refresh_token"],
      "expiresAt": int(time.time() * 1000) + token_data["expires_in"] * 1000,
      "scopes": token_data.get("scope", "").split(),
      "email": email,
    }
  }
  path = _CLAUDE_CONFIG_PATH / ".credentials.json"
  fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(fd, "w") as f:
    json.dump(creds, f)
  log.info("Recovery: Claude credentials written for %s", email or "(unknown)")


# --- Claude endpoints -----------------------------------------------

@router.post("/recover/provider/claude/start")
@_limiter.limit("5/minute")
def claude_start(
  request: Request,
  moebius_recover: str | None = Cookie(default=None),
):
  """Generates PKCE params and returns the Claude OAuth URL."""
  _require_recovery_session(moebius_recover)
  global _active_pkce
  verifier, challenge = _generate_pkce()
  state = secrets.token_urlsafe(32)
  _active_pkce = {"verifier": verifier, "state": state, "ts": time.time()}

  auth_url = (
    f"{_AUTHORIZE_URL}?code=true"
    f"&client_id={_CLAUDE_CLIENT_ID}"
    f"&response_type=code"
    f"&redirect_uri={_REDIRECT_URI.replace(':', '%3A').replace('/', '%2F')}"
    f"&scope={_SCOPES.replace(':', '%3A').replace(' ', '+')}"
    f"&code_challenge={challenge}"
    f"&code_challenge_method=S256"
    f"&state={state}"
  )
  return {"auth_url": auth_url}


@router.post("/recover/provider/claude/code")
@_limiter.limit("10/minute")
async def claude_code(
  request: Request,
  payload: dict = Body(...),
  moebius_recover: str | None = Cookie(default=None),
):
  """Exchanges the authorization code for tokens and writes them to disk."""
  _require_recovery_session(moebius_recover)
  global _active_pkce
  if not _active_pkce:
    raise HTTPException(
      status_code=400,
      detail="No auth flow in progress. Start one first.",
    )
  if time.time() - _active_pkce["ts"] > _PKCE_TIMEOUT:
    _active_pkce = None
    raise HTTPException(
      status_code=400,
      detail="Auth flow expired. Please start again.",
    )

  raw_code = (payload.get("code") or "").strip()
  if not raw_code:
    raise HTTPException(status_code=400, detail="code required")

  pkce = _active_pkce
  _active_pkce = None

  code, returned_state = _extract_provider_code_and_state(raw_code)
  if returned_state is not None and returned_state != pkce["state"]:
    raise HTTPException(
      status_code=403,
      detail="OAuth state mismatch. Start the auth flow again.",
    )

  try:
    async with httpx.AsyncClient(timeout=30.0) as client:
      r = await client.post(
        _TOKEN_URL,
        json={
          "grant_type": "authorization_code",
          "code": code,
          "client_id": _CLAUDE_CLIENT_ID,
          "redirect_uri": _REDIRECT_URI,
          "code_verifier": pkce["verifier"],
          "state": pkce["state"],
        },
        headers={"Content-Type": "application/json"},
      )
    if r.status_code != 200:
      log.error(
        "Recovery: Claude token exchange failed (%d): %s",
        r.status_code, r.text[:500],
      )
      raise HTTPException(
        status_code=502,
        detail="Token exchange failed. Try starting the auth flow again.",
      )

    _write_claude_credentials(r.json())
    return {"ok": True}
  except httpx.TimeoutException:
    raise HTTPException(
      status_code=504, detail="Token exchange timed out.",
    )
  except HTTPException:
    raise
  except Exception as exc:
    log.error("Recovery: Claude token exchange error: %s", exc)
    raise HTTPException(status_code=500, detail=str(exc))


# --- Codex device-auth flow -----------------------------------------

# Absolute cap on how long a codex device-auth subprocess is allowed
# to live waiting for the user to complete authentication. The 15s
# `asyncio.timeout` in codex_start only bounds the INITIAL readline
# loop (until the device code is parsed); after that, _watch_codex_login
# does a bare `proc.wait()` with no deadline. A user who walks away
# would leave the subprocess running indefinitely. Codex review caught
# this.
_CODEX_LOGIN_MAX_LIFETIME = 600  # 10 minutes — generous for slow auth


async def _watch_codex_login(proc) -> None:
  """Awaits proc.wait() (with a lifetime cap) and records the outcome.

  If the user never completes authentication within
  _CODEX_LOGIN_MAX_LIFETIME, the subprocess is killed and the status
  reads as 'failed'. Without this cap, an abandoned login would
  outlive every other recovery action and could in principle complete
  (and recreate /data/cli-auth/codex/auth.json) long after the user
  has moved on — the same class of leak factory_reset has to guard
  against.
  """
  try:
    await asyncio.wait_for(proc.wait(), timeout=_CODEX_LOGIN_MAX_LIFETIME)
  except asyncio.TimeoutError:
    try:
      proc.kill()
    except (ProcessLookupError, OSError):
      pass
    try:
      await asyncio.wait_for(proc.wait(), timeout=2.0)
    except (asyncio.TimeoutError, BaseException):
      pass
  if _codex_login_procs.get("active") is proc:
    _codex_login_status["result"] = (
      "complete" if proc.returncode == 0 else "failed"
    )
    _codex_login_procs.pop("active", None)


def terminate_active_codex_login() -> bool:
  """Kills any in-flight codex device-auth subprocess.

  Called from destructive admin actions (factory reset) so a login
  that completes AFTER the reset can't recreate the credentials the
  reset just wiped. Returns True if a proc was active and got
  killed, False if there was nothing to terminate. Codex review
  caught the gap: factory_reset was killing the recovery rescue
  agent but not the device-auth login proc, so /data/cli-auth/codex/
  could be repopulated post-reset.
  """
  proc = _codex_login_procs.pop("active", None)
  if proc is None:
    return False
  if proc.returncode is None:
    try:
      proc.kill()
    except (ProcessLookupError, OSError):
      pass
  _codex_login_status["result"] = "failed"
  return True


@router.post("/recover/provider/codex/start")
@_limiter.limit("3/minute")
async def codex_start(
  request: Request,
  moebius_recover: str | None = Cookie(default=None),
):
  """Starts `codex login --device-auth` and returns URL + code."""
  _require_recovery_session(moebius_recover)
  old_proc = _codex_login_procs.pop("active", None)
  if old_proc and old_proc.returncode is None:
    old_proc.kill()
    try:
      await asyncio.wait_for(old_proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
      pass

  _CODEX_CONFIG_PATH.mkdir(parents=True, exist_ok=True)

  env = dict(os.environ)
  env["CODEX_HOME"] = str(_CODEX_CONFIG_PATH)

  # args-as-list spawn (no shell), per the Möbius safe-subprocess pattern.
  proc = await asyncio.create_subprocess_exec(
    "codex", "login", "--device-auth",
    stdin=asyncio.subprocess.DEVNULL,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.STDOUT,
    env=env,
  )
  # Register the proc BEFORE the readline loop so a concurrent
  # factory reset (or another destructive call) can find and kill
  # it during the startup window. Without this, terminate_active_
  # codex_login() would see an empty registry, kill nothing, and
  # let the login complete after the reset — recreating the
  # credentials the reset just wiped. Codex review caught this.
  _codex_login_procs["active"] = proc
  _codex_login_status.pop("result", None)

  output = ""
  try:
    async with asyncio.timeout(15):
      while True:
        line = await proc.stdout.readline()
        if not line:
          break
        output += line.decode("utf-8", errors="replace")
        if "code" in output.lower() and re.search(
          r'[A-Z0-9]{4,}-[A-Z0-9]{4,}', output
        ):
          break
  except asyncio.TimeoutError:
    # Cleanup the registry entry — no watcher to do it for us yet.
    _codex_login_procs.pop("active", None)
    proc.kill()
    try:
      await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
      pass
    log.warning("Recovery: codex login timed out, output: %s", output[:500])
    raise HTTPException(500, "Codex login timed out")

  clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', output)

  url_match = re.search(r'(https://[^\s<>"\']+)', clean)
  code_match = re.search(r'([A-Z0-9]{4,}-[A-Z0-9]{4,})', clean)
  if not url_match or not code_match:
    # Same cleanup as the timeout branch — no watcher yet.
    _codex_login_procs.pop("active", None)
    proc.kill()
    try:
      await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
      pass
    log.warning(
      "Recovery: could not parse codex device code from output: %s",
      output[:500],
    )
    raise HTTPException(500, "Could not parse device code")

  # Proc is already registered above; just start the watcher now
  # that we know parsing succeeded.
  asyncio.create_task(_watch_codex_login(proc))

  parsed_url = url_match.group(1).rstrip('.,;:!?')
  return {"url": parsed_url, "code": code_match.group(1)}


@router.get("/recover/provider/codex/status")
def codex_status(
  request: Request,
  moebius_recover: str | None = Cookie(default=None),
):
  """Polls the codex login state.

  Returns one of: in_progress, complete, failed, idle.

  Reads `_codex_login_status["result"]` non-destructively so two
  concurrent pollers (e.g. EventSource reconnect, two browser tabs,
  or a poll that races with the watcher's status set) both observe
  the terminal state. The result is cleared on the next
  /provider/codex/start, not on first read. Codex review caught the
  pop-destroys-state bug.
  """
  _require_recovery_session(moebius_recover)
  if "active" in _codex_login_procs:
    return {"state": "in_progress"}
  result = _codex_login_status.get("result")
  if result == "complete":
    return {"state": "complete"}
  if result == "failed":
    return {"state": "failed"}
  return {"state": "idle"}
