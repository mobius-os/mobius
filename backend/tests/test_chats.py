"""Chat route regression tests."""

import asyncio
from datetime import UTC, datetime
import io
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app import memory, models, questions
from app.pending_questions import PendingQuestion
from sqlalchemy import event


def _make_pending() -> PendingQuestion:
  """Builds a fresh PendingQuestion with a live future on the running loop."""
  return PendingQuestion(
    question_id=str(uuid4()),
    questions=[{"id": "q1", "question": "Pick one", "options": ["a", "b"]}],
    future=asyncio.get_event_loop().create_future(),
  )


def test_delete_chat_cancels_orphan_pending_question(client, auth, chat):
  # This belongs in the chat route tests because it exercises DELETE
  # /api/chats/{id} and its side effects on idle-chat cleanup.
  async def go():
    pending = _make_pending()
    questions.register(chat.id, pending)

    response = client.delete(f"/api/chats/{chat.id}", headers=auth)

    assert response.status_code == 204
    assert questions.get(chat.id) is None
    assert pending.future.done()

  asyncio.run(go())


def test_delete_and_recover_publish_exact_projection_events(
  client, auth, chat,
):
  """Shells receive authoritative ids for both sides of the tombstone."""
  with patch("app.routes.chats.get_system_broadcast") as mock_get_sb:
    fake_sb = MagicMock()
    mock_get_sb.return_value = fake_sb

    deleted = client.delete(f"/api/chats/{chat.id}", headers=auth)
    recovered = client.post(f"/api/chats/{chat.id}/recover", headers=auth)

  assert deleted.status_code == 204, deleted.text
  assert recovered.status_code == 200, recovered.text
  assert fake_sb.publish.call_args_list == [
    (({"type": "chat_deleted", "chatId": str(chat.id)},), {}),
    (({"type": "chat_recovered", "chatId": str(chat.id)},), {}),
  ]


def test_delete_response_stays_authoritative_after_cleanup_failure(
  client, auth, chat,
):
  """Post-commit cleanup cannot turn a durable deletion into a false failure."""
  with (
    patch(
      "app.routes.chats._finish_run",
      new=AsyncMock(side_effect=RuntimeError("cleanup failed")),
    ),
    patch("app.routes.chats.get_system_broadcast") as mock_get_sb,
  ):
    fake_sb = MagicMock()
    mock_get_sb.return_value = fake_sb
    deleted = client.delete(f"/api/chats/{chat.id}", headers=auth)

  assert deleted.status_code == 204, deleted.text
  assert client.get(f"/api/chats/{chat.id}", headers=auth).status_code == 404
  fake_sb.publish.assert_called_once_with({
    "type": "chat_deleted", "chatId": str(chat.id),
  })


def test_agent_context_includes_evolving_chat_summary(
  client, auth, chat, monkeypatch,
):
  monkeypatch.setattr(
    "app.compaction.load_cumulative_summary",
    lambda _data_dir, chat_id: (
      "The cumulative handoff." if chat_id == chat.id else None
    ),
  )
  monkeypatch.setattr(
    "app.memory.load_chat_summary_metadata",
    lambda _data_dir, chat_id: {
      "description": "A one-line summary" if chat_id == chat.id else None,
      "digest": "The bounded digest." if chat_id == chat.id else None,
    },
  )
  monkeypatch.setattr(
    "app.memory.build_memory_block",
    lambda *_args, **_kwargs: memory.MemoryBlock(
      text="<recent_chat>...</recent_chat>",
      loaded=["chats/older/index.md"],
      entries=[{
        "name": "Older chat",
        "location": "chats/older/index.md",
        "digest": "A bounded digest.",
      }],
      mode="recent_chats",
    ),
  )
  monkeypatch.setattr("app.providers.get_skill_origin", lambda: "platform")

  response = client.get(
    f"/api/chats/{chat.id}/agent-context",
    headers=auth,
  )

  assert response.status_code == 200
  payload = response.json()
  assert {
    key: payload[key]
    for key in ("chat_description", "chat_digest", "chat_summary")
  } == {
    "chat_description": "A one-line summary",
    "chat_digest": "The bounded digest.",
    "chat_summary": "The cumulative handoff.",
  }
  assert payload["recent_chat_entries"] == [{
    "name": "Older chat",
    "location": "chats/older/index.md",
    "digest": "A bounded digest.",
  }]
  assert payload["system_prompt_origin"] == "platform"


