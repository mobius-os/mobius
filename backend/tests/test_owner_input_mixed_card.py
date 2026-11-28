"""One card holds every kind of owner request, each settled on its own.

Questions, approvals, restarts and secrets recorded with `owner_input` are
entries of the one request-list card shown at turn end. The card never holds
the chat (`pending_question_id` stays empty). What barring used to guarantee
is now per entry:

- an approval is granted only by an explicit option id of the shown revision,
  re-bound inside the writer; typed text is a message and leaves it open;
- Restart now is its own request to the restart resolver for that entry; the
  press answers the request in the commit that approves the restart, so a
  duplicate, a retry or a later card can never dispatch it again;
- a secret's values go only to the sealed endpoint and its one-use receipt,
  keyed by the request id, so no republished card can deliver them twice.
"""
import json
from datetime import timedelta

import pytest

from app import models, owner_input_requests as requests, transcript_rows
from app.chat_writer import (
  AppendPending,
  ClaimSecureInput,
  FinishRun,
  PromotePending,
  PromotePendingBlocked,
  ReplaceTranscript,
  ResolvePlatformRestartCard,
  SettleSecureInput,
  get_writer,
)
from app.database import SessionLocal
from app.questions import AnswerConflict
from app.timeutil import now_naive_utc
from tests import owner_input_fixtures as inputs
from tests.owner_input_fixtures import (  # noqa: F401 (fixtures)
  APPROVAL,
  QUESTION,
  SECRET_VALUE,
  boot,
  consumer,
  isolated_restart_source,
  no_wake,
  run_auth,
)


def _add_all(client, chat, run_auth, tmp_path):
  inputs.edit(client, chat.id, run_auth, op="add", key="mail-key", **inputs.secret_body(tmp_path))
  inputs.edit(client, chat.id, run_auth, op="add", key="changelog-tone", **QUESTION)
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)
  inputs.edit(client, chat.id, run_auth, op="add", key="restart", kind="restart")


async def _mixed_card(client, chat, run_auth, monkeypatch, tmp_path):
  _add_all(client, chat, run_auth, tmp_path)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  return inputs.cards(chat.id)[-1]


def _cancel_secret(client, auth, chat_id, card, entry, *, revision=None):
  """The secret entry's Cancel, naming the card and revision it was shown on."""
  return client.post(
    f"/api/secure-inputs/{chat_id}/{entry['id']}/cancel", headers=auth,
    json={"question_id": card["question_id"],
          "request_revision": entry["request_revision"] if revision is None else revision},
  )


def _post(client, auth, chat_id, card, answers=None, selected=None):
  body = {"hidden": True, "question_id": card["question_id"], "content": "Restart now"}
  if answers is not None:
    body["answers"] = answers
    body["content"] = "\n".join(f"- {q}: {a}" for q, a in answers.items())
  if selected is not None:
    body["selected_options"] = selected
  return client.post(f"/api/chats/{chat_id}/messages", headers=auth, json=body)


# ---- Composition ------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_card_holds_every_kind_oldest_first_in_the_published_shape(
  client, chat, db, run_auth, monkeypatch, tmp_path, boot,
):
  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)

  assert card["type"] == "question" and card["response_mode"] == "continuation"
  assert card["input_requests"] is True
  assert not {"action_key", "platform_action", "secure_input"} & set(card)
  secret, question, approval, restart = card["questions"]
  assert [e["kind"] for e in card["questions"]] == ["secret", "question", "approval", "restart"]
  for entry in card["questions"]:
    row = inputs.request_row(entry["request_key"])
    assert (entry["id"], entry["request_revision"]) == (row.id, row.revision)
    assert row.published_question_id == card["question_id"]
  assert secret["secure_input"] == {
    "title": "Connect the mail service",
    "description": "The key goes straight to the local connector.",
    "mode": "sealed",
    "fields": [{"name": "api_key", "label": "API key", "type": "password",
                "autocomplete": "off"}],
  }
  assert secret["options"] == [] and "consumer" not in json.dumps(card)
  assert question["question"] == QUESTION["prompt"] and question["options"] == []
  assert approval["action_key"] == APPROVAL["work_key"]
  assert [o["id"] for o in approval["options"]] == ["0", "1"]
  action = restart["platform_action"]
  assert set(action) == {"version", "type", "action_id", "restart_option_id",
                         "requirement", "status", "wait_id"}
  assert action["version"] == 2 and action["status"] == "awaiting_owner"
  assert action["requirement"]["source_boot_id"] == boot
  assert restart["options"] == [{
    "id": action["restart_option_id"], "label": "Restart now", "on_answer": "close",
    "description": "Finish active work, then restart once.",
  }]
  # Nothing on the card holds the chat.
  assert inputs.markers(chat.id) == (None, card["question_id"])
  with SessionLocal() as session:
    assert session.get(models.SavedSecureInput, secret["id"]).status == "pending"
    assert session.get(models.ChatWait, action["wait_id"]).linked_question_id == card["question_id"]


# ---- Approvals in the partial-answer path -----------------------------------

