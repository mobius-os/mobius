"""End-to-end Goal scenarios: nested plans, parallel helpers, questions, reports.

Each test composes the Goals redesign pieces the way long-running work uses
them (spawn under plan tasks, scoped briefs, ask_parent/answer, result
delivery, owner Stop, restart continuations, compaction refresh) through the
real routes and owning functions. No provider runs: turn starts are captured.
"""
import asyncio
import json
from datetime import timedelta

import pytest

import app.chat as chat_mod
import app.chat_start as chat_start_mod
from app import transcript_rows, auth as auth_mod, delegations, models
from app.goal_context import project_goal
from app.goal_plans import normalize_tasks
from app.timeutil import now_naive_utc
from tests.goal_fixtures import goal_run

# Sample sizes for structural projection tests; not plan admission limits.
MAX_TASKS = 64
MAX_TITLE = 160
MAX_NOTE = 1000
MAX_RESULT = 1000


SCHEMA_RESULT = "SCHEMA_RESULT: users table has a tenant_id column."
LEGAL_RESULT = "LEGAL_RESULT: licence review is unrelated to the backend."
BACKEND_RULE = "BACKEND_RULE: keep the public API backwards compatible."


def _task(key, parent=None, status="pending", **extra):
  return {"id": key, "title": f"{key} title", "status": status, "depends_on": [],
          **({"parent_id": parent} if parent else {}), **extra}


def _release_plan():
  """A nested plan with two parallel running branches and settled evidence."""
  return {"tasks": normalize_tasks([
    _task("release", status="running"),
    _task("schema", "release", "completed", result=SCHEMA_RESULT),
    _task("legal", "release", "completed", result=LEGAL_RESULT),
    _task("backend", "release", "running", depends_on=["schema"],
          completion_condition=BACKEND_RULE),
    _task("backend-api", "backend"),
    _task("backend-db", "backend"),
    _task("frontend", "release", "running"),
    _task("docs", "release", depends_on=["backend", "frontend"]),
  ])}


def _bearer(token):
  return {"Authorization": f"Bearer {token}"}


def _helper_headers(db, row, run_id):
  owner = db.query(models.Owner).first()
  return _bearer(auth_mod.create_agent_token(
    row.child_chat_id, owner.username, owner.token_epoch, run_id=run_id,
    delegation_id=row.id, delegation_chat=row.child_chat_id,
  ))


@pytest.fixture
def started(monkeypatch):
  """Capture helper turn starts instead of running a provider."""
  calls = []

  async def fake_start(**kwargs):
    calls.append(kwargs)
    return True

  monkeypatch.setattr("app.routes.delegations.start_programmatic_chat_turn", fake_start)
  return calls


def _goal_parent(client, owner_token, db, plan=None):
  chat_id = client.post("/api/chats", json={"title": "Release"},
                        headers=_bearer(owner_token)).json()["id"]
  db.add(goal_run(db, id="coord-run", root_run_id="coord-run", chat_id=chat_id,
                  status="running", provider="claude", goal_id="release-goal",
                  goal_objective="Ship the release",
                  goal_plan_json=plan or _release_plan(),
                  started_at=now_naive_utc() - timedelta(minutes=10)))
  db.commit()
  return chat_id


def _spawn(client, headers, parent_chat_id, task_key, plan_task=None):
  body = {"parent_chat_id": parent_chat_id, "task_key": task_key,
          "prompt": f"Work on {task_key}.", "provider": "claude"}
  if plan_task is not None:
    body["plan_task"] = plan_task
  return client.post("/api/delegations", json=body, headers=headers)


def _helper_run(db, row, run_id, status="running"):
  db.add(models.ChatRun(id=run_id, root_run_id=run_id, chat_id=row.child_chat_id,
                        status=status, provider="claude",
                        started_at=now_naive_utc() - timedelta(minutes=1)))
  db.commit()
  return run_id


def _spawned(client, owner_token, db, parent_chat_id, task_key, plan_task):
  response = _spawn(client, _bearer(owner_token), parent_chat_id, task_key, plan_task)
  assert response.status_code == 201, response.text
  db.expire_all()
  return db.get(models.Delegation, response.json()["id"])


