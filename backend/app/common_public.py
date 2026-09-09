"""Shared public directory/board service for personal and social hosts.

The on-disk schema is the existing Common schema rooted at
``<data_dir>/common``:

* ``directory.json``
* ``board/<post-id>.json``
* ``board-media/<post-id>.{jpg,png,webp}``
* ``peers/*.json`` (bounded remote actor-key cache)

No post, reply, directory entry, or media object is expired or pruned.  The
only short-lived data is replay metadata embedded in a post record so an exact
reaction retry cannot reverse the first request. Mutations use local and
cross-process file locks, and every installed file is written atomically.
"""

from __future__ import annotations

import hashlib
import json
import fcntl
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from slowapi import Limiter

from app.common_protocol import (
  ATTACHMENT_MIME_EXT,
  CLOCK_SKEW_S,
  MAX_BIO_CHARS,
  MAX_NAME_CHARS,
  MAX_REPLY_TEXT_CHARS,
  ActorVerifier,
  canonical,
  read_envelope,
  valid_host,
  valid_id,
  validate_attachment,
  validate_text_or_attachment,
)
from app.storage_io import atomic_write

BOARD_PAGE_LIMIT = 50
BOARD_REPLY_LIMIT = 200
BOARD_LIKE_LIMIT = 2000
DIRECTORY_LIMIT = 2000
# A host never silently deletes public/user data.  These admission ceilings
# bound durable abuse instead: an operator can raise them after provisioning
# more storage, while existing imported records remain readable at any size.
BOARD_POST_LIMIT = 10_000
# Verification accepts timestamps up to one skew window in the future and
# one in the past. Retain a token for both windows from first receipt, so it
# cannot expire while that same signed envelope is still admissible.
REACTION_REPLAY_TTL_S = 2 * CLOCK_SKEW_S
# Bound per-post metadata; saturation rejects new reactions rather than
# discarding live tokens and making earlier requests replayable.
REACTION_REPLAY_LIMIT = 2048


def _request_peer(request: Request) -> str:
  """Use the real TCP peer, never a caller-spoofable forwarding header."""
  return request.client.host if request.client else "unknown"


