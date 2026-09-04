"""Common federated shared objects (protocol common/0 extension).

A *shared object* is one JSON document owned by an app (a kanban board, a
shared list, a group-chat roster) that lives on the instance of whoever
created it and is edited together by invited peers. This generalizes the
Common federation layer from messages to collaborative state, so ANY app can
be collaborative by pointing at this one surface — no app ships its own sync
backend.

Design, deliberately mirroring the proven pieces of the platform:

- **Identity and trust come from Common.** Members are instances (their
  federation host); every peer request is an Ed25519-signed envelope verified
  against the sender's cached actor card, exactly like a DM. The document's
  home is its creator's instance; each member instance talks to that host.
- **Writes are compare-and-swap, reads are version-gated** — the same
  semantics as app storage `If-Match` and Shared Apps state, so concurrent
  editors merge instead of clobbering. The platform treats the document as
  opaque; apps own their schema (and its forward/backward tolerance).
- **Invites are capability strings** (`<object>@<host>#<secret>`), created by
  the host owner with a role, redeemable once each until revoked or expired.
  Possession of the secret plus a verifiable instance signature admits the
  member; the host can revoke any member later.

Peer surface (public; envelope signatures are the authority):
  POST /api/common/objects/{oid}/peer   join / state / write / leave envelopes

Owner surface (owner JWT, or an app's scoped token for its OWN objects):
  POST   /api/common/objects                      create a hosted object
  GET    /api/common/objects?app=                 list hosted + joined objects
  POST   /api/common/objects/join                 redeem an invite string
  POST   /api/common/objects/{oid}/invites        mint an invite (host only)
  GET    /api/common/objects/{host}/{oid}/state   read (local or proxied)
  PUT    /api/common/objects/{host}/{oid}/state   CAS write (local or proxied)
  GET    /api/common/objects/{oid}/members        membership view (host only)
  DELETE /api/common/objects/{oid}/members/{mhost} revoke a member (host only)
  POST   /api/common/objects/{host}/{oid}/leave   leave a joined object
  DELETE /api/common/objects/{oid}                delete a hosted object

Server state lives under `<data_dir>/common/objects/`. Apps read and write
through their own instance; the instance signs and forwards to the host when
the object lives elsewhere. An app token may only touch objects whose `app`
matches its own slug — an app cannot reach another app's shared state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets as pysecrets
import time
import uuid
from pathlib import Path
from weakref import WeakValueDictionary

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import models, push
from app.config import get_settings
from app.database import get_db
from app.deps import Principal, get_principal
from app.storage_io import atomic_write, read_capped_body
from app.routes.common import (
  CLOCK_SKEW_S,
  OUTBOUND_TIMEOUT_S,
  PROTOCOL,
  _load_identity,
  _own_host,
  _peer_base_url,
  _sign,
  _valid_host,
  _verify_peer_envelope,
)

router = APIRouter(prefix="/api/common/objects", tags=["common-objects"])

MAX_DOC_BYTES = 256 * 1024
MAX_ENVELOPE_BYTES = MAX_DOC_BYTES + 8 * 1024
MAX_KIND_CHARS = 40
MAX_LABEL_CHARS = 120
INVITE_TTL_S = 7 * 24 * 3600
PRESENCE_TTL_S = 12
PRESENCE_RETENTION_S = 5 * 60
ROLES = ("editor", "viewer")

_OID_RE = re.compile(r"^[a-f0-9]{32}$")
_APP_SLUG_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,62})$")
_INVITE_RE = re.compile(
  r"^(?P<oid>[a-f0-9]{32})@(?P<host>[a-z0-9]([a-z0-9.-]{0,250})(:\d{1,5})?)"
  r"#(?P<secret>[A-Za-z0-9_-]{16,64})$"
)

_object_locks: "WeakValueDictionary[str, asyncio.Lock]" = WeakValueDictionary()
_object_presence: dict[tuple[str, str], float] = {}


def _object_lock(oid: str) -> asyncio.Lock:
  lock = _object_locks.get(oid)
  if lock is None:
    lock = asyncio.Lock()
    _object_locks[oid] = lock
  return lock


def _mark_present(oid: str, host: str, now: float | None = None) -> None:
  """Record ephemeral board presence without turning heartbeats into disk I/O."""
  observed_at = time.time() if now is None else now
  _object_presence[(oid, host)] = observed_at
  if len(_object_presence) > 2048:
    cutoff = observed_at - PRESENCE_RETENTION_S
    for key, seen_at in list(_object_presence.items()):
      if seen_at < cutoff:
        _object_presence.pop(key, None)


def _member_is_active(oid: str, host: str, now: float | None = None) -> bool:
  observed_at = _object_presence.get((oid, host))
  if observed_at is None:
    return False
  current = time.time() if now is None else now
  return observed_at >= current - PRESENCE_TTL_S


# ── paths ────────────────────────────────────────────────────────────────────

def _objects_dir() -> Path:
  d = Path(get_settings().data_dir) / "common" / "objects"
  d.mkdir(parents=True, exist_ok=True)
  return d


def _hosted_dir(oid: str) -> Path:
  return _objects_dir() / "hosted" / oid


def _remote_dir() -> Path:
  d = _objects_dir() / "remote"
  d.mkdir(parents=True, exist_ok=True)
  return d


def _remote_path(host: str, oid: str) -> Path:
  safe = re.sub(r"[^a-z0-9.-]", "_", host)
  return _remote_dir() / f"{safe}__{oid}.json"


# ── hosted object records ───────────────────────────────────────────────────

def _load_object(oid: str) -> dict | None:
  path = _hosted_dir(oid) / "object.json"
  if not path.is_file():
    return None
  return json.loads(path.read_text())


def _save_object(obj: dict) -> None:
  d = _hosted_dir(obj["id"])
  d.mkdir(parents=True, exist_ok=True)
  atomic_write(d / "object.json", json.dumps(obj, indent=2))


def _load_doc(oid: str) -> dict | list | None:
  path = _hosted_dir(oid) / "doc.json"
  if not path.is_file():
    return None
  return json.loads(path.read_text())


def _save_doc(oid: str, doc) -> None:
  d = _hosted_dir(oid)
  d.mkdir(parents=True, exist_ok=True)
  atomic_write(d / "doc.json", json.dumps(doc))


def _encode_doc(doc) -> bytes:
  encoded = json.dumps(doc).encode()
  if len(encoded) > MAX_DOC_BYTES:
    raise HTTPException(status_code=413, detail="Document is too large.")
  return encoded


def _hash_secret(secret: str) -> str:
  return hashlib.sha256(secret.encode()).hexdigest()


def _prune_invites(obj: dict) -> None:
  now = time.time()
  obj["invites"] = {
    h: inv for h, inv in (obj.get("invites") or {}).items()
    if inv.get("expires_at", 0) > now
  }


def _public_object(obj: dict) -> dict:
  """The object metadata a member may see (no invite secrets)."""
  return {
    "id": obj["id"],
    "app": obj["app"],
    "kind": obj.get("kind") or "",
    "label": obj.get("label") or "",
    "host": _own_host(),
    "version": obj["version"],
    "created_at": obj["created_at"],
    "members": {
      h: {
        "role": m.get("role"),
        "name": m.get("name") or "",
        "handle": m.get("handle") or "",
        **({"pending": True} if m.get("pending") else {}),
        **(
          {"active": True}
          if not m.get("pending") and _member_is_active(obj["id"], h)
          else {}
        ),
      }
      for h, m in (obj.get("members") or {}).items()
    },
  }


# ── caller gating ───────────────────────────────────────────────────────────

def _caller_app_slug(db: Session, principal: Principal) -> str | None:
  """The calling app's slug, or None for the owner."""
  if principal.app_id is None:
    return None
  app = db.query(models.App).filter(models.App.id == principal.app_id).first()
  if app is None:
    raise HTTPException(status_code=403, detail="Unknown app token.")
  return app.slug


