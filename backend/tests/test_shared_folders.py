"""Folders the owner shares from a browser: relay, safety, and access contracts."""

import asyncio
import base64
import importlib.util
import io
import threading
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import auth as auth_module
from app import shared_folders
from app.broadcast import get_system_broadcast
from app.routes.shared_folders import FolderCommandBody, normalized_path
from app.shared_folders import DeviceReport, SharedFolder, SharedFolderRegistry
from tests.test_browser_access_routes import https, invite  # noqa: F401 - fixture and helper


def _folder(folder_id="f1", *, name="Downloads", mode="edit", access="remembered", state="ready"):
  return SharedFolder(id=folder_id, name=name, mode=mode, access=access, state=state)


def _report(subscription_id="s1", *, device="laptop", sequence=1, folders=None, at=None):
  return DeviceReport(
    subscription_id=subscription_id,
    device_id=device,
    device_label=f"Chrome on {device}",
    sequence=sequence,
    folders=tuple(folders if folders is not None else [_folder()]),
    reported_at=at if at is not None else time.time(),
  )


class _FakeStreams:
  """Live system-stream subscriptions, recording what each one receives."""

  def __init__(self, *live):
    self.live = set(live)
    self.sent: list[tuple[str, dict]] = []

  def is_live(self, subscription_id):
    return subscription_id in self.live

  def deliver(self, subscription_id, event):
    if subscription_id not in self.live:
      return False
    self.sent.append((subscription_id, event))
    return True

  def registry(self):
    return SharedFolderRegistry(self.is_live, self.deliver)


async def _next_event(streams):
  for _ in range(100):
    if streams.sent:
      return streams.sent.pop(0)
    await asyncio.sleep(0.01)
  raise AssertionError("no command was delivered")


@pytest.mark.asyncio
async def test_command_reaches_only_the_reporting_stream_and_returns_its_answer():
  streams = _FakeStreams("s1", "s2")
  registry = streams.registry()
  assert registry.report(_report("s1", folders=[_folder("f1")]))
  assert registry.report(_report("s2", device="phone", folders=[_folder("f2")]))

  waiting = asyncio.create_task(registry.issue("f1", {"op": "list", "path": ""}))
  subscription_id, event = await _next_event(streams)
  assert subscription_id == "s1"
  assert event["type"] == "shared_folder_command" and event["folderId"] == "f1"
  # Another tab or device cannot answer a command it was never sent.
  assert not registry.answer("s2", event["commandId"], {"ok": True, "result": "forged"})
  assert registry.answer("s1", event["commandId"], {"ok": True, "result": {"entries": []}})
  assert await waiting == {"ok": True, "result": {"entries": []}}
  assert not registry.answer("s1", event["commandId"], {"ok": True, "result": "late"})


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,access", [("read", "remembered"), ("read", "visit"), ("edit", "visit")])
async def test_read_only_or_visit_folder_refuses_every_change_before_reaching_the_browser(mode, access):
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report(folders=[_folder(mode=mode, access=access)]))
  for operation in shared_folders.EDIT_OPERATIONS:
    outcome = await registry.issue("f1", {"op": operation, "path": "a.txt"})
    assert outcome["code"] == "read_only"
  assert streams.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["needs_permission", "needs_pick", "paused"])
async def test_unready_folder_returns_an_owner_actionable_reason(state):
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report(folders=[_folder(state=state)]))
  outcome = await registry.issue("f1", {"op": "list", "path": ""})
  assert outcome["ok"] is False and outcome["code"] == state
  assert "Settings" in outcome["error"]


@pytest.mark.asyncio
async def test_closed_browser_is_offline_and_unknown_folder_is_not_found():
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report("s1"))
  streams.live.clear()
  assert (await registry.issue("f1", {"op": "list", "path": ""}))["code"] == "offline"
  assert (await registry.issue("nope", {"op": "list", "path": ""}))["code"] == "not_found"
  listed = registry.folders()
  assert listed[0]["online"] is False and listed[0]["state"] == "offline"


