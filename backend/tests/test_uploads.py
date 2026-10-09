# backend/tests/test_uploads.py
import io

import pytest

from PIL import Image

from app import models, transcript_rows


def test_upload_single_file(client, db, auth, chat):
  """POST /api/chats/{id}/uploads stores file and returns record."""
  data = io.BytesIO(b"hello world")
  res = client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("hello.txt", data, "text/plain"))],
    headers=auth,
  )
  assert res.status_code == 200
  records = res.json()
  assert len(records) == 1
  assert records[0]["name"] == "hello.txt"
  assert records[0]["size"] == 11
  assert records[0]["mime_type"] == "text/plain"

  db.refresh(chat)
  assert len(chat.uploads) == 1
  assert chat.uploads[0]["name"] == "hello.txt"


def test_upload_files_rejects_cross_site_request(client, auth, chat):
  data = io.BytesIO(b"hello world")
  cross = client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("hello.txt", data, "text/plain"))],
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_upload_deduplicates_filename(client, db, auth, chat):
  """Second upload with same name gets a numeric suffix."""
  for _ in range(2):
    client.post(
      f"/api/chats/{chat.id}/uploads",
      files=[("files", ("photo.png", io.BytesIO(b"data"), "image/png"))],
      headers=auth,
    )
  db.refresh(chat)
  names = [u["name"] for u in chat.uploads]
  assert "photo.png" in names
  assert "photo_1.png" in names


def test_upload_shortens_a_name_longer_than_the_filesystem_allows(
  client, db, auth, chat,
):
  """A 300-byte name (150 accented letters) is shortened, keeping its type."""
  long_name = "é" * 150 + ".png"
  for _ in range(2):
    res = client.post(
      f"/api/chats/{chat.id}/uploads",
      files=[("files", (long_name, io.BytesIO(b"data"), "image/png"))],
      headers=auth,
    )
    assert res.status_code == 200, res.text
  db.refresh(chat)
  names = [u["name"] for u in chat.uploads]
  assert len(set(names)) == 2
  for name in names:
    assert name.endswith(".png")
    assert len(name.encode("utf-8")) <= 255
    served = client.get(f"/api/chats/{chat.id}/uploads/{name}", headers=auth)
    assert served.status_code == 200


def test_list_uploads(client, db, auth, chat):
  """GET /api/chats/{id}/uploads returns the stored upload list."""
  client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("a.txt", io.BytesIO(b"x"), "text/plain"))],
    headers=auth,
  )
  res = client.get(f"/api/chats/{chat.id}/uploads", headers=auth)
  assert res.status_code == 200
  assert len(res.json()) == 1


def test_serve_uploaded_file(client, db, auth, chat):
  """GET /api/chats/{id}/uploads/{filename} returns the file content.

  Uses a media token on ?token= (owner JWTs are rejected on that path to
  prevent the 30-day token from leaking into access logs/history/Referer).
  """
  client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("note.txt", io.BytesIO(b"secret"), "text/plain"))],
    headers=auth,
  )
  media_token_r = client.post(
    f"/api/chats/{chat.id}/media-token",
    headers=auth,
  )
  media_token = media_token_r.json()["token"]
  res = client.get(
    f"/api/chats/{chat.id}/uploads/note.txt",
    params={"token": media_token},
  )
  assert res.status_code == 200
  assert res.content == b"secret"


def test_serve_uploaded_image_preview_without_touching_original(
  client, db, auth, chat,
):
  image_bytes = io.BytesIO()
  Image.new("RGB", (1800, 1200), (42, 91, 130)).save(image_bytes, "PNG")
  original = image_bytes.getvalue()
  client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("photo.png", io.BytesIO(original), "image/png"))],
    headers=auth,
  )
  media_token = client.post(
    f"/api/chats/{chat.id}/media-token", headers=auth,
  ).json()["token"]

  preview = client.get(
    f"/api/chats/{chat.id}/uploads/photo.png",
    params={"token": media_token, "preview": "true"},
  )

  assert preview.status_code == 200
  assert preview.headers["content-type"] == "image/webp"
  with Image.open(io.BytesIO(preview.content)) as image:
    assert max(image.size) == 1024

  full = client.get(
    f"/api/chats/{chat.id}/uploads/photo.png",
    params={"token": media_token},
  )
  assert full.content == original