def _brief(client, db, row, run_id, task=None):
  path = f"/api/chats/{row.child_chat_id}/goal-brief"
  if task:
    path += f"?task={task}"
  response = client.get(path, headers=_helper_headers(db, row, run_id))
  assert response.status_code == 200, response.text
  return response.json()


# (a) Parallel helpers on nested tasks get scoped briefs ------------------------


def test_parallel_helpers_on_nested_tasks_get_only_their_own_scoped_brief(
  client, owner_token, db, started,
):
  parent = _goal_parent(client, owner_token, db)
  backend = _spawned(client, owner_token, db, parent, "backend-worker", "backend")
  frontend = _spawned(client, owner_token, db, parent, "frontend-worker", "frontend")
  assert (backend.goal_task_id, frontend.goal_task_id) == ("backend", "frontend")
  assert len(started) == 2
  backend_run = _helper_run(db, backend, "backend-run")
  frontend_run = _helper_run(db, frontend, "frontend-run")

  mine = _brief(client, db, backend, backend_run)
  assert mine["role"] == "helper"
  goal = mine["goal"]
  assert goal["focus"] == "backend" and goal["task"]["id"] == "backend"
  assert goal["assignment"]["plan_task"] == "backend"
  assert goal["assignment"]["helper"] == "backend-worker"
  # Its prerequisite's result is delivered; an unrelated sibling's is not.
  assert [d["id"] for d in goal["dependencies"]] == ["schema"]
  assert goal["dependencies"][0]["result"] == SCHEMA_RESULT
  text = json.dumps(mine)
  assert BACKEND_RULE in text
  assert LEGAL_RESULT not in text
  assert "frontend-worker" not in text
  assert not {"checkpoint", "next_action", "outcome_contract"} & set(goal)
  # The per-turn brief is the same projection the route returns.
  from app.goals import helper_goal_brief
  turn_brief = helper_goal_brief(db, backend.child_chat_id)
  embedded = json.loads(turn_brief.split("<mobius_goal_brief>")[1]
                        .split("</mobius_goal_brief>")[0])
  assert embedded == goal

  theirs = _brief(client, db, frontend, frontend_run)["goal"]
  assert theirs["focus"] == "frontend"
  assert "dependencies" not in theirs
  theirs_text = json.dumps(theirs)
  assert SCHEMA_RESULT not in theirs_text and LEGAL_RESULT not in theirs_text
  # The parallel branch is not in the default brief at all; expanding the
  # shared parent shows it as a summary, never its subtree or assignment.
  assert "siblings" not in theirs
  assert "backend-api" not in theirs_text and "backend-worker" not in theirs_text
  release = _brief(client, db, frontend, frontend_run, task="release")["goal"]
  peer = next(c for c in release["children"] if c["id"] == "backend")
  assert peer["descendants"] == {"pending": 2}
  assert "backend-worker" not in json.dumps(release)

  # read_goal reaches every task of the plan in full from either helper.
  plan = db.get(models.ChatGoal, "release-goal").plan_json["tasks"]
  for task in plan:
    read = _brief(client, db, frontend, frontend_run, task=task["id"])["goal"]
    assert read["task"] == task
  # The coordinator sees both parallel branches and their helpers.
  coordinator = client.get(
    f"/api/chats/{parent}/goal-brief?task=release",
    headers=_bearer(auth_mod.create_agent_token(
      parent, db.query(models.Owner).first().username,
      db.query(models.Owner).first().token_epoch, run_id="coord-run")),
  ).json()
  assert coordinator["role"] == "coordinator"
  assert coordinator["goal"]["focus"] == "release"
  assert {h["plan_task"] for h in coordinator["helpers"]} == {"backend", "frontend"}


