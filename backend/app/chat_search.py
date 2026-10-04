"""Search chat titles and conversation prose through normalized documents.

``chat_search_docs_v2`` is the one search source on SQLite and PostgreSQL: one
title row and one row per visible owner/assistant message. Search never scans
serialized transcript JSON. SQLite uses an external-content FTS5 table to find
candidate document rows; PostgreSQL filters the same ordinary rows with a
portable text predicate. Both paths apply the same word/prefix matcher,
snippet builder, grouping, and ranking after candidate selection.

The documents are disposable derived data. A bounded background task builds
the initial generation; search reconciles previously indexed chats and rejects
any result whose exact updated_at or transcript revision is stale. Transcript
persistence remains exclusively behind chat_writer.py.
"""

import heapq
import json
import sqlite3
import re
import threading

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from app import chat_visibility, models, transcript_rows

# Private-use sentinels around snippet matches. The API converts them to a
# JSON-friendly form; they can never collide with real transcript text.
_MARK_OPEN = "\ue000"
_MARK_CLOSE = "\ue001"

# Message roles whose `content` strings are conversation prose.
_PROSE_ROLES = ("user", "assistant")
_RECONCILE_LOCK = threading.Lock()


def _prose_docs(
  title: str,
  messages,
) -> list[tuple[int, int | None, str | None, str]]:
  """(msg_idx, ts, role, text) rows: title at -1 (ts/role None), then prose.

  `ts` and `role` are stored so a search result can point the drawer at the
  exact transcript row to reveal — the chat UI keys each message row as
  ``<role>-<ts>``. `ts` is unique within a chat, so it needs no message index.
  """
  docs: list[tuple[int, int | None, str | None, str]] = []
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
    sql("DELETE FROM chat_search_docs_v2 WHERE chat_id = :cid"), {"cid": chat_id}
  )
  db.execute(
    sql("DELETE FROM chat_search_state_v2 WHERE chat_id = :cid"), {"cid": chat_id}
  )


def _write_docs(
  db: Session,
  chat_id: str,
  docs: list[tuple[int, int | None, str | None, str]],
) -> None:
  if not docs:
    return
  db.execute(
    sql(
      "INSERT INTO chat_search_docs_v2 (chat_id, msg_idx, ts, role, text)"
      " VALUES (:cid, :idx, :ts, :role, :txt)"
    ),
    [
      {"cid": chat_id, "idx": idx, "ts": ts, "role": role, "txt": text}
      for idx, ts, role, text in docs
    ],
  )


def _upsert_state(db: Session, *, chat_id: str, updated_text: str, revision: int) -> None:
  db.execute(sql(
    "INSERT INTO chat_search_state_v2 (chat_id, indexed_updated_at, indexed_revision)"
    " VALUES (:cid, :updated, :revision) ON CONFLICT(chat_id) DO UPDATE SET"
    " indexed_updated_at=excluded.indexed_updated_at,"
    " indexed_revision=excluded.indexed_revision"
  ), {"cid": chat_id, "updated": updated_text, "revision": revision})


def create_schema_v2(conn) -> None:
  """Prepare a disposable generation without touching the old search index."""
  raw = isinstance(conn, sqlite3.Connection)
  dialect = "sqlite" if raw else conn.dialect.name
  ident = "INTEGER" if dialect == "sqlite" else "BIGSERIAL"
  statements = [
    "CREATE TABLE IF NOT EXISTS chat_search_docs_v2 ("
    f"id {ident} PRIMARY KEY, chat_id VARCHAR(64) NOT NULL, msg_idx INTEGER NOT NULL, "
    "ts BIGINT, role VARCHAR(16), text TEXT NOT NULL)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_chat_search_docs_v2_chat_message "
    "ON chat_search_docs_v2(chat_id,msg_idx)",
    "CREATE TABLE IF NOT EXISTS chat_search_state_v2 ("
    "chat_id VARCHAR(64) PRIMARY KEY, indexed_updated_at TEXT NOT NULL, "
    "indexed_revision INTEGER NOT NULL)",
  ]
  if dialect == "sqlite":
    statements += [
      "CREATE VIRTUAL TABLE IF NOT EXISTS chat_search_fts_v2 USING fts5("
      "text, content='chat_search_docs_v2', content_rowid='id', "
      "tokenize='unicode61 remove_diacritics 2')",
      "CREATE TRIGGER IF NOT EXISTS chat_search_docs_v2_ai "
      "AFTER INSERT ON chat_search_docs_v2 BEGIN "
      "INSERT INTO chat_search_fts_v2(rowid,text) VALUES(new.id,new.text); END",
      "CREATE TRIGGER IF NOT EXISTS chat_search_docs_v2_ad "
      "AFTER DELETE ON chat_search_docs_v2 BEGIN "
      "INSERT INTO chat_search_fts_v2(chat_search_fts_v2,rowid,text) "
      "VALUES('delete',old.id,old.text); END",
    ]
  for statement in statements:
    conn.execute(statement if raw else sql(statement))


