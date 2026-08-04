from unittest.mock import patch

from PIL import Image

from app.chat_media_dimensions import project_message_image_dimensions
from app.image_previews import (
  dimensions_cache_path,
  preview_cache_path,
  stored_image_dimensions,
)


def test_stored_dimensions_are_header_read_then_disk_cached(tmp_path):
  source = tmp_path / "wide.png"
  Image.new("RGB", (1680, 957), (20, 40, 60)).save(source, "PNG")

  assert stored_image_dimensions(source, tmp_path) == {
    "width": 1680,
    "height": 957,
  }
  assert dimensions_cache_path(source, tmp_path).is_file()

  # A warm lookup is satisfied by the small sidecar without opening or decoding
  # the image again.
  with patch("app.image_previews.Image.open", side_effect=AssertionError("reopened")):
    assert stored_image_dimensions(source, tmp_path) == {
      "width": 1680,
      "height": 957,
    }


def test_stored_dimensions_follow_exif_orientation(tmp_path):
  source = tmp_path / "phone.jpg"
  exif = Image.Exif()
  exif[274] = 6
  Image.new("RGB", (1200, 800), (20, 40, 60)).save(
    source,
    "JPEG",
    exif=exif,
  )

  assert stored_image_dimensions(source, tmp_path) == {
    "width": 800,
    "height": 1200,
  }


def test_nested_duplicate_basenames_keep_distinct_dimension_sidecars(tmp_path):
  base = tmp_path / "media"
  landscape = base / "reports/landscape/result.png"
  portrait = base / "reports/portrait/result.png"
  landscape.parent.mkdir(parents=True)
  portrait.parent.mkdir(parents=True)
  Image.new("RGB", (120, 60)).save(landscape, "PNG")
  Image.new("RGB", (60, 120)).save(portrait, "PNG")

  assert stored_image_dimensions(landscape, base) == {
    "width": 120,
    "height": 60,
  }
  assert stored_image_dimensions(portrait, base) == {
    "width": 60,
    "height": 120,
  }
  landscape_sidecar = dimensions_cache_path(landscape, base)
  portrait_sidecar = dimensions_cache_path(portrait, base)
  assert landscape_sidecar != portrait_sidecar
  assert landscape_sidecar.is_file()
  assert portrait_sidecar.is_file()

  with patch("app.image_previews.Image.open", side_effect=AssertionError("reopened")):
    assert stored_image_dimensions(landscape, base) == {
      "width": 120,
      "height": 60,
    }
    assert stored_image_dimensions(portrait, base) == {
      "width": 60,
      "height": 120,
    }


def test_out_of_base_preview_cache_key_is_normalized_and_confined(tmp_path):
  base = tmp_path / "media"
  outside = tmp_path / "outside/result.png"
  equivalent_outside = base / "../outside/result.png"
  inside = base / "outside/result.png"

  cache_path = preview_cache_path(outside, base)
  assert cache_path == preview_cache_path(equivalent_outside, base)
  assert cache_path.parent == base / ".previews"
  assert cache_path != preview_cache_path(inside, base)


def test_projection_attaches_path_metadata_without_mutating_transcript(tmp_path):
  chat_id = "example-chat"
  media = tmp_path / "chats" / chat_id / "media"
  uploads = tmp_path / "chats" / chat_id / "uploads"
  media.mkdir(parents=True)
  uploads.mkdir(parents=True)
  Image.new("RGB", (1680, 957)).save(media / "shot.png", "PNG")
  Image.new("RGB", (600, 900)).save(uploads / "phone.jpg", "JPEG")
  messages = [{
    "role": "assistant",
    "blocks": [{
      "type": "text",
      "content": (
        f"![wide](/api/chats/{chat_id}/media/shot.png?preview=true)\n"
        f"![phone](/api/chats/{chat_id}/uploads/phone.jpg)"
      ),
    }],
  }]

  projected = project_message_image_dimensions(
    messages,
    chat_id=chat_id,
    data_dir=str(tmp_path),
  )

  assert "media_dimensions" not in messages[0]
  assert projected[0]["media_dimensions"] == {
    f"/api/chats/{chat_id}/media/shot.png": {
      "width": 1680,
      "height": 957,
    },
    f"/api/chats/{chat_id}/uploads/phone.jpg": {
      "width": 600,
      "height": 900,
    },
  }


def test_projection_leaves_unreadable_local_image_without_guessed_dimensions(
  tmp_path,
):
  chat_id = "example-chat"
  media = tmp_path / "chats" / chat_id / "media"
  media.mkdir(parents=True)
  (media / "broken.png").write_bytes(b"not an image")
  messages = [{
    "role": "assistant",
    "content": f"![broken](/api/chats/{chat_id}/media/broken.png)",
  }]

  projected = project_message_image_dimensions(
    messages,
    chat_id=chat_id,
    data_dir=str(tmp_path),
  )

  assert projected is not messages
  assert projected[0]["media_dimensions"] == {}
