"""Position-addressed transcript persistence and its legacy mirror.

Each saved transcript item is a ``chat_messages`` row keyed by
``(chat_id, seq)``; rows are dense from 0. Optional ``id``/``cid`` values are
lookup hints, never keys. Every write goes through a ``chat_writer`` domain
command, which calls the mutation functions below inside its transaction and
leaves commit/rollback to that command.

Two-release storage change (TRANSCRIPT_STORAGE_DESIGN.md). While the previous
release's ``chats.messages`` column exists (``legacy_present``):

* A chat's rows are authoritative only once a ``chat_transcript_state`` row
  exists for it. Until then ``chats.messages`` is, and every reader below
  reads that value, decoded once per call or ``History`` handle: exactly what
  the previous release reads. Reads never convert and never wait.
* Only a mutation converts, inline in its own transaction (the writer's), so
  the change and the conversion commit or roll back together.
* The ``before_commit`` hook below rewrites ``chats.messages`` from the rows
  of every chat changed in the transaction, so the previous image can be
  rolled back to at any committed state and sees every transcript.
* The schema trigger ``chats_messages_written`` deletes the state row whenever
  ``chats.messages`` is updated. The hook re-inserts it right after its own
  update; the previous image never does, so exactly the chats it changed (or
  created) are read from their legacy value again, until converted again.

Search entries and purge cleanup are maintained by schema triggers (see
``schema_migrations._add_transcript_rows``) and need no code here.
"""

from __future__ import annotations

import json
import math
import weakref
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime

from sqlalchemy import (
  DateTime, Text, bindparam, delete, event, func, insert, inspect, select, text, update,
)
from sqlalchemy.orm import Session, object_session

from app import models

EDIT_PREVIEW = 1
HIDDEN = 8
GOAL_COMPLETION = 16
PROSE = 32
DERIVED_CID = 64
ATTACHMENTS = 128

_PROSE_ROLES = ("user", "assistant")
# Set by chat_writer on the actor's own Session.
WRITER_SESSION = "transcript_writer"
_DIRTY = "transcript_dirty"
# New chats whose initial rows this transaction wrote; settled at commit.
_NEW_TRANSCRIPTS = "transcript_new_chats"
_M = models.ChatMessage


# -- Projections -------------------------------------------------------------

def _is_goal_tool(block) -> bool:
  from app.goal_plans import UPDATE_GOAL_TOOLS
  tool = block.get("tool")
  return block.get("type") == "tool" and isinstance(tool, str) and tool in UPDATE_GOAL_TOOLS


def attributes(body) -> dict:
  """Lookup projections of one body; the body itself stays authoritative.

  Total over arbitrary JSON: legacy transcripts may hold any value anywhere,
  and a projection must never be the reason a chat cannot be converted.
  """
  if not isinstance(body, dict):
    return {"message_key": None, "message_id": None, "client_id": None,
            "role": None, "ts": None, "flags": 0}
  from app.chat_writer import cid_of
  # Truthiness, as every reader of `hidden` (and the search rule) uses it.
  hidden = bool(body.get("hidden"))
  flags = HIDDEN if hidden else 0
  if body.get("attachments"):
    flags |= ATTACHMENTS
  blocks = body.get("blocks")
  for block in blocks if isinstance(blocks, list) else []:
    if not isinstance(block, dict):
      continue
    if block.get("attachments"):
      flags |= ATTACHMENTS
    if block.get("type") == "tool" and isinstance(block.get("edit_preview"), dict):
      flags |= EDIT_PREVIEW
    if _is_goal_tool(block):
      flags |= GOAL_COMPLETION
  role = body.get("role") if isinstance(body.get("role"), str) else None
  content = body.get("content")
  # Search prose: the previous release's indexing rule, decided only here.
  # The row triggers copy `content` for rows carrying this flag.
  if not hidden and role in _PROSE_ROLES and isinstance(content, str) and content.strip():
    flags |= PROSE
  # Exactly the writers' identity (cid_of), so one equality lookup matches
  # what every dedupe compares; flagged when derived so coordinates never
  # present a cid the body does not carry.
  client_id = cid_of(body)
  if client_id is not None and not body.get("cid"):
    flags |= DERIVED_CID
  message_id = body.get("id")
  return {"message_key": None if message_id is None else _key(message_id),
          "message_id": message_id, "client_id": client_id, "role": role,
          "ts": body.get("ts"), "flags": flags}