def _index_chat_raw(conn: sqlite3.Connection, cid: str, title: str,
                    settings_raw: str | None, app_id: int | None,
                    updated: str, revision: int) -> None:
  conn.execute("DELETE FROM chat_search_docs_v2 WHERE chat_id=?", (cid,))
  conn.execute("DELETE FROM chat_search_state_v2 WHERE chat_id=?", (cid,))
  try:
    settings = json.loads(settings_raw or "{}")
  except (ValueError, TypeError):
    settings = {}
  # SQLite JSON may contain a legacy JSON string. Apply the same second
  # decode as the ORM visibility policy, not a parallel policy.
  settings = chat_visibility.coerce_agent_settings(settings)
  hidden = settings.get("drawer_hidden")
  visible = not bool(hidden) if hidden is not None else (
    app_id is None or settings.get("owner_visible") is True)
  if visible:
    if title and title.strip():
      conn.execute("INSERT INTO chat_search_docs_v2(chat_id,msg_idx,ts,role,text) "
                   "VALUES(?,-1,NULL,NULL,?)", (cid, title.strip()))
    cursor = conn.execute("SELECT seq,body FROM chat_messages WHERE chat_id=? ORDER BY seq", (cid,))
    for seq, body_raw in cursor:
      body = json.loads(body_raw)
      if not isinstance(body, dict) or body.get("hidden"):
        continue
      role = body.get("role")
      content = body.get("content")
      if role not in _PROSE_ROLES or not isinstance(content, str) or not content.strip():
        continue
      ts = body.get("ts")
      conn.execute("INSERT INTO chat_search_docs_v2(chat_id,msg_idx,ts,role,text) "
                   "VALUES(?,?,?,?,?)", (cid, seq, ts if isinstance(ts, int) and not isinstance(ts, bool) else None,
                                         role, content))
  conn.execute("INSERT INTO chat_search_state_v2(chat_id,indexed_updated_at,indexed_revision) "
               "VALUES(?,?,?)", (cid, updated, revision))


def _index_chat(db: Session, chat_id: str, updated_text: str, revision: int) -> None:
  chat = db.get(models.Chat, chat_id)
  if chat is None or chat.deleted_at is not None:
    _delete_chat_docs(db, chat_id)
    return
  _delete_chat_docs(db, chat_id)
  if chat_visibility.visible_in_owner_drawer(chat):
    _write_docs(db, chat_id, _prose_docs(chat.title or "", transcript_rows.iterate(db, chat)))
  _upsert_state(db, chat_id=chat_id, updated_text=updated_text, revision=revision)


INDEX_BATCH_BODY_BUDGET = 8 * 1024 * 1024
INDEX_BATCH_MAX_CHATS = 20
# External-content FTS writes docs, tokens and WAL. Be conservative for a
# single oversized chat; the gate checks this before opening the write batch.
_INDEX_SPACE_MULTIPLIER = 12
_INDEX_SPACE_FLOOR = 40 * 1024 * 1024


def _raw_stale_clause() -> str:
  return (
    "FROM chats c JOIN chat_transcript_state t ON t.chat_id=c.id "
    "LEFT JOIN chat_search_state_v2 s ON s.chat_id=c.id "
    "WHERE c.deleted_at IS NULL AND (s.chat_id IS NULL OR "
    "s.indexed_updated_at <> COALESCE(CAST(c.updated_at AS TEXT),'') OR "
    "s.indexed_revision <> t.revision)"
  )


def _raw_batch_plan(conn: sqlite3.Connection, batch_size: int,
                    byte_budget: int) -> tuple[list[tuple], int]:
  """Select a whole-chat batch capped by source bytes, with giant-unit escape.

  Sizing uses SQLite's BLOB length and never decodes bodies. This is also the
  preflight plan: the PostTask reserve should call index_batch_space_bytes.
  """
  rows = conn.execute(
    "SELECT c.id,c.title,c.agent_settings_json,c.created_by_app_id,"
    "COALESCE(CAST(c.updated_at AS TEXT),''),t.revision "
    + _raw_stale_clause() + " ORDER BY c.id LIMIT ?", (batch_size,)
  ).fetchall()
  chosen = []
  bytes_total = 0
  for row in rows:
    body_bytes = conn.execute(
      "SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))),0) "
      "FROM chat_messages WHERE chat_id=?", (row[0],)
    ).fetchone()[0]
    # Always admit one oversized unit; otherwise it could never progress.
    if chosen and bytes_total + body_bytes > byte_budget:
      break
    chosen.append(row)
    bytes_total += body_bytes
  return chosen, bytes_total


