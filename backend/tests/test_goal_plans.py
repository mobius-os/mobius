"""Durable Goal-plan validation, ordering, progress, and route contracts."""

from datetime import timedelta
import importlib.util
import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import auth as auth_mod, models
from app import broadcast as broadcast_mod
from app.runner_registry import RunnerKind, registry


def _goal_plan_script():
  path = Path(__file__).resolve().parents[1] / "scripts" / "goal_plan.py"
  spec = importlib.util.spec_from_file_location("goal_plan_script", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _active_goal(client, owner_token, db):
  auth = {"Authorization": f"Bearer {owner_token}"}
  response = client.post("/api/chats", json={"title": "Planned goal"}, headers=auth)
  assert response.status_code == 200, response.text
  chat_id = response.json()["id"]
  db.add(models.ChatRun(
    id="goal-root",
    root_run_id="goal-root",
    chat_id=chat_id,
    status="running",
    provider="codex",
    goal_objective="Ship the release",
    goal_id="goal-1",
  ))
  db.commit()
  return auth, chat_id


def _agent_run_auth(db, chat_id, run_id):
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(
    chat_id,
    run_id,
    owner.username,
    owner.token_epoch,
    expires_delta=timedelta(minutes=5),
  )
  return {"Authorization": f"Bearer {token}"}


def test_terminal_goal_history_projects_onto_final_assistant_message(
  client, owner_token, db,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  base = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
  created = client.post(
    "/api/chats",
    json={
      "title": "Goal history",
      "messages": [
        {"role": "user", "content": "start", "ts": 1_787_486_400_000},
        {"role": "assistant", "content": "first leg", "ts": 1_787_486_401_000},
        {"role": "user", "content": "continue", "ts": 1_787_486_410_000},
        {"role": "assistant", "content": "finished", "ts": 1_787_486_411_000},
      ],
    },
    headers=auth,
  )
  chat_id = created.json()["id"]
  db.add_all([
    models.ChatRun(
      id="history-root", root_run_id="history-root", chat_id=chat_id,
      status="completed", provider="codex", goal_objective="Ship safely",
      goal_id="history-goal", started_at=base,
      ended_at=base + timedelta(seconds=5),
      goal_plan_json={
        "version": 1,
        "updated_at": base.isoformat(),
        "tasks": [{
          "id": "ship", "title": "Ship safely", "status": "completed",
          "depends_on": [],
        }],
      },
      goal_plan_revision=1,
    ),
    models.ChatRun(
      id="history-resume", root_run_id="history-root", chat_id=chat_id,
      status="completed", provider="codex", goal_objective="Ship safely",
      goal_id="history-goal", started_at=base + timedelta(seconds=10),
      ended_at=base + timedelta(seconds=15),
    ),
  ])
  db.commit()

  response = client.get(f"/api/chats/{chat_id}?limit=20", headers=auth)
  assert response.status_code == 200, response.text
  messages = response.json()["messages"]
  assert "goal_summaries" not in messages[1]
  summary = messages[3]["goal_summaries"][0]
  assert summary["id"] == "history-goal"
  assert summary["objective"] == "Ship safely"
  assert summary["status"] == "completed"
  assert summary["duration_seconds"] == 15
  assert summary["plan"]["summary"] == {
    "completed": 1,
    "total": 1,
    "running": [],
    "ready": [],
    "can_complete": True,
    "completion_blockers": [],
  }


def test_terminal_goal_history_respects_message_pagination(
  client, owner_token, db,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  base = datetime(2026, 8, 23, 13, 0, tzinfo=UTC)
  created = client.post(
    "/api/chats",
    json={
      "title": "Paginated goal history",
      "messages": [
        {"role": "user", "content": "older", "ts": 1_787_490_000_000},
        {"role": "assistant", "content": "done", "ts": 1_787_490_001_000},
        {"role": "user", "content": "newer", "ts": 1_787_490_010_000},
      ],
    },
    headers=auth,
  )
  chat_id = created.json()["id"]
  db.add(models.ChatRun(
    id="old-goal-run", root_run_id="old-goal-run", chat_id=chat_id,
    status="completed", provider="claude", goal_objective="Old Goal",
    goal_id="old-goal", started_at=base,
    ended_at=base + timedelta(seconds=3),
  ))
  db.commit()

  latest = client.get(f"/api/chats/{chat_id}?limit=1", headers=auth).json()
  assert latest["offset"] == 2
  assert "goal_summaries" not in latest["messages"][0]
  older = client.get(
    f"/api/chats/{chat_id}?limit=2&before=2", headers=auth,
  ).json()
  assert older["messages"][1]["goal_summaries"][0]["id"] == "old-goal"


def test_current_turn_promotes_atomically_without_a_goal_message(
  client, owner_token, db,
):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  created = client.post("/api/chats", json={"title": "Ordinary"}, headers=owner_auth)
  chat_id = created.json()["id"]
  db.add(models.ChatRun(
    id="ordinary-run",
    root_run_id="ordinary-run",
    chat_id=chat_id,
    status="running",
    provider="codex",
  ))
  db.commit()
  agent_auth = _agent_run_auth(db, chat_id, "ordinary-run")

  broadcast = broadcast_mod.create_broadcast(chat_id)
  try:
    promoted = client.post(
      f"/api/chats/{chat_id}/goal",
      json={"objective": "Repair every defect and verify the suite"},
      headers=agent_auth,
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json() == {
      "objective": "Repair every defect and verify the suite",
      "root_run_id": "ordinary-run",
      "run_id": "ordinary-run",
      "state": "promoted",
    }
    assert [
      event["type"] for event in broadcast.event_log
    ] == ["goal_activated"]
    db.expire_all()
    run = db.query(models.ChatRun).filter(models.ChatRun.id == "ordinary-run").one()
    assert run.goal_objective == "Repair every defect and verify the suite"
    chat = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
    assert all(
      "/goal" not in str(message.get("content", ""))
      for message in chat.messages
    )

    retry = client.post(
      f"/api/chats/{chat_id}/goal",
      json={"objective": "Repair every defect and verify the suite"},
      headers=agent_auth,
    )
    assert retry.status_code == 200
    assert retry.json()["state"] == "active"
    assert [
      event["type"] for event in broadcast.event_log
    ] == ["goal_activated"]
    conflict = client.post(
      f"/api/chats/{chat_id}/goal",
      json={"objective": "Do something else"},
      headers=agent_auth,
    )
    assert conflict.status_code == 409

    plan = client.put(
      f"/api/chats/{chat_id}/goal-plan",
      json={
        "expected_revision": 0,
        "tasks": [{"id": "repair", "title": "Repair every defect"}],
      },
      headers=agent_auth,
    )
    assert plan.status_code == 200, plan.text
    assert (
      plan.json()["plan"]["objective"]
      == "Repair every defect and verify the suite"
    )
  finally:
    broadcast_mod.remove_broadcast(chat_id)


def test_goal_promotion_rejects_browser_wrong_chat_and_terminal_run_tokens(
  client, owner_token, db,
):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  first = client.post("/api/chats", json={"title": "First"}, headers=owner_auth).json()
  second = client.post("/api/chats", json={"title": "Second"}, headers=owner_auth).json()
  db.add_all([
    models.ChatRun(
      id="live-run", root_run_id="live-run", chat_id=first["id"],
      status="running", provider="claude",
    ),
    models.ChatRun(
      id="settled-run", root_run_id="settled-run", chat_id=first["id"],
      status="completed", provider="claude",
    ),
  ])
  db.commit()
  agent_auth = _agent_run_auth(db, first["id"], "live-run")
  settled_auth = _agent_run_auth(db, first["id"], "settled-run")

  browser = client.post(
    f"/api/chats/{first['id']}/goal",
    json={"objective": "Not allowed"}, headers=owner_auth,
  )
  assert browser.status_code == 403
  wrong_chat = client.post(
    f"/api/chats/{second['id']}/goal",
    json={"objective": "Not allowed"}, headers=agent_auth,
  )
  assert wrong_chat.status_code == 403
  terminal = client.post(
    f"/api/chats/{first['id']}/goal",
    json={"objective": "Too late"}, headers=settled_auth,
  )
  assert terminal.status_code == 401
  assert "no longer active" in terminal.json()["detail"]


def test_confirmed_goal_clear_is_exact_and_preserves_real_pending_work(
  client, owner_token, db,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = client.post(
    "/api/chats", json={"title": "Clear directly"}, headers=auth,
  ).json()["id"]
  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  chat.pending_messages = [{
    "role": "user", "content": "keep this follow-up", "ts": 2,
    "cid": "real-follow-up",
  }]
  db.add(models.ChatRun(
    id="clear-goal-run", root_run_id="clear-goal-run", chat_id=chat_id,
    status="running", provider="claude", goal_objective="Clear safely",
    goal_id="clear-goal-id",
  ))
  db.commit()

  stale = client.request(
    "DELETE", f"/api/chats/{chat_id}/goal",
    json={"goal_id": "stale-goal-id"}, headers=auth,
  )
  assert stale.status_code == 409, stale.text
  db.expire_all()
  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  run = db.query(models.ChatRun).filter(
    models.ChatRun.id == "clear-goal-run",
  ).one()
  assert chat.dismissed_goal_id is None
  assert [row["cid"] for row in chat.pending_messages] == ["real-follow-up"]
  assert run.status == "running"

  cleared = client.request(
    "DELETE", f"/api/chats/{chat_id}/goal",
    json={"goal_id": "clear-goal-id"}, headers=auth,
  )
  assert cleared.status_code == 200, cleared.text
  assert cleared.json() == {"cleared": True, "goal": None}
  db.expire_all()
  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  run = db.query(models.ChatRun).filter(
    models.ChatRun.id == "clear-goal-run",
  ).one()
  assert chat.dismissed_goal_id == "clear-goal-id"
  assert [row["cid"] for row in chat.pending_messages] == ["real-follow-up"]
  assert run.status == "stopped"


def test_completed_plan_clear_dismisses_without_interrupting_final_response(
  client, owner_token, db,
):
  class FinishingGoal:
    chat_id = ""
    kind = RunnerKind.CODEX_SDK

    def __init__(self, chat_id):
      self.chat_id = chat_id
      self.clear_calls = 0

    async def clear_goal(self):
      self.clear_calls += 1

  auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = client.post(
    "/api/chats", json={"title": "Finishing goal"}, headers=auth,
  ).json()["id"]
  run = models.ChatRun(
    id="finishing-goal-run", root_run_id="finishing-goal-run",
    chat_id=chat_id, status="running", provider="codex",
    goal_objective="Finish without interruption", goal_id="finishing-goal-id",
    goal_plan_json={
      "version": 1,
      "tasks": [{
        "id": "finish", "title": "Finish the work", "status": "completed",
        "depends_on": [],
      }],
    },
    goal_plan_revision=1,
  )
  db.add(run)
  db.commit()
  handle = FinishingGoal(chat_id)
  registry.register(handle)

  try:
    cleared = client.request(
      "DELETE", f"/api/chats/{chat_id}/goal",
      json={"goal_id": "finishing-goal-id"}, headers=auth,
    )
  finally:
    registry.unregister(chat_id, handle.kind)

  assert cleared.status_code == 200, cleared.text
  assert cleared.json() == {"cleared": True, "goal": None}
  assert handle.clear_calls == 0
  db.expire_all()
  chat = db.query(models.Chat).filter(models.Chat.id == chat_id).one()
  persisted_run = db.query(models.ChatRun).filter(
    models.ChatRun.id == "finishing-goal-run",
  ).one()
  assert chat.dismissed_goal_id == "finishing-goal-id"
  assert persisted_run.status == "running"
  assert persisted_run.ended_at is None


def test_paused_goal_clear_does_not_interrupt_a_later_ordinary_turn(
  client, owner_token, db,
):
  class OrdinaryTurn:
    chat_id = ""
    kind = RunnerKind.CODEX_SDK

    def __init__(self, chat_id):
      self.chat_id = chat_id
      self.clear_calls = 0

    async def clear_goal(self):
      self.clear_calls += 1

  auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = client.post(
    "/api/chats", json={"title": "Retained goal"}, headers=auth,
  ).json()["id"]
  started_at = datetime.now(UTC)
  db.add_all([
    models.ChatRun(
      id="paused-retained-goal", root_run_id="paused-retained-goal",
      chat_id=chat_id, status="stopped", provider="codex",
      goal_objective="Retained history", goal_id="retained-goal-id",
      started_at=started_at,
    ),
    models.ChatRun(
      id="later-ordinary-turn", root_run_id="later-ordinary-turn",
      chat_id=chat_id, status="running", provider="codex",
      started_at=started_at + timedelta(seconds=1),
    ),
  ])
  db.commit()
  handle = OrdinaryTurn(chat_id)
  registry.register(handle)

  try:
    cleared = client.request(
      "DELETE", f"/api/chats/{chat_id}/goal",
      json={"goal_id": "retained-goal-id"}, headers=auth,
    )
  finally:
    registry.unregister(chat_id, handle.kind)

  assert cleared.status_code == 200, cleared.text
  assert handle.clear_calls == 0
  db.expire_all()
  ordinary_run = db.query(models.ChatRun).filter(
    models.ChatRun.id == "later-ordinary-turn",
  ).one()
  assert ordinary_run.status == "running"
  assert ordinary_run.ended_at is None


def test_goal_wait_ownership_excludes_a_later_ordinary_turn(db, chat):
  from app.chat_waits import declare_wait
  from app.goal_plans import presented_goal

  started_at = datetime.now(UTC)
  goal_run = models.ChatRun(
    id="waiting-goal-run", root_run_id="waiting-goal-run", chat_id=chat.id,
    status="parked", provider="codex", goal_objective="Wait precisely",
    goal_id="waiting-goal-id", started_at=started_at,
  )
  ordinary_run = models.ChatRun(
    id="later-ordinary-run", root_run_id="later-ordinary-run",
    chat_id=chat.id, status="running", provider="codex",
    started_at=started_at + timedelta(seconds=1),
  )
  db.add_all([goal_run, ordinary_run])
  chat.pending_question_id = "ordinary-question"
  db.commit()
  ordinary_wait = declare_wait(
    db,
    chat_id=chat.id,
    description="ordinary wait",
    kind="timer",
    delay_secs=60,
    created_by_run_id=ordinary_run.id,
  )

  assert "wait_kind" not in presented_goal(db, chat.id)

  ordinary_run.status = "completed"
  ordinary_wait.status = "cancelled"
  chat.pending_question_id = "goal-question"
  db.commit()
  assert presented_goal(db, chat.id)["wait_kind"] == "owner_question"

  chat.pending_question_id = None
  declare_wait(
    db,
    chat_id=chat.id,
    description="goal wait",
    kind="timer",
    delay_secs=60,
    created_by_run_id=goal_run.id,
  )
  assert presented_goal(db, chat.id)["wait_kind"] == "monitor"


def test_legacy_queued_goal_clear_is_retired_without_opening_a_turn(db, chat):
  from app.chat_writer import PromotePending, get_writer

  chat.pending_messages = [{
    "role": "user", "content": "/goal clear", "ts": 1,
    "cid": "legacy-goal-clear",
  }]
  db.commit()

  result = get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="must-not-open",
  )).result(timeout=5)

  assert result["promoted"] is None
  db.expire_all()
  assert db.query(models.Chat).filter(
    models.Chat.id == chat.id,
  ).one().pending_messages == []
  assert db.query(models.ChatRun).filter(
    models.ChatRun.id == "must-not-open",
  ).first() is None


def test_goal_promotion_rejects_delegation_and_app_scope_tokens(
  client, owner_token, db,
):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = client.post(
    "/api/chats", json={"title": "Scoped"}, headers=owner_auth,
  ).json()["id"]
  db.add(models.ChatRun(
    id="scoped-run", root_run_id="scoped-run", chat_id=chat_id,
    status="running", provider="codex",
  ))
  app = models.App(
    name="Delegate", description="", slug="goal-scope-app",
    source_dir="/tmp/mobius-tests/goal-scope-app",
    jsx_source="export default () => null", token_nonce="scope-nonce",
  )
  db.add(app)
  db.commit()
  db.refresh(app)

  owner = db.query(models.Owner).first()
  # (a) A delegated child holds a delegation-scope token; the promotion route
  # requires the owner-scope agent-run bearer, so the server boundary refuses
  # it before any writer work.
  delegation_auth = {
    "Authorization": "Bearer " + auth_mod.create_delegation_token(
      "delegation-scope", app.id, chat_id, "scoped-run",
      owner.username, owner.token_epoch,
    )
  }
  delegated = client.post(
    f"/api/chats/{chat_id}/goal",
    json={"objective": "Delegated cannot promote"}, headers=delegation_auth,
  )
  assert delegated.status_code in (401, 403), delegated.text

  # (b) A write-scope app token (app_id set) is likewise rejected: it is not
  # the current owner agent run.
  app_auth = {
    "Authorization": "Bearer " + auth_mod.create_app_token(
      app.id, owner.username, owner.token_epoch, "scope-nonce",
    )
  }
  app_scoped = client.post(
    f"/api/chats/{chat_id}/goal",
    json={"objective": "App cannot promote"}, headers=app_auth,
  )
  assert app_scoped.status_code in (401, 403), app_scoped.text


def test_promote_run_to_goal_rejects_a_terminal_run(client, owner_token, db):
  from app import chat_writer

  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = client.post(
    "/api/chats", json={"title": "Terminal"}, headers=owner_auth,
  ).json()["id"]
  db.add(models.ChatRun(
    id="done-run", root_run_id="done-run", chat_id=chat_id,
    status="completed", provider="codex",
  ))
  db.commit()

  result = chat_writer.get_writer()._promote_run_to_goal(
    db,
    chat_writer.PromoteRunToGoal(
      chat_id=chat_id, run_token="done-run", objective="Too late",
    ),
  )
  assert isinstance(result, chat_writer.GoalPromotionRejected)
  assert result.reason == "run_not_active"


def test_promote_run_to_goal_rejects_a_superseded_run(client, owner_token, db):
  from app import chat_writer

  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = client.post(
    "/api/chats", json={"title": "Superseded"}, headers=owner_auth,
  ).json()["id"]
  now = datetime.now(UTC).replace(tzinfo=None)
  db.add_all([
    models.ChatRun(
      id="prior-run", root_run_id="prior-run", chat_id=chat_id,
      status="running", provider="codex",
      started_at=now - timedelta(minutes=1),
    ),
    models.ChatRun(
      id="newer-run", root_run_id="newer-run", chat_id=chat_id,
      status="running", provider="codex", started_at=now,
    ),
  ])
  db.commit()

  result = chat_writer.get_writer()._promote_run_to_goal(
    db,
    chat_writer.PromoteRunToGoal(
      chat_id=chat_id, run_token="prior-run", objective="Not current",
    ),
  )
  assert isinstance(result, chat_writer.GoalPromotionRejected)
  assert result.reason == "run_not_current"


def test_promote_run_to_goal_rejects_a_missing_logical_root(
  client, owner_token, db,
):
  from app import chat_writer

  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = client.post(
    "/api/chats", json={"title": "Rootless"}, headers=owner_auth,
  ).json()["id"]
  db.add(models.ChatRun(
    id="orphan-run", root_run_id="absent-root", chat_id=chat_id,
    status="running", provider="codex",
  ))
  db.commit()

  result = chat_writer.get_writer()._promote_run_to_goal(
    db,
    chat_writer.PromoteRunToGoal(
      chat_id=chat_id, run_token="orphan-run", objective="No root",
    ),
  )
  assert isinstance(result, chat_writer.GoalPromotionRejected)
  assert result.reason == "logical_root_missing"


def test_promotion_of_a_continuation_stamps_its_logical_root(
  client, owner_token, db,
):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = client.post(
    "/api/chats", json={"title": "Continued"}, headers=owner_auth,
  ).json()["id"]
  db.add_all([
    models.ChatRun(
      id="logical-root", root_run_id="logical-root", chat_id=chat_id,
      status="interrupted", provider="claude",
    ),
    models.ChatRun(
      id="physical-resume", root_run_id="logical-root", chat_id=chat_id,
      status="running", provider="claude",
    ),
  ])
  db.commit()

  promoted = client.post(
    f"/api/chats/{chat_id}/goal",
    json={"objective": "Finish the resumed migration"},
    headers=_agent_run_auth(db, chat_id, "physical-resume"),
  )

  assert promoted.status_code == 200, promoted.text
  assert promoted.json()["root_run_id"] == "logical-root"
  db.expire_all()
  roots = {
    row.id: row.goal_objective
    for row in db.query(models.ChatRun).filter(models.ChatRun.chat_id == chat_id)
  }
  assert roots == {
    "logical-root": "Finish the resumed migration",
    "physical-resume": "Finish the resumed migration",
  }


def test_goal_promotion_commit_failure_is_loud_and_atomic(
  client, owner_token, db, monkeypatch,
):
  from app import chat_writer

  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = client.post(
    "/api/chats", json={"title": "Atomic failure"}, headers=owner_auth,
  ).json()["id"]
  db.add(models.ChatRun(
    id="failing-run", root_run_id="failing-run", chat_id=chat_id,
    status="running", provider="codex",
  ))
  db.commit()

  def fail_commit(session):
    session.rollback()
    return False

  monkeypatch.setattr(chat_writer, "_commit_or_rollback", fail_commit)
  with pytest.raises(chat_writer._PersistFailed, match="PromoteRunToGoal"):
    chat_writer.get_writer()._promote_run_to_goal(
      db,
      chat_writer.PromoteRunToGoal(
        chat_id=chat_id,
        run_token="failing-run",
        objective="Persist all-or-nothing",
      ),
    )

  db.expire_all()
  run = db.query(models.ChatRun).filter(models.ChatRun.id == "failing-run").one()
  assert run.goal_objective is None


def test_parallel_roots_release_dependent_task_only_after_all_complete(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  tasks = [
    {"id": "a", "title": "Run A", "depends_on": []},
    {"id": "b", "title": "Run B", "depends_on": []},
    {"id": "c", "title": "Run C", "depends_on": ["a", "b"]},
  ]
  created = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={"expected_revision": 0, "tasks": tasks}, headers=auth,
  )
  assert created.status_code == 200, created.text
  plan = created.json()["plan"]
  assert plan["revision"] == 1
  assert plan["summary"] == {
    "completed": 0,
    "total": 3,
    "running": [],
    "ready": ["a", "b"],
    "can_complete": False,
    "completion_blockers": ["a", "b", "c"],
  }

  a_running = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/a",
    json={"expected_revision": 1, "status": "running"}, headers=auth,
  )
  assert a_running.status_code == 200, a_running.text
  assert a_running.json()["plan"]["summary"]["running"] == ["a"]

  premature = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/c",
    json={"expected_revision": 2, "status": "running"}, headers=auth,
  )
  assert premature.status_code == 422
  assert "dependencies complete" in premature.json()["detail"]

  revision = 2
  for task_id, status in (
    ("a", "completed"),
    ("b", "running"),
    ("b", "completed"),
    ("c", "running"),
  ):
    response = client.patch(
      f"/api/chats/{chat_id}/goal-plan/tasks/{task_id}",
      json={"expected_revision": revision, "status": status}, headers=auth,
    )
    assert response.status_code == 200, response.text
    revision += 1
  final = response.json()["plan"]
  assert final["summary"]["running"] == ["c"]
  assert final["summary"]["completed"] == 2
  assert final["summary"]["can_complete"] is False
  assert final["summary"]["completion_blockers"] == ["c"]


def test_repeated_task_needs_full_progress_and_stale_revision_cannot_overwrite(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  created = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={
      "expected_revision": 0,
      "tasks": [{
        "id": "repeat",
        "title": "Run the audit three times",
        "status": "running",
        "depends_on": [],
        "progress": {"current": 0, "total": 3},
      }],
    },
    headers=auth,
  )
  assert created.status_code == 200, created.text

  partial = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/repeat",
    json={"expected_revision": 1, "progress": {"current": 2, "total": 3}},
    headers=auth,
  )
  assert partial.status_code == 200, partial.text
  not_done = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/repeat",
    json={"expected_revision": 2, "status": "completed"}, headers=auth,
  )
  assert not_done.status_code == 422
  assert "repeated progress is full" in not_done.json()["detail"]

  stale = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/repeat",
    json={"expected_revision": 1, "note": "stale writer"}, headers=auth,
  )
  assert stale.status_code == 409

  full = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/repeat",
    json={"expected_revision": 2, "progress": {"current": 3, "total": 3}},
    headers=auth,
  )
  assert full.status_code == 200, full.text
  completed = client.patch(
    f"/api/chats/{chat_id}/goal-plan/tasks/repeat",
    json={"expected_revision": 3, "status": "completed"}, headers=auth,
  )
  assert completed.status_code == 200, completed.text
  assert completed.json()["plan"]["summary"]["completed"] == 1
  assert completed.json()["plan"]["summary"]["can_complete"] is True
  assert completed.json()["plan"]["summary"]["completion_blockers"] == []


