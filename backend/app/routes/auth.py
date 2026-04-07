"""Authentication routes: first-boot setup and login."""

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from base64 import urlsafe_b64encode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import auth, models, schemas
from app.config import get_settings
from app.database import get_db
from app.deps import get_current_owner

router = APIRouter(prefix="/api/auth", tags=["auth"])
_limiter = Limiter(key_func=get_remote_address)
log = logging.getLogger("moebius.auth")

# Global login attempt tracking — single-owner app, so we track
# consecutive failures across all IPs.
_login_failures = 0
_login_cooldown_until = 0.0


def _check_login_cooldown():
  """Raises 429 if in a cooldown period from too many failed logins."""
  if time.time() < _login_cooldown_until:
    remaining = int(_login_cooldown_until - time.time())
    raise HTTPException(
      status_code=429,
      detail=f"Too many failed attempts. Try again in {remaining}s.",
    )


def _record_login_failure():
  """Increments failure count and sets cooldown if threshold reached."""
  global _login_failures, _login_cooldown_until
  _login_failures += 1
  if _login_failures >= 30:
    _login_cooldown_until = time.time() + 900  # 15 min
  elif _login_failures >= 20:
    _login_cooldown_until = time.time() + 300  # 5 min
  elif _login_failures >= 10:
    _login_cooldown_until = time.time() + 60   # 1 min


def _reset_login_failures():
  """Resets the failure counter on successful login."""
  global _login_failures, _login_cooldown_until
  _login_failures = 0
  _login_cooldown_until = 0.0


@router.get("/setup/status", response_model=schemas.SetupStatus)
def setup_status(db: Session = Depends(get_db)):
  """Returns whether the owner account has been configured."""
  configured = db.query(models.Owner).first() is not None
  return schemas.SetupStatus(configured=configured)


@router.post("/setup", response_model=schemas.TokenResponse)
@_limiter.limit("3/minute")
def setup(
  request: Request,
  body: schemas.SetupRequest, db: Session = Depends(get_db)
):
  """Creates the owner account on first boot and returns a JWT."""
  if db.query(models.Owner).first():
    raise HTTPException(
      status_code=400, detail="Already configured."
    )
  owner = models.Owner(
    username=body.username,
    hashed_password=auth.hash_password(body.password),
  )
  db.add(owner)
  db.commit()
  token = auth.create_access_token({"sub": body.username})
  return schemas.TokenResponse(access_token=token)


@router.post("/token", response_model=schemas.TokenResponse)
@_limiter.limit("5/minute")
def login(
  request: Request,
  form: OAuth2PasswordRequestForm = Depends(),
  db: Session = Depends(get_db),
):
  """Authenticates the owner and returns a JWT access token."""
  _check_login_cooldown()
  owner = (
    db.query(models.Owner)
    .filter(models.Owner.username == form.username)
    .first()
  )
  if not owner or not auth.verify_password(
    form.password, owner.hashed_password
  ):
    _record_login_failure()
    raise HTTPException(
      status_code=401,
      detail="Incorrect username or password.",
      headers={"WWW-Authenticate": "Bearer"},
    )
  _reset_login_failures()
  token = auth.create_access_token({"sub": owner.username})
  return schemas.TokenResponse(access_token=token)


