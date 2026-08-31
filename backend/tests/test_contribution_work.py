"""Contracts for chat-attached, token-minimal contribution helpers."""

import asyncio

import pytest

from app import contribution_work, delegations, models
from app.routes import github as github_routes
from test_app_fixtures import create_local_app


github_routes._limiter.enabled = False


def _apps_and_source(client, auth, db):
  contribute_id = create_local_app(client, auth, name="Contribute")["id"]
  subagents_id = create_local_app(client, auth, name="Subagents")["id"]
  source = models.Chat(
    id="source-chat",
    title="Source",
    messages=[{"role": "user", "content": "TOP SECRET TRANSCRIPT SENTINEL"}],
    provider="codex",
    agent_settings_json={"model": "gpt-5.6-sol", "effort": "high"},
  )
  db.add(source)
  db.commit()
  return contribute_id, subagents_id, source


def _snapshot(revision="edit-1:/data/platform/backend/app/demo.py"):
  return {
    "unsorted_entries": [{
      "id": "edit-1",
      "ts": 1770000000000,
      "paths": ["/data/platform/backend/app/demo.py"],
    }],
    "unsorted_revision": revision,
    "workflow_revision": f"{revision}||",
    "record_views": [],
  }


def _auth(owner_token):
  return {"Authorization": f"Bearer {owner_token}"}


def test_active_source_accepts_exact_work_and_retry_attaches(
  client, owner_token, db, monkeypatch,
):
  auth = _auth(owner_token)
  app_id, subagents_id, source = _apps_and_source(client, auth, db)
  source_run = models.ChatRun(
    id="source-run",
    root_run_id="source-run",
    chat_id=source.id,
    status="running",
    provider="codex",
  )
  db.add(source_run)
  db.commit()

  snapshots = []

  async def fake_snapshot(_db, requested_app_id, requested_chat_id):
    snapshots.append((requested_app_id, requested_chat_id))
    return _snapshot()

  starts = []

  async def forbidden_start(*_args, **_kwargs):
    starts.append(True)
    raise AssertionError("active source work must not start its child")

  monkeypatch.setattr(github_routes, "_contribution_work_snapshot", fake_snapshot)
  monkeypatch.setattr(github_routes, "ensure_delegation_started", forbidden_start)
  request = {
    "intent": "prepare",
    "expected_revision": "edit-1:/data/platform/backend/app/demo.py",
    "record_ids": [],
  }

  first = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json=request,
  )
  assert first.status_code == 202, first.text
  first_work = first.json()["work"]
  assert set(first_work) == {
    "id", "intent", "status", "task_key", "child_chat_id", "result",
    "result_truncated", "created_at", "usage",
  }
  assert first_work["usage"] == {
    "coverage": {"runs": 0, "runs_with_usage": 0},
    "totals": {
      "input_tokens": None,
      "output_tokens": None,
      "cache_read_input_tokens": None,
      "cache_creation_input_tokens": None,
      "reasoning_output_tokens": None,
      "total_tokens": None,
    },
  }
  assert first_work["status"] == "accepted"
  assert first.json()["attached"] is False

  second = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json=request,
  )
  assert second.status_code == 202, second.text
  assert second.json()["attached"] is True
  assert second.json()["work"] == first_work
  assert snapshots == [(app_id, source.id)]
  assert starts == []

  db.expire_all()
  row = db.query(models.Delegation).one()
  assert row.app_id == subagents_id
  assert row.parent_chat_id == source.id
  assert row.provider == "codex"
  assert row.model == "gpt-5.6-sol"
  assert row.effort == "high"
  assert row.notify_parent_on_complete is False
  assert row.parent_woken_at is None
  assert row.source_work_active_chat_id == source.id
  assert set(row.source_work_envelope) == {
    "v", "intent", "source_chat_id", "edit_revision", "paths",
    "record_ids", "project_roots",
  }
  assert row.source_work_envelope["paths"] == [{
    "path": "/data/platform/backend/app/demo.py",
    "reviewed_through": 1770000000000,
  }]
  assert "/data/apps/contribute/attached-work.md" in row.startup_prompt
  assert "TOP SECRET TRANSCRIPT SENTINEL" not in row.startup_prompt
  assert "diff --git" not in row.startup_prompt
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == source.id,
  ).count() == 1
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == row.child_chat_id,
  ).count() == 0

  db.add(models.ChatRun(
    id="helper-measured-run",
    root_run_id="helper-measured-run",
    chat_id=row.child_chat_id,
    status="completed",
    provider="codex",
    input_tokens=800,
    output_tokens=200,
    total_tokens=1_000,
    usage_json={"provider": "codex"},
  ))
  db.commit()

  measured_work = delegations.serialize_source_work(db, row)
  assert measured_work["usage"]["coverage"] == {
    "runs": 1,
    "runs_with_usage": 1,
  }
  assert measured_work["usage"]["totals"]["total_tokens"] == 1_000
  db.query(models.ChatRun).filter(
    models.ChatRun.id == "helper-measured-run",
  ).delete()
  db.commit()

  projected = client.get(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}", headers=auth,
  )
  assert projected.status_code == 200, projected.text
  assert projected.json()["work"]["id"] == first_work["id"]
  assert projected.json()["work"]["status"] == "accepted"
  assert projected.json()["work_history_count"] == 1

  history = client.get(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work/history",
    headers=auth,
  )
  assert history.status_code == 200, history.text
  assert [item["id"] for item in history.json()["items"]] == [first_work["id"]]
  assert history.json()["items"][0]["usage"]["totals"]["total_tokens"] is None
  assert history.json()["total"] == 1
  assert history.json()["truncated"] is False


