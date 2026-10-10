"""Scoped Goal briefs: exact assignment, bounded projection, read_goal, refresh."""

import asyncio
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest

from app.chat_writer import create_chat

from app import auth as auth_mod, models
from app.goal_context import project_goal
from app.goal_plans import goal_assignment, normalize_tasks
from tests.goal_fixtures import goal_run

# Sample sizes for structural projection tests; not plan admission limits.
MAX_TASKS = 64
MAX_TITLE = 160
MAX_NOTE = 1000
MAX_RESULT = 1000


def _goal(tasks, **extra):
  values = dict(id="goal", revision=3, objective="Entire approved outcome", status="open",
                checkpoint="OWNER_CHECKPOINT", next_action="OWNER_NEXT_ACTION",
                plan_json={"tasks": tasks}, hold_json=None)
  values.update(extra)
  return SimpleNamespace(**values)


def _task(key, parent=None, status="pending", **kwargs):
  return {"id": key, "title": key + " title", "status": status, "depends_on": [],
          **({"parent_id": parent} if parent else {}), **kwargs}


def _chars(value):
  return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


# Projection ---------------------------------------------------------------


def test_helper_view_omits_coordinator_authority_but_keeps_hold():
  goal = _goal([_task("a", status="running")])
  helper = project_goal(goal, "a", role="helper")
  assert not {"checkpoint", "next_action", "outcome_contract"} & set(helper)
  coordinator = project_goal(goal, "a")
  assert coordinator["checkpoint"] == "OWNER_CHECKPOINT"
  held = _goal([_task("a")], status="stopped", hold_json={
    "cause": "stop", "actor": "owner", "source_id": "s", "run_id": None,
    "actor_id": None, "at": "2026-10-03T12:00:00+00:00"})
  assert project_goal(held, role="helper")["hold"]["actor"] == "owner"
  with pytest.raises(ValueError):
    project_goal(goal, role="owner")


def test_summaries_are_structural_flags_and_read_goal_expands_in_full():
  long = "R" * MAX_RESULT
  goal = _goal([_task("parent", status="running"),
                _task("done", "parent", "completed", result=long, note="aside",
                      completion_condition="Verified by test"),
                _task("short", "parent", "completed", result="Verified"),
                _task("later", "parent", depends_on=["done"])])
  saved = copy.deepcopy(goal.plan_json)
  view = project_goal(goal, "parent")
  children = {child["id"]: child for child in view["children"]}
  # Structure, not clipped text: id/title/status, flags naming what read_goal
  # expands, and dependency ids. Short text is not inlined either.
  assert children["done"] == {"id": "done", "title": "done title", "status": "completed",
                              "has_result": True, "has_note": True}
  assert children["short"] == {"id": "short", "title": "short title",
                               "status": "completed", "has_result": True}
  assert children["later"]["depends_on"] == ["done"]
  text = json.dumps(view)
  for gone in ("_excerpt", "_chars", "_page", "navigation", "Verified by test"):
    assert gone not in text
  # Focusing the task is the expansion: its full record, unclipped.
  assert project_goal(goal, "done")["task"]["result"] == long
  assert goal.plan_json == saved


def test_ancestor_constraints_stay_whole_while_sibling_detail_is_a_flag():
  constraint = "Keep every owner file. " * 40
  goal = _goal([_task("root", status="running", completion_condition=constraint),
                _task("leaf", "root", "running"),
                _task("peer", "root", completion_condition=constraint, note="peer note")])
  view = project_goal(goal, "leaf")
  assert view["ancestors"][0]["completion_condition"] == constraint
  peer = view["siblings"][0]
  assert peer["has_note"] is True
  assert "completion_condition" not in peer and "note" not in peer
  # A helper's default view is its vertical branch; its parent lists peers.
  helper = project_goal(goal, "leaf", role="helper")
  assert helper["ancestors"][0]["completion_condition"] == constraint
  assert "siblings" not in helper and "totals" not in helper
  assert [c["id"] for c in project_goal(goal, "root", role="helper")["children"]] == [
    "leaf", "peer"]


def test_a_helper_sees_blockers_that_can_hold_up_its_work_with_full_reasons():
  """Blockers in a helper's subtree or along its prerequisite chain (however
  deep, with their subtrees) are shown with full reasons; a blocker in an
  unrelated branch is the coordinator's, who sees every blocker once."""
  reason = "Owner must approve the external account. " * 20
  goal = _goal([_task("mine", status="running", depends_on=["prep"]),
                _task("mine-sub", "mine", "blocked", note="own reason"),
                _task("prep", depends_on=["upstream"]),
                _task("upstream"), _task("upstream-sub", "upstream", "blocked", note=reason),
                _task("other"), _task("deep", "other"),
                _task("far", "deep", "blocked", note="unrelated reason")])
  view = project_goal(goal, "mine", role="helper")
  assert [b["id"] for b in view["open_blockers"]] == ["upstream-sub"]
  assert view["open_blockers"][0]["note"] == reason
  assert view["children"][0]["note"] == "own reason"
  assert "unrelated reason" not in json.dumps(view)
  coordinator = project_goal(goal)
  listed = [b["id"] for b in coordinator.get("open_blockers", [])]
  assert {"upstream-sub", "far"} <= set(listed) and len(listed) == len(set(listed))
  # A blocker already shown elsewhere is not repeated.
  shown = project_goal(goal, "deep")
  assert "far" not in [b["id"] for b in shown.get("open_blockers", [])]
  assert shown["children"][0]["note"] == "unrelated reason"