def _key(value) -> str:
  # str() is the identity comparison the writers always used; the ASCII JSON
  # escape makes any id (even a lone surrogate) a storable equality key.
  return json.dumps(str(value))


def _row(chat_id, seq, body) -> dict:
  return {"chat_id": chat_id, "seq": seq, "body": body, **attributes(body)}


def _id(chat) -> str:
  return chat if isinstance(chat, str) else chat.id


def damaged_messages() -> list[dict]:
  return [{
    "role": "assistant",
    "content": "This chat's stored transcript is damaged. "
               "Its original bytes have been preserved for recovery.",
    "blocks": [{"type": "text",
                "content": "Damaged transcript — original preserved for recovery."}],
    "transcript_damage": True,
  }]


# -- Legacy column and conversion -------------------------------------------

_LEGACY_BY_ENGINE: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def legacy_present(db) -> bool:
  """Whether this database still has the previous release's column.

  A schema fact read once per engine. It is false only after the next
  release dropped the column; every database this release creates has it.
  """
  engine = db.get_bind().engine
  known = _LEGACY_BY_ENGINE.get(engine)
  if known is None:
    with engine.connect() as conn:
      known = "messages" in {
        row[1] for row in conn.exec_driver_sql("PRAGMA table_info(chats)")
      }
    _LEGACY_BY_ENGINE[engine] = known
  return known


_ALL_CONVERTED: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def conversion_settled(bind) -> bool:
  """True once every chat on this engine is known converted (or no legacy).

  Recorded by ``mark_all_converted`` after background conversion leaves no
  chat behind. Nothing in this process can unconvert a chat afterwards (its
  own mirror re-marks), so readers stop consulting the marker.
  """
  engine = bind.engine
  return bool(_ALL_CONVERTED.get(engine))


def mark_all_converted(db) -> None:
  if unconverted_count(db) == 0:
    _ALL_CONVERTED[db.get_bind().engine] = True


def reset_conversion_facts() -> None:
  """Forget per-engine facts; tests reuse one engine across databases."""
  _ALL_CONVERTED.clear()
  _LEGACY_BY_ENGINE.clear()


def rows_are_authority(db) -> bool:
  """Whether every chat's rows are authoritative, with no per-chat check."""
  return not legacy_present(db) or conversion_settled(db.get_bind())


def is_converted(db, chat_id: str) -> bool:
  if rows_are_authority(db):
    return True
  return db.execute(text(
    "SELECT 1 FROM chat_transcript_state WHERE chat_id = :id"
  ), {"id": chat_id}).first() is not None


_NULL = "The stored transcript is JSON null"


def _parse_legacy(raw: bytes) -> tuple[list, str | None]:
  """Decode a legacy value as the previous release reads it.

  Returns the messages and why the stored bytes are not exactly a message
  list: damage (with the visible placeholder as the messages), or ``_NULL``,
  the empty chat the previous release displayed for a JSON null.
  """
  try:
    messages = json.loads(raw)
  except (ValueError, UnicodeError) as exc:
    return damaged_messages(), f"The stored transcript is not valid JSON: {exc}"
  if messages is None:
    return [], _NULL
  if not isinstance(messages, list):
    return damaged_messages(), "The stored transcript is not a message list"
  return messages, None


def _legacy_raw(db, chat_id: str) -> bytes | None:
  row = db.execute(text(
    "SELECT CAST(c.messages AS BLOB) FROM chats c WHERE c.id = :id AND NOT EXISTS "
    "(SELECT 1 FROM chat_transcript_state s WHERE s.chat_id = c.id)"
  ), {"id": chat_id}).first()
  return None if row is None else bytes(row[0] or b"")


