"""Owner chat archiving: filing a chat away keeps it whole and restorable."""
from sqlalchemy.orm import object_session
from app import transcript_rows
from app.chat_writer import create_chat

from datetime import timedelta

from app import auth as auth_mod
from app import models
from app.timeutil import now_naive_utc


def _noop_runner(monkeypatch):
  async def _noop_run_chat(*args, **kwargs):
    return None

  monkeypatch.setattr("app.routes.chats_stream.run_chat", _noop_run_chat)


def _listed(client, auth, chat_id):
  rows = client.get("/api/chats", headers=auth).json()
  return next((row for row in rows if row["id"] == chat_id), None)


def test_archiving_keeps_history_and_lists_the_chat_as_archived(
  client, auth, chat, db,
):
  transcript_rows.replace_all(object_session(chat), chat, [{"role": "user", "content": "keep me", "ts": 1}])
  chat.has_messages = True
  db.commit()

  response = client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  assert response.status_code == 200, response.text
  assert response.json()["archived_at"] is not None
  db.refresh(chat)
  assert chat.deleted_at is None
  assert list(transcript_rows.history(chat)) == [{"role": "user", "content": "keep me", "ts": 1}]
  row = _listed(client, auth, chat.id)
  assert row is not None and row["archived_at"] is not None
  detail = client.get(f"/api/chats/{chat.id}", headers=auth)
  assert detail.status_code == 200


def test_archiving_unpins_and_restoring_does_not_repin(client, auth, chat, db):
  chat.pinned_at = now_naive_utc()
  db.commit()

  archived = client.post(f"/api/chats/{chat.id}/archive", headers=auth).json()
  assert archived["pinned_at"] is None

  restored = client.post(f"/api/chats/{chat.id}/unarchive", headers=auth).json()
  assert restored == {"archived_at": None, "pinned_at": None}


def test_restore_keeps_the_chat_at_its_previous_recents_position(
  client, auth, chat, db,
):
  earlier = now_naive_utc() - timedelta(days=3)
  chat.activity_at = earlier
  db.commit()

  client.post(f"/api/chats/{chat.id}/archive", headers=auth)
  client.post(f"/api/chats/{chat.id}/unarchive", headers=auth)

  db.refresh(chat)
  assert chat.archived_at is None
  assert chat.activity_at == earlier


def test_archiving_twice_keeps_the_original_archive_time(client, auth, chat, db):
  first = client.post(f"/api/chats/{chat.id}/archive", headers=auth).json()
  second = client.post(f"/api/chats/{chat.id}/archive", headers=auth).json()
  assert second["archived_at"] == first["archived_at"]


def test_archiving_a_deleted_chat_is_not_found(client, auth, chat, db):
  chat.deleted_at = now_naive_utc()
  db.commit()
  response = client.post(f"/api/chats/{chat.id}/archive", headers=auth)
  assert response.status_code == 404


def test_owner_message_restores_an_archived_chat(
  client, auth, chat, db, monkeypatch,
):
  _noop_runner(monkeypatch)
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "picking this back up"},
    headers=auth,
  )

  assert response.status_code == 202, response.text
  db.refresh(chat)
  assert chat.archived_at is None


def test_owner_send_and_restore_share_the_writer_commit(
  client, auth, chat, db, monkeypatch,
):
  from app import chat_writer

  _noop_runner(monkeypatch)
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)
  real_commit = chat_writer._commit_or_rollback
  observed = []

  def inspect_commit(session):
    row = session.get(models.Chat, chat.id)
    if any(m.get("content") == "atomic pickup" for m in list(transcript_rows.history(row)) or []):
      observed.append(row.archived_at)
    return real_commit(session)

  monkeypatch.setattr(chat_writer, "_commit_or_rollback", inspect_commit)
  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "atomic pickup", "cid": "archive-atomic"},
    headers=auth,
  )

  assert response.status_code == 202, response.text
  assert observed == [None], "the writer must commit message and restore together"
  db.refresh(chat)
  assert chat.archived_at is None


