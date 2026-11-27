"""Restart receipts remain visible independently of continuation admission."""
from app import transcript_rows

import asyncio
from copy import deepcopy

import pytest

from app import models, platform_update
from app.database import SessionLocal
from app.platform_restart import project_restart_observations
from app.routes.chats import _lifecycle_projection_key
from app.timeutil import now_naive_utc
from tests.test_platform_restart_cards import _install


def test_restart_receipt_is_delivered_while_late_update_edits_are_pending(
  client, auth, monkeypatch,
):
  from app import chat_waits

  chat_id = "observed-held"
  qid, wait_id, _run, _requirement = _install(chat_id)
  with SessionLocal() as db:
    db.add(models.PlatformBootSnapshot(
      boot_id="boot-new", source_kind="platform", source_sha="new-sha",
      loaded_files_json={}, service_ready=True, captured_at=now_naive_utc(),
    ))
    db.commit()
  monkeypatch.setattr(platform_update, "late_edits_pending", lambda: True)
  delivered = []
  async def capture_delivery(row_id):
    delivered.append(row_id)
    return True
  monkeypatch.setattr(chat_waits, "_deliver_resume", capture_delivery)
  changed = []
  monkeypatch.setattr(chat_waits, "_broadcast_changed", changed.append)

  assert asyncio.run(chat_waits.sweep_due_waits()) == 1
  assert delivered == [wait_id]
  assert changed == [chat_id]
  response = client.get(f"/api/chats/{chat_id}?compact=true", headers=auth)
  assert response.status_code == 200
  card = next(block for message in response.json()["messages"]
              for block in message.get("blocks", [])
              if block.get("question_id") == qid)
  observation = card["platform_action"]["observation"]
  assert observation["continuation"] == "pending"
  assert observation["observed_at"].endswith("Z")
  runtime = client.get(f"/api/chats/{chat_id}/runtime", headers=auth).json()
  assert runtime["lifecycle_projection_key"] == response.json()["lifecycle_projection_key"]
  # Observation grants neither restart approval nor agent admission, and the
  # already offered action remains intact for an explicit owner choice.
  assert card["platform_action"]["status"] == "awaiting_owner"
  assert "answers" not in card
  with SessionLocal() as db:
    wait = db.get(models.ChatWait, wait_id)
    chat = db.get(models.Chat, chat_id)
    assert wait.status == "met"
    assert wait.resume_delivered_at is None
    assert wait.action_approved_at is None
    assert chat.pending_question_id == qid
    assert "observation" not in list(transcript_rows.history(chat))[0]["blocks"][0]["platform_action"]


def test_observation_follows_delivery_without_transcript_writes(monkeypatch):
  chat_id = "observed-transitions"
  _qid, wait_id, _run, _requirement = _install(chat_id, status="met")
  monkeypatch.setattr(platform_update, "late_edits_pending", lambda: True)
  with SessionLocal() as db:
    chat = db.get(models.Chat, chat_id)
    original = deepcopy(list(transcript_rows.history(chat)))
    def observation():
      return project_restart_observations(db, chat_id, list(transcript_rows.history(chat)))[0][
        "blocks"][0]["platform_action"]["observation"]
    assert observation()["continuation"] == "pending"
    wait = db.get(models.ChatWait, wait_id)
    wait.resume_delivered_at = now_naive_utc()
    db.commit()
    assert observation()["continuation"] == "delivered"
    assert list(transcript_rows.history(chat)) == original


def test_written_response_preserves_observed_restart_without_claiming_auto_resume():
  chat_id = "observed-response"
  _qid, wait_id, _run, _requirement = _install(chat_id, status="met")
  with SessionLocal() as db:
    wait = db.get(models.ChatWait, wait_id)
    wait.status = "cancelled"
    db.commit()
    messages = deepcopy(list(transcript_rows.history(db.get(models.Chat, chat_id))))
    card = messages[0]["blocks"][0]
    card["platform_action"]["status"] = "responded"
    card["answers"] = {"Restart?": "Didn't we just restart?"}
    card["selected_options"] = {}
    projected = project_restart_observations(db, chat_id, messages)[0]["blocks"][0]
    assert projected["platform_action"]["observation"]["continuation"] == "cancelled"
    assert projected["answers"] == card["answers"]
    assert projected["selected_options"] == {}
    assert projected["platform_action"]["status"] == "responded"


@pytest.mark.parametrize("mismatch", ["chat", "question", "requirement", "unobserved"])
def test_receipts_cannot_leak_across_cards_or_invent_a_restart(mismatch):
  chat_id = "observed-identity"
  _qid, wait_id, _run, _requirement = _install(chat_id, status="met")
  with SessionLocal() as db:
    messages = deepcopy(list(transcript_rows.history(db.get(models.Chat, chat_id))))
    card = messages[0]["blocks"][0]
    if mismatch == "question":
      card["question_id"] = "unrelated-question"
    elif mismatch == "requirement":
      card["platform_action"]["requirement"] = {"version": 1}
    elif mismatch == "unobserved":
      wait = db.get(models.ChatWait, wait_id)
      wait.met_at = None
      wait.status = "cancelled"
      db.commit()
    result = project_restart_observations(
      db, "unrelated-chat" if mismatch == "chat" else chat_id, messages,
    )
    assert result is messages
    assert "observation" not in result[0]["blocks"][0]["platform_action"]


def test_projection_preserves_message_window_and_unrelated_blocks(monkeypatch):
  chat_id = "observed-page"
  _qid, _wait, _run, _requirement = _install(chat_id, status="met")
  monkeypatch.setattr(platform_update, "late_edits_pending", lambda: False)
  with SessionLocal() as db:
    messages = deepcopy(list(transcript_rows.history(db.get(models.Chat, chat_id))))
    unrelated = {"role": "user", "content": "Another topic"}
    messages.append(unrelated)
    text = {"type": "text", "content": "Preserve me"}
    messages[0]["blocks"].append(text)
    result = project_restart_observations(db, chat_id, messages)
    assert len(result) == 2
    assert result[1] is unrelated
    assert result[0]["blocks"][1] is text
    assert project_restart_observations(db, chat_id, [unrelated]) == [unrelated]


def test_receipt_freshness_tracks_observation_and_delivery_not_update_state(monkeypatch):
  chat_id = "observed-freshness"
  _qid, wait_id, _run, _requirement = _install(chat_id)
  held = [True]
  monkeypatch.setattr(platform_update, "late_edits_pending", lambda: held[0])
  with SessionLocal() as db:
    def key():
      return _lifecycle_projection_key(db, chat_id, 0, include_foreign_context=True)

    keys = [key()]
    wait = db.get(models.ChatWait, wait_id)
    wait.met_at = now_naive_utc()
    wait.status = "met"
    db.commit()
    keys.append(key())
    held[0] = False
    assert key() == keys[-1], "update state on disk is not a receipt input"
    wait.resume_delivered_at = now_naive_utc()
    db.commit()
    keys.append(key())
    assert len(set(keys)) == 3
