"""A recovery receipt owns one deletion, including retry and history behavior."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app import models
from app.config import get_settings
from app.recovery_notifications import complete_recovery_action
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
  assert receipt.actions[0]["deleted_at"] == row.deleted_at.replace(tzinfo=UTC).isoformat()
  assert receipt.actions[0]["expires_at"] == (row.deleted_at + SOFT_DELETE_TTL).replace(tzinfo=UTC).isoformat()
  assert receipt.body == f"Receipt {kind}"
  return receipt


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


def test_expired_receipt_does_not_restore_its_tombstone(client, auth, db, resource):
  _, row, url = resource
  receipt = _delete(client, auth, db, resource)
  row.deleted_at = now_naive_utc() - timedelta(days=8)
  receipt.actions = [{
    **receipt.actions[0],
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
      completed_at = complete_recovery_action(
        other, owner_id=owner_id, notification_id=receipt.id,
        resource_type=kind, resource_id=str(row.id),
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
