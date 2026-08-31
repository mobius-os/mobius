"""Contracts for durable delegated tasks and restrictive child policy."""

import hashlib

from app import models
from app.chat_writer import (
  AppendPending, Barrier, PromotePending, StartTurn, get_writer,
)
from app.codex_sdk_runner import _codex_config_overrides
from app.claude_sdk_runner import _guarded_subagent_bash
from app.delegations import (
  RunPolicy,
  delegation_execution_token,
  derived_status,
  mark_cancelled,
  parent_root_run_id,
  policy_for_chat,
)
from test_app_fixtures import create_local_app


def _parent_with_run(client, owner_token, db):
  auth = {"Authorization": f"Bearer {owner_token}"}
  response = client.post("/api/chats", json={"title": "Parent"}, headers=auth)
  assert response.status_code == 200, response.text
  chat_id = response.json()["id"]
  db.add(models.ChatRun(
    id="parent-physical",
    root_run_id="parent-root",
    chat_id=chat_id,
    status="running",
    provider="codex",
  ))
  db.commit()
  return chat_id


def test_read_delegation_receives_only_a_delegation_scoped_bearer(
  client, owner_token, db,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Read policy")['id']
  db.add_all([
    models.Chat(id="parent", title="Parent", messages=[]),
    models.Chat(id="read-child", title="Child", messages=[], created_by_app_id=app_id),
    models.Chat(id="write-child", title="Child", messages=[], created_by_app_id=app_id),
  ])
  db.add_all([
    models.Delegation(
      id="read-policy", app_id=app_id, parent_chat_id="parent",
      parent_root_run_id="parent-root", task_key="read",
      child_chat_id="read-child", provider="codex", model=None, effort=None,
      scope="read", cwd="/data/platform",
      prompt_sha256=hashlib.sha256(b"read").hexdigest(),
    ),
    models.Delegation(
      id="write-policy", app_id=app_id, parent_chat_id="parent",
      parent_root_run_id="parent-root", task_key="write",
      child_chat_id="write-child", provider="codex", model=None, effort=None,
      scope="write", cwd="/data/platform",
      prompt_sha256=hashlib.sha256(b"write").hexdigest(),
    ),
    models.ChatRun(
      id="read-run", root_run_id="read-run", chat_id="read-child",
      status="running", provider="codex",
    ),
    models.ChatRun(
      id="write-run", root_run_id="write-run", chat_id="write-child",
      status="running", provider="codex",
    ),
  ])
  db.commit()
  base = dict(
    delegation_id="read-policy",
    app_id=app_id,
    provider="codex",
    model=None,
    effort=None,
    cwd="/data/platform",
  )

  read_token = delegation_execution_token(
    db, RunPolicy(scope="read", **base), "read-run",
  )
  write_token = delegation_execution_token(
    db, RunPolicy(
      scope="write", **{**base, "delegation_id": "write-policy"},
    ),
    "write-run",
  )
  assert read_token and write_token
  active_write = client.get(
    "/api/delegations", headers={"Authorization": f"Bearer {write_token}"},
  )
  assert active_write.status_code == 200

  db.get(models.ChatRun, "read-run").status = "completed"
  db.commit()
  stale = client.get(
    "/api/delegations/capabilities",
    headers={"Authorization": f"Bearer {read_token}"},
  )
  assert stale.status_code == 401
  db.get(models.ChatRun, "write-run").status = "completed"
  db.commit()
  stale_write = client.get(
    "/api/delegations", headers={"Authorization": f"Bearer {write_token}"},
  )
  assert stale_write.status_code == 401


def test_submit_is_idempotent_per_parent_root_and_task_key(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Subagents")['id']
  parent_chat_id = _parent_with_run(client, owner_token, db)
  starts = []

  async def fake_start(**kwargs):
    starts.append(kwargs)
    return True

  monkeypatch.setattr(
    "app.routes.delegations.start_programmatic_chat_turn", fake_start,
  )
  body = {
    "app_id": app_id,
    "parent_chat_id": parent_chat_id,
    "task_key": "audit-restart",
    "prompt": "Audit restart recovery.",
    "provider": "codex",
    "scope": "read",
    "cwd": "/data/platform",
  }

  first = client.post("/api/delegations", json=body, headers=auth)
  assert first.status_code == 201, first.text
  assert first.json()["attached"] is False
  assert "max_budget_usd" not in first.json()
  assert first.json()["parent_root_run_id"] == "parent-root"
  assert first.json()["status"] == "starting"

  second = client.post("/api/delegations", json=body, headers=auth)
  assert second.status_code == 201, second.text
  assert second.json()["attached"] is True
  assert second.json()["id"] == first.json()["id"]
  # With no real StartTurn in this isolated test, attachment safely re-enters
  # the same child claim rather than creating another control/chat row.
  assert len(starts) == 2
  assert db.query(models.Delegation).count() == 1

  conflict = client.post(
    "/api/delegations",
    json={**body, "prompt": "Different work."},
    headers=auth,
  )
  assert conflict.status_code == 409

  wake_policy_conflict = client.post(
    "/api/delegations",
    json={**body, "notify_parent_on_complete": False},
    headers=auth,
  )
  assert wake_policy_conflict.status_code == 409


def test_goal_identity_is_the_delegation_idempotency_parent(db, chat):
  db.add(models.ChatRun(
    id="goal-physical", root_run_id="logical-before-restart",
    chat_id=chat.id, status="running", provider="codex",
    goal_objective="Ship", goal_id="stable-goal",
  ))
  db.commit()
  assert parent_root_run_id(db, chat.id, require_active=True) == "stable-goal"


def test_app_token_can_only_submit_bounded_work_under_its_own_child(
  client, owner_token, db, monkeypatch,
):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, owner_auth, name="Subagents")['id']
  app_token = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=owner_auth,
  ).json()["token"]
  app_auth = {"Authorization": f"Bearer {app_token}"}
  parent_chat_id = _parent_with_run(client, owner_token, db)

  async def fake_start(**_kwargs):
    return True

  monkeypatch.setattr(
    "app.routes.delegations.start_programmatic_chat_turn", fake_start,
  )
  body = {
    "app_id": app_id,
    "parent_chat_id": parent_chat_id,
    "task_key": "bounded-review",
    "prompt": "Review only.",
    "provider": "claude",
    "scope": "read",
    "cwd": "/data",
  }
  created = client.post("/api/delegations", json=body, headers=owner_auth)
  assert created.status_code == 201, created.text

  listing = client.get("/api/delegations", headers=app_auth)
  assert listing.status_code == 200, listing.text
  assert [row["id"] for row in listing.json()["items"]] == [created.json()["id"]]

  rejected = client.post("/api/delegations", json=body, headers=app_auth)
  assert rejected.status_code == 403

  child_id = created.json()["child_chat_id"]
  db.add(models.ChatRun(
    id="child-parent-run", root_run_id="child-parent-run",
    chat_id=child_id, status="running", provider="claude",
  ))
  db.commit()
  child_policy = policy_for_chat(db, child_id)
  assert child_policy is not None and child_policy.depth == 1
  plain_app_nested = client.post("/api/delegations", json={
    **body,
    "parent_chat_id": child_id,
    "task_key": "app-frame-cannot-spend",
    "prompt": "Try to spend from an app frame.",
  }, headers=app_auth)
  assert plain_app_nested.status_code == 403
  child_auth = {
    "Authorization": (
      f"Bearer {delegation_execution_token(db, child_policy, 'child-parent-run')}"
    )
  }
  parent_delegation = db.get(models.Delegation, created.json()["id"])
  parent_delegation.provider = "codex"
  db.commit()
  codex_nested = client.post("/api/delegations", json={
    **body,
    "parent_chat_id": child_id,
    "task_key": "codex-needs-local-bridge",
    "prompt": "Try to create a nested child without the local bridge.",
  }, headers=child_auth)
  assert codex_nested.status_code == 409
  assert "narrow local bridge" in codex_nested.json()["detail"]
  parent_delegation.provider = "claude"
  db.commit()
  other_app_id = create_local_app(client, owner_auth, name="Other delegates")['id']
  foreign_nested = client.post("/api/delegations", json={
    **body,
    "app_id": other_app_id,
    "parent_chat_id": child_id,
    "task_key": "other-app-child",
    "prompt": "Owner-authorized work from another app.",
  }, headers=owner_auth)
  assert foreign_nested.status_code == 201, foreign_nested.text
  child_listing = client.get("/api/delegations", headers=child_auth)
  assert child_listing.status_code == 200, child_listing.text
  assert child_listing.json()["items"] == []
  async def fake_models(_data_dir):
    return {
      "claude": [{"id": "claude-sonnet-4-6", "label": "Sonnet"}],
      "codex": [{"id": "gpt-5.6-sol", "label": "Sol"}],
    }
  monkeypatch.setattr(
    "app.routes.delegations.providers.list_models", fake_models,
  )
  capabilities = client.get(
    "/api/delegations/capabilities", headers=child_auth,
  )
  assert capabilities.status_code == 200, capabilities.text
  assert capabilities.json()["app_id"] == app_id
  assert capabilities.json()["models"]["codex"][0]["id"] == "gpt-5.6-sol"
  nested = client.post("/api/delegations", json={
    **body,
    "parent_chat_id": child_id,
    "task_key": "nested-check",
    "prompt": "Check one bounded detail.",
  }, headers=child_auth)
  assert nested.status_code == 201, nested.text
  nested_policy = policy_for_chat(db, nested.json()["child_chat_id"])
  assert nested_policy is not None and nested_policy.depth == 2
  escalated = client.post("/api/delegations", json={
    **body,
    "parent_chat_id": child_id,
    "task_key": "nested-write",
    "prompt": "Try to write.",
    "scope": "write",
  }, headers=child_auth)
  assert escalated.status_code == 403
  assert "read-only" in escalated.json()["detail"]

  # Ownership may continue through as many useful local levels as the work
  # needs. Every bearer still owns only its direct children, and a read-only
  # owner still cannot create a write-capable descendant.
  nested_parent = nested.json()["child_chat_id"]
  for depth in (3, 4):
    nested_run_id = f"depth-{depth}-parent-run"
    db.add(models.ChatRun(
      id=nested_run_id,
      root_run_id=nested_run_id,
      chat_id=nested_parent,
      status="running",
      provider="claude",
    ))
    db.commit()
    nested_parent_policy = policy_for_chat(db, nested_parent)
    assert nested_parent_policy is not None
    nested_auth = {
      "Authorization": (
        f"Bearer {delegation_execution_token(db, nested_parent_policy, nested_run_id)}"
      )
    }
    deeper = client.post("/api/delegations", json={
      **body,
      "parent_chat_id": nested_parent,
      "task_key": f"depth-{depth}",
      "prompt": f"Check depth {depth}.",
    }, headers=nested_auth)
    assert deeper.status_code == 201, deeper.text
    nested_parent = deeper.json()["child_chat_id"]

  db.add(models.ChatRun(
    id="depth-5-parent-run",
    root_run_id="depth-5-parent-run",
    chat_id=nested_parent,
    status="running",
    provider="claude",
  ))
  db.commit()
  fifth_parent_policy = policy_for_chat(db, nested_parent)
  assert fifth_parent_policy is not None and fifth_parent_policy.depth == 4
  fifth_parent_auth = {
    "Authorization": (
      "Bearer "
      f"{delegation_execution_token(db, fifth_parent_policy, 'depth-5-parent-run')}"
    )
  }
  fifth = client.post("/api/delegations", json={
    **body,
    "parent_chat_id": nested_parent,
    "task_key": "depth-5",
    "prompt": "Continue one more bounded level.",
  }, headers=fifth_parent_auth)
  assert fifth.status_code == 201, fifth.text
  fifth_policy = policy_for_chat(db, fifth.json()["child_chat_id"])
  assert fifth_policy is not None and fifth_policy.depth == 5


