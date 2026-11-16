"""Invite people to a live Project by their Möbius @handle.

A handle invitation is an ordinary single-use Project invitation whose secret
is delivered to the invitee's own Möbius instead of being shown as a link.

Sender:    POST /send creates the invitation, resolves the handle to its
           deployment through the account directory, and delivers
           ``{from_handle, invite_url}`` to that deployment's ``/inbox``.
Receiver:  ``/inbox`` trusts nothing in the envelope. It resolves the claimed
           sender handle itself and requires the invitation URL's host to be
           one of that handle's deployments, then asks that host (``/check``)
           to confirm the secret names a live invitation and to describe it.
           Only then does it store the invite and notify its owner.
Accepting: the receiver redeems the secret with the sender's Möbius
           (``/redeem``) and keeps the returned project-only access token, so
           the Projects list can reopen the shared workspace later.

A lost answer never proves the peer did nothing. Delivery is idempotent and
retried; an invitation whose delivery can't be confirmed stays live for its
owner to revoke. Redemption carries a key the receiver saved before trying,
so a retry gets the same membership back instead of a spent invitation.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from slowapi import Limiter
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import auth, models
from app.config import get_settings
from app.database import get_db
from app.deps import (
  get_current_owner, get_current_owner_for_lifecycle_control, reject_cross_site,
)
from app.federation_transport import FederationTransportError, federation_request
from app.push import notify_owner_async
from app.routes.identity import _HANDLE_RE, owner_handle, resolve_handle_hosts
from app.routes.projects import (
  _INVITE_TTL, _invite_hash, _issue_project_invite, _live_project,
  _project_response,
)
from app.timeutil import now_naive_utc

router = APIRouter(prefix="/api/project-invites", tags=["project-invites"])

FORMAT = "mobius-project-invite-v1"
Role = Literal["viewer", "editor", "maintainer"]
SECRET_RE = r"^[A-Za-z0-9_-]{43}$"
HOST_RE = r"^[a-z0-9]([a-z0-9.-]{0,250})(:\d{1,5})?$"
# Unanswered invitations one sender may have waiting here, so nobody can
# crowd out everyone else's. Counted over the invitation lifetime by arrival,
# never by the expiry the sender reports.
MAX_PENDING_PER_SENDER = 10
# An invitation with less time left than this is refused on arrival.
MIN_INVITE_LIFETIME = timedelta(hours=1)
# The receiver makes two bounded outbound calls (directory, then /check)
# before answering a delivery, so one delivery may take longer than either;
# all deliveries of one invitation share one overall budget.
DELIVERY_TIMEOUT_S = 30.0
DELIVERY_BUDGET_S = 45.0
PEER_TIMEOUT_S = 12.0
# federation_request refuses hosts that don't resolve or aren't public with an
# HTTPException; to callers here that is one more unreachable peer.
PEER_ERRORS = (FederationTransportError, ValueError, httpx.HTTPError, HTTPException)
# Failures that prove a request never reached the peer, so it changed nothing
# there. Any other failure, or a 5xx, may have come after the peer acted.
NOT_SENT = (ValueError, HTTPException, httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
GONE = "This project invitation is invalid or has expired."

# Deliveries arrive from other people's servers through this instance's own
# proxy, so there is no per-sender address to key on. One bucket caps the
# outbound verification work strangers can cause; MAX_PENDING_PER_SENDER
# keeps any one of them from filling the inbox.
inbox_limiter = Limiter(key_func=lambda request: "project-invite-inbox")


class StrictBody(BaseModel):
  model_config = ConfigDict(extra="forbid")


def clean_handle(value: str) -> str:
  text = value.strip().lower().lstrip("@")
  return text if _HANDLE_RE.fullmatch(text) else ""


class HandleInviteSend(StrictBody):
  project_id: str = Field(min_length=1, max_length=64)
  handle: str = Field(min_length=1, max_length=40)
  role: Role = "editor"


class InviteSecret(StrictBody):
  invite: str = Field(pattern=SECRET_RE)


class HandleRedeem(StrictBody):
  invite: str = Field(pattern=SECRET_RE)
  redeem_key: str = Field(pattern=SECRET_RE)


class InboxEnvelope(StrictBody):
  format: Literal["mobius-project-invite-v1"]
  from_handle: str = Field(min_length=1, max_length=40)
  invite_url: str = Field(min_length=1, max_length=512)


# Answers from another Möbius. Unknown fields are ignored so a newer sender
# can describe more without breaking this receiver.

class PeerAnswer(BaseModel):
  model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)


class CheckedProject(PeerAnswer):
  name: str = Field(min_length=1, max_length=256)
  project_type: str | None = Field(default=None, max_length=128)


class CheckedInvite(PeerAnswer):
  format: Literal["mobius-project-invite-v1"]
  project: CheckedProject
  role: Role
  expires_at: datetime
  invitee: str | None = None


class RedeemedProject(PeerAnswer):
  id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
  name: str | None = Field(default=None, min_length=1, max_length=256)


class RedeemedAccess(PeerAnswer):
  access_token: str = Field(min_length=20, max_length=4096)
  project: RedeemedProject
  expires_at: datetime | None = None


# -- sealing ---------------------------------------------------------------

def _fernet() -> Fernet:
  material = f"mobius-received-project-invite-v1:{get_settings().secret_key}".encode()
  return Fernet(base64.urlsafe_b64encode(hashlib.sha256(material).digest()))


def _seal(value: str) -> str:
  return _fernet().encrypt(value.encode()).decode()


def _unseal(value: str | None) -> str | None:
  if not value:
    return None
  try:
    return _fernet().decrypt(value.encode()).decode()
  except (InvalidToken, ValueError):
    return None


# -- helpers ---------------------------------------------------------------

def _iso(value: datetime | None) -> str | None:
  return value.replace(tzinfo=UTC).isoformat() if value is not None else None


def _naive_utc(value: datetime | None) -> datetime | None:
  """Stored times are naive UTC; a peer may send either form."""
  if value is None or value.tzinfo is None:
    return value
  return value.astimezone(UTC).replace(tzinfo=None)


def _peer_detail(response: httpx.Response) -> str | None:
  try:
    detail = response.json().get("detail")
  except (ValueError, AttributeError):
    return None
  return detail[:300] if isinstance(detail, str) else None


def parse_invite_url(url: str) -> tuple[str, str]:
  """Return ``(host, secret)`` from a Project invitation link, or raise 422."""
  try:
    parsed = urlsplit(url)
    host = (parsed.netloc or "").lower()
  except ValueError as exc:
    raise HTTPException(422, "That is not a project invitation.") from exc
  if (
    parsed.scheme != "https"
    or parsed.path != "/project-invite"
    or parsed.query
    or parsed.username
    or parsed.password
    or not re.fullmatch(HOST_RE, host)
    or not re.fullmatch(SECRET_RE, parsed.fragment or "")
  ):
    raise HTTPException(422, "That is not a project invitation.")
  return host, parsed.fragment


def _view(row: models.ReceivedProjectInvite, now: datetime) -> dict[str, Any]:
  return {
    "id": row.id,
    "from_handle": row.from_handle,
    "from_host": row.from_host,
    "project_name": row.project_name,
    "project_type": row.project_type,
    "role": row.role,
    "status": row.status,
    "received_at": _iso(row.received_at),
    "expires_at": _iso(row.expires_at),
    "responded_at": _iso(row.responded_at),
    # Once an accept attempt may have reached the sender, the invitation
    # stays actionable: only the sender can say it was spent.
    "expired": (
      row.status == "pending" and row.expires_at <= now and row.redeem_key_sealed is None
    ),
    "access_expired": row.status == "accepted" and (
      row.access_expires_at is not None and row.access_expires_at <= now
    ),
  }


def _row(db: Session, invite_id: str) -> models.ReceivedProjectInvite:
  row = db.get(models.ReceivedProjectInvite, invite_id)
  if row is None:
    raise HTTPException(404, "Invitation not found.")
  return row


# -- sender ----------------------------------------------------------------

@router.post("/send", dependencies=[Depends(reject_cross_site)])
async def send_handle_invite(
  body: HandleInviteSend,
  response: Response,
  owner: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  handle = clean_handle(body.handle)
  if not handle:
    raise HTTPException(400, "Enter a Möbius @handle, like @alex.")
  project = _live_project(db, body.project_id)
  sender = await owner_handle(db, owner)
  if not sender:
    raise HTTPException(
      409, "Choose your @handle in Möbius · You before inviting people by handle.",
    )
  if handle == sender:
    raise HTTPException(400, "That’s your own @handle.")
  hosts = await resolve_handle_hosts(db, owner.id, handle)
  if hosts is None:
    raise HTTPException(
      409, "This Möbius isn’t signed in to a Möbius account, so it can’t find @handles.",
    )
  hosts = [host.lower() for host in hosts if re.fullmatch(HOST_RE, host.lower())][:5]
  if not hosts:
    raise HTTPException(404, f"@{handle} doesn’t have a Möbius online right now.")

  invite = _issue_project_invite(db, project, f"@{handle}", body.role)
  envelope = {"format": FORMAT, "from_handle": sender, "invite_url": invite["join_url"]}
  delivered_to = None
  # A host that may hold the invitation although it never said so: one whose
  # answer was lost, or one still being asked if this request is cancelled.
  unconfirmed = None
  in_flight = None
  refusal = None
  deadline = time.monotonic() + DELIVERY_BUDGET_S
  try:
    for host in hosts:
      # /inbox stores each invitation once, so asking again after a lost
      # answer is safe and settles whether the first delivery landed.
      outcome = None
      for _attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 1:
          break
        in_flight = host
        outcome, detail = await _deliver(host, envelope, min(DELIVERY_TIMEOUT_S, remaining))
        in_flight = None
        if outcome != "unknown":
          break
        unconfirmed = unconfirmed or host
      if outcome == "delivered":
        delivered_to = host
        break
      if outcome == "refused":
        refusal = detail or refusal
  finally:
    if delivered_to is None and unconfirmed is None and in_flight is None:
      # The secret provably never reached anyone else; retire the row now
      # rather than leaving an unusable pending invitation in the panel.
      row = db.get(models.ProjectInvite, invite["id"])
      if row is not None:
        row.revoked_at = now_naive_utc()
        db.commit()
  result = {
    "id": invite["id"],
    "handle": handle,
    "invitee_name": invite["invitee_name"],
    "role": invite["role"],
    "created_at": invite["created_at"],
    "expires_at": invite["expires_at"],
    "delivered_to": delivered_to,
    "delivery": "delivered" if delivered_to else "unconfirmed",
  }
  if delivered_to is None and unconfirmed is not None:
    # It may be waiting in their Projects. It stays among this project's
    # pending invitations, where the owner can revoke it.
    response.status_code = 202
    return result
  if delivered_to is None:
    message = f"Couldn’t deliver the invitation to @{handle}’s Möbius."
    if refusal:
      message += f" It said: {refusal}"
    raise HTTPException(502, message)
  return result


async def _deliver(host: str, envelope: dict[str, str], timeout: float) -> tuple[str, str | None]:
  """POST one delivery: ``delivered``, ``refused``, ``not_sent`` or ``unknown``."""
  try:
    response = await federation_request(
      "POST", f"https://{host}/api/project-invites/inbox",
      json=envelope, timeout_seconds=timeout,
    )
  except NOT_SENT:
    return "not_sent", None
  except PEER_ERRORS:
    return "unknown", None
  if response.status_code == 200:
    return "delivered", None
  if response.status_code >= 500:
    return "unknown", None
  return "refused", _peer_detail(response)


@router.post("/check")
def check_invite(
  body: InviteSecret, response: Response, db: Session = Depends(get_db),
):
  """Describe one live invitation to whoever already holds its secret."""
  now = now_naive_utc()
  invite = db.query(models.ProjectInvite).filter(
    models.ProjectInvite.token_hash == _invite_hash(body.invite),
    models.ProjectInvite.revoked_at.is_(None),
    models.ProjectInvite.consumed_at.is_(None),
    models.ProjectInvite.expires_at > now,
  ).first()
  if invite is None:
    raise HTTPException(410, "This project invitation is invalid or has expired.")
  project = _live_project(db, str(invite.project_id))
  response.headers["Cache-Control"] = "no-store"
  return {
    "format": FORMAT,
    "project": {"name": project.name, "project_type": project.project_type},
    "role": invite.role,
    "expires_at": _iso(invite.expires_at),
    # Whom the invitation was made for: "@handle" for one sent by handle.
    "invitee": invite.invitee_name,
  }


@router.post("/redeem", dependencies=[Depends(reject_cross_site)])
def redeem_handle_invite(body: HandleRedeem, db: Session = Depends(get_db)):
  """Spend a handle invitation once; repeat the answer to whoever spent it.

  The invitee's Möbius saves ``redeem_key`` before its first attempt and
  sends the same key on every retry. The first redemption stores the key's
  digest with the new membership in one transaction. If that answer is lost,
  a retry with the same key gets a fresh pass for the same membership, with
  the same expiry, rather than a 410 for an invitation it already spent.
  """
  now = now_naive_utc()
  key_hash = _invite_hash(body.redeem_key)

  def addressed_invite() -> models.ProjectInvite:
    # Invitations addressed to an @handle; the rest redeem at
    # /api/projects/invites/redeem.
    invite = db.query(models.ProjectInvite).filter(
      models.ProjectInvite.token_hash == _invite_hash(body.invite),
      models.ProjectInvite.revoked_at.is_(None),
    ).first()
    if invite is None or not (invite.invitee_name or "").startswith("@"):
      raise HTTPException(410, GONE)
    return invite

  # Unknown secrets are refused before taking the database write lock.
  addressed_invite()
  db.commit()
  db.execute(text("BEGIN IMMEDIATE"))
  try:
    invite = addressed_invite()
    project = _live_project(db, str(invite.project_id))
    owner = db.query(models.Owner).first()
    if owner is None:
      raise HTTPException(503, "The project owner is unavailable.")
    if invite.consumed_at is None:
      if invite.expires_at <= now:
        raise HTTPException(410, GONE)
      member = models.ProjectMember(
        id=str(uuid.uuid4()), project_id=project.id,
        display_name=invite.invitee_name, role=invite.role,
        token_epoch=0, joined_at=now,
      )
      db.add(member)
      db.flush()
      invite.consumed_at = now
      invite.accepted_member_id = member.id
      db.add(models.ProjectInviteRedemption(
        invite_id=invite.id, key_hash=key_hash, member_id=member.id,
        owner_epoch=owner.token_epoch, redeemed_at=now,
      ))
      redeemed_at = now
    else:
      redemption = db.get(models.ProjectInviteRedemption, invite.id)
      member = db.get(models.ProjectMember, redemption.member_id) if redemption else None
      # A replay only recovers a lost answer: within the invitation's own
      # lifetime, and never across the owner signing out everywhere.
      if (
        redemption is None or member is None or member.revoked_at is not None
        or not hmac.compare_digest(redemption.key_hash, key_hash)
        or redemption.owner_epoch != owner.token_epoch
        or now >= redemption.redeemed_at + _INVITE_TTL
      ):
        raise HTTPException(410, "This project invitation has already been used.")
      redeemed_at = redemption.redeemed_at
    expires_at = redeemed_at + auth.PROJECT_COLLABORATOR_TTL
    if expires_at <= now:
      raise HTTPException(410, GONE)
    # Mint from the epochs checked under the lock, not ones re-read after it.
    token = auth.create_project_collaborator_token(
      owner_username=owner.username, owner_epoch=owner.token_epoch,
      project_id=str(project.id), member_id=member.id,
      member_epoch=member.token_epoch, expires_delta=expires_at - now,
    )
    answer = {
      "access_token": token,
      "token_type": "bearer",
      "project": _project_response(project, []),
      "role": member.role,
      "member_id": member.id,
      "expires_at": expires_at.replace(tzinfo=UTC),
    }
    db.commit()
  except BaseException:
    db.rollback()
    raise
  return answer


# -- receiver --------------------------------------------------------------

def _admit_sender(db: Session, handle: str, now: datetime) -> None:
  pending = db.query(models.ReceivedProjectInvite).filter(
    models.ReceivedProjectInvite.from_handle == handle,
    models.ReceivedProjectInvite.status == "pending",
    models.ReceivedProjectInvite.received_at > now - _INVITE_TTL,
  ).count()
  if pending >= MAX_PENDING_PER_SENDER:
    raise HTTPException(429, f"@{handle} already has {pending} invitations waiting here.")


@router.post("/inbox")
@inbox_limiter.limit("30/minute")
async def receive_handle_invite(
  request: Request,
  body: InboxEnvelope,
  db: Session = Depends(get_db),
):
  handle = clean_handle(body.from_handle)
  if not handle:
    raise HTTPException(422, "The sender’s @handle is not valid.")
  host, secret = parse_invite_url(body.invite_url)
  owner = db.query(models.Owner).first()
  if owner is None:
    raise HTTPException(503, "This Möbius is not set up yet.")
  delivery_key = hashlib.sha256(f"{host}\n{secret}".encode()).hexdigest()
  if db.query(models.ReceivedProjectInvite).filter_by(delivery_key=delivery_key).first():
    return {"status": "received"}
  now = now_naive_utc()
  # Cheap early refusal before any outbound call; the binding check is
  # repeated at admission below.
  _admit_sender(db, handle, now)

  hosts = await resolve_handle_hosts(db, owner.id, handle)
  if hosts is None:
    raise HTTPException(409, "This Möbius can’t check @handles right now.")
  if host not in {str(item).lower() for item in hosts}:
    raise HTTPException(403, f"That invitation didn’t come from @{handle}’s Möbius.")

  try:
    checked = await federation_request(
      "POST", f"https://{host}/api/project-invites/check",
      json={"invite": secret}, timeout_seconds=PEER_TIMEOUT_S,
    )
  except PEER_ERRORS as exc:
    raise HTTPException(422, f"Couldn’t confirm the invitation with @{handle}’s Möbius.") from exc
  if checked.status_code != 200:
    raise HTTPException(422, "That invitation is no longer valid.")
  try:
    details = CheckedInvite.model_validate_json(checked.content)
  except ValidationError as exc:
    raise HTTPException(422, "The invitation details could not be read.") from exc
  expires_at = _naive_utc(details.expires_at)
  if expires_at < now + MIN_INVITE_LIFETIME:
    raise HTTPException(422, "That invitation is about to expire. Ask for a new one.")
  # Only an invitation made for this person may land in their inbox, not any
  # live link or invitation the sender issued.
  try:
    me = await owner_handle(db, owner)
  except HTTPException as exc:
    raise HTTPException(503, "This Möbius can’t check @handles right now.") from exc
  if me is None or details.invitee != f"@{me}":
    raise HTTPException(403, "That invitation was made for someone else.")

  row = models.ReceivedProjectInvite(
    id=str(uuid.uuid4()), delivery_key=delivery_key, from_handle=handle,
    from_host=host, project_name=details.project.name,
    project_type=details.project.project_type, role=details.role,
    invite_secret_sealed=_seal(secret), received_at=now,
    expires_at=min(expires_at, now + _INVITE_TTL), status="pending",
  )
  # Deliveries that passed the early check together may all have been
  # waiting on the directory and /check. Count and insert under the database
  # write lock so the per-sender limit holds however they interleave. Nothing
  # in this section awaits.
  db.commit()
  db.execute(text("BEGIN IMMEDIATE"))
  try:
    if db.query(models.ReceivedProjectInvite).filter_by(delivery_key=delivery_key).first():
      db.rollback()
      return {"status": "received"}
    _admit_sender(db, handle, now)
    db.add(row)
    db.commit()
  except IntegrityError:
    db.rollback()
    return {"status": "received"}
  except BaseException:
    db.rollback()
    raise
  await notify_owner_async(
    db, owner.id,
    title=f"@{handle} invited you to a project",
    body=f"“{row.project_name}” · {row.role.capitalize()}. Open Projects to accept or decline.",
    source_type="system", source_id=row.id,
    target="/shell/?projects=1", tag=f"project-invite:{row.id}",
  )
  return {"status": "received"}


@router.get("")
def list_received_invites(
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  now = now_naive_utc()
  rows = db.query(models.ReceivedProjectInvite).filter(
    models.ReceivedProjectInvite.status.in_(("pending", "accepted")),
  ).order_by(models.ReceivedProjectInvite.received_at.desc()).all()
  return [_view(row, now) for row in rows]


@router.post("/{invite_id}/accept", dependencies=[Depends(reject_cross_site)])
async def accept_received_invite(
  invite_id: str,
  _: models.Owner = Depends(get_current_owner_for_lifecycle_control),
  db: Session = Depends(get_db),
):
  row = _row(db, invite_id)
  now = now_naive_utc()
  if row.status == "accepted":
    return {**_view(row, now), "open_url": _open_url(row)}
  if row.status != "pending":
    raise HTTPException(409, "This invitation was declined.")
  secret = _unseal(row.invite_secret_sealed)
  # After an attempt that may have reached the sender, only the sender can
  # say whether the invitation was spent, so local expiry no longer decides.
  if secret is None or (row.expires_at <= now and row.redeem_key_sealed is None):
    raise HTTPException(410, "This invitation has expired. Ask for a new one.")
  if row.redeem_key_sealed is None:
    # One key per invitation, saved before the first attempt: a concurrent
    # accept must reuse it, never replace it.
    db.query(models.ReceivedProjectInvite).filter(
      models.ReceivedProjectInvite.id == row.id,
      models.ReceivedProjectInvite.redeem_key_sealed.is_(None),
    ).update({
      models.ReceivedProjectInvite.redeem_key_sealed: _seal(secrets.token_urlsafe(32)),
    }, synchronize_session=False)
    db.commit()
    db.refresh(row)
  redeem_key = _unseal(row.redeem_key_sealed)
  if redeem_key is None:
    raise HTTPException(410, "This invitation has expired. Ask for a new one.")
  try:
    response = await federation_request(
      "POST", f"https://{row.from_host}/api/project-invites/redeem",
      json={"invite": secret, "redeem_key": redeem_key},
      timeout_seconds=PEER_TIMEOUT_S,
    )
  except PEER_ERRORS as exc:
    # The sender may have spent it; the saved key makes trying again safe.
    raise HTTPException(502, f"Couldn’t reach @{row.from_handle}’s Möbius. Try again soon.") from exc
  db.expire_all()
  row = _row(db, invite_id)
  if row.status == "accepted":
    return {**_view(row, now), "open_url": _open_url(row)}
  if response.status_code == 410:
    row.expires_at = now
    row.invite_secret_sealed = None
    row.redeem_key_sealed = None
    db.commit()
    raise HTTPException(410, "This invitation was withdrawn or already used. Ask for a new one.")
  if response.status_code != 200:
    raise HTTPException(502, _peer_detail(response) or "The invitation couldn’t be accepted.")
  try:
    access = RedeemedAccess.model_validate_json(response.content)
  except ValidationError as exc:
    raise HTTPException(502, "The shared project returned an unexpected answer.") from exc
  if access.project.name:
    row.project_name = access.project.name
  row.status = "accepted"
  row.responded_at = now
  row.remote_project_id = access.project.id
  row.access_token_sealed = _seal(access.access_token)
  row.access_expires_at = _naive_utc(access.expires_at)
  row.invite_secret_sealed = None
  row.redeem_key_sealed = None
  db.commit()
  return {**_view(row, now), "open_url": _open_url(row)}


def _open_url(row: models.ReceivedProjectInvite) -> str:
  token = _unseal(row.access_token_sealed)
  if row.status != "accepted" or not token or not row.remote_project_id:
    raise HTTPException(409, "Accept this invitation first.")
  if row.access_expires_at is not None and row.access_expires_at <= now_naive_utc():
    raise HTTPException(410, f"Your access expired. Ask @{row.from_handle} to invite you again.")
  # The token rides in the fragment: it never reaches the host's access logs
  # or a Referer, and the shared workspace moves it into tab storage at once.
  return f"https://{row.from_host}/shared/project/{row.remote_project_id}#access={token}"


@router.post("/{invite_id}/open", dependencies=[Depends(reject_cross_site)])
def open_received_project(
  invite_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  return {"url": _open_url(_row(db, invite_id))}


@router.post("/{invite_id}/decline", dependencies=[Depends(reject_cross_site)])
def decline_received_invite(
  invite_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  row = _row(db, invite_id)
  if row.status != "pending":
    raise HTTPException(409, "Only a waiting invitation can be declined.")
  row.status = "declined"
  row.responded_at = now_naive_utc()
  row.invite_secret_sealed = None
  row.redeem_key_sealed = None
  db.commit()
  return _view(row, now_naive_utc())


@router.delete("/{invite_id}", status_code=204, dependencies=[Depends(reject_cross_site)])
def remove_received_invite(
  invite_id: str,
  _: models.Owner = Depends(get_current_owner),
  db: Session = Depends(get_db),
):
  db.delete(_row(db, invite_id))
  db.commit()
  return Response(status_code=204)
