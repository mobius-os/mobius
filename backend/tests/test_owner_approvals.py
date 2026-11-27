"""Owner approval travels through real saved cards and the ordinary answer queue.

An approval is recorded with `owner_input` (kind approval) without ending the
turn, which claims its exact action; the turn's end shows it as an entry of
the request-list card. Tests that save a card directly cover barring Approval
cards saved by earlier releases. A helper's recorded question is the one
result that still ends a turn, so the turn-end rendezvous contracts are
exercised with its receipt.
"""
from sqlalchemy.orm import object_session
from app import transcript_rows
from app.chat_writer import create_chat

import asyncio
import json
from datetime import timedelta

import pytest

from app import auth as auth_mod, chat as chat_mod, models, questions
from app.broadcast import create_broadcast
from app.chat_event_sink import (
  ChatEventSink,
  register_active_sink,
  unregister_active_sink,
)
from app.chat_writer import AnswerQuestion, Barrier, FinishRun, StartTurn, get_writer
from app.database import SessionLocal
from app.owner_card_receipts import owner_card_receipt_id as _owner_card_receipt_id
from app.routes import chats_stream
from tests import owner_input_fixtures as inputs


PROMPT = {
  "question": "Restart to activate the tested change? This interrupts active turns.",
  "work_key": "platform:test-revision:restart",
  "options": [
    {"label": "Not now", "description": "Leave the change pending."},
    {"label": "Restart now", "description": "Interrupt active turns to activate it."},
  ],
}
QUESTION_PROMPT = {
  key: value for key, value in PROMPT.items() if key != "work_key"
}


@pytest.fixture
def approval_run(chat, db):
  run_id = f"approval-{chat.id}"
  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token=run_id,
    user_msg={"role": "user", "content": "Prepare the change", "ts": 1},
  )).result(timeout=5)
  bc = create_broadcast(chat.id)
  sink = ChatEventSink(bc, chat.id, run_token=run_id,
                       )
  register_active_sink(chat.id, sink)
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(
    chat_id=chat.id, owner_username=owner.username,
    token_epoch=owner.token_epoch, run_id=run_id,
    expires_delta=timedelta(minutes=5),
  )
  yield sink, {"Authorization": f"Bearer {token}"}
  unregister_active_sink(chat.id, sink)


def _row(chat_id):
  get_writer().submit(Barrier()).result(timeout=5)
  with SessionLocal() as db:
    row = db.get(models.Chat, chat_id)
    return row.pending_question_id, list(transcript_rows.history(row)), row.pending_messages


def _list_card_id(chat_id):
  get_writer().submit(Barrier()).result(timeout=5)
  with SessionLocal() as db:
    return db.get(models.Chat, chat_id).open_request_card_id


def _record(client, chat, approval_run, prompt=None, key="approval"):
  return inputs.add(client, chat.id, approval_run[1], key,
                    **inputs.approval_body(prompt or PROMPT))


def _ask(approval_run, prompt=None):
  """Save a barring Approval card, as chats keep from earlier releases; its id.

  The run stays running, as between the card commit and the turn's terminal
  transition; `_finish` then completes the turn. The request-list entry form
  is covered in test_owner_input_mixed_card.py.
  """
  return inputs.save_legacy_approval(approval_run[0], prompt or PROMPT)


def _answer(client, chat, auth, qid):
  return client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": f"- {PROMPT['question']}: Not now", "hidden": True,
    "answers": {PROMPT["question"]: "Not now"}, "question_id": qid,
  })


def test_answering_retained_card_does_not_orphan_newer_question(chat, db):
  transcript_rows.replace_all(object_session(chat), chat, [{
    "role": "assistant",
    "blocks": [
      {"type": "question", "question_id": "older", "questions": []},
      {"type": "question", "question_id": "newer", "questions": []},
    ],
  }])
  chat.pending_question_id = "newer"
  db.commit()

  get_writer().submit(AnswerQuestion(
    chat_id=chat.id, question_id="older", answers={"Choice": "Yes"},
  )).result(timeout=5)
  db.expire_all()
  refreshed = db.get(models.Chat, chat.id)
  assert refreshed.pending_question_id == "newer"
  assert list(transcript_rows.history(refreshed))[0]["blocks"][0]["answers"] == {"Choice": "Yes"}


def _finish(chat, sink):
  return inputs.finish_turn(sink)


class _FakeCardHandle:
  """A registered runner handle that records turn-finish requests."""

  def __init__(self, chat_id):
    from app.runner_registry import RunnerKind
    self.chat_id = chat_id
    self.kind = RunnerKind.CLAUDE_SDK
    self.finishes = 0

  def begin_finish_after_owner_card(self):
    self.finishes += 1
    return self._finish()

  async def _finish(self):
    pass

  async def stop(self, timeout: float = 2.0) -> bool:
    return True

  async def force_stop(self, timeout: float = 5.0) -> bool:
    return True


def _turn_end_receipt(sink) -> tuple[str, str]:
  """A helper question's recorded turn-ending receipt, as ask_parent prints it."""
  receipt_id = sink.record_turn_end()
  return receipt_id, json.dumps({"state": "turn_end", "turn_end_id": receipt_id})