@pytest.mark.asyncio
async def test_tab_closing_mid_command_fails_fast_instead_of_waiting_for_timeout(monkeypatch):
  monkeypatch.setattr(shared_folders, "LIVENESS_POLL_SECONDS", 0.02)
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report())
  waiting = asyncio.create_task(registry.issue("f1", {"op": "read", "path": "a.txt"}))
  await _next_event(streams)
  streams.live.clear()
  started = time.monotonic()
  assert (await waiting)["code"] == "offline"
  assert time.monotonic() - started < 1


@pytest.mark.asyncio
async def test_unanswered_command_times_out_and_is_forgotten(monkeypatch):
  monkeypatch.setattr(shared_folders, "COMMAND_TIMEOUT_SECONDS", 0.05)
  monkeypatch.setattr(shared_folders, "LIVENESS_POLL_SECONDS", 0.02)
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report())
  assert (await registry.issue("f1", {"op": "info", "path": ""}))["code"] == "timeout"
  assert registry._pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", sorted(shared_folders.EDIT_OPERATIONS))
async def test_a_change_without_an_answer_may_still_have_happened(monkeypatch, operation):
  monkeypatch.setattr(shared_folders, "COMMAND_TIMEOUT_SECONDS", 0.05)
  monkeypatch.setattr(shared_folders, "LIVENESS_POLL_SECONDS", 0.02)
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report())
  timed_out = await registry.issue("f1", {"op": operation, "path": "a.txt"})
  assert timed_out["code"] == "timeout" and "may still have happened" in timed_out["error"]
  waiting = asyncio.create_task(registry.issue("f1", {"op": operation, "path": "a.txt"}))
  await asyncio.sleep(0.01)
  streams.live.clear()
  closed = await waiting
  assert closed["code"] == "offline" and "may still have happened" in closed["error"]


@pytest.mark.asyncio
async def test_a_read_without_an_answer_is_simply_not_done(monkeypatch):
  monkeypatch.setattr(shared_folders, "COMMAND_TIMEOUT_SECONDS", 0.05)
  monkeypatch.setattr(shared_folders, "LIVENESS_POLL_SECONDS", 0.02)
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report())
  assert "may still have happened" not in (await registry.issue("f1", {"op": "read", "path": "a.txt"}))["error"]


@pytest.mark.asyncio
async def test_busy_browser_refuses_more_than_the_pending_limit(monkeypatch):
  monkeypatch.setattr(shared_folders, "MAX_PENDING_PER_STREAM", 1)
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report())
  first = asyncio.create_task(registry.issue("f1", {"op": "info", "path": ""}))
  _, event = await _next_event(streams)
  assert (await registry.issue("f1", {"op": "info", "path": ""}))["code"] == "busy"
  registry.answer("s1", event["commandId"], {"ok": True, "result": {}})
  assert (await first)["ok"] is True


@pytest.mark.asyncio
async def test_folder_picked_in_one_tab_is_served_by_that_tab_not_a_newer_one():
  # Safari and Firefox keep a picked folder only in the tab where it was picked.
  streams = _FakeStreams("picker-tab", "other-tab")
  registry = streams.registry()
  registry.report(_report("picker-tab", at=1, folders=[_folder(access="visit", mode="read")]))
  registry.report(_report("other-tab", at=2, folders=[_folder(access="visit", mode="read", state="needs_pick")]))
  assert [folder["state"] for folder in registry.folders()] == ["ready"]
  waiting = asyncio.create_task(registry.issue("f1", {"op": "list", "path": ""}))
  subscription_id, event = await _next_event(streams)
  assert subscription_id == "picker-tab"
  registry.answer("picker-tab", event["commandId"], {"ok": True, "result": {}})
  assert (await waiting)["ok"] is True