def test_a_helper_can_file_its_own_helper_under_a_task_of_its_assigned_goal(
  client, owner_token, db, started,
):
  """A branch coordinator splits its task: the nested helper's brief is that subtask."""
  parent = _goal_parent(client, owner_token, db)
  backend = _spawned(client, owner_token, db, parent, "backend-worker", "backend")
  run = _helper_run(db, backend, "backend-run")
  headers = _helper_headers(db, backend, run)

  # Unfiled, the nested helper inherits its parent's branch.
  inherited = _spawn(client, headers, backend.child_chat_id, "backend-review")
  assert inherited.status_code == 201, inherited.text
  # Filed explicitly, it works the named subtask of the same Goal.
  filed = _spawn(client, headers, backend.child_chat_id, "api-worker", "backend-api")
  assert filed.status_code == 201, filed.text
  unknown = _spawn(client, headers, backend.child_chat_id, "ghost", "no-such-task")
  assert unknown.status_code == 422
  # A branch helper splits only its own branch, as its brief is scoped: a task
  # in another branch (or the root) is refused, not silently filed.
  for other in ("frontend", "release"):
    foreign = _spawn(client, headers, backend.child_chat_id, f"stray-{other}", other)
    assert foreign.status_code == 422, foreign.text

  db.expire_all()
  api = db.get(models.Delegation, filed.json()["id"])
  assert api.goal_task_id == "backend-api"
  api_run = _helper_run(db, api, "api-run")
  brief = _brief(client, db, api, api_run)["goal"]
  assert (brief["focus"], brief["assignment"]["depth"]) == ("backend-api", 2)
  assert [a["id"] for a in brief["ancestors"]] == ["release", "backend"]
  assert brief["ancestors"][1]["completion_condition"] == BACKEND_RULE
  # The branch's prerequisite result is inherited; a sibling subtask is not in
  # the default brief, and expanding the branch lists it as a summary.
  assert [d["result"] for d in brief["dependencies"]] == [SCHEMA_RESULT]
  assert "siblings" not in brief
  branch = _brief(client, db, api, api_run, task="backend")["goal"]
  assert "backend-db" in {c["id"] for c in branch["children"]}
  review = db.get(models.Delegation, inherited.json()["id"])
  review_run = _helper_run(db, review, "review-run")
  assert _brief(client, db, review, review_run)["goal"]["focus"] == "backend"


# Shared lifecycle steps -------------------------------------------------------


NARRATION = "NARRATION: still reading the call sites. "


def _settle(db, row, run_id, *, status="completed", report=None, narration=True,
            closing=None):
  """End a helper run as its runner would: progress blocks plus a final report."""
  from app.broadcast import remove_broadcast
  chat_mod.discard_starting(row.child_chat_id)
  remove_broadcast(row.child_chat_id)
  db.expire_all()
  run = db.get(models.ChatRun, run_id)
  run.status = status
  run.ended_at = now_naive_utc()
  chat = db.get(models.Chat, row.child_chat_id)
  message = {"id": f"{run_id}:assistant:1", "role": "assistant", "blocks": [
    *([{"type": "text", "content": NARRATION * 20}] if narration else []),
    *([{"type": "text", "content": closing}] if closing else []),
  ]}
  if report is not None:
    message["blocks"].append({"type": "text", "content": "Done."})
    message["result"] = report
  transcript_rows.replace_all(db, chat, [*transcript_rows.history(chat), message])
  chat.live_assistant = None
  db.commit()


def _ask(client, db, row, run_id, question):
  response = client.post(f"/api/delegations/{row.id}/questions",
                         json={"question": question, "options": []},
                         headers=_helper_headers(db, row, run_id))
  assert response.status_code == 200, response.text
  return response.json()["question_id"]


def _answer(client, headers, row, message, question_id):
  return client.post(f"/api/delegations/{row.id}/messages",
                     json={"message": message, "question_id": question_id},
                     headers=headers)


def _status(db, row):
  db.expire_all()
  return delegations.derived_status(db, db.get(models.Delegation, row.id))


@pytest.fixture
def continuations(monkeypatch):
  """Capture answer continuations instead of running a provider task."""
  calls = []
  monkeypatch.setattr(chat_mod, "_schedule_continuation",
                      lambda **kwargs: calls.append(kwargs) or True)
  return calls


@pytest.fixture
def wakes(monkeypatch):
  calls = []

  async def fake_start(**kwargs):
    calls.append(kwargs)
    return True

  monkeypatch.setattr(chat_start_mod, "start_programmatic_activity_continuation", fake_start)
  return calls


