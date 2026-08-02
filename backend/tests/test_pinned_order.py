"""Atomic ordering contract for the drawer's combined pinned chat/app list."""

from datetime import datetime, timedelta

from app import models


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
  db.add_all([*chats, *apps])
  db.commit()
  for row in [*chats, *apps]:
    db.refresh(row)
  return chats, apps


def test_combined_pinned_order_commits_one_coherent_rank_sequence(
  client, auth, db,
):
  chats, apps = _seed_pinned_rows(db)
  requested = [
    {"kind": "app", "id": str(apps[1].id)},
    {"kind": "chat", "id": chats[0].id},
    {"kind": "app", "id": str(apps[0].id)},
    {"kind": "chat", "id": chats[1].id},
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
  }
  assert [rows[(item["kind"], item["id"])].pinned_at for item in requested] == stamps


def test_combined_pinned_order_rejects_a_partial_identity_set_without_writes(
  client, auth, db,
):
  chats, apps = _seed_pinned_rows(db)
  before = {
    ("chat", row.id): row.pinned_at for row in chats
  } | {
    ("app", str(row.id)): row.pinned_at for row in apps
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
  }
  assert after == before
