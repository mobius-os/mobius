"""Approvals, restarts and secrets are asked through the request list.

The agent records them without ending its turn, like any question. At turn
end every open request, of every kind, is an entry of the one request-list
card, oldest first; the card never bars a turn and each entry is settled on
its own. The three guarantees hold per entry: a secret value never reaches
the model, transcript, logs or the list, and goes once to its consumer; an
approval authorises one exact, claimed action, granted only by an explicit
option; a restart is derived and dispatched by the platform, at most once.
More mixed-card cases are in test_owner_input_mixed_card.py.
"""
import json
import uuid

import pytest

from app import models, owner_input_requests as requests
from app.chat_writer import (
  FinishRun,
  PrepareChatStop,
  create_chat,
  get_writer,
)
from app.database import SessionLocal
from tests import owner_input_fixtures as inputs
from tests.owner_input_fixtures import (  # noqa: F401 (fixtures)
  APPROVAL,
  QUESTION,
  SECRET_VALUE,
  consumer,
  isolated_restart_source,
  no_wake,
  run_auth,
)


# ---- Approval ---------------------------------------------------------------

def test_adding_an_approval_claims_its_action_without_ending_the_turn(
  client, chat, db, run_auth,
):
  response = inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)

  assert response.status_code == 200, response.text
  listed = response.json()["open"]
  assert [(row["key"], row["kind"], row["work_key"]) for row in listed] == [
    ("publish-notes", "approval", APPROVAL["work_key"])]
  assert listed[0]["options"] == ["Publish", "Not now"]
  with SessionLocal() as session:
    claim = session.query(models.AgentWorkClaim).filter_by(
      work_key=APPROVAL["work_key"]).one()
    assert claim.owner_chat_id == chat.id and claim.released_at is None
    assert session.get(models.ChatRun, inputs.asking_run_id(chat)).status == "running"
  assert inputs.markers(chat.id) == (None, None)


def _other_chat(client, auth, db, title="Peer"):
  created = client.post("/api/chats", headers=auth, json={"title": title})
  assert created.status_code == 200, created.text
  chat_id = created.json()["id"]
  return chat_id, inputs.start_run(db, chat_id, f"peer-{chat_id}")


def test_an_approval_another_chat_owns_records_nothing_and_follows_the_owner(
  client, auth, chat, db, run_auth,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)
  peer_id, peer_auth = _other_chat(client, auth, db)

  response = inputs.edit(client, peer_id, peer_auth, op="add", key="publish", **APPROVAL)

  assert response.status_code == 200, response.text
  body = response.json()
  assert body["state"] == "held_by_peer" and body["owner_chat_id"] == chat.id
  assert body["work_key"] == APPROVAL["work_key"]
  assert "No approval request was recorded" in body["next_action"]
  assert body["open"] == []
  with SessionLocal() as session:
    assert session.query(models.OwnerInputRequest).filter_by(chat_id=peer_id).count() == 0


def test_an_approval_for_a_completed_action_records_nothing(client, auth, chat, db, run_auth):
  from app.agent_work_claims import claim_work, finish_work

  owner = db.query(models.Owner).first()
  peer_id, peer_auth = _other_chat(client, auth, db)
  claim_work(db, owner_id=owner.id, chat_id=peer_id, run_id=f"peer-{peer_id}",
             work_key=APPROVAL["work_key"], summary="Publish")
  finish_work(db, owner_id=owner.id, chat_id=peer_id, work_key=APPROVAL["work_key"],
              outcome="Published.", release=False)

  body = inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL).json()

  assert body["state"] == "completed"
  assert "do not repeat it" in body["next_action"]
  assert body["open"] == []


def test_a_refused_approval_never_takes_the_claim(client, chat, db, run_auth):
  for index in range(requests.MAX_OPEN_REQUESTS):
    requests.add_request(db, chat_id=chat.id, run_id=None, goal_id=None,
                         key=f"q-{index}", spec={"prompt": f"Question {index}?"})

  response = inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)

  assert response.status_code == 422
  with SessionLocal() as session:
    assert session.query(models.AgentWorkClaim).count() == 0