# ---- Receipt recognition (history keeps saved-card receipts) ----------------

def test_owner_card_receipt_detection_handles_provider_result_shapes():
  receipt = {
    "state": "waiting_for_owner", "question_id": "saved-1",
    "next_action": "End now",
  }
  assert _owner_card_receipt_id(json.dumps(receipt)) == "saved-1"
  assert _owner_card_receipt_id({
    "content": [{"type": "text", "text": json.dumps(receipt)}],
    "isError": False,
  }) == "saved-1"
  assert _owner_card_receipt_id(
    "Script completed\nOutput:\n" + json.dumps(receipt)
  ) == "saved-1"
  assert _owner_card_receipt_id({
    "content": [{"type": "text", "text": json.dumps(receipt)}],
    "isError": True,
  }) is None
  assert _owner_card_receipt_id({
    "stdout": json.dumps(receipt), "stderr": "", "interrupted": False,
  }) == "saved-1"


@pytest.mark.parametrize("state", [{"draft": {}}, [], ["answered"], None, 1, True])
@pytest.mark.parametrize("encoded", [False, True])
def test_owner_card_receipt_ignores_non_string_state(state, encoded):
  value = {"state": state, "question_id": "not-a-card", "next_action": "End now"}
  assert _owner_card_receipt_id(json.dumps(value) if encoded else value) is None


def test_owner_card_receipt_traverses_wrapper_with_structured_state():
  receipt = {
    "state": "answered", "question_id": "saved-2", "next_action": "End now",
  }
  assert _owner_card_receipt_id({
    "state": {"draft": {}}, "result": receipt,
  }) == "saved-2"


def test_a_saved_card_receipt_ends_a_turn_only_for_this_turns_key_card(approval_run):
  """A recorded helper question and this turn's key card end a turn; any
  other card-shaped receipt (an old one re-printed by a tool, a request-list
  card, an earlier release's card) never does."""
  from app.owner_card_receipts import turn_end_receipt_id

  sink = approval_run[0]
  card_receipt = json.dumps({
    "state": "waiting_for_owner", "question_id": "saved-1", "next_action": "End",
  })
  assert turn_end_receipt_id(card_receipt) == "saved-1"
  assert sink.ends_turn("saved-1") is False
  sink.assistant_blocks.append({
    "type": "question", "question_id": "saved-1", "response_mode": "continuation",
    "questions": [], "input_requests": True,
  })
  assert sink.ends_turn("saved-1") is False
  sink.assistant_blocks[-1] = {
    "type": "question", "question_id": "saved-1", "response_mode": "continuation",
    "questions": [], "key_request": {"id": "req-1", "key": "weather"},
  }
  assert sink.ends_turn("saved-1") is True


def test_studio_status_output_does_not_crash_chat(approval_run):
  sink, _headers = approval_run
  content = json.dumps({"assistant": {"name": "Fixture bot"}, "state": {"draft": {}}})
  sink.publish({"type": "tool_start", "tool": "Bash", "input": "owner.py context",
                "tool_use_id": "studio-status"})
  sink.publish({"type": "tool_output", "content": content,
                "tool_use_id": "studio-status", "output_complete": True,
                "output_exit_code": 0})
  sink.publish({"type": "tool_end", "tool_use_id": "studio-status"})
  block = next(b for b in sink.assistant_blocks if b.get("tool_use_id") == "studio-status")
  assert block["output"] == content
  assert block["status"] == "done"
  assert "owner_card_question_id" not in block


# ---- The turn-end rendezvous, for a recorded helper question ----------------

def test_recording_a_turn_end_receipt_does_not_interrupt_its_own_result(chat, approval_run):
  """Recording returns before a separate post-receipt cut."""
  from app.runner_registry import registry
  handle = _FakeCardHandle(chat.id)
  registry.register(handle)
  try:
    receipt_id, _text = _turn_end_receipt(approval_run[0])
    assert approval_run[0].ends_turn(receipt_id)
    assert handle.finishes == 0
  finally:
    registry.unregister(chat.id, handle.kind)


@pytest.mark.parametrize("failed", [False, True])
def test_codex_post_tool_rendezvous_waits_for_interrupt_ack(
  client, chat, approval_run, failed,
):
  from app.runner_registry import RunnerKind, registry

  class HookHandle(_FakeCardHandle):
    def __init__(self):
      super().__init__(chat.id)
      self.kind = RunnerKind.CODEX_SDK
      self.owner_card_requested = False
      self.acknowledged = False

    def begin_finish_after_owner_card(self):
      if self.owner_card_requested:
        return None
      self.owner_card_requested = True
      self.finishes += 1
      return self._finish()

    async def _finish(self):
      await asyncio.sleep(0)
      if failed:
        raise RuntimeError("provider interrupt failed")
      self.acknowledged = True

    async def wait_for_owner_card_end(self):
      assert self.acknowledged

  handle = HookHandle()
  registry.register(handle)
  try:
    receipt_id, _text = _turn_end_receipt(approval_run[0])
    assert handle.finishes == 0  # Recording must never cancel its own result.
    url = f"/api/chats/{chat.id}/turn-end"
    headers = approval_run[1]
    wrong = client.post(url, json={"receipt_id": "old-receipt"}, headers=headers)
    assert wrong.status_code == 409
    assert handle.finishes == 0
    ended = client.post(url, json={"receipt_id": receipt_id}, headers=headers)
    if failed:
      assert ended.status_code == 503
      assert handle.acknowledged is False
    else:
      assert ended.status_code == 200
      assert ended.json() == {"ended": True}
      assert handle.acknowledged is True
      assert client.post(url, json={"receipt_id": receipt_id}, headers=headers).status_code == 200
      assert handle.finishes == 1
  finally:
    registry.unregister(chat.id, handle.kind)


