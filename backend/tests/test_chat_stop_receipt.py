"""A per-chat Stop answers with the exact run it ended.

An app that cancels work must bind that cancellation to the run Stop actually
addressed. Reading status after the response can observe a successor (an
owner resume, a Wait firing, a helper result); releasing that successor would
let the app start duplicate work. The receipt is therefore fixed at the
writer's Stop boundary, inside the per-chat transition lock, and only a Stop
that ended the run and held the queue issues one. It certifies only that
run: any later work runs under a different run id.
"""
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app import models
from app.chat_writer import PrepareChatStop, StartTurn, get_writer
from app.goals import goal_hold


def submit(command):
  return get_writer().submit(command).result(timeout=5)


def seed_run(db, chat, run_id, *, status, goal_id=None, age_minutes=1):
  run = models.ChatRun(
    id=run_id, root_run_id=run_id, chat_id=chat.id, status=status,
    goal_id=goal_id, goal_objective="Prepare the project" if goal_id else None,
    provider="codex",
    started_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
  )
  db.add(run)
  db.commit()
  return run


def seed_goal(db, chat):
  goal = models.ChatGoal(
    id="cycle-goal", chat_id=chat.id, objective="Prepare the project",
    revision=1, plan_json={"tasks": []},
  )
  db.add(goal)
  db.commit()
  return goal


def owner_resume(chat, token):
  return StartTurn(
    chat_id=chat.id, run_token=token, owner_input=True,
    user_msg={"role": "user", "content": "Resume", "cid": f"{token}-cid", "ts": 9},
  )


def runtime_run_id(client, auth, chat):
  response = client.get(f"/api/chats/{chat.id}/runtime", headers=auth)
  assert response.status_code == 200, response.text
  return response.json()["run_id"]


def test_receipt_keeps_the_revoked_run_when_the_owner_resumes_before_the_response(
  client, auth, db, chat, monkeypatch,
):
  stopped_run = seed_run(db, chat, "stopped-run", status="running")
  chat.pending_messages = [
    {"role": "user", "content": "queued", "cid": "queued-cid", "ts": 2},
  ]
  db.commit()
  from app.routes import delegations
  original_cancel = delegations.cancel_active_for_parent

  async def owner_resumes_after_the_stop_boundary(session, parent_chat_id):
    # Stop has released the transition lock; the owner's resume is admitted
    # before this response is built, exactly as a later status read would see.
    submit(owner_resume(chat, "resumed-run"))
    return await original_cancel(session, parent_chat_id)

  monkeypatch.setattr(
    delegations, "cancel_active_for_parent", owner_resumes_after_the_stop_boundary,
  )
  response = client.post("/api/chat/stop", json={"chat_id": chat.id}, headers=auth)

  assert response.status_code == 200, response.text
  body = response.json()
  assert body["stopped"] is True
  assert body["cleared_pending_cids"] == []
  assert body["cancelled_delegations"] == []
  assert body["stop_receipt"] == {"chat_id": chat.id, "run_id": "stopped-run"}
  # Status now names the successor: binding Cancel to it would release it.
  assert runtime_run_id(client, auth, chat) == "resumed-run"
  db.expire_all()
  assert stopped_run.status == "stopped"
  assert db.get(models.ChatRun, "resumed-run").status == "running"


def test_receipt_names_the_successor_when_its_resume_won_the_stop_boundary(
  client, auth, db, chat,
):
  seed_run(db, chat, "earlier-run", status="completed", age_minutes=5)
  submit(owner_resume(chat, "resumed-run"))

  response = client.post("/api/chat/stop", json={"chat_id": chat.id}, headers=auth)

  assert response.status_code == 200, response.text
  assert response.json()["stop_receipt"] == {"chat_id": chat.id, "run_id": "resumed-run"}
  db.expire_all()
  assert db.get(models.ChatRun, "resumed-run").status == "stopped"
  assert db.get(models.ChatRun, "earlier-run").status == "completed"


def test_idle_paused_goal_stop_receipt_names_its_exact_lifecycle_without_a_process(
  client, auth, db, chat,
):
  goal = seed_goal(db, chat)
  seed_run(db, chat, "paused-run", status="completed", goal_id=goal.id)

  response = client.post("/api/chat/stop", json={"chat_id": chat.id}, headers=auth)

  assert response.status_code == 200, response.text
  assert response.json()["stopped"] is True
  assert response.json()["stop_receipt"] == {"chat_id": chat.id, "run_id": "paused-run"}
  assert runtime_run_id(client, auth, chat) == "paused-run"
  db.expire_all()
  assert goal.status == "stopped" and goal_hold(goal)["run_id"] == "paused-run"
  assert db.query(models.ChatRun).filter_by(chat_id=chat.id).count() == 1


