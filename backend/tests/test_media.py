import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from app.config import agent_scratch_root, get_settings
from app.image_previews import display_image_preview, preview_cache_path


@pytest.mark.parametrize("icons_first", [False, True])
def test_preview_pixel_limit_does_not_depend_on_icon_import_order(icons_first):
  code = "from PIL import Image; "
  if icons_first:
    code += "import app.icon_assets; "
  code += (
    "Image.MAX_IMAGE_PIXELS = None; "
    "import app.image_previews; "
    "assert Image.MAX_IMAGE_PIXELS == 32_000_000"
  )
  subprocess.run([sys.executable, "-c", code], check=True, timeout=30)


def test_decompression_bomb_preview_is_refused_before_decode(tmp_path, monkeypatch):
  source = tmp_path / "compressed.png"
  Image.new("1", (9000, 9000)).save(source)

  def forbidden_load(self, *args, **kwargs):
    pytest.fail("An oversized preview must be refused before decoding")

  monkeypatch.setattr(PngImagePlugin.PngImageFile, "load", forbidden_load)
  assert display_image_preview(source, tmp_path) is None
  assert not preview_cache_path(source, tmp_path).exists()


def test_phone_photo_above_icon_pixel_limit_still_gets_a_preview(tmp_path):
  source = tmp_path / "phone.png"
  Image.new("1", (7300, 5480)).save(source)
  with pytest.warns(Image.DecompressionBombWarning):
    preview = display_image_preview(source, tmp_path)
  assert preview is not None
  with Image.open(preview) as image:
    assert image.format == "WEBP"
    assert max(image.size) == 1024


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


def test_nested_chat_media_previews_with_same_name_do_not_share_cache(
  client, auth, chat,
):
  media_dir = Path(get_settings().data_dir) / "chats" / chat.id / "media"
  landscape = media_dir / "reports/landscape/result.png"
  portrait = media_dir / "reports/portrait/result.png"
  landscape.parent.mkdir(parents=True)
  portrait.parent.mkdir(parents=True)
  Image.new("RGB", (120, 60), (220, 20, 20)).save(landscape, "PNG")
  Image.new("RGB", (60, 120), (20, 20, 220)).save(portrait, "PNG")
  token = _media_token(client, auth, chat.id)

  landscape_preview = client.get(
    f"/api/chats/{chat.id}/media/reports/landscape/result.png",
    params={"token": token, "preview": "true"},
  )
  portrait_preview = client.get(
    f"/api/chats/{chat.id}/media/reports/portrait/result.png",
    params={"token": token, "preview": "true"},
  )

  assert landscape_preview.status_code == 200
  assert portrait_preview.status_code == 200
  assert landscape_preview.content != portrait_preview.content
  with Image.open(BytesIO(landscape_preview.content)) as image:
    assert image.size == (120, 60)
  with Image.open(BytesIO(portrait_preview.content)) as image:
    assert image.size == (60, 120)
  assert len(list((media_dir / ".previews").glob("*.webp"))) == 2


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


def test_global_tmp_files_have_no_chat_preview_route(client, auth, chat):
  # A /tmp path is shared and mutable, so it cannot prove what a chat viewed.
  # Views preview their chat-owned snapshot under /media instead.
  with tempfile.NamedTemporaryFile(dir="/tmp", suffix=".png") as image:
    image.write(b"\x89PNG\r\n\x1a\nanother chat's render")
    image.flush()
    response = client.get(
      f"/api/chats/{chat.id}/tmp-images/{Path(image.name).name}",
      params={"token": _media_token(client, auth, chat.id)},
    )

  assert response.status_code == 404


def test_serve_agent_scratch_image_from_same_chat_with_media_token(
  client, auth, chat,
):
  source = agent_scratch_root() / chat.id / "renders" / "preview one.png"
  source.parent.mkdir(parents=True, exist_ok=True)
  source.write_bytes(b"current-scratch-image")

  response = client.get(
    f"/api/chats/{chat.id}/scratch-images/renders/preview%20one.png",
    params={"token": _media_token(client, auth, chat.id)},
  )

  assert response.status_code == 200
  assert response.content == b"current-scratch-image"
  assert response.headers["content-type"] == "image/png"
  assert response.headers["cache-control"] == "private, no-store"


def test_serve_agent_scratch_image_rejects_other_chat_token(client, auth, chat):
  from uuid import uuid4

  other_chat_id = str(uuid4())
  source = agent_scratch_root() / other_chat_id / "private.png"
  source.parent.mkdir(parents=True, exist_ok=True)
  source.write_bytes(b"other-chat-image")

  response = client.get(
    f"/api/chats/{other_chat_id}/scratch-images/private.png",
    params={"token": _media_token(client, auth, chat.id)},
  )

  assert response.status_code == 403


def test_serve_agent_scratch_image_rejects_non_raster_and_escape(
  client, auth, chat, tmp_path,
):
  scratch = agent_scratch_root() / chat.id
  scratch.mkdir(parents=True, exist_ok=True)
  (scratch / "private.txt").write_text("not an image", encoding="utf-8")
  outside = tmp_path / "outside.png"
  outside.write_bytes(b"outside")
  (scratch / "escape.png").symlink_to(outside)

  non_raster = client.get(
    f"/api/chats/{chat.id}/scratch-images/private.txt",
    headers=auth,
  )
  escape = client.get(
    f"/api/chats/{chat.id}/scratch-images/escape.png",
    headers=auth,
  )

  assert non_raster.status_code == 415
  assert escape.status_code == 400


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
