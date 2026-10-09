"""Core helpers retain the same bounded task ownership without an App row."""
from app.chat_writer import create_chat
import asyncio
import hashlib
from datetime import timedelta

import pytest
from app import models, delegations
from app.timeutil import now_naive_utc
from tests.goal_fixtures import goal_run


def core_task(db, *, status="parked", reason="usage_limit"):
  parent=create_chat(id="core-parent",title="Parent",messages=[])
  child=create_chat(id="core-child",title="Child",messages=[],created_by_app_id=None,
                    auto_resume_on_restart=True,auto_resume_on_limit=False)
  row=models.Delegation(
    id="core-task",app_id=None,parent_chat_id=parent.id,parent_root_run_id="parent-run",
    task_key="bounded",child_chat_id=child.id,provider="codex",model="test-model",
    scope="write",cwd="/data",prompt_sha256=hashlib.sha256(b"task").hexdigest(),
    startup_prompt="task",
  )
  db.add_all([parent,child,row])
  run=goal_run(db,id="core-run",root_run_id="core-run",chat_id=child.id,
               status=status,provider="codex",initiated_by_app_id=None,
               park_reason=reason,parked_until=now_naive_utc()-timedelta(seconds=1))
  db.add(run);db.commit()
  return row,child,run


def test_core_limit_identity_is_not_mistaken_for_no_delegation(db):
  row,child,run=core_task(db)
  kwargs=dict(child_chat_id=child.id,run_token=run.id,initiated_by_app_id=None)
  assert delegations.limit_resume_delegation(db,**kwargs).id==row.id
  assert delegations.limit_resume_delegation(db,**{**kwargs,"initiated_by_app_id":123}) is None
  row.cancelled_at=now_naive_utc();db.commit()
  assert delegations.limit_resume_delegation(db,**kwargs) is None


def test_core_restart_requires_exact_nonce_latest_run_and_uncancelled_task(db):
  row,child,run=core_task(db,status="running",reason=None)
  run.restart_nonce="approved";db.commit()
  kwargs=dict(child_chat_id=child.id,run_token=run.id,initiated_by_app_id=None)
  assert delegations.restart_resume_delegation(db,**kwargs,restart_nonce="approved").id==row.id
  assert delegations.restart_resume_delegation(db,**kwargs,restart_nonce="wrong") is None
  row.cancelled_at=now_naive_utc();db.commit()
  assert delegations.restart_resume_delegation(db,**kwargs,restart_nonce="approved") is None
  row.cancelled_at=None
  db.add(goal_run(db,id="later",root_run_id="later",chat_id=child.id,status="running",
                  provider="codex",started_at=now_naive_utc()+timedelta(seconds=1)))
  db.commit()
  assert delegations.restart_resume_delegation(db,**kwargs,restart_nonce="approved") is None


def test_core_limit_successor_keeps_exact_owned_lineage(db):
  row,child,park=core_task(db,status="completed")
  successor=goal_run(db,id="successor",root_run_id=park.root_run_id,chat_id=child.id,
                     status="running",provider="codex",initiated_by_app_id=None,
                     started_at=now_naive_utc()+timedelta(seconds=1))
  db.add(successor);db.commit()
  kwargs=dict(child_chat_id=child.id,parked_run_token=park.id,
              successor_run_token=successor.id,initiated_by_app_id=None)
  assert delegations.limit_resume_successor_delegation(db,**kwargs).id==row.id
  successor.root_run_id="other";db.commit()
  assert delegations.limit_resume_successor_delegation(db,**kwargs) is None


def test_core_policy_and_execution_token_preserve_ownership_and_cancellation(client,owner_token,db):
  row,child,run=core_task(db,status="running",reason=None)
  policy=delegations.policy_for_chat(db,child.id)
  assert policy.app_id is None
  assert "bounded task" in policy.system_prompt.lower()
  token=delegations.delegation_execution_token(db,policy,run_id=run.id)
  assert token
  from app.deps import get_delegation_principal
  principal=get_delegation_principal(token=token,db=db)
  assert principal.delegation_id==row.id and principal.app_id is None
  row.cancelled_at=now_naive_utc();db.commit()
  with pytest.raises(RuntimeError,match="delegation is unavailable"):
    delegations.delegation_execution_token(db,policy,run_id=run.id)


def test_boot_recovers_unstarted_core_task_without_an_app(db,monkeypatch):
  row,child,run=core_task(db)
  db.delete(run);db.commit()
  started=[]
  async def fake_start(session,task,*args,**kwargs):
    started.append(task.id);return True
  monkeypatch.setattr(delegations,"ensure_delegation_started",fake_start)
  assert asyncio.run(delegations.reconcile_unstarted_delegations())==1
  assert started==[row.id]
  row.cancelled_at=now_naive_utc();db.commit()
  assert asyncio.run(delegations.reconcile_unstarted_delegations())==0


