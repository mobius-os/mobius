"""Intent survives process interruption without becoming somebody else's Goal."""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app import models
from app import transcript_rows
from app.chat_writer import (
  AppendPending, FinishRun, GoalPromotionRejected, PrepareChatStop,
  PromoteRunToGoal, StartTurn, StartTurnRecoveryChanged, get_writer,
)
from app.goals import goal_hold, stage_goal_hold, update_goal_record
from app.goal_plans import presented_goal
from app.run_state import goal_identity_for_run_start


def submit(command):
  return get_writer().submit(command).result(timeout=5)


def seed(db, chat, *, status="running"):
  goal = models.ChatGoal(
    id="original", chat_id=chat.id, objective="The entire promised outcome",
    revision=4, plan_json={"tasks": [
      {"id": "verify", "title": "Verify outcome", "status": "completed", "depends_on": []},
    ]},
  )
  run = models.ChatRun(id="original-run", root_run_id="original-run", chat_id=chat.id,
    status=status, goal_id=goal.id, goal_objective=goal.objective,
    started_at=datetime.now(UTC) - timedelta(minutes=1), provider="codex")
  db.add_all([goal, run]); db.commit()
  return goal, run


def resume(chat, goal, *, revision=None, token="resumed"):
  return StartTurn(chat_id=chat.id, run_token=token,
    user_msg={"kind": "continuation", "continuation_reason": "manual", "cid": token},
    resume_goal_id=goal.id, resume_goal_revision=goal.revision if revision is None else revision)


@pytest.mark.parametrize("run_token", ["original-run", ""])
@pytest.mark.parametrize("status", ["stopped", "interrupted", "failed"])
def test_process_terminal_never_manufactures_goal_hold(db, chat, run_token, status):
  goal, run = seed(db, chat)
  submit(FinishRun(chat_id=chat.id, run_token=run_token, terminal_status=status))
  db.expire_all()
  assert goal.status == "open" and goal.hold_json is None and goal.revision == 4
  assert "pause_reason" not in presented_goal(db, chat.id)


def test_explicit_stop_records_intent_before_provider_timeout(db, chat, monkeypatch):
  from app import chat as runtime
  goal, run = seed(db, chat)
  handle = SimpleNamespace(kind="codex")
  monkeypatch.setattr(runtime.registry, "get_handles", lambda _: [handle])
  async def refuses_to_stop(*args, **kwargs):
    db.expire_all()
    assert goal.status == "stopped"
    assert goal_hold(goal)["actor"] == "owner"
    assert goal_hold(goal)["run_id"] == run.id
    return False, False
  monkeypatch.setattr(runtime, "_stop_handle_with_escalation", refuses_to_stop)
  assert asyncio.run(runtime.stop_chat_for(chat.id, actor="owner"))[0] is False
  db.expire_all()
  assert goal.status == "stopped" and run.status == "running"


@pytest.mark.parametrize("actor,reason", [("owner", "owner"), ("agent", "agent")])
def test_exact_stop_source_is_durable_and_retry_does_not_rewrite_it(db, chat, actor, reason):
  goal, run = seed(db, chat)
  action = PrepareChatStop(chat_id=chat.id, actor=actor, actor_id="actor-run", source_id="stop-action")
  submit(action); db.expire_all()
  hold = dict(goal.hold_json)
  submit(PrepareChatStop(chat_id=chat.id, actor=actor, actor_id="actor-run", source_id="stop-action")); db.expire_all()
  assert goal.hold_json == hold and goal.revision == 5
  assert hold["source_id"] == "stop-action" and hold["actor_id"] == "actor-run"
  assert presented_goal(db, chat.id)["pause_reason"] == reason


def test_stop_of_unrelated_live_turn_does_not_pause_retained_goal(db, chat):
  goal, run = seed(db, chat, status="completed")
  db.add(models.ChatRun(id="other", root_run_id="other", chat_id=chat.id, status="running"))
  db.commit()
  submit(PrepareChatStop(chat_id=chat.id, actor="owner")); db.expire_all()
  assert goal.status == "open" and goal.hold_json is None


