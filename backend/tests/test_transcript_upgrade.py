"""Exercise the concrete conversion, not merely a toy upgrade registry."""

import json
import sqlite3

import pytest
from sqlalchemy import create_engine

from app import models, one_way_upgrades as upgrades
from app.database import Base
from app.schema_migrations import _create_chat_search_tables
from app.transcript_upgrade import TranscriptStep


def legacy(tmp_path, values):
  # Step-level hybrid fixture, not a complete previous-release image database.
  path = tmp_path / "legacy.db"
  eng = create_engine(f"sqlite:///{path}")
  Base.metadata.create_all(eng)
  _create_chat_search_tables(eng)
  with eng.begin() as connection:
    connection.exec_driver_sql("ALTER TABLE chats RENAME COLUMN messages_v1 TO messages")
    for cid, raw in values.items():
      connection.exec_driver_sql(
        "INSERT INTO chats(id,title,title_locked,messages,has_messages,pending_messages,uploads,provider,"
        "auto_resume_on_limit,auto_resume_on_restart,created_at,updated_at) "
        "VALUES(?, 'legacy', 0, ?, 1, '[]', '[]', 'claude', 0, 1, '2026-09-27', '2026-09-27')",
        (cid, raw),
      )
  seen = upgrades.preflight(eng)
  upgrades.ensure_compat_record(str(path), seen)
  eng.dispose()
  return path, seen


def items(conn, cid):
  return [json.loads(raw) for (raw,) in conn.execute(
    "SELECT body FROM chat_messages WHERE chat_id=? ORDER BY seq", (cid,),
  )]


def test_conversion_preserves_positions_missing_ids_duplicate_ids_and_original_bytes(tmp_path):
  messages = [{"role": "user", "cid": "u"}, {"role": "assistant", "id": "dup"},
              {"role": "assistant", "id": "dup", "cid": "also"}, {"role": "assistant"},
              {"role": "assistant", "blocks": 42}, None, "legacy scalar",
              {"role": "user", "id": 1.0, "cid": 2**80, "ts": 1.0}, 1.0, 2**80]
  raw = json.dumps(messages, indent=2, ensure_ascii=False)
  path, seen = legacy(tmp_path, {"one": raw})
  upgrades.run_gate(str(path), seen.existing_tables)
  with sqlite3.connect(path) as conn:
    assert items(conn, "one") == messages
    assert conn.execute("SELECT floor FROM platform_compat").fetchone()[0] == 1
    assert conn.execute("SELECT messages_v1 FROM chats").fetchone()[0] == raw
    assert upgrades.verified_legacy_copy(conn, 1, "one") == raw.encode()
    with pytest.raises(sqlite3.OperationalError, match="no such column"):
      conn.execute("UPDATE chats SET messages='[]'")


def test_interrupted_prepare_resumes_without_duplicate_or_missing_messages(tmp_path, monkeypatch):
  raw = json.dumps([{"role": "user", "content": "retained"}])
  path, seen = legacy(tmp_path, {"a": raw, "b": raw})
  monkeypatch.setattr(upgrades, "BATCH_BYTES", 1)
  original = TranscriptStep.convert
  def crash(self, conn, cid, source):
    if cid == "b":
      raise RuntimeError("simulated kill between batches")
    return original(self, conn, cid, source)
  monkeypatch.setattr(TranscriptStep, "convert", crash)
  with pytest.raises(RuntimeError, match="simulated kill"):
    upgrades.run_gate(str(path), seen.existing_tables)
  with sqlite3.connect(path) as conn:
    assert conn.execute("SELECT messages FROM chats WHERE id='a'").fetchone()[0] == raw
    assert conn.execute("SELECT COUNT(*) FROM chat_messages WHERE chat_id='a'").fetchone()[0] == 1
  monkeypatch.setattr(TranscriptStep, "convert", original)
  upgrades.run_gate(str(path), seen.existing_tables)
  with sqlite3.connect(path) as conn:
    assert items(conn, "a") == items(conn, "b") == json.loads(raw)


def test_corrupt_chat_is_quarantined_byte_for_byte_without_blocking_other_chats(tmp_path):
  path, seen = legacy(tmp_path, {"damaged": '{"cut off":', "good": '[{"role":"user"}]'})
  upgrades.run_gate(str(path), seen.existing_tables)
  with sqlite3.connect(path) as conn:
    assert items(conn, "good") == [{"role": "user"}]
    assert items(conn, "damaged")[0]["transcript_damage"] is True
    assert upgrades.verified_legacy_copy(conn, 1, "damaged") == b'{"cut off":'
    assert TranscriptStep().clear_legacy_batch(conn) == (2, 0)
    assert conn.execute("SELECT raw FROM upgrade_quarantine WHERE unit_id='damaged'").fetchone()[0] == b'{"cut off":'


def test_disk_shortage_leaves_legacy_authoritative_and_no_active_floor(tmp_path, monkeypatch):
  raw = '[{"role":"user","content":"not lost"}]'
  path, seen = legacy(tmp_path, {"one": raw})
  monkeypatch.setattr(upgrades, "_free_bytes", lambda _path: 0)
  with pytest.raises(upgrades.StepRefusal) as refused:
    upgrades.run_gate(str(path), seen.existing_tables)
  assert refused.value.database_failure_reason == "upgrade_needs_space"
  with sqlite3.connect(path) as conn:
    assert conn.execute("SELECT messages FROM chats").fetchone()[0] == raw
    assert conn.execute("SELECT floor FROM platform_compat").fetchone()[0] == 0


