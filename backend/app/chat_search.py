"""Full-text search over chat transcripts (drawer search).

Design: an SQLite FTS5 index over conversation prose — the owner's messages,
the assistant's replies, and chat titles. Tool output, thinking traces, and
`blocks` machinery are deliberately excluded: they live in their own tables,
they are the bulk of the bytes, and they are noise for "where did we talk
about X".

The index is DERIVED data, reconciled lazily at query time:

- `chat_search_docs` holds one plain-text row per indexed message (plus one
  title row per chat, ``msg_idx = -1``). `chat_search_fts` is an
  external-content FTS5 table over it, kept in sync by triggers on the docs
  table itself (the canonical FTS5 external-content pattern — the triggers
  watch OUR derived table, never `chats`).
- `chat_search_state` remembers, per chat, how many messages are indexed and
  the exact `chats.updated_at` text at index time. A search request first
  reconciles: any live chat whose `updated_at` no longer equals the remembered
  text gets its NEW messages appended (or a per-chat rebuild when history
  shrank or the title changed); index rows for deleted/purged chats are
  dropped. Chats whose timestamp moved without new content (run markers,
  streaming) cost one state-row update, not a re-tokenize.

Because reconciliation happens inside the search request there is no second
code path that could forget to update the index, nothing hooks into the turn
lifecycle, and a missing index (or this feature landing on an existing
instance) simply rebuilds itself on first use. Memory: FTS5 reads b-tree
pages from disk per query; nothing is cached in process memory. Only changed
chats ever have their `messages` JSON hydrated — reconcile compares
timestamps, not transcripts.

Writes here touch only the three `chat_search_*` tables — never
`Chat.messages` / `Chat.pending_messages` (those stay behind `chat_writer.py`).
"""

import re

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from app import models

# Private-use sentinels around FTS5 snippet matches. The API converts them to
# a JSON-friendly form; they can never collide with real transcript text.
_MARK_OPEN = "\ue000"
_MARK_CLOSE = "\ue001"