def test_generic_stop_preparation_does_not_invent_a_deliberate_hold(db, chat):
  goal, run = seed(db, chat)
  submit(PrepareChatStop(chat_id=chat.id)); db.expire_all()
  assert goal.status == "open" and goal.hold_json is None


def test_legacy_goal_resume_after_unrelated_turn_reuses_original_checklist(db, chat):
  goal, run = seed(db, chat, status="interrupted")
  goal.status = "stopped"  # Migrated data has no reliable actor.
  db.add(models.ChatRun(id="later-question", root_run_id="later-question", chat_id=chat.id, status="completed"))
  db.commit()
  result = submit(resume(chat, goal)); db.expire_all()
  successor = db.get(models.ChatRun, "resumed")
  assert result["history"]
  assert successor.goal_id == goal.id and successor.root_run_id == run.id
  assert goal.status == "open" and goal.hold_json is None and goal.revision == 5
  assert goal.plan_json["tasks"][0]["id"] == "verify"
  assert db.query(models.ChatGoal).count() == 1
  update_goal_record(db, successor, goal, goal.revision, complete="Original outcome verified")
  assert goal.status == "completed"


@pytest.mark.parametrize("status", models.NONTERMINAL_RUN_STATUSES)
def test_exact_goal_resume_does_not_interrupt_unrelated_unfinished_work(db, chat, status):
  goal, run = seed(db, chat, status="completed")
  goal.status = "stopped"
  other = models.ChatRun(
    id="other-work", root_run_id="other-work", chat_id=chat.id, status=status,
    park_reason="usage_limit" if status != "running" else None,
  )
  db.add(other)
  db.commit()
  assert isinstance(submit(resume(chat, goal)), StartTurnRecoveryChanged)
  db.expire_all()
  assert other.status == status and other.ended_at is None
  assert goal.status == "stopped" and goal.revision == 4
  assert db.get(models.ChatRun, "resumed") is None


@pytest.mark.parametrize("status", models.CONTINUATION_RUN_STATUSES)
def test_exact_goal_resume_can_continue_its_own_park(db, chat, status):
  goal, prior = seed(db, chat, status=status)
  assert submit(resume(chat, goal))["history"]
  db.expire_all()
  successor = db.get(models.ChatRun, "resumed")
  assert successor.goal_id == goal.id
  assert successor.continuation_json["supersedes_run_token"] == prior.id


def test_goal_resume_is_exact_revision_fenced_and_leaves_question_intact(db, chat):
  goal, run = seed(db, chat, status="completed")
  old_revision = goal.revision
  submit(PrepareChatStop(chat_id=chat.id, actor="owner")); db.expire_all()
  result = submit(resume(chat, goal, revision=old_revision))
  assert isinstance(result, StartTurnRecoveryChanged)
  db.expire_all()
  assert goal.status == "stopped" and db.get(models.ChatRun, "resumed") is None
  chat.pending_question_id = "other-question"; db.commit()
  from app.chat_writer import StartTurnBlockedByPendingQuestion
  result = submit(resume(chat, goal))
  assert isinstance(result, StartTurnBlockedByPendingQuestion)
  db.expire_all()
  assert chat.pending_question_id == "other-question" and goal.status == "stopped"


def test_goal_resume_replay_cannot_change_target_or_revision(db, chat):
  goal, run = seed(db, chat, status="completed")
  submit(PrepareChatStop(chat_id=chat.id, actor="owner")); db.expire_all()
  command = resume(chat, goal)
  submit(command); db.expire_all()
  assert submit(resume(chat, goal, revision=command.resume_goal_revision))["duplicate"] is True
  assert isinstance(submit(resume(chat, goal, revision=command.resume_goal_revision + 1)), StartTurnRecoveryChanged)
  assert db.query(models.ChatRun).count() == 2


