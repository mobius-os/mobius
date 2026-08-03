"""Drawer chat search: FTS index reconciliation + /api/chats/search."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import threading
import uuid

from app import chat_search, models
from app.chat_search import sql
from app.timeutil import now_naive_utc


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
  assert hit["anchor_key"] == "user-1001"


def test_result_falls_back_to_role_index_anchor_without_timestamp(db):
  c = models.Chat(
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
  assert hit["ts"] is None and hit["role"] is None
  assert hit["anchor_key"] is None


def test_prefix_match_on_last_token(db):
  c = _make_chat(db, "Money", ["monthly budgeting spreadsheet"])
  assert any(r["id"] == c.id for r in chat_search.search(db, "budg"))


def test_portable_fallback_preserves_visibility_prefix_and_reveal_contract(
  db, monkeypatch,
):
  suffix = uuid.uuid4().hex
  exact = f"portablepostgres{suffix}"
  visible = _make_chat(
    db,
    "Portable result",
    [f"{exact} capybara appears in visible prose"],
  )
  hidden_row = models.Chat(
    id=str(uuid.uuid4()),
    title="Hidden transcript row",
    messages=[{
      "role": "user",
      "content": f"{exact} capybara private answer",
      "ts": 1000,
      "hidden": True,
    }],
  )
  hidden_chat = models.Chat(
    id=str(uuid.uuid4()),
    title="Hidden drawer chat",
    messages=[{
      "role": "user",
      "content": f"{exact} capybara hidden chat",
      "ts": 1000,
    }],
    agent_settings_json={"drawer_hidden": True},
  )
  tool_only = models.Chat(
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

  def unexpected_reconcile(_db):
    raise AssertionError("portable search must not touch the SQLite FTS schema")

  monkeypatch.setattr(chat_search, "reconcile", unexpected_reconcile)
  results = chat_search.search(db, f"{exact} capy")
  ids = {result["id"] for result in results}
  assert visible.id in ids
  assert {hidden_row.id, hidden_chat.id, tool_only.id}.isdisjoint(ids)
  hit = next(result for result in results if result["id"] == visible.id)
  assert hit["anchor_key"] == "user-1000"
  assert "capybara" in hit["snippet"]


def test_portable_fallback_finds_json_escaped_unicode_transcript(db, monkeypatch):
  c = _make_chat(db, "Unicode", ["réunion café itinerary"])
  monkeypatch.setattr(chat_search, "_database_dialect", lambda _db: "postgresql")

  hit = next(
    result for result in chat_search.search(db, "réunion caf")
    if result["id"] == c.id
  )

  assert hit["anchor_key"] == "user-1000"
  assert "café" in hit["snippet"]


def test_portable_fallback_bounds_candidates_by_recent_activity(db, monkeypatch):
  needle = f"boundedportable{uuid.uuid4().hex}"
  now = now_naive_utc()
  older = _make_chat(db, "Older dense match", [f"{needle} {needle} {needle}"])
  newer = _make_chat(db, "Newer match", [needle])
  older.activity_at = now - timedelta(days=1)
  newer.activity_at = now
  db.commit()

  monkeypatch.setattr(chat_search, "_database_dialect", lambda _db: "postgresql")
  monkeypatch.setattr(chat_search, "_PORTABLE_CANDIDATE_LIMIT", 1)

  results = chat_search.search(db, needle, limit=20)

  assert [result["id"] for result in results] == [newer.id]


def test_title_match_has_no_snippet(db):
  c = _make_chat(db, "Xylophone maintenance", ["unrelated body text"])
  hit = next(
    r for r in chat_search.search(db, "xylophone") if r["id"] == c.id
  )
  assert hit["snippet"] is None


def test_appended_message_sync_keeps_one_doc_per_transcript_row(db):
  c = _make_chat(db, "Log", ["first entry"])
  chat_search.search(db, "first")  # index it
  before = _doc_count(db, c.id)
  c.messages = c.messages + [
    {"role": "assistant", "content": "quokka sighting confirmed", "ts": 2}
  ]
  db.commit()
  assert any(r["id"] == c.id for r in chat_search.search(db, "quokka"))
  assert _doc_count(db, c.id) == before + 1


def test_append_preserves_stable_fts_document_rows(db):
  c = _make_chat(db, "Stable history", ["first stable", "second stable"])
  chat_search.search(db, "stable")
  before = dict(db.execute(
    sql("SELECT msg_idx, id FROM chat_search_docs WHERE chat_id = :cid"),
    {"cid": c.id},
  ).fetchall())

  c.messages = c.messages + [
    {"role": "assistant", "content": "third stable", "ts": 1002}
  ]
  db.commit()
  chat_search.search(db, "stable")
  after = dict(db.execute(
    sql("SELECT msg_idx, id FROM chat_search_docs WHERE chat_id = :cid"),
    {"cid": c.id},
  ).fetchall())

  assert before.items() <= after.items()
  assert 2 in after


def test_same_length_transcript_replacement_updates_existing_search_rows(db):
  c = _make_chat(db, "Mutable", ["oldplatypus phrase"])
  assert any(r["id"] == c.id for r in chat_search.search(db, "oldplatypus"))

  c.messages = [
    {"role": "user", "content": "newporcupine phrase", "ts": 1000},
  ]
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


def test_hidden_transcript_rows_never_surface_and_can_become_visible(db):
  c = models.Chat(
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

  messages = list(c.messages)
  messages[1] = {**messages[1], "hidden": False}
  c.messages = messages
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
    row = models.Chat(
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
    chat = models.Chat(
      id=str(uuid.uuid4()),
      title="Visibility contract",
      messages=[],
      created_by_app_id=created_by_app_id,
      agent_settings_json=settings,
    )
    assert chat_search._visible_in_owner_drawer(chat) is drawer_visible(chat)


def test_index_version_rebuild_purges_rows_from_older_indexing_semantics(db):
  c = models.Chat(
    id=str(uuid.uuid4()),
    title="Versioned search",
    messages=[{
      "role": "user",
      "content": "legacyhiddenibis",
      "ts": 1000,
      "hidden": True,
    }],
  )
  db.add(c)
  db.commit()
  chat_search.search(db, "unrelated")

  # Model an older generation that indexed this now-hidden row. Changing the
  # semantic version must replace the complete disposable index before query.
  db.execute(sql(
    "INSERT INTO chat_search_docs (chat_id, msg_idx, ts, role, text)"
    " VALUES (:cid, 0, 1000, 'user', 'legacyhiddenibis')"
  ), {"cid": c.id})
  db.execute(sql(
    "UPDATE chat_search_meta SET value = 'legacy' WHERE key = 'index_version'"
  ))
  db.commit()
  chat_search._schema_ready = False

  assert not any(
    result["id"] == c.id
    for result in chat_search.search(db, "legacyhiddenibis")
  )
  version = db.execute(sql(
    "SELECT value FROM chat_search_meta WHERE key = 'index_version'"
  )).scalar_one()
  assert version == chat_search._INDEX_VERSION
  assert _doc_count(db, c.id) == 1  # title only; hidden message stayed absent


def test_long_matching_chat_cannot_crowd_other_chats_out_before_grouping(db):
  suffix = uuid.uuid4().hex
  needle = f"fairresult{suffix}"
  long_chat = _make_chat(db, "Long match", [needle] * 240)
  short_a = _make_chat(db, "Short A", [needle])
  short_b = _make_chat(db, "Short B", [needle])

  ids = {result["id"] for result in chat_search.search(db, needle, limit=3)}
  assert ids == {long_chat.id, short_a.id, short_b.id}


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
  assert detail["offset"] == 1
  assert [row["content"] for row in detail["messages"]] == [
    "the searchable narwhal", "after",
  ]