def test_helper_gets_its_own_task_and_prerequisite_results_in_full():
  evidence = "E" * MAX_RESULT
  goal = _goal([_task("prep", status="completed", result=evidence, note="prep note"),
                _task("mine", status="running", depends_on=["prep"], result="partial"),
                _task("unrelated", status="completed", result="U" * MAX_RESULT)])
  view = project_goal(goal, "mine", role="helper")
  assert view["task"]["result"] == "partial"
  assert view["dependencies"][0]["result"] == evidence
  assert view["dependencies"][0]["note"] == "prep note"
  assert "unrelated" not in json.dumps(view)
  # Expanding the unrelated task is allowed and returns it in full.
  assert project_goal(goal, "unrelated", role="helper")["task"]["result"] == "U" * MAX_RESULT


def test_every_task_in_a_list_is_shown_without_paging():
  children = [_task(f"c{i:02d}", "parent", "completed", result="ok") for i in range(MAX_TASKS - 3)]
  children[50]["status"] = "blocked"
  children[50]["note"] = "Blocked late in plan order"
  goal = _goal([_task("parent", status="running"), *children,
                _task("x"), _task("y")])
  view = project_goal(goal, "parent")
  # Plan order, all of them: MAX_TASKS is the bound, not a page size.
  assert [c["id"] for c in view["children"]] == [c["id"] for c in children]
  assert view["children"][50]["note"] == "Blocked late in plan order"
  assert not [key for key in view if key.endswith("_page") or key == "navigation"]
  with pytest.raises(TypeError):
    project_goal(goal, "parent", page=2)


def _max_plan(width, depth):
  """An admitted plan with every text field at its validation limit."""
  text = lambda c: (c * MAX_NOTE)
  tasks, parent = [], None
  for level in range(depth):
    key = f"branch-{level}"
    tasks.append({"id": key, "title": "T" * MAX_TITLE, "status": "running",
                  "parent_id": parent, "depends_on": [],
                  "completion_condition": text("C"), "note": text("N")})
    parent = key
  for index in range(width):
    tasks.append({"id": f"leaf-{index}", "title": "L" * MAX_TITLE, "status": "completed",
                  "parent_id": parent, "depends_on": [],
                  "completion_condition": text("c"), "result": "x" * MAX_RESULT})
  return normalize_tasks([{k: v for k, v in t.items() if v is not None} for t in tasks])


def test_production_maximum_plan_brief_is_well_below_the_plan():
  tasks = _max_plan(MAX_TASKS - 3, 3)
  assert len(tasks) == MAX_TASKS
  goal = _goal(tasks)
  plan_chars = _chars(goal.plan_json)
  coordinator = project_goal(goal)
  helper = project_goal(goal, "leaf-0", role="helper")
  assert coordinator["focus"] == "branch-2"
  assert len(coordinator["children"]) == MAX_TASKS - 3
  # Summaries carry no result text, so every child is listed and the brief
  # is a small fraction of the plan; whole ancestor constraints remain.
  assert _chars(coordinator) < plan_chars / 5
  assert _chars(helper) < plan_chars / 5
  assert helper["task"]["result"] == "x" * MAX_RESULT


def test_stress_deep_chain_keeps_every_ancestor_constraint_without_descendants():
  tasks = [_task(f"d{i}", f"d{i - 1}" if i else None, "running",
                 completion_condition=f"constraint {i}") for i in range(200)]
  view = project_goal(_goal(tasks), "d100", role="helper")
  assert [a["completion_condition"] for a in view["ancestors"]] == [
    f"constraint {i}" for i in range(100)]
  assert "d101" in json.dumps(view["children"]) and "d150" not in json.dumps(view)


def test_stress_wide_plan_beyond_production_limit_stays_structural():
  tasks = [_task("root", status="running")] + [
    _task(f"w{i}", "root", note="detail " * 100, result="r" * 1000) for i in range(500)]
  goal = _goal(tasks)
  view = project_goal(goal, "root")
  assert len(view["children"]) == 500
  assert _chars(view) < _chars(goal.plan_json) / 10


# Assignment resolution ----------------------------------------------------


def _owner_chat(db, chat_id):
  db.add(create_chat(id=chat_id, title=chat_id, messages=[]))
  db.flush()