def test_cancelled_work_is_removed_from_the_completion_route(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  created = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={
      "expected_revision": 0,
      "tasks": [
        {"id": "done", "title": "Required work", "status": "completed"},
        {"id": "removed", "title": "No longer needed", "status": "cancelled"},
      ],
    },
    headers=auth,
  )
  assert created.status_code == 200, created.text
  summary = created.json()["plan"]["summary"]
  assert summary["can_complete"] is True
  assert summary["completion_blockers"] == []


def test_nested_children_settle_before_parent_becomes_ready_to_verify(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  created = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={"expected_revision": 0, "tasks": [
      {"id": "b", "title": "Deliver B", "status": "running"},
      {"id": "x", "title": "Do X", "parent_id": "b"},
      {"id": "y", "title": "Do Y", "parent_id": "b"},
    ]}, headers=auth,
  )
  assert created.status_code == 200, created.text
  initial_tasks = {
    task["id"]: task for task in created.json()["plan"]["tasks"]
  }
  assert initial_tasks["b"]["ready"] is False
  assert initial_tasks["b"]["ready_to_verify"] is False
  assert initial_tasks["x"]["ready"] is True
  assert initial_tasks["y"]["ready"] is True
  revision = 1
  for task_id, status in (("x", "completed"), ("y", "completed")):
    response = client.patch(
      f"/api/chats/{chat_id}/goal-plan/tasks/{task_id}",
      json={"expected_revision": revision, "status": status}, headers=auth,
    )
    assert response.status_code == 200, response.text
    revision += 1
  tasks = {task["id"]: task for task in response.json()["plan"]["tasks"]}
  assert tasks["b"]["children"] == ["x", "y"]
  assert tasks["b"]["ready"] is False
  assert tasks["b"]["ready_to_verify"] is True

  incomplete_parent = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={"expected_revision": revision, "tasks": [
      {"id": "b", "title": "Deliver B", "status": "completed"},
      {"id": "x", "title": "Do X", "parent_id": "b"},
    ]}, headers=auth,
  )
  assert incomplete_parent.status_code == 422
  assert "children settle" in incomplete_parent.json()["detail"]


