"""Append-only provider-session -> chat identity map (subagent observability).

Deep module over the ``chat_session_links`` table: a caller hands it one
sighting — a ``(provider, session_id, chat_id)`` triple the runner just
persisted — and this module owns everything about turning that into a durable
record. The insert-or-bump upsert, the naive-UTC timestamps, the never-delete
invariant, and the concurrent-insert race all live here. A caller (the SDK
runner's session-id persistence path) knows only "tell the map I saw this
session for this chat"; it never learns whether the row already existed, whether
this was a first sight, or how a re-sight is folded in.

Why a separate table from ``Chat.session_id``: that column tracks only the
CURRENT session and is wiped on a provider switch or a session reset, so it
cannot answer "which chat did session X belong to?" once the live pointer moves
on. This map never forgets — see ``models.ChatSessionLink``.
"""

from __future__ import annotations

import asyncio
import logging

from sqlalchemy.exc import IntegrityError

from app import models
from app.database import SessionLocal
from app.timeutil import now_naive_utc

log = logging.getLogger(__name__)


async def record_session_link_async(
  provider: str, session_id: str, chat_id: str
) -> None:
  """Records a session->chat sighting from an async context, safely.

  The SDK runners call this from the event loop while a turn streams. The write
  is a synchronous SQLite commit, so it runs in a worker thread
  (``asyncio.to_thread``) on its OWN short-lived session — never the runner's
  request-local ``db`` (which ``chat.py`` deliberately ``close()``s before the
  long provider run to free the loop, and which the following unguarded
  ``Chat`` query reuses). Doing the commit inline on that shared session would
  both block the loop under WAL write contention and, on a non-Integrity commit
  failure, leave it pending-rollback so the later ``Chat.session_id`` save
  raises. Its own session isolates both failure modes: a failure here is logged
  and dropped, and the resume-continuity save is untouched.
  """
  if not provider or not session_id or not chat_id:
    return
  try:
    await asyncio.to_thread(_record_isolated, provider, session_id, chat_id)
  except Exception:
    log.warning(
      "session-link recording failed provider=%s session_id=%s chat_id=%s",
      provider, session_id, chat_id, exc_info=True,
    )


def _record_isolated(provider: str, session_id: str, chat_id: str) -> None:
  """Opens a dedicated session, records the link, and closes it."""
  with SessionLocal() as db:
    record_session_link(db, provider, session_id, chat_id)


def record_session_link(
  db, provider: str, session_id: str, chat_id: str
) -> None:
  """Records that ``session_id`` (under ``provider``) belongs to ``chat_id``.

  Idempotent upsert: the first sighting inserts a row stamped
  ``first_seen_at == last_seen_at``; every later sighting of the same
  ``(provider, session_id)`` only advances ``last_seen_at`` (monotonically — a
  clock-skewed older stamp never moves it backwards). The mapping is
  append-only — a re-sight never rewrites ``chat_id``; a re-sight that arrives
  with a DIFFERENT ``chat_id`` is a contract violation (a session id is unique
  within a provider) and is logged and ignored rather than silently accepted.

  No-ops on any missing argument so a caller on a first turn (no session id
  established yet) can invoke it unconditionally. Commits the ``db`` session it
  is handed. Prefer ``record_session_link_async`` from an event-loop context —
  this synchronous form is for a session the caller already owns off the loop.
  """
  if db is None or not provider or not session_id or not chat_id:
    return
  now = now_naive_utc()
  existing = db.get(models.ChatSessionLink, (provider, session_id))
  if existing is not None:
    if existing.chat_id != chat_id:
      log.warning(
        "session-link conflict: (%s, %s) already maps to chat %s, not %s — "
        "keeping the first binding", provider, session_id,
        existing.chat_id, chat_id,
      )
      return
    if existing.last_seen_at is None or existing.last_seen_at < now:
      existing.last_seen_at = now
    db.commit()
    return
  db.add(models.ChatSessionLink(
    provider=provider,
    session_id=session_id,
    chat_id=chat_id,
    first_seen_at=now,
    last_seen_at=now,
  ))
  try:
    db.commit()
  except IntegrityError:
    # A concurrent sighting inserted the same (provider, session_id) first.
    # Roll back our losing insert and fold this sighting into the winner as a
    # re-sight — the append-only invariant holds whichever writer won the PK.
    db.rollback()
    existing = db.get(models.ChatSessionLink, (provider, session_id))
    if existing is not None:
      existing.last_seen_at = now
      db.commit()
