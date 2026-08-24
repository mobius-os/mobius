"""Contracts for durable declared waits: declare, check, resume, restart-safety."""

import asyncio
from datetime import timedelta

import pytest

from app import auth as auth_mod
from app import chat as chat_mod
from app import chat_start as chat_start_mod
from app import chat_waits as chat_waits_mod
from app import models
from app.chat_waits import (
  WaitValidationError,
  cancel_wait,
  declare_wait,
  sweep_due_waits,
)
from app.continuations import WAIT_RESULT_MESSAGE_KIND
from app.run_state import goal_identity_for_run_start
from app.timeutil import now_naive_utc


def _owner_chat(client, owner_token):
  auth = {"Authorization": f"Bearer {owner_token}"}
  response = client.post("/api/chats", json={"title": "Waits"}, headers=auth)
  assert response.status_code == 200, response.text
  return response.json()["id"]


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


def _capture_starts(monkeypatch, *, running=False):
  starts = []

  async def fake_start(**kwargs):
    starts.append(kwargs)
    return True

  monkeypatch.setattr(chat_start_mod, "start_programmatic_chat_turn", fake_start)
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _cid: running)
  return starts


# ─────────────────────────── declare validation ───────────────────────────


def test_declare_validates_shape(client, owner_token, db):
  chat_id = _owner_chat(client, owner_token)
  with pytest.raises(WaitValidationError):
    declare_wait(db, chat_id=chat_id, description="", kind="command",
                 command="true")
  with pytest.raises(WaitValidationError):
    declare_wait(db, chat_id=chat_id, description="x", kind="command")
  with pytest.raises(WaitValidationError):
    declare_wait(db, chat_id=chat_id, description="x", kind="command",
                 command="true", interval_secs=5)
  with pytest.raises(WaitValidationError):
    declare_wait(db, chat_id=chat_id, description="x", kind="command",
                 command="true", interval_secs=0)
  with pytest.raises(WaitValidationError):
    declare_wait(db, chat_id=chat_id, description="x", kind="command",
                 command="true", deadline_secs=0)
  with pytest.raises(WaitValidationError):
    declare_wait(db, chat_id=chat_id, description="x", kind="timer",
                 delay_secs=120, command="true")


def test_declare_caps_armed_waits_per_chat(client, owner_token, db):
  chat_id = _owner_chat(client, owner_token)
  for index in range(chat_waits_mod.MAX_ARMED_WAITS_PER_CHAT):
    declare_wait(db, chat_id=chat_id, description=f"wait {index}",
                 kind="command", command="true")
  with pytest.raises(WaitValidationError):
    declare_wait(db, chat_id=chat_id, description="one too many",
                 kind="command", command="true")