def _end_parent_turn(db, run_id="coord-run"):
  db.expire_all()
  db.get(models.ChatRun, run_id).status = "completed"
  db.get(models.ChatRun, run_id).ended_at = now_naive_utc()
  db.commit()


def _parallel_pair(client, owner_token, db):
  parent = _goal_parent(client, owner_token, db)
  backend = _spawned(client, owner_token, db, parent, "backend-worker", "backend")
  frontend = _spawned(client, owner_token, db, parent, "frontend-worker", "frontend")
  _helper_run(db, backend, "backend-run")
  _helper_run(db, frontend, "frontend-run")
  return parent, backend, frontend


# (b) Parallel question plus finished sibling -----------------------------------


def test_parallel_question_and_finished_sibling_reach_parent_and_one_answer_resumes(
  client, owner_token, db, started, continuations, wakes,
):
  parent, backend, frontend = _parallel_pair(client, owner_token, db)
  _end_parent_turn(db)  # The coordinator waits on its helpers.
  question_id = _ask(client, db, backend, "backend-run", "Keep the v1 endpoint?")
  _settle(db, backend, "backend-run", closing="Asked my parent about v1.")
  _settle(db, frontend, "frontend-run", report="FRONTEND_REPORT: UI shipped.")
  assert _status(db, backend)[0] == "needs_input"
  assert _status(db, frontend)[0] == "completed"
  root = backend.goal_id
  assert root == frontend.goal_id == "release-goal"
  assert backend.parent_root_run_id == frontend.parent_root_run_id == "coord-run"

  # Both settles wake only the idle coordinator (the fake start creates no
  # run, so each settle may attempt its own start; a real start is the latch).
  asyncio.run(delegations.wake_parent_after_child_settled(backend.child_chat_id))
  asyncio.run(delegations.wake_parent_after_child_settled(frontend.child_chat_id))
  assert wakes and {w["chat_id"] for w in wakes} == {parent}
  delivery = delegations.build_delegation_result_context(db, parent, source_work_id=root)
  assert dict(delivery.results) == {backend.id: "backend-run", frontend.id: "frontend-run"}
  assert "FRONTEND_REPORT: UI shipped." in delivery.text
  assert "NARRATION" not in delivery.text
  assert f'"question":{{"id":"{question_id}"' in delivery.text
  assert delivery.text.count('"status":"needs_input"') == 1
  assert delegations.mark_results_delivered(db, dict(delivery.results))
  db.commit()

  # The coordinator's next turn answers with its own run-bound bearer.
  db.add(goal_run(db, id="coord-answer", root_run_id="coord-run", chat_id=parent,
                  status="running", provider="claude", goal_id="release-goal"))
  db.commit()
  coordinator = _bearer(auth_mod.create_agent_token(
    parent, db.query(models.Owner).first().username,
    db.query(models.Owner).first().token_epoch, run_id="coord-answer"))
  first = _answer(client, coordinator, backend, "Keep v1.", question_id)
  assert first.status_code == 202, first.text
  assert first.json()["already_answered"] is False
  # A retried tool call attaches to the same reserved continuation.
  retry = _answer(client, coordinator, backend, "Keep v1.", question_id)
  assert retry.status_code == 202 and retry.json()["already_answered"] is True
  question = db.get(models.DelegationQuestion, question_id)
  assert [c["run_token"] for c in continuations] == [question.answer_run_id]
  db.expire_all()
  child = db.get(models.Chat, backend.child_chat_id)
  cid = delegations.helper_answer_continuation_id(question_id)
  assert [m.get("cid") for m in transcript_rows.history(child)].count(cid) == 1
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == backend.child_chat_id).count() == 2
  # The finished sibling is untouched by the answer.
  assert _status(db, frontend)[0] == "completed"

  # The answer run's report is the helper's next result, delivered once.
  _settle(db, backend, question.answer_run_id, report="BACKEND_REPORT: v1 kept.")
  status, latest, result = _status(db, backend)
  assert (status, latest.id, result) == (
    "completed", question.answer_run_id, "BACKEND_REPORT: v1 kept.")
  later = delegations.build_delegation_result_context(db, parent, source_work_id=root)
  assert dict(later.results) == {backend.id: question.answer_run_id}
  assert "FRONTEND_REPORT" not in later.text and "NARRATION" not in later.text


