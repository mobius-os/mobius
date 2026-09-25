"""Owner approval travels through real saved cards and the ordinary answer queue."""

import asyncio
import json
from datetime import timedelta

import pytest

from app import auth as auth_mod, chat as chat_mod, models, questions
from app.broadcast import create_broadcast
from app.chat_event_sink import (
  ChatEventSink,
  _owner_card_receipt_id,
  register_active_sink,
  unregister_active_sink,
)
from app.chat_writer import AnswerQuestion, Barrier, FinishRun, StartTurn, get_writer
from app.database import SessionLocal
from app.routes import chats_stream


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
    return row.pending_question_id, row.messages, row.pending_messages


def _ask(client, chat, approval_run, prompt=None):
  return client.post(f"/api/chats/{chat.id}/approval", json=prompt or PROMPT,
                     headers=approval_run[1])


def _answer(client, chat, auth, qid):
  return client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content": f"- {PROMPT['question']}: Not now", "hidden": True,
    "answers": {PROMPT["question"]: "Not now"}, "question_id": qid,
  })


def test_answering_retained_card_does_not_orphan_newer_question(chat, db):
  chat.messages = [{
    "role": "assistant",
    "blocks": [
      {"type": "question", "question_id": "older", "questions": []},
      {"type": "question", "question_id": "newer", "questions": []},
    ],
  }]
  chat.pending_question_id = "newer"
  db.commit()

  get_writer().submit(AnswerQuestion(
    chat_id=chat.id, question_id="older", answers={"Choice": "Yes"},
  )).result(timeout=5)
  db.expire_all()
  refreshed = db.get(models.Chat, chat.id)
  assert refreshed.pending_question_id == "newer"
  assert refreshed.messages[0]["blocks"][0]["answers"] == {"Choice": "Yes"}


def _finish(chat, sink):
  async def finish():
    with SessionLocal() as db:
      return await chat_mod._complete_turn(
        bc=sink.bc, sink=sink, db=db, chat_id=chat.id, run_gen=None,
        provider_id="claude", cost_usd=0, close_browser=False,
      )
  return asyncio.run(finish())


class _FakeCardHandle:
  """A registered runner handle that records card-finish requests."""

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
  # The Claude CLI's Bash tool_response, which the card-end hook reads when the
  # turn saved its card through the owner_approval.py / secure-input helper.
  assert _owner_card_receipt_id({
    "stdout": json.dumps(receipt), "stderr": "", "interrupted": False,
  }) == "saved-1"


def test_continuation_card_save_does_not_interrupt_its_own_receipt(
  client, chat, approval_run,
):
  """The save request must return before a separate post-receipt cut."""
  from app.runner_registry import registry
  handle = _FakeCardHandle(chat.id)
  registry.register(handle)
  try:
    res = _ask(client, chat, approval_run)
    assert res.status_code == 200, res.text
    assert res.json()["state"] == "waiting_for_owner"
    assert handle.finishes == 0
  finally:
    registry.unregister(chat.id, handle.kind)


def test_completed_receipt_ends_only_the_exact_saved_card_turn(
  client, chat, approval_run,
):
  from app.runner_registry import registry
  handle = _FakeCardHandle(chat.id)
  registry.register(handle)
  try:
    approval_run[0].publish({
      "type": "tool_start", "tool": "Bash", "input": "owner helper",
      "tool_use_id": "owner-helper-1",
    })
    saved = _ask(client, chat, approval_run)
    qid = saved.json()["question_id"]
    async def deliver(content, *, complete=True, exit_code=0):
      approval_run[0].publish({
        "type": "tool_output", "content": content,
        "output_complete": complete, "output_exit_code": exit_code,
        "tool_use_id": "owner-helper-1",
      })
      # The runner must own the clean card ending before this callback returns;
      # a provider terminal may be the very next already-queued event.
      if complete and exit_code == 0:
        assert handle.finishes == 1
      await asyncio.sleep(0)

    asyncio.run(deliver(saved.text, complete=False))
    asyncio.run(deliver(saved.text, exit_code=1))
    assert handle.finishes == 0
    asyncio.run(deliver(saved.text))
    assert handle.finishes == 1
    assert approval_run[0].assistant_blocks[0][
      "owner_card_question_id"
    ] == qid

    asyncio.run(deliver({
      "content": [{
        "type": "text",
        "text": json.dumps({
          "state": "waiting_for_owner",
          "question_id": "another-card",
          "next_action": "End",
        }),
      }],
    }))
    assert handle.finishes == 1
  finally:
    registry.unregister(chat.id, handle.kind)