def test_an_approvals_action_never_changes_but_its_wording_may(client, chat, run_auth):
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)

  moved = inputs.edit(client, chat.id, run_auth, op="update", key="publish-notes",
                      work_key="site:other-page:publish")
  re_added = inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes",
                         **{**APPROVAL, "work_key": "site:other-page:publish"})
  reworded = inputs.edit(client, chat.id, run_auth, op="update", key="publish-notes",
                         prompt="Publish the 2.0 release notes now?")

  assert moved.status_code == 422 and "work_key cannot change" in moved.text
  assert re_added.status_code == 422
  assert reworded.status_code == 200, reworded.text
  row = reworded.json()["open"][0]
  assert row["revision"] == 2 and row["work_key"] == APPROVAL["work_key"]


def test_rewording_an_approval_keeps_its_context(client, chat, run_auth):
  context = "The notes are already reviewed; publishing makes them public."
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes",
              **APPROVAL, context=context)

  reworded = inputs.edit(client, chat.id, run_auth, op="update", key="publish-notes",
                         prompt="Publish the 2.0 release notes now?")

  assert reworded.status_code == 200, reworded.text
  spec = inputs.request_row("publish-notes").spec_json
  assert spec["prompt"] == "Publish the 2.0 release notes now?"
  assert spec["context"] == context


@pytest.mark.parametrize("body", [
  {**APPROVAL, "options": APPROVAL["options"][:1]},
  {**APPROVAL, "options": APPROVAL["options"] * 2},
  {**APPROVAL, "work_key": "No Spaces Allowed"},
  {k: v for k, v in APPROVAL.items() if k != "work_key"},
  {**APPROVAL, "multiple": True},
])
def test_an_approval_keeps_its_cards_own_limits(client, chat, run_auth, body):
  assert inputs.edit(client, chat.id, run_auth, op="add", key="publish", **body).status_code == 422


@pytest.mark.parametrize("kind", ["approval", "restart", "secret"])
def test_only_the_owner_can_settle_an_approval_restart_or_secret(
  client, chat, run_auth, monkeypatch, tmp_path, kind,
):
  monkeypatch.setenv("MOBIUS_BOOT_ID", "boot-resolve")
  inputs.edit(client, chat.id, run_auth, op="add", key="needs-owner",
              **inputs.request_body(kind, tmp_path))

  resolved = inputs.edit(client, chat.id, run_auth, op="resolve", key="needs-owner",
                         answer="Yes", source="found", reason="I decided myself.")
  withdrawn = inputs.edit(client, chat.id, run_auth, op="withdraw", key="needs-owner",
                          reason="No longer needed.")

  assert resolved.status_code == 422 and "settled only by the owner" in resolved.text
  assert withdrawn.status_code == 200 and withdrawn.json()["open"] == []


@pytest.mark.asyncio
async def test_an_approval_is_an_entry_of_the_one_card_beside_the_questions(
  client, chat, db, run_auth, monkeypatch,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="changelog-tone", **QUESTION)
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)

  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))

  cards = inputs.cards(chat.id)
  assert len(cards) == 1
  card = cards[0]
  assert card["input_requests"] is True and card["response_mode"] == "continuation"
  assert [(q["request_key"], q["kind"]) for q in card["questions"]] == [
    ("changelog-tone", "question"), ("publish-notes", "approval")]
  approval = inputs.entry(card, "publish-notes")
  # The Approval card's content, now an entry: its action key and option ids.
  assert approval["header"] == "Approval"
  assert approval["question"] == APPROVAL["prompt"]
  assert [o["id"] for o in approval["options"]] == ["0", "1"]
  assert approval["action_key"] == APPROVAL["work_key"]
  assert "action_key" not in card
  row = inputs.request_row("publish-notes")
  assert approval["id"] == row.id and approval["request_revision"] == row.revision
  assert row.published_question_id == card["question_id"]
  # Shown, never barring: another turn starts while the approval is open.
  assert inputs.markers(chat.id) == (None, card["question_id"])
  assert inputs.start_turn(chat.id, "unbarred-turn") is True


