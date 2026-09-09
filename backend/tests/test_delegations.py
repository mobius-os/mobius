"""Contracts for durable delegated tasks and restrictive child policy."""

import asyncio
from contextlib import asynccontextmanager
import hashlib
from datetime import UTC, datetime, timedelta
import threading

import pytest

from app import auth, models
from app.chat_writer import (
  AppendPending, Barrier, FinishRun, PromotePending, StartTurn, get_writer,
)
from app.codex_sdk_runner import _codex_config_overrides
from app.delegations import (
  RunPolicy,
  background_helper_chat_ids,
  background_helper_goal_ids,
  delegation_execution_token,
  derived_status,
  ensure_delegation_started,
  limit_resume_app_id,
  mark_cancelled,
  parent_root_run_id,
  policy_for_chat,
  serialize_background_helpers,
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


def test_delegation_inherits_owner_tools_with_run_bound_delegation_identity(
  client, owner_token, db,
):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, owner_auth, name="Read policy")['id']
  db.add_all([
    models.Chat(id="parent", title="Parent", messages=[]),
    models.Chat(id="read-child", title="Child", messages=[], created_by_app_id=app_id),
  ])
  db.add(models.Delegation(
    id="read-policy", app_id=app_id, parent_chat_id="parent",
    parent_root_run_id="parent-root", task_key="read", child_chat_id="read-child",
    provider="codex", model=None, effort=None, scope="read", cwd="/data/platform",
    prompt_sha256=hashlib.sha256(b"read").hexdigest(),
  ))
  db.add(models.ChatRun(
    id="physical-child-run", root_run_id="physical-child-run",
    chat_id="read-child", status="running", provider="codex",
  ))
  db.commit()
  base = dict(
    delegation_id="read-policy",
    app_id=app_id,
    provider="codex",
    model=None,
    effort=None,
    cwd="/data/platform",
  )

  token = delegation_execution_token(
    db, RunPolicy(scope="read", **base), run_id="physical-child-run",
  )
  claims = auth.decode_access_token(token)
  assert claims is not None
  assert claims.get("scope") is None
  assert claims["agent_chat"] == "read-child"
  assert claims["agent_run"] == "physical-child-run"
  assert claims["delegation_id"] == "read-policy"
  assert claims["delegation_chat"] == "read-child"

  response = client.get(
    "/api/connect/hosts",
    headers={"Authorization": f"Bearer {token}"},
  )
  assert response.status_code == 200, response.text


def test_limit_resume_identity_requires_the_exact_active_delegation_run(
  client, owner_token, db,
):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, owner_auth, name="Resume policy")['id']
  db.add_all([
    models.Chat(id="resume-parent", title="Parent", messages=[]),
    models.Chat(
      id="resume-child", title="Child", messages=[],
      created_by_app_id=app_id,
    ),
  ])
  db.add(models.Delegation(
    id="resume-delegation", app_id=app_id,
    parent_chat_id="resume-parent", parent_root_run_id="resume-root",
    task_key="resume", child_chat_id="resume-child", provider="claude",
    model="claude-opus-4-8", effort="low", scope="read", cwd="/data",
    prompt_sha256=hashlib.sha256(b"resume").hexdigest(),
  ))
  db.add(models.ChatRun(
    id="resume-park", root_run_id="resume-park", chat_id="resume-child",
    status="parked", provider="claude", initiated_by_app_id=app_id,
  ))
  db.commit()

  assert limit_resume_app_id(
    db, child_chat_id="resume-child", run_token="resume-park",
    initiated_by_app_id=app_id,
  ) == app_id
  db.get(models.ChatRun, "resume-park").status = "parked_notified"
  db.commit()
  assert limit_resume_app_id(
    db, child_chat_id="resume-child", run_token="resume-park",
    initiated_by_app_id=app_id,
  ) == app_id
  assert limit_resume_app_id(
    db, child_chat_id="resume-child", run_token="resume-park",
    initiated_by_app_id=app_id + 1,
  ) is None

  delegation = db.get(models.Delegation, "resume-delegation")
  delegation.cancelled_at = datetime.now(UTC).replace(tzinfo=None)
  db.commit()
  assert limit_resume_app_id(
    db, child_chat_id="resume-child", run_token="resume-park",
    initiated_by_app_id=app_id,
  ) is None
  delegation.cancelled_at = None
  db.commit()

  app = db.get(models.App, app_id)
  app.deleted_at = datetime.now(UTC).replace(tzinfo=None)
  db.commit()
  assert limit_resume_app_id(
    db, child_chat_id="resume-child", run_token="resume-park",
    initiated_by_app_id=app_id,
  ) is None
  app.deleted_at = None
  db.commit()

  db.add(models.ChatRun(
    id="resume-newer", root_run_id="resume-newer", chat_id="resume-child",
    status="running", provider="claude", initiated_by_app_id=app_id,
  ))
  db.commit()
  assert limit_resume_app_id(
    db, child_chat_id="resume-child", run_token="resume-park",
    initiated_by_app_id=app_id,
  ) is None


