"""Account-bound shared browser sign-in; issuer is discovery and proof, never local authority."""

import base64
import hashlib
import hmac
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import httpx
from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.orm import Session

from app import browser_access as access
from app.config import get_settings
from app.timeutil import now_naive_utc

log = logging.getLogger(__name__)
PENDING_TTL = timedelta(minutes=10)


class SharedAccessError(HTTPException):
  """An error the Connect app can act on: ``{"detail": ..., "code": ...}``."""

  def __init__(self, status_code: int, detail: str, code: str):
    super().__init__(status_code, detail)
    self.code = code


def account_unlinked() -> SharedAccessError:
  return SharedAccessError(409, "Link your mobius.you account in Identity first.", "account_unlinked")


# Directory error codes with a message the owner can act on. Any other code is
# still passed through, with a generic message.
_DIRECTORY_MESSAGES = {
  "unknown_handle": (404, "No mobius.you account has that handle."),
  "invalid_handle": (422, "Enter a valid mobius.you handle."),
}
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


async def issuer_request(db: Session, owner_id: int, method: str, suffix: str, payload: dict | None = None) -> httpx.Response:
  """Only the configured account service receives a scoped local credential."""
  from app.routes import identity
  settings = get_settings()
  if settings.mobius_sso_enabled:
    return await identity._managed_response(
      method, "/api/instance/v1/browser-access" + suffix,
      **({"json": payload} if payload is not None else {}),
    )
  link = identity._linked_row(db, owner_id)
  if link is None:
    raise account_unlinked()
  try:
    token = identity._open(link.access_token_encrypted)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
      return await client.request(
        method, settings.mobius_account_origin + "/api/account/v1/browser-access" + suffix,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        **({"json": payload} if payload is not None else {}),
      )
  except (httpx.HTTPError, OSError) as exc:
    raise HTTPException(502, "The account directory could not be reached. Retry this operation.") from exc


def remote_json(response: httpx.Response, *, allowed=(200, 201)) -> dict:
  """Directory JSON, or its rejection passed through as a status and code."""
  if response.status_code not in allowed:
    status = response.status_code if response.status_code in (400, 401, 403, 404, 409, 422) else 502
    try:
      code = response.json().get("error")
    except (ValueError, AttributeError):
      code = None
    if not isinstance(code, str) or not _CODE.fullmatch(code):
      code = "directory_rejected"
    status, detail = _DIRECTORY_MESSAGES.get(code, (
      status, "The account directory rejected the request. Retry or check your account link.",
    ))
    raise SharedAccessError(status, detail, code)
  try:
    value = response.json()
  except ValueError as exc:
    raise HTTPException(502, "The account directory returned an invalid response.") from exc
  if not isinstance(value, dict):
    raise HTTPException(502, "The account directory returned an invalid response.")
  return value


async def unregister(db: Session, grant) -> str:
  """Remove a grant from the directory: ``revoked``, ``pending`` (retry later)
  or ``credential_rejected`` (the link credential can no longer clean up)."""
  try:
    response = await issuer_request(db, grant.owner_id, "DELETE", "/grants/" + quote(grant.id, safe=""))
  except HTTPException as exc:
    # 409: no link, or its stored credential can no longer be opened.
    return "credential_rejected" if exc.status_code == 409 else "pending"
  if response.status_code in (200, 204):
    return "revoked"
  return "credential_rejected" if response.status_code == 401 else "pending"


def _invalid() -> HTTPException:
  return HTTPException(401, "Account sign-in unavailable. Start again from the shared link.")


def _require_usable(db: Session, grant, pending=None) -> None:
  """An account grant may sign in only while active, registered, and bound to
  the owner's current account link; a pending sign-in must match it."""
  if (grant is None or grant.kind != "account" or grant.remote_status != "active"
      or grant.revoked_at is not None or not grant.subject
      or not access.account_binding_holds(db, grant)):
    raise _invalid()
  if pending is not None and (
    pending.issuer != grant.issuer or pending.subject != grant.subject
    or access._owner(db, grant.owner_id).token_epoch != pending.owner_token_epoch
  ):
    raise _invalid()


def start(db: Session, grant_id: str):
  grant = db.get(access.BrowserAccessGrant, grant_id)
  _require_usable(db, grant)
  owner = access._owner(db, grant.owner_id)
  state, cookie, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(4))
  challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
  pending = access.BrowserAccountPending(
    id=secrets.token_urlsafe(24), grant_id=grant.id, state_hash=access.digest(state),
    cookie_hash=access.digest(cookie), verifier=verifier, nonce=nonce,
    owner_token_epoch=owner.token_epoch,
    issuer=grant.issuer, subject=grant.subject,
    expires_at=now_naive_utc() + PENDING_TTL,
  )
  db.query(access.BrowserAccountPending).filter(
    access.BrowserAccountPending.expires_at < now_naive_utc() - timedelta(days=1),
  ).delete(synchronize_session=False)
  db.add(pending)
  db.commit()
  url = access.issuer_origin() + "/shared-access/authorize?" + urlencode({
    "origin": grant.origin, "grant_id": grant.id, "state": state,
    "code_challenge": challenge, "nonce": nonce,
  })
  return cookie, url


def cookie_matches_state(db: Session, state: str, cookie: str) -> bool:
  """Whether this cookie is the one bound to this callback state."""
  if not isinstance(state, str) or not 20 <= len(state) <= 256 or not isinstance(cookie, str) or not 20 <= len(cookie) <= 256:
    return False
  pending = db.query(access.BrowserAccountPending).filter_by(state_hash=access.digest(state)).first()
  return pending is not None and hmac.compare_digest(pending.cookie_hash, access.digest(cookie))