def _sent_of(streams, kind):
  return [(sid, event) for sid, event in streams.sent if event["type"] == kind]


@pytest.mark.asyncio
async def test_stop_on_any_device_refuses_everywhere_until_the_holder_confirms():
  streams = _FakeStreams("laptop-tab", "phone-tab")
  registry = streams.registry()
  registry.report(_report("laptop-tab", device="laptop", folders=[_folder("f1")]))
  registry.report(_report("phone-tab", device="phone", folders=[]))
  # The owner taps Stop on the phone, which shares nothing itself.
  assert registry.pause() == ["f1"]
  told = _sent_of(streams, "shared_folder_pause")
  assert sorted(sid for sid, _ in told) == ["laptop-tab", "phone-tab"]
  assert told[0][1]["folderIds"] == ["f1"]
  streams.sent.clear()
  refused = await registry.issue("f1", {"op": "read", "path": "a.txt"})
  assert refused["code"] == "paused"
  assert _sent_of(streams, "shared_folder_command") == []
  assert registry.folders()[0]["state"] == "paused"
  # The laptop pauses the folder itself; resuming there shares it again.
  registry.report(_report("laptop-tab", device="laptop", sequence=2, folders=[_folder("f1", state="paused")]))
  registry.report(_report("laptop-tab", device="laptop", sequence=3, folders=[_folder("f1")]))
  waiting = asyncio.create_task(registry.issue("f1", {"op": "read", "path": "a.txt"}))
  _, event = await _next_event(streams)
  assert event["type"] == "shared_folder_command"
  registry.answer("laptop-tab", event["commandId"], {"ok": True, "result": {}})
  assert (await waiting)["ok"] is True


@pytest.mark.asyncio
async def test_stop_holds_until_every_tab_holding_the_folder_has_paused():
  # Two tabs of one browser hold the same remembered folder.
  streams = _FakeStreams("tab-a", "tab-b")
  registry = streams.registry()
  registry.report(_report("tab-a", folders=[_folder("f1")]))
  registry.report(_report("tab-b", folders=[_folder("f1")]))
  registry.pause()
  registry.report(_report("tab-a", sequence=2, folders=[_folder("f1", state="paused")]))
  streams.sent.clear()
  refused = await registry.issue("f1", {"op": "mkdir", "path": "new"})
  assert refused["code"] == "paused"
  assert _sent_of(streams, "shared_folder_command") == []
  # Once both keep the pause, the owner resumes it in the browser.
  registry.report(_report("tab-b", sequence=2, folders=[_folder("f1", state="paused")]))
  registry.report(_report("tab-a", sequence=3, folders=[_folder("f1")]))
  waiting = asyncio.create_task(registry.issue("f1", {"op": "mkdir", "path": "new"}))
  subscription_id, event = await _next_event(streams)
  assert (subscription_id, event["type"]) == ("tab-a", "shared_folder_command")
  registry.answer("tab-a", event["commandId"], {"ok": True, "result": {}})
  assert (await waiting)["ok"] is True


@pytest.mark.asyncio
async def test_a_change_must_start_before_the_server_stops_waiting_for_it():
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report("s1"))
  waiting = asyncio.create_task(registry.issue("f1", {"op": "mkdir", "path": "new"}))
  _, event = await _next_event(streams)
  assert 0 < event["startWithinMs"] < shared_folders.COMMAND_TIMEOUT_SECONDS * 1000
  registry.answer("s1", event["commandId"], {"ok": True, "result": {}})
  await waiting


def test_a_browser_closed_during_stop_is_told_again_when_it_returns():
  streams = _FakeStreams("old-tab")
  registry = streams.registry()
  registry.report(_report("old-tab", folders=[_folder("f1")]))
  streams.live.clear()
  registry.pause()
  assert streams.sent == []
  streams.live.add("new-tab")
  registry.report(_report("new-tab", at=time.time() + 1, folders=[_folder("f1")]))
  assert streams.sent == [("new-tab", {"type": "shared_folder_pause", "folderIds": ["f1"]})]