def test_turn_end_rejects_browser_other_chat_and_stale_run(client, chat, approval_run, auth, db):
  from app.runner_registry import RunnerKind, registry

  receipt_id, _text = _turn_end_receipt(approval_run[0])
  url = f"/api/chats/{chat.id}/turn-end"
  body = {"receipt_id": receipt_id}
  assert client.post(url, json=body, headers=auth).status_code == 403
  assert client.post("/api/chats/another-chat/turn-end", json=body,
                     headers=approval_run[1]).status_code == 403
  # No Codex handle owns this chat's turn.
  assert client.post(url, json=body, headers=approval_run[1]).status_code == 409
  owner = db.query(models.Owner).first()
  stale = auth_mod.create_agent_token(
    chat_id=chat.id, owner_username=owner.username, token_epoch=owner.token_epoch,
    run_id="previous-physical-run", expires_delta=timedelta(minutes=5),
  )
  handle = _FakeCardHandle(chat.id)
  handle.kind = RunnerKind.CODEX_SDK
  registry.register(handle)
  try:
    # Existing authentication rejects a nonexistent/stale physical run before
    # the route can inspect its sink. Do not weaken that earlier guard.
    assert client.post(url, json=body, headers={"Authorization": f"Bearer {stale}"}).status_code == 401
    assert handle.finishes == 0
  finally:
    registry.unregister(chat.id, handle.kind)


def test_completed_receipt_ends_only_the_exact_recorded_turn(chat, approval_run):
  from app.runner_registry import registry
  handle = _FakeCardHandle(chat.id)
  registry.register(handle)
  try:
    approval_run[0].publish({
      "type": "tool_start", "tool": "Bash", "input": "ask parent",
      "tool_use_id": "ask-parent-1",
    })
    _receipt_id, text = _turn_end_receipt(approval_run[0])

    async def deliver(content, *, complete=True, exit_code=0):
      approval_run[0].publish({
        "type": "tool_output", "content": content,
        "output_complete": complete, "output_exit_code": exit_code,
        "tool_use_id": "ask-parent-1",
      })
      # The runner must own the clean ending before this callback returns;
      # a provider terminal may be the very next already-queued event.
      if complete and exit_code == 0:
        assert handle.finishes == 1
      await asyncio.sleep(0)

    asyncio.run(deliver(text, complete=False))
    asyncio.run(deliver(text, exit_code=1))
    assert handle.finishes == 0
    asyncio.run(deliver(text))
    assert handle.finishes == 1

    asyncio.run(deliver({
      "content": [{
        "type": "text",
        "text": json.dumps({"state": "turn_end", "turn_end_id": "another-receipt"}),
      }],
    }))
    assert handle.finishes == 1
  finally:
    registry.unregister(chat.id, handle.kind)


def test_streamed_receipt_survives_an_empty_completed_payload(chat, approval_run):
  """The cut must not depend on which channel carried the receipt.

  A provider that streams command output can omit its re-aggregated copy on
  completion (Codex's aggregatedOutput is optional). The completed event then
  arrives empty; it must neither erase the streamed output nor skip the cut.
  """
  from app.runner_registry import registry
  handle = _FakeCardHandle(chat.id)
  registry.register(handle)
  try:
    approval_run[0].publish({
      "type": "tool_start", "tool": "Bash", "input": "ask parent",
      "tool_use_id": "ask-parent-1",
    })
    _receipt_id, text = _turn_end_receipt(approval_run[0])

    async def deliver(content, *, complete=False, exit_code=None):
      event = {
        "type": "tool_output", "content": content,
        "tool_use_id": "ask-parent-1",
      }
      if complete:
        event["output_complete"] = True
      if exit_code is not None:
        event["output_exit_code"] = exit_code
      approval_run[0].publish(event)
      await asyncio.sleep(0)

    asyncio.run(deliver(text))
    asyncio.run(deliver("", complete=True, exit_code=0))
    assert handle.finishes == 1
    blk = next(
      block for block in approval_run[0].assistant_blocks
      if block.get("type") == "tool"
    )
    assert text in blk["output"]
  finally:
    registry.unregister(chat.id, handle.kind)


def test_native_question_event_does_not_end_the_active_turn(chat, approval_run):
  """The native AskUserQuestion path shares `publish_question` but carries no
  `response_mode`: it parks on an awaited future in question_bridge and must NOT
  be interrupted here."""
  from app.runner_registry import registry
  sink = approval_run[0]
  handle = _FakeCardHandle(chat.id)
  registry.register(handle)

  async def go():
    await sink.publish_question({
      "type": "question",
      "question_id": "native-1",
      "questions": [{
        "question": "Pick a color",
        "options": [
          {"label": "Blue", "description": "b"},
          {"label": "Red", "description": "r"},
        ],
      }],
    })

  try:
    asyncio.run(go())
    assert handle.finishes == 0
    block = _row(chat.id)[1][-1]["blocks"][-1]
    assert block["question_id"] == "native-1"
    assert "response_mode" not in block
  finally:
    registry.unregister(chat.id, handle.kind)