@pytest.mark.asyncio
async def test_one_submission_answers_a_question_and_an_approval_and_leaves_the_rest(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, boot, no_wake,
):
  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)
  approval = inputs.entry(card, "publish-notes")

  response = _post(client, auth, chat.id, card, answers={
    QUESTION["prompt"]: "Casual", APPROVAL["prompt"]: "Publish",
  }, selected={approval["id"]: ["0"]})

  assert response.status_code == 202, response.text
  assert inputs.request_row("changelog-tone").answer_json == {"text": "Casual"}
  answered = inputs.request_row("publish-notes")
  assert answered.status == "answered" and answered.answered_by == "card"
  assert answered.answer_json == {"text": "Publish", "selected": ["0"]}
  # The secret and the restart act on their own, so they stay usable on this
  # card, which stays open for them; nothing else is left on it.
  for key in ("mail-key", "restart"):
    row = inputs.request_row(key)
    assert row.status == "open" and row.published_question_id == card["question_id"]
  assert inputs.markers(chat.id) == (None, card["question_id"])
  with SessionLocal() as session:
    secret = session.get(models.SavedSecureInput, inputs.entry(card, "mail-key")["id"])
    assert secret.status == "pending"
  restart_wait = inputs.entry(card, "restart")["platform_action"]["wait_id"]
  assert inputs.wait_row(restart_wait).status == "armed"


@pytest.mark.asyncio
async def test_after_a_partial_answer_the_secret_and_restart_still_work_on_the_same_card(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, boot, consumer, no_wake,
):
  dispatched = []

  async def restart_worker(*args, **kwargs):
    dispatched.append(True)

  monkeypatch.setattr("app.restart_util.restart_this_worker", restart_worker)
  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)
  secret, restart = inputs.entry(card, "mail-key"), inputs.entry(card, "restart")
  assert _post(client, auth, chat.id, card,
               answers={QUESTION["prompt"]: "Casual"}).status_code == 202

  submitted = inputs.submit_secret(client, auth, chat.id, secret["id"])
  assert submitted.status_code == 200 and submitted.json() == {"status": "consuming"}, submitted.text
  assert inputs.wait_for_secret(secret["id"]) == "completed"
  assert consumer["seen"] == [{"api_key": SECRET_VALUE}]
  press = _post(client, auth, chat.id, card,
                selected={restart["id"]: [restart["platform_action"]["restart_option_id"]]})
  assert press.status_code == 202, press.text
  assert press.json()["state"]["status"] == "restart_requested"
  assert dispatched == [True]
  assert inputs.wait_row(restart["platform_action"]["wait_id"]).action_approved_at is not None
  assert {inputs.request_row(k).status for k in ("mail-key", "restart", "changelog-tone")} == {"answered"}
  # The approval was never answered: it waits for the next card.
  approval = inputs.request_row("publish-notes")
  assert approval.status == "open" and approval.published_question_id is None


@pytest.mark.asyncio
async def test_the_next_turn_end_moves_what_a_partial_answer_left_to_a_new_card(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, boot, no_wake,
):
  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)
  assert _post(client, auth, chat.id, card,
               answers={QUESTION["prompt"]: "Casual"}).status_code == 202

  inputs.start_turn(chat.id, "continuation")
  await inputs.complete_turn(chat.id, monkeypatch, "continuation")

  newer = inputs.cards(chat.id)[-1]
  assert newer["question_id"] != card["question_id"]
  assert {q["request_key"] for q in newer["questions"]} == {"mail-key", "publish-notes", "restart"}
  assert inputs.markers(chat.id) == (None, newer["question_id"])
  old_wait = inputs.entry(card, "restart")["platform_action"]["wait_id"]
  assert inputs.wait_row(old_wait).status == "cancelled"
  from app.chat_writer import RestartCardStateChanged
  with pytest.raises(RestartCardStateChanged):
    inputs.press_restart(chat.id, card)


@pytest.mark.asyncio
async def test_a_partly_answered_card_takes_no_second_set_of_answers(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, boot, no_wake,
):
  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)
  approval = inputs.entry(card, "publish-notes")
  assert _post(client, auth, chat.id, card,
               answers={QUESTION["prompt"]: "Casual"}).status_code == 202

  again = _post(client, auth, chat.id, card, answers={APPROVAL["prompt"]: "Publish"},
                selected={approval["id"]: ["0"]})

  assert again.status_code == 410, again.text
  assert inputs.request_row("publish-notes").status == "open"
  assert inputs.cards(chat.id)[-1]["answers"] == {QUESTION["prompt"]: "Casual"}


@pytest.mark.asyncio
async def test_a_partial_answer_closes_the_card_once_nothing_acting_on_its_own_is_left(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, boot, consumer, no_wake,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="mail-key", **inputs.secret_body(tmp_path))
  inputs.edit(client, chat.id, run_auth, op="add", key="changelog-tone", **QUESTION)
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)
  inputs.edit(client, chat.id, run_auth, op="add", key="restart", kind="restart")
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[-1]
  secret, restart = inputs.entry(card, "mail-key"), inputs.entry(card, "restart")
  assert _cancel_secret(client, auth, chat.id, card, secret).status_code == 200

  # Written feedback settles the restart without restarting, so nothing that
  # acts on its own is left and the card closes.
  assert _post(client, auth, chat.id, card, answers={
    QUESTION["prompt"]: "Casual", restart["question"]: "Not yet",
  }).status_code == 202

  assert inputs.markers(chat.id) == (None, None)
  assert inputs.request_row("restart").status == "answered"
  assert inputs.wait_row(restart["platform_action"]["wait_id"]).status == "cancelled"
  approval = inputs.request_row("publish-notes")
  assert approval.status == "open" and approval.published_question_id is None