def _helper(db, parent_chat_id, root, key, *, task=None, **extra):
  child = f"child-{key}"
  prompt = f"Do {key}"
  db.add(create_chat(id=child, title=key, provider="claude",
                     messages=[{"role": "user", "content": prompt}]))
  db.flush()
  row = models.Delegation(
    id=f"del-{key}", parent_chat_id=parent_chat_id, parent_root_run_id=root,
    task_key=key, goal_task_id=task, child_chat_id=child, provider="claude",
    scope="write", cwd="/data", prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
    **extra,
  )
  db.add(row)
  db.flush()
  return row


def _goal_fixture(db, chat_id="owner-chat", goal_id="goal-a", run_id="run-a", status="running"):
  _owner_chat(db, chat_id)
  db.add(goal_run(db, id=run_id, chat_id=chat_id, status=status, goal_id=goal_id,
                  goal_objective="Objective A", goal_plan_json={"tasks": [
                    _task("build", status="running"), _task("verify", depends_on=["build"]),
                  ]}))
  db.flush()
  return chat_id


def test_assignment_uses_the_anchored_goal_not_the_parents_latest_goal(db):
  chat = _goal_fixture(db)
  helper = _helper(db, chat, "goal-a", "builder", task="build")
  # The parent later runs a different Goal; the helper's assignment is unchanged.
  db.add(goal_run(db, id="run-b", chat_id=chat, status="running", goal_id="goal-b",
                  goal_objective="Objective B", goal_plan_json={"tasks": [_task("other")]}))
  db.commit()
  found = goal_assignment(db, helper.child_chat_id)
  assert (found.goal.id, found.plan_task, found.depth) == ("goal-a", "build", 1)


def test_root_promoted_after_spawn_does_not_retroactively_adopt_helper(db):
  _owner_chat(db, "chat-p")
  db.add(goal_run(db, id="turn-1", chat_id="chat-p", status="completed"))
  db.add(goal_run(db, id="turn-2", root_run_id="turn-1", chat_id="chat-p", status="running",
                  goal_id="goal-p", goal_objective="Promoted", goal_plan_json={"tasks": [_task("a")]}))
  helper = _helper(db, "chat-p", "turn-1", "early")
  db.commit()
  assert goal_assignment(db, helper.child_chat_id) is None


def test_nested_helper_inherits_nearest_filed_task_and_depth(db):
  chat = _goal_fixture(db)
  top = _helper(db, chat, "goal-a", "builder", task="build")
  mid = _helper(db, top.child_chat_id, "top-run", "mid")
  leaf = _helper(db, mid.child_chat_id, "mid-run", "leaf", task="verify")
  db.commit()
  inherited = goal_assignment(db, mid.child_chat_id)
  assert (inherited.goal.id, inherited.plan_task, inherited.depth) == ("goal-a", "build", 2)
  nearest = goal_assignment(db, leaf.child_chat_id)
  assert (nearest.plan_task, nearest.depth, nearest.helper) == ("verify", 3, "leaf")


def test_ambiguous_unowned_app_and_source_work_roots_get_no_assignment(db):
  _owner_chat(db, "chat-q")
  db.add(goal_run(db, id="r1", chat_id="chat-q", status="completed", goal_id="g1",
                  goal_objective="One"))
  db.add(goal_run(db, id="r2", root_run_id="r1", chat_id="chat-q", status="running",
                  goal_id="g2", goal_objective="Two"))
  ambiguous = _helper(db, "chat-q", "r1", "ambiguous")
  plain = _helper(db, "chat-q", "no-goal-root", "plain")
  app = models.App(slug="brief-app", source_dir="/tmp/brief-app", name="A",
                   description="", jsx_source="")
  db.add(app)
  db.flush()
  chat = _goal_fixture(db)
  owned = _helper(db, chat, "goal-a", "owned", app_id=app.id)
  nested_owned = _helper(db, owned.child_chat_id, "x", "nested-owned")
  source = _helper(db, chat, "goal-a", "source", source_work_id="work-1")
  db.commit()
  for row in (ambiguous, plain, owned, nested_owned, source):
    assert goal_assignment(db, row.child_chat_id) is None, row.task_key
  assert goal_assignment(db, chat) is None


def test_removed_plan_task_falls_back_to_overview_with_a_named_flag(db):
  chat = _goal_fixture(db)
  helper = _helper(db, chat, "goal-a", "stale", task="gone")
  db.commit()
  found = goal_assignment(db, helper.child_chat_id)
  assert (found.plan_task, found.plan_task_missing) == (None, True)


# read_goal ----------------------------------------------------------------


def _token(db, chat_id, run_id, delegation_id=None):
  owner = db.query(models.Owner).first()
  return {"Authorization": "Bearer " + auth_mod.create_agent_token(
    chat_id, owner.username, owner.token_epoch, run_id=run_id,
    delegation_id=delegation_id, delegation_chat=chat_id if delegation_id else None,
  )}


def _helper_run(db, row, run_id="helper-run", status="running"):
  db.add(models.ChatRun(id=run_id, chat_id=row.child_chat_id, status=status, provider="claude"))
  db.commit()
  return run_id