def test_accepted_work_starts_once_after_source_settles(
  client, owner_token, db, monkeypatch,
):
  auth = _auth(owner_token)
  app_id, _subagents_id, source = _apps_and_source(client, auth, db)
  source_run = models.ChatRun(
    id="source-run",
    root_run_id="source-run",
    chat_id=source.id,
    status="running",
    provider="codex",
  )
  db.add(source_run)
  db.commit()

  async def fake_snapshot(_db, _app_id, _chat_id):
    return _snapshot()

  starts = []

  async def fake_start(start_db, row):
    starts.append(row.id)
    start_db.add(models.ChatRun(
      id="child-run",
      root_run_id="child-run",
      chat_id=row.child_chat_id,
      status="running",
      provider=row.provider,
    ))
    row.startup_prompt = None
    row.source_work_status = None
    row.source_work_result = None
    start_db.commit()
    return True

  monkeypatch.setattr(github_routes, "_contribution_work_snapshot", fake_snapshot)
  monkeypatch.setattr(github_routes, "ensure_delegation_started", fake_start)
  accepted = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json={"intent": "prepare", "expected_revision": "", "record_ids": []},
  )
  assert accepted.status_code == 202, accepted.text
  assert accepted.json()["work"]["status"] == "accepted"
  assert starts == []

  # Empty client revisions are canonicalized under the source lock and their
  # retry attaches to that same live selector.
  db.expire_all()
  row = db.query(models.Delegation).one()
  assert row.source_work_envelope["edit_revision"] == (
    "edit-1:/data/platform/backend/app/demo.py"
  )
  retry = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json={"intent": "prepare", "expected_revision": "", "record_ids": []},
  )
  assert retry.status_code == 202, retry.text
  assert retry.json()["attached"] is True
  assert retry.json()["work"]["id"] == accepted.json()["work"]["id"]

  source_run.status = "completed"
  db.commit()
  assert asyncio.run(
    github_routes.reconcile_attached_contribution_work(source.id)
  ) == 1
  assert starts == [row.id]
  assert asyncio.run(
    github_routes.reconcile_attached_contribution_work(source.id)
  ) == 0
  assert starts == [row.id]

  db.expire_all()
  row = db.query(models.Delegation).one()
  assert row.source_work_status is None
  assert row.source_work_active_chat_id == source.id
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == source.id,
  ).count() == 1
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == row.child_chat_id,
  ).count() == 1


