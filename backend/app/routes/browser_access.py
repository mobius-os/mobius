"""Connect-owned shared-browser grants and independently revocable sessions."""

from datetime import timedelta
from typing import Literal
import logging
from urllib.parse import urlsplit, quote
import re
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm import Session

from app import auth, models, browser_access as access, account_browser_access as account_access
from app.database import get_db
from app.deps import (
  Principal, get_principal, get_owner_or_app_with_connect_manage,
  require_installation_owner_control, reject_cross_site,
)

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/connect/browser-access", tags=["connect"])
_limiter = Limiter(key_func=get_remote_address)
_COOKIE = "mobius_shared_browser"
_COOKIE_PATH = "/api/connect/browser-access/session"
# The cookie is written only when a browser signs in, never on renewal, so a
# late renewal response can never overwrite a newer sign-in. Its lifetime is
# therefore long and fixed; the server-side session idle expiry is the authority.
_COOKIE_MAX_AGE = int(timedelta(days=400).total_seconds())
_ACCESS_TTL = timedelta(minutes=15)
_NO_STORE = {"Cache-Control": "no-store"}
_SECRET_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


class LogoutRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  grant_id: str = Field(min_length=1, max_length=64)


class AccountRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  recipient_handle: str = Field(min_length=3, max_length=31)
  instance_name: str | None = Field(default=None, max_length=128)


class SharedInvitationResponse(BaseModel):
  model_config = ConfigDict(extra="forbid")
  origin: str = Field(max_length=255)
  grant_id: str = Field(pattern=r"^[A-Za-z0-9_-]{20,64}$")
  action: Literal["accept", "later"]


class GrantUpdate(BaseModel):
  model_config = ConfigDict(extra="forbid")
  full_access: bool


class FinalizeAccountRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  pending_id: str = Field(min_length=1, max_length=64)


def _cookie_request(request: Request) -> None:
  # Cookie-bearing session renewal must be same ORIGIN, not merely same site.
  # The app's opaque-frame bearer exception does not authorize these cookies.
  try:
    same_origin = access.canonical_https_origin(request.headers.get("origin")) == access.runtime_origin()
  except (ValueError, HTTPException):
    same_origin = False
  if not same_origin:
    raise HTTPException(403, "Open this invitation on the receiving Möbius address.")


def _manager(
  principal: Principal = Depends(get_principal),
  owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
) -> models.Owner:
  require_installation_owner_control(principal)
  return owner


def _grant_status(db: Session, grant) -> str:
  if grant.revoked_at:
    return "revoked"
  if grant.kind == "account":
    # A relink or address change leaves the grant unusable until re-invited.
    return grant.remote_status if access.account_binding_holds(db, grant) else "inactive"
  accepted = db.query(access.BrowserAccessInvite.id).filter(
    access.BrowserAccessInvite.grant_id == grant.id,
    access.BrowserAccessInvite.consumed_at.isnot(None),
  ).first() is not None
  return "active" if accepted else "invited"


def _grant_view(db: Session, grant) -> dict:
  return {
    "stop_pending": bool(grant.revoked_at) and access.stop_pending(db, grant.id),
    "id": grant.id, "label": grant.label,
    "status": _grant_status(db, grant),
    "created_at": grant.created_at.isoformat() + "Z",
    "kind": grant.kind,
    "recipient_handle": grant.recipient_handle,
    "directory_cleanup_pending": grant.remote_status == "cleanup_pending",
    "full_access": bool(grant.full_access),
  }


def _session_response(db: Session, grant, session, owner, new_secret: str | None = None) -> JSONResponse:
  """Mint the browser bearer; set the cookie only when a sign-in created it."""
  token = auth.create_access_token(
    {"sub": owner.username},
    expires_delta=_ACCESS_TTL, token_epoch=owner.token_epoch,
    browser=access.BrowserLineage(grant.id, session.id),
  )
  response = JSONResponse({
    "access_token": token, "token_type": "bearer",
    "expires_in": int(_ACCESS_TTL.total_seconds()),
    "grant": _grant_view(db, grant),
  }, headers=_SECRET_HEADERS)
  if new_secret is not None:
    response.set_cookie(
      _COOKIE, new_secret, httponly=True, secure=True, samesite="strict",
      path=_COOKIE_PATH, max_age=_COOKIE_MAX_AGE,
    )
  return response


@router.get("")
async def list_browser_grants(owner: models.Owner = Depends(_manager), db: Session = Depends(get_db)):
  rows = db.query(access.BrowserAccessGrant).filter_by(owner_id=owner.id).order_by(
    access.BrowserAccessGrant.created_at.asc(),
  ).all()
  return {"grants": [_grant_view(db, row) for row in rows]}


