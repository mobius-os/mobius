"""A recovery receipt owns one deletion, including retry and history behavior."""

import asyncio
import math
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app import models
from app.config import get_settings
from app.recovery_notifications import (
  complete_recovery_action,
  recovery_resource_generation,
)
from app.timeutil import SOFT_DELETE_TTL, now_naive_utc


@pytest.fixture(params=["chat", "app", "project"])
def resource(request, db, auth, monkeypatch):
  kind = request.param
  root = Path(get_settings().data_dir)
  if kind == "chat":
    row = models.Chat(id=str(uuid4()), title="Receipt chat")
  elif kind == "app":
    source = root / "apps" / "receipt-app"
    source.mkdir(parents=True)
    row = models.App(
      name="Receipt app", slug="receipt-app", source_dir=str(source),
      jsx_source="export default () => null",
    )
    monkeypatch.setattr("app.routes.apps.app_bundle_uses_current_compile_contract", lambda _: True)
    monkeypatch.setattr("app.routes.apps._revoke_app_publish_tokens", AsyncMock())
    monkeypatch.setattr("app.routes.apps.reconcile_app_cron_supervision", lambda _: (0, []))
    monkeypatch.setattr("app.install.restore_app_skills", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.install.deactivate_app_skills", AsyncMock(return_value=[]))
    monkeypatch.setattr("app.routes.apps._drop_cron_only", lambda _: None)
  else:
    project_id = str(uuid4())
    root_path = f"projects/{project_id}"
    (root / root_path).mkdir(parents=True)
    row = models.Project(
      id=project_id, name="Receipt project", project_type="blank",
      root_path=root_path, template_snapshot_json={},
    )
    db.add(models.Chat(
      id=str(uuid4()), title="Receipt project chat", project_id=project_id,
    ))
  db.add(row)
  db.commit()
  return kind, row, f"/api/{kind}s/{row.id}"


def _delete(client, auth, db, resource):
  kind, row, url = resource
  response = client.delete(url, headers=auth)
  assert response.status_code == 204, response.text
  receipt_id = response.headers["X-Recovery-Notification-Id"]
  db.expire_all()
  receipt = db.get(models.Notification, receipt_id)
  generation_witness = row.token_nonce if kind == "app" else row.created_at
  assert receipt.actions[0]["resource_generation"] == (
    recovery_resource_generation(kind, generation_witness)
  )
  assert receipt.actions[0]["deleted_at"] == row.deleted_at.replace(tzinfo=UTC).isoformat()
  assert receipt.actions[0]["expires_at"] == (row.deleted_at + SOFT_DELETE_TTL).replace(tzinfo=UTC).isoformat()
  assert receipt.body == f"Receipt {kind}"
  return receipt


@pytest.mark.parametrize("resource", ["app"], indirect=True)
def test_app_receipt_uses_the_app_lifecycle_expiry(
  client, auth, db, resource, monkeypatch,
):
  _, row, url = resource
  app_ttl = timedelta(hours=12)
  monkeypatch.setattr("app.routes.apps.APP_SOFT_DELETE_TTL", app_ttl)

  response = client.delete(url, headers=auth)

  assert response.status_code == 204, response.text
  db.expire_all()
  receipt = db.get(
    models.Notification, response.headers["X-Recovery-Notification-Id"],
  )
  assert receipt.actions[0]["expires_at"] == (
    row.deleted_at + app_ttl
  ).replace(tzinfo=UTC).isoformat()


@pytest.mark.parametrize("kind", ["chat", "project"])
def test_running_resource_delete_keeps_receipt_bound_to_owner(
  client, auth, db, monkeypatch, kind,
):
  """Stop-result throwaways must not replace the authenticated owner."""
  owner = db.query(models.Owner).one()
  chat = models.Chat(id=str(uuid4()), title="Running recovery chat")
  if kind == "chat":
    row = chat
    url = f"/api/chats/{chat.id}"
  else:
    project_id = str(uuid4())
    root_path = f"projects/{project_id}"
    (Path(get_settings().data_dir) / root_path).mkdir(parents=True)
    row = models.Project(
      id=project_id,
      name="Running recovery project",
      project_type="blank",
      root_path=root_path,
      template_snapshot_json={},
    )
    chat.project_id = project_id
    url = f"/api/projects/{project_id}"
    db.add(row)
  db.add(chat)
  db.commit()

  route = import_module(f"app.routes.{kind}s")
  stop = AsyncMock(return_value=(True, ["cleared-pending-cid"]))
  finish = AsyncMock()
  running_checks = iter([True, False])
  monkeypatch.setattr(
    route, "is_chat_running", lambda _chat_id: next(running_checks, False),
  )
  monkeypatch.setattr(route, "stop_chat_for", stop)
  monkeypatch.setattr(route, "_finish_run", finish)

  response = client.delete(url, headers=auth)

  assert response.status_code == 204, response.text
  receipt_id = response.headers["X-Recovery-Notification-Id"]
  stop.assert_awaited_once()
  finish.assert_awaited_once_with(chat.id, terminal_status="stopped")
  db.expire_all()
  deleted_at = db.get(type(row), row.id).deleted_at
  assert deleted_at is not None
  receipt = db.get(models.Notification, receipt_id)
  assert receipt.owner_id == owner.id

  # A lost-response retry may re-enter chat cleanup or discover the project is
  # already absent, but it must never move the tombstone or mint a new receipt.
  retry = client.delete(url, headers=auth)
  assert retry.status_code == (204 if kind == "chat" else 404), retry.text
  db.expire_all()
  assert db.get(type(row), row.id).deleted_at == deleted_at
  assert db.get(models.Notification, receipt_id) is not None
  assert db.query(models.Notification).filter(
    models.Notification.title == f"{kind.capitalize()} deleted",
  ).count() == 1


@pytest.mark.parametrize("complete_first_receipt", [False, True])
def test_old_receipt_cannot_restore_a_later_deletion(
  client, auth, db, resource, complete_first_receipt,
):
  _, row, url = resource
  first = _delete(client, auth, db, resource)
  first_payload = {"notification_id": first.id}
  recovered = client.post(
    f"{url}/recover", headers=auth,
    json=first_payload if complete_first_receipt else None,
  )
  assert recovered.status_code == 200, recovered.text
  second = _delete(client, auth, db, resource)
  assert second.id != first.id
  result = client.post(f"{url}/recover", headers=auth, json=first_payload)
  assert result.status_code == 409, result.text
  assert result.json()["detail"]["code"] == "recovery_superseded"
  db.refresh(row)
  assert row.deleted_at is not None
  result = client.post(f"{url}/recover", headers=auth, json={"notification_id": second.id})
  assert result.status_code == 200, result.text


@pytest.mark.parametrize("complete_receipt", [False, True])
def test_receipt_cannot_touch_a_recreated_same_id_resource(
  client, auth, db, resource, complete_receipt,
):
  """Generation, not only id/tombstone time, owns recovery convergence."""
  kind, row, url = resource
  receipt = _delete(client, auth, db, resource)
  original_deleted_at = row.deleted_at
  if complete_receipt:
    recovered = client.post(
      f"{url}/recover", headers=auth, json={"notification_id": receipt.id},
    )
    assert recovered.status_code == 200, recovered.text

  db.expire_all()
  current = db.get(type(row), row.id)
  # App ids are deliberately reusable. Keep the successor timestamp identical
  # so this regression proves the app-owned random generation, not timestamp
  # uniqueness, is what fences the old receipt.
  next_created_at = (
    current.created_at
    if kind == "app"
    else current.created_at + timedelta(seconds=1)
  )
  if kind == "project":
    db.query(models.Chat).filter(models.Chat.project_id == row.id).delete(
      synchronize_session=False,
    )
  db.delete(current)
  db.commit()

  if kind == "chat":
    replacement = models.Chat(
      id=str(row.id), title="Recreated receipt chat", created_at=next_created_at,
    )
  elif kind == "app":
    replacement = models.App(
      id=row.id, name="Recreated receipt app", slug="receipt-app",
      source_dir=str(Path(get_settings().data_dir) / "apps" / "receipt-app"),
      jsx_source="export default () => null", created_at=next_created_at,
    )
  else:
    replacement = models.Project(
      id=str(row.id), name="Recreated receipt project", project_type="blank",
      root_path=f"projects/{row.id}", template_snapshot_json={},
      created_at=next_created_at,
    )
  # Reuse the original tombstone time too: neither part of the old compound
  # identity may authorize a successor row. Completed retries exercise the
  # post-commit cleanup path against a live successor.
  expected_deleted_at = None if complete_receipt else original_deleted_at
  replacement.deleted_at = expected_deleted_at
  db.add(replacement)
  db.commit()

  result = client.post(
    f"{url}/recover", headers=auth, json={"notification_id": receipt.id},
  )
  assert result.status_code == 409, result.text
  assert result.json()["detail"]["code"] == "recovery_superseded"
  db.expire_all()
  assert db.get(type(replacement), replacement.id).deleted_at == expected_deleted_at


def test_expired_receipt_does_not_restore_its_tombstone(client, auth, db, resource):
  _, row, url = resource
  receipt = _delete(client, auth, db, resource)
  row.deleted_at = now_naive_utc() - timedelta(days=8)
  if resource[0] != "app":
    row.created_at = row.deleted_at - timedelta(seconds=1)
  generation_witness = row.token_nonce if resource[0] == "app" else row.created_at
  receipt.actions = [{
    **receipt.actions[0],
    "resource_generation": recovery_resource_generation(
      resource[0], generation_witness,
    ),
    "deleted_at": row.deleted_at.replace(tzinfo=UTC).isoformat(),
    "expires_at": (row.deleted_at + SOFT_DELETE_TTL).replace(tzinfo=UTC).isoformat(),
  }]
  db.commit()
  result = client.post(f"{url}/recover", headers=auth, json={"notification_id": receipt.id})
  assert result.status_code == 410, result.text
  assert result.json()["detail"] == "Recovery window has expired."
  db.refresh(row)
  assert row.deleted_at is not None


def test_retry_reads_completion_after_obtaining_lifecycle_lock(
  client, auth, db, resource, monkeypatch,
):
  """Another recovery wins while this request waits to enter its owning lock."""
  kind, row, url = resource
  receipt = _delete(client, auth, db, resource)
  owner_id = db.query(models.Owner.id).scalar()
  completed_at = None

  def other_request_commits():
    nonlocal completed_at
    from app.database import SessionLocal
    with SessionLocal() as other:
      live = other.get(type(row), row.id)
      live.deleted_at = None
      if kind == "project":
        for project_chat in other.query(models.Chat).filter(
          models.Chat.project_id == row.id,
        ):
          project_chat.deleted_at = None
      completed_at = complete_recovery_action(
        other, owner_id=owner_id, notification_id=receipt.id,
        resource_type=kind, resource_id=str(row.id),
        resource_generation=recovery_resource_generation(
          kind, live.token_nonce if kind == "app" else live.created_at,
        ),
      )
      other.commit()

  @asynccontextmanager
  async def asynchronous_lock(*_args):
    other_request_commits()
    yield

  class SynchronousLock:
    def __enter__(self):
      other_request_commits()

    def __exit__(self, *_args):
      pass

  if kind == "app":
    monkeypatch.setattr("app.fs_locks.install_uninstall_lock", asynchronous_lock)
  elif kind == "chat":
    monkeypatch.setattr("app.chat_queue.get_transition_lock", asynchronous_lock)
  else:
    module = import_module("app.routes.projects")
    monkeypatch.setattr(module, "PROJECT_LIFECYCLE_LOCK", SynchronousLock())
  result = client.post(f"{url}/recover", headers=auth, json={"notification_id": receipt.id})
  assert result.status_code == 200, result.text
  assert datetime.fromisoformat(result.json()["completed_at"]) == completed_at
  if kind == "chat":
    from app.chat import current_run_generation
    assert math.isfinite(current_run_generation(str(row.id)))
  elif kind == "project":
    from app.chat import current_run_generation
    child_ids = [
      chat_id for (chat_id,) in db.query(models.Chat.id).filter(
        models.Chat.project_id == row.id,
      )
    ]
    assert child_ids
    assert all(math.isfinite(current_run_generation(chat_id)) for chat_id in child_ids)
  else:
    restore_skills = import_module("app.install").restore_app_skills
    assert restore_skills.await_count == 1


def test_receipt_retry_repairs_lost_projection_without_replacing_a_live_run(
  client, auth, db, resource, monkeypatch,
):
  from app import chat as chat_mod
  from app.chat_writer import StartTurn, get_writer

  kind, row, url = resource
  receipt = _delete(client, auth, db, resource)
  module = import_module(f"app.routes.{kind}s")
  broadcast = MagicMock()
  broadcast.publish.side_effect = RuntimeError("recovery projection unavailable")
  monkeypatch.setattr(module, "get_system_broadcast", lambda: broadcast)
  payload = {"notification_id": receipt.id}
  with pytest.raises(RuntimeError, match="recovery projection unavailable"):
    client.post(f"{url}/recover", headers=auth, json=payload)

  db.expire_all()
  assert row.deleted_at is None
  completed_at = receipt.actions[0]["completed_at"]
  if kind == "chat":
    chat_ids = [str(row.id)]
  elif kind == "project":
    chat_ids = [chat_id for (chat_id,) in db.query(models.Chat.id).filter(
      models.Chat.project_id == row.id,
    )]
  else:
    chat_ids = []
  generations = {}
  for chat_id in chat_ids:
    # The owner can begin a successor after the database commit even though
    # the first request failed to publish every post-commit projection.
    assert math.isfinite(chat_mod.current_run_generation(chat_id))
    generations[chat_id] = chat_mod.bump_run_generation(chat_id)
    assert chat_mod.mark_starting(chat_id)
    get_writer().submit(StartTurn(
      chat_id=chat_id, run_token=f"successor-{chat_id}",
      user_msg={"role": "user", "content": "After recovery", "ts": 1},
      title_source="After recovery",
    )).result(timeout=5)

  broadcast.publish.reset_mock()
  broadcast.publish.side_effect = None
  recovered = client.post(f"{url}/recover", headers=auth, json=payload)
  assert recovered.status_code == 200, recovered.text
  assert recovered.json()["completed_at"] == completed_at
  db.expire_all()
  assert row.deleted_at is None
  assert receipt.actions[0]["completed_at"] == completed_at
  assert db.query(models.Notification).count() == 1
  expected = [
    (({"type": "chat_recovered", "chatId": chat_id},), {})
    for chat_id in chat_ids
  ]
  if kind == "project":
    expected.append((({
      "type": "project_recovered", "projectId": str(row.id), "chatIds": chat_ids,
    },), {}))
  elif kind == "app":
    expected.append((({"type": "app_recovered", "appId": str(row.id)},), {}))
    assert import_module("app.install").restore_app_skills.await_count == 1
  assert broadcast.publish.call_args_list == expected
  for chat_id in chat_ids:
    assert chat_mod.current_run_generation(chat_id) == generations[chat_id]
    assert db.get(models.ChatRun, f"successor-{chat_id}").status == "running"


def test_recovery_remains_reachable_after_more_than_one_history_page(client, auth, db, chat):
  deleted = client.delete(f"/api/chats/{chat.id}", headers=auth)
  receipt_id = deleted.headers["X-Recovery-Notification-Id"]
  owner_id = db.query(models.Owner.id).scalar()
  for index in range(19):
    db.add(models.Notification(
      id=str(uuid4()), owner_id=owner_id, title=f"Newer notification {index}",
      source_type="shell", sent_at=now_naive_utc(),
    ))
  db.commit()
  seen = []
  before = None
  while True:
    params = {"limit": 8}
    if before:
      params["before"] = before
    page = client.get("/api/notifications", headers=auth, params=params).json()
    seen.extend(row["id"] for row in page)
    if len(page) < 8:
      break
    before = page[-1]["id"]
  assert len(seen) == 20
  assert len(set(seen)) == 20
  assert seen[-1] == receipt_id
  result = client.post(
    f"/api/chats/{chat.id}/recover", headers=auth, json={"notification_id": receipt_id},
  )
  assert result.status_code == 200, result.text


def test_immediate_undo_receipt_is_readable_by_cross_origin_shell(client, auth, chat):
  response = client.delete(
    f"/api/chats/{chat.id}",
    headers={**auth, "Origin": get_settings().frontend_origin},
  )
  assert response.status_code == 204, response.text
  assert response.headers["X-Recovery-Notification-Id"]
  assert "X-Recovery-Notification-Id" in response.headers["Access-Control-Expose-Headers"]


@pytest.mark.parametrize("kind,pause_index", [
  ("chat", 0), ("project", 0), ("project", 1), ("project_child_retry", 0),
])
def test_recovery_waits_for_all_destructive_cleanup_before_starting_a_new_run(
  client, auth, db, monkeypatch, kind, pause_index,
):
  """Pause an actual FinishRun ack, not only the tombstone's DB transaction."""
  import httpx
  from app import chat as chat_mod, chat_queue, drawer_pins
  from app.chat_writer import FinishRun, StartTurn, get_writer
  from app.database import SessionLocal
  from app.main import app as application

  project_id = str(uuid4()) if kind != "chat" else None
  if project_id:
    root_path = f"projects/{project_id}"
    (Path(get_settings().data_dir) / root_path).mkdir(parents=True)
    db.add(models.Project(
      id=project_id, name="Cleanup ordering", project_type="blank",
      root_path=root_path, template_snapshot_json={},
    ))
  chat_ids = [str(uuid4()) for _ in range(2 if project_id else 1)]
  db.add_all(models.Chat(id=chat_id, project_id=project_id, title="Cleanup child") for chat_id in chat_ids)
  db.commit()
  before_generations = {chat_id: chat_mod.current_run_generation(chat_id) for chat_id in chat_ids}
  for chat_id in chat_ids:
    get_writer().submit(StartTurn(
      chat_id=chat_id, run_token=f"old-{chat_id}",
      user_msg={"role": "user", "content": "Before deletion", "ts": 1},
      title_source="Before deletion",
    )).result(timeout=5)

  url = f"/api/projects/{project_id}" if project_id else f"/api/chats/{chat_ids[0]}"
  lifecycle_key = f"project-lifecycle:{project_id}" if project_id else chat_ids[0]
  delete_url = url
  expected_cleanup_ids = chat_ids
  if kind == "project_child_retry":
    # An independent retry on a child already tombstoned by the project must
    # not keep tokenless cleanup alive past the project's subsequent Undo.
    initial_delete = client.delete(url, headers=auth)
    assert initial_delete.status_code == 204, initial_delete.text
    delete_url = f"/api/chats/{chat_ids[0]}"
    expected_cleanup_ids = [chat_ids[0]]
  route_kind = "chat" if kind == "project_child_retry" else kind
  module = import_module(f"app.routes.{route_kind}s")
  real_finish = module._finish_run
  real_get_lock = chat_queue.get_transition_lock

  async def race():
    cleanup_paused = asyncio.Event()
    release_cleanup = asyncio.Event()
    recovery_attempted = asyncio.Event()
    recovery_acquired = asyncio.Event()
    cleanup_order = []
    entrants = 0

    async def pause_finish(chat_id, **kwargs):
      cleanup_order.append(chat_id)
      if len(cleanup_order) - 1 == pause_index:
        cleanup_paused.set()
        await release_cleanup.wait()
      await real_finish(chat_id, **kwargs)

    @asynccontextmanager
    async def observe_lock(key):
      nonlocal entrants
      is_recovery = False
      if key == lifecycle_key:
        entrants += 1
        is_recovery = entrants == 2
        if is_recovery:
          recovery_attempted.set()
      async with real_get_lock(key):
        if is_recovery:
          recovery_acquired.set()
        yield

    monkeypatch.setattr(module, "_finish_run", pause_finish)
    monkeypatch.setattr(chat_queue, "get_transition_lock", observe_lock)
    async with httpx.AsyncClient(
      transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as ac:
      deleting = asyncio.create_task(ac.delete(delete_url, headers=auth))
      recovering = None
      try:
        await asyncio.wait_for(cleanup_paused.wait(), 5)
        with SessionLocal() as check:
          receipt = check.query(models.Notification).filter_by(title="Project deleted" if project_id else "Chat deleted").one()
          receipt_id = receipt.id
          assert not receipt.actions[0].get("completed_at")
          assert all(check.get(models.Chat, chat_id).deleted_at for chat_id in chat_ids)
          # No drawer or SQLite write lock is held while the async cleanup waits.
          assert drawer_pins._WRITE_LOCK.acquire(blocking=False)
          drawer_pins._WRITE_LOCK.release()
          check.add(models.Notification(
            id=str(uuid4()), owner_id=receipt.owner_id, source_type="shell",
            title="Independent write during cleanup", sent_at=now_naive_utc(),
          ))
          check.commit()
        recovering = asyncio.create_task(ac.post(
          f"{url}/recover", headers=auth, json={"notification_id": receipt_id},
        ))
        await asyncio.wait_for(recovery_attempted.wait(), 5)
        assert not recovery_acquired.is_set(), "Undo entered before deletion cleanup settled"
        assert not recovering.done()
        release_cleanup.set()
        deleted, recovered = await asyncio.wait_for(asyncio.gather(deleting, recovering), 5)
        assert deleted.status_code == 204, deleted.text
        assert recovered.status_code == 200, recovered.text
        assert set(cleanup_order) == set(expected_cleanup_ids)
        assert len(cleanup_order) == len(expected_cleanup_ids)
        with SessionLocal() as check:
          receipt = check.get(models.Notification, receipt_id)
          assert receipt.actions[0]["completed_at"] == recovered.json()["completed_at"]
          if project_id:
            assert check.get(models.Project, project_id).deleted_at is None
          for chat_id in chat_ids:
            assert check.get(models.Chat, chat_id).deleted_at is None
            assert check.get(models.ChatRun, f"old-{chat_id}").status == "stopped"
            generation = chat_mod.current_run_generation(chat_id)
            assert math.isfinite(generation)
            assert generation > before_generations[chat_id]
            assert chat_mod.mark_starting(chat_id)
            await asyncio.wrap_future(get_writer().submit(StartTurn(
              chat_id=chat_id, run_token=f"new-{chat_id}",
              user_msg={"role": "user", "content": "After recovery", "ts": 2},
              title_source="After recovery",
            )))
            check.expire_all()
            assert check.get(models.ChatRun, f"new-{chat_id}").status == "running"
            assert chat_mod.current_run_generation(chat_id) == generation
            await asyncio.wrap_future(get_writer().submit(FinishRun(
              chat_id=chat_id, run_token=f"new-{chat_id}",
            )))
            chat_mod.forget_chat(chat_id)
      finally:
        release_cleanup.set()
        await asyncio.gather(deleting, *([recovering] if recovering else []), return_exceptions=True)

  asyncio.run(race())


def test_follower_delivery_is_after_cleanup_and_outside_project_and_chat_gates(
  client, auth, db, chat, monkeypatch,
):
  import httpx
  from app import chat as chat_mod
  from app.agent_work_claims import ReleasedClaim
  from app.chat_writer import StartTurn, get_writer
  from app.main import app as application

  project_id = str(uuid4())
  db.add(models.Project(
    id=project_id, name="Follower ordering", project_type="blank",
    root_path=f"projects/{project_id}", template_snapshot_json={},
  ))
  chat.project_id = project_id
  db.commit()
  chat_id = chat.id
  get_writer().submit(StartTurn(
    chat_id=chat_id, run_token="before-followers",
    user_msg={"role": "user", "content": "Before deletion", "ts": 1},
    title_source="Before deletion",
  )).result(timeout=5)
  monkeypatch.setattr("app.agent_work_claims.stage_release_claims_for_chat", lambda *_: [
    ReleasedClaim("claim-id", "test:cleanup-followers", 2, ["follower-id"]),
  ])
  monkeypatch.setattr("app.agent_coordination.send_work_claim_notice", lambda *_args, **_kwargs: None)
  monkeypatch.setattr("app.agent_work_claims.acknowledge_notice", lambda *_args, **_kwargs: None)

  async def race():
    delivering = asyncio.Event()
    release_delivery = asyncio.Event()

    async def deliver(**_kwargs):
      assert chat_mod.current_run_generation(chat_id) == float("inf")
      db.expire_all()
      assert db.get(models.ChatRun, "before-followers").status == "stopped"
      delivering.set()
      await release_delivery.wait()

    monkeypatch.setattr("app.agent_coordination.deliver_peer_recipients", deliver)
    async with httpx.AsyncClient(
      transport=httpx.ASGITransport(app=application), base_url="http://test",
    ) as ac:
      deleting = asyncio.create_task(ac.delete(f"/api/chats/{chat_id}", headers=auth))
      try:
        await asyncio.wait_for(delivering.wait(), 5)
        receipt = db.query(models.Notification).filter_by(title="Chat deleted").one()
        recovered = await asyncio.wait_for(ac.post(
          f"/api/chats/{chat_id}/recover", headers=auth,
          json={"notification_id": receipt.id},
        ), 5)
        assert recovered.status_code == 200, recovered.text
        generation = chat_mod.current_run_generation(chat_id)
        assert math.isfinite(generation)
        assert chat_mod.mark_starting(chat_id)
        await asyncio.wrap_future(get_writer().submit(StartTurn(
          chat_id=chat_id, run_token="after-followers",
          user_msg={"role": "user", "content": "After recovery", "ts": 2},
          title_source="After recovery",
        )))
        release_delivery.set()
        deleted = await asyncio.wait_for(deleting, 5)
        assert deleted.status_code == 204, deleted.text
        db.expire_all()
        assert db.get(models.ChatRun, "after-followers").status == "running"
        assert chat_mod.current_run_generation(chat_id) == generation
      finally:
        release_delivery.set()
        await asyncio.gather(deleting, return_exceptions=True)
        chat_mod.forget_chat(chat_id)

  asyncio.run(race())