@pytest.mark.asyncio
async def test_a_stop_outlives_its_holder_closing_while_other_devices_report():
  streams = _FakeStreams("laptop-tab", "phone-tab")
  registry = streams.registry()
  registry.report(_report("laptop-tab", folders=[_folder("f1")]))
  registry.report(_report("phone-tab", device="phone", folders=[_folder("f2", name="Photos")]))
  # The laptop closes before it can confirm the pause.
  streams.live.discard("laptop-tab")
  registry.pause()
  registry.report(_report("phone-tab", device="phone", sequence=2, folders=[_folder("f2", name="Photos", state="paused")]))
  streams.sent.clear()
  # It comes back still unpaused: it is told again, and agents stay refused.
  streams.live.add("laptop-again")
  registry.report(_report("laptop-again", at=time.time() + 1, folders=[_folder("f1")]))
  assert _sent_of(streams, "shared_folder_pause") == [
    ("laptop-again", {"type": "shared_folder_pause", "folderIds": ["f1"]}),
  ]
  refused = await registry.issue("f1", {"op": "mkdir", "path": "new"})
  assert refused["code"] == "paused"
  assert _sent_of(streams, "shared_folder_command") == []


def test_stop_can_name_folders_and_ignores_unknown_ones():
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report(folders=[_folder("f1"), _folder("f2", name="Photos")]))
  assert registry.pause(["f2", "nope"]) == ["f2"]
  assert {f["id"]: f["state"] for f in registry.folders()} == {"f1": "ready", "f2": "paused"}


@pytest.mark.asyncio
async def test_activity_reaches_every_owner_tab_and_no_other_stream():
  # "guest-tab" is live on the system stream but never reported folders.
  streams = _FakeStreams("laptop-tab", "phone-tab", "guest-tab")
  registry = streams.registry()
  registry.report(_report("laptop-tab", device="laptop", folders=[_folder("f1")]))
  registry.report(_report("phone-tab", device="phone", folders=[]))
  waiting = asyncio.create_task(registry.issue("f1", {"op": "write", "path": "a.txt", "content": "x"}))
  _, command = await _next_event(streams)
  started = _sent_of(streams, "shared_folder_activity")
  assert sorted(sid for sid, _ in started) == ["laptop-tab", "phone-tab"]
  assert started[0][1] == {
    "type": "shared_folder_activity", "commands": 1,
    "folderId": "f1", "folderName": "Downloads", "deviceId": "laptop", "deviceLabel": "Chrome on laptop",
  }
  streams.sent.clear()
  registry.answer("laptop-tab", command["commandId"], {"ok": True, "result": {}})
  await waiting
  assert sorted((sid, e["commands"]) for sid, e in _sent_of(streams, "shared_folder_activity")) == [
    ("laptop-tab", 0), ("phone-tab", 0),
  ]
  assert all(sid != "guest-tab" for sid, _ in streams.sent)


def test_delayed_older_report_cannot_overwrite_a_newer_one():
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report(sequence=2, folders=[_folder(name="New")]))
  registry.report(_report(sequence=1, folders=[_folder(name="Old")]))
  assert [folder["name"] for folder in registry.folders()] == ["New"]


def test_report_from_a_disconnected_stream_is_refused():
  registry = _FakeStreams().registry()
  assert registry.report(_report("gone")) is False
  assert registry.folders() == []


def test_one_device_keeps_its_live_tab_and_forgets_superseded_closed_tabs():
  streams = _FakeStreams("new")
  registry = streams.registry()
  streams.live.add("old")
  registry.report(_report("old", at=1, folders=[_folder(name="Old tab")]))
  registry.report(_report("new", at=2, folders=[_folder(name="New tab")]))
  streams.live.discard("old")
  registry.report(_report("new", sequence=2, at=3, folders=[_folder(name="New tab")]))
  assert set(registry._reports) == {"new"}
  assert [folder["name"] for folder in registry.folders()] == ["New tab"]