def test_explicit_retry_uses_the_exact_owned_limit_park(
  client, owner_token, db, monkeypatch,
):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, owner_auth, name="Retry paused helper")['id']
  db.add_all([
    models.Chat(id="retry-parent", title="Parent", messages=[]),
    models.Chat(
      id="retry-child", title="Child", messages=[],
      created_by_app_id=app_id,
    ),
  ])
  row = models.Delegation(
    id="retry-delegation", app_id=app_id,
    parent_chat_id="retry-parent", parent_root_run_id="retry-root",
    task_key="retry", child_chat_id="retry-child", provider="claude",
    model="claude-opus-4-8", effort="low", scope="read", cwd="/data",
    prompt_sha256=hashlib.sha256(b"retry").hexdigest(),
  )
  db.add(row)
  db.add(models.ChatRun(
    id="retry-park", root_run_id="retry-park", chat_id="retry-child",
    status="parked_notified", provider="claude", initiated_by_app_id=app_id,
    park_reason="usage_limit",
    parked_until=(
      datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=4)
    ),
  ))
  db.commit()
  resumed = []

  async def _resume(chat_id, park_token=None, **kwargs):
    resumed.append((chat_id, park_token, kwargs))
    return True

  monkeypatch.setattr("app.chat._auto_resume_chat", _resume)
  # Ordinary idempotent attachment is observational: it must not spend a
  # provider retry merely because the helper reconnected to poll status.
  assert asyncio.run(ensure_delegation_started(db, row)) is False
  assert resumed == []

  foreign_app_id = create_local_app(
    client, owner_auth, name="Unrelated retry caller",
  )['id']
  foreign_token = client.post(
    "/api/auth/app-token", json={"app_id": foreign_app_id},
    headers=owner_auth,
  ).json()["token"]
  foreign = client.post(
    "/api/delegations/retry-delegation/retry",
    json={"run_token": "retry-park"},
    headers={"Authorization": f"Bearer {foreign_token}"},
  )
  assert foreign.status_code == 404
  assert resumed == []

  wrong = client.post(
    "/api/delegations/retry-delegation/retry",
    json={"run_token": "wrong-park"}, headers=owner_auth,
  )
  assert wrong.status_code == 200, wrong.text
  assert wrong.json()["retry_started"] is False
  assert wrong.json()["status"] == "paused"

  retried = client.post(
    "/api/delegations/retry-delegation/retry",
    json={"run_token": "retry-park"}, headers=owner_auth,
  )
  assert retried.status_code == 200, retried.text
  assert retried.json()["retry_started"] is True, retried.text
  assert retried.json()["status"] == "resuming"
  assert resumed == [("retry-child", "retry-park", {})]
  assert db.get(models.ChatRun, "retry-park").status == "resume_pending"

  replay = client.post(
    "/api/delegations/retry-delegation/retry",
    json={"run_token": "retry-park"}, headers=owner_auth,
  )
  assert replay.status_code == 200, replay.text
  assert replay.json()["retry_started"] is False
  assert resumed == [("retry-child", "retry-park", {})]

  old = db.get(models.ChatRun, "retry-park")
  old.status = "completed"
  db.add(models.ChatRun(
    id="retry-new-park", root_run_id="retry-new-park",
    chat_id="retry-child", status="parked", provider="claude",
    initiated_by_app_id=app_id, park_reason="usage_limit",
    started_at=(
      datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=1)
    ),
    parked_until=(
      datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=5)
    ),
  ))
  db.commit()
  stale_replay = client.post(
    "/api/delegations/retry-delegation/retry",
    json={"run_token": "retry-park"}, headers=owner_auth,
  )
  assert stale_replay.status_code == 200, stale_replay.text
  assert stale_replay.json()["retry_started"] is False
  assert stale_replay.json()["physical_run_id"] == "retry-new-park"
  assert resumed == [("retry-child", "retry-park", {})]


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
  assert first.json()["observation_mode"] == "parent_wake"
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

  omitted_cwd = dict(body)
  omitted_cwd.pop("cwd")
  legacy_default_attach = client.post(
    "/api/delegations", json=omitted_cwd, headers=auth,
  )
  assert legacy_default_attach.status_code == 201, legacy_default_attach.text
  assert legacy_default_attach.json()["attached"] is True
  assert legacy_default_attach.json()["cwd"] == "/data/platform"

  explicit_cwd_conflict = client.post(
    "/api/delegations", json={**body, "cwd": "/data"}, headers=auth,
  )
  assert explicit_cwd_conflict.status_code == 409

  conflict = client.post(
    "/api/delegations",
    json={**body, "prompt": "Different work."},
    headers=auth,
  )
  assert conflict.status_code == 409

  blocking_attach = client.post(
    "/api/delegations",
    json={**body, "notify_parent_on_complete": False},
    headers=auth,
  )
  assert blocking_attach.status_code == 201, blocking_attach.text
  assert blocking_attach.json()["attached"] is True
  assert blocking_attach.json()["observation_mode"] == "inline"
  db.expire_all()
  inline_owner = db.query(models.Delegation).filter_by(
    task_key="audit-restart",
  ).one()
  assert inline_owner.notify_parent_on_complete is False

  background_task = {
    **body,
    "task_key": "observer-upgrade",
    "notify_parent_on_complete": False,
  }
  blocking_first = client.post(
    "/api/delegations", json=background_task, headers=auth,
  )
  assert blocking_first.status_code == 201, blocking_first.text
  assert blocking_first.json()["observation_mode"] == "inline"
  db.expire_all()
  observation_only = db.query(models.Delegation).filter_by(
    task_key="observer-upgrade",
  ).one()
  assert observation_only.notify_parent_on_complete is False
  background_later = client.post(
    "/api/delegations",
    json={**background_task, "notify_parent_on_complete": True},
    headers=auth,
  )
  assert background_later.status_code == 201, background_later.text
  assert background_later.json()["attached"] is True
  assert background_later.json()["observation_mode"] == "inline"
  db.expire_all()
  retained_inline_owner = db.query(models.Delegation).filter_by(
    task_key="observer-upgrade",
  ).one()
  assert retained_inline_owner.notify_parent_on_complete is False


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
  child_auth = {
    "Authorization": f"Bearer {delegation_execution_token(db, child_policy)}"
  }
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
    db.add(models.ChatRun(
      id=f"depth-{depth}-parent-run",
      root_run_id=f"depth-{depth}-parent-run",
      chat_id=nested_parent,
      status="running",
      provider="claude",
    ))
    db.commit()
    nested_parent_policy = policy_for_chat(db, nested_parent)
    assert nested_parent_policy is not None
    nested_auth = {
      "Authorization": (
        f"Bearer {delegation_execution_token(db, nested_parent_policy)}"
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
      f"Bearer {delegation_execution_token(db, fifth_parent_policy)}"
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
  assert "provider-native helper tools" in policy.system_prompt
  assert "top-level parent owns any durable Möbius Wait" in policy.system_prompt

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


def test_delegated_codex_config_routes_questions_up_but_keeps_native_agents():
  overrides = _codex_config_overrides(
    allow_questions=False, allow_multi_agent=True, allow_goals=False,
  )
  assert "features.default_mode_request_user_input=true" not in overrides
  assert "features.multi_agent_v2.enabled=true" in overrides
  assert "features.goals=true" not in overrides


# --- Parent auto-wake on child completion ------------------------------------

import asyncio

import app.chat as chat_mod
import app.chat_start as chat_start_mod
import app.delegations as delegations_mod
from app.delegations import (
  background_helper_goal_ids,
  serialize_background_helpers,
)
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


def test_background_helper_projection_owns_waiting_until_parent_wake(
  client, owner_token, db,
):
  parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="waiting-owner", child_status="running",
  )
  _seed_delegation(
    db,
    suffix="detached-owner",
    parent_id=parent_id,
    child_status="running",
    notify=False,
  )

  assert background_helper_chat_ids(db, [parent_id]) == {parent_id}
  assert background_helper_goal_ids(db, parent_id) == {"root-waiting-owner"}
  summary = serialize_background_helpers(db, parent_id)
  assert summary == {
    "count": 1,
    "items": [{
      "id": delegation_id,
      "task_key": "task-waiting-owner",
      "provider": "claude",
      "status": "running",
    }],
  }

  auth_headers = {"Authorization": f"Bearer {owner_token}"}
  chats = client.get("/api/chats", headers=auth_headers)
  assert chats.status_code == 200, chats.text
  parent_summary = next(row for row in chats.json() if row["id"] == parent_id)
  assert parent_summary["waiting"] is True

  detail = client.get(f"/api/chats/{parent_id}", headers=auth_headers)
  assert detail.status_code == 200, detail.text
  assert detail.json()["background_helpers"] == summary
  runtime = client.get(f"/api/chats/{parent_id}/runtime", headers=auth_headers)
  assert runtime.status_code == 200, runtime.text
  assert runtime.json()["background_helpers"] == summary

  child_run = db.get(models.ChatRun, "child-run-waiting-owner")
  child_run.status = "completed"
  db.commit()
  assert serialize_background_helpers(db, parent_id)["count"] == 1

  delegation = db.get(models.Delegation, delegation_id)
  delegation.parent_woken_at = now_naive_utc()
  db.commit()
  assert background_helper_chat_ids(db, [parent_id]) == set()
  assert background_helper_goal_ids(db, parent_id) == set()
  assert serialize_background_helpers(db, parent_id) == {"count": 0, "items": []}


def test_background_helper_wait_event_is_parent_scoped_and_best_effort(
  monkeypatch,
):
  events = []

  class Broadcast:
    def publish(self, event):
      events.append(event)

  monkeypatch.setattr("app.broadcast.get_system_broadcast", lambda: Broadcast())
  delegations_mod.publish_parent_waiting_changed("parent-visible")
  assert events == [{
    "type": "chat_wait_changed",
    "chatId": "parent-visible",
    "source": "background_helpers",
  }]

  class BrokenBroadcast:
    def publish(self, _event):
      raise RuntimeError("socket is already closed")

  monkeypatch.setattr(
    "app.broadcast.get_system_broadcast", lambda: BrokenBroadcast(),
  )
  delegations_mod.publish_parent_waiting_changed("parent-visible")


def test_terminal_helper_publishes_one_exact_chat_activity_hint(
  db, monkeypatch,
):
  parent_id, child_id, _delegation_id = _seed_delegation(
    db,
    suffix="activity-hint",
    child_status="stopped",
    result_blocks=[{"type": "text", "content": "Stopped result."}],
  )
  events = []

  class Broadcast:
    def publish(self, event):
      events.append(event)

  monkeypatch.setattr("app.broadcast.get_system_broadcast", lambda: Broadcast())

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert [event for event in events if event.get("type") == "chat_activity_changed"] == [{
    "type": "chat_activity_changed",
    "chatId": parent_id,
  }]


def _seed_idle_parent_wake_root(
  db, delegation_id, *, goal_objective=None,
):
  row = db.get(models.Delegation, delegation_id)
  parent = db.get(models.Chat, row.parent_chat_id)
  parent.agent_settings_json = {"model": "claude-sonnet-4-6"}
  physical_id = (
    f"physical-{row.parent_root_run_id}"
    if goal_objective is not None else row.parent_root_run_id
  )
  db.add(models.ChatRun(
    id=physical_id,
    root_run_id=physical_id,
    chat_id=row.parent_chat_id,
    status="completed",
    provider=row.provider,
    started_at=now_naive_utc(),
    goal_objective=goal_objective,
    goal_id=(row.parent_root_run_id if goal_objective is not None else None),
  ))
  db.commit()
  return physical_id


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


def _capture_activity_starts(monkeypatch):
  starts = []

  async def fake_start(**kwargs):
    starts.append(kwargs)
    return True

  monkeypatch.setattr(
    chat_start_mod, "start_programmatic_activity_continuation", fake_start,
  )
  return starts


def test_child_completion_starts_one_non_message_checkpoint_for_waiting_parent(
  db, monkeypatch,
):
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="activity-wait",
    result_blocks=[{"type": "text", "content": "All checks passed."}],
  )
  root_run_id = _seed_idle_parent_wake_root(db, delegation_id)
  starts = _capture_activity_starts(monkeypatch)
  before = list(db.get(models.Chat, parent_id).messages or [])

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert starts == [{
    "chat_id": parent_id,
    "root_run_id": root_run_id,
    "run_token": delegations_mod._activity_continuation_run_id(
      db.get(models.Delegation, delegation_id),
    ),
    "source_work_id": "root-activity-wait",
    "activity_id": delegation_id,
    "_transition_lock_held": True,
  }]
  db.expire_all()
  assert db.get(models.Chat, parent_id).messages == before
  assert db.get(models.Chat, parent_id).pending_messages == []
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None

  # Scheduling is not consumption. Until provider success, a restart-safe
  # sweep retries the same stable activity identity rather than minting work.
  assert asyncio.run(delegations_mod._deliver_parent_wake_once(
    parent_id, "root-activity-wait",
  )) is True
  assert starts[-1]["run_token"] == starts[0]["run_token"]