def test_upload_rejects_missing_chat(client, auth):
  """Upload to a well-formed but non-existent chat_id must return 404."""
  import uuid
  res = client.post(
    f"/api/chats/{uuid.uuid4()}/uploads",
    files=[("files", ("x.txt", io.BytesIO(b"x"), "text/plain"))],
    headers=auth,
  )
  assert res.status_code == 404


def test_delete_upload(client, db, auth, chat):
  """DELETE /api/chats/{id}/uploads/{filename} removes file and DB entry."""
  client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("remove-me.txt", io.BytesIO(b"bye"), "text/plain"))],
    headers=auth,
  )
  db.refresh(chat)
  assert len(chat.uploads) == 1

  res = client.delete(
    f"/api/chats/{chat.id}/uploads/remove-me.txt",
    headers=auth,
  )
  assert res.status_code == 204

  db.refresh(chat)
  assert len(chat.uploads) == 0


def test_delete_upload_rejects_cross_site_request(client, auth, chat):
  cross = client.delete(
    f"/api/chats/{chat.id}/uploads/remove-me.txt",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross.status_code == 403


def test_delete_upload_missing_chat(client, auth):
  """DELETE to a well-formed but non-existent chat_id returns 404."""
  import uuid
  res = client.delete(f"/api/chats/{uuid.uuid4()}/uploads/any.txt", headers=auth)
  assert res.status_code == 404


def test_delete_upload_leaves_others_intact(client, db, auth, chat):
  """Deleting one upload does not affect other uploads on the same chat."""
  r1 = client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("keep-one.txt", io.BytesIO(b"aaa"), "text/plain"))],
    headers=auth,
  )
  r2 = client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("keep-two.txt", io.BytesIO(b"bbb"), "text/plain"))],
    headers=auth,
  )
  name1 = r1.json()[0]["name"]
  name2 = r2.json()[0]["name"]
  db.refresh(chat)
  assert len(chat.uploads) == 2

  client.delete(f"/api/chats/{chat.id}/uploads/{name1}", headers=auth)
  db.refresh(chat)
  assert len(chat.uploads) == 1
  assert chat.uploads[0]["name"] == name2


def test_delete_upload_missing_file_still_cleans_db(client, db, auth, chat):
  """DELETE succeeds even if the file was already removed from disk."""
  client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("ghost.txt", io.BytesIO(b"x"), "text/plain"))],
    headers=auth,
  )
  db.refresh(chat)
  import pathlib, os
  from app.config import get_settings
  fpath = pathlib.Path(get_settings().data_dir) / "chats" / chat.id / "uploads" / "ghost.txt"
  if fpath.exists():
    fpath.unlink()

  res = client.delete(
    f"/api/chats/{chat.id}/uploads/ghost.txt",
    headers=auth,
  )
  assert res.status_code == 204
  db.refresh(chat)
  assert len(chat.uploads) == 0


def test_upload_rejects_invalid_chat_id_format(client, auth):
  """Upload to a chat_id that is not a UUID4 must return 400 (Task 2 path hygiene)."""
  import io
  res = client.post(
    "/api/chats/../etc/passwd/uploads",
    files=[("files", ("x.txt", io.BytesIO(b"x"), "text/plain"))],
    headers=auth,
  )
  # FastAPI path routing may normalise .. but the slug 'etc' is not a UUID4.
  assert res.status_code in (400, 404, 422)


def test_upload_rejects_non_uuid_chat_id(client, auth):
  """chat_id that looks like a path component but isn't a UUID4 returns 400."""
  import io
  res = client.post(
    "/api/chats/not-a-uuid/uploads",
    files=[("files", ("x.txt", io.BytesIO(b"x"), "text/plain"))],
    headers=auth,
  )
  assert res.status_code == 400


def test_serve_upload_rejects_non_uuid_chat_id(client, auth):
  """Serve endpoint rejects non-UUID4 chat_id with 400 (Task 2)."""
  from app.auth import create_access_token
  token = create_access_token({"sub": "test"})
  res = client.get(
    "/api/chats/not-a-uuid/uploads/file.txt",
    params={"token": token},
  )
  assert res.status_code == 400