@pytest.mark.asyncio
async def test_typed_text_on_an_approval_is_a_message_never_a_grant(
  client, auth, chat, db, run_auth, monkeypatch, no_wake,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="changelog-tone", **QUESTION)
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[-1]

  # Even text equal to an option label carries no option authority.
  response = _post(client, auth, chat.id, card, answers={
    QUESTION["prompt"]: "Casual", APPROVAL["prompt"]: "Publish",
  })

  assert response.status_code == 202, response.text
  assert inputs.request_row("changelog-tone").status == "answered"
  approval = inputs.request_row("publish-notes")
  assert approval.status == "open" and approval.answer_json is None
  with SessionLocal() as session:
    chat_row = session.get(models.Chat, chat.id)
    continuation = list(chat_row.pending_messages or []) or [
      m for m in transcript_rows.history(chat_row) if m.get("hidden")]
    # The agent still hears the reply, as a message.
    assert any(APPROVAL["prompt"] in (m.get("content") or "") for m in continuation)


@pytest.mark.asyncio
async def test_an_approval_selection_must_name_the_shown_option(
  client, auth, chat, db, run_auth, monkeypatch, no_wake,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[-1]
  entry = inputs.entry(card, "publish-notes")

  mismatched = _post(client, auth, chat.id, card, answers={APPROVAL["prompt"]: "Publish"},
                     selected={entry["id"]: ["1"]})
  unknown = _post(client, auth, chat.id, card, answers={APPROVAL["prompt"]: "Publish"},
                  selected={entry["id"]: ["7"]})

  assert mismatched.status_code == 409 and unknown.status_code == 409
  assert inputs.request_row("publish-notes").status == "open"
  assert inputs.markers(chat.id) == (None, card["question_id"])


@pytest.mark.asyncio
async def test_the_writer_rebinds_selections_whatever_a_caller_sends(
  client, chat, db, run_auth, monkeypatch, tmp_path, boot,
):
  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)
  approval, restart, secret = (inputs.entry(card, k) for k in ("publish-notes", "restart", "mail-key"))

  def append(answers, selected=None):
    return get_writer().submit(AppendPending(
      chat_id=chat.id, question_id=card["question_id"], answers=answers,
      selected_options=selected, require_answer_match=True, front=True,
      user_msg={"role": "user", "hidden": True, "content": "x"},
    )).result(timeout=5)

  # A forged grant, Restart now and a plaintext secret are all refused below
  # the route, and nothing on the card is settled.
  with pytest.raises(AnswerConflict):
    append({APPROVAL["prompt"]: "Publish"}, {approval["id"]: ["1"]})
  with pytest.raises(AnswerConflict):
    append({restart["question"]: "Restart now"},
           {restart["id"]: [restart["platform_action"]["restart_option_id"]]})
  with pytest.raises(AnswerConflict):
    append({secret["question"]: SECRET_VALUE})
  assert {inputs.request_row(k).status for k in ("publish-notes", "restart", "mail-key")} == {"open"}
  assert inputs.markers(chat.id) == (None, card["question_id"])
  assert inputs.wait_row(restart["platform_action"]["wait_id"]).action_approved_at is None


@pytest.mark.asyncio
async def test_an_answer_to_a_revision_the_agent_replaced_is_refused_whole(
  client, auth, chat, db, run_auth, monkeypatch, no_wake,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="changelog-tone", **QUESTION)
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[-1]
  entry = inputs.entry(card, "publish-notes")
  with SessionLocal() as session:
    requests.update_request(session, chat_id=chat.id, ref="publish-notes", spec={
      **requests.editable_spec(inputs.request_row("publish-notes").spec_json),
      "prompt": "Publish the 2.0 release notes now?",
    })

  response = _post(client, auth, chat.id, card, answers={
    QUESTION["prompt"]: "Casual", APPROVAL["prompt"]: "Publish",
  }, selected={entry["id"]: ["0"]})

  assert response.status_code == 410, response.text
  detail = response.json()["detail"]
  assert detail["code"] == "card_out_of_date"
  assert "changed while you were answering" in detail["message"]
  assert inputs.request_row("publish-notes").status == "open"
  assert inputs.request_row("changelog-tone").status == "open"


# ---- Restart entries --------------------------------------------------------

@pytest.mark.asyncio
async def test_pressing_restart_now_twice_through_the_route_dispatches_once(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, boot,
):
  dispatched = []

  async def restart_worker(*args, **kwargs):
    dispatched.append(True)

  monkeypatch.setattr("app.restart_util.restart_this_worker", restart_worker)
  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)
  entry = inputs.entry(card, "restart")
  press = {entry["id"]: [entry["platform_action"]["restart_option_id"]]}

  first = _post(client, auth, chat.id, card, selected=press)
  second = _post(client, auth, chat.id, card, selected=press)

  assert first.status_code == 202, first.text
  assert second.status_code == 202, second.text
  assert first.json()["request_id"] == entry["id"]
  assert first.json()["state"]["status"] == "restart_requested"
  assert dispatched == [True]
  assert inputs.request_row("restart").status == "answered"
  # Only the restart was settled; the card stays open for everything else.
  assert inputs.markers(chat.id) == (None, card["question_id"])
  assert {inputs.request_row(k).status
          for k in ("mail-key", "changelog-tone", "publish-notes")} == {"open"}
  stored = inputs.cards(chat.id)[-1]["request_states"][entry["id"]]
  assert stored["platform_action"]["status"] == "restart_requested"