# (c) Owner Stop leaves an open question; explicit cancel closes it ------------


def test_owner_stop_cancels_working_helpers_but_leaves_an_open_question_unresolved(
  client, owner_token, db, started, continuations,
):
  parent, backend, frontend = _parallel_pair(client, owner_token, db)
  question_id = _ask(client, db, backend, "backend-run", "Drop the legacy column?")
  _settle(db, backend, "backend-run")

  # The owner presses Stop on the coordinator's live turn.
  response = client.post("/api/chat/stop", json={"chat_id": parent},
                         headers=_bearer(owner_token))
  assert response.status_code == 200, response.text
  stopped = response.json()["cancelled_delegations"]
  # The owner's Stop cascade withdraws the working helper only.
  assert stopped == [frontend.id]
  assert _status(db, frontend)[0] == "cancelled"
  status, _, _ = _status(db, backend)
  assert status == "needs_input"
  db.expire_all()
  row = db.get(models.Delegation, backend.id)
  assert row.cancelled_at is None
  question = db.get(models.DelegationQuestion, question_id)
  assert db.get(models.ChatRun, question.answer_run_id) is None  # not answered
  open_now = delegations.open_questions(db, [(row, _status(db, backend)[1])])
  assert open_now[backend.id].id == question_id
  # The Goal is held by the owner, and the question stays visible to it.
  goal = db.get(models.ChatGoal, "release-goal")
  from app.goals import goal_hold
  assert goal.status == "stopped" and goal_hold(goal)["actor"] == "owner"
  helpers = {h["id"]: h for h in delegations.own_helper_statuses(db, parent, "coord-run")}
  assert helpers[backend.id]["status"] == "needs_input"
  assert helpers[backend.id]["question"]["id"] == question_id
  assert helpers[frontend.id]["status"] == "cancelled"

  # Explicit helper cancellation closes it; a late answer cannot revive it.
  response = client.post(f"/api/delegations/{backend.id}/cancel",
                         headers=_bearer(owner_token))
  assert response.status_code == 200, response.text
  assert response.json()["status"] == "cancelled" and response.json()["question"] is None
  late = _answer(client, _bearer(owner_token), backend, "Yes.", question_id)
  assert late.status_code == 409
  assert continuations == []
  db.expire_all()
  assert db.get(models.ChatRun, question.answer_run_id) is None


# (d) Restart continuation keeps the report; manual Resume does not ------------


@pytest.mark.parametrize("reason,inherits", [("restart", True), ("manual", False)])
def test_restarted_helper_keeps_its_report_but_manual_resume_starts_fresh(
  client, owner_token, db, started, reason, inherits,
):
  parent, backend, _frontend = _parallel_pair(client, owner_token, db)
  _settle(db, backend, "backend-run", status="interrupted",
          report="BACKEND_REPORT: migrated before the restart.")
  db.add(models.ChatRun(
    id="backend-resumed", root_run_id="backend-run", chat_id=backend.child_chat_id,
    status="running", provider="claude", started_at=now_naive_utc(),
    continuation_json={"reason": reason, "supersedes_run_token": "backend-run"},
  ))
  db.commit()
  # The resumed run ends blank: no new text, no report.
  _settle(db, backend, "backend-resumed", narration=False)
  status, latest, result = _status(db, backend)
  assert (status, latest.id) == ("completed", "backend-resumed")
  delivery = delegations.build_delegation_result_context(
    db, parent, source_work_id=backend.goal_id)
  assert delivery.results and dict(delivery.results)[backend.id] == "backend-resumed"
  if inherits:
    assert result == "BACKEND_REPORT: migrated before the restart."
    assert "BACKEND_REPORT: migrated before the restart." in delivery.text
  else:
    assert result == ""
    assert "BACKEND_REPORT" not in delivery.text
  assert "NARRATION" not in delivery.text