def test_remembered_offline_devices_are_capped(monkeypatch):
  monkeypatch.setattr(shared_folders, "MAX_REMEMBERED_DEVICES", 3)
  streams = _FakeStreams()
  registry = streams.registry()
  for index in range(6):
    streams.live.add(f"s{index}")
    registry.report(_report(f"s{index}", device=f"d{index}", at=index))
    streams.live.discard(f"s{index}")
  assert len(registry._reports) <= 3


@pytest.mark.parametrize("path", ["..", "a/../b", "./a", "a//b", "a\\b", "a\x00b", "a/\nb"])
def test_path_cannot_leave_the_shared_folder(path):
  with pytest.raises(ValueError):
    normalized_path(path, allow_root=False)


def test_path_is_plain_segments_and_root_only_where_allowed():
  assert normalized_path("/Photos/2026/a.jpg", allow_root=False) == "Photos/2026/a.jpg"
  assert normalized_path("", allow_root=True) == ""
  with pytest.raises(ValueError):
    normalized_path("  ", allow_root=False)


@pytest.mark.parametrize("body", [
  {"op": "list", "path": "", "content": "x"},
  {"op": "read", "path": "a.txt", "to": "b.txt"},
  {"op": "move", "path": "a.txt"},
  {"op": "write", "path": "a.txt"},
  {"op": "read", "path": ""},
  {"op": "read", "path": "a.txt", "length": 4 * 1024 * 1024 + 1},
  {"op": "exec", "path": "a.txt"},
  {"op": "move", "path": "a.txt", "to": "../b.txt"},
  {"op": "image", "path": ""},
  {"op": "image", "path": "a.jpg", "maxEdge": 8000},
  {"op": "list", "path": "", "maxEdge": 2576},
])
def test_command_accepts_only_its_own_fields_and_safe_paths(body):
  with pytest.raises(ValidationError):
    FolderCommandBody(**body)