def pending_for_callback(db: Session, state: str, cookie: str):
  if not isinstance(state, str) or not 20 <= len(state) <= 256 or not isinstance(cookie, str) or not 20 <= len(cookie) <= 256:
    raise _invalid()
  pending = db.query(access.BrowserAccountPending).filter_by(state_hash=access.digest(state)).first()
  if (pending is None or pending.consumed_at is not None or pending.expires_at <= now_naive_utc()
      or not hmac.compare_digest(pending.cookie_hash, access.digest(cookie))):
    raise _invalid()
  grant = db.get(access.BrowserAccessGrant, pending.grant_id)
  _require_usable(db, grant, pending)
  return pending, grant


async def exchange_code(pending, grant, code: str) -> datetime:
  """Redeem the issuer's code; return the proof's expiry (aware UTC)."""
  if not isinstance(code, str) or not 20 <= len(code) <= 256:
    raise _invalid()
  try:
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
      response = await client.post(access.issuer_origin() + "/shared-access/token", json={
        "code": code, "code_verifier": pending.verifier,
        "origin": grant.origin, "grant_id": grant.id,
      }, headers={"Accept": "application/json"})
  except httpx.HTTPError as exc:
    raise HTTPException(502, "Account sign-in could not be verified. Try again.") from exc
  if response.status_code != 200:
    raise _invalid() if response.status_code in (400, 401, 403, 404, 409) else HTTPException(502, "Account sign-in could not be verified. Try again.")
  try:
    proof = response.json()
  except ValueError as exc:
    raise HTTPException(502, "Account sign-in returned invalid proof.") from exc
  if not isinstance(proof, dict):
    raise _invalid()
  try:
    expiry = datetime.fromisoformat(proof.get("expires_at").replace("Z", "+00:00"))
  except (AttributeError, ValueError) as exc:
    raise _invalid() from exc
  now = datetime.now(timezone.utc)
  if expiry.tzinfo is None or expiry <= now or expiry > now + timedelta(minutes=2):
    raise _invalid()
  expected = {"iss": pending.issuer, "sub": pending.subject,
              "aud": "mobius-shared-browser", "origin": grant.origin,
              "grant_id": grant.id, "nonce": pending.nonce}
  if any(proof.get(key) != value for key, value in expected.items()):
    raise _invalid()
  return expiry


def mark_verified(db: Session, pending_id: str, proof_expiry: datetime) -> None:
  now = now_naive_utc()
  expiry = min(proof_expiry.astimezone(timezone.utc).replace(tzinfo=None), now + timedelta(seconds=60))
  if expiry <= now:
    raise _invalid()
  changed = db.execute(update(access.BrowserAccountPending).where(
    access.BrowserAccountPending.id == pending_id,
    access.BrowserAccountPending.consumed_at.is_(None),
    access.BrowserAccountPending.verified_at.is_(None),
    access.BrowserAccountPending.expires_at > now,
  ).values(verified_at=now, verified_expires_at=expiry)).rowcount
  if changed != 1:
    db.rollback()
    raise _invalid()
  db.commit()


def verified_for_cookie(db: Session, cookie: str) -> access.BrowserAccountPending:
  if not isinstance(cookie, str) or not 20 <= len(cookie) <= 256:
    raise _invalid()
  pending = db.query(access.BrowserAccountPending).filter_by(cookie_hash=access.digest(cookie)).first()
  if (pending is None or pending.verified_at is None or pending.consumed_at is not None
      or pending.expires_at <= now_naive_utc()
      or pending.verified_expires_at is None or pending.verified_expires_at <= now_naive_utc()):
    raise _invalid()
  return pending


def _consume_verified(db: Session, pending_id: str):
  """Spend one verified pending proof; return it with its locked, usable grant.

  The caller commits or rolls back."""
  now = now_naive_utc()
  pending = db.get(access.BrowserAccountPending, pending_id)
  if (pending is None or pending.verified_at is None
      or pending.verified_expires_at is None or pending.verified_expires_at <= now):
    raise _invalid()
  consumed = db.execute(update(access.BrowserAccountPending).where(
    access.BrowserAccountPending.id == pending_id,
    access.BrowserAccountPending.consumed_at.is_(None),
    access.BrowserAccountPending.expires_at > now,
  ).values(consumed_at=now)).rowcount
  if consumed != 1:
    raise _invalid()
  grant = access._lock_active_grant(db, pending.grant_id)
  _require_usable(db, grant, pending)
  return pending, grant


def complete(db: Session, pending_id: str, previous_session_secret: str | None = None):
  try:
    _pending, grant = _consume_verified(db, pending_id)
    owner = access._owner(db, grant.owner_id)
    secret, session = access.open_session(db, grant, owner, previous_session_secret)
    db.commit()
    return secret, session, grant, owner
  except Exception:
    db.rollback()
    raise


async def leave(db: Session, pending_id: str) -> access.BrowserAccessGrant:
  """The verified recipient gives up their own grant: the owner's revoke.

  Access lives on this instance, so the recipient need not wait for the owner.
  Revocation commits first; cleanup that cannot finish now stays visible and
  retryable in the owner's Connect, exactly as after the owner's own revoke."""
  try:
    _pending, grant = _consume_verified(db, pending_id)
    db.commit()
  except Exception:
    db.rollback()
    raise
  grant = access.revoke_grant(db, grant.id, grant.owner_id)
  ended = await access.end_grant(db, grant)
  if ended.stop_error is not None:
    log.warning("browser grant %s left; stopping its work failed: %r", grant.id, ended.stop_error)
  return grant
