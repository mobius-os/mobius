"""Common federation — group chats (protocol common/0, group extension).

A group lives on its creator's instance, the **group host**. The host owns the
authoritative membership record; members keep their own copy of every message,
exactly like 1:1 conversations. Six signed envelope types extend the
protocol, all delivered to `/api/common/groups/inbox`:

- `group_added`  host → invitee   invitation; carries group metadata
- `group_accept` invitee → host   owner explicitly joins the group
- `group_decline` invitee → host  owner dismisses the invitation
- `group_post`   member → host    a member's message for the group
- `group_message` host → member   the host's fan-out relay of a post
- `group_deleted` host → member   the group is closed; retain local history

A relayed `group_message` is signed by the *host* and carries the complete
author-signed `group_post` as `original`. Members verify both signatures, so
the host controls membership and fan-out without being able to forge another
member's authorship. Nothing about a group ever touches a third instance.

Owner surface (owner JWT or the Common app's scoped token):
  POST /api/common/groups                     create a group + invite members
  POST /api/common/groups/{gid}/send          send a message to the group
  POST /api/common/groups/{gid}/accept        accept + join a remote group
  POST /api/common/groups/{gid}/decline       decline a remote invitation
  POST /api/common/groups/{gid}/members       add a member (host only)
  DELETE /api/common/groups/{gid}             close a group (host only)

Host-side authoritative records live in `<data_dir>/common/groups/`; each
instance's own copy of group conversations lives in the Common app's per-app
storage under `groups/<gid>/`, where the app UI reads it.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import fs_locks, models, push
from app.common_protocol import (
  MAX_ENVELOPE_BYTES,
  MAX_NAME_CHARS,
  OUTBOUND_TIMEOUT_S,
  peer_base_url as _peer_base_url,
  read_envelope as _read_envelope,
  sign as _sign,
  valid_host as _valid_host,
  validate_attachment as _validate_attachment,
  validate_reply_to as _validate_reply_to,
  validate_text_or_attachment as _validate_text_or_attachment,
)
from app.common_transport import federation_request
from app.database import get_db
from app.deps import Principal, get_principal, require_nondelegated_owner_control
from app.routes.common import (
  _app_data_dir,
  _bump_version,
  _common_app,
  _common_dir,
  _fetch_actor,
  _load_identity,
  _message_preview,
  _own_host,
  _require_owner_or_common_app,
  _verify_peer_envelope,
  _write_app_attachment,
)
from app.storage_io import atomic_write

router = APIRouter(prefix="/api/common/groups", tags=["common"])

MAX_GROUP_MEMBERS = 64
_GID_RE = re.compile(r"^[a-f0-9-]{8,64}$")
_REQUEST_STATES = {"pending", "accepted", "declined"}
_group_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


def _group_lock(gid: str) -> asyncio.Lock:
  # Group lifecycle is outermost; app-storage locks are acquired only inside it.
  lock = _group_locks.get(gid)
  if lock is None:
    lock = asyncio.Lock()
    _group_locks[gid] = lock
  return lock


def _validate_gid(gid: str) -> None:
  if not isinstance(gid, str) or not _GID_RE.fullmatch(gid):
    raise HTTPException(status_code=400, detail="Group id is invalid.")


def _require_open(group: dict) -> None:
  if group.get("deleted_at"):
    raise HTTPException(status_code=410, detail="This group has been deleted.")



# ── host-side authoritative records ─────────────────────────────────────────

def _host_groups_dir() -> Path:
  d = _common_dir() / "groups"
  d.mkdir(parents=True, exist_ok=True)
  return d


def _host_group_path(gid: str) -> Path:
  return _host_groups_dir() / f"{gid}.json"


def _load_host_group(gid: str) -> dict | None:
  path = _host_group_path(gid)
  return json.loads(path.read_text()) if path.is_file() else None


# ── member-side app-storage copies (what the UI reads) ──────────────────────

def _group_dir(app: models.App, gid: str) -> Path:
  return _app_data_dir(app) / "groups" / gid


def _load_group_meta(app: models.App, gid: str) -> dict | None:
  path = _group_dir(app, gid) / "meta.json"
  return json.loads(path.read_text()) if path.is_file() else None


async def _store_group_meta(app: models.App, gid: str, updates: dict) -> None:
  async with fs_locks.app_storage_lock(app.id):
    path = _group_dir(app, gid) / "meta.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    meta = json.loads(path.read_text()) if path.is_file() else {}
    meta.update(updates)
    atomic_write(path, json.dumps(meta))
    _bump_version(app)


async def _store_group_message(
  app: models.App, gid: str, record: dict,
  attachment: tuple[dict, bytes] | None = None,
) -> bool:
  async with fs_locks.app_storage_lock(app.id):
    group = _group_dir(app, gid)
    msgs = group / "msgs"
    message_path = msgs / f"{record['id']}.json"
    if message_path.is_file():
      return False
    if attachment is not None:
      record["attachment"] = _write_app_attachment(
        group, record["id"], attachment
      )
    msgs.mkdir(parents=True, exist_ok=True)
    atomic_write(message_path, json.dumps(record))
    meta_path = group / "meta.json"
    had_meta = meta_path.is_file()
    meta = json.loads(meta_path.read_text()) if had_meta else {}
    request_state = meta.get("request_status")
    if request_state not in _REQUEST_STATES:
      # Every pre-request-migration group is an established conversation.
      request_state = "accepted" if had_meta else "pending"
    meta.update(
      gid=gid,
      last_text=_message_preview(record["text"]),
      last_at=record["sent_at"],
      last_from_handle=record.get("author_handle") or record.get("author") or "",
      last_dir=record["dir"],
      request_status=request_state,
    )
    if record["dir"] == "in" and request_state == "accepted":
      meta["unread"] = int(meta.get("unread") or 0) + 1
    elif record["dir"] == "in" and request_state == "pending":
      meta["request_count"] = int(meta.get("request_count") or 0) + 1
      meta["unread"] = 0
    elif request_state != "accepted":
      meta["unread"] = 0
    atomic_write(meta_path, json.dumps(meta))
    _bump_version(app)
    return True


def _group_request_state(meta: dict) -> str:
  state = meta.get("request_status")
  return state if state in _REQUEST_STATES else "accepted"


def _host_member_active(member: dict) -> bool:
  # Host records created before message requests have no status; they remain
  # active rather than being silently demoted during upgrade.
  return member.get("status") in (None, "active")


def _require_accepted_group(meta: dict) -> None:
  if _group_request_state(meta) != "accepted":
    raise HTTPException(
      status_code=409,
      detail="Accept this group invitation before sending messages.",
    )


def _members_snapshot(group: dict) -> list[dict]:
  # Privacy contract: only host + handle circulate; display names never do.
  return [
    {"host": h, "handle": str(m.get("handle") or "")[:MAX_NAME_CHARS]}
    for h, m in group["members"].items()
    if m.get("status") != "declined"
  ]


# ── outbound delivery ───────────────────────────────────────────────────────

async def _deliver(host: str, envelope: dict) -> bool:
  try:
    response = await federation_request(
      "POST", f"{_peer_base_url(host)}/api/common/groups/inbox",
      json=envelope, max_response_bytes=MAX_ENVELOPE_BYTES,
      timeout_seconds=OUTBOUND_TIMEOUT_S,
    )
    response.raise_for_status()
    return True
  except Exception:
    return False


def _signed(envelope: dict, identity: dict) -> dict:
  envelope["sig"] = _sign(envelope, identity["private_key_b64"])
  return envelope


async def _fan_out(group: dict, envelopes_by_host: dict[str, dict]) -> dict:
  """Deliver per-member envelopes concurrently; returns host → delivered."""
  hosts = list(envelopes_by_host)
  results = await asyncio.gather(
    *(_deliver(h, envelopes_by_host[h]) for h in hosts)
  )
  return dict(zip(hosts, results))


def _added_envelope(identity: dict, group: dict, member: str) -> dict:
  member_record = group["members"][member]
  invitation_id = member_record.get("invitation_id")
  if not isinstance(invitation_id, str) or not _GID_RE.fullmatch(invitation_id):
    # Stable compatibility identity for a legacy active member receiving a
    # roster refresh. New pending invitations always persist a random id.
    invitation_id = str(uuid.uuid5(
      uuid.NAMESPACE_URL, f"common-group:{group['id']}:{member}"
    ))
  invitation_version = member_record.get("invitation_version")
  if not isinstance(invitation_version, int) or isinstance(invitation_version, bool):
    invitation_version = 1
  return _signed({
    "v": 0,
    "type": "group_added",
    "id": invitation_id,
    "from": _own_host(),
    "to": member,
    "gid": group["id"],
    "invitation_version": invitation_version,
    "group_name": group["name"],
    "members": _members_snapshot(group),
    "sent_at": time.time(),
  }, identity)


def _post_envelope(
  identity: dict, gid: str, host: str, *, message_id: str, text: str,
  sent_at: float, attachment: tuple[dict, bytes] | None = None,
  reply_to: dict | None = None,
) -> dict:
  envelope = {
    "v": 0,
    "type": "group_post",
    "id": message_id,
    "from": _own_host(),
    "to": host,
    "gid": gid,
    "text": text,
    "sent_at": sent_at,
  }
  if attachment is not None:
    envelope["attachment"] = attachment[0]
  if reply_to is not None:
    envelope["reply_to"] = reply_to
  return _signed(envelope, identity)


def _relay_envelope(
  identity: dict, group: dict, member: str, *, original: dict,
  message_id: str, author: str, author_handle: str, text: str, sent_at: float,
  attachment: tuple[dict, bytes] | None = None,
  reply_to: dict | None = None,
) -> dict:
  envelope = {
    "v": 0,
    "type": "group_message",
    "id": message_id,
    "from": _own_host(),
    "to": member,
    "gid": group["id"],
    "group_name": group["name"],
    "author": author,
    "author_handle": author_handle,
    "text": text,
    "original": original,
    "sent_at": sent_at,
  }
  if attachment is not None:
    envelope["attachment"] = attachment[0]
  if reply_to is not None:
    envelope["reply_to"] = reply_to
  return _signed(envelope, identity)


async def _host_accept_post(
  db: Session, app: models.App, group: dict, *, original: dict,
  message_id: str, author: str, author_handle: str, text: str, sent_at: float,
  attachment: tuple[dict, bytes] | None = None,
  reply_to: dict | None = None,
) -> dict:
  """Host duties for one accepted group post: store, relay, notify."""
  record = {
    "id": message_id, "gid": group["id"], "author": author,
    "author_handle": author_handle, "text": text, "sent_at": sent_at,
    "dir": "out" if author == _own_host() else "in",
    "status": "delivered",
  }
  if reply_to is not None:
    record["reply_to"] = reply_to
  await _store_group_message(app, group["id"], record, attachment)
  identity = _load_identity()
  relays = {
    member: _relay_envelope(
      identity, group, member, original=original, message_id=message_id,
      author=author, author_handle=author_handle, text=text, sent_at=sent_at,
      attachment=attachment, reply_to=reply_to,
    )
    for member in group["members"]
    if (
      member not in (_own_host(), author)
      and _host_member_active(group["members"][member])
    )
  }
  delivered = await _fan_out(group, relays)
  if author != _own_host():
    _notify_group_message(db, app, group["name"], author_handle, text)
  return delivered


def _notify_group_message(
  db: Session, app: models.App, group_name: str, author_handle: str, text: str
) -> None:
  owner = db.query(models.Owner).first()
  if owner is None:
    return
  try:
    push.notify_owner(
      db,
      owner.id,
      title=f"{group_name} — {author_handle}",
      body=_message_preview(text),
      source_type="app",
      source_id=str(app.id),
      target=f"/shell/?app={app.id}",
    )
  except Exception:
    pass  # message delivery must not fail on push problems


# ── peer surface ────────────────────────────────────────────────────────────

@router.post("/inbox")
async def group_inbox(request: Request, db: Session = Depends(get_db)):
  """Verify peer authority before serializing against the group lifecycle."""
  envelope = await _read_envelope(request)
  if envelope.get("v") != 0:
    raise HTTPException(status_code=400, detail="Unsupported envelope version.")
  if envelope.get("to") != _own_host():
    raise HTTPException(status_code=400, detail="Envelope is addressed elsewhere.")
  gid = envelope.get("gid")
  _validate_gid(gid)
  if envelope.get("type") not in (
    "group_added", "group_deleted", "group_post", "group_message",
    "group_accept", "group_decline",
  ):
    raise HTTPException(status_code=400, detail="Unsupported envelope type.")
  app = _common_app(db)
  actor = await _verify_peer_envelope(envelope)
  async with _group_lock(gid):
    return await _accept_group_envelope(db, app, envelope, actor)


async def _accept_group_envelope(
  db: Session, app: models.App, envelope: dict, actor: dict,
) -> dict:
  """Handle a verified envelope while holding the group's lifecycle lock."""
  gid = envelope["gid"]
  kind = envelope["type"]
  sender = envelope["from"]
  if kind in ("group_accept", "group_decline"):
    action_id = envelope.get("id")
    if not isinstance(action_id, str) or not _GID_RE.fullmatch(action_id):
      raise HTTPException(status_code=400, detail="Request id is invalid.")
    group = _load_host_group(gid)
    if group is None:
      raise HTTPException(status_code=404, detail="Unknown group.")
    _require_open(group)
    member = group["members"].get(sender)
    if member is None:
      raise HTTPException(status_code=403, detail="No invitation exists for this group.")
    status = member.get("status")
    if status is None:
      # A legacy member was already active before invitation requests existed.
      return {"status": "accepted"}
    if envelope.get("invitation_id") != member.get("invitation_id"):
      raise HTTPException(status_code=409, detail="This invitation has been replaced.")
    if kind == "group_accept":
      if status == "active":
        return {"status": "accepted"}
      if status != "invited":
        raise HTTPException(status_code=409, detail="This invitation is no longer pending.")
      member.update(
        status="active",
        handle=str(actor.get("handle") or member.get("handle") or "")[:MAX_NAME_CHARS],
        joined_at=time.time(),
      )
      atomic_write(_host_group_path(gid), json.dumps(group, indent=2))
      await _store_group_meta(app, gid, {"members": _members_snapshot(group)})
      return {"status": "accepted"}
    if status == "declined":
      return {"status": "declined"}
    if status == "active":
      raise HTTPException(status_code=409, detail="An active member cannot decline an invitation.")
    member["status"] = "declined"
    atomic_write(_host_group_path(gid), json.dumps(group, indent=2))
    await _store_group_meta(app, gid, {"members": _members_snapshot(group)})
    return {"status": "declined"}

  if kind in ("group_added", "group_deleted"):
    meta = _load_group_meta(app, gid)
    hosted = _load_host_group(gid)
    known_host = hosted.get("host") if hosted else meta.get("host") if meta else None
    if known_host and sender != known_host:
      raise HTTPException(status_code=403, detail="Only the group host may update this group.")
    if hosted:
      raise HTTPException(status_code=403, detail="Hosted groups are managed locally.")
    name = str(envelope.get("group_name") or "Group")[:MAX_NAME_CHARS]
    if kind == "group_deleted":
      # A tombstone also precedes a delayed first invite; reordering must not
      # resurrect a closed group. Member-owned history is never erased.
      prior_state = _group_request_state(meta) if meta else "declined"
      await _store_group_meta(app, gid, {
        "gid": gid, "host": sender, "name": (meta or {}).get("name") or name,
        "deleted_at": (meta or {}).get("deleted_at") or time.time(),
        "unread": 0,
        "request_status": (
          "accepted" if prior_state == "accepted" else "declined"
        ),
      })
      return {"status": "deleted"}
    if meta:
      _require_open(meta)
    invitation_id = envelope.get("id")
    if not isinstance(invitation_id, str) or not _GID_RE.fullmatch(invitation_id):
      raise HTTPException(status_code=400, detail="Invitation id is invalid.")
    members = envelope.get("members")
    if not isinstance(members, list) or len(members) > MAX_GROUP_MEMBERS:
      raise HTTPException(status_code=400, detail="Member list is invalid.")
    roster = {}
    for member in members:
      if not isinstance(member, dict) or not _valid_host(member.get("host") or ""):
        raise HTTPException(status_code=400, detail="Member list is invalid.")
      roster[member["host"]] = {
        "host": member["host"],
        "handle": str(member.get("handle") or "")[:MAX_NAME_CHARS],
      }
    invitation_version = envelope.get("invitation_version", 1)
    if (
      not isinstance(invitation_version, int)
      or isinstance(invitation_version, bool)
      or invitation_version < 1
    ):
      raise HTTPException(status_code=400, detail="Invitation version is invalid.")
    current_invitation_version = (meta or {}).get("invitation_version", 0)
    if invitation_version < current_invitation_version:
      return {"status": "stale"}
    if (
      meta
      and invitation_version == current_invitation_version
      and meta.get("invitation_id") not in (None, invitation_id)
    ):
      return {"status": "stale"}
    request_state = _group_request_state(meta) if meta else "pending"
    # A host can deliberately re-invite a previously declined member. Accepted
    # groups treat group_added only as an authorized roster refresh.
    if (
      request_state == "declined"
      and (meta or {}).get("invitation_id") != invitation_id
    ):
      request_state = "pending"
    await _store_group_meta(app, gid, {
      "gid": gid, "name": name, "host": sender,
      "members": list(roster.values()),
      "invited_by_handle": str(actor.get("handle") or "")[:MAX_NAME_CHARS],
      "invitation_id": invitation_id,
      "invitation_version": invitation_version,
      "request_status": request_state,
      "request_count": (
        max(1, int((meta or {}).get("request_count") or 0))
        if request_state == "pending" else 0
      ),
      "unread": 0 if request_state == "pending" else int((meta or {}).get("unread") or 0),
    })
    return {"status": "pending" if request_state == "pending" else "updated"}

  message_id = envelope.get("id")
  if not isinstance(message_id, str) or not _GID_RE.fullmatch(message_id):
    raise HTTPException(status_code=400, detail="Message id is invalid.")

  if kind == "group_post":
    group = _load_host_group(gid)
    if group is None:
      raise HTTPException(status_code=404, detail="Unknown group.")
    _require_open(group)
    member = group["members"].get(sender)
    if member is None or not _host_member_active(member):
      raise HTTPException(status_code=403, detail="Not a member of this group.")
    text = envelope.get("text")
    attachment = _validate_attachment(envelope.get("attachment"))
    _validate_text_or_attachment(text, attachment, "Message text is invalid.")
    reply_to = _validate_reply_to(envelope.get("reply_to"))
    existing = _group_dir(app, gid) / "msgs" / f"{message_id}.json"
    if existing.is_file():
      return {"status": "duplicate"}
    delivered = await _host_accept_post(
      db, app, group, original=envelope,
      message_id=message_id, author=sender,
      author_handle=member.get("handle") or sender,
      text=text, sent_at=envelope["sent_at"],
      attachment=attachment, reply_to=reply_to,
    )
    return {"status": "delivered", "relayed": delivered}

  # A member verifies both the known host relay and its author-signed original.
  meta = _load_group_meta(app, gid)
  if meta is None:
    return {"status": "unknown_group"}
  if sender != meta.get("host"):
    raise HTTPException(status_code=403, detail="Relay is not from the group host.")
  _require_open(meta)
  original = envelope.get("original")
  if (
    not isinstance(original, dict)
    or original.get("type") != "group_post"
    or original.get("gid") != gid
    or original.get("id") != message_id
    or original.get("to") != meta["host"]
  ):
    raise HTTPException(status_code=403, detail="Original message is invalid.")
  try:
    original_actor = await _verify_peer_envelope(original)
  except HTTPException as exc:
    raise HTTPException(
      status_code=403, detail="Original message signature is invalid."
    ) from exc
  text = original.get("text")
  attachment = _validate_attachment(original.get("attachment"))
  _validate_text_or_attachment(text, attachment, "Message text is invalid.")
  reply_to = _validate_reply_to(original.get("reply_to"))
  author = original["from"]
  author_handle = str(original_actor.get("handle") or author)[:MAX_NAME_CHARS]
  record = {
    "id": message_id, "gid": gid, "author": author,
    "author_handle": author_handle, "text": text,
    "sent_at": original["sent_at"], "dir": "in", "status": "delivered",
  }
  if reply_to is not None:
    record["reply_to"] = reply_to
  created = await _store_group_message(app, gid, record, attachment)
  if not created:
    return {"status": "duplicate"}
  if _group_request_state(meta) == "accepted":
    _notify_group_message(db, app, meta.get("name") or "Group", author_handle, text)
    return {"status": "delivered"}
  return {"status": "pending"}