def test_running_parent_retains_result_for_next_context_without_queueing(
  db, monkeypatch,
):
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="activity-running",
    result_blocks=[{"type": "text", "content": "Finished while busy."}],
  )
  db.add(models.ChatRun(
    id="root-activity-running", root_run_id="root-activity-running",
    chat_id=parent_id, status="running", provider="claude",
    started_at=now_naive_utc(),
  ))
  db.commit()
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _chat_id: True)
  starts = _capture_activity_starts(monkeypatch)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert starts == []
  parent = db.get(models.Chat, parent_id)
  assert parent.messages == []
  assert parent.pending_messages == []
  delivery = delegations_mod.build_delegation_result_context(db, parent_id)
  assert delivery.delegation_ids == (delegation_id,)
  assert "Finished while busy." in delivery.text


def test_owner_question_remains_authoritative_over_delayed_helper_result(
  db,
):
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="activity-question",
    parent_pending_question_id="owner-decision",
    result_blocks=[{"type": "text", "content": "Use only after answer."}],
  )
  _seed_idle_parent_wake_root(db, delegation_id)
  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  parent = db.get(models.Chat, parent_id)
  assert parent.pending_question_id == "owner-decision"
  assert parent.messages == []
  assert parent.pending_messages == []
  assert delegations_mod.build_delegation_result_context(
    db, parent_id,
  ).delegation_ids == (delegation_id,)


def test_stopped_parent_fences_delayed_result_until_owner_work(
  db,
):
  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="activity-stopped",
    result_blocks=[{"type": "text", "content": "Retain after Stop."}],
  )
  db.add(models.ChatRun(
    id="root-activity-stopped", root_run_id="root-activity-stopped",
    chat_id=parent_id, status="stopped", provider="claude",
    started_at=now_naive_utc(), ended_at=now_naive_utc(),
  ))
  db.commit()
  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert db.get(models.Delegation, delegation_id).parent_woken_at is None
  assert delegations_mod.build_delegation_result_context(
    db, parent_id,
  ).delegation_ids == (delegation_id,)