def test_receipt_ends_the_turn_then_preserves_the_raced_tail(chat, approval_run):
  """A successful completed receipt synchronously claims the runner's turn end
  (the fallback path, for a runner that did not already end itself), while
  provider events already in flight remain live and durable: Möbius removes
  the cause of post-receipt output, never the evidence."""
  from app.runner_registry import registry

  sink = approval_run[0]
  handle = _FakeCardHandle(chat.id)
  registry.register(handle)
  try:
    assert sink.publish({
      "type": "tool_start", "tool": "Bash", "input": "ask parent",
      "tool_use_id": "ask-parent-race",
    })
    _receipt_id, text = _turn_end_receipt(sink)

    async def deliver_receipt_and_raced_tail():
      assert sink.publish({
        "type": "tool_output", "content": text,
        "output_complete": True, "output_exit_code": 0,
        "tool_use_id": "ask-parent-race",
      })
      assert handle.finishes == 1

      log_before = len(sink.bc.event_log)
      assert sink.publish({"type": "text", "content": "Already emitted tail."})
      assert sink.publish({"type": "thinking", "content": "Already emitted trace."})
      assert len(sink.bc.event_log) == log_before + 2
      await sink.finalize()

    asyncio.run(deliver_receipt_and_raced_tail())

    persisted = _row(chat.id)[1][-1]["blocks"]
    assert [block["type"] for block in persisted] == ["tool", "text", "thinking"]
    assert persisted[1]["content"] == "Already emitted tail."
  finally:
    registry.unregister(chat.id, handle.kind)


def test_turn_end_is_gated_on_the_exact_receipt_this_turn_recorded(chat, approval_run):
  """A receipt-shaped tool result alone must never end a turn."""
  from app.runner_registry import registry

  sink = approval_run[0]
  handle = _FakeCardHandle(chat.id)
  registry.register(handle)
  try:
    receipt_id, _text = _turn_end_receipt(sink)
    assert sink.ends_turn(receipt_id) is True
    assert sink.ends_turn("receipt-from-last-week") is False
    for stale in (
      {"state": "turn_end", "turn_end_id": "receipt-from-last-week"},
      {"state": "waiting_for_owner", "question_id": "card-from-last-week",
       "next_action": "End now"},
    ):
      assert sink.publish({
        "type": "tool_output", "content": json.dumps(stale),
        "output_complete": True, "output_exit_code": 0,
        "tool_use_id": "stale-echo",
      })
    assert handle.finishes == 0
  finally:
    registry.unregister(chat.id, handle.kind)


def test_a_streamed_messages_tail_still_lands_in_its_own_block(chat, approval_run):
  """A message tail reattaches to its original block across a card, while a
  distinct later message remains visible after it instead of being discarded."""
  sink = approval_run[0]
  assert sink.publish(
    {"type": "text", "content": "Reading it", "text_item_id": "msg-1"},
  )
  asyncio.run(sink.publish_question({
    "type": "question", "question_id": "native-tail",
    "questions": [{"question": "Pick one", "options": []}],
  }))
  assert sink.publish(
    {"type": "text", "content": " first.", "text_item_id": "msg-1"},
  )
  assert [b["type"] for b in sink.assistant_blocks] == ["text", "question"]
  assert sink.assistant_blocks[0]["content"] == "Reading it first."
  assert sink.publish(
    {"type": "text", "content": " Done.", "text_item_id": "msg-2"},
  )
  assert [b["type"] for b in sink.assistant_blocks] == [
    "text", "question", "text",
  ]
  assert sink.assistant_blocks[-1]["content"] == " Done."


def test_native_question_keeps_recording_post_card_prose(chat, approval_run):
  """A native AskUserQuestion is mid-turn, so prose after that card stays
  ordinary transcript."""
  sink = approval_run[0]

  async def go():
    await sink.publish_question({
      "type": "question",
      "question_id": "native-post-1",
      "questions": [{
        "question": "Pick one",
        "options": [
          {"label": "A", "description": "a"},
          {"label": "B", "description": "b"},
        ],
      }],
    })

  asyncio.run(go())
  assert sink.publish({"type": "text", "content": "While you decide, notes."})
  assert [b["type"] for b in sink.assistant_blocks] == ["question", "text"]


# ---- Approval requests and their saved card ---------------------------------

