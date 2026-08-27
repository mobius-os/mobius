"""Authentication routes: first-boot setup and login."""

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from base64 import urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app import auth, models, schemas
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal,
  get_chat_view_principal,
  get_current_owner, get_current_owner_or_app,
  get_owner_app_or_chat_embed_for_models, reject_cross_site,
  require_chat_embed_operation,
)
from app.timeutil import now_naive_utc

router = APIRouter(prefix="/api/auth", tags=["auth"])
_limiter = Limiter(key_func=get_remote_address)
log = logging.getLogger("moebius.auth")

# Global login attempt tracking keyed by username. IP-level defense
# remains handled separately by slowapi rate limits.
#
# Both dicts are bounded at _LOGIN_TRACK_CAP entries: a real instance
# never sees more than a handful of distinct usernames, so a much lower
# cap would suffice, but 10k guards against a targeted enumeration
# attack stuffing the maps with random usernames and exhausting the
# process heap (a low-cost attack that would OOM the already-tight host).
# When the cap is hit, oldest entries (sorted by key insertion order,
# which CPython's dict preserves) are evicted first.
_LOGIN_TRACK_CAP = 10_000
_login_failures: dict[str, int] = {}
_login_cooldown_until: dict[str, datetime] = {}


def _ensure_login_tracking_maps() -> None:
  """Normalizes test-reset globals back to the keyed tracking maps."""
  global _login_failures, _login_cooldown_until
  if not isinstance(_login_failures, dict):
    _login_failures = {}
  if not isinstance(_login_cooldown_until, dict):
    _login_cooldown_until = {}


def _check_login_cooldown(username: str):
  """Raises 429 if in a cooldown period from too many failed logins."""
  _ensure_login_tracking_maps()
  until = _login_cooldown_until.get(username)
  if until and datetime.now(UTC) < until:
    remaining = int((until - datetime.now(UTC)).total_seconds())
    raise HTTPException(
      status_code=429,
      detail=f"Too many failed attempts. Try again in {remaining}s.",
    )
  if until:
    _login_cooldown_until.pop(username, None)


def _evict_oldest_if_over_cap(d: dict) -> None:
  """Removes the oldest entry when the dict exceeds _LOGIN_TRACK_CAP.

  CPython dicts maintain insertion order, so `next(iter(d))` is the
  oldest key. Evicting one entry per insertion keeps the dict at most
  _LOGIN_TRACK_CAP + 1 briefly, then immediately back to the cap.
  """
  if len(d) > _LOGIN_TRACK_CAP:
    d.pop(next(iter(d)), None)


def _record_login_failure(username: str):
  """Increments failure count and sets cooldown if threshold reached."""
  _ensure_login_tracking_maps()
  failures = _login_failures.get(username, 0) + 1
  _login_failures[username] = failures
  _evict_oldest_if_over_cap(_login_failures)
  if failures >= 30:
    _login_cooldown_until[username] = datetime.now(UTC) + timedelta(minutes=15)
    _evict_oldest_if_over_cap(_login_cooldown_until)
  elif failures >= 20:
    _login_cooldown_until[username] = datetime.now(UTC) + timedelta(minutes=5)
    _evict_oldest_if_over_cap(_login_cooldown_until)
  elif failures >= 10:
    _login_cooldown_until[username] = datetime.now(UTC) + timedelta(minutes=1)
    _evict_oldest_if_over_cap(_login_cooldown_until)


def _reset_login_failures(username: str):
  """Resets the failure counter on successful login."""
  _ensure_login_tracking_maps()
  _login_failures.pop(username, None)
  _login_cooldown_until.pop(username, None)


def _extract_provider_code_and_state(raw_code: str) -> tuple[str, str | None]:
  """Extract the provider code and echoed state from pasted input."""
  raw = raw_code.strip()
  parsed = urlparse(raw)
  values = {}
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
  return code, state


@router.get("/setup/status", response_model=schemas.SetupStatus)
def setup_status(db: Session = Depends(get_db)):
  """Returns whether the owner account has been configured."""
  settings = get_settings()
  configured = db.query(models.Owner).first() is not None
  return schemas.SetupStatus(
    configured=configured,
    auth_mode="mobius_sso" if settings.mobius_sso_enabled else "local",
  )


