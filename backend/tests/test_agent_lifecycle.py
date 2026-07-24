"""Durable normalized helper-lifecycle contract."""

import asyncio
from concurrent.futures import Future
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import models
from app import chat as chat_module
from app.agent_lifecycle import normalize_chat_event, record_event
from app.chat import _ChatEventSink
from test_app_fixtures import create_local_app


def _chat_run(db, chat_id="chat-life", run_id="run-life", *, deleted=False):
  chat = models.Chat(
    id=chat_id,
    title="Lifecycle",
    messages=[],
    pending_messages=[],
    deleted_at=datetime(2026, 7, 1) if deleted else None,
  )
  run = models.ChatRun(
    id=run_id,
    chat_id=chat_id,
    provider="claude",
    status="running",
    started_at=datetime(2026, 7, 22, 10, 0, 0),
  )
  db.add_all([chat, run])
  db.commit()
  return chat, run


def _start(*, run_id="run-life", summary="Review storage"):
  return normalize_chat_event(
    chat_id="chat-life",
    chat_run_id=run_id,
    observed_at=datetime(2026, 7, 22, 10, 0, 1),
    event={
      "type": "task_start",
      "task_id": "task-1",
      "description": summary,
      "task_type": "Explore",
      "provider_session_id": "session-1",
      "source_event_id": "uuid-start",
    },
  )


def _terminal(*, status="completed", source="uuid-done"):
  return normalize_chat_event(
    chat_id="chat-life",
    chat_run_id="run-life",
    observed_at=datetime(2026, 7, 22, 10, 1, 0),
    event={
      "type": "task_done",
      "task_id": "task-1",
      "status": status,
      "summary": "Finished the review",
      "provider_session_id": "session-1",
      "source_event_id": source,
    },
  )


def test_normalize_bounds_scrubs_and_never_persists_prompt():
  secret = "sk-" + "x" * 40
  values = _start(summary=f"Inspect {secret} " + "z" * 800)

  assert values["summary"].startswith("Inspect [redacted-key]")
  assert len(values["summary"]) <= 500
  assert "prompt" not in values
  assert values["event_type"] == "agent_started"
  assert values["state"] == "running"
  assert values["time_quality"] == "observed"


def test_record_event_is_idempotent_for_exact_and_alternate_terminal_sources(db):
  _chat_run(db)
  first = _terminal(source="notification-uuid")
  alternate = _terminal(source="updated-uuid")
  assert first["event_key"] != alternate["event_key"]

  assert record_event(db, first) is True
  assert record_event(db, first) is False
  assert record_event(db, alternate) is True
  assert db.query(models.AgentLifecycleEvent).count() == 2


def test_late_start_fact_is_retained_without_rewriting_terminal(db):
  _chat_run(db)
  assert record_event(db, _terminal(status="failed")) is True
  assert record_event(db, _start()) is True

  rows = db.query(models.AgentLifecycleEvent).order_by(
    models.AgentLifecycleEvent.id
  ).all()
  assert [(row.event_type, row.state) for row in rows] == [
    ("agent_terminal", "failed"), ("agent_started", "running"),
  ]


def test_same_claude_task_id_in_different_sessions_has_distinct_identity():
  one = _start()
  two = normalize_chat_event(
    chat_id="chat-life",
    chat_run_id="run-other",
    event={
      "type": "task_start",
      "task_id": "task-1",
      "description": "Other",
      "provider_session_id": "session-2",
    },
  )
  assert one["agent_id"] != two["agent_id"]
  assert one["event_key"] != two["event_key"]


def test_same_logical_agent_in_new_chat_run_has_distinct_activation():
  one = _start(run_id="run-life")
  two = _start(run_id="run-followup")
  assert one["agent_id"] == two["agent_id"]
  assert one["activation_id"] != two["activation_id"]
  assert one["event_key"] != two["event_key"]


def test_hashed_identifiers_fit_the_declared_cross_database_schema():
  values = _start()

  assert len(values["agent_id"]) <= models.AgentLifecycleEvent.agent_id.type.length
  assert (
    len(values["activation_id"])
    <= models.AgentLifecycleEvent.activation_id.type.length
  )
  assert models.AgentLifecycleEvent.activation_id.type.length == 75
  assert models.AgentLifecycleEvent.parent_activation_id.type.length == 75