def test_deferred_work_with_changed_revision_needs_review_without_start(
  client, owner_token, db, monkeypatch,
):
  auth = _auth(owner_token)
  app_id, _subagents_id, source = _apps_and_source(client, auth, db)
  source_run = models.ChatRun(
    id="source-run",
    root_run_id="source-run",
    chat_id=source.id,
    status="running",
    provider="codex",
  )
  db.add(source_run)
  db.commit()
  current = {"snapshot": _snapshot()}

  async def fake_snapshot(_db, _app_id, _chat_id):
    return current["snapshot"]

  starts = []

  async def forbidden_start(*_args, **_kwargs):
    starts.append(True)
    raise AssertionError("stale source work must not start")

  monkeypatch.setattr(github_routes, "_contribution_work_snapshot", fake_snapshot)
  monkeypatch.setattr(github_routes, "ensure_delegation_started", forbidden_start)
  accepted = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json={
      "intent": "prepare",
      "expected_revision": "edit-1:/data/platform/backend/app/demo.py",
      "record_ids": [],
    },
  )
  assert accepted.status_code == 202, accepted.text
  current["snapshot"] = _snapshot(
    "edit-2:/data/platform/backend/app/demo.py"
  )
  current["snapshot"]["unsorted_entries"][0]["id"] = "edit-2"
  source_run.status = "completed"
  db.commit()

  assert asyncio.run(
    github_routes.reconcile_attached_contribution_work(source.id)
  ) == 0
  assert starts == []
  db.expire_all()
  row = db.query(models.Delegation).one()
  assert row.source_work_status == "needs_review"
  assert row.source_work_active_chat_id is None
  assert row.startup_prompt is None
  assert "source changed" in row.source_work_result.lower()
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == row.child_chat_id,
  ).count() == 0

  projected = client.get(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}", headers=auth,
  )
  assert projected.status_code == 200, projected.text
  assert projected.json()["work"]["status"] == "needs_review"


def test_idle_stale_private_revision_binds_to_current_source_once(
  client, owner_token, db, monkeypatch,
):
  auth = _auth(owner_token)
  app_id, _subagents_id, source = _apps_and_source(client, auth, db)

  async def fake_snapshot(_db, _app_id, _chat_id):
    return _snapshot()

  async def accept_start(_delegation_id):
    return True

  monkeypatch.setattr(github_routes, "_contribution_work_snapshot", fake_snapshot)
  monkeypatch.setattr(
    github_routes, "_start_attached_contribution_work", accept_start,
  )
  response = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json={"intent": "prepare", "expected_revision": "obsolete", "record_ids": []},
  )
  assert response.status_code == 202, response.text
  row = db.query(models.Delegation).one()
  assert row.source_work_envelope["edit_revision"] == (
    "edit-1:/data/platform/backend/app/demo.py"
  )


def test_immediate_start_failure_still_returns_durable_accepted_work(
  client, owner_token, db, monkeypatch,
):
  auth = _auth(owner_token)
  app_id, _subagents_id, source = _apps_and_source(client, auth, db)

  async def fake_snapshot(_db, _app_id, _chat_id):
    return _snapshot()

  async def failed_start(_delegation_id):
    raise RuntimeError("temporary provider admission failure")

  monkeypatch.setattr(github_routes, "_contribution_work_snapshot", fake_snapshot)
  monkeypatch.setattr(
    github_routes, "_start_attached_contribution_work", failed_start,
  )
  response = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json={
      "intent": "prepare",
      "expected_revision": "edit-1:/data/platform/backend/app/demo.py",
      "record_ids": [],
    },
  )

  assert response.status_code == 202, response.text
  assert response.json()["work"]["status"] == "accepted"
  db.expire_all()
  row = db.query(models.Delegation).one()
  assert row.source_work_status == "accepted"
  assert row.startup_prompt
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == row.child_chat_id,
  ).count() == 0