def test_core_limit_sweep_resumes_without_owner_quota_opt_in(db,monkeypatch):
  from app import chat
  row,child,run=core_task(db)
  calls=[]
  async def resume(chat_id,*args,**kwargs):
    calls.append((chat_id,kwargs));return True
  monkeypatch.setattr(chat,"_auto_resume_chat",resume)
  monkeypatch.setattr(chat,"is_chat_running",lambda _id:False)
  asyncio.run(chat.sweep_reset_parks(db))
  assert [c[0] for c in calls]==[child.id]
  assert db.get(models.Chat,child.id).auto_resume_on_limit is False


@pytest.mark.parametrize("reason", ["memory", "model_capacity", "storage", "restart"])
def test_cancelled_core_task_cannot_reenter_any_automatic_recovery(db, monkeypatch, reason):
  from app import chat
  row, child, run = core_task(db, reason=reason)
  run.restart_nonce = "approved"
  db.commit()
  delegations.mark_cancelled(db, row)
  calls = []
  async def resume(*args, **kwargs):
    calls.append((args, kwargs))
    return True
  monkeypatch.setattr(chat, "_auto_resume_chat", resume)
  monkeypatch.setattr(chat, "is_chat_running", lambda _id: False)
  asyncio.run(chat.sweep_reset_parks(db, restart_authorization="approved"))
  assert calls == []
  assert db.query(models.ChatRun).filter_by(chat_id=child.id).count() == 1
  assert not delegations.delegation_recovery_allowed(
    db, child_chat_id=child.id, initiated_by_app_id=None,
  )


def test_core_recovery_checks_owner_without_disabling_ordinary_chats(db):
  row, child, run = core_task(db, reason="memory")
  assert delegations.delegation_recovery_allowed(
    db, child_chat_id=child.id, initiated_by_app_id=None,
  )
  assert not delegations.delegation_recovery_allowed(
    db, child_chat_id=child.id, initiated_by_app_id=42,
  )
  assert delegations.delegation_recovery_allowed(
    db, child_chat_id="ordinary", initiated_by_app_id=None,
  )


@pytest.mark.parametrize("reason", ["memory", "model_capacity", "storage", "restart"])
def test_committed_retry_recovery_rechecks_delegation_cancellation(db, monkeypatch, reason):
  from app import chat
  row, child, park = core_task(db, status="completed", reason=reason)
  successor = goal_run(
    db, id=chat._auto_resume_run_token(park.id), root_run_id=park.root_run_id,
    chat_id=child.id, status="running", provider="codex",
    started_at=now_naive_utc() + timedelta(seconds=1),
    continuation_json={"supersedes_run_token": park.id, "reason": reason},
  )
  db.add(successor)
  db.commit()
  recovered = {"content": "continue"}
  monkeypatch.setattr(chat, "recover_start_continuation", lambda *a, **k: recovered)
  assert chat._auto_resume_recovery(db, child, successor) == (park, recovered)
  delegations.mark_cancelled(db, row)
  assert chat._auto_resume_recovery(db, child, successor) is None


def test_legacy_confined_core_token_preserves_nullable_owner_and_rejects_mismatch(
  client, owner_token, db,
):
  from app.auth import create_access_token
  from app.deps import get_delegation_principal
  from fastapi import HTTPException
  row, child, run = core_task(db)
  owner = db.query(models.Owner).first()
  claims = {"sub": owner.username, "scope": "delegation", "app_id": None,
            "delegation_id": row.id, "delegation_chat": child.id}
  principal = get_delegation_principal(token=create_access_token(claims), db=db)
  assert principal.app_id is None and principal.delegation_id == row.id
  with pytest.raises(HTTPException) as exc:
    get_delegation_principal(token=create_access_token({**claims, "app_id": 55}), db=db)
  assert exc.value.status_code == 403


@pytest.mark.parametrize("reason", ["memory", "model_capacity", "storage", "restart"])
def test_actual_retry_admission_refuses_cancelled_core_work(db, monkeypatch, reason):
  from app import chat
  row, child, park = core_task(db, status="resume_pending", reason=reason)
  park.restart_nonce = "approved"
  db.commit()
  delegations.mark_cancelled(db, row)
  scheduled = []
  monkeypatch.setattr(chat, "_schedule_continuation", lambda **kw: scheduled.append(kw))
  assert asyncio.run(chat._auto_resume_chat(
    child.id, park_token=park.id, restart_authorization="approved",
  )) is False
  assert scheduled == []
  assert db.query(models.ChatRun).filter_by(chat_id=child.id).count() == 1


def test_retired_core_helper_cannot_reenter_any_recovery(db):
  row, child, run = core_task(db, status="running", reason=None)
  run.restart_nonce = "approved"
  row.scope = "read"
  row.interrupted_at = now_naive_utc()
  db.commit()
  assert not delegations.delegation_recovery_allowed(
    db, child_chat_id=child.id, initiated_by_app_id=None,
  )
  assert delegations.restart_resume_delegation(
    db, child_chat_id=child.id, run_token=run.id,
    initiated_by_app_id=None, restart_nonce="approved",
  ) is None
  with pytest.raises(RuntimeError, match="Legacy helper cannot resume"):
    delegations.policy_for_chat(db, child.id)
