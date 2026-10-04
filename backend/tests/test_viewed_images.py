"""A viewed image previews the chat snapshot of exactly what the provider saw."""

import base64
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

from app import viewed_images
from app.codex_events import _is_control_image_view, _tool_start_event
from app.codex_sdk_runner import _codex_config_overrides
from app.config import get_settings
from app.events import process_event

PNG = b"\x89PNG\r\n\x1a\n" + b"first image"
OTHER_PNG = b"\x89PNG\r\n\x1a\n" + b"second image"


def _control(monkeypatch, chat_id):
  path = Path(__file__).resolve().parents[1] / "scripts" / "mobius_control_mcp.py"
  spec = importlib.util.spec_from_file_location("mobius_control_view_test", path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  monkeypatch.setenv("MOBIUS_IMAGE_VIEWER", "1")
  monkeypatch.setenv("MOBIUS_RUN_TOKEN", "run")
  monkeypatch.setenv("DATA_DIR", get_settings().data_dir)
  monkeypatch.setenv("CHAT_ID", chat_id)
  return module


def _view(control, monkeypatch, chat_id, path):
  monkeypatch.setenv("CHAT_ID", chat_id)
  return control._call_tool({"name": "view_image", "arguments": {"path": str(path)}})


def _bound(chat_id, result):
  return viewed_images.bound_snapshot(get_settings().data_dir, chat_id, result)


def _served(client, auth, chat_id, name):
  return client.get(f"/api/chats/{chat_id}/media/{name}", headers=auth)


def test_the_preview_serves_the_bytes_returned_to_the_provider(
  monkeypatch, tmp_path, client, auth, chat,
):
  source = tmp_path / "render.png"
  source.write_bytes(PNG)
  control = _control(monkeypatch, chat.id)

  result = _view(control, monkeypatch, chat.id, source)

  image, note = result["content"]
  assert result["isError"] is False
  assert base64.b64decode(image["data"]) == PNG
  assert image["mimeType"] == "image/png"
  assert str(source) in note["text"]
  name = _bound(chat.id, result)
  assert viewed_images.SNAPSHOT_NAME.fullmatch(name)
  assert _served(client, auth, chat.id, name).content == PNG


def test_overwriting_the_path_after_the_view_keeps_its_preview(
  monkeypatch, tmp_path, client, auth, chat,
):
  source = tmp_path / "render.png"
  source.write_bytes(PNG)
  control = _control(monkeypatch, chat.id)
  first = _view(control, monkeypatch, chat.id, source)

  source.write_bytes(OTHER_PNG)
  second = _view(control, monkeypatch, chat.id, source)

  assert _served(client, auth, chat.id, _bound(chat.id, first)).content == PNG
  assert _served(client, auth, chat.id, _bound(chat.id, second)).content == OTHER_PNG


def test_another_chats_same_named_file_cannot_bind_or_replace_this_view(
  monkeypatch, tmp_path, client, auth, chat,
):
  source = tmp_path / "shared-name.png"
  other_chat = str(uuid.uuid4())
  control = _control(monkeypatch, chat.id)
  source.write_bytes(PNG)
  mine = _view(control, monkeypatch, chat.id, source)
  source.write_bytes(OTHER_PNG)
  theirs = _view(control, monkeypatch, other_chat, source)

  # Each view binds only inside its own chat, to its own bytes.
  assert _bound(chat.id, theirs) == ""
  assert _bound(other_chat, mine) == ""
  assert _served(client, auth, chat.id, _bound(chat.id, mine)).content == PNG


def test_a_non_image_is_refused_and_leaves_no_snapshot(monkeypatch, tmp_path, chat):
  source = tmp_path / "notes.png"
  source.write_text("private text with an image name", encoding="utf-8")
  control = _control(monkeypatch, chat.id)

  result = _view(control, monkeypatch, chat.id, source)

  assert result["isError"] is True
  assert "not a PNG, JPEG, GIF, or WebP image" in result["content"][0]["text"]
  media = viewed_images.chat_media_dir(get_settings().data_dir, chat.id)
  assert not list(media.glob("viewed-*"))


def test_a_view_that_cannot_store_its_snapshot_still_shows_the_model(
  monkeypatch, tmp_path, chat,
):
  source = tmp_path / "render.png"
  source.write_bytes(PNG)
  control = _control(monkeypatch, chat.id)

  def read_only(*_args):
    raise PermissionError("read-only")

  monkeypatch.setattr(control._VIEWED_IMAGES, "store_snapshot", read_only)
  result = _view(control, monkeypatch, chat.id, source)

  assert base64.b64decode(result["content"][0]["data"]) == PNG
  assert "no preview" in result["content"][1]["text"]
  assert _bound(chat.id, result) == ""


def test_only_the_exact_stored_payload_binds(chat):
  name = viewed_images.store_snapshot(
    viewed_images.chat_media_dir(get_settings().data_dir, chat.id), PNG, "image/png",
  )
  image = {"type": "image", "data": base64.b64encode(PNG).decode(), "mimeType": "image/png"}

  assert _bound(chat.id, {"content": [image]}) == name
  assert _bound(chat.id, {"content": [{**image, "mimeType": "image/gif"}]}) == ""
  assert _bound(chat.id, {"content": [image, image]}) == ""
  assert _bound(chat.id, {"content": [{**image, "data": "not base64!"}]}) == ""
  unstored = base64.b64encode(OTHER_PNG).decode()
  assert _bound(chat.id, {"content": [{**image, "data": unstored}]}) == ""
  assert _bound(chat.id, None) == ""


def test_view_image_is_offered_only_where_it_replaces_codexs_viewer(monkeypatch, chat):
  control = _control(monkeypatch, chat.id)
  assert "view_image" in control._available_tool_names()
  monkeypatch.delenv("MOBIUS_IMAGE_VIEWER")
  assert "view_image" not in control._available_tool_names()
  assert "tools.view_image=false" in _codex_config_overrides()


def test_a_control_view_renders_as_an_image_view_of_its_path():
  class McpCall(SimpleNamespace):
    pass

  sdk = {"McpToolCallThreadItem": McpCall}
  item = McpCall(server="mobius_control", tool="view_image", arguments={"path": "/tmp/a.png"})
  assert _tool_start_event(item, sdk) == {
    "type": "tool_start", "tool": "ViewImage", "input": "/tmp/a.png",
  }
  other = McpCall(server="elsewhere", tool="view_image", arguments={"path": "/tmp/a.png"})
  assert not _is_control_image_view(other, sdk)


def test_tool_end_keeps_only_a_snapshot_name_on_the_image_view():
  blocks = [
    {"type": "tool", "tool": "ViewImage", "status": "running", "tool_use_id": "image"},
    {"type": "tool", "tool": "Bash", "status": "running", "tool_use_id": "shell"},
  ]
  name = viewed_images.snapshot_name(PNG, "image/png")
  process_event({"type": "tool_end", "tool_use_id": "image", "viewed_image_media": name}, blocks)
  process_event({"type": "tool_end", "tool_use_id": "shell", "viewed_image_media": name}, blocks)
  assert blocks[0]["viewed_image_media"] == name
  assert "viewed_image_media" not in blocks[1]

  blocks[0]["status"] = "running"
  blocks[0].pop("viewed_image_media")
  process_event({
    "type": "tool_end", "tool_use_id": "image", "viewed_image_media": "../uploads/x.png",
  }, blocks)
  assert "viewed_image_media" not in blocks[0]