def test_explicit_main_and_agent_parent_kinds_are_not_conflated():
  main_child = normalize_chat_event(
    chat_id="chat-life", chat_run_id="run-life", event={
      "type": "agent_lifecycle", "provider": "codex",
      "provider_session_id": "root", "provider_agent_id": "child",
      "provider_activation_id": "child-activation",
      "parent_kind": "main", "parent_provider_agent_id": "root",
      "event_type": "agent_spawned",
    })
  nested = normalize_chat_event(
    chat_id="chat-life", chat_run_id="run-life", event={
      "type": "agent_lifecycle", "provider": "codex",
      "provider_session_id": "root", "provider_agent_id": "grandchild",
      "provider_activation_id": "grandchild-activation",
      "parent_kind": "agent", "parent_provider_agent_id": "child",
      "parent_provider_activation_id": "child-activation",
      "event_type": "agent_spawned",
    })
  assert main_child["parent_kind"] == "main"
  assert main_child["parent_agent_id"] is None
  assert nested["parent_kind"] == "agent"
  assert nested["parent_agent_id"] == main_child["agent_id"]
  assert nested["parent_activation_id"] == main_child["activation_id"]


def test_conflicting_duplicate_fact_is_not_silently_acknowledged(db):
  _chat_run(db)
  first = _start(summary="Original")
  conflict = _start(summary="Different")
  assert record_event(db, first) is True
  with pytest.raises(IntegrityError):
    record_event(db, conflict)


def test_sqlite_lifecycle_cursor_is_never_reused_after_tail_delete(db):
  _chat_run(db)
  first = _start(summary="First")
  assert record_event(db, first) is True
  first_id = db.query(models.AgentLifecycleEvent.id).scalar()
  db.query(models.AgentLifecycleEvent).delete()
  db.commit()
  second = normalize_chat_event(
    chat_id="chat-life", chat_run_id="run-life", event={
      "type": "task_start", "task_id": "task-2",
      "provider_session_id": "session-1", "source_event_id": "uuid-2",
    })
  assert record_event(db, second) is True
  assert db.query(models.AgentLifecycleEvent.id).scalar() > first_id


def test_sqlite_run_update_cursor_is_never_reused_after_tail_delete(db):
  _, run = _chat_run(db)
  first_id = db.query(models.AgentLifecycleRunUpdate.id).scalar()
  db.query(models.AgentLifecycleRunUpdate).delete()
  db.commit()
  run.status = "completed"
  run.ended_at = datetime(2026, 7, 22, 10, 2, 0)
  db.commit()
  assert db.query(models.AgentLifecycleRunUpdate.id).scalar() > first_id


def test_chat_run_delete_emits_tombstone_with_foreign_keys_enabled(db):
  _, run = _chat_run(db)
  db.execute(text("PRAGMA foreign_keys=ON"))
  db.delete(run)
  db.commit()
  assert db.get(models.ChatRun, "run-life") is None
  updates = db.query(models.AgentLifecycleRunUpdate).order_by(
    models.AgentLifecycleRunUpdate.id
  ).all()
  assert [row.status for row in updates] == ["running", "deleted"]


def test_claude_start_and_done_share_fallback_session_identity():
  start = normalize_chat_event(
    chat_id="chat-life",
    chat_run_id="run-life",
    event={"type": "task_start", "task_id": "task-1"},
  )
  done = normalize_chat_event(
    chat_id="chat-life",
    chat_run_id="run-life",
    event={"type": "task_done", "task_id": "task-1"},
  )
  assert start["provider_session_id"] == "run:run-life"
  assert start["agent_id"] == done["agent_id"]


def test_codex_interrupted_normalizes_to_canonical_terminal_event():
  values = normalize_chat_event(
    chat_id="chat-life",
    chat_run_id=None,
    event={
      "type": "agent_lifecycle",
      "provider": "codex",
      "provider_session_id": "root-thread",
      "provider_agent_id": "child-thread",
      "event_type": "agent_terminal",
      "state": "stopped",
    },
  )
  assert values is not None
  assert values["event_type"] == "agent_terminal"
  assert values["state"] == "stopped"


def test_sink_submits_lifecycle_through_writer_actor(db):
  _chat_run(db)

  class Bus:
    def publish(self, _event):
      pass

  sink = _ChatEventSink(Bus(), "chat-life", "run-life")
  sink.record_lifecycle({
    "type": "task_start",
    "task_id": "task-1",
    "description": "Inspect the parser",
    "provider_session_id": "session-1",
    "source_event_id": "uuid-1",
  })
  # Even a content-free turn fences its private lifecycle writes.
  asyncio.run(sink.finalize())

  db.expire_all()
  row = db.query(models.AgentLifecycleEvent).one()
  assert row.chat_id == "chat-life"
  assert row.chat_run_id == "run-life"
  assert row.summary == "Inspect the parser"
  assert row.activation_id.startswith("activation-")