@router.post("/app-token")
def create_app_token_endpoint(
  body: schemas.AppTokenRequest,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Returns a short-lived JWT scoped to a specific mini-app."""
  app = db.query(models.App).filter(models.App.id == body.app_id).first()
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")
  token = auth.create_app_token(body.app_id, owner.username)
  return {"token": token}


# -- Provider discovery ---------------------------------------------------

@router.get("/providers")
def list_providers():
  """Returns which AI providers are available (CLI installed)."""
  from app.providers import PROVIDERS, detect_available
  available = detect_available()
  return [
    {"id": pid, "name": p.name, "available": pid in available}
    for pid, p in PROVIDERS.items()
  ]


# -- Provider auth (self-managed PKCE OAuth) -------------------------------
#
# Generates PKCE params server-side, returns an OAuth URL to the frontend.
# After the user authorizes and pastes the code, the server exchanges it
# for tokens via httpx (no CLI subprocess).  This avoids the CLI's broken
# headless stdin handling that caused auth to hang on every provider.
#
# Credentials are written in the CLI's expected format so `claude` can
# use them for chat sessions and auto-refresh them.

# Claude CLI OAuth constants
_CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
_AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
_SCOPES = (
  "org:create_api_key user:profile user:inference "
  "user:sessions:claude_code user:mcp_servers user:file_upload"
)
_PKCE_TIMEOUT = 300  # 5 minutes

# In-flight PKCE state — only one auth flow at a time (single-owner app).
_active_pkce: dict | None = None


def _cli_env() -> tuple[dict, str]:
  """Returns (env dict, cli_home path) for CLI subprocess calls."""
  settings = get_settings()
  cli_home = os.path.join(settings.data_dir, "cli-auth", "claude")
  os.makedirs(cli_home, exist_ok=True)
  env = dict(os.environ)
  env["CLAUDE_CONFIG_DIR"] = cli_home
  return env, cli_home


def _generate_pkce() -> tuple[str, str]:
  """Returns (code_verifier, code_challenge) for PKCE S256."""
  verifier = secrets.token_urlsafe(43)
  digest = hashlib.sha256(verifier.encode()).digest()
  challenge = urlsafe_b64encode(digest).rstrip(b"=").decode()
  return verifier, challenge


def _write_credentials(token_data: dict) -> None:
  """Transforms the token endpoint response into CLI credential format."""
  for field in ("access_token", "refresh_token", "expires_in"):
    if field not in token_data:
      raise ValueError(f"Token response missing '{field}'")

  _, cli_home = _cli_env()

  # Extract email from the account object if present.
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
  path = os.path.join(cli_home, ".credentials.json")
  fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(fd, "w") as f:
    json.dump(creds, f)
  log.info("Credentials written for %s", email or "(unknown)")


@router.post("/provider/login")
@_limiter.limit("3/minute")
async def provider_login(
  request: Request,
  _: models.Owner = Depends(get_current_owner),
):
  """Generates PKCE params and returns the OAuth URL."""
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


@router.post("/provider/code")
@_limiter.limit("5/minute")
async def provider_code(
  request: Request,
  body: schemas.ProviderCodeRequest,
  _: models.Owner = Depends(get_current_owner),
):
  """Exchanges the authorization code for tokens via the token endpoint."""
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

  pkce = _active_pkce
  _active_pkce = None

  # The code may include a #fragment with the state echo — strip it.
  raw = body.code.strip()
  code = raw.split("#")[0]

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
      log.error("Token exchange failed (%d): %s", r.status_code, r.text[:500])
      raise HTTPException(
        status_code=502,
        detail="Token exchange failed. Try starting the auth flow again.",
      )

    _write_credentials(r.json())
    return {"ok": True}
  except httpx.TimeoutException:
    raise HTTPException(
      status_code=504, detail="Token exchange timed out.",
    )
  except HTTPException:
    raise
  except Exception as exc:
    log.error("Token exchange error: %s", exc)
    raise HTTPException(status_code=500, detail=str(exc))


@router.get("/provider/status")
async def provider_status(
  _: models.Owner = Depends(get_current_owner),
):
  """Checks whether the CLI is authenticated.

  Reads the credential file directly rather than spawning `claude auth
  status`, which is faster and avoids subprocess overhead.  Falls back
  to the CLI subprocess if the file can't be read.
  """
  _, cli_home = _cli_env()
  creds_path = os.path.join(cli_home, ".credentials.json")

  # Fast path: read the credential file directly.
  try:
    with open(creds_path) as f:
      data = json.load(f)
    oauth = data.get("claudeAiOauth", {})
    if oauth.get("accessToken"):
      return {
        "authenticated": True,
        "provider": "claude",
        "email": oauth.get("email", ""),
        "subscription": oauth.get("subscriptionType", ""),
      }
  except (FileNotFoundError, json.JSONDecodeError, KeyError):
    pass

  # Slow path: ask the CLI (handles cases where credential format
  # differs from what we expect, e.g. after a CLI update).
  env, _ = _cli_env()
  try:
    proc = await asyncio.create_subprocess_exec(
      "claude", "auth", "status",
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
      env=env,
    )
    stdout, _ = await asyncio.wait_for(
      proc.communicate(), timeout=10.0,
    )
    data = json.loads(stdout.decode("utf-8", errors="replace"))
    return {
      "authenticated": data.get("loggedIn", False),
      "provider": "claude",
      "email": data.get("email", ""),
      "subscription": data.get("subscriptionType", ""),
    }
  except Exception as exc:
    log.warning("Provider status check failed: %s", exc)
    return {"authenticated": False, "provider": "claude"}
