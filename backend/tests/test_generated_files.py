import asyncio
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import shutil
import uuid

import pytest

from app import generated_files as gf
from app import chat_writer
from app import chat_event_sink
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
  assert res.headers["x-content-type-options"] == "nosniff"


def test_safe_generated_file_preview_opens_inline(client, db, auth, chat):
  stored_name = _stored_file(chat)
  _write_row(db, chat, name="report.pdf", path=stored_name)

  res = client.get(
    f"/api/chats/{chat.id}/generated-files/report.pdf",
    params={"token": _media_token(client, auth, chat.id), "preview": True},
  )

  assert res.status_code == 200
  assert res.headers["content-disposition"] == 'inline; filename="report.pdf"'
  assert res.headers["x-content-type-options"] == "nosniff"


def test_unsafe_generated_file_preview_still_downloads(client, db, auth, chat):
  stored_name = _stored_file(chat, name="stored.svg", content=b"<svg/>")
  _write_row(
    db, chat, name="drawing.svg", path=stored_name,
    mime_type="image/svg+xml",
  )

  res = client.get(
    f"/api/chats/{chat.id}/generated-files/drawing.svg",
    params={"token": _media_token(client, auth, chat.id), "preview": True},
  )

  assert res.status_code == 200
  assert res.headers["content-disposition"] == 'attachment; filename="drawing.svg"'


def test_preview_policy_keeps_active_images_out_of_inline_documents():
  assert gf.previewable_mime_type("image/png") is True
  assert gf.previewable_mime_type("image/svg+xml") is False
  assert gf.previewable_mime_type("text/html") is False


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
  assert res.status_code == 404


def test_managed_store_root_cannot_be_replaced_by_symlink(tmp_path):
  data_dir = str(tmp_path / "data")
  inbox = gf.output_dir(data_dir, "chat", create=True)
  outside = tmp_path / "outside"
  outside.mkdir()
  (inbox.parent / "files").symlink_to(outside, target_is_directory=True)
  (inbox / "report.pdf").write_bytes(b"report")

  assert gf._freeze_file(data_dir, "chat", "report.pdf") is None
  assert list(outside.iterdir()) == []


def test_route_rejects_symlinked_managed_store_root(client, db, auth, chat):
  settings = get_settings()
  managed = gf.stored_dir(settings.data_dir, chat.id, create=True)
  stored_name = "opaque.pdf"
  _write_row(db, chat, name="report.pdf", path=stored_name)
  outside = managed.parent / "outside-store"
  outside.mkdir()
  (outside / stored_name).write_bytes(b"not chat-scoped")
  shutil.rmtree(managed)
  managed.symlink_to(outside, target_is_directory=True)

  res = client.get(
    f"/api/chats/{chat.id}/generated-files/report.pdf",
    params={"token": _media_token(client, auth, chat.id)},
  )

  assert res.status_code == 404


def test_capture_keeps_held_store_when_directory_path_is_swapped(
  tmp_path, monkeypatch,
):
  data_dir = str(tmp_path / "data")
  inbox = gf.output_dir(data_dir, "chat", create=True)
  (inbox / "report.pdf").write_bytes(b"report")
  managed = gf.stored_dir(data_dir, "chat", create=True)
  parked = managed.parent / "parked-store"
  outside = tmp_path / "outside"
  outside.mkdir()
  real_open = gf._open_directory
  swapped = False

  def open_then_swap(directory, *, create):
    nonlocal swapped
    fd = real_open(directory, create=create)
    if directory == managed and not swapped:
      swapped = True
      managed.rename(parked)
      managed.symlink_to(outside, target_is_directory=True)
    return fd

  monkeypatch.setattr(gf, "_open_directory", open_then_swap)

  captured = gf._freeze_file(data_dir, "chat", "report.pdf")

  assert captured is not None
  assert list(outside.iterdir()) == []
  assert (parked / captured["path"]).read_bytes() == b"report"


