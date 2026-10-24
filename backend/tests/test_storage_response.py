"""Pinned storage responses retain file semantics and close their owned handle."""

import asyncio

import pytest
from fastapi import HTTPException

from app import storage_response


def _track_open(monkeypatch):
  opened = []

  def tracked_open(path, mode):
    source = open(path, mode)
    opened.append(source)
    return source

  monkeypatch.setattr(storage_response, "open", tracked_open, raising=False)
  return opened


async def _receive():
  return {"type": "http.disconnect"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "range_header", "expected"), [
  ("GET", None, 200), ("HEAD", None, 200),
  ("GET", "bytes=1-3", 206), ("HEAD", "bytes=1-3", 206),
  ("GET", "bytes=1-2,4-5", 206),
  ("GET", "invalid", 400), ("GET", "bytes=999999-", 416),
])
async def test_storage_response_closes_pinned_file_and_never_offloads_its_alias(
  tmp_path, monkeypatch, method, range_header, expected,
):
  target = tmp_path / "stored.bin"
  target.write_bytes(b"0123456789" * 20000)
  opened = _track_open(monkeypatch)
  messages = []

  async def send(message):
    messages.append(message)

  await storage_response.StorageFileResponse(target)(
    {
      "type": "http", "method": method,
      "headers": [(b"range", range_header.encode())] if range_header else [],
      "extensions": {"http.response.pathsend": {}},
    },
    _receive, send,
  )

  assert len(opened) == 1
  assert opened[0].closed
  assert messages[0]["status"] == expected
  assert not any(message["type"] == "http.response.pathsend" for message in messages)
  assert max(len(message.get("body", b"")) for message in messages) <= 64 * 1024
  if method == "HEAD":
    assert all(not message.get("body") for message in messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError("send failed"), asyncio.CancelledError()])
async def test_storage_response_closes_pinned_file_when_send_fails_or_is_cancelled(
  tmp_path, monkeypatch, failure,
):
  target = tmp_path / "stored.bin"
  target.write_bytes(b"original")
  opened = _track_open(monkeypatch)

  async def send(message):
    if message["type"] == "http.response.body":
      raise failure

  with pytest.raises(type(failure)):
    await storage_response.StorageFileResponse(target)(
      {"type": "http", "method": "GET", "headers": []}, _receive, send,
    )
  assert len(opened) == 1
  assert opened[0].closed


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", [False, True])
async def test_storage_response_missing_or_replaced_by_directory_returns_404_before_headers(
  tmp_path, directory,
):
  target = tmp_path / "gone.bin"
  if directory:
    target.mkdir()
  messages = []

  async def send(message):
    messages.append(message)

  with pytest.raises(HTTPException) as caught:
    await storage_response.StorageFileResponse(target)(
      {"type": "http", "method": "GET", "headers": []}, _receive, send,
    )
  assert caught.value.status_code == 404
  assert messages == []
