"""Common federation — group chats (protocol common/0, group extension).

A group lives on its creator's instance, the **group host**. The host owns the
authoritative membership record; members keep their own copy of every message,
exactly like 1:1 conversations. Three signed envelope types extend the
protocol, all delivered to `/api/common/groups/inbox`:

- `group_added`  host → member    you were added; carries group metadata
- `group_post`   member → host    a member's message for the group
- `group_message` host → member   the host's fan-out relay of a post

A relayed `group_message` is signed by the *host* and carries the complete
author-signed `group_post` as `original`. Members verify both signatures, so
the host controls membership and fan-out without being able to forge another
member's authorship. Nothing about a group ever touches a third instance.

Owner surface (owner JWT or the Common app's scoped token):
  POST /api/common/groups                     create a group + invite members
  POST /api/common/groups/{gid}/send          send a message to the group
  POST /api/common/groups/{gid}/members       add a member (host only)

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

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app import fs_locks, models, push
from app.database import get_db
from app.deps import Principal, get_principal
from app.routes.common import (
  MAX_NAME_CHARS,
  OUTBOUND_TIMEOUT_S,
  _app_data_dir,
  _bump_version,
  _common_app,
  _common_dir,
  _fetch_actor,
  _load_identity,
  _message_preview,
  _own_host,
  _peer_base_url,
  _read_envelope,
  _require_owner_or_common_app,
  _sign,
  _valid_host,
  _validate_attachment,
  _validate_reply_to,
  _validate_text_or_attachment,
  _verify_peer_envelope,
  _write_app_attachment,
)
from app.storage_io import atomic_write

router = APIRouter(prefix="/api/common/groups", tags=["common"])

MAX_GROUP_MEMBERS = 64
_GID_RE = re.compile(r"^[a-f0-9-]{8,64}$")


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
) -> None:
  async with fs_locks.app_storage_lock(app.id):
    group = _group_dir(app, gid)
    if attachment is not None:
      record["attachment"] = _write_app_attachment(
        group, record["id"], attachment
      )
    msgs = group / "msgs"
    msgs.mkdir(parents=True, exist_ok=True)
    atomic_write(msgs / f"{record['id']}.json", json.dumps(record))
    meta_path = group / "meta.json"
    meta = json.loads(meta_path.read_text()) if meta_path.is_file() else {}
    meta.update(
      gid=gid,
      last_text=_message_preview(record["text"]),
      last_at=record["sent_at"],
      last_from_handle=record.get("author_handle") or record.get("author") or "",
      last_dir=record["dir"],
    )
    if record["dir"] == "in":
      meta["unread"] = int(meta.get("unread") or 0) + 1
    atomic_write(meta_path, json.dumps(meta))
    _bump_version(app)


def _members_snapshot(group: dict) -> list[dict]:
  # Privacy contract: only host + handle circulate; display names never do.
  return [
    {"host": h, "handle": m.get("handle") or ""}
    for h, m in group["members"].items()
  ]


# ── outbound delivery ───────────────────────────────────────────────────────

async def _deliver(host: str, envelope: dict) -> bool:
  try:
    async with httpx.AsyncClient(timeout=OUTBOUND_TIMEOUT_S) as client:
      response = await client.post(
        f"{_peer_base_url(host)}/api/common/groups/inbox", json=envelope
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
  return _signed({
    "v": 0,
    "type": "group_added",
    "id": str(uuid.uuid4()),
    "from": _own_host(),
    "to": member,
    "gid": group["id"],
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
    if member not in (_own_host(), author)
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
  """Accept one signed group envelope from a peer instance."""
  envelope = await _read_envelope(request)
  if envelope.get("v") != 0:
    raise HTTPException(status_code=400, detail="Unsupported envelope version.")
  kind = envelope.get("type")
  gid = envelope.get("gid")
  if not isinstance(gid, str) or not _GID_RE.fullmatch(gid):
    raise HTTPException(status_code=400, detail="Group id is invalid.")
  app = _common_app(db)

  if kind == "group_added":
    actor = await _verify_peer_envelope(envelope)
    name = str(envelope.get("group_name") or "Group")[:MAX_NAME_CHARS]
    members = envelope.get("members")
    if not isinstance(members, list) or len(members) > MAX_GROUP_MEMBERS:
      raise HTTPException(status_code=400, detail="Member list is invalid.")
    await _store_group_meta(app, gid, {
      "gid": gid,
      "name": name,
      "host": envelope["from"],
      "members": [
        {
          "host": str(m.get("host") or "")[:255],
          "handle": str(m.get("handle") or "")[:MAX_NAME_CHARS],
        }
        for m in members if isinstance(m, dict)
      ],
    })
    adder = f"@{actor['handle']}" if actor.get("handle") else envelope["from"]
    owner = db.query(models.Owner).first()
    if owner is not None:
      try:
        push.notify_owner(
          db, owner.id,
          title=f"Added to {name}",
          body=f"{adder} added you to a group.",
          source_type="app", source_id=str(app.id),
          target=f"/shell/?app={app.id}",
        )
      except Exception:
        pass
    return {"status": "added"}

  if kind not in ("group_post", "group_message"):
    raise HTTPException(status_code=400, detail="Unsupported envelope type.")
  message_id = envelope.get("id")
  if not isinstance(message_id, str) or not _GID_RE.fullmatch(message_id):
    raise HTTPException(status_code=400, detail="Message id is invalid.")

  if kind == "group_post":
    # We are the group host: verify the sender instance, then membership.
    group = _load_host_group(gid)
    if group is None:
      raise HTTPException(status_code=404, detail="Unknown group.")
    await _verify_peer_envelope(envelope)
    sender = envelope["from"]
    member = group["members"].get(sender)
    if member is None:
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

  if kind == "group_message":
    # We are a member: verify the host relay, then its author-signed original.
    meta_path = _group_dir(app, gid) / "meta.json"
    if not meta_path.is_file():
      return {"status": "unknown_group"}
    meta = json.loads(meta_path.read_text())
    if envelope.get("from") != meta.get("host"):
      raise HTTPException(status_code=403, detail="Relay is not from the group host.")
    await _verify_peer_envelope(envelope)
    original = envelope.get("original")
    if (
      not isinstance(original, dict)
      or original.get("type") != "group_post"
      or original.get("gid") != gid
      or original.get("id") != message_id
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
    existing = _group_dir(app, gid) / "msgs" / f"{message_id}.json"
    if existing.is_file():
      return {"status": "duplicate"}
    author = original["from"]
    author_handle = str(original_actor.get("handle") or author)[:MAX_NAME_CHARS]
    record = {
      "id": message_id, "gid": gid, "author": author,
      "author_handle": author_handle, "text": text,
      "sent_at": original["sent_at"], "dir": "in", "status": "delivered",
    }
    if reply_to is not None:
      record["reply_to"] = reply_to
    await _store_group_message(app, gid, record, attachment)
    _notify_group_message(
      db, app, meta.get("name") or "Group", author_handle, text
    )
    return {"status": "delivered"}

  raise HTTPException(status_code=400, detail="Unsupported envelope type.")


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


@router.post("")
async def create_group(
  body: CreateGroup,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Create a group hosted on this instance and invite its first members."""
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
    },
  }
  for host in member_hosts:
    entry = {"handle": "", "joined_at": time.time()}
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
  atomic_write(_host_group_path(gid), json.dumps(group, indent=2))

  await _store_group_meta(app, gid, {
    "gid": gid, "name": name, "host": _own_host(),
    "members": _members_snapshot(group),
    "last_at": time.time(), "last_text": "", "unread": 0,
  })
  invited = await _fan_out(group, {
    host: _added_envelope(identity, group, host) for host in member_hosts
  })
  return {"status": "created", "gid": gid, "invited": invited}


