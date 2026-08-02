"""Unit tests for `app.resource_access.get_active_chat_or_404`.

Three behaviors locked in: 404 on missing, 404 on soft-deleted,
returns row on active. The helper centralizes a query pattern that
five route files used to copy; the tests guard the contract against
silent drift if the soft-delete column ever changes.
"""

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app import models
from app.deps import Principal
from app.resource_access import (
  get_active_chat_or_404,
  require_active_chat_access,
)


def test_returns_active_chat(db):
  """An active (non-soft-deleted) chat row is returned."""
  chat = models.Chat(id="alive", title="hi", messages=[])
  db.add(chat)
  db.commit()
  result = get_active_chat_or_404(db, "alive")
  assert result.id == "alive"


def test_raises_404_on_missing_chat(db):
  """Nonexistent id raises HTTPException(404)."""
  with pytest.raises(HTTPException) as exc:
    get_active_chat_or_404(db, "nope")
  assert exc.value.status_code == 404


def test_raises_404_on_soft_deleted_chat(db):
  """A chat with `deleted_at` set is treated as not found. This is
  the load-bearing behavior — the helper exists to make sure no
  caller forgets the filter."""
  chat = models.Chat(
    id="dead", title="gone", messages=[], deleted_at=datetime.now(UTC),
  )
  db.add(chat)
  db.commit()
  with pytest.raises(HTTPException) as exc:
    get_active_chat_or_404(db, "dead")
  assert exc.value.status_code == 404


def test_returns_same_row_callers_can_mutate(db):
  """The returned row is the live SQLAlchemy object — callers can
  mutate it and commit, which several route handlers do."""
  chat = models.Chat(id="mut", title="orig", messages=[])
  db.add(chat)
  db.commit()
  result = get_active_chat_or_404(db, "mut")
  result.title = "renamed"
  db.commit()
  refetched = get_active_chat_or_404(db, "mut")
  assert refetched.title == "renamed"


def test_access_only_gate_does_not_select_transcript_json(db):
  """An ownership check never decodes the large chat payload columns."""
  chat = models.Chat(
    id="large",
    title="large transcript",
    messages=[{"role": "user", "content": "x" * 100_000}],
    live_assistant={"role": "assistant", "blocks": [{"type": "text"}]},
    pending_messages=[{"role": "user", "content": "queued"}],
  )
  db.add(chat)
  db.commit()
  db.expunge_all()

  statements: list[str] = []

  def capture_select(_conn, _cursor, statement, _params, _context, _many):
    if "FROM chats" in statement:
      statements.append(statement)

  event.listen(db.bind, "before_cursor_execute", capture_select)
  try:
    require_active_chat_access(
      db,
      "large",
      Principal(owner=models.Owner(username="owner"), app_id=None),
    )
  finally:
    event.remove(db.bind, "before_cursor_execute", capture_select)

  assert len(statements) == 1
  selected = statements[0].partition("FROM chats")[0]
  assert "chats.id" in selected
  assert "chats.created_by_app_id" in selected
  assert "chats.messages" not in selected
  assert "chats.live_assistant" not in selected
  assert "chats.pending_messages" not in selected


def test_access_only_gate_preserves_app_chat_ownership(db):
  """The minimal projection keeps the same app-vs-foreign authorization."""
  db.add_all([
    models.App(
      slug="test-resource-access-107",
      source_dir="/tmp/mobius-tests/test-resource-access-107",
      id=7,
      name="owner app",
      description="",
      jsx_source="",
      compiled_path="",
    ),
    models.Chat(
      id="app-chat",
      title="owned",
      messages=[],
      created_by_app_id=7,
    ),
  ])
  db.commit()
  db.expunge_all()

  owner = models.Owner(username="owner")
  require_active_chat_access(
    db, "app-chat", Principal(owner=owner, app_id=7, scope="app"),
  )

  with pytest.raises(HTTPException) as exc:
    require_active_chat_access(
      db, "app-chat", Principal(owner=owner, app_id=8, scope="app"),
    )
  assert exc.value.status_code == 403
