"""Contracts for durable delegated tasks and restrictive child policy."""

import hashlib

from app import models
from app.chat_writer import (
  AppendPending, Barrier, PromotePending, StartTurn, get_writer,
)
from app.codex_sdk_runner import _codex_config_overrides
from app.delegations import derived_status, mark_cancelled, policy_for_chat
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


def test_app_token_can_observe_own_work_but_cannot_submit_spend(
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
    max_budget_usd=5.0,
  )
  db.add(row)
  db.commit()

  policy = policy_for_chat(db, child.id)
  assert policy is not None
  assert policy.allow_session_reseed is False
  assert policy.max_budget_usd == 5.0
  assert "Do not launch" in policy.system_prompt

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
  db.expire_all()
  assert db.get(models.Chat, child.id).auto_resume_on_restart is False
  assert db.get(models.Chat, child.id).auto_resume_on_limit is False


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
