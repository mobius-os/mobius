"""Full-text search over chat transcripts (drawer search).

Design: SQLite uses an FTS5 index over conversation prose — the owner's
messages, the assistant's replies, and chat titles. PostgreSQL and other
dialects use the same visibility and matching contract through a portable
candidate query plus bounded Python ranking. Tool output, thinking traces, and
`blocks` machinery are deliberately excluded: they live in their own tables,
they are the bulk of the bytes, and they are noise for "where did we talk
about X".

The index is DERIVED data, reconciled lazily at query time:

- `chat_search_docs` holds one plain-text row per indexed message (plus one
  title row per chat, ``msg_idx = -1``). `chat_search_fts` is an
  external-content FTS5 table over it, kept in sync by triggers on the docs
  table itself (the canonical FTS5 external-content pattern — the triggers
  watch OUR derived table, never `chats`).
- `chat_search_state` remembers the exact `chats.updated_at` text indexed for
  each chat. A search request first reconciles: any live chat whose row changed
  is rebuilt from its current transcript, while deleted/purged chats are
  dropped. Rebuilding the changed chat is deliberate: assistant snapshots and
  owner transcript replacements can rewrite an existing message without
  changing the list length, so an append-only shortcut would make stale text
  permanent.
- Only chats that belong in the owner's drawer are indexed, and hidden
  transcript messages are omitted. A small derived-schema version makes those
  indexing semantics migratable: changing them drops and regenerates only the
  disposable search tables, never the source chat rows.

For SQLite, reconciliation happens inside the search request, so there is no
second code path that could forget to update the index, nothing hooks into the
turn lifecycle, and a missing index (or this feature landing on an existing
instance) simply rebuilds itself on first use. Memory: FTS5 reads b-tree pages
from disk per query; nothing is cached in process memory. Only changed chats
ever have their `messages` JSON hydrated — reconcile compares timestamps, not
transcripts.

Writes here touch only the derived `chat_search_*` tables — never
`Chat.messages` / `Chat.pending_messages` (those stay behind `chat_writer.py`).
"""

import json
import re
import threading

from sqlalchemy import Text, cast, func, or_, text as sql
from sqlalchemy.orm import Session

from app import models
from app.chat_visibility import visible_in_owner_drawer as _visible_in_owner_drawer

# Private-use sentinels around FTS5 snippet matches. The API converts them to
# a JSON-friendly form; they can never collide with real transcript text.
_MARK_OPEN = "\ue000"
_MARK_CLOSE = "\ue001"

# The version covers both table shape and indexing semantics. Search data is
# entirely derived, so a mismatch is safer and simpler to handle by rebuilding
# than by carrying migrations for disposable rows.
_INDEX_VERSION = "2"
_SCHEMA_LOCK = threading.Lock()
_RECONCILE_LOCK = threading.Lock()

# The portable backend has no derived full-text index. Keep its fallback
# honest and bounded: rank matches only within the most recently active chats
# whose serialized source contains every query token. This avoids turning one
# broad drawer query into an unbounded transcript hydration. SQLite retains
# complete-history semantics through its on-disk FTS index.
_PORTABLE_CANDIDATE_LIMIT = 512

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
CREATE UNIQUE INDEX IF NOT EXISTS chat_search_docs_chat_message
  ON chat_search_docs(chat_id, msg_idx);
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
  indexed_updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_search_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

_schema_ready = False