def test_route_serves_held_inode_when_recorded_path_is_swapped(
  client, db, auth, chat, monkeypatch,
):
  settings = get_settings()
  stored_name = _stored_file(chat, content=b"original")
  _write_row(db, chat, name="report.pdf", path=stored_name)
  managed = gf.stored_dir(settings.data_dir, chat.id)
  outside = managed.parent / "outside.pdf"
  outside.write_bytes(b"secret")
  parked = managed / "parked.pdf"
  real_open = gf.open_stored_file

  def open_then_swap(data_dir, chat_id, name):
    fd, info = real_open(data_dir, chat_id, name)
    (managed / name).rename(parked)
    (managed / name).symlink_to(outside)
    return fd, info

  monkeypatch.setattr(gf, "open_stored_file", open_then_swap)

  res = client.get(
    f"/api/chats/{chat.id}/generated-files/report.pdf",
    params={"token": _media_token(client, auth, chat.id)},
  )

  assert res.status_code == 200
  assert res.content == b"original"


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
  assert all(file["previewable"] is True for file in block["files"])
  rows = db.query(models.GeneratedFile).filter_by(chat_id=chat.id).all()
  assert len(rows) == 2
  assert rows[0].path != rows[1].path
  stored = gf.stored_dir(settings.data_dir, chat.id)
  assert (stored / rows[0].path).read_bytes() == b"first report"
  assert (stored / rows[1].path).read_bytes() == b"second report"

  live = db.get(models.ChatLiveAssistant, chat.id)
  file_blocks = [
    block for block in live.snapshot["blocks"]
    if block.get("type") == "generated_files"
  ]
  assert [file["name"] for file in file_blocks[0]["files"]] == [
    "report.pdf", "report_1.pdf",
  ]


def test_non_regular_swap_is_rejected_without_blocking(tmp_path):
  data_dir = str(tmp_path / "data")
  inbox = gf.output_dir(data_dir, "chat", create=True)
  fifo = inbox / "report.pdf"
  os.mkfifo(fifo)

  assert gf._freeze_file(data_dir, "chat", "report.pdf") is None


def test_oversized_candidates_do_not_starve_later_valid_file(
  tmp_path, monkeypatch,
):
  data_dir = str(tmp_path / "data")
  inbox = gf.output_dir(data_dir, "chat", create=True)
  monkeypatch.setattr(gf, "MAX_RECORDED_BYTES", 4)
  for index in range(gf.MAX_CANDIDATES_PER_TURN):
    (inbox / f"a{index:02}.pdf").write_bytes(b"oversized")
  (inbox / "z-valid.pdf").write_bytes(b"good")

  assert gf._inbox_names(data_dir, "chat") == ["z-valid.pdf"]


def test_full_chat_does_not_recopy_queued_files_on_later_turns(
  db, chat, monkeypatch,
):
  settings = get_settings()
  inbox = gf.output_dir(settings.data_dir, chat.id, create=True)
  (inbox / "report.pdf").write_bytes(b"report")
  _write_row(db, chat, name="existing.pdf", path="existing.pdf")
  monkeypatch.setattr(gf, "MAX_RECORDED_ROWS_PER_CHAT", 1)
  freeze_calls = 0
  original_freeze = gf._freeze_file

  def counted_freeze(*args, **kwargs):
    nonlocal freeze_calls
    freeze_calls += 1
    return original_freeze(*args, **kwargs)

  monkeypatch.setattr(gf, "_freeze_file", counted_freeze)

  for _ in range(2):
    asyncio.run(gf.publish_inbox_files(
      _sink(chat), data_dir=settings.data_dir, chat_id=chat.id,
    ))

  assert (inbox / "report.pdf").read_bytes() == b"report"
  stored = gf._chat_root(settings.data_dir, chat.id) / "files"
  assert not stored.exists() or list(stored.iterdir()) == []
  assert db.query(models.GeneratedFile).filter_by(chat_id=chat.id).count() == 1
  assert freeze_calls == 0


def test_publish_failure_keeps_inbox_and_frozen_copy(chat):
  class FailingSink:
    async def generated_file_capacity(self):
      return 1

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
  assert [item.read_bytes() for item in stored.iterdir()] == [b"report"]


def test_uncertain_publish_keeps_inbox_and_frozen_copy(chat):
  class UncertainSink:
    async def generated_file_capacity(self):
      return 1

    async def publish_generated_file(self, _event):
      return gf.PUBLICATION_UNCERTAIN

  settings = get_settings()
  inbox = gf.output_dir(settings.data_dir, chat.id, create=True)
  (inbox / "report.pdf").write_bytes(b"report")

  asyncio.run(gf.publish_inbox_files(
    UncertainSink(), data_dir=settings.data_dir, chat_id=chat.id,
  ))

  assert (inbox / "report.pdf").read_bytes() == b"report"
  stored = gf.stored_dir(settings.data_dir, chat.id)
  assert [item.read_bytes() for item in stored.iterdir()] == [b"report"]


