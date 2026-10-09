"""Search chat titles and conversation prose through derived entries.

``chat_search_entries`` holds one row per non-empty title (seq -1) and one
per prose transcript row, with an external-content FTS5 index. Schema
triggers keep it current for every writer in the same transaction as the
change (schema_migrations._add_transcript_rows); search itself never writes.
Which rows are prose is decided by transcript_rows.attributes. Drawer
visibility is applied here, at query time, from the chat's current columns.

The previous release's own ``chat_search_docs`` tables are left for that
release, which reconciles them from ``updated_at`` after a rollback.
"""

import heapq
import json
import re
from types import SimpleNamespace

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from app import chat_visibility

# Private-use sentinels around snippet matches. The API converts them to a
# JSON-friendly form; they can never collide with real transcript text.
_MARK_OPEN = "\ue000"
_MARK_CLOSE = "\ue001"


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


def _candidate_rows(db: Session, tokens: list[str]):
  """Stream FTS candidates of live chats, grouped by chat and position.

  While the previous release's column exists, a chat's prose entries count
  only once it is converted: the previous release may have replaced its
  transcript since they were derived. Titles always count.
  """
  from app import transcript_rows

  return db.execute(
    sql(
      # Raw bytes: json_extract renders an escaped lone surrogate as bytes
      # that are not UTF-8, which the driver would refuse to decode.
      "SELECT e.chat_id, e.seq, e.ts, e.role, CAST(e.text AS BLOB), chat.title, "
      "CAST(COALESCE(chat.activity_at, chat.updated_at) AS TEXT), "
      "chat.archived_at IS NOT NULL, chat.agent_settings_json, chat.created_by_app_id "
      "FROM chat_search_entries_fts "
      "JOIN chat_search_entries e ON e.id = chat_search_entries_fts.rowid "
      "JOIN chats chat ON chat.id = e.chat_id "
      "WHERE chat.deleted_at IS NULL AND chat_search_entries_fts MATCH :query "
      "AND (e.seq < 0 OR :rows_authoritative OR EXISTS "
      "(SELECT 1 FROM chat_transcript_state s WHERE s.chat_id = e.chat_id)) "
      "ORDER BY e.chat_id, e.seq"
    ).execution_options(stream_results=True, max_row_buffer=256),
    {"query": _fts_query(tokens),
     "rows_authoritative": transcript_rows.rows_are_authority(db)},
  )


def _visible_rows(rows):
  """Only chats in the owner's drawer; hidden app and helper chats never match."""
  visible = {}
  for row in rows:
    chat_id, *_rest, settings, app_id = row
    if chat_id not in visible:
      # Raw JSON-column text, decoded as the ORM's JSON type would.
      visible[chat_id] = chat_visibility.visible_in_owner_drawer(SimpleNamespace(
        agent_settings_json=None if settings is None else json.loads(settings),
        created_by_app_id=app_id,
      ))
    if visible[chat_id]:
      chat_id, seq, ts, role, raw, *rest = row[:8]
      yield (chat_id, seq, ts, role, bytes(raw).decode("utf-8", "replace"), *rest)


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


def search(db: Session, raw_query: str, limit: int = 20) -> list[dict]:
  """Return ranked chat hits with only the fields consumed by the shell."""
  tokens = _query_tokens(raw_query)
  if not tokens or limit <= 0:
    return []
  return _rank_results(_visible_rows(_candidate_rows(db, tokens)), tokens, limit)
