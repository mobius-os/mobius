"""Sequential goal-plan contracts at the durable chat-writer boundary."""

from app import models
from app.chat_writer import (
  AdvanceGoalPlan,
  AppendPending,
  Barrier,
  PromotePending,
  StartGoalPlan,
  StartTurn,
  StopGoalPlan,
  get_writer,
)
from app.database import SessionLocal
from app.goal_plans import active_plan_summary, parse_goal_plan


def _seed_chat(chat_id: str) -> None:
  db = SessionLocal()
  try:
    db.add(models.Chat(
      id=chat_id, title="Goal plan", messages=[], pending_messages=[],
      provider="codex",
    ))
    db.commit()
  finally:
    db.close()


def _start_plan(chat_id: str, run_token: str = "stage-1") -> None:
  get_writer().submit(StartTurn(
    chat_id=chat_id,
    run_token=run_token,
    user_msg={
      "role": "user",
      "content": (
        "/goals Ship a durable goal flow\n"
        "- Build the state transition\n"
        "- Verify the continuation"
      ),
      "ts": 1,
    },
    title_source="Goal plan",
    default_provider="codex",
  )).result(timeout=5)
  get_writer().submit(StartGoalPlan(
    chat_id=chat_id,
    run_token=run_token,
    overall_objective="Ship a durable goal flow",
    stages=["Build the state transition", "Verify the continuation"],
  )).result(timeout=5)


def test_goal_plan_parser_requires_an_outcome_and_multiple_listed_stages():
  parsed = parse_goal_plan(
    "/goals Ship a release\n- Map the flow\n2. Build it\n* Verify it"
  )
  assert parsed is not None
  assert parsed.overall_objective == "Ship a release"
  assert parsed.stages == ("Map the flow", "Build it", "Verify it")
  assert parse_goal_plan("/goals Ship a release\n- Only one") is None
  assert parse_goal_plan("/goals Ship a release\nmap it\nbuild it") is None


def test_goal_plan_stage_advance_precedes_existing_owner_queue():
  chat_id = "goal-plan-order"
  _seed_chat(chat_id)
  _start_plan(chat_id)

  # A real owner follow-up is already waiting when stage one settles. The next
  # plan stage must still be the first continuation promoted.
  get_writer().submit(AppendPending(
    chat_id=chat_id,
    run_token="stage-1",
    user_msg={"role": "user", "content": "After the plan, summarize it", "ts": 2},
  )).result(timeout=5)
  advanced = get_writer().submit(AdvanceGoalPlan(
    chat_id=chat_id,
    run_token="stage-1",
    completed_run_token="stage-1",
    next_run_token="stage-2",
  )).result(timeout=5)
  assert advanced["advanced"] is True
  assert advanced["completed"] is False

  db = SessionLocal()
  try:
    chat = db.get(models.Chat, chat_id)
    assert [item.get("kind") for item in chat.pending_messages] == [
      "goal_plan_step", None,
    ]
    summary = active_plan_summary(db, chat_id, "stage-2")
    assert summary == {
      "id": summary["id"],
      "overall_objective": "Ship a durable goal flow",
      "stage_index": 1,
      "stage_count": 2,
      "stage_objective": "Verify the continuation",
      "stage_label": "Ship a durable goal flow · Stage 2/2 · Verify the continuation",
    }
  finally:
    db.close()

  promoted = get_writer().submit(PromotePending(
    chat_id=chat_id, run_token="stage-2",
  )).result(timeout=5)
  assert promoted["promoted"]["content"] == "Continue the active goal plan."

  db = SessionLocal()
  try:
    stage_two = db.get(models.ChatRun, "stage-2")
    chat = db.get(models.Chat, chat_id)
    assert stage_two.goal_objective == (
      "Ship a durable goal flow · Stage 2/2 · Verify the continuation"
    )
    assert [item["content"] for item in chat.pending_messages] == [
      "After the plan, summarize it",
    ]
  finally:
    db.close()


def test_stopped_plan_cannot_queue_a_later_stage():
  chat_id = "goal-plan-stop"
  _seed_chat(chat_id)
  _start_plan(chat_id)
  assert get_writer().submit(StopGoalPlan(chat_id=chat_id)).result(timeout=5) == {
    "stopped": True,
  }
  assert get_writer().submit(AdvanceGoalPlan(
    chat_id=chat_id,
    run_token="stage-1",
    completed_run_token="stage-1",
    next_run_token="stage-2",
  )).result(timeout=5) == {"advanced": False}
  get_writer().submit(Barrier()).result(timeout=5)

  db = SessionLocal()
  try:
    plan = db.query(models.GoalPlan).filter_by(chat_id=chat_id).one()
    chat = db.get(models.Chat, chat_id)
    assert plan.status == "stopped"
    assert chat.pending_messages == []
  finally:
    db.close()


def test_queued_goal_clear_stops_the_plan_before_another_stage_starts():
  chat_id = "goal-plan-queued-clear"
  _seed_chat(chat_id)
  _start_plan(chat_id)
  get_writer().submit(AppendPending(
    chat_id=chat_id,
    run_token="stage-1",
    user_msg={"role": "user", "content": "/goal clear", "ts": 2},
  )).result(timeout=5)
  advanced = get_writer().submit(AdvanceGoalPlan(
    chat_id=chat_id,
    run_token="stage-1",
    completed_run_token="stage-1",
    next_run_token="stage-2",
  )).result(timeout=5)
  assert advanced == {"advanced": False, "stopped": True}

  db = SessionLocal()
  try:
    plan = db.query(models.GoalPlan).filter_by(chat_id=chat_id).one()
    chat = db.get(models.Chat, chat_id)
    assert plan.status == "stopped"
    assert [item["content"] for item in chat.pending_messages] == ["/goal clear"]
  finally:
    db.close()
