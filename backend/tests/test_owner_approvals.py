"""Owner approval travels through real saved cards and the ordinary answer queue."""

import asyncio
from datetime import timedelta

import pytest

from app import auth as auth_mod, chat as chat_mod, models, questions
from app.broadcast import create_broadcast
from app.chat_event_sink import ChatEventSink, register_active_sink, unregister_active_sink
from app.chat_writer import Barrier, FinishRun, StartTurn, get_writer
from app.database import SessionLocal
from app.memory_recall import EMPTY_RECALL_BINDING
from app.routes import chats_stream


PROMPT = {
  "question": "Restart to activate the tested change? This interrupts active turns.",
  "options": [
    {"label": "Not now", "description": "Leave the change pending."},
    {"label": "Restart now", "description": "Interrupt active turns to activate it."},
  ],
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
                       recall_binding=EMPTY_RECALL_BINDING)
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


def _finish(chat, sink):
  async def finish():
    with SessionLocal() as db:
      return await chat_mod._complete_turn(
        bc=sink.bc, sink=sink, db=db, chat_id=chat.id, run_gen=None,
        provider_id="claude", cost_usd=0, close_browser=False,
      )
  return asyncio.run(finish())


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
  assert block["questions"][0]["options"] == PROMPT["options"]
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


def test_creation_failure_has_no_receipt_or_orphan_card(
  client, chat, approval_run, monkeypatch,
):
  writer = get_writer()
  original = writer._persist_question_required

  def fail(*args, **kwargs):
    raise RuntimeError("test write failed")

  monkeypatch.setattr(writer, "_persist_question_required", fail)
  res = _ask(client, chat, approval_run)
  assert res.status_code == 503
  assert _row(chat.id)[0] is None
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
  client, chat, auth, approval_run,
):
  assert client.post(f"/api/chats/{chat.id}/approval", json=PROMPT, headers=auth).status_code == 403
  foreign = client.post("/api/chats", json={"title": "Other"}, headers=auth).json()["id"]
  assert client.post(f"/api/chats/{foreign}/approval", json=PROMPT,
                     headers=approval_run[1]).status_code == 403
  assert _row(foreign)[0] is None


def test_approval_rejects_missing_sink_and_superseded_run(client, chat, approval_run):
  sink = approval_run[0]
  sink.run_token = "another-run"
  assert _ask(client, chat, approval_run).status_code == 409
  unregister_active_sink(chat.id, sink)
  assert _ask(client, chat, approval_run).status_code == 409


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


def test_helper_rejects_unconfirmed_receipt_and_transport_failure(monkeypatch):
  import io
  from urllib.error import URLError
  from tests.test_platform_tools import _control_module

  helper = _control_module()._APPROVALS
  for name in ("API_BASE_URL", "AGENT_TOKEN", "CHAT_ID", "MOBIUS_RUN_TOKEN"):
    monkeypatch.setenv(name, "test-value")
  monkeypatch.setenv("API_BASE_URL", "http://testserver")
  monkeypatch.setattr(helper, "urlopen", lambda *a, **kw: io.BytesIO(b'{}'))
  with pytest.raises(SystemExit, match="Invalid approval receipt"):
    helper.request_approval(**PROMPT)

  def fail(*args, **kwargs):
    raise URLError("disconnected")

  monkeypatch.setattr(helper, "urlopen", fail)
  with pytest.raises(SystemExit, match="No approval was granted"):
    helper.request_approval(**PROMPT)


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
    "id": "direction", "header": "Direction", **PROMPT,
  }]}
  response = client.post(f"/api/chats/{chat.id}/{route}",
                         json=payload, headers=approval_run[1])
  assert response.status_code == 200, response.text
  receipt = response.json()
  assert "without further text or tools" in receipt["next_action"]
  assert questions.get(chat.id) is None
  _finish(chat, approval_run[0])
  assert _row(chat.id)[0] == receipt["question_id"]
  blocked = client.post(f"/api/chats/{chat.id}/messages", headers=auth,
                        json={"content": "Skip past the card"})
  assert blocked.status_code == 409
  assert _row(chat.id)[0] == receipt["question_id"]


def test_saved_questions_keep_multiple_choices_and_retry_identity(client, chat, approval_run):
  payload = {"questions": [
    {"id": "direction", "header": "Direction", **PROMPT},
    {"id": "timing", "header": "Timing", "question": "When?", "options": []},
  ]}
  first = client.post(f"/api/chats/{chat.id}/question", json=payload, headers=approval_run[1])
  again = client.post(f"/api/chats/{chat.id}/question", json=payload, headers=approval_run[1])
  assert first.status_code == 200, first.text
  assert first.json() == again.json()
  assert _row(chat.id)[1][-1]["blocks"][-1]["questions"] == payload["questions"]


def test_question_tool_saves_receipt_and_never_returns_a_default_answer(monkeypatch):
  from tests.test_platform_tools import _control_module
  control = _control_module()
  expected = {"state": "waiting_for_owner", "question_id": "q1", "next_action": "End"}
  captured = []
  monkeypatch.setattr(control._APPROVALS, "request_question", lambda questions: (
    captured.append(questions) or expected
  ))
  payload = [{"id": "choice", "header": "Choice", **PROMPT}]
  assert control._call_request_question({"questions": payload}) == expected
  assert captured == [payload]
  assert "answers" not in expected