def test_dropped_owner_send_commit_keeps_message_and_archive_together(
  client, auth, chat, db, monkeypatch,
):
  from app import chat_writer

  _noop_runner(monkeypatch)
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)
  real_commit = chat_writer._commit_or_rollback

  def drop_input_commit(session):
    row = session.get(models.Chat, chat.id)
    if any(m.get("content") == "dropped pickup" for m in list(transcript_rows.history(row)) or []):
      session.rollback()
      return False
    return real_commit(session)

  monkeypatch.setattr(chat_writer, "_commit_or_rollback", drop_input_commit)
  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "dropped pickup", "cid": "archive-drop"},
    headers=auth,
  )

  assert response.status_code == 503, response.text
  db.refresh(chat)
  assert chat.archived_at is not None
  assert not any(m.get("content") == "dropped pickup" for m in list(transcript_rows.history(chat)) or [])


def test_rejected_owner_send_keeps_an_archived_chat_archived(client, auth, chat, db):
  chat.agent_settings_json = None
  db.commit()
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "keep this as a draft", "cid": "missing-model-archive"},
    headers=auth,
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "model_selection_required"
  db.refresh(chat)
  assert chat.archived_at is not None
  assert list(transcript_rows.history(chat)) == []
  assert chat.pending_messages == []


def test_rejected_restart_choice_keeps_an_archived_chat_archived(
  client, auth, chat, db, monkeypatch,
):
  monkeypatch.setattr(
    "app.platform_restart.restart_action_block",
    lambda _chat, _qid: {"type": "question", "question_id": "restart-card"},
  )
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "", "hidden": True, "question_id": "restart-card",
          "selected_options": {"restart": []}},
    headers=auth,
  )

  assert response.status_code == 409, response.text
  db.refresh(chat)
  assert chat.archived_at is not None


def test_duplicate_owner_send_does_not_restore_a_later_archive(
  client, auth, chat, db, monkeypatch,
):
  _noop_runner(monkeypatch)
  body = {"content": "one accepted message", "cid": "archive-retry"}
  first = client.post(f"/api/chats/{chat.id}/messages", json=body, headers=auth)
  assert first.status_code == 202, first.text
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  retry = client.post(f"/api/chats/{chat.id}/messages", json=body, headers=auth)

  assert retry.status_code in (200, 202), retry.text
  db.refresh(chat)
  assert chat.archived_at is not None


def test_duplicate_queued_send_does_not_restore_a_later_archive(
  client, auth, chat, db, monkeypatch,
):
  _noop_runner(monkeypatch)
  monkeypatch.setattr("app.routes.chats_stream.is_chat_running", lambda _id: True)
  body = {"content": "queued once", "cid": "archive-queued-retry"}
  first = client.post(f"/api/chats/{chat.id}/messages", json=body, headers=auth)
  assert first.status_code == 202, first.text
  assert first.json()["status"] == "queued"
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  retry = client.post(f"/api/chats/{chat.id}/messages", json=body, headers=auth)

  assert retry.status_code == 202, retry.text
  assert retry.json()["status"] == "queued"
  db.refresh(chat)
  assert chat.archived_at is not None
  assert len(chat.pending_messages) == 1


def test_agent_message_leaves_an_archived_chat_archived(
  client, auth, chat, db, monkeypatch,
):
  _noop_runner(monkeypatch)
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)
  owner = db.query(models.Owner).first()
  other_chat = create_chat(id="agent-origin-chat", title="Other")
  db.add(other_chat)
  db.commit()
  agent_token = auth_mod.create_agent_token(
    chat_id=other_chat.id, owner_username=owner.username,
    token_epoch=owner.token_epoch,
  )

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "note from another agent"},
    headers={"Authorization": f"Bearer {agent_token}"},
  )

  assert response.status_code == 202, response.text
  db.refresh(chat)
  assert chat.archived_at is not None