# ── Account grants (recipient identified by a mobius.you handle) ──


async def _end(db: Session, grant) -> access.GrantEnd:
  ended = await access.end_grant(db, grant)
  if ended.stop_error is not None:
    raise ended.stop_error
  return ended


@router.post("/accounts", dependencies=[Depends(reject_cross_site)])
async def add_account_recipient(
  body: AccountRequest, owner: models.Owner = Depends(_manager), db: Session = Depends(get_db),
):
  handle = body.recipient_handle.strip().lower().lstrip("@")
  if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,28}[a-z0-9]", handle):
    raise account_access.SharedAccessError(422, "Enter a valid mobius.you handle.", "invalid_handle")
  origin = access.runtime_origin()
  # The recipient sees this name in "Shared with me"; default to this address.
  name = (body.instance_name or "").strip() or urlsplit(origin).netloc
  issuer = access.issuer_origin()
  binding = access.account_binding(db, owner.id)
  owner_epoch = owner.token_epoch
  if binding is None:
    raise account_access.account_unlinked()
  # One live grant per recipient. The directory pins a grant's name, so a
  # repeated invite returns the existing grant rather than adding a second.
  grant = db.query(access.BrowserAccessGrant).filter_by(
    owner_id=owner.id, kind="account", recipient_handle=handle, revoked_at=None,
  ).order_by(access.BrowserAccessGrant.created_at.asc()).first()
  if grant is not None and not access.account_binding_holds(db, grant):
    # Created under an earlier account link or address: replace it.
    access.revoke_grant(db, grant.id, owner.id)
    await _end(db, grant)
    grant = None
  if grant is not None and grant.remote_status == "active":
    return JSONResponse({"grant": _grant_view(db, grant)}, headers=_NO_STORE)
  if grant is None:
    grant = access.BrowserAccessGrant(
      id=secrets.token_urlsafe(24), owner_id=owner.id, label=name,
      kind="account", issuer=issuer, recipient_handle=handle,
      origin=origin, remote_status="pending", grantor_binding=binding,
    )
    db.add(grant)
    db.commit()
  # Keep a reserved ID (and its name) on failure: the issuer may have committed
  # before its response was lost, so a fresh ID could leave a real grant behind.
  value = account_access.remote_json(await account_access.issuer_request(db, owner.id, "POST", "/grants", {
    "grant_id": grant.id, "recipient_handle": handle, "instance_name": grant.label,
  }))
  if (value.get("issuer") != issuer or value.get("grant_id") != grant.id
      or value.get("origin") != origin or value.get("handle") != handle
      or not isinstance(value.get("subject"), str) or not value["subject"]):
    raise HTTPException(502, "The account directory returned mismatched grant identity. Retry this operation.")
  db.refresh(grant)
  db.refresh(owner)
  if (grant.revoked_at is not None or not access.account_binding_holds(db, grant)
      or owner.token_epoch != owner_epoch):
    # A remote registration may have committed just as local permission ended.
    # Clean it if possible, but never activate the now-stale local grant.
    access.revoke_grant(db, grant.id, owner.id)
    await _end(db, grant)
    raise HTTPException(409, "This grant changed while the account directory responded. Local access ended; check directory cleanup status.")
  grant.subject = value["subject"]
  grant.remote_status = "active"
  db.commit()
  return JSONResponse({"grant": _grant_view(db, grant)}, headers=_NO_STORE)


