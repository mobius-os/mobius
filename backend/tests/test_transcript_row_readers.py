"""Materialized row reads retain legacy coordinates without loading full bodies."""

from app import chat_writer, transcript_rows
from app.chat_transcript import materialized_messages
from app.routes.chats import _chat_detail_window


def test_live_overlay_and_bounded_page(db):
  rows = [
    {"role": "user", "cid": "u", "content": "first", "ts": 1},
    {"role": "assistant", "id": "a", "content": "old", "blocks": [{"type": "text", "content": "old"}], "ts": 2},
    {"role": "user", "cid": "v", "content": "tail", "ts": 3},
  ]
  chat = chat_writer.create_chat(id="reader-overlay", title="Rows", messages=rows)
  db.add(chat)
  db.commit()
  chat.live_assistant = {"role": "assistant", "id": "a", "content": "new", "blocks": [{"type": "text", "content": "new"}], "ts": 2}
  materialized = materialized_messages(chat)
  assert len(materialized) == 3
  assert materialized[1]["content"] == "new"
  assert [r["content"] for r in reversed(materialized)] == ["tail", "new", "first"]
  page, start, found = _chat_detail_window(materialized, limit=1, before=None, anchor_key="a", metadata=transcript_rows.metadata(db, chat))
  assert (start, found) == (0, True)
  assert [r["content"] for r in page] == ["first", "new", "tail"]

  chat.live_assistant = {"role": "assistant", "id": "new-a", "content": "append", "blocks": [{"type": "text", "content": "append"}], "ts": 4}
  materialized = materialized_messages(chat)
  assert len(materialized) == 4
  assert materialized[-1]["content"] == "append"
  assert materialized[len(materialized):] == []
  assert materialized[len(materialized):len(materialized)] == []
  assert materialized[:0] == []
  page, start, found = _chat_detail_window(materialized, limit=2, before=3, anchor_key=None)
  assert (start, found) == (1, None)
  assert [r["content"] for r in page] == ["old", "tail"]


def test_search_v2_initial_batch_and_revision_freshness(db):
  from app import chat_search
  chat_search.create_schema_v2(db.connection())
  db.execute(chat_search.sql("UPDATE upgrade_tasks SET status='pending' WHERE level=1 AND task='index_messages'"))
  chat = chat_writer.create_chat(
    id="reader-search", title="Field notes",
    messages=[{"role": "user", "content": "wombat trail", "ts": 1}],
  )
  db.add(chat)
  db.commit()
  assert chat_search.search(db, "wombat") == []  # initial build is background-only
  assert chat_search.index_batch(db.connection()) == (1, 0)
  db.commit()
  assert chat_search.search(db, "wombat")[0]["anchor_key"] == "user-1"
  transcript_rows.append(db, chat, {"role": "assistant", "content": "platypus creek", "ts": 2})
  db.commit()
  # A stale generation cannot leak the old snippet even before reconcile runs.
  assert chat_search._rank_results(chat_search._candidate_rows(db, ["wombat"]), ["wombat"], 20) == []
  assert chat_search.search(db, "platypus")[0]["anchor_key"] == "assistant-2"


def test_raw_sqlite_search_callbacks_share_gate_connection(db):
  from app import chat_search
  raw = db.connection().connection.driver_connection
  chat_search.create_schema_v2(raw)
  chat = chat_writer.create_chat(
    id="reader-raw-index", title="Raw indexing",
    messages=[{"role": "user", "content": "capybara bank", "ts": 12}],
  )
  db.add(chat)
  db.flush()
  assert chat_search.index_batch(raw, batch_size=1) == (1, 0)
  assert db.execute(chat_search.sql("SELECT indexed_revision FROM chat_search_state_v2 "
                                    "WHERE chat_id='reader-raw-index'")).scalar_one() >= 1


def test_anchor_first_match_across_id_and_cid(db):
  from app.chat_transcript import materialized_metadata
  chat = chat_writer.create_chat(id="reader-anchors", title="Anchors", messages=[
    {"role": "user", "id": "same", "cid": "cid-one", "content": "first", "ts": 1},
    {"role": "assistant", "id": "same", "content": "second", "ts": 2},
    {"role": "user", "cid": "same", "content": "third", "ts": 3},
  ])
  db.add(chat)
  db.commit()
  view = materialized_messages(chat)
  coordinates = materialized_metadata(chat, view)
  for key, expected in (("same", 0), ("cid-one", 0), ("assistant-2", 1), ("user-2", 2)):
    page, start, found = _chat_detail_window(view, limit=1, before=None, anchor_key=key, metadata=coordinates)
    assert found is True
    assert start == max(0, expected - 1)
    assert page[-1]["content"] == "third"


