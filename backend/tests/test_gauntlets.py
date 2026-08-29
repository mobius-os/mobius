"""Durable Gauntlet barriers over read Delegations and one owner writer."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json

import pytest

from app import models
from app.chat import discard_starting
from app.chat import reconcile_startup_chats
from app.chat_retention import purge_expired_chat_tombstones
from app.chat_start import start_programmatic_chat_continuation
from app.chat_writer import (
  AppendPending,
  ClearPending,
  Finalize,
  FinishRun,
  StartTurn,
  get_writer,
)
from app.database import SessionLocal
from app.gauntlets import (
  ActiveGauntletConflict,
  _bounded_evidence_json,
  _cost_observation,
  _integrator_prompt,
  _latch_stopping,
  _task_outcome,
  limit_resume_policy,
  new_gauntlet_run,
  reconcile_gauntlet,
  reconcile_running_gauntlets,
  repair_terminal_gauntlet_projections,
  stop_gauntlet,
  writer_policy_for_run,
)
from app.timeutil import SOFT_DELETE_TTL, now_naive_utc
from test_app_fixtures import create_local_app


def _controller(
  client, auth, db, app_id, *, suffix="main", provider="codex",
):
  app_token = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=auth,
  ).json()["token"]
  response = client.post(
    "/api/app-chats",
    json={"title": f"Gauntlet {suffix}", "provider": provider},
    headers={"Authorization": f"Bearer {app_token}"},
  )
  assert response.status_code == 201, response.text
  chat_id = response.json()["id"]
  root_id = f"gauntlet-root-{suffix}"
  db.add(models.ChatRun(
    id=root_id,
    root_run_id=root_id,
    chat_id=chat_id,
    status="running",
    provider=provider,
  ))
  db.commit()
  return chat_id, root_id


def _body(app_id, parent_chat_id, *, run_id="run-one"):
  return {
    "run_id": run_id,
    "app_id": app_id,
    "parent_chat_id": parent_chat_id,
    "target": "Demo Defense",
    "target_path": "/data/apps/demo-defense",
    "references": ["a classic tower-defense game"],
    "core_test": "A new player understands and enjoys the first wave.",
    "constraints": ["No clipped controls at the partner viewport."],
    "critic_roles": [
      {"key": "visual", "focus": "rendered hierarchy and finish"},
      {"key": "feel", "focus": "controls, pacing, and feedback"},
    ],
    "provider": "codex",
    "model": "gpt-5.6-sol",
    "effort": "high",
    "max_rounds": 2,
    "max_hours": 8,
    "max_budget_usd": 30,
    "allow_replacement": True,
  }


def _settle_controller(db, root_id):
  db.expire_all()
  root = db.get(models.ChatRun, root_id)
  root.status = "completed"
  db.commit()


def _complete_child(db, child_id, run_id, prompt, result, *, cost=None):
  get_writer().submit(StartTurn(
    chat_id=child_id,
    run_token=run_id,
    user_msg={"role": "user", "content": prompt, "ts": 1},
    title_source="Gauntlet critic",
    default_provider="codex",
  )).result(timeout=5)
  get_writer().submit(Finalize(
    chat_id=child_id,
    run_token=run_id,
    snapshot={
      "role": "assistant",
      "content": result,
      "blocks": ([{"type": "text", "content": result}] if result else []),
      "ts": 2,
    },
  )).result(timeout=5)
  get_writer().submit(FinishRun(
    chat_id=child_id,
    run_token=run_id,
    terminal_status="completed",
  )).result(timeout=5)
  if cost is not None:
    db.expire_all()
    physical = db.get(models.ChatRun, run_id)
    physical.cost_usd = cost
    db.commit()


def test_create_is_owner_only_and_reserves_only_read_critic_slots(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  other_id = create_local_app(client, auth, name="Other")["id"]
  parent_id, root_id = _controller(client, auth, db, app_id)
  starts = []

  async def fake_start(**kwargs):
    starts.append(kwargs)
    return True

  monkeypatch.setattr("app.gauntlets.start_programmatic_chat_turn", fake_start)
  body = _body(app_id, parent_id)
  misattributed = client.post(
    "/api/gauntlets",
    json={**body, "run_id": "wrong-app", "app_id": other_id},
    headers=auth,
  )
  assert misattributed.status_code == 409
  provider_mismatch = client.post(
    "/api/gauntlets",
    json={**body, "run_id": "wrong-provider", "provider": "claude", "model": None},
    headers=auth,
  )
  assert provider_mismatch.status_code == 409
  created = client.post("/api/gauntlets", json=body, headers=auth)
  assert created.status_code == 201, created.text
  payload = created.json()
  assert payload["attached"] is False
  assert payload["parent_root_run_id"] == root_id
  assert payload["phase"] == "baseline"
  assert payload["cost_complete"] is False
  assert payload["tasks"] == []
  _settle_controller(db, root_id)
  payload = asyncio.run(reconcile_gauntlet(body["run_id"]))
  assert len(payload["tasks"]) == 2
  assert {task["scope"] for task in payload["tasks"]} == {"read"}
  assert db.query(models.Delegation).count() == 2
  assert all(row.scope == "read" for row in db.query(models.Delegation).all())
  assert len(starts) == 2

  app_token = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=auth,
  ).json()["token"]
  app_auth = {"Authorization": f"Bearer {app_token}"}
  rejected = client.post("/api/gauntlets", json=body, headers=app_auth)
  assert rejected.status_code == 403
  own_read = client.get(f"/api/gauntlets/{body['run_id']}", headers=app_auth)
  assert own_read.status_code == 200, own_read.text

  other_token = client.post(
    "/api/auth/app-token", json={"app_id": other_id}, headers=auth,
  ).json()["token"]
  other_auth = {"Authorization": f"Bearer {other_token}"}
  assert client.get(
    f"/api/gauntlets/{body['run_id']}", headers=other_auth,
  ).status_code == 404
  other_list = client.get("/api/gauntlets", headers=other_auth)
  assert other_list.status_code == 200
  assert other_list.json()["items"] == []
  assert client.post(
    f"/api/gauntlets/{body['run_id']}/stop", headers=other_auth,
  ).status_code == 404

  attached = client.post("/api/gauntlets", json=body, headers=auth)
  assert attached.status_code == 201, attached.text
  assert attached.json()["attached"] is True
  assert db.query(models.GauntletTask).count() == 2
  assert db.query(models.Delegation).count() == 2


@pytest.mark.parametrize(
  "references",
  [None, [], [" ", "\n"]],
  ids=["omitted", "empty", "blank-only"],
)
def test_create_allows_goal_without_named_reference(
  client, owner_token, db, monkeypatch, references,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, _ = _controller(
    client, auth, db, app_id, suffix="no-reference",
  )

  async def fake_start(**_kwargs):
    return True

  monkeypatch.setattr("app.gauntlets.start_programmatic_chat_turn", fake_start)
  body = _body(app_id, parent_id, run_id="no-reference")
  if references is None:
    body.pop("references")
  else:
    body["references"] = references

  created = client.post("/api/gauntlets", json=body, headers=auth)

  assert created.status_code == 201, created.text
  db.expire_all()
  assert db.get(models.GauntletRun, body["run_id"]).contract_json["references"] == []


def test_active_target_lease_conflicts_then_releases_on_stop(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  first_parent, _ = _controller(client, auth, db, app_id, suffix="lease-one")
  second_parent, _ = _controller(client, auth, db, app_id, suffix="lease-two")

  async def fake_start(**_kwargs):
    return True

  monkeypatch.setattr("app.gauntlets.start_programmatic_chat_turn", fake_start)
  first = _body(app_id, first_parent, run_id="lease-first")
  second = _body(app_id, second_parent, run_id="lease-second")
  assert client.post("/api/gauntlets", json=first, headers=auth).status_code == 201

  conflict = client.post("/api/gauntlets", json=second, headers=auth)
  assert conflict.status_code == 409
  detail = conflict.json()["detail"]
  assert isinstance(detail, dict), conflict.text
  assert detail["active_run_id"] == "lease-first"

  stopped = client.post("/api/gauntlets/lease-first/stop", headers=auth)
  assert stopped.status_code == 200, stopped.text
  assert stopped.json()["status"] == "stopped"
  db.expire_all()
  assert db.get(models.GauntletRun, "lease-first").active_target_key is None

  created = client.post("/api/gauntlets", json=second, headers=auth)
  assert created.status_code == 201, created.text


def test_same_run_insert_race_attaches_to_exact_winner(
  client, owner_token, db,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(client, auth, db, app_id, suffix="race")
  target_path = "/data/apps/race-target"
  contract = {
    "target": "Race target",
    "target_path": target_path,
    "references": ["reference"],
    "core_test": "core test",
    "constraints": [],
    "critic_roles": [
      {"key": "one", "focus": "one"},
      {"key": "two", "focus": "two"},
    ],
    "provider": "codex",
    "model": None,
    "effort": None,
    "max_rounds": 2,
    "max_hours": 8,
    "max_budget_usd": None,
    "allow_replacement": False,
  }
  kwargs = {
    "run_id": "same-run-race",
    "app_id": app_id,
    "parent_chat_id": parent_id,
    "parent_root_run_id": root_id,
    "target_path": target_path,
    "contract": contract,
    "provider": "codex",
    "model": None,
    "effort": None,
    "max_rounds": 2,
    "max_hours": 8,
    "max_budget_usd": None,
  }
  barrier = __import__("threading").Barrier(2)

  def create():
    with SessionLocal() as session:
      barrier.wait(timeout=5)
      row, attached = new_gauntlet_run(session, **kwargs)
      return row.id, attached

  with ThreadPoolExecutor(max_workers=2) as pool:
    outcomes = [future.result(timeout=15) for future in (
      pool.submit(create), pool.submit(create),
    )]

  assert sorted(attached for _run_id, attached in outcomes) == [False, True]
  assert {run_id for run_id, _attached in outcomes} == {"same-run-race"}
  with SessionLocal() as check:
    assert check.query(models.GauntletRun).filter(
      models.GauntletRun.id == "same-run-race",
    ).count() == 1


def test_active_target_insert_race_reports_winning_run(
  client, owner_token, db,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(
    client, auth, db, app_id, suffix="target-race",
  )
  other_parent_id, other_root_id = _controller(
    client, auth, db, app_id, suffix="target-race-other",
  )
  target_path = "/data/apps/shared-race-target"
  contract = {
    "target": "Target race",
    "target_path": target_path,
    "references": ["reference"],
    "core_test": "core test",
    "constraints": [],
    "critic_roles": [
      {"key": "one", "focus": "one"},
      {"key": "two", "focus": "two"},
    ],
  }
  common = {
    "app_id": app_id,
    "parent_chat_id": parent_id,
    "parent_root_run_id": root_id,
    "target_path": target_path,
    "contract": contract,
    "provider": "codex",
    "model": None,
    "effort": None,
    "max_rounds": 2,
    "max_hours": 8,
    "max_budget_usd": None,
  }
  barrier = __import__("threading").Barrier(2)

  def create(run_id, parent, root):
    try:
      with SessionLocal() as session:
        barrier.wait(timeout=5)
        row, attached = new_gauntlet_run(
          session,
          run_id=run_id,
          **{
            **common,
            "parent_chat_id": parent,
            "parent_root_run_id": root,
          },
        )
        return ("created", row.id, attached)
    except ActiveGauntletConflict as exc:
      return ("conflict", exc.active_run_id, None)

  with ThreadPoolExecutor(max_workers=2) as pool:
    outcomes = [future.result(timeout=15) for future in (
      pool.submit(create, "target-one", parent_id, root_id),
      pool.submit(create, "target-two", other_parent_id, other_root_id),
    )]
  created = [item for item in outcomes if item[0] == "created"]
  conflicts = [item for item in outcomes if item[0] == "conflict"]
  assert len(created) == 1
  assert len(conflicts) == 1
  assert conflicts[0][1] == created[0][1]


def test_aggregate_evidence_is_bounded_and_prioritizes_latest_integrator():
  context = [
    {
      "phase": "baseline",
      "round": index,
      "role": f"critic-{index}",
      "result": f"OLD-{index}-" + ("x" * 4000),
    }
    for index in range(8)
  ]
  context.append({
    "phase": "integrate",
    "round": 9,
    "role": "integrator",
    "result": "LATEST-INTEGRATOR-" + ("y" * 4000),
  })

  encoded = _bounded_evidence_json(context, max_chars=1800)
  decoded = json.loads(encoded)

  assert len(encoded) <= 1800
  assert decoded[-1]["truncated"] is True
  assert decoded[-1]["omitted"] >= 1
  selected = decoded[:-1]
  assert selected[-1]["phase"] == "integrate"
  assert selected[-1]["result"].startswith("LATEST-INTEGRATOR-")


def test_all_of_barrier_uses_fresh_nonempty_results_then_reserves_one_owner_writer(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(
    client, auth, db, app_id, suffix="barrier",
  )
  monkeypatch.setattr(
    "app.gauntlets.start_programmatic_chat_turn",
    lambda **_kwargs: asyncio.sleep(0, result=True),
  )
  body = _body(app_id, parent_id, run_id="barrier-run")
  response = client.post("/api/gauntlets", json=body, headers=auth)
  assert response.status_code == 201, response.text
  _settle_controller(db, root_id)
  asyncio.run(reconcile_gauntlet(body["run_id"]))

  tasks = db.query(models.GauntletTask).order_by(
    models.GauntletTask.ordinal.asc()
  ).all()
  assert len(tasks) == 2
  first = db.get(models.Delegation, tasks[0].delegation_id)
  second = db.get(models.Delegation, tasks[1].delegation_id)
  prompts = {}
  for index, delegation in enumerate((first, second)):
    prompt = (
      "Baseline visual evidence with ranked defects."
      if index == 0 else "Baseline interaction evidence with acceptance checks."
    )
    prompts[delegation.id] = prompt
    _complete_child(
      db, delegation.child_chat_id, f"critic-run-{index}",
      "Inspect baseline", prompt,
    )

  writer_starts = []

  async def fake_writer(**kwargs):
    writer_starts.append(kwargs)
    return True

  monkeypatch.setattr(
    "app.gauntlets.start_programmatic_chat_continuation", fake_writer,
  )
  result = asyncio.run(reconcile_gauntlet(body["run_id"]))
  assert result["phase"] == "integrate"
  assert result["current_round"] == 1
  writer_tasks = db.query(models.GauntletTask).filter(
    models.GauntletTask.scope == "write",
  ).all()
  assert len(writer_tasks) == 1
  writer = writer_tasks[0]
  assert writer.delegation_id is None
  assert writer.chat_run_id is not None
  assert len(writer_starts) == 1
  assert writer_starts[0]["chat_id"] == parent_id
  assert writer_starts[0]["root_run_id"] == root_id
  assert all(report in writer_starts[0]["content"] for report in prompts.values())
  # Dollar telemetry is unknown for both Codex critics, but the run continues
  # under hard time/round ceilings rather than pretending unknown means zero.
  assert result["cost_usd"] == 0
  assert result["cost_complete"] is False

  # Reconciliation is idempotent: no second writer slot or start.
  result = asyncio.run(reconcile_gauntlet(body["run_id"]))
  assert result["phase"] == "integrate"
  assert db.query(models.GauntletTask).filter(
    models.GauntletTask.scope == "write",
  ).count() == 1
  assert len(writer_starts) == 2  # same reserved physical start is retried
  assert writer_starts[0]["run_token"] == writer_starts[1]["run_token"]


def test_completed_critic_without_authoritative_output_fails_before_writer(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(
    client, auth, db, app_id, suffix="empty",
  )

  async def fake_start(**_kwargs):
    return True

  monkeypatch.setattr("app.gauntlets.start_programmatic_chat_turn", fake_start)
  body = _body(app_id, parent_id, run_id="empty-result")
  response = client.post("/api/gauntlets", json=body, headers=auth)
  assert response.status_code == 201, response.text
  _settle_controller(db, root_id)
  asyncio.run(reconcile_gauntlet(body["run_id"]))
  tasks = db.query(models.GauntletTask).order_by(
    models.GauntletTask.ordinal.asc()
  ).all()
  for index, task in enumerate(tasks):
    delegation = db.get(models.Delegation, task.delegation_id)
    _complete_child(
      db, delegation.child_chat_id, f"empty-child-{index}",
      "Inspect baseline", "" if index == 0 else "Substantive evidence.",
    )

  writer_starts = []

  async def fake_writer(**kwargs):
    writer_starts.append(kwargs)
    return True

  monkeypatch.setattr(
    "app.gauntlets.start_programmatic_chat_continuation", fake_writer,
  )
  result = asyncio.run(reconcile_gauntlet(body["run_id"]))
  assert result["status"] == "failed"
  assert "without durable output" in result["terminal_reason"]
  assert writer_starts == []
  assert db.query(models.GauntletTask).filter(
    models.GauntletTask.scope == "write",
  ).count() == 0


def test_completed_writer_launches_fresh_all_of_evaluation_and_passes(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(client, auth, db, app_id, suffix="pass")

  async def fake_child_start(**_kwargs):
    return True

  monkeypatch.setattr(
    "app.gauntlets.start_programmatic_chat_turn", fake_child_start,
  )
  body = {
    **_body(app_id, parent_id, run_id="passing-run"),
    "max_rounds": 1,
  }
  created = client.post("/api/gauntlets", json=body, headers=auth)
  assert created.status_code == 201, created.text
  _settle_controller(db, root_id)
  asyncio.run(reconcile_gauntlet(body["run_id"]))
  baseline = db.query(models.GauntletTask).filter(
    models.GauntletTask.phase == "baseline",
  ).order_by(models.GauntletTask.ordinal.asc()).all()
  for index, task in enumerate(baseline):
    child = db.get(models.Delegation, task.delegation_id)
    _complete_child(
      db, child.child_chat_id, f"pass-baseline-{index}",
      "Inspect baseline", f"Substantive baseline report {index}.",
    )
  scheduled = []

  def fake_schedule(**kwargs):
    scheduled.append(kwargs)
    return True

  monkeypatch.setattr("app.chat._schedule_continuation", fake_schedule)
  integrated = asyncio.run(reconcile_gauntlet(body["run_id"]))
  assert integrated["phase"] == "integrate"
  assert len(scheduled) == 1
  writer_task = db.query(models.GauntletTask).filter(
    models.GauntletTask.scope == "write",
  ).one()
  writer_run = db.get(models.ChatRun, writer_task.chat_run_id)
  assert writer_run.root_run_id == root_id
  get_writer().submit(Finalize(
    chat_id=parent_id,
    run_token=writer_run.id,
    snapshot={
      "role": "assistant",
      "content": "Applied and play-tested the round-one changes.",
      "blocks": [{
        "type": "text",
        "content": "Applied and play-tested the round-one changes.",
      }],
      "ts": 20,
    },
  )).result(timeout=5)
  get_writer().submit(FinishRun(
    chat_id=parent_id,
    run_token=writer_run.id,
    terminal_status="completed",
  )).result(timeout=5)
  discard_starting(parent_id)

  evaluating = asyncio.run(reconcile_gauntlet(body["run_id"]))
  assert evaluating["phase"] == "evaluate", (
    evaluating["terminal_reason"], evaluating["tasks"]
  )
  evaluators = db.query(models.GauntletTask).filter(
    models.GauntletTask.phase == "evaluate",
  ).order_by(models.GauntletTask.ordinal.asc()).all()
  assert len(evaluators) == 2
  assert all(task.scope == "read" for task in evaluators)
  for index, task in enumerate(evaluators):
    child = db.get(models.Delegation, task.delegation_id)
    verdict = (
      f"Evidence for every gate. "
      '<gauntlet_verdict>{"passed":true,"score":88,'
      f'"summary":"Evaluator {index} passed the core test."}}'
      "</gauntlet_verdict>"
    )
    _complete_child(
      db, child.child_chat_id, f"pass-eval-{index}",
      "Evaluate current target", verdict,
    )

  completed = asyncio.run(reconcile_gauntlet(body["run_id"]))
  assert completed["status"] == "completed"
  assert completed["phase"] == "terminal"
  assert "average 88.0/100" in completed["terminal_reason"]
  assert db.query(models.GauntletTask).filter(
    models.GauntletTask.scope == "write",
  ).count() == 1


def test_failed_final_evaluation_reports_round_budget_exhausted(
  client, owner_token, db, monkeypatch,
):
  """A strict negative verdict cannot be relabelled success at max_rounds."""
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(client, auth, db, app_id, suffix="roundcap")

  async def fake_start(**_kwargs):
    return True

  monkeypatch.setattr("app.gauntlets.start_programmatic_chat_turn", fake_start)
  body = {
    **_body(app_id, parent_id, run_id="round-cap"),
    "max_rounds": 1,
  }
  assert client.post(
    "/api/gauntlets", json=body, headers=auth,
  ).status_code == 201
  run = db.get(models.GauntletRun, body["run_id"])
  run.phase = "evaluate"
  run.current_round = 1
  root = db.get(models.ChatRun, root_id)
  root.status = "completed"
  # Retire baseline tasks so the synthetic evaluation state is internally
  # coherent without launching a writer in this narrow round-cap contract.
  db.query(models.GauntletTask).delete(synchronize_session=False)
  db.query(models.Delegation).delete(synchronize_session=False)
  db.query(models.Chat).filter(
    models.Chat.id != parent_id,
  ).delete(synchronize_session=False)
  db.commit()
  evaluating = asyncio.run(reconcile_gauntlet(body["run_id"]))
  assert evaluating["phase"] == "evaluate"
  for index, task in enumerate(db.query(models.GauntletTask).all()):
    child = db.get(models.Delegation, task.delegation_id)
    verdict = (
      "Core test still fails. "
      '<gauntlet_verdict>{"passed":false,"score":42,'
      '"summary":"Controls remain unclear."}</gauntlet_verdict>'
    )
    _complete_child(
      db, child.child_chat_id, f"cap-eval-{index}", "Evaluate", verdict,
    )
  terminal = asyncio.run(reconcile_gauntlet(body["run_id"]))
  assert terminal["status"] == "budget_exhausted"
  assert terminal["terminal_reason"] == "maximum round count reached (1)"


def test_boot_reconciliation_repairs_missing_slots_without_duplicates(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(client, auth, db, app_id, suffix="boot")
  starts = []

  async def fake_start(**kwargs):
    starts.append(kwargs)
    return True

  monkeypatch.setattr("app.gauntlets.start_programmatic_chat_turn", fake_start)
  # Suppress the create-time scheduler to model a crash after GauntletRun
  # commit but before its first durable Delegation slot.
  async def defer_reconcile(_run_id):
    return {"deferred": True}

  monkeypatch.setattr(
    "app.routes.gauntlets.reconcile_gauntlet", defer_reconcile,
  )
  body = _body(app_id, parent_id, run_id="boot-repair")
  response = client.post("/api/gauntlets", json=body, headers=auth)
  assert response.status_code == 201, response.text
  assert db.query(models.GauntletTask).count() == 0
  _settle_controller(db, root_id)

  assert asyncio.run(reconcile_running_gauntlets()) == 1
  assert db.query(models.GauntletTask).count() == 2
  assert db.query(models.Delegation).count() == 2
  assert asyncio.run(reconcile_running_gauntlets()) == 1
  assert db.query(models.GauntletTask).count() == 2
  assert db.query(models.Delegation).count() == 2
  # Missing physical runs are retried with the same immutable child identities.
  assert len(starts) == 4


def test_same_root_owner_continuation_is_idempotently_promoted(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(
    client, auth, db, app_id, suffix="continuation",
  )
  root = db.get(models.ChatRun, root_id)
  root.status = "completed"
  controller = db.get(models.Chat, parent_id)
  controller.messages = [{
    "role": "user",
    "content": "launch",
    "ts": 1,
    "viewport": {"width": 390, "height": 844},
    "timezone": "Europe/London",
  }]
  db.commit()
  scheduled = []

  def fake_schedule(**kwargs):
    scheduled.append(kwargs)
    return True

  monkeypatch.setattr("app.chat._schedule_continuation", fake_schedule)
  kwargs = {
    "chat_id": parent_id,
    "root_run_id": root_id,
    "run_token": "writer-physical-one",
    "content": "Integrate the evidenced fixes.",
    "continuation_id": "gauntlet-writer-one",
    "reason": "gauntlet",
  }
  assert asyncio.run(start_programmatic_chat_continuation(**kwargs)) is True
  db.expire_all()
  physical = db.get(models.ChatRun, "writer-physical-one")
  assert physical.root_run_id == root_id
  assert physical.initiated_by_app_id is None
  chat = db.get(models.Chat, parent_id)
  assert chat.messages[-1]["kind"] == "continuation"
  assert chat.messages[-1]["cid"] == "gauntlet-writer-one"
  assert chat.messages[-1]["viewport"] == {"width": 390, "height": 844}
  assert chat.messages[-1]["timezone"] == "Europe/London"
  assert len(scheduled) == 1

  # Same reserved physical identity attaches without another transcript row.
  before = len(chat.messages)
  assert asyncio.run(start_programmatic_chat_continuation(**kwargs)) is True
  db.expire_all()
  assert len(db.get(models.Chat, parent_id).messages) == before
  assert len(scheduled) == 1
  discard_starting(parent_id)


def test_atomic_continuation_recovers_exact_legacy_pending_row(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(
    client, auth, db, app_id, suffix="pending-recovery",
  )
  root = db.get(models.ChatRun, root_id)
  root.status = "completed"
  db.commit()
  get_writer().submit(AppendPending(
    chat_id=parent_id,
    run_token="",
    user_msg={
      "role": "user",
      "content": "Recover this exact continuation.",
      "ts": 10,
      "cid": "gauntlet-recover-cid",
      "kind": "continuation",
      "continuation_reason": "gauntlet",
    },
  )).result(timeout=5)
  scheduled = []

  def fake_schedule(**kwargs):
    scheduled.append(kwargs)
    return True

  monkeypatch.setattr("app.chat._schedule_continuation", fake_schedule)
  started = asyncio.run(start_programmatic_chat_continuation(
    chat_id=parent_id,
    root_run_id=root_id,
    run_token="recovered-physical-run",
    content="Recover this exact continuation.",
    continuation_id="gauntlet-recover-cid",
    reason="gauntlet",
  ))

  assert started is True
  db.expire_all()
  chat = db.get(models.Chat, parent_id)
  assert chat.pending_messages == []
  assert [
    message.get("cid") for message in chat.messages
    if message.get("cid") == "gauntlet-recover-cid"
  ] == ["gauntlet-recover-cid"]
  physical = db.get(models.ChatRun, "recovered-physical-run")
  assert physical.root_run_id == root_id
  assert len(scheduled) == 1
  discard_starting(parent_id)


def test_atomic_continuation_never_consumes_foreign_owner_pending(
  client, owner_token, db,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(
    client, auth, db, app_id, suffix="foreign-pending",
  )
  root = db.get(models.ChatRun, root_id)
  root.status = "completed"
  db.commit()
  get_writer().submit(AppendPending(
    chat_id=parent_id,
    run_token="",
    user_msg={
      "role": "user",
      "content": "Owner queued this independently.",
      "ts": 10,
      "cid": "owner-pending",
    },
  )).result(timeout=5)

  started = asyncio.run(start_programmatic_chat_continuation(
    chat_id=parent_id,
    root_run_id=root_id,
    run_token="must-not-exist",
    content="Gauntlet continuation.",
    continuation_id="gauntlet-foreign-check",
    reason="gauntlet",
  ))

  assert started is False
  db.expire_all()
  chat = db.get(models.Chat, parent_id)
  assert [message["cid"] for message in chat.pending_messages] == [
    "owner-pending"
  ]
  assert db.get(models.ChatRun, "must-not-exist") is None


def test_app_stop_is_idempotent_and_cancels_owned_critics(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, _root_id = _controller(
    client, auth, db, app_id, suffix="stop",
  )

  async def fake_start(**_kwargs):
    return True

  monkeypatch.setattr("app.gauntlets.start_programmatic_chat_turn", fake_start)
  body = _body(app_id, parent_id, run_id="stop-run")
  created = client.post("/api/gauntlets", json=body, headers=auth)
  assert created.status_code == 201, created.text
  active_ids = []
  for index, delegation in enumerate(db.query(models.Delegation).all()):
    physical_id = f"stop-child-{index}"
    active_ids.append(physical_id)
    get_writer().submit(StartTurn(
      chat_id=delegation.child_chat_id,
      run_token=physical_id,
      user_msg={"role": "user", "content": "Active critic", "ts": index + 1},
      title_source="Active critic",
      default_provider="codex",
      initiated_by_app_id=app_id,
    )).result(timeout=5)
  app_token = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=auth,
  ).json()["token"]
  app_auth = {"Authorization": f"Bearer {app_token}"}

  stopped = client.post(
    f"/api/gauntlets/{body['run_id']}/stop", headers=app_auth,
  )
  assert stopped.status_code == 200, stopped.text
  assert stopped.json()["status"] == "stopped"
  assert "stop requested" in stopped.json()["terminal_reason"]
  db.expire_all()
  assert all(db.get(models.ChatRun, run_id).status == "stopped" for run_id in active_ids)
  assert all(
    delegation.cancelled_at is not None
    for delegation in db.query(models.Delegation).all()
  )
  again = client.post(
    f"/api/gauntlets/{body['run_id']}/stop", headers=app_auth,
  )
  assert again.status_code == 200
  assert again.json()["ended_at"] == stopped.json()["ended_at"]


def test_baseline_waits_for_controller_and_preexisting_queue(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(
    client, auth, db, app_id, suffix="idle-barrier",
  )
  starts = []

  async def fake_start(**kwargs):
    starts.append(kwargs)
    return True

  monkeypatch.setattr("app.gauntlets.start_programmatic_chat_turn", fake_start)
  body = _body(app_id, parent_id, run_id="idle-barrier")
  created = client.post("/api/gauntlets", json=body, headers=auth)
  assert created.status_code == 201
  assert created.json()["tasks"] == []

  _settle_controller(db, root_id)
  get_writer().submit(AppendPending(
    chat_id=parent_id,
    run_token="",
    user_msg={"role": "user", "content": "already queued", "ts": 10},
  )).result(timeout=5)
  assert asyncio.run(reconcile_gauntlet(body["run_id"]))["tasks"] == []
  get_writer().submit(ClearPending(
    chat_id=parent_id, run_token="",
  )).result(timeout=5)
  progressed = asyncio.run(reconcile_gauntlet(body["run_id"]))
  assert len(progressed["tasks"]) == 2
  assert len(starts) == 2


def test_writer_slot_does_not_claim_foreign_same_root_run_before_seed(
  client, owner_token, db,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(client, auth, db, app_id, suffix="foreign")
  body = _body(app_id, parent_id, run_id="foreign-writer")
  assert client.post("/api/gauntlets", json=body, headers=auth).status_code == 201
  _settle_controller(db, root_id)
  run = db.get(models.GauntletRun, body["run_id"])
  run.phase = "integrate"
  run.current_round = 1
  task = models.GauntletTask(
    id="foreign-writer-task",
    gauntlet_run_id=run.id,
    phase="integrate",
    round=1,
    ordinal=0,
    role="integrator",
    scope="write",
    chat_run_id="deterministic-seed-is-absent",
    prompt_sha256="0" * 64,
  )
  db.add(task)
  db.commit()
  db.add(models.ChatRun(
    id="foreign-owner-continuation",
    root_run_id=root_id,
    chat_id=parent_id,
    status="running",
    provider="codex",
  ))
  db.commit()

  assert _task_outcome(db, task) == ("starting", "")
  assert writer_policy_for_run(
    db, chat_id=parent_id, run_token="foreign-owner-continuation",
  ) is None


def test_resumed_writer_chain_blocks_then_uses_bound_result_and_cost(
  client, owner_token, db,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(client, auth, db, app_id, suffix="resume")
  body = _body(app_id, parent_id, run_id="resumed-writer")
  assert client.post("/api/gauntlets", json=body, headers=auth).status_code == 201
  _settle_controller(db, root_id)
  run = db.get(models.GauntletRun, body["run_id"])
  run.phase = "integrate"
  run.current_round = 1
  task = models.GauntletTask(
    id="resumed-writer-task",
    gauntlet_run_id=run.id,
    phase="integrate",
    round=1,
    ordinal=0,
    role="integrator",
    scope="write",
    chat_run_id="writer-seed",
    prompt_sha256="1" * 64,
  )
  db.add(task)
  db.commit()
  db.add_all((
    models.ChatRun(
      id="writer-seed", root_run_id=root_id, chat_id=parent_id,
      status="completed", provider="codex", cost_usd=1.25,
    ),
    models.ChatRun(
      id="writer-resume", root_run_id=root_id, chat_id=parent_id,
      status="running", provider="codex", cost_usd=None,
    ),
  ))
  chat = db.get(models.Chat, parent_id)
  chat.messages = [
    {
      "role": "user", "content": "integrate", "ts": 1,
      "cid": f"gauntlet-{run.id}-integrate-1",
      "kind": "continuation", "continuation_reason": "gauntlet",
    },
    {"role": "assistant", "content": "first physical", "ts": 2},
    {
      "role": "user", "content": "continue", "ts": 3,
      "kind": "continuation", "continuation_reason": "usage_limit",
    },
    {"role": "assistant", "content": "final resumed result", "ts": 4},
  ]
  db.commit()

  assert _task_outcome(db, task) == ("running", "")
  writer_policy = writer_policy_for_run(
    db, chat_id=parent_id, run_token="writer-resume",
  )
  assert writer_policy is not None
  assert writer_policy.provider == "codex"
  assert writer_policy.model == "gpt-5.6-sol"
  assert writer_policy.effort == "high"

  # The coordinator's transition fence prevents the controller picker or app
  # metadata route from drifting the execution policy between writer rounds.
  assert client.patch(
    f"/api/chats/{parent_id}",
    json={"agent_settings_json": {"model": "gpt-5.6-terra"}},
    headers=auth,
  ).status_code == 409
  app_token = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=auth,
  ).json()["token"]
  assert client.patch(
    f"/api/app-chats/{parent_id}",
    json={"model": "gpt-5.6-terra"},
    headers={"Authorization": f"Bearer {app_token}"},
  ).status_code == 409
  resumed = db.get(models.ChatRun, "writer-resume")
  resumed.status = "completed"
  resumed.cost_usd = 0.75
  db.commit()
  assert _task_outcome(db, task) == ("completed", "final resumed result")
  assert _cost_observation(db, run.id) == (2.0, True)


def test_stop_timeout_retains_lease_and_first_terminal_intent(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, _root_id = _controller(client, auth, db, app_id, suffix="timeout")
  body = _body(app_id, parent_id, run_id="stop-timeout")
  assert client.post("/api/gauntlets", json=body, headers=auth).status_code == 201

  monkeypatch.setattr("app.chat.is_chat_running", lambda _chat_id: True)

  async def timed_out(_chat_id):
    return False

  monkeypatch.setattr("app.chat.stop_chat_for", timed_out)
  payload = asyncio.run(stop_gauntlet(body["run_id"]))
  assert payload["status"] == "stopping"
  db.expire_all()
  run = db.get(models.GauntletRun, body["run_id"])
  lease = run.active_target_key
  assert lease is not None
  assert run.requested_terminal_status == "stopped"
  original_reason = run.terminal_reason

  _latch_stopping(
    db, run,
    reason="later deadline must not overwrite Stop",
    terminal_status="budget_exhausted",
  )
  db.expire_all()
  run = db.get(models.GauntletRun, body["run_id"])
  assert run.active_target_key == lease
  assert run.requested_terminal_status == "stopped"
  assert run.terminal_reason == original_reason

  monkeypatch.setattr("app.chat.is_chat_running", lambda _chat_id: False)
  asyncio.run(reconcile_running_gauntlets())
  db.expire_all()
  assert db.get(models.GauntletRun, body["run_id"]).status == "stopped"


def test_terminal_notification_is_idempotent_and_targets_app(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, _root_id = _controller(client, auth, db, app_id, suffix="notify")
  body = _body(app_id, parent_id, run_id="notify-once")
  assert client.post("/api/gauntlets", json=body, headers=auth).status_code == 201
  from app import push
  async_notify = push.notify_owner_async
  async_calls = []

  async def observed_async_notify(*args, **kwargs):
    async_calls.append(kwargs["notification_id"])
    return await async_notify(*args, **kwargs)

  monkeypatch.setattr(push, "notify_owner_async", observed_async_notify)
  monkeypatch.setattr(
    push,
    "notify_owner",
    lambda *_args, **_kwargs: (_ for _ in ()).throw(
      AssertionError("Gauntlet terminal delivery must use the async seam")
    ),
  )
  assert asyncio.run(stop_gauntlet(body["run_id"]))["status"] == "stopped"
  assert len(async_calls) == 1
  client.get(f"/api/gauntlets/{body['run_id']}", headers=auth)
  client.get(f"/api/gauntlets/{body['run_id']}", headers=auth)
  rows = db.query(models.Notification).filter(
    models.Notification.source_type == "app",
    models.Notification.source_id == str(app_id),
    models.Notification.title == "Gauntlet stopped",
  ).all()
  assert len(rows) == 1
  assert rows[0].target == f"/shell/?app={app_id}"

  # Simulate a crash after the authoritative terminal commit but before its
  # two projections. Startup repair restores each deterministic identity once;
  # ordinary GET/list remains read-only.
  notification_id = rows[0].id
  db.delete(rows[0])
  db.query(models.AgentLifecycleEvent).filter(
    models.AgentLifecycleEvent.source == "gauntlet",
    models.AgentLifecycleEvent.provider_agent_id == body["run_id"],
    models.AgentLifecycleEvent.event_type == "agent_terminal",
  ).delete(synchronize_session=False)
  db.commit()
  assert client.get(
    f"/api/gauntlets/{body['run_id']}", headers=auth,
  ).status_code == 200
  assert db.get(models.Notification, notification_id) is None
  assert asyncio.run(repair_terminal_gauntlet_projections()) == 1
  assert asyncio.run(repair_terminal_gauntlet_projections()) == 0
  assert db.get(models.Notification, notification_id) is not None


def test_hard_purge_controller_reclaims_gauntlet_and_hidden_critics(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(client, auth, db, app_id, suffix="purge")

  async def fake_start(**_kwargs):
    return True

  monkeypatch.setattr("app.gauntlets.start_programmatic_chat_turn", fake_start)
  body = _body(app_id, parent_id, run_id="purge-run")
  assert client.post("/api/gauntlets", json=body, headers=auth).status_code == 201
  _settle_controller(db, root_id)
  asyncio.run(reconcile_gauntlet(body["run_id"]))
  child_ids = [row[0] for row in db.query(models.Delegation.child_chat_id).all()]
  assert asyncio.run(stop_gauntlet(body["run_id"]))["status"] == "stopped"
  controller = db.get(models.Chat, parent_id)
  controller.deleted_at = now_naive_utc() - SOFT_DELETE_TTL - timedelta(days=1)
  db.commit()

  purged = purge_expired_chat_tombstones(db)
  assert parent_id in purged
  assert all(child_id in purged for child_id in child_ids)
  assert db.get(models.Chat, parent_id) is None
  assert all(db.get(models.Chat, child_id) is None for child_id in child_ids)
  assert db.get(models.GauntletRun, body["run_id"]) is None
  assert db.query(models.GauntletTask).count() == 0
  assert db.query(models.Delegation).count() == 0


def test_app_hard_purge_rolls_back_the_complete_gauntlet_graph(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, _root_id = _controller(
    client, auth, db, app_id, suffix="app-purge-rollback",
  )

  async def fake_start(**_kwargs):
    return True

  monkeypatch.setattr("app.gauntlets.start_programmatic_chat_turn", fake_start)
  body = _body(app_id, parent_id, run_id="app-purge-rollback")
  assert client.post("/api/gauntlets", json=body, headers=auth).status_code == 201
  critic_ids = [
    row[0] for row in db.query(models.Delegation.child_chat_id).all()
  ]
  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  app = db.get(models.App, app_id)
  app.deleted_at = now_naive_utc() - timedelta(days=8)
  db.commit()

  session_type = type(db)
  real_commit = session_type.commit

  def fail_app_delete_commit(session):
    if any(
      isinstance(row, models.App) and row.id == app_id
      for row in session.deleted
    ):
      raise RuntimeError("simulated final app-delete commit failure")
    return real_commit(session)

  monkeypatch.setattr(session_type, "commit", fail_app_delete_commit)
  assert client.get("/api/apps/", headers=auth).status_code == 200

  db.expire_all()
  assert db.get(models.App, app_id) is not None
  assert db.get(models.GauntletRun, body["run_id"]) is not None
  assert db.get(models.Chat, parent_id) is not None
  assert all(db.get(models.Chat, child_id) is not None for child_id in critic_ids)
  assert db.query(models.GauntletTask).count() == len(critic_ids)
  assert db.query(models.Delegation).count() == len(critic_ids)


def test_retention_never_purges_a_live_standalone_delegation_graph(
  client, owner_token, db,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Delegated retention")["id"]
  parent_id, root_id = _controller(
    client, auth, db, app_id, suffix="delegation-retention",
  )
  child_id = "standalone-retention-child"
  child_run_id = "standalone-retention-run"
  db.add_all((
    models.Chat(
      id=child_id,
      title="Hidden child",
      messages=[],
      provider="codex",
      created_by_app_id=app_id,
    ),
    models.Delegation(
      id="standalone-retention-delegation",
      app_id=app_id,
      parent_chat_id=parent_id,
      parent_root_run_id=root_id,
      task_key="standalone-retention",
      child_chat_id=child_id,
      provider="codex",
      scope="write",
      cwd="/data/platform",
      prompt_sha256="a" * 64,
    ),
    models.ChatRun(
      id=child_run_id,
      root_run_id=child_run_id,
      chat_id=child_id,
      status="running",
      provider="codex",
      initiated_by_app_id=app_id,
    ),
  ))
  parent = db.get(models.Chat, parent_id)
  parent.deleted_at = now_naive_utc() - SOFT_DELETE_TTL - timedelta(days=1)
  db.commit()

  assert purge_expired_chat_tombstones(db) == []
  assert db.get(models.Chat, parent_id) is not None
  assert db.get(models.Chat, child_id) is not None

  db.get(models.ChatRun, child_run_id).status = "completed"
  db.commit()
  purged = purge_expired_chat_tombstones(db)
  assert parent_id in purged and child_id in purged
  assert db.get(models.Delegation, "standalone-retention-delegation") is None


def test_critic_limit_resume_uses_latched_total_reservation(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(
    client, auth, db, app_id, suffix="limit", provider="claude",
  )

  async def fake_start(**_kwargs):
    return True

  monkeypatch.setattr("app.gauntlets.start_programmatic_chat_turn", fake_start)
  body = {
    **_body(app_id, parent_id, run_id="critic-limit"),
    "provider": "claude",
    "model": None,
    "max_budget_usd": 1.0,
  }
  assert client.post("/api/gauntlets", json=body, headers=auth).status_code == 201
  _settle_controller(db, root_id)
  asyncio.run(reconcile_gauntlet(body["run_id"]))
  task = db.query(models.GauntletTask).filter(
    models.GauntletTask.scope == "read",
  ).order_by(models.GauntletTask.ordinal.asc()).first()
  delegation = db.get(models.Delegation, task.delegation_id)
  assert task.max_budget_usd == pytest.approx(0.5)
  physical = models.ChatRun(
    id="critic-limit-physical",
    root_run_id="critic-limit-physical",
    chat_id=delegation.child_chat_id,
    status="resume_pending",
    provider="claude",
    initiated_by_app_id=app_id,
    cost_usd=0.2,
  )
  db.add(physical)
  db.commit()
  policy = limit_resume_policy(
    db,
    child_chat_id=delegation.child_chat_id,
    run_token=physical.id,
    initiated_by_app_id=app_id,
  )
  assert policy is not None and policy.allowed is True
  from app.delegations import policy_for_chat
  assert policy_for_chat(
    db, delegation.child_chat_id,
  ).explicit_provider_budget_usd == pytest.approx(0.3)

  physical.cost_usd = task.max_budget_usd - 0.0005
  db.commit()
  denied = limit_resume_policy(
    db,
    child_chat_id=delegation.child_chat_id,
    run_token=physical.id,
    initiated_by_app_id=app_id,
  )
  assert denied is not None and denied.allowed is False
  assert "safely schedulable remainder" in (denied.boundary_reason or "")


def test_round_two_integrator_prompt_is_stable_across_slot_reservation(
  client, owner_token, db,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(client, auth, db, app_id, suffix="prompt")
  body = _body(app_id, parent_id, run_id="stable-prompt")
  assert client.post("/api/gauntlets", json=body, headers=auth).status_code == 201
  _settle_controller(db, root_id)
  run = db.get(models.GauntletRun, body["run_id"])
  run.phase = "integrate"
  run.current_round = 2
  first = models.GauntletTask(
    id="stable-round-one",
    gauntlet_run_id=run.id,
    phase="integrate",
    round=1,
    ordinal=0,
    role="integrator",
    scope="write",
    chat_run_id="stable-round-one-physical",
    prompt_sha256="2" * 64,
  )
  db.add(first)
  db.commit()
  db.add(models.ChatRun(
    id=first.chat_run_id,
    root_run_id=root_id,
    chat_id=parent_id,
    status="completed",
    provider="codex",
  ))
  chat = db.get(models.Chat, parent_id)
  chat.messages = [
    {
      "role": "user", "content": "round one", "ts": 1,
      "cid": f"gauntlet-{run.id}-integrate-1",
      "kind": "continuation", "continuation_reason": "gauntlet",
    },
    {"role": "assistant", "content": "round one evidence", "ts": 2},
  ]
  db.commit()
  before = _integrator_prompt(db, run)
  db.add(models.GauntletTask(
    id="stable-round-two",
    gauntlet_run_id=run.id,
    phase="integrate",
    round=2,
    ordinal=0,
    role="integrator",
    scope="write",
    chat_run_id="stable-round-two-physical",
    prompt_sha256="3" * 64,
  ))
  db.commit()
  db.refresh(run)
  assert _integrator_prompt(db, run) == before
  assert "round one evidence" in before


def test_startup_preserves_exact_unscheduled_writer_for_gauntlet_repair(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Gauntlet")["id"]
  parent_id, root_id = _controller(client, auth, db, app_id, suffix="orphan")
  body = _body(app_id, parent_id, run_id="writer-orphan")
  assert client.post("/api/gauntlets", json=body, headers=auth).status_code == 201
  _settle_controller(db, root_id)
  run = db.get(models.GauntletRun, body["run_id"])
  run.phase = "integrate"
  run.current_round = 1
  db.commit()

  monkeypatch.setattr("app.chat._schedule_continuation", lambda **_kwargs: False)
  first = asyncio.run(reconcile_gauntlet(run.id))
  assert first["phase"] == "integrate"
  writer_task = db.query(models.GauntletTask).filter(
    models.GauntletTask.scope == "write",
  ).one()
  discard_starting(parent_id)
  db.expire_all()
  assert db.get(models.ChatRun, writer_task.chat_run_id).status == "running"

  recovered = reconcile_startup_chats(db)
  assert parent_id not in recovered.manual
  db.expire_all()
  assert db.get(models.ChatRun, writer_task.chat_run_id).status == "running"

  scheduled = []

  def schedule(**kwargs):
    scheduled.append(kwargs)
    return True

  monkeypatch.setattr("app.chat._schedule_continuation", schedule)
  asyncio.run(reconcile_running_gauntlets())
  assert [item["run_token"] for item in scheduled] == [writer_task.chat_run_id]
  discard_starting(parent_id)