def test_streamed_receipt_survives_an_empty_completed_payload(
  client, chat, approval_run,
):
  """The card cut must not depend on which channel carried the receipt.

  A provider that streams command output can omit its re-aggregated copy on
  completion (Codex's aggregatedOutput is optional). The completed event then
  arrives empty; it must neither erase the streamed output nor skip the
  finish-after-owner-card cut.
  """
  from app.runner_registry import registry
  handle = _FakeCardHandle(chat.id)
  registry.register(handle)
  try:
    approval_run[0].publish({
      "type": "tool_start", "tool": "Bash", "input": "owner helper",
      "tool_use_id": "owner-helper-1",
    })
    saved = _ask(client, chat, approval_run)
    qid = saved.json()["question_id"]
    async def deliver(content, *, complete=False, exit_code=None):
      event = {
        "type": "tool_output", "content": content,
        "tool_use_id": "owner-helper-1",
      }
      if complete:
        event["output_complete"] = True
      if exit_code is not None:
        event["output_exit_code"] = exit_code
      approval_run[0].publish(event)
      await asyncio.sleep(0)

    asyncio.run(deliver(saved.text))
    asyncio.run(deliver("", complete=True, exit_code=0))
    assert handle.finishes == 1
    blk = next(
      block for block in approval_run[0].assistant_blocks
      if block.get("type") == "tool"
    )
    assert blk["owner_card_question_id"] == qid
    assert saved.text in blk["output"]
  finally:
    registry.unregister(chat.id, handle.kind)


def test_native_question_event_does_not_end_the_active_turn(chat, approval_run):
  """The native AskUserQuestion path shares `publish_question` but carries no
  `response_mode`: it parks on an awaited future in question_bridge and must NOT
  be interrupted here. Only continuation cards end the turn."""
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


def test_approval_saves_before_receipt_without_a_waiting_future(
  client, chat, approval_run,
):
  res = _ask(client, chat, approval_run)
  assert res.status_code == 200, res.text
  qid = res.json()["question_id"]
  assert res.json()["state"] == "waiting_for_owner"
  marker, messages, pending = _row(chat.id)
  assert marker == qid
  assert pending == []
  assert questions.get(chat.id) is None
  block = messages[-1]["blocks"][-1]
  assert block["response_mode"] == "continuation"
  assert block["action_key"] == PROMPT["work_key"]
  assert block["questions"][0]["options"] == [
    {**option, "id": str(index)} for index, option in enumerate(PROMPT["options"])
  ]
  assert "answers" not in block


def test_identical_creation_retry_returns_same_card_and_different_request_conflicts(
  client, chat, approval_run,
):
  first = _ask(client, chat, approval_run)
  again = _ask(client, chat, approval_run)
  assert first.json() == again.json()
  assert len([b for b in _row(chat.id)[1][-1]["blocks"] if b["type"] == "question"]) == 1
  changed = _ask(client, chat, approval_run, {**PROMPT, "question": "Different action?"})
  assert changed.status_code == 409


SHARED_KEY = "github:mobius-os/mobius:pr:1079:3134e050:merge"


def _second_approval_chat(db, chat_id="other-approval-chat"):
  """Another running chat with its own Goal and saved-card sink."""
  other = models.Chat(id=chat_id, title="Duplicate integrator", messages=[])
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


def _ask_as(client, other, sink, headers, prompt):
  register_active_sink(other.id, sink)
  try:
    return client.post(
      f"/api/chats/{other.id}/approval", json=prompt, headers=headers,
    )
  finally:
    unregister_active_sink(other.id, sink)


def test_request_approval_claims_an_unclaimed_key_in_one_call(
  client, chat, approval_run, db,
):
  saved = _ask(client, chat, approval_run, {**PROMPT, "work_key": SHARED_KEY})

  assert saved.status_code == 200, saved.text
  assert saved.json()["state"] == "waiting_for_owner"
  claim = db.query(models.AgentWorkClaim).one()
  assert (claim.work_key, claim.owner_chat_id) == (SHARED_KEY, chat.id)
  assert claim.completed_at is None and claim.released_at is None