def test_new_owner_attempt_can_explicitly_reattach_held_goal_but_stopped_attempt_cannot(db, chat):
  goal, run = seed(db, chat)
  submit(PrepareChatStop(chat_id=chat.id, actor="owner")); db.expire_all()
  rejected = submit(PromoteRunToGoal(chat_id=chat.id, run_token=run.id,
    objective=goal.objective, resume_goal_id=goal.id))
  assert isinstance(rejected, GoalPromotionRejected) and rejected.reason == "attempt_was_stopped"
  submit(FinishRun(chat_id=chat.id, run_token=run.id, terminal_status="stopped"))
  submit(StartTurn(chat_id=chat.id, run_token="owner-follow-up", owner_input=True,
    user_msg={"role":"user", "content":"Please finish the original work", "cid":"follow-up"}))
  assert submit(PromoteRunToGoal(chat_id=chat.id, run_token="owner-follow-up",
    objective=goal.objective, resume_goal_id=goal.id))["state"] == "promoted"
  db.expire_all()
  assert goal.status == "open" and goal.hold_json is None
  assert db.get(models.ChatRun,"owner-follow-up").goal_id == goal.id


def test_reply_resume_does_not_borrow_a_retained_goal(db, chat):
  goal, run = seed(db, chat, status="completed")
  db.add(models.ChatRun(id="unrelated", root_run_id="unrelated", chat_id=chat.id, status="interrupted"))
  db.commit()
  submit(StartTurn(chat_id=chat.id, run_token="reply-resume", resume_run_id="unrelated",
    user_msg={"kind":"continuation", "continuation_reason":"manual", "cid":"reply-resume"}))
  db.expire_all()
  assert db.get(models.ChatRun,"reply-resume").goal_id is None
  assert goal.status == "open" and goal.revision == 4


def test_saved_card_answer_carries_its_exact_goal_not_newest_goal(db, chat):
  goal, run = seed(db, chat, status="completed")
  db.add(models.ChatGoal(id="newer", chat_id=chat.id, objective="Separate outcome"))
  transcript_rows.replace_all(db, chat, [{"role":"assistant", "id": run.id, "blocks":[
    {"type":"question", "question_id":"card", "questions":[], "response_mode":"continuation"},
  ]}])
  chat.pending_question_id = "card"; db.commit()
  queued = submit(AppendPending(chat_id=chat.id, question_id="card", answers={"q":"yes"},
    require_answer_match=True, user_msg={"role":"user", "content":"yes", "cid":"answer",
      "kind":"continuation", "continuation_reason":"question_answer"}))
  assert queued["stored"]["goal_id"] == goal.id
  db.expire_all()
  assert goal_identity_for_run_start(db, chat.id, queued["stored"]) == (goal.objective, goal.id)


@pytest.mark.parametrize("status", ["stopped", "completed", "cannot_complete", "cancelled"])
def test_automatic_exact_continuation_never_overrides_held_or_terminal_goal(db, chat, status):
  goal, run = seed(db, chat, status="interrupted")
  goal.status = status; db.commit()
  assert goal_identity_for_run_start(db, chat.id, {
    "kind":"continuation", "continuation_reason":"restart", "goal_id":goal.id,
  }) == (None, None)


def test_goal_resume_http_retries_exact_target_and_preserves_followup_queue(client, auth, db, chat, monkeypatch):
  from app import chat as runtime
  goal, run = seed(db, chat, status="completed")
  goal.status = "stopped"
  db.add(models.ChatRun(id="other-ended", root_run_id="other-ended", chat_id=chat.id, status="completed"))
  chat.pending_messages = [{"role":"user", "content":"Separate follow-up", "cid":"next", "ts":1}]
  db.commit()
  calls = []
  async def capture(*args, **kwargs):
    calls.append(kwargs)
  monkeypatch.setattr("app.routes.chats_stream.run_chat", capture)
  request = {"content":"", "continuation":"manual", "cid":"goal-resume",
    "resume_goal_id":goal.id, "resume_goal_revision":goal.revision}
  try:
    first = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json=request)
    assert first.status_code == 202, first.text
    again = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json=request)
    assert again.status_code == 200 and again.json()["status"] == "duplicate"
    for changed in ({"resume_goal_id":"foreign"}, {"resume_goal_revision":999}):
      refused = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={**request, **changed})
      assert refused.status_code == 409
    assert len(calls) == 1
    db.expire_all()
    successor = db.get(models.ChatRun,calls[0]["run_token"])
    assert successor.goal_id == goal.id and goal.status == "open"
    assert [row["cid"] for row in chat.pending_messages] == ["next"]
    assert not any(row.get("cid") == "goal-resume" for row in transcript_rows.history(chat))
  finally:
    runtime.discard_starting(chat.id)


