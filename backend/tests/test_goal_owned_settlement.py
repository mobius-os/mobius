"""Terminal settlement keeps every unfinished Goal under one real owner."""

import pytest

from app import models
from app.chat_writer import PromotePending, get_writer
from app.run_state import GOAL_HANDOFF_REASON


UNFINISHED = {"version": 1, "tasks": [{
  "id": "finish", "title": "Finish", "status": "running",
  "depends_on": [],
}]}
SETTLED = {"version": 1, "tasks": [{
  "id": "finish", "title": "Finish", "status": "completed",
  "depends_on": [],
}]}


def _add_goal_run(
  db,
  chat,
  run_id="goal-run",
  *,
  goal_id="goal-run",
  plan=UNFINISHED,
):
  db.add(models.ChatRun(
    id=run_id,
    root_run_id=run_id,
    chat_id=chat.id,
    status="running",
    provider="codex",
    goal_objective="Finish the work",
    goal_id=goal_id,
    goal_plan_json=plan,
  ))
  db.commit()


def _terminal_promote(
  chat_id,
  ending_run_token,
  *,
  run_token="successor",
  ending_status="completed",
):
  return get_writer().submit(PromotePending(
    chat_id=chat_id,
    run_token=run_token,
    ending_run_token=ending_run_token,
    ending_status=ending_status,
    allow_goal_continuation=True,
  )).result(timeout=5)


def _automatic_continuation(goal_id, cid="existing-goal-executor"):
  return {
    "role": "user",
    "content": "continue",
    "kind": "continuation",
    "continuation_reason": GOAL_HANDOFF_REASON,
    "goal_id": goal_id,
    "cid": cid,
    "ts": 2,
  }


def test_clean_terminal_continues_unfinished_goal_without_an_owner(db, chat):
  _add_goal_run(db, chat)

  result = _terminal_promote(chat.id, "goal-run")

  assert result["promoted"] is not None
  assert result["promoted"]["content"] == "continue"
  assert result["promoted"]["continuation_reason"] == GOAL_HANDOFF_REASON
  assert result["promoted"]["_goal_id"] == "goal-run"
  db.expire_all()
  ending = db.get(models.ChatRun, "goal-run")
  successor = db.get(models.ChatRun, "successor")
  assert ending.status == "completed"
  assert successor.status == "running"
  assert successor.goal_id == "goal-run"
  assert db.get(models.Chat, chat.id).pending_messages == []


def test_terminal_does_not_continue_a_settled_or_failed_goal(db, chat):
  _add_goal_run(db, chat, plan=SETTLED)
  settled = _terminal_promote(chat.id, "goal-run")
  assert settled["promoted"] is None

  db.get(models.ChatRun, "goal-run").status = "completed"
  db.commit()
  _add_goal_run(db, chat, run_id="failed-goal")
  failed = _terminal_promote(
    chat.id,
    "failed-goal",
    run_token="must-not-start",
    ending_status="failed",
  )
  assert failed["promoted"] is None
  assert db.get(models.ChatRun, "must-not-start") is None


@pytest.mark.parametrize("outcome", ["met", "expired", "failed"])
def test_finished_wait_keeps_goal_ownership_until_result_admission(db, chat, outcome):
  """A sweep can settle a check just before the declaring turn finishes."""
  from app import chat_waits

  _add_goal_run(db, chat)
  wait = chat_waits.declare_wait(
    db, chat_id=chat.id, created_by_run_id="goal-run",
    description="Observe the external gate", kind="timer", delay_secs=60,
  )
  wait.status = outcome
  db.commit()

  result = _terminal_promote(chat.id, "goal-run")

  assert result["promoted"] is None, "Goal raced its already-owned Wait result"
  db.expire_all()
  assert db.get(models.ChatRun, "successor") is None
  assert db.get(models.ChatWait, wait.id).resume_delivered_at is None


@pytest.mark.parametrize("case", ["delivered", "cancelled", "different_goal"])
def test_wait_without_exact_outstanding_delivery_cannot_hold_goal(db, chat, case):
  from app import chat_waits
  from app.timeutil import now_naive_utc

  _add_goal_run(db, chat)
  source_id = "goal-run"
  if case == "different_goal":
    source_id = "other-goal"
    _add_goal_run(db, chat, run_id=source_id, goal_id=source_id)
  wait = chat_waits.declare_wait(
    db, chat_id=chat.id, created_by_run_id=source_id,
    description="Observe the gate", kind="timer", delay_secs=60,
  )
  wait.status = "cancelled" if case == "cancelled" else "met"
  if case == "delivered":
    wait.resume_delivered_at = now_naive_utc()
  db.commit()

  result = _terminal_promote(chat.id, "goal-run")

  assert result["promoted"]["continuation_reason"] == GOAL_HANDOFF_REASON
  assert result["promoted"]["_goal_id"] == "goal-run"