def _write_service_token(username: str, token_epoch: int) -> None:
  """Mints a 90-day service token for cron jobs and writes it to
  /data/service-token.txt (chmod 600). The entrypoint refresh path
  only runs when an owner exists at boot, so on first-time setup we
  have to seed it here — otherwise the file is missing until the
  next container restart.

  Stamped with the owner's token_epoch so "sign out everywhere"
  revokes it too — a 90-day unrevocable token would be the largest
  hole in the revocation story. The owner must re-mint it afterward
  (the entrypoint refresh path does this on the next restart)."""
  settings = get_settings()
  path = os.path.join(settings.data_dir, "service-token.txt")
  token = auth.create_access_token(
    {"sub": username},
    expires_delta=timedelta(days=90),
    token_epoch=token_epoch,
  )
  fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
  with os.fdopen(fd, "w") as f:
    f.write(token)


# Self-hosted ownership contract: without managed SSO, possession of the
# instance URL is the security boundary for the short first-setup window. A
# Railway-managed instance never reaches this path: injected SSO configuration
# closes local setup and binds the owner through the launcher's one-time code.
@router.post("/setup", response_model=schemas.TokenResponse,
             dependencies=[Depends(reject_cross_site)])
@_limiter.limit("3/minute")
def setup(
  request: Request,
  body: schemas.SetupRequest, db: Session = Depends(get_db)
):
  """Creates the owner account on first boot and returns a JWT."""
  if get_settings().mobius_sso_enabled:
    raise HTTPException(
      status_code=403,
      detail="Managed sign-in is enabled for this deployment.",
    )
  if db.query(models.Owner).first():
    raise HTTPException(status_code=400, detail="Already configured.")
  owner = models.Owner(
    username=body.username,
    hashed_password=auth.hash_password(body.password),
  )
  db.add(owner)
  try:
    db.commit()
  except IntegrityError:
    db.rollback()
    raise HTTPException(status_code=400, detail="Already configured.")
  db.refresh(owner)
  try:
    _write_service_token(owner.username, owner.token_epoch)
  except OSError as exc:
    log.warning("Could not write service token: %s", exc)
  token = auth.create_access_token(
    {"sub": owner.username}, token_epoch=owner.token_epoch
  )
  return schemas.TokenResponse(access_token=token)


def _safe_sso_return_path(raw: str | None) -> str:
  """Keep the post-login destination on this exact instance origin."""
  value = (raw or "/").strip()
  if (
    not value.startswith("/")
    or value.startswith("//")
    or "\\" in value
    or "%5c" in value.lower()
  ):
    return "/"
  parsed = urlparse(value)
  if parsed.scheme or parsed.netloc:
    return "/"
  return parsed.path + (
    ("?" + parsed.query) if parsed.query else ""
  ) + (("#" + parsed.fragment) if parsed.fragment else "")


def _sso_cookie_secure() -> bool:
  return get_settings().frontend_origin.startswith("https://")


def _sso_error_redirect():
  response = RedirectResponse(url="/shell/?mobius_sso_error=1", status_code=303)
  response.headers["Cache-Control"] = "no-store"
  response.headers["Referrer-Policy"] = "no-referrer"
  response.delete_cookie("mobius_sso_state", path="/api/auth/sso/callback")
  return response


@router.get("/sso/start")
def start_managed_sso(return_path: str = "/"):
  settings = get_settings()
  if not settings.mobius_sso_enabled:
    raise HTTPException(status_code=404, detail="Managed sign-in is not configured.")
  state = secrets.token_urlsafe(32)
  verifier, challenge = _generate_pkce()
  redirect_uri = settings.frontend_origin.rstrip("/") + "/api/auth/sso/callback"
  context = auth.create_access_token(
    {
      "scope": "mobius_sso_state",
      "state": state,
      "code_verifier": verifier,
      "redirect_uri": redirect_uri,
      "return_path": _safe_sso_return_path(return_path),
    },
    expires_delta=timedelta(minutes=10),
  )
  authorize_url = settings.mobius_sso_issuer + "/sso/authorize?" + urlencode(
    {
      "instance_id": settings.mobius_sso_instance_id,
      "state": state,
      "redirect_uri": redirect_uri,
      "code_challenge": challenge,
    }
  )
  response = RedirectResponse(url=authorize_url, status_code=303)
  response.set_cookie(
    "mobius_sso_state",
    context,
    httponly=True,
    secure=_sso_cookie_secure(),
    samesite="lax",
    max_age=10 * 60,
    path="/api/auth/sso/callback",
  )
  response.headers["Cache-Control"] = "no-store"
  response.headers["Referrer-Policy"] = "no-referrer"
  return response


