from pathlib import Path

from app.config import get_settings


def _write_chat_image(chat_id: str, subdir: str, filename: str, data: bytes) -> None:
  directory = Path(get_settings().data_dir) / "chats" / chat_id / subdir
  directory.mkdir(parents=True, exist_ok=True)
  (directory / filename).write_bytes(data)


def _media_token(client, auth, chat_id: str) -> str:
  response = client.post(f"/api/chats/{chat_id}/media-token", headers=auth)
  assert response.status_code == 200
  return response.json()["token"]


def test_serve_chat_media(client, auth, chat):
  _write_chat_image(chat.id, "media", "screenshot.png", b"image-bytes")

  response = client.get(
    f"/api/chats/{chat.id}/media/screenshot.png",
    params={"token": _media_token(client, auth, chat.id)},
  )

  assert response.status_code == 200
  assert response.content == b"image-bytes"
  assert response.headers["content-type"] == "image/png"


def test_serve_chat_media_uses_safe_raster_content_type(client, auth, chat):
  _write_chat_image(chat.id, "media", "photo.jpg", b"jpeg-bytes")

  response = client.get(
    f"/api/chats/{chat.id}/media/photo.jpg",
    headers=auth,
  )

  assert response.status_code == 200
  assert response.headers["content-type"] == "image/jpeg"


def test_serve_chat_media_rejects_directory(client, auth, chat):
  directory = Path(get_settings().data_dir) / "chats" / chat.id / "media" / "folder"
  directory.mkdir(parents=True)

  response = client.get(
    f"/api/chats/{chat.id}/media/folder",
    headers=auth,
  )
  assert response.status_code == 404


def test_serve_media_rejects_non_uuid_chat_id(client, auth):
  response = client.get(
    "/api/chats/not-a-uuid/media/some.png",
    headers=auth,
  )
  assert response.status_code == 400


def test_image_generation_endpoint_is_not_available(client, auth, chat):
  response = client.post(
    f"/api/chats/{chat.id}/generate-image",
    json={"prompt": "a landscape"},
    headers=auth,
  )
  assert response.status_code == 404


def test_old_generated_route_is_not_available(client, auth, chat):
  response = client.get(
    f"/api/chats/{chat.id}/generated/old.png",
    headers=auth,
  )
  assert response.status_code == 404
