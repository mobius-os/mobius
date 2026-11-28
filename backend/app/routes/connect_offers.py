"""Offer Connect access to someone by their Möbius @handle.

The paste-a-command flow asks the person who will gain access to create a
pairing command on their Möbius and send it over.  A handle offer runs the
same pairing in the other direction of travel, without anyone copying text:

Granter:   ``POST /api/connect/offers`` resolves the @handle to its
           deployments through the account directory and delivers
           ``{from_handle, offer_url}`` to that deployment's ``/inbox``.
Recipient: ``/inbox`` trusts nothing in the envelope.  It resolves the claimed
           sender handle itself, requires the offer URL's host to be one of
           that handle's deployments, and asks that host (``/check``) to
           confirm the secret names a live offer made for this owner's handle.
           Only then does it store the offer and notify its owner.
Accepting: the recipient creates an ordinary unpaired machine entry and sends
           its one-time pairing code to the granter (``/redeem``).  The granter
           pairs only with a host that the account directory lists for the
           invitee's handle, then runs the same outbound runner a pasted command
           would.  The result is the same revocable "Can control this Möbius"
           entry, and the same "Machines you control" entry on the other side.

A leaked offer secret cannot point the granter anywhere else: redemption
pairs only with the invitee's own deployments.  A retried accept reuses the
same machine entry, and a paired entry proves an earlier answer was lost.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from slowapi import Limiter
from sqlalchemy.orm import Session

from app import connect_outbound, models
from app.config import get_settings
from app.database import get_db
from app.deps import (
  get_owner_or_app_with_connect_manage, reject_cross_site,
  require_nondelegated_owner_or_app_control,
)
from app.federation_transport import FederationTransportError, federation_request
from app.push import notify_owner_async
from app.routes import connect as connect_routes
from app.routes.identity import _HANDLE_RE, owner_handle, resolve_handle_hosts
from app.storage_io import atomic_write

router = APIRouter(
  prefix="/api/connect/offers",
  tags=["connect"],
  dependencies=[Depends(reject_cross_site)],
)

FORMAT = "mobius-connect-offer-v1"
OFFER_PATH = "/connect-offer"
SECRET_RE = r"^[A-Za-z0-9_-]{43}$"
HOST_RE = r"^[a-z0-9]([a-z0-9.-]{0,250})(:\d{1,5})?$"
ID_RE = re.compile(r"^co_[a-f0-9]{16}$")
OFFER_TTL_SECONDS = 7 * 24 * 3600
MIN_OFFER_LIFETIME_SECONDS = 3600
MAX_PENDING_PER_SENDER = 5
DELIVERY_TIMEOUT_S = 30.0
DELIVERY_BUDGET_S = 45.0
PEER_TIMEOUT_S = 12.0
# Redeeming downloads the recipient's runner and waits for it to pair.
REDEEM_TIMEOUT_S = 90.0
PEER_ERRORS = (FederationTransportError, ValueError, httpx.HTTPError, HTTPException)
NOT_SENT = (ValueError, HTTPException, httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
GONE = "This access offer was withdrawn, already used, or has expired."
OWNER_DEPS = [Depends(require_nondelegated_owner_or_app_control)]

inbox_limiter = Limiter(key_func=lambda request: "connect-offer-inbox")
# Peer calls arrive through this instance's own proxy, so there is no
# per-caller address to key on; one bucket each bounds the work strangers cause.
peer_limiter = Limiter(key_func=lambda request: "connect-offer-check")
redeem_limiter = Limiter(key_func=lambda request: "connect-offer-redeem")
# Guards short read-modify-write steps on offer files; never held across a
# network call or a pairing.
_state_lock = asyncio.Lock()
# Offers this process is redeeming (granter) or accepting (recipient) now.
_redeeming: set[str] = set()
_accepting: set[str] = set()


class StrictBody(BaseModel):
  model_config = ConfigDict(extra="forbid")


class OfferSend(StrictBody):
  handle: str = Field(min_length=1, max_length=40)
  agent: bool = False


class OfferSecret(StrictBody):
  offer: str = Field(pattern=SECRET_RE)


class OfferRedeem(StrictBody):
  offer: str = Field(pattern=SECRET_RE)
  host: str = Field(min_length=1, max_length=256)
  pairing_code: str = Field(min_length=8, max_length=16)


class InboxEnvelope(StrictBody):
  format: Literal["mobius-connect-offer-v1"]
  from_handle: str = Field(min_length=1, max_length=40)
  offer_url: str = Field(min_length=1, max_length=512)


class CheckedOffer(BaseModel):
  model_config = ConfigDict(extra="ignore")
  format: Literal["mobius-connect-offer-v1"]
  invitee: str
  agent: bool = False
  expires_at: float


# -- storage ---------------------------------------------------------------

def _dir(kind: Literal["sent", "received"]) -> Path:
  path = Path(get_settings().data_dir) / "shared" / "connect" / "offers" / kind
  path.mkdir(parents=True, exist_ok=True, mode=0o700)
  return path


def _path(kind: Literal["sent", "received"], offer_id: str) -> Path:
  if not ID_RE.fullmatch(offer_id or ""):
    raise HTTPException(404, "No such access offer.")
  return _dir(kind) / f"{offer_id}.json"


def _load(kind: Literal["sent", "received"], offer_id: str) -> dict | None:
  try:
    return json.loads(_path(kind, offer_id).read_text("utf-8"))
  except (OSError, ValueError):
    return None


def _save(kind: Literal["sent", "received"], record: dict) -> None:
  atomic_write(_path(kind, record["id"]), json.dumps(record, indent=2), mode=0o600)


def _delete(kind: Literal["sent", "received"], offer_id: str) -> None:
  _path(kind, offer_id).unlink(missing_ok=True)


def _all(kind: Literal["sent", "received"]) -> list[dict]:
  records = []
  for path in sorted(_dir(kind).glob("co_*.json")):
    if not ID_RE.fullmatch(path.stem):
      continue
    try:
      records.append(json.loads(path.read_text("utf-8")))
    except (OSError, ValueError):
      continue
  return sorted(records, key=lambda item: float(item.get("created_at") or 0), reverse=True)


def _new_id() -> str:
  return "co_" + secrets.token_hex(8)


def _digest(value: str) -> str:
  return hashlib.sha256(value.encode()).hexdigest()


def _fernet() -> Fernet:
  material = f"mobius-received-connect-offer-v1:{get_settings().secret_key}".encode()
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

def clean_handle(value: str) -> str:
  text = str(value or "").strip().lower().lstrip("@")
  return text if _HANDLE_RE.fullmatch(text) else ""


def _own_host() -> str:
  return (urlsplit(get_settings().frontend_origin).netloc or "").lower()


def parse_offer_url(url: str) -> tuple[str, str]:
  try:
    parsed = urlsplit(url)
    host = (parsed.netloc or "").lower()
  except ValueError as exc:
    raise HTTPException(422, "That is not a Connect access offer.") from exc
  if (
    parsed.scheme != "https" or parsed.path != OFFER_PATH or parsed.query
    or parsed.username or parsed.password
    or not re.fullmatch(HOST_RE, host)
    or not re.fullmatch(SECRET_RE, parsed.fragment or "")
  ):
    raise HTTPException(422, "That is not a Connect access offer.")
  return host, parsed.fragment


def _peer_detail(response: httpx.Response) -> str | None:
  try:
    detail = response.json().get("detail")
  except (ValueError, AttributeError):
    return None
  return detail[:300] if isinstance(detail, str) else None


async def _handle_hosts(db: Session, owner: models.Owner, handle: str) -> list[str]:
  hosts = await resolve_handle_hosts(db, owner.id, handle)
  if hosts is None:
    raise HTTPException(
      409, "This Möbius isn’t signed in to a Möbius account, so it can’t find @handles.",
    )
  return [h.lower() for h in hosts if re.fullmatch(HOST_RE, str(h).lower())][:5]


def _sent_view(record: dict, now: float) -> dict:
  status = record.get("status") or "pending"
  if status == "pending" and float(record.get("expires_at") or 0) <= now:
    status = "expired"
  return {
    "id": record["id"],
    "handle": record.get("handle"),
    "agent": record.get("agent") is True,
    "status": status,
    "delivered_to": record.get("delivered_to"),
    "created_at": record.get("created_at"),
    "expires_at": record.get("expires_at"),
  }


def _received_view(record: dict, now: float) -> dict:
  status = record.get("status") or "pending"
  if status == "pending" and float(record.get("expires_at") or 0) <= now:
    status = "expired"
  return {
    "id": record["id"],
    "from_handle": record.get("from_handle"),
    "from_host": record.get("from_host"),
    "agent": record.get("agent") is True,
    "status": status,
    "received_at": record.get("received_at"),
    "expires_at": record.get("expires_at"),
  }


def _live_sent(secret: str) -> dict | None:
  digest = _digest(secret)
  for record in _all("sent"):
    if hmac.compare_digest(str(record.get("secret_sha256") or ""), digest):
      return record
  return None


def _connect_app_target(db: Session) -> str | None:
  app_id = db.query(models.App.id).filter(models.App.slug == "connect").scalar()
  return f"/shell/?app={app_id}" if app_id else None


# -- granter ---------------------------------------------------------------

@router.post("", dependencies=OWNER_DEPS)
async def send_offer(
  body: OfferSend,
  response: Response,
  owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
  db: Session = Depends(get_db),
):
  """Offer @handle control of this Möbius; they accept in their Connect."""
  handle = clean_handle(body.handle)
  if not handle:
    raise HTTPException(400, "Enter a Möbius @handle, like @alex.")
  sender = await owner_handle(db, owner)
  if not sender:
    raise HTTPException(
      409, "Choose your @handle in Möbius · You before offering access by handle.",
    )
  if handle == sender:
    raise HTTPException(400, "That’s your own @handle.")
  hosts = await _handle_hosts(db, owner, handle)
  if not hosts:
    raise HTTPException(404, f"@{handle} doesn’t have a Möbius online right now.")

  now = time.time()
  secret = secrets.token_urlsafe(32)
  record = {
    "id": _new_id(),
    "handle": handle,
    "agent": body.agent,
    "secret_sha256": _digest(secret),
    "status": "pending",
    "created_at": now,
    "expires_at": now + OFFER_TTL_SECONDS,
    "delivered_to": None,
  }
  _save("sent", record)
  envelope = {
    "format": FORMAT,
    "from_handle": sender,
    "offer_url": f"https://{_own_host()}{OFFER_PATH}#{secret}",
  }
  delivered_to = unconfirmed = in_flight = refusal = None
  deadline = time.monotonic() + DELIVERY_BUDGET_S
  try:
    for host in hosts:
      outcome = None
      # The inbox stores each offer once, so asking again settles a lost answer.
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
      # Provably never reached anyone: nothing to keep or revoke.
      _delete("sent", record["id"])
  if delivered_to is not None:
    # A withdrawal or an accept may have landed while delivery was in flight;
    # record the delivery on the current state rather than restoring the old one.
    async with _state_lock:
      current = _load("sent", record["id"])
      if current is not None:
        current["delivered_to"] = delivered_to
        _save("sent", current)
    return _sent_view(current or {**record, "status": "withdrawn"}, now)
  if unconfirmed is not None:
    # It may be waiting in their Connect; it stays listed so it can be withdrawn.
    response.status_code = 202
    return _sent_view(record, now)
  message = f"Couldn’t deliver the offer to @{handle}’s Möbius."
  if refusal:
    message += f" It said: {refusal}"
  raise HTTPException(502, message)


async def _deliver(host: str, envelope: dict, timeout: float) -> tuple[str, str | None]:
  try:
    response = await federation_request(
      "POST", f"https://{host}/api/connect/offers/inbox",
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
  if response.status_code == 404:
    return "refused", "Their Möbius doesn’t support access offers yet. Ask them to update it."
  return "refused", _peer_detail(response)


@router.delete("/sent/{offer_id}", dependencies=OWNER_DEPS)
async def withdraw_offer(
  offer_id: str,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
):
  async with _state_lock:
    record = _load("sent", offer_id)
    if record is None:
      raise HTTPException(404, "No such access offer.")
    if offer_id in _redeeming:
      # The redemption in progress sees this and undoes its pairing.
      record["status"] = "withdrawn"
      _save("sent", record)
      return {"ok": True}
    stale = record.get("status") == "redeeming" and record.get("profile_id")
    _delete("sent", offer_id)
  if stale and connect_outbound.profile_status(record["profile_id"]) is not None:
    # A redemption interrupted by a restart may have paired before it stopped.
    await _revoke_quietly(record["profile_id"])
  # A connected offer is only history; its access lives on in the outbound list.
  return {"ok": True}


async def _revoke_quietly(profile_id: str) -> None:
  try:
    await connect_outbound.revoke_profile(profile_id)
  except LookupError:
    pass


@router.post("/check")
@peer_limiter.limit("60/minute")
def check_offer(request: Request, body: OfferSecret, response: Response):
  """Describe one live offer to whoever already holds its secret."""
  record = _live_sent(body.offer)
  if (
    record is None or record.get("status") != "pending"
    or float(record.get("expires_at") or 0) <= time.time()
  ):
    raise HTTPException(410, GONE)
  response.headers["Cache-Control"] = "no-store"
  return {
    "format": FORMAT,
    "invitee": f"@{record['handle']}",
    "agent": record.get("agent") is True,
    "expires_at": record["expires_at"],
  }


def _same_code(record: dict, code: str) -> bool:
  return hmac.compare_digest(str(record.get("pairing_code_sha256") or ""), _digest(code))


@router.post("/redeem")
@redeem_limiter.limit("10/minute")
async def redeem_offer(request: Request, body: OfferRedeem, db: Session = Depends(get_db)):
  """Pair with the invitee's Möbius using the code it sent, once.

  The offer is marked ``redeeming`` with the profile id it will create before
  pairing starts, so a withdrawal during pairing undoes it and a retry after
  a restart can recognise a pairing that already happened.
  """
  host = body.host.strip().lower()
  code = connect_routes._normalize_code(body.pairing_code)
  if code is None or not re.fullmatch(HOST_RE, host):
    raise HTTPException(422, "That pairing request is not valid.")
  stale_profile = None
  async with _state_lock:
    record = _live_sent(body.offer)
    if record is None:
      raise HTTPException(410, GONE)
    offer_id = record["id"]
    if record.get("status") == "connected":
      # A lost answer: the same code proves it is the same accept.
      if _same_code(record, code):
        return {"status": "connected"}
      raise HTTPException(410, GONE)
    if offer_id in _redeeming:
      raise HTTPException(409, "This offer is already being accepted.")
    if record.get("status") == "redeeming":
      # Interrupted by a restart: finish it if pairing had succeeded, even
      # when the runner's sign-in was saved but the profile not yet marked.
      if _same_code(record, code) and await connect_outbound.complete_interrupted_pairing(
        record.get("profile_id"),
      ):
        record.update({"status": "connected", "connected_at": time.time()})
        _save("sent", record)
        return {"status": "connected"}
      if connect_outbound.profile_status(record.get("profile_id")) is not None:
        stale_profile = record["profile_id"]
      record["status"] = "pending"
      _save("sent", record)
    if record.get("status") != "pending" or float(record.get("expires_at") or 0) <= time.time():
      raise HTTPException(410, GONE)
    _redeeming.add(offer_id)
  try:
    if stale_profile:
      # Its pairing never confirmed; that half-made access must not linger.
      await _revoke_quietly(stale_profile)
    owner = db.query(models.Owner).first()
    if owner is None:
      raise HTTPException(503, "This Möbius is not set up yet.")
    # Only the invitee's own Möbius may receive access, whoever holds the secret.
    try:
      hosts = await _handle_hosts(db, owner, record["handle"])
    except HTTPException as exc:
      raise HTTPException(503, "This Möbius can’t check @handles right now.") from exc
    if host not in hosts:
      raise HTTPException(403, f"That Möbius doesn’t belong to @{record['handle']}.")
    base_url = f"https://{host}"
    try:
      # The runner comes through the SSRF-safe transport, which also refuses
      # a host that resolves to a private or local address.
      fetched = await federation_request(
        "GET", base_url + "/api/connect/runner", response_format="binary",
        max_response_bytes=connect_outbound._MAX_RUNNER_BYTES, timeout_seconds=30.0,
      )
    except PEER_ERRORS as exc:
      raise HTTPException(502, f"Couldn’t reach @{record['handle']}’s Möbius.") from exc
    if fetched.status_code != 200:
      raise HTTPException(502, f"@{record['handle']}’s Möbius didn’t provide its Connect runner.")
    profile_id = connect_outbound.new_profile_id()
    async with _state_lock:
      current = _load("sent", offer_id)
      if current is None or current.get("status") != "pending":
        raise HTTPException(410, GONE)
      current.update({
        "status": "redeeming", "profile_id": profile_id, "pairing_code_sha256": _digest(code),
      })
      _save("sent", current)
    try:
      await connect_outbound.create_paired_profile(
        f"@{record['handle']}", base_url, code, agent=record.get("agent") is True,
        source=fetched.content, profile_id=profile_id,
      )
    except connect_outbound.OutboundConnectError as exc:
      async with _state_lock:
        current = _load("sent", offer_id)
        if current is not None and current.get("status") == "redeeming":
          current["status"] = "pending"
          _save("sent", current)
      raise HTTPException(502, str(exc)) from exc
    async with _state_lock:
      current = _load("sent", offer_id)
      withdrawn = current is None or current.get("status") == "withdrawn"
      if withdrawn:
        _delete("sent", offer_id)
      else:
        current.update({"status": "connected", "connected_at": time.time()})
        _save("sent", current)
    if withdrawn:
      await _revoke_quietly(profile_id)
      raise HTTPException(410, GONE)
  finally:
    _redeeming.discard(offer_id)
  await notify_owner_async(
    db, owner.id,
    title=f"@{record['handle']} can now control this Möbius",
    body="They accepted your Connect access offer. Revoke it any time in Connect.",
    source_type="system", source_id=offer_id,
    target=_connect_app_target(db), tag=f"connect-offer:{offer_id}",
  )
  return {"status": "connected"}


# -- recipient -------------------------------------------------------------

def _admit_sender(handle: str, now: float) -> None:
  pending = sum(
    1 for item in _all("received")
    if item.get("from_handle") == handle and item.get("status") == "pending"
    and float(item.get("expires_at") or 0) > now
  )
  if pending >= MAX_PENDING_PER_SENDER:
    raise HTTPException(429, f"@{handle} already has {pending} access offers waiting here.")


@router.post("/inbox")
@inbox_limiter.limit("30/minute")
async def receive_offer(request: Request, body: InboxEnvelope, db: Session = Depends(get_db)):
  handle = clean_handle(body.from_handle)
  if not handle:
    raise HTTPException(422, "The sender’s @handle is not valid.")
  host, secret = parse_offer_url(body.offer_url)
  owner = db.query(models.Owner).first()
  if owner is None:
    raise HTTPException(503, "This Möbius is not set up yet.")
  delivery_key = _digest(f"{host}\n{secret}")
  if any(item.get("delivery_key") == delivery_key for item in _all("received")):
    return {"status": "received"}
  now = time.time()
  _admit_sender(handle, now)

  try:
    hosts = await _handle_hosts(db, owner, handle)
  except HTTPException as exc:
    raise HTTPException(409, "This Möbius can’t check @handles right now.") from exc
  if host not in hosts:
    raise HTTPException(403, f"That offer didn’t come from @{handle}’s Möbius.")
  try:
    checked = await federation_request(
      "POST", f"https://{host}/api/connect/offers/check",
      json={"offer": secret}, timeout_seconds=PEER_TIMEOUT_S,
    )
  except PEER_ERRORS as exc:
    raise HTTPException(422, f"Couldn’t confirm the offer with @{handle}’s Möbius.") from exc
  if checked.status_code != 200:
    raise HTTPException(422, "That offer is no longer valid.")
  try:
    details = CheckedOffer.model_validate_json(checked.content)
  except ValidationError as exc:
    raise HTTPException(422, "The offer details could not be read.") from exc
  if details.expires_at < now + MIN_OFFER_LIFETIME_SECONDS:
    raise HTTPException(422, "That offer is about to expire. Ask for a new one.")
  try:
    me = await owner_handle(db, owner)
  except HTTPException as exc:
    raise HTTPException(503, "This Möbius can’t check @handles right now.") from exc
  if me is None or details.invitee != f"@{me}":
    raise HTTPException(403, "That offer was made for someone else.")

  record = {
    "id": _new_id(),
    "delivery_key": delivery_key,
    "from_handle": handle,
    "from_host": host,
    "agent": details.agent,
    "secret_sealed": _seal(secret),
    "status": "pending",
    "created_at": now,
    "received_at": now,
    "expires_at": min(details.expires_at, now + OFFER_TTL_SECONDS),
    "host_id": None,
  }
  # Concurrent deliveries of one offer, or past the sender's limit, both
  # passed the early checks; only one of them may store it.
  async with _state_lock:
    if any(item.get("delivery_key") == delivery_key for item in _all("received")):
      return {"status": "received"}
    _admit_sender(handle, now)
    _save("received", record)
  access = "full access" if details.agent else "command access"
  await notify_owner_async(
    db, owner.id,
    title=f"@{handle} offered you access to their Möbius",
    body=f"Accept in Connect to add it to your machines ({access}).",
    source_type="system", source_id=record["id"],
    target=_connect_app_target(db), tag=f"connect-offer:{record['id']}",
  )
  return {"status": "received"}


@router.get("")
async def list_offers(_owner: models.Owner = Depends(get_owner_or_app_with_connect_manage)):
  now = time.time()
  return {
    "sent": [_sent_view(item, now) for item in _all("sent") if item.get("status") == "pending"],
    "received": [
      _received_view(item, now) for item in _all("received")
      if item.get("status") == "pending"
    ],
  }


@router.post("/received/{offer_id}/accept", dependencies=OWNER_DEPS)
async def accept_offer(
  offer_id: str,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
):
  async with _state_lock:
    record = _load("received", offer_id)
    if record is None:
      raise HTTPException(404, "No such access offer.")
    if offer_id in _accepting:
      raise HTTPException(409, "This offer is already being accepted.")
    _accepting.add(offer_id)
  try:
    return await _accept(record)
  finally:
    _accepting.discard(offer_id)


async def _accept(record: dict) -> dict:
  now = time.time()
  if record.get("status") == "accepted":
    return _received_view(record, now)
  if record.get("status") != "pending":
    raise HTTPException(409, "This offer was declined.")
  secret = _unseal(record.get("secret_sealed"))
  host = connect_routes._load_host(record["host_id"]) if record.get("host_id") else None
  if host is not None and host.get("token_sha256"):
    # An earlier accept paired but its answer was lost.
    return _finish_accept(record, now)
  if secret is None or (host is None and float(record.get("expires_at") or 0) <= now):
    raise HTTPException(410, "This offer has expired. Ask for a new one.")
  if host is None:
    # Saved before the first attempt so a retry reuses this machine entry.
    host = _new_host(f"@{record['from_handle']}’s Möbius")
    record["host_id"] = host["id"]
    _save("received", record)
  # Keeps the entry's existing code, so a retry sends the same one.
  code = connect_routes._issue_pairing(host)["pairing_code"]
  try:
    response = await federation_request(
      "POST", f"https://{record['from_host']}/api/connect/offers/redeem",
      json={"offer": secret, "host": _own_host(), "pairing_code": code},
      timeout_seconds=REDEEM_TIMEOUT_S,
    )
  except PEER_ERRORS as exc:
    host = connect_routes._load_host(record["host_id"])
    if host is not None and host.get("token_sha256"):
      # Paired, but the answer was lost on the way back.
      return _finish_accept(record, now)
    raise HTTPException(
      502, f"Couldn’t reach @{record['from_handle']}’s Möbius. Try again soon.",
    ) from exc
  if response.status_code == 200:
    return _finish_accept(record, now)
  host = connect_routes._load_host(record["host_id"])
  if host is not None and host.get("token_sha256"):
    return _finish_accept(record, now)
  if response.status_code == 410:
    if host is not None:
      connect_routes._forget_host(host["id"])
    record.update({"status": "expired", "secret_sealed": None, "host_id": None})
    _save("received", record)
    raise HTTPException(410, "This offer was withdrawn or already used. Ask for a new one.")
  raise HTTPException(502, _peer_detail(response) or "The offer couldn’t be accepted.")


def _new_host(name: str) -> dict:
  host = {
    "id": connect_routes._new_id(),
    "name": name,
    "created_at": connect_routes._now(),
    "token_sha256": None,
    "paired_at": None,
    "last_seen": None,
    "platform": None,
    "runner_protocol": None,
    "runner_release": None,
    "active_commands": [],
  }
  connect_routes._issue_pairing(host)
  return host


def _finish_accept(record: dict, now: float) -> dict:
  record.update({"status": "accepted", "secret_sealed": None, "responded_at": now})
  _save("received", record)
  return _received_view(record, now)


@router.post("/received/{offer_id}/decline", dependencies=OWNER_DEPS)
async def decline_offer(
  offer_id: str,
  _owner: models.Owner = Depends(get_owner_or_app_with_connect_manage),
):
  async with _state_lock:
    record = _load("received", offer_id)
    if record is None:
      raise HTTPException(404, "No such access offer.")
    if offer_id in _accepting:
      raise HTTPException(409, "This offer is being accepted right now.")
    if record.get("status") == "accepted":
      raise HTTPException(409, "This offer was already accepted. Remove the machine instead.")
    host_id = record.get("host_id")
    host = connect_routes._load_host(host_id) if host_id else None
    if host is not None and host.get("token_sha256"):
      # An earlier accept paired although its answer was lost: it is accepted.
      _finish_accept(record, time.time())
      raise HTTPException(409, "This offer was already accepted. Remove the machine instead.")
    if host is not None:
      connect_routes._forget_host(host_id)
    _delete("received", offer_id)
  return {"ok": True}