@pytest.mark.asyncio
async def test_restart_now_never_rides_along_with_other_answers(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, boot, no_wake,
):
  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)
  restart, approval = inputs.entry(card, "restart"), inputs.entry(card, "publish-notes")

  response = _post(client, auth, chat.id, card, answers={
    APPROVAL["prompt"]: "Publish", restart["question"]: "Restart now",
  }, selected={approval["id"]: ["0"],
               restart["id"]: [restart["platform_action"]["restart_option_id"]]})

  assert response.status_code == 409, response.text
  assert {inputs.request_row(k).status for k in ("restart", "publish-notes")} == {"open"}
  assert inputs.wait_row(restart["platform_action"]["wait_id"]).action_approved_at is None


@pytest.mark.asyncio
async def test_a_press_whose_commit_never_landed_can_be_pressed_again_once(
  client, chat, db, run_auth, monkeypatch, tmp_path, boot,
):
  from app import chat_writer

  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)
  real = chat_writer._commit_or_rollback

  def lost(session):
    session.rollback()
    return False

  # The process dies (or the commit drops) before the press is durable.
  monkeypatch.setattr(chat_writer, "_commit_or_rollback", lost)
  with pytest.raises(chat_writer._PersistFailed, match="did not persist"):
    inputs.press_restart(chat.id, card)
  monkeypatch.setattr(chat_writer, "_commit_or_rollback", real)
  assert inputs.request_row("restart").status == "open"
  restart_wait = inputs.entry(card, "restart")["platform_action"]["wait_id"]
  assert inputs.wait_row(restart_wait).action_approved_at is None

  assert inputs.press_restart(chat.id, card)["dispatch"] is True
  assert inputs.press_restart(chat.id, card)["dispatch"] is False


@pytest.mark.asyncio
async def test_after_a_press_later_input_waits_for_the_ready_boot_which_resumes_the_agent(
  client, chat, db, run_auth, monkeypatch, tmp_path, boot,
):
  from app.chat_writer import StartContinuation
  from app.platform_restart import activation_notice, activation_wait_verdict

  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)
  entry = inputs.entry(card, "restart")
  wait_id = entry["platform_action"]["wait_id"]
  queue = [{"role": "user", "content": "B", "cid": "later-b", "ts": 50}]
  with SessionLocal() as session:
    session.get(models.Chat, chat.id).pending_messages = queue
    session.commit()
  # Unpressed, the restart holds nothing.
  assert inputs.start_turn(chat.id, "unpressed-turn") is True
  get_writer().submit(FinishRun(chat_id=chat.id, run_token="unpressed-turn",
                                terminal_status="completed")).result(timeout=5)

  assert inputs.press_restart(chat.id, card)["dispatch"] is True
  # The process dies after the durable press: a retry never dispatches again,
  # and later input stays queued behind the approved restart.
  assert inputs.press_restart(chat.id, card)["dispatch"] is False
  blocked = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="b-turn")).result(timeout=5)
  assert isinstance(blocked, PromotePendingBlocked) and blocked.reason == "activation"
  with SessionLocal() as session:
    wait = session.get(models.ChatWait, wait_id)
    assert activation_wait_verdict(session, wait)[0] == "pending"
    session.add(models.PlatformBootSnapshot(
      boot_id="boot-after", source_kind="test", loaded_files_json={},
      service_ready=True, captured_at=now_naive_utc() + timedelta(seconds=1),
    ))
    session.commit()
    assert activation_wait_verdict(session, wait)[0] == "met"
    wait.status, wait.met_at = "met", now_naive_utc()
    session.commit()
    root_id, source_id = wait.root_run_id, wait.created_by_run_id
    content = activation_notice(wait, "met")

  resumed = get_writer().submit(StartContinuation(
    chat_id=chat.id, run_token=f"activation-resume-{wait_id}",
    root_run_id=root_id, content=content, cid=f"activation-result-{wait_id}",
    reason="wait_result", message_kind="platform_activation_result",
    source_work_id=source_id, hidden=True, activation_wait_id=wait_id,
  )).result(timeout=5)

  assert isinstance(resumed, dict)
  with SessionLocal() as session:
    chat_row = session.get(models.Chat, chat.id)
    assert [m["content"] for m in chat_row.pending_messages] == ["B"]
  # The press keeps its answer; the entry records the activation.
  assert inputs.request_row("restart").answer_json["text"] == "Restart now"
  state = inputs.cards(chat.id)[-1]["request_states"][entry["id"]]
  assert state["status"] == "activated" and state["selected"] == [
    entry["platform_action"]["restart_option_id"]]


@pytest.mark.asyncio
async def test_a_pressed_restart_is_shown_settled_and_never_offered_again(
  client, chat, db, run_auth, monkeypatch, tmp_path, boot,
):
  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)
  inputs.press_restart(chat.id, card)

  # A turn ends before the boot: the rest is republished, the restart is not.
  inputs.start_turn(chat.id, "before-boot")
  await inputs.complete_turn(chat.id, monkeypatch, "before-boot")
  newer = inputs.cards(chat.id)[-1]

  assert newer["question_id"] != card["question_id"]
  assert [q["request_key"] for q in newer["questions"]] == [
    "mail-key", "changelog-tone", "publish-notes"]
  # The pressed card keeps its in-flight state; its press cannot repeat.
  old = next(c for c in inputs.cards(chat.id) if c["question_id"] == card["question_id"])
  assert old["request_states"][inputs.entry(card, "restart")["id"]]["status"] == "restart_requested"
  assert inputs.press_restart(chat.id, card)["dispatch"] is False
  # The approved wait survives the supersession: it is the barrier and resume.
  assert inputs.wait_row(inputs.entry(card, "restart")["platform_action"]["wait_id"]).status == "armed"