def test_idle_stop_receipt_matches_status_when_a_later_turn_followed_the_goal(
  client, auth, db, chat,
):
  goal = seed_goal(db, chat)
  seed_run(db, chat, "goal-run", status="completed", goal_id=goal.id, age_minutes=5)
  seed_run(db, chat, "later-question", status="completed", age_minutes=1)

  response = client.post("/api/chat/stop", json={"chat_id": chat.id}, headers=auth)

  assert response.status_code == 200, response.text
  # The receipt names the lifecycle status reports, so an unchanged chat matches.
  assert response.json()["stop_receipt"] == {"chat_id": chat.id, "run_id": "later-question"}
  assert runtime_run_id(client, auth, chat) == "later-question"
  db.expire_all()
  # Stop still holds the Goal it presented; only the receipt names the latest run.
  assert goal.status == "stopped" and goal_hold(goal)["run_id"] == "goal-run"


def test_stop_of_a_chat_that_never_ran_receipts_no_run(client, auth, chat):
  response = client.post("/api/chat/stop", json={"chat_id": chat.id}, headers=auth)

  assert response.status_code == 200, response.text
  assert response.json()["stop_receipt"] == {"chat_id": chat.id, "run_id": None}
  assert runtime_run_id(client, auth, chat) is None


def test_unconfirmed_stop_issues_no_receipt_and_keeps_the_run(
  client, auth, db, chat, monkeypatch,
):
  from app import chat as runtime
  run = seed_run(db, chat, "draining-run", status="running")
  handle = SimpleNamespace(kind="codex")
  monkeypatch.setattr(runtime.registry, "get_handles", lambda _: [handle])

  async def refuses_to_stop(*args, **kwargs):
    return False, False

  monkeypatch.setattr(runtime, "_stop_handle_with_escalation", refuses_to_stop)
  response = client.post("/api/chat/stop", json={"chat_id": chat.id}, headers=auth)

  assert response.status_code == 200, response.text
  body = response.json()
  assert body["stopped"] is False
  assert body["stop_receipt"] is None
  assert body["cancelled_delegations"] == []
  db.expire_all()
  assert run.status == "running"


def test_stop_during_a_restart_drain_holds_the_queue_and_issues_a_receipt(
  client, auth, db, chat, monkeypatch,
):
  """Holding deletes nothing, so a Stop inside the drain holds the queue like
  any other Stop: it survives the restart and runs only when the owner sends."""
  from app import chat as runtime
  seed_run(db, chat, "drained-run", status="running")
  chat.pending_messages = [{"role": "user", "content": "queued", "cid": "queued-cid", "_owner_input_at": "2026-11-06T10:00:00+00:00"}]
  db.commit()
  monkeypatch.setattr(runtime, "draining", True)

  response = client.post("/api/chat/stop", json={"chat_id": chat.id}, headers=auth)

  assert response.status_code == 200, response.text
  body = response.json()
  assert body["stopped"] is True
  assert body["cleared_pending_cids"] == []
  assert body["stop_receipt"] == {"chat_id": chat.id, "run_id": "drained-run"}
  db.expire_all()
  assert [(row["cid"], row.get("held")) for row in db.get(models.Chat, chat.id).pending_messages] == [
    ("queued-cid", True),
  ]


def test_global_stop_sweep_issues_no_per_chat_receipt(client, auth, db, chat):
  seed_run(db, chat, "swept-run", status="running")

  response = client.post("/api/chat/stop", json={"chat_id": ""}, headers=auth)

  assert response.status_code == 200, response.text
  assert response.json()["stop_receipt"] is None


def test_writer_stop_boundary_reports_the_latest_run_for_generic_and_explicit_stops(db, chat):
  goal = seed_goal(db, chat)
  seed_run(db, chat, "goal-run", status="completed", goal_id=goal.id, age_minutes=5)
  seed_run(db, chat, "live-run", status="running", age_minutes=1)

  generic = submit(PrepareChatStop(chat_id=chat.id))
  explicit = submit(PrepareChatStop(chat_id=chat.id, actor="owner"))

  assert generic.run_id == "live-run" and explicit.run_id == "live-run"
  db.expire_all()
  # Stop of an unrelated live turn still never pauses the retained Goal.
  assert goal.status == "open" and goal.hold_json is None