def legacy_messages(db, chat) -> list | None:
  """An unconverted chat's authoritative legacy value, decoded; else None.

  None means the rows are authoritative (or the chat does not exist, whose
  rows are then simply empty). Reads nothing for a converted chat beyond the
  marker, and writes nothing.
  """
  if not isinstance(chat, str) and inspect(chat).pending:
    db.flush()  # A chat created in this session is converted from birth.
  if rows_are_authority(db):
    return None
  raw = _legacy_raw(db, _id(chat))
  return None if raw is None else _parse_legacy(raw)[0]


def convert(db, chat_id: str) -> bool:
  """Make one chat's rows authoritative from its legacy value, in ``db``'s
  transaction. Returns whether anything was converted.

  Valid JSON is not rewritten: its rows decode to the same values. Damaged
  bytes are preserved before the legacy value is replaced by the visible
  placeholder that the rows then hold. Rows left from an earlier conversion
  (the previous release has written since) are rewritten by position, so
  only positions it changed are touched.
  """
  if is_converted(db, chat_id):
    return False
  raw = _legacy_raw(db, chat_id)
  if raw is None:
    return False
  messages, error = _parse_legacy(raw)
  _rewrite(db, chat_id, messages)
  if error is not None:
    if error != _NULL:
      db.add(models.ChatTranscriptDamage(chat_id=chat_id, raw=raw, error=error))
    # The mirror replaces the stored bytes with the list the rows now hold.
    _changed(db, chat_id)
  db.execute(text("INSERT OR IGNORE INTO chat_transcript_state(chat_id) VALUES (:id)"),
             {"id": chat_id})
  return True


def next_unconverted(db, after: str | None) -> str | None:
  """The next chat in id order without authoritative rows (keyset scan)."""
  return db.execute(text(
    "SELECT c.id FROM chats c WHERE (:after IS NULL OR c.id > :after) "
    "AND NOT EXISTS (SELECT 1 FROM chat_transcript_state s WHERE s.chat_id = c.id) "
    "ORDER BY c.id LIMIT 1"
  ), {"after": after}).scalar()


def unconverted_count(db) -> int:
  if rows_are_authority(db):
    return 0
  return db.execute(text(
    "SELECT COUNT(*) FROM chats c WHERE NOT EXISTS "
    "(SELECT 1 FROM chat_transcript_state s WHERE s.chat_id = c.id)"
  )).scalar()


def _rows_for_write(db, chat) -> str:
  """Make the chat's rows authoritative before a mutation, in its transaction."""
  chat_id = _id(chat)
  if not isinstance(chat, str) and inspect(chat).pending:
    db.flush()  # A chat created in this session is converted from birth.
  convert(db, chat_id)
  return chat_id


# -- The legacy mirror: one write path ---------------------------------------

def _changed(db, chat) -> None:
  db.info.setdefault(_DIRTY, set()).add(_id(chat))


# Bodies in position order through the primary key, so no sort is needed;
# joined in Python (SQLite's ordered group_concat builds a temporary B-tree).
_BODIES = text("SELECT body FROM chat_messages WHERE chat_id = :id ORDER BY seq")
_MIRROR = text(
  "UPDATE chats SET messages = :messages, has_messages = :has_messages, "
  "updated_at = :now WHERE id = :id"
).bindparams(bindparam("now", type_=DateTime))
_SCALARS = text(
  "UPDATE chats SET has_messages = EXISTS (SELECT 1 FROM chat_messages WHERE chat_id = :id), "
  "updated_at = :now WHERE id = :id"
).bindparams(bindparam("now", type_=DateTime))
_MARK_CONVERTED = text(
  "INSERT OR IGNORE INTO chat_transcript_state(chat_id) SELECT id FROM chats WHERE id = :id"
)


