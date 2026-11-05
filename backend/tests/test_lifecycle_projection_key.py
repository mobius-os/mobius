"""Runtime versions the lifecycle cards chat detail projects beside the transcript."""
from datetime import timedelta

from app import models
from app.chat_waits import declare_wait
from app.routes.chats import _lifecycle_projection_key
from app.timeutil import now_naive_utc


def _idle_chat(client, auth, title="Lifecycle cards"):
  ts = round((now_naive_utc() - timedelta(minutes=5)).timestamp() * 1000)
  created = client.post("/api/chats", json={"title": title, "messages": [
    {"role": "user", "content": "watch CI", "ts": ts},
    {"role": "assistant", "content": "Waiting on CI.", "ts": ts + 1000},
  ]}, headers=auth)
  assert created.status_code == 200, created.text
  return created.json()["id"]


def _read(client, auth, chat_id):
  runtime = client.get(f"/api/chats/{chat_id}/runtime", headers=auth).json()
  detail = client.get(f"/api/chats/{chat_id}?limit=20&compact=1", headers=auth).json()
  assert detail["lifecycle_projection_key"] == runtime["lifecycle_projection_key"]
  return runtime, detail


def test_idle_wait_cancel_moves_the_runtime_proof_but_a_routine_check_does_not(
  client, auth, db,
):
  chat_id = _idle_chat(client, auth)
  wait = declare_wait(
    db, chat_id=chat_id, description="CI finishes", condition_owner="CI",
    kind="command", command="false", deadline_secs=3600,
  )
  armed, _ = _read(client, auth, chat_id)

  wait.checks_count += 1
  wait.last_checked_at = now_naive_utc()
  db.commit()
  checked, _ = _read(client, auth, chat_id)
  assert checked["lifecycle_projection_key"] == armed["lifecycle_projection_key"]

  response = client.post(f"/api/chat-waits/{wait.id}/cancel", headers=auth)
  assert response.status_code == 200, response.text
  cancelled, detail = _read(client, auth, chat_id)
  # The transcript version cannot see the new card; only the lifecycle key can.
  assert cancelled["updated_at"] == armed["updated_at"]
  assert cancelled["restart_observation_key"] == armed["restart_observation_key"]
  assert cancelled["lifecycle_projection_key"] != armed["lifecycle_projection_key"]
  assert [summary["id"] for message in detail["messages"]
          for summary in message.get("wait_summaries", [])] == [wait.id]


def test_goal_and_dependency_outcomes_move_the_key_but_open_plan_revisions_do_not(
  client, auth, db,
):
  chat_id = _idle_chat(client, auth)
  source_id = _idle_chat(client, auth, title="Source")
  goal = models.ChatGoal(
    id="lifecycle-goal", chat_id=chat_id, objective="Ship it",
    plan_json={"version": 1, "tasks": []},
  )
  db.add(goal)
  db.commit()

  def key(include_foreign_context=True):
    return _lifecycle_projection_key(
      db, chat_id, include_foreign_context=include_foreign_context,
    )

  open_key = key()
  goal.revision += 1
  goal.plan_json = {"version": 1, "tasks": [
    {"id": "build", "title": "Build", "status": "running"},
  ]}
  db.commit()
  assert key() == open_key, "the progress rail reads an open Goal from runtime"

  dependency = models.GoalDependency(
    id="lifecycle-dependency", waiting_goal_id=goal.id, waiting_chat_id=chat_id,
    target_kind="owner_card", target_key=f"card:{source_id}:question",
    target_chat_id=source_id, target_question_id="question",
  )
  db.add(dependency)
  db.commit()
  pending_key = key()
  dependency.resolved_at = now_naive_utc()
  dependency.outcome_json = {
    "kind": "answered", "summary": "Pick a region", "source_chat_id": source_id,
  }
  db.commit()
  resolved_key = key()
  assert resolved_key != pending_key

  db.get(models.Chat, source_id).title = "Renamed source"
  db.commit()
  assert key() != resolved_key, "a dependency card names its source chat"
  embed_key = key(include_foreign_context=False)
  db.get(models.Chat, source_id).title = "Renamed again"
  db.commit()
  assert key(include_foreign_context=False) == embed_key, (
    "an embed never sees another chat's identity, so it does not version it"
  )

  before_outcome = key()
  goal.status = "cancelled"
  goal.revision += 1
  goal.result = "Owner changed direction"
  goal.completed_at = now_naive_utc()
  db.commit()
  assert key() != before_outcome
