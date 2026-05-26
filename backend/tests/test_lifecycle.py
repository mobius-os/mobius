# backend/tests/test_lifecycle.py
from datetime import datetime, timedelta
from pathlib import Path
from app import models


def test_ttl_is_seven_days(client, db, auth, chat):
  """Chats deleted fewer than 7 days ago must not be purged."""
  chat.deleted_at = datetime.utcnow() - timedelta(days=6)
  db.commit()

  client.get("/api/chats", headers=auth)

  still_there = db.query(models.Chat).filter(
    models.Chat.id == "testchat"
  ).first()
  assert still_there is not None, "Chat deleted 6 days ago must survive"


def test_purge_after_seven_days(client, db, auth, chat):
  """Chats deleted more than 7 days ago must be hard-deleted."""
  chat.deleted_at = datetime.utcnow() - timedelta(days=8)
  db.commit()

  client.get("/api/chats", headers=auth)

  gone = db.query(models.Chat).filter(
    models.Chat.id == "testchat"
  ).first()
  assert gone is None, "Chat deleted 8 days ago must be purged"


def test_purge_removes_data_dir(client, db, auth, chat):
  """Hard delete must remove /data/chats/{id}/ directory."""
  import os
  chat.deleted_at = datetime.utcnow() - timedelta(days=8)
  db.commit()

  data_dir = os.environ["DATA_DIR"]
  chat_dir = Path(data_dir) / "chats" / "testchat"
  chat_dir.mkdir(parents=True, exist_ok=True)
  (chat_dir / "uploads").mkdir()
  (chat_dir / "uploads" / "file.txt").write_text("hello")

  client.get("/api/chats", headers=auth)

  assert not chat_dir.exists(), "Chat directory must be deleted with chat"


def test_purge_removes_agent_browser_profile(client, db, auth, chat):
  """Hard delete must also remove the agent-browser Chromium profile.

  Profiles accumulate at /data/agent-browser-profiles/chat-{id}/
  whenever a chat invokes agent-browser. Previously this path was
  untouched by both delete and 7-day purge, leaking 50-200 MB per
  profile to disk indefinitely (ticket 051).
  """
  import os
  chat.deleted_at = datetime.utcnow() - timedelta(days=8)
  db.commit()

  data_dir = os.environ["DATA_DIR"]
  profile_dir = Path(data_dir) / "agent-browser-profiles" / "chat-testchat"
  profile_dir.mkdir(parents=True, exist_ok=True)
  (profile_dir / "Cache").mkdir()
  (profile_dir / "Cache" / "blob.bin").write_text("fake-cache")

  client.get("/api/chats", headers=auth)

  assert not profile_dir.exists(), (
    "agent-browser profile dir must be deleted with chat"
  )


def test_notifications_older_than_90_days_purged(client, db, auth):
  """Notifications older than 90 days must be deleted by list_chats.

  The notification table had no TTL — rows accumulated indefinitely
  from every AskUserQuestion ack and agent-driven push notification.
  Ticket 052 caps growth by deleting >90-day rows alongside the
  existing soft-deleted-chat purge.
  """
  owner = db.query(models.Owner).first()
  old = models.Notification(
    id="old-notif",
    owner_id=owner.id,
    source_type="chat",
    source_id="testchat",
    title="Old",
    body="should be purged",
    sent_at=datetime.utcnow() - timedelta(days=91),
  )
  recent = models.Notification(
    id="recent-notif",
    owner_id=owner.id,
    source_type="chat",
    source_id="testchat",
    title="Recent",
    body="should survive",
    sent_at=datetime.utcnow() - timedelta(days=30),
  )
  db.add(old)
  db.add(recent)
  db.commit()

  client.get("/api/chats", headers=auth)
  db.expire_all()

  assert db.query(models.Notification).filter(
    models.Notification.id == "old-notif"
  ).first() is None, "Notification older than 90 days must be purged"
  assert db.query(models.Notification).filter(
    models.Notification.id == "recent-notif"
  ).first() is not None, "Notification newer than 90 days must survive"


def test_chat_has_uploads_column(db, chat):
  """Chat.uploads must default to an empty list."""
  assert chat.uploads == []


def test_chat_has_generated_images_column(db, chat):
  """Chat.generated_images must default to an empty list."""
  assert chat.generated_images == []


def test_owner_has_gemini_key_column(db, owner_token):
  """Owner.gemini_api_key_enc must default to None."""
  owner = db.query(models.Owner).filter(
    models.Owner.username == "test"
  ).first()
  assert owner is not None
  assert hasattr(owner, "gemini_api_key_enc")
  assert owner.gemini_api_key_enc is None
