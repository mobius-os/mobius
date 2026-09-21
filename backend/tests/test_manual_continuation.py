"""Manual Resume attribution stays product-owned while providers can continue."""

import pytest
from pydantic import ValidationError

from app import schemas
from app.routes.chats_stream import _user_message_from_body


def test_manual_resume_builds_a_typed_control_without_content(chat):
  message = _user_message_from_body(
    chat,
    schemas.SendMessage(content="", continuation="manual"),
  )

  assert message["role"] == "user"
  assert message["content"] == ""
  assert message["kind"] == "continuation"
  assert message["continuation_reason"] == "manual"


def test_manual_resume_cannot_hide_arbitrary_owner_prose():
  with pytest.raises(ValidationError):
    schemas.SendMessage(
      content="delete everything",
      continuation="manual",
    )


@pytest.mark.parametrize("later_status", ["running", "completed"])
def test_delayed_resume_cannot_restart_an_automatically_superseded_turn(
  client, auth, chat, monkeypatch, later_status,
):
  from app.chat_writer import AppendPending, FinishRun, StartContinuation, StartTurn, get_writer
  from app.database import SessionLocal
  from app import models

  cid = chat.id
  get_writer().submit(StartTurn(
    chat_id=cid, run_token="original-a",
    user_msg={"role": "user", "content": "A", "cid": "a", "ts": 1},
  )).result(timeout=5)
  get_writer().submit(AppendPending(
    chat_id=cid, user_msg={"role": "user", "content": "B", "cid": "b", "ts": 2},
  )).result(timeout=5)
  # The automatic path already admitted A's continuation before the owner's
  # previously rendered Resume request reaches the actor.
  with SessionLocal() as db:
    db.get(models.ChatRun, "original-a").status = "resume_pending"
    db.commit()
  get_writer().submit(StartContinuation(
    chat_id=cid, root_run_id="original-a", run_token="automatic-a",
    supersedes_run_token="original-a", content="continue",
    cid="automatic-marker", reason="restart",
  )).result(timeout=5)
  if later_status == "completed":
    get_writer().submit(FinishRun(
      chat_id=cid, run_token="automatic-a", terminal_status="completed",
    )).result(timeout=5)
  monkeypatch.setattr("app.routes.chats_stream.run_chat", lambda *a, **k: pytest.fail("stale Resume started work"))
  response = client.post(f"/api/chats/{cid}/messages", headers=auth, json={
    "content": "", "continuation": "manual", "cid": "late-manual",
    "resume_run_id": "original-a",
  })
  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "recovery_changed"
  with SessionLocal() as db:
    current = db.get(models.Chat, cid)
    assert [row["cid"] for row in current.pending_messages] == ["b"]
    assert all(row.get("cid") != "late-manual" for row in current.messages)
    assert db.query(models.ChatRun).filter_by(chat_id=cid).count() == 2


@pytest.mark.parametrize("app_id", [None, 42])
def test_manual_resume_retry_preserves_queue_and_original_authority(
  client, auth, chat, monkeypatch, app_id,
):
  from app.chat_writer import AppendPending, FinishRun, StartTurn, get_writer
  from app.database import SessionLocal
  from app import chat as chat_mod, models

  cid = chat.id
  get_writer().submit(StartTurn(
    chat_id=cid, run_token="a-interrupted", initiated_by_app_id=app_id,
    user_msg={"role": "user", "content": "A", "cid": "a", "ts": 1},
  )).result(timeout=5)
  get_writer().submit(AppendPending(
    chat_id=cid, user_msg={"role": "user", "content": "B", "cid": "b", "ts": 2},
  )).result(timeout=5)
  get_writer().submit(FinishRun(
    chat_id=cid, run_token="a-interrupted", terminal_status="interrupted",
  )).result(timeout=5)
  calls = []

  async def capture(*args, **kwargs):
    calls.append(kwargs)

  monkeypatch.setattr("app.routes.chats_stream.run_chat", capture)
  request = {"content": "", "continuation": "manual", "cid": "manual-a", "resume_run_id": "a-interrupted"}
  first = client.post(f"/api/chats/{cid}/messages", headers=auth, json=request)
  assert first.status_code == 202, first.text
  second = client.post(f"/api/chats/{cid}/messages", headers=auth, json=request)
  assert second.status_code == 200, second.text
  assert second.json()["status"] == "duplicate"
  changed_target = client.post(
    f"/api/chats/{cid}/messages", headers=auth,
    json={**request, "resume_run_id": "different-interrupted-run"},
  )
  assert changed_target.status_code == 409, changed_target.text
  assert changed_target.json()["detail"]["code"] == "recovery_changed"
  assert len(calls) == 1
  # A late automatic sweep cannot reclaim the park after manual admission.
  import asyncio
  assert asyncio.run(chat_mod._auto_resume_chat(cid, park_token="a-interrupted")) is False
  with SessionLocal() as db:
    current = db.get(models.Chat, cid)
    assert [row["cid"] for row in current.pending_messages] == ["b"]
    assert all(row.get("cid") != "manual-a" for row in current.messages)
    resumed = db.get(models.ChatRun, calls[0]["run_token"])
    assert resumed.continuation_json == {
      "reason": "manual",
      "control_id": "manual-a",
      "supersedes_run_token": "a-interrupted",
    }
    assert resumed.root_run_id == "a-interrupted"
    assert resumed.initiated_by_app_id == app_id
  chat_mod.discard_starting(cid)


