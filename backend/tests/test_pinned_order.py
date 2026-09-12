"""Atomic ordering contract for the drawer's combined pinned list."""

import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from app import models
from app.routes import chats as chat_routes


def _seed_pinned_rows(db):
  base = datetime(2026, 7, 30, 1, 0, 0)
  chats = [
    models.Chat(
      id=f"chat-{index}",
      title=f"Chat {index}",
      messages=[],
      pinned_at=base + timedelta(seconds=index),
    )
    for index in (1, 2)
  ]
  apps = [
    models.App(
      source_dir=f"/tmp/mobius-tests/app-{index}",
      name=f"App {index}",
      description="",
      jsx_source="export default function App() {}",
      slug=f"app-{index}",
      pinned_at=base + timedelta(seconds=index + 2),
    )
    for index in (1, 2)
  ]
  projects = [
    models.Project(
      id=f"project-{index}",
      name=f"Project {index}",
      project_type="blank",
      root_path=f"projects/project-{index}",
      template_snapshot_json={},
    )
    for index in (1, 2)
  ]
  states = [
    models.ProjectDrawerState(
      project_id=project.id,
      pinned_at=base + timedelta(seconds=index + 4),
    )
    for index, project in enumerate(projects, start=1)
  ]
  db.add_all([*chats, *apps, *projects, *states])
  db.commit()
  for row in [*chats, *apps, *states]:
    db.refresh(row)
  return chats, apps, states


def test_combined_pinned_order_commits_one_coherent_rank_sequence(
  client, auth, db,
):
  chats, apps, projects = _seed_pinned_rows(db)
  requested = [
    {"kind": "app", "id": str(apps[1].id)},
    {"kind": "project", "id": projects[0].project_id},
    {"kind": "chat", "id": chats[0].id},
    {"kind": "app", "id": str(apps[0].id)},
    {"kind": "chat", "id": chats[1].id},
    {"kind": "project", "id": projects[1].project_id},
  ]

  response = client.put(
    "/api/chats/pinned-order",
    headers=auth,
    json={"items": requested},
  )

  assert response.status_code == 200, response.text
  payload = response.json()["items"]
  assert [(item["kind"], item["id"]) for item in payload] == [
    (item["kind"], item["id"]) for item in requested
  ]
  stamps = [datetime.fromisoformat(item["pinned_at"]) for item in payload]
  assert stamps == sorted(stamps)
  assert len(set(stamps)) == len(stamps)

  db.expire_all()
  rows = {
    **{("chat", row.id): row for row in db.query(models.Chat).all()},
    **{("app", str(row.id)): row for row in db.query(models.App).all()},
    **{("project", row.project_id): row for row in db.query(models.ProjectDrawerState).all()},
  }
  assert [rows[(item["kind"], item["id"])].pinned_at for item in requested] == stamps


def test_combined_pinned_order_rejects_a_partial_identity_set_without_writes(
  client, auth, db,
):
  chats, apps, projects = _seed_pinned_rows(db)
  before = {
    ("chat", row.id): row.pinned_at for row in chats
  } | {
    ("app", str(row.id)): row.pinned_at for row in apps
  } | {
    ("project", row.project_id): row.pinned_at for row in projects
  }

  response = client.put(
    "/api/chats/pinned-order",
    headers=auth,
    json={"items": [{"kind": "chat", "id": chats[0].id}]},
  )

  assert response.status_code == 409
  db.expire_all()
  after = {
    **{("chat", row.id): row.pinned_at for row in db.query(models.Chat).all()},
    **{("app", str(row.id)): row.pinned_at for row in db.query(models.App).all()},
    **{("project", row.project_id): row.pinned_at for row in db.query(models.ProjectDrawerState).all()},
  }
  assert after == before


def test_chat_pin_response_returns_the_exact_persisted_rank(client, auth, db):
  chat = models.Chat(id="canonical-pin", title="Canonical pin", messages=[])
  db.add(chat)
  db.commit()

  response = client.patch(
    f"/api/chats/{chat.id}", headers=auth, json={"pinned": True},
  )

  assert response.status_code == 200, response.text
  returned = datetime.fromisoformat(response.json()["pinned_at"])
  db.expire_all()
  assert db.get(models.Chat, chat.id).pinned_at == returned