def test_transient_prestart_snapshot_failure_retries_then_starts_same_job(
  client, owner_token, db, monkeypatch,
):
  auth = _auth(owner_token)
  app_id, _subagents_id, source = _apps_and_source(client, auth, db)
  source_run = models.ChatRun(
    id="source-run",
    root_run_id="source-run",
    chat_id=source.id,
    status="running",
    provider="codex",
  )
  db.add(source_run)
  db.commit()
  fail_next_snapshot = {"value": False}

  async def sometimes_failing_snapshot(_db, _app_id, _chat_id):
    if fail_next_snapshot["value"]:
      fail_next_snapshot["value"] = False
      raise RuntimeError("temporary chat barrier failure")
    return _snapshot()

  starts = []

  async def successful_start(start_db, row):
    starts.append(row.id)
    start_db.add(models.ChatRun(
      id="child-run",
      root_run_id="child-run",
      chat_id=row.child_chat_id,
      status="running",
      provider=row.provider,
    ))
    row.startup_prompt = None
    row.source_work_status = None
    row.source_work_result = None
    start_db.commit()
    return True

  monkeypatch.setattr(
    github_routes, "_contribution_work_snapshot", sometimes_failing_snapshot,
  )
  monkeypatch.setattr(github_routes, "ensure_delegation_started", successful_start)
  accepted = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json={
      "intent": "prepare",
      "expected_revision": "edit-1:/data/platform/backend/app/demo.py",
      "record_ids": [],
    },
  )
  assert accepted.status_code == 202, accepted.text
  work_id = accepted.json()["work"]["id"]

  source_run.status = "completed"
  db.commit()
  fail_next_snapshot["value"] = True
  assert asyncio.run(
    github_routes.reconcile_attached_contribution_work(source.id)
  ) == 0
  db.expire_all()
  row = db.query(models.Delegation).one()
  assert row.source_work_id == work_id
  assert row.source_work_status == "retrying"
  assert "retry it automatically" in row.source_work_result
  assert row.source_work_active_chat_id == source.id
  assert starts == []

  assert asyncio.run(
    github_routes.reconcile_attached_contribution_work(source.id)
  ) == 1
  db.expire_all()
  row = db.query(models.Delegation).one()
  assert starts == [row.id]
  assert row.source_work_id == work_id
  assert row.source_work_status is None
  assert row.source_work_result is None
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == row.child_chat_id,
  ).count() == 1


def test_repeated_prestart_admission_failures_become_actionable_attention(
  client, owner_token, db, monkeypatch,
):
  auth = _auth(owner_token)
  app_id, _subagents_id, source = _apps_and_source(client, auth, db)
  source_run = models.ChatRun(
    id="source-run",
    root_run_id="source-run",
    chat_id=source.id,
    status="running",
    provider="codex",
  )
  db.add(source_run)
  db.commit()

  async def fake_snapshot(_db, _app_id, _chat_id):
    return _snapshot()

  attempts = []

  async def persistently_failing_start(_start_db, row):
    attempts.append(row.id)
    raise RuntimeError("PRIVATE PROVIDER DIAGNOSTIC")

  monkeypatch.setattr(github_routes, "_contribution_work_snapshot", fake_snapshot)
  monkeypatch.setattr(
    github_routes, "ensure_delegation_started", persistently_failing_start,
  )
  accepted = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json={
      "intent": "prepare",
      "expected_revision": "edit-1:/data/platform/backend/app/demo.py",
      "record_ids": [],
    },
  )
  assert accepted.status_code == 202, accepted.text

  source_run.status = "completed"
  db.commit()
  assert asyncio.run(
    github_routes.reconcile_attached_contribution_work(source.id)
  ) == 0
  db.expire_all()
  row = db.query(models.Delegation).one()
  assert row.source_work_status == "retrying"
  assert row.source_work_active_chat_id == source.id

  assert asyncio.run(
    github_routes.reconcile_attached_contribution_work(source.id)
  ) == 0
  db.expire_all()
  row = db.query(models.Delegation).one()
  assert attempts == [row.id, row.id]
  assert row.source_work_status == "needs_review"
  assert row.source_work_active_chat_id is None
  assert row.startup_prompt is None
  assert "current contribution action" in row.source_work_result
  assert "PRIVATE PROVIDER DIAGNOSTIC" not in row.source_work_result
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == row.child_chat_id,
  ).count() == 0
  db.refresh(source)
  assert source.messages == [{
    "role": "user", "content": "TOP SECRET TRANSCRIPT SENTINEL",
  }]

  projected = client.get(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}", headers=auth,
  )
  assert projected.status_code == 200, projected.text
  assert projected.json()["work"]["status"] == "needs_review"
  assert "current contribution action" in projected.json()["work"]["result"]

  original_work_id = row.source_work_id

  async def successful_start(start_db, retry_row):
    start_db.add(models.ChatRun(
      id=f"retry-run-{retry_row.id}",
      root_run_id=f"retry-run-{retry_row.id}",
      chat_id=retry_row.child_chat_id,
      status="running",
      provider="codex",
    ))
    retry_row.startup_prompt = None
    retry_row.source_work_status = None
    retry_row.source_work_result = None
    start_db.commit()
    return True

  monkeypatch.setattr(
    github_routes, "ensure_delegation_started", successful_start,
  )
  retry_body = {
    "intent": "prepare",
    "expected_revision": "edit-1:/data/platform/backend/app/demo.py",
    "record_ids": [],
    "retry_of": original_work_id,
  }
  retried = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json=retry_body,
  )
  assert retried.status_code == 202, retried.text
  retry_work_id = retried.json()["work"]["id"]
  assert retry_work_id != original_work_id
  assert retried.json()["work"]["status"] == "running"
  assert db.query(models.Delegation).count() == 2

  duplicate = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json=retry_body,
  )
  assert duplicate.status_code == 202, duplicate.text
  assert duplicate.json()["work"]["id"] == retry_work_id
  assert db.query(models.Delegation).count() == 2
  db.refresh(source)
  assert source.messages == [{
    "role": "user", "content": "TOP SECRET TRANSCRIPT SENTINEL",
  }]


