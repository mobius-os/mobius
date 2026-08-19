# backend/tests/test_recover.py
from datetime import UTC, datetime, timedelta
from app import models


def test_recover_deleted_chat(client, db, auth, chat):
  """The chat recovery endpoint must clear deleted_at."""
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
  db.commit()

  res = client.post(f"/api/chats/{chat.id}/recover", headers=auth)
  assert res.status_code == 200
  assert res.json()["ok"] is True

  db.refresh(chat)
  assert chat.deleted_at is None


def test_recover_expired_chat(client, db, auth, chat):
  """Chat recovery past the 7-day window must return 410."""
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()

  res = client.post(f"/api/chats/{chat.id}/recover", headers=auth)
  assert res.status_code == 410


def test_recover_active_chat_returns_404(client, db, auth, chat):
  """Recovering a non-deleted chat must return 404."""
  res = client.post(f"/api/chats/{chat.id}/recover", headers=auth)
  assert res.status_code == 404