def test_write_size_is_measured_in_bytes_for_text_and_base64():
  limit = 4 * 1024 * 1024
  assert FolderCommandBody(op="write", path="a.txt", content="a" * limit)
  # Two bytes per letter: under the character cap, over the byte limit.
  with pytest.raises(ValidationError, match="4 MB"):
    FolderCommandBody(op="write", path="a.txt", content="ж" * (limit // 2 + 1))
  exact = base64.b64encode(b"x" * limit).decode()
  assert FolderCommandBody(op="write", path="a.bin", content=exact, encoding="base64")
  with pytest.raises(ValidationError):
    FolderCommandBody(op="write", path="a.bin", content=base64.b64encode(b"x" * (limit + 1)).decode(), encoding="base64")


def test_image_command_is_a_read_that_carries_only_its_size_limit():
  body = FolderCommandBody(op="image", path="Photos/IMG_1.HEIC", maxEdge=2576)
  assert body.wire() == {"op": "image", "path": "Photos/IMG_1.HEIC", "maxEdge": 2576}
  assert "image" in shared_folders.READ_OPERATIONS


@pytest.mark.asyncio
async def test_read_only_folder_still_shows_images():
  streams = _FakeStreams("s1")
  registry = streams.registry()
  registry.report(_report(folders=[_folder(mode="read")]))
  waiting = asyncio.create_task(registry.issue("f1", {"op": "image", "path": "a.jpg", "maxEdge": 2576}))
  _, event = await _next_event(streams)
  registry.answer("s1", event["commandId"], {"ok": True, "result": {"shrunk": True}})
  assert (await waiting)["ok"] is True


def test_move_command_normalizes_both_paths():
  body = FolderCommandBody(op="move", path="/a.txt", to="Done/a.txt")
  assert body.wire() == {"op": "move", "path": "a.txt", "to": "Done/a.txt"}


# ---------------------------------------------------------------------------
# Routes: who may report, answer, list and command.


def _agent(chat_id="chat-1", *, chat_app_id=None, run_app_id=None, **kwargs):
  """A running agent turn in `chat_id`; app ids mark an app-started chat or turn."""
  from app import models
  from app.database import SessionLocal
  run_id = f"run-{chat_id}"
  with SessionLocal() as db:
    if db.get(models.Chat, chat_id) is None:
      db.add(models.Chat(id=chat_id, legacy_messages=[], created_by_app_id=chat_app_id))
    if db.get(models.ChatRun, run_id) is None:
      db.add(models.ChatRun(id=run_id, chat_id=chat_id, status="running", initiated_by_app_id=run_app_id))
    db.commit()
  return {"Authorization": "Bearer " + auth_module.create_agent_token(chat_id, "test", 0, run_id=run_id, **kwargs)}


_REPORT = {
  "sequence": 1,
  "deviceId": "laptop",
  "deviceLabel": "Chrome on Mac",
  "folders": [{"id": "f1", "name": "Downloads", "mode": "edit", "access": "remembered", "state": "ready"}],
}


@pytest.fixture
def live_stream():
  broadcast = get_system_broadcast()
  subscription = broadcast.subscribe()
  yield subscription
  broadcast.unsubscribe(subscription)
  shared_folders.registry._reports.clear()
  shared_folders.registry._pending.clear()
  shared_folders.registry._stopped.clear()


def test_only_the_owner_browser_can_report_folders(client, auth, live_stream):
  path = f"/api/shared-folders/devices/{live_stream.id}"
  assert client.put(path, headers=_agent(), json=_REPORT).status_code == 403
  assert client.put(path, headers=auth, json=_REPORT).status_code == 204
  assert client.put("/api/shared-folders/devices/gone", headers=auth, json=_REPORT).status_code == 404


def test_owner_browser_lists_but_cannot_command_folders(client, auth, live_stream):
  client.put(f"/api/shared-folders/devices/{live_stream.id}", headers=auth, json=_REPORT)
  listed = client.get("/api/shared-folders", headers=auth)
  assert listed.status_code == 200
  assert listed.json()["folders"][0]["online"] is True
  command = client.post("/api/shared-folders/f1/commands", headers=auth, json={"op": "list"})
  assert command.status_code == 403


def test_guest_and_guest_started_agent_never_reach_owner_folders(https, auth):  # noqa: F811
  grant_id, token, _ = invite(https)
  guest = {"Authorization": "Bearer " + token}
  assert https.get("/api/shared-folders", headers=guest).status_code == 403
  assert https.put("/api/shared-folders/devices/x", headers=guest, json=_REPORT).status_code == 403
  from app.browser_access import BrowserLineage
  guest_agent = _agent(browser=BrowserLineage(grant_id))
  assert https.get("/api/shared-folders", headers=guest_agent).status_code == 403
  assert https.post(
    "/api/shared-folders/f1/commands", headers=guest_agent, json={"op": "list"},
  ).status_code == 403


def test_mini_app_tokens_never_reach_shared_folders(client, auth, db):
  from test_app_fixtures import create_local_app
  from app import models
  app_row = create_local_app(client, auth, name="Folder Snoop")
  owner = db.query(models.Owner).one()
  token = auth_module.create_app_token(app_row["id"], owner.username, owner.token_epoch)
  app_headers = {"Authorization": f"Bearer {token}"}
  assert client.get("/api/shared-folders", headers=app_headers).status_code == 403
  assert client.post(
    "/api/shared-folders/f1/commands", headers=app_headers, json={"op": "list"},
  ).status_code == 403


def test_only_the_owners_own_live_chats_reach_shared_folders(client, auth, live_stream):
  from test_app_fixtures import create_local_app
  client.put(f"/api/shared-folders/devices/{live_stream.id}", headers=auth, json=_REPORT)
  news = create_local_app(client, auth, name="News")["id"]
  unattended = {
    "app-started chat": _agent("news-digest", chat_app_id=news, run_app_id=news),
    "app-started turn in an owner chat": _agent("owner-chat", run_app_id=news),
    "agent token without a run": {
      "Authorization": "Bearer " + auth_module.create_agent_token("chat-1", "test", 0),
    },
  }
  for label, headers in unattended.items():
    assert client.get("/api/shared-folders", headers=headers).status_code == 403, label
    command = client.post("/api/shared-folders/f1/commands", headers=headers, json={"op": "list"})
    assert command.status_code == 403, label
    assert "background or scheduled" in command.json()["detail"], label
  delivered = []
  while not live_stream.empty():
    delivered.append(live_stream.get_nowait()["type"])
  assert "shared_folder_command" not in delivered
  listed = client.get("/api/shared-folders", headers=_agent("my-chat"))
  assert listed.status_code == 200 and listed.json()["folders"][0]["id"] == "f1"


def test_agent_command_round_trips_through_the_owner_browser(client, auth, live_stream):
  client.put(f"/api/shared-folders/devices/{live_stream.id}", headers=auth, json=_REPORT)
  outcome = {}

  def agent_runs_command():
    outcome["response"] = client.post(
      "/api/shared-folders/f1/commands", headers=_agent(),
      json={"op": "read", "path": "notes.txt", "length": 10},
    )

  thread = threading.Thread(target=agent_runs_command)
  thread.start()
  event = _await_stream_event(live_stream)
  assert event == {
    "type": "shared_folder_command", "commandId": event["commandId"], "folderId": "f1",
    "startWithinMs": 25000, "op": "read", "path": "notes.txt", "length": 10,
  }
  answered = client.post("/api/shared-folders/responses", headers=auth, json={
    "subscriptionId": live_stream.id, "commandId": event["commandId"],
    "ok": True, "result": {"text": "hello", "size": 5},
  })
  assert answered.status_code == 200, answered.text
  thread.join(timeout=10)
  assert outcome["response"].json() == {"ok": True, "result": {"text": "hello", "size": 5}}


def _answer_a_read(client, auth, live_stream, result):
  """The browser's answer response and what the waiting agent receives."""
  client.put(f"/api/shared-folders/devices/{live_stream.id}", headers=auth, json=_REPORT)
  outcome = {}

  def agent_runs_command():
    outcome["agent"] = client.post(
      "/api/shared-folders/f1/commands", headers=_agent(), json={"op": "read", "path": "notes.txt"},
    )

  thread = threading.Thread(target=agent_runs_command)
  thread.start()
  event = _await_stream_event(live_stream)
  answered = client.post("/api/shared-folders/responses", headers=auth, json={
    "subscriptionId": live_stream.id, "commandId": event["commandId"], "ok": True, "result": result,
  })
  thread.join(timeout=10)
  return answered, outcome["agent"].json()


def test_non_ascii_answer_is_measured_by_its_real_size(client, auth, live_stream):
  # 4 MB of Cyrillic: under the 8 MB cap, but 12 MB if each letter were \u-escaped.
  text = "ж" * 2_000_000
  answered, agent = _answer_a_read(client, auth, live_stream, {"text": text})
  assert answered.status_code == 200, answered.text
  assert agent["ok"] is True
  assert len(agent["result"]["text"]) == 2_000_000


def test_answer_over_the_size_cap_reaches_the_agent_as_too_large(client, auth, live_stream):
  answered, agent = _answer_a_read(client, auth, live_stream, {"text": "a" * (8 * 1024 * 1024 + 1)})
  assert answered.status_code == 413
  assert (agent["ok"], agent["code"]) == (False, "too_large")


def test_agent_sees_refusals_as_ordinary_results(client, auth, live_stream):
  report = {**_REPORT, "folders": [{**_REPORT["folders"][0], "mode": "read"}]}
  client.put(f"/api/shared-folders/devices/{live_stream.id}", headers=auth, json=report)
  response = client.post(
    "/api/shared-folders/f1/commands", headers=_agent(), json={"op": "bin", "path": "a.txt"},
  )
  assert response.status_code == 200
  assert response.json()["code"] == "read_only"


def test_only_the_owner_browser_can_stop_shared_folders(client, auth, live_stream):
  client.put(f"/api/shared-folders/devices/{live_stream.id}", headers=auth, json=_REPORT)
  assert client.post("/api/shared-folders/pause", headers=_agent(), json={}).status_code == 403
  stopped = client.post("/api/shared-folders/pause", headers=auth, json={})
  assert stopped.status_code == 200 and stopped.json() == {"paused": ["f1"]}
  refused = client.post("/api/shared-folders/f1/commands", headers=_agent(), json={"op": "list"})
  assert refused.json()["code"] == "paused"


def test_agent_cannot_answer_for_the_browser(client, auth, live_stream):
  response = client.post("/api/shared-folders/responses", headers=_agent(), json={
    "subscriptionId": live_stream.id, "commandId": "x", "ok": True,
  })
  assert response.status_code == 403


def _await_stream_event(subscription, timeout=5.0):
  deadline = time.monotonic() + timeout
  while time.monotonic() < deadline:
    try:
      return subscription.get_nowait()
    except asyncio.QueueEmpty:
      time.sleep(0.01)
  raise AssertionError("the browser stream received no command")


# ---------------------------------------------------------------------------
# The agent's `folders` command.

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def folders_cli(monkeypatch):
  """The command with a fake connection that records what it would send."""
  monkeypatch.syspath_prepend(str(_SCRIPTS))
  spec = importlib.util.spec_from_file_location("folders_cli_under_test", _SCRIPTS / "folders.py")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  client = module.Folders.__new__(module.Folders)
  client._listing = [{"id": "f1", "name": "Downloads", "mode": "edit", "access": "remembered", "state": "ready"}]
  client.sent = []
  client.run = lambda folder, command: client.sent.append(command) or {
    "path": command["path"], "to": command.get("to"), "size": 0, "created": True,
  }

  def run(*argv, stdin=b""):
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin)))
    args = module._parser().parse_args(list(argv))
    args.run(client, args)
    return client.sent

  return run