def test_false_start_without_child_run_counts_as_prestart_failure(
  client, owner_token, db, monkeypatch,
):
  auth = _auth(owner_token)
  app_id, _subagents_id, source = _apps_and_source(client, auth, db)
  source_run = models.ChatRun(
    id="source-run",
    root_run_id="source-run",
    chat_id=source.id,
    status="running",
    provider="codex",
  )
  db.add(source_run)
  db.commit()

  async def fake_snapshot(_db, _app_id, _chat_id):
    return _snapshot()

  attempts = []

  async def declined_start(_start_db, row):
    attempts.append(row.id)
    return False

  monkeypatch.setattr(github_routes, "_contribution_work_snapshot", fake_snapshot)
  monkeypatch.setattr(github_routes, "ensure_delegation_started", declined_start)
  accepted = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json={
      "intent": "prepare",
      "expected_revision": "edit-1:/data/platform/backend/app/demo.py",
      "record_ids": [],
    },
  )
  assert accepted.status_code == 202, accepted.text

  source_run.status = "completed"
  db.commit()
  assert asyncio.run(
    github_routes.reconcile_attached_contribution_work(source.id)
  ) == 0
  db.expire_all()
  row = db.query(models.Delegation).one()
  assert attempts == [row.id]
  assert row.source_work_status == "retrying"
  assert row.source_work_active_chat_id == source.id
  assert db.query(models.ChatRun).filter(
    models.ChatRun.chat_id == row.child_chat_id,
  ).count() == 0

  assert asyncio.run(
    github_routes.reconcile_attached_contribution_work(source.id)
  ) == 0
  db.expire_all()
  row = db.query(models.Delegation).one()
  assert attempts == [row.id, row.id]
  assert row.source_work_status == "needs_review"
  assert row.source_work_active_chat_id is None
  assert row.startup_prompt is None