@event.listens_for(Session, "before_commit")
def _mirror_changed_transcripts(session) -> None:
  """Derive each changed chat's legacy value and scalars from its rows.

  Each body holds default (ASCII) ``json.dumps`` text, so ``'[' + ',
  '.join(bodies) + ']'`` is exactly ``json.dumps(list)``: the bytes the
  previous release writes and decodes itself. One update per changed chat
  per root commit.

  The changed set belongs to the root transaction (cleared only when it
  ends, below), so a rolled-back savepoint or a failed and retried commit
  still mirrors every chat the committing transaction changed. Mirroring a
  chat whose savepoint change was rolled back is harmless: the mirror is
  derived from the rows as they commit.
  """
  if session.in_nested_transaction():
    return  # A savepoint release; the root commit mirrors.
  session.flush()
  dirty = session.info.get(_DIRTY)
  if not dirty:
    return
  legacy = legacy_present(session)
  now = datetime.now(UTC)
  for chat_id in sorted(dirty):
    params = {"id": chat_id, "now": now}
    if legacy:
      bodies = session.execute(_BODIES, {"id": chat_id}).scalars().all()
      session.execute(_MIRROR, {**params, "messages": "[" + ", ".join(bodies) + "]",
                                "has_messages": bool(bodies)})
      # chats_messages_written just deleted the state row; this release's own
      # mirror leaves the chat converted.
      session.execute(_MARK_CONVERTED, {"id": chat_id})
    else:
      session.execute(_SCALARS, params)


@event.listens_for(Session, "after_commit")
def _settle_new_chat_transcripts(session) -> None:
  for chat in session.info.pop(_NEW_TRANSCRIPTS, ()):
    chat.__dict__.pop("_initial_transcript", None)


@event.listens_for(Session, "after_transaction_end")
def _forget_changes_with_their_transaction(session, transaction) -> None:
  # Root commit, rollback or close; never a savepoint inside it.
  if transaction.parent is None:
    session.info.pop(_DIRTY, None)
    session.info.pop(_NEW_TRANSCRIPTS, None)


# -- Mutations (writer domain commands only) --------------------------------

def _insert(db, chat_id: str, start: int, messages) -> None:
  rows = [_row(chat_id, start + i, body) for i, body in enumerate(messages)]
  if rows:
    db.execute(insert(_M.__table__), rows)


def _size(db, chat_id: str) -> int:
  last = db.execute(select(func.max(_M.seq)).where(_M.chat_id == chat_id)).scalar()
  return 0 if last is None else last + 1


def initialize_new(chat, messages) -> None:
  """Attach a new chat's initial transcript; ``chat_writer.create_chat`` owns
  this. The chat has no session yet, so its rows are written when the
  session it joins next flushes (below). It is authoritative from birth.
  """
  chat._initial_transcript = list(messages)


@event.listens_for(Session, "before_flush")
def _write_new_chat_transcripts(session, _context, _instances) -> None:
  new = [obj for obj in session.new
         if isinstance(obj, models.Chat) and hasattr(obj, "_initial_transcript")]
  if not new:
    return
  legacy = legacy_present(session)
  for chat in new:
    # Kept on the object until the transaction commits: a failed flush or
    # commit rolls these rows back, and a retry of the same object writes
    # them again.
    messages = chat._initial_transcript
    session.info.setdefault(_NEW_TRANSCRIPTS, []).append(chat)
    if legacy:
      # The previous release's NOT NULL column has no default on databases
      # it created; the commit mirror replaces this placeholder.
      chat.legacy_messages = []
      session.execute(text("INSERT INTO chat_transcript_state(chat_id) VALUES (:id)"),
                      {"id": chat.id})
    _insert(session, chat.id, 0, messages)
    _changed(session, chat.id)


def append(db, chat, message) -> int:
  chat_id = _rows_for_write(db, chat)
  seq = _size(db, chat_id)
  _insert(db, chat_id, seq, [message])
  _changed(db, chat_id)
  return seq


def append_many(db, chat, messages) -> None:
  chat_id = _rows_for_write(db, chat)
  _insert(db, chat_id, _size(db, chat_id), list(messages))
  _changed(db, chat_id)


def _stored_text(db, chat_id: str, seq: int):
  return db.execute(select(_M.body.cast(Text)).where(
    _M.chat_id == chat_id, _M.seq == seq,
  )).scalar()