def test_delegation_listing_exposes_run_usage_without_loading_result(
  client, owner_token, db, monkeypatch,
):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, owner_auth, name="Subagents")['id']
  app_token = client.post(
    "/api/auth/app-token", json={"app_id": app_id}, headers=owner_auth,
  ).json()["token"]
  parent_chat_id = _parent_with_run(client, owner_token, db)

  async def fake_start(**_kwargs):
    return True

  monkeypatch.setattr(
    "app.routes.delegations.start_programmatic_chat_turn", fake_start,
  )
  created = client.post("/api/delegations", json={
    "app_id": app_id,
    "parent_chat_id": parent_chat_id,
    "task_key": "usage-visible",
    "prompt": "Review only.",
    "provider": "codex",
    "scope": "read",
  }, headers=owner_auth).json()
  db.add(models.ChatRun(
    id="usage-run", root_run_id="usage-run",
    chat_id=created["child_chat_id"], status="completed", provider="codex",
    input_tokens=1200, output_tokens=300, cache_read_input_tokens=800,
    reasoning_output_tokens=75, total_tokens=1575, cost_usd=0.42,
  ))
  db.commit()

  listing = client.get(
    "/api/delegations", headers={"Authorization": f"Bearer {app_token}"},
  )
  assert listing.status_code == 200, listing.text
  row = listing.json()["items"][0]
  assert row["result"] == ""
  assert row["usage"] == {
    "input_tokens": 1200,
    "output_tokens": 300,
    "cache_read_input_tokens": 800,
    "cache_creation_input_tokens": None,
    "reasoning_output_tokens": 75,
    "total_tokens": 1575,
    "cost_usd": 0.42,
  }