def test_raw_index_byte_budget_oversized_unit_and_resume(db):
  from app import chat_search
  raw = db.connection().connection.driver_connection
  chat_search.create_schema_v2(raw)
  for cid, size in (("a-giant", 600), ("b-small", 70), ("c-small", 70)):
    db.add(chat_writer.create_chat(id=cid, title=cid, messages=[
      {"role": "user", "content": "Z" * size, "ts": 1},
    ]))
  db.commit()
  raw = db.connection().connection.driver_connection
  # Source bytes include JSON envelope, so the 200-byte budget admits one
  # oversized unit but never combines it with a second chat.
  reserve = chat_search.index_batch_space_bytes(raw)
  assert reserve >= 40 * 1024 * 1024
  assert chat_search.index_batch(raw, byte_budget=200) == (1, 2)
  indexed = {row[0] for row in raw.execute("SELECT chat_id FROM chat_search_state_v2")}
  assert "a-giant" in indexed and "b-small" not in indexed
  assert chat_search.index_batch(raw, byte_budget=200) == (1, 1)
  assert chat_search.index_batch(raw, byte_budget=200) == (1, 0)
  assert chat_search.index_batch(raw, byte_budget=200) == (0, 0)
  db.commit()


def test_raw_initial_index_uses_legacy_string_visibility_policy(db):
  import json
  from app import chat_search, models
  raw = db.connection().connection.driver_connection
  chat_search.create_schema_v2(raw)
  db.add(models.App(id=789, name="Index visibility", slug="index-visibility",
                    source_dir="/tmp/index-visibility"))
  db.add_all([
    chat_writer.create_chat(id="hidden-legacy", title="secretquokka",
                            agent_settings_json=json.dumps({"drawer_hidden": True}),
                            messages=[{"role": "user", "content": "secretquokka", "ts": 1}]),
    chat_writer.create_chat(id="visible-legacy", title="publicquokka",
                            created_by_app_id=789,
                            agent_settings_json=json.dumps({"owner_visible": True}),
                            messages=[{"role": "user", "content": "publicquokka", "ts": 1}]),
  ])
  db.commit()
  raw = db.connection().connection.driver_connection
  assert chat_search.index_batch(raw) == (2, 0)
  db.commit()
  assert chat_search.search(db, "secretquokka") == []
  assert [r["id"] for r in chat_search.search(db, "publicquokka")] == ["visible-legacy"]


def test_search_index_reserve_scales_for_one_giant_unit(monkeypatch):
  from app import chat_search
  monkeypatch.setattr(chat_search, "_raw_batch_plan",
                      lambda _conn, _count, _budget: (["giant"], 5 * 1024 * 1024))
  assert chat_search.index_batch_space_bytes(None) >= 76 * 1024 * 1024


def test_whole_materialized_iteration_uses_bounded_pages_not_per_row_queries(db):
  from sqlalchemy import event
  chat = chat_writer.create_chat(id="reader-streaming", messages=[
    {"role": "user", "content": str(i)} for i in range(130)
  ])
  db.add(chat)
  db.commit()
  statements = []
  def record(_conn, _cursor, statement, _parameters, _context, _many):
    if "chat_messages.body" in statement:
      statements.append(statement)
  event.listen(db.get_bind(), "before_cursor_execute", record)
  try:
    assert len(list(materialized_messages(chat))) == 130
  finally:
    event.remove(db.get_bind(), "before_cursor_execute", record)
  assert len(statements) == 3
  assert all("chat_messages.seq >=" in sql and "chat_messages.seq <" in sql for sql in statements)


def test_detail_read_owner_pins_live_count_coordinates_and_bodies(db, monkeypatch):
  from app.database import SessionLocal
  from app.routes.chats import _chat_detail_response
  chat = chat_writer.create_chat(id="reader-detail-race", messages=[
    {"role": "user", "cid": "initial", "content": "original", "ts": 1}
  ])
  db.add(chat)
  db.commit()
  metadata = transcript_rows.metadata
  def append_between_detail_reads(session, current):
    with SessionLocal() as other:
      transcript_rows.append(other, other.get(type(chat), chat.id),
                             {"role": "assistant", "id": "later", "content": "new", "ts": 2})
      other.commit()
    return metadata(session, current)
  monkeypatch.setattr(transcript_rows, "metadata", append_between_detail_reads)
  payload = _chat_detail_response(chat, db=db)
  assert payload["total"] == 1
  assert [m["content"] for m in payload["messages"]] == ["original"]
  db.rollback()
  assert transcript_rows.count(db, chat) == 2