def test_helper_reads_only_its_assigned_goal_without_owner_fields(client, owner_token, db):
  chat = _goal_fixture(db)
  db.add(models.ChatGoal(id="goal-other", chat_id=chat, objective="OTHER_GOAL", status="open"))
  helper = _helper(db, chat, "goal-a", "builder", task="build")
  run = _helper_run(db, helper)
  headers = _token(db, helper.child_chat_id, run, helper.id)
  response = client.get(f"/api/chats/{helper.child_chat_id}/goal-brief", headers=headers)
  assert response.status_code == 200, response.text
  body = response.json()
  assert body["role"] == "helper" and body["goal"]["focus"] == "build"
  assert body["goal"]["assignment"]["plan_task"] == "build"
  text = json.dumps(body)
  for leaked in ("OWNER_CHECKPOINT", "OWNER_NEXT_ACTION", "OTHER_GOAL", "outcome_contract"):
    assert leaked not in text
  navigated = client.get(f"/api/chats/{helper.child_chat_id}/goal-brief?task=verify",
                         headers=headers).json()
  assert navigated["goal"]["task"]["id"] == "verify"
  assert client.get(f"/api/chats/{helper.child_chat_id}/goal-brief?task=missing",
                    headers=headers).status_code == 422
  # Its bearer cannot read another chat's brief, and a mismatched delegation
  # identity cannot borrow this chat's assignment.
  assert client.get(f"/api/chats/{chat}/goal-brief", headers=headers).status_code == 403
  other = _helper(db, chat, "goal-a", "other")
  forged = _token(db, helper.child_chat_id, run, other.id)
  assert client.get(f"/api/chats/{helper.child_chat_id}/goal-brief",
                    headers=forged).status_code == 403


def test_coordinator_reads_its_goal_and_own_helper_status(client, owner_token, db):
  chat = _goal_fixture(db)
  db.get(models.ChatGoal, "goal-a").checkpoint = "OWNER_CHECKPOINT"
  _helper(db, chat, "goal-a", "builder", task="build")
  db.commit()
  headers = _token(db, chat, "run-a")
  body = client.get(f"/api/chats/{chat}/goal-brief?task=verify", headers=headers).json()
  assert body["role"] == "coordinator"
  assert body["goal"]["focus"] == "verify" and body["goal"]["task"]["id"] == "verify"
  assert body["goal"]["checkpoint"] == "OWNER_CHECKPOINT"
  assert body["helpers"] == [{"id": "del-builder", "task_key": "builder",
                              "status": "starting", "plan_task": "build"}]


def test_coordinator_overview_is_update_goal_and_read_goal_only_expands(client, owner_token, db):
  """One overview per level: a coordinator's is update_goal with no
  arguments; read_goal is the single per-task expansion for both levels."""
  chat = _goal_fixture(db)
  db.commit()
  headers = _token(db, chat, "run-a")
  response = client.get(f"/api/chats/{chat}/goal-brief", headers=headers)
  assert response.status_code == 422
  assert "update_goal with no arguments" in response.json()["detail"]
  assert client.get(f"/api/chats/{chat}/goal-brief?task=build",
                    headers=headers).status_code == 200


def test_reading_a_held_goal_never_resumes_or_attaches_it(client, owner_token, db):
  _owner_chat(db, "held-chat")
  db.add(goal_run(db, id="held-run", chat_id="held-chat", status="stopped", goal_id="held",
                  goal_objective="Held outcome", goal_plan_json={"tasks": [_task("a")]}))
  db.add(models.ChatRun(id="plain-run", chat_id="held-chat", status="running", provider="claude"))
  db.commit()
  goal = db.get(models.ChatGoal, "held")
  before = (goal.status, goal.revision)
  body = client.get("/api/chats/held-chat/goal-brief?task=a",
                    headers=_token(db, "held-chat", "plain-run")).json()
  assert body["goal"]["status"] == "stopped"
  db.expire_all()
  assert (db.get(models.ChatGoal, "held").status, db.get(models.ChatGoal, "held").revision) == before
  assert db.get(models.ChatRun, "plain-run").goal_id is None


def test_helper_without_goal_reads_none(client, owner_token, db):
  _owner_chat(db, "solo")
  helper = _helper(db, "solo", "plain-root", "solo-helper")
  run = _helper_run(db, helper)
  body = client.get(f"/api/chats/{helper.child_chat_id}/goal-brief",
                    headers=_token(db, helper.child_chat_id, run, helper.id)).json()
  assert body == {"role": "helper", "goal": None, "helpers": []}


# Per-turn delivery ----------------------------------------------------------