def test_activity_continuation_writer_changes_run_state_not_messages(db):
  from app.chat_writer import StartActivityContinuation

  parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="writer-activity",
    result_blocks=[{"type": "text", "content": "Durable result."}],
    parent_messages=[{"role": "user", "content": "Original owner work."}],
  )
  root_run_id = _seed_idle_parent_wake_root(db, delegation_id)
  parent = db.get(models.Chat, parent_id)
  before_messages = list(parent.messages or [])
  before_pending = list(parent.pending_messages or [])
  run_token = delegations_mod._activity_continuation_run_id(
    db.get(models.Delegation, delegation_id),
  )

  result = get_writer().submit(StartActivityContinuation(
    chat_id=parent_id, run_token=run_token, root_run_id=root_run_id,
    source_work_id="root-writer-activity", activity_id=delegation_id,
  )).result(timeout=5)

  assert "promoted" not in result
  assert [message.content for message in result["history"]] == [
    "Original owner work.",
  ]
  db.expire_all()
  parent = db.get(models.Chat, parent_id)
  assert parent.messages == before_messages
  assert parent.pending_messages == before_pending
  physical = db.get(models.ChatRun, run_token)
  assert physical.status == "running"
  assert physical.root_run_id == root_run_id


def test_activity_scope_keeps_goal_source_distinct_from_physical_root(db):
  from app.chat_writer import StartActivityContinuation

  parent_id, _child_id, delegation_id = _seed_delegation(
    db,
    suffix="goal-source-identity",
    parent_root_id="stable-goal-source",
    result_blocks=[{"type": "text", "content": "Goal result."}],
  )
  root_run_id = _seed_idle_parent_wake_root(
    db, delegation_id, goal_objective="Ship the bounded goal",
  )
  row = db.get(models.Delegation, delegation_id)
  run_token = delegations_mod._activity_continuation_run_id(row)
  get_writer().submit(StartActivityContinuation(
    chat_id=parent_id,
    run_token=run_token,
    root_run_id=root_run_id,
    source_work_id=row.parent_root_run_id,
    activity_id=delegation_id,
  )).result(timeout=5)

  db.expire_all()
  physical = db.get(models.ChatRun, run_token)
  assert physical.root_run_id == "physical-stable-goal-source"
  assert physical.goal_id == "stable-goal-source"
  assert physical.activity_delivery_json["source_work_id"] == (
    "stable-goal-source"
  )
  assert delegations_mod.activity_continuation_delivery_source_work_id(
    db, parent_id, run_token,
  ) == "stable-goal-source"


def test_successful_finalize_consumes_exact_activity_once(db):
  from app.chat_writer import (
    AdmitProviderExecution, Finalize, StartActivityContinuation,
  )

  parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="activity-ack",
    result_blocks=[{"type": "text", "content": "Consume exactly once."}],
    parent_messages=[{"role": "user", "content": "Original owner work."}],
  )
  root_run_id = _seed_idle_parent_wake_root(db, delegation_id)
  run_token = delegations_mod._activity_continuation_run_id(
    db.get(models.Delegation, delegation_id),
  )
  get_writer().submit(StartActivityContinuation(
    chat_id=parent_id, run_token=run_token, root_run_id=root_run_id,
    source_work_id="root-activity-ack", activity_id=delegation_id,
  )).result(timeout=5)
  get_writer().submit(AdmitProviderExecution(
    chat_id=parent_id, run_token=run_token,
    activity_delegation_ids=(delegation_id,),
  )).result(timeout=5)
  # Provider return is carried directly into the terminal write. The assistant
  # reply and result consumption become durable in the same Finalize commit.
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None
  terminal = Finalize(
    chat_id=parent_id,
    run_token=run_token,
    snapshot={
      "id": run_token,
      "role": "assistant",
      "blocks": [{"type": "text", "content": "Result incorporated."}],
    },
    incorporate_activity_delivery=True,
  )
  assert get_writer().submit(terminal).result(timeout=5) is True
  db.expire_all()
  delegation = db.get(models.Delegation, delegation_id)
  first_woken_at = delegation.parent_woken_at
  first_incorporated_at = delegation.result_incorporated_at
  assert first_woken_at is not None
  assert first_incorporated_at is not None
  from app.chat_activity import chat_activity_page
  assert chat_activity_page(db, parent_id)["events"][0][
    "consumption"
  ] == "incorporated"
  assert db.get(models.ChatRun, run_token).activity_delivery_json[
    "delivery_contract"
  ] == delegations_mod.ACTIVITY_DELIVERY_FINALIZE_ATOMIC
  assert get_writer().submit(Finalize(
    chat_id=terminal.chat_id,
    run_token=terminal.run_token,
    snapshot=terminal.snapshot,
    incorporate_activity_delivery=True,
  )).result(timeout=5) is True
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at == first_woken_at
  assert db.get(
    models.Delegation, delegation_id,
  ).result_incorporated_at == first_incorporated_at
  assert delegations_mod.build_delegation_result_context(
    db, parent_id,
  ).delegation_ids == ()


class _ActivityCheckpointProvider:
  """Network-free provider double for the real parent-checkpoint turn path."""

  def __init__(self, provider_id):
    self.name = f"Test {provider_id}"
    self.runtime_kind = f"{provider_id}_sdk"

  def check_auth(self, _data_dir):
    return None

  async def ensure_auth(self, _data_dir):
    return None

  def build_env(self, **_kwargs):
    return {}