def index_batch_space_bytes(conn: sqlite3.Connection) -> int:
  """Space reservation for the next raw PostTask batch (derived rows + WAL)."""
  _, source_bytes = _raw_batch_plan(
    conn, INDEX_BATCH_MAX_CHATS, INDEX_BATCH_BODY_BUDGET)
  return max(_INDEX_SPACE_FLOOR,
             16 * 1024 * 1024 + _INDEX_SPACE_MULTIPLIER * source_bytes)


def index_batch(conn, batch_size: int = INDEX_BATCH_MAX_CHATS,
                byte_budget: int = INDEX_BATCH_BODY_BUDGET) -> tuple[int, int]:
  """Index at most one bounded batch on the caller's pinned transaction.

  Returns (processed units, remaining units); never commits or opens another
  connection. The raw SQLite path is used by the one-way upgrade gate.
  """
  if isinstance(conn, sqlite3.Connection):
    rows, _source_bytes = _raw_batch_plan(conn, batch_size, byte_budget)
    stale_sql = _raw_stale_clause()
    for cid, title, settings, app_id, updated, revision in rows:
      _index_chat_raw(conn, cid, title or "", settings, app_id, updated, revision)
    remaining = conn.execute("SELECT COUNT(*) " + stale_sql).fetchone()[0]
    return len(rows), remaining
  if conn.dialect.name == "sqlite":
    return index_batch(conn.connection.driver_connection, batch_size, byte_budget)
  from sqlalchemy.orm import Session
  with Session(bind=conn) as db:
    rows = db.execute(sql(
      "SELECT c.id, COALESCE(CAST(c.updated_at AS TEXT),''), t.revision "
      "FROM chats c JOIN chat_transcript_state t ON t.chat_id=c.id "
      "LEFT JOIN chat_search_state_v2 s ON s.chat_id=c.id "
      "WHERE c.deleted_at IS NULL AND (s.chat_id IS NULL OR "
      "s.indexed_updated_at <> COALESCE(CAST(c.updated_at AS TEXT),'') OR "
      "s.indexed_revision <> t.revision) ORDER BY c.id LIMIT :n"
    ), {"n": batch_size}).fetchall()
    for cid, updated, revision in rows:
      _index_chat(db, cid, updated, revision)
    db.flush()
    remaining = db.execute(sql(
      "SELECT COUNT(*) FROM chats c JOIN chat_transcript_state t ON t.chat_id=c.id "
      "LEFT JOIN chat_search_state_v2 s ON s.chat_id=c.id "
      "WHERE c.deleted_at IS NULL AND (s.chat_id IS NULL OR "
      "s.indexed_updated_at <> COALESCE(CAST(c.updated_at AS TEXT),'') OR "
      "s.indexed_revision <> t.revision)"
    )).scalar_one()
    return len(rows), remaining


def reconcile(db: Session) -> None:
  """Bring the derived index in line with chats, one reconciler at a time."""
  # FastAPI runs this synchronous route in a worker pool. Aborting an older
  # browser fetch does not stop its worker, so successive debounced queries can
  # overlap on first-use backfill. Serialize the derived writer in-process;
  # the unique `(chat_id, msg_idx)` index is the database-level idempotency net.
  with _RECONCILE_LOCK:
    _reconcile_locked(db)