def test_child_policy_is_integrity_checked_and_write_loss_needs_review(db):
  app = models.App(
    slug="test-delegations-116",
    source_dir="/tmp/mobius-tests/test-delegations-116",
    name="Subagents", description="", jsx_source="",
  )
  db.add(app)
  db.flush()
  parent = models.Chat(id="parent", title="Parent", messages=[])
  child = models.Chat(
    id="child", title="Child",
    messages=[{"role": "user", "content": "Make the bounded edit."}],
    provider="claude",
    created_by_app_id=app.id,
  )
  db.add_all((parent, child))
  db.flush()
  row = models.Delegation(
    id="delegation",
    app_id=app.id,
    parent_chat_id=parent.id,
    parent_root_run_id="parent-root",
    task_key="bounded-edit",
    child_chat_id=child.id,
    provider="claude",
    model="claude-sonnet-4-6",
    effort="high",
    scope="write",
    cwd="/data/platform",
    prompt_sha256=hashlib.sha256(
      b"Make the bounded edit."
    ).hexdigest(),
  )
  db.add(row)
  db.commit()

  policy = policy_for_chat(db, child.id)
  assert policy is not None
  assert policy.allow_session_reseed is False
  assert "$MOBIUS_SUBAGENT_HELPER" in policy.system_prompt
  assert "Do not use any other agent CLI" in policy.system_prompt

  codex_policy = RunPolicy(
    delegation_id="codex-child", app_id=app.id, provider="codex",
    model=None, effort=None, scope="read", cwd="/data/platform",
  )
  assert (
    "Nested delegated work is not available" in codex_policy.system_prompt
  )
  assert "$MOBIUS_SUBAGENT_HELPER" not in codex_policy.system_prompt

  child.messages = [
    {"role": "user", "content": "Make the bounded edit."},
    {
      "role": "assistant",
      "blocks": [{
        "type": "error",
        "message": (
          "DELEGATION_WRITE_REVIEW_REQUIRED: Review before replaying."
        ),
      }],
    },
  ]
  db.add(models.ChatRun(
    id="child-run", root_run_id="child-run", chat_id=child.id,
    status="failed", provider="claude",
  ))
  db.commit()

  status, _, result = derived_status(db, row)
  assert status == "needs_review"
  assert result == "Review before replaying."

  mark_cancelled(db, row)
  mark_cancelled(db, row)
  db.expire_all()
  assert db.get(models.Chat, child.id).auto_resume_on_restart is False
  assert db.get(models.Chat, child.id).auto_resume_on_limit is False
  lifecycle = db.query(models.AgentLifecycleEvent).filter(
    models.AgentLifecycleEvent.provider_agent_id == row.id,
    models.AgentLifecycleEvent.source_event_id
      == f"delegation:{row.id}:terminal:cancelled",
  ).all()
  assert len(lifecycle) == 1
  assert lifecycle[0].event_type == "agent_terminal"
  assert lifecycle[0].state == "stopped"