def _run_activity_checkpoint(
  db, monkeypatch, *, parent_id, root_run_id, delegation_id, response,
  provider_id="claude", run_gen=None, before_provider_return=None,
):
  """Drive the owning chat.py provider/ack/finalize path for one wake."""
  from app import chat_queue, schemas
  from app.broadcast import create_broadcast, remove_broadcast
  from app.chat_writer import StartActivityContinuation

  row = db.get(models.Delegation, delegation_id)
  if db.query(models.Owner).first() is None:
    db.add(models.Owner(
      username="activity-checkpoint-owner",
      hashed_password="unused",
      provider="claude",
    ))
    db.commit()
  run_token = delegations_mod._activity_continuation_run_id(row)
  started = get_writer().submit(StartActivityContinuation(
    chat_id=parent_id,
    run_token=run_token,
    root_run_id=root_run_id,
    source_work_id=row.parent_root_run_id,
    activity_id=delegation_id,
  )).result(timeout=5)
  assert "promoted" not in started

  seen_prompts = []

  async def provider_turn(*, user_message, bc, **_kwargs):
    seen_prompts.append(user_message)
    bc.publish({"type": "text", "content": response})
    if before_provider_return is not None:
      before_provider_return()
    return {
      "session_id": None,
      "cost_usd": 0.0,
      "error": None,
      "terminal_status": "completed",
    }

  monkeypatch.setattr(
    chat_mod, "get_provider", lambda _id: _ActivityCheckpointProvider(provider_id),
  )
  monkeypatch.setattr(
    f"app.{provider_id}_sdk_runner.run_{provider_id}_sdk_turn", provider_turn,
  )

  async def skip_browser_cleanup(_chat_id):
    return None

  # This regression owns only the chat state machine. Never let its terminal
  # path address even a disposable agent-browser namespace.
  monkeypatch.setattr(chat_mod, "_close_browser_session", skip_browser_cleanup)
  source = delegations_mod.activity_continuation_source(
    run_token=run_token,
    source_work_id=row.parent_root_run_id,
  )
  create_broadcast(parent_id)
  try:
    disposition = asyncio.run(chat_mod._run_chat_impl(
      messages=[schemas.ChatMessage(role="user", content=source["content"])],
      chat_id=parent_id,
      session_id=None,
      provider_id=provider_id,
      run_gen=run_gen,
      run_token=run_token,
    ))
  finally:
    remove_broadcast(parent_id)
  get_writer().submit(Barrier()).result(timeout=5)
  return disposition, seen_prompts


@pytest.mark.parametrize("provider_id", ["claude", "codex"])
def test_automatic_root_checkpoint_does_not_admit_or_ack_sibling_root_result(
  db, monkeypatch, provider_id,
):
  """A root-A wake must not consume a terminal helper owned by root B."""
  from app import chat_queue

  parent_id, _child_a, delegation_a = _seed_delegation(
    db,
    suffix="root-bound-a",
    parent_root_id="root-bound-a",
    result_blocks=[{"type": "text", "content": "Result owned by root A."}],
    parent_messages=[{"role": "user", "content": "Original owner work."}],
  )
  _same_parent, _child_b, delegation_b = _seed_delegation(
    db,
    suffix="root-bound-b",
    parent_id=parent_id,
    parent_root_id="root-bound-b",
    result_blocks=[{"type": "text", "content": "Result owned by root B."}],
  )
  root_b = _seed_idle_parent_wake_root(db, delegation_b)
  root_a = _seed_idle_parent_wake_root(db, delegation_a)
  # StartActivityContinuation's production gate requires root A to be the
  # parent's latest completed root. Make the otherwise-real-time ordering
  # explicit so this probe cannot depend on clock resolution.
  db.get(models.ChatRun, root_b).started_at = now_naive_utc() - timedelta(minutes=1)
  db.get(models.ChatRun, root_a).started_at = now_naive_utc()
  db.commit()

  disposition, seen_prompts = _run_activity_checkpoint(
    db,
    monkeypatch,
    parent_id=parent_id,
    root_run_id=root_a,
    delegation_id=delegation_a,
    response="Root A incorporated its helper.",
    provider_id=provider_id,
  )

  db.expire_all()
  envelope = db.get(
    models.ChatRun,
    delegations_mod._activity_continuation_run_id(
      db.get(models.Delegation, delegation_a),
    ),
  ).activity_delivery_json
  delivery_after_root_a = delegations_mod.build_delegation_result_context(
    db, parent_id,
  )
  assert disposition is chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED
  assert db.get(models.Delegation, delegation_a).parent_woken_at is not None
  assert {
    "root_b_was_injected": "Result owned by root B." in seen_prompts[0],
    "root_b_was_consumed": (
      db.get(models.Delegation, delegation_b).parent_woken_at is not None
    ),
    "root_b_is_redeliverable": delegation_b in delivery_after_root_a.delegation_ids,
    "admitted_ids": envelope["delegation_ids"],
    "admitted_source": envelope["source_work_id"],
  } == {
    "root_b_was_injected": False,
    "root_b_was_consumed": False,
    "root_b_is_redeliverable": True,
    "admitted_ids": [delegation_a],
    "admitted_source": "root-bound-a",
  }


def test_ordinary_owner_turn_keeps_intentional_chat_wide_result_breadth(db):
  """Root binding narrows machine wakes, not an owner's ordinary turn."""
  from app.chat_writer import (
    AdmitProviderExecution, Finalize,
  )

  parent_id, _child_a, delegation_a = _seed_delegation(
    db,
    suffix="owner-breadth-a",
    parent_root_id="root-owner-breadth-a",
    result_blocks=[{"type": "text", "content": "Owner result A."}],
    parent_messages=[{"role": "user", "content": "Earlier owner work."}],
  )
  _same_parent, _child_b, delegation_b = _seed_delegation(
    db,
    suffix="owner-breadth-b",
    parent_id=parent_id,
    parent_root_id="root-owner-breadth-b",
    result_blocks=[{"type": "text", "content": "Owner result B."}],
  )
  scoped = delegations_mod.build_delegation_result_context(
    db, parent_id, source_work_id="root-owner-breadth-a",
  )
  ordinary = delegations_mod.build_delegation_result_context(db, parent_id)
  assert scoped.delegation_ids == (delegation_a,)
  assert set(ordinary.delegation_ids) == {delegation_a, delegation_b}

  run_token = "rt-ordinary-owner-breadth"
  get_writer().submit(StartTurn(
    chat_id=parent_id,
    run_token=run_token,
    user_msg={"role": "user", "content": "Continue owner work.", "ts": 2},
    title_source="Continue owner work.",
    default_provider="claude",
  )).result(timeout=5)
  get_writer().submit(AdmitProviderExecution(
    chat_id=parent_id,
    run_token=run_token,
    activity_delegation_ids=ordinary.delegation_ids,
  )).result(timeout=5)
  assert get_writer().submit(Finalize(
    chat_id=parent_id,
    run_token=run_token,
    snapshot={
      "id": run_token,
      "role": "assistant",
      "blocks": [{"type": "text", "content": "Both incorporated."}],
    },
    incorporate_activity_delivery=True,
  )).result(timeout=5) is True

  db.expire_all()
  assert db.get(models.Delegation, delegation_a).parent_woken_at is not None
  assert db.get(models.Delegation, delegation_b).parent_woken_at is not None