def test_transcript_failure_rolls_back_generated_row_and_keeps_inbox(
  db, chat, monkeypatch,
):
  settings = get_settings()
  inbox = gf.output_dir(settings.data_dir, chat.id, create=True)
  (inbox / "report.pdf").write_bytes(b"report")
  monkeypatch.setattr(chat_writer, "update_live_assistant", lambda *a, **k: None)

  asyncio.run(gf.publish_inbox_files(
    _sink(chat), data_dir=settings.data_dir, chat_id=chat.id,
  ))

  assert (inbox / "report.pdf").read_bytes() == b"report"
  assert db.query(models.GeneratedFile).filter_by(chat_id=chat.id).count() == 0
  stored = gf.stored_dir(settings.data_dir, chat.id)
  assert list(stored.iterdir()) == []


def test_generated_file_wait_keeps_concurrent_error_and_no_placeholder(
  chat, monkeypatch,
):
  sink = _sink(chat)
  record_ack = Future()
  barrier_snapshots = []

  class Writer:
    def submit(self, command):
      if isinstance(command, chat_writer.RecordGeneratedFile):
        return record_ack
      assert isinstance(command, chat_writer.PersistTranscriptBarrier)
      barrier_snapshots.append(command.snapshot)
      ack = Future()
      ack.set_result(True)
      return ack

  monkeypatch.setattr(chat_event_sink, "get_writer", lambda: Writer())

  async def scenario():
    task = asyncio.create_task(sink.publish_generated_file({
      "type": "generated_file", "name": "report.pdf", "path": "opaque.pdf",
      "size": 6, "mime_type": "application/pdf", "previewable": True,
    }))
    await asyncio.sleep(0)
    sink.publish({
      "type": "error", "message": "Stopped", "resumable": True,
    })
    record_ack.set_result("report.pdf")
    assert await task == "report.pdf"

  asyncio.run(scenario())

  assert [block["type"] for block in sink.assistant_blocks] == [
    "error", "generated_files",
  ]
  assert barrier_snapshots[-1]["blocks"] == sink.assistant_blocks
  assert "__pending_generated_" not in str(barrier_snapshots)


def test_rejected_generated_file_does_not_erase_concurrent_error(
  chat, monkeypatch,
):
  sink = _sink(chat)
  record_ack = Future()

  class Writer:
    def submit(self, command):
      if isinstance(command, chat_writer.RecordGeneratedFile):
        return record_ack
      assert isinstance(command, chat_writer.PersistTranscriptBarrier)
      ack = Future()
      ack.set_result(True)
      return ack

  monkeypatch.setattr(chat_event_sink, "get_writer", lambda: Writer())

  async def scenario():
    task = asyncio.create_task(sink.publish_generated_file({
      "type": "generated_file", "name": "report.pdf", "path": "opaque.pdf",
      "size": 6, "mime_type": "application/pdf", "previewable": True,
    }))
    await asyncio.sleep(0)
    sink.publish({
      "type": "error", "message": "Stopped", "resumable": True,
    })
    record_ack.set_result(None)
    assert await task is None

  asyncio.run(scenario())

  assert [block["type"] for block in sink.assistant_blocks] == ["error"]


