"""Concrete one-way conversion from legacy chat values to message rows.

Import-light because image compatibility is checked before database imports.
The original bytes remain authoritative until activation and are archived by
the gate. Only verified copies allow clearing the hot legacy column later.
"""

from __future__ import annotations

import json
import sqlite3

from app.one_way_upgrades import BATCH_BYTES, DamagedUnit, OneWayStep, PostTask, StepRefusal


class TranscriptStep(OneWayStep):
  level = 1
  name = "transcript_rows"
  unit_table = "chats"
  unit_key = "id"
  authoritative_tables = ("chat_messages", "chat_transcript_state")
  owned_tables = (
    ("chat_messages", "chat_id"),
    ("chat_transcript_state", "chat_id"),
    ("chat_transcript_cleanup", "chat_id"),
    ("chat_search_docs_v2", "chat_id"),
    ("chat_search_state_v2", "chat_id"),
  )

  def legacy_present(self, conn):
    return "messages" in {row[1] for row in conn.execute("PRAGMA table_info(chats)")}

  def create_owned_tables(self, conn):
    # These are the same models create_all makes on an ordinary boot. SQLite
    # migration copies use the identical declarations, without opening an ORM
    # session on the dedicated gate connection.
    from sqlalchemy.dialects.sqlite import dialect
    from sqlalchemy.schema import CreateIndex, CreateTable
    from app import models
    for model in (models.ChatMessage, models.ChatTranscriptState):
      conn.execute(str(CreateTable(model.__table__, if_not_exists=True).compile(dialect=dialect())))
      for index in model.__table__.indexes:
        conn.execute(str(CreateIndex(index, if_not_exists=True).compile(dialect=dialect())))
    conn.execute("CREATE TABLE IF NOT EXISTS chat_transcript_cleanup "
                 "(chat_id VARCHAR(64) PRIMARY KEY, cleared INTEGER NOT NULL DEFAULT 0)")
    from app.chat_search import create_schema_v2
    create_schema_v2(conn)

  def iter_unit_ids(self, conn):
    # Close the cursor before the gate commits a batch on this connection.
    return [row[0] for row in conn.execute("SELECT id FROM chats ORDER BY id").fetchall()]

  def read_raw(self, conn, unit_id):
    row = conn.execute("SELECT CAST(messages AS BLOB) FROM chats WHERE id=?", (unit_id,)).fetchone()
    return None if row is None else bytes(row[0] or b"")

  def _write_messages(self, conn, unit_id, messages):
    from app.transcript_rows import attributes
    conn.execute("DELETE FROM chat_messages WHERE chat_id=?", (unit_id,))
    conn.executemany(
      "INSERT INTO chat_messages(chat_id,seq,message_key,message_id,client_id,role,ts,flags,body) VALUES(?,?,?,?,?,?,?,?,?)",
      [(unit_id, i, attrs["message_key"], json.dumps(attrs["message_id"]), json.dumps(attrs["client_id"]),
        json.dumps(attrs["role"]), json.dumps(attrs["ts"]),
        attrs["flags"], json.dumps(body))
       for i, body in enumerate(messages) for attrs in [attributes(body)]],
    )
    conn.execute(
      "INSERT INTO chat_transcript_state(chat_id,message_count,revision) VALUES(?,?,1) "
      "ON CONFLICT(chat_id) DO UPDATE SET message_count=excluded.message_count, "
      "revision=chat_transcript_state.revision+1", (unit_id, len(messages)),
    )
    conn.execute("INSERT INTO chat_transcript_cleanup(chat_id,cleared) VALUES(?,0) "
                 "ON CONFLICT(chat_id) DO UPDATE SET cleared=0", (unit_id,))

  def convert(self, conn, unit_id, raw):
    try:
      messages = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
      raise DamagedUnit("The stored transcript is not valid JSON") from exc
    if not isinstance(messages, list):
      raise DamagedUnit("The stored transcript is not a message list")
    self._write_messages(conn, unit_id, messages)

  def convert_damaged(self, conn, unit_id, error):
    self._write_messages(conn, unit_id, self.damaged_messages())

  def damaged_messages(self):
    return [{
      "role": "assistant", "content": "This chat's stored transcript is damaged. "
      "Its original bytes have been preserved for recovery.",
      "blocks": [{"type": "text", "content": "Damaged transcript — original preserved for recovery."}],
      "transcript_damage": True,
    }]

  def prepared_matches(self, conn, unit_id, raw):
    try:
      messages = json.loads(raw)
      if not isinstance(messages, list):
        messages = self.damaged_messages()
    except (ValueError, UnicodeError):
      messages = self.damaged_messages()
    state = conn.execute("SELECT message_count FROM chat_transcript_state WHERE chat_id=?",
                         (unit_id,)).fetchone()
    if state is None or state[0] != len(messages):
      return False
    from app.transcript_rows import attributes
    cleanup = conn.execute("SELECT cleared FROM chat_transcript_cleanup WHERE chat_id=?", (unit_id,)).fetchone()
    if cleanup is None or cleanup[0] != 0:
      return False
    cursor = conn.execute("SELECT seq,body,message_key,message_id,client_id,role,ts,flags "
                          "FROM chat_messages WHERE chat_id=? ORDER BY seq", (unit_id,))
    for index, expected in enumerate(messages):
      row = cursor.fetchone()
      attrs = attributes(expected)
      expected_row = (index, json.dumps(expected), attrs["message_key"],
                      json.dumps(attrs["message_id"]), json.dumps(attrs["client_id"]),
                      json.dumps(attrs["role"]), json.dumps(attrs["ts"]), attrs["flags"])
      if row is None or tuple(row) != expected_row:
        cursor.close()
        return False
    extra = cursor.fetchone()
    cursor.close()
    return extra is None

  def remove_unit(self, conn, unit_id):
    for table, key in self.owned_tables:
      conn.execute(f"DELETE FROM {table} WHERE {key}=?", (unit_id,))

  def activation_schema_edits(self, conn):
    conn.execute("ALTER TABLE chats RENAME COLUMN messages TO messages_v1")
    conn.execute("ALTER TABLE chat_search_state RENAME TO chat_search_state_v1")

  def post_tasks(self):
    from app.chat_search import index_batch_space_bytes
    return (
      PostTask("clear_legacy_values", self.clear_legacy_batch, self.clear_space_bytes),
      PostTask("index_messages", self.index_batch, index_batch_space_bytes),
      PostTask("retire_legacy_search", self.retire_search_batch),
      PostTask("sweep_orphans", self.sweep_orphans),
    )

  def clear_legacy_batch(self, conn):
    from app.one_way_upgrades import verified_legacy_copy
    if "messages_v1" not in {row[1] for row in conn.execute("PRAGMA table_info(chats)")}:
      return 0, 0
    candidates = conn.execute("SELECT c.id,length(CAST(c.messages_v1 AS BLOB)),s.message_count "
                        "FROM chats c JOIN chat_transcript_cleanup p ON p.chat_id=c.id "
                        "JOIN chat_transcript_state s ON s.chat_id=c.id "
                        "WHERE p.cleared=0 ORDER BY c.id LIMIT 16").fetchall()
    cleared = 0
    batch_bytes = 0
    for cid, length, size in candidates:
      if cleared and batch_bytes + (length or 0) > BATCH_BYTES:
        break
      raw = conn.execute("SELECT CAST(messages_v1 AS BLOB) FROM chats WHERE id=?", (cid,)).fetchone()[0]
      archived = verified_legacy_copy(conn, self.level, cid)
      if archived is None or archived != bytes(raw or b""):
        raise StepRefusal("upgrade_archive_mismatch",
                          "The preserved chat copy does not verify; the original was not cleared.",
                          chat_id=cid)
      conn.execute("UPDATE chats SET messages_v1='[]',has_messages=? WHERE id=?", (int(size > 0), cid))
      conn.execute("UPDATE chat_transcript_cleanup SET cleared=1 WHERE chat_id=?", (cid,))
      cleared += 1
      batch_bytes += len(raw or b"")
    remaining = conn.execute("SELECT COUNT(*) FROM chat_transcript_cleanup WHERE cleared=0").fetchone()[0]
    return cleared, remaining

  def clear_space_bytes(self, conn):
    largest = conn.execute(
      "SELECT MAX(length(CAST(c.messages_v1 AS BLOB))) FROM chats c "
      "JOIN chat_transcript_cleanup p ON p.chat_id=c.id WHERE p.cleared=0"
    ).fetchone()[0] or 0
    # A single message/chat may exceed the ordinary batch bound. Its verified
    # archive and rewrite are indivisible, so reserve for that unit's WAL too.
    return 2 * max(BATCH_BYTES, largest) + 1024 * 1024

  def index_batch(self, conn):
    from app.chat_search import index_batch
    return index_batch(conn)

  def retire_search_batch(self, conn):
    # Derived search remains unavailable to old code from activation onward.
    # Reclamation is data-sized work, never part of the activation lock.
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "chat_search_docs" in names:
      before = conn.total_changes
      conn.execute("DELETE FROM chat_search_docs WHERE id IN "
                   "(SELECT id FROM chat_search_docs LIMIT 256)")
      removed = conn.total_changes - before
      remaining = conn.execute("SELECT COUNT(*) FROM chat_search_docs").fetchone()[0]
      if remaining:
        return removed, remaining
      for trigger in ("chat_search_docs_ai", "chat_search_docs_ad", "chat_search_docs_au"):
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
      conn.execute("DROP TABLE IF EXISTS chat_search_fts")
      conn.execute("DROP TABLE chat_search_docs")
    conn.execute("DROP TABLE IF EXISTS chat_search_state_v1")
    return 0, 0

  def sweep_orphans(self, conn):
    done = 0
    for table, key in self.owned_tables:
      before = conn.total_changes
      conn.execute(f"DELETE FROM {table} WHERE {key} IN "
                   f"(SELECT {key} FROM {table} WHERE NOT EXISTS "
                   f"(SELECT 1 FROM chats WHERE chats.id={table}.{key}) LIMIT 256)")
      done += conn.total_changes - before
    remaining = sum(conn.execute(
      f"SELECT COUNT(*) FROM {table} WHERE NOT EXISTS "
      f"(SELECT 1 FROM chats WHERE chats.id={table}.{key})"
    ).fetchone()[0] for table, key in self.owned_tables)
    return done, remaining
