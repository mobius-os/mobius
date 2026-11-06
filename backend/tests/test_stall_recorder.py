"""The opt-in stall log records slow work with its cause, never its content."""

import asyncio
import gc
import json
import logging
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import database, stall_recorder
from app.config import get_settings


class _Records(logging.Handler):
  def __init__(self):
    super().__init__()
    self.events: list[dict] = []

  def emit(self, record):
    self.events.append(json.loads(record.getMessage()))


@pytest.fixture
def records():
  handler = _Records()
  stall_recorder._log.addHandler(handler)
  yield handler.events
  stall_recorder._log.removeHandler(handler)


@pytest.fixture
def config_file():
  path = Path(get_settings().data_dir) / "tracing.json"
  yield path
  path.unlink(missing_ok=True)


@pytest.fixture(autouse=True)
def _no_installed_recorder(monkeypatch):
  monkeypatch.setattr(stall_recorder, "_recorder", None)


def _app_with(recorder: stall_recorder.StallRecorder) -> FastAPI:
  app = FastAPI()

  @app.get("/items/{item_id}")
  def slow_item(item_id: str):
    time.sleep(0.3)
    return {"ok": True}

  app.add_middleware(stall_recorder.SlowRequestMiddleware, recorder=recorder)
  return app


def test_stall_log_is_off_without_config(config_file, records):
  app = FastAPI()
  assert stall_recorder.install(app, request_seconds=0) is False
  assert app.user_middleware == []
  stall_recorder.note_db_hold("GET /x", 5000)
  asyncio.run(asyncio.wait_for(stall_recorder.run(Path("/nonexistent")), 1))
  assert records == []


def test_stall_log_is_independent_of_tracing(config_file, records):
  config_file.write_text(json.dumps({"enabled": False, "stall_log": True}))
  app = FastAPI()
  assert stall_recorder.install(app, request_seconds=2) is True
  assert app.user_middleware[0].cls is stall_recorder.SlowRequestMiddleware
  stall_recorder.note_db_hold("GET /api/items/{item_id}", 2500)
  assert records[-1]["kind"] == "db_hold"


def test_slow_request_keeps_its_route_template_not_its_path(records):
  recorder = stall_recorder.StallRecorder(
    request_seconds=0.1,
    snapshot_threads=lambda exclude: {"busy": [], "waiting": []},
  )
  client = TestClient(_app_with(recorder))
  assert client.get("/items/secret-file.pdf?token=abc").status_code == 200
  (event,) = [e for e in records if e["kind"] == "slow_request"]
  assert event["route"] == "/items/{item_id}"
  assert event["method"] == "GET" and event["status"] == 200
  assert event["ms"] >= 100
  dumped = json.dumps(records)
  assert "secret" not in dumped and "token" not in dumped


@contextmanager
def _running(recorder, log_path, *, loop_threshold):
  """Run the recorder on its own event loop, as the server's supervisor does."""
  loop = asyncio.new_event_loop()
  loop.slow_callback_duration = loop_threshold
  task = loop.create_task(recorder.run(log_path))

  def serve():
    with suppress(asyncio.CancelledError):
      loop.run_until_complete(task)

  thread = threading.Thread(target=serve)
  thread.start()
  try:
    yield
  finally:
    loop.call_soon_threadsafe(task.cancel)
    thread.join()
    loop.close()


def test_late_request_with_no_busy_thread_is_marked_waiting(tmp_path, records):
  recorder = stall_recorder.StallRecorder(
    request_seconds=0.05,
    snapshot_threads=lambda exclude: {"busy": [], "waiting": []},
  )
  with _running(recorder, tmp_path / "stalls.jsonl", loop_threshold=0.02):
    TestClient(_app_with(recorder)).get("/items/1")
  (event,) = [e for e in records if e["kind"] == "slow_request"]
  assert event["no_busy_threads"] is True and event["snapshot"] is None
  assert not [e for e in records if e["kind"] == "snapshot"]


def _block_the_loop():
  time.sleep(0.4)