@pytest.mark.asyncio
async def test_a_superseded_cards_restart_cannot_be_pressed_and_one_wait_stands(
  client, chat, db, run_auth, monkeypatch, tmp_path, boot,
):
  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)
  inputs.start_turn(chat.id, "republish")
  await inputs.complete_turn(chat.id, monkeypatch, "republish")
  newer = inputs.cards(chat.id)[-1]

  from app.chat_writer import RestartCardStateChanged
  with pytest.raises(RestartCardStateChanged):
    inputs.press_restart(chat.id, card)
  assert inputs.request_row("restart").status == "open"
  old_wait = inputs.wait_row(inputs.entry(card, "restart")["platform_action"]["wait_id"])
  new_wait = inputs.wait_row(inputs.entry(newer, "restart")["platform_action"]["wait_id"])
  assert old_wait.status == "cancelled" and new_wait.status == "armed"
  assert inputs.press_restart(chat.id, newer)["dispatch"] is True


# ---- Secret entries ---------------------------------------------------------

@pytest.mark.asyncio
async def test_a_secret_value_reaches_only_its_consumer_not_broadcasts_or_results(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, consumer, caplog,
):
  from app.broadcast import create_broadcast, remove_broadcast

  monkeypatch.setattr("app.routes.chats_stream._schedule_continuation", lambda **kw: None)
  inputs.edit(client, chat.id, run_auth, op="add", key="mail-key", **inputs.secret_body(tmp_path))
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  entry = inputs.entry(inputs.cards(chat.id)[-1], "mail-key")
  broadcast = create_broadcast(chat.id)
  events = []
  original = broadcast.publish
  monkeypatch.setattr(broadcast, "publish", lambda event: (events.append(event), original(event))[1])
  try:
    response = inputs.submit_secret(client, auth, chat.id, entry["id"])
    assert response.status_code == 200 and response.json() == {"status": "consuming"}, response.text
    assert inputs.wait_for_secret(entry["id"]) == "completed"
    again = inputs.submit_secret(client, auth, chat.id, entry["id"])
    assert again.status_code == 200 and again.json() == {"status": "completed"}, again.text
  finally:
    remove_broadcast(chat.id)

  assert consumer["seen"] == [{"api_key": SECRET_VALUE}]
  assert {e["type"] for e in events} >= {"input_request_settled"}
  listed = inputs.edit(client, chat.id, inputs.start_run(db, chat.id, "reader-turn"), op="list")
  with SessionLocal() as session:
    chat_row = session.get(models.Chat, chat.id)
    everything = json.dumps([
      events, listed.json(), list(transcript_rows.history(chat_row)),
      chat_row.pending_messages,
      [(r.spec_json, r.answer_json, r.outcome_json)
       for r in session.query(models.OwnerInputRequest)],
      [(r.command_json, r.outcome) for r in session.query(models.SavedSecureInput)],
    ], default=str)
  assert SECRET_VALUE not in everything
  assert SECRET_VALUE not in caplog.text


@pytest.mark.asyncio
async def test_a_failing_consumer_settles_with_its_fixed_safe_outcome(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, consumer,
):
  from app.saved_secure_inputs import SAFE_OUTCOMES

  monkeypatch.setattr("app.routes.chats_stream._schedule_continuation", lambda **kw: None)
  consumer["code"] = 3
  inputs.edit(client, chat.id, run_auth, op="add", key="mail-key", **inputs.secret_body(tmp_path))
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[-1]
  entry = inputs.entry(card, "mail-key")

  response = inputs.submit_secret(client, auth, chat.id, entry["id"])
  assert response.status_code == 200 and response.json() == {"status": "consuming"}, response.text
  assert inputs.wait_for_secret(entry["id"]) == "failed"
  again = inputs.submit_secret(client, auth, chat.id, entry["id"])
  assert again.status_code == 200 and again.json() == {"status": "failed"}, again.text

  assert consumer["seen"] == [{"api_key": SECRET_VALUE}]
  row = inputs.request_row("mail-key")
  assert row.answer_json == {"text": SAFE_OUTCOMES["failed"], "status": "failed"}
  with SessionLocal() as session:
    chat_row = session.get(models.Chat, chat.id)
    queued = [m for m in chat_row.pending_messages or []
              if m.get("cid") == f"secure-input:{entry['id']}"]
    started = [m for m in transcript_rows.history(chat_row)
               if m.get("cid") == f"secure-input:{entry['id']}"]
    assert len(queued + started) == 1
    assert (queued + started)[0]["content"] == f"- Secure input (mail-key): {SAFE_OUTCOMES['failed']}"


@pytest.mark.asyncio
async def test_cancelling_a_secret_entry_settles_it_and_leaves_the_card_open(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, boot, consumer,
):
  from app.saved_secure_inputs import SAFE_OUTCOMES

  monkeypatch.setattr("app.routes.chats_stream._schedule_continuation", lambda **kw: None)
  card = await _mixed_card(client, chat, run_auth, monkeypatch, tmp_path)
  entry = inputs.entry(card, "mail-key")

  cancelled = _cancel_secret(client, auth, chat.id, card, entry)
  again = inputs.submit_secret(client, auth, chat.id, entry["id"])

  assert cancelled.status_code == 200 and cancelled.json() == {"status": "cancelled"}
  assert again.status_code == 200 and again.json() == {"status": "cancelled"}
  assert consumer["seen"] == []
  assert inputs.request_row("mail-key").answer_json == {
    "text": SAFE_OUTCOMES["cancelled"], "status": "cancelled"}
  assert inputs.markers(chat.id) == (None, card["question_id"])
  assert {inputs.request_row(k).status
          for k in ("changelog-tone", "publish-notes", "restart")} == {"open"}


