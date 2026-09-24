import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
import uuid

import pytest

from app import generated_files as gf
from app import models
from app.broadcast import ChatBroadcast
from app.chat_event_sink import ChatEventSink
from app.chat_media import fix_forward_chat_media
from app.chat_retention import purge_expired_chat_tombstones
from app.config import get_settings
from app.agent_activity import EMPTY_AGENT_ACTIVITY_BINDING


def _write_row(db, chat, *, name, path, size=11, mime_type="application/pdf"):
  row = models.GeneratedFile(
    chat_id=chat.id, name=name, path=path, size=size, mime_type=mime_type,
  )
  db.add(row)
  db.commit()
  return row


def _stored_file(chat, name="stored.pdf", content=b"%PDF-1.4 fake"):
  settings = get_settings()
  directory = gf.stored_dir(settings.data_dir, chat.id, create=True)
  (directory / name).write_bytes(content)
  return name


def _sink(chat):
  return ChatEventSink(
    ChatBroadcast(chat.id), chat.id, run_token="rt-generated",
    agent_activity_binding=EMPTY_AGENT_ACTIVITY_BINDING,
  )


def _media_token(client, auth, chat_id):
  return client.post(
    f"/api/chats/{chat_id}/media-token", headers=auth,
  ).json()["token"]


def test_serve_generated_file_by_recorded_name(client, db, auth, chat):
  stored_name = _stored_file(chat)
  _write_row(db, chat, name="report.pdf", path=stored_name)

  res = client.get(
    f"/api/chats/{chat.id}/generated-files/report.pdf",
    params={"token": _media_token(client, auth, chat.id)},
  )

  assert res.status_code == 200
  assert res.content == b"%PDF-1.4 fake"
  assert res.headers["content-disposition"] == 'attachment; filename="report.pdf"'


def test_serve_generated_file_unknown_name_404s(client, auth, chat):
  res = client.get(
    f"/api/chats/{chat.id}/generated-files/never-recorded.pdf",
    params={"token": _media_token(client, auth, chat.id)},
  )
  assert res.status_code == 404


def test_serve_generated_file_rejects_recorded_path_escape(
  client, db, auth, chat,
):
  _write_row(db, chat, name="evil.pdf", path="../outside.pdf")

  res = client.get(
    f"/api/chats/{chat.id}/generated-files/evil.pdf",
    params={"token": _media_token(client, auth, chat.id)},
  )
  assert res.status_code == 400


def test_serve_generated_file_rejects_post_record_symlink_escape(
  client, db, auth, chat,
):
  settings = get_settings()
  second_chat = models.Chat(
    id=str(uuid.uuid4()), title="Second test chat", messages=[],
    agent_settings_json={"model": "claude-opus-4-8"},
  )
  db.add(second_chat)
  db.commit()
  other = gf.stored_dir(settings.data_dir, second_chat.id, create=True) / "secret.pdf"
  other.write_bytes(b"other chat")
  mine = gf.stored_dir(settings.data_dir, chat.id, create=True)
  (mine / "alias.pdf").symlink_to(other)
  _write_row(db, chat, name="alias.pdf", path="alias.pdf")

  res = client.get(
    f"/api/chats/{chat.id}/generated-files/alias.pdf",
    params={"token": _media_token(client, auth, chat.id)},
  )
  assert res.status_code == 400


def test_generated_file_row_is_scoped_to_its_chat(client, db, auth, chat):
  second_chat = models.Chat(
    id=str(uuid.uuid4()), title="Second test chat", messages=[],
    agent_settings_json={"model": "claude-opus-4-8"},
  )
  db.add(second_chat)
  db.commit()
  stored_name = _stored_file(chat, name="private.csv", content=b"a,b,c\n")
  _write_row(
    db, chat, name="shared-name.csv", path=stored_name, mime_type="text/csv",
  )

  res = client.get(
    f"/api/chats/{second_chat.id}/generated-files/shared-name.csv",
    params={"token": _media_token(client, auth, second_chat.id)},
  )
  assert res.status_code == 404


