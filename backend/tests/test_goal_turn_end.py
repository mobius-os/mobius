"""A Goal keeps responsibility until an explicit outcome or durable handoff.

One targeted clean-ending settlement pass replaces unattended normal idleness.
It never fabricates owner questions, shrinks scope or recovers physical failure.
"""
from sqlalchemy.orm import object_session
from app import transcript_rows

from tests.goal_fixtures import goal_run as make_goal_run

import pytest

from app import models
from app.chat_writer import create_chat
from app.chat_writer import PromotePending, StartTurn, get_writer


UNFINISHED = {"version": 1, "tasks": [{
  "id": "finish", "title": "Finish", "status": "running",
  "depends_on": [],
}]}
SETTLED = {"version": 1, "tasks": [{
  "id": "finish", "title": "Finish", "status": "completed",
  "depends_on": [],
}]}


def _add_goal_run(db, chat, run_id="goal-run", *, goal_id="goal-run", plan=UNFINISHED):
  # Admission owns both the durable run and the writer's current token.
  # A row inserted directly cannot authorize another provider execution.
  get_writer().submit(StartTurn(
    chat_id=chat.id, run_token=run_id,
    user_msg={"role": "user", "content": "Finish the work", "cid": "owner-request", "ts": 1},
    title_source="Finish the work", default_provider="codex",
  )).result(timeout=5)
  db.refresh(chat)
  run = db.get(models.ChatRun, run_id)
  run.goal_id = goal_id
  run.goal_objective = "Finish the work"
  run.provider_execution_admitted = True
  db.add(models.ChatGoal(
    id=goal_id, chat_id=chat.id, objective="Finish the work",
    status="open", plan_json=plan, revision=1,
  ))
  db.commit()


def _question_blocks(chat):
  return [
    block
    for message in list(transcript_rows.history(chat)) or []
    for block in message.get("blocks") or []
    if block.get("type") == "question"
  ]


@pytest.mark.asyncio
@pytest.mark.parametrize("closing_text", [
  "The Goal is complete.",
  "I'll wait for you to say how you want to split the work.",
  "The reviewed change is ready; Send PR in Contribute is the next step.",
])
async def test_clean_goal_ending_keeps_exact_responsibility_in_one_settlement_pass(
  db, chat, monkeypatch, closing_text,
):
  """Prose claims of completion or owner dependence are not saved outcomes."""
  from app import chat as chat_mod, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink

  _add_goal_run(db, chat)
  scheduled = []
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation", lambda **kwargs: scheduled.append(kwargs),
  )
  monkeypatch.setattr(chat_mod, "_publish_chat_run_finished", lambda *_: None)
  broadcast = create_broadcast(chat.id)
  sink = ChatEventSink(broadcast, chat.id, run_token="goal-run")
  sink.publish({"type": "text", "content": closing_text})
  try:
    disposition = await chat_mod._complete_turn(
      bc=broadcast, sink=sink, db=db, chat_id=chat.id, run_gen=None,
      provider_id="codex", cost_usd=0, close_browser=False,
    )
  finally:
    remove_broadcast(chat.id)

  assert disposition is chat_queue.TerminalDisposition.CONTINUATION_PROMOTED
  assert len(scheduled) == 1
  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  assert saved.pending_question_id is None
  assert saved.pending_messages in (None, [])
  assert _question_blocks(saved) == []
  assert db.get(models.ChatGoal, "goal-run").status == "open"
  runs = db.query(models.ChatRun).filter_by(chat_id=chat.id).all()
  recovery = next(run for run in runs if run.id != "goal-run")
  assert recovery.goal_id == "goal-run"
  assert recovery.continuation_json["reason"] == "goal_settlement"
  assert recovery.status == "running"
  assert not any(message.get("kind") == "continuation" for message in transcript_rows.history(saved))


def test_queue_promotion_without_exact_ending_authority_never_starts_a_goal(db, chat):
  _add_goal_run(db, chat)

  result = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="successor", ending_status="completed",
  )).result(timeout=5)

  assert result["promoted"] is None
  db.expire_all()
  assert db.get(models.Chat, chat.id).pending_messages in (None, [])
  assert db.get(models.ChatRun, "successor") is None