def _reconcile_locked(db: Session) -> None:
  # The initial generation is exclusively the bounded PostTask. Once that
  # task is durably done, a chat created later has no state row and must join
  # the same bounded request-time reconciliation as an indexed chat that
  # changed. The task status is the generation boundary, not an inference from
  # an empty stale scan (which would race the background builder).
  initial_complete = db.execute(sql(
    "SELECT status FROM upgrade_tasks WHERE level=1 AND task='index_messages'"
  )).scalar() == "done"
  orphans = db.execute(sql(
    "SELECT s.chat_id FROM chat_search_state_v2 s LEFT JOIN chats c ON c.id=s.chat_id "
    "WHERE c.id IS NULL OR c.deleted_at IS NOT NULL LIMIT 20"
  )).fetchall()
  for (cid,) in orphans:
    _delete_chat_docs(db, cid)
  candidates = db.execute(sql(
    "SELECT c.id, COALESCE(CAST(c.updated_at AS TEXT),''), t.revision "
    "FROM chats c JOIN chat_transcript_state t ON t.chat_id=c.id "
    "LEFT JOIN chat_search_state_v2 s ON s.chat_id=c.id "
    "WHERE c.deleted_at IS NULL AND ("
    " (:complete=1 AND s.chat_id IS NULL) OR"
    " (s.chat_id IS NOT NULL AND (s.indexed_updated_at <> "
    " COALESCE(CAST(c.updated_at AS TEXT),'') OR s.indexed_revision <> t.revision))) "
    "ORDER BY c.id LIMIT :n"
  ), {"complete": int(initial_complete), "n": INDEX_BATCH_MAX_CHATS}).fetchall()
  chosen = []
  source_bytes = 0
  size_sql = (
    "SELECT COALESCE(SUM(LENGTH(CAST(body AS BLOB))),0) "
    if db.get_bind().dialect.name == "sqlite"
    else "SELECT COALESCE(SUM(OCTET_LENGTH(CAST(body AS TEXT))),0) "
  ) + "FROM chat_messages WHERE chat_id=:cid"
  for cid, updated, revision in candidates:
    # Count bytes in SQL, never decode an unrelated chat's body. One giant
    # chat is admitted alone so it cannot permanently strand search.
    size = db.execute(sql(size_sql), {"cid": cid}).scalar_one()
    if chosen and source_bytes + size > INDEX_BATCH_BODY_BUDGET:
      break
    chosen.append((cid, updated, revision))
    source_bytes += size
  for cid, updated, revision in chosen:
    _index_chat(db, cid, updated, revision)
  if chosen or orphans:
    db.commit()


def _fts_query(tokens: list[str]) -> str:
  """Build a safe FTS5 query from already-neutralized tokens."""
  quoted = [f'"{token}"' for token in tokens]
  quoted[-1] = f"{quoted[-1]}*"
  return " ".join(quoted)


def _query_tokens(raw: str) -> list[str]:
  """Normalize one query consistently across both database backends."""
  return re.findall(r"\w+", raw, flags=re.UNICODE)


def _token_patterns(tokens: list[str]) -> list[re.Pattern[str]]:
  patterns = []
  for index, token in enumerate(tokens):
    escaped = re.escape(token)
    if index == len(tokens) - 1:
      escaped = rf"(?<!\w){escaped}\w*"
    else:
      escaped = rf"(?<!\w){escaped}(?!\w)"
    patterns.append(re.compile(escaped, flags=re.IGNORECASE | re.UNICODE))
  return patterns