def test_restart_during_an_answer_run_keeps_that_runs_report_not_the_question_text(
  client, owner_token, db, started, continuations,
):
  parent, backend, _frontend = _parallel_pair(client, owner_token, db)
  question_id = _ask(client, db, backend, "backend-run", "Keep the v1 endpoint?")
  _settle(db, backend, "backend-run", closing="QUESTION_TEXT: asked my parent.")
  answered = _answer(client, _bearer(owner_token), backend, "Keep v1.", question_id)
  assert answered.status_code == 202, answered.text
  answer_run = db.get(models.DelegationQuestion, question_id).answer_run_id
  # A planned restart interrupts the answer run after its report, then resumes it.
  _settle(db, backend, answer_run, status="interrupted",
          report="BACKEND_REPORT: v1 kept, migration done.")
  db.add(models.ChatRun(
    id="answer-resumed", root_run_id="backend-run", chat_id=backend.child_chat_id,
    status="running", provider="claude", started_at=now_naive_utc(),
    continuation_json={"reason": "restart", "supersedes_run_token": answer_run},
  ))
  db.commit()
  assert _status(db, backend)[0] == "running"
  _settle(db, backend, "answer-resumed", narration=False)
  status, latest, result = _status(db, backend)
  assert (status, latest.id) == ("completed", "answer-resumed")
  assert result == "BACKEND_REPORT: v1 kept, migration done."
  assert "QUESTION_TEXT" not in result
  # The answered question stays closed across the restart.
  assert delegations.open_questions(db, [(db.get(models.Delegation, backend.id), latest)]) == {}
  assert len(continuations) == 1


# (e) Nested helper questions stay inside their own branch ---------------------


def test_nested_helper_question_reaches_its_own_parent_never_the_owner(
  client, owner_token, db, started, continuations, wakes,
):
  parent = _goal_parent(client, owner_token, db)
  backend = _spawned(client, owner_token, db, parent, "backend-worker", "backend")
  _helper_run(db, backend, "backend-run")
  nested = _spawn(client, _helper_headers(db, backend, "backend-run"),
                  backend.child_chat_id, "backend-db-worker")
  assert nested.status_code == 201, nested.text
  db.expire_all()
  leaf = db.get(models.Delegation, nested.json()["id"])
  _helper_run(db, leaf, "leaf-run")
  _end_parent_turn(db)

  question_id = _ask(client, db, leaf, "leaf-run", "Which index name?")
  _settle(db, leaf, "leaf-run")
  _settle(db, backend, "backend-run", report="Waiting for my helper.")
  assert _status(db, leaf)[0] == "needs_input"

  # Delivery goes to the asking helper's own parent chat only.
  wakes.clear()
  asyncio.run(delegations.wake_parent_after_child_settled(leaf.child_chat_id))
  asyncio.run(delegations.deliver_results_after_parent_settled(backend.child_chat_id))
  assert {w["chat_id"] for w in wakes} <= {backend.child_chat_id, parent}
  assert any(w["chat_id"] == backend.child_chat_id and w["activity_id"] == leaf.id
             for w in wakes)
  branch = delegations.build_delegation_result_context(
    db, backend.child_chat_id, source_work_id=leaf.goal_id)
  assert question_id in branch.text
  top = delegations.build_delegation_result_context(
    db, parent, source_work_id=backend.goal_id)
  assert question_id not in top.text and "Which index name?" not in top.text
  assert leaf.id not in dict(top.results)

  # It is never an owner card, wait, or approval anywhere in the tree.
  db.expire_all()
  for chat_id in (parent, backend.child_chat_id, leaf.child_chat_id):
    assert db.get(models.Chat, chat_id).pending_question_id is None
  assert db.query(models.ChatWait).count() == 0
  # The top coordinator cannot answer it: only its parent helper may.
  db.add(goal_run(db, id="coord-later", root_run_id="coord-run", chat_id=parent,
                  status="running", provider="claude", goal_id="release-goal"))
  db.commit()
  from_owner_agent = _answer(
    client, _bearer(auth_mod.create_agent_token(
      parent, db.query(models.Owner).first().username,
      db.query(models.Owner).first().token_epoch, run_id="coord-later")),
    leaf, "idx_a", question_id)
  assert from_owner_agent.status_code in (403, 404)
  db.add(models.ChatRun(id="backend-next", root_run_id="backend-run",
                        chat_id=backend.child_chat_id, status="running",
                        provider="claude", started_at=now_naive_utc()))
  db.commit()
  answered = _answer(client, _helper_headers(db, backend, "backend-next"),
                     leaf, "idx_tenant", question_id)
  assert answered.status_code == 202, answered.text
  assert len(continuations) == 1
  db.expire_all()
  assert db.get(models.Chat, parent).pending_question_id is None