def test_agent_can_reconcile_named_legacy_goal_without_creating_a_replacement(client, db, chat):
  from app.auth import create_agent_token
  goal, run = seed(db, chat, status="interrupted")
  goal.status = "stopped"
  db.commit()
  submit(StartTurn(chat_id=chat.id, run_token="current", owner_input=True,
    user_msg={"role":"user", "content":"Verify the old Goal", "cid":"verify-old"}))
  db.expire_all()
  owner = db.query(models.Owner).first()
  headers = {"Authorization":"Bearer "+create_agent_token(chat.id, owner.username, owner.token_epoch, run_id="current")}
  path = f"/api/chats/{chat.id}/goal/update"
  assert client.post(path, headers=headers, json={"next_action":"Verify"}).status_code == 409
  result = client.post(path, headers=headers, json={"goal_id":goal.id, "complete":"Original outcome independently verified"})
  assert result.status_code == 200, result.text
  db.expire_all()
  assert goal.status == "completed" and goal.objective == "The entire promised outcome"
  assert db.query(models.ChatGoal).count() == 1


@pytest.mark.parametrize("actor", ["owner", "agent"])
def test_stop_route_records_actual_principal_not_default_owner(client, auth, db, chat, actor):
  from app.auth import create_agent_token
  goal, run = seed(db, chat)
  headers = auth
  if actor == "agent":
    owner = db.query(models.Owner).first()
    headers = {"Authorization":"Bearer "+create_agent_token(chat.id, owner.username, owner.token_epoch, run_id=run.id)}
  result = client.post("/api/chat/stop", headers=headers, json={"chat_id":chat.id})
  assert result.status_code == 200, result.text
  db.expire_all()
  assert goal_hold(goal)["actor"] == actor
  assert run.status == "stopped"


@pytest.mark.parametrize("status", ["stopped", "completed", "cannot_complete", "cancelled"])
def test_automatic_park_cannot_bypass_goal_state_even_after_crash(db, chat, status):
  from app import chat as runtime
  from app.chat_writer import StartContinuation, StartContinuationBlocked
  goal, run = seed(db, chat, status="resume_pending")
  goal.status = status
  run.park_reason = "memory"
  run.parked_until = datetime.now(UTC) - timedelta(minutes=1)
  db.commit()
  assert runtime.continuation_handoff_for_chat(db, chat.id)["kind"] == "recovery"
  assert asyncio.run(runtime._auto_resume_chat(chat.id, park_token=run.id)) is False
  result = submit(StartContinuation(chat_id=chat.id, run_token="automatic",
    root_run_id=run.id, supersedes_run_token=run.id, reason="memory", cid="auto", content="continue"))
  assert isinstance(result, StartContinuationBlocked) and result.reason == "goal_held"
  db.expire_all()
  assert db.get(models.ChatRun,"automatic") is None and goal.status == status


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("automatic", ["activity", "result"])
def test_automatic_work_cannot_explicitly_reopen_held_goal(db, chat, legacy, automatic):
  goal, run = seed(db, chat, status="completed")
  if legacy:
    goal.status = "stopped"
  else:
    stage_goal_hold(goal, cause="stop", actor="owner", source_id="stop", run_id=run.id)
  db.add(models.ChatRun(id="automatic", chat_id=chat.id, status="running",
    root_run_id="automatic", activity_delivery_json={"source_work_id":automatic}))
  db.commit()
  result = submit(PromoteRunToGoal(chat_id=chat.id, run_token="automatic",
    resume_goal_id=goal.id, objective=goal.objective))
  assert isinstance(result, GoalPromotionRejected)
  assert result.reason == "owner_continuation_required"
  db.expire_all()
  assert goal.status == "stopped" and db.get(models.ChatRun,"automatic").goal_id is None