def _require_app_match(caller_slug: str | None, object_app: str) -> None:
  """An app token may only touch its own app's objects; the owner may touch any."""
  if caller_slug is not None and caller_slug != object_app:
    raise HTTPException(
      status_code=403, detail="Objects belong to the app that created them."
    )


# ── remote membership records ───────────────────────────────────────────────

def _load_remote(host: str, oid: str) -> dict | None:
  path = _remote_path(host, oid)
  if not path.is_file():
    return None
  return json.loads(path.read_text())


def _save_remote(record: dict) -> None:
  atomic_write(
    _remote_path(record["host"], record["id"]), json.dumps(record, indent=2)
  )


# ── peer surface ────────────────────────────────────────────────────────────

async def _read_object_envelope(request: Request) -> dict:
  body = await read_capped_body(request, MAX_ENVELOPE_BYTES)
  try:
    envelope = json.loads(body)
  except Exception as exc:
    raise HTTPException(status_code=400, detail="Envelope is not JSON.") from exc
  if not isinstance(envelope, dict):
    raise HTTPException(status_code=400, detail="Envelope is not an object.")
  if envelope.get("v") != 0:
    raise HTTPException(status_code=400, detail="Unsupported envelope version.")
  if envelope.get("to") != _own_host():
    raise HTTPException(status_code=400, detail="Envelope is addressed elsewhere.")
  return envelope