def test_chat_reads_keep_goal_identity_after_a_mid_turn_question(
  client, auth, chat, db,
):
  started_at = datetime.now(UTC)
  started_ms = int(started_at.timestamp() * 1000)
  chat.messages = [
    {
      "role": "user",
      "content": "/goal finish the review",
      "ts": started_ms - 5,
      "cid": "goal-start",
    },
    {"role": "assistant", "content": "Working", "ts": started_ms + 5},
    {
      "role": "user",
      "content": "A steered question",
      "ts": started_ms + 10,
      "cid": "goal-steer",
    },
  ]
  db.add(models.ChatRun(
    id="active-goal-run",
    chat_id=chat.id,
    status="running",
    provider="codex",
    goal_objective="finish the review",
    started_at=started_at,
  ))
  db.commit()

  detail = client.get(f"/api/chats/{chat.id}", headers=auth)
  runtime = client.get(f"/api/chats/{chat.id}/runtime", headers=auth)

  assert detail.status_code == 200
  assert runtime.status_code == 200
  assert detail.json()["active_goal_objective"] == "finish the review"
  assert runtime.json()["active_goal_objective"] == "finish the review"


def test_chat_usage_reports_totals_and_historic_coverage(
  client, auth, chat, db,
):
  db.add_all([
    models.ChatRun(
      id="historic-run",
      chat_id=chat.id,
      status="completed",
      provider="claude",
      started_at=datetime.now(UTC),
    ),
    models.ChatRun(
      id="measured-run",
      chat_id=chat.id,
      status="completed",
      provider="codex",
      provider_session_id="thread-1",
      cost_usd=0.125,
      input_tokens=900,
      output_tokens=200,
      cache_read_input_tokens=500,
      cache_creation_input_tokens=0,
      reasoning_output_tokens=100,
      total_tokens=1_100,
      model_context_window=200_000,
      usage_json={"provider": "codex", "calculation": "thread_delta"},
      started_at=datetime.now(UTC),
    ),
  ])
  db.commit()

  response = client.get(f"/api/chats/{chat.id}/usage", headers=auth)

  assert response.status_code == 200
  payload = response.json()
  assert payload["coverage"] == {
    "runs": 2,
    "runs_with_usage": 1,
    "runs_with_cost": 1,
  }
  assert payload["totals"] == {
    "input_tokens": 900,
    "output_tokens": 200,
    "cache_read_input_tokens": 500,
    "cache_creation_input_tokens": 0,
    "reasoning_output_tokens": 100,
    "total_tokens": 1_100,
    "cost_usd": 0.125,
  }
  measured = next(
    run for run in payload["runs"] if run["id"] == "measured-run"
  )
  assert measured["provider_session_id"] == "thread-1"
  assert measured["model_context_window"] == 200_000
  assert measured["usage"]["calculation"] == "thread_delta"

  summary_response = client.get(
    f"/api/chats/{chat.id}/usage?include_runs=false", headers=auth,
  )
  assert summary_response.status_code == 200
  summary = summary_response.json()
  assert summary["coverage"] == payload["coverage"]
  assert summary["totals"] == payload["totals"]
  assert summary["runs"] == []