@pytest.mark.parametrize("stale_revision", [False, True])
def test_activation_barrier_never_queues_goal_resume(client, auth, db, chat, stale_revision):
  goal, run = seed(db, chat, status="completed")
  goal.status = "stopped"
  now = datetime.now(UTC)
  db.add(models.ChatWait(id="activation-A", chat_id=chat.id, kind="platform_activation",
    created_by_run_id="another-run", goal_id="another-goal", root_run_id="another-root",
    description="Another Goal activation", condition_owner="Startup", status="armed",
    interval_secs=60, deadline_at=now+timedelta(days=1), next_check_at=now,
    action_approved_at=now))
  chat.pending_messages = [{"role":"user", "content":"Owner follow-up", "cid":"queued", "ts":1}]
  db.commit()
  before = list(chat.pending_messages)
  revision = goal.revision - 1 if stale_revision else goal.revision
  response = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
    "content":"", "continuation":"manual", "cid":"blocked-resume",
    "resume_goal_id":goal.id, "resume_goal_revision":revision,
  })
  assert response.status_code == 409 and response.json()["detail"]["code"] == "recovery_changed"
  assert isinstance(submit(resume(chat,goal,revision=revision)), StartTurnRecoveryChanged)
  db.expire_all()
  assert goal.status == "stopped" and goal.revision == 4
  assert chat.pending_messages == before and db.query(models.ChatRun).count() == 1


def test_queued_owner_input_is_stamped_at_acceptance_not_promotion(db, chat):
  from app.chat_writer import PromotePending
  from app.goals import has_owner_input_after_hold
  goal, run = seed(db, chat)
  queued = submit(AppendPending(chat_id=chat.id, owner_input=True,
    user_msg={"role":"user", "content":"Follow-up", "cid":"owner"}))
  accepted = datetime.fromisoformat(queued["stored"]["_owner_input_at"])
  submit(PrepareChatStop(chat_id=chat.id, actor="owner")); db.expire_all()
  submit(FinishRun(chat_id=chat.id, run_token=run.id, terminal_status="stopped"))
  submit(PromotePending(chat_id=chat.id, run_token="promoted")); db.expire_all()
  successor = db.get(models.ChatRun,"promoted")
  assert successor.owner_input_at.replace(tzinfo=UTC) == accepted
  assert not has_owner_input_after_hold(successor,goal)
  assert all("_owner_input_at" not in row for row in transcript_rows.history(chat))


@pytest.mark.parametrize("hold_kind", ["explicit", "legacy", "invalid"])
def test_later_queued_owner_turn_can_reattach_without_guessing_from_text(db, chat, hold_kind):
  from app.chat_writer import PromotePending
  goal, run = seed(db, chat, status="completed")
  stage_goal_hold(goal,cause="stop",actor="owner",source_id="stop",run_id=run.id)
  if hold_kind != "explicit":
    goal.hold_json = None if hold_kind == "legacy" else {"actor":"owner"}
  db.commit()
  submit(AppendPending(chat_id=chat.id, owner_input=True,
    user_msg={"role":"user", "content":"Please do that", "cid":"owner"}))
  submit(PromotePending(chat_id=chat.id, run_token="promoted"))
  result = submit(PromoteRunToGoal(chat_id=chat.id,run_token="promoted",
    objective=goal.objective,resume_goal_id=goal.id))
  assert result["state"] == "promoted"


def test_physical_recovery_preserves_owner_admission_not_fresh_authority(db, chat):
  from app.chat_writer import StartContinuation
  from app.goals import has_owner_input_after_hold
  goal, old = seed(db, chat, status="completed")
  stage_goal_hold(goal,cause="stop",actor="owner",source_id="stop",run_id=old.id); db.commit()
  submit(StartTurn(chat_id=chat.id, run_token="owner-turn", owner_input=True,
    user_msg={"role":"user","content":"Finish it", "cid":"owner"}))
  db.expire_all(); request = db.get(models.ChatRun,"owner-turn")
  request.status="resume_pending"; request.park_reason="memory"; db.commit()
  submit(StartContinuation(chat_id=chat.id,run_token="recovery",root_run_id=request.id,
    supersedes_run_token=request.id,reason="memory",cid="recover",content="continue"))
  db.expire_all(); successor = db.get(models.ChatRun,"recovery")
  assert successor.owner_input_at == request.owner_input_at
  assert has_owner_input_after_hold(successor,goal)
  # A later hold wins even if this execution is recovered again.
  goal.hold_json = {**goal.hold_json,"at":datetime.now(UTC).isoformat()}; db.commit()
  assert not has_owner_input_after_hold(successor,goal)