def test_continuation_physical_runs_inherit_one_logical_root(db):
  chat = models.Chat(id="rooted-chat", title="Rooted", messages=[])
  db.add(chat)
  db.commit()

  get_writer().submit(StartTurn(
    chat_id=chat.id,
    run_token="physical-1",
    user_msg={"role": "user", "content": "Start", "ts": 1},
    title_source="Start",
    default_provider="codex",
  )).result(timeout=5)
  get_writer().submit(AppendPending(
    chat_id=chat.id,
    run_token="",
    user_msg={
      "role": "user", "content": "continue", "ts": 2,
      "kind": "continuation", "continuation_reason": "restart",
    },
  )).result(timeout=5)
  get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="physical-2",
  )).result(timeout=5)
  get_writer().submit(Barrier()).result(timeout=5)

  db.expire_all()
  first = db.get(models.ChatRun, "physical-1")
  second = db.get(models.ChatRun, "physical-2")
  assert first.root_run_id == "physical-1"
  assert second.root_run_id == "physical-1"

  get_writer().submit(AppendPending(
    chat_id=chat.id,
    run_token="",
    user_msg={"role": "user", "content": "New work", "ts": 3},
  )).result(timeout=5)
  get_writer().submit(PromotePending(
    chat_id=chat.id, run_token="physical-3",
  )).result(timeout=5)
  get_writer().submit(Barrier()).result(timeout=5)
  db.expire_all()
  third = db.get(models.ChatRun, "physical-3")
  assert third.root_run_id == "physical-3"


def test_delegated_codex_config_has_no_questions_or_nested_agents():
  overrides = _codex_config_overrides(
    allow_questions=False, allow_multi_agent=False, allow_goals=False,
  )
  assert "features.default_mode_request_user_input=true" not in overrides
  assert not any("multi_agent" in item for item in overrides)
  assert "features.goals=true" not in overrides


def test_read_only_claude_admits_only_the_guarded_read_child_command():
  admitted = _guarded_subagent_bash({"command": (
    "python3 /data/apps/subagents/subagents.py run --provider codex "
    "--name audit-x --scope read --prompt 'Check migration X.\nReport evidence.'"
  )})
  assert admitted
  assert "'Check migration X.\nReport evidence.'" in admitted["command"]
  assert not _guarded_subagent_bash({"command": (
    "python3 /data/apps/subagents/subagents.py run --provider codex "
    "--name audit-x --scope write --prompt 'Change X.'"
  )})
  assert not _guarded_subagent_bash({"command": (
    "python3 /data/apps/subagents/subagents.py run --provider codex "
    "--name audit-x --scope read --explicit --prompt 'Bypass a paused provider.'"
  )})
  assert not _guarded_subagent_bash({"command": (
    "python3 /data/apps/subagents/subagents.py run --provider codex "
    "--name audit-x --scope read --prompt 'Check X.'; rm -rf /data"
  )})
  substitution = _guarded_subagent_bash({"command": (
    "python3 /data/apps/subagents/subagents.py run --provider codex "
    '--name audit-x --scope read --prompt "Report $(touch /data/pwned)."'
  )})
  assert substitution
  assert "'Report $(touch /data/pwned).'" in substitution["command"]


def test_delegated_capability_read_refuses_symlinked_storage(tmp_path):
  from app.routes.delegations import _read_delegation_storage_json

  storage = tmp_path / "apps" / "7"
  storage.mkdir(parents=True)
  victim = tmp_path / "victim.json"
  victim.write_text('{"secret":"must-not-follow"}', encoding="utf-8")
  (storage / "config.json").symlink_to(victim)

  assert _read_delegation_storage_json(
    str(tmp_path), 7, "config.json",
  ) == {}


# --- Parent auto-wake on child completion ------------------------------------

import asyncio
import threading

import app.chat as chat_mod
import app.chat_start as chat_start_mod
import app.delegations as delegations_mod
from app.chat_writer import PromotePending
from app.timeutil import now_naive_utc