@pytest.mark.parametrize("provider_id", ["claude", "codex"])
def test_failed_finalize_keeps_injected_activity_result_redeliverable(
  db, monkeypatch, provider_id,
):
  """Provider success alone must not consume data whose final reply was lost."""
  from app import chat_queue

  parent_id, _child_id, delegation_id = _seed_delegation(
    db,
    suffix="finalize-redelivery",
    parent_root_id="root-finalize-redelivery",
    parent_messages=[{"role": "user", "content": "Original owner work."}],
    result_blocks=[{
      "type": "text", "content": "Deliver again until the reply commits.",
    }],
  )
  root_run_id = _seed_idle_parent_wake_root(db, delegation_id)

  real_stage = get_writer()._stage_activity_delivery_consumption

  def reject_terminal_commit(*args, **kwargs):
    # Exercise the transaction boundary after BOTH the assistant snapshot and
    # parent_woken_at have been staged. The actor rollback must erase both.
    real_stage(*args, **kwargs)
    raise RuntimeError("injected terminal persistence failure")

  monkeypatch.setattr(
    get_writer(), "_stage_activity_delivery_consumption",
    reject_terminal_commit,
  )
  # Owning production order under test:
  # Provider success -> _complete_turn -> sink.finalize/Finalize rollback. The
  # negative check below requires the activity latch to remain open when that
  # terminal response never commits.
  disposition, _seen_prompts = _run_activity_checkpoint(
    db,
    monkeypatch,
    parent_id=parent_id,
    root_run_id=root_run_id,
    delegation_id=delegation_id,
    response="Provider reply that Finalize must commit.",
    provider_id=provider_id,
  )

  db.expire_all()
  parent = db.get(models.Chat, parent_id)
  durable_messages = list(parent.messages or [])
  delivery_after_failure = delegations_mod.build_delegation_result_context(
    db, parent_id,
  )
  assert disposition is chat_queue.TerminalDisposition.FAILED_LEAVE_MARKER
  assert not any(
    "Provider reply that Finalize must commit." in str(message)
    for message in durable_messages
  )
  assert {
    "activity_was_consumed": (
      db.get(models.Delegation, delegation_id).parent_woken_at is not None
    ),
    "activity_is_redeliverable": (
      delegation_id in delivery_after_failure.delegation_ids
    ),
  } == {
    "activity_was_consumed": False,
    "activity_is_redeliverable": True,
  }
  assert db.get(
    models.Delegation, delegation_id,
  ).result_incorporated_at is None
  from app.chat_activity import chat_activity_page
  assert chat_activity_page(db, parent_id)["events"][0][
    "consumption"
  ] == "available"


@pytest.mark.parametrize("race", ["stop", "superseded"])
def test_stop_and_supersession_gates_keep_activity_result_redeliverable(
  db, monkeypatch, race,
):
  """A clean provider return cannot bypass terminal ownership gates."""
  from app import chat_queue

  parent_id, _child_id, delegation_id = _seed_delegation(
    db,
    suffix=f"activity-{race}-gate",
    result_blocks=[{"type": "text", "content": "Keep through race."}],
    parent_messages=[{"role": "user", "content": "Original owner work."}],
  )
  root_run_id = _seed_idle_parent_wake_root(db, delegation_id)
  assert chat_mod.mark_starting(parent_id) is True
  run_gen = chat_mod.current_run_generation(parent_id)

  def race_terminal_gate():
    if race == "stop":
      chat_mod._clear_after_terminal_generation[parent_id] = run_gen
      chat_mod._clear_after_terminal_status[parent_id] = "stopped"
    chat_mod.bump_run_generation(parent_id)
    if race == "stop":
      chat_mod.discard_starting(parent_id)

  try:
    disposition, _seen_prompts = _run_activity_checkpoint(
      db,
      monkeypatch,
      parent_id=parent_id,
      root_run_id=root_run_id,
      delegation_id=delegation_id,
      response="Provider reply at the ownership race.",
      run_gen=run_gen,
      before_provider_return=race_terminal_gate,
    )
  finally:
    chat_mod.discard_starting(parent_id)
    chat_mod.forget_chat(parent_id)
    chat_mod._clear_after_terminal_generation.pop(parent_id, None)
    chat_mod._clear_after_terminal_status.pop(parent_id, None)

  db.expire_all()
  assert disposition is chat_queue.TerminalDisposition.STALE_NO_ACTION
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None
  assert db.get(
    models.Delegation, delegation_id,
  ).result_incorporated_at is None
  envelope = db.get(
    models.ChatRun,
    delegations_mod._activity_continuation_run_id(
      db.get(models.Delegation, delegation_id),
    ),
  ).activity_delivery_json
  assert envelope.get("delivery_contract") == (
    delegations_mod.ACTIVITY_DELIVERY_FINALIZE_ATOMIC
  )
  durable_reply = any(
    "Provider reply at the ownership race." in str(message)
    for message in (db.get(models.Chat, parent_id).messages or [])
  )
  assert durable_reply is (race == "stop")
  assert delegations_mod.build_delegation_result_context(
    db, parent_id,
  ).delegation_ids == (delegation_id,)
  from app.chat_activity import chat_activity_page
  assert chat_activity_page(db, parent_id)["events"][0][
    "consumption"
  ] == "available"


@pytest.mark.parametrize("provider_id", ["claude", "codex"])
def test_stop_after_provider_return_before_finalize_keeps_activity_redeliverable(
  db, monkeypatch, provider_id,
):
  """Writer eligibility closes the post-gate Stop window on real turn flow."""
  from app.chat_writer import FinishRun, await_ack

  parent_id, _child_id, delegation_id = _seed_delegation(
    db,
    suffix=f"activity-{provider_id}-post-return-stop",
    result_blocks=[{"type": "text", "content": "Keep after late Stop."}],
    parent_messages=[{"role": "user", "content": "Original owner work."}],
  )
  root_run_id = _seed_idle_parent_wake_root(db, delegation_id)
  run_token = delegations_mod._activity_continuation_run_id(
    db.get(models.Delegation, delegation_id),
  )
  real_finalize = chat_mod._ChatEventSink.finalize

  async def stop_before_finalize(sink, **kwargs):
    await await_ack(get_writer().submit(FinishRun(
      chat_id=parent_id,
      run_token=run_token,
      terminal_status="stopped",
    )))
    return await real_finalize(sink, **kwargs)

  monkeypatch.setattr(
    chat_mod._ChatEventSink, "finalize", stop_before_finalize,
  )
  _disposition, seen_prompts = _run_activity_checkpoint(
    db,
    monkeypatch,
    parent_id=parent_id,
    root_run_id=root_run_id,
    delegation_id=delegation_id,
    response="Partial response persisted after Stop.",
    provider_id=provider_id,
  )

  db.expire_all()
  assert "Keep after late Stop." in seen_prompts[0]
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None
  assert any(
    "Partial response persisted after Stop." in str(message)
    for message in (db.get(models.Chat, parent_id).messages or [])
  )
  assert delegations_mod.build_delegation_result_context(
    db, parent_id,
  ).delegation_ids == (delegation_id,)