def _shared_instance(row) -> dict | None:
  """Validate one discovery row; neither a notification nor its URL grants access."""
  if not isinstance(row, dict):
    return None
  origin, gid = row.get("origin"), row.get("grant_id")
  try:
    parsed = urlsplit(origin) if isinstance(origin, str) else None
    if (not parsed or parsed.scheme != "https" or not parsed.hostname
        or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
        or not parsed.netloc or len(origin) > 255):
      return None
    parsed.port  # Reject malformed ports rather than forwarding them.
  except ValueError:
    return None
  name, owner_handle = row.get("name"), row.get("owner_handle")
  if (not isinstance(gid, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", gid)
      or not isinstance(name, str) or len(name) > 128
      or not isinstance(owner_handle, str) or len(owner_handle) > 128
      or row.get("status") not in ("invited", "accepted")
      or type(row.get("unread")) is not bool):
    return None
  query = "?grant_id=" + quote(gid, safe="")
  return {"grant_id": gid, "name": name, "origin": origin, "owner_handle": owner_handle,
          "status": row["status"], "unread": row["unread"],
          "open_url": origin + "/api/connect/browser-access/session/account/start" + query,
          # Leaving runs on the sharing instance, which must support it.
          "leave_url": origin + "/api/connect/browser-access/session/account/leave/start" + query}


@router.get("/shared")
async def shared_with_me(owner: models.Owner = Depends(_manager), db: Session = Depends(get_db)):
  value = account_access.remote_json(await account_access.issuer_request(db, owner.id, "GET", "/shared"))
  rows = value.get("instances")
  if not isinstance(rows, list):
    raise HTTPException(502, "The account directory returned an invalid response.")
  # One malformed row must not hide every other invitation.
  instances = [instance for instance in map(_shared_instance, rows) if instance is not None]
  return JSONResponse({"instances": instances}, headers=_NO_STORE)


@router.post("/shared/respond", dependencies=[Depends(reject_cross_site)])
async def respond_to_shared_invitation(
  body: SharedInvitationResponse, owner: models.Owner = Depends(_manager),
  db: Session = Depends(get_db),
):
  probe = {"origin": body.origin, "grant_id": body.grant_id, "name": "",
           "owner_handle": "", "status": "invited", "unread": False}
  if _shared_instance(probe) is None:
    raise HTTPException(422, "The invitation address is invalid.")
  # Only the fixed issuer is contacted. It authenticates the recipient from the
  # existing account credential; the submitted instance address is never fetched.
  value = account_access.remote_json(await account_access.issuer_request(
    db, owner.id, "POST", "/shared/respond", body.model_dump(),
  ))
  instance = _shared_instance(value.get("instance"))
  if (instance is None or instance["origin"] != body.origin or instance["grant_id"] != body.grant_id
      or instance["unread"] or (body.action == "accept" and instance["status"] != "accepted")):
    raise HTTPException(502, "The account directory returned a mismatched invitation response.")
  return JSONResponse({"instance": instance}, headers=_NO_STORE)


_PENDING_COOKIE = "mobius_shared_account_pending"
# The same one-use proof flow, started to give access up rather than sign in.
# Which cookie holds the proof is its purpose: a sign-in proof cannot leave.
_LEAVE_COOKIE = "mobius_shared_account_leave"
_PENDING_PATH = "/api/connect/browser-access/session/account"


def _start_proof(db: Session, grant_id: str, cookie_name: str) -> RedirectResponse:
  cookie, url = account_access.start(db, grant_id)
  response = RedirectResponse(url, status_code=303, headers=_SECRET_HEADERS)
  response.set_cookie(cookie_name, cookie, httponly=True, secure=True,
                      samesite="lax", path=_PENDING_PATH, max_age=600)
  return response


@router.get("/session/account/start")
@_limiter.limit("10/minute")
def start_account_session(request: Request, grant_id: str, db: Session = Depends(get_db)):
  return _start_proof(db, grant_id, _PENDING_COOKIE)


@router.get("/session/account/leave/start")
@_limiter.limit("10/minute")
def start_account_leave(request: Request, grant_id: str, db: Session = Depends(get_db)):
  """Recipient-initiated: prove the account, then confirm leaving here."""
  return _start_proof(db, grant_id, _LEAVE_COOKIE)


@router.get("/session/account/callback")
@_limiter.limit("10/minute")
async def complete_account_session(
  request: Request, code: str, state: str, db: Session = Depends(get_db),
):
  # The directory returns both flows here. Only a leave cookie bound to this
  # exact state makes it a leave; otherwise it is an ordinary sign-in.
  leaving = account_access.cookie_matches_state(db, state, request.cookies.get(_LEAVE_COOKIE, ""))
  pending, grant = account_access.pending_for_callback(
    db, state, request.cookies.get(_LEAVE_COOKIE if leaving else _PENDING_COOKIE, ""),
  )
  proof_expiry = await account_access.exchange_code(pending, grant, code)
  account_access.mark_verified(db, pending.id, proof_expiry)
  # Leaving is confirmed on this origin, so a cross-site link alone never
  # ends someone's access.
  fragment = "account-leave=" if leaving else "account-finalize="
  return RedirectResponse("/shell/shared#" + fragment + pending.id, status_code=303,
                          headers=_SECRET_HEADERS)


@router.post("/session/account/finalize")
@_limiter.limit("10/minute")
def finalize_account_session(
  body: FinalizeAccountRequest, request: Request, db: Session = Depends(get_db),
):
  _cookie_request(request)
  pending = account_access.verified_for_cookie(db, request.cookies.get(_PENDING_COOKIE, ""))
  if pending.id != body.pending_id:
    raise HTTPException(401, "Account sign-in unavailable. Start again from the shared link.")
  secret, session, grant, owner = account_access.complete(
    db, pending.id, request.cookies.get(_COOKIE),
  )
  response = _session_response(db, grant, session, owner, secret)
  response.delete_cookie(_PENDING_COOKIE, path=_PENDING_PATH,
                         secure=True, httponly=True, samesite="lax")
  return response


@router.post("/session/account/leave")
@_limiter.limit("10/minute")
async def leave_shared_access(
  body: FinalizeAccountRequest, request: Request, db: Session = Depends(get_db),
):
  _cookie_request(request)
  pending = account_access.verified_for_cookie(db, request.cookies.get(_LEAVE_COOKIE, ""))
  if pending.id != body.pending_id:
    raise HTTPException(401, "Account sign-in unavailable. Start again from the shared link.")
  grant = await account_access.leave(db, pending.id)
  response = JSONResponse({"left": True, "name": grant.label}, headers=_NO_STORE)
  response.delete_cookie(_LEAVE_COOKIE, path=_PENDING_PATH,
                         secure=True, httponly=True, samesite="lax")
  # This browser's shared session belonged to the grant and is now dead.
  response.delete_cookie(_COOKIE, path=_COOKIE_PATH, secure=True, httponly=True, samesite="strict")
  return response


# ── Sessions and revocation (all grant kinds) ────────────────────


@router.post("/session")
@_limiter.limit("60/minute")
def browser_session(request: Request, db: Session = Depends(get_db)):
  _cookie_request(request)
  grant, session, owner = access.renew_session(db, request.cookies.get(_COOKIE, ""))
  return _session_response(db, grant, session, owner)


@router.post("/session/logout", status_code=204)
def logout_browser_session(body: LogoutRequest, request: Request, db: Session = Depends(get_db)):
  _cookie_request(request)
  access.logout_session(db, request.cookies.get(_COOKIE, ""), grant_id=body.grant_id)
  response = Response(status_code=204, headers=_NO_STORE)
  response.delete_cookie(_COOKIE, path=_COOKIE_PATH, secure=True, httponly=True, samesite="strict")
  return response


@router.patch("/{grant_id}", dependencies=[Depends(reject_cross_site)])
def update_browser_grant(
  grant_id: str, body: GrantUpdate,
  owner: models.Owner = Depends(_manager), db: Session = Depends(get_db),
):
  """Turn one person's Full access on or off. It applies from their next
  request, including to work they already started."""
  grant = access._fresh(db, access.BrowserAccessGrant, grant_id)
  if grant is None or grant.owner_id != owner.id:
    raise HTTPException(404, "No such shared access.")
  if grant.revoked_at is not None:
    raise HTTPException(409, "This access was revoked.")
  grant.full_access = body.full_access
  db.commit()
  return JSONResponse({"grant": _grant_view(db, grant)}, headers=_NO_STORE)


@router.delete("/{grant_id}", status_code=204, dependencies=[Depends(reject_cross_site)])
async def revoke_browser_access(
  grant_id: str, owner: models.Owner = Depends(_manager), db: Session = Depends(get_db),
):
  grant = access.revoke_grant(db, grant_id, owner.id)
  # The revoke is committed before cleanup, so a failed stop step is reported
  # as unfinished stopping (retryable), never as access that was kept.
  ended = await access.end_grant(db, grant)
  if ended.stop_error is not None:
    log.warning("browser grant %s revoked; stopping its work failed: %r", grant.id, ended.stop_error)
  if not (ended.stop_pending or ended.directory_cleanup_pending):
    return Response(status_code=204)
  body = {
    "revoked": True, "pending_commands": ended.pending_commands,
    "pending_chat_ids": ended.pending_chat_ids,
    "stop_incomplete": ended.stop_error is not None,
  }
  if grant.kind == "account":
    body["directory_cleanup_pending"] = ended.directory_cleanup_pending
  return JSONResponse(body, status_code=202, headers=_NO_STORE)


# ── Link invitations (retired; redemption of already-issued links only) ──


class RedeemRequest(BaseModel):
  model_config = ConfigDict(extra="forbid")
  invite: str = Field(min_length=20, max_length=256)


@router.post("/session/redeem")
@_limiter.limit("10/minute")
def redeem_browser_invitation(body: RedeemRequest, request: Request, db: Session = Depends(get_db)):
  _cookie_request(request)
  secret, session, grant, owner = access.redeem_invitation(
    db, body.invite, previous_session_secret=request.cookies.get(_COOKIE),
  )
  return _session_response(db, grant, session, owner, secret)