def test_loop_stall_is_logged_with_what_blocked_it(tmp_path):
  async def scenario():
    loop = asyncio.get_running_loop()
    loop.slow_callback_duration = 0.05
    task = asyncio.create_task(stall_recorder.StallRecorder(
      request_seconds=2,
    ).run(tmp_path / "logs" / "stalls.jsonl"))
    await asyncio.sleep(0.15)
    _block_the_loop()
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task

  asyncio.run(scenario())
  events = [
    json.loads(line)
    for line in (tmp_path / "logs" / "stalls.jsonl").read_text().splitlines()
  ]
  stalls = [e for e in events if e["kind"] == "loop_stall"]
  assert stalls and stalls[0]["ms"] >= 300
  snapshots = {e["snapshot_id"]: e for e in events if e["kind"] == "snapshot"}
  snapshot = snapshots[stalls[0]["snapshot"]]
  assert any(
    "_block_the_loop" in frame
    for thread in snapshot["busy"] for frame in thread["stack"]
  )


def test_stall_seen_while_the_snapshot_runs_keeps_its_snapshot(tmp_path):
  """The loop can wake while the watchdog is still snapshotting it; the
  loop_stall line must still link the snapshot, and no snapshot is orphaned."""
  def slow_snapshot(exclude):
    time.sleep(0.3)
    return {"busy": [{"thread": "t", "stack": ["f"]}], "waiting": []}

  async def scenario():
    asyncio.get_running_loop().slow_callback_duration = 0.05
    task = asyncio.create_task(stall_recorder.StallRecorder(
      request_seconds=2, snapshot_threads=slow_snapshot,
    ).run(tmp_path / "stalls.jsonl"))
    await asyncio.sleep(0.15)
    time.sleep(0.15)  # wakes before the 0.3 s snapshot finishes
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task

  asyncio.run(scenario())
  events = [
    json.loads(line) for line in (tmp_path / "stalls.jsonl").read_text().splitlines()
  ]
  stalls = [e for e in events if e["kind"] == "loop_stall"]
  snapshot_ids = {e["snapshot_id"] for e in events if e["kind"] == "snapshot"}
  assert len(stalls) == 1 and stalls[0]["ms"] >= 100
  assert {stalls[0]["snapshot"]} == snapshot_ids


def test_short_database_hold_never_resolves_its_label(monkeypatch, records):
  monkeypatch.setattr(stall_recorder, "_recorder", stall_recorder.StallRecorder(
    request_seconds=2,
  ))
  monkeypatch.setattr(database, "CHECKOUT_WARN_SECONDS", 3600)

  def label():
    raise AssertionError("resolved for a short hold")

  token = database.set_database_request_label(label)
  try:
    with database.engine.connect():
      pass
  finally:
    database.reset_database_request_label(token)
  assert not [e for e in records if e["kind"] == "db_hold"]


def test_request_label_names_the_route_and_lets_go_of_the_request():
  """Tasks a request starts inherit its label and can outlive it."""
  from app.main import _DatabaseRequestContextMiddleware

  seen = []

  async def routed_app(scope, receive, send):
    scope["route"] = SimpleNamespace(path="/items/{item_id}")
    seen.append(database._request_label.get())

  scope = {"type": "http", "method": "GET", "path": "/items/secret-file.pdf",
           "headers": [(b"cookie", b"session=abc")]}
  asyncio.run(_DatabaseRequestContextMiddleware(routed_app)(scope, None, None))
  (label,) = seen
  scope["route"] = SimpleNamespace(path="/changed")
  assert label() == "GET /items/{item_id}"
  assert not any(referent is scope for referent in gc.get_referents(label))


def test_long_database_hold_is_named_by_its_resolved_label(monkeypatch, records):
  recorder = stall_recorder.StallRecorder(request_seconds=2)
  monkeypatch.setattr(stall_recorder, "_recorder", recorder)
  monkeypatch.setattr(database, "CHECKOUT_WARN_SECONDS", 0)
  resolved = []

  def label():
    resolved.append(True)
    return "GET /items/{item_id}"

  token = database.set_database_request_label(label)
  try:
    with database.engine.connect():
      pass
  finally:
    database.reset_database_request_label(token)
  assert resolved == [True]
  holds = [e for e in records if e["kind"] == "db_hold"]
  assert holds[-1]["request"] == "GET /items/{item_id}"
  lifetime = database.database_pool_snapshot()["lifetime"]
  assert lifetime["last_long_checkout"]["request"] == "GET /items/{item_id}"