def test_write_never_reads_input_unless_asked_and_refuses_empty_content(folders_cli):
  with pytest.raises(SystemExit):
    folders_cli("write", "Downloads/a.txt")
  with pytest.raises(SystemExit, match="empty"):
    folders_cli("write", "Downloads/a.txt", "--overwrite", "--from-file", "-")
  with pytest.raises(SystemExit, match="empty"):
    folders_cli("write", "Downloads/a.txt", "--text", "")
  assert folders_cli("write", "Downloads/a.txt", "--from-file", "-", stdin=b"hi")[-1]["content"] == "aGk="
  assert folders_cli("write", "Downloads/b.txt", "--text", "", "--allow-empty")[-1]["content"] == ""


def test_move_stays_inside_the_folder_named_by_both_paths(folders_cli):
  sent = folders_cli("move", "Downloads/IMG_1.jpg", "Downloads/Photos/2026-10-01.jpg")
  assert sent[-1] == {"op": "move", "path": "IMG_1.jpg", "to": "Photos/2026-10-01.jpg"}
  assert folders_cli("move", "Downloads/IMG_1.jpg", "downloads/a.jpg")[-1]["to"] == "a.jpg"
  for destination in ("Photos/2026-10-01.jpg", "Downloads", "Downloads/"):
    with pytest.raises(SystemExit, match="inside one shared folder"):
      folders_cli("move", "Downloads/IMG_1.jpg", destination)
  assert folders_cli("move", "IMG_1.jpg", "Photos/a.jpg", "--folder", "f1")[-1]["to"] == "Photos/a.jpg"