def test_legacy_raw_write_without_timestamp_change_is_reconverted_before_activation(tmp_path):
  path, seen = legacy(tmp_path, {"one": '[{"role":"user","content":"old"}]'})
  class RacingStep(TranscriptStep):
    wrote = False
    def convert(self, conn, cid, raw):
      super().convert(conn, cid, raw)
      if not self.wrote:
        self.wrote = True
        # This commit deliberately simulates old raw-SQL code, which does not
        # update the ORM timestamp. A full re-hash, not updated_at, must catch it.
        self.pending = True
    def iter_unit_ids(self, conn):
      if getattr(self, "pending", False):
        self.pending = False
        with sqlite3.connect(path) as older:
          older.execute("UPDATE chats SET messages=? WHERE id='one'",
                        ('[{"role":"user","content":"new"}]',))
      return super().iter_unit_ids(conn)
  upgrades.run_gate(str(path), seen.existing_tables, [RacingStep()])
  with sqlite3.connect(path) as conn:
    assert items(conn, "one")[0]["content"] == "new"


def test_deletion_during_prepare_removes_archive_and_rows_even_without_foreign_keys(tmp_path):
  path, seen = legacy(tmp_path, {"one": '[{"role":"user","content":"erase with chat"}]'})
  step = TranscriptStep()
  conn = upgrades.open_pinned(str(path))
  try:
    upgrades._begin_step(conn, step, activate=False)
    upgrades._sync_units(conn, step, str(path))
    assert conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 1
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM chats WHERE id='one'")
    for table in ("chat_messages", "chat_transcript_state", "upgrade_archive", "upgrade_units"):
      assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
  finally:
    conn.close()


def test_archive_mismatch_never_clears_the_only_hot_original(tmp_path):
  raw = '[{"role":"user","content":"original"}]'
  path, seen = legacy(tmp_path, {"one": raw})
  upgrades.run_gate(str(path), seen.existing_tables)
  with sqlite3.connect(path) as conn:
    conn.execute("UPDATE upgrade_archive SET zlib_raw=?", (b"corrupt compressed copy",))
    with pytest.raises(upgrades.StepRefusal):
      TranscriptStep().clear_legacy_batch(conn)
    assert conn.execute("SELECT messages_v1 FROM chats").fetchone()[0] == raw


def test_fresh_database_activates_without_legacy_conversion(tmp_path):
  path = tmp_path / "fresh.db"
  eng = create_engine(f"sqlite:///{path}")
  seen = upgrades.preflight(eng)
  upgrades.ensure_compat_record(str(path), seen)
  Base.metadata.create_all(eng)
  _create_chat_search_tables(eng)
  upgrades.run_gate(str(path), seen.existing_tables)
  with sqlite3.connect(path) as conn:
    columns = {r[1] for r in conn.execute("PRAGMA table_info(chats)")}
    assert "messages" not in columns
    assert "messages_v1" in columns
    assert conn.execute("SELECT COUNT(*) FROM upgrade_archive").fetchone()[0] == 0
    assert conn.execute("SELECT floor FROM platform_compat").fetchone()[0] == 1
  eng.dispose()


@pytest.mark.parametrize("damage", ["archive", "rows", "state", "body", "projections", "cleanup"])
def test_resume_reconstructs_damaged_prepared_artifacts_before_activation(tmp_path, damage):
  raw = '[{"role":"assistant","id":"original","content":"preserved"}]'
  path, seen = legacy(tmp_path, {"one": raw})
  step = TranscriptStep()
  conn = upgrades.open_pinned(str(path))
  try:
    upgrades._begin_step(conn, step, activate=False)
    upgrades._sync_units(conn, step, str(path))
    if damage == "archive":
      conn.execute("DELETE FROM upgrade_archive")
    elif damage == "rows":
      conn.execute("DELETE FROM chat_messages")
    elif damage == "state":
      conn.execute("DELETE FROM chat_transcript_state")
    elif damage == "body":
      conn.execute("UPDATE chat_messages SET body='{}'")
    elif damage == "projections":
      conn.execute("UPDATE chat_messages SET message_id='null',role='user',flags=7")
    else:
      conn.execute("DELETE FROM chat_transcript_cleanup")
  finally:
    conn.close()
  upgrades.run_gate(str(path), seen.existing_tables)
  with sqlite3.connect(path) as conn:
    assert items(conn, "one") == json.loads(raw)
    assert upgrades.verified_legacy_copy(conn, 1, "one") == raw.encode()


@pytest.mark.parametrize("field", ["content", "id", "cid", "role"])
def test_valid_escaped_surrogate_is_preserved_not_mistaken_for_damaged_json(tmp_path, field):
  message = {"role": "assistant", field: "\ud83d"}
  raw = json.dumps([message])
  path, seen = legacy(tmp_path, {"surrogate": raw})
  upgrades.run_gate(str(path), seen.existing_tables)
  with sqlite3.connect(path) as conn:
    assert items(conn, "surrogate") == [message]
    assert upgrades.verified_legacy_copy(conn, 1, "surrogate") == raw.encode()
    assert conn.execute("SELECT COUNT(*) FROM upgrade_quarantine").fetchone()[0] == 0