def update_at(db, chat, index: int, body) -> None:
  chat_id = _rows_for_write(db, chat)
  stored = _stored_text(db, chat_id, index)
  if stored is None:
    raise IndexError(index)
  if stored == json.dumps(body):
    return
  db.execute(update(_M).where(_M.chat_id == chat_id, _M.seq == index).values(
    body=body, **attributes(body),
  ))
  _changed(db, chat_id)


def _rewrite(db, chat_id: str, messages: list) -> bool:
  """Make the rows equal ``messages``, writing only positions that differ."""
  stored = dict(db.execute(text(
    "SELECT seq, body FROM chat_messages WHERE chat_id = :id"
  ), {"id": chat_id}).tuples().all())
  changed = False
  for index, body in enumerate(messages):
    if index in stored and stored[index] != json.dumps(body):
      db.execute(update(_M).where(_M.chat_id == chat_id, _M.seq == index).values(
        body=body, **attributes(body),
      ))
      changed = True
  if len(messages) > len(stored):
    _insert(db, chat_id, len(stored), messages[len(stored):])
    changed = True
  elif len(messages) < len(stored):
    db.execute(delete(_M).where(_M.chat_id == chat_id, _M.seq >= len(messages)))
    changed = True
  return changed


def replace_all(db, chat, messages) -> None:
  """Explicit whole-history operations rewrite only positions that changed."""
  chat_id = _rows_for_write(db, chat)
  if _rewrite(db, chat_id, list(messages)):
    _changed(db, chat_id)


# -- Reads -------------------------------------------------------------------
# Each reader takes one decision: an unconverted chat (``legacy_messages``)
# is read from its legacy value, every other chat from its rows.

def pin_read_snapshot(db) -> None:
  """Keep one read owner's metadata and body windows on one SQLite snapshot.

  Python's legacy sqlite3 transaction mode issues no BEGIN for SELECT, so a
  concurrently settled reply could otherwise move a page's coordinates
  between its reads. Taken before the first read, so the conversion marker
  and whichever value it selects are read from the same snapshot.
  """
  connection = db.connection()
  if connection.dialect.name == "sqlite":
    raw = connection.connection.driver_connection
    if not raw.in_transaction:
      connection.exec_driver_sql("BEGIN")


def count(db, chat) -> int:
  legacy = legacy_messages(db, chat)
  return len(legacy) if legacy is not None else _size(db, _id(chat))


def at(db, chat, index: int):
  """The body at ``index`` (negative from the end); None outside the range."""
  legacy = legacy_messages(db, chat)
  if legacy is not None:
    return legacy[index] if -len(legacy) <= index < len(legacy) else None
  chat_id = _id(chat)
  if index < 0:
    index += _size(db, chat_id)
  if index < 0:
    return None
  row = db.execute(select(_M.body).where(_M.chat_id == chat_id, _M.seq == index)).first()
  return None if row is None else row[0]


def _window(db, chat_id: str, start: int, end: int) -> list:
  return list(db.execute(select(_M.body).where(
    _M.chat_id == chat_id, _M.seq >= start, _M.seq < end,
  ).order_by(_M.seq)).scalars())


def _stream(db, chat_id: str, *, descending: bool = False,
            below: int | None = None) -> Iterator[tuple[int, object]]:
  # One statement is one consistent SQLite snapshot, without row-count batches.
  order = _M.seq.desc() if descending else _M.seq
  condition = _M.chat_id == chat_id
  if below is not None:
    condition = condition & (_M.seq < below)
  yield from db.execute(select(_M.seq, _M.body).where(condition).order_by(order)).tuples()


def iterate(db, chat) -> Iterator:
  legacy = legacy_messages(db, chat)
  if legacy is not None:
    return iter(legacy)
  return (body for _seq, body in _stream(db, _id(chat)))


def reverse_iter(db, chat) -> Iterator[tuple[int, object]]:
  legacy = legacy_messages(db, chat)
  if legacy is not None:
    return ((index, legacy[index]) for index in range(len(legacy) - 1, -1, -1))
  return _stream(db, _id(chat), descending=True)


