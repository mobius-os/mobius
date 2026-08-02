"""Tests for the gated, redacted chat-log read API (capability B).

These lock in the two things that matter: the permission gate (owner
always; app needs chat_log_access>=summary) and the server-side
structural redaction (tool/thinking/question/error blocks, attachments,
hidden/pending messages, fs-path augmentation, and secrets never leave
the server).
"""

from app import models


def _make_app(db, name, chat_log_access="none"):
  slug = name.lower().replace(" ", "-")
  app = models.App(
    slug=slug,
    source_dir=f"/tmp/mobius-tests/{slug}",
    name=name,
    description="",
    jsx_source="export default () => null",
    chat_log_access=chat_log_access,
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def _app_token(client, owner_token, app_id):
  r = client.post(
    "/api/auth/app-token", json={"app_id": app_id},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 200, r.text
  return r.json()["token"]


def _seed_chat(db, chat_id="logchat"):
  """A chat whose transcript exercises every redaction case."""
  chat = models.Chat(
    id=chat_id,
    title="My grocery list and stuff",
    messages=[
      {
        "role": "user",
        "content": (
          "Please summarize my notes\n\n"
          "[Files in this session:\n"
          "- notes.txt → /data/chats/x/notes.txt (text/plain, 3 KB)]"
        ),
        "attachments": [{"name": "notes.txt", "path": "/data/x/notes.txt"}],
        "ts": 1,
      },
      {
        "role": "assistant",
        "content": "Done, here is the summary.",
        "blocks": [
          {"type": "text", "content": "Done, here is the summary."},
          {
            "type": "tool",
            "tool": "Bash",
            "input": "cat /data/cli-auth/claude/.credentials.json",
            "output": "accessToken: sk-ant-api03-ABCDEFGHIJKLMNOP",
            "status": "done",
          },
          {
            "type": "thinking",
            "content": "owner jwt is "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.ZZZZZZZZZZZZ",
          },
          {"type": "error", "message": "failed at /data/secret/path"},
          {"type": "question", "questions": [{"question": "Which one?"}]},
        ],
      },
      {
        "role": "user",
        "content": "hidden answer api_key=TOPSECRET12345",
        "hidden": True,
        "ts": 2,
      },
    ],
    pending_messages=[{"role": "user", "content": "queued and unseen", "ts": 3}],
  )
  db.add(chat)
  db.commit()
  db.refresh(chat)
  return chat


def test_chat_logs_summary_strips_tool_blocks_and_secrets(
  client, owner_token, db,
):
  _seed_chat(db)
  app = _make_app(db, "reader", chat_log_access="summary")
  token = _app_token(client, owner_token, app.id)

  r = client.get(
    "/api/chat-logs/logchat",
    headers={"Authorization": f"Bearer {token}"},
  )
  assert r.status_code == 200, r.text
  body = r.json()
  flat = " ".join(m["text"] for m in body["messages"])

  # Tool block command + output gone.
  assert "cat /data/cli-auth" not in flat
  assert "sk-ant-api03" not in flat
  # Thinking block + the JWT it quoted gone.
  assert "eyJhbGci" not in flat
  # Error + question block content gone.
  assert "/data/secret/path" not in flat
  assert "Which one?" not in flat
  # fs-path augmentation block + attachment path gone.
  assert "/data/chats/x/notes.txt" not in flat
  assert "[Files in this session" not in flat
  # Hidden + pending messages gone.
  assert "TOPSECRET" not in flat
  assert "queued and unseen" not in flat
  # No structural fields leak through — whitelist is {role, text}.
  for m in body["messages"]:
    assert set(m.keys()) == {"role", "text"}, m
  # Legit conversational text survives.
  assert "Please summarize my notes" in flat
  assert "Done, here is the summary." in flat


def test_chat_logs_list_scrubs_title_and_reports_visible_count(
  client, owner_token, db,
):
  _seed_chat(db)
  app = _make_app(db, "reader", chat_log_access="summary")
  token = _app_token(client, owner_token, app.id)

  r = client.get(
    "/api/chat-logs",
    headers={"Authorization": f"Bearer {token}"},
  )
  assert r.status_code == 200, r.text
  items = r.json()["items"]
  assert len(items) == 1
  entry = items[0]
  assert entry["id"] == "logchat"
  # message_count reflects post-redaction visible messages (the user
  # turn + the assistant text turn = 2; hidden + pending excluded).
  assert entry["message_count"] == 2
  # Excerpt is redacted (no fs-path augmentation) + non-empty.
  assert entry["excerpt"]
  assert "[Files in this session" not in entry["excerpt"]


def test_app_without_grant_gets_403(client, owner_token, db):
  _seed_chat(db)
  app = _make_app(db, "nosy")  # chat_log_access defaults to 'none'
  token = _app_token(client, owner_token, app.id)

  r = client.get(
    "/api/chat-logs",
    headers={"Authorization": f"Bearer {token}"},
  )
  assert r.status_code == 403, r.text
  r = client.get(
    "/api/chat-logs/logchat",
    headers={"Authorization": f"Bearer {token}"},
  )
  assert r.status_code == 403, r.text


def test_owner_token_reads_chat_logs_without_a_grant(client, owner_token, db):
  """The permission map governs apps, not the owner — owner always passes."""
  _seed_chat(db)
  r = client.get(
    "/api/chat-logs",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 200, r.text
  assert r.json()["items"][0]["id"] == "logchat"


def test_revoking_grant_blocks_next_request_without_reissuing_token(
  client, owner_token, db,
):
  """Permission is read from the App row at request time — flipping the
  column to 'none' revokes on the very next call with the SAME token."""
  _seed_chat(db)
  app = _make_app(db, "reader", chat_log_access="summary")
  token = _app_token(client, owner_token, app.id)

  ok = client.get(
    "/api/chat-logs", headers={"Authorization": f"Bearer {token}"},
  )
  assert ok.status_code == 200

  app.chat_log_access = "none"
  db.commit()

  revoked = client.get(
    "/api/chat-logs", headers={"Authorization": f"Bearer {token}"},
  )
  assert revoked.status_code == 403


def test_owner_approval_unblocks_deleted_scope_on_the_existing_app_token(
  client, owner_token, db,
):
  """The explicit grant path and live-row request gate form one contract."""
  from datetime import datetime

  chat = _seed_chat(db, chat_id="approved-deleted")
  chat.deleted_at = datetime.now()
  app = _make_app(db, "approval-reader", chat_log_access="summary")
  token = _app_token(client, owner_token, app.id)
  headers = {"Authorization": f"Bearer {token}"}

  denied = client.get(
    "/api/chat-logs",
    params={"include_deleted": True},
    headers=headers,
  )
  assert denied.status_code == 403

  approved = client.patch(
    f"/api/apps/{app.id}",
    json={"chat_log_access": "summary_with_deleted"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert approved.status_code == 200, approved.text

  allowed = client.get(
    "/api/chat-logs",
    params={"include_deleted": True},
    headers=headers,
  )
  assert allowed.status_code == 200, allowed.text
  assert "approved-deleted" in {
    item["id"] for item in allowed.json()["items"]
  }


def test_chat_logs_excludes_soft_deleted_chats(client, owner_token, db):
  from datetime import UTC, datetime
  chat = _seed_chat(db, chat_id="goner")
  chat.deleted_at = datetime.now(UTC)
  db.commit()

  app = _make_app(db, "reader", chat_log_access="summary")
  token = _app_token(client, owner_token, app.id)

  lst = client.get(
    "/api/chat-logs", headers={"Authorization": f"Bearer {token}"},
  )
  assert lst.status_code == 200
  assert all(i["id"] != "goner" for i in lst.json()["items"])
  one = client.get(
    "/api/chat-logs/goner", headers={"Authorization": f"Bearer {token}"},
  )
  assert one.status_code == 404


def test_recoverable_deleted_chats_need_explicit_higher_tier(
  client, owner_token, db,
):
  from datetime import UTC, datetime, timedelta

  active = _seed_chat(db, chat_id="active")
  deleted = _seed_chat(db, chat_id="recoverable")
  expired = _seed_chat(db, chat_id="expired")
  active.activity_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=2)
  deleted.deleted_at = datetime.now(UTC).replace(tzinfo=None)
  expired.deleted_at = (
    datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  )
  db.commit()

  summary = _make_app(db, "active-only", chat_log_access="summary")
  summary_headers = {
    "Authorization": (
      f"Bearer {_app_token(client, owner_token, summary.id)}"
    ),
  }
  denied = client.get(
    "/api/chat-logs",
    params={"include_deleted": True},
    headers=summary_headers,
  )
  assert denied.status_code == 403
  denied_detail = client.get(
    "/api/chat-logs/recoverable",
    params={"include_deleted": True},
    headers=summary_headers,
  )
  assert denied_detail.status_code == 403

  reader = _make_app(
    db, "lifecycle-reader", chat_log_access="summary_with_deleted",
  )
  headers = {
    "Authorization": f"Bearer {_app_token(client, owner_token, reader.id)}",
  }
  default_list = client.get("/api/chat-logs", headers=headers)
  assert default_list.status_code == 200
  assert "recoverable" not in {
    item["id"] for item in default_list.json()["items"]
  }

  lifecycle_list = client.get(
    "/api/chat-logs",
    params={"include_deleted": True},
    headers=headers,
  )
  assert lifecycle_list.status_code == 200, lifecycle_list.text
  items = {item["id"]: item for item in lifecycle_list.json()["items"]}
  assert {"active", "recoverable"} <= set(items)
  assert "expired" not in items
  assert items["active"]["deleted_at"] is None
  assert items["recoverable"]["deleted_at"] is not None
  # The deletion itself is the durable-consumer recency signal.
  assert items["recoverable"]["recency_at"] == items["recoverable"]["deleted_at"]

  detail = client.get(
    "/api/chat-logs/recoverable",
    params={"include_deleted": True},
    headers=headers,
  )
  assert detail.status_code == 200, detail.text
  assert detail.json()["deleted_at"] is not None
  assert detail.json()["tier"] == "summary"
  assert detail.json()["messages"]

  expired_detail = client.get(
    "/api/chat-logs/expired",
    params={"include_deleted": True},
    headers=headers,
  )
  assert expired_detail.status_code == 404


def test_deleted_chat_keyset_uses_deletion_recency_at_page_boundary(
  client, owner_token, db,
):
  """A newly deleted old chat must not make the next page skip newer work."""
  from datetime import datetime, timedelta

  now = datetime.now()
  deleted = _seed_chat(db, chat_id="deleted-boundary")
  active_mid = _seed_chat(db, chat_id="active-mid")
  active_old = _seed_chat(db, chat_id="active-old")
  deleted.activity_at = now - timedelta(days=6)
  deleted.deleted_at = now
  active_mid.activity_at = now - timedelta(days=1)
  active_old.activity_at = now - timedelta(days=2)
  db.commit()

  reader = _make_app(
    db, "keyset-reader", chat_log_access="summary_with_deleted",
  )
  headers = {
    "Authorization": f"Bearer {_app_token(client, owner_token, reader.id)}",
  }
  first = client.get(
    "/api/chat-logs",
    params={"include_deleted": True, "limit": 1},
    headers=headers,
  )

  assert first.status_code == 200, first.text
  first_body = first.json()
  assert [item["id"] for item in first_body["items"]] == [
    "deleted-boundary",
  ]
  assert first_body["next_before"]["recency_at"] == (
    first_body["items"][0]["deleted_at"]
  )

  second = client.get(
    "/api/chat-logs",
    params={
      "include_deleted": True,
      "limit": 1,
      "before_recency": first_body["next_before"]["recency_at"],
      "before_id": first_body["next_before"]["id"],
    },
    headers=headers,
  )

  assert second.status_code == 200, second.text
  assert [item["id"] for item in second.json()["items"]] == ["active-mid"]


def test_chat_logs_install_validates_chat_log_access_value():
  """install.py rejects an out-of-range chat_log_access tier."""
  from fastapi import HTTPException
  from app.install import _validate_manifest

  good = {
    "id": "x", "name": "X", "version": "1", "description": "d",
    "entry": "index.jsx",
    "permissions": {"chat_log_access": "summary"},
  }
  _validate_manifest(good)  # no raise
  _validate_manifest({
    **good,
    "permissions": {"chat_log_access": "summary_with_deleted"},
  })

  for retired_or_invalid in ("full", "everything"):
    bad = dict(
      good,
      permissions={"chat_log_access": retired_or_invalid},
    )
    try:
      _validate_manifest(bad)
      assert False, "expected HTTPException for bad chat_log_access"
    except HTTPException as exc:
      assert exc.status_code == 400


def test_chat_logs_orders_by_activity_not_updated(client, owner_token, db):
  """Recency follows activity_at, matching the owner's drawer.

  updated_at also moves on non-activity writes (a snapshot backfill
  once bumped it for 312 historical chats), so a row whose updated_at
  is newest but whose activity_at is oldest must still list last.
  """
  from datetime import datetime

  old = _seed_chat(db, chat_id="old-activity")
  new = _seed_chat(db, chat_id="new-activity")
  # The "old" chat was touched by a migration (fresh updated_at) but
  # its real activity predates the "new" chat's.
  old.activity_at = datetime(2026, 1, 1)
  old.updated_at = datetime(2026, 7, 1)
  new.activity_at = datetime(2026, 6, 1)
  new.updated_at = datetime(2026, 2, 1)
  db.commit()

  app = _make_app(db, "orderer", chat_log_access="summary")
  token = _app_token(client, owner_token, app.id)
  lst = client.get(
    "/api/chat-logs", headers={"Authorization": f"Bearer {token}"},
  )
  assert lst.status_code == 200
  ids = [i["id"] for i in lst.json()["items"]]
  assert ids.index("new-activity") < ids.index("old-activity")
  entries = {item["id"]: item for item in lst.json()["items"]}
  assert entries["new-activity"]["recency_at"].startswith("2026-06-01")


def test_chat_logs_keyset_pages_without_offset_drift(
  client, owner_token, db,
):
  from datetime import datetime

  newest = _seed_chat(db, chat_id="newest")
  middle = _seed_chat(db, chat_id="middle")
  oldest = _seed_chat(db, chat_id="oldest")
  newest.activity_at = datetime(2026, 6, 3)
  middle.activity_at = datetime(2026, 6, 2)
  oldest.activity_at = datetime(2026, 6, 1)
  db.commit()
  app = _make_app(db, "pager", chat_log_access="summary")
  token = _app_token(client, owner_token, app.id)
  headers = {"Authorization": f"Bearer {token}"}

  first = client.get("/api/chat-logs?limit=2", headers=headers)
  assert first.status_code == 200, first.text
  assert [item["id"] for item in first.json()["items"]] == [
    "newest", "middle",
  ]
  marker = first.json()["next_before"]
  assert marker == {
    "recency_at": first.json()["items"][-1]["recency_at"],
    "id": "middle",
  }

  # A new head row would shift an offset page. The immutable key from the
  # prior page still lands directly after `middle`, so `oldest` is not skipped.
  inserted = _seed_chat(db, chat_id="inserted-later")
  inserted.activity_at = datetime(2026, 6, 4)
  db.commit()
  second = client.get(
    "/api/chat-logs",
    params={
      "limit": 2,
      "before_recency": marker["recency_at"],
      "before_id": marker["id"],
    },
    headers=headers,
  )
  assert second.status_code == 200, second.text
  assert [item["id"] for item in second.json()["items"]] == ["oldest"]
  assert second.json()["next_before"] is None