# (f) Compaction refresh carries the current assignment once -------------------


def test_compaction_refresh_delivers_the_post_update_assignment_once(
  client, owner_token, db, started,
):
  from app.goals import compaction_brief_refresh, turn_goal_brief
  parent, backend, _frontend = _parallel_pair(client, owner_token, db)
  start_brief = turn_goal_brief(db, backend.child_chat_id, "backend-run", delegated=True)
  assert "NEW_BACKEND_CONSTRAINT" not in start_brief
  refresh = compaction_brief_refresh(backend.child_chat_id, "backend-run", delegated=True)
  # Nothing is owed before a compaction.
  assert asyncio.run(refresh.take()) == ""

  # The coordinator revises the helper's task and its prerequisite meanwhile.
  owner = db.query(models.Owner).first()
  coordinator = _bearer(auth_mod.create_agent_token(
    parent, owner.username, owner.token_epoch, run_id="coord-run"))
  updated = client.post(f"/api/chats/{parent}/goal/update", headers=coordinator, json={
    "tasks": [{"id": "backend", "note": "NEW_BACKEND_CONSTRAINT: no downtime."},
              {"id": "schema", "status": "completed", "result": "SCHEMA_V2_RESULT"}]})
  assert updated.status_code == 200, updated.text

  refresh.mark_compacted()
  delivered = asyncio.run(refresh.take())
  assert delivered.count("<mobius_goal_brief>") == 1
  brief = json.loads(delivered.split("<mobius_goal_brief>")[1].split("</mobius_goal_brief>")[0])
  assert brief["revision"] == db.get(models.ChatGoal, "release-goal").revision
  assert brief["task"]["note"] == "NEW_BACKEND_CONSTRAINT: no downtime."
  assert brief["dependencies"][0]["result"] == "SCHEMA_V2_RESULT"
  assert SCHEMA_RESULT not in delivered
  assert asyncio.run(refresh.take()) == ""

  # Removing the task from the plan refreshes to the overview, flagged.
  removed = client.post(f"/api/chats/{parent}/goal/update", headers=coordinator, json={
    "tasks": [{"id": "backend", "status": "cancelled"}]})
  assert removed.status_code == 200, removed.text
  refresh.mark_compacted()
  again = asyncio.run(refresh.take())
  current = json.loads(again.split("<mobius_goal_brief>")[1].split("</mobius_goal_brief>")[0])
  assert current["task"]["status"] == "cancelled"
  assert asyncio.run(refresh.take()) == ""


# (g) Plan growth ---------------------------------------------------------------