# ── owner surface ───────────────────────────────────────────────────────────

class CreateGroup(BaseModel):
  name: str
  members: list[str] = []


class GroupSend(BaseModel):
  text: str
  attachment: Any = None
  reply_to: Any = None


class AddMember(BaseModel):
  host: str
  handle: str | None = None


def _membership_action_envelope(
  identity: dict, gid: str, host: str, action: str, invitation_id: str,
) -> dict:
  return _signed({
    "v": 0,
    "type": action,
    "id": str(uuid.uuid4()),
    "from": _own_host(),
    "to": host,
    "gid": gid,
    "invitation_id": invitation_id,
    "sent_at": time.time(),
  }, identity)


@router.post("")
async def create_group(
  body: CreateGroup,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Create a group hosted on this instance and invite its first members."""
  require_nondelegated_owner_control(principal)
  app = _require_owner_or_common_app(db, principal)
  name = body.name.strip()[:MAX_NAME_CHARS]
  if not name:
    raise HTTPException(status_code=400, detail="Give the group a name.")
  identity = _load_identity()
  if not identity.get("joined_at"):
    raise HTTPException(status_code=409, detail="Join Common before creating groups.")
  member_hosts = []
  for host in body.members:
    host = host.strip().lower()
    if host and host != _own_host():
      if not _valid_host(host):
        raise HTTPException(status_code=400, detail=f"Invalid member: {host}")
      member_hosts.append(host)
  if len(member_hosts) + 1 > MAX_GROUP_MEMBERS:
    raise HTTPException(status_code=400, detail="Too many members.")

  gid = str(uuid.uuid4())
  members = {
    _own_host(): {
      "handle": identity.get("handle") or "", "joined_at": time.time(),
      "status": "active",
    },
  }
  for host in member_hosts:
    entry = {
      "handle": "", "invited_at": time.time(), "status": "invited",
      "invitation_id": str(uuid.uuid4()),
      "invitation_version": 1,
    }
    try:
      actor = await _fetch_actor(host)
      entry["handle"] = actor.get("handle") or ""
    except HTTPException:
      pass  # unreachable now; metadata heals on their first post
    members[host] = entry
  group = {
    "id": gid, "name": name, "host": _own_host(),
    "created_at": time.time(), "members": members,
  }
  async with _group_lock(gid):
    atomic_write(_host_group_path(gid), json.dumps(group, indent=2))

    await _store_group_meta(app, gid, {
      "gid": gid, "name": name, "host": _own_host(),
      "members": _members_snapshot(group),
      "last_at": time.time(), "last_text": "", "unread": 0,
      "request_status": "accepted",
    })
    invited = await _fan_out(group, {
      host: _added_envelope(identity, group, host) for host in member_hosts
    })
  return {"status": "created", "gid": gid, "invited": invited}


@router.post("/{gid}/accept")
async def accept_group_invitation(
  gid: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Join only after the owner explicitly accepts the host's invitation."""
  require_nondelegated_owner_control(principal)
  app = _require_owner_or_common_app(db, principal)
  _validate_gid(gid)
  identity = _load_identity()
  if not identity.get("joined_at"):
    raise HTTPException(status_code=409, detail="Join Social before accepting groups.")
  async with _group_lock(gid):
    meta = _load_group_meta(app, gid)
    if meta is None:
      raise HTTPException(status_code=404, detail="Group invitation not found.")
    _require_open(meta)
    state = _group_request_state(meta)
    if state == "accepted":
      return {"status": "accepted"}
    if state != "pending":
      raise HTTPException(status_code=409, detail="This invitation is no longer pending.")
    host = meta.get("host")
    if not _valid_host(host or "") or host == _own_host():
      raise HTTPException(status_code=409, detail="Group host is invalid.")
    invitation_id = meta.get("invitation_id")
    if not isinstance(invitation_id, str) or not _GID_RE.fullmatch(invitation_id):
      raise HTTPException(status_code=409, detail="Invitation identity is missing.")
    envelope = _membership_action_envelope(
      identity, gid, host, "group_accept", invitation_id
    )

  # Do not hold the lifecycle lock across a call to the same host that may be
  # delivering a tombstone concurrently.
  if not await _deliver(host, envelope):
    raise HTTPException(status_code=502, detail="The group host could not be reached.")

  async with _group_lock(gid):
    current = _load_group_meta(app, gid)
    if current is None or current.get("host") != host:
      raise HTTPException(status_code=409, detail="Group invitation changed while accepting.")
    _require_open(current)
    if _group_request_state(current) == "accepted":
      return {"status": "accepted"}
    if _group_request_state(current) != "pending":
      raise HTTPException(status_code=409, detail="This invitation is no longer pending.")
    await _store_group_meta(app, gid, {
      "request_status": "accepted", "request_count": 0, "unread": 0,
    })
  return {"status": "accepted"}


@router.post("/{gid}/decline")
async def decline_group_invitation(
  gid: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Dismiss an invitation locally without erasing its retained history."""
  require_nondelegated_owner_control(principal)
  app = _require_owner_or_common_app(db, principal)
  _validate_gid(gid)
  identity = _load_identity()
  async with _group_lock(gid):
    meta = _load_group_meta(app, gid)
    if meta is None:
      raise HTTPException(status_code=404, detail="Group invitation not found.")
    _require_open(meta)
    state = _group_request_state(meta)
    if state == "declined":
      return {"status": "declined", "host_notified": True}
    if state != "pending":
      raise HTTPException(status_code=409, detail="This invitation is no longer pending.")
    host = meta.get("host")
    if not _valid_host(host or "") or host == _own_host():
      raise HTTPException(status_code=409, detail="Group host is invalid.")
    await _store_group_meta(app, gid, {
      "request_status": "declined", "request_count": 0, "unread": 0,
    })
    invitation_id = meta.get("invitation_id")
    if not isinstance(invitation_id, str) or not _GID_RE.fullmatch(invitation_id):
      raise HTTPException(status_code=409, detail="Invitation identity is missing.")
    envelope = _membership_action_envelope(
      identity, gid, host, "group_decline", invitation_id
    )

  # Declining is locally final even when the host is offline. It never grants
  # membership, and the host can retry an invitation later.
  notified = await _deliver(host, envelope)
  return {"status": "declined", "host_notified": notified}


@router.post("/{gid}/send")
async def send_group_message(
  gid: str,
  body: GroupSend,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Send while open; host acceptance serializes with membership and deletion."""
  require_nondelegated_owner_control(principal)
  app = _require_owner_or_common_app(db, principal)
  _validate_gid(gid)
  text = body.text.strip()
  attachment = _validate_attachment(body.attachment)
  _validate_text_or_attachment(text, attachment, "Message text is invalid.")
  reply_to = _validate_reply_to(body.reply_to)
  identity = _load_identity()
  message_id = str(uuid.uuid4())
  sent_at = time.time()

  async with _group_lock(gid):
    group = _load_host_group(gid)
    if group is not None:
      _require_open(group)
      original = _post_envelope(
        identity, gid, _own_host(), message_id=message_id, text=text,
        sent_at=sent_at, attachment=attachment, reply_to=reply_to,
      )
      delivered = await _host_accept_post(
        db, app, group, original=original,
        message_id=message_id, author=_own_host(),
        author_handle=identity.get("handle") or _own_host(),
        text=text, sent_at=sent_at, attachment=attachment, reply_to=reply_to,
      )
      failed = [h for h, ok in delivered.items() if not ok]
      return {"status": "delivered", "id": message_id, "failed_members": failed}
    meta = _load_group_meta(app, gid)
    if meta is None:
      raise HTTPException(status_code=404, detail="Unknown group.")
    _require_open(meta)
    _require_accepted_group(meta)
    envelope = _post_envelope(
      identity, gid, meta["host"], message_id=message_id, text=text,
      sent_at=sent_at, attachment=attachment, reply_to=reply_to,
    )

  # Do not hold a member lock while contacting its host: a simultaneous host
  # deletion needs to deliver its tombstone back here without a lock cycle.
  ok = await _deliver(meta["host"], envelope)
  async with _group_lock(gid):
    current = _load_group_meta(app, gid)
    if current is None:
      raise HTTPException(status_code=404, detail="Unknown group.")
    # A host receipt can precede a closure notice arriving locally. Keep that
    # already-accepted message and the tombstone; only failed sends are refused.
    if not ok:
      _require_open(current)
    record = {
      "id": message_id, "gid": gid, "author": _own_host(),
      "author_handle": identity.get("handle") or _own_host(), "text": text,
      "sent_at": sent_at, "dir": "out",
      "status": "delivered" if ok else "failed",
    }
    if reply_to is not None:
      record["reply_to"] = reply_to
    await _store_group_message(app, gid, record, attachment)
  detail = None if ok else f"The group host {meta['host']} could not be reached."
  return {
    "status": "delivered" if ok else "failed", "id": message_id, "detail": detail,
    **({"group_deleted": True} if current.get("deleted_at") else {}),
  }


@router.post("/{gid}/members")
async def add_group_member(
  gid: str,
  body: AddMember,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Add one deployment, or retry its current roster delivery, without history."""
  require_nondelegated_owner_control(principal)
  app = _require_owner_or_common_app(db, principal)
  _validate_gid(gid)
  host = body.host.strip().lower()
  if not _valid_host(host):
    raise HTTPException(status_code=400, detail="Invalid member address.")
  async with _group_lock(gid):
    group = _load_host_group(gid)
    if group is None:
      raise HTTPException(status_code=404, detail="Only the group's host can add members.")
    _require_open(group)
    already_member = host in group["members"]
    reinvited = False
    if not already_member:
      if len(group["members"]) >= MAX_GROUP_MEMBERS:
        raise HTTPException(status_code=400, detail="The group is full.")
      entry = {
        "handle": (body.handle or "")[:MAX_NAME_CHARS],
        "invited_at": time.time(), "status": "invited",
        "invitation_id": str(uuid.uuid4()),
        "invitation_version": 1,
      }
      try:
        actor = await _fetch_actor(host)
        entry["handle"] = str(actor.get("handle") or entry["handle"])[:MAX_NAME_CHARS]
      except HTTPException:
        pass  # an unreachable member can still receive a deliberate retry
      group["members"][host] = entry
      atomic_write(_host_group_path(gid), json.dumps(group, indent=2))
    elif group["members"][host].get("status") == "declined":
      group["members"][host].update(
        status="invited", invited_at=time.time(),
        invitation_id=str(uuid.uuid4()),
        invitation_version=int(
          group["members"][host].get("invitation_version") or 1
        ) + 1,
      )
      atomic_write(_host_group_path(gid), json.dumps(group, indent=2))
      reinvited = True
    members = _members_snapshot(group)
    await _store_group_meta(app, gid, {"members": members})
    identity = _load_identity()
    delivered = await _fan_out(group, {
      member: _added_envelope(identity, group, member)
      for member, entry in group["members"].items()
      if member != _own_host() and entry.get("status") != "declined"
    })
    return {
      "status": (
        "reinvited" if reinvited else "already_member" if already_member else "added"
      ),
      "members": members, "delivered": delivered,
    }


@router.delete("/{gid}")
async def delete_group(
  gid: str,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Close the host-owned group durably; never erase a member's local history."""
  require_nondelegated_owner_control(principal)
  app = _require_owner_or_common_app(db, principal)
  _validate_gid(gid)
  async with _group_lock(gid):
    group = _load_host_group(gid)
    if group is None:
      raise HTTPException(status_code=404, detail="Only the group's host can delete it.")
    if not group.get("deleted_at"):
      group["deleted_at"] = time.time()
      atomic_write(_host_group_path(gid), json.dumps(group, indent=2))
    await _store_group_meta(app, gid, {
      "deleted_at": group["deleted_at"], "unread": 0,
    })
    identity = _load_identity()
    delivered = await _fan_out(group, {
      member: _signed({
        "v": 0, "type": "group_deleted", "id": str(uuid.uuid4()),
        "from": _own_host(), "to": member, "gid": gid,
        "group_name": group["name"], "sent_at": time.time(),
      }, identity)
      for member in group["members"] if member != _own_host()
    })
    return {"status": "deleted", "delivered": delivered}