@router.get("/sso/callback")
async def complete_managed_sso(
  request: Request,
  code: str = "",
  state: str = "",
  db: Session = Depends(get_db),
):
  settings = get_settings()
  if not settings.mobius_sso_enabled:
    raise HTTPException(status_code=404, detail="Managed sign-in is not configured.")
  context_token = request.cookies.get("mobius_sso_state", "")
  context = auth.decode_access_token(context_token) if context_token else None
  if (
    not context
    or context.get("scope") != "mobius_sso_state"
    or not code
    or not state
    or not secrets.compare_digest(str(context.get("state") or ""), state)
  ):
    return _sso_error_redirect()

  redirect_uri = str(context.get("redirect_uri") or "")
  code_verifier = str(context.get("code_verifier") or "")
  try:
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
      exchange = await client.post(
        settings.mobius_sso_issuer + "/sso/token",
        json={
          "instance_id": settings.mobius_sso_instance_id,
          "client_secret": settings.mobius_sso_client_secret,
          "code": code,
          "code_verifier": code_verifier,
          "redirect_uri": redirect_uri,
        },
        headers={"Accept": "application/json"},
      )
    if exchange.status_code != 200:
      return _sso_error_redirect()
    identity = exchange.json()
  except (httpx.HTTPError, ValueError):
    return _sso_error_redirect()

  subject = str(identity.get("sub") or "").strip()
  email = str(identity.get("email") or "").strip().lower()
  display_name = str(identity.get("name") or "").strip()
  if (
    not re.fullmatch(r"user_[A-Za-z0-9_-]{3,80}", subject)
    or not email
    or len(email) > 320
  ):
    return _sso_error_redirect()

  owner = db.query(models.Owner).first()
  created_owner = owner is None
  if owner is None:
    username = (display_name or email.split("@", 1)[0] or "Owner").strip()[:64]
    if not username:
      username = "Owner"
    owner = models.Owner(
      username=username,
      # Managed accounts have no default local password. A random unreachable
      # hash keeps the legacy non-null schema and password endpoint inert until
      # an explicit local-login credential is added in Settings.
      hashed_password=auth.hash_password(secrets.token_urlsafe(64)),
      sso_subject=subject,
      sso_email=email,
    )
    db.add(owner)
  elif owner.sso_subject and not secrets.compare_digest(owner.sso_subject, subject):
    return _sso_error_redirect()
  else:
    owner.sso_subject = subject
    owner.sso_email = email

  try:
    db.commit()
  except (IntegrityError, SQLAlchemyError):
    db.rollback()
    return _sso_error_redirect()
  db.refresh(owner)
  if created_owner:
    try:
      _write_service_token(owner.username, owner.token_epoch)
    except OSError as exc:
      log.warning("Could not write service token: %s", exc)

  access_token = auth.create_access_token(
    {"sub": owner.username}, token_epoch=owner.token_epoch
  )
  handoff = auth.create_access_token(
    {
      "scope": "mobius_sso_handoff",
      "access_token": access_token,
      "new_owner": created_owner,
      "return_path": _safe_sso_return_path(context.get("return_path")),
    },
    expires_delta=timedelta(seconds=60),
  )
  response = RedirectResponse(url="/shell/?mobius_sso=1", status_code=303)
  response.set_cookie(
    "mobius_sso_handoff",
    handoff,
    httponly=True,
    secure=_sso_cookie_secure(),
    samesite="lax",
    max_age=60,
    path="/api/auth/sso/session",
  )
  response.delete_cookie("mobius_sso_state", path="/api/auth/sso/callback")
  response.headers["Cache-Control"] = "no-store"
  response.headers["Referrer-Policy"] = "no-referrer"
  return response


@router.post("/sso/session", dependencies=[Depends(reject_cross_site)])
def consume_managed_sso_session(request: Request):
  handoff = request.cookies.get("mobius_sso_handoff", "")
  payload = auth.decode_access_token(handoff) if handoff else None
  if not payload or payload.get("scope") != "mobius_sso_handoff":
    raise HTTPException(status_code=401, detail="Managed sign-in expired.")
  response = JSONResponse(
    {
      "access_token": payload.get("access_token"),
      "token_type": "bearer",
      "new_owner": payload.get("new_owner") is True,
      "return_path": _safe_sso_return_path(payload.get("return_path")),
    }
  )
  response.delete_cookie("mobius_sso_handoff", path="/api/auth/sso/session")
  response.headers["Cache-Control"] = "no-store"
  return response