def _ensure_schema(db: Session) -> None:
  global _schema_ready
  if _schema_ready:
    return
  with _SCHEMA_LOCK:
    if _schema_ready:
      return
    expected_tables = {
      "chat_search_docs",
      "chat_search_fts",
      "chat_search_state",
      "chat_search_meta",
    }
    present_tables = {
      row[0]
      for row in db.execute(sql(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name IN ('chat_search_docs', 'chat_search_fts',"
        "              'chat_search_state', 'chat_search_meta')"
      )).fetchall()
    }
    version = None
    if "chat_search_meta" in present_tables:
      row = db.execute(sql(
        "SELECT value FROM chat_search_meta WHERE key = 'index_version'"
      )).fetchone()
      version = row[0] if row else None
    columns = (
      {
        row[1]
        for row in db.execute(sql("PRAGMA table_info(chat_search_docs)"))
      }
      if "chat_search_docs" in present_tables
      else set()
    )
    schema_current = (
      present_tables == expected_tables
      and version == _INDEX_VERSION
      and {"id", "chat_id", "msg_idx", "ts", "role", "text"} <= columns
    )
    if not schema_current:
      # Dropping the external-content virtual table removes its shadow tables;
      # dropping docs removes its sync triggers. A single transaction ensures a
      # concurrent search sees either the old generation or the new empty one.
      for statement in (
        "DROP TABLE IF EXISTS chat_search_fts",
        "DROP TABLE IF EXISTS chat_search_docs",
        "DROP TABLE IF EXISTS chat_search_state",
        "DROP TABLE IF EXISTS chat_search_meta",
      ):
        db.execute(sql(statement))
    for statement in _split_schema(_SCHEMA):
      db.execute(sql(statement))
    db.execute(
      sql(
        "INSERT INTO chat_search_meta (key, value)"
        " VALUES ('index_version', :version)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
      ),
      {"version": _INDEX_VERSION},
    )
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
    # Hidden rows carry internal reminders and silent question answers. They
    # are deliberately absent from the transcript, so returning their text in
    # drawer snippets would both disclose UI-private content and produce an
    # anchor that can never render.
    if message.get("hidden"):
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
  if not docs:
    return
  db.execute(
    sql(
      "INSERT INTO chat_search_docs (chat_id, msg_idx, ts, role, text)"
      " VALUES (:cid, :idx, :ts, :role, :txt)"
    ),
    [
      {"cid": chat_id, "idx": idx, "ts": ts, "role": role, "txt": text}
      for idx, ts, role, text in docs
    ],
  )


def _upsert_state(
  db: Session,
  *,
  chat_id: str,
  updated_text: str,
) -> None:
  db.execute(
    sql(
      "INSERT INTO chat_search_state"
      " (chat_id, indexed_updated_at)"
      " VALUES (:cid, :updated)"
      " ON CONFLICT(chat_id) DO UPDATE SET"
      "  indexed_updated_at = excluded.indexed_updated_at"
    ),
    {
      "cid": chat_id,
      "updated": updated_text,
    },
  )


def reconcile(db: Session) -> None:
  """Bring the derived index in line with chats, one reconciler at a time."""
  # FastAPI runs this synchronous route in a worker pool. Aborting an older
  # browser fetch does not stop its worker, so successive debounced queries can
  # overlap on first-use backfill. Serialize the derived writer in-process;
  # the unique `(chat_id, msg_idx)` index is the database-level idempotency net.
  with _RECONCILE_LOCK:
    _reconcile_locked(db)


def _reconcile_locked(db: Session) -> None:
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
    # The index is disposable: replace this chat's derived rows at its durable
    # revision boundary instead of maintaining a second row-diff algorithm.
    # Non-drawer chats retain only a tiny state row so an unchanged hidden
    # app/autopilot chat is not re-hydrated on every query.
    _delete_chat_docs(db, chat_id)
    if _visible_in_owner_drawer(chat):
      _write_docs(db, chat_id, _prose_docs(title, messages))
    _upsert_state(
      db,
      chat_id=chat_id,
      updated_text=updated_text,
    )
    # Release the hydrated transcript before the next stale chat.
    db.expire(chat)

  if orphans or stale:
    db.commit()


def _fts_query(raw: str) -> str:
  """User text -> safe FTS5 query: quoted tokens, prefix on the last one."""
  tokens = _query_tokens(raw)
  if not tokens:
    return ""
  quoted = [f'"{token}"' for token in tokens]
  quoted[-1] = f"{quoted[-1]}*"
  return " ".join(quoted)


def _query_tokens(raw: str) -> list[str]:
  """Normalize one query consistently across FTS and portable backends."""
  return re.findall(r"\w+", raw, flags=re.UNICODE)


def _portable_token_patterns(tokens: list[str]) -> list[re.Pattern[str]]:
  patterns = []
  for index, token in enumerate(tokens):
    escaped = re.escape(token)
    if index == len(tokens) - 1:
      escaped = rf"(?<!\w){escaped}\w*"
    else:
      escaped = rf"(?<!\w){escaped}(?!\w)"
    patterns.append(re.compile(escaped, flags=re.IGNORECASE | re.UNICODE))
  return patterns


def _portable_match_spans(
  text: str,
  patterns: list[re.Pattern[str]],
) -> list[tuple[int, int]]:
  spans: list[tuple[int, int]] = []
  for pattern in patterns:
    matches = list(pattern.finditer(text))
    if not matches:
      return []
    spans.extend(match.span() for match in matches)
  return sorted(set(spans))


def _portable_snippet(
  text: str,
  spans: list[tuple[int, int]],
  width: int = 180,
) -> str:
  """Build one sentinel-marked excerpt for the non-FTS fallback."""
  first_start = spans[0][0]
  start = max(0, first_start - width // 3)
  end = min(len(text), start + width)
  start = max(0, end - width)
  visible = [span for span in spans if span[0] >= start and span[1] <= end]
  pieces = ["…"] if start else []
  cursor = start
  for span_start, span_end in visible:
    if span_start < cursor:
      continue
    pieces.extend((
      text[cursor:span_start],
      _MARK_OPEN,
      text[span_start:span_end],
      _MARK_CLOSE,
    ))
    cursor = span_end
  pieces.append(text[cursor:end])
  if end < len(text):
    pieces.append("…")
  return "".join(pieces)


def _portable_like_pattern(token: str) -> str:
  escaped = (
    token.lower()
    .replace("\\", "\\\\")
    .replace("%", "\\%")
    .replace("_", "\\_")
  )
  return f"%{escaped}%"


def _portable_transcript_patterns(token: str) -> tuple[str, ...]:
  """Candidate forms for JSON serializers that preserve or escape Unicode."""
  raw = _portable_like_pattern(token)
  serialized = json.dumps(token, ensure_ascii=True)[1:-1]
  escaped = _portable_like_pattern(serialized)
  return (raw,) if escaped == raw else (raw, escaped)


def _portable_search(
  db: Session,
  raw_query: str,
  limit: int,
) -> list[dict]:
  """Preserve the search contract when SQLite FTS5 is unavailable."""
  tokens = _query_tokens(raw_query)
  if not tokens:
    return []
  title_text = func.lower(func.coalesce(models.Chat.title, ""))
  transcript_text = func.lower(cast(models.Chat.messages, Text))
  candidate_filters = [
    or_(
      title_text.like(_portable_like_pattern(token), escape="\\"),
      *(
        transcript_text.like(pattern, escape="\\")
        for pattern in _portable_transcript_patterns(token)
      ),
    )
    for token in tokens
  ]
  candidates = (
    db.query(
      models.Chat.id,
      models.Chat.title,
      models.Chat.messages,
      models.Chat.agent_settings_json,
      models.Chat.created_by_app_id,
      models.Chat.activity_at,
      models.Chat.updated_at,
    )
    .filter(models.Chat.deleted_at.is_(None), *candidate_filters)
    .order_by(
      func.coalesce(models.Chat.activity_at, models.Chat.updated_at).desc(),
      models.Chat.id,
    )
    .limit(_PORTABLE_CANDIDATE_LIMIT)
    .yield_per(64)
  )
  patterns = _portable_token_patterns(tokens)
  results: list[dict] = []
  for chat in candidates:
    if not _visible_in_owner_drawer(chat):
      continue
    matches = []
    for doc in _prose_docs(chat.title or "", chat.messages or []):
      spans = _portable_match_spans(doc[3], patterns)
      if spans:
        matches.append((doc, spans))
    if not matches:
      continue
    prose_matches = [match for match in matches if match[0][0] >= 0]
    chosen_doc, chosen_spans = max(
      prose_matches or matches,
      key=lambda match: (len(match[1]), -match[0][0]),
    )
    msg_idx, ts, role, doc_text = chosen_doc
    active = chat.activity_at or chat.updated_at
    active_text = active.isoformat() if hasattr(active, "isoformat") else str(active or "")
    entry = {
      "id": chat.id,
      "title": chat.title,
      "snippet": (
        _portable_snippet(doc_text, chosen_spans) if msg_idx >= 0 else None
      ),
      "ts": ts if msg_idx >= 0 else None,
      "role": role if msg_idx >= 0 else None,
      "anchor_key": None,
      "last_active": active_text,
      "match_count": len(matches),
      "_occurrences": sum(len(spans) for _, spans in matches),
    }
    if msg_idx >= 0:
      entry["anchor_key"] = (
        f"{role}-{ts}" if ts is not None else f"{role}-{msg_idx}"
      )
    results.append(entry)
  results.sort(
    key=lambda entry: (
      entry["_occurrences"], entry["match_count"], entry["last_active"],
    ),
    reverse=True,
  )
  for entry in results:
    entry.pop("_occurrences", None)
  return results[:limit]


def _database_dialect(db: Session) -> str:
  return db.get_bind().dialect.name


def purge_chat_docs(db: Session, chat_ids: list[str]) -> None:
  """Delete SQLite-derived search data inside the hard-purge transaction."""
  if not chat_ids or _database_dialect(db) != "sqlite":
    return
  tables = {
    row[0]
    for row in db.execute(sql(
      "SELECT name FROM sqlite_master WHERE type = 'table'"
      " AND name IN ('chat_search_docs', 'chat_search_state')"
    )).fetchall()
  }
  for chat_id in chat_ids:
    if "chat_search_docs" in tables:
      db.execute(
        sql("DELETE FROM chat_search_docs WHERE chat_id = :cid"),
        {"cid": chat_id},
      )
    if "chat_search_state" in tables:
      db.execute(
        sql("DELETE FROM chat_search_state WHERE chat_id = :cid"),
        {"cid": chat_id},
      )


def search(db: Session, raw_query: str, limit: int = 20) -> list[dict]:
  """Ranked chats matching the query, best doc per chat, with a snippet."""
  if _database_dialect(db) != "sqlite":
    return _portable_search(db, raw_query, limit)
  match = _fts_query(raw_query)
  if not match:
    return []
  reconcile(db)

  # Choose chats before choosing snippets. Limiting the raw FTS row stream lets
  # one long conversation with hundreds of matching messages crowd every other
  # chat out of the result set. The FTS5 `rank` pseudo-column is bm25-compatible
  # and, unlike the bm25() auxiliary function, can be aggregated by SQLite.
  chats = db.execute(
    sql(
      "SELECT d.chat_id, MIN(chat_search_fts.rank) AS best_rank,"
      "  COUNT(*) AS match_count, c.title,"
      "  CAST(COALESCE(c.activity_at, c.updated_at) AS TEXT) AS active_text"
      " FROM chat_search_fts"
      " JOIN chat_search_docs d ON d.id = chat_search_fts.rowid"
      " JOIN chats c ON c.id = d.chat_id AND c.deleted_at IS NULL"
      " WHERE chat_search_fts MATCH :q"
      " GROUP BY d.chat_id"
      " ORDER BY best_rank, active_text DESC, d.chat_id"
      " LIMIT :limit"
    ),
    {"q": match, "limit": limit},
  ).fetchall()

  results: list[dict] = []
  for chat_id, _rank, match_count, title, active_text in chats:
    # Prefer the best prose hit so a chat whose title also matches can still
    # jump to useful transcript context. A title-only chat returns no snippet
    # or anchor and opens at its ordinary saved position.
    hit = db.execute(
      sql(
        "SELECT d.msg_idx, d.ts, d.role,"
        "  snippet(chat_search_fts, 0, :mo, :mc, '…', 12) AS snip"
        " FROM chat_search_fts"
        " JOIN chat_search_docs d ON d.id = chat_search_fts.rowid"
        " WHERE chat_search_fts MATCH :q AND d.chat_id = :cid"
        " ORDER BY (d.msg_idx < 0), chat_search_fts.rank, d.id"
        " LIMIT 1"
      ),
      {"q": match, "cid": chat_id, "mo": _MARK_OPEN, "mc": _MARK_CLOSE},
    ).fetchone()
    msg_idx, ts, role, snip = hit if hit is not None else (-1, None, None, None)
    entry = {
      "id": chat_id,
      "title": title,
      "snippet": snip if msg_idx >= 0 else None,
      "ts": ts if msg_idx >= 0 else None,
      "role": role if msg_idx >= 0 else None,
      "anchor_key": None,
      "last_active": active_text,
      "match_count": match_count,
    }
    if msg_idx >= 0:
      # Match the durable keys accepted by GET /chats/{id}?anchor=… and the
      # transcript DOM. Timestamps are the normal stable identity; older or
      # repaired rows without one still have the role/index address.
      entry["anchor_key"] = (
        f"{role}-{ts}" if ts is not None else f"{role}-{msg_idx}"
      )
    results.append(entry)
  return results