def test_upload_does_not_impose_a_per_chat_directory_quota(
  client, db, auth, chat,
):
  """Existing chat media must not turn a healthy disk into a chat-local 413."""
  from pathlib import Path
  from app.config import get_settings

  upload_dir = Path(get_settings().data_dir) / "chats" / chat.id / "uploads"
  upload_dir.mkdir(parents=True, exist_ok=True)
  # Sparse allocation proves behavior beyond the retired 200 MB ceiling
  # without consuming 200 MB in the test runtime.
  with (upload_dir / "existing-video.mp4").open("wb") as existing:
    existing.truncate(201 * 1024 * 1024)

  res = client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("next.txt", io.BytesIO(b"ok"), "text/plain"))],
    headers=auth,
  )

  assert res.status_code == 200
  assert res.json()[0]["name"] == "next.txt"


def test_upload_multi_file_over_cap_cleans_partial(client, db, auth, chat, monkeypatch):
  """If a later file in a multi-file upload exceeds the cap, the files already
  written this request are removed — no orphan on disk without a metadata row."""
  import os
  import pathlib
  import sys
  from app.config import get_settings
  # Patch the cap everywhere it could be read: the live route's own module
  # globals AND every `app.routes.uploads` object in sys.modules — a sibling
  # test may have reloaded the module into a second instance, so a plain
  # `monkeypatch.setattr(uploads, ...)` could patch the wrong one.
  for mod in list(sys.modules.values()):
    if getattr(mod, "__name__", "") == "app.routes.uploads":
      monkeypatch.setattr(mod, "_MAX_UPLOAD_BYTES", 10, raising=False)
  ep = next(
    (r.endpoint for r in client.app.routes
     if getattr(r, "path", None) == "/api/chats/{chat_id}/uploads"
     and "POST" in getattr(r, "methods", set())),
    None,
  )
  if ep is not None:
    monkeypatch.setitem(ep.__globals__, "_MAX_UPLOAD_BYTES", 10)
  res = client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[
      ("files", ("small.txt", io.BytesIO(b"ok"), "text/plain")),      # fits
      ("files", ("big.txt", io.BytesIO(b"x" * 50), "text/plain")),    # over cap
    ],
    headers=auth,
  )
  assert res.status_code == 413
  # The file written before the cap was hit was cleaned up — assert the specific
  # name is gone (robust to any unrelated files a shared fixture left here).
  upload_dir = pathlib.Path(get_settings().data_dir) / "chats" / chat.id / "uploads"
  leftover = os.listdir(upload_dir) if upload_dir.is_dir() else []
  assert not any(n.startswith("small") for n in leftover), (
    f"partial upload left an orphan: {leftover}"
  )
  db.refresh(chat)
  assert (chat.uploads or []) == []


def _upload(client, auth, chat, name, body=b"draft"):
  return client.post(
    f"/api/chats/{chat.id}/uploads", headers=auth,
    files=[("files", (name, io.BytesIO(body), "text/plain"))],
  ).json()[0]


def _discard(client, auth, chat, name):
  return client.delete(f"/api/chats/{chat.id}/uploads/{name}", headers=auth)


def test_new_uploads_are_unclaimed_drafts_without_secrets(client, db, auth, chat):
  record = _upload(client, auth, chat, "draft.txt")
  assert record["claimed"] is False
  assert "discard_token" not in record
  listed = client.get(f"/api/chats/{chat.id}/uploads", headers=auth).json()
  assert listed == [record]