# One-time install passes. iOS seals every Home Screen web app inside its own
# storage container, so an app installed from a signed-in Safari session
# launches signed out. The URL carries only a random opaque reference; no JWT
# or owner bearer can be extracted from it. The durable row owns expiry,
# app-binding, revocation, and atomic one-use consumption across restarts.
_INSTALL_PASS_TTL = timedelta(minutes=30)
# The URL-carried pass is short-lived; the credential minted after it is spent
# is an ordinary owner session. Giving an installed app another 30-minute
# credential would merely defer the same per-app login until half an hour after
# installation, with no security benefit once the pass has left the URL.
_INSTALL_SESSION_TTL = timedelta(days=30)


def _install_pass_hash(secret: str) -> str:
  return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _invalid_install_pass() -> None:
  raise HTTPException(
    status_code=401, detail="This sign-in pass is invalid or has expired."
  )


@router.post(
  "/install-pass",
  response_model=schemas.InstallPassResponse,
  dependencies=[Depends(reject_cross_site)],
)
def mint_install_pass(
  body: schemas.InstallPassRequest,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Mints a one-time pass for installing one app to the home screen.

  Owner-authenticated: the caller must already hold the session the pass will
  carry, so this never widens who can obtain one.
  """
  app_row = (
    db.query(models.App)
    .filter(models.App.slug == body.slug, models.App.deleted_at.is_(None))
    .first()
  )
  if not app_row:
    raise HTTPException(status_code=404, detail="App not found.")
  now = now_naive_utc()
  # Expired references carry no value and need not accumulate forever. This
  # cleanup is inside the ordinary mint transaction, so no background job or
  # second lifecycle mechanism is needed.
  db.query(models.InstallPassGrant).filter(
    models.InstallPassGrant.expires_at <= now,
  ).delete(synchronize_session=False)

  # A collision is cryptographically implausible, but the unique constraint is
  # the authority. Retry rather than turning it into a 500 if it ever happens.
  secret = ""
  for _attempt in range(3):
    secret = secrets.token_urlsafe(32)
    db.add(models.InstallPassGrant(
      token_hash=_install_pass_hash(secret),
      app_id=app_row.id,
      owner_epoch=owner.token_epoch,
      expires_at=now + _INSTALL_PASS_TTL,
    ))
    try:
      db.commit()
      break
    except IntegrityError:
      db.rollback()
  else:
    raise HTTPException(status_code=503, detail="Could not create a sign-in pass.")

  return JSONResponse(
    {"install_pass": secret},
    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
  )


@router.post(
  "/install-pass/redeem",
  response_model=schemas.TokenResponse,
  dependencies=[Depends(reject_cross_site)],
)
def redeem_install_pass(
  body: schemas.InstallPassRedeemRequest,
  db: Session = Depends(get_db),
):
  """Atomically spends an opaque pass and mints a fresh short session."""
  now = now_naive_utc()
  grant = db.query(models.InstallPassGrant).filter(
    models.InstallPassGrant.token_hash == _install_pass_hash(body.install_pass),
  ).first()
  if grant is None or grant.consumed_at is not None or grant.expires_at <= now:
    _invalid_install_pass()

  app_row = db.query(models.App).filter(
    models.App.id == grant.app_id,
    models.App.slug == body.slug,
    models.App.deleted_at.is_(None),
  ).first()
  owner = db.query(models.Owner).first()
  if app_row is None or owner is None or owner.token_epoch != grant.owner_epoch:
    _invalid_install_pass()

  # The conditional UPDATE is the one-use boundary. Two workers may both read
  # the row above, but only one can transition consumed_at from NULL.
  consumed = db.query(models.InstallPassGrant).filter(
    models.InstallPassGrant.id == grant.id,
    models.InstallPassGrant.consumed_at.is_(None),
    models.InstallPassGrant.expires_at > now,
  ).update({models.InstallPassGrant.consumed_at: now}, synchronize_session=False)
  if consumed != 1:
    db.rollback()
    _invalid_install_pass()
  db.commit()

  access_token = auth.create_access_token(
    {"sub": owner.username},
    token_epoch=owner.token_epoch,
    expires_delta=_INSTALL_SESSION_TTL,
  )
  return JSONResponse(
    {"access_token": access_token, "token_type": "bearer"},
    headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
  )


# Bcrypt-verifying against this dummy hash when the username is unknown makes the
# missing-user path cost the same as a wrong-password path (anti-enumeration —
# the old short-circuit skipped bcrypt entirely for unknown users, leaking
# existence by timing). Computed at import with the real hasher so the cost
# factor matches stored hashes.
_DUMMY_PASSWORD_HASH = auth.hash_password(
  "login-timing-equalizer-not-a-real-credential"
)


@router.post("/token", response_model=schemas.TokenResponse)
@_limiter.limit("5/minute")
def login(
  request: Request,
  form: OAuth2PasswordRequestForm = Depends(),
  db: Session = Depends(get_db),
):
  """Authenticates the owner and returns a JWT access token."""
  _check_login_cooldown(form.username)
  owner = (
    db.query(models.Owner)
    .filter(models.Owner.username == form.username)
    .first()
  )
  # Constant-time: always run bcrypt so a missing username can't be told from a
  # wrong password by response timing. Verifying against a dummy hash when the
  # owner is absent keeps the cost identical; the boolean is then discarded.
  password_ok = auth.verify_password(
    form.password, owner.hashed_password if owner else _DUMMY_PASSWORD_HASH
  )
  if not owner or not password_ok:
    _record_login_failure(form.username)
    raise HTTPException(
      status_code=401,
      detail="Incorrect username or password.",
      headers={"WWW-Authenticate": "Bearer"},
    )
  # Existing installations may carry the legacy raw-bcrypt format, where only
  # the first 72 password bytes mattered. Upgrade it only after a successful
  # verification, keeping login compatible without a one-shot DB migration or
  # forced password reset. A failed best-effort write must not lock the owner
  # out after their credential already proved valid.
  owner_username = owner.username
  owner_token_epoch = owner.token_epoch
  if auth.password_needs_rehash(owner.hashed_password):
    try:
      owner.hashed_password = auth.hash_password(form.password)
      db.commit()
    except SQLAlchemyError as exc:
      db.rollback()
      log.warning("Could not upgrade legacy owner password hash: %s", exc)
  _reset_login_failures(form.username)
  token = auth.create_access_token(
    {"sub": owner_username}, token_epoch=owner_token_epoch
  )
  return schemas.TokenResponse(access_token=token)


@router.post("/app-token", dependencies=[Depends(reject_cross_site)])
def create_app_token_endpoint(
  body: schemas.AppTokenRequest,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Returns a short-lived JWT scoped to a specific mini-app."""
  # A tombstoned (soft-deleted) app must not be granted fresh authority — no new
  # token for an uninstalled app. Revive (reinstall/recover) makes it mintable
  # again. See feature 110.
  app = (
    db.query(models.App)
    .filter(models.App.id == body.app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")
  token = auth.create_app_token(
    body.app_id,
    owner.username,
    owner.token_epoch,
    app_nonce=app.token_nonce,
  )
  return {"token": token}


@router.post("/app-job-token", dependencies=[Depends(reject_cross_site)])
def create_app_job_token_endpoint(
  body: schemas.AppTokenRequest,
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Mint a narrower-lifetime app token for one supervised job run."""
  from datetime import timedelta

  app = (
    db.query(models.App)
    .filter(models.App.id == body.app_id, models.App.deleted_at.is_(None))
    .first()
  )
  if not app:
    raise HTTPException(status_code=404, detail="App not found.")
  return {
    "token": auth.create_app_token(
      body.app_id,
      owner.username,
      owner.token_epoch,
      app_nonce=app.token_nonce,
      expires_delta=timedelta(hours=2),
    )
  }


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


@router.post("/provider/login", dependencies=[Depends(reject_cross_site)])
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


@router.post("/provider/code", dependencies=[Depends(reject_cross_site)])
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

  code, returned_state = _extract_provider_code_and_state(body.code)
  # State is verified only when the user pasted the full callback
  # URL (which contains `#state=...`). Bare-code pastes are still
  # accepted — the original flow worked that way and breaking it
  # would lock out anyone whose provider redirect doesn't surface
  # the state fragment in a copy-pasteable shape. PKCE's
  # code-verifier check below is the load-bearing CSRF defense; the
  # state check is belt-and-suspenders only when state is present.
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
  owner: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  """Checks whether the active provider has local credentials configured.

  Uses the provider's own check_auth method so this endpoint works
  for any registered provider, not just Claude. `authenticated` remains as a
  compatibility alias; neither field performs a remote token probe.
  """
  from starlette.concurrency import run_in_threadpool
  from app.providers import (
    get_provider,
    owner_default_provider,
    provider_requirement_error,
  )
  # The active provider is the one the last-selected model implies (the single
  # source of truth), so this status matches the provider chats will actually use.
  provider_id = owner_default_provider(get_settings().data_dir, owner.provider)
  provider = get_provider(provider_id)
  error = provider_requirement_error(provider, db)
  if error is None:
    error = await run_in_threadpool(provider.check_auth, get_settings().data_dir)
  return {
    "provider": provider_id,
    "provider_name": provider.name,
    "configured": error is None,
    "authenticated": error is None,
    "error": error,
  }


@router.get("/providers/status")
async def providers_status(
  principal: Principal = Depends(get_chat_view_principal),
  db: Session = Depends(get_db),
):
  """Returns local credential status for ALL registered providers.

  The `/provider/status` route above only reports the currently-
  active provider. Mini-app setup screens also need the full provider
  map, using app tokens, so their model pickers can disable disconnected
  providers instead of guessing. `configured` is the durable semantic field;
  `authenticated` is retained for compatibility with installed mini-apps.

  Availability is safe to share across the app/embed boundary; the owner's
  Möbius trial balance is not. `trial` is therefore attached only for a true
  owner caller, so an app or chat-embed principal sees the same availability
  fields without the owner's credit units and grant expiries.
  """
  require_chat_embed_operation(principal, "models:read")
  is_owner_caller = principal.app_id is None and principal.scope == "owner"
  from starlette.concurrency import run_in_threadpool
  from app.providers import PROVIDERS, provider_requirement_error
  data_dir = get_settings().data_dir
  out = {}
  for pid, provider in PROVIDERS.items():
    requirement_error = provider_requirement_error(provider, db)
    # check_auth may perform blocking I/O (e.g. MobiusProvider probes the
    # local broker over a Unix socket). Run it off the event loop so a slow
    # broker or central service cannot stall the ASGI worker for other callers.
    error = requirement_error or await run_in_threadpool(provider.check_auth, data_dir)
    out[pid] = {
      "name": provider.name,
      "configured": error is None,
      "authenticated": error is None,
      "error": error,
    }
    if provider.required_app_slug:
      out[pid]["available"] = requirement_error is None
    if pid == "mobius" and requirement_error is None and is_owner_caller:
      if error is None:
        try:
          out[pid]["trial"] = await run_in_threadpool(provider.trial_status)
        except Exception:
          out[pid]["trial"] = None
  return out


def _claude_tier(model_id: str) -> str | None:
  """Derives the marketing tier (opus / sonnet / haiku) from a Claude
  model id. Used by `/providers/models` so mini-app pickers don't need
  to parse model ids themselves to group rows by tier. Returns None
  for ids that don't match a known tier substring — the caller leaves
  the field out rather than fabricating a label."""
  lowered = model_id.lower()
  for tier in ("opus", "sonnet", "haiku"):
    if tier in lowered:
      return tier
  return None


@router.get("/providers/models")
async def providers_models(
  owner: models.Owner = Depends(get_owner_app_or_chat_embed_for_models),
):
  """Per-provider model list for mini-app pickers.

  Mini-apps (news, future siblings) can't import the shell's JS
  constants, so they ask the backend for the same list the shell
  shows. Data flows through `providers.list_models()` — the SDK-
  aware path that hits Anthropic's `/v1/models` for Claude and
  `AsyncCodex.models()` for Codex, with a 5-minute cache and a
  KNOWN_MODELS fallback per provider so a transient upstream blip
  still returns a usable list.

  Response shape is tighter than `/api/models` (which the shell uses):
  no `available` flag, no `provider` key inside each row (the outer
  key already says it), and a derived `tier` for Claude models so
  pickers can group by Opus/Sonnet/Haiku without parsing ids. The
  shell keeps its own endpoint because its picker depends on the
  richer fields; mini-apps get a stable, narrow surface. Hidden-model
  preferences are still honored so app pickers match the chat picker.

  Accepts owner OR app-scoped tokens — mini-app Settings tabs (news
  picker and Reflection Settings) need this list to
  render real choices. Rejecting app tokens here was the silent reason
  those pickers fell back to FALLBACK_GROUPS (one model per provider).
  This is a read; no state changes and no cross-app concerns — the
  CLI runtime already exposes the same list to every running app.
  """
  from app.providers import list_models
  data_dir = get_settings().data_dir
  registry = await list_models(data_dir)
  from app.providers import hidden_model_ids
  hidden_ids = set(hidden_model_ids(owner.model_prefs_json))
  out: dict[str, list[dict[str, str]]] = {}
  for provider_id, entries in registry.items():
    rows: list[dict[str, str]] = []
    for entry in entries:
      if entry["id"] in hidden_ids:
        continue
      row: dict[str, str] = {
        "id": entry["id"],
        "name": entry["label"],
      }
      if provider_id == "claude":
        tier = _claude_tier(entry["id"])
        if tier:
          row["tier"] = tier
      rows.append(row)
    out[provider_id] = rows
  return out


# -- Codex device-auth flow -------------------------------------------------
#
# Uses `codex login --device-auth` subprocess. The backend starts the
# process, parses the URL and one-time code from stdout, returns them
# to the frontend, then a background watcher awaits completion.

from pathlib import Path

from app.codex_login_parse import banner_has_code, parse_login_banner

_codex_login_procs: dict[str, asyncio.subprocess.Process] = {}
_codex_login_status: dict[str, str] = {}  # "complete" | "failed"


async def _watch_codex_login(proc):
  """Background task that awaits proc.wait() and stores the result."""
  await proc.wait()
  # Only update if this proc is still the active one -- a newer
  # login may have replaced it.
  if _codex_login_procs.get("active") is proc:
    _codex_login_status["result"] = (
      "complete" if proc.returncode == 0 else "failed"
    )
    _codex_login_procs.pop("active", None)


@router.post(
  "/provider/codex/login", dependencies=[Depends(reject_cross_site)],
)
async def codex_login_start(
  _: models.Owner = Depends(get_current_owner),
):
  """Starts codex login --device-auth and returns the URL + code."""
  # Kill any existing login process before starting a new one.
  old_proc = _codex_login_procs.pop("active", None)
  if old_proc and old_proc.returncode is None:
    old_proc.kill()
    try:
      await asyncio.wait_for(old_proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
      pass

  settings = get_settings()
  codex_home = str(Path(settings.data_dir) / "cli-auth" / "codex")
  Path(codex_home).mkdir(parents=True, exist_ok=True)

  env = dict(os.environ)
  env["CODEX_HOME"] = codex_home

  proc = await asyncio.create_subprocess_exec(
    "codex", "login", "--device-auth",
    stdin=asyncio.subprocess.DEVNULL,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.STDOUT,  # merge stderr into stdout
    env=env,
  )

  # Read until we see the device code or EOF.
  output = ""
  try:
    async with asyncio.timeout(15):
      while True:
        line = await proc.stdout.readline()
        if not line:
          break
        output += line.decode("utf-8", errors="replace")
        if banner_has_code(output):
          break
  except asyncio.TimeoutError:
    proc.kill()
    try:
      await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
      pass
    log.warning("codex login timed out, output: %s", output[:500])
    raise HTTPException(500, "Codex login timed out")

  parsed = parse_login_banner(output)
  if parsed is None:
    proc.kill()
    try:
      await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
      pass
    log.warning(
      "Could not parse device code from codex output: %s",
      output[:500],
    )
    raise HTTPException(500, "Could not parse device code")

  _codex_login_procs["active"] = proc
  _codex_login_status.pop("result", None)
  asyncio.create_task(_watch_codex_login(proc))

  return parsed


@router.get("/provider/codex/status")
async def codex_login_status_view(
  _: models.Owner = Depends(get_current_owner),
):
  """Returns the device-auth login status (for frontend polling)."""
  if "active" not in _codex_login_procs:
    result = _codex_login_status.pop("result", None)
    if result:
      return {"status": result}
    return {"status": "none"}
  return {"status": "pending"}