def test_reconcile_isolates_one_failed_start_from_other_source_chats(
  client, owner_token, db, monkeypatch,
):
  auth = _auth(owner_token)
  app_id, _subagents_id, first_source = _apps_and_source(client, auth, db)
  second_source = models.Chat(
    id="second-source-chat",
    title="Second source",
    messages=[],
    provider="codex",
    agent_settings_json={"model": "gpt-5.6-sol", "effort": "high"},
  )
  first_run = models.ChatRun(
    id="first-source-run",
    root_run_id="first-source-run",
    chat_id=first_source.id,
    status="running",
    provider="codex",
  )
  second_run = models.ChatRun(
    id="second-source-run",
    root_run_id="second-source-run",
    chat_id=second_source.id,
    status="running",
    provider="codex",
  )
  db.add_all((second_source, first_run, second_run))
  db.commit()

  async def fake_snapshot(_db, _app_id, chat_id):
    value = _snapshot()
    value["unsorted_entries"][0]["id"] = f"edit-{chat_id}"
    value["unsorted_revision"] = (
      f"edit-{chat_id}:/data/platform/backend/app/demo.py"
    )
    value["workflow_revision"] = f"{value['unsorted_revision']}||"
    return value

  monkeypatch.setattr(github_routes, "_contribution_work_snapshot", fake_snapshot)
  for source in (first_source, second_source):
    revision = f"edit-{source.id}:/data/platform/backend/app/demo.py"
    accepted = client.post(
      f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
      headers=auth,
      json={"intent": "prepare", "expected_revision": revision, "record_ids": []},
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["work"]["status"] == "accepted"

  first_run.status = "completed"
  second_run.status = "completed"
  db.commit()
  attempts = []

  async def isolated_start(delegation_id, **_kwargs):
    attempts.append(delegation_id)
    if len(attempts) == 1:
      raise RuntimeError("one provider is temporarily unavailable")
    return True

  monkeypatch.setattr(contribution_work, "start_attached", isolated_start)

  assert asyncio.run(contribution_work.reconcile(
    snapshot_loader=fake_snapshot,
  )) == 1
  assert len(attempts) == 2


def test_record_action_revision_matches_frontend_review_action_key():
  body = github_routes.ContributionWorkBody(
    intent="updates",
    expected_revision="one:stable-a|two:stable-b",
    record_ids=["two", "one"],
  )
  snapshot = {
    "record_views": [
      {"id": "one", "action_key": "stable-a"},
      {"id": "two", "action_key": "stable-b"},
    ],
  }
  assert github_routes._work_revision(snapshot, body) == body.expected_revision


@pytest.mark.parametrize("hostile_root", [
  "/data/shared/memory",
  "/data/cli-auth/codex",
  "/data/contrib/private/worktree",
  "/data/apps/123",
  "/data/apps/safe/../../shared",
])
def test_record_work_rejects_hostile_agent_authored_source_roots(
  client, owner_token, db, monkeypatch, hostile_root,
):
  auth = _auth(owner_token)
  app_id, _subagents_id, source = _apps_and_source(client, auth, db)
  snapshot = _snapshot()
  snapshot["unsorted_entries"] = [{
    "id": "private-edit",
    "ts": 1770000000000,
    "paths": ["/data/shared/private.md"],
  }]
  snapshot["record_views"] = [{
    "id": "review-one",
    "action_key": "exact-review",
    "source_root": hostile_root,
  }]
  snapshot["unsorted_revision"] = "private-edit:/data/shared/private.md"
  snapshot["workflow_revision"] = "private"

  async def fake_snapshot(_db, _app_id, _chat_id):
    return snapshot

  monkeypatch.setattr(github_routes, "_contribution_work_snapshot", fake_snapshot)
  response = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json={
      "intent": "followup",
      "expected_revision": "review-one:exact-review",
      "record_ids": ["review-one"],
    },
  )

  assert response.status_code == 409, response.text
  assert db.query(models.Delegation).count() == 0
  envelope = contribution_work.envelope(
    source.id,
    contribution_work.ContributionWorkBody(
      intent="followup",
      expected_revision="review-one:exact-review",
      record_ids=["review-one"],
    ),
    snapshot,
  )
  assert envelope["paths"] == []
  assert envelope["project_roots"] == []


def test_project_root_boundary_normalizes_only_platform_and_app_sources():
  assert contribution_work.project_root("/data/platform/") == "/data/platform"
  assert contribution_work.project_root(
    "/data/apps/my-app/src/main.js",
  ) == "/data/apps/my-app"
  for value in (
    "", "/data", "/data/apps/123", "/data/apps/..",
    "/data/apps/my-app/../other", "/data/shared", "/data/cli-auth",
  ):
    assert contribution_work.project_root(value) == ""
  assert github_routes._chat_review_projection({
    "id": "hostile",
    "plan": {"source_repo_path": "/data/shared/memory"},
  }, 80)["source_root"] == ""