@pytest.mark.asyncio
async def test_declining_an_approval_answers_the_request_but_keeps_the_claim(
  client, auth, chat, db, run_auth, monkeypatch, no_wake,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[0]
  entry = inputs.entry(card, "publish-notes")

  response = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": f"- {APPROVAL['prompt']}: Not now", "hidden": True,
    "answers": {APPROVAL["prompt"]: "Not now"}, "question_id": card["question_id"],
    "selected_options": {entry["id"]: ["1"]},
  })

  assert response.status_code == 202, response.text
  row = inputs.request_row("publish-notes")
  assert row.status == "answered"
  assert row.answer_json == {"text": "Not now", "selected": ["1"]}
  assert row.answered_revision == row.revision and row.answered_by == "card"
  assert inputs.markers(chat.id) == (None, None)
  with SessionLocal() as session:
    # Declining is an answer, not a release: the agent settles its claim.
    claim = session.query(models.AgentWorkClaim).filter_by(
      work_key=APPROVAL["work_key"]).one()
    assert claim.released_at is None and claim.completed_at is None
    assert requests.open_snapshot(session, chat.id)["open"] == []


@pytest.mark.asyncio
async def test_an_approval_whose_claim_was_released_is_withdrawn_not_shown(
  client, chat, db, run_auth, monkeypatch,
):
  from app.agent_work_claims import finish_work

  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)
  owner = db.query(models.Owner).first()
  finish_work(db, owner_id=owner.id, chat_id=chat.id, work_key=APPROVAL["work_key"],
              outcome="Not needed after all.", release=True)

  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))

  assert inputs.cards(chat.id) == []
  row = inputs.request_row("publish-notes")
  assert row.status == "withdrawn" and "work claim" in row.outcome_json["reason"]


# ---- Every kind on one card, oldest first ----------------------------------

@pytest.mark.asyncio
async def test_a_later_turn_end_republishes_every_kind_on_a_new_card_and_bars_nothing(
  client, chat, db, run_auth, monkeypatch,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="changelog-tone", **QUESTION)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  list_card = inputs.cards(chat.id)[-1]
  assert inputs.markers(chat.id) == (None, list_card["question_id"])

  inputs.start_turn(chat.id, "approval-turn")
  inputs.edit(client, chat.id, inputs.agent_auth(db, chat.id, "approval-turn"), op="add",
              key="publish-notes", **APPROVAL)
  await inputs.complete_turn(chat.id, monkeypatch, "approval-turn")

  card = inputs.cards(chat.id)[-1]
  assert [q["request_key"] for q in card["questions"]] == ["changelog-tone", "publish-notes"]
  assert inputs.markers(chat.id) == (None, card["question_id"])
  with SessionLocal() as session:
    outcome = session.get(models.SavedDecisionCard, (chat.id, list_card["question_id"]))
    assert outcome.outcome_json["kind"] == "superseded"
  assert inputs.request_row("changelog-tone").published_question_id == card["question_id"]
  assert inputs.start_turn(chat.id, "unbarred") is True


# ---- Restart ----------------------------------------------------------------

def test_a_restart_request_needs_a_restart_loadable_boot_and_takes_nothing_else(
  client, chat, run_auth, monkeypatch,
):
  monkeypatch.delenv("MOBIUS_BOOT_ID", raising=False)
  monkeypatch.setattr("app.restart_ledger.current_boot_id", lambda: None)
  refused = inputs.edit(client, chat.id, run_auth, op="add", key="restart", kind="restart")
  assert refused.status_code == 409
  assert "platform_source_is_not_loaded" in refused.text

  monkeypatch.setattr("app.restart_ledger.current_boot_id", lambda: "boot-a")
  with_command = inputs.edit(client, chat.id, run_auth, op="add", key="restart",
                             kind="restart", prompt="Run git reset first")
  added = inputs.edit(client, chat.id, run_auth, op="add", key="restart", kind="restart")
  second = inputs.edit(client, chat.id, run_auth, op="add", key="restart-again", kind="restart")

  assert with_command.status_code == 422
  assert added.status_code == 200 and added.json()["open"][0]["kind"] == "restart"
  assert second.status_code == 422 and "already has an open restart" in second.text


