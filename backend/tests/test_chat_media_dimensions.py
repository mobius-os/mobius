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


def test_projection_marks_unreadable_local_images_explicitly(tmp_path):
  chat_id = "example-chat"
  media = tmp_path / "chats" / chat_id / "media"
  media.mkdir(parents=True)
  Image.new("RGB", (640, 480)).save(media / "ok.png", "PNG")
  (media / "broken.png").write_bytes(b"not an image")
  prefix = f"/api/chats/{chat_id}/media"
  messages = [{
    "role": "assistant",
    "content": (
      f"![ok]({prefix}/ok.png) ![broken]({prefix}/broken.png) "
      f"![missing]({prefix}/missing.png) ![escape]({prefix}/..%2F..%2Fsecret.png)"
    ),
  }]

  projected = project_message_image_dimensions(
    messages,
    chat_id=chat_id,
    data_dir=str(tmp_path),
  )

  # Unreadable images are recorded as null rather than omitted, so the
  # renderer can tell "unreadable" apart from "not in this map".
  assert projected[0]["media_dimensions"] == {
    f"{prefix}/ok.png": {"width": 640, "height": 480},
    f"{prefix}/broken.png": None,
    f"{prefix}/missing.png": None,
    f"{prefix}/..%2F..%2Fsecret.png": None,
  }


def test_projection_sizes_native_and_markdown_generated_images_from_recorded_store(tmp_path, db, chat):
  from app.models import GeneratedFile
  from app.generated_files import stored_dir

  base = stored_dir(str(tmp_path), chat.id, create=True)
  Image.new('RGB', (1536, 1024)).save(base / 'opaque-key', 'PNG')
  db.add(GeneratedFile(chat_id=chat.id, name='art work.png', path='opaque-key',
    size=100, mime_type='image/png'))
  db.commit()
  href = f'/api/chats/{chat.id}/generated-files/art%20work.png'
  messages = [
    {'role': 'assistant', 'blocks': [{'type': 'generated_files', 'files': [{
      'name': 'art work.png', 'mime_type': 'image/png', 'previewable': True,
    }]}]},
    {'role': 'assistant', 'content': f'![art]({href}?preview=true)'},
  ]
  projected = project_message_image_dimensions(messages, chat_id=chat.id,
    data_dir=str(tmp_path), db=db)
  with patch('app.image_previews.Image.open', side_effect=AssertionError('warm sizing reopened')):
    assert project_message_image_dimensions(messages, chat_id=chat.id,
      data_dir=str(tmp_path), db=db) == projected
  assert all('media_dimensions' not in message for message in messages)
  assert all(message['media_dimensions'] == {
    href: {'width': 1536, 'height': 1024},
  } for message in projected)


def test_generated_dimensions_follow_recorded_storage_and_reject_unrecorded_paths(tmp_path, db, chat):
  from app.models import GeneratedFile
  from app.generated_files import stored_dir

  base = stored_dir(str(tmp_path), chat.id, create=True)
  Image.new('RGB', (120, 300)).save(base / 'frozen-key', 'PNG')
  Image.new('RGB', (500, 500)).save(base / 'unrecorded.png', 'PNG')
  (base / 'symlink-key').symlink_to(base / 'frozen-key')
  for name, path in [
    ('stored.png', 'frozen-key'),
    ('missing.png', 'missing-key'),
    ('escape.png', '../outside.png'),
    ('link.png', 'symlink-key'),
  ]:
    db.add(GeneratedFile(chat_id=chat.id, name=name, path=path,
      size=100, mime_type='image/png'))
  db.commit()
  prefix = f'/api/chats/{chat.id}/generated-files'
  names = ['stored.png', 'missing.png', 'escape.png', 'link.png', 'unrecorded.png']
  messages = [{'role': 'assistant', 'content': ' '.join(f'![x]({prefix}/{name})' for name in names)}]
  result = project_message_image_dimensions(messages, chat_id=chat.id,
    data_dir=str(tmp_path), db=db)[0]['media_dimensions']
  assert result[f'{prefix}/stored.png'] == {'width': 120, 'height': 300}
  assert all(result[f'{prefix}/{name}'] is None for name in names[1:])