def test_mixed_parent_dependency_cycle_is_rejected_before_it_can_deadlock(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  response = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={"expected_revision": 0, "tasks": [
      {"id": "parent", "title": "Parent", "status": "running"},
      {
        "id": "child", "title": "Child", "parent_id": "parent",
        "depends_on": ["parent"],
      },
    ]}, headers=auth,
  )
  assert response.status_code == 422
  assert "completion cycle" in response.json()["detail"]


def test_failed_parent_never_masquerades_as_ready_to_verify(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  response = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={"expected_revision": 0, "tasks": [
      {"id": "parent", "title": "Parent", "status": "failed"},
      {
        "id": "child", "title": "Child", "parent_id": "parent",
        "status": "completed",
      },
    ]}, headers=auth,
  )
  assert response.status_code == 200, response.text
  tasks = {task["id"]: task for task in response.json()["plan"]["tasks"]}
  assert tasks["parent"]["ready_to_verify"] is False


def test_plan_follows_stable_goal_identity_across_a_new_logical_run(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  created = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={"expected_revision": 0, "tasks": [{"id": "a", "title": "A"}]},
    headers=auth,
  )
  assert created.status_code == 200, created.text
  db.query(models.ChatRun).filter(models.ChatRun.id == "goal-root").update({
    models.ChatRun.status: "interrupted",
  })
  db.add(models.ChatRun(
    id="recovered-root", root_run_id="recovered-root", chat_id=chat_id,
    status="running", provider="codex", goal_objective="Ship the release",
    goal_id="goal-1",
  ))
  db.commit()
  recovered = client.get(f"/api/chats/{chat_id}/goal-plan", headers=auth)
  assert recovered.status_code == 200, recovered.text
  assert recovered.json()["plan"]["revision"] == 1
  assert recovered.json()["plan"]["goal_id"] == "goal-1"