def test_unadmitted_activity_restart_reschedules_same_physical_turn(
  db, monkeypatch,
):
  from app.chat_writer import StartActivityContinuation

  parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="activity-restart",
    result_blocks=[{"type": "text", "content": "Redeliver after restart."}],
  )
  root_run_id = _seed_idle_parent_wake_root(db, delegation_id)
  row = db.get(models.Delegation, delegation_id)
  run_token = delegations_mod._activity_continuation_run_id(row)
  get_writer().submit(StartActivityContinuation(
    chat_id=parent_id, run_token=run_token, root_run_id=root_run_id,
    source_work_id=row.parent_root_run_id, activity_id=delegation_id,
  )).result(timeout=5)
  scheduled = []

  def capture_task(coro):
    coro.close()
    scheduled.append(run_token)
    return object()

  monkeypatch.setattr(asyncio, "create_task", capture_task)

  for _restart in range(2):
    assert asyncio.run(
      chat_start_mod.start_programmatic_activity_continuation(
        chat_id=parent_id,
        root_run_id=root_run_id,
        run_token=run_token,
        source_work_id=row.parent_root_run_id,
        activity_id=delegation_id,
      )
    ) is True
    chat_mod.discard_starting(parent_id)
    chat_start_mod.remove_broadcast(parent_id)

  assert scheduled == [run_token, run_token]
  assert db.query(models.ChatRun).filter(
    models.ChatRun.id == run_token,
  ).count() == 1
  assert db.get(models.Chat, parent_id).messages == []


def test_admitted_provider_return_crash_keeps_result_for_owner_replay(db):
  from app.chat_writer import (
    AdmitProviderExecution, StartActivityContinuation,
  )

  parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="activity-admission-crash",
    result_blocks=[{"type": "text", "content": "Replay after crash."}],
  )
  root_run_id = _seed_idle_parent_wake_root(db, delegation_id)
  run_token = delegations_mod._activity_continuation_run_id(
    db.get(models.Delegation, delegation_id),
  )
  get_writer().submit(StartActivityContinuation(
    chat_id=parent_id, run_token=run_token, root_run_id=root_run_id,
    source_work_id="root-activity-admission-crash", activity_id=delegation_id,
  )).result(timeout=5)
  get_writer().submit(AdmitProviderExecution(
    chat_id=parent_id, run_token=run_token,
    activity_delegation_ids=(delegation_id,),
  )).result(timeout=5)
  # Simulate reconciliation after the process died after provider return but
  # before Finalize could durably incorporate its assistant response.
  get_writer().submit(FinishRun(
    chat_id=parent_id, run_token=run_token, terminal_status="interrupted",
  )).result(timeout=5)
  assert asyncio.run(delegations_mod._deliver_parent_wake_once(
    parent_id, "root-activity-admission-crash",
  )) is False
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None
  assert delegations_mod.build_delegation_result_context(
    db, parent_id,
  ).delegation_ids == (delegation_id,)
  assert db.query(models.ChatRun).filter(
    models.ChatRun.id == run_token,
  ).count() == 1


def test_pre_atomic_completed_delivery_repair_remains_supported(db):
  """Existing completed envelopes retain their historical crash repair."""
  parent_id, _child_id, delegation_id = _seed_delegation(
    db,
    suffix="legacy-activity-delivery-repair",
    result_blocks=[{"type": "text", "content": "Historical result."}],
  )
  db.add(models.ChatRun(
    id="rt-legacy-activity-delivery-repair",
    root_run_id="rt-legacy-activity-delivery-repair",
    chat_id=parent_id,
    status="completed",
    provider="claude",
    provider_execution_admitted=True,
    activity_delivery_json={"delegation_ids": [delegation_id]},
    started_at=now_naive_utc(),
    ended_at=now_naive_utc(),
  ))
  db.commit()

  assert delegations_mod.repair_completed_activity_deliveries(
    db, parent_id,
  ) == {delegation_id}
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at is not None
  assert db.get(
    models.Delegation, delegation_id,
  ).result_incorporated_at is None


@pytest.mark.parametrize("contract", [
  delegations_mod.ACTIVITY_DELIVERY_FINALIZE_ATOMIC, "unknown-contract",
])
def test_atomic_completed_envelope_alone_never_repairs_activity_delivery(db, contract):
  """New run status is not a substitute for the atomic Finalize commit."""
  parent_id, _child_id, delegation_id = _seed_delegation(
    db,
    suffix="atomic-activity-delivery-no-repair",
    result_blocks=[{"type": "text", "content": "Still undelivered."}],
  )
  db.add(models.ChatRun(
    id="rt-atomic-activity-delivery-no-repair",
    root_run_id="rt-atomic-activity-delivery-no-repair",
    chat_id=parent_id,
    status="completed",
    provider="claude",
    provider_execution_admitted=True,
    activity_delivery_json={
      "delegation_ids": [delegation_id],
      "delivery_contract": contract,
    },
    started_at=now_naive_utc(),
    ended_at=now_naive_utc(),
  ))
  db.commit()

  assert delegations_mod.repair_completed_activity_deliveries(
    db, parent_id,
  ) == set()
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None
  assert db.get(
    models.Delegation, delegation_id,
  ).result_incorporated_at is None


@pytest.mark.parametrize("terminal_status", ["stopped", "interrupted"])
def test_stopped_or_superseded_activity_never_consumes_accepted_result(
  db, terminal_status,
):
  from app.chat_writer import (
    AdmitProviderExecution, Finalize, StartActivityContinuation,
  )

  parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="activity-run-stop",
    result_blocks=[{"type": "text", "content": "Still available."}],
    parent_messages=[{"role": "user", "content": "Original owner work."}],
  )
  root_run_id = _seed_idle_parent_wake_root(db, delegation_id)
  run_token = delegations_mod._activity_continuation_run_id(
    db.get(models.Delegation, delegation_id),
  )
  get_writer().submit(StartActivityContinuation(
    chat_id=parent_id, run_token=run_token, root_run_id=root_run_id,
    source_work_id="root-activity-run-stop", activity_id=delegation_id,
  )).result(timeout=5)
  get_writer().submit(AdmitProviderExecution(
    chat_id=parent_id, run_token=run_token,
    activity_delegation_ids=(delegation_id,),
  )).result(timeout=5)
  get_writer().submit(FinishRun(
    chat_id=parent_id, run_token=run_token, terminal_status=terminal_status,
  )).result(timeout=5)
  # A stopped/superseded provider can still leave partial assistant output
  # that Finalize must preserve. The terminal run is no longer eligible to
  # incorporate helper context even when its terminal command carries a
  # successful-provider intent.
  assert get_writer().submit(Finalize(
    chat_id=parent_id,
    run_token=run_token,
    snapshot={
      "id": run_token,
      "role": "assistant",
      "blocks": [{"type": "text", "content": "Persisted partial output."}],
    },
    incorporate_activity_delivery=True,
  )).result(timeout=5) is True
  assert asyncio.run(delegations_mod._deliver_parent_wake_once(
    parent_id, "root-activity-run-stop",
  )) is False
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None
  assert db.get(
    models.Delegation, delegation_id,
  ).result_incorporated_at is None
  assert any(
    "Persisted partial output." in str(message)
    for message in (db.get(models.Chat, parent_id).messages or [])
  )
  from app.chat_activity import chat_activity_page
  assert chat_activity_page(db, parent_id)["events"][0][
    "consumption"
  ] == "available"