@router.post("/{gid}/send")
async def send_group_message(
  gid: str,
  body: GroupSend,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Send a message to a group, as host (fan out) or member (post to host)."""
  app = _require_owner_or_common_app(db, principal)
  if not _GID_RE.fullmatch(gid):
    raise HTTPException(status_code=400, detail="Group id is invalid.")
  text = body.text.strip()
  attachment = _validate_attachment(body.attachment)
  _validate_text_or_attachment(text, attachment, "Message text is invalid.")
  reply_to = _validate_reply_to(body.reply_to)
  identity = _load_identity()
  message_id = str(uuid.uuid4())
  sent_at = time.time()

  group = _load_host_group(gid)
  if group is not None:
    # The host authors the same signed post shape it requires from members.
    original = _post_envelope(
      identity, gid, _own_host(), message_id=message_id, text=text,
      sent_at=sent_at, attachment=attachment, reply_to=reply_to,
    )
    delivered = await _host_accept_post(
      db, app, group, original=original,
      message_id=message_id, author=_own_host(),
      author_handle=identity.get("handle") or _own_host(),
      text=text, sent_at=sent_at,
      attachment=attachment, reply_to=reply_to,
    )
    failed = [h for h, ok in delivered.items() if not ok]
    return {"status": "delivered", "id": message_id, "failed_members": failed}

  # We are a member: our copy first, then post to the group host.
  meta_path = _group_dir(app, gid) / "meta.json"
  if not meta_path.is_file():
    raise HTTPException(status_code=404, detail="Unknown group.")
  meta = json.loads(meta_path.read_text())
  envelope = _post_envelope(
    identity, gid, meta["host"], message_id=message_id, text=text,
    sent_at=sent_at, attachment=attachment, reply_to=reply_to,
  )
  ok = await _deliver(meta["host"], envelope)
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
  return {"status": "delivered" if ok else "failed", "id": message_id, "detail": detail}


@router.post("/{gid}/members")
async def add_group_member(
  gid: str,
  body: AddMember,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Add a member to a group hosted here, and tell everyone."""
  app = _require_owner_or_common_app(db, principal)
  group = _load_host_group(gid)
  if group is None:
    raise HTTPException(
      status_code=404, detail="Only the group's host can add members."
    )
  host = body.host.strip().lower()
  if not _valid_host(host):
    raise HTTPException(status_code=400, detail="Invalid member address.")
  if host in group["members"]:
    return {"status": "already_member"}
  if len(group["members"]) >= MAX_GROUP_MEMBERS:
    raise HTTPException(status_code=400, detail="The group is full.")
  entry = {
    "handle": (body.handle or "")[:MAX_NAME_CHARS],
    "joined_at": time.time(),
  }
  try:
    actor = await _fetch_actor(host)
    entry["handle"] = actor.get("handle") or entry["handle"]
  except HTTPException:
    pass
  group["members"][host] = entry
  atomic_write(_host_group_path(gid), json.dumps(group, indent=2))
  await _store_group_meta(app, gid, {"members": _members_snapshot(group)})
  identity = _load_identity()
  # The new member gets the group; existing members get the refreshed roster.
  invited = await _fan_out(group, {
    member: _added_envelope(identity, group, member)
    for member in group["members"] if member != _own_host()
  })
  return {"status": "added", "delivered": invited}