async def _published_restart(client, chat, run_auth, monkeypatch):
  monkeypatch.setattr("app.restart_ledger.current_boot_id", lambda: "boot-at-ask")
  inputs.edit(client, chat.id, run_auth, op="add", key="restart", kind="restart")
  # The card binds to the boot that is current when the owner is shown it.
  monkeypatch.setattr("app.restart_ledger.current_boot_id", lambda: "boot-at-show")
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  return inputs.cards(chat.id)[-1]


def _restart_entry(card):
  return inputs.entry(card, "restart")


@pytest.mark.asyncio
async def test_a_restart_entry_binds_to_the_boot_current_when_it_is_shown(
  client, chat, db, run_auth, monkeypatch,
):
  card = await _published_restart(client, chat, run_auth, monkeypatch)

  entry = _restart_entry(card)
  action = entry["platform_action"]
  assert action["requirement"]["source_boot_id"] == "boot-at-show"
  assert action["type"] == "restart" and action["status"] == "awaiting_owner"
  assert [o["label"] for o in entry["options"]] == ["Restart now"]
  assert entry["options"][0]["id"] == action["restart_option_id"]
  assert "platform_action" not in card
  # An unpressed restart bars nothing.
  assert inputs.markers(chat.id) == (None, card["question_id"])
  with SessionLocal() as session:
    wait = session.get(models.ChatWait, action["wait_id"])
    assert wait.linked_question_id == card["question_id"]
    assert wait.condition_json["source_boot_id"] == "boot-at-show"
    assert wait.status == "armed" and wait.action_approved_at is None
    from app.platform_restart import activation_barrier_wait_id
    assert activation_barrier_wait_id(session, chat.id) is None
  assert inputs.start_turn(chat.id, "unbarred") is True


@pytest.mark.asyncio
async def test_restart_now_dispatches_once_and_settles_the_request(
  client, chat, db, run_auth, monkeypatch,
):
  card = await _published_restart(client, chat, run_auth, monkeypatch)
  option = _restart_entry(card)["platform_action"]["restart_option_id"]

  first = inputs.press_restart(chat.id, card)
  # A lost response or a second click repeats the same settled press.
  retry = inputs.press_restart(chat.id, card)

  assert first["dispatch"] is True and retry["dispatch"] is False
  assert retry["state"]["selected"] == [option]
  row = inputs.request_row("restart")
  assert row.status == "answered"
  assert row.answer_json == {"text": "Restart now", "selected": [option]}
  with SessionLocal() as session:
    wait = session.get(models.ChatWait, _restart_entry(card)["platform_action"]["wait_id"])
    assert wait.action_approved_at is not None
  # Settled, so a later turn end shows no second restart entry.
  inputs.start_turn(chat.id, "after-restart")
  await inputs.complete_turn(chat.id, monkeypatch, "after-restart")
  assert len([c for c in inputs.cards(chat.id)
              if any(q.get("kind") == "restart" for q in c["questions"])]) == 1


@pytest.mark.asyncio
async def test_a_written_restart_reply_settles_the_request_without_restarting(
  client, auth, chat, db, run_auth, monkeypatch,
):
  monkeypatch.setattr("app.routes.chats_stream._schedule_continuation",
                      lambda **kw: None)
  card = await _published_restart(client, chat, run_auth, monkeypatch)
  entry = _restart_entry(card)

  response = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": f"- {entry['question']}: Wait until tonight", "hidden": True,
    "answers": {entry["question"]: "Wait until tonight"},
    "question_id": card["question_id"],
  })

  assert response.status_code == 202, response.text
  row = inputs.request_row("restart")
  assert row.status == "answered" and row.answer_json == {"text": "Wait until tonight"}
  with SessionLocal() as session:
    wait = session.get(models.ChatWait, entry["platform_action"]["wait_id"])
    assert wait.status == "cancelled" and wait.action_approved_at is None