class CommonPublicStore:
  """The canonical Common public-store implementation."""

  def __init__(self, data_dir: str | Path | Callable[[], str | Path]):
    self._data_dir = data_dir
    self._directory_lock = threading.Lock()
    self._board_lock = threading.Lock()

  def data_dir(self) -> Path:
    value = self._data_dir() if callable(self._data_dir) else self._data_dir
    return Path(value)

  def common_dir(self) -> Path:
    path = self.data_dir() / "common"
    path.mkdir(parents=True, exist_ok=True)
    return path

  def initialize(self) -> None:
    """Create only public storage directories; no network or owner state."""
    self.board_dir()
    self.board_media_dir()
    (self.common_dir() / "peers").mkdir(parents=True, exist_ok=True)

  @contextmanager
  def _mutation_lock(self, local_lock: threading.Lock, name: str):
    """Serialize both threads and the personal/sidecar process boundary."""
    with local_lock:
      lock_dir = self.common_dir() / ".locks"
      lock_dir.mkdir(parents=True, exist_ok=True)
      with (lock_dir / f"{name}.lock").open("a+b") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
          yield
        finally:
          fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

  def directory_path(self) -> Path:
    return self.common_dir() / "directory.json"

  def board_dir(self) -> Path:
    path = self.common_dir() / "board"
    path.mkdir(parents=True, exist_ok=True)
    return path

  def board_media_dir(self) -> Path:
    path = self.common_dir() / "board-media"
    path.mkdir(parents=True, exist_ok=True)
    return path

  def board_media_path(self, post_id: str, mime: str) -> Path:
    return self.board_media_dir() / f"{post_id}.{ATTACHMENT_MIME_EXT[mime]}"

  @staticmethod
  def find_image(directory: Path, stem: str) -> tuple[Path, str] | None:
    for mime, extension in ATTACHMENT_MIME_EXT.items():
      path = directory / f"{stem}.{extension}"
      if path.is_file():
        return path, mime
    return None

  @staticmethod
  def serve_image(found: tuple[Path, str] | None) -> FileResponse:
    if found is None:
      raise HTTPException(status_code=404, detail="Board image not found.")
    path, mime = found
    return FileResponse(
      str(path), media_type=mime,
      headers={"X-Content-Type-Options": "nosniff"},
    )

  @staticmethod
  def _load_object(path: Path) -> dict:
    if not path.is_file():
      return {}
    try:
      value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
      raise HTTPException(status_code=500, detail="Public data record is invalid.") from exc
    if not isinstance(value, dict):
      raise HTTPException(status_code=500, detail="Public data record is invalid.")
    return value

  def search_directory(self, query: str = "") -> dict:
    entries = self._load_object(self.directory_path())
    needle = query.strip().lower()
    results = []
    for host, entry in entries.items():
      if not isinstance(host, str) or not isinstance(entry, dict):
        continue
      handle = entry.get("handle") if isinstance(entry.get("handle"), str) else ""
      bio = entry.get("bio") if isinstance(entry.get("bio"), str) else ""
      if needle and needle not in f"{handle} {host} {bio}".lower():
        continue
      result = {"host": host}
      if "handle" in entry:
        result["handle"] = handle
      if "bio" in entry:
        result["bio"] = bio
      results.append(result)
    results.sort(key=lambda entry: (entry.get("handle") or entry["host"]).lower())
    return {"users": results[:200]}

  def register(self, host: str, handle: str, bio: str) -> dict:
    with self._mutation_lock(self._directory_lock, "directory"):
      path = self.directory_path()
      entries = self._load_object(path)
      if host not in entries and len(entries) >= DIRECTORY_LIMIT:
        raise HTTPException(status_code=507, detail="Directory is full.")
      entries[host] = {
        "handle": handle,
        "bio": bio,
        "registered_at": time.time(),
      }
      atomic_write(path, json.dumps(entries, indent=2))
    return {"status": "registered"}

  def read_board(
    self, limit: int, before: float | None, viewer: str | None = None,
  ) -> list[dict]:
    posts = []
    for file in self.board_dir().glob("*.json"):
      try:
        raw = self._load_object(file)
      except HTTPException:
        # Preserve browsing when one independently-addressed record is damaged.
        continue
      post = dict(raw)
      if before is not None and post.get("created_at", 0) >= before:
        continue
      likes = post.pop("likes", {})
      if not isinstance(likes, dict):
        likes = {}
      post["like_count"] = len(likes)
      if viewer is not None:
        post["liked"] = viewer in likes
      replies = post.pop("replies", [])
      if not isinstance(replies, list):
        replies = []
      post["reply_count"] = len(replies)
      # Replay state is host-internal metadata, never part of the feed.
      post.pop("_reaction_replays", None)
      posts.append(post)
    posts.sort(key=lambda post: post.get("created_at", 0), reverse=True)
    return posts[:limit]

  def store_post(
    self, post: dict, attachment: tuple[dict, bytes] | None = None,
  ) -> bool:
    """Store once by stable id; return False without changing a duplicate."""
    with self._mutation_lock(self._board_lock, "board"):
      path = self.board_dir() / f"{post['id']}.json"
      if path.is_file():
        return False
      if sum(1 for _ in self.board_dir().glob("*.json")) >= BOARD_POST_LIMIT:
        raise HTTPException(status_code=507, detail="Board is full.")
      record = dict(post)
      if attachment is not None:
        wire, data = attachment
        atomic_write(self.board_media_path(post["id"], wire["mime"]), data)
        record["attachment"] = {
          "mime": wire["mime"], "w": wire["w"], "h": wire["h"],
        }
      atomic_write(path, json.dumps(record))
      return True

  def toggle_like(
    self, post_id: str, host: str, *, replay_token: str | None = None,
  ) -> dict:
    """Toggle a like once, making an exact signed-envelope retry idempotent."""
    with self._mutation_lock(self._board_lock, "board"):
      path = self.board_dir() / f"{post_id}.json"
      if not path.is_file():
        raise HTTPException(status_code=404, detail="Unknown post.")
      post = self._load_object(path)
      likes = post.setdefault("likes", {})
      if not isinstance(likes, dict):
        likes = {}
        post["likes"] = likes
      now = time.time()
      if replay_token is not None:
        journal = post.setdefault("_reaction_replays", {})
        if not isinstance(journal, dict):
          journal = {}
          post["_reaction_replays"] = journal
        live = {
          token: expiry for token, expiry in journal.items()
          if isinstance(token, str)
          and isinstance(expiry, (int, float)) and not isinstance(expiry, bool)
          and expiry >= now
        }
        if replay_token in live:
          return {"status": "ok", "likes": len(likes), "liked": host in likes}
        if len(live) >= REACTION_REPLAY_LIMIT:
          raise HTTPException(status_code=429, detail="Reaction replay journal is full.")
        live[replay_token] = now + REACTION_REPLAY_TTL_S
        post["_reaction_replays"] = live
      if host not in likes and len(likes) >= BOARD_LIKE_LIMIT:
        raise HTTPException(status_code=507, detail="Post reaction limit reached.")
      if host in likes:
        del likes[host]
      else:
        likes[host] = now
      atomic_write(path, json.dumps(post))
      return {"status": "ok", "likes": len(likes), "liked": host in likes}

  def add_reply(
    self, post_id: str, reply_id: str, host: str, handle: str,
    text: str, created_at: float,
  ) -> dict:
    with self._mutation_lock(self._board_lock, "board"):
      path = self.board_dir() / f"{post_id}.json"
      if not path.is_file():
        raise HTTPException(status_code=404, detail="Unknown post.")
      post = self._load_object(path)
      replies = post.get("replies")
      if not isinstance(replies, list):
        replies = []
        post["replies"] = replies
      if any(isinstance(reply, dict) and reply.get("id") == reply_id for reply in replies):
        return {"status": "ok", "reply_count": len(replies)}
      if len(replies) >= BOARD_REPLY_LIMIT:
        raise HTTPException(status_code=507, detail="Post reply limit reached.")
      replies.append({
        "id": reply_id,
        "host": host,
        "handle": handle,
        "text": text,
        "created_at": created_at,
      })
      atomic_write(path, json.dumps(post))
      return {"status": "ok", "reply_count": len(replies)}

  def get_replies(self, post_id: str) -> dict:
    path = self.board_dir() / f"{post_id}.json"
    if not path.is_file():
      raise HTTPException(status_code=404, detail="Unknown post.")
    post = self._load_object(path)
    replies = post.get("replies")
    if not isinstance(replies, list):
      replies = []
    return {
      "replies": sorted(
        replies,
        key=lambda reply: reply.get("created_at", 0) if isinstance(reply, dict) else 0,
      )
    }


