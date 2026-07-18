"""Tests for the gated, redacted chat-log read API (capability B).

These lock in the two things that matter: the permission gate (owner
always; app needs chat_log_access>=summary) and the server-side
structural redaction (tool/thinking/question/error blocks, attachments,
hidden/pending messages, fs-path augmentation, and secrets never leave
the server).
"""

from app import models


def _make_app(db, name, chat_log_access="none"):
  app = models.App(
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

  bad = dict(good, permissions={"chat_log_access": "everything"})
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