@pytest.mark.asyncio
async def test_a_later_ready_boot_settles_an_open_restart_request(
  client, chat, db, run_auth, monkeypatch,
):
  from app.chat_writer import StartContinuation

  card = await _published_restart(client, chat, run_auth, monkeypatch)
  wait_id = _restart_entry(card)["platform_action"]["wait_id"]
  with SessionLocal() as session:
    wait = session.get(models.ChatWait, wait_id)
    wait.status, wait.met_at = "met", wait.created_at
    session.commit()

  from app.platform_restart import activation_notice

  with SessionLocal() as session:
    wait = session.get(models.ChatWait, wait_id)
    root_id, source_id = wait.root_run_id, wait.created_by_run_id
    content = activation_notice(wait, "met")
  result = get_writer().submit(StartContinuation(
    chat_id=chat.id, run_token=f"activation-resume-{wait_id}",
    root_run_id=root_id, content=content,
    cid=f"activation-result-{wait_id}", reason="wait_result",
    message_kind="platform_activation_result", source_work_id=source_id,
    hidden=True, activation_wait_id=wait_id,
  )).result(timeout=5)
  assert isinstance(result, dict)

  row = inputs.request_row("restart")
  assert row.status == "answered"
  assert row.answer_json == {"text": "Möbius restarted", "status": "activated"}
  state = inputs.cards(chat.id)[-1]["request_states"][row.id]
  assert state["status"] == "activated"
  assert inputs.markers(chat.id)[0] is None


# ---- Secret -----------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_secret_is_asked_by_its_labels_and_its_receipt_names_the_consumer(
  client, auth, chat, db, run_auth, monkeypatch, tmp_path, consumer,
):
  """What the agent and the card see of a secret; how its value stays out
  of every record is in test_owner_input_mixed_card.py."""
  from app import saved_secure_inputs

  monkeypatch.setattr("app.routes.chats_stream._schedule_continuation",
                      lambda **kw: None)
  added = inputs.edit(client, chat.id, run_auth, op="add", key="mail-key",
                      **inputs.secret_body(tmp_path))
  assert added.status_code == 200, added.text
  assert added.json()["open"][0]["secret_fields"] == ["API key"]
  with SessionLocal() as session:
    context = requests.build_open_requests_context(session, chat.id)
  body = json.loads(context.rsplit("<open_owner_requests>", 1)[1].split(
    "</open_owner_requests>", 1)[0])
  assert [(row["key"], row["secret_fields"]) for row in body] == [("mail-key", ["API key"])]
  assert "json.load" not in context and str(tmp_path) not in context
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[-1]
  entry = inputs.entry(card, "mail-key")
  assert entry["kind"] == "secret"
  assert entry["secure_input"]["fields"][0]["name"] == "api_key"
  assert inputs.markers(chat.id) == (None, card["question_id"])
  with SessionLocal() as session:
    # One private receipt per request, keyed by the request id.
    private = session.get(models.SavedSecureInput, entry["id"])
    assert private.status == "pending" and private.cwd == str(tmp_path)

  response = inputs.submit_secret(client, auth, chat.id, entry["id"])
  assert response.status_code == 200 and response.json() == {"status": "consuming"}, response.text
  assert inputs.wait_for_secret(entry["id"]) == "completed"
  assert consumer["seen"] == [{"api_key": SECRET_VALUE}]
  row = inputs.request_row("mail-key")
  assert row.status == "answered"
  assert row.answer_json == {
    "text": saved_secure_inputs.SAFE_OUTCOMES["success"], "status": "completed"}