def _seed_delegation(
  db,
  *,
  suffix,
  parent_id=None,
  child_status="completed",
  result_blocks=None,
  notify=True,
  cancelled=False,
  parent_messages=None,
  parent_pending_question_id=None,
  parent_root_id=None,
):
  """Create a parent chat, a child chat (+ its ChatRun at child_status), and a
  Delegation row. Returns (parent_id, child_id, delegation_id)."""
  app = models.App(
    slug=f"wake-app-{suffix}",
    source_dir=f"/tmp/mobius-tests/wake-app-{suffix}",
    name="Subagents", description="", jsx_source="",
  )
  db.add(app)
  db.flush()
  if parent_id is None:
    parent_id = f"parent-{suffix}"
    db.add(models.Chat(
      id=parent_id, title="Parent",
      messages=parent_messages or [], provider="claude",
      pending_question_id=parent_pending_question_id,
    ))
  child_id = f"child-{suffix}"
  messages = [{"role": "user", "content": "Do the bounded task."}]
  if result_blocks is not None:
    messages.append({"role": "assistant", "blocks": result_blocks})
  db.add(models.Chat(
    id=child_id, title="Child", messages=messages,
    provider="claude", created_by_app_id=app.id,
  ))
  db.flush()
  delegation_id = f"delegation-{suffix}"
  db.add(models.Delegation(
    id=delegation_id,
    app_id=app.id,
    parent_chat_id=parent_id,
    parent_root_run_id=parent_root_id or f"root-{suffix}",
    task_key=f"task-{suffix}",
    child_chat_id=child_id,
    provider="claude",
    model="claude-sonnet-4-6",
    effort="high",
    scope="read",
    cwd="/data/platform",
    prompt_sha256=hashlib.sha256(b"Do the bounded task.").hexdigest(),
    notify_parent_on_complete=notify,
    parent_woken_at=None,
    cancelled_at=now_naive_utc() if cancelled else None,
  ))
  if child_status is not None:
    db.add(models.ChatRun(
      id=f"child-run-{suffix}", root_run_id=f"child-run-{suffix}",
      chat_id=child_id, status=child_status, provider="claude",
      started_at=now_naive_utc(),
    ))
  db.commit()
  return parent_id, child_id, delegation_id


def _capture_starts(monkeypatch, *, running=False):
  """Stub start_programmatic_chat_turn + is_chat_running; return the start log."""
  starts = []

  async def fake_start(**kwargs):
    starts.append(kwargs)
    return True

  monkeypatch.setattr(chat_start_mod, "start_programmatic_chat_turn", fake_start)
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _cid: running)
  return starts


def test_direct_send_to_delegation_child_is_rejected(
  client, owner_token, db,
):
  _, child_id, _ = _seed_delegation(db, suffix="send-gate")

  response = client.post(
    f"/api/chats/{child_id}/messages",
    json={"content": "Bypass the parent workflow."},
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "delegation_managed"


def test_child_completion_wakes_idle_parent_once(db, monkeypatch):
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="idle",
    result_blocks=[{"type": "text", "content": "All 3 checks passed."}],
  )
  starts = _capture_starts(monkeypatch, running=False)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert len(starts) == 1
  assert starts[0]["chat_id"] == parent_id
  assert "task-idle" in starts[0]["content"]
  assert "All 3 checks passed." in starts[0]["content"]
  assert starts[0]["initiated_by_app_id"] is None
  assert starts[0]["hidden"] is True
  assert starts[0]["message_kind"] == "delegation_result"
  assert starts[0]["source_work_id"] == "root-idle"
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at is not None

  # Second settle is a no-op — the latch holds.
  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))
  assert len(starts) == 1


def test_two_finishes_coalesce_into_one_wake(db, monkeypatch):
  parent_id, child_a, del_a = _seed_delegation(
    db, suffix="coa", parent_root_id="shared-root",
    result_blocks=[{"type": "text", "content": "A done"}],
  )
  # Second delegation shares the same parent chat.
  _, child_b, del_b = _seed_delegation(
    db, suffix="cob", parent_id=parent_id,
    parent_root_id="shared-root",
    result_blocks=[{"type": "text", "content": "B done"}],
  )
  starts = _capture_starts(monkeypatch, running=False)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_a))

  assert len(starts) == 1
  content = starts[0]["content"]
  assert "task-coa" in content and "task-cob" in content
  db.expire_all()
  assert db.get(models.Delegation, del_a).parent_woken_at is not None
  assert db.get(models.Delegation, del_b).parent_woken_at is not None

  # The other child settling now finds nothing eligible → no second wake.
  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_b))
  assert len(starts) == 1


def test_parent_wake_bounds_one_notice_and_retries_the_remainder(
  db, monkeypatch,
):
  parent_id, child_a, del_a = _seed_delegation(
    db, suffix="batch-a", parent_root_id="batch-root",
    result_blocks=[{"type": "text", "content": "A done"}],
  )
  _, child_b, del_b = _seed_delegation(
    db, suffix="batch-b", parent_id=parent_id,
    parent_root_id="batch-root",
    result_blocks=[{"type": "text", "content": "B done"}],
  )
  _, child_c, del_c = _seed_delegation(
    db, suffix="batch-c", parent_id=parent_id,
    parent_root_id="batch-root",
    result_blocks=[{"type": "text", "content": "C done"}],
  )
  starts = _capture_starts(monkeypatch, running=False)
  monkeypatch.setattr(delegations_mod, "WAKE_NOTICE_DELEGATION_LIMIT", 2)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_a))

  assert len(starts) == 1
  first_notice = starts[0]["content"]
  assert sum(
    key in first_notice for key in ("task-batch-a", "task-batch-b", "task-batch-c")
  ) == 2
  db.expire_all()
  latched = {
    row.id for row in db.query(models.Delegation).filter(
      models.Delegation.id.in_((del_a, del_b, del_c)),
      models.Delegation.parent_woken_at.isnot(None),
    )
  }
  assert len(latched) == 2

  remaining = next(
    child for delegation, child in (
      (del_a, child_a), (del_b, child_b), (del_c, child_c),
    ) if delegation not in latched
  )
  asyncio.run(delegations_mod.wake_parent_after_child_settled(remaining))

  assert len(starts) == 2
  db.expire_all()
  assert all(
    db.get(models.Delegation, delegation_id).parent_woken_at is not None
    for delegation_id in (del_a, del_b, del_c)
  )