def test_archived_chats_are_searchable_and_labelled(client, auth, chat, db):
  chat.title = "Quarterly zebra plan"
  chat.has_messages = True
  transcript_rows.replace_all(object_session(chat), chat, [{"role": "user", "content": "zebra budget", "ts": 1}])
  db.commit()
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  hits = client.get("/api/chats/search", params={"q": "zebra"}, headers=auth)

  assert hits.status_code == 200, hits.text
  hit = next(item for item in hits.json() if item["id"] == chat.id)
  assert hit["archived"] is True


def test_archived_chats_are_left_out_of_recent_chat_continuity(
  client, auth, chat, db, tmp_path, monkeypatch,
):
  from app import memory
  from app.config import get_settings

  data_dir = get_settings().data_dir
  archived = create_chat(id="archived-continuity", title="Filed")
  db.add(archived)
  db.commit()
  root = memory.memory_dir(data_dir)
  for chat_id, name in ((chat.id, "Active work"), (archived.id, "Filed work")):
    note = root / "chats" / chat_id / "index.md"
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text(
      f"---\ndescription: {name}\n---\n\n## Digest\n{name} digest.\n",
      encoding="utf-8",
    )
  client.post(f"/api/chats/{archived.id}/archive", headers=auth)

  context = client.get(f"/api/chats/{chat.id}/agent-context", headers=auth)

  assert context.status_code == 200, context.text
  text = str(context.json())
  assert "Active work" in text
  assert "Filed work" not in text


# ── Lifecycle edge cases ───────────────────────────────────────────────────


def test_archiving_leaves_armed_waits_and_the_run_alone(
  client, auth, chat, db, monkeypatch,
):
  now = now_naive_utc()
  db.add(models.ChatWait(
    id="archive-keeps-wait", chat_id=chat.id, description="Wait for CI",
    condition_owner="CI", kind="command", command="true", interval_secs=60,
    deadline_at=now + timedelta(days=1), next_check_at=now + timedelta(minutes=1),
    status="armed", created_at=now,
  ))
  db.commit()

  def _must_not_stop(*_args, **_kwargs):
    raise AssertionError("archiving must not stop the chat's work")

  monkeypatch.setattr("app.routes.chats.stop_chat_for", _must_not_stop)

  assert client.post(f"/api/chats/{chat.id}/archive", headers=auth).status_code == 200

  wait = db.get(models.ChatWait, "archive-keeps-wait")
  db.refresh(wait)
  assert wait.status == "armed"
  assert wait.cancelled_at is None


def test_archive_and_restore_publish_one_live_event_each(
  client, auth, chat, db, monkeypatch,
):
  from app import chat_archive

  published = []

  class _Recorder:
    def publish(self, event):
      published.append(event)

  monkeypatch.setattr(chat_archive, "get_system_broadcast", lambda: _Recorder())

  client.post(f"/api/chats/{chat.id}/archive", headers=auth)
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)  # already archived
  client.post(f"/api/chats/{chat.id}/unarchive", headers=auth)
  client.post(f"/api/chats/{chat.id}/unarchive", headers=auth)  # already restored

  # The event only names the chat; every shell re-reads that row, so a
  # window holding no (or a stale) copy converges on server truth.
  assert published == [
    {"type": "chat_archive_changed", "chatId": chat.id},
    {"type": "chat_archive_changed", "chatId": chat.id},
  ]


def test_unknown_chat_cannot_be_archived_or_restored(client, auth):
  assert client.post("/api/chats/no-such-chat/archive", headers=auth).status_code == 404
  assert client.post("/api/chats/no-such-chat/unarchive", headers=auth).status_code == 404