def test_current_chat_usage_is_bounded_to_selected_provider_session(
  client, auth, chat, db,
):
  now = datetime.now(UTC)
  chat.provider = "codex"
  chat.session_id = "thread-current"
  db.add_all([
    models.ChatRun(
      id="older-thread",
      chat_id=chat.id,
      status="completed",
      provider="codex",
      provider_session_id="thread-old",
      model_context_window=200_000,
      usage_json={
        "provider": "codex",
        "model_calls": [{"input_tokens": 150_000}],
      },
      started_at=now,
    ),
    models.ChatRun(
      id="selected-thread",
      chat_id=chat.id,
      status="completed",
      provider="codex",
      provider_session_id="thread-current",
      model_context_window=258_400,
      usage_json={
        "provider": "codex",
        "model_calls": [
          {"input_tokens": 80_000},
          {"input_tokens": 193_800},
        ],
      },
      started_at=now,
    ),
  ])
  db.commit()

  response = client.get(
    f"/api/chats/{chat.id}/usage/current",
    headers=auth,
  )

  assert response.status_code == 200
  assert response.json() == {
    "provider": "codex",
    "provider_session_id": "thread-current",
    "input_tokens": 193_800,
    "context_window": 258_400,
  }


def test_current_chat_usage_reads_normalized_claude_call_occupancy(
  client, auth, chat, db,
):
  chat.provider = "claude"
  chat.session_id = "claude-session-current"
  db.add(models.ChatRun(
    id="selected-claude-session",
    chat_id=chat.id,
    status="completed",
    provider="claude",
    provider_session_id="claude-session-current",
    model_context_window=200_000,
    usage_json={
      "provider": "claude",
      "latest_model_input_tokens": 123_456,
    },
    started_at=datetime.now(UTC),
  ))
  db.commit()

  response = client.get(
    f"/api/chats/{chat.id}/usage/current",
    headers=auth,
  )

  assert response.status_code == 200
  assert response.json() == {
    "provider": "claude",
    "provider_session_id": "claude-session-current",
    "input_tokens": 123_456,
    "context_window": 200_000,
  }


def test_current_chat_usage_reads_codex_shaped_app_provider_metrics(
  client, auth, chat, db,
):
  chat.provider = "mobius"
  chat.session_id = "mobius-session"
  db.add(models.ChatRun(
    id="mobius-evolve-run",
    chat_id=chat.id,
    status="completed",
    provider="mobius",
    provider_session_id="mobius-session",
    model_context_window=235_929,
    usage_json={
      "provider": "codex",
      "model_calls": [
        {"input_tokens": 12_000},
        {"input_tokens": 20_220},
      ],
    },
    started_at=datetime.now(UTC),
  ))
  db.commit()

  response = client.get(
    f"/api/chats/{chat.id}/usage/current",
    headers=auth,
  )

  assert response.status_code == 200
  assert response.json() == {
    "provider": "mobius",
    "provider_session_id": "mobius-session",
    "input_tokens": 20_220,
    "context_window": 235_929,
  }


def test_current_chat_usage_returns_unknown_for_a_fresh_session(
  client, auth, chat,
):
  response = client.get(
    f"/api/chats/{chat.id}/usage/current",
    headers=auth,
  )

  assert response.status_code == 200
  assert response.json() == {
    "provider": "claude",
    "provider_session_id": None,
    "input_tokens": None,
    "context_window": None,
  }


