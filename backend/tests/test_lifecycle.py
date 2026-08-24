# backend/tests/test_lifecycle.py
from datetime import UTC, datetime, timedelta
from pathlib import Path
import uuid

from app import chat_search, models
from app.chat_retention import purge_expired_chat_tombstones
from sqlalchemy import event


def test_ttl_is_seven_days(db, chat):
  """Chats deleted fewer than 7 days ago must not be purged."""
  chat_id = chat.id  # capture before any purge
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=6)
  db.commit()

  purge_expired_chat_tombstones(db)

  still_there = db.query(models.Chat).filter(
    models.Chat.id == chat_id
  ).first()
  assert still_there is not None, "Chat deleted 6 days ago must survive"


def test_purge_after_seven_days(db, chat):
  """Chats deleted more than 7 days ago must be hard-deleted."""
  chat_id = chat.id  # capture before purge deletes the row
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()

  purge_expired_chat_tombstones(db)

  gone = db.query(models.Chat).filter(
    models.Chat.id == chat_id
  ).first()
  assert gone is None, "Chat deleted 8 days ago must be purged"


def test_hard_purge_removes_durable_waits(db, chat):
  """A deleted chat cannot leave executable wait checks behind."""
  chat_id = chat.id
  db.add(models.ChatWait(
    id="wait-for-purged-chat",
    chat_id=chat_id,
    description="external task finishes",
    kind="command",
    command="false",
    interval_secs=300,
    deadline_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(days=1),
    status="armed",
    next_check_at=datetime.now(UTC).replace(tzinfo=None),
  ))
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()

  purge_expired_chat_tombstones(db)

  assert db.get(models.Chat, chat_id) is None
  assert db.get(models.ChatWait, "wait-for-purged-chat") is None


def test_hard_purge_removes_derived_search_transcript_without_later_search(
  db, chat,
):
  chat.messages = [{
    "role": "user",
    "content": "retentioncassowary searchable transcript",
    "ts": 1000,
  }]
  db.commit()
  assert any(
    result["id"] == chat.id
    for result in chat_search.search(db, "retentioncassowary")
  )

  chat_id = chat.id
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()
  purge_expired_chat_tombstones(db)

  assert db.execute(
    chat_search.sql(
      "SELECT count(*) FROM chat_search_docs WHERE chat_id = :chat_id"
    ),
    {"chat_id": chat_id},
  ).scalar_one() == 0
  assert db.execute(
    chat_search.sql(
      "SELECT count(*) FROM chat_search_state WHERE chat_id = :chat_id"
    ),
    {"chat_id": chat_id},
  ).scalar_one() == 0


def test_expired_tombstone_purge_does_not_hydrate_transcript_json(
  db, chat,
):
  """The retention sweep selects tombstone ids, not complete Chat entities."""
  chat_id = chat.id
  chat.messages = [{"role": "user", "content": "large transcript sentinel"}]
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()
  # Remove the fixture entity from this session so an accidental
  # ``query(Chat).all()`` must instantiate it and fire the load event below.
  db.expunge_all()

  hydrated_chat_ids = []

  def on_load(loaded_chat, _context):
    hydrated_chat_ids.append(loaded_chat.id)

  event.listen(models.Chat, "load", on_load)
  try:
    purge_expired_chat_tombstones(db)
  finally:
    event.remove(models.Chat, "load", on_load)

  assert chat_id not in hydrated_chat_ids


def test_expired_tombstone_survives_drawer_reads(client, db, auth, chat):
  """GET /api/chats is projection-only and never performs permanent cleanup."""
  chat_id = chat.id
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()

  response = client.get("/api/chats", headers=auth)

  assert response.status_code == 200
  db.expire_all()
  assert db.get(models.Chat, chat_id) is not None


def test_new_delete_reclaims_older_expired_tombstones(
  client, db, auth, chat,
):
  """An explicit delete is the runtime lifecycle boundary for old tombstones."""
  expired_id = str(uuid.uuid4())
  db.add(models.Chat(
    id=expired_id,
    title="Expired tombstone",
    messages=[],
    deleted_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8),
  ))
  db.commit()

  response = client.delete(f"/api/chats/{chat.id}", headers=auth)

  assert response.status_code == 204
  db.expire_all()
  assert db.get(models.Chat, expired_id) is None
  assert db.get(models.Chat, chat.id).deleted_at is not None