def _run_helper_turn(db, monkeypatch, row, *, provider, session_id, hosts, run_id, messages):
  from app import chat as chat_mod, schemas
  from app.broadcast import create_broadcast
  monkeypatch.setenv("MOBIUS_HELPER_HOSTS", "1" if hosts else "0")
  cls = "ClaudeProvider" if provider == "claude" else "CodexProvider"
  monkeypatch.setattr(f"app.providers.{cls}.check_auth", lambda *a: None)
  monkeypatch.setattr(f"app.providers.{cls}.ensure_auth", lambda *a: asyncio.sleep(0))
  captured = []

  async def runner(**kwargs):
    captured.append(kwargs)
    return {"session_id": session_id or "new-session", "cost_usd": 0.0, "error": None}

  monkeypatch.setattr(f"app.{provider}_sdk_runner.run_{provider}_sdk_turn", runner)
  monkeypatch.setattr("app.claude_helper_host.run_claude_host_turn", runner)
  # A resumed private Claude session needs its CLI transcript on disk.
  monkeypatch.setattr("app.claude_sdk_runner._resumable", lambda *a: True)
  child = db.get(models.Chat, row.child_chat_id)
  child.provider = provider
  child.session_id = session_id
  row.provider = provider
  db.add(models.ChatRun(id=run_id, chat_id=row.child_chat_id, status="running",
                        provider=provider, provider_execution_admitted=False))
  db.commit()
  create_broadcast(row.child_chat_id)
  asyncio.run(chat_mod._run_chat_impl(
    messages=[schemas.ChatMessage(**m) for m in messages],
    chat_id=row.child_chat_id, session_id=session_id, provider_id=provider,
    run_token=run_id, run_gen=chat_mod.current_run_generation(row.child_chat_id),
  ))
  assert captured, "provider was not reached"
  return captured[-1]


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("hosts", [True, False])
@pytest.mark.parametrize("path", ["fresh", "follow_up_or_restart"])
def test_every_helper_turn_carries_its_fresh_assignment_brief(
  client, owner_token, db, monkeypatch, provider, hosts, path,
):
  chat = _goal_fixture(db)
  helper = _helper(db, chat, "goal-a", "builder", task="build")
  db.commit()
  messages = [{"role": "user", "content": "Do builder"}]
  session = None
  if path != "fresh":
    messages += [{"role": "assistant", "content": "partial"},
                 {"role": "user", "content": "Resume after restart"}]
    session = "existing-session"
  sent = _run_helper_turn(db, monkeypatch, helper, provider=provider, session_id=session,
                          hosts=hosts, run_id=f"helper-{path}", messages=messages)
  prompt = sent["user_message"]
  assert prompt.count("<mobius_goal_brief>") == 1
  brief = json.loads(prompt.split("<mobius_goal_brief>")[1].split("</mobius_goal_brief>")[0])
  assert brief["focus"] == "build" and brief["assignment"]["helper"] == "builder"
  assert "OWNER_CHECKPOINT" not in prompt and "<mobius_goal>" not in prompt
  # Every execution path retains its turn-owned refresh, including a shared
  # Claude host; provider-specific hooks determine the delivery boundary.
  assert sent["goal_brief_refresh"] is not None


def test_nested_parent_turn_sees_its_own_helpers(client, owner_token, db, monkeypatch):
  chat = _goal_fixture(db)
  helper = _helper(db, chat, "goal-a", "builder", task="build")
  _helper(db, helper.child_chat_id, "helper-nested", "grandchild", goal_id="goal-a")
  db.commit()
  sent = _run_helper_turn(db, monkeypatch, helper, provider="codex", session_id=None,
                          hosts=False, run_id="helper-nested",
                          messages=[{"role": "user", "content": "Do builder"}])
  assert '"task_key":"grandchild"' in sent["user_message"]


def test_new_helper_policy_points_to_read_goal_for_compaction():
  from app.delegations import RunPolicy
  prompt = RunPolicy(delegation_id="d", app_id=None, provider="claude", model=None,
                     effort=None, cwd="/data").system_prompt
  assert "read_goal" in prompt and "<mobius_goal_brief>" in prompt


# Compaction refresh -----------------------------------------------------------


def test_compaction_pointer_names_each_levels_own_read(monkeypatch):
  from app import goals
  monkeypatch.setattr(goals, "turn_goal_brief", lambda *a, **k: "BRIEF")
  for delegated, pointer in ((True, "read_goal re-reads it"),
                             (False, "update_goal with no arguments shows the plan")):
    refresh = goals.compaction_brief_refresh("chat", "run", delegated=delegated)
    monkeypatch.setattr(refresh, "_load", lambda: "BRIEF")
    refresh.mark_compacted()
    assert asyncio.run(refresh.take()) == (
      f"Context was compacted; current Goal brief follows ({pointer}).\nBRIEF")


def test_refresh_is_quiet_until_compaction_then_delivers_once():
  from app.goals import CompactionBriefRefresh
  loads = []
  refresh = CompactionBriefRefresh(lambda: loads.append(1) or "<mobius_goal>x</mobius_goal>")
  assert asyncio.run(refresh.take()) == "" and loads == []
  refresh.mark_compacted()
  assert "current Goal brief" in asyncio.run(refresh.take())
  assert asyncio.run(refresh.take()) == "" and loads == [1]
  failing = CompactionBriefRefresh(lambda: 1 / 0)
  failing.mark_compacted()
  assert asyncio.run(failing.take()) == ""