def test_discard_removes_only_unclaimed_drafts(client, db, auth, chat):
  from pathlib import Path
  draft = _upload(client, auth, chat, "draft.txt")
  claimed = _upload(client, auth, chat, "claimed.txt")
  legacy = _upload(client, auth, chat, "legacy.txt")
  db.refresh(chat)
  chat.uploads = [
    draft,
    {**claimed, "claimed": True},
    {k: v for k, v in legacy.items() if k != "claimed"},
  ]
  db.commit()
  stray = Path(draft["path"]).parent / "stray.txt"
  stray.write_text("on disk but never recorded as an upload")

  for name in ("claimed.txt", "legacy.txt", "stray.txt", "missing.txt", "caf\u00e9.txt"):
    assert _discard(client, auth, chat, name).status_code == 204
  for path in (claimed["path"], legacy["path"], stray):
    assert Path(path).exists()

  assert _discard(client, auth, chat, "draft.txt").status_code == 204
  assert not Path(draft["path"]).exists()
  db.refresh(chat)
  assert [u["name"] for u in chat.uploads] == ["claimed.txt", "legacy.txt"]


def test_admitted_messages_claim_their_uploads(client, db, auth, chat):
  from pathlib import Path
  from app.chat_writer import get_writer, AppendPending, ClearPending, StartTurn

  started = _upload(client, auth, chat, "started.txt")
  queued = _upload(client, auth, chat, "queued.txt")
  untouched = _upload(client, auth, chat, "untouched.txt")
  writer = get_writer()
  writer.submit(StartTurn(
    chat_id=chat.id, run_token="claim-run",
    user_msg={"role": "user", "content": "see file", "ts": 5,
              "attachments": [{"name": started["name"]}]},
    title_source="see file",
  )).result(timeout=5)
  writer.submit(AppendPending(
    chat_id=chat.id, user_msg={"role": "user", "content": "and this",
                               "attachments": [{"name": queued["name"]}]},
  )).result(timeout=5)
  writer.submit(ClearPending(chat_id=chat.id)).result(timeout=5)

  db.refresh(chat)
  claimed = {u["name"]: u["claimed"] for u in chat.uploads}
  assert claimed == {"started.txt": True, "queued.txt": True, "untouched.txt": False}
  for record in (started, queued):
    assert _discard(client, auth, chat, record["name"]).status_code == 204
    assert Path(record["path"]).exists()
  assert _discard(client, auth, chat, untouched["name"]).status_code == 204
  assert not Path(untouched["path"]).exists()


def test_expired_drafts_are_swept_on_next_upload(client, db, auth, chat):
  from datetime import UTC, datetime, timedelta
  from pathlib import Path
  from app.upload_lifecycle import UNCLAIMED_UPLOAD_TTL

  old = (datetime.now(UTC) - UNCLAIMED_UPLOAD_TTL - timedelta(hours=1)).isoformat()
  stale = _upload(client, auth, chat, "stale.txt")
  sent = _upload(client, auth, chat, "sent.txt")
  legacy = _upload(client, auth, chat, "legacy.txt")
  fresh = _upload(client, auth, chat, "fresh.txt")
  db.refresh(chat)
  chat.uploads = [
    {**stale, "uploaded_at": old},
    {**sent, "uploaded_at": old, "claimed": True},
    {k: v for k, v in {**legacy, "uploaded_at": old}.items() if k != "claimed"},
    fresh,
  ]
  db.commit()

  _upload(client, auth, chat, "next.txt")
  db.refresh(chat)
  assert [u["name"] for u in chat.uploads] == [
    "sent.txt", "legacy.txt", "fresh.txt", "next.txt",
  ]
  assert not Path(stale["path"]).exists()
  for record in (sent, legacy, fresh):
    assert Path(record["path"]).exists()


def test_discard_waits_for_admission_and_rereads(client, db, auth, chat):
  import asyncio
  from pathlib import Path
  from app import chat_queue
  from app.deps import Principal
  from app.routes.uploads import delete_upload

  record = _upload(client, auth, chat, "race.txt", b"keep")
  principal = Principal(owner=db.query(models.Owner).first(), app_id=None)

  async def race():
    async with chat_queue.get_lock(chat.id):
      discard = asyncio.create_task(delete_upload(
        chat.id, record["name"], principal=principal, db=db,
      ))
      await asyncio.sleep(0)
      assert not discard.done()
      from app.chat_writer import get_writer, AppendPending
      await asyncio.wrap_future(get_writer().submit(AppendPending(
        chat_id=chat.id,
        user_msg={"role": "user", "content": "x", "attachments": [{"name": record["name"]}]},
      )))
    response = await discard
    assert response.status_code == 204

  asyncio.run(race())
  assert Path(record["path"]).exists()


