"""Normalized persistence protects positions, atomicity and bounded reads."""

import json

import pytest
from sqlalchemy import event, text

from app import models, transcript_rows as rows
from app.chat_writer import create_chat
from app.database import SessionLocal, engine


@pytest.fixture
def db():
  with SessionLocal() as session:
    yield session


def seed(db, cid="row-chat", messages=None):
  chat = create_chat(id=cid, messages=messages or [])
  db.add(chat)
  db.commit()
  return chat


def test_new_chat_never_reuses_another_transcript(db):
  a = seed(db, "a", [{"role": "user", "content": "private to a"}])
  b = seed(db, "b")
  assert rows.read_all(db, b) == []
  rows.append(db, b, {"role": "assistant", "content": "only b"})
  db.commit()
  assert rows.read_all(db, a) == [{"role": "user", "content": "private to a"}]
  assert rows.count(db, b) == 1


def test_append_and_update_never_rewrite_other_rows(db):
  chat = seed(db, messages=[{"role": "user", "ts": 1, "content": "x" * 200_000}] * 8)
  statements = []
  def capture(_conn, _cursor, statement, parameters, _context, _many):
    statements.append((statement, parameters))
  event.listen(engine, "before_cursor_execute", capture)
  try:
    rows.append(db, chat, {"role": "assistant", "id": "answer", "ts": 9, "content": "new"})
    db.commit()
    statements.clear()
    rows.update_at(db, chat, 8, {"role": "assistant", "id": "answer", "ts": 9, "content": "edited"})
    db.commit()
  finally:
    event.remove(engine, "before_cursor_execute", capture)
  updates = [(sql, params) for sql, params in statements if sql.startswith("UPDATE chat_messages")]
  assert len(updates) == 1
  assert "edited" in str(updates[0][1])
  # The commit's one mirror statement rewrites the legacy value in SQLite.
  assert sum(sql.startswith("UPDATE chats SET messages") for sql, _ in statements) == 1
  assert rows.at(db, chat, 0)["content"] == "x" * 200_000


def test_changed_rows_mirror_and_flags_rollback_together(db):
  chat = seed(db, messages=[{"role": "user", "content": "before"}])
  rows.update_at(db, chat, 0, {"role": "assistant", "blocks": [{"type": "question"}]})
  rows.append(db, chat, {"role": "user", "content": "not committed"})
  db.flush()
  db.rollback()
  assert rows.count(db, chat) == 1
  assert rows.at(db, chat, 0) == {"role": "user", "content": "before"}
  assert db.execute(text("SELECT messages FROM chats WHERE id = :id"), {"id": chat.id}).scalar() \
    == '[{"role": "user", "content": "before"}]'


def test_duplicate_ids_and_trailing_adoption_keep_old_matching_rules(db):
  chat = seed(db, messages=[
    {"role": "assistant", "cid": "same", "content": "not an id"},
    {"role": "assistant", "id": "same", "content": "first id"},
    {"role": "assistant", "id": "same", "content": "second id"},
    {"role": "assistant", "content": "old trailing row"},
  ])
  assert rows.assistant_index(db, chat, {"id": "same"}) == 1
  assert rows.assistant_index(db, chat, {"id": "new"}) == 3
  rows.append(db, chat, {"role": "user"})
  assert rows.assistant_index(db, chat, {"id": "new"}) == -1


def test_metadata_preserves_both_anchor_identities_and_timestamp_type(db):
  chat = seed(db, messages=[{"role": "assistant", "id": 123, "cid": "other", "ts": 4.5}])
  projected = rows.metadata(db, chat)[0]
  assert projected["id"] == 123
  assert projected["cid"] == "other"
  assert projected["ts"] == 4.5


@pytest.mark.converted_chats  # A converted chat's row-path query shape.
def test_bounded_window_does_not_hydrate_earlier_bodies(db):
  chat = seed(db, messages=[{"role": "user", "content": "x" * 50_000, "ts": i} for i in range(100)])
  statements = []
  def capture(_conn, _cursor, sql, _params, _context, _many):
    statements.append(sql)
  event.listen(engine, "before_cursor_execute", capture)
  try:
    history = rows.history(chat)
    assert len(history) == 100
    assert [m["ts"] for m in history[90:95]] == list(range(90, 95))
  finally:
    event.remove(engine, "before_cursor_execute", capture)
  selects = [sql for sql in statements if "chat_messages.body" in sql]
  assert len(selects) == 1
  assert "chat_messages.seq >=" in selects[0] and "chat_messages.seq <" in selects[0]


def test_replace_whole_history_preserves_unchanged_rows_and_dense_positions(db):
  chat = seed(db, messages=[{"role": "user", "ts": i} for i in range(5)])
  rows.replace_all(db, chat, [{"role": "user", "ts": 0}, {"role": "assistant", "ts": 1}])
  db.commit()
  assert rows.count(db, chat) == 2
  assert [r[0] for r in db.query(models.ChatMessage.seq).filter_by(chat_id=chat.id)] == [0, 1]
  assert rows.at(db, chat, 1)["role"] == "assistant"


def test_metadata_does_not_decode_normal_message_bodies(db):
  chat = seed(db, messages=[{"role": "assistant", "id": "a", "content": "x" * 200_000}])
  statements = []
  def capture(_conn, _cursor, sql, _params, _context, _many):
    statements.append(sql)
  event.listen(engine, "before_cursor_execute", capture)
  try:
    assert rows.metadata(db, chat)[0]["id"] == "a"
  finally:
    event.remove(engine, "before_cursor_execute", capture)
  assert not any("chat_messages.body" in sql for sql in statements)