@pytest.mark.parametrize("terminal", ["failed", "stopped", "interrupted"])
def test_physical_failure_or_owner_stop_never_authorizes_goal_settlement(db, chat, terminal):
  _add_goal_run(db, chat)
  response = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="not-authorized", ending_run_token="goal-run",
    ending_status=terminal,
  )).result(timeout=5)
  assert response["promoted"] is None
  assert db.get(models.ChatRun, "not-authorized") is None


def test_provider_free_ending_never_reenters_unavailable_provider(db, chat):
  _add_goal_run(db, chat)
  db.get(models.ChatRun, "goal-run").provider_execution_admitted = False
  db.commit()
  response = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="not-authorized", ending_run_token="goal-run",
  )).result(timeout=5)
  assert response["promoted"] is None


def test_real_owner_queue_wins_over_automatic_goal_settlement(db, chat):
  _add_goal_run(db, chat)
  chat.pending_messages = [{"role": "user", "cid": "new-owner-input",
                            "content": "A separate question", "ts": 3}]
  db.commit()
  response = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="ordinary-reply", ending_run_token="goal-run",
  )).result(timeout=5)
  assert response["promoted"]["content"] == "A separate question"
  db.expire_all()
  assert db.get(models.ChatRun, "ordinary-reply").goal_id is None
  assert db.get(models.ChatGoal, "goal-run").status == "open"


def test_armed_exact_goal_wait_owns_handoff_without_another_executor(db, chat):
  from app import chat_waits
  _add_goal_run(db, chat)
  chat_waits.declare_wait(db, chat_id=chat.id, created_by_run_id="goal-run",
                          description="Accepted external work", kind="timer", delay_secs=60)
  response = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="not-needed", ending_run_token="goal-run",
  )).result(timeout=5)
  assert response["promoted"] is None


def test_an_unrelated_wait_is_not_a_handoff_for_this_goal(db, chat):
  from datetime import datetime
  from app import chat_waits
  _add_goal_run(db, chat)
  db.add(models.ChatRun(id="separate-work", chat_id=chat.id, status="completed",
                        started_at=datetime(2000, 1, 1)))
  db.commit()
  chat_waits.declare_wait(db, chat_id=chat.id, created_by_run_id="separate-work",
                          description="Independent monitor", kind="timer", delay_secs=60)
  response = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="provisional", ending_run_token="goal-run",
  )).result(timeout=5)
  assert response["promoted"]["continuation_reason"] == "goal_settlement"


def test_wake_enabled_exact_goal_helper_owns_handoff(db, chat, monkeypatch):
  from types import SimpleNamespace
  from app import delegations
  _add_goal_run(db, chat)
  monkeypatch.setattr(delegations, "_self_resuming_helper_rows", lambda *_: [
    (SimpleNamespace(parent_root_run_id="goal-run"), "running"),
  ])
  response = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="not-needed", ending_run_token="goal-run",
  )).result(timeout=5)
  assert response["promoted"] is None


def test_helper_from_an_earlier_attempt_of_this_goal_keeps_its_handoff(db, chat, monkeypatch):
  from datetime import datetime
  from types import SimpleNamespace
  from app import delegations
  _add_goal_run(db, chat)
  db.add(models.ChatRun(id="middle-attempt", root_run_id="middle-root",
                        chat_id=chat.id, goal_id="goal-run", status="completed",
                        started_at=datetime(2000, 1, 1)))
  db.commit()
  monkeypatch.setattr(delegations, "_self_resuming_helper_rows", lambda *_: [
    (SimpleNamespace(parent_root_run_id="middle-root"), "running"),
  ])
  response = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="not-needed", ending_run_token="goal-run",
  )).result(timeout=5)
  assert response["promoted"] is None


def test_unrelated_live_reply_cannot_reactivate_retained_goal(db, chat):
  _add_goal_run(db, chat)
  db.get(models.ChatRun, "goal-run").status = "completed"
  db.add(models.ChatRun(id="ordinary-reply", chat_id=chat.id, status="running",
                        provider_execution_admitted=True))
  db.commit()
  response = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="not-authorized", ending_run_token="ordinary-reply",
  )).result(timeout=5)
  assert response["promoted"] is None