def test_a_secret_request_is_refused_where_no_one_could_submit_it(
  client, chat, db, run_auth, tmp_path,
):
  missing_dir = inputs.edit(client, chat.id, run_auth, op="add", key="mail-key",
                            **inputs.secret_body(tmp_path, cwd=str(tmp_path / "missing")))
  bad_field = inputs.edit(client, chat.id, run_auth, op="add", key="mail-key",
                          **{**inputs.secret_body(tmp_path), "fields": [
                            {"name": "Bad Name", "type": "password", "label": "Key"}]})
  mixed = inputs.edit(client, chat.id, run_auth, op="add", key="mail-key",
                      **inputs.secret_body(tmp_path, preset="owner_credentials"))
  app_chat = create_chat(id=str(uuid.uuid4()), title="App chat", messages=[])
  slug = f"helper-{uuid.uuid4().hex[:6]}"
  app = models.App(name="Helper app", slug=slug, source_dir=str(tmp_path / slug))
  db.add(app)
  db.flush()
  app_chat.created_by_app_id = app.id
  db.add(app_chat)
  db.commit()
  app_auth = inputs.start_run(db, app_chat.id, f"app-run-{app_chat.id}")
  in_app_chat = inputs.edit(client, app_chat.id, app_auth, op="add", key="mail-key",
                            **inputs.secret_body(tmp_path))

  assert missing_dir.status_code == 422 and "working directory" in missing_dir.text
  assert bad_field.status_code == 422 and "lowercase" in bad_field.text
  assert mixed.status_code == 422 and "preset" in mixed.text
  assert in_app_chat.status_code == 422 and "owner chats" in in_app_chat.text


@pytest.mark.parametrize("change, message", [
  ({"mode": "reveal"}, "mode"),
  ({"command": "python3 leak.py"}, "command"),
  ({"values": {"api_key": SECRET_VALUE}}, "values"),
])
def test_a_secret_request_is_always_sealed_and_never_carries_values(
  client, chat, run_auth, tmp_path, change, message,
):
  response = inputs.edit(client, chat.id, run_auth, op="add", key="mail-key",
                         **inputs.secret_body(tmp_path, **change))

  assert response.status_code == 422 and message in response.text
  # Refused before anything is stored. (A value an agent itself sends may be
  # echoed back to it in the validation error; owner values never pass here.)
  with SessionLocal() as session:
    assert session.query(models.OwnerInputRequest).count() == 0


def test_the_credentials_preset_uses_the_platforms_own_consumer(client, chat, run_auth):
  response = inputs.edit(client, chat.id, run_auth, op="add", key="sign-in",
                         kind="secret", preset="owner_credentials")

  assert response.status_code == 200, response.text
  row = inputs.request_row("sign-in")
  assert row.spec_json["consumer"]["action"] == "owner-credentials"
  assert row.spec_json["consumer"]["command"][-1].endswith("update-owner-credentials.py")
  assert row.spec_json["secure_input"]["title"] == "Update sign-in"


def test_a_secrets_consumer_never_changes_but_its_wording_may(client, chat, run_auth, tmp_path):
  inputs.edit(client, chat.id, run_auth, op="add", key="mail-key", **inputs.secret_body(tmp_path))

  moved = inputs.edit(client, chat.id, run_auth, op="update", key="mail-key",
                      command=["/bin/sh", "-c", "cat > /tmp/leak"])
  reworded = inputs.edit(client, chat.id, run_auth, op="update", key="mail-key",
                         title="Connect the mail relay")

  assert moved.status_code == 422 and "consumer cannot change" in moved.text
  assert reworded.status_code == 200 and reworded.json()["open"][0]["revision"] == 2


# ---- Stop, branch, replacement, deletion ------------------------------------