def _match_spans(
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


def _snippet(
  text: str,
  spans: list[tuple[int, int]],
  width: int = 180,
) -> str:
  """Build one sentinel-marked excerpt around the first matching span."""
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


def _database_dialect(db: Session) -> str:
  return db.get_bind().dialect.name


def _candidate_rows(db: Session, tokens: list[str]):
  """Return candidate normalized documents through the dialect's light seam."""
  columns = (
    "d.chat_id, d.msg_idx, d.ts, d.role, d.text, chat.title, "
    "CAST(COALESCE(chat.activity_at, chat.updated_at) AS TEXT), "
    "chat.archived_at IS NOT NULL"
  )
  if _database_dialect(db) == "sqlite":
    return db.execute(
      sql(
        f"SELECT {columns} FROM chat_search_fts_v2 "
        "JOIN chat_search_docs_v2 d ON d.id = chat_search_fts_v2.rowid "
        "JOIN chats chat ON chat.id = d.chat_id "
        "JOIN chat_transcript_state t ON t.chat_id=chat.id "
        "JOIN chat_search_state_v2 st ON st.chat_id=chat.id "
        "WHERE chat.deleted_at IS NULL AND st.indexed_revision=t.revision "
        "AND st.indexed_updated_at=COALESCE(CAST(chat.updated_at AS TEXT), '') "
        "AND chat_search_fts_v2 MATCH :query "
        "ORDER BY d.chat_id, d.msg_idx"
      ).execution_options(stream_results=True, max_row_buffer=256),
      {"query": _fts_query(tokens)},
    )

  # Query tokens cannot contain ``%``. An underscore can broaden LIKE by one
  # character, but the shared matcher below removes that false positive; the
  # database predicate therefore cannot discard a true document hit.
  clauses = []
  parameters = {}
  for index, token in enumerate(tokens):
    key = f"token_{index}"
    clauses.append(f"LOWER(d.text) LIKE :{key}")
    parameters[key] = f"%{token.lower()}%"
  return db.execute(
    sql(
      f"SELECT {columns} FROM chat_search_docs_v2 d "
      "JOIN chats chat ON chat.id = d.chat_id "
      "JOIN chat_transcript_state t ON t.chat_id=chat.id "
      "JOIN chat_search_state_v2 st ON st.chat_id=chat.id "
      "WHERE chat.deleted_at IS NULL AND st.indexed_revision=t.revision "
      "AND st.indexed_updated_at=COALESCE(CAST(chat.updated_at AS TEXT), '') AND "
      + " AND ".join(clauses)
      + " ORDER BY d.chat_id, d.msg_idx"
    ).execution_options(stream_results=True, max_row_buffer=256),
    parameters,
  )


def _iso_timestamp(stored: str) -> str | None:
  """Convert a CAST-to-text timestamp to ISO 8601, or None when absent.

  Both backends serialize the naive-UTC datetime space-separated
  ('YYYY-MM-DD HH:MM:SS.ffffff'); the shell's relative-time formatter — and
  Safari's stricter ``Date.parse`` — need the 'T' separator. Empty text means
  the chat carried neither an ``activity_at`` nor an ``updated_at`` value.
  """
  if not stored:
    return None
  return stored.replace(" ", "T", 1)


def _rank_results(rows, tokens: list[str], limit: int) -> list[dict]:
  """Return matching chats recent-first with memory bounded by ``limit``.

  Candidate selection already requires every query token to occur in one
  visible title/message document. Counting the same words across a long chat
  therefore measures transcript length, not match quality, and used to let old
  verbose conversations crowd recent exact reports out of the result window.
  Recency owns ordering once that full-query relevance boundary is crossed.
  """
  patterns = _token_patterns(tokens)
  top_results = []
  current = None

  def finish(result) -> None:
    if result is None or result["match_count"] == 0:
      return
    rank = (result["last_active"], result["id"])
    heapq.heappush(top_results, (rank, result))
    if len(top_results) > limit:
      heapq.heappop(top_results)

  for (
    chat_id, msg_idx, timestamp, role, doc_text, title, active_text, archived,
  ) in rows:
    if current is None or current["id"] != chat_id:
      finish(current)
      current = {
        "id": chat_id,
        "title": title,
        "archived": bool(archived),
        "last_active": active_text or "",
        "match_count": 0,
        "best_prose": None,
      }
    spans = _match_spans(doc_text, patterns)
    if not spans:
      continue
    current["match_count"] += 1
    if msg_idx < 0:
      continue
    candidate = (len(spans), -msg_idx, timestamp, role, doc_text, spans)
    if (
      current["best_prose"] is None
      or candidate[:2] > current["best_prose"][:2]
    ):
      current["best_prose"] = candidate
  finish(current)

  output = []
  for _rank, result in sorted(top_results, reverse=True):
    best = result["best_prose"]
    if best is None:
      snippet = None
      anchor_key = None
    else:
      _count, negative_index, timestamp, role, doc_text, spans = best
      msg_idx = -negative_index
      snippet = _snippet(doc_text, spans)
      anchor_key = (
        f"{role}-{timestamp}" if timestamp is not None else f"{role}-{msg_idx}"
      )
    output.append({
      "id": result["id"],
      "title": result["title"],
      "snippet": snippet,
      "anchor_key": anchor_key,
      "last_active": _iso_timestamp(result["last_active"]),
      # Archived chats stay findable; the shell labels them.
      "archived": result["archived"],
    })
  return output


def purge_chat_docs(db: Session, chat_ids: list[str]) -> None:
  """Remove derived rows inside the source chat's hard-purge transaction."""
  parameters = [{"chat_id": chat_id} for chat_id in chat_ids]
  if not parameters:
    return
  db.execute(
    sql("DELETE FROM chat_search_docs_v2 WHERE chat_id = :chat_id"), parameters,
  )
  db.execute(
    sql("DELETE FROM chat_search_state_v2 WHERE chat_id = :chat_id"), parameters,
  )


def search(db: Session, raw_query: str, limit: int = 20) -> list[dict]:
  """Return ranked chat hits with only the fields consumed by the shell."""
  tokens = _query_tokens(raw_query)
  if not tokens or limit <= 0:
    return []
  reconcile(db)
  return _rank_results(_candidate_rows(db, tokens), tokens, limit)