def test_old_empty_chat_survives_drawer_reads(client, db, auth):
  """Age and empty content do not imply owner intent to delete a chat."""
  chat_id = str(uuid.uuid4())
  db.add(models.Chat(
    id=chat_id,
    title="Old empty chat",
    messages=[],
    pending_messages=[],
    session_id=None,
    created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=365),
  ))
  db.commit()

  response = client.get("/api/chats", headers=auth)

  assert response.status_code == 200
  assert chat_id in {row["id"] for row in response.json()}
  db.expire_all()
  assert db.get(models.Chat, chat_id) is not None


def test_purge_removes_data_dir(db, chat):
  """Hard delete must remove /data/chats/{id}/ directory."""
  import os
  chat_id = chat.id  # capture before purge
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()

  data_dir = os.environ["DATA_DIR"]
  chat_dir = Path(data_dir) / "chats" / chat_id
  chat_dir.mkdir(parents=True, exist_ok=True)
  (chat_dir / "uploads").mkdir()
  (chat_dir / "uploads" / "file.txt").write_text("hello")

  purge_expired_chat_tombstones(db)

  assert not chat_dir.exists(), "Chat directory must be deleted with chat"


def test_purge_removes_agent_browser_profile(db, chat):
  """Hard delete must also remove the agent-browser Chromium profile.

  Profiles accumulate at /data/agent-browser-profiles/chat-{id}/
  whenever a chat invokes agent-browser. Previously this path was
  untouched by both delete and 7-day purge, leaking 50-200 MB per
  profile to disk indefinitely (ticket 051).
  """
  import os
  chat_id = chat.id  # capture before purge
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()

  data_dir = os.environ["DATA_DIR"]
  profile_dir = Path(data_dir) / "agent-browser-profiles" / f"chat-{chat_id}"
  profile_dir.mkdir(parents=True, exist_ok=True)
  (profile_dir / "Cache").mkdir()
  (profile_dir / "Cache" / "blob.bin").write_text("fake-cache")

  purge_expired_chat_tombstones(db)

  assert not profile_dir.exists(), (
    "agent-browser profile dir must be deleted with chat"
  )


def test_purge_removes_memory_note_dir(db, chat):
  """Hard delete must also remove the chat's memory note dir.

  The note (`shared/memory/chats/<id>/index.md`) is derived from the
  chat, so the owner's delete intent covers it — an orphan note would
  otherwise linger as a memory entry pointing at a chat that no
  longer exists.
  """
  import os
  chat_id = chat.id  # capture before purge
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()

  data_dir = os.environ["DATA_DIR"]
  note_dir = Path(data_dir) / "shared" / "memory" / "chats" / chat_id
  note_dir.mkdir(parents=True, exist_ok=True)
  (note_dir / "index.md").write_text("---\ntype: chat\n---\n## Summary\nx")

  purge_expired_chat_tombstones(db)

  assert not note_dir.exists(), (
    "memory note dir must be deleted with chat"
  )


def test_old_notifications_survive_chat_drawer_reads(client, db, auth):
  """Listing chats does not silently impose retention on notification history."""
  notif_source = str(uuid.uuid4())  # doesn't need to match a real chat
  owner = db.query(models.Owner).first()
  old = models.Notification(
    id="old-notif",
    owner_id=owner.id,
    source_type="chat",
    source_id=notif_source,
    title="Old",
    body="should be purged",
    sent_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=91),
  )
  recent = models.Notification(
    id="recent-notif",
    owner_id=owner.id,
    source_type="chat",
    source_id=notif_source,
    title="Recent",
    body="should survive",
    sent_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30),
  )
  db.add(old)
  db.add(recent)
  db.commit()

  client.get("/api/chats", headers=auth)
  db.expire_all()

  assert db.query(models.Notification).filter(
    models.Notification.id == "old-notif"
  ).first() is not None, "Chat listing must preserve old notification history"
  assert db.query(models.Notification).filter(
    models.Notification.id == "recent-notif"
  ).first() is not None, "Notification newer than 90 days must survive"


def test_chat_has_uploads_column(db, chat):
  """Chat.uploads must default to an empty list."""
  assert chat.uploads == []