def test_finishes_from_different_logical_work_never_share_a_wake(
  db, monkeypatch,
):
  parent_id, child_a, del_a = _seed_delegation(
    db, suffix="goal-a", parent_root_id="goal-a",
    result_blocks=[{"type": "text", "content": "A done"}],
  )
  _, child_b, del_b = _seed_delegation(
    db, suffix="goal-b", parent_id=parent_id, parent_root_id="goal-b",
    result_blocks=[{"type": "text", "content": "B done"}],
  )
  starts = _capture_starts(monkeypatch, running=False)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_a))

  assert len(starts) == 1
  assert "task-goal-a" in starts[0]["content"]
  assert "task-goal-b" not in starts[0]["content"]
  assert starts[0]["source_work_id"] == "goal-a"
  db.expire_all()
  assert db.get(models.Delegation, del_a).parent_woken_at is not None
  assert db.get(models.Delegation, del_b).parent_woken_at is None

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_b))

  assert len(starts) == 2
  assert "task-goal-b" in starts[1]["content"]
  assert starts[1]["source_work_id"] == "goal-b"


def test_nested_owner_keeps_direct_child_roster_across_physical_turns(db):
  _root, child_owner, owner_id = _seed_delegation(
    db, suffix="owner-roster", child_status="interrupted",
  )
  _same_parent, _leaf, _leaf_id = _seed_delegation(
    db, suffix="leaf-roster", parent_id=child_owner,
    parent_root_id=owner_id, child_status="running",
  )
  db.add(models.ChatRun(
    id="owner-recovery", root_run_id="owner-recovery",
    chat_id=child_owner, status="running", provider="claude",
    started_at=now_naive_utc(),
  ))
  db.commit()

  assert delegations_mod.parent_root_run_id(
    db, child_owner, physical_run_id="owner-recovery",
  ) == owner_id
  context = delegations_mod.active_parent_context(
    db, child_owner, "owner-recovery",
  )
  assert "task-leaf-roster" in context
  assert '"status":"running"' in context


def test_running_parent_gets_appended_not_started(db, monkeypatch):
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="run",
    result_blocks=[{"type": "text", "content": "Result while busy."}],
  )
  starts = _capture_starts(monkeypatch, running=True)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert starts == []  # never starts a competing turn
  db.expire_all()
  parent = db.get(models.Chat, parent_id)
  pending = parent.pending_messages or []
  wake = next(
    m for m in pending if "task-run" in (m.get("content") or "")
  )
  assert wake["hidden"] is True
  assert wake["kind"] == "delegation_result"
  assert wake["source_work_id"] == "root-run"
  assert db.get(models.Delegation, delegation_id).parent_woken_at is not None

  # The queued notice survives the parent's own drain into a continuation.
  get_writer().submit(PromotePending(
    chat_id=parent_id, run_token="parent-next",
  )).result(timeout=5)
  get_writer().submit(Barrier()).result(timeout=5)
  db.expire_all()
  parent = db.get(models.Chat, parent_id)
  promoted = " ".join(
    (m.get("content") or "") for m in (parent.messages or [])
  )
  assert "task-run" in promoted
  promoted_wake = next(
    m for m in (parent.messages or [])
    if "task-run" in (m.get("content") or "")
  )
  assert promoted_wake["hidden"] is True
  assert promoted_wake["kind"] == "delegation_result"


def test_question_blocked_parent_gets_appended_not_started(db, monkeypatch):
  """The actor rejects the start and delegation falls back to the queue."""
  parent_id, child_id, delegation_id = _seed_delegation(
    db,
    suffix="question",
    result_blocks=[{"type": "text", "content": "Result while blocked."}],
    parent_pending_question_id="owner-decision",
  )
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _cid: False)
  starts = []
  queued = []

  async def blocked_start(**kwargs):
    starts.append(kwargs)
    return False

  async def append_pending(content, chat_id, source_work_id=None):
    queued.append((content, chat_id, source_work_id))
    return True

  monkeypatch.setattr(chat_start_mod, "start_programmatic_chat_turn", blocked_start)
  monkeypatch.setattr(delegations_mod, "_append_wake_pending", append_pending)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert [start["chat_id"] for start in starts] == [parent_id]
  assert len(queued) == 1
  assert queued[0][1] == parent_id
  assert "task-question" in queued[0][0]
  assert queued[0][2] == "root-question"
  db.expire_all()
  parent = db.get(models.Chat, parent_id)
  assert parent.pending_question_id == "owner-decision"
  assert db.get(models.Delegation, delegation_id).parent_woken_at is not None


def test_stopped_and_cancelled_children_do_not_wake(db, monkeypatch):
  _, stopped_child, stopped_id = _seed_delegation(
    db, suffix="stop", child_status="stopped",
  )
  _, cancelled_child, cancelled_id = _seed_delegation(
    db, suffix="canc", child_status="completed", cancelled=True,
  )
  starts = _capture_starts(monkeypatch, running=False)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(stopped_child))
  asyncio.run(delegations_mod.wake_parent_after_child_settled(cancelled_child))

  assert starts == []
  db.expire_all()
  assert db.get(models.Delegation, stopped_id).parent_woken_at is None
  assert db.get(models.Delegation, cancelled_id).parent_woken_at is None