@pytest.mark.parametrize("busy", ["running", "draining", "admission_closed"])
def test_resume_never_becomes_a_queued_control_when_busy(client, auth, chat, monkeypatch, busy):
  from app import chat as chat_mod, models
  from app.database import SessionLocal

  monkeypatch.setattr(chat_mod, "draining", busy == "draining")
  if busy == "running":
    chat_mod.mark_starting(chat.id)
  if busy == "admission_closed":
    chat_mod.registry.close_admission()
  response = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "", "continuation": "manual", "cid": "busy-resume",
  })
  assert response.status_code == 409, response.text
  with SessionLocal() as db:
    assert db.get(models.Chat, chat.id).pending_messages == []
  chat_mod.discard_starting(chat.id)


def test_detail_and_runtime_name_the_exact_idle_recovery_attempt(client, auth, chat):
  from app.chat_writer import FinishRun, StartTurn, get_writer
  from app import chat as chat_mod

  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token="recover-this-attempt",
    user_msg={"role": "user", "content": "A", "cid": "a", "ts": 1},
  )).result(timeout=5)
  get_writer().submit(FinishRun(
    chat_id=chat.id, run_token="recover-this-attempt", terminal_status="interrupted",
  )).result(timeout=5)
  for suffix in ("", "/runtime"):
    response = client.get(f"/api/chats/{chat.id}{suffix}", headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["recovery_run_id"] == "recover-this-attempt"
  chat_mod.mark_starting(chat.id)
  for suffix in ("", "/runtime"):
    response = client.get(f"/api/chats/{chat.id}{suffix}", headers=auth)
    assert response.status_code == 200, response.text
    assert response.json()["recovery_run_id"] is None
  chat_mod.discard_starting(chat.id)


def test_legacy_resume_without_cid_gets_a_fresh_control_identity(
  client, auth, chat, monkeypatch,
):
  from app.chat_writer import FinishRun, StartTurn, get_writer
  from app import chat as chat_mod, models
  from app.database import SessionLocal
  from app.broadcast import get_broadcast

  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token="original-owner-run",
    user_msg={"role": "user", "content": "owner work", "cid": "owner", "ts": 1},
  )).result(timeout=5)
  calls = []

  async def capture(*args, **kwargs):
    calls.append(kwargs)

  monkeypatch.setattr("app.routes.chats_stream.run_chat", capture)
  prior = "original-owner-run"
  try:
    for _ in range(2):
      get_writer().submit(FinishRun(
        chat_id=chat.id, run_token=prior, terminal_status="interrupted",
      )).result(timeout=5)
      chat_mod.discard_starting(chat.id)
      broadcast = get_broadcast(chat.id)
      if broadcast is not None:
        broadcast.mark_completed()
      response = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
        "content": "continue", "continuation": "manual", "resume_run_id": prior,
      })
      assert response.status_code == 202, response.text
      assert "message" not in response.json()
      prior = calls[-1]["run_token"]
    assert len(calls) == 2
    assert calls[0]["run_token"] != calls[1]["run_token"]
    with SessionLocal() as db:
      assert [row["cid"] for row in db.get(models.Chat, chat.id).messages] == ["owner"]
  finally:
    chat_mod.discard_starting(chat.id)


@pytest.mark.parametrize("goal_status", ["open", "stopped"])
def test_provider_only_resume_preserves_durable_goal_without_transcript_control(
  client, chat, goal_status,
):
  from app import models
  from app.chat_writer import FinishRun, StartTurn, get_writer
  from app.database import SessionLocal
  from app.goals import resume_context

  writer = get_writer()
  writer.submit(StartTurn(
    chat_id=chat.id, run_token="goal-original",
    user_msg={"role": "user", "content": "/goal Verify recovery", "cid": "goal-owner", "ts": 1},
  )).result(timeout=5)
  writer.submit(FinishRun(
    chat_id=chat.id, run_token="goal-original", terminal_status="interrupted",
  )).result(timeout=5)
  with SessionLocal() as db:
    original = db.get(models.ChatRun, "goal-original")
    goal_id = original.goal_id
    goal = db.get(models.ChatGoal, goal_id)
    goal.status = goal_status
    before = list(db.get(models.Chat, chat.id).messages)
    db.commit()

  result = writer.submit(StartTurn(
    chat_id=chat.id, run_token="goal-resumed", resume_run_id="goal-original",
    user_msg={"role": "user", "content": "", "cid": "goal-resume-control",
              "kind": "continuation", "continuation_reason": "manual"},
  )).result(timeout=5)

  with SessionLocal() as db:
    run = db.get(models.ChatRun, "goal-resumed")
    assert run.goal_id == goal_id
    assert run.continuation_json["goal_id"] == goal_id
    assert db.get(models.ChatGoal, goal_id).status == "open"
    assert db.query(models.ChatGoal).filter_by(chat_id=chat.id).count() == 1
    assert db.get(models.Chat, chat.id).messages == before
    assert "Verify recovery" in resume_context(db, run.id)
  assert result["history"][-1].content
