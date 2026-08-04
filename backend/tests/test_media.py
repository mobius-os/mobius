from io import BytesIO
from pathlib import Path

from PIL import Image

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


def test_serve_chat_media_from_nested_report_directory(client, auth, chat):
  _write_chat_image(
    chat.id,
    "media/report/desktop",
    "contact-sheet.png",
    b"nested-image-bytes",
  )

  response = client.get(
    f"/api/chats/{chat.id}/media/report/desktop/contact-sheet.png",
    params={"token": _media_token(client, auth, chat.id)},
  )

  assert response.status_code == 200
  assert response.content == b"nested-image-bytes"
  assert response.headers["content-type"] == "image/png"


def test_serve_chat_media_uses_safe_raster_content_type(client, auth, chat):
  _write_chat_image(chat.id, "media", "photo.jpg", b"jpeg-bytes")

  response = client.get(
    f"/api/chats/{chat.id}/media/photo.jpg",
    headers=auth,
  )

  assert response.status_code == 200
  assert response.headers["content-type"] == "image/jpeg"


def test_chat_media_preview_is_bounded_webp_and_original_stays_unchanged(
  client, auth, chat,
):
  media_dir = Path(get_settings().data_dir) / "chats" / chat.id / "media"
  media_dir.mkdir(parents=True, exist_ok=True)
  source_path = media_dir / "large-screenshot.png"
  Image.new("RGB", (2400, 1600), (91, 67, 184)).save(source_path, "PNG")
  original = source_path.read_bytes()
  token = _media_token(client, auth, chat.id)

  preview = client.get(
    f"/api/chats/{chat.id}/media/{source_path.name}",
    params={"token": token, "preview": "true"},
  )

  assert preview.status_code == 200
  assert preview.headers["content-type"] == "image/webp"
  assert preview.headers["cache-control"] == "private, max-age=86400"
  with Image.open(BytesIO(preview.content)) as image:
    assert image.format == "WEBP"
    assert max(image.size) == 1024

  full = client.get(
    f"/api/chats/{chat.id}/media/{source_path.name}",
    params={"token": token},
  )
  assert full.content == original
  assert full.headers["content-type"] == "image/png"

  previews = list((media_dir / ".previews").glob("*.webp"))
  assert len(previews) == 1
  first_mtime = previews[0].stat().st_mtime_ns
  again = client.get(
    f"/api/chats/{chat.id}/media/{source_path.name}",
    params={"token": token, "preview": "true"},
  )
  assert again.content == preview.content
  assert previews[0].stat().st_mtime_ns == first_mtime


def test_chat_media_preview_falls_back_for_undecodable_image(client, auth, chat):
  _write_chat_image(chat.id, "media", "broken.png", b"not-a-real-png")

  response = client.get(
    f"/api/chats/{chat.id}/media/broken.png",
    params={
      "token": _media_token(client, auth, chat.id),
      "preview": "true",
    },
  )

  assert response.status_code == 200
  assert response.content == b"not-a-real-png"
  assert response.headers["content-type"] == "image/png"


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