def test_losing_approval_request_follows_the_owner_without_a_card(
  client, chat, approval_run, db,
):
  keyed = {**PROMPT, "work_key": SHARED_KEY}
  first = _ask(client, chat, approval_run, keyed)
  assert first.status_code == 200, first.text
  other, sink, headers = _second_approval_chat(db)

  follower = _ask_as(client, other, sink, headers, keyed)

  # Same result claim_agent_work returns, not an error: the turn continues.
  assert follower.status_code == 200, follower.text
  body = follower.json()
  assert (body["state"], body["owner_chat_id"]) == ("held_by_peer", chat.id)
  assert "question_id" not in body
  assert "No approval card was saved and your turn continues" in body["next_action"]
  assert db.query(models.AgentWorkClaim).count() == 1
  assert db.query(models.AgentWorkInterest).one().chat_id == other.id
  assert _row(other.id)[0] is None

  from app.agent_work_claims import finish_work
  finish_work(db, owner_id=db.query(models.Owner).first().id, chat_id=chat.id,
              work_key=SHARED_KEY, outcome="Merged as 0b44dc9d", release=False)
  done = _ask_as(client, other, sink, headers, keyed)
  assert done.status_code == 200, done.text
  assert done.json()["state"] == "completed"
  assert "do not repeat it" in done.json()["next_action"]
  assert _row(other.id)[0] is None


def approval_run_id(chat):
  return f"approval-{chat.id}"


def test_racing_approval_requests_on_one_key_save_exactly_one_card(
  client, chat, approval_run, db,
):
  """The other chat's claim lands between this request's check and insert."""
  from sqlalchemy import event
  from sqlalchemy.orm import Session
  from app.agent_work_claims import claim_work

  other, sink, headers = _second_approval_chat(db)
  owner_id = db.query(models.Owner).first().id
  raced = []

  def first_chat_wins_inside_the_gap(session, _ctx, _instances):
    if raced or not any(
      isinstance(row, models.AgentWorkClaim) and row.owner_chat_id == other.id
      for row in session.new
    ):
      return
    raced.append(True)
    with SessionLocal() as competing:
      claim_work(competing, owner_id=owner_id, chat_id=chat.id,
                 run_id=approval_run_id(chat), work_key=SHARED_KEY,
                 summary="Merge the reviewed PR")

  keyed = {**PROMPT, "work_key": SHARED_KEY}
  event.listen(Session, "before_flush", first_chat_wins_inside_the_gap)
  try:
    lost = _ask_as(client, other, sink, headers, keyed)
  finally:
    event.remove(Session, "before_flush", first_chat_wins_inside_the_gap)
  won = _ask(client, chat, approval_run, keyed)

  assert raced == [True]
  assert lost.json()["state"] == "held_by_peer", lost.text
  assert won.json()["state"] == "waiting_for_owner", won.text
  assert _row(other.id)[0] is None
  assert _row(chat.id)[0] == won.json()["question_id"]
  assert db.query(models.AgentWorkClaim).count() == 1


def test_approval_without_action_identity_is_rejected_before_card_creation(
  client, chat, approval_run,
):
  unowned = {key: value for key, value in PROMPT.items() if key != "work_key"}

  response = _ask(client, chat, approval_run, unowned)

  assert response.status_code == 422
  assert _row(chat.id)[0] is None


def test_unsaved_card_keeps_its_claim_for_the_identical_retry(
  client, chat, approval_run, monkeypatch, db,
):
  writer = get_writer()
  original = writer._persist_question_required

  def fail(*args, **kwargs):
    raise RuntimeError("test write failed")

  monkeypatch.setattr(writer, "_persist_question_required", fail)
  res = _ask(client, chat, approval_run)
  assert res.status_code == 503
  assert _row(chat.id)[0] is None
  # Admission succeeded, so exact-action ownership remains reserved for the
  # identical retry even though the card acknowledgement failed.
  claim = db.query(models.AgentWorkClaim).one()
  assert (claim.owner_chat_id, claim.released_at) == (chat.id, None)
  assert not any(b["type"] == "question" for b in approval_run[0].assistant_blocks)
  monkeypatch.setattr(writer, "_persist_question_required", original)
  assert _ask(client, chat, approval_run).status_code == 200


