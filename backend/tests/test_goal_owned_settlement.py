"""Terminal settlement keeps every unfinished Goal under one real owner."""

from tests.goal_fixtures import goal_run as make_goal_run

import pytest

from app import models
from app.chat_writer import ClearPending, PromotePending, get_writer
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
  if not chat.messages:
    chat.messages = [{
      "role": "user", "content": "Finish the work", "cid": "owner-request",
      "ts": 1,
    }]
  db.add(make_goal_run(db,
    id=run_id,
    root_run_id=run_id,
    chat_id=chat.id,
    status="running",
    provider="codex",
    goal_objective="Finish the work",
    goal_id=goal_id,
    goal_plan_json=plan,
    goal_plan_revision=1,
    goal_plan_revision_at_admission=0,
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
  assert "Reconcile the durable plan" in result["promoted"]["content"]
  assert result["promoted"]["_messages"] == []
  assert result["promoted"]["continuation_reason"] == GOAL_HANDOFF_REASON
  assert result["promoted"]["_goal_id"] == "goal-run"
  db.expire_all()
  ending = db.get(models.ChatRun, "goal-run")
  successor = db.get(models.ChatRun, "successor")
  assert ending.status == "completed"
  assert successor.status == "running"
  assert successor.goal_id == "goal-run"
  saved = db.get(models.Chat, chat.id)
  assert saved.pending_messages == []
  assert all(
    row.get("continuation_reason") != GOAL_HANDOFF_REASON
    for row in saved.messages
  )


def test_terminal_does_not_continue_a_settled_or_failed_goal(db, chat):
  _add_goal_run(db, chat, plan=SETTLED)
  from app.goals import update_goal_record
  goal = db.get(models.ChatGoal, "goal-run")
  update_goal_record(
    db, db.get(models.ChatRun, "goal-run"), goal, goal.revision,
    result="Verified",
  )
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
async def test_wait_delivery_after_terminal_gap_starts_only_its_exact_make_goal_run(db, chat, monkeypatch):
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
  # Scheduling is not delivery; the resume turn latches it at admission.
  assert chat_waits.claim_admitted_wait_result(chat.id, f"wait-resume-{wait.id}")
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
  assert all(
    message.get("continuation_reason") != GOAL_HANDOFF_REASON
    for message in saved.messages
  )


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
  assert queued[0]["hidden"] is True

  goal_turn = _terminal_promote(
    chat.id, "owner-turn", run_token="goal-successor",
  )
  assert goal_turn["promoted"]["continuation_reason"] == GOAL_HANDOFF_REASON
  assert goal_turn["promoted"]["_goal_id"] == "goal-run"
  db.expire_all()
  assert db.get(models.ChatRun, "goal-successor").goal_id == "goal-run"


def test_stop_cleanup_retires_hidden_goal_control_without_resending_it(db, chat):
  chat.pending_messages = [{
    **_automatic_continuation("goal-run"),
    "hidden": True,
  }]
  db.commit()

  result = get_writer().submit(ClearPending(
    chat_id=chat.id,
  )).result(timeout=5)

  assert result["cleared_cids"] == []
  db.expire_all()
  assert db.get(models.Chat, chat.id).pending_messages == []


@pytest.mark.asyncio
async def test_complete_turn_schedules_the_terminal_goal_executor(
  db, chat, monkeypatch,
):
  from app import chat as chat_mod, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink

  _add_goal_run(db, chat)
  broadcast = create_broadcast(chat.id)
  sink = ChatEventSink(
    broadcast,
    chat.id,
    run_token="goal-run",
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
async def test_zero_legacy_allowance_does_not_interrupt_authorized_work(
  db, chat, monkeypatch,
):
  """Owner-authorized work continues without a turn-count question."""
  from app import chat as chat_mod, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink

  _add_goal_run(db, chat)
  db.commit()
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
  assert scheduled[0]["next_user"]["goal_plan_revision"] == 1
  db.expire_all()

  continuation_run = scheduled[0]["run_token"]
  second_broadcast = create_broadcast(chat.id)
  second_sink = ChatEventSink(
    second_broadcast,
    chat.id,
    run_token=continuation_run,
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
  assert card["questions"][0]["header"] == "Goal needs reconciliation"
  assert "without handing off this Goal" in card["questions"][0]["question"]
  assert db.get(models.ChatRun, continuation_run).status == "interrupted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ("actor", "expected_rollover"),
  [
    ("owner", True),
    ("same_chat_agent", False),
    ("cross_chat_agent", False),
    ("app", False),
    ("hidden_system", False),
  ],
)
async def test_only_visible_owner_steer_reauthorizes_one_goal_rollover(
  db, chat, monkeypatch, actor, expected_rollover,
):
  """An in-turn owner question returns to work without weakening loop bounds."""
  from app import chat as chat_mod, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink

  _add_goal_run(db, chat)
  root = db.get(models.ChatRun, "goal-run")
  root.goal_plan_revision_at_admission = db.get(
    models.ChatGoal, "goal-run",
  ).revision
  db.commit()
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation", lambda **kwargs: scheduled.append(kwargs),
  )
  monkeypatch.setattr(chat_mod, "_publish_chat_run_finished", lambda *_: None)

  broadcast = create_broadcast(chat.id)
  sink = ChatEventSink(
    broadcast,
    chat.id,
    run_token="goal-run",
  )
  sink.publish({"type": "text", "content": "Working on the saved plan."})
  steer = {
    "role": "user",
    "content": "Explain this part before continuing.",
    "cid": f"{actor}-steer",
    "ts": 2,
    **(
      {"_owner_authored": True}
      if actor == "owner" else
      {"_initiated_by_app_id": 42}
      if actor == "app" else
      {"hidden": True, "kind": "continuation"}
      if actor == "hidden_system" else {}
    ),
  }
  await sink.commit_steer_cut(steer, [])
  sink.publish({"type": "text", "content": "Here is the explanation."})
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

  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  if expected_rollover:
    assert disposition is chat_queue.TerminalDisposition.CONTINUATION_PROMOTED
    assert len(scheduled) == 1
    assert scheduled[0]["next_user"]["continuation_reason"] == GOAL_HANDOFF_REASON
    assert saved.pending_question_id is None
  else:
    assert disposition is chat_queue.TerminalDisposition.QUESTION_PARKED
    assert scheduled == []
    assert saved.pending_question_id == "goal-handoff-goal-run"


@pytest.mark.asyncio
@pytest.mark.parametrize("card_saved", [True, False])
async def test_only_saved_owner_question_prevents_terminal_goal_fallback_question(
  db, chat, monkeypatch, card_saved,
):
  """Only a durable card owns the unfinished Goal; failed saves cannot strand it."""
  from app import chat as chat_mod, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink
  from app.goal_plans import goal_plan_revision

  _add_goal_run(db, chat)
  root = db.get(models.ChatRun, "goal-run")
  root.goal_plan_revision_at_admission = goal_plan_revision(db, chat.id, root.goal_id)
  db.commit()
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation", lambda **kwargs: scheduled.append(kwargs),
  )
  monkeypatch.setattr(chat_mod, "_publish_chat_run_finished", lambda *_: None)

  broadcast = create_broadcast(chat.id)
  sink = ChatEventSink(
    broadcast,
    chat.id,
    run_token="goal-run",
  )
  sink.publish({"type": "text", "content": "The reviewed batch is ready."})
  question = {
    "type": "question",
    "question_id": "publish-approval",
    "response_mode": "continuation",
    "questions": [{
      "id": "approval",
      "header": "Approval",
      "question": "Publish the reviewed batch?",
      "options": [{
        "label": "Publish",
        "description": "Publish only the reviewed heads.",
      }],
    }],
  }
  try:
    if card_saved:
      await sink.publish_question(question)
    else:
      from app import chat_writer

      def fail_question_commit(*_args, **_kwargs):
        raise RuntimeError("Question save failed")

      with monkeypatch.context() as patch:
        patch.setattr(
          chat_writer.ChatWriterActor, "_persist_question_required", fail_question_commit,
        )
        with pytest.raises(RuntimeError, match="Question save failed"):
          await sink.publish_question(question)
    assert sink.has_open_continuation_card() is card_saved
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

  assert disposition is chat_queue.TerminalDisposition.QUESTION_PARKED
  assert scheduled == []
  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  expected_question = "publish-approval" if card_saved else "goal-handoff-goal-run"
  assert saved.pending_question_id == expected_question
  cards = [
    block
    for message in saved.messages
    for block in message.get("blocks") or []
    if block.get("type") == "question"
  ]
  assert [card.get("question_id") for card in cards] == [expected_question]
  assert not any(
    block.get("type") == "error"
    for message in saved.messages
    for block in message.get("blocks") or []
  )


@pytest.mark.parametrize(("block", "expected"), [
  ({"type": "text", "content": "No owner handoff"}, False),
  ({"type": "question", "question_id": "native"}, False),
  ({"type": "question", "response_mode": "continuation"}, True),
  ({"type": "question", "response_mode": "continuation", "answers": {}}, True),
  ({"type": "question", "response_mode": "continuation",
    "answers": {"choice": "Continue"}}, False),
])
def test_open_continuation_card_requires_an_unanswered_terminal_card(block, expected):
  from app.broadcast import ChatBroadcast
  from app.chat_event_sink import ChatEventSink

  sink = ChatEventSink(
    ChatBroadcast("card-state"), "card-state",
  )
  sink.assistant_blocks.append(block)
  assert sink.has_open_continuation_card() is expected


def test_plan_revision_progress_allows_the_next_goal_rollover(db, chat):
  """A running task may span turns when its durable plan keeps advancing."""
  _add_goal_run(db, chat)
  first = _terminal_promote(chat.id, "goal-run", run_token="first-successor")
  assert first["promoted"]["goal_plan_revision"] == 1

  successor = db.get(models.ChatRun, "first-successor")
  successor.goal_plan_revision_at_admission = 1
  goal = db.get(models.ChatGoal, "goal-run")
  goal.revision = 2
  goal.plan_json = {
    "version": 1,
    "tasks": [{
      "id": "finish", "title": "Finish", "status": "running",
      "depends_on": [], "note": "Verified the first half",
    }],
  }
  db.commit()

  second = _terminal_promote(
    chat.id, "first-successor", run_token="second-successor",
  )

  assert second["promoted"]["goal_plan_revision"] == 2
  assert second["promoted"]["_messages"] == []
  assert db.get(models.ChatRun, "second-successor").goal_id == "goal-run"


@pytest.mark.asyncio
async def test_provider_free_terminal_does_not_loop_an_unfinished_goal(
  db, chat, monkeypatch,
):
  from app import chat as chat_mod, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink

  _add_goal_run(db, chat)
  broadcast = create_broadcast(chat.id)
  sink = ChatEventSink(
    broadcast,
    chat.id,
    run_token="goal-run",
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


@pytest.mark.asyncio
async def test_plan_with_nothing_runnable_hands_off_instead_of_a_no_op_turn(
  db, chat, monkeypatch,
):
  """Recording a blocker advances the plan but leaves nothing to run.

  The ending turn parks on the owner card at once instead of starting a paid
  automatic turn that can only restate the blocker.
  """
  from app import chat as chat_mod, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink

  _add_goal_run(db, chat, plan={"version": 1, "tasks": [
    {"id": "publish", "title": "Publish", "status": "blocked", "depends_on": []},
    {"id": "verify", "title": "Verify", "status": "pending",
     "depends_on": ["publish"]},
  ]})
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation", lambda **kwargs: scheduled.append(kwargs),
  )
  monkeypatch.setattr(chat_mod, "_publish_chat_run_finished", lambda *_: None)
  broadcast = create_broadcast(chat.id)
  sink = ChatEventSink(
    broadcast, chat.id, run_token="goal-run",
  )
  sink.publish({"type": "text", "content": "Blocked until the owner acts."})
  try:
    disposition = await chat_mod._complete_turn(
      bc=broadcast, sink=sink, db=db, chat_id=chat.id, run_gen=None,
      provider_id="codex", cost_usd=0, close_browser=False,
    )
  finally:
    remove_broadcast(chat.id)
  db.expire_all()

  assert disposition is chat_queue.TerminalDisposition.QUESTION_PARKED
  assert scheduled == []
  saved = db.get(models.Chat, chat.id)
  assert saved.pending_question_id == "goal-handoff-goal-run"


def _task(task_id, status, depends_on=(), parent_id=None):
  return {"id": task_id, "title": task_id, "status": status,
          "depends_on": list(depends_on), "parent_id": parent_id}


@pytest.mark.parametrize(("tasks", "automatic"), [
  ([_task("gate", "blocked"), _task("next", "pending", ["gate"])], False),
  ([_task("broken", "failed"), _task("next", "pending", ["broken"])], False),
  ([_task("gate", "blocked"), _task("other", "pending")], True),
  ([_task("gate", "blocked"), _task("next", "running")], True),
  ([_task("gate", "blocked"), _task("parent", "pending"),
    _task("child", "completed", parent_id="parent")], True),
  ([_task("done", "completed")], True),
], ids=["blocked", "failed", "ready", "running", "ready-to-verify",
        "completable"])
def test_rollover_needs_something_a_successor_can_run(db, chat, tasks, automatic):
  from app.goal_plans import goal_terminal_handoff

  _add_goal_run(db, chat, plan={"version": 1, "tasks": tasks})

  handoff = goal_terminal_handoff(db, chat.id, "goal-run")
  assert handoff.automatic_allowed is automatic


SETTLED_RUNNING_GOAL = {"version": 1, "tasks": [{
  "id": "finish", "title": "Finish", "status": "completed", "depends_on": [],
}]}


def _complete(db, result="Verified: checks green"):
  from app.goals import update_goal_record

  goal = db.get(models.ChatGoal, "goal-run")
  return update_goal_record(
    db, db.get(models.ChatRun, "goal-run"), goal, goal.revision, result=result,
  )


@pytest.mark.parametrize("outcome", ["met", "expired", "failed"])
def test_verified_completion_takes_delivery_of_its_fired_wait(db, chat, outcome):
  """The running turn saw the outcome its Wait was watching for.

  The Wait's resume could only be delivered after this turn, so it held the
  Goal open; the verified completion is that delivery, and no resume later
  wakes the finished Goal.
  """
  from app import chat_waits

  _add_goal_run(db, chat, plan=SETTLED_RUNNING_GOAL)
  wait = chat_waits.declare_wait(
    db, chat_id=chat.id, created_by_run_id="goal-run",
    description="Upstream checks finishing", kind="timer", delay_secs=60,
  )
  wait.status = outcome
  db.commit()

  assert _complete(db)["status"] == "completed"
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).resume_delivered_at is not None


