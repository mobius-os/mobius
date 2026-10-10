"""A Codex image view previews the chat snapshot of exactly what the model saw.

Codex's native viewer reports only a mutable path. The image it sent to the
model is recorded in the thread's rollout after the view completes, so the
runner binds each view from that record once the turn ends.
"""

import asyncio
import base64
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

from app import codex_sdk_runner, viewed_images
from app.config import get_settings
from app.events import process_event

from tests.test_codex_sdk_runner import (
  _FakeBroadcast,
  _FakeThread,
  _FakeTurnCompletedNotification,
  _FakeTurnHandle,
  _fake_sdk,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"first image"
OTHER_PNG = b"\x89PNG\r\n\x1a\n" + b"second image"
THREAD = "01a11cfa-03f1-72d1-9114-e7be8db73825"


def _rollout(home: Path, thread_id: str = THREAD) -> Path:
  path = home / "sessions" / "2026" / "10" / "08" / f"rollout-2026-10-08T19-25-09-{thread_id}.jsonl"
  path.parent.mkdir(parents=True, exist_ok=True)
  path.touch()
  return path


def _output(call_id: str, output) -> dict:
  return {"timestamp": "t", "type": "response_item", "payload": {
    "type": "function_call_output", "call_id": call_id, "output": output,
  }}


def _image_output(call_id: str, data: bytes, mime: str = "image/png") -> dict:
  return _output(call_id, [{
    "type": "input_image",
    "image_url": f"data:{mime};base64,{base64.b64encode(data).decode()}",
    "detail": "high",
  }])


def _append(path: Path, *records: dict) -> None:
  with path.open("a") as handle:
    for record in records:
      handle.write(json.dumps(record) + "\n")


def _bind(home: Path, chat_id: str, mark, *call_ids: str) -> dict[str, str]:
  return viewed_images.bind_turn_views(get_settings().data_dir, chat_id, mark, call_ids)


def _served(client, auth, chat_id, name):
  return client.get(f"/api/chats/{chat_id}/media/{name}", headers=auth)


def test_a_view_previews_the_image_codex_recorded_for_the_model(
  tmp_path, client, auth, chat,
):
  rollout = _rollout(tmp_path)
  mark = viewed_images.TurnMark.capture(tmp_path, THREAD)
  _append(rollout, _image_output("call_1", PNG))

  bound = _bind(tmp_path, chat.id, mark, "call_1")

  assert viewed_images.SNAPSHOT_NAME.fullmatch(bound["call_1"])
  served = _served(client, auth, chat.id, bound["call_1"])
  assert served.status_code == 200
  assert served.content == PNG


def test_the_viewed_path_is_never_read(tmp_path, chat):
  # Overwriting or replacing the file after the view cannot change what binds:
  # only the bytes Codex recorded as sent to the model are stored.
  source = tmp_path / "render.png"
  source.write_bytes(OTHER_PNG)
  rollout = _rollout(tmp_path)
  mark = viewed_images.TurnMark.capture(tmp_path, THREAD)
  _append(rollout, _image_output("call_1", PNG))
  source.write_bytes(b"not even an image")

  name = _bind(tmp_path, chat.id, mark, "call_1")["call_1"]

  media = viewed_images.chat_media_dir(get_settings().data_dir, chat.id)
  assert (media / name).read_bytes() == PNG


def test_only_this_turns_records_bind(tmp_path, chat):
  rollout = _rollout(tmp_path)
  _append(rollout, _image_output("call_1", OTHER_PNG))
  mark = viewed_images.TurnMark.capture(tmp_path, THREAD)

  assert _bind(tmp_path, chat.id, mark, "call_1") == {}

  _append(rollout, _image_output("call_1", PNG))
  name = _bind(tmp_path, chat.id, mark, "call_1")["call_1"]
  media = viewed_images.chat_media_dir(get_settings().data_dir, chat.id)
  assert (media / name).read_bytes() == PNG


def test_a_first_turn_binds_from_the_rollout_it_creates(tmp_path, chat):
  mark = viewed_images.TurnMark.capture(tmp_path, THREAD)
  assert mark.path is None

  _append(_rollout(tmp_path), _image_output("call_1", PNG))

  assert set(_bind(tmp_path, chat.id, mark, "call_1")) == {"call_1"}


def test_another_threads_record_cannot_bind(tmp_path, chat):
  _rollout(tmp_path)
  other = "01a11cfa-0000-7000-8000-000000000000"
  other_rollout = _rollout(tmp_path, other)
  mark = viewed_images.TurnMark.capture(tmp_path, THREAD)
  _append(other_rollout, _image_output("call_1", PNG))

  assert _bind(tmp_path, chat.id, mark, "call_1") == {}


def test_each_chat_stores_and_serves_only_its_own_snapshot(
  tmp_path, client, auth, chat,
):
  rollout = _rollout(tmp_path)
  mark = viewed_images.TurnMark.capture(tmp_path, THREAD)
  _append(rollout, _image_output("call_1", PNG), _image_output("call_2", OTHER_PNG))
  other_chat = str(uuid.uuid4())

  mine = _bind(tmp_path, chat.id, mark, "call_1")["call_1"]
  theirs = _bind(tmp_path, other_chat, mark, "call_2")["call_2"]

  assert _served(client, auth, chat.id, mine).content == PNG
  assert _served(client, auth, chat.id, theirs).status_code == 404


def test_a_view_without_one_valid_recorded_image_gets_no_snapshot(tmp_path, chat):
  rollout = _rollout(tmp_path)
  mark = viewed_images.TurnMark.capture(tmp_path, THREAD)
  png = base64.b64encode(PNG).decode()
  _append(
    rollout,
    # A failed view records only its error text.
    _output("failed", "unable to locate image at `/tmp/x.png`"),
    # Bytes that are not an image, even when labelled as one.
    _image_output("text", b"plain text", "image/png"),
    # A label that disagrees with the bytes.
    _image_output("mislabelled", PNG, "image/jpeg"),
    _output("two", [
      {"type": "input_image", "image_url": f"data:image/png;base64,{png}"},
      {"type": "input_image", "image_url": f"data:image/png;base64,{png}"},
    ]),
    _output("broken", [{"type": "input_image", "image_url": "data:image/png;base64,@@"}]),
    _output("remote", [{"type": "input_image", "image_url": "https://example.com/x.png"}]),
  )

  assert _bind(
    tmp_path, chat.id, mark,
    "failed", "text", "mislabelled", "two", "broken", "remote",
  ) == {}
  media = viewed_images.chat_media_dir(get_settings().data_dir, chat.id)
  assert not media.exists() or not any(media.iterdir())


def test_a_partly_written_final_line_is_ignored(tmp_path, chat):
  rollout = _rollout(tmp_path)
  mark = viewed_images.TurnMark.capture(tmp_path, THREAD)
  with rollout.open("a") as handle:
    handle.write(json.dumps(_image_output("call_1", PNG)))  # no newline yet

  assert _bind(tmp_path, chat.id, mark, "call_1") == {}


def test_a_replaced_rollout_binds_nothing(tmp_path, chat):
  rollout = _rollout(tmp_path)
  _append(rollout, _output("old", "x" * 200))
  mark = viewed_images.TurnMark.capture(tmp_path, THREAD)
  rollout.write_text(json.dumps(_image_output("call_1", PNG)) + "\n")

  assert _bind(tmp_path, chat.id, mark, "call_1") == {}


def test_a_rollout_regrown_past_the_mark_binds_nothing(tmp_path, chat):
  # Truncated and rewritten in place, at least as long as before, with a
  # matching output starting exactly at the captured offset.
  rollout = _rollout(tmp_path)
  _append(rollout, _output("old", "x" * 200))
  mark = viewed_images.TurnMark.capture(tmp_path, THREAD)
  _append(rollout, _image_output("call_1", PNG))
  with rollout.open("r+b") as handle:
    handle.seek(0)
    handle.write(b"y" * (mark.offset - 1) + b"\n")

  assert _bind(tmp_path, chat.id, mark, "call_1") == {}


def test_a_rollout_replaced_by_another_file_binds_nothing(tmp_path, chat):
  # Same bytes before the mark, but a different file at the path.
  rollout = _rollout(tmp_path)
  _append(rollout, _output("old", "x" * 200))
  mark = viewed_images.TurnMark.capture(tmp_path, THREAD)
  replacement = tmp_path / "replacement.jsonl"
  replacement.write_bytes(rollout.read_bytes())
  _append(replacement, _image_output("call_1", PNG))
  replacement.replace(rollout)

  assert _bind(tmp_path, chat.id, mark, "call_1") == {}


def test_malformed_rollout_records_are_skipped_without_failing_the_turn(tmp_path, chat):
  rollout = _rollout(tmp_path)
  mark = viewed_images.TurnMark.capture(tmp_path, THREAD)
  with rollout.open("a") as handle:
    for line in (
      '"function_call_output"',
      '["function_call_output"]',
      '{"type": "response_item", "payload": ["function_call_output"]}',
    ):
      handle.write(line + "\n")
  for call_id in (["call_1"], {"call_1": 1}, 7):
    _append(rollout, {"type": "response_item", "payload": {
      "type": "function_call_output", "call_id": call_id, "output": [],
    }})
  _append(rollout, _image_output("call_1", PNG))

  assert set(_bind(tmp_path, chat.id, mark, "call_1")) == {"call_1"}


def test_unsafe_thread_ids_never_reach_the_filesystem(tmp_path):
  _rollout(tmp_path)
  for thread_id in ("", "*", "../x", f"{THREAD}/..", None):
    assert viewed_images.rollout_path(tmp_path, thread_id) is None
  assert viewed_images.rollout_path(tmp_path, THREAD) is not None


def _view_block(view_id: str) -> dict:
  return {
    "type": "tool", "tool": "ViewImage", "tool_use_id": view_id,
    "input": "/tmp/render.png", "status": "done",
  }


def test_the_snapshot_binds_only_its_exact_view():
  name = "viewed-" + "a" * 64 + ".png"
  blocks = [
    _view_block("view-1"),
    {"type": "tool", "tool": "Bash", "tool_use_id": "shell", "status": "done"},
    {"type": "tool", "tool": "ViewImage", "status": "running"},
  ]

  for event in (
    {"type": "viewed_image", "tool_use_id": "shell", "viewed_image_media": name},
    {"type": "viewed_image", "tool_use_id": "missing", "viewed_image_media": name},
    {"type": "viewed_image", "tool_use_id": "view-1", "viewed_image_media": "../x.png"},
    {"type": "viewed_image", "viewed_image_media": name},
  ):
    process_event(event, blocks)
  assert all("viewed_image_media" not in block for block in blocks)
  # An id-less open view is never adopted by a snapshot meant for another.
  assert "tool_use_id" not in blocks[2]

  process_event(
    {"type": "viewed_image", "tool_use_id": "view-1", "viewed_image_media": name},
    blocks,
  )
  assert blocks[0]["viewed_image_media"] == name


def test_codex_keeps_its_native_image_viewer():
  overrides = codex_sdk_runner._codex_config_overrides()
  assert not any(o.startswith(("features.view_image", "tools.view_image")) for o in overrides)


def test_a_codex_turn_binds_its_views_when_the_turn_completes(
  monkeypatch, tmp_path, chat,
):
  home = tmp_path / "codex-home"
  rollout = _rollout(home)
  _append(rollout, _output("earlier-turn", "x"))

  class ImageViewThreadItem:
    id = "call_view"
    path = "/tmp/render.png"

  class ItemCompleted:
    def __init__(self, item):
      self.item = item
      self.completed_at_ms = 1

  class RecordingTurn(_FakeTurnHandle):
    async def stream(self):
      yield SimpleNamespace(method="item/completed", payload=ItemCompleted(ImageViewThreadItem()))
      # Codex appends the view's output after the item completes and before
      # the turn completes.
      _append(rollout, _image_output("call_view", PNG))
      yield SimpleNamespace(
        method="turn/completed",
        payload=_FakeTurnCompletedNotification(
          SimpleNamespace(id="turn-1", usage=None, error=None),
        ),
      )

  thread = _FakeThread(THREAD, RecordingTurn())

  class FakeAsyncCodex:
    def __init__(self, config=None):
      self.config = config

    async def __aenter__(self):
      return self

    async def __aexit__(self, *_exc):
      return None

    async def thread_start(self, *_args, **_kwargs):
      return thread

  sdk = _fake_sdk(FakeAsyncCodex)
  sdk["ImageViewThreadItem"] = ImageViewThreadItem
  sdk["ItemCompletedNotification"] = ItemCompleted
  monkeypatch.setattr(codex_sdk_runner, "_sdk_imports", lambda: sdk)

  bc = _FakeBroadcast()
  result = asyncio.run(codex_sdk_runner.run_codex_sdk_turn(
    user_message="look",
    session_id=None,
    base_env={"CODEX_HOME": str(home)},
    cwd="/tmp",
    chat_id=chat.id,
    bc=bc,
    pending_questions={},
    db=None,
  ))

  assert result["error"] is None
  types = [event.get("type") for event in bc.events]
  bound = [event for event in bc.events if event.get("type") == "viewed_image"]
  assert len(bound) == 1
  assert bound[0]["tool_use_id"] == "call_view"
  assert types.index("viewed_image") > types.index("tool_end")
  media = viewed_images.chat_media_dir(get_settings().data_dir, chat.id)
  assert (media / bound[0]["viewed_image_media"]).read_bytes() == PNG