@pytest.mark.asyncio
async def test_wait_delivery_after_terminal_gap_starts_only_its_exact_goal_run(db, chat, monkeypatch):
  from app import chat as chat_mod, chat_waits

  _add_goal_run(db, chat)
  wait = chat_waits.declare_wait(
    db, chat_id=chat.id, created_by_run_id="goal-run",
    description="External gate finished", kind="timer", delay_secs=60,
  )
  wait.status = "met"
  db.commit()
  assert _terminal_promote(chat.id, "goal-run")["promoted"] is None
  db.expire_all()
  db.get(models.ChatRun, "goal-run").status = "completed"
  db.commit()
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))

  assert await chat_waits._deliver_resume(wait.id) is True
  assert await chat_waits._deliver_resume(wait.id) is False

  db.expire_all()
  assert len(scheduled) == 1
  successor = db.get(models.ChatRun, f"wait-resume-{wait.id}")
  assert successor.goal_id == "goal-run"
  assert db.get(models.ChatRun, "successor") is None
  assert db.get(models.ChatWait, wait.id).resume_delivered_at is not None
  assert all(message.get("continuation_reason") != GOAL_HANDOFF_REASON
             for message in db.get(models.Chat, chat.id).messages)


def test_existing_exact_goal_executor_is_not_duplicated(db, chat):
  _add_goal_run(db, chat)
  chat.pending_messages = [_automatic_continuation("goal-run")]
  db.commit()

  result = _terminal_promote(chat.id, "goal-run")

  assert result["promoted"]["cid"] == "existing-goal-executor"
  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  assert saved.pending_messages == []
  assert sum(
    message.get("continuation_reason") == GOAL_HANDOFF_REASON
    for message in saved.messages
  ) == 1


def test_question_answer_supersedes_automatic_goal_executor(db, chat):
  _add_goal_run(db, chat)
  chat.pending_messages = [
    _automatic_continuation("goal-run"),
    {
      "role": "user",
      "content": "Proceed with the reviewed choice",
      "kind": "continuation",
      "continuation_reason": "question_answer",
      "cid": "question-answer",
      "ts": 3,
    },
  ]
  db.commit()

  result = _terminal_promote(chat.id, "goal-run")

  assert result["promoted"]["cid"] == "question-answer"
  assert result["promoted"]["content"] == "Proceed with the reviewed choice"
  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  assert saved.pending_messages == []
  assert all(
    message.get("continuation_reason") != GOAL_HANDOFF_REASON
    for message in saved.messages
  )


def test_unrelated_queued_turn_cannot_orphan_goal_executor(db, chat):
  _add_goal_run(db, chat)
  chat.pending_messages = [{
    "role": "user", "content": "An unrelated follow-up", "cid": "owner",
    "ts": 1,
  }]
  db.commit()

  owner_turn = _terminal_promote(
    chat.id, "goal-run", run_token="owner-turn",
  )
  assert owner_turn["promoted"]["cid"] == "owner"
  db.expire_all()
  queued = db.get(models.Chat, chat.id).pending_messages
  assert len(queued) == 1
  assert queued[0]["continuation_reason"] == GOAL_HANDOFF_REASON

  goal_turn = _terminal_promote(
    chat.id, "owner-turn", run_token="goal-successor",
  )
  assert goal_turn["promoted"]["continuation_reason"] == GOAL_HANDOFF_REASON
  assert goal_turn["promoted"]["_goal_id"] == "goal-run"
  db.expire_all()
  assert db.get(models.ChatRun, "goal-successor").goal_id == "goal-run"


