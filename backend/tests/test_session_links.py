"""Tests for the append-only session->chat identity map.

Covers the `session_links.record_session_link` upsert (insert, idempotent
re-sight, append-only chat_id, no-op guards), the invariant that a provider
switch NULLs `Chat.session_id` but leaves the link rows untouched, and the
owner-only `GET /api/chats/session-links` endpoint contract.
"""

from __future__ import annotations

from app import models
from app.session_links import backfill_current_session_links, record_session_link
from test_app_fixtures import create_local_app


def _chat(db, chat_id: str, *, provider: str = "claude", session_id=None):
  c = models.Chat(
    id=chat_id,
    title="t",
    messages=[],
    pending_messages=[],
    provider=provider,
    session_id=session_id,
  )
  db.add(c)
  db.commit()
  return c


def test_record_session_link_inserts_a_row(db):
  _chat(db, "chat-1")
  record_session_link(db, "claude", "sess-A", "chat-1")

  link = db.get(models.ChatSessionLink, ("claude", "sess-A"))
  assert link is not None
  assert link.chat_id == "chat-1"
  # First sighting stamps both timestamps to the same instant.
  assert link.first_seen_at == link.last_seen_at


def test_record_session_link_resight_bumps_only_last_seen(db):
  _chat(db, "chat-1")
  record_session_link(db, "claude", "sess-A", "chat-1")
  original = db.get(models.ChatSessionLink, ("claude", "sess-A"))
  first_seen = original.first_seen_at
  # Force a later last_seen so the bump is observable regardless of clock
  # resolution, then re-sight.
  from datetime import timedelta
  original.last_seen_at = first_seen - timedelta(seconds=5)
  db.commit()

  record_session_link(db, "claude", "sess-A", "chat-1")

  db.expire_all()
  rows = db.query(models.ChatSessionLink).all()
  # Idempotent: still exactly one row for this (provider, session_id).
  assert len(rows) == 1
  link = rows[0]
  # first_seen is anchored; last_seen advanced past the value we backdated.
  assert link.first_seen_at == first_seen
  assert link.last_seen_at > link.first_seen_at - timedelta(seconds=5)


def test_record_session_link_is_append_only_never_rewrites_chat(db):
  """A re-sight of the same (provider, session_id) never repoints chat_id —
  the mapping's identity is fixed at first sight."""
  _chat(db, "chat-1")
  _chat(db, "chat-2")
  record_session_link(db, "claude", "sess-A", "chat-1")

  # A (buggy or racing) re-sight naming a different chat must not steal the id.
  record_session_link(db, "claude", "sess-A", "chat-2")

  link = db.get(models.ChatSessionLink, ("claude", "sess-A"))
  assert link.chat_id == "chat-1"
  assert db.query(models.ChatSessionLink).count() == 1


def test_record_session_link_same_session_id_distinct_per_provider(db):
  """The composite PK is (provider, session_id): the same id string under two
  providers is two independent rows, not a collision."""
  _chat(db, "chat-1")
  record_session_link(db, "claude", "dup-id", "chat-1")
  record_session_link(db, "codex", "dup-id", "chat-1")

  assert db.get(models.ChatSessionLink, ("claude", "dup-id")) is not None
  assert db.get(models.ChatSessionLink, ("codex", "dup-id")) is not None
  assert db.query(models.ChatSessionLink).count() == 2


def test_record_session_link_noops_on_missing_args(db):
  _chat(db, "chat-1")
  record_session_link(db, "", "sess-A", "chat-1")
  record_session_link(db, "claude", "", "chat-1")
  record_session_link(db, "claude", "sess-A", "")
  record_session_link(None, "claude", "sess-A", "chat-1")

  assert db.query(models.ChatSessionLink).count() == 0