def test_source_work_policy_allows_its_one_required_workflow_skill(
  client, owner_token, db, monkeypatch,
):
  auth = _auth(owner_token)
  app_id, _subagents_id, source = _apps_and_source(client, auth, db)
  source_run = models.ChatRun(
    id="source-policy-run",
    root_run_id="source-policy-run",
    chat_id=source.id,
    status="running",
    provider="codex",
  )
  db.add(source_run)
  db.commit()

  async def fake_snapshot(_db, _app_id, _chat_id):
    return _snapshot()

  monkeypatch.setattr(github_routes, "_contribution_work_snapshot", fake_snapshot)
  response = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json={
      "intent": "prepare",
      "expected_revision": "edit-1:/data/platform/backend/app/demo.py",
      "record_ids": [],
    },
  )
  assert response.status_code == 202, response.text

  row = db.query(models.Delegation).one()
  policy = delegations.policy_for_chat(db, row.child_chat_id)
  assert policy is not None
  assert policy.allowed_skill_paths == (
    "/data/apps/contribute/attached-work.md",
  )
  assembled = f"{policy.system_prompt}\n\n{row.startup_prompt}"
  assert "read the complete required playbook" in assembled
  assert "Read and follow /data/apps/contribute/attached-work.md" in assembled
  assert "Do not inspect unrelated chats, Memory, skills" in assembled


def test_owner_can_stop_prestart_contribution_work_and_release_its_lease(
  client, owner_token, db, monkeypatch,
):
  auth = _auth(owner_token)
  app_id, _subagents_id, source = _apps_and_source(client, auth, db)
  source_run = models.ChatRun(
    id="source-stop-run",
    root_run_id="source-stop-run",
    chat_id=source.id,
    status="running",
    provider="codex",
  )
  db.add(source_run)
  db.commit()

  async def fake_snapshot(_db, _app_id, _chat_id):
    return _snapshot()

  monkeypatch.setattr(github_routes, "_contribution_work_snapshot", fake_snapshot)
  accepted = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json={
      "intent": "prepare",
      "expected_revision": "edit-1:/data/platform/backend/app/demo.py",
      "record_ids": [],
    },
  )
  assert accepted.status_code == 202, accepted.text

  stopped = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work/stop",
    headers=auth,
  )
  assert stopped.status_code == 200, stopped.text
  assert stopped.json()["stopped"] is True
  assert stopped.json()["work"]["status"] == "cancelled"
  db.expire_all()
  row = db.query(models.Delegation).one()
  assert row.cancelled_at is not None
  assert row.source_work_active_chat_id is None


@pytest.mark.parametrize("terminal", ["completed", "failed"])
def test_hidden_source_work_terminal_persists_one_owner_notification(
  client, owner_token, db, monkeypatch, terminal,
):
  auth = _auth(owner_token)
  app_id, _subagents_id, source = _apps_and_source(client, auth, db)
  source_run = models.ChatRun(
    id=f"source-notify-{terminal}",
    root_run_id=f"source-notify-{terminal}",
    chat_id=source.id,
    status="running",
    provider="codex",
  )
  db.add(source_run)
  db.commit()

  async def fake_snapshot(_db, _app_id, _chat_id):
    return _snapshot()

  monkeypatch.setattr(github_routes, "_contribution_work_snapshot", fake_snapshot)
  accepted = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{source.id}/work",
    headers=auth,
    json={
      "intent": "prepare",
      "expected_revision": "edit-1:/data/platform/backend/app/demo.py",
      "record_ids": [],
    },
  )
  assert accepted.status_code == 202, accepted.text
  row = db.query(models.Delegation).one()
  db.add(models.ChatRun(
    id=f"child-notify-{terminal}",
    root_run_id=f"child-notify-{terminal}",
    chat_id=row.child_chat_id,
    status=terminal,
    provider="codex",
  ))
  db.commit()

  asyncio.run(delegations.wake_parent_after_child_settled(row.child_chat_id))
  asyncio.run(delegations.wake_parent_after_child_settled(row.child_chat_id))

  db.expire_all()
  row = db.query(models.Delegation).one()
  notifications = db.query(models.Notification).all()
  assert row.parent_woken_at is not None
  assert row.source_work_active_chat_id is None
  assert len(notifications) == 1
  assert notifications[0].source_type == "agent"
  assert notifications[0].source_id == source.id
  assert notifications[0].target == f"/shell/?chat={source.id}"
  assert (
    "finished" if terminal == "completed" else "needs attention"
  ) in notifications[0].title.lower()