def test_concurrent_pin_waits_until_reorder_validation_and_commit_finish(
  client, auth, db, monkeypatch,
):
  chats, apps, projects = _seed_pinned_rows(db)
  later_chat = models.Chat(id="later-pin", title="Later pin", messages=[])
  db.add(later_chat)
  db.commit()
  requested = [
    *({"kind": "chat", "id": row.id} for row in chats),
    *({"kind": "app", "id": str(row.id)} for row in apps),
    *({"kind": "project", "id": row.project_id} for row in projects),
  ]

  reorder_inside_commit = threading.Event()
  release_reorder = threading.Event()
  pin_finished = threading.Event()
  call_count = 0
  call_count_lock = threading.Lock()
  reorder_time = datetime(2026, 9, 12, 12, 0, 0)

  def controlled_now():
    nonlocal call_count
    with call_count_lock:
      call_count += 1
      call = call_count
    if call == 1:
      reorder_inside_commit.set()
      assert release_reorder.wait(2), "test did not release reorder commit"
      return reorder_time
    return reorder_time + timedelta(seconds=1)

  monkeypatch.setattr(chat_routes, "now_naive_utc", controlled_now)

  def pin_chat():
    try:
      return client.patch(
        f"/api/chats/{later_chat.id}", headers=auth, json={"pinned": True},
      )
    finally:
      pin_finished.set()

  with ThreadPoolExecutor(max_workers=2) as pool:
    reorder = pool.submit(
      client.put,
      "/api/chats/pinned-order",
      headers=auth,
      json={"items": requested},
    )
    assert reorder_inside_commit.wait(2), "reorder never entered its commit boundary"
    pin = pool.submit(pin_chat)
    try:
      assert not pin_finished.wait(0.1), (
        "pin escaped while reorder held the shared commit boundary"
      )
    finally:
      release_reorder.set()
    reorder_response = reorder.result(timeout=2)
    pin_response = pin.result(timeout=2)

  assert reorder_response.status_code == 200, reorder_response.text
  assert pin_response.status_code == 200, pin_response.text
  reordered_ranks = [
    datetime.fromisoformat(item["pinned_at"])
    for item in reorder_response.json()["items"]
  ]
  assert datetime.fromisoformat(pin_response.json()["pinned_at"]) > max(reordered_ranks)


def test_concurrent_delete_waits_until_reorder_validation_and_commit_finish(
  client, auth, db, monkeypatch,
):
  chats, apps, projects = _seed_pinned_rows(db)
  requested = [
    *({"kind": "chat", "id": row.id} for row in chats),
    *({"kind": "app", "id": str(row.id)} for row in apps),
    *({"kind": "project", "id": row.project_id} for row in projects),
  ]

  reorder_inside_commit = threading.Event()
  release_reorder = threading.Event()
  delete_finished = threading.Event()
  call_count = 0
  call_count_lock = threading.Lock()

  def controlled_now():
    nonlocal call_count
    with call_count_lock:
      call_count += 1
      call = call_count
    if call == 1:
      reorder_inside_commit.set()
      assert release_reorder.wait(2), "test did not release reorder commit"
    return datetime(2026, 9, 12, 12, 0, 0) + timedelta(seconds=call)

  monkeypatch.setattr(chat_routes, "now_naive_utc", controlled_now)

  def delete_chat():
    try:
      return client.delete(f"/api/chats/{chats[0].id}", headers=auth)
    finally:
      delete_finished.set()

  with ThreadPoolExecutor(max_workers=2) as pool:
    reorder = pool.submit(
      client.put,
      "/api/chats/pinned-order",
      headers=auth,
      json={"items": requested},
    )
    assert reorder_inside_commit.wait(2), "reorder never entered its commit boundary"
    delete = pool.submit(delete_chat)
    try:
      assert not delete_finished.wait(0.1), (
        "delete escaped while reorder held the shared commit boundary"
      )
    finally:
      release_reorder.set()
    reorder_response = reorder.result(timeout=2)
    delete_response = delete.result(timeout=2)

  assert reorder_response.status_code == 200, reorder_response.text
  assert delete_response.status_code == 204, delete_response.text
  assert any(
    item["kind"] == "chat" and item["id"] == chats[0].id
    for item in reorder_response.json()["items"]
  )
  db.expire_all()
  assert db.get(models.Chat, chats[0].id).deleted_at is not None