def test_sink_fences_and_retries_unreconstructable_lifecycle_fact(monkeypatch):
  first, second = Future(), Future()
  first.set_exception(RuntimeError("temporary write failure"))
  second.set_result(True)

  class Writer:
    def __init__(self):
      self.commands = []

    def submit(self, command):
      self.commands.append(command)
      if command.ack is None:
        command.ack = first if len(self.commands) == 1 else second
      return command.ack

  writer = Writer()
  monkeypatch.setattr(chat_module, "get_writer", lambda: writer)
  sink = _ChatEventSink(object(), "chat-life", "run-life")
  sink.record_lifecycle({
    "type": "task_start", "task_id": "task-1",
    "provider_session_id": "session-1", "source_event_id": "uuid-1",
  })
  asyncio.run(sink.finalize())
  assert len(writer.commands) == 2
  assert writer.commands[0] is not writer.commands[1]
  assert writer.commands[0].values == writer.commands[1].values


def test_owner_endpoint_paginates_events_and_run_updates_independently(
  client, auth, db,
):
  _, run = _chat_run(db)
  assert record_event(db, _start()) is True
  assert record_event(db, _terminal()) is True
  run.status = "resume_pending"
  db.commit()

  # Deleted-chat events never leave the owner endpoint.
  _chat_run(db, "chat-deleted", "run-deleted", deleted=True)
  hidden = normalize_chat_event(
    chat_id="chat-deleted",
    chat_run_id="run-deleted",
    event={
      "type": "task_start",
      "task_id": "hidden",
      "description": "Hidden",
      "provider_session_id": "hidden-session",
    },
  )
  assert record_event(db, hidden) is True

  first = client.get(
    "/api/chats/agent-lifecycle?after_id=0&limit=1&run_limit=1", headers=auth,
  )
  assert first.status_code == 200, first.text
  page = first.json()
  assert len(page["events"]) == 1
  assert page["has_more"] is True
  assert page["events"][0]["chat_id"] == "chat-life"
  assert page["events"][0]["summary"] == "Review storage"
  assert "prompt" not in page["events"][0]
  assert [item["id"] for item in page["runs"]] == ["run-life"]
  assert page["runs_has_more"] is True
  run_cursor = page["next_runs_after_id"]

  # The run projection is current on every page, even with no newer event.
  run.status = "completed"
  run.ended_at = run.started_at + timedelta(minutes=3)
  db.commit()
  tail = client.get(
    f"/api/chats/agent-lifecycle?after_id=999999&runs_after_id={run_cursor}&limit=1",
    headers=auth,
  ).json()
  assert tail["events"] == []
  assert tail["runs"][-1]["status"] == "completed"
  assert tail["runs"][-1]["ended_at"] == "2026-07-22T10:03:00"

  # A consumer that saw other, later ids while this chat was soft-deleted can
  # replay the recovered chat alone from zero without rewinding the global feed.
  chat = db.get(models.Chat, "chat-life")
  chat.deleted_at = datetime(2026, 7, 22, 10, 4, 0)
  db.commit()
  hidden_replay = client.get(
    "/api/chats/agent-lifecycle?after_id=0&chat_id=chat-life", headers=auth,
  ).json()
  assert hidden_replay["events"] == []
  chat.deleted_at = None
  db.commit()
  recovered = client.get(
    "/api/chats/agent-lifecycle?after_id=0&runs_after_id=0&chat_id=chat-life",
    headers=auth,
  ).json()
  assert len(recovered["events"]) == 2
  assert recovered["runs"][-1]["status"] == "completed"


def test_owner_endpoint_rejects_app_token(client, owner_token):
  headers = {"Authorization": f"Bearer {owner_token}"}
  created = create_local_app(
    client, headers, name="lifecycle-reader", description="test",
  )
  token = client.post(
    "/api/auth/app-token", json={"app_id": created["id"]},
    headers=headers,
  ).json()["token"]

  response = client.get(
    "/api/chats/agent-lifecycle",
    headers={"Authorization": f"Bearer {token}"},
  )
  assert response.status_code == 403


def test_stale_chat_hard_purge_removes_lifecycle_before_run(client, auth, db):
  _chat_run(db, "chat-stale", "run-stale", deleted=True)
  values = normalize_chat_event(
    chat_id="chat-stale",
    chat_run_id="run-stale",
    event={
      "type": "task_start",
      "task_id": "stale-task",
      "description": "Old task",
      "provider_session_id": "stale-session",
    },
  )
  assert record_event(db, values) is True

  response = client.get("/api/chats", headers=auth)
  assert response.status_code == 200, response.text
  db.expire_all()
  assert db.query(models.AgentLifecycleEvent).filter_by(
    event_key=values["event_key"]
  ).first() is None
  assert db.query(models.AgentLifecycleRunUpdate).filter_by(
    chat_id="chat-stale"
  ).first() is None
  assert db.get(models.ChatRun, "run-stale") is None
  assert db.get(models.Chat, "chat-stale") is None