def _chars(value):
  return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _wide_plan():
  """MAX_TASKS tasks: four running branches of leaves with long text and results."""
  tasks = [{"id": "root", "title": "R" * MAX_TITLE, "status": "running",
            "completion_condition": "Root rule. " * 40}]
  branches = 4
  per_branch = (MAX_TASKS - 1 - branches) // branches
  for b in range(branches):
    tasks.append({"id": f"b{b}", "title": "B" * MAX_TITLE, "status": "running",
                  "parent_id": "root", "completion_condition": f"Branch {b} rule. " * 30})
    for i in range(per_branch):
      done = i < per_branch // 2
      tasks.append({
        "id": f"b{b}-t{i}", "title": "T" * MAX_TITLE, "parent_id": f"b{b}",
        "status": "completed" if done else ("running" if i == per_branch // 2 else "pending"),
        "note": "n" * MAX_NOTE, "completion_condition": "c" * MAX_NOTE,
        **({"result": "r" * MAX_RESULT} if done else {}),
        "depends_on": [f"b{b}-t{j}" for j in range(max(0, i - 3), i)],
      })
  while len(tasks) < MAX_TASKS:
    tasks.append({"id": f"extra-{len(tasks)}", "title": "E", "parent_id": "root",
                  "status": "pending"})
  return normalize_tasks(tasks)


def test_maximum_plan_with_parallel_branches_keeps_briefs_well_below_the_plan(
  client, owner_token, db, started,
):
  tasks = _wide_plan()
  assert len(tasks) == MAX_TASKS
  parent = _goal_parent(client, owner_token, db, plan={"tasks": tasks})
  goal = db.get(models.ChatGoal, "release-goal")
  plan_chars = _chars(goal.plan_json)
  coordinator = project_goal(goal)
  # Running leaves in four branches: the coordinator focuses their common root.
  assert coordinator["focus"] == "root"
  assert _chars(coordinator) * 4 < plan_chars

  running = [t["id"] for t in tasks if t["status"] == "running" and "-t" in t["id"]]
  assert len(running) == 4
  for index, task_id in enumerate(running):
    row = _spawned(client, owner_token, db, parent, f"worker-{index}", task_id)
    run = _helper_run(db, row, f"worker-run-{index}")
    brief = _brief(client, db, row, run)["goal"]
    assert brief["focus"] == task_id
    # Full detail of its own task and of its prerequisites' settled evidence;
    # nothing is clipped, and the rest of the plan stays out of the brief.
    assert brief["task"] == next(t for t in tasks if t["id"] == task_id)
    assert {d["id"] for d in brief["dependencies"]} == set(brief["task"]["depends_on"])
    assert all(d.get("result") == "r" * MAX_RESULT for d in brief["dependencies"]
               if next(t for t in tasks if t["id"] == d["id"]).get("result"))
    assert _chars(brief) * 4 < plan_chars
    dependency = brief["task"]["depends_on"][0]
    assert _brief(client, db, row, run, task=dependency)["goal"]["task"] == next(
      t for t in tasks if t["id"] == dependency)


def test_blocked_work_stays_visible_once_even_when_it_dominates_the_plan():
  """Blocker reasons are never clipped or paged away.

  The coordinator owns the whole plan, so it lists every blocker exactly once
  with its full reason. A helper sees only blockers inside its own branch: a
  plan dominated by other branches' blockers leaves its brief small.
  """
  from types import SimpleNamespace
  tasks = [_task("mine", status="running"), _task("mine-sub", "mine", "blocked",
           note="Waiting on the owner's API key")] + [
    {"id": f"x{i}", "title": "T" * MAX_TITLE, "status": "blocked",
     "note": (f"{i:02d}:" + "n" * MAX_NOTE)[:MAX_NOTE], "depends_on": []}
    for i in range(MAX_TASKS - 2)]
  goal = SimpleNamespace(id="g", revision=1, objective="o", status="open",
                         checkpoint=None, next_action=None, hold_json=None,
                         plan_json={"tasks": normalize_tasks(tasks)})
  coordinator = project_goal(goal)
  listed = [s["id"] for s in coordinator.get("siblings", [])] + [
    b["id"] for b in coordinator.get("open_blockers", [])] + [
    c["id"] for c in coordinator.get("children", [])]
  blocked = sorted(t["id"] for t in tasks if t["status"] == "blocked")
  assert sorted(key for key in listed if key in blocked) == blocked
  assert len(listed) == len(set(listed))
  assert all(len(b["note"]) == MAX_NOTE
             for b in coordinator.get("open_blockers", []) if b["id"].startswith("x"))
  assert _chars(coordinator) <= _chars(goal.plan_json)

  brief = project_goal(goal, "mine", role="helper")
  own = [c for c in brief.get("children", []) if c["id"] == "mine-sub"]
  assert own and own[0]["note"] == "Waiting on the owner's API key"
  assert "open_blockers" not in brief
  assert _chars(brief) * 20 < _chars(goal.plan_json)
