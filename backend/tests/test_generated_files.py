# backend/tests/test_generated_files.py
from pathlib import Path

from app import models
from app.config import get_settings


def _write_row(db, chat, *, name, path, size=11, mime_type="application/pdf"):
  row = models.GeneratedFile(
    chat_id=chat.id, name=name, path=path, size=size, mime_type=mime_type,
  )
  db.add(row)
  db.commit()
  return row


def test_serve_generated_file_by_recorded_name(client, db, auth, chat):
  """The route serves the exact path recorded for THIS chat's own row, never
  a client-supplied filesystem path — see routes/generated_files.py."""
  settings = get_settings()
  (Path(settings.data_dir) / "report.pdf").write_bytes(b"%PDF-1.4 fake")
  _write_row(db, chat, name="report.pdf", path="report.pdf")

  media_token_r = client.post(f"/api/chats/{chat.id}/media-token", headers=auth)
  media_token = media_token_r.json()["token"]
  res = client.get(
    f"/api/chats/{chat.id}/generated-files/report.pdf",
    params={"token": media_token},
  )
  assert res.status_code == 200
  assert res.content == b"%PDF-1.4 fake"
  assert res.headers["content-disposition"] == 'attachment; filename="report.pdf"'


def test_serve_generated_file_unknown_name_404s(client, auth, chat):
  """A name never recorded for this chat 404s — it is never resolved against
  cwd on the fly, so an unrecorded filename can't be probed for."""
  media_token_r = client.post(f"/api/chats/{chat.id}/media-token", headers=auth)
  media_token = media_token_r.json()["token"]
  res = client.get(
    f"/api/chats/{chat.id}/generated-files/never-recorded.pdf",
    params={"token": media_token},
  )
  assert res.status_code == 404


def test_serve_generated_file_rejects_traversal_in_recorded_path(client, db, auth, chat):
  """Even a recorded row can't point outside cwd — defense in depth on top
  of the by-name lookup, matching validate_path_within_base elsewhere."""
  _write_row(db, chat, name="evil.txt", path="../outside.txt")

  media_token_r = client.post(f"/api/chats/{chat.id}/media-token", headers=auth)
  media_token = media_token_r.json()["token"]
  res = client.get(
    f"/api/chats/{chat.id}/generated-files/evil.txt",
    params={"token": media_token},
  )
  assert res.status_code == 400


def test_serve_generated_file_does_not_leak_across_chats(
  client, db, auth, chat, second_chat,
):
  """A file recorded for one chat is invisible through another chat's id —
  the lookup is scoped by chat_id, not just by name."""
  settings = get_settings()
  (Path(settings.data_dir) / "shared-name.csv").write_bytes(b"a,b,c\n")
  _write_row(db, chat, name="shared-name.csv", path="shared-name.csv", mime_type="text/csv")

  media_token_r = client.post(f"/api/chats/{second_chat.id}/media-token", headers=auth)
  media_token = media_token_r.json()["token"]
  res = client.get(
    f"/api/chats/{second_chat.id}/generated-files/shared-name.csv",
    params={"token": media_token},
  )
  assert res.status_code == 404