def test_page_metadata_and_bodies_share_a_snapshot_during_concurrent_append(db):
  chat = seed(db, messages=[{"role": "user", "ts": 1}])
  rows.pin_read_snapshot(db)
  view = rows.history(chat)
  assert len(view) == 1
  with SessionLocal() as other:
    rows.append(other, other.get(models.Chat, chat.id), {"role": "assistant", "ts": 2})
    other.commit()
  assert len(rows.metadata(db, chat)) == 1
  assert view[:] == [{"role": "user", "ts": 1}]
  db.rollback()
  assert len(rows.history(chat)) == 2


@pytest.mark.parametrize("value", [1, 1.0, -0.0, 2**80, True, "12", None])
def test_scalar_metadata_preserves_exact_json_type_after_session_reload(db, value):
  chat = seed(db, messages=[{"role": "assistant", "id": value, "cid": value, "ts": value}])
  db.expire_all()
  projected = rows.metadata(db, chat)[0]
  for key in ("id", "cid", "ts"):
    if key == "cid" and not value:
      # The writers' identity (cid_of) treats a falsy cid as absent.
      assert "cid" not in projected
      continue
    actual = projected.get(key)
    assert actual == value
    assert type(actual) is type(value)
  if value == 0.0 and type(value) is float:
    import math
    assert math.copysign(1, projected["ts"]) == -1


@pytest.mark.parametrize("value", [1.0, -0.0, 2**80])
def test_scalar_transcript_items_preserve_type_after_reload(db, value):
  chat = seed(db, messages=[value])
  db.expire_all()
  stored = rows.at(db, chat, 0)
  assert stored == value and type(stored) is type(value)


@pytest.mark.parametrize("provider", ["claude", "codex", "mobius"])
def test_in_turn_steer_uses_identity_metadata_not_historical_bodies(db, provider):
  from app import chat_writer
  original = [{"role": "user", "cid": f"old-{i}", "ts": i + 1,
               "content": "x" * 10000} for i in range(130)]
  chat = create_chat(id="steer-rows", provider=provider, messages=original)
  db.add(chat)
  db.commit()
  statements = []
  def record(_conn, _cursor, statement, _parameters, _context, _many):
    statements.append(statement.lower())
  event.listen(engine, "before_cursor_execute", record)
  try:
    stored = chat_writer.get_writer().submit(chat_writer.AppendSteeredUserMessage(
      chat_id=chat.id, run_token="", user_msg={"role": "user", "cid": "new-cid",
                                             "ts": 1, "content": "new steer"},
    )).result(timeout=5)
  finally:
    event.remove(engine, "before_cursor_execute", record)
  assert stored["stored"]["ts"] == 131
  assert not any("chat_messages.body" in sql for sql in statements if sql.lstrip().startswith("select"))
  assert len([sql for sql in statements if sql.lstrip().startswith("insert into chat_messages")]) == 1
  db.expire_all()
  assert rows.history(db.get(models.Chat, chat.id))[0:130] == original


def test_generic_reads_do_not_trust_cached_rows_after_external_writer_ack(db):
  chat = seed(db, messages=[{"role": "user", "content": "before"}])
  assert rows.count(db, chat) == 1
  with SessionLocal() as writer:
    rows.update_at(writer, writer.get(models.Chat, chat.id), 0,
                   {"role": "user", "content": "after"})
    rows.append(writer, writer.get(models.Chat, chat.id), {"role": "assistant", "content": "added"})
    writer.commit()
  # No implicit snapshot is held and the external ack owns fresh authority;
  # rows are read with statements, never from an identity map.
  assert rows.count(db, chat) == 2
  assert rows.at(db, chat, 0)["content"] == "after"


@pytest.mark.parametrize("before,after", [(True, 1), (1, 1.0), (-0.0, 0.0)])
def test_single_row_update_preserves_json_type_change_and_projection(db, before, after):
  import json
  chat = seed(db, messages=[{"role": "assistant", "id": before, "ts": before}])
  rows.update_at(db, chat, 0, {"role": "assistant", "id": after, "ts": after})
  db.commit()
  db.expire_all()
  actual = rows.at(db, chat, 0)
  projected = rows.metadata(db, chat)[0]
  for result in (actual, projected):
    assert json.dumps(result["id"]) == json.dumps(after)
    assert json.dumps(result["ts"]) == json.dumps(after)


def test_surrogate_identity_lookup_preserves_first_matching_message(db):
  key = "\ud83d"
  chat = seed(db, messages=[{"role": "assistant", "id": key, "content": "original"}])
  assert rows.assistant_index(db, chat, {"id": key}) == 0
  assert rows.metadata(db, chat)[0]["id"] == key


@pytest.mark.parametrize("key", ["a" * 256, "long" * 1000, "\ud800" * 256])
def test_arbitrary_legacy_ids_keep_exact_lookup_keys_and_identity(db, key):
  chat = create_chat(id="bounded-id", title="test", messages=[
    {"role": "assistant", "id": key, "content": "original"}], agent_settings_json={"model": "test"})
  db.add(chat)
  db.commit()
  assert db.query(models.ChatMessage.message_key).filter_by(chat_id=chat.id).scalar() == json.dumps(key)
  assert rows.assistant_index(db, chat, {"id": key}) == 0
  assert rows.at(db, chat, 0)["id"] == key