def _member_role(obj: dict, host: str) -> str | None:
  member = (obj.get("members") or {}).get(host)
  return member.get("role") if member else None


@router.post("/{oid}/peer")
async def peer_operation(oid: str, request: Request):
  """One signed peer surface: join, state, write, and leave envelopes."""
  if not _OID_RE.fullmatch(oid):
    raise HTTPException(status_code=404, detail="No such object.")
  envelope = await _read_object_envelope(request)
  kind = envelope.get("type")
  if kind not in ("object_join", "object_state", "object_write", "object_leave"):
    raise HTTPException(status_code=400, detail="Unsupported envelope type.")
  actor = await _verify_peer_envelope(envelope)
  sender = envelope["from"]

  async with _object_lock(oid):
    obj = _load_object(oid)
    if obj is None:
      raise HTTPException(status_code=404, detail="No such object.")

    if kind == "object_join":
      # Two admission paths: a pre-authorized handle invitation (the sender is
      # already a pending member) or a capability code carried in the envelope.
      secret = envelope.get("invite")
      _prune_invites(obj)
      invite = None
      if isinstance(secret, str) and secret:
        invite = (obj.get("invites") or {}).get(_hash_secret(secret))
      existing = (obj.get("members") or {}).get(sender) or {}
      existing_role = existing.get("role")
      if invite is None and existing_role is None:
        raise HTTPException(status_code=403, detail="Invite is invalid or expired.")
      role = existing_role or invite["role"]
      if invite is not None and existing_role is None:
        obj["invites"].pop(_hash_secret(secret), None)
      obj.setdefault("members", {})[sender] = {
        "role": role,
        "name": str(actor.get("name") or existing.get("name") or "")[:MAX_LABEL_CHARS],
        "handle": str(actor.get("handle") or existing.get("handle") or "")[:MAX_LABEL_CHARS],
        "joined_at": time.time(),
      }
      _mark_present(oid, sender)
      _save_object(obj)
      return {
        "status": "joined",
        "object": _public_object(obj),
        "doc": _load_doc(oid),
      }

    role = _member_role(obj, sender)
    if role is None:
      raise HTTPException(status_code=403, detail="Not a member of this object.")

    if kind == "object_leave":
      obj["members"].pop(sender, None)
      _save_object(obj)
      return {"status": "left"}

    if kind == "object_state":
      _mark_present(oid, sender)
      since = envelope.get("since_version")
      since = since if isinstance(since, int) else -1
      payload = {"status": "ok", "version": obj["version"], "object": _public_object(obj)}
      if obj["version"] > since:
        payload["doc"] = _load_doc(oid)
      return payload

    # object_write
    if role != "editor":
      raise HTTPException(status_code=403, detail="Viewers cannot write.")
    doc = envelope.get("doc")
    expected = envelope.get("expected_version")
    if doc is None or not isinstance(expected, int):
      raise HTTPException(status_code=400, detail="Write envelope is malformed.")
    _encode_doc(doc)
    if expected != obj["version"]:
      return {
        "status": "conflict",
        "version": obj["version"],
        "doc": _load_doc(oid),
      }
    _save_doc(oid, doc)
    _mark_present(oid, sender)
    obj["version"] += 1
    obj["updated_at"] = time.time()
    obj.setdefault("members", {}).get(sender, {})["last_write_at"] = time.time()
    _save_object(obj)
    return {"status": "ok", "version": obj["version"]}