@pytest.mark.asyncio
async def test_stop_leaves_an_open_approval_on_the_card_for_the_next_turn_end(
  client, chat, db, run_auth, monkeypatch,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  first = inputs.cards(chat.id)[-1]

  get_writer().submit(PrepareChatStop(chat_id=chat.id)).result(timeout=5)
  get_writer().submit(FinishRun(chat_id=chat.id, terminal_status="stopped")).result(timeout=5)

  # The card never held the chat, so Stop has nothing of it to release.
  assert inputs.markers(chat.id) == (None, first["question_id"])
  row = inputs.request_row("publish-notes")
  assert row.status == "open" and row.published_question_id == first["question_id"]
  inputs.start_turn(chat.id, "after-stop")
  await inputs.complete_turn(chat.id, monkeypatch, "after-stop")
  again = inputs.cards(chat.id)[-1]
  assert again["question_id"] != first["question_id"]
  assert inputs.entry(again, "publish-notes")["id"] == row.id
  assert inputs.markers(chat.id) == (None, again["question_id"])


@pytest.mark.asyncio
async def test_stop_cancels_an_unpressed_restarts_wait_and_leaves_the_entry(
  client, chat, db, run_auth, monkeypatch,
):
  card = await _published_restart(client, chat, run_auth, monkeypatch)
  wait_id = _restart_entry(card)["platform_action"]["wait_id"]

  stopped = get_writer().submit(PrepareChatStop(chat_id=chat.id)).result(timeout=5)

  assert stopped.cancelled_activations == 1
  assert inputs.markers(chat.id) == (None, card["question_id"])
  row = inputs.request_row("restart")
  assert row.status == "open" and row.published_question_id == card["question_id"]
  with SessionLocal() as session:
    assert session.get(models.ChatWait, wait_id).status == "cancelled"
  # As on a saved Restart card, the offered button still restarts once, but
  # the stopped work is not resumed afterwards.
  assert inputs.press_restart(chat.id, card)["dispatch"] is True
  assert inputs.press_restart(chat.id, card)["dispatch"] is False
  with SessionLocal() as session:
    from app.platform_restart import activation_barrier_wait_id
    assert session.get(models.ChatWait, wait_id).status == "cancelled"
    assert activation_barrier_wait_id(session, chat.id) is None


@pytest.mark.asyncio
async def test_a_stop_while_the_card_is_saved_leaves_one_card_and_no_live_restart_wait(
  client, chat, db, run_auth, monkeypatch,
):
  """Stop takes the turn after its end began showing the open requests and
  before the card commits. The card is saved whole (in production, whole or
  not at all), and the restart entry's wait ends as a Stop after the card
  would end it: the entry may still be pressed, but nothing the Stop ended
  is resumed."""
  from app import chat as chat_mod
  from app.chat_event_sink import ChatEventSink
  from app.platform_restart import ACTIVATION_WAIT_KIND, activation_barrier_wait_id

  monkeypatch.setattr("app.restart_ledger.current_boot_id", lambda: "boot-stop-race")
  inputs.edit(client, chat.id, run_auth, op="add", key="changelog-tone", **QUESTION)
  inputs.edit(client, chat.id, run_auth, op="add", key="restart", kind="restart")
  publish_question = ChatEventSink.publish_question
  stops = []

  async def stop_then_commit(self, event, **kwargs):
    if event.get("input_requests") and not stops:
      stops.append(await chat_mod.stop_chat_for(self.chat_id))
    return await publish_question(self, event, **kwargs)

  monkeypatch.setattr(ChatEventSink, "publish_question", stop_then_commit)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat),
                             run_gen=chat_mod.current_run_generation(chat.id))

  assert len(stops) == 1 and stops[0].stopped
  cards = inputs.cards(chat.id)
  assert len(cards) == 1
  card_id = cards[0]["question_id"]
  assert inputs.markers(chat.id) == (None, card_id)
  with SessionLocal() as session:
    rows = session.query(models.OwnerInputRequest).all()
    assert {(row.key, row.status, row.published_question_id) for row in rows} == {
      ("changelog-tone", "open", card_id), ("restart", "open", card_id)}
    waits = session.query(models.ChatWait).filter_by(
      chat_id=chat.id, kind=ACTIVATION_WAIT_KIND).all()
    assert [(wait.linked_question_id, wait.status) for wait in waits] == [
      (card_id, "cancelled")]
    assert activation_barrier_wait_id(session, chat.id) is None
  assert inputs.press_restart(chat.id, cards[0])["dispatch"] is True
  with SessionLocal() as session:
    assert session.get(models.ChatWait, waits[0].id).status == "cancelled"
    assert activation_barrier_wait_id(session, chat.id) is None


