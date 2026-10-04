"""Position-addressed transcript persistence, inside the writer transaction.

Optional message ids are lookup hints, never primary keys. Every operation
leaves commit/rollback to its domain-command caller. Readers query bodies in
bounded windows; metadata is sufficient for identity and lifecycle placement.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from copy import deepcopy
import hashlib
import json

from sqlalchemy.orm import object_session

from app import models

EDIT_PREVIEW = 1
QUESTION = 2
LEGACY_MEDIA = 4
HIDDEN = 8
GOAL_COMPLETION = 16


def identity_key(value):
  # A bounded lookup hint, never identity authority. Escaped encoding handles
  # arbitrary legacy ids; callers compare original values after the lookup.
  return None if value is None else hashlib.sha256(json.dumps(str(value)).encode("ascii")).hexdigest()


def attributes(body):
  if not isinstance(body, dict):
    return {"message_key": None, "message_id": None, "client_id": None, "role": None, "ts": None, "flags": 0}
  flags = HIDDEN if body.get("hidden") is True else 0
  blocks = body.get("blocks")
  for block in blocks if isinstance(blocks, list) else []:
    if not isinstance(block, dict):
      continue
    if block.get("type") == "edit_preview":
      flags |= EDIT_PREVIEW
    if block.get("type") == "question":
      flags |= QUESTION
    if block.get("type") == "tool" and "update_goal" in str(block.get("tool", "")):
      flags |= GOAL_COMPLETION
  # Legacy repair and contribution discovery deliberately keep their former
  # serialized predicates; flags avoid hydrating every large body at lookup.
  serialized = json.dumps(body)
  if '"edit_preview"' in serialized:
    flags |= EDIT_PREVIEW
  if "/api/chats/" in serialized and ("/media/" in serialized or "/generated/" in serialized):
    flags |= LEGACY_MEDIA
  key = body.get("id")
  if key is None:
    key = body.get("cid")
  return {"message_key": identity_key(key),
          "message_id": body.get("id"), "client_id": body.get("cid"),
          "role": body.get("role") if isinstance(body.get("role"), str) else None,
          "ts": body.get("ts"), "flags": flags}


def _id(chat):
  return chat if isinstance(chat, str) else chat.id


def pin_read_snapshot(db):
  """Keep metadata and body windows on one actual SQLite read transaction.

  Python's legacy sqlite3 transaction mode does not issue BEGIN for SELECT,
  even when SQLAlchemy reports a transaction. Without this, a concurrently
  settled live reply could change a page's coordinates between its reads.
  Writer primitives deliberately do not invoke this read-only boundary.
  """
  connection = db.connection()
  if connection.dialect.name == "sqlite":
    raw = connection.connection.driver_connection
    if not raw.in_transaction:
      connection.exec_driver_sql("BEGIN")


def count(db, chat):
  # The platform Session disables autoflush. A domain command can append and
  # then inspect that row before committing; make its own changes query-visible
  # without ending the command's transaction or acknowledging it early.
  db.flush()
  # A route may have cached the state before an external writer ack. Scalar
  # reads honor the current transaction, not ORM identity-map authority.
  state = db.query(models.ChatTranscriptState.message_count).filter(
    models.ChatTranscriptState.chat_id == _id(chat),
  ).first()
  return state[0] if state is not None else 0


def _changed(db, chat, size):
  cid = _id(chat)
  state = db.get(models.ChatTranscriptState, cid)
  if state is None:
    state = models.ChatTranscriptState(chat_id=cid, message_count=size, revision=1)
    db.add(state)
  else:
    state.message_count = size
    state.revision += 1
  owner = db.get(models.Chat, cid) if isinstance(chat, str) else chat
  if owner is not None:
    owner.has_messages = bool(size)


def initialize_new(chat, messages):
  """Seed a newly constructed chat; the domain creation command owns this."""
  chat.transcript_rows = [
    models.ChatMessage(chat_id=chat.id, seq=i, body=deepcopy(body), **attributes(body))
    for i, body in enumerate(messages)
  ]
  chat.transcript_state = models.ChatTranscriptState(
    chat_id=chat.id, message_count=len(messages), revision=1 if messages else 0,
  )
  chat.has_messages = bool(messages)


def at(db, chat, index):
  size = count(db, chat)
  if index < 0:
    index += size
  if index < 0 or index >= size:
    return None
  row = db.query(models.ChatMessage.body).filter(
    models.ChatMessage.chat_id == _id(chat), models.ChatMessage.seq == index,
  ).first()
  if row is None:
    raise RuntimeError("Transcript position is missing from its recorded range")
  return row[0]


def window(db, chat, start, end):
  return [body for (body,) in db.query(models.ChatMessage.body).filter(
    models.ChatMessage.chat_id == _id(chat), models.ChatMessage.seq >= start,
    models.ChatMessage.seq < end,
  ).order_by(models.ChatMessage.seq).all()]


def iterate(db, chat, batch_size=64) -> Iterator:
  size = count(db, chat)
  for start in range(0, size, batch_size):
    yield from window(db, chat, start, min(size, start + batch_size))


def reverse_iter(db, chat, batch_size=64) -> Iterator:
  end = count(db, chat)
  while end:
    start = max(0, end - batch_size)
    batch = window(db, chat, start, end)
    yield from ((start + i, body) for i, body in reversed(list(enumerate(batch))))
    end = start


def tail(db, chat, n=1):
  size = count(db, chat)
  return window(db, chat, max(0, size - n), size)


def read_all(db, chat):
  return list(iterate(db, chat))


def assistant_index(db, chat, message):
  key = message.get("id") if isinstance(message, dict) else None
  if key is not None:
    matches = db.query(models.ChatMessage.seq, models.ChatMessage.message_id).filter(
      models.ChatMessage.chat_id == _id(chat), models.ChatMessage.role == "assistant",
      models.ChatMessage.message_key == identity_key(key),
    ).order_by(models.ChatMessage.seq).all()
    for index, candidate_id in matches:
      if candidate_id is not None and str(candidate_id) == str(key):
        return index
  size = count(db, chat)
  last = at(db, chat, size - 1) if size else None
  if isinstance(last, dict) and last.get("role") == "assistant":
    if key is None or last.get("id") is None:
      return size - 1
  return -1


def max_timestamp(db, chat):
  # Older timestamps need not be present or numeric. This matches the writer's
  # numeric monotonic-clock input without inspecting message bodies.
  values = db.query(models.ChatMessage.ts).filter(
    models.ChatMessage.chat_id == _id(chat),
  ).all()
  return max((ts for (ts,) in values if isinstance(ts, (int, float))
              and not isinstance(ts, bool)), default=0)


def append(db, chat, message):
  seq = count(db, chat)
  db.add(models.ChatMessage(chat_id=_id(chat), seq=seq, body=deepcopy(message),
                            **attributes(message)))
  _changed(db, chat, seq + 1)
  return seq


def append_many(db, chat, messages):
  for body in messages:
    append(db, chat, body)


def update_at(db, chat, index, body):
  row = db.get(models.ChatMessage, (_id(chat), index))
  if row is None:
    raise IndexError(index)
  if models.transcript_values_equal(row.body, body):
    return
  row.body = deepcopy(body)
  for key, value in attributes(body).items():
    setattr(row, key, value)
  _changed(db, chat, count(db, chat))


def replace_all(db, chat, messages):
  """Explicit whole-history operations preserve unchanged row bytes."""
  old_count = count(db, chat)
  for index, body in enumerate(messages):
    if index < old_count:
      update_at(db, chat, index, body)
    else:
      append(db, chat, body)
  if old_count > len(messages):
    db.query(models.ChatMessage).filter(
      models.ChatMessage.chat_id == _id(chat), models.ChatMessage.seq >= len(messages),
    ).delete(synchronize_session="fetch")
    _changed(db, chat, len(messages))


def _projected_rows(db, chat):
  for seq, message_id, client_id, role, ts, flags in db.query(
    models.ChatMessage.seq, models.ChatMessage.message_id, models.ChatMessage.client_id,
    models.ChatMessage.role, models.ChatMessage.ts, models.ChatMessage.flags,
  ).filter(models.ChatMessage.chat_id == _id(chat)).order_by(models.ChatMessage.seq):
    message = {"role": role, "ts": ts, "hidden": bool(flags & HIDDEN)}
    if message_id is not None:
      message["id"] = message_id
    if client_id is not None:
      message["cid"] = client_id
    yield seq, message, flags


def identity_metadata(db, chat):
  """Identity and ordering predicates never need historical body hydration."""
  return [message for _seq, message, _flags in _projected_rows(db, chat)]


def metadata(db, chat):
  result = []
  for seq, message, flags in _projected_rows(db, chat):
    if flags & GOAL_COMPLETION:
      body = at(db, chat, seq)
      message["blocks"] = body.get("blocks", []) if isinstance(body, dict) else []
    result.append(message)
  return result


class History(Sequence):
  """A bounded, position-addressed view; never a second cached authority."""
  def __init__(self, chat):
    self.chat = chat
    self.db = object_session(chat)
    if self.db is None:
      raise RuntimeError("Transcript reads require the chat's database session")
    self.size = count(self.db, chat)

  def __len__(self):
    return self.size

  def __getitem__(self, key):
    if isinstance(key, slice):
      start, stop, step = key.indices(self.size)
      if step == 1:
        return window(self.db, self.chat, start, stop)
      return [self[index] for index in range(start, stop, step)]
    index = key + self.size if key < 0 else key
    if index < 0 or index >= self.size:
      raise IndexError(key)
    return at(self.db, self.chat, index)

  def __iter__(self):
    return iterate(self.db, self.chat)

  def __reversed__(self):
    return (body for _seq, body in reverse_iter(self.db, self.chat))


def history(chat):
  return History(chat)
