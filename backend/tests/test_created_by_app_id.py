"""Tests for the Chat.created_by_app_id additive column (Capability A groundwork).

Covers the column's persistence contract and the actor gate in
`get_active_chat_for_principal`. These tests are self-contained: they use
the DB fixture directly and do NOT depend on the app-chat-creation endpoint
or the send/stream route changes (both of which land in a later slice).

The gate function is included in this groundwork slice because it is a
purely additive helper — no existing route behavior changes. The tests here
lock in the two invariants that make the column safe to ship independently:

  1. The column round-trips through SQLAlchemy (NULL default, integer set).
  2. The gate function correctly accepts/rejects principals against the
     column value, independent of any HTTP layer.
"""

import pytest
from fastapi import HTTPException

from app import models
from app.deps import Principal
from app.resource_access import get_active_chat_for_principal


# ---------------------------------------------------------------------------
# Column persistence
# ---------------------------------------------------------------------------

def test_created_by_app_id_defaults_to_null(db):
  """Owner-created chats have created_by_app_id = NULL."""
  chat = models.Chat(id="owner-chat", title="mine", messages=[])
  db.add(chat)
  db.commit()
  db.refresh(chat)
  assert chat.created_by_app_id is None


def test_created_by_app_id_persists_integer(db):
  """Setting created_by_app_id to an integer round-trips through the DB."""
  app = models.App(
    name="myapp", description="test", jsx_source="export default () => null",
  )
  db.add(app)
  db.commit()
  db.refresh(app)

  chat = models.Chat(
    id="app-chat", title="app's", messages=[],
    created_by_app_id=app.id,
  )
  db.add(chat)
  db.commit()

  row = db.query(models.Chat).filter(models.Chat.id == "app-chat").first()
  assert row is not None
  assert row.created_by_app_id == app.id


# ---------------------------------------------------------------------------
# get_active_chat_for_principal actor gate
# ---------------------------------------------------------------------------

def _owner_principal(db):
  """Returns a Principal representing the owner (app_id=None)."""
  owner = db.query(models.Owner).first()
  if owner is None:
    owner = models.Owner(username="tester", hashed_password="x")
    db.add(owner)
    db.commit()
  return Principal(owner=owner, app_id=None)


def _app_principal(db, app_id: int):
  """Returns a Principal representing an app token."""
  owner = db.query(models.Owner).first()
  if owner is None:
    owner = models.Owner(username="tester", hashed_password="x")
    db.add(owner)
    db.commit()
  return Principal(owner=owner, app_id=app_id)


def test_owner_principal_drives_any_chat(db):
  """An owner token may drive any chat regardless of created_by_app_id."""
  app = models.App(
    name="a1", description="", jsx_source="export default () => null",
  )
  db.add(app)
  db.commit()
  db.refresh(app)

  chat = models.Chat(
    id="some-chat", title="t", messages=[],
    created_by_app_id=app.id,
  )
  db.add(chat)
  db.commit()

  principal = _owner_principal(db)
  result = get_active_chat_for_principal(db, "some-chat", principal)
  assert result.id == "some-chat"


def test_owner_principal_drives_owner_created_chat(db):
  """An owner token may drive a chat with created_by_app_id = NULL."""
  chat = models.Chat(id="owner-only", title="t", messages=[])
  db.add(chat)
  db.commit()

  principal = _owner_principal(db)
  result = get_active_chat_for_principal(db, "owner-only", principal)
  assert result.id == "owner-only"


def test_app_principal_drives_own_chat(db):
  """An app token may drive a chat it created (matching created_by_app_id)."""
  app = models.App(
    name="a2", description="", jsx_source="export default () => null",
  )
  db.add(app)
  db.commit()
  db.refresh(app)

  chat = models.Chat(
    id="mine", title="t", messages=[],
    created_by_app_id=app.id,
  )
  db.add(chat)
  db.commit()

  principal = _app_principal(db, app.id)
  result = get_active_chat_for_principal(db, "mine", principal)
  assert result.id == "mine"


def test_app_principal_blocked_from_owner_chat(db):
  """An app token receives 403 when targeting an owner-created chat (NULL)."""
  app = models.App(
    name="a3", description="", jsx_source="export default () => null",
  )
  db.add(app)
  db.commit()
  db.refresh(app)

  chat = models.Chat(id="owner-chat2", title="t", messages=[])
  db.add(chat)
  db.commit()

  principal = _app_principal(db, app.id)
  with pytest.raises(HTTPException) as exc:
    get_active_chat_for_principal(db, "owner-chat2", principal)
  assert exc.value.status_code == 403


def test_app_principal_blocked_from_foreign_app_chat(db):
  """An app token receives 403 when targeting a chat owned by a different app."""
  app_a = models.App(
    name="a4", description="", jsx_source="export default () => null",
  )
  app_b = models.App(
    name="a5", description="", jsx_source="export default () => null",
  )
  db.add(app_a)
  db.add(app_b)
  db.commit()
  db.refresh(app_a)
  db.refresh(app_b)

  chat = models.Chat(
    id="b-chat", title="t", messages=[],
    created_by_app_id=app_b.id,
  )
  db.add(chat)
  db.commit()

  principal = _app_principal(db, app_a.id)  # app_a tries to drive app_b's chat
  with pytest.raises(HTTPException) as exc:
    get_active_chat_for_principal(db, "b-chat", principal)
  assert exc.value.status_code == 403


def test_gate_raises_404_on_missing_chat(db):
  """404 is returned when the chat doesn't exist — same shape owner sees."""
  principal = _owner_principal(db)
  with pytest.raises(HTTPException) as exc:
    get_active_chat_for_principal(db, "nonexistent", principal)
  assert exc.value.status_code == 404