def test_finished_approval_remains_answerable_and_late_answer_starts_once(
  client, chat, auth, approval_run, monkeypatch,
):
  qid = _ask(client, chat, approval_run).json()["question_id"]
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


def test_immediate_answer_queues_once_and_drain_starts_it_after_current_turn(
  client, chat, auth, approval_run, monkeypatch,
):
  qid = _ask(client, chat, approval_run).json()["question_id"]
  result = _answer(client, chat, auth, qid)
  assert result.status_code == 202, result.text
  assert result.json()["status"] == "queued"
  assert result.json()["answer_turn"] == "queued"
  marker, messages, pending = _row(chat.id)
  assert marker is None
  assert len(pending) == 1 and pending[0]["continuation_reason"] == "question_answer"
  assert messages[-1]["blocks"][-1]["answers"] == {PROMPT["question"]: "Not now"}
  assert _answer(client, chat, auth, qid).status_code == 410
  assert _ask(client, chat, approval_run).json()["state"] == "answered"
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  _finish(chat, approval_run[0])
  assert len(scheduled) == 1
  assert _row(chat.id)[2] == []
  assert _row(chat.id)[0] is None


def test_failed_early_answer_keeps_the_card_open_and_does_not_queue(
  client, chat, auth, approval_run, monkeypatch,
):
  qid = _ask(client, chat, approval_run).json()["question_id"]
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


def test_approval_requires_exact_agent_run_not_plain_owner_or_foreign_chat(
  client, chat, auth, approval_run, db,
):
  assert client.post(f"/api/chats/{chat.id}/approval", json=PROMPT, headers=auth).status_code == 403
  foreign = client.post("/api/chats", json={"title": "Other"}, headers=auth).json()["id"]
  assert client.post(f"/api/chats/{foreign}/approval", json=PROMPT,
                     headers=approval_run[1]).status_code == 403
  assert _row(foreign)[0] is None
  assert db.query(models.AgentWorkClaim).count() == 0


def test_approval_rejects_missing_sink_and_superseded_run(
  client, chat, approval_run, db,
):
  sink = approval_run[0]
  sink.run_token = "another-run"
  assert _ask(client, chat, approval_run).status_code == 409
  unregister_active_sink(chat.id, sink)
  assert _ask(client, chat, approval_run).status_code == 409
  assert db.query(models.AgentWorkClaim).count() == 0


@pytest.mark.parametrize("status,keeps_card", [
  ("completed", True), ("interrupted", True), ("failed", True), ("stopped", False),
])
def test_terminal_status_preserves_owner_handoff_but_stop_cancels_it(
  client, chat, approval_run, status, keeps_card,
):
  qid = _ask(client, chat, approval_run).json()["question_id"]
  get_writer().submit(FinishRun(
    chat_id=chat.id, run_token=approval_run[0].run_token, terminal_status=status,
  )).result(timeout=5)
  assert _row(chat.id)[0] == (qid if keeps_card else None)
  assert _row(chat.id)[2] == []
  # A stale stream snapshot must not reopen an explicitly cancelled approval.
  asyncio.run(approval_run[0].finalize())
  assert _row(chat.id)[0] == (qid if keeps_card else None)


def test_native_question_cannot_overlap_an_owner_approval(client, chat, approval_run):
  qid = _ask(client, chat, approval_run).json()["question_id"]
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


def test_lost_broadcast_after_commit_recovers_same_receipt(client, chat, approval_run, monkeypatch):
  sink = approval_run[0]
  original = sink.bc.publish

  def fail(event):
    if event.get("type") == "question":
      raise RuntimeError("connection lost after commit")
    original(event)

  monkeypatch.setattr(sink.bc, "publish", fail)
  assert _ask(client, chat, approval_run).status_code == 503
  qid = _row(chat.id)[0]
  assert qid
  monkeypatch.setattr(sink.bc, "publish", original)
  assert _ask(client, chat, approval_run).json()["question_id"] == qid