def test_create_chat_rejects_cross_site_request(client, auth):
  cross = client.post(
    "/api/chats",
    json={"title": "Blocked"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_create_chat_returns_canonical_owner_drawer_summary(client, auth):
  created = client.post(
    "/api/chats",
    json={"title": "Canonical create"},
    headers=auth,
  )
  assert created.status_code == 200
  body = created.json()

  listed = client.get("/api/chats", headers=auth)
  assert listed.status_code == 200
  row = next(item for item in listed.json() if item["id"] == body["id"])
  assert {key: body[key] for key in row} == row
  assert body["messages"] == []

  detail = client.get(f"/api/chats/{body['id']}", headers=auth)
  assert detail.status_code == 200
  detail_body = detail.json()
  assert body["detail"] == detail_body


def test_create_chat_honors_client_uuid_and_retries_idempotently(
  client, auth, db,
):
  chat_id = str(uuid4())
  payload = {"id": chat_id, "title": "Client-owned chat"}

  first = client.post("/api/chats", json=payload, headers=auth)
  retry = client.post("/api/chats", json=payload, headers=auth)

  assert first.status_code == 200, first.text
  assert retry.status_code == 200, retry.text
  assert first.json()["id"] == chat_id
  assert retry.json() == first.json()
  assert db.query(models.Chat).filter(models.Chat.id == chat_id).count() == 1

  detail = client.get(f"/api/chats/{chat_id}", headers=auth)
  assert detail.status_code == 200, detail.text
  assert retry.json()["detail"] == detail.json()


def test_create_chat_rejects_invalid_client_uuid(client, auth):
  created = client.post(
    "/api/chats",
    json={"id": "not-a-uuid", "title": "Invalid client id"},
    headers=auth,
  )

  assert created.status_code == 422
  assert created.json()["detail"] == "invalid chat id"


def test_create_chat_rejects_tombstoned_client_uuid(client, auth):
  chat_id = str(uuid4())
  created = client.post(
    "/api/chats",
    json={"id": chat_id, "title": "Delete this chat"},
    headers=auth,
  )
  assert created.status_code == 200, created.text
  deleted = client.delete(f"/api/chats/{chat_id}", headers=auth)
  assert deleted.status_code == 204, deleted.text

  retry = client.post(
    "/api/chats",
    json={"id": chat_id, "title": "Do not resurrect"},
    headers=auth,
  )

  assert retry.status_code == 409
  assert retry.json()["detail"] == "chat id was deleted"


def test_create_chat_returns_occupied_client_uuid_unchanged(client, auth):
  chat_id = str(uuid4())
  original_messages = [{"role": "user", "content": "Existing history"}]
  original = client.post(
    "/api/chats",
    json={
      "id": chat_id,
      "title": "Existing chat",
      "messages": original_messages,
    },
    headers=auth,
  )
  assert original.status_code == 200, original.text

  occupied = client.post(
    "/api/chats",
    json={
      "id": chat_id,
      "title": "Replacement title",
      "messages": [{"role": "user", "content": "Replacement history"}],
    },
    headers=auth,
  )

  assert occupied.status_code == 200, occupied.text
  assert occupied.json() == original.json()
  assert occupied.json()["title"] == "Existing chat"
  assert occupied.json()["messages"] == original_messages


def test_create_repair_chat_is_idempotent_across_ambiguous_retries(client, auth):
  payload = {
    "title": "Fix a Möbius error",
    "recovery_request_id": "recovery-request-1",
  }
  first = client.post("/api/chats", json=payload, headers=auth)
  retry = client.post("/api/chats", json=payload, headers=auth)

  assert first.status_code == 200
  assert retry.status_code == 200
  assert retry.json()["id"] == first.json()["id"]
  assert retry.json()["messages"] == []

  listed = client.get("/api/chats", headers=auth)
  assert listed.status_code == 200
  matches = [row for row in listed.json() if row["id"] == first.json()["id"]]
  assert len(matches) == 1


def test_chat_list_projects_summaries_without_hydrating_transcripts(
  client, auth, db,
):
  created = client.post(
    "/api/chats",
    json={
      "title": "Projected row",
      "messages": [{"role": "user", "content": "large history sentinel"}],
    },
    headers=auth,
  )
  assert created.status_code == 200

  hydrated_chat_ids = []
  drawer_selects = []

  def on_load(chat, _context):
    hydrated_chat_ids.append(chat.id)

  def capture_sql(_conn, _cursor, statement, _parameters, _context, _many):
    if "FROM chats" in statement:
      drawer_selects.append(statement)

  event.listen(models.Chat, "load", on_load)
  event.listen(db.get_bind(), "before_cursor_execute", capture_sql)
  try:
    listed = client.get("/api/chats", headers=auth)
  finally:
    event.remove(models.Chat, "load", on_load)
    event.remove(db.get_bind(), "before_cursor_execute", capture_sql)

  assert listed.status_code == 200
  row = next(item for item in listed.json() if item["id"] == created.json()["id"])
  assert row["has_messages"] is True
  assert hydrated_chat_ids == [], (
    "the drawer list must not instantiate Chat objects and decode messages"
  )
  drawer_query = next(
    statement for statement in drawer_selects
    if "ORDER BY" in statement and "chats.has_messages" in statement
  )
  assert "chats.messages AS" not in drawer_query, (
    "the drawer list must not read the transcript blob"
  )


def test_chat_list_message_summary_tracks_transcript_replacements(
  client, auth,
):
  created = client.post(
    "/api/chats",
    json={
      "title": "Summary invariant",
      "messages": [{"role": "user", "content": "hello"}],
    },
    headers=auth,
  )
  assert created.status_code == 200
  chat_id = created.json()["id"]

  cleared = client.put(
    f"/api/chats/{chat_id}", json={"messages": []}, headers=auth,
  )
  assert cleared.status_code == 200
  listed = client.get("/api/chats", headers=auth)
  assert next(row for row in listed.json() if row["id"] == chat_id)[
    "has_messages"
  ] is False

  restored = client.put(
    f"/api/chats/{chat_id}",
    json={"messages": [{"role": "user", "content": "back"}]},
    headers=auth,
  )
  assert restored.status_code == 200
  listed = client.get("/api/chats", headers=auth)
  assert next(row for row in listed.json() if row["id"] == chat_id)[
    "has_messages"
  ] is True


def test_chat_list_orders_by_owner_activity_not_agent_updates(
  client, auth, db,
):
  """A later agent write must not outrank a newer owner send or steer."""
  db.add_all([
    models.Chat(
      id="agent-finished",
      title="Agent finished",
      messages=[{"role": "user", "content": "older owner activity"}],
      activity_at=datetime(2026, 7, 28, 9, 0, tzinfo=UTC),
      updated_at=datetime(2026, 7, 28, 12, 0, tzinfo=UTC),
    ),
    models.Chat(
      id="owner-steered",
      title="Owner steered",
      messages=[{"role": "user", "content": "newer owner activity"}],
      activity_at=datetime(2026, 7, 28, 11, 0, tzinfo=UTC),
      updated_at=datetime(2026, 7, 28, 11, 0, tzinfo=UTC),
    ),
  ])
  db.commit()

  listed = client.get("/api/chats", headers=auth)

  assert listed.status_code == 200
  ids = [row["id"] for row in listed.json()]
  assert ids.index("owner-steered") < ids.index("agent-finished")


def test_update_chat_rejects_cross_site_request(client, auth, chat):
  cross = client.put(
    f"/api/chats/{chat.id}",
    json={"title": "Blocked"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_delete_chat_rejects_cross_site_request(client, auth, chat):
  cross = client.delete(
    f"/api/chats/{chat.id}",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_recover_chat_rejects_cross_site_request(client, auth, chat):
  cross = client.post(
    f"/api/chats/{chat.id}/recover",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_question_answers_rejects_cross_site_request(client, auth, chat):
  cross = client.post(
    f"/api/chats/{chat.id}/question-answers",
    json={"answers": {"q1": "red"}},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_send_message_rejects_cross_site_request(client, auth, chat):
  cross = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "hi"},
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_send_requires_explicit_model_before_any_durable_side_effect(
  client, auth, chat, db,
):
  chat.agent_settings_json = None
  db.commit()

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "keep this as a draft", "cid": "missing-model"},
    headers=auth,
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"] == {
    "code": "model_selection_required",
    "message": "Choose a model before sending this chat.",
  }
  db.refresh(chat)
  assert chat.messages == []
  assert chat.pending_messages == []
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == chat.id,
  ).count() == 0


def test_fresh_send_response_includes_stored_user_message(
  client, auth, chat, db, monkeypatch,
):
  async def _noop_run_chat(*args, **kwargs):
    return None

  monkeypatch.setattr("app.routes.chats_stream.run_chat", _noop_run_chat)

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "build forge"},
    headers=auth,
  )

  assert response.status_code == 202, response.text
  body = response.json()
  assert body["status"] == "started"
  assert body["message"]["role"] == "user"
  assert body["message"]["content"] == "build forge"
  assert isinstance(body["message"]["ts"], int)

  db.refresh(chat)
  assert chat.messages == [body["message"]]


def test_uploaded_file_can_start_a_turn_without_typed_text(
  client, auth, chat, db, monkeypatch,
):
  async def _noop_run_chat(*args, **kwargs):
    return None

  monkeypatch.setattr("app.routes.chats_stream.run_chat", _noop_run_chat)
  uploaded = client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("brief.txt", io.BytesIO(b"review this"), "text/plain"))],
    headers=auth,
  )
  assert uploaded.status_code == 200, uploaded.text
  attachment = {
    key: uploaded.json()[0][key]
    for key in ("name", "size", "mime_type")
  }

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "", "attachments": [attachment]},
    headers=auth,
  )

  assert response.status_code == 202, response.text
  body = response.json()
  assert body["status"] == "started"
  assert body["message"]["attachments"] == [attachment]
  assert "[Files in this session:" in body["message"]["content"]
  assert "brief.txt" in body["message"]["content"]

  db.refresh(chat)
  assert chat.messages == [body["message"]]


