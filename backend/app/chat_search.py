"""Search chat titles and conversation prose through normalized documents.

``chat_search_docs`` is the one search source on SQLite and PostgreSQL: one
title row and one row per visible owner/assistant message. Search never scans
serialized transcript JSON. SQLite uses an external-content FTS5 table to find
candidate document rows; PostgreSQL filters the same ordinary rows with a
portable text predicate. Both paths apply the same word/prefix matcher,
snippet builder, grouping, and ranking after candidate selection.

The documents are disposable derived data. A search lazily reconciles chats
whose exact ``updated_at`` representation changed, while the numbered schema
migration owns tables, indexes, and SQLite triggers. Transcript persistence
remains exclusively behind ``chat_writer.py``.
"""

import heapq
import re
import threading

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from app import chat_visibility, models

# Private-use sentinels around snippet matches. The API converts them to a
# JSON-friendly form; they can never collide with real transcript text.
_MARK_OPEN = "\ue000"
_MARK_CLOSE = "\ue001"

# Message roles whose `content` strings are conversation prose.
_PROSE_ROLES = ("user", "assistant")
_RECONCILE_LOCK = threading.Lock()


def _prose_docs(
  title: str,
  messages: list,
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
    sql("DELETE FROM chat_search_docs WHERE chat_id = :cid"), {"cid": chat_id}
  )
  db.execute(
    sql("DELETE FROM chat_search_state WHERE chat_id = :cid"), {"cid": chat_id}
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
  stale = db.execute(sql(
    "SELECT c.id, COALESCE(CAST(c.updated_at AS TEXT), '') FROM chats c"
    " LEFT JOIN chat_search_state s ON s.chat_id = c.id"
    " WHERE c.deleted_at IS NULL"
    " AND (s.chat_id IS NULL OR s.indexed_updated_at <>"
    "      COALESCE(CAST(c.updated_at AS TEXT), ''))"
  )).fetchall()

  for chat_id, updated_text in stale:
    # Hydrates messages JSON for THIS chat only.
    chat = db.get(models.Chat, chat_id)
    if chat is None or chat.deleted_at is not None:
      continue
    messages = chat.messages or []
    title = chat.title or ""
    # The index is disposable: replace this chat's derived rows at its durable
    # revision boundary instead of maintaining a second row-diff algorithm.
    # Non-drawer chats retain only a tiny state row so an unchanged hidden
    # app/autopilot chat is not re-hydrated on every query.
    _delete_chat_docs(db, chat_id)
    if chat_visibility.visible_in_owner_drawer(chat):
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
    "CAST(COALESCE(chat.activity_at, chat.updated_at) AS TEXT)"
  )
  if _database_dialect(db) == "sqlite":
    return db.execute(
      sql(
        f"SELECT {columns} FROM chat_search_fts "
        "JOIN chat_search_docs d ON d.id = chat_search_fts.rowid "
        "JOIN chats chat ON chat.id = d.chat_id "
        "WHERE chat.deleted_at IS NULL AND chat_search_fts MATCH :query "
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
      f"SELECT {columns} FROM chat_search_docs d "
      "JOIN chats chat ON chat.id = d.chat_id "
      "WHERE chat.deleted_at IS NULL AND " + " AND ".join(clauses)
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

  for chat_id, msg_idx, timestamp, role, doc_text, title, active_text in rows:
    if current is None or current["id"] != chat_id:
      finish(current)
      current = {
        "id": chat_id,
        "title": title,
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
    })
  return output


def purge_chat_docs(db: Session, chat_ids: list[str]) -> None:
  """Remove derived rows inside the source chat's hard-purge transaction."""
  parameters = [{"chat_id": chat_id} for chat_id in chat_ids]
  if not parameters:
    return
  db.execute(
    sql("DELETE FROM chat_search_docs WHERE chat_id = :chat_id"), parameters,
  )
  db.execute(
    sql("DELETE FROM chat_search_state WHERE chat_id = :chat_id"), parameters,
  )


def search(db: Session, raw_query: str, limit: int = 20) -> list[dict]:
  """Return ranked chat hits with only the fields consumed by the shell."""
  tokens = _query_tokens(raw_query)
  if not tokens or limit <= 0:
    return []
  reconcile(db)
  return _rank_results(_candidate_rows(db, tokens), tokens, limit)