def test_approval_card_is_saved_at_turn_end_without_a_waiting_future(
  client, chat, approval_run,
):
  recorded = _record(client, chat, approval_run)
  assert recorded.status_code == 200, recorded.text
  # Asking is not showing: nothing bars the chat until the turn ends.
  assert _row(chat.id)[0] is None
  assert "answers" not in json.dumps(recorded.json()["open"])

  _finish(chat, approval_run[0])

  marker, messages, pending = _row(chat.id)
  block = messages[-1]["blocks"][-1]
  # An entry of the request-list card, which never bars the chat.
  assert marker is None and _list_card_id(chat.id) == block["question_id"]
  assert pending == []
  assert questions.get(chat.id) is None
  assert block["response_mode"] == "continuation" and block["input_requests"] is True
  entry = block["questions"][0]
  assert entry["kind"] == "approval" and entry["action_key"] == PROMPT["work_key"]
  assert block["questions"][0]["options"] == [
    {**option, "id": str(index)} for index, option in enumerate(PROMPT["options"])
  ]
  assert "answers" not in block
  # The turn's closing text is committed with its card, before it.
  assert messages[-1]["blocks"][0]["type"] == "text"


def test_identical_retry_records_one_request_and_a_second_approval_is_a_second_entry(
  client, chat, approval_run,
):
  first = _record(client, chat, approval_run)
  again = _record(client, chat, approval_run)
  assert [row["id"] for row in again.json()["open"]] == [first.json()["open"][0]["id"]]
  other = _record(client, chat, approval_run,
                  {**PROMPT, "question": "Different action?",
                   "work_key": "platform:test-revision:other"}, key="other")
  assert [row["key"] for row in other.json()["open"]] == ["approval", "other"]

  _finish(chat, approval_run[0])

  cards = [b for m in _row(chat.id)[1] for b in m.get("blocks", []) if b["type"] == "question"]
  # Every open request is an entry of the one card, oldest first.
  assert len(cards) == 1
  assert [(entry["kind"], entry["question"], entry["action_key"])
          for entry in cards[0]["questions"]] == [
    ("approval", PROMPT["question"], PROMPT["work_key"]),
    ("approval", "Different action?", "platform:test-revision:other"),
  ]


SHARED_KEY = "github:mobius-os/mobius:pr:1079:3134e050:merge"


def _second_approval_chat(db, chat_id="other-approval-chat"):
  """Another running chat with its own Goal."""
  other = create_chat(id=chat_id, title="Duplicate integrator", messages=[])
  other_run = models.ChatRun(
    id=f"{chat_id}-run", root_run_id=f"{chat_id}-run", chat_id=other.id,
    goal_id=f"{chat_id}-goal", goal_objective="Integrate the PR",
    status="running", provider="codex",
  )
  db.add_all([other, other_run, models.ChatGoal(
    id=f"{chat_id}-goal", chat_id=other.id, objective="Integrate the PR",
  )])
  db.commit()
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(
    chat_id=other.id, owner_username=owner.username,
    token_epoch=owner.token_epoch, run_id=other_run.id,
    expires_delta=timedelta(minutes=5),
  )
  sink = ChatEventSink(
    create_broadcast(other.id), other.id, run_token=other_run.id,
  )
  return other, sink, {"Authorization": f"Bearer {token}"}


def _record_as(client, other, headers, prompt, key="approval"):
  return inputs.add(client, other.id, headers, key, **inputs.approval_body(prompt))


def test_an_approval_request_claims_an_unclaimed_key_in_one_call(
  client, chat, approval_run, db,
):
  recorded = _record(client, chat, approval_run, {**PROMPT, "work_key": SHARED_KEY})

  assert recorded.status_code == 200, recorded.text
  assert recorded.json()["open"][0]["work_key"] == SHARED_KEY
  claim = db.query(models.AgentWorkClaim).one()
  assert (claim.work_key, claim.owner_chat_id) == (SHARED_KEY, chat.id)
  assert claim.completed_at is None and claim.released_at is None


def test_losing_approval_request_follows_the_owner_without_a_request(
  client, chat, approval_run, db,
):
  keyed = {**PROMPT, "work_key": SHARED_KEY}
  assert _record(client, chat, approval_run, keyed).status_code == 200
  other, sink, headers = _second_approval_chat(db)

  follower = _record_as(client, other, headers, keyed)

  # Same result claim_agent_work returns, not an error: the turn continues.
  assert follower.status_code == 200, follower.text
  body = follower.json()
  assert (body["state"], body["owner_chat_id"]) == ("held_by_peer", chat.id)
  assert "question_id" not in body and body["open"] == []
  assert "No approval request was recorded and your turn continues" in body["next_action"]
  assert db.query(models.AgentWorkClaim).count() == 1
  assert db.query(models.GoalDependency).one().waiting_chat_id == other.id
  inputs.finish_turn(sink)
  assert _row(other.id)[0] is None

  from app.agent_work_claims import finish_work
  finish_work(db, owner_id=db.query(models.Owner).first().id, chat_id=chat.id,
              work_key=SHARED_KEY, outcome="Merged as 0b44dc9d", release=False)
  done_chat, _done_sink, done_headers = _second_approval_chat(db, "third-approval-chat")
  done = _record_as(client, done_chat, done_headers, keyed)
  assert done.status_code == 200, done.text
  assert done.json()["state"] == "completed"
  assert "do not repeat it" in done.json()["next_action"]
  assert db.query(models.OwnerInputRequest).filter_by(chat_id=done_chat.id).count() == 0


def approval_run_id(chat):
  return f"approval-{chat.id}"