@pytest.mark.asyncio
async def test_a_stale_tabs_cancel_of_a_reworded_secret_cancels_nothing(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, consumer, no_wake,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="mail-key", **inputs.secret_body(tmp_path))
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  old_card = inputs.cards(chat.id)[-1]
  old_entry = inputs.entry(old_card, "mail-key")
  reword = inputs.start_run(db, chat.id, "reword")
  assert inputs.edit(client, chat.id, reword, op="update", key="mail-key",
                     title="Connect the mail relay").status_code == 200
  await inputs.complete_turn(chat.id, monkeypatch, "reword")
  card = inputs.cards(chat.id)[-1]
  entry = inputs.entry(card, "mail-key")
  assert entry["id"] == old_entry["id"] and entry["request_revision"] == 2

  # A tab still showing the older card, or the older revision, cancels nothing.
  assert _cancel_secret(client, auth, chat.id, old_card, old_entry).status_code == 410
  assert _cancel_secret(client, auth, chat.id, card, entry, revision=1).status_code == 410
  # The writer refuses it too, whatever reaches it.
  assert get_writer().submit(SettleSecureInput(
    chat_id=chat.id, request_id=entry["id"], status="cancelled", outcome="cancelled",
    question_id=card["question_id"], revision=1,
  )).result(timeout=5) == {"status": "closed"}
  assert inputs.request_row("mail-key").status == "open"
  with SessionLocal() as session:
    assert session.get(models.SavedSecureInput, entry["id"]).status == "pending"

  current = _cancel_secret(client, auth, chat.id, card, entry)
  assert current.status_code == 200 and current.json() == {"status": "cancelled"}
  assert inputs.request_row("mail-key").status == "answered"


@pytest.mark.asyncio
async def test_a_secret_is_never_answered_in_plain_text(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, no_wake,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="mail-key", **inputs.secret_body(tmp_path))
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[-1]
  entry = inputs.entry(card, "mail-key")

  response = _post(client, auth, chat.id, card, answers={entry["question"]: SECRET_VALUE})

  assert response.status_code == 409, response.text
  assert SECRET_VALUE not in response.text
  assert inputs.request_row("mail-key").status == "open"
  with SessionLocal() as session:
    chat_row = session.get(models.Chat, chat.id)
    assert SECRET_VALUE not in json.dumps([
      list(transcript_rows.history(chat_row)), chat_row.pending_messages], default=str)


@pytest.mark.asyncio
async def test_a_consumer_running_across_a_republish_is_shown_in_progress_and_runs_once(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, consumer,
):
  from app.saved_secure_inputs import SAFE_OUTCOMES

  inputs.edit(client, chat.id, run_auth, op="add", key="mail-key", **inputs.secret_body(tmp_path))
  inputs.edit(client, chat.id, run_auth, op="add", key="changelog-tone", **QUESTION)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  entry = inputs.entry(inputs.cards(chat.id)[-1], "mail-key")
  claim = get_writer().submit(ClaimSecureInput(
    chat_id=chat.id, request_id=entry["id"], revision=entry["request_revision"],
  )).result(timeout=5)
  assert claim["status"] == "claimed"

  # Another turn ends while the consumer runs: the agent cannot withdraw it,
  # and the new card shows it in progress rather than offering it again.
  turn_auth = inputs.start_run(db, chat.id, "while-consuming")
  refused = inputs.edit(client, chat.id, turn_auth, op="withdraw", key="mail-key", reason="Moot.")
  assert refused.status_code == 422 and "being consumed" in refused.text
  await inputs.complete_turn(chat.id, monkeypatch, "while-consuming")
  newer = inputs.cards(chat.id)[-1]
  assert newer["request_states"][entry["id"]] == {"status": "consuming"}
  resubmitted = inputs.submit_secret(client, auth, chat.id, entry["id"])
  assert resubmitted.json() == {"status": "consuming"}
  assert consumer["seen"] == []

  settled = get_writer().submit(SettleSecureInput(
    chat_id=chat.id, request_id=entry["id"], status="completed", outcome="success",
  )).result(timeout=5)

  assert settled["queued"] is True and settled["question_id"] == newer["question_id"]
  assert inputs.request_row("mail-key").answer_json == {
    "text": SAFE_OUTCOMES["success"], "status": "completed"}
  assert inputs.cards(chat.id)[-1]["request_states"][entry["id"]]["status"] == "completed"
  # The question beside it is untouched and still answerable.
  assert inputs.request_row("changelog-tone").status == "open"
  assert inputs.markers(chat.id) == (None, newer["question_id"])