# Message roles whose `content` strings are conversation prose.
_PROSE_ROLES = ("user", "assistant")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_search_docs (
  id INTEGER PRIMARY KEY,
  chat_id TEXT NOT NULL,
  msg_idx INTEGER NOT NULL,
  ts INTEGER,
  role TEXT,
  text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chat_search_docs_chat
  ON chat_search_docs(chat_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chat_search_fts USING fts5(
  text,
  content='chat_search_docs',
  content_rowid='id',
  tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chat_search_docs_ai
  AFTER INSERT ON chat_search_docs BEGIN
    INSERT INTO chat_search_fts(rowid, text) VALUES (new.id, new.text);
  END;
CREATE TRIGGER IF NOT EXISTS chat_search_docs_ad
  AFTER DELETE ON chat_search_docs BEGIN
    INSERT INTO chat_search_fts(chat_search_fts, rowid, text)
      VALUES ('delete', old.id, old.text);
  END;
CREATE TABLE IF NOT EXISTS chat_search_state (
  chat_id TEXT PRIMARY KEY,
  indexed_count INTEGER NOT NULL,
  indexed_title TEXT NOT NULL,
  indexed_updated_at TEXT NOT NULL
);
"""

_schema_ready = False


def _ensure_schema(db: Session) -> None:
  global _schema_ready
  if _schema_ready:
    return
  # A pre-reveal index has a `chat_search_docs` without the `ts`/`role`
  # columns. This is DERIVED data that rebuilds itself, so drop the stale
  # search tables and recreate at the current schema rather than ALTER-dancing;
  # the reconcile in the same search request repopulates from `chats`. Dropping
  # the external-content FTS virtual table also removes its shadow tables, and
  # dropping the docs table removes the sync triggers defined on it.
  existing = db.execute(
    sql(
      "SELECT 1 FROM sqlite_master"
      " WHERE type = 'table' AND name = 'chat_search_docs'"
    )
  ).fetchone()
  if existing is not None:
    columns = [row[1] for row in db.execute(sql("PRAGMA table_info(chat_search_docs)"))]
    if "ts" not in columns:
      for statement in (
        "DROP TABLE IF EXISTS chat_search_fts",
        "DROP TABLE IF EXISTS chat_search_docs",
        "DROP TABLE IF EXISTS chat_search_state",
      ):
        db.execute(sql(statement))
      db.commit()
  for statement in _split_schema(_SCHEMA):
    db.execute(sql(statement))
  db.commit()
  _schema_ready = True


def _split_schema(schema: str) -> list[str]:
  """Split the DDL on statement boundaries, keeping trigger bodies intact."""
  statements, buf, in_trigger = [], [], False
  for line in schema.splitlines():
    stripped = line.strip()
    if not stripped:
      continue
    buf.append(line)
    if stripped.upper().startswith("CREATE TRIGGER"):
      in_trigger = True
    if stripped.endswith(";"):
      if in_trigger and stripped.upper() != "END;":
        continue
      statements.append("\n".join(buf))
      buf, in_trigger = [], False
  return statements


def _prose_docs(title: str, messages: list) -> list[tuple[int, object, object, str]]:
  """(msg_idx, ts, role, text) rows: title at -1 (ts/role None), then prose.

  `ts` and `role` are stored so a search result can point the drawer at the
  exact transcript row to reveal — the chat UI keys each message row as
  ``<role>-<ts>``. `ts` is unique within a chat, so it needs no message index.
  """
  docs: list[tuple[int, object, object, str]] = []
  if title and title.strip():
    docs.append((-1, None, None, title.strip()))
  for idx, message in enumerate(messages):
    if not isinstance(message, dict):
      continue
    role = message.get("role")
    if role not in _PROSE_ROLES:
      continue
    content = message.get("content")
    if isinstance(content, str) and content.strip():
      ts = message.get("ts")
      docs.append((idx, ts if isinstance(ts, int) else None, role, content))
  return docs


def _delete_chat_docs(db: Session, chat_id: str) -> None:
  db.execute(
    sql("DELETE FROM chat_search_docs WHERE chat_id = :cid"), {"cid": chat_id}
  )
  db.execute(
    sql("DELETE FROM chat_search_state WHERE chat_id = :cid"), {"cid": chat_id}
  )


def _write_docs(db: Session, chat_id: str, docs: list[tuple[int, object, object, str]]) -> None:
  for msg_idx, ts, role, doc_text in docs:
    db.execute(
      sql(
        "INSERT INTO chat_search_docs (chat_id, msg_idx, ts, role, text)"
        " VALUES (:cid, :idx, :ts, :role, :txt)"
      ),
      {"cid": chat_id, "idx": msg_idx, "ts": ts, "role": role, "txt": doc_text},
    )


def reconcile(db: Session) -> None:
  """Bring the index in line with the chats table. Cheap when nothing changed."""
  _ensure_schema(db)

  # Index rows whose chat is gone or tombstoned. A restored chat re-enters
  # through the stale scan below (restore bumps updated_at; no state row
  # remains, so it reindexes from scratch).
  orphans = db.execute(
    sql(
      "SELECT s.chat_id FROM chat_search_state s"
      " WHERE NOT EXISTS (SELECT 1 FROM chats c"
      "   WHERE c.id = s.chat_id AND c.deleted_at IS NULL)"
    )
  ).fetchall()
  for (chat_id,) in orphans:
    _delete_chat_docs(db, chat_id)

  # Live chats whose row changed since we last indexed them. Compared as the
  # exact stored text (equality, not ordering) so timestamp formatting can
  # never produce a false "fresh".
  stale = db.execute(
    sql(
      "SELECT c.id FROM chats c"
      " LEFT JOIN chat_search_state s ON s.chat_id = c.id"
      " WHERE c.deleted_at IS NULL"
      "   AND (s.chat_id IS NULL"
      "        OR s.indexed_updated_at IS NOT CAST(c.updated_at AS TEXT))"
    )
  ).fetchall()

  for (chat_id,) in stale:
    # Hydrates messages JSON for THIS chat only.
    chat = db.get(models.Chat, chat_id)
    if chat is None or chat.deleted_at is not None:
      continue
    row = db.execute(
      sql("SELECT CAST(updated_at AS TEXT) FROM chats WHERE id = :cid"),
      {"cid": chat_id},
    ).fetchone()
    updated_text = row[0] if row else ""
    messages = chat.messages or []
    title = chat.title or ""
    state = db.execute(
      sql(
        "SELECT indexed_count, indexed_title FROM chat_search_state"
        " WHERE chat_id = :cid"
      ),
      {"cid": chat_id},
    ).fetchone()

    if state is not None and state[1] == title and len(messages) >= state[0]:
      # History is append-only, so only messages beyond the indexed count are
      # new. A shrink (retention/repair rewrote history) or retitle falls
      # through to the full per-chat rebuild below.
      new_docs = [
        doc for doc in _prose_docs("", messages) if doc[0] >= state[0]
      ]
      _write_docs(db, chat_id, new_docs)
    else:
      _delete_chat_docs(db, chat_id)
      _write_docs(db, chat_id, _prose_docs(title, messages))

    db.execute(
      sql(
        "INSERT INTO chat_search_state"
        " (chat_id, indexed_count, indexed_title, indexed_updated_at)"
        " VALUES (:cid, :count, :title, :updated)"
        " ON CONFLICT(chat_id) DO UPDATE SET"
        "  indexed_count = excluded.indexed_count,"
        "  indexed_title = excluded.indexed_title,"
        "  indexed_updated_at = excluded.indexed_updated_at"
      ),
      {
        "cid": chat_id,
        "count": len(messages),
        "title": title,
        "updated": updated_text,
      },
    )
    # Release the hydrated transcript before the next stale chat.
    db.expire(chat)

  if orphans or stale:
    db.commit()


def _fts_query(raw: str) -> str:
  """User text -> safe FTS5 query: quoted tokens, prefix on the last one."""
  tokens = re.findall(r"[^\s\"'()*^]+", raw)
  if not tokens:
    return ""
  quoted = [f'"{token}"' for token in tokens]
  quoted[-1] = f"{quoted[-1]}*"
  return " ".join(quoted)


def search(db: Session, raw_query: str, limit: int = 20) -> list[dict]:
  """Ranked chats matching the query, best doc per chat, with a snippet."""
  match = _fts_query(raw_query)
  if not match:
    return []
  reconcile(db)

  rows = db.execute(
    sql(
      "SELECT d.chat_id, d.msg_idx, d.ts, d.role,"
      "  snippet(chat_search_fts, 0, :mo, :mc, '…', 12) AS snip,"
      "  bm25(chat_search_fts) AS rank,"
      "  c.title, CAST(COALESCE(c.activity_at, c.updated_at) AS TEXT)"
      " FROM chat_search_fts"
      " JOIN chat_search_docs d ON d.id = chat_search_fts.rowid"
      " JOIN chats c ON c.id = d.chat_id AND c.deleted_at IS NULL"
      " WHERE chat_search_fts MATCH :q"
      " ORDER BY rank LIMIT 200"
    ),
    {"q": match, "mo": _MARK_OPEN, "mc": _MARK_CLOSE},
  ).fetchall()

  # Best-ranked doc per chat wins; a title hit falls back to no snippet (the
  # row already shows the title). bm25 is ascending-better in SQLite. The
  # snippet's own prose doc supplies `ts`/`role` so the drawer can reveal that
  # exact transcript row — a title-only hit has neither and simply opens the
  # chat at its last position.
  by_chat: dict[str, dict] = {}
  for chat_id, msg_idx, ts, role, snip, rank, title, active_text in rows:
    entry = by_chat.get(chat_id)
    if entry is None:
      entry = {
        "id": chat_id,
        "title": title,
        "snippet": None,
        "ts": None,
        "role": None,
        "rank": rank,
        "last_active": active_text,
        "match_count": 0,
      }
      by_chat[chat_id] = entry
    entry["match_count"] += 1
    if msg_idx >= 0 and entry["snippet"] is None:
      entry["snippet"] = snip
      entry["ts"] = ts
      entry["role"] = role

  results = sorted(
    by_chat.values(), key=lambda e: (e["rank"], e["last_active"] or "")
  )[:limit]
  for entry in results:
    entry.pop("rank", None)
  return results