def test_retry_of_durable_message_is_acknowledged_without_new_turn(
  client, auth, chat, db, monkeypatch,
):
  calls = []

  async def _record_run_chat(*args, **kwargs):
    calls.append((args, kwargs))

  monkeypatch.setattr("app.routes.chats_stream.run_chat", _record_run_chat)
  stored = {
    "role": "user",
    "content": "build forge",
    "ts": 123,
    "cid": "cid-retry",
  }
  chat.messages = [stored]
  db.commit()

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "build forge", "cid": "cid-retry"},
    headers=auth,
  )

  assert response.status_code == 200, response.text
  assert response.json() == {
    "status": "duplicate",
    "message": stored,
    "running": False,
  }
  db.refresh(chat)
  assert chat.messages == [stored]
  assert calls == []


def test_retry_of_durable_message_preserves_a_later_running_turn(
  client, auth, chat, db, monkeypatch,
):
  stored = {
    "role": "user",
    "content": "first request",
    "ts": 123,
    "cid": "cid-retry",
  }
  chat.messages = [stored]
  db.commit()
  monkeypatch.setattr(
    "app.routes.chats_stream.is_chat_running", lambda _chat_id: True,
  )

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "first request", "cid": "cid-retry"},
    headers=auth,
  )

  assert response.status_code == 200, response.text
  assert response.json()["status"] == "duplicate"
  assert response.json()["running"] is True
  db.refresh(chat)
  assert chat.messages == [stored]