def test_stale_ending_identity_does_not_take_newer_goal_execution(db, chat):
  _add_goal_run(db, chat)
  db.add(make_goal_run(db, id="newer-goal", chat_id=chat.id, status="running",
                        goal_objective="Different work", provider_execution_admitted=True))
  db.commit()
  response = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="not-authorized", ending_run_token="goal-run",
  )).result(timeout=5)
  assert response["promoted"] is None


@pytest.mark.parametrize("owner", [None, "another-owner"])
def test_goal_settlement_requires_the_current_writer_token(db, chat, owner):
  _add_goal_run(db, chat)
  writer = get_writer()
  if owner is None:
    writer._run_token_owner.pop(chat.id)
  else:
    writer._run_token_owner[chat.id] = owner
  response = writer.submit(PromotePending(
    chat_id=chat.id, run_token="not-authorized", ending_run_token="goal-run",
  )).result(timeout=5)
  assert response["promoted"] is None
  db.expire_all()
  assert db.query(models.ChatRun).filter_by(chat_id=chat.id).count() == 1
  assert db.get(models.ChatGoal, "goal-run").status == "open"


@pytest.mark.parametrize("state", ["active", "cancelled", "interrupted", "read-only", "wrong-app"])
def test_goal_settlement_preserves_delegation_authority(db, chat, state):
  from datetime import UTC, datetime
  _add_goal_run(db, chat)
  db.add(create_chat(id="parent", title="Parent", messages=[]))
  delegation = models.Delegation(
    id="helper", parent_chat_id="parent", parent_root_run_id="parent-root",
    task_key="bounded-task", child_chat_id=chat.id, provider="codex",
    scope="read" if state == "read-only" else "write", cwd="/data/bounded",
    prompt_sha256="a" * 64,
  )
  if state == "cancelled":
    delegation.cancelled_at = datetime.now(UTC)
  if state == "interrupted":
    delegation.interrupted_at = datetime.now(UTC)
  if state == "wrong-app":
    app = models.App(name="Test app", slug="fixture", source_dir="/tmp/fixture", jsx_source="")
    db.add(app)
    db.flush()
    delegation.app_id = app.id
  db.add(delegation)
  db.commit()
  response = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="provisional", ending_run_token="goal-run",
  )).result(timeout=5)
  assert bool(response["promoted"]) is (state == "active")
  db.expire_all()
  assert db.get(models.ChatGoal, "goal-run").status == "open"
  if state == "active":
    successor = db.get(models.ChatRun, response["promoted"]["_run_token"])
    assert successor.goal_id == "goal-run" and successor.root_run_id == "goal-run"
  else:
    assert db.query(models.ChatRun).filter_by(chat_id=chat.id).count() == 1


@pytest.mark.asyncio
async def test_second_unhanded_ending_preserves_goal_and_records_durable_recovery(db, chat, monkeypatch):
  from app import chat as chat_mod, chat_queue
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_event_sink import ChatEventSink
  _add_goal_run(db, chat)
  first = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="provisional", ending_run_token="goal-run",
  )).result(timeout=5)
  token = first["promoted"]["_run_token"]
  db.expire_all()
  db.get(models.ChatRun, token).provider_execution_admitted = True
  db.commit()
  scheduled = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  monkeypatch.setattr(chat_mod, "_publish_chat_run_finished", lambda *_: None)
  bc = create_broadcast(chat.id)
  sink = ChatEventSink(bc, chat.id, run_token=token)
  sink.publish({"type": "text", "content": "Still not settled."})
  try:
    disposition = await chat_mod._complete_turn(
      bc=bc, sink=sink, db=db, chat_id=chat.id, run_gen=None,
      provider_id="codex", cost_usd=0, close_browser=False,
    )
  finally:
    remove_broadcast(chat.id)
  assert disposition is chat_queue.TerminalDisposition.GOAL_SETTLEMENT_FAILED
  assert scheduled == []
  db.expire_all()
  assert db.get(models.ChatGoal, "goal-run").status == "open"
  assert db.get(models.ChatRun, token).status == "failed"
  assert any(block.get("code") == "goal_settlement_unfinished"
             for message in transcript_rows.history(db.get(models.Chat, chat.id))
             for block in message.get("blocks") or [])