def test_cancelling_an_owner_settles_descendants_before_the_parent(db):
  _parent, child_owner, owner_id = _seed_delegation(
    db, suffix="cancel-owner", child_status="running",
  )
  _same_parent, child_leaf, leaf_id = _seed_delegation(
    db,
    suffix="cancel-leaf",
    parent_id=child_owner,
    child_status="running",
  )

  assert asyncio.run(
    delegations_mod.cancel_delegation_execution(owner_id)
  ) is True

  db.expire_all()
  owner = db.get(models.Delegation, owner_id)
  leaf = db.get(models.Delegation, leaf_id)
  assert owner.cancelled_at is not None
  assert leaf.cancelled_at is not None
  owner_run = db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == child_owner,
  ).first()
  leaf_run = db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == child_leaf,
  ).first()
  assert owner_run.status == "stopped"
  assert leaf_run.status == "stopped"
  assert leaf.cancelled_at <= owner.cancelled_at


def test_chat_delete_settles_owned_delegations(client, owner_token, db):
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="delete-chat", child_status="running",
  )

  response = client.delete(
    f"/api/chats/{parent_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 204, response.text
  db.expire_all()
  assert db.get(models.Chat, parent_id).deleted_at is not None
  assert db.get(models.Delegation, delegation_id).cancelled_at is not None
  run = db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == child_id,
  ).first()
  assert run.status == "stopped"


def test_app_delete_settles_owned_delegations(client, owner_token, db):
  _parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="delete-app", child_status="running",
  )
  app_id = db.get(models.Delegation, delegation_id).app_id

  response = client.delete(
    f"/api/apps/{app_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 204, response.text
  db.expire_all()
  assert db.get(models.App, app_id).deleted_at is not None
  assert db.get(models.Delegation, delegation_id).cancelled_at is not None
  run = db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == child_id,
  ).first()
  assert run.status == "stopped"


def test_child_chat_delete_settles_its_own_delegation(client, owner_token, db):
  # Deleting the delegated CHILD chat runs under get_transition_lock(child_id).
  # active_delegation_ids_for_chat returns the delegation whose child IS this
  # chat, so the cancellation path must settle it WITHOUT re-acquiring that same
  # non-reentrant lock — otherwise the delete deadlocks and wedges the chat.
  _parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="delete-child", child_status="running",
  )

  response = client.delete(
    f"/api/chats/{child_id}",
    headers={"Authorization": f"Bearer {owner_token}"},
  )

  assert response.status_code == 204, response.text
  db.expire_all()
  assert db.get(models.Chat, child_id).deleted_at is not None
  assert db.get(models.Delegation, delegation_id).cancelled_at is not None
  run = db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == child_id,
  ).first()
  assert run.status == "stopped"


def test_interrupted_child_does_not_wake_it_resumes(db, monkeypatch):
  _, child_id, delegation_id = _seed_delegation(
    db, suffix="intr", child_status="interrupted",
  )
  starts = _capture_starts(monkeypatch, running=False)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert starts == []
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None


def test_needs_review_is_reported(db, monkeypatch):
  _, child_id, _ = _seed_delegation(
    db, suffix="rev", child_status="failed",
    result_blocks=[{
      "type": "error",
      "message": "DELEGATION_WRITE_REVIEW_REQUIRED: Look before replay.",
    }],
  )
  starts = _capture_starts(monkeypatch, running=False)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert len(starts) == 1
  content = starts[0]["content"]
  assert "needs_review" in content
  assert "Look before replay." in content
  assert "DELEGATION_WRITE_REVIEW_REQUIRED" not in content


def test_notify_flag_false_never_wakes(db, monkeypatch):
  _, child_id, delegation_id = _seed_delegation(
    db, suffix="off", notify=False,
    result_blocks=[{"type": "text", "content": "done"}],
  )
  starts = _capture_starts(monkeypatch, running=False)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert starts == []
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None


def test_reconcile_wakes_parent_for_completed_while_away(db, monkeypatch):
  _, _child, delegation_id = _seed_delegation(
    db, suffix="away",
    result_blocks=[{"type": "text", "content": "Finished during downtime."}],
  )
  starts = _capture_starts(monkeypatch, running=False)

  woken = asyncio.run(
    delegations_mod.wake_parents_for_completed_delegations()
  )
  assert woken.woken_parents == 1
  assert woken.attempted_groups == 1
  assert len(starts) == 1
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at is not None

  # Idempotent: a second boot pass wakes nobody.
  woken_again = asyncio.run(
    delegations_mod.wake_parents_for_completed_delegations()
  )
  assert woken_again.woken_parents == 0
  assert woken_again.attempted_groups == 0
  assert len(starts) == 1