def test_upload_reads_outside_admission_then_commits_under_lock(db, chat):
  import asyncio
  from pathlib import Path
  from fastapi import UploadFile
  from app import chat_queue
  from app.deps import Principal
  from app.routes.uploads import upload_files
  from app.config import get_settings

  principal = Principal(owner=db.query(models.Owner).first(), app_id=None)

  async def race():
    read_finished = asyncio.Event()

    class ObservedUpload(UploadFile):
      async def read(self, size=-1):
        result = await super().read(size)
        if not result:
          read_finished.set()
        return result

    file = ObservedUpload(file=io.BytesIO(b"new"), filename="outside.txt")
    async with chat_queue.get_lock(chat.id):
      upload = asyncio.create_task(upload_files(chat.id, [file], principal, db))
      await asyncio.wait_for(read_finished.wait(), timeout=1)
      assert not upload.done(), "body read must finish without admission; placement must wait"
      db.refresh(chat)
      assert not chat.uploads
    return await upload

  records = asyncio.run(race())
  assert Path(records[0]["path"]).read_bytes() == b"new"
  upload_dir = Path(get_settings().data_dir) / "chats" / chat.id / "uploads"
  assert [p.name for p in upload_dir.iterdir()] == ["outside.txt"]


def test_session_file_notice_marks_own_files_and_hides_unsent_drafts(client, db, auth, chat):
  """The agent sees sent files and this message's own (marked), never drafts."""
  from app.routes.chats_stream import _content_with_uploads

  _upload(client, auth, chat, "report.pdf")
  _upload(client, auth, chat, "sent.pdf")
  _upload(client, auth, chat, "draft.pdf")
  db.refresh(chat)
  chat.uploads = [
    {**u, "claimed": True} if u["name"] == "sent.pdf" else u for u in chat.uploads
  ]
  db.commit()

  lines = _content_with_uploads(chat, "see attached", [{"name": "report.pdf"}]).splitlines()

  assert any("report.pdf" in line and "attached to this message" in line for line in lines)
  assert any("sent.pdf" in line and "attached to this message" not in line for line in lines)
  assert not any("draft.pdf" in line for line in lines)


def test_pending_edit_keeps_the_rows_attachment_mark(client, db, auth, chat):
  """Re-deriving the notice on a queued edit still marks the row's own files."""
  chat.pending_messages = [
    {"role": "user", "content": "before", "ts": 100, "cid": "c-mark",
     "attachments": [{"name": "report.pdf"}]},
  ]
  db.commit()
  _upload(client, auth, chat, "report.pdf")

  resp = client.patch(
    f"/api/chats/{chat.id}/pending/c-mark", headers=auth, json={"content": "after"},
  )

  assert resp.status_code == 200, resp.text
  edited = resp.json()["pending_messages"][0]["content"]
  assert "report.pdf" in edited and "attached to this message" in edited


def test_cancelled_queued_message_releases_files_no_other_message_names(client, db, auth, chat):
  from pathlib import Path
  from app.chat_writer import get_writer, AppendPending, CancelPending

  alone = _upload(client, auth, chat, "alone.txt")
  shared = _upload(client, auth, chat, "shared.txt")
  writer = get_writer()
  for cid, names in (("c-cancel", [alone, shared]), ("c-keep", [shared])):
    writer.submit(AppendPending(chat_id=chat.id, user_msg={
      "role": "user", "content": cid, "cid": cid,
      "attachments": [{"name": r["name"]} for r in names],
    })).result(timeout=5)
  writer.submit(CancelPending(chat_id=chat.id, cid="c-cancel")).result(timeout=5)

  db.refresh(chat)
  assert {u["name"]: u["claimed"] for u in chat.uploads} == {"alone.txt": False, "shared.txt": True}
  assert _discard(client, auth, chat, shared["name"]).status_code == 204
  assert Path(shared["path"]).exists()
  assert _discard(client, auth, chat, alone["name"]).status_code == 204
  assert not Path(alone["path"]).exists()