def test_resource_and_restart_recovery_cannot_reset_the_settlement_budget(db, chat):
  from app.continuations import recovery_attempted
  _add_goal_run(db, chat)
  first = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="provisional", ending_run_token="goal-run",
  )).result(timeout=5)
  token = first["promoted"]["_run_token"]
  db.expire_all()
  db.get(models.ChatRun, token).status = "completed"
  recovered = models.ChatRun(id="resource-recovery", chat_id=chat.id, goal_id="goal-run",
    status="running", continuation_json={"reason": "restart", "supersedes_run_token": token})
  db.add(recovered)
  db.commit()
  assert recovery_attempted(db, recovered, reason="goal_settlement") is True
  recovered.continuation_json = {"reason": "manual", "supersedes_run_token": token}
  db.commit()
  assert recovery_attempted(db, recovered, reason="goal_settlement") is False


def test_unknown_recovery_history_stays_closed_without_claiming_an_attempt(db, chat):
  from app.continuations import recovery_attempted, GOAL_SETTLEMENT_UNFINISHED_MESSAGE
  _add_goal_run(db, chat)
  run = db.get(models.ChatRun, "goal-run")
  run.continuation_json = {"reason": "restart", "supersedes_run_token": "missing"}
  db.commit()
  assert recovery_attempted(db, run, reason="goal_settlement") is True
  assert "after one recovery attempt" not in GOAL_SETTLEMENT_UNFINISHED_MESSAGE
  assert "cannot safely continue" in GOAL_SETTLEMENT_UNFINISHED_MESSAGE


@pytest.mark.asyncio
async def test_wait_delivery_starts_only_its_exact_goal_run(db, chat, monkeypatch):
  from app import chat as chat_mod, chat_waits

  _add_goal_run(db, chat)
  wait = chat_waits.declare_wait(
    db, chat_id=chat.id, created_by_run_id="goal-run",
    description="External gate finished", kind="timer", delay_secs=60,
  )
  wait.status = "met"
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
  # Scheduling is not delivery: the Wait stays owed until a turn carrying it
  # succeeds.
  assert db.get(models.ChatWait, wait.id).resume_delivered_at is None


def _complete(db, result="Verified: checks green"):
  from app.goals import update_goal_record

  goal = db.get(models.ChatGoal, "goal-run")
  return update_goal_record(
    db, db.get(models.ChatRun, "goal-run"), goal, goal.revision, complete=result,
  )


@pytest.mark.parametrize("outcome", ["met", "expired", "failed"])
def test_verified_completion_takes_delivery_of_its_fired_wait(db, chat, outcome):
  """The running turn saw the outcome its Wait was watching for.

  The Wait's resume could only be delivered after this turn; the verified
  completion is that delivery, so no resume later wakes the finished Goal.
  """
  from app import chat_waits

  _add_goal_run(db, chat, plan=SETTLED)
  wait = chat_waits.declare_wait(
    db, chat_id=chat.id, created_by_run_id="goal-run",
    description="Upstream checks finishing", kind="timer", delay_secs=60,
  )
  wait.status = outcome
  db.commit()

  assert _complete(db)["status"] == "completed"
  db.expire_all()
  assert db.get(models.ChatWait, wait.id).resume_delivered_at is not None


def test_an_armed_wait_does_not_block_completion_and_keeps_watching(db, chat):
  """Done is one action; a Wait the agent armed still does what it was set to."""
  from app import chat_waits

  _add_goal_run(db, chat, plan=SETTLED)
  armed = chat_waits.declare_wait(
    db, chat_id=chat.id, created_by_run_id="goal-run",
    description="Release gate", kind="timer", delay_secs=60,
  )
  db.commit()

  assert _complete(db)["status"] == "completed"
  db.expire_all()
  assert db.get(models.ChatWait, armed.id).status == "armed"


def test_an_open_owner_card_does_not_block_completion(db, chat):
  _add_goal_run(db, chat, plan=SETTLED)
  transcript_rows.replace_all(object_session(chat), chat, [*list(transcript_rows.history(chat)), {
    "role": "assistant", "id": "goal-run:assistant:1", "ts": 2, "content": "",
    "blocks": [{
      "type": "question", "question_id": "merge-approval",
      "response_mode": "continuation",
      "questions": [{"id": "q", "question": "Merge it?", "options": []}],
    }],
  }])
  chat.pending_question_id = "merge-approval"
  db.commit()

  assert _complete(db)["status"] == "completed"
  db.expire_all()
  assert db.get(models.Chat, chat.id).pending_question_id == "merge-approval"