def test_archiving_requires_the_owner(client, owner_token, chat):
  from tests.test_app_chat_contract import _make_app

  assert client.post(f"/api/chats/{chat.id}/archive").status_code == 401
  _app_id, app_token = _make_app(client, owner_token, "archiver")
  response = client.post(
    f"/api/chats/{chat.id}/archive",
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code in (401, 403)


def test_deleting_an_archived_chat_uses_normal_recovery_and_keeps_it_archived(
  client, auth, chat, db,
):
  chat.has_messages = True
  transcript_rows.replace_all(object_session(chat), chat, [{"role": "user", "content": "filed", "ts": 1}])
  db.commit()
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  deleted = client.delete(f"/api/chats/{chat.id}", headers=auth)
  assert deleted.status_code == 204, deleted.text
  assert _listed(client, auth, chat.id) is None

  recovered = client.post(f"/api/chats/{chat.id}/recover", headers=auth)
  assert recovered.status_code == 200, recovered.text
  row = _listed(client, auth, chat.id)
  assert row is not None and row["archived_at"] is not None


def test_retention_never_purges_an_archived_chat(chat, db):
  from app.chat_retention import purge_expired_chat_tombstones

  chat.archived_at = now_naive_utc() - timedelta(days=400)
  db.commit()

  purged = purge_expired_chat_tombstones(db)

  assert chat.id not in purged
  assert db.get(models.Chat, chat.id) is not None


def test_renaming_an_archived_chat_keeps_it_archived(client, auth, chat, db):
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  renamed = client.patch(
    f"/api/chats/{chat.id}", json={"title": "Filed plan"}, headers=auth,
  )

  assert renamed.status_code == 200, renamed.text
  db.refresh(chat)
  assert chat.title == "Filed plan"
  assert chat.archived_at is not None


def test_an_archived_chat_cannot_be_pinned_so_restore_never_brings_back_a_pin(
  client, auth, chat, db,
):
  """Archive -> stale pin attempt -> restore leaves the chat unpinned."""
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  pinned = client.patch(
    f"/api/chats/{chat.id}", json={"pinned": True}, headers=auth,
  )

  assert pinned.status_code == 409, pinned.text
  db.refresh(chat)
  assert chat.archived_at is not None
  assert chat.pinned_at is None

  restored = client.post(f"/api/chats/{chat.id}/unarchive", headers=auth)
  assert restored.json() == {"archived_at": None, "pinned_at": None}


def test_archived_chats_still_accept_unpin_and_other_edits(client, auth, chat, db):
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  unpinned = client.patch(
    f"/api/chats/{chat.id}", json={"pinned": False, "title": "Filed"}, headers=auth,
  )

  assert unpinned.status_code == 200, unpinned.text
  db.refresh(chat)
  assert chat.pinned_at is None
  assert chat.title == "Filed"


def test_embedded_app_panel_send_leaves_an_archived_chat_archived(
  client, owner_token, auth, db, monkeypatch,
):
  from tests.test_chat_embed_capability import _embed_headers, _session

  _noop_runner(monkeypatch)
  _app_id, _app_token, chat_id, _capability, session = _session(
    client, owner_token, name="archive-embed",
  )
  client.post(f"/api/chats/{chat_id}/archive", headers=auth)

  response = client.post(
    f"/api/chats/{chat_id}/messages",
    json={"content": "from the app panel"},
    headers=_embed_headers(session),
  )

  assert response.status_code == 202, response.text
  row = db.get(models.Chat, chat_id)
  db.refresh(row)
  assert row.archived_at is not None


def test_owner_send_to_a_busy_archived_chat_queues_and_restores(
  client, auth, chat, db, monkeypatch,
):
  _noop_runner(monkeypatch)
  monkeypatch.setattr("app.routes.chats_stream.is_chat_running", lambda _id: True)
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "one more thing"},
    headers=auth,
  )

  assert response.status_code == 202, response.text
  db.refresh(chat)
  assert chat.archived_at is None