def test_inbox_capture_is_immutable_across_same_name_regeneration(db, chat):
  settings = get_settings()
  inbox = gf.output_dir(settings.data_dir, chat.id, create=True)
  sink = _sink(chat)

  (inbox / "report.pdf").write_bytes(b"first report")
  asyncio.run(gf.publish_inbox_files(
    sink, data_dir=settings.data_dir, chat_id=chat.id,
  ))
  assert not (inbox / "report.pdf").exists()

  (inbox / "report.pdf").write_bytes(b"second report")
  asyncio.run(gf.publish_inbox_files(
    sink, data_dir=settings.data_dir, chat_id=chat.id,
  ))

  block = next(
    item for item in sink.assistant_blocks
    if item.get("type") == "generated_files"
  )
  assert [file["name"] for file in block["files"]] == [
    "report.pdf", "report_1.pdf",
  ]
  rows = db.query(models.GeneratedFile).filter_by(chat_id=chat.id).all()
  assert len(rows) == 2
  assert rows[0].path != rows[1].path
  stored = gf.stored_dir(settings.data_dir, chat.id)
  assert (stored / rows[0].path).read_bytes() == b"first report"
  assert (stored / rows[1].path).read_bytes() == b"second report"


def test_rejected_capture_keeps_inbox_and_removes_frozen_copy(
  db, chat, monkeypatch,
):
  settings = get_settings()
  inbox = gf.output_dir(settings.data_dir, chat.id, create=True)
  (inbox / "report.pdf").write_bytes(b"report")
  monkeypatch.setattr(gf, "MAX_RECORDED_ROWS_PER_CHAT", 0)

  asyncio.run(gf.publish_inbox_files(
    _sink(chat), data_dir=settings.data_dir, chat_id=chat.id,
  ))

  assert (inbox / "report.pdf").read_bytes() == b"report"
  stored = gf.stored_dir(settings.data_dir, chat.id)
  assert not stored.exists() or list(stored.iterdir()) == []
  assert db.query(models.GeneratedFile).filter_by(chat_id=chat.id).count() == 0


def test_publish_failure_keeps_inbox_and_removes_frozen_copy(chat):
  class FailingSink:
    async def publish_generated_file(self, _event):
      raise RuntimeError("writer unavailable")

  settings = get_settings()
  inbox = gf.output_dir(settings.data_dir, chat.id, create=True)
  (inbox / "report.pdf").write_bytes(b"report")

  with pytest.raises(RuntimeError, match="writer unavailable"):
    asyncio.run(gf.publish_inbox_files(
      FailingSink(), data_dir=settings.data_dir, chat_id=chat.id,
    ))

  assert (inbox / "report.pdf").read_bytes() == b"report"
  stored = gf.stored_dir(settings.data_dir, chat.id)
  assert not stored.exists() or list(stored.iterdir()) == []


def test_inbox_ignores_nested_symlink_and_unapproved_extension(tmp_path):
  inbox = gf.output_dir(str(tmp_path), "chat", create=True)
  outside = tmp_path / "secret.pdf"
  outside.write_bytes(b"secret")
  (inbox / "alias.pdf").symlink_to(outside)
  (inbox / "notes.txt").write_text("not a deliverable")

  assert gf._inbox_names(str(tmp_path), "chat") == []


def test_deliverables_namespace_survives_legacy_media_fix_forward(db, chat):
  settings = get_settings()
  stored_name = _stored_file(chat, content=b"stable")
  _write_row(db, chat, name="report.pdf", path=stored_name)

  assert fix_forward_chat_media(db, settings.data_dir) == 0
  assert (
    gf.stored_dir(settings.data_dir, chat.id) / stored_name
  ).read_bytes() == b"stable"


def test_output_dir_cannot_escape_chat_root(tmp_path):
  directory = gf.output_dir(str(tmp_path), "../outside", create=True)
  assert directory.is_relative_to(tmp_path / "chats")
  assert directory.name == "inbox"


def test_hard_purge_removes_generated_file_rows(db, chat):
  _write_row(db, chat, name="report.pdf", path="stored.pdf")
  chat_id = chat.id
  chat.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()

  purge_expired_chat_tombstones(db)

  assert db.get(models.Chat, chat_id) is None
  assert db.query(models.GeneratedFile).filter_by(chat_id=chat_id).first() is None


def test_non_ascii_filename_downloads_instead_of_500(client, db, auth, chat):
  stored_name = _stored_file(chat, content=b"%PDF-1.4 cjk")
  _write_row(db, chat, name="报告.pdf", path=stored_name)

  res = client.get(
    f"/api/chats/{chat.id}/generated-files/报告.pdf",
    params={"token": _media_token(client, auth, chat.id)},
  )
  assert res.status_code == 200
  assert res.content == b"%PDF-1.4 cjk"
  assert "filename*=utf-8''" in res.headers["content-disposition"]