def test_retry_of_pending_message_returns_its_existing_queue_position(
  client, auth, chat, db, monkeypatch,
):
  first = {
    "role": "user", "content": "first", "ts": 10, "cid": "cid-first",
  }
  retry = {
    "role": "user", "content": "second", "ts": 11, "cid": "cid-retry",
  }
  chat.pending_messages = [first, retry]
  db.commit()
  monkeypatch.setattr(
    "app.routes.chats_stream.is_chat_running", lambda _chat_id: True,
  )

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "second", "cid": "cid-retry"},
    headers=auth,
  )

  assert response.status_code == 202, response.text
  assert response.json()["status"] == "queued"
  assert response.json()["position"] == 2
  assert response.json()["pending_message"] == retry
  db.refresh(chat)
  assert chat.pending_messages == [first, retry]


def test_fresh_send_returns_503_when_writer_is_unavailable(client, auth, chat):
  from app.chat_writer import get_writer

  get_writer()._go_fatal()
  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "please keep this draft"},
    headers=auth,
  )

  assert response.status_code == 503
  assert response.json()["detail"] == "Could not save your message; please try again."


def test_queued_send_returns_503_when_writer_is_unavailable(
  client, auth, chat, db,
):
  from app.chat_writer import get_writer

  chat.pending_messages = [{
    "role": "user", "content": "already queued", "ts": 1, "cid": "prior",
  }]
  db.commit()
  get_writer()._go_fatal()

  response = client.post(
    f"/api/chats/{chat.id}/messages",
    json={"content": "queue this too"},
    headers=auth,
  )

  assert response.status_code == 503
  assert response.json()["detail"] == "Could not save your message; please try again."