def create_public_router(
  store: CommonPublicStore, verifier: ActorVerifier, *, prefix: str = "/api/common",
) -> tuple[APIRouter, Limiter]:
  """Build the exact public-host surface shared by both runtimes."""
  router = APIRouter(prefix=prefix, tags=["common-public"])
  # One limiter per router instance keeps personal and sidecar ingress scopes
  # independent and prevents repeated app-factory construction from stacking
  # duplicate decorators under SlowAPI's module/function registry keys.
  limiter = Limiter(key_func=_request_peer, key_style="endpoint")
  write_limit = limiter.shared_limit(
    "120/minute", scope="common-public-write-ingress",
  )

  @router.get("/directory")
  def search_directory(q: str = ""):
    return store.search_directory(q)

  @router.post("/directory")
  @write_limit
  async def register_in_directory(request: Request):
    envelope = await read_envelope(request)
    if envelope.get("v") != 0 or envelope.get("type") != "register":
      raise HTTPException(status_code=400, detail="Unsupported envelope type.")
    actor = await verifier.verify_envelope(envelope)
    handle = envelope.get("handle") or actor.get("handle") or ""
    bio = envelope.get("bio") or ""
    if (
      not isinstance(handle, str) or len(handle) > MAX_NAME_CHARS
      or not isinstance(bio, str) or len(bio) > MAX_BIO_CHARS
    ):
      raise HTTPException(status_code=400, detail="Directory profile is invalid.")
    return store.register(envelope["from"], handle, bio)

  @router.get("/board")
  def get_board(
    limit: int = 30, before: float | None = None, viewer: str | None = None,
  ):
    if viewer is not None and not valid_host(viewer):
      viewer = None
    return {
      "posts": store.read_board(
        min(max(limit, 1), BOARD_PAGE_LIMIT), before, viewer,
      )
    }

  @router.get("/board/media/{post_id}")
  def get_board_media(post_id: str):
    if not valid_id(post_id):
      raise HTTPException(status_code=400, detail="Post id is invalid.")
    return store.serve_image(store.find_image(store.board_media_dir(), post_id))

  @router.get("/board/{post_id}/replies")
  def get_board_replies(post_id: str):
    if not valid_id(post_id):
      raise HTTPException(status_code=400, detail="Post id is invalid.")
    return store.get_replies(post_id)

  @router.post("/board/react")
  @write_limit
  async def react_to_board(request: Request):
    envelope = await read_envelope(request)
    if envelope.get("v") != 0 or envelope.get("type") != "board_react":
      raise HTTPException(status_code=400, detail="Unsupported envelope type.")
    post_id = envelope.get("post_id")
    if not valid_id(post_id):
      raise HTTPException(status_code=400, detail="Post id is invalid.")
    await verifier.verify_envelope(envelope)
    replay_token = hashlib.sha256(canonical(envelope)).hexdigest()
    return store.toggle_like(
      post_id, envelope["from"], replay_token=replay_token,
    )

  @router.post("/board/reply")
  @write_limit
  async def reply_to_board(request: Request):
    envelope = await read_envelope(request)
    if envelope.get("v") != 0 or envelope.get("type") != "board_reply":
      raise HTTPException(status_code=400, detail="Unsupported envelope type.")
    post_id = envelope.get("post_id")
    reply_id = envelope.get("id")
    if not valid_id(post_id):
      raise HTTPException(status_code=400, detail="Post id is invalid.")
    if not valid_id(reply_id):
      raise HTTPException(status_code=400, detail="Reply id is invalid.")
    text = envelope.get("text")
    if (
      not isinstance(text, str) or not text.strip()
      or len(text) > MAX_REPLY_TEXT_CHARS
    ):
      raise HTTPException(status_code=400, detail="Reply text is invalid.")
    actor = await verifier.verify_envelope(envelope)
    return store.add_reply(
      post_id, reply_id, envelope["from"], actor.get("handle") or "",
      text, envelope["sent_at"],
    )

  @router.post("/board")
  @write_limit
  async def post_to_board(request: Request):
    envelope = await read_envelope(request)
    if envelope.get("v") != 0 or envelope.get("type") != "board_post":
      raise HTTPException(status_code=400, detail="Unsupported envelope type.")
    attachment = validate_attachment(envelope.get("attachment"))
    text = envelope.get("text")
    validate_text_or_attachment(text, attachment, "Post text is invalid.")
    post_id = envelope.get("id")
    if not valid_id(post_id):
      raise HTTPException(status_code=400, detail="Post id is invalid.")
    actor = await verifier.verify_envelope(envelope)
    store.store_post({
      "id": post_id,
      "host": envelope["from"],
      "handle": actor.get("handle") or "",
      "text": text,
      "created_at": envelope["sent_at"],
      "replies": [],
    }, attachment)
    return {"status": "posted"}

  return router, limiter


__all__ = [
  "BOARD_LIKE_LIMIT", "BOARD_PAGE_LIMIT", "BOARD_POST_LIMIT", "BOARD_REPLY_LIMIT",
  "CommonPublicStore", "DIRECTORY_LIMIT", "REACTION_REPLAY_LIMIT",
  "REACTION_REPLAY_TTL_S", "create_public_router",
]