def test_expired_drafts_are_swept_when_a_message_arrives_but_not_its_own(client, db, auth, chat):
  from datetime import UTC, datetime, timedelta
  from pathlib import Path
  from unittest.mock import patch
  from app.upload_lifecycle import UNCLAIMED_UPLOAD_TTL

  old = (datetime.now(UTC) - UNCLAIMED_UPLOAD_TTL - timedelta(hours=1)).isoformat()
  stale = _upload(client, auth, chat, "stale.txt")
  restored = _upload(client, auth, chat, "restored.txt")
  db.refresh(chat)
  chat.uploads = [{**stale, "uploaded_at": old}, {**restored, "uploaded_at": old}]
  db.commit()

  async def fake_run_chat(*args, **kwargs):
    return None

  with patch("app.routes.chats_stream.run_chat", new=fake_run_chat):
    res = client.post(f"/api/chats/{chat.id}/messages", headers=auth, json={
      "content": "sending an old draft", "attachments": [{"name": "restored.txt"}],
    })
  assert res.status_code == 202, res.text

  db.refresh(chat)
  assert [(u["name"], u["claimed"]) for u in chat.uploads] == [("restored.txt", True)]
  assert not Path(stale["path"]).exists()
  assert Path(restored["path"]).exists()


@pytest.mark.parametrize("where", ["transcript", "live"])
def test_cancel_keeps_files_an_answered_card_still_shows(client, db, auth, chat, where):
  """Covers cards in the transcript and cards answered mid-turn (live row)."""
  from app.chat_writer import get_writer, AppendPending, CancelPending

  shown = _upload(client, auth, chat, "shown.txt")
  card = {"role": "assistant", "content": "", "ts": 1, "blocks": [{
    "type": "question", "question_id": "q", "answers": {"Q?": "Attached 1 file"},
    "attachments": [{"name": shown["name"]}],
  }]}
  if where == "live":
    chat.live_assistant = card
  else:
    transcript_rows.replace_all(db, chat, [card])
  db.commit()
  get_writer().submit(AppendPending(chat_id=chat.id, user_msg={
    "role": "user", "content": "answer", "cid": "c-answer",
    "attachments": [{"name": shown["name"]}],
  })).result(timeout=5)
  get_writer().submit(CancelPending(chat_id=chat.id, cid="c-answer")).result(timeout=5)

  db.refresh(chat)
  assert chat.uploads[0]["claimed"] is True


def test_failed_upload_write_leaves_no_file_behind(tmp_path, monkeypatch):
  """A write that dies part-way never leaves a partial file or temp file."""
  import os
  from app.routes import uploads

  def broken_fsync(fd):
    raise OSError("disk full")

  monkeypatch.setattr(os, "fsync", broken_fsync)
  with pytest.raises(OSError):
    uploads._create_upload_file(tmp_path, "report.pdf", b"data")
  assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("failure", ["commit", "cancelled"])
def test_upload_cleanup_only_when_the_record_did_not_land(db, chat, monkeypatch, failure):
  """A failed commit removes the new files; a cancel after commit keeps them."""
  import asyncio
  from pathlib import Path
  from fastapi import UploadFile
  from app.deps import Principal
  from app.routes import uploads
  from app.upload_lifecycle import upload_dir

  real_record = uploads._record_uploads

  def record(db_, chat_, saved):
    if failure == "commit":
      raise RuntimeError("commit failed")
    real_record(db_, chat_, saved)
    raise asyncio.CancelledError()

  monkeypatch.setattr(uploads, "_record_uploads", record)
  principal = Principal(owner=db.query(models.Owner).first(), app_id=None)
  file = UploadFile(file=io.BytesIO(b"data"), filename="kept.txt")
  with pytest.raises((RuntimeError, asyncio.CancelledError)):
    asyncio.run(uploads.upload_files(chat.id, [file], principal, db))

  db.refresh(chat)
  on_disk = sorted(p.name for p in upload_dir(chat.id).iterdir())
  if failure == "commit":
    assert on_disk == [] and not chat.uploads
  else:
    assert on_disk == ["kept.txt"] and [u["name"] for u in chat.uploads] == ["kept.txt"]