# ---- Nothing is held --------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["approval", "restart", "secret"])
async def test_owner_turns_and_platform_wakes_start_while_each_kind_is_open(
  client, chat, db, run_auth, monkeypatch, tmp_path, boot, kind,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="needs-owner",
              **inputs.request_body(kind, tmp_path))
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[-1]
  assert card["questions"][0]["kind"] == kind

  assert inputs.start_turn(chat.id, "owner-turn") is True
  await inputs.complete_turn(chat.id, monkeypatch, "owner-turn")
  with SessionLocal() as session:
    session.get(models.Chat, chat.id).pending_messages = [{
      "role": "user", "hidden": True, "kind": "continuation",
      "continuation_reason": "wait_result", "cid": "wake",
      "content": "The timer fired.", "ts": 90}]
    session.commit()
  promoted = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="wake-turn")).result(timeout=5)

  assert not isinstance(promoted, PromotePendingBlocked)
  assert inputs.markers(chat.id)[0] is None
  assert inputs.request_row("needs-owner").status == "open"


# ---- Legacy cards -----------------------------------------------------------

@pytest.mark.parametrize("kind", ["approval", "restart", "secret"])
def test_a_barring_card_saved_by_an_earlier_release_still_bars_and_settles(
  client, auth, chat, db, monkeypatch, tmp_path, boot, no_wake, kind,
):
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink

  headers = inputs.start_run(db, chat.id, "legacy-run")
  # A request recorded on the list meanwhile is not shown over the barring card.
  assert inputs.add(client, chat.id, headers, "later", **QUESTION).status_code == 200
  sink = ChatEventSink(create_broadcast(chat.id), chat.id, run_token="legacy-run")
  try:
    if kind == "approval":
      qid = inputs.save_legacy_approval(sink, {
        "question": APPROVAL["prompt"], "options": APPROVAL["options"],
        "work_key": APPROVAL["work_key"]})
    elif kind == "restart":
      qid = inputs.save_legacy_restart(sink)
    else:
      qid = inputs.save_legacy_secret(sink, inputs.secret_body(tmp_path))
    assert inputs.markers(chat.id) == (qid, None)
    inputs.publish_now(sink)
    assert inputs.last_card(chat.id)["question_id"] == qid
  finally:
    remove_broadcast(chat.id)
  get_writer().submit(FinishRun(chat_id=chat.id, run_token="legacy-run",
                                terminal_status="completed")).result(timeout=5)
  assert inputs.start_turn(chat.id, "blocked") is False

  if kind == "approval":
    response = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
      "content": "- Not now", "hidden": True, "question_id": qid,
      "answers": {APPROVAL["prompt"]: "Not now"}, "selected_options": {"approval": ["1"]}})
    assert response.status_code == 202, response.text
    assert inputs.last_card(chat.id)["answers"] == {APPROVAL["prompt"]: "Not now"}
  elif kind == "restart":
    option = inputs.last_card(chat.id)["platform_action"]["restart_option_id"]
    result = get_writer().submit(ResolvePlatformRestartCard(
      chat_id=chat.id, question_id=qid, selected_option_id=option)).result(timeout=5)
    assert result["dispatch"] is True
  else:
    response = client.post(f"/api/secure-inputs/{chat.id}/{qid}/cancel", headers=auth)
    assert response.status_code == 200, response.text
    with SessionLocal() as session:
      assert session.get(models.SavedSecureInput, qid).status == "cancelled"
  assert inputs.markers(chat.id)[0] is None
  # Settling the card leaves the list alone: the later request is still asked.
  row = inputs.request_row("later")
  assert row.status == "open" and row.published_question_id is None


def test_an_answered_card_carrying_an_old_request_field_is_served_and_settles_nothing(
  client, auth, chat, db, run_auth,
):
  """Old transcripts may hold a settled card naming one request by
  `input_request_id`. It is read and listed like any other answered card and
  never touches the request rows."""
  assert inputs.add(client, chat.id, run_auth, "open-one", **QUESTION).status_code == 200
  request_id = inputs.request_row("open-one").id
  with SessionLocal() as session:
    row = session.get(models.Chat, chat.id)
    transcript_rows.append(session, row, {
      "role": "assistant", "ts": 5, "blocks": [{
        "type": "question", "question_id": "old-card", "response_mode": "continuation",
        "input_request_id": request_id, "input_request_revision": 1,
        "questions": [{"id": "approval", "header": "Approval", "question": "Publish?",
                       "options": [{"id": "0", "label": "Publish"}]}],
        "answers": {"Publish?": "Publish"}, "selected_options": {"approval": ["0"]},
      }]})
    session.commit()

  detail = client.get(f"/api/chats/{chat.id}", headers=auth)
  assert detail.status_code == 200, detail.text
  assert any(block.get("question_id") == "old-card"
             for message in detail.json()["messages"] for block in message.get("blocks") or [])
  listed = client.get("/api/chats", headers=auth)
  assert listed.status_code == 200, listed.text
  assert inputs.markers(chat.id) == (None, None)
  # A repeated answer is refused and the request it names stays as it was.
  response = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": "- Publish", "hidden": True, "question_id": "old-card",
    "answers": {"Publish?": "Publish"}, "selected_options": {"approval": ["0"]}})
  assert response.status_code == 410, response.text
  row = inputs.request_row("open-one")
  assert (row.status, row.revision, row.published_question_id) == ("open", 1, None)


# ---- Lifecycle with each kind open ------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["approval", "restart", "secret"])
async def test_replacing_the_transcript_returns_each_kind_to_the_list(
  client, chat, db, run_auth, monkeypatch, tmp_path, boot, kind,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="needs-owner",
              **inputs.request_body(kind, tmp_path))
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  entry = inputs.cards(chat.id)[-1]["questions"][0]

  get_writer().submit(ReplaceTranscript(chat_id=chat.id, messages=[
    {"role": "user", "content": "Ship the release", "ts": 1},
  ])).result(timeout=5)

  assert inputs.markers(chat.id) == (None, None)
  row = inputs.request_row("needs-owner")
  assert row.status == "open" and row.published_question_id is None
  with SessionLocal() as session:
    if kind == "restart":
      assert session.get(models.ChatWait, entry["platform_action"]["wait_id"]).status == "cancelled"
    if kind == "secret":
      assert session.get(models.SavedSecureInput, entry["id"]).status == "pending"


