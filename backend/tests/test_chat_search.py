"""Drawer chat search: FTS index reconciliation + /api/chats/search."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
import uuid

from app import chat_search, chat_writer, models, transcript_rows
from app.chat_search import sql
from app.chat_visibility import visible_in_owner_drawer
from app.timeutil import now_naive_utc


import pytest


@pytest.fixture(autouse=True)
def _background_search_build(db, monkeypatch):
  chat_search.create_schema_v2(db.connection())
  real_search = chat_search.search
  def built_search(session, query, limit=20):
    while True:
      done, _remaining = chat_search.index_batch(session.connection())
      session.commit()
      if _remaining == 0:
        break
    return real_search(session, query, limit=limit)
  built_search.__wrapped__ = real_search
  monkeypatch.setattr(chat_search, "search", built_search)


def _make_chat(db, title, texts, role="user"):
  # Distinct, increasing ts per message: ts is the drawer's reveal anchor and
  # is unique within a chat in production data.
  c = chat_writer.create_chat(
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
    sql("SELECT count(*) FROM chat_search_docs_v2 WHERE chat_id = :c"),
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
  assert hit["anchor_key"] == "user-1001"
  assert set(hit) == {
    "id", "title", "snippet", "anchor_key", "last_active", "archived",
  }


def test_result_carries_iso_last_active_timestamp(db):
  # The shell shows recency on each hit; the value must be an ISO-8601 string
  # (T-separated) so the browser's Date.parse — including Safari's — accepts it.
  c = _make_chat(db, "Recency notes", ["a quetzal sighting near the ridge"])
  hit = next(r for r in chat_search.search(db, "quetzal") if r["id"] == c.id)
  assert hit["last_active"]
  assert "T" in hit["last_active"] and " " not in hit["last_active"]


def test_result_falls_back_to_role_index_anchor_without_timestamp(db):
  c = chat_writer.create_chat(
    id=str(uuid.uuid4()),
    title="Untimed notes",
    messages=[
      {"role": "user", "content": "intro"},
      {"role": "assistant", "content": "needle in an untimed row"},
    ],
  )
  db.add(c)
  db.commit()
  hit = next(r for r in chat_search.search(db, "needle") if r["id"] == c.id)
  assert hit["anchor_key"] == "assistant-1"


def test_title_only_hit_has_no_reveal_anchor(db):
  c = _make_chat(db, "Marimba tuning", ["unrelated body"])
  hit = next(r for r in chat_search.search(db, "marimba") if r["id"] == c.id)
  assert hit["anchor_key"] is None


def test_prefix_match_on_last_token(db):
  c = _make_chat(db, "Money", ["monthly budgeting spreadsheet"])
  assert any(r["id"] == c.id for r in chat_search.search(db, "budg"))


def test_normalized_documents_preserve_visibility_prefix_and_reveal_contract(
  db, monkeypatch,
):
  suffix = uuid.uuid4().hex
  exact = f"portablepostgres{suffix}"
  visible = _make_chat(
    db,
    "Portable result",
    [f"{exact} capybara appears in visible prose"],
  )
  hidden_row = chat_writer.create_chat(
    id=str(uuid.uuid4()),
    title="Hidden transcript row",
    messages=[{
      "role": "user",
      "content": f"{exact} capybara private answer",
      "ts": 1000,
      "hidden": True,
    }],
  )
  hidden_chat = chat_writer.create_chat(
    id=str(uuid.uuid4()),
    title="Hidden drawer chat",
    messages=[{
      "role": "user",
      "content": f"{exact} capybara hidden chat",
      "ts": 1000,
    }],
    agent_settings_json={"drawer_hidden": True},
  )
  tool_only = chat_writer.create_chat(
    id=str(uuid.uuid4()),
    title="Tool output",
    messages=[{
      "role": "tool",
      "content": f"{exact} capybara tool payload",
      "ts": 1000,
    }],
  )
  db.add_all((hidden_row, hidden_chat, tool_only))
  db.commit()

  monkeypatch.setattr(chat_search, "_database_dialect", lambda _db: "postgresql")
  results = chat_search.search(db, f"{exact} capy")
  ids = {result["id"] for result in results}
  assert visible.id in ids
  assert {hidden_row.id, hidden_chat.id, tool_only.id}.isdisjoint(ids)
  hit = next(result for result in results if result["id"] == visible.id)
  assert hit["anchor_key"] == "user-1000"
  assert "capybara" in hit["snippet"]


def test_normalized_postgres_path_finds_unicode_document_text(db, monkeypatch):
  c = _make_chat(db, "Unicode", ["réunion café itinerary"])
  monkeypatch.setattr(chat_search, "_database_dialect", lambda _db: "postgresql")

  hit = next(
    result for result in chat_search.search(db, "réunion caf")
    if result["id"] == c.id
  )

  assert hit["anchor_key"] == "user-1000"
  assert "café" in hit["snippet"]


def test_postgres_path_keeps_recent_matches_beyond_the_old_512_chat_cap(
  db, monkeypatch,
):
  needle = f"completeportable{uuid.uuid4().hex}"
  now = now_naive_utc()
  verbose_old = chat_writer.create_chat(
    id=str(uuid.uuid4()),
    title="Older verbose match",
    messages=[{
      "role": "user",
      "content": f"{needle} {needle} {needle}",
      "ts": 1000,
    }],
    activity_at=now - timedelta(days=1),
  )
  recent = [
    chat_writer.create_chat(
      id=str(uuid.uuid4()),
      title=f"Recent match {index}",
      messages=[{"role": "user", "content": needle, "ts": 1000}],
      activity_at=now,
    )
    for index in range(512)
  ]
  db.add_all([verbose_old, *recent])
  db.commit()

  monkeypatch.setattr(chat_search, "_database_dialect", lambda _db: "postgresql")

  assert [result["id"] for result in chat_search.search(db, needle, limit=1)] == [
    max(chat.id for chat in recent),
  ]


def test_title_match_has_no_snippet(db):
  c = _make_chat(db, "Xylophone maintenance", ["unrelated body text"])
  hit = next(
    r for r in chat_search.search(db, "xylophone") if r["id"] == c.id
  )
  assert hit["snippet"] is None


def test_appended_message_rebuild_keeps_one_doc_per_transcript_row(db):
  c = _make_chat(db, "Log", ["first entry"])
  chat_search.search(db, "first")  # index it
  before = _doc_count(db, c.id)
  transcript_rows.append_many(db, c, [
    {"role": "assistant", "content": "quokka sighting confirmed", "ts": 2}
  ])
  db.commit()
  assert any(r["id"] == c.id for r in chat_search.search(db, "quokka"))
  assert _doc_count(db, c.id) == before + 1


def test_same_length_transcript_replacement_updates_existing_search_rows(db):
  c = _make_chat(db, "Mutable", ["oldplatypus phrase"])
  assert any(r["id"] == c.id for r in chat_search.search(db, "oldplatypus"))

  transcript_rows.replace_all(db, c, [
    {"role": "user", "content": "newporcupine phrase", "ts": 1000},
  ])
  db.commit()

  assert any(r["id"] == c.id for r in chat_search.search(db, "newporcupine"))
  assert not any(r["id"] == c.id for r in chat_search.search(db, "oldplatypus"))


def test_rename_reindexes_title(db):
  c = _make_chat(db, "Old name", ["body"])
  chat_search.search(db, "body")
  c.title = "Brand new marimba title"
  db.commit()
  assert any(r["id"] == c.id for r in chat_search.search(db, "marimba"))
  assert not any(r["id"] == c.id for r in chat_search.search(db, "old name"))


def test_deleted_chat_leaves_index_and_restore_returns(db):
  c = _make_chat(db, "Doomed", ["ephemeral pangolin facts"])
  chat_search.search(db, "pangolin")
  c.deleted_at = now_naive_utc()
  db.commit()
  assert chat_search.search(db, "pangolin") == []
  # Generation rows may remain disposable while deleted; queries gate visibility.
  c.deleted_at = None
  db.commit()
  assert any(r["id"] == c.id for r in chat_search.search(db, "pangolin"))


def test_shrunk_history_triggers_full_rebuild(db):
  c = _make_chat(db, "Trimmed", ["alpha wombat", "beta wombat"])
  chat_search.search(db, "wombat")
  transcript_rows.replace_all(db, c, [{"role": "user", "content": "gamma capybara", "ts": 3}])
  db.commit()
  assert not any(
    r["id"] == c.id for r in chat_search.search(db, "wombat")
  )
  assert any(r["id"] == c.id for r in chat_search.search(db, "capybara"))


def test_tool_noise_roles_are_not_indexed(db):
  c = chat_writer.create_chat(
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


def test_hidden_transcript_rows_never_surface_and_can_become_visible(db):
  c = chat_writer.create_chat(
    id=str(uuid.uuid4()),
    title="Private transcript mechanics",
    messages=[
      {"role": "user", "content": "ordinary visible prose", "ts": 1000},
      {
        "role": "user",
        "content": "concealedcassowary answer",
        "ts": 1001,
        "hidden": True,
      },
    ],
  )
  db.add(c)
  db.commit()

  assert not any(
    r["id"] == c.id for r in chat_search.search(db, "concealedcassowary")
  )
  assert _doc_count(db, c.id) == 2  # title + visible message

  messages = list(transcript_rows.history(c))
  messages[1] = {**messages[1], "hidden": False}
  transcript_rows.replace_all(db, c, messages)
  db.commit()
  hit = next(
    r for r in chat_search.search(db, "concealedcassowary") if r["id"] == c.id
  )
  assert hit["anchor_key"] == "user-1001"


def test_search_visibility_matches_owner_drawer_contract(db):
  suffix = uuid.uuid4().hex
  needle = f"drawercontract{suffix}"
  app = models.App(
    source_dir=f"/tmp/search-visibility-{suffix}",
    name="Search visibility fixture",
    description="",
    jsx_source="",
    slug=f"search-visibility-{suffix}",
  )
  db.add(app)
  db.flush()

  def chat(label, *, app_owned=False, settings=None):
    row = chat_writer.create_chat(
      id=str(uuid.uuid4()),
      title=label,
      messages=[{"role": "user", "content": needle, "ts": 1000}],
      created_by_app_id=app.id if app_owned else None,
      agent_settings_json=settings,
    )
    db.add(row)
    return row

  owner_default = chat("Owner default")
  owner_hidden = chat("Owner hidden", settings={"drawer_hidden": True})
  app_default = chat("App default", app_owned=True)
  app_visible = chat(
    "App owner visible", app_owned=True,
    settings='{"owner_visible": true}',
  )
  app_forced_visible = chat(
    "App forced visible", app_owned=True,
    settings={"drawer_hidden": False},
  )
  app_forced_hidden = chat(
    "App forced hidden", app_owned=True,
    settings={"owner_visible": True, "drawer_hidden": True},
  )
  db.commit()

  ids = {result["id"] for result in chat_search.search(db, needle)}
  assert {owner_default.id, app_visible.id, app_forced_visible.id} <= ids
  assert {owner_hidden.id, app_default.id, app_forced_hidden.id}.isdisjoint(ids)
  assert _doc_count(db, owner_hidden.id) == 0
  assert _doc_count(db, app_default.id) == 0


def test_search_and_drawer_visibility_helpers_share_one_behavior_contract():
  from app.routes.chats import _visible_in_owner_drawer as drawer_visible

  cases = (
    (None, None),
    (None, {"drawer_hidden": True}),
    (42, None),
    (42, {"owner_visible": True}),
    (42, '{"owner_visible": true}'),
    (42, {"owner_visible": True, "drawer_hidden": True}),
    (42, {"drawer_hidden": False}),
  )
  for created_by_app_id, settings in cases:
    chat = chat_writer.create_chat(
      id=str(uuid.uuid4()),
      title="Visibility contract",
      messages=[],
      created_by_app_id=created_by_app_id,
      agent_settings_json=settings,
    )
    assert visible_in_owner_drawer(chat) is drawer_visible(chat)


def test_long_matching_chat_cannot_crowd_other_chats_out_before_grouping(db):
  suffix = uuid.uuid4().hex
  needle = f"fairresult{suffix}"
  long_chat = _make_chat(db, "Long match", [needle] * 240)
  short_a = _make_chat(db, "Short A", [needle])
  short_b = _make_chat(db, "Short B", [needle])

  ids = {result["id"] for result in chat_search.search(db, needle, limit=3)}
  assert ids == {long_chat.id, short_a.id, short_b.id}


def test_streaming_ranker_keeps_complete_recent_top_k_when_newest_arrives_last():
  rows = iter((
    ("chat-a", 0, 1, "user", "one lynx", "A", "2026-08-01", False),
    ("chat-b", 0, 2, "user", "lynx lynx lynx", "B", "2026-08-02", False),
  ))

  results = chat_search._rank_results(rows, ["lynx"], limit=1)

  assert [result["id"] for result in results] == ["chat-b"]


def test_recent_match_outranks_old_chat_that_repeats_the_query():
  rows = iter((
    (
      "old-verbose", 0, 1, "user",
      "new chat drawer new chat drawer new chat drawer",
      "Old verbose discussion", "2026-08-01", False,
    ),
    (
      "recent-report", -1, None, None, "Missing new chat in web drawer",
      "Missing new chat in web drawer", "2026-08-28", False,
    ),
  ))

  results = chat_search._rank_results(rows, ["new", "chat", "drawer"], limit=2)

  assert [result["id"] for result in results] == [
    "recent-report", "old-verbose",
  ]


def test_overlapping_first_searches_leave_one_idempotent_document_generation(db):
  suffix = uuid.uuid4().hex
  needle = f"concurrentsearch{suffix}"
  c = _make_chat(db, "Concurrent index", [needle])
  start = threading.Barrier(2)

  def run_search():
    from app.database import SessionLocal

    session = SessionLocal()
    try:
      start.wait(timeout=2)
      return chat_search.search(session, needle)
    finally:
      session.close()

  with ThreadPoolExecutor(max_workers=2) as pool:
    outcomes = list(pool.map(lambda _: run_search(), range(2)))

  assert all(any(result["id"] == c.id for result in rows) for rows in outcomes)
  assert _doc_count(db, c.id) == 2  # one title row + one message row


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


def test_search_anchor_opens_one_authoritative_window_through_the_tail(client, auth, db):
  c = _make_chat(db, "Window", ["before", "the searchable narwhal", "after"])
  hit = next(
    result for result in client.get(
      "/api/chats/search?q=narwhal", headers=auth
    ).json() if result["id"] == c.id
  )
  detail = client.get(
    f"/api/chats/{c.id}?anchor={hit['anchor_key']}&compact=1",
    headers=auth,
  ).json()
  assert detail["requested_anchor_found"] is True
  assert detail["offset"] == 0
  assert [row["content"] for row in detail["messages"]] == [
    "before", "the searchable narwhal", "after",
  ]


def test_new_chats_reconcile_only_after_initial_generation_done(db):
  from app import transcript_rows
  # Call the production search, bypassing this file's simulated background
  # build wrapper. A new chat is not indexed by a request during initial build.
  real_search = chat_search.search.__wrapped__
  db.execute(sql("UPDATE upgrade_tasks SET status='pending' WHERE level=1 AND task='index_messages'"))
  first = _make_chat(db, "Initial tiger", ["initialtiger prose"])
  assert real_search(db, "initialtiger") == []
  done, remaining = chat_search.index_batch(db.connection())
  assert done >= 1 and remaining == 0
  db.execute(sql("UPDATE upgrade_tasks SET status='done', done_units=:done, remaining_units=0 "
                 "WHERE level=1 AND task='index_messages'"), {"done": done})
  db.commit()
  assert [r["id"] for r in real_search(db, "initialtiger")] == [first.id]

  visible = _make_chat(db, "New lemur", ["freshlemur body"])
  archived = _make_chat(db, "Archived mink", ["archivedmink body"])
  archived.archived_at = now_naive_utc()
  db.commit()
  assert [r["id"] for r in real_search(db, "freshlemur")] == [visible.id]
  archived_hit = real_search(db, "archivedmink")[0]
  assert archived_hit["id"] == archived.id and archived_hit["archived"] is True

  transcript_rows.replace_all(db, visible, [
    {"role": "user", "content": "revisedlemur body", "ts": 1000},
  ])
  db.commit()
  assert real_search(db, "freshlemur") == []
  assert [r["id"] for r in real_search(db, "revisedlemur")] == [visible.id]


def test_completed_generation_new_chat_reconcile_is_bounded(db):
  real_search = chat_search.search.__wrapped__
  assert db.execute(sql("SELECT status FROM upgrade_tasks WHERE level=1 AND task='index_messages'")).scalar_one() == "done"
  for i in range(25):
    db.add(chat_writer.create_chat(
      id=f"new-search-{i:02d}", title=f"Bounded {i}",
      messages=[{"role": "user", "content": f"boundedotter{i:02d}", "ts": i}],
    ))
  db.commit()
  assert real_search(db, "boundedotter24") == []
  indexed = db.execute(sql(
    "SELECT COUNT(*) FROM chat_search_state_v2 WHERE chat_id LIKE 'new-search-%'"
  )).scalar_one()
  assert indexed == chat_search.INDEX_BATCH_MAX_CHATS
  assert [r["id"] for r in real_search(db, "boundedotter24")] == ["new-search-24"]