def test_resume_contending_for_the_stop_gate_is_admitted_after_the_receipt(
  db, chat, monkeypatch,
):
  import asyncio

  from app import chat as runtime
  from app import chat_queue

  seed_run(db, chat, "stopped-run", status="running")
  resumed = []

  async def scenario():
    async def owner_resume_through_the_send_gate():
      async with chat_queue.get_transition_lock(chat.id):
        await runtime._await_ack(get_writer().submit(owner_resume(chat, "resumed-run")))
        resumed.append("resumed-run")

    contender = None
    real_finish = runtime._finish_run_strict

    async def finish_while_the_owner_resumes(chat_id, **kwargs):
      nonlocal contender
      contender = asyncio.create_task(owner_resume_through_the_send_gate())
      for _ in range(5):
        await asyncio.sleep(0)
      assert resumed == [], "a resume must not pass Stop's transition gate"
      return await real_finish(chat_id, **kwargs)

    monkeypatch.setattr(runtime, "_finish_run_strict", finish_while_the_owner_resumes)
    outcome = await runtime.stop_chat_for(chat.id, actor="owner")
    await contender
    return outcome

  outcome = asyncio.run(asyncio.wait_for(scenario(), timeout=10))

  assert outcome.stopped is True
  assert outcome.receipt == {"chat_id": chat.id, "run_id": "stopped-run"}
  assert resumed == ["resumed-run"]
  db.expire_all()
  assert db.get(models.ChatRun, "stopped-run").status == "stopped"
  assert db.get(models.ChatRun, "resumed-run").status == "running"


def test_failed_queue_hold_issues_no_receipt_and_keeps_the_queue(
  client, auth, db, chat, monkeypatch,
):
  """A receipt promises no queued turn starts by itself; a hold the writer
  failed to persist leaves them runnable, so Stop must not issue one."""
  from app.chat_writer import ChatWriterActor
  seed_run(db, chat, "stopped-run", status="running")
  chat.pending_messages = [{"role": "user", "content": "queued", "cid": "queued-cid"}]
  db.commit()

  def hold_fails(self, db, cmd):
    raise RuntimeError("database is locked")

  monkeypatch.setattr(ChatWriterActor, "_hold_pending", hold_fails)

  response = client.post("/api/chat/stop", json={"chat_id": chat.id}, headers=auth)

  assert response.status_code == 200, response.text
  body = response.json()
  assert body["stopped"] is True
  assert body["cleared_pending_cids"] == []
  assert body["stop_receipt"] is None
  db.expire_all()
  assert [row["cid"] for row in db.get(models.Chat, chat.id).pending_messages] == ["queued-cid"]


def test_stop_that_could_not_close_the_run_issues_no_receipt(
  client, auth, db, chat, monkeypatch,
):
  """With no process to stop, the run only ends when FinishRun lands; a
  receipt for a run that is still open would certify nothing."""
  from app.chat_writer import ChatWriterActor
  seed_run(db, chat, "orphaned-run", status="running")

  def finish_fails(self, db, cmd):
    raise RuntimeError("database is locked")

  monkeypatch.setattr(ChatWriterActor, "_finish_run", finish_fails)

  response = client.post("/api/chat/stop", json={"chat_id": chat.id}, headers=auth)

  assert response.status_code == 200, response.text
  assert response.json()["stopped"] is True
  assert response.json()["stop_receipt"] is None
  db.expire_all()
  assert db.get(models.ChatRun, "orphaned-run").status == "running"


def test_a_wait_firing_after_stop_runs_under_a_different_run_id(
  client, auth, db, chat, monkeypatch,
):
  """Stop does not disarm a Wait outside a Goal; when it fires, the work it
  starts is a new run, so a receipt-bound cancellation never covers it."""
  import asyncio

  from app import chat as runtime
  from app import chat_waits
  from app.timeutil import now_naive_utc

  seed_run(db, chat, "declaring-run", status="running")
  row = chat_waits.declare_wait(
    db, chat_id=chat.id, description="after stop", condition_owner="test",
    kind="command", command="true", deadline_secs=3600,
    created_by_run_id="declaring-run",
  )
  db.commit()

  response = client.post("/api/chat/stop", json={"chat_id": chat.id}, headers=auth)
  assert response.status_code == 200, response.text
  receipt = response.json()["stop_receipt"]
  assert receipt == {"chat_id": chat.id, "run_id": "declaring-run"}

  db.expire_all()
  row = db.get(models.ChatWait, row.id)
  row.status = "met"
  row.met_at = now_naive_utc()
  db.commit()
  monkeypatch.setattr(runtime, "_schedule_continuation", lambda **_kwargs: True)
  assert asyncio.run(chat_waits._deliver_resume(row.id)) is True

  resumed = runtime_run_id(client, auth, chat)
  assert resumed == f"wait-resume-{row.id}"
  assert resumed != receipt["run_id"]