def test_generated_file_timeout_preserves_late_writer_commit(
  db, chat, monkeypatch,
):
  settings = get_settings()
  inbox = gf.output_dir(settings.data_dir, chat.id, create=True)
  (inbox / "report.pdf").write_bytes(b"report")
  sink = _sink(chat)
  chat_writer.get_writer().submit(chat_writer.ReplaceTranscript(
    chat_id=chat.id,
    messages=[{
      "role": "user", "content": "make a report", "ts": 1, "cid": "u1",
    }],
  )).result(timeout=5)
  from app.database import SessionLocal
  writer = chat_writer.ChatWriterActor(session_factory=SessionLocal)
  writer.pause_for_test()
  writer.start()
  assert writer._session_ready.wait(timeout=5)
  monkeypatch.setattr(chat_event_sink, "get_writer", lambda: writer)
  sink.assistant_blocks.append({"type": "text", "content": "Done"})

  async def capacity_available():
    return 1

  sink.generated_file_capacity = capacity_available

  async def scenario():
    monkeypatch.setattr(chat_writer, "ACK_TIMEOUT_SECS", 0.01)
    await gf.publish_inbox_files(
      sink, data_dir=settings.data_dir, chat_id=chat.id,
    )
    assert (inbox / "report.pdf").read_bytes() == b"report"
    sink.publish({
      "type": "error", "message": "Stopped", "resumable": True,
    })
    monkeypatch.setattr(chat_writer, "ACK_TIMEOUT_SECS", 2)
    finalize = asyncio.create_task(sink.finalize())
    await asyncio.sleep(0.02)
    writer.resume_for_test()
    await finalize

  try:
    asyncio.run(scenario())
  finally:
    writer.resume_for_test()
    writer.stop(timeout=5)

  db.expire_all()
  row = db.query(models.GeneratedFile).filter_by(chat_id=chat.id).one()
  assert row.name == "report.pdf"
  assert not (inbox / "report.pdf").exists()
  assert [item.name for item in gf.stored_dir(
    settings.data_dir, chat.id,
  ).iterdir()] == [row.path]
  db.refresh(chat)
  assistant = chat.messages[-1]
  assert assistant["role"] == "assistant"
  assert [block["type"] for block in assistant["blocks"]] == [
    "text", "error", "generated_files",
  ]
  assert assistant["blocks"][-1]["files"][0]["name"] == "report.pdf"
  assert db.get(models.ChatLiveAssistant, chat.id) is None

  asyncio.run(gf.publish_inbox_files(
    _sink(chat), data_dir=settings.data_dir, chat_id=chat.id,
  ))
  db.expire_all()
  assert db.query(models.GeneratedFile).filter_by(chat_id=chat.id).count() == 1
  assert len(list(gf.stored_dir(settings.data_dir, chat.id).iterdir())) == 1


def test_generated_file_timeout_removes_frozen_copy_after_confirmed_rejection(
  chat, monkeypatch,
):
  settings = get_settings()
  inbox = gf.output_dir(settings.data_dir, chat.id, create=True)
  (inbox / "report.pdf").write_bytes(b"report")
  captured = gf._freeze_file(settings.data_dir, chat.id, "report.pdf")
  assert captured is not None
  sink = _sink(chat)
  sink._uncertain_generated_files[captured["path"]] = {
    "event": {
      "type": "generated_file", "name": "report.pdf",
      "path": captured["path"], "size": captured["size"],
      "mime_type": captured["mime_type"], "previewable": True,
    },
    "data_dir": settings.data_dir,
    "captured": captured,
  }

  class Writer:
    def submit(self, command):
      assert isinstance(command, chat_writer.ResolveGeneratedFilePublication)
      ack = Future()
      ack.set_result(None)
      return ack

  monkeypatch.setattr(chat_event_sink, "get_writer", lambda: Writer())
  asyncio.run(sink._resolve_uncertain_generated_files())

  assert (inbox / "report.pdf").read_bytes() == b"report"
  assert list(gf.stored_dir(settings.data_dir, chat.id).iterdir()) == []
  assert sink._uncertain_generated_files == {}


def test_inbox_accepts_unknown_formats_but_ignores_symlinks(tmp_path):
  inbox = gf.output_dir(str(tmp_path), "chat", create=True)
  outside = tmp_path / "secret.pdf"
  outside.write_bytes(b"secret")
  (inbox / "alias.pdf").symlink_to(outside)
  (inbox / "notes.txt").write_text("plain text")
  (inbox / "custom.unknown").write_bytes(b"custom")
  (inbox / "README").write_text("extensionless")

  assert gf._inbox_names(str(tmp_path), "chat") == [
    "README", "custom.unknown", "notes.txt",
  ]


def test_unknown_format_is_frozen_with_an_opaque_storage_name(tmp_path):
  data_dir = str(tmp_path / "data")
  inbox = gf.output_dir(data_dir, "chat", create=True)
  (inbox / "artifact.custom").write_bytes(b"custom")

  captured = gf._freeze_file(data_dir, "chat", "artifact.custom")

  assert captured is not None
  assert captured["mime_type"] == "application/octet-stream"
  assert Path(captured["path"]).suffix == ""
  assert (
    gf.stored_dir(data_dir, "chat") / captured["path"]
  ).read_bytes() == b"custom"


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