def test_plan_projects_recursive_delegation_ownership_without_transcripts(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  created = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={"expected_revision": 0, "tasks": [{
      "id": "b", "title": "Do B", "status": "completed",
    }]},
    headers=auth,
  )
  assert created.status_code == 200, created.text
  app = models.App(
    slug="goal-tree-subagents", source_dir="/tmp/goal-tree-subagents",
    name="Subagents", description="", jsx_source="",
  )
  db.add(app)
  db.flush()
  child_b = models.Chat(
    id="child-b", title="B", messages=[], created_by_app_id=app.id,
  )
  child_x = models.Chat(
    id="child-x", title="X", messages=[], created_by_app_id=app.id,
  )
  db.add_all([child_b, child_x])
  db.flush()
  common = {
    "app_id": app.id, "provider": "codex", "model": None,
    "effort": None, "scope": "read", "cwd": "/data",
    "prompt_sha256": hashlib.sha256(b"").hexdigest(),
  }
  db.add_all([
    models.Delegation(
      id="delegation-b", parent_chat_id=chat_id,
      parent_root_run_id="goal-root", task_key="b", child_chat_id="child-b",
      **common,
    ),
    models.Delegation(
      id="delegation-x", parent_chat_id="child-b",
      parent_root_run_id="child-b-run", task_key="x", child_chat_id="child-x",
      **common,
    ),
    models.ChatRun(
      id="child-b-run", root_run_id="child-b-run", chat_id="child-b",
      status="running", provider="codex",
    ),
    models.ChatRun(
      id="child-x-run", root_run_id="child-x-run", chat_id="child-x",
      status="running", provider="codex",
    ),
  ])
  db.commit()

  plan = client.get(f"/api/chats/{chat_id}/goal-plan", headers=auth).json()["plan"]
  assert plan["delegations"] == [{
    "id": "delegation-b", "task_key": "b", "provider": "codex",
    "status": "running", "children": [{
      "id": "delegation-x", "task_key": "x", "provider": "codex",
      "status": "running", "children": [],
    }],
  }]
  assert plan["summary"]["completed"] == 0
  assert plan["summary"]["can_complete"] is False
  assert plan["summary"]["completion_blockers"] == ["b", "x"]

  db.query(models.ChatRun).filter(models.ChatRun.id.in_([
    "goal-root", "child-b-run",
  ])).update({"status": "completed"}, synchronize_session=False)
  db.commit()
  descendant_plan = client.get(
    f"/api/chats/{chat_id}/goal-plan", headers=auth,
  ).json()["plan"]
  assert descendant_plan["delegations"][0]["status"] == "completed"
  assert descendant_plan["delegations"][0]["children"][0]["status"] == "running"
  assert descendant_plan["summary"]["completed"] == 0
  assert descendant_plan["summary"]["completion_blockers"] == ["b", "x"]
  runtime = client.get(f"/api/chats/{chat_id}/runtime", headers=auth).json()
  assert runtime["running"] is False
  assert runtime["active_goal_objective"] == "Ship the release"

  db.query(models.ChatRun).filter(models.ChatRun.id == "child-x-run").update({
    "status": "completed",
  })
  db.commit()
  settled_plan = client.get(
    f"/api/chats/{chat_id}/goal-plan", headers=auth,
  ).json()["plan"]
  assert settled_plan["delegations"][0]["status"] == "completed"
  assert settled_plan["delegations"][0]["children"][0]["status"] == "completed"
  assert settled_plan["summary"]["completed"] == 1
  assert settled_plan["summary"]["can_complete"] is True
  assert settled_plan["summary"]["completion_blockers"] == []
  assert client.get(
    f"/api/chats/{chat_id}/runtime", headers=auth,
  ).json()["active_goal_objective"] is None