def test_recovery_selection_runs_off_the_event_loop(db, monkeypatch):
  # The correlated GROUP BY selection scan must execute in a worker thread, not
  # on the server event loop, exactly as autopilot_lease_recovery_loop offloads
  # its sibling sweep. Reverting the asyncio.to_thread offload makes this the
  # only failing test in the file.
  _seed_delegation(db, suffix="offloaded", child_status="completed")

  real_select = delegations_mod._wake_recovery_groups
  select_thread: dict[str, int] = {}

  def probe(*args, **kwargs):
    select_thread["ident"] = threading.get_ident()
    return real_select(*args, **kwargs)

  monkeypatch.setattr(delegations_mod, "_wake_recovery_groups", probe)

  async def capture_delivery(parent_chat_id, source_work_id):
    return False

  monkeypatch.setattr(
    delegations_mod, "_deliver_parent_wake", capture_delivery,
  )

  async def drive():
    loop_thread = threading.get_ident()
    result = await delegations_mod.wake_parents_for_completed_delegations()
    return loop_thread, result

  loop_thread, result = asyncio.run(drive())
  assert result.attempted_groups == 1
  assert "ident" in select_thread
  assert select_thread["ident"] != loop_thread


def test_recovery_scan_is_bounded_fair_and_status_only(db, monkeypatch):
  for index in range(20):
    _seed_delegation(
      db, suffix=f"ineligible-{index}", child_status="stopped",
    )
  expected = set()
  for index in range(5):
    _parent, _child, delegation_id = _seed_delegation(
      db, suffix=f"eligible-{index}", child_status="completed",
    )
    expected.add(delegation_id)

  attempts = []

  async def capture_delivery(parent_chat_id, source_work_id):
    attempts.append((parent_chat_id, source_work_id))
    return False

  def transcript_probe(*_args, **_kwargs):
    raise AssertionError("recovery selection must not load child transcripts")

  monkeypatch.setattr(
    delegations_mod, "_deliver_parent_wake", capture_delivery,
  )
  monkeypatch.setattr(delegations_mod, "derived_status", transcript_probe)

  first = asyncio.run(
    delegations_mod.wake_parents_for_completed_delegations(batch_size=2)
  )
  second = asyncio.run(
    delegations_mod.wake_parents_for_completed_delegations(
      after=first.next_cursor, batch_size=2,
    )
  )
  third = asyncio.run(
    delegations_mod.wake_parents_for_completed_delegations(
      after=second.next_cursor, batch_size=2,
    )
  )

  assert [first.attempted_groups, second.attempted_groups, third.attempted_groups] == [
    2, 2, 1,
  ]
  assert first.next_cursor is not None
  assert second.next_cursor is not None
  assert third.next_cursor is None
  assert len(attempts) == 5
  assert {source_work_id for _parent, source_work_id in attempts} == {
    f"root-eligible-{index}" for index in range(5)
  }

  # Failed delivery did not latch or trap the cursor at the first page: every
  # eligible group was attempted once, while stopped children were never read.
  db.expire_all()
  assert {
    row.id for row in db.query(models.Delegation).filter(
      models.Delegation.parent_woken_at.is_(None),
      models.Delegation.id.in_(expected),
    )
  } == expected


def test_recovery_times_out_one_parent_without_starving_the_next(
  db, monkeypatch,
):
  for index in range(2):
    _seed_delegation(
      db, suffix=f"timeout-{index}", child_status="completed",
    )
  attempts = []
  blocked = asyncio.Event()

  async def deliver_once(parent_chat_id, source_work_id):
    attempts.append((parent_chat_id, source_work_id))
    if source_work_id == "root-timeout-0":
      await blocked.wait()
    return True

  monkeypatch.setattr(
    delegations_mod, "_deliver_parent_wake_once", deliver_once,
  )
  monkeypatch.setattr(
    delegations_mod, "WAKE_PARENT_DELIVERY_TIMEOUT_SECS", 0.01,
  )

  result = asyncio.run(
    delegations_mod.wake_parents_for_completed_delegations(batch_size=2)
  )

  assert result.attempted_groups == 2
  assert result.woken_parents == 1
  assert {source_work_id for _parent, source_work_id in attempts} == {
    "root-timeout-0", "root-timeout-1",
  }


def test_wake_disposition_gate_excludes_non_durable_terminals():
  import app.chat_queue as chat_queue
  from app.chat import _DELEGATION_WAKE_DISPOSITIONS

  assert (
    chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED
    in _DELEGATION_WAKE_DISPOSITIONS
  )
  assert (
    chat_queue.TerminalDisposition.PROVIDER_FREE_COMPLETED
    in _DELEGATION_WAKE_DISPOSITIONS
  )
  for excluded in (
    chat_queue.TerminalDisposition.FAILED_LEAVE_MARKER,
    chat_queue.TerminalDisposition.LIMIT_PARKED,
    chat_queue.TerminalDisposition.CONTINUATION_PROMOTED,
    chat_queue.TerminalDisposition.STALE_NO_ACTION,
    chat_queue.TerminalDisposition.DRAINED_FOR_RESTART,
  ):
    assert excluded not in _DELEGATION_WAKE_DISPOSITIONS


def test_migration_adds_wake_columns_idempotently(db):
  from sqlalchemy import inspect as sa_inspect

  from app.database import engine
  from app.schema_migrations import _add_delegation_parent_wake

  # Safe to re-run against the live (already-migrated) schema.
  _add_delegation_parent_wake(engine)
  _add_delegation_parent_wake(engine)
  cols = {c["name"] for c in sa_inspect(engine).get_columns("delegations")}
  assert "notify_parent_on_complete" in cols
  assert "parent_woken_at" in cols