def test_armed_wait_still_blocks_completion_and_is_named(db, chat):
  """An armed Wait is live observation: only its owner may drop it."""
  from app import chat_waits
  from app.goal_plans import GoalPlanError

  _add_goal_run(db, chat, plan=SETTLED_RUNNING_GOAL)
  fired = chat_waits.declare_wait(
    db, chat_id=chat.id, created_by_run_id="goal-run",
    description="Earlier gate", kind="timer", delay_secs=60,
  )
  fired.status = "met"
  armed = chat_waits.declare_wait(
    db, chat_id=chat.id, created_by_run_id="goal-run",
    description="Release gate", kind="timer", delay_secs=60,
  )
  db.commit()

  with pytest.raises(GoalPlanError) as refused:
    _complete(db)
  message = str(refused.value)
  assert f"armed Wait {armed.id} (Release gate)" in message
  assert "cancel_wait" in message
  db.expire_all()
  # A refused completion consumes nothing: the fired Wait still resumes.
  assert db.get(models.ChatWait, fired.id).resume_delivered_at is None
  assert db.get(models.ChatGoal, "goal-run").status == "open"


def test_completion_withdraws_the_queued_resume_of_its_fired_wait(db, chat):
  """The Wait fired mid-turn and queued its resume; the same turn then verified
  the outcome and completed. The queued resume is now stale and must not start
  a turn for the finished Goal, while other queued messages stay."""
  import asyncio

  from app import chat_waits
  from app.goals import settle_after_goal_completion

  _add_goal_run(db, chat, plan=SETTLED_RUNNING_GOAL)
  wait = chat_waits.declare_wait(
    db, chat_id=chat.id, created_by_run_id="goal-run",
    description="Checks finishing", kind="timer", delay_secs=60,
  )
  wait.status = "met"
  chat.pending_messages = [{
    "role": "user", "content": "A wait you declared has completed.",
    "ts": 1, "cid": f"wait-result-{wait.id}", "hidden": True,
    "kind": "wait_result", "source_work_id": "goal-run",
  }, {"role": "user", "content": "Owner follow-up", "ts": 2, "cid": "owner-1"}]
  db.commit()

  assert _complete(db)["status"] == "completed"
  asyncio.run(settle_after_goal_completion(chat.id))

  db.expire_all()
  assert db.get(models.ChatWait, wait.id).resume_delivered_at is not None
  assert [m["cid"] for m in db.get(models.Chat, chat.id).pending_messages] == [
    "owner-1",
  ]