def test_unfinished_tasks_still_block_completion(db, chat):
  from app.goal_plans import GoalPlanError

  _add_goal_run(db, chat, plan=UNFINISHED)

  with pytest.raises(GoalPlanError, match="unfinished tasks"):
    _complete(db)


def test_completion_withdraws_the_queued_resume_of_its_fired_wait(db, chat):
  """The Wait fired mid-turn and queued its resume; the same turn then verified
  the outcome and completed. The queued resume is now stale and must not start
  a turn for the finished Goal, while other queued messages stay."""
  import asyncio

  from app import chat_waits
  from app.goals import settle_after_goal_completion

  _add_goal_run(db, chat, plan=SETTLED)
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


def test_a_planless_goal_cannot_complete_while_its_helper_works(db, chat, monkeypatch):
  """Done waits for running helpers whether or not the Goal has a plan."""
  from app import goal_plans
  from app.goal_plans import GoalPlanError

  _add_goal_run(db, chat, plan=None)
  helper = {"id": "d1", "task_key": "review", "status": "running", "children": []}
  def helper_tree(*_, all_attempts=False):
    assert all_attempts  # Settlement includes superseded attempts, not just the UI projection.
    return [helper]
  monkeypatch.setattr(goal_plans, "_delegation_tree", helper_tree)

  with pytest.raises(GoalPlanError, match="active delegations"):
    _complete(db)
  assert db.get(models.ChatGoal, "goal-run").status == "open"

  helper["status"] = "completed"
  assert _complete(db)["status"] == "completed"


def _legacy_goal_handoff(cid="goal-handoff-old-run"):
  """The hidden control the pre-2026-09-27 writer queued to keep a Goal going."""
  return {
    "role": "user", "content": "Continue the unfinished Goal from its saved plan.",
    "kind": "continuation", "continuation_reason": "goal_handoff",
    "goal_id": "goal-run", "hidden": True, "cid": cid, "ts": 5,
  }


def test_a_lone_legacy_goal_handoff_is_retired_unrun(db, chat):
  _add_goal_run(db, chat)
  chat.pending_messages = [_legacy_goal_handoff()]
  db.commit()

  result = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="successor",
  )).result(timeout=5)

  assert result["promoted"] is None
  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  assert saved.pending_messages == []
  assert not any(
    "Continue the unfinished Goal" in str(m.get("content")) for m in list(transcript_rows.history(saved))
  )
  assert db.get(models.ChatRun, "successor") is None


def test_a_legacy_goal_handoff_behind_owner_input_never_runs(db, chat):
  _add_goal_run(db, chat)
  chat.pending_messages = [
    {"role": "user", "content": "Owner follow-up", "cid": "owner-1", "ts": 4},
    _legacy_goal_handoff(),
  ]
  db.commit()

  result = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="successor",
  )).result(timeout=5)

  assert result["promoted"]["content"] == "Owner follow-up"
  db.expire_all()
  saved = db.get(models.Chat, chat.id)
  assert saved.pending_messages == []
  assert not any(
    "Continue the unfinished Goal" in str(m.get("content")) for m in list(transcript_rows.history(saved))
  )


def test_stop_retires_a_legacy_goal_handoff_without_resending_it(db, chat):
  from app.chat_writer import ClearPending

  wait_result = {
    "role": "user", "content": "A wait you declared has completed.",
    "kind": "wait_result", "hidden": True, "cid": "wait-result-1", "ts": 6,
  }
  chat.pending_messages = [
    {"role": "user", "content": "Owner text", "cid": "owner-1", "ts": 4},
    _legacy_goal_handoff(),
    wait_result,
  ]
  db.commit()

  cleared = get_writer().submit(ClearPending(
    chat_id=chat.id, run_token="",
  )).result(timeout=5)

  assert cleared == {"cleared": 2, "cleared_cids": ["owner-1"]}
  db.expire_all()
  assert db.get(models.Chat, chat.id).pending_messages == [wait_result]