def test_resumed_goal_projects_only_latest_delegation_attempt_per_task(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  created = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={"expected_revision": 0, "tasks": [{
      "id": "audit", "title": "Audit", "status": "completed",
    }]}, headers=auth,
  )
  assert created.status_code == 200, created.text
  app = models.App(
    slug="goal-retry-subagents", source_dir="/tmp/goal-retry-subagents",
    name="Subagents", description="", jsx_source="",
  )
  db.add(app)
  db.flush()
  old_child = models.Chat(
    id="goal-old-child", title="Old", messages=[], created_by_app_id=app.id,
  )
  new_child = models.Chat(
    id="goal-new-child", title="New", messages=[], created_by_app_id=app.id,
  )
  db.add_all([old_child, new_child])
  db.flush()
  common = {
    "app_id": app.id, "provider": "codex", "model": None,
    "effort": None, "scope": "read", "cwd": "/data",
    "prompt_sha256": hashlib.sha256(b"").hexdigest(),
  }
  now = datetime.now(UTC).replace(tzinfo=None)
  db.add_all([
    models.ChatRun(
      id="resumed-goal-run", root_run_id="resumed-goal-run", chat_id=chat_id,
      status="interrupted", provider="codex", goal_objective="Ship the release",
      goal_id="goal-1",
    ),
    models.Delegation(
      id="old-attempt", parent_chat_id=chat_id,
      parent_root_run_id="goal-root", task_key="audit",
      child_chat_id=old_child.id, created_at=now - timedelta(minutes=1),
      **common,
    ),
    models.Delegation(
      id="new-attempt", parent_chat_id=chat_id,
      parent_root_run_id="resumed-goal-run", task_key="audit",
      child_chat_id=new_child.id, created_at=now,
      **common,
    ),
    models.ChatRun(
      id="old-attempt-run", root_run_id="old-attempt-run",
      chat_id=old_child.id, status="completed", provider="codex",
    ),
    models.ChatRun(
      id="new-attempt-run", root_run_id="new-attempt-run",
      chat_id=new_child.id, status="running", provider="codex",
    ),
  ])
  db.commit()

  plan = client.get(f"/api/chats/{chat_id}/goal-plan", headers=auth).json()["plan"]
  assert [node["id"] for node in plan["delegations"]] == ["new-attempt"]
  assert plan["summary"]["completed"] == 0
  assert plan["summary"]["completion_blockers"] == ["audit"]


