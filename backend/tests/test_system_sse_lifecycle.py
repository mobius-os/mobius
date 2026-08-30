"""Lifecycle contracts for the process-wide system SSE subscription."""

import asyncio
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest
from starlette.requests import ClientDisconnect

from app import models
from app.broadcast import SystemBroadcast
from app.deps import Principal
from app.routes import notify as notify_routes
from app.routes import projects as project_routes


class _ConnectedRequest:
  async def is_disconnected(self):
    return False


class _ClosingSession:
  def __init__(self):
    self.close_calls = 0

  def close(self):
    self.close_calls += 1


async def _build_system_stream(monkeypatch):
  broadcast = SystemBroadcast()
  monkeypatch.setattr(
    notify_routes, "get_system_broadcast", lambda: broadcast,
  )
  db = _ClosingSession()
  response = await notify_routes.stream_system_events(
    request=_ConnectedRequest(),
    principal=Principal(
      owner=models.Owner(username="test"), app_id=None, scope="owner",
    ),
    db=db,
  )
  return broadcast, db, response


@pytest.mark.asyncio
async def test_system_stream_response_construction_does_not_subscribe(
  monkeypatch,
):
  """A client lost before body iteration must not leave an unread queue."""
  broadcast, db, response = await _build_system_stream(monkeypatch)

  assert db.close_calls == 1
  assert broadcast.subscribers == []

  await response.body_iterator.aclose()
  assert broadcast.subscribers == []


@pytest.mark.asyncio
async def test_system_stream_first_yield_subscribes_and_close_unsubscribes(
  monkeypatch,
):
  """The live queue exists only for the generator's active lifetime."""
  broadcast, _db, response = await _build_system_stream(monkeypatch)
  iterator = response.body_iterator

  try:
    first = await iterator.__anext__()
    assert "system_stream_open" in first
    assert len(broadcast.subscribers) == 1
  finally:
    await iterator.aclose()

  assert broadcast.subscribers == []


@pytest.mark.asyncio
async def test_project_change_event_is_serializable_on_system_stream(monkeypatch):
  """Project mutation events must survive the raw json.dumps SSE boundary."""
  broadcast, _db, response = await _build_system_stream(monkeypatch)
  iterator = response.body_iterator
  await iterator.__anext__()

  change = project_routes._change_view(SimpleNamespace(
    id=7,
    kind="file_written",
    path="index.html",
    prior_path=None,
    revision="abc123",
    actor_key="owner",
    display_name="Owner",
    created_at=datetime(2026, 8, 26, tzinfo=timezone.utc),
  ))
  broadcast.publish({
    "type": "project_file_changed",
    "projectId": "project-1",
    "change": change,
  })

  try:
    event = await iterator.__anext__()
    payload = json.loads(event.removeprefix("data: ").strip())
    assert payload["change"]["created_at"] == "2026-08-26T00:00:00+00:00"
  finally:
    await iterator.aclose()


@pytest.mark.asyncio
async def test_system_stream_cancellation_unsubscribes_waiting_queue(
  monkeypatch,
):
  """Cancelling a client blocked on the next event releases its queue."""
  broadcast, _db, response = await _build_system_stream(monkeypatch)
  iterator = response.body_iterator
  await iterator.__anext__()
  assert len(broadcast.subscribers) == 1

  waiting = asyncio.create_task(iterator.__anext__())
  await asyncio.sleep(0)
  assert not waiting.done()

  waiting.cancel()
  with pytest.raises(asyncio.CancelledError):
    await waiting

  assert broadcast.subscribers == []
  await iterator.aclose()


@pytest.mark.asyncio
async def test_system_stream_disconnect_during_body_send_unsubscribes(
  monkeypatch,
):
  """ASGI cancellation after a yielded frame must close the iterator."""
  broadcast, _db, response = await _build_system_stream(monkeypatch)
  body_send_started = asyncio.Event()

  async def receive():
    await body_send_started.wait()
    return {"type": "http.disconnect"}

  async def send(message):
    if message["type"] == "http.response.body":
      body_send_started.set()
      await asyncio.Future()

  await response(
    {
      "type": "http",
      "method": "GET",
      "path": "/api/events/system",
      "headers": [],
      "asgi": {"version": "3.0", "spec_version": "2.3"},
    },
    receive,
    send,
  )

  assert broadcast.subscribers == []


@pytest.mark.asyncio
async def test_system_stream_failed_body_send_unsubscribes(monkeypatch):
  """ASGI 2.4 send failure must close the iterator before surfacing."""
  broadcast, _db, response = await _build_system_stream(monkeypatch)

  async def receive():
    await asyncio.Future()

  async def send(message):
    if message["type"] == "http.response.body":
      raise OSError("client closed")

  with pytest.raises(ClientDisconnect):
    await response(
      {
        "type": "http",
        "method": "GET",
        "path": "/api/events/system",
        "headers": [],
        "asgi": {"version": "3.0", "spec_version": "2.4"},
      },
      receive,
      send,
    )

  assert broadcast.subscribers == []


@pytest.mark.asyncio
async def test_system_stream_response_task_cancellation_unsubscribes(
  monkeypatch,
):
  """Server cancellation while sending must still run shielded cleanup."""
  broadcast, _db, response = await _build_system_stream(monkeypatch)
  body_send_started = asyncio.Event()

  async def receive():
    await asyncio.Future()

  async def send(message):
    if message["type"] == "http.response.body":
      body_send_started.set()
      await asyncio.Future()

  response_task = asyncio.create_task(response(
    {
      "type": "http",
      "method": "GET",
      "path": "/api/events/system",
      "headers": [],
      "asgi": {"version": "3.0", "spec_version": "2.4"},
    },
    receive,
    send,
  ))
  await body_send_started.wait()
  assert len(broadcast.subscribers) == 1

  response_task.cancel()
  with pytest.raises(asyncio.CancelledError):
    await response_task

  assert broadcast.subscribers == []
