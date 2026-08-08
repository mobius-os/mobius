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

  wake_policy_conflict = client.post(
    "/api/delegations",
    json={**body, "notify_parent_on_complete": False},
    headers=auth,
  )
  assert wake_policy_conflict.status_code == 409


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


# --- Parent auto-wake on child completion ------------------------------------

import asyncio

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
    parent_root_run_id=f"root-{suffix}",
    task_key=f"task-{suffix}",
    child_chat_id=child_id,
    provider="claude",
    model="claude-sonnet-4-6",
    effort="high",
    scope="read",
    cwd="/data/platform",
    prompt_sha256=hashlib.sha256(b"Do the bounded task.").hexdigest(),
    max_budget_usd=5.0,
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
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at is not None

  # Second settle is a no-op — the latch holds.
  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))
  assert len(starts) == 1


def test_two_finishes_coalesce_into_one_wake(db, monkeypatch):
  parent_id, child_a, del_a = _seed_delegation(
    db, suffix="coa", result_blocks=[{"type": "text", "content": "A done"}],
  )
  # Second delegation shares the same parent chat.
  _, child_b, del_b = _seed_delegation(
    db, suffix="cob", parent_id=parent_id,
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
  assert any(
    "task-run" in (m.get("content") or "") for m in pending
  ), pending
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

  async def append_pending(content, chat_id):
    queued.append((content, chat_id))
    return True

  monkeypatch.setattr(chat_start_mod, "start_programmatic_chat_turn", blocked_start)
  monkeypatch.setattr(delegations_mod, "_append_wake_pending", append_pending)

  asyncio.run(delegations_mod.wake_parent_after_child_settled(child_id))

  assert [start["chat_id"] for start in starts] == [parent_id]
  assert len(queued) == 1
  assert queued[0][1] == parent_id
  assert "task-question" in queued[0][0]
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
  assert woken == 1
  assert len(starts) == 1
  db.expire_all()
  assert db.get(models.Delegation, delegation_id).parent_woken_at is not None

  # Idempotent: a second boot pass wakes nobody.
  woken_again = asyncio.run(
    delegations_mod.wake_parents_for_completed_delegations()
  )
  assert woken_again == 0
  assert len(starts) == 1


def test_is_delegation_child_detects_child_and_ignores_others(db):
  from app.delegations import is_delegation_child

  _, child_id, _ = _seed_delegation(db, suffix="isc")
  assert is_delegation_child(child_id) is True
  assert is_delegation_child("parent-isc") is False
  assert is_delegation_child("no-such-chat") is False
  assert is_delegation_child("") is False


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

  from app.database import _add_delegation_parent_wake, engine

  # Safe to re-run against the live (already-migrated) schema.
  _add_delegation_parent_wake(engine)
  _add_delegation_parent_wake(engine)
  cols = {c["name"] for c in sa_inspect(engine).get_columns("delegations")}
  assert "notify_parent_on_complete" in cols
  assert "parent_woken_at" in cols
