"""A Restart now press on a request-list card's restart entry, through the
real route, with the body the interface sends. Each condition that refuses a
press gives the status asserted here, and every refused press is logged with
its status and reason.
"""
import logging

import pytest

from app import models
from app.chat_archive import archive_chat
from tests import owner_input_fixtures as inputs
from tests.owner_input_fixtures import (  # noqa: F401 (fixtures)
  QUESTION,
  boot,
  isolated_restart_source,
  run_auth,
)


@pytest.fixture
def dispatched(monkeypatch):
  calls = []

  async def restart_worker(*args, **kwargs):
    calls.append(True)

  monkeypatch.setattr("app.restart_util.restart_this_worker", restart_worker)
  return calls


async def _restart_card(client, chat, run_auth, monkeypatch, *, with_question=False):
  if with_question:
    inputs.edit(client, chat.id, run_auth, op="add", key="tone", **QUESTION)
  response = inputs.edit(client, chat.id, run_auth, op="add", key="restart", kind="restart")
  assert response.status_code == 200, response.text
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  return inputs.cards(chat.id)[-1]


def _press_body(card, **extra):
  """What the interface's pressRestartEntry sends: streamSend('Restart now',
  undefined, {hidden, question_id, selected_options, entryPress}) plus the
  timezone and viewport every send carries."""
  entry = inputs.entry(card, "restart")
  return {
    "content": "Restart now",
    "hidden": True,
    "question_id": card["question_id"],
    "selected_options": {entry["id"]: [entry["platform_action"]["restart_option_id"]]},
    "timezone": "Etc/UTC",
    "viewport": {"width": 390, "height": 844, "pixelRatio": 3},
    **extra,
  }


def _post(client, auth, chat_id, body):
  return client.post(f"/api/chats/{chat_id}/messages", headers=auth, json=body)


@pytest.mark.asyncio
async def test_the_exact_press_body_both_interfaces_send_is_accepted_and_dispatches_once(
  client, auth, chat, db, run_auth, monkeypatch, boot, dispatched,
):
  card = await _restart_card(client, chat, run_auth, monkeypatch)

  response = _post(client, auth, chat.id, _press_body(card))

  assert response.status_code == 202, response.text
  assert response.json()["state"]["status"] == "restart_requested"
  assert dispatched == [True]
  # The same press again (a second tap, a lost response) changes nothing.
  again = _post(client, auth, chat.id, _press_body(card))
  assert again.status_code == 202, again.text
  assert again.json()["state"]["status"] == "restart_requested"
  assert dispatched == [True]


@pytest.mark.asyncio
async def test_a_press_while_another_turn_runs_is_accepted(
  client, auth, chat, db, run_auth, monkeypatch, boot, dispatched,
):
  card = await _restart_card(client, chat, run_auth, monkeypatch)
  # A wake, helper result or owner message started a turn after the card.
  assert inputs.start_turn(chat.id, "next-turn")

  response = _post(client, auth, chat.id, _press_body(card))

  assert response.status_code == 202, response.text
  assert dispatched == [True]


@pytest.mark.asyncio
async def test_a_press_on_an_archived_chat_is_accepted_and_restores_it(
  client, auth, chat, db, run_auth, monkeypatch, boot, dispatched,
):
  card = await _restart_card(client, chat, run_auth, monkeypatch)
  archive_chat(db, db.get(models.Chat, chat.id))

  response = _post(client, auth, chat.id, _press_body(card))

  assert response.status_code == 202, response.text
  db.expire_all()
  assert db.get(models.Chat, chat.id).archived_at is None


@pytest.mark.asyncio
async def test_an_older_card_still_on_screen_is_refused_with_its_own_code_and_the_newer_one_accepted(
  client, auth, chat, db, run_auth, monkeypatch, boot, dispatched, caplog,
):
  older = await _restart_card(client, chat, run_auth, monkeypatch)
  assert inputs.start_turn(chat.id, "republish")
  await inputs.complete_turn(chat.id, monkeypatch, "republish")
  newer = inputs.cards(chat.id)[-1]
  assert inputs.entry(newer, "restart")["id"] == inputs.entry(older, "restart")["id"], (
    "both cards show one request")

  with caplog.at_level(logging.INFO, logger="app.routes.chats_stream"):
    stale = _post(client, auth, chat.id, _press_body(older))
  assert stale.status_code == 410, stale.text
  assert stale.json()["detail"]["code"] == "question_state_changed"
  assert "Restart entry press refused" in caplog.text and "status=410" in caplog.text
  assert dispatched == []

  current = _post(client, auth, chat.id, _press_body(newer))
  assert current.status_code == 202, current.text
  assert dispatched == [True]


@pytest.mark.asyncio
async def test_a_press_with_other_answers_or_files_is_refused_409_and_logged(
  client, auth, chat, db, run_auth, monkeypatch, boot, dispatched, caplog,
):
  card = await _restart_card(client, chat, run_auth, monkeypatch, with_question=True)
  entry = inputs.entry(card, "restart")

  # The entry's own label is allowed beside the press.
  with caplog.at_level(logging.INFO, logger="app.routes.chats_stream"):
    other = _post(client, auth, chat.id, _press_body(card, answers={QUESTION["prompt"]: "Casual"}))
    files = _post(client, auth, chat.id, _press_body(card, attachments=[{"name": "a.txt"}]))
  assert other.status_code == 409, other.text
  assert files.status_code == 409, files.text
  assert caplog.text.count("Restart entry press refused") == 2
  assert "status=409" in caplog.text
  assert dispatched == []
  assert inputs.request_row("restart").status == "open"

  own_label = _post(client, auth, chat.id, _press_body(card, answers={entry["question"]: "Restart now"}))
  assert own_label.status_code == 202, own_label.text


@pytest.mark.asyncio
async def test_a_card_selection_refused_before_the_press_is_recognised_is_logged(
  client, auth, chat, db, run_auth, monkeypatch, boot, dispatched, caplog,
):
  card = await _restart_card(client, chat, run_auth, monkeypatch)
  body = _press_body(card, question_id="card-that-does-not-exist")

  with caplog.at_level(logging.INFO, logger="app.routes.chats_stream"):
    response = _post(client, auth, chat.id, body)

  assert response.status_code == 410, response.text
  assert "Card selection refused" in caplog.text
  assert f"status={response.status_code}" in caplog.text
  assert dispatched == []