def test_owner_admission_migration_is_additive_and_does_not_invent_old_authority(tmp_path):
  from sqlalchemy import create_engine, inspect, text
  from app.schema_migrations import _add_run_owner_input_at
  engine = create_engine(f"sqlite:///{tmp_path / 'owner-input.db'}")
  with engine.begin() as connection:
    connection.execute(text("CREATE TABLE chat_runs (id VARCHAR(64) PRIMARY KEY, status VARCHAR(16))"))
    connection.execute(text("INSERT INTO chat_runs VALUES ('old', 'stopped')"))
  _add_run_owner_input_at(engine); _add_run_owner_input_at(engine)
  with engine.connect() as connection:
    assert connection.execute(text("SELECT * FROM chat_runs")).one() == ("old","stopped",None)
  assert inspect(engine).get_columns("chat_runs")[-1]["nullable"]
  engine.dispose()


@pytest.mark.parametrize("invalid", [None, {"actor":"owner"}])
def test_inherited_owner_input_cannot_guess_ordering_of_unknown_hold(db, chat, invalid):
  from app.goals import has_owner_input_after_hold
  goal, old = seed(db,chat,status="completed")
  goal.status="stopped"; goal.hold_json=invalid
  now=datetime.now(UTC)
  run=models.ChatRun(id="recovery",chat_id=chat.id,status="running",
    owner_input_at=now,started_at=now,continuation_json={"reason":"memory"})
  assert not has_owner_input_after_hold(run,goal)


@pytest.mark.parametrize("direct_owner", [True, False])
def test_only_direct_owner_http_input_can_supply_later_reattachment_evidence(client, auth, db, chat, monkeypatch, direct_owner):
  from app import chat as runtime
  from app.auth import create_agent_token
  goal, old = seed(db, chat, status="completed")
  stage_goal_hold(goal, cause="stop", actor="owner", source_id="stop", run_id=old.id)
  db.commit()
  headers = auth
  if not direct_owner:
    old.status = "running"
    db.commit()
    owner = db.query(models.Owner).first()
    headers = {"Authorization": "Bearer " + create_agent_token(
      chat.id, owner.username, owner.token_epoch, run_id=old.id)}
  calls = []
  async def capture(*args, **kwargs):
    calls.append(kwargs)
  monkeypatch.setattr("app.routes.chats_stream.run_chat", capture)
  try:
    response = client.post(f"/api/chats/{chat.id}/messages", headers=headers, json={
      "content": "Investigate the outstanding work", "cid": "new-input",
      "_owner_input_at": "2999-01-01T00:00:00+00:00",
    })
    assert response.status_code == 202, response.text
    db.expire_all()
    run = db.get(models.ChatRun, calls[0]["run_token"])
    assert (run.owner_input_at is not None) == direct_owner
    assert all("_owner_input_at" not in row for row in transcript_rows.history(chat))
  finally:
    runtime.discard_starting(chat.id)


def test_pending_provenance_cannot_be_supplied_by_message_data(db, chat):
  queued = submit(AppendPending(chat_id=chat.id, user_msg={
    "role": "user", "content": "A programmatic message", "cid": "programmatic",
    "_owner_input_at": "2999-01-01T00:00:00+00:00",
  }))
  assert "_owner_input_at" not in queued["stored"]


def test_fresh_schema_owner_admission_is_nullable_and_migration_replays(tmp_path):
  from sqlalchemy import create_engine, inspect
  from app.schema_migrations import _add_run_owner_input_at
  engine = create_engine(f"sqlite:///{tmp_path / 'fresh-owner-input.db'}")
  models.Base.metadata.create_all(engine)
  before = inspect(engine).get_columns("chat_runs")
  _add_run_owner_input_at(engine)
  _add_run_owner_input_at(engine)
  after = inspect(engine).get_columns("chat_runs")
  assert [column["name"] for column in after] == [column["name"] for column in before]
  assert next(column for column in after if column["name"] == "owner_input_at")["nullable"]
  engine.dispose()