# ── handle invitations ──────────────────────────────────────────────────────
#
# A handle invitation authorizes a member by WHO THEY ARE instead of by a
# code they carry: the host adds the invitee's instance as a pending member
# and delivers a signed invitation envelope to that instance, which stores it
# and notifies its owner. Accepting is an ordinary join — no secret needed,
# because the sender's verified signature is the credential.

def _invitations_dir() -> Path:
  d = _objects_dir() / "invitations"
  d.mkdir(parents=True, exist_ok=True)
  return d


def _invitation_path(host: str, oid: str) -> Path:
  safe = re.sub(r"[^a-z0-9.-]", "_", host)
  return _invitations_dir() / f"{safe}__{oid}.json"


async def _resolve_invitee(address: str, db: Session, owner_id: int) -> str:
  """A handle, `name@host`, or bare host → the routable instance host.

  A bare handle (no dot, no host part) resolves through the community-host
  directory, which maps mobius.you handles to instance hosts. Address forms
  that already carry a host skip the directory.
  """
  raw = address.strip().lower().lstrip("@")
  if "@" in raw:
    host = raw.rsplit("@", 1)[-1]
    if not _valid_host(host):
      raise HTTPException(status_code=400, detail="That handle is not valid.")
    return host
  if "." in raw and _valid_host(raw):
    return raw
  # A mobius.you account link is the authoritative handle registry and does
  # not require the recipient to have installed or joined Social.
  from app.routes.identity import resolve_handle_hosts
  account_hosts = await resolve_handle_hosts(db, owner_id, raw)
  if account_hosts is not None:
    matches = [host for host in dict.fromkeys(account_hosts) if _valid_host(host)]
    if len(matches) == 1:
      return matches[0]
    if len(matches) > 1:
      raise HTTPException(
        status_code=409,
        detail="That handle is linked to more than one Möbius. Try their full address.",
      )
    raise HTTPException(
      status_code=409,
      detail="That handle exists but does not have a reachable Möbius yet.",
    )

  # Unlinked local owners retain the opt-in Common directory fallback.
  from app.routes.common import _load_identity as _ident, _directory_path
  identity = _ident()
  community = identity.get("community_host") or _own_host()
  entries = {}
  if community == _own_host():
    path = _directory_path()
    if path.is_file():
      entries = json.loads(path.read_text())
  else:
    try:
      async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
        response = await client.get(
          f"{_peer_base_url(community)}/api/common/directory", params={"q": raw}
        )
        response.raise_for_status()
        entries = {
          u["host"]: u for u in response.json().get("users", []) if u.get("host")
        }
    except Exception as exc:
      raise HTTPException(
        status_code=502, detail=f"The directory at {community} could not be reached."
      ) from exc
  matches = [
    host for host, entry in entries.items()
    if str(entry.get("handle") or "").lower() == raw
  ]
  if len(matches) == 1:
    return matches[0]
  if len(matches) > 1:
    raise HTTPException(
      status_code=409, detail="That handle matches more than one instance."
    )
  raise HTTPException(
    status_code=404,
    detail="No one with that handle is in the directory. "
    "Try their full address (handle@their-mobius-host).",
  )


@router.post("/invitations/deliver")
async def receive_invitation(request: Request, db: Session = Depends(get_db)):
  """Accept one signed board/object invitation from the hosting instance."""
  envelope = await _read_object_envelope(request)
  if envelope.get("type") != "object_invitation":
    raise HTTPException(status_code=400, detail="Unsupported envelope type.")
  actor = await _verify_peer_envelope(envelope)
  sender = envelope["from"]
  meta = envelope.get("object")
  if not isinstance(meta, dict) or not _OID_RE.fullmatch(str(meta.get("id") or "")):
    raise HTTPException(status_code=400, detail="Invitation is malformed.")
  invitation = {
    "id": meta["id"],
    "host": sender,
    "app": str(meta.get("app") or "")[:63],
    "kind": str(meta.get("kind") or "")[:MAX_KIND_CHARS],
    "label": str(meta.get("label") or "")[:MAX_LABEL_CHARS],
    "role": meta.get("role") if meta.get("role") in ROLES else "viewer",
    "from_name": str(actor.get("name") or "")[:MAX_LABEL_CHARS],
    "received_at": time.time(),
  }
  atomic_write(_invitation_path(sender, meta["id"]), json.dumps(invitation, indent=2))
  owner = db.query(models.Owner).first()
  if owner is not None:
    app_row = (
      db.query(models.App).filter(models.App.slug == invitation["app"]).first()
      if invitation["app"] else None
    )
    try:
      push.notify_owner(
        db,
        owner.id,
        title=f"{invitation['from_name'] or sender} invited you",
        body=invitation["label"] or f"A shared {invitation['kind'] or 'item'}",
        source_type="app" if app_row else "agent",
        source_id=str(app_row.id) if app_row else None,
        target=f"/shell/?app={app_row.id}" if app_row else "/",
      )
    except Exception:
      pass  # storing the invitation must not fail on push problems
  return {"status": "delivered"}