def test_refresh_loader_reads_current_plan_in_its_own_session(client, owner_token, db):
  from app.goals import compaction_brief_refresh
  chat = _goal_fixture(db)
  helper = _helper(db, chat, "goal-a", "builder", task="build")
  db.commit()
  refresh = compaction_brief_refresh(helper.child_chat_id, "unused", delegated=True)
  goal = db.get(models.ChatGoal, "goal-a")
  goal.plan_json = {"tasks": [_task("build", status="running", note="NEW_CONSTRAINT")]}
  db.commit()
  refresh.mark_compacted()
  assert "NEW_CONSTRAINT" in asyncio.run(refresh.take())


def _stale_refresh(text="FRESH_BRIEF"):
  from app.goals import CompactionBriefRefresh
  return CompactionBriefRefresh(lambda: text)


@pytest.mark.asyncio
async def test_claude_compaction_refreshes_brief_at_next_root_tool_result(monkeypatch):
  from tests.test_claude_sdk_runner import _Bus, _install_fake_client, _run_turn
  clients = _install_fake_client(monkeypatch)
  refresh = _stale_refresh()
  await _run_turn("chat-claude-refresh", bc=_Bus(), cwd="/data", goal_brief_refresh=refresh)
  hooks = clients[0].options.hooks
  brief_hook = next(hook for matcher in hooks["PostToolUse"] for hook in matcher.hooks
                    if hook.__name__ == "goal_brief_after_compaction_hook")
  tool = {"hook_event_name": "PostToolUse", "tool_name": "Bash"}
  assert await brief_hook(tool, "t0", {}) == {"continue_": True}
  await hooks["PreCompact"][0].hooks[0]({"trigger": "auto"}, None, {})
  # A Task-spawned subagent's tool result is not the root context.
  assert await brief_hook({**tool, "agent_id": "sub"}, "t1", {}) == {"continue_": True}
  refreshed = await brief_hook(tool, "t2", {})
  assert refreshed["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
  assert refreshed["hookSpecificOutput"]["additionalContext"].endswith("FRESH_BRIEF")
  assert await brief_hook(tool, "t3", {}) == {"continue_": True}


def test_codex_compaction_notification_alone_owes_no_brief(monkeypatch):
  # Only Codex's post-compaction hook delivers the brief; the runner's own
  # compaction observation is display-only and arms nothing a later hook or
  # steer could consume.
  from tests import test_codex_sdk_runner as harness

  async def no_hook_discovery(codex, sdk, cwd, config, hook_overrides, **_):
    return config

  monkeypatch.setattr(harness.codex_sdk_runner, "_codex_platform_hook_thread_config",
                      no_hook_discovery)
  for kind in ("context_compaction_item", "legacy_notification"):
    refresh = _stale_refresh()
    original = harness.codex_sdk_runner.run_codex_sdk_turn

    async def run_with_refresh(**kwargs):
      return await original(**kwargs, goal_brief_refresh=refresh)

    monkeypatch.setattr(harness.codex_sdk_runner, "run_codex_sdk_turn", run_with_refresh)
    harness.test_run_codex_sdk_turn_publishes_marker_from_current_and_legacy_events(
      monkeypatch, kind,
    )
    monkeypatch.setattr(harness.codex_sdk_runner, "run_codex_sdk_turn", original)
    assert not refresh.stale, kind


def test_codex_steer_never_carries_the_goal_brief():
  from app.runner_registry import RunnerKind, registry
  from app.codex_sdk_runner import ActiveCodexTurn
  from tests.test_codex_sdk_runner import _FakeSteerSink, _FakeTurnHandle

  async def scenario(turn):
    refresh = _stale_refresh()
    refresh.mark_compacted()  # Even an outstanding need is the hook's alone.
    sink = _FakeSteerSink()
    active = ActiveCodexTurn(object(), turn, chat_id="codex-refresh", sink=sink,
                             goal_brief_refresh=refresh)
    registry.register(active)
    try:
      assert await active.steer("ping", [{"role": "user", "content": "ping", "cid": "one"}], ["one"])
      while active.steer_in_flight:
        await asyncio.sleep(0)
    finally:
      registry.unregister("codex-refresh", RunnerKind.CODEX_SDK)
    return refresh

  turn = _FakeTurnHandle()
  refresh = asyncio.run(scenario(turn))
  assert turn.steered == ["ping"]
  assert refresh.stale


def test_read_goal_tool_is_one_result_bearing_read_for_both_levels(monkeypatch):
  from app import platform_tools
  from tests.test_platform_tools import _control_module
  control = _control_module()
  assert control.READ_GOAL_TOOL in control.OWNER_TOOLS
  assert control.READ_GOAL_TOOL in control.DELEGATED_TOOLS
  assert platform_tools.READ_GOAL_TOOL_NAME in platform_tools.DELEGATED_CONTROL_TOOL_NAMES
  assert platform_tools.READ_GOAL_TOOL_NAME in platform_tools.OWNER_CONTROL_TOOL_NAMES
  calls = []
  monkeypatch.setenv("CHAT_ID", "chat 1")

  def api(method, path):
    calls.append((method, path))
    return {"role": "helper", "goal": None, "helpers": []}

  monkeypatch.setattr(control, "_agent_api_call", api)
  receipt = control._call_read_goal({"task": "build"})
  assert calls == [("GET", "/api/chats/chat%201/goal-brief?task=build")]
  assert "no assigned Goal" in receipt["goal"]
  control._call_read_goal({})
  assert calls[-1] == ("GET", "/api/chats/chat%201/goal-brief")
  with pytest.raises(ValueError, match="does not take: goal_id"):
    control._call_read_goal({"goal_id": "other"})
  # Paging is gone: every list is whole, bounded by MAX_TASKS.
  with pytest.raises(ValueError, match="does not take: page"):
    control._call_read_goal({"task": "build", "page": 2})
  schema = control._TOOL_DEFINITIONS[control.READ_GOAL_TOOL]["inputSchema"]
  assert set(schema["properties"]) == {"task"}


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_coordinator_turn_carries_brief_and_arms_compaction_refresh(
  client, auth, db, monkeypatch, provider,
):
  from app import chat as chat_mod, schemas
  from app.broadcast import create_broadcast
  chat_id = client.post("/api/chats", json={"title": "Coordinator"}, headers=auth).json()["id"]
  cls = "ClaudeProvider" if provider == "claude" else "CodexProvider"
  monkeypatch.setattr(f"app.providers.{cls}.check_auth", lambda *a: None)
  monkeypatch.setattr(f"app.providers.{cls}.ensure_auth", lambda *a: asyncio.sleep(0))
  captured = {}

  async def runner(**kwargs):
    captured.update(kwargs)
    return {"session_id": "s", "cost_usd": 0.0, "error": None}

  monkeypatch.setattr(f"app.{provider}_sdk_runner.run_{provider}_sdk_turn", runner)
  db.add(goal_run(db, id="coord-run", root_run_id="coord-run", chat_id=chat_id,
                  status="running", provider=provider, provider_execution_admitted=False,
                  goal_id="coord-goal", goal_objective="Coordinate",
                  goal_plan_json={"tasks": [_task("a", status="running")]}))
  db.commit()
  create_broadcast(chat_id)
  asyncio.run(chat_mod._run_chat_impl(
    messages=[schemas.ChatMessage(role="user", content="continue")],
    chat_id=chat_id, session_id=None, provider_id=provider,
    run_token="coord-run", run_gen=chat_mod.current_run_generation(chat_id),
  ))
  assert captured["user_message"].count("<mobius_goal>") == 1
  assert "read_goal" in captured["user_message"]
  assert captured["goal_brief_refresh"] is not None


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("path", ["promotion", "successor", "never_goal"])
def test_goal_less_turn_arms_lazy_refresh_for_mid_turn_promotion(
  client, auth, db, monkeypatch, provider, path,
):
  from app import chat as chat_mod, schemas
  from app.broadcast import create_broadcast
  from app.chat_writer import PromoteRunToGoal, get_writer
  from app.database import SessionLocal
  from app.goals import update_goal_record

  chat_id = client.post("/api/chats", json={"title": "Ordinary turn"}, headers=auth).json()["id"]
  cls = "ClaudeProvider" if provider == "claude" else "CodexProvider"
  monkeypatch.setattr(f"app.providers.{cls}.check_auth", lambda *a: None)
  monkeypatch.setattr(f"app.providers.{cls}.ensure_auth", lambda *a: asyncio.sleep(0))
  restored = []

  async def runner(**kwargs):
    assert "<mobius_goal>" not in kwargs["user_message"]
    refresh = kwargs["goal_brief_refresh"]
    assert refresh is not None
    assert await refresh.take() == ""
    if provider == "claude":
      from tests.test_claude_sdk_runner import _Bus, _install_fake_client, _run_turn
      clients = _install_fake_client(monkeypatch)
      await _run_turn(chat_id, bc=_Bus(), goal_brief_refresh=refresh)
      hooks = clients[0].options.hooks
      brief_hook = next(hook for matcher in hooks["PostToolUse"] for hook in matcher.hooks
                        if hook.__name__ == "goal_brief_after_compaction_hook")

      async def compact():
        await hooks["PreCompact"][0].hooks[0]({"trigger": "auto"}, None, {})
        output = await brief_hook({"tool_name": "Bash"}, "tool", {})
        return output.get("hookSpecificOutput", {}).get("additionalContext", "")
    else:
      from app.codex_sdk_runner import ActiveCodexTurn
      active = ActiveCodexTurn(SimpleNamespace(id="thread"), object(), chat_id=chat_id,
                               goal_brief_refresh=refresh)

      async def compact():
        return await active.goal_brief_after_compaction("thread")

    # Installing either provider boundary with no Goal must add no context.
    assert await compact() == ""
    goal_id = None
    if path != "never_goal":
      with SessionLocal() as session:
        def promote(objective):
          result = get_writer()._promote_run_to_goal(session, PromoteRunToGoal(
            chat_id=chat_id, run_token="ordinary-run", objective=objective,
          ))
          assert result["state"] == "promoted"
          return result["goal_id"]

        goal_id = promote("Goal A after entry")
        if path == "successor":
          run = session.get(models.ChatRun, "ordinary-run")
          goal = session.get(models.ChatGoal, goal_id)
          update_goal_record(session, run, goal, goal.revision, complete=True)
          goal_id = promote("Goal B after A")
    context = await compact()
    if goal_id is None:
      assert context == ""
    else:
      brief = json.loads(context.split("<mobius_goal>")[1].split("</mobius_goal>")[0])
      assert brief["id"] == goal_id
      assert brief["objective"] == ("Goal B after A" if path == "successor" else "Goal A after entry")
      if path == "successor":
        assert "Goal A after entry" not in context
    restored.append(context)
    return {"session_id": "s", "cost_usd": 0.0, "error": None}

  monkeypatch.setattr(f"app.{provider}_sdk_runner.run_{provider}_sdk_turn", runner)
  db.add(models.ChatRun(id="ordinary-run", root_run_id="ordinary-run", chat_id=chat_id,
                       status="running", provider=provider, provider_execution_admitted=False))
  db.commit()
  create_broadcast(chat_id)
  asyncio.run(chat_mod._run_chat_impl(
    messages=[schemas.ChatMessage(role="user", content="Do approved work")],
    chat_id=chat_id, session_id=None, provider_id=provider,
    run_token="ordinary-run", run_gen=chat_mod.current_run_generation(chat_id),
  ))
  assert len(restored) == 1, "provider did not reach the compaction boundary"


@pytest.mark.asyncio
async def test_refresh_load_failure_is_retried_without_a_second_compaction():
  from app.goals import CompactionBriefRefresh
  calls = []
  def load():
    calls.append(1)
    if len(calls) == 1:
      raise RuntimeError('temporary read failure')
    return 'RECOVERED'
  refresh = CompactionBriefRefresh(load)
  refresh.mark_compacted()
  assert await refresh.take() == '' and refresh.stale
  assert (await refresh.take()).endswith('RECOVERED') and not refresh.stale


@pytest.mark.asyncio
async def test_parallel_refreshes_share_one_delivery_and_preserve_new_compaction():
  import threading
  from app.goals import CompactionBriefRefresh
  entered, release = threading.Event(), threading.Event()
  calls = []
  def load():
    calls.append(1)
    if len(calls) == 1:
      entered.set()
      assert release.wait(3)
    return 'CURRENT'
  refresh = CompactionBriefRefresh(load)
  refresh.mark_compacted()
  first = asyncio.create_task(refresh.take())
  try:
    assert await asyncio.to_thread(entered.wait, 3)
    refresh.mark_compacted()
    second = asyncio.create_task(refresh.take())
    release.set()
    results = await asyncio.gather(first, second)
    assert all(result.endswith('CURRENT') for result in results)
    assert len(calls) == 2 and not refresh.stale
    refresh.mark_compacted()
    results = await asyncio.gather(refresh.take(), refresh.take(), refresh.take())
    assert sum(bool(result) for result in results) == 1
    assert len(calls) == 3
  finally:
    release.set()


@pytest.mark.asyncio
async def test_shared_claude_compaction_restores_each_live_helpers_own_brief(tmp_path):
  from tests.test_helper_hosts import _claude_host, _turn
  from app.goals import CompactionBriefRefresh
  host = _claude_host(tmp_path)
  turns = []
  for name in ['a', 'b', 'finished']:
    turn = _turn(tmp_path, dispatch_id=name)
    turn.agent_id = name
    turn.started.set()
    turn.goal_brief_refresh = CompactionBriefRefresh(lambda n=name: f'ONLY_{n}')
    host._turn_by_agent[name] = host._turn_by_dispatch[name] = turn
    turns.append(turn)
  turns[-1].done.set()
  try:
    # Current SDK does not identify which agent compacted; no attribution guess.
    await host.pre_compact({'trigger': 'auto'}, None, {})
    assert not turns[-1].goal_brief_refresh.stale
    assert await host.post_tool_use({'tool_name': 'Read'}, 'dispatcher-tool', {}) == {}
    for name in ['a', 'b']:
      payload = {'tool_name': 'Read', 'agent_id': name}
      result = await host.post_tool_use(payload, f'{name}-read', {})
      assert result['hookSpecificOutput']['additionalContext'].endswith(f'ONLY_{name}')
      assert await host.post_tool_use(payload, f'{name}-read-again', {}) == {}
    assert await host.post_tool_use({'agent_id': 'finished'}, 'old', {}) == {}
    assert await host.post_tool_use({'agent_id': 'unknown'}, 'foreign', {}) == {}
    # If a future supported event carries identity, it must not mark siblings.
    await host.pre_compact({'agent_id': 'a', 'trigger': 'auto'}, None, {})
    assert turns[0].goal_brief_refresh.stale and not turns[1].goal_brief_refresh.stale
  finally:
    for turn in turns:
      turn.env_file.remove()
