"""Recovery is an owner-confirmed lifecycle control, never delegated work."""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from app import auth as auth_mod, models
from app.config import get_settings
from app.delegations import RunPolicy, delegation_execution_token
from app.timeutil import now_naive_utc


def _authorization_context(db):
  owner = db.query(models.Owner).one()
  child_chat_id = str(uuid.uuid4())
  parent_chat_id = str(uuid.uuid4())
  top_level_chat_id = str(uuid.uuid4())
  db.add_all([
    models.Chat(id=child_chat_id, title="Recovery child", messages=[]),
    models.Chat(id=parent_chat_id, title="Recovery parent", messages=[]),
    models.Chat(id=top_level_chat_id, title="Recovery top level", messages=[]),
  ])
  app = models.App(
    name="Recovery boundary",
    description="",
    slug=f"recovery-boundary-{uuid.uuid4().hex}",
    source_dir="/tmp/mobius-tests/recovery-boundary",
    jsx_source="export default () => null",
    token_nonce="recovery-boundary-nonce",
  )
  db.add(app)
  db.flush()
  policy = RunPolicy(
    delegation_id=str(uuid.uuid4()),
    app_id=app.id,
    provider="codex",
    model=None,
    effort=None,
    scope="write",
    cwd="/data",
  )
  db.add(models.Delegation(
    id=policy.delegation_id,
    app_id=app.id,
    parent_chat_id=parent_chat_id,
    parent_root_run_id="recovery-parent-root",
    task_key="recovery-boundary",
    child_chat_id=child_chat_id,
    provider=policy.provider,
    model=policy.model,
    effort=policy.effort,
    scope=policy.scope,
    cwd=policy.cwd,
    prompt_sha256=hashlib.sha256(b"recovery boundary").hexdigest(),
  ))
  db.add_all([
    models.ChatRun(
      id="recovery-child-run",
      root_run_id="recovery-child-run",
      chat_id=child_chat_id,
      status="running",
      provider="codex",
    ),
    models.ChatRun(
      id="recovery-top-level-run",
      root_run_id="recovery-top-level-run",
      chat_id=top_level_chat_id,
      status="running",
      provider="codex",
    ),
  ])
  db.commit()
  delegated = delegation_execution_token(
    db, policy, run_id="recovery-child-run",
  )
  top_level = auth_mod.create_agent_token(
    top_level_chat_id,
    "recovery-top-level-run",
    owner.username,
    owner.token_epoch,
  )
  return {
    "delegated": {"Authorization": f"Bearer {delegated}"},
    "top_level": {"Authorization": f"Bearer {top_level}"},
  }


def _deleted_project(db) -> models.Project:
  project_id = str(uuid.uuid4())
  root_path = f"projects/{project_id}"
  (Path(get_settings().data_dir) / root_path).mkdir(parents=True)
  row = models.Project(
    id=project_id,
    name="Recoverable project",
    project_type="blank",
    root_path=root_path,
    template_snapshot_json={},
    deleted_at=now_naive_utc(),
  )
  db.add(row)
  db.commit()
  return row


def test_real_delegated_bearer_cannot_recover_project(client, owner_token, db):
  actor = _authorization_context(db)
  project = _deleted_project(db)

  response = client.post(
    f"/api/projects/{project.id}/recover", headers=actor["delegated"],
  )

  assert response.status_code == 403
  db.expire_all()
  assert db.get(models.Project, project.id).deleted_at is not None


def test_owner_and_top_level_agent_can_recover_projects(client, auth, db):
  actor = _authorization_context(db)
  for headers in (auth, actor["top_level"]):
    project = _deleted_project(db)
    response = client.post(
      f"/api/projects/{project.id}/recover", headers=headers,
    )
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.get(models.Project, project.id).deleted_at is None