def test_control_tool_to_helper_to_saved_card_returns_receipt_not_permission(
  client, chat, approval_run, monkeypatch,
):
  import io
  import json
  from tests.test_platform_tools import _control_module

  control = _control_module()
  monkeypatch.setenv("API_BASE_URL", "http://testserver")
  monkeypatch.setenv("CHAT_ID", chat.id)
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", approval_run[0].run_token)
  monkeypatch.setenv("AGENT_TOKEN", approval_run[1]["Authorization"].removeprefix("Bearer "))

  def open_request(request, timeout):
    assert timeout == 35  # save deadline, never a human-answer deadline
    response = client.post(
      request.full_url, content=request.data, headers=dict(request.header_items()),
    )
    assert response.status_code == 200, response.text
    return io.BytesIO(response.content)

  monkeypatch.setattr(control._APPROVALS, "urlopen", open_request)
  result = control._dispatch_message({
    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
    "params": {"name": "request_approval", "arguments": PROMPT},
  })["result"]
  assert not result["isError"]
  receipt = json.loads(result["content"][0]["text"])
  assert receipt["state"] == "waiting_for_owner"
  assert _row(chat.id)[0] == receipt["question_id"]
  assert "not approval" in receipt["next_action"]


def test_control_tool_returns_follower_claim_to_the_losing_chat(
  client, chat, approval_run, db, monkeypatch,
):
  """The losing request_approval is one successful call, not an error to retry."""
  import io
  import json
  from tests.test_platform_tools import _control_module

  keyed = {**PROMPT, "work_key": SHARED_KEY}
  assert _ask(client, chat, approval_run, keyed).status_code == 200
  other, sink, headers = _second_approval_chat(db)
  control = _control_module()
  monkeypatch.setenv("API_BASE_URL", "http://testserver")
  monkeypatch.setenv("CHAT_ID", other.id)
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", sink.run_token)
  monkeypatch.setenv("AGENT_TOKEN", headers["Authorization"].removeprefix("Bearer "))

  def open_request(request, timeout):
    response = client.post(
      request.full_url, content=request.data, headers=dict(request.header_items()),
    )
    assert response.status_code == 200, response.text
    return io.BytesIO(response.content)

  monkeypatch.setattr(control._APPROVALS, "urlopen", open_request)
  register_active_sink(other.id, sink)
  try:
    result = control._dispatch_message({
      "jsonrpc": "2.0", "id": 1, "method": "tools/call",
      "params": {"name": "request_approval", "arguments": keyed},
    })["result"]
  finally:
    unregister_active_sink(other.id, sink)

  assert not result["isError"]
  follower = json.loads(result["content"][0]["text"])
  assert (follower["state"], follower["owner_chat_id"]) == ("held_by_peer", chat.id)
  assert _row(other.id)[0] is None


def test_helper_rejects_unconfirmed_receipt_and_transport_failure(monkeypatch):
  import io
  from urllib.error import URLError
  from tests.test_platform_tools import _control_module

  helper = _control_module()._APPROVALS
  for name in ("API_BASE_URL", "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN"):
    monkeypatch.setenv(name, "test-value")
  monkeypatch.setenv("API_BASE_URL", "http://testserver")
  monkeypatch.setattr(helper, "urlopen", lambda *a, **kw: io.BytesIO(b'{}'))
  with pytest.raises(SystemExit, match="Invalid owner-input card receipt"):
    helper.request_approval(**PROMPT)

  def fail(*args, **kwargs):
    raise URLError("disconnected")

  monkeypatch.setattr(helper, "urlopen", fail)
  with pytest.raises(SystemExit, match="No answer or approval was granted"):
    helper.request_approval(**PROMPT)


def test_helper_preserves_bounded_deterministic_rejection_detail(monkeypatch):
  import io
  from urllib.error import HTTPError
  from tests.test_platform_tools import _control_module

  helper = _control_module()._APPROVALS
  for name in ("API_BASE_URL", "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN"):
    monkeypatch.setenv(name, "test-value")
  monkeypatch.setenv("API_BASE_URL", "http://testserver")

  def reject(request, timeout):
    return_value = HTTPError(
      request.full_url, 409, "Conflict", {},
      io.BytesIO(b'{"detail":"Owned by the integration chat; no duplicate card."}'),
    )
    raise return_value

  monkeypatch.setattr(helper, "urlopen", reject)
  with pytest.raises(SystemExit, match="Owned by the integration chat") as exc:
    helper.request_approval(**PROMPT)
  assert "owner-input card" in str(exc.value)
  assert "Fix the stated conflict" in str(exc.value)