def test_project_chats_report_archive_state(client, auth, db):
  created = client.post(
    "/api/projects", json={"name": "Archive project"}, headers=auth,
  )
  assert created.status_code in (200, 201), created.text
  project = created.json()
  chat_id = project["chats"][0]["id"] if project.get("chats") else None
  if chat_id is None:
    made = client.post(
      f"/api/projects/{project['id']}/chats", json={}, headers=auth,
    )
    assert made.status_code in (200, 201), made.text
    chat_id = made.json()["id"]
  client.post(f"/api/chats/{chat_id}/archive", headers=auth)

  chats = client.get(f"/api/projects/{project['id']}/chats", headers=auth)

  assert chats.status_code == 200, chats.text
  row = next(item for item in chats.json() if item["id"] == chat_id)
  assert row["archived_at"] is not None


# ── Migration ──────────────────────────────────────────────────────────────


def test_archive_migration_upgrades_an_existing_chats_table_idempotently(tmp_path):
  from sqlalchemy import create_engine, inspect, text
  from app import schema_migrations

  eng = create_engine(f"sqlite:///{tmp_path / 'pre-archive.db'}")
  with eng.begin() as conn:
    conn.execute(text(
      "CREATE TABLE chats (id VARCHAR(64) PRIMARY KEY, title VARCHAR(256), "
      "updated_at DATETIME, activity_at DATETIME, pinned_at DATETIME, "
      "deleted_at DATETIME, created_by_app_id INTEGER, has_messages BOOLEAN, "
      "pending_question_id VARCHAR(64), project_id VARCHAR(64), "
      "agent_settings_json JSON)"
    ))
    conn.execute(text("INSERT INTO chats (id, title) VALUES ('old', 'Old chat')"))
  schema_migrations._add_chat_drawer_covering_index(eng)

  schema_migrations._add_chat_archive(eng)
  schema_migrations._add_chat_archive(eng)

  inspector = inspect(eng)
  assert "archived_at" in {column["name"] for column in inspector.get_columns("chats")}
  indexes = {index["name"] for index in inspector.get_indexes("chats")}
  assert "ix_chats_drawer_v2" in indexes
  assert "ix_chats_drawer" not in indexes
  with eng.connect() as conn:
    assert conn.execute(text(
      "SELECT archived_at FROM chats WHERE id = 'old'"
    )).scalar() is None


# ── Question cards in archived chats ───────────────────────────────────────


def _archived_chat_with_open_card(client, auth, chat, db, monkeypatch, qid):
  from app import chat as chat_mod
  from app.routes import chats_stream
  from tests.test_chats_stream_answer_race import _seed_question_block

  monkeypatch.setattr(
    chats_stream, "_schedule_continuation",
    lambda **kwargs: chat_mod.discard_starting(kwargs["chat_id"]),
  )
  _seed_question_block(chat.id, qid)
  db.expire_all()
  row = db.get(models.Chat, chat.id)
  row.pending_question_id = qid
  db.commit()
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)


def _answer(client, chat_id, qid, headers):
  return client.post(
    f"/api/chats/{chat_id}/messages",
    json={
      "content": "- Pick one: b", "hidden": True,
      "answers": {"Pick one": "b"}, "question_id": qid,
    },
    headers=headers,
  )


def test_archived_chat_with_an_open_card_is_listed_as_needing_the_owner(
  client, auth, chat, db, monkeypatch,
):
  _archived_chat_with_open_card(client, auth, chat, db, monkeypatch, "q-listed")

  row = _listed(client, auth, chat.id)

  assert row["archived_at"] is not None
  assert row["owner_input_kind"] == "question"


def test_owner_answering_a_card_restores_an_archived_chat(
  client, auth, chat, db, monkeypatch,
):
  _archived_chat_with_open_card(client, auth, chat, db, monkeypatch, "q-owner")

  response = _answer(client, chat.id, "q-owner", auth)

  assert response.status_code == 202, response.text
  db.expire_all()
  assert db.get(models.Chat, chat.id).archived_at is None