def test_legacy_completion_carrier_is_recognized_without_rewriting_history(db):
  parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="legacy-carrier",
    result_blocks=[{"type": "text", "content": "Historical carrier."}],
  )
  row = db.get(models.Delegation, delegation_id)
  content = delegations_mod._compose_wake_notice(db, [row])
  run_token, cid = delegations_mod._parent_wake_delivery_identity([row])
  carrier = {
    "role": "user", "content": content, "cid": cid, "hidden": True,
    "kind": "delegation_result", "source_work_id": row.parent_root_run_id,
  }
  parent = db.get(models.Chat, parent_id)
  parent.messages = [carrier]
  db.commit()
  before = list(parent.messages)

  assert delegations_mod.claim_scheduled_parent_wake(parent_id, carrier) is True
  db.expire_all()
  assert db.get(models.Chat, parent_id).messages == before
  assert db.get(models.Delegation, delegation_id).parent_woken_at is not None
  assert run_token.startswith("delegation-wake-")


def test_legacy_committed_carrier_keeps_its_exact_restart_recovery(
  db, monkeypatch,
):
  parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="legacy-recovery",
    result_blocks=[{"type": "text", "content": "Recover legacy once."}],
  )
  root_run_id = _seed_idle_parent_wake_root(db, delegation_id)
  row = db.get(models.Delegation, delegation_id)
  content = delegations_mod._compose_wake_notice(db, [row])
  run_token, cid = delegations_mod._parent_wake_delivery_identity([row])
  carrier = {
    "role": "user", "content": content, "cid": cid, "hidden": True,
    "kind": "delegation_result", "source_work_id": row.parent_root_run_id,
  }
  parent = db.get(models.Chat, parent_id)
  parent.messages = [carrier]
  parent.live_assistant = {
    "id": run_token, "role": "assistant", "blocks": [], "ts": 2,
  }
  db.add(models.ChatRun(
    id=run_token, root_run_id=root_run_id, chat_id=parent_id,
    status="running", provider="claude", provider_execution_admitted=False,
    started_at=now_naive_utc() + timedelta(seconds=1),
  ))
  db.commit()
  attempts = []

  async def recover(**kwargs):
    attempts.append(kwargs)
    return True

  monkeypatch.setattr(
    chat_start_mod, "start_programmatic_chat_continuation", recover,
  )

  assert delegations_mod.safe_parent_wake_startup_writer_orphan(
    db, parent, db.get(models.ChatRun, run_token),
  ) is True
  assert asyncio.run(delegations_mod._deliver_parent_wake_once(
    parent_id, row.parent_root_run_id,
  )) is True
  assert attempts[0]["run_token"] == run_token
  db.expire_all()
  assert db.get(models.Chat, parent_id).messages == [carrier]


def test_legacy_pending_carrier_is_read_only_compatibility_history(db):
  parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="legacy-pending",
    result_blocks=[{"type": "text", "content": "Already queued."}],
  )
  row = db.get(models.Delegation, delegation_id)
  content = delegations_mod._compose_wake_notice(db, [row])
  _run_token, cid = delegations_mod._parent_wake_delivery_identity([row])
  carrier = {
    "role": "user", "content": content, "cid": cid, "hidden": True,
    "kind": "delegation_result", "source_work_id": row.parent_root_run_id,
  }
  parent = db.get(models.Chat, parent_id)
  parent.pending_messages = [carrier]
  db.commit()

  assert asyncio.run(delegations_mod._append_wake_pending(
    content, parent_id, row.parent_root_run_id,
  )) is True
  db.expire_all()
  assert db.get(models.Chat, parent_id).pending_messages == [carrier]


def test_cancelling_an_owner_settles_descendants_before_the_parent(db):
  _parent, child_owner, owner_id = _seed_delegation(
    db, suffix="cancel-owner", child_status="running",
  )
  _same_parent, child_leaf, leaf_id = _seed_delegation(
    db, suffix="cancel-leaf", parent_id=child_owner, child_status="running",
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


def test_non_wake_terminal_and_inline_results_never_start_activity(
  db, monkeypatch,
):
  _, stopped_child, _ = _seed_delegation(
    db, suffix="activity-child-stop", child_status="stopped",
  )
  _, interrupted_child, _ = _seed_delegation(
    db, suffix="activity-child-interrupted", child_status="interrupted",
  )
  _, inline_child, inline_id = _seed_delegation(
    db, suffix="activity-inline", notify=False,
    result_blocks=[{"type": "text", "content": "Inline only."}],
  )
  starts = _capture_activity_starts(monkeypatch)

  for child_id in (stopped_child, interrupted_child, inline_child):
    asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert starts == []
  inline_parent = db.get(models.Delegation, inline_id).parent_chat_id
  assert delegations_mod.build_delegation_result_context(
    db, inline_parent,
  ).delegation_ids == ()


def test_reconcile_schedules_completed_activity_once_without_consuming(db, monkeypatch):
  parent_id, _child_id, delegation_id = _seed_delegation(
    db, suffix="activity-away",
    result_blocks=[{"type": "text", "content": "Finished while away."}],
  )
  _seed_idle_parent_wake_root(db, delegation_id)
  starts = _capture_activity_starts(monkeypatch)

  first = asyncio.run(delegations_mod.wake_parents_for_completed_delegations())
  second = asyncio.run(delegations_mod.wake_parents_for_completed_delegations())

  assert first.woken_parents == 1
  assert second.woken_parents == 1
  assert len(starts) == 2
  assert starts[0]["run_token"] == starts[1]["run_token"]
  assert db.get(models.Delegation, delegation_id).parent_woken_at is None


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