@pytest.mark.asyncio
async def test_recovering_a_later_wedged_run_keeps_the_card_open_and_answerable(
  client, auth, chat, db, run_auth, monkeypatch, no_wake,
):
  from app.chat_writer import RecoverWedgedRun

  inputs.edit(client, chat.id, run_auth, op="add", key="changelog-tone", **QUESTION)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[-1]
  assert inputs.start_turn(chat.id, "wedged")

  get_writer().submit(RecoverWedgedRun(
    chat_id=chat.id, run_token="wedged",
    interruption_block={"type": "error", "message": "This response could not be saved."},
  )).result(timeout=5)

  assert inputs.markers(chat.id) == (None, card["question_id"])
  assert inputs.request_row("changelog-tone").published_question_id == card["question_id"]
  response = _post(client, auth, chat.id, card, answers={QUESTION["prompt"]: "Casual"})
  assert response.status_code == 202, response.text
  assert inputs.request_row("changelog-tone").answer_json == {"text": "Casual"}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["approval", "restart", "secret"])
async def test_a_branch_copies_no_entry_it_could_settle(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, boot, kind,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="needs-owner",
              **inputs.request_body(kind, tmp_path))
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  entry = inputs.cards(chat.id)[-1]["questions"][0]

  branched = client.post(f"/api/chats/{chat.id}/branch", headers=auth,
                         json={"through_id": inputs.asking_run_id(chat)})

  assert branched.status_code == 200, branched.text
  branch_id = branched.json()["id"]
  assert inputs.markers(branch_id) == (None, None)
  with SessionLocal() as session:
    assert session.query(models.OwnerInputRequest).filter_by(chat_id=branch_id).count() == 0
    assert session.query(models.SavedSecureInput).filter_by(chat_id=branch_id).count() == 0
  if kind == "secret":
    copied = inputs.submit_secret(client, auth, branch_id, entry["id"])
    assert copied.status_code == 404
  assert inputs.request_row("needs-owner").status == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["approval", "restart", "secret"])
async def test_deleting_a_chat_with_each_kind_open_leaves_its_request_alone(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, boot, kind,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="needs-owner",
              **inputs.request_body(kind, tmp_path))
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card_id = inputs.cards(chat.id)[-1]["question_id"]

  assert client.delete(f"/api/chats/{chat.id}", headers=auth).status_code == 204

  with SessionLocal() as session:
    row = session.query(models.OwnerInputRequest).filter_by(key="needs-owner").one()
    # It stays open on the deleted chat's own card, which a restore brings back.
    assert (row.status, row.published_question_id) == ("open", card_id)
    assert session.get(models.Chat, chat.id).open_request_card_id == card_id


@pytest.mark.asyncio
async def test_a_secret_is_claimable_only_on_the_current_card_at_the_shown_revision(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, consumer, no_wake,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="mail-key", **inputs.secret_body(tmp_path))
  inputs.edit(client, chat.id, run_auth, op="add", key="changelog-tone", **QUESTION)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[-1]
  entry = inputs.entry(card, "mail-key")

  def claim(revision):
    return get_writer().submit(ClaimSecureInput(
      chat_id=chat.id, request_id=entry["id"], revision=revision)).result(timeout=5)

  assert claim(entry["request_revision"] + 1) == {"status": "closed"}
  # Once a newer card shows the request, the older one no longer offers it.
  inputs.start_turn(chat.id, "republish")
  await inputs.complete_turn(chat.id, monkeypatch, "republish")
  newer = inputs.cards(chat.id)[-1]
  assert newer["question_id"] != card["question_id"]
  with SessionLocal() as session:
    row = session.get(models.OwnerInputRequest, entry["id"])
    row.published_question_id = card["question_id"]
    session.commit()
  assert claim(entry["request_revision"]) == {"status": "closed"}
  late = inputs.submit_secret(client, auth, chat.id, entry["id"])
  assert late.status_code == 410
  assert consumer["seen"] == []
  with SessionLocal() as session:
    assert session.get(models.SavedSecureInput, entry["id"]).status == "pending"


@pytest.mark.parametrize("kind, typed", [("approval", "Publish"), ("restart", "Restart now")])
def test_typing_an_options_label_is_framed_as_text_never_as_the_choice(kind, typed):
  """The agent must not read a typed "Publish" or "Restart now" as the pick."""
  from app import questions

  card = {
    "input_requests": True,
    "questions": [{
      "id": "req-1", "kind": kind, "question": "Go ahead?",
      "options": [{"id": "0", "label": typed}, {"id": "1", "label": "Not now"}],
    }],
    "answers": {"Go ahead?": typed},
  }
  assert questions.typed_replies(card) == [("Go ahead?", typed)]
  # An explicit selection is the choice, and is not reported as typed text.
  assert questions.typed_replies({**card, "selected_options": {"req-1": ["0"]}}) == []
  # A legacy card keeps accepting a label from clients that predate option ids.
  legacy = {key: value for key, value in card.items() if key != "input_requests"}
  assert questions.typed_replies(legacy) == []