def test_stale_question_answer_does_not_restore_an_archived_chat(
  client, auth, chat, db,
):
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  response = _answer(client, chat.id, "missing-question", auth)

  assert response.status_code in (409, 410), response.text
  db.expire_all()
  assert db.get(models.Chat, chat.id).archived_at is not None


def test_exact_answer_retry_does_not_restore_a_later_archive(
  client, auth, chat, db, monkeypatch,
):
  _archived_chat_with_open_card(client, auth, chat, db, monkeypatch, "q-retry")
  first = _answer(client, chat.id, "q-retry", auth)
  assert first.status_code == 202, first.text
  client.post(f"/api/chats/{chat.id}/archive", headers=auth)

  retry = _answer(client, chat.id, "q-retry", auth)

  assert retry.status_code in (202, 409, 410), retry.text
  db.expire_all()
  assert db.get(models.Chat, chat.id).archived_at is not None


def test_agent_answering_a_card_leaves_an_archived_chat_archived(
  client, auth, chat, db, monkeypatch,
):
  _archived_chat_with_open_card(client, auth, chat, db, monkeypatch, "q-agent")
  owner = db.query(models.Owner).first()
  agent_token = auth_mod.create_agent_token(
    chat_id=chat.id, owner_username=owner.username, token_epoch=owner.token_epoch,
  )

  response = _answer(
    client, chat.id, "q-agent", {"Authorization": f"Bearer {agent_token}"},
  )

  assert response.status_code == 202, response.text
  db.expire_all()
  assert db.get(models.Chat, chat.id).archived_at is not None


# ── Project collaborators ──────────────────────────────────────────────────


def test_project_collaborators_cannot_see_or_archive_project_chats(
  client, auth, db,
):
  """Project chats, and so their archive state, belong to the owner alone.

  An invited editor shares the project's files and previews but receives no
  chat list, cannot open, message, search, or archive a project chat, and the
  owner archiving one changes nothing in the collaborator's project view.
  """
  from tests.test_project_collaboration import _invite, _project, _redeem

  project = _project(client, auth, "Shared with Sam")
  made = client.post(f"/api/projects/{project['id']}/chats", json={}, headers=auth)
  assert made.status_code in (200, 201), made.text
  chat_id = made.json()["id"]
  row = db.get(models.Chat, chat_id)
  row.title = "Private planning"
  transcript_rows.replace_all(object_session(row), row, [{"role": "user", "content": "owner-only notes", "ts": 1}])
  row.has_messages = True
  db.commit()
  _invite_payload, secret = _invite(client, auth, project["id"], role="editor")
  _joined, sam = _redeem(client, secret)

  before = client.get(f"/api/projects/{project['id']}", headers=sam)
  assert before.status_code == 200, before.text
  assert before.json()["chats"] == []

  for method, path, body in (
    ("get", "/api/chats", None),
    ("get", f"/api/chats/{chat_id}", None),
    ("get", "/api/chats/search?q=owner-only", None),
    ("get", f"/api/projects/{project['id']}/chats", None),
    ("post", f"/api/chats/{chat_id}/messages", {"content": "hi"}),
    ("post", f"/api/chats/{chat_id}/archive", None),
    ("post", f"/api/chats/{chat_id}/unarchive", None),
  ):
    response = getattr(client, method)(
      path, headers=sam, **({"json": body} if body is not None else {}),
    )
    assert response.status_code in (401, 403), (path, response.status_code)

  client.post(f"/api/chats/{chat_id}/archive", headers=auth)

  after = client.get(f"/api/projects/{project['id']}", headers=sam)
  assert after.status_code == 200, after.text
  assert after.json()["chats"] == []
  assert after.json()["id"] == before.json()["id"]
  db.expire_all()
  assert db.get(models.Chat, chat_id).archived_at is not None