def test_racing_approval_requests_on_one_key_record_exactly_one_request(
  client, chat, approval_run, db, monkeypatch,
):
  """The other chat's claim lands between this request's check and insert."""
  from app import agent_work_claims
  from app.agent_work_claims import claim_work

  other, sink, headers = _second_approval_chat(db)
  raced = []
  original_fence = agent_work_claims.fence_owner

  def first_chat_wins_before_the_fence(session, owner_id):
    # Compete after the collision read, before acquisition holds the fence.
    if not raced:
      raced.append(True)
      with SessionLocal() as competing:
        claim_work(competing, owner_id=owner_id, chat_id=chat.id,
                   run_id=approval_run_id(chat), work_key=SHARED_KEY,
                   summary="Merge the reviewed PR")
    original_fence(session, owner_id)

  keyed = {**PROMPT, "work_key": SHARED_KEY}
  monkeypatch.setattr(agent_work_claims, "fence_owner", first_chat_wins_before_the_fence)
  lost = _record_as(client, other, headers, keyed)
  won = _record(client, chat, approval_run, keyed)

  assert raced == [True]
  assert lost.json()["state"] == "held_by_peer", lost.text
  assert [row["key"] for row in won.json()["open"]] == ["approval"], won.text
  assert db.query(models.OwnerInputRequest).filter_by(chat_id=other.id).count() == 0
  assert db.query(models.AgentWorkClaim).count() == 1


def test_approval_without_action_identity_is_rejected_before_any_claim(
  client, chat, approval_run, db,
):
  unowned = {key: value for key, value in inputs.approval_body(PROMPT).items()
             if key != "work_key"}

  response = inputs.add(client, chat.id, approval_run[1], "approval", **unowned)

  assert response.status_code == 422
  assert db.query(models.AgentWorkClaim).count() == 0
  assert db.query(models.OwnerInputRequest).count() == 0


def test_unsaved_card_keeps_its_claim_and_request_for_the_next_turn_end(
  client, chat, approval_run, monkeypatch, db,
):
  writer = get_writer()
  original = writer._persist_question_required

  def fail(*args, **kwargs):
    raise RuntimeError("test write failed")

  assert _record(client, chat, approval_run).status_code == 200
  monkeypatch.setattr(writer, "_persist_question_required", fail)
  inputs.publish_now(approval_run[0])
  assert _row(chat.id)[0] is None
  assert not any(b["type"] == "question" for b in approval_run[0].assistant_blocks)
  # Asking succeeded, so exact-action ownership stays reserved, and the
  # request stays open and unshown for the next turn end.
  claim = db.query(models.AgentWorkClaim).one()
  assert (claim.owner_chat_id, claim.released_at) == (chat.id, None)
  row = db.query(models.OwnerInputRequest).one()
  assert row.status == "open" and row.published_question_id is None
  monkeypatch.setattr(writer, "_persist_question_required", original)
  inputs.publish_now(approval_run[0])
  assert _list_card_id(chat.id) == inputs.last_card(chat.id)["question_id"]


def test_approval_requires_exact_agent_run_not_plain_owner_or_foreign_chat(
  client, chat, auth, approval_run, db,
):
  body = {"op": "add", "key": "approval", **inputs.approval_body(PROMPT)}
  assert client.post(f"/api/chats/{chat.id}/input-requests", json=body,
                     headers=auth).status_code == 403
  foreign = client.post("/api/chats", json={"title": "Other"}, headers=auth).json()["id"]
  assert client.post(f"/api/chats/{foreign}/input-requests", json=body,
                     headers=approval_run[1]).status_code == 403
  assert _row(foreign)[0] is None
  assert db.query(models.AgentWorkClaim).count() == 0


def test_approval_rejects_a_run_that_is_no_longer_current(
  client, chat, approval_run, db,
):
  get_writer().submit(FinishRun(
    chat_id=chat.id, run_token=approval_run[0].run_token, terminal_status="completed",
  )).result(timeout=5)
  assert _record(client, chat, approval_run).status_code == 401
  assert db.query(models.AgentWorkClaim).count() == 0


def test_lost_broadcast_after_commit_keeps_the_saved_card_last(client, chat, approval_run, monkeypatch):
  sink = approval_run[0]
  original = sink.bc.publish

  def fail(event):
    if event.get("type") == "question":
      raise RuntimeError("connection lost after commit")
    original(event)

  assert _record(client, chat, approval_run).status_code == 200
  monkeypatch.setattr(sink.bc, "publish", fail)
  inputs.publish_now(sink)
  qid = _list_card_id(chat.id)
  assert qid
  # The committed card is shown on reload and stays the turn's last item; no
  # "could not be shown" error is appended after it.
  assert sink.assistant_blocks[-1]["question_id"] == qid
  monkeypatch.setattr(sink.bc, "publish", original)
  _finish(chat, sink)
  assert _row(chat.id)[1][-1]["blocks"][-1]["question_id"] == qid