def read_all(db, chat) -> list:
  legacy = legacy_messages(db, chat)
  if legacy is not None:
    return legacy
  return [body for _seq, body in _stream(db, _id(chat))]


def assistant_index(db, chat, message) -> int:
  """Position of the assistant row this message updates, or -1 to append."""
  key = message.get("id") if isinstance(message, dict) else None
  legacy = legacy_messages(db, chat)
  if legacy is not None:
    projected = [attributes(body) for body in legacy]
    if key is not None:
      for seq, row in enumerate(projected):
        if row["role"] == "assistant" and row["message_key"] == _key(key):
          return seq
    if not projected:
      return -1
    last = projected[-1]
    if last["role"] == "assistant" and (key is None or last["message_id"] is None):
      return len(projected) - 1
    return -1
  chat_id = _id(chat)
  if key is not None:
    found = db.execute(select(_M.seq).where(
      _M.chat_id == chat_id, _M.role == "assistant", _M.message_key == _key(key),
    ).order_by(_M.seq).limit(1)).scalar()
    if found is not None:
      return found
  size = _size(db, chat_id)
  if not size:
    return -1
  last = db.execute(select(_M.role, _M.message_id).where(
    _M.chat_id == chat_id, _M.seq == size - 1,
  )).first()
  if last.role == "assistant" and (key is None or last.message_id is None):
    return size - 1
  return -1


def client_message_seq(db, chat, client_id: str, *, role: str = "user") -> int | None:
  """Position of the first ``role`` row whose cid (``cid_of``) matches."""
  legacy = legacy_messages(db, chat)
  if legacy is not None:
    return next((seq for seq, body in enumerate(legacy)
                 if (row := attributes(body))["role"] == role
                 and row["client_id"] == client_id), None)
  return db.execute(select(_M.seq).where(
    _M.chat_id == _id(chat), _M.role == role, _M.client_id == client_id,
  ).order_by(_M.seq).limit(1)).scalar()


def attachment_bodies(db, chat) -> list:
  """Bodies of the rows that name attachments (on the message or a block).

  Upload release asks whether anything still names a file; only these rows
  can, so a converted chat never decodes the rest of its history.
  """
  legacy = legacy_messages(db, chat)
  if legacy is not None:
    return [body for body in legacy if attributes(body)["flags"] & ATTACHMENTS]
  return list(db.execute(select(_M.body).where(
    _M.chat_id == _id(chat), _M.flags.op("&")(ATTACHMENTS) != 0,
  ).order_by(_M.seq)).scalars())


def _numeric_ts(value) -> bool:
  return (isinstance(value, (int, float)) and not isinstance(value, bool)
          and not (isinstance(value, float) and not math.isfinite(value)))


def max_timestamp(db, chat):
  """Largest finite numeric ``ts`` (never bool), exact as stored; 0 when none."""
  legacy = legacy_messages(db, chat)
  if legacy is not None:
    stamps = [body.get("ts") for body in legacy if isinstance(body, dict)]
    return max((ts for ts in stamps if _numeric_ts(ts)), key=float, default=0)
  value = db.execute(text(
    "SELECT ts FROM chat_messages WHERE chat_id = :id "
    "AND json_type(ts) IN ('integer', 'real') "
    "ORDER BY CAST(ts AS REAL) DESC LIMIT 1"
  ), {"id": _id(chat)}).scalar()
  return 0 if value is None else json.loads(value)


def _coordinates(message_id, client_id, role, ts, flags) -> dict:
  message = {"role": role, "ts": ts, "hidden": bool(flags & HIDDEN)}
  if message_id is not None:
    message["id"] = message_id
  if client_id is not None and not flags & DERIVED_CID:
    message["cid"] = client_id
  return message


