"""In-memory live rooms: tiny broadcast channels that an app's frames share.

A room is named by the app (``app_id``) and a free-form room name, so apps
never see each other's traffic. The hub keeps no state beyond the connected
members; nothing is stored. It is a relay, not a referee: apps decide what the
messages mean (Ball Game's host page, a game's shooter-authoritative hits).

Limits keep one noisy app from costing the owner their instance: members per
room, rooms per app, total connections, message size, and a per-connection
message budget. A slow receiver drops frames instead of stalling the room.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field

MAX_MEMBERS_DEFAULT = 8
MAX_MEMBERS_CEILING = 16
MAX_ROOMS_PER_APP = 32
MAX_CONNECTIONS = 256
MAX_MESSAGE_BYTES = 16 * 1024
MAX_NAME_CHARS = 24
OUTBOX = 256
# Token bucket: sustained messages per second and the burst allowed.
RATE_PER_SECOND = 90.0
RATE_BURST = 180.0


class LiveError(Exception):
  def __init__(self, code: str):
    super().__init__(code)
    self.code = code


@dataclass
class Member:
  id: str
  name: str
  room: "Room"
  outbox: "asyncio.Queue[str]" = field(default_factory=lambda: asyncio.Queue(OUTBOX))
  tokens: float = RATE_BURST
  stamp: float = field(default_factory=time.monotonic)
  dropped: int = 0

  def offer(self, frame: str) -> None:
    try:
      self.outbox.put_nowait(frame)
    except asyncio.QueueFull:
      self.dropped += 1   # lossy by design: newer state supersedes

  def allow(self) -> bool:
    now = time.monotonic()
    self.tokens = min(RATE_BURST, self.tokens + (now - self.stamp) * RATE_PER_SECOND)
    self.stamp = now
    if self.tokens < 1:
      return False
    self.tokens -= 1
    return True


@dataclass
class Room:
  app_id: int
  name: str
  max_members: int
  members: dict[str, Member] = field(default_factory=dict)

  def roster(self) -> list[dict]:
    return [{"id": m.id, "name": m.name} for m in self.members.values()]


class Hub:
  def __init__(self) -> None:
    self.rooms: dict[tuple[int, str], Room] = {}
    self.connections = 0

  def join(self, app_id: int, room_name: str, name: str, max_members: int | None) -> Member:
    if self.connections >= MAX_CONNECTIONS:
      raise LiveError("busy")
    key = (app_id, room_name)
    room = self.rooms.get(key)
    if room is None:
      if sum(1 for k in self.rooms if k[0] == app_id) >= MAX_ROOMS_PER_APP:
        raise LiveError("busy")
      cap = max(2, min(int(max_members or MAX_MEMBERS_DEFAULT), MAX_MEMBERS_CEILING))
      room = self.rooms[key] = Room(app_id, room_name, cap)
    if len(room.members) >= room.max_members:
      raise LiveError("full")
    clean = "".join(ch for ch in (name or "") if ch.isprintable()).strip()[:MAX_NAME_CHARS] or "Player"
    member = Member(secrets.token_hex(4), clean, room)
    room.members[member.id] = member
    self.connections += 1
    member.offer(json.dumps({"t": "welcome", "you": {"id": member.id, "name": clean},
                             "members": room.roster(), "max": room.max_members}))
    self._presence(room, exclude=member.id)
    return member

  def leave(self, member: Member) -> None:
    room = member.room
    if room.members.pop(member.id, None) is None:
      return
    self.connections -= 1
    if not room.members:
      self.rooms.pop((room.app_id, room.name), None)
    else:
      self._presence(room)

  def _presence(self, room: Room, exclude: str | None = None) -> None:
    frame = json.dumps({"t": "presence", "members": room.roster()})
    for m in room.members.values():
      if m.id != exclude:
        m.offer(frame)

  def roster(self, member: Member) -> None:
    member.offer(json.dumps({"t": "presence", "members": member.room.roster()}))

  def relay(self, member: Member, data, to: list[str] | None) -> None:
    frame = json.dumps({"t": "msg", "from": member.id, "data": data}, separators=(",", ":"))
    room = member.room
    if to:
      for target in to:
        m = room.members.get(target)
        if m is not None and m is not member:
          m.offer(frame)
      return
    for m in room.members.values():
      if m is not member:
        m.offer(frame)


hub = Hub()
