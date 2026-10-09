"""Materialized row reads retain legacy coordinates without loading full bodies."""

import pytest

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


def test_search_applies_legacy_string_visibility_at_query_time(db):
  import json
  from app import chat_search, models
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
  assert chat_search.search(db, "secretquokka") == []
  assert [r["id"] for r in chat_search.search(db, "publicquokka")] == ["visible-legacy"]
  # Visibility is read when searching, so a chat that becomes visible is found
  # without reindexing.
  db.get(models.Chat, "hidden-legacy").agent_settings_json = json.dumps({"drawer_hidden": False})
  db.commit()
  assert [r["id"] for r in chat_search.search(db, "secretquokka")] == ["hidden-legacy"]


@pytest.mark.converted_chats  # A converted chat's row-path query shape.
def test_whole_materialized_iteration_streams_one_statement_not_per_row_queries(db):
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
  assert len(statements) == 1
  assert "ORDER BY chat_messages.seq" in statements[0]


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