def test_question_helper_leaves_canonicalization_to_server(monkeypatch):
  from tests.test_platform_tools import _control_module

  helper = _control_module()._APPROVALS
  captured = []
  monkeypatch.setattr(helper, "save_card", lambda kind, body: (
    captured.append((kind, body)) or {"state": "waiting_for_owner"}
  ))

  helper.request_question([{
    "question": "Which repair should I prepare?",
    "options": [{"label": "Permanent repair", "description": "Fix the cause."}],
  }])

  assert captured == [("question", {"questions": [{
    "question": "Which repair should I prepare?",
    "options": [{"label": "Permanent repair", "description": "Fix the cause."}],
  }]})]


def test_helper_surfaces_fastapi_validation_paths_without_echoing_input(monkeypatch):
  import io
  from urllib.error import HTTPError
  from tests.test_platform_tools import _control_module

  helper = _control_module()._APPROVALS
  for name in ("API_BASE_URL", "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN"):
    monkeypatch.setenv(name, "test-value")
  monkeypatch.setenv("API_BASE_URL", "http://testserver")

  def reject(request, timeout):
    raise HTTPError(
      request.full_url, 422, "Unprocessable Content", {}, io.BytesIO(json.dumps({
        "detail": [
          {"loc": ["body", "questions", 0, "header"],
           "msg": "Field required", "type": "missing",
           "input": "must-not-appear"},
          {"loc": ["body", "questions", 0, "options"],
           "msg": "List should have at most 3 items", "type": "too_long"},
          {"loc": ["body", "questions", 0, "sk-live-do-not-echo"],
           "msg": "Extra inputs are not permitted", "type": "extra_forbidden"},
        ],
      }).encode()),
    )

  monkeypatch.setattr(helper, "urlopen", reject)
  with pytest.raises(SystemExit) as exc:
    helper.request_question([{
      "id": "choice", "header": "Direction", "question": "Which?",
      "options": [],
    }])
  message = str(exc.value)
  assert "questions[0].header: Field required" in message
  assert "questions[0].options: List should have at most 3 items" in message
  assert "questions[0].<field>: Extra inputs are not permitted" in message
  assert "sk-live-do-not-echo" not in message
  assert "must-not-appear" not in message


def test_helper_preserves_structured_restart_rejection_detail(monkeypatch):
  import io
  from urllib.error import HTTPError
  from tests.test_platform_tools import _control_module

  helper = _control_module()._APPROVALS
  for name in ("API_BASE_URL", "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN"):
    monkeypatch.setenv(name, "test-value")
  monkeypatch.setenv("API_BASE_URL", "http://testserver")

  def reject(request, timeout):
    raise HTTPError(
      request.full_url, 409, "Conflict", {}, io.BytesIO(json.dumps({
        "detail": {
          "code": "restart_source_must_be_committed",
          "message": "Möbius could not bind a Restart card to exact committed source.",
        },
      }).encode()),
    )

  monkeypatch.setattr(helper, "urlopen", reject)
  with pytest.raises(SystemExit, match="restart_source_must_be_committed") as exc:
    helper.request_restart()
  assert "exact committed source" in str(exc.value)


def test_stop_winning_answer_admission_does_not_queue_a_continuation(
  client, chat, auth, approval_run, monkeypatch,
):
  from contextlib import asynccontextmanager
  from app import chat_queue
  from app.chat_writer import await_ack

  qid = _ask(client, chat, approval_run).json()["question_id"]
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


@pytest.mark.parametrize("route", ["question", "approval"])
def test_saved_owner_cards_share_blocking_marker_and_terminal_receipt(
  client, chat, auth, approval_run, route,
):
  payload = PROMPT if route == "approval" else {"questions": [{
    "id": "direction", "header": "Direction", **QUESTION_PROMPT,
  }]}
  response = client.post(f"/api/chats/{chat.id}/{route}",
                         json=payload, headers=approval_run[1])
  assert response.status_code == 200, response.text
  receipt = response.json()
  # The receipt still states the turn's terminal contract; only the reason
  # changed — the response is CUT at the card rather than merely asked to end.
  assert "The turn is over" in receipt["next_action"]
  assert "nothing further can be delivered" in receipt["next_action"]
  assert questions.get(chat.id) is None
  _finish(chat, approval_run[0])
  assert _row(chat.id)[0] == receipt["question_id"]
  blocked = client.post(f"/api/chats/{chat.id}/messages", headers=auth,
                        json={"content": "Skip past the card"})
  assert blocked.status_code == 409
  assert _row(chat.id)[0] == receipt["question_id"]