@pytest.mark.asyncio
async def test_complete_turn_schedules_the_terminal_goal_executor(
  db, chat, monkeypatch,
):
  from app import chat as chat_mod, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink
  from app.memory_recall import EMPTY_RECALL_BINDING

  _add_goal_run(db, chat)
  broadcast = create_broadcast(chat.id)
  sink = ChatEventSink(
    broadcast,
    chat.id,
    run_token="goal-run",
    recall_binding=EMPTY_RECALL_BINDING,
  )
  sink.publish({"type": "text", "content": "Progress is saved."})
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation", lambda **kwargs: scheduled.append(kwargs),
  )
  monkeypatch.setattr(chat_mod, "_publish_chat_run_finished", lambda *_: None)
  try:
    disposition = await chat_mod._complete_turn(
      bc=broadcast,
      sink=sink,
      db=db,
      chat_id=chat.id,
      run_gen=None,
      provider_id="codex",
      cost_usd=0,
      close_browser=False,
    )
  finally:
    remove_broadcast(chat.id)

  assert disposition is chat_queue.TerminalDisposition.CONTINUATION_PROMOTED
  assert len(scheduled) == 1
  assert scheduled[0]["next_user"]["continuation_reason"] == GOAL_HANDOFF_REASON
  successor = db.get(models.ChatRun, scheduled[0]["run_token"])
  assert successor is not None and successor.goal_id == "goal-run"


@pytest.mark.asyncio
async def test_no_progress_across_two_terminals_stops_at_a_saved_owner_question(
  db, chat, monkeypatch,
):
  """A clean continuation cannot recursively manufacture provider turns."""
  from app import chat as chat_mod, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink
  from app.memory_recall import EMPTY_RECALL_BINDING

  _add_goal_run(db, chat)
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation", lambda **kwargs: scheduled.append(kwargs),
  )
  monkeypatch.setattr(chat_mod, "_publish_chat_run_finished", lambda *_: None)

  first_broadcast = create_broadcast(chat.id)
  first_sink = ChatEventSink(
    first_broadcast,
    chat.id,
    run_token="goal-run",
    recall_binding=EMPTY_RECALL_BINDING,
  )
  first_sink.publish({"type": "text", "content": "Work remains."})
  first = await chat_mod._complete_turn(
    bc=first_broadcast,
    sink=first_sink,
    db=db,
    chat_id=chat.id,
    run_gen=None,
    provider_id="codex",
    cost_usd=0,
    close_browser=False,
  )
  remove_broadcast(chat.id)
  assert first is chat_queue.TerminalDisposition.CONTINUATION_PROMOTED
  assert len(scheduled) == 1
  assert scheduled[0]["next_user"]["goal_settled_count"] == 0

  continuation_run = scheduled[0]["run_token"]
  second_broadcast = create_broadcast(chat.id)
  second_sink = ChatEventSink(
    second_broadcast,
    chat.id,
    run_token=continuation_run,
    recall_binding=EMPTY_RECALL_BINDING,
  )
  second_sink.publish({
    "type": "text", "content": "No plan task changed status.",
  })
  try:
    second = await chat_mod._complete_turn(
      bc=second_broadcast,
      sink=second_sink,
      db=db,
      chat_id=chat.id,
      run_gen=None,
      provider_id="codex",
      cost_usd=0,
      close_browser=False,
    )
  finally:
    remove_broadcast(chat.id)

  assert second is chat_queue.TerminalDisposition.QUESTION_PARKED
  assert len(scheduled) == 1
  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  assert saved.pending_question_id == f"goal-handoff-{continuation_run}"
  card = saved.messages[-1]["blocks"][-1]
  assert card["type"] == "question"
  assert card["response_mode"] == "continuation"
  assert "without enough plan progress" in card["questions"][0]["question"]
  assert db.get(models.ChatRun, continuation_run).status == "interrupted"


@pytest.mark.asyncio
async def test_provider_free_terminal_does_not_loop_an_unfinished_goal(
  db, chat, monkeypatch,
):
  from app import chat as chat_mod, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink
  from app.memory_recall import EMPTY_RECALL_BINDING

  _add_goal_run(db, chat)
  broadcast = create_broadcast(chat.id)
  sink = ChatEventSink(
    broadcast,
    chat.id,
    run_token="goal-run",
    recall_binding=EMPTY_RECALL_BINDING,
  )
  sink.publish({"type": "text", "content": "Connect an agent to continue."})
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation", lambda **kwargs: scheduled.append(kwargs),
  )
  monkeypatch.setattr(chat_mod, "_publish_chat_run_finished", lambda *_: None)
  try:
    disposition = await chat_mod._complete_turn(
      bc=broadcast,
      sink=sink,
      db=db,
      chat_id=chat.id,
      run_gen=None,
      provider_id="codex",
      cost_usd=0,
      close_browser=False,
      provider_free=True,
    )
  finally:
    remove_broadcast(chat.id)

  assert disposition is chat_queue.TerminalDisposition.PROVIDER_FREE_COMPLETED
  assert scheduled == []