def metadata(db, chat) -> list[dict]:
  """Identity and lifecycle coordinates without hydrating ordinary bodies."""
  legacy = legacy_messages(db, chat)
  if legacy is not None:
    result = []
    for body in legacy:
      row = attributes(body)
      message = _coordinates(row["message_id"], row["client_id"], row["role"],
                             row["ts"], row["flags"])
      if row["flags"] & GOAL_COMPLETION:
        message["blocks"] = body.get("blocks", [])
      result.append(message)
    return result
  chat_id = _id(chat)
  result, goal_rows = [], []
  for seq, message_id, client_id, role, ts, flags in db.execute(select(
    _M.seq, _M.message_id, _M.client_id, _M.role, _M.ts, _M.flags,
  ).where(_M.chat_id == chat_id).order_by(_M.seq)).tuples():
    if flags & GOAL_COMPLETION:
      goal_rows.append(seq)
    result.append(_coordinates(message_id, client_id, role, ts, flags))
  if goal_rows:
    # Goal placement needs those rows' blocks; only they are decoded.
    for seq, body in db.execute(select(_M.seq, _M.body).where(
      _M.chat_id == chat_id, _M.seq.in_(goal_rows),
    )).tuples():
      result[seq]["blocks"] = body.get("blocks", []) if isinstance(body, dict) else []
  return result


def chats_with_flag(db, flag: int, legacy_marker: str):
  """Chat ids whose transcript may hold a row with ``flag``, as a subquery.

  Converted chats answer from their rows' flags; while the legacy column
  exists, an unconverted chat qualifies when its legacy text contains
  ``legacy_marker`` (a superset the caller's exact body check then narrows).
  """
  flagged = select(_M.chat_id).where(_M.flags.op("&")(flag) != 0)
  if rows_are_authority(db):
    return flagged
  unconverted = select(models.Chat.id).where(
    ~select(models.ChatTranscriptState.chat_id).where(
      models.ChatTranscriptState.chat_id == models.Chat.id,
    ).exists(),
    func.instr(text("CAST(chats.messages AS TEXT)"), legacy_marker) > 0,
  )
  return flagged.union(unconverted)


class History(Sequence):
  """A position-addressed view of one chat's transcript, sized when opened.

  An unconverted chat's legacy value is decoded once, when opened. For rows,
  iteration streams the current rows; indexing a position that has since
  vanished raises IndexError instead of returning a placeholder.
  """

  def __init__(self, chat):
    self.chat = chat
    self.db = object_session(chat)
    if self.db is None:
      raise RuntimeError("Transcript reads require the chat's database session")
    self.legacy = legacy_messages(self.db, chat)
    self.size = len(self.legacy) if self.legacy is not None else _size(self.db, chat.id)

  def __len__(self):
    return self.size

  def __getitem__(self, key):
    if isinstance(key, slice):
      start, stop, step = key.indices(self.size)
      if self.legacy is not None:
        return self.legacy[start:stop:step]
      if step == 1:
        return _window(self.db, self.chat.id, start, stop)
      return [self[index] for index in range(start, stop, step)]
    index = key + self.size if key < 0 else key
    if index < 0 or index >= self.size:
      raise IndexError(key)
    if self.legacy is not None:
      return self.legacy[index]
    row = self.db.execute(select(_M.body).where(
      _M.chat_id == self.chat.id, _M.seq == index,
    )).first()
    if row is None:
      raise IndexError(key)
    return row[0]

  def __iter__(self):
    # Bounded by the size taken at opening, so iteration never yields more
    # items than len(); positions removed since then are simply absent.
    if self.legacy is not None:
      return iter(self.legacy)
    if not self.size:
      return iter(())  # An empty transcript is known from opening; read nothing.
    return (body for _seq, body in _stream(self.db, self.chat.id, below=self.size))

  def items_reversed(self) -> Iterator[tuple[int, object]]:
    """(position, body) from the last position, bounded like iteration."""
    if self.legacy is not None:
      return ((index, self.legacy[index]) for index in range(self.size - 1, -1, -1))
    if not self.size:
      return iter(())
    return _stream(self.db, self.chat.id, descending=True, below=self.size)

  def __reversed__(self):
    return (body for _seq, body in self.items_reversed())


def history(chat) -> History:
  return History(chat)