def test_saved_questions_keep_multiple_choices_and_retry_identity(client, chat, approval_run):
  payload = {"questions": [
    {"id": "direction", "header": "Direction", **QUESTION_PROMPT},
    {"id": "timing", "header": "Timing", "question": "When?", "options": []},
  ]}
  first = client.post(f"/api/chats/{chat.id}/question", json=payload, headers=approval_run[1])
  again = client.post(f"/api/chats/{chat.id}/question", json=payload, headers=approval_run[1])
  assert first.status_code == 200, first.text
  assert first.json() == again.json()
  saved = _row(chat.id)[1][-1]["blocks"][-1]["questions"]
  assert saved[0]["options"] == [
    {**option, "id": str(index)}
    for index, option in enumerate(QUESTION_PROMPT["options"])
  ]
  assert saved[1] == payload["questions"][1]


def test_saved_questions_canonicalize_card_only_metadata_at_route_boundary(
  client, chat, approval_run,
):
  payload = {"questions": [
    {"question": "Which direction?"},
    {"question": "When?", "options": []},
  ]}
  response = client.post(
    f"/api/chats/{chat.id}/question", json=payload, headers=approval_run[1],
  )
  assert response.status_code == 200, response.text
  assert _row(chat.id)[1][-1]["blocks"][-1]["questions"] == [
    {
      "id": "question-1", "header": "Question 1",
      "question": "Which direction?", "options": [],
    },
    {
      "id": "question-2", "header": "Question 2",
      "question": "When?", "options": [],
    },
  ]


def test_saved_single_question_uses_neutral_default_heading(
  client, chat, approval_run,
):
  response = client.post(
    f"/api/chats/{chat.id}/question",
    json={"questions": [{"question": "Which direction?"}]},
    headers=approval_run[1],
  )
  assert response.status_code == 200, response.text
  assert _row(chat.id)[1][-1]["blocks"][-1]["questions"] == [{
    "id": "question-1", "header": "Your choice",
    "question": "Which direction?", "options": [],
  }]


def test_question_defaults_cannot_collide_with_an_explicit_id(
  client, chat, approval_run,
):
  response = client.post(
    f"/api/chats/{chat.id}/question",
    json={"questions": [
      {"id": "question-2", "question": "First?"},
      {"question": "Second?"},
    ]},
    headers=approval_run[1],
  )
  assert response.status_code == 422
  assert "question ids must be distinct" in response.text


def test_question_tool_saves_receipt_and_never_returns_a_default_answer(monkeypatch):
  from tests.test_platform_tools import _control_module
  control = _control_module()
  expected = {"state": "waiting_for_owner", "question_id": "q1", "next_action": "End"}
  captured = []
  monkeypatch.setattr(control._APPROVALS, "request_question", lambda questions: (
    captured.append(questions) or expected
  ))
  payload = [{"id": "choice", "header": "Choice", **QUESTION_PROMPT}]
  assert control._call_request_question({"questions": payload}) == expected
  assert captured == [payload]
  assert "answers" not in expected


def test_prose_a_provider_races_after_a_saved_card_is_never_discarded(
  client, chat, approval_run,
):
  """Generation is cut at the card, but anything that still arrives stays.

  Möbius removes the CAUSE of post-card prose (Claude's card-end hook refuses
  the next model request; other paths interrupt) and never the EVIDENCE: output
  a provider did produce remains part of both session histories, so a future
  leak is visible instead of masked.
  """
  sink = approval_run[0]
  assert sink.publish({"type": "text", "content": "Reading the contract first."})
  saved = _ask(client, chat, approval_run)
  assert sink.assistant_blocks[-1]["type"] == "question"

  log_before = len(sink.bc.event_log)
  assert sink.publish({"type": "text", "content": "Card saved — waiting on you."})
  assert sink.publish({"type": "thinking", "content": "Should I say more?"})
  assert [b["type"] for b in sink.assistant_blocks] == [
    "text", "question", "text", "thinking",
  ]
  assert len(sink.bc.event_log) == log_before + 2
  asyncio.run(sink.finalize())
  persisted = _row(chat.id)[1][-1]["blocks"]
  assert [b["type"] for b in persisted] == [
    "text", "question", "text", "thinking",
  ]
  assert persisted[1]["question_id"] == saved.json()["question_id"]


