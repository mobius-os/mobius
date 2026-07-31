"""Drawer chat search: FTS index reconciliation + /api/chats/search."""

import uuid

from app import chat_search, models
from app.chat_search import sql


def _make_chat(db, title, texts, role="user"):
  # Distinct, increasing ts per message: ts is the drawer's reveal anchor and
  # is unique within a chat in production data.
  c = models.Chat(
    id=str(uuid.uuid4()),
    title=title,
    messages=[
      {"role": role, "content": t, "ts": 1000 + i} for i, t in enumerate(texts)
    ],
  )
  db.add(c)
  db.commit()
  return c


def _doc_count(db, chat_id):
  return db.execute(
    sql("SELECT count(*) FROM chat_search_docs WHERE chat_id = :c"),
    {"c": chat_id},
  ).fetchone()[0]


def test_search_finds_message_text_with_snippet(db):
  c = _make_chat(db, "Trip notes", ["let us plan the zanzibar itinerary"])
  results = chat_search.search(db, "zanzibar")
  hit = next(r for r in results if r["id"] == c.id)
  assert hit["title"] == "Trip notes"
  assert "zanzibar" in hit["snippet"]


def test_result_carries_reveal_anchor_of_matching_message(db):
  # The snippet's message, not the first one, supplies ts/role for the jump.
  c = _make_chat(
    db, "Notes", ["intro line", "the wombat migration route", "outro line"]
  )
  hit = next(r for r in chat_search.search(db, "wombat") if r["id"] == c.id)
  assert hit["role"] == "user"
  assert hit["ts"] == 1001


def test_title_only_hit_has_no_reveal_anchor(db):
  c = _make_chat(db, "Marimba tuning", ["unrelated body"])
  hit = next(r for r in chat_search.search(db, "marimba") if r["id"] == c.id)
  assert hit["ts"] is None and hit["role"] is None


def test_prefix_match_on_last_token(db):
  c = _make_chat(db, "Money", ["monthly budgeting spreadsheet"])
  assert any(r["id"] == c.id for r in chat_search.search(db, "budg"))


def test_title_match_has_no_snippet(db):
  c = _make_chat(db, "Xylophone maintenance", ["unrelated body text"])
  hit = next(
    r for r in chat_search.search(db, "xylophone") if r["id"] == c.id
  )
  assert hit["snippet"] is None


def test_new_messages_append_incrementally(db):
  c = _make_chat(db, "Log", ["first entry"])
  chat_search.search(db, "first")  # index it
  before = _doc_count(db, c.id)
  c.messages = c.messages + [
    {"role": "assistant", "content": "quokka sighting confirmed", "ts": 2}
  ]
  db.commit()
  assert any(r["id"] == c.id for r in chat_search.search(db, "quokka"))
  assert _doc_count(db, c.id) == before + 1


def test_rename_reindexes_title(db):
  c = _make_chat(db, "Old name", ["body"])
  chat_search.search(db, "body")
  c.title = "Brand new marimba title"
  db.commit()
  assert any(r["id"] == c.id for r in chat_search.search(db, "marimba"))
  assert not any(r["id"] == c.id for r in chat_search.search(db, "old name"))


def test_deleted_chat_leaves_index_and_restore_returns(db):
  from app.timeutil import now_naive_utc

  c = _make_chat(db, "Doomed", ["ephemeral pangolin facts"])
  chat_search.search(db, "pangolin")
  c.deleted_at = now_naive_utc()
  db.commit()
  assert chat_search.search(db, "pangolin") == []
  assert _doc_count(db, c.id) == 0
  c.deleted_at = None
  db.commit()
  assert any(r["id"] == c.id for r in chat_search.search(db, "pangolin"))


def test_shrunk_history_triggers_full_rebuild(db):
  c = _make_chat(db, "Trimmed", ["alpha wombat", "beta wombat"])
  chat_search.search(db, "wombat")
  c.messages = [{"role": "user", "content": "gamma capybara", "ts": 3}]
  db.commit()
  assert chat_search.search(db, "wombat") == [] or not any(
    r["id"] == c.id for r in chat_search.search(db, "wombat")
  )
  assert any(r["id"] == c.id for r in chat_search.search(db, "capybara"))


def test_tool_noise_roles_are_not_indexed(db):
  c = models.Chat(
    id=str(uuid.uuid4()),
    title="Noise",
    messages=[
      {"role": "tool", "content": "secret ocelot stacktrace"},
      {"role": "user", "content": {"not": "a string"}},
    ],
  )
  db.add(c)
  db.commit()
  assert not any(
    r["id"] == c.id for r in chat_search.search(db, "ocelot")
  )


def test_operator_input_is_neutralized(db):
  _make_chat(db, "Safe", ["plain text"])
  for hostile in ['" OR 1=1 --', "NEAR(", "a*b^c", "   ", ""]:
    chat_search.search(db, hostile)  # must not raise


def test_search_endpoint_requires_owner_and_returns_hits(client, auth, db):
  c = _make_chat(db, "Endpoint", ["searchable axolotl payload"])
  assert client.get("/api/chats/search?q=axolotl").status_code == 401
  r = client.get("/api/chats/search?q=axolotl", headers=auth)
  assert r.status_code == 200
  assert any(hit["id"] == c.id for hit in r.json())
  assert client.get("/api/chats/search?q=", headers=auth).json() == []
