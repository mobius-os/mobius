# backend/tests/test_uploads.py
import io
from app import models


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
  """GET /api/chats/{id}/uploads/{filename} returns the file content."""
  client.post(
    f"/api/chats/{chat.id}/uploads",
    files=[("files", ("note.txt", io.BytesIO(b"secret"), "text/plain"))],
    headers=auth,
  )
  from app.auth import create_access_token
  token = create_access_token({"sub": "test"})
  res = client.get(
    f"/api/chats/{chat.id}/uploads/note.txt",
    params={"token": token},
  )
  assert res.status_code == 200
  assert res.content == b"secret"


def test_upload_rejects_missing_chat(client, auth):
  """Upload to nonexistent chat must return 404."""
  res = client.post(
    "/api/chats/nope/uploads",
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
  """DELETE to a nonexistent chat returns 404."""
  res = client.delete("/api/chats/nope/uploads/any.txt", headers=auth)
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