def test_completed_card_receipt_ends_the_turn_then_preserves_the_raced_tail(
  client, chat, approval_run,
):
  """The two owner-card guarantees hold at the same receipt boundary.

  A successful completed receipt synchronously claims the runner's card end
  (the fallback path, for a runner that did not already end itself at the
  card), while provider events already in flight remain live and durable.
  """
  from app.runner_registry import registry

  sink = approval_run[0]
  handle = _FakeCardHandle(chat.id)
  registry.register(handle)
  try:
    assert sink.publish({
      "type": "tool_start", "tool": "Bash", "input": "owner helper",
      "tool_use_id": "owner-helper-race",
    })
    saved = _ask(client, chat, approval_run)
    qid = saved.json()["question_id"]

    async def deliver_receipt_and_raced_tail():
      assert sink.publish({
        "type": "tool_output", "content": saved.text,
        "output_complete": True, "output_exit_code": 0,
        "tool_use_id": "owner-helper-race",
      })
      assert handle.finishes == 1

      log_before = len(sink.bc.event_log)
      assert sink.publish({"type": "text", "content": "Already emitted tail."})
      assert sink.publish({
        "type": "thinking", "content": "Already emitted trace.",
      })
      assert len(sink.bc.event_log) == log_before + 2
      await sink.finalize()

    asyncio.run(deliver_receipt_and_raced_tail())

    persisted = _row(chat.id)[1][-1]["blocks"]
    assert [block["type"] for block in persisted] == [
      "tool", "question", "text", "thinking",
    ]
    assert persisted[0]["owner_card_question_id"] == qid
    assert persisted[1]["question_id"] == qid
    assert persisted[2]["content"] == "Already emitted tail."
  finally:
    registry.unregister(chat.id, handle.kind)


def test_card_end_is_gated_on_the_exact_card_this_turn_saved(
  client, chat, approval_run,
):
  """A receipt-shaped tool result alone must never end a turn.

  `has_continuation_card` is the single identity gate both card-end paths ask
  (the sink's fallback signal here, Claude's PostToolUse hook in its runner), so
  a tool that merely printed an older card's JSON leaves the turn running.
  """
  from app.runner_registry import registry

  sink = approval_run[0]
  handle = _FakeCardHandle(chat.id)
  registry.register(handle)
  try:
    saved = _ask(client, chat, approval_run)
    qid = saved.json()["question_id"]
    assert sink.has_continuation_card(qid) is True
    assert sink.has_continuation_card("card-from-last-week") is False

    stale = json.dumps({
      "state": "waiting_for_owner", "question_id": "card-from-last-week",
      "next_action": "End now",
    })
    assert sink.publish({
      "type": "tool_output", "content": stale,
      "output_complete": True, "output_exit_code": 0,
      "tool_use_id": "stale-echo",
    })
    assert handle.finishes == 0
    # The stale output is still recorded — no filtering, only no cut.
    assert sink.assistant_blocks[-1].get("owner_card_question_id") is None
  finally:
    registry.unregister(chat.id, handle.kind)


def test_a_streamed_messages_tail_still_lands_in_its_own_block(
  client, chat, approval_run,
):
  """A pre-card message tail reattaches to its original block while a distinct
  raced message remains visible after the card instead of being discarded."""
  sink = approval_run[0]
  assert sink.publish(
    {"type": "text", "content": "Reading it", "text_item_id": "msg-1"},
  )
  _ask(client, chat, approval_run)
  assert sink.publish(
    {"type": "text", "content": " first.", "text_item_id": "msg-1"},
  )
  assert [b["type"] for b in sink.assistant_blocks] == ["text", "question"]
  assert sink.assistant_blocks[0]["content"] == "Reading it first."
  # A different message item is genuinely later, but must remain visible so
  # Möbius and the provider session never diverge while the interrupt drains.
  assert sink.publish(
    {"type": "text", "content": " Done.", "text_item_id": "msg-2"},
  )
  assert [b["type"] for b in sink.assistant_blocks] == [
    "text", "question", "text",
  ]
  assert sink.assistant_blocks[-1]["content"] == " Done."


def test_native_question_keeps_recording_post_card_prose(chat, approval_run):
  """Only a continuation card is terminal. A native AskUserQuestion is
  mid-turn, so prose after that card stays ordinary transcript."""
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