def _control_against(client, monkeypatch, chat_id, run_token, headers):
  """The real control server, its HTTP calls answered by this test client."""
  import io
  from tests.test_platform_tools import _control_module

  control = _control_module()
  monkeypatch.setenv("API_BASE_URL", "http://testserver")
  monkeypatch.setenv("CHAT_ID", chat_id)
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", run_token)
  monkeypatch.setenv("AGENT_TOKEN", headers["Authorization"].removeprefix("Bearer "))

  def open_request(request, timeout):
    response = client.post(
      request.full_url, content=request.data, headers=dict(request.header_items()),
    )
    assert response.status_code == 200, response.text
    return io.BytesIO(response.content)

  monkeypatch.setattr(control, "urlopen", open_request)
  return control


def test_control_tool_records_an_approval_that_is_not_permission(
  client, chat, approval_run, monkeypatch,
):
  control = _control_against(
    client, monkeypatch, chat.id, approval_run[0].run_token, approval_run[1],
  )
  result = control._dispatch_message({
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "owner_input", "arguments": {
      "op": "add", "key": "approval", **inputs.approval_body(PROMPT)}},
  })["result"]
  assert not result["isError"]
  body = json.loads(result["content"][0]["text"])
  assert [row["kind"] for row in body["open"]] == ["approval"]
  assert "Nothing here is answered" in body["next_action"]
  assert not any("answer" in row for row in body["open"])
  assert _row(chat.id)[0] is None


def test_control_tool_returns_follower_claim_to_the_losing_chat(
  client, chat, approval_run, db, monkeypatch,
):
  """The losing approval is one successful call, not an error to retry."""
  keyed = {**PROMPT, "work_key": SHARED_KEY}
  assert _record(client, chat, approval_run, keyed).status_code == 200
  other, sink, headers = _second_approval_chat(db)
  control = _control_against(client, monkeypatch, other.id, sink.run_token, headers)

  result = control._dispatch_message({
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "owner_input", "arguments": {
      "op": "add", "key": "approval", **inputs.approval_body(keyed)}},
  })["result"]

  assert not result["isError"]
  follower = json.loads(result["content"][0]["text"])
  assert (follower["state"], follower["owner_chat_id"]) == ("held_by_peer", chat.id)
  assert _row(other.id)[0] is None


# ---- Barring cards saved by earlier releases --------------------------------
#
# Chats keep the barring Approval and Q&A cards earlier releases saved; they
# still bar the chat and settle through their own answer path.

def test_a_legacy_approval_card_stays_answerable_after_its_turn_and_a_late_answer_starts_once(
  client, chat, auth, approval_run, monkeypatch,
):
  qid = _ask(approval_run)
  _finish(chat, approval_run[0])
  assert not chat_mod.is_chat_running(chat.id)
  assert _row(chat.id)[0] == qid
  # This also represents restart recovery: only the saved card owns the wait.
  unregister_active_sink(chat.id, approval_run[0])
  scheduled = []
  monkeypatch.setattr(chats_stream, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  result = _answer(client, chat, auth, qid)
  assert result.status_code == 202, result.text
  assert result.json()["answer_turn"] == "new"
  assert len(scheduled) == 1
  assert scheduled[0]["next_user"]["continuation_reason"] == "question_answer"
  assert _row(chat.id)[0] is None
  assert _answer(client, chat, auth, qid).status_code == 410
  assert len(scheduled) == 1


@pytest.mark.parametrize("hidden", [False, True])
def test_an_answer_to_a_legacy_card_does_not_reorder_owner_models(
  client, chat, auth, approval_run, monkeypatch, db, hidden,
):
  """A durable card continuation is not a new owner composer send."""
  qid = _ask(approval_run)
  _finish(chat, approval_run[0])
  unregister_active_sink(chat.id, approval_run[0])
  owner = db.query(models.Owner).first()
  owner.model_prefs_json = {"recent_ids": ["claude-opus-4-7"]}
  chat.agent_settings_json = {"model": "claude-sonnet-4-6"}
  db.commit()
  scheduled = []
  monkeypatch.setattr(chats_stream, "_schedule_continuation", lambda **kw: scheduled.append(kw))

  answer = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": f"- {PROMPT['question']}: Not now", "hidden": hidden,
    "answers": {PROMPT["question"]: "Not now"}, "question_id": qid,
    "cid": f"saved-answer-{hidden}",
  })
  assert answer.status_code == 202, answer.text
  assert answer.json()["answer_turn"] == "new"
  assert len(scheduled) == 1
  assert scheduled[0]["next_user"]["continuation_reason"] == "question_answer"
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == [
    "claude-opus-4-7",
  ]
  assert _answer(client, chat, auth, qid).status_code == 410
  assert client.get("/api/owner/model-prefs", headers=auth).json()["recent_ids"] == [
    "claude-opus-4-7",
  ]


def test_an_immediate_answer_to_a_legacy_approval_card_queues_once_and_starts_after_the_turn(
  client, chat, auth, approval_run, monkeypatch,
):
  qid = _ask(approval_run)
  result = _answer(client, chat, auth, qid)
  assert result.status_code == 202, result.text
  assert result.json()["status"] == "queued"
  assert result.json()["answer_turn"] == "queued"
  marker, messages, pending = _row(chat.id)
  assert marker is None
  assert len(pending) == 1 and pending[0]["continuation_reason"] == "question_answer"
  assert messages[-1]["blocks"][-1]["answers"] == {PROMPT["question"]: "Not now"}
  assert _answer(client, chat, auth, qid).status_code == 410
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  _finish(chat, approval_run[0])
  assert len(scheduled) == 1
  assert _row(chat.id)[2] == []
  assert _row(chat.id)[0] is None


