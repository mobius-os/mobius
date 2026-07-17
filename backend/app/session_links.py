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

from sqlalchemy.exc import IntegrityError

from app import models
from app.timeutil import now_naive_utc


def record_session_link(
  db, provider: str, session_id: str, chat_id: str
) -> None:
  """Records that ``session_id`` (under ``provider``) belongs to ``chat_id``.

  Idempotent upsert: the first sighting inserts a row stamped
  ``first_seen_at == last_seen_at``; every later sighting of the same
  ``(provider, session_id)`` only advances ``last_seen_at``. The mapping is
  append-only — a re-sight never rewrites ``chat_id`` and nothing here deletes.

  No-ops on any missing argument so a caller on a first turn (no session id
  established yet) can invoke it unconditionally. Commits the ``db`` session it
  is handed, mirroring the runner's own session-id persistence discipline.
  """
  if db is None or not provider or not session_id or not chat_id:
    return
  now = now_naive_utc()
  existing = db.get(models.ChatSessionLink, (provider, session_id))
  if existing is not None:
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