def test_owner_chat_list_projects_durable_waiting_state(
  client, owner_token, db,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = _owner_chat(client, owner_token)
  row = declare_wait(
    db,
    chat_id=chat_id,
    description="resume after the gate",
    kind="command",
    command="false",
  )

  armed = client.get("/api/chats", headers=auth)
  assert armed.status_code == 200, armed.text
  chat = next(item for item in armed.json() if item["id"] == chat_id)
  assert chat["waiting"] is True
  assert chat["running"] is False

  cancel_wait(db, row)
  cancelled = client.get("/api/chats", headers=auth)
  assert cancelled.status_code == 200, cancelled.text
  chat = next(item for item in cancelled.json() if item["id"] == chat_id)
  assert chat["waiting"] is False


def test_deleting_chat_cancels_armed_waits(client, owner_token, db):
  auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = _owner_chat(client, owner_token)
  row = declare_wait(
    db,
    chat_id=chat_id,
    description="external task finishes",
    kind="command",
    command="false",
  )

  response = client.delete(f"/api/chats/{chat_id}", headers=auth)

  assert response.status_code == 204, response.text
  db.expire_all()
  assert db.get(models.ChatWait, row.id).status == "cancelled"


def test_declare_route_requires_agent_run_bearer(client, owner_token, db):
  chat_id = _owner_chat(client, owner_token)
  payload = {"description": "CI green", "kind": "command", "command": "true"}

  plain = client.post(
    "/api/chat-waits", json=payload,
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert plain.status_code == 403, plain.text

  db.add(models.ChatRun(
    id="declaring-run",
    root_run_id="declaring-root",
    chat_id=chat_id,
    status="running",
    provider="claude",
  ))
  db.commit()
  agent = client.post(
    "/api/chat-waits", json=payload,
    headers=_agent_run_auth(db, chat_id, "declaring-run"),
  )
  assert agent.status_code == 200, agent.text
  body = agent.json()
  assert body["chat_id"] == chat_id
  assert body["status"] == "armed"


def test_agent_run_bearer_cannot_read_or_cancel_another_chats_wait(
  client, owner_token, db,
):
  own_chat_id = _owner_chat(client, owner_token)
  other_chat_id = _owner_chat(client, owner_token)
  other_wait = declare_wait(
    db,
    chat_id=other_chat_id,
    description="other chat's gate",
    kind="command",
    command="false",
  )
  db.add(models.ChatRun(
    id="bounded-wait-reader",
    root_run_id="bounded-wait-reader",
    chat_id=own_chat_id,
    status="running",
    provider="claude",
  ))
  db.commit()
  agent_auth = _agent_run_auth(db, own_chat_id, "bounded-wait-reader")

  listed = client.get(
    f"/api/chat-waits?chat_id={other_chat_id}", headers=agent_auth,
  )
  cancelled = client.post(
    f"/api/chat-waits/{other_wait.id}/cancel", headers=agent_auth,
  )

  assert listed.status_code == 403, listed.text
  assert cancelled.status_code == 403, cancelled.text
  db.expire_all()
  assert db.get(models.ChatWait, other_wait.id).status == "armed"


# ─────────────────────────── check + resume ───────────────────────────


def test_met_command_wait_resumes_idle_chat(client, owner_token, db, monkeypatch):
  chat_id = _owner_chat(client, owner_token)
  row = declare_wait(
    db, chat_id=chat_id, description="gate PR merged",
    kind="command", command="true", created_by_run_id="declaring-run",
  )
  # Make it due now.
  row.next_check_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  starts = _capture_starts(monkeypatch, running=False)

  delivered = asyncio.run(sweep_due_waits())

  assert delivered == 1
  assert len(starts) == 1
  assert starts[0]["chat_id"] == chat_id
  assert starts[0]["hidden"] is True
  assert starts[0]["message_kind"] == WAIT_RESULT_MESSAGE_KIND
  assert starts[0]["source_work_id"] == "declaring-run"
  assert "gate PR merged" in starts[0]["content"]
  assert '"outcome":"met"' in starts[0]["content"]
  db.expire_all()
  refreshed = db.get(models.ChatWait, row.id)
  assert refreshed.status == "met"
  assert refreshed.resume_delivered_at is not None

  # The latch holds: a second sweep never redelivers.
  assert asyncio.run(sweep_due_waits()) == 0
  assert len(starts) == 1


def test_unmet_command_wait_reschedules(client, owner_token, db, monkeypatch):
  chat_id = _owner_chat(client, owner_token)
  row = declare_wait(
    db, chat_id=chat_id, description="not yet",
    kind="command", command="false", interval_secs=120,
  )
  row.next_check_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  starts = _capture_starts(monkeypatch, running=False)

  assert asyncio.run(sweep_due_waits()) == 0

  assert starts == []
  db.expire_all()
  refreshed = db.get(models.ChatWait, row.id)
  assert refreshed.status == "armed"
  assert refreshed.checks_count == 1
  assert refreshed.last_exit_code == 1
  assert refreshed.next_check_at > now_naive_utc()


def test_broken_command_wait_wakes_with_diagnostic_instead_of_rotting(
  client, owner_token, db, monkeypatch,
):
  chat_id = _owner_chat(client, owner_token)
  row = declare_wait(
    db, chat_id=chat_id, description="malformed PR check",
    kind="command", command="broken check",
  )
  starts = _capture_starts(monkeypatch, running=False)

  async def broken(_command, *, wait_id=None):
    return (1, "accepts at most 1 arg, received 5\n")

  monkeypatch.setattr(chat_waits_mod, "_run_check", broken)

  delivered = asyncio.run(sweep_due_waits())

  assert delivered == 1
  assert len(starts) == 1
  assert '"outcome":"check_failed"' in starts[0]["content"]
  assert "accepts at most 1 arg" in starts[0]["content"]
  db.expire_all()
  refreshed = db.get(models.ChatWait, row.id)
  assert refreshed.status == "failed"
  assert refreshed.checks_count == 1
  assert refreshed.last_exit_code == 1


def test_command_wait_probes_on_next_supervisor_tick(
  client, owner_token, db,
):
  chat_id = _owner_chat(client, owner_token)
  before = now_naive_utc()
  row = declare_wait(
    db, chat_id=chat_id, description="probe now",
    kind="command", command="true", interval_secs=3600,
  )

  assert row.next_check_at >= before
  assert row.next_check_at < before + timedelta(seconds=5)


def test_check_output_is_drained_with_a_bounded_tail():
  exit_code, output = asyncio.run(chat_waits_mod._run_check(
    "python3 -c 'print(\"prefix-\" + \"x\" * 12000 + \"-tail\")'"
  ))

  assert exit_code == 0
  assert len(output) <= chat_waits_mod._OUTPUT_TAIL
  assert output.endswith("-tail\n")
  assert "prefix-" not in output


def test_due_checks_do_not_block_globally_behind_one_slow_probe(
  client, owner_token, db, monkeypatch,
):
  chat_id = _owner_chat(client, owner_token)
  slow = declare_wait(
    db, chat_id=chat_id, description="slow",
    kind="command", command="slow",
  )
  fast = declare_wait(
    db, chat_id=chat_id, description="fast",
    kind="command", command="fast",
  )
  slow.next_check_at = now_naive_utc() - timedelta(seconds=2)
  fast.next_check_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  fast_finished = asyncio.Event()
  active = 0
  peak_active = 0

  async def fake_check(row_id):
    nonlocal active, peak_active
    active += 1
    peak_active = max(peak_active, active)
    try:
      if row_id == slow.id:
        await asyncio.wait_for(fast_finished.wait(), timeout=0.5)
      else:
        fast_finished.set()
    finally:
      active -= 1

  monkeypatch.setattr(chat_waits_mod, "_check_one", fake_check)

  assert asyncio.run(sweep_due_waits()) == 0
  assert fast_finished.is_set()
  assert 1 < peak_active <= chat_waits_mod.MAX_CONCURRENT_CHECKS


def test_deadline_expiry_wakes_the_chat_instead_of_rotting(
  client, owner_token, db, monkeypatch,
):
  chat_id = _owner_chat(client, owner_token)
  row = declare_wait(
    db, chat_id=chat_id, description="never comes",
    kind="command", command="false",
  )
  row.next_check_at = now_naive_utc() - timedelta(seconds=1)
  row.deadline_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  starts = _capture_starts(monkeypatch, running=False)

  delivered = asyncio.run(sweep_due_waits())

  assert delivered == 1
  assert len(starts) == 1
  assert '"outcome":"deadline_expired"' in starts[0]["content"]
  db.expire_all()
  assert db.get(models.ChatWait, row.id).status == "expired"


def test_timer_wait_fires_after_due(client, owner_token, db, monkeypatch):
  chat_id = _owner_chat(client, owner_token)
  row = declare_wait(
    db, chat_id=chat_id, description="check back later",
    kind="timer", delay_secs=3600,
  )
  starts = _capture_starts(monkeypatch, running=False)

  # Not due yet: nothing happens.
  assert asyncio.run(sweep_due_waits()) == 0
  assert starts == []

  row.next_check_at = now_naive_utc() - timedelta(seconds=1)
  row.due_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  assert asyncio.run(sweep_due_waits()) == 1
  assert len(starts) == 1
  assert '"outcome":"met"' in starts[0]["content"]


def test_met_wait_with_undelivered_resume_retries_after_restart(
  client, owner_token, db, monkeypatch,
):
  """A crash between met and delivered must redeliver, not lose the resume."""
  chat_id = _owner_chat(client, owner_token)
  row = declare_wait(
    db, chat_id=chat_id, description="met before crash",
    kind="command", command="true",
  )
  row.status = "met"
  row.met_at = now_naive_utc()
  db.commit()
  starts = _capture_starts(monkeypatch, running=False)

  assert asyncio.run(sweep_due_waits()) == 1
  assert len(starts) == 1


def test_running_chat_gets_pending_append_not_a_turn(
  client, owner_token, db, monkeypatch,
):
  chat_id = _owner_chat(client, owner_token)
  row = declare_wait(
    db, chat_id=chat_id, description="while busy",
    kind="command", command="true",
  )
  row.next_check_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  starts = _capture_starts(monkeypatch, running=True)
  appended = []

  async def fake_append(command):
    appended.append(command)

    class _Ack:
      pass
    return _Ack()

  class _FakeWriter:
    def submit(self, command):
      return command

  async def fake_await_ack(command):
    appended.append(command)
    return {}

  monkeypatch.setattr(
    "app.chat_writer.get_writer", lambda: _FakeWriter(),
  )
  monkeypatch.setattr("app.chat_writer.await_ack", fake_await_ack)

  delivered = asyncio.run(sweep_due_waits())

  assert delivered == 1
  assert starts == []
  assert len(appended) == 1
  assert appended[0].chat_id == chat_id
  assert appended[0].user_msg["kind"] == WAIT_RESULT_MESSAGE_KIND


def test_parked_chat_gets_pending_append_never_a_clobbering_turn(
  client, owner_token, db, monkeypatch,
):
  """A limit-parked chat reads as not-running, but StartTurn would supersede
  the park as owner intent — the wake must queue instead (finding: park
  clobber)."""
  chat_id = _owner_chat(client, owner_token)
  db.add(models.ChatRun(
    id="parked-run",
    root_run_id="parked-root",
    chat_id=chat_id,
    status="parked",
    park_reason="limit",
    parked_until=now_naive_utc() + timedelta(hours=3),
    provider="claude",
  ))
  db.commit()
  row = declare_wait(
    db, chat_id=chat_id, description="during park",
    kind="command", command="true",
  )
  row.next_check_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  starts = _capture_starts(monkeypatch, running=False)
  appended = []

  class _FakeWriter:
    def submit(self, command):
      return command

  async def fake_await_ack(command):
    appended.append(command)
    return {}

  monkeypatch.setattr("app.chat_writer.get_writer", lambda: _FakeWriter())
  monkeypatch.setattr("app.chat_writer.await_ack", fake_await_ack)

  delivered = asyncio.run(sweep_due_waits())

  assert delivered == 1
  assert starts == []  # never a StartTurn that would close the park
  assert len(appended) == 1
  assert appended[0].user_msg["kind"] == WAIT_RESULT_MESSAGE_KIND
  db.expire_all()
  parked = db.get(models.ChatRun, "parked-run")
  assert parked.status == "parked"  # the park survives the wake


def test_interval_can_never_outrun_the_deadline(client, owner_token, db):
  """A large interval must not defer past deadline_at — declare clamps the
  first check and reschedules clamp later ones, so the expiry wake is never
  late by more than a sweep tick."""
  with pytest.raises(WaitValidationError):
    declare_wait(db, chat_id=_owner_chat(client, owner_token),
                 description="x", kind="command", command="true",
                 interval_secs=chat_waits_mod.MAX_INTERVAL_SECS + 1)

  chat_id = _owner_chat(client, owner_token)
  row = declare_wait(
    db, chat_id=chat_id, description="hourly check, short deadline",
    kind="command", command="false",
    interval_secs=chat_waits_mod.MAX_INTERVAL_SECS,
    deadline_secs=1800,
  )
  assert row.next_check_at <= row.deadline_at

  # A reschedule after an unmet check clamps too.
  row.next_check_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  asyncio.run(sweep_due_waits())
  db.expire_all()
  refreshed = db.get(models.ChatWait, row.id)
  assert refreshed.status == "armed"
  assert refreshed.next_check_at <= refreshed.deadline_at


def test_timer_delay_cannot_exceed_deadline_cap(client, owner_token, db):
  chat_id = _owner_chat(client, owner_token)
  with pytest.raises(WaitValidationError):
    declare_wait(
      db, chat_id=chat_id, description="too far out",
      kind="timer", delay_secs=chat_waits_mod.MAX_DEADLINE_SECS + 3600,
    )


def test_cancelled_wait_is_never_checked(client, owner_token, db, monkeypatch):
  chat_id = _owner_chat(client, owner_token)
  row = declare_wait(
    db, chat_id=chat_id, description="cancelled",
    kind="command", command="true",
  )
  row.next_check_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  from app.chat_waits import cancel_wait
  cancel_wait(db, row)
  starts = _capture_starts(monkeypatch, running=False)

  assert asyncio.run(sweep_due_waits()) == 0
  assert starts == []


def test_cancelling_wait_kills_its_running_check(client, owner_token, db):
  chat_id = _owner_chat(client, owner_token)
  row = declare_wait(
    db, chat_id=chat_id, description="running cancellation",
    kind="command", command="sleep 30",
  )

  async def exercise():
    task = asyncio.create_task(chat_waits_mod._run_check(
      row.command, wait_id=row.id,
    ))
    for _ in range(100):
      with chat_waits_mod._ACTIVE_CHECKS_LOCK:
        if row.id in chat_waits_mod._ACTIVE_CHECK_PIDS:
          break
      await asyncio.sleep(0.01)
    else:
      raise AssertionError("check process did not start")

    cancel_wait(db, row)
    exit_code, output = await asyncio.wait_for(task, timeout=2)
    assert exit_code != 0
    assert output == ""
    with chat_waits_mod._ACTIVE_CHECKS_LOCK:
      assert row.id not in chat_waits_mod._ACTIVE_CHECK_PIDS

  asyncio.run(exercise())


# ─────────────────────────── goal identity ───────────────────────────


def test_wait_resume_reconnects_declaring_runs_goal(client, owner_token, db):
  chat_id = _owner_chat(client, owner_token)
  db.add(models.ChatRun(
    id="wait-goal-run",
    root_run_id="wait-goal-root",
    chat_id=chat_id,
    status="completed",
    provider="claude",
    goal_objective="Land the gate PR",
    goal_id="goal-wait-1",
  ))
  db.commit()

  objective, goal_id = goal_identity_for_run_start(db, chat_id, {
    "role": "user",
    "content": "wait done",
    "kind": WAIT_RESULT_MESSAGE_KIND,
    "source_work_id": "wait-goal-run",
  })
  assert objective == "Land the gate PR"
  assert goal_id == "goal-wait-1"

  # Unknown source run: no goal, no crash.
  objective, goal_id = goal_identity_for_run_start(db, chat_id, {
    "role": "user",
    "content": "wait done",
    "kind": WAIT_RESULT_MESSAGE_KIND,
    "source_work_id": "missing-run",
  })
  assert objective is None and goal_id is None