def test_a_failed_early_answer_keeps_a_legacy_approval_card_open_and_queues_nothing(
  client, chat, auth, approval_run, monkeypatch,
):
  qid = _ask(approval_run)
  from app.chat_writer import AppendPending
  writer = get_writer()
  original = writer.submit

  def submit(command):
    if isinstance(command, AppendPending):
      from concurrent.futures import Future
      future = Future()
      future.set_exception(RuntimeError("answer write failed"))
      return future
    return original(command)

  monkeypatch.setattr(writer, "submit", submit)
  assert _answer(client, chat, auth, qid).status_code == 503
  assert _row(chat.id)[0] == qid
  assert _row(chat.id)[2] == []


@pytest.mark.parametrize("status,keeps_card", [
  ("completed", True), ("interrupted", True), ("failed", True), ("stopped", False),
])
def test_a_terminal_status_keeps_a_legacy_approval_card_but_stop_cancels_it(
  client, chat, approval_run, status, keeps_card,
):
  qid = _ask(approval_run)
  get_writer().submit(FinishRun(
    chat_id=chat.id, run_token=approval_run[0].run_token, terminal_status=status,
  )).result(timeout=5)
  assert _row(chat.id)[0] == (qid if keeps_card else None)
  assert _row(chat.id)[2] == []
  # A stale stream snapshot must not reopen an explicitly cancelled approval.
  asyncio.run(approval_run[0].finalize())
  assert _row(chat.id)[0] == (qid if keeps_card else None)


def test_a_native_question_cannot_overlap_a_legacy_approval_card(client, chat, approval_run):
  qid = _ask(approval_run)
  from app.question_bridge import QuestionPersistenceError, park_question

  async def native():
    with pytest.raises(QuestionPersistenceError):
      await park_question(
        chat_id=chat.id, questions=[{"id": "other", "question": "Another?"}],
        bc=approval_run[0], pending_questions=questions._pending,
      )
    assert questions.get(chat.id) is None

  asyncio.run(native())
  assert _row(chat.id)[0] == qid


def test_stop_winning_answer_admission_on_a_legacy_approval_card_queues_nothing(
  client, chat, auth, approval_run, monkeypatch,
):
  from contextlib import asynccontextmanager
  from app import chat_queue
  from app.chat_writer import await_ack

  qid = _ask(approval_run)
  original_gate = chat_queue.get_transition_lock

  @asynccontextmanager
  async def stop_wins(chat_id):
    # Stop lands after the answer's initial read but before admission. It
    # changes the same generation and durable run state as the actual owner.
    async with original_gate(chat_id):
      chat_mod.bump_run_generation(chat_id)
      await await_ack(get_writer().submit(FinishRun(
        chat_id=chat_id, run_token=approval_run[0].run_token,
        terminal_status="stopped",
      )))
      yield

  monkeypatch.setattr(chat_queue, "get_transition_lock", stop_wins)
  response = _answer(client, chat, auth, qid)
  assert response.status_code == 410
  assert _row(chat.id)[0] is None
  assert _row(chat.id)[2] == []


def test_a_legacy_approval_card_bars_a_plain_send_until_answered(
  client, chat, auth, approval_run,
):
  qid = _ask(approval_run)
  _finish(chat, approval_run[0])
  assert _row(chat.id)[0] == qid
  blocked = client.post(f"/api/chats/{chat.id}/messages", headers=auth,
                        json={"content": "Skip past the card"})
  assert blocked.status_code == 409
  assert _row(chat.id)[0] == qid


def test_answering_a_legacy_ten_question_card_saves_every_answer_and_resumes_once(
  client, chat, auth, approval_run, monkeypatch,
):
  """A chat's saved barring Q&A card (earlier releases saved these) stays
  answerable: every answer lands and exactly one continuation starts."""
  question_id = inputs.save_barring_question(approval_run[0], [
    {"question": f"Decision {index}?", "options": []} for index in range(1, 11)
  ])
  _finish(chat, approval_run[0])
  unregister_active_sink(chat.id, approval_run[0])
  scheduled = []
  monkeypatch.setattr(chats_stream, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  answers = {f"Decision {index}?": f"Reply {index}" for index in range(1, 11)}
  body = {"content": "Ten decisions answered", "hidden": True,
          "answers": answers, "question_id": question_id}
  response = client.post(f"/api/chats/{chat.id}/messages", json=body, headers=auth)
  assert response.status_code == 202, response.text
  pending_id, messages, _ = _row(chat.id)
  assert pending_id is None
  card = next(
    block for message in messages for block in message.get("blocks", [])
    if block.get("question_id") == question_id
  )
  assert card["answers"] == answers
  assert len(scheduled) == 1
  assert scheduled[0]["next_user"]["continuation_reason"] == "question_answer"
  retry = client.post(f"/api/chats/{chat.id}/messages", json=body, headers=auth)
  assert retry.status_code == 410
  assert len(scheduled) == 1