@router.get("/invitations")
def list_invitations(
  app: str = "",
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  caller = _caller_app_slug(db, principal)
  wanted = caller or (app.strip() or None)
  invitations = []
  for record in sorted(_invitations_dir().glob("*.json")):
    try:
      inv = json.loads(record.read_text())
    except Exception:
      continue
    if wanted and inv.get("app") != wanted:
      continue
    invitations.append(inv)
  return {"invitations": invitations}


@router.post("/invitations/{host}/{oid}/decline")
async def decline_invitation(
  host: str,
  oid: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  caller = _caller_app_slug(db, principal)
  path = _invitation_path(host, oid) if _valid_host(host) else None
  if path is None or not path.is_file():
    raise HTTPException(status_code=404, detail="No such invitation.")
  inv = json.loads(path.read_text())
  _require_app_match(caller, inv.get("app") or "")
  identity = _load_identity()
  envelope = {
    "v": 0,
    "type": "object_leave",
    "from": _own_host(),
    "to": host,
    "sent_at": time.time(),
  }
  envelope["sig"] = _sign(envelope, identity["private_key_b64"])
  try:
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      await client.post(
        f"{_peer_base_url(host)}/api/common/objects/{oid}/peer", json=envelope
      )
  except Exception:
    pass  # the host prunes the pending member on next contact
  path.unlink(missing_ok=True)
  return {"status": "declined"}


# ── owner / app surface ─────────────────────────────────────────────────────

class CreateObject(BaseModel):
  app: str
  kind: str = ""
  label: str = ""
  doc: dict | list


class JoinObject(BaseModel):
  app: str
  invite: str | None = None    # capability-code form: <oid>@<host>#<secret>
  host: str | None = None      # handle-invitation form: object host + id
  id: str | None = None
  label: str = ""


class CreateInvite(BaseModel):
  role: str = "editor"
  address: str | None = None   # handle form: name@host or bare host
  ttl_seconds: int | None = None


class WriteState(BaseModel):
  doc: dict | list
  expected_version: int


@router.post("")
async def create_object(
  body: CreateObject,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  caller = _caller_app_slug(db, principal)
  if not _APP_SLUG_RE.fullmatch(body.app):
    raise HTTPException(status_code=400, detail="Invalid app slug.")
  _require_app_match(caller, body.app)
  _encode_doc(body.doc)
  identity = _load_identity()
  oid = uuid.uuid4().hex
  obj = {
    "id": oid,
    "app": body.app,
    "kind": body.kind.strip()[:MAX_KIND_CHARS],
    "label": body.label.strip()[:MAX_LABEL_CHARS],
    "version": 1,
    "created_at": time.time(),
    "updated_at": time.time(),
    "members": {
      _own_host(): {
        "role": "editor",
        "name": str(identity.get("name") or "")[:MAX_LABEL_CHARS],
        "handle": str(identity.get("handle") or "")[:MAX_LABEL_CHARS],
        "joined_at": time.time(),
        "host_owner": True,
      }
    },
    "invites": {},
  }
  async with _object_lock(oid):
    _save_doc(oid, body.doc)
    _save_object(obj)
  return {"id": oid, "host": _own_host(), "version": 1}


@router.get("")
def list_objects(
  app: str = "",
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  caller = _caller_app_slug(db, principal)
  wanted = caller or (app.strip() or None)
  hosted = []
  hosted_root = _objects_dir() / "hosted"
  if hosted_root.is_dir():
    for record in sorted(hosted_root.glob("*/object.json")):
      try:
        obj = json.loads(record.read_text())
      except Exception:
        continue
      if wanted and obj.get("app") != wanted:
        continue
      hosted.append(_public_object(obj))
  joined = []
  for record in sorted(_remote_dir().glob("*.json")):
    try:
      membership = json.loads(record.read_text())
    except Exception:
      continue
    if wanted and membership.get("app") != wanted:
      continue
    joined.append(membership)
  return {"hosted": hosted, "joined": joined}


@router.post("/join")
async def join_object(
  body: JoinObject,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  caller = _caller_app_slug(db, principal)
  if not _APP_SLUG_RE.fullmatch(body.app):
    raise HTTPException(status_code=400, detail="Invalid app slug.")
  _require_app_match(caller, body.app)
  if body.invite:
    match = _INVITE_RE.fullmatch(body.invite.strip())
    if match is None:
      raise HTTPException(status_code=400, detail="Invite is malformed.")
    host, oid, secret = match["host"], match["oid"], match["secret"]
  elif body.host and body.id and _valid_host(body.host) and _OID_RE.fullmatch(body.id):
    # Handle-invitation acceptance: membership is pre-authorized on the host,
    # so the signed join itself is the credential.
    host, oid, secret = body.host, body.id, ""
  else:
    raise HTTPException(status_code=400, detail="Invite is malformed.")
  if host == _own_host():
    raise HTTPException(
      status_code=400, detail="This object already lives on this instance."
    )
  identity = _load_identity()
  envelope = {
    "v": 0,
    "type": "object_join",
    "from": _own_host(),
    "to": host,
    "invite": secret,
    "sent_at": time.time(),
  }
  envelope["sig"] = _sign(envelope, identity["private_key_b64"])
  try:
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      response = await client.post(
        f"{_peer_base_url(host)}/api/common/objects/{oid}/peer", json=envelope
      )
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail=f"{host} could not be reached."
    ) from exc
  if response.status_code != 200:
    detail = "Join was refused."
    try:
      detail = response.json().get("detail") or detail
    except Exception:
      pass
    raise HTTPException(status_code=response.status_code, detail=detail)
  joined = response.json()
  remote_object = joined.get("object") or {}
  if remote_object.get("app") != body.app:
    raise HTTPException(
      status_code=400,
      detail="That invite belongs to a different app.",
    )
  membership = {
    "id": oid,
    "host": host,
    "app": body.app,
    "kind": remote_object.get("kind") or "",
    "label": body.label.strip()[:MAX_LABEL_CHARS]
    or remote_object.get("label") or "",
    "role": (remote_object.get("members") or {}).get(_own_host(), {}).get("role")
    or "editor",
    "joined_at": time.time(),
  }
  _save_remote(membership)
  # Accepting consumes any stored invitation for this object.
  _invitation_path(host, oid).unlink(missing_ok=True)
  return {"status": "joined", "membership": membership, "doc": joined.get("doc")}


@router.post("/{oid}/invites")
async def create_invite(
  oid: str,
  body: CreateInvite,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  caller = _caller_app_slug(db, principal)
  if not _OID_RE.fullmatch(oid):
    raise HTTPException(status_code=404, detail="No such object.")
  if body.role not in ROLES:
    raise HTTPException(status_code=400, detail="Role must be editor or viewer.")

  if body.address:
    # Handle invitation: pre-authorize the invitee's instance as a pending
    # member, then deliver a signed invitation to it. Their verified signature
    # is the credential when they accept — no code changes hands.
    peer = await _resolve_invitee(body.address, db, principal.owner.id)
    if peer == _own_host():
      raise HTTPException(status_code=400, detail="That handle is this instance.")
    async with _object_lock(oid):
      obj = _load_object(oid)
      if obj is None:
        raise HTTPException(status_code=404, detail="No such object.")
      _require_app_match(caller, obj["app"])
      existing = (obj.get("members") or {}).get(peer)
      if existing and not existing.get("pending"):
        raise HTTPException(status_code=400, detail="They are already a member.")
      invited_handle = body.address.strip().lstrip("@")
      if "@" in invited_handle:
        invited_handle = invited_handle.rsplit("@", 1)[0]
      obj.setdefault("members", {})[peer] = {
        "role": body.role,
        "name": body.address.strip()[:MAX_LABEL_CHARS],
        "handle": invited_handle[:MAX_LABEL_CHARS] if "." not in invited_handle else "",
        "invited_at": time.time(),
        "pending": True,
      }
      _save_object(obj)
      meta = {
        "id": oid,
        "app": obj["app"],
        "kind": obj.get("kind") or "",
        "label": obj.get("label") or "",
        "role": body.role,
      }
    identity = _load_identity()
    envelope = {
      "v": 0,
      "type": "object_invitation",
      "from": _own_host(),
      "to": peer,
      "object": meta,
      "sent_at": time.time(),
    }
    envelope["sig"] = _sign(envelope, identity["private_key_b64"])
    delivery = "delivered"
    try:
      async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
        response = await client.post(
          f"{_peer_base_url(peer)}/api/common/objects/invitations/deliver",
          json=envelope,
        )
        response.raise_for_status()
    except Exception:
      delivery = "unreachable"
    return {"status": "invited", "host": peer, "role": body.role, "delivery": delivery}

  # Capability-code fallback for someone you cannot address directly.
  ttl = body.ttl_seconds if body.ttl_seconds and body.ttl_seconds > 0 else INVITE_TTL_S
  secret = pysecrets.token_urlsafe(24)
  async with _object_lock(oid):
    obj = _load_object(oid)
    if obj is None:
      raise HTTPException(status_code=404, detail="No such object.")
    _require_app_match(caller, obj["app"])
    _prune_invites(obj)
    obj.setdefault("invites", {})[_hash_secret(secret)] = {
      "role": body.role,
      "created_at": time.time(),
      "expires_at": time.time() + ttl,
    }
    _save_object(obj)
  return {
    "invite": f"{oid}@{_own_host()}#{secret}",
    "role": body.role,
    "expires_at": obj["invites"][_hash_secret(secret)]["expires_at"],
  }


@router.get("/{oid}/members")
def get_members(
  oid: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  caller = _caller_app_slug(db, principal)
  obj = _load_object(oid) if _OID_RE.fullmatch(oid) else None
  if obj is None:
    raise HTTPException(status_code=404, detail="No such object.")
  _require_app_match(caller, obj["app"])
  return _public_object(obj)


@router.delete("/{oid}/members/{member_host}")
async def revoke_member(
  oid: str,
  member_host: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  caller = _caller_app_slug(db, principal)
  if not _OID_RE.fullmatch(oid):
    raise HTTPException(status_code=404, detail="No such object.")
  async with _object_lock(oid):
    obj = _load_object(oid)
    if obj is None:
      raise HTTPException(status_code=404, detail="No such object.")
    _require_app_match(caller, obj["app"])
    if member_host == _own_host():
      raise HTTPException(status_code=400, detail="The host cannot revoke itself.")
    if member_host not in (obj.get("members") or {}):
      raise HTTPException(status_code=404, detail="No such member.")
    obj["members"].pop(member_host, None)
    _object_presence.pop((oid, member_host), None)
    _save_object(obj)
  return {"status": "revoked"}


@router.delete("/{oid}")
async def delete_object(
  oid: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  caller = _caller_app_slug(db, principal)
  if not _OID_RE.fullmatch(oid):
    raise HTTPException(status_code=404, detail="No such object.")
  async with _object_lock(oid):
    obj = _load_object(oid)
    if obj is None:
      raise HTTPException(status_code=404, detail="No such object.")
    _require_app_match(caller, obj["app"])
    d = _hosted_dir(oid)
    for name in ("object.json", "doc.json"):
      (d / name).unlink(missing_ok=True)
    if d.is_dir():
      try:
        d.rmdir()
      except OSError:
        pass
    for key in [key for key in _object_presence if key[0] == oid]:
      _object_presence.pop(key, None)
  return {"status": "deleted"}


@router.post("/{host}/{oid}/leave")
async def leave_object(
  host: str,
  oid: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  caller = _caller_app_slug(db, principal)
  membership = _load_remote(host, oid) if _valid_host(host) else None
  if membership is None:
    raise HTTPException(status_code=404, detail="Not a member of that object.")
  _require_app_match(caller, membership["app"])
  identity = _load_identity()
  envelope = {
    "v": 0,
    "type": "object_leave",
    "from": _own_host(),
    "to": host,
    "sent_at": time.time(),
  }
  envelope["sig"] = _sign(envelope, identity["private_key_b64"])
  try:
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      await client.post(
        f"{_peer_base_url(host)}/api/common/objects/{oid}/peer", json=envelope
      )
  except Exception:
    pass  # local leave still succeeds; the host prunes on next contact
  _remote_path(host, oid).unlink(missing_ok=True)
  return {"status": "left"}


async def _proxied_state(host: str, oid: str, since_version: int) -> dict:
  identity = _load_identity()
  envelope = {
    "v": 0,
    "type": "object_state",
    "from": _own_host(),
    "to": host,
    "since_version": since_version,
    "sent_at": time.time(),
  }
  envelope["sig"] = _sign(envelope, identity["private_key_b64"])
  try:
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      response = await client.post(
        f"{_peer_base_url(host)}/api/common/objects/{oid}/peer", json=envelope
      )
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail=f"{host} could not be reached."
    ) from exc
  if response.status_code != 200:
    detail = "The host refused the request."
    try:
      detail = response.json().get("detail") or detail
    except Exception:
      pass
    raise HTTPException(status_code=response.status_code, detail=detail)
  return response.json()


@router.get("/{host}/{oid}/state")
async def read_state(
  host: str,
  oid: str,
  since_version: int = -1,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  caller = _caller_app_slug(db, principal)
  if not _OID_RE.fullmatch(oid) or not _valid_host(host):
    raise HTTPException(status_code=404, detail="No such object.")
  if host == _own_host():
    obj = _load_object(oid)
    if obj is None:
      raise HTTPException(status_code=404, detail="No such object.")
    _require_app_match(caller, obj["app"])
    _mark_present(oid, _own_host())
    payload = {"status": "ok", "version": obj["version"], "object": _public_object(obj)}
    if obj["version"] > since_version:
      payload["doc"] = _load_doc(oid)
    return payload
  membership = _load_remote(host, oid)
  if membership is None:
    raise HTTPException(status_code=404, detail="Not a member of that object.")
  _require_app_match(caller, membership["app"])
  return await _proxied_state(host, oid, since_version)


@router.put("/{host}/{oid}/state")
async def write_state(
  host: str,
  oid: str,
  body: WriteState,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  caller = _caller_app_slug(db, principal)
  if not _OID_RE.fullmatch(oid) or not _valid_host(host):
    raise HTTPException(status_code=404, detail="No such object.")
  _encode_doc(body.doc)

  if host == _own_host():
    async with _object_lock(oid):
      obj = _load_object(oid)
      if obj is None:
        raise HTTPException(status_code=404, detail="No such object.")
      _require_app_match(caller, obj["app"])
      if body.expected_version != obj["version"]:
        return {
          "status": "conflict",
          "version": obj["version"],
          "doc": _load_doc(oid),
        }
      _save_doc(oid, body.doc)
      _mark_present(oid, _own_host())
      obj["version"] += 1
      obj["updated_at"] = time.time()
      _save_object(obj)
      return {"status": "ok", "version": obj["version"]}

  membership = _load_remote(host, oid)
  if membership is None:
    raise HTTPException(status_code=404, detail="Not a member of that object.")
  _require_app_match(caller, membership["app"])
  identity = _load_identity()
  envelope = {
    "v": 0,
    "type": "object_write",
    "from": _own_host(),
    "to": host,
    "doc": body.doc,
    "expected_version": body.expected_version,
    "sent_at": time.time(),
  }
  envelope["sig"] = _sign(envelope, identity["private_key_b64"])
  try:
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      response = await client.post(
        f"{_peer_base_url(host)}/api/common/objects/{oid}/peer", json=envelope
      )
  except Exception as exc:
    raise HTTPException(
      status_code=502, detail=f"{host} could not be reached."
    ) from exc
  if response.status_code != 200:
    detail = "The host refused the write."
    try:
      detail = response.json().get("detail") or detail
    except Exception:
      pass
    raise HTTPException(status_code=response.status_code, detail=detail)
  return response.json()