def test_backfill_current_session_links_is_idempotent(db):
  _chat(db, "chat-1", provider="claude", session_id="sess-A")
  _chat(db, "chat-2", provider="codex", session_id="sess-B")
  _chat(db, "chat-empty", provider="claude", session_id="")

  assert backfill_current_session_links(db) == 2
  assert backfill_current_session_links(db) == 0

  links = {
    (link.provider, link.session_id): link.chat_id
    for link in db.query(models.ChatSessionLink).all()
  }
  assert links == {
    ("claude", "sess-A"): "chat-1",
    ("codex", "sess-B"): "chat-2",
  }


def test_backfill_preserves_existing_and_skips_ambiguous_claims(db):
  _chat(db, "chat-existing", provider="claude", session_id="sess-existing")
  _chat(db, "chat-claim", provider="claude", session_id="sess-existing")
  _chat(db, "chat-a", provider="codex", session_id="sess-ambiguous")
  _chat(db, "chat-b", provider="codex", session_id="sess-ambiguous")
  db.add(models.ChatSessionLink(
    provider="claude",
    session_id="sess-existing",
    chat_id="chat-existing",
  ))
  db.commit()

  assert backfill_current_session_links(db) == 0
  assert db.get(
    models.ChatSessionLink, ("claude", "sess-existing")
  ).chat_id == "chat-existing"
  assert db.get(
    models.ChatSessionLink, ("codex", "sess-ambiguous")
  ) is None


def test_provider_switch_nulls_session_id_but_leaves_links(
  client, auth, db, monkeypatch,
):
  """The provider switch wipes Chat.session_id (a Claude id is not a valid
  Codex thread id) but the append-only link survives, so the old id still
  resolves to its chat afterward."""
  from app import providers

  # A switch to codex requires codex to read as connected.
  monkeypatch.setattr(
    providers.CodexProvider, "check_auth", lambda self, d: None,
  )

  chat = _chat(db, "chat-switch", provider="claude", session_id="sess-A")
  record_session_link(db, "claude", "sess-A", "chat-switch")

  r = client.patch(
    f"/api/chats/{chat.id}", headers=auth, json={"provider": "codex"},
  )
  assert r.status_code == 200, r.text
  assert r.json()["provider"] == "codex"

  db.expire_all()
  switched = db.query(models.Chat).filter(
    models.Chat.id == "chat-switch"
  ).first()
  # The live pointer is gone...
  assert switched.session_id is None
  assert switched.provider == "codex"
  # ...but the identity map still remembers where sess-A belonged.
  link = db.get(models.ChatSessionLink, ("claude", "sess-A"))
  assert link is not None
  assert link.chat_id == "chat-switch"


def test_session_links_endpoint_returns_links_newest_first(
  client, auth, db,
):
  from datetime import datetime, timedelta

  _chat(db, "chat-1")
  _chat(db, "chat-2")
  record_session_link(db, "claude", "sess-old", "chat-1")
  record_session_link(db, "codex", "sess-new", "chat-2")
  # Pin deterministic last_seen ordering (record stamps "now" for both).
  base = datetime(2026, 7, 17, 12, 0, 0)
  db.get(models.ChatSessionLink, ("claude", "sess-old")).last_seen_at = base
  db.get(models.ChatSessionLink, ("codex", "sess-new")).last_seen_at = (
    base + timedelta(minutes=5)
  )
  db.commit()

  r = client.get("/api/chats/session-links", headers=auth)
  assert r.status_code == 200, r.text
  links = r.json()["links"]
  assert [l["session_id"] for l in links] == ["sess-new", "sess-old"]
  newest = links[0]
  assert newest["provider"] == "codex"
  assert newest["chat_id"] == "chat-2"
  assert newest["first_seen_at"]
  assert newest["last_seen_at"]


def test_session_links_endpoint_rejects_app_token(client, owner_token):
  """Owner-only surface: an app-scoped token is 403, never the map."""
  app_id = create_local_app(
    client, {"Authorization": f"Bearer {owner_token}"},
    name="test-app", description="test",
  )["id"]

  r = client.post(
    "/api/auth/app-token", json={"app_id": app_id},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  app_token = r.json()["token"]

  r = client.get("/api/chats/session-links", headers={
    "Authorization": f"Bearer {app_token}",
  })
  assert r.status_code == 403
