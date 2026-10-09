"""Paths the maintainer review found reading unconverted chats on the event
loop, and the writer's write lock after a command that commits nothing.

Every chat here stands for one the previous release wrote last: its
conversion marker is gone and ``chats.messages`` is its authority.
"""

import asyncio
import json

from sqlalchemy import text

from app import chat_writer, models, transcript_rows
from app.chat_writer import create_chat
from app.database import SessionLocal, engine
from app.routes import github as github_routes
from test_app_fixtures import create_local_app

github_routes._limiter.enabled = False

EDIT = {"type": "tool", "tool": "Edit", "tool_use_id": "edit-1", "edit_preview": {
  "diff": "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n".replace(
    "a/x", "a//data/platform/backend/app/demo.py").replace(
    "b/x", "b//data/platform/backend/app/demo.py"),
}}


def _previous_release_wrote(chat_id, messages):
  with engine.begin() as conn:
    conn.execute(text("UPDATE chats SET messages = :m WHERE id = :id"),
                 {"m": json.dumps(messages), "id": chat_id})


def _converted(chat_id):
  with engine.connect() as conn:
    return conn.execute(text("SELECT 1 FROM chat_transcript_state WHERE chat_id = :id"),
                        {"id": chat_id}).first() is not None


def _source_chat(db, chat_id="unconverted-source"):
  db.add(create_chat(id=chat_id, title="Source", provider="codex", messages=[]))
  db.commit()
  _previous_release_wrote(chat_id, [
    {"role": "user", "content": "edit it", "ts": 1770000000000},
    {"role": "assistant", "ts": 1770000001000, "blocks": [EDIT]},
  ])
  assert not _converted(chat_id)
  return chat_id


def test_recorded_chat_edits_read_an_unconverted_source_on_the_event_loop(db):
  chat_id = _source_chat(db)

  async def on_loop():
    with SessionLocal() as session:
      return await github_routes._recorded_chat_edits(session, chat_id)

  entries = asyncio.run(on_loop())
  assert [entry["paths"] for entry in entries] == [["/data/platform/backend/app/demo.py"]]
  assert not _converted(chat_id)


def test_contribution_work_for_an_unconverted_source_is_accepted(
  client, owner_token, db, monkeypatch,
):
  auth = {"Authorization": f"Bearer {owner_token}"}
  app_id = create_local_app(client, auth, name="Contribute")["id"]
  create_local_app(client, auth, name="Subagents")
  chat_id = _source_chat(db)
  started = []

  async def record_start(*args, **kwargs):
    started.append(True)

  monkeypatch.setattr(github_routes, "ensure_delegation_started", record_start)
  response = client.post(
    f"/api/github/contributions/{app_id}/for-chat/{chat_id}/work",
    headers=auth, json={"intent": "prepare", "record_ids": []},
  )
  assert response.status_code == 202, response.text
  assert response.json()["work"]["status"] in {"accepted", "starting", "running"}


def test_a_command_that_commits_nothing_never_holds_the_write_lock():
  """The previous review's probe: a no-op command left SQLite's write
  transaction open until the next command."""
  with SessionLocal() as db:
    db.add(create_chat(id="lock-probe", title="Lock", messages=[
      {"role": "user", "content": "hi", "ts": 1, "cid": "dup"}]))
    db.commit()
  _previous_release_wrote("lock-probe", [{"role": "user", "content": "hi", "ts": 1, "cid": "dup"}])
  writer = chat_writer.get_writer()
  # A duplicate append: the writer reads, finds the cid, and returns.
  chat_writer.wait_ack(writer.submit(chat_writer.AppendPending(
    chat_id="lock-probe", user_msg={"role": "user", "content": "hi", "cid": "dup"})))
  driver = writer._db.connection().connection.driver_connection
  assert not driver.in_transaction
  # Another connection can write at once.
  with engine.begin() as conn:
    conn.execute(text("UPDATE chats SET title = 'free' WHERE id = 'lock-probe'"))


def test_a_steer_and_a_wake_read_unconverted_chats(db):
  """The steer dedupe and the wake notice read the recipient's and helper's
  transcripts directly."""
  from app.delegations import _compose_wake_notice
  db.add(create_chat(id="steer-parent", title="Parent", provider="codex", messages=[]))
  db.add(create_chat(id="steer-child", title="Child", provider="codex", messages=[]))
  db.add(models.Delegation(
    id="steer-deleg", parent_chat_id="steer-parent", parent_root_run_id="steer-root",
    task_key="t", child_chat_id="steer-child", provider="codex", scope="write", cwd="/data",
    startup_prompt="task", prompt_sha256="0" * 64))
  db.commit()
  _previous_release_wrote("steer-parent", [{"role": "user", "content": "go", "ts": 5, "cid": "s1"}])
  _previous_release_wrote("steer-child", [
    {"role": "assistant", "content": "", "blocks": [{"type": "text", "content": "child report"}]}])

  async def on_loop():
    with SessionLocal() as session:
      seq = transcript_rows.client_message_seq(session, "steer-parent", "s1")
      stamp = transcript_rows.max_timestamp(session, "steer-parent")
      row = session.get(models.Delegation, "steer-deleg")
      return seq, stamp, _compose_wake_notice(session, [row], {row.id: None})

  seq, stamp, notice = asyncio.run(on_loop())
  assert (seq, stamp) == (0, 5)
  assert "child report" in notice
  assert not _converted("steer-parent") and not _converted("steer-child")