def test_update_icon_rejects_cross_site_request(client, auth):
  # The cross-site dependency fires before the handler, so a non-existent
  # app id still 403s (mirrors test_update_app_rejects_cross_site_request).
  cross = client.put(
    "/api/apps/1/icon",
    content=b"not-an-image",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_chat_title_naming_precedence(client, auth, db, chat):
  """user > agent > first-message: a manual rename locks the name (the agent's
  by_agent sync can't clobber it); clear unlocks + falls back to the first
  message; the agent can fill the name again once it's unlocked."""
  from app import models
  cid = chat.id
  chat.messages = [{"role": "user", "content": "help me dial in espresso"}]
  chat.title = "help me dial in espresso"
  db.commit()

  def patch(payload):
    return client.patch(f"/api/chats/{cid}", json=payload, headers=auth)

  def current():
    db.expire_all()
    return db.query(models.Chat).filter_by(id=cid).first()

  # 1) agent fills the name when not locked
  assert patch({"title": "Espresso shot dial-in", "by_agent": True}).status_code == 200
  c = current(); assert c.title == "Espresso shot dial-in" and c.title_locked is False
  # 2) a manual (user) rename locks it
  assert patch({"title": "Coffee help"}).status_code == 200
  c = current(); assert c.title == "Coffee help" and c.title_locked is True
  # 3) the agent can NOT overwrite a locked name
  patch({"title": "Something else", "by_agent": True})
  assert current().title == "Coffee help"
  # 4) clear unlocks + falls back to the first message
  assert patch({"clear_title": True}).status_code == 200
  c = current(); assert c.title == "help me dial in espresso" and c.title_locked is False
  # 5) the agent can fill again once unlocked
  patch({"title": "Espresso dial-in", "by_agent": True})
  assert current().title == "Espresso dial-in"


def test_committed_chat_rename_publishes_live_projection_event(
  client, auth, db, chat,
):
  """The summary-owned rename reaches open tabs only after durable commit."""
  with patch("app.routes.chats.get_system_broadcast") as mock_get_sb:
    fake_sb = MagicMock()
    mock_get_sb.return_value = fake_sb

    renamed = client.patch(
      f"/api/chats/{chat.id}",
      json={"title": "Current topic", "by_agent": True},
      headers=auth,
    )
    unchanged = client.patch(
      f"/api/chats/{chat.id}",
      json={"title": "Current topic", "by_agent": True},
      headers=auth,
    )

  assert renamed.status_code == 200
  assert unchanged.status_code == 200
  db.expire_all()
  refreshed = db.query(models.Chat).filter_by(id=chat.id).one()
  assert refreshed.title == "Current topic"
  fake_sb.publish.assert_called_once_with({
    "type": "chat_renamed",
    "chatId": str(chat.id),
    "title": "Current topic",
    "updatedAt": refreshed.updated_at.isoformat(),
  })


def test_clearing_chat_title_uses_the_same_first_message_preview_limit(
  client, auth, db, chat,
):
  """Resetting a name preserves the same 80-character drawer fallback."""
  content = [
    {"type": "text", "text": (
      "Explain how the chat drawer derives a temporary title"
    )},
    {"type": "text", "text": (
      "from the opening message before a summary is available"
    )},
  ]
  first_message = " ".join(part["text"] for part in content)
  chat.messages = [{"role": "user", "content": content}]
  chat.title = "Drawer title behavior"
  chat.title_locked = True
  db.commit()

  response = client.patch(
    f"/api/chats/{chat.id}",
    json={"clear_title": True},
    headers=auth,
  )

  assert response.status_code == 200
  db.expire_all()
  current = db.query(models.Chat).filter_by(id=chat.id).first()
  assert current.title == first_message[:80]
  assert current.title_locked is False