@pytest.mark.asyncio
async def test_stop_during_a_sealed_consumer_settles_its_request_as_unknown(
  client, chat, db, run_auth, monkeypatch, tmp_path,
):
  from app.chat_writer import ClaimSecureInput
  from app.saved_secure_inputs import SAFE_OUTCOMES

  inputs.edit(client, chat.id, run_auth, op="add", key="mail-key", **inputs.secret_body(tmp_path))
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  entry = inputs.entry(inputs.cards(chat.id)[-1], "mail-key")
  claim = get_writer().submit(ClaimSecureInput(
    chat_id=chat.id, request_id=entry["id"], revision=entry["request_revision"],
  )).result(timeout=5)
  assert claim["status"] == "claimed"

  get_writer().submit(FinishRun(chat_id=chat.id, terminal_status="stopped")).result(timeout=5)

  # Its consumer may have acted: never shown again for a repeat.
  row = inputs.request_row("mail-key")
  assert row.status == "answered"
  assert row.answer_json == {"text": SAFE_OUTCOMES["interrupted"], "status": "interrupted"}
  with SessionLocal() as session:
    assert session.get(models.SavedSecureInput, entry["id"]).status == "cancelled"


@pytest.mark.asyncio
async def test_stop_leaves_a_waiting_secret_entry_waiting(
  client, chat, db, run_auth, monkeypatch, tmp_path,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="mail-key", **inputs.secret_body(tmp_path))
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  entry = inputs.entry(inputs.cards(chat.id)[-1], "mail-key")

  get_writer().submit(FinishRun(chat_id=chat.id, terminal_status="stopped")).result(timeout=5)

  with SessionLocal() as session:
    assert session.get(models.SavedSecureInput, entry["id"]).status == "pending"
  assert inputs.request_row("mail-key").status == "open"


@pytest.mark.asyncio
async def test_a_branch_inherits_neither_the_requests_nor_an_answerable_card(
  client, auth, chat, db, run_auth, monkeypatch,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card = inputs.cards(chat.id)[-1]
  entry = inputs.entry(card, "publish-notes")

  # The card holds nothing, so the reply showing it can be branched through.
  branched = client.post(f"/api/chats/{chat.id}/branch", headers=auth,
                         json={"through_id": inputs.asking_run_id(chat)})

  assert branched.status_code == 200, branched.text
  branch_id = branched.json()["id"]
  refused = client.post(f"/api/chats/{branch_id}/messages", headers=auth, json={
    "content": "- Publish", "hidden": True, "question_id": card["question_id"],
    "answers": {APPROVAL["prompt"]: "Publish"}, "selected_options": {entry["id"]: ["0"]},
  })
  assert refused.status_code == 410, refused.text
  assert inputs.markers(branch_id) == (None, None)
  with SessionLocal() as session:
    assert session.query(models.OwnerInputRequest).filter_by(chat_id=branch_id).count() == 0
  assert inputs.request_row("publish-notes").status == "open"


@pytest.mark.asyncio
async def test_deleting_the_chat_leaves_its_request_on_the_chats_own_card(
  client, auth, chat, db, run_auth, monkeypatch,
):
  inputs.edit(client, chat.id, run_auth, op="add", key="publish-notes", **APPROVAL)
  await inputs.complete_turn(chat.id, monkeypatch, inputs.asking_run_id(chat))
  card_id = inputs.cards(chat.id)[-1]["question_id"]

  assert client.delete(f"/api/chats/{chat.id}", headers=auth).status_code == 204

  with SessionLocal() as session:
    deleted = session.get(models.Chat, chat.id)
    row = session.query(models.OwnerInputRequest).filter_by(key="publish-notes").one()
    # The row names the deleted chat's own card, which a restore brings back;
    # nothing lists a deleted chat's requests, and its claim was released, so
    # a restored chat's next turn end withdraws the approval with its reason.
    assert row.published_question_id == deleted.open_request_card_id == card_id
    assert session.query(models.AgentWorkClaim).one().released_at is not None