def test_completion_preflight_names_only_unfinished_required_work():
  helper = _goal_plan_script()
  plan = {
    "tasks": [
      {"id": "done", "title": "Finished", "status": "completed"},
      {"id": "removed", "title": "Removed", "status": "cancelled"},
      {"id": "next", "title": "Run final audit", "status": "pending"},
      {"id": "blocked", "title": "Resolve blocker", "status": "blocked"},
    ],
  }
  assert helper._completion_blockers(None) == []
  assert helper._completion_blockers(plan) == [
    "Run final audit", "Resolve blocker",
  ]
  plan["summary"] = {"completion_blockers": ["next", "live-child"]}
  assert helper._completion_blockers(plan) == [
    "Run final audit", "live-child",
  ]


def test_plan_rejects_cycles_missing_dependencies_and_non_goal_runs(
  client, owner_token, db,
):
  auth, chat_id = _active_goal(client, owner_token, db)
  cycle = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={
      "expected_revision": 0,
      "tasks": [
        {"id": "a", "title": "A", "depends_on": ["b"]},
        {"id": "b", "title": "B", "depends_on": ["a"]},
      ],
    }, headers=auth,
  )
  assert cycle.status_code == 422
  assert "cycle" in cycle.json()["detail"]

  missing = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={
      "expected_revision": 0,
      "tasks": [{"id": "a", "title": "A", "depends_on": ["gone"]}],
    }, headers=auth,
  )
  assert missing.status_code == 422
  assert "missing task" in missing.json()["detail"]

  created = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={"expected_revision": 0, "tasks": [{"id": "a", "title": "A"}]},
    headers=auth,
  )
  assert created.status_code == 200

  db.query(models.ChatRun).filter(models.ChatRun.id == "goal-root").update({
    models.ChatRun.status: "completed",
  })
  db.commit()
  inactive = client.get(f"/api/chats/{chat_id}/goal-plan", headers=auth)
  assert inactive.status_code == 200
  assert inactive.json()["plan"]["summary"]["can_complete"] is False
  rejected = client.put(
    f"/api/chats/{chat_id}/goal-plan",
    json={"expected_revision": 0, "tasks": [{"id": "a", "title": "A"}]},
    headers=auth,
  )
  assert rejected.status_code == 409
