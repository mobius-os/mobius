"""Lock-in spec: long-lived SSE endpoints must not pin a pooled DB
connection for the lifetime of the stream.

The bug this guards against: `GET /api/events/system` (one per open
Shell tab, held for the whole session) and `GET /api/chats/{id}/stream`
inject a session via `Depends(get_db)`. FastAPI defers a
yield-dependency's teardown for a StreamingResponse until the response
body FINISHES streaming, so without an explicit early `db.close()` each
open EventSource held one pooled connection until disconnect. On
Postgres the default QueuePool is 5 + 10 overflow; a day's worth of
Shell tabs and reconnects exhausted it and every DB-touching request
began failing with `QueuePool limit ... reached, connection timed out`
while /api/health (no DB) stayed green — the platform looked "up" but
was unusable.

The fix releases the request's session immediately after the auth /
ownership gate, before entering the stream loop.

These tests invoke the route functions directly rather than through a
TestClient: neither Starlette's TestClient nor httpx's ASGITransport
can cancel an infinite SSE generator mid-stream (ASGITransport buffers
the complete body; TestClient's close never flips is_disconnected), so
an HTTP-level test either hangs or only observes the pool AFTER the
deferred teardown already ran — exactly the window the bug lives in.
Direct invocation observes the invariant deterministically: the handler
must have released its session by the time it hands back the
StreamingResponse. The session passed in has an open connection checked
out (simulating the auth queries FastAPI's cached get_db sub-dependency
already ran on it), and the engine here is the app's real engine, whose
QueuePool is the same pool class Postgres uses in production.
"""

import pytest
from sqlalchemy import text

from app import models
from app.broadcast import create_broadcast, remove_broadcast
from app.database import SessionLocal, engine
from app.deps import Principal


class _ConnectedRequest:
  """Stand-in for starlette.Request whose client never disconnects."""

  async def is_disconnected(self):
    return False


def _pinned_session(baseline):
  """A session with a live connection checked out of the engine pool.

  Mirrors the state a request-scoped session is in when the stream
  handler runs: get_current_owner / get_principal already queried
  through it, so it holds a pooled connection. Checkout counts are
  relative to `baseline` because long-lived components (the chat-writer
  actor, activity logging) may hold their own connections for the whole
  test process — the invariant is that the STREAM's session goes back,
  not that the pool is globally empty.
  """
  db = SessionLocal()
  db.execute(text("SELECT 1"))
  assert engine.pool.checkedout() == baseline + 1, (
    "precondition: the session should pin one pooled connection"
  )
  return db


def _assert_pool_drained(baseline, when):
  held = engine.pool.checkedout() - baseline
  assert held == 0, (
    f"{held} pooled DB connection(s) still checked out {when} — "
    "the stream handler is pinning its request session. Release it "
    "(db.close()) before entering the stream loop."
  )


@pytest.mark.asyncio
async def test_system_stream_releases_db_connection_while_open():
  from app.routes.notify import stream_system_events

  baseline = engine.pool.checkedout()
  db = _pinned_session(baseline)
  try:
    response = await stream_system_events(
      request=_ConnectedRequest(),
      _owner=models.Owner(username="test"),
      db=db,
    )
    gen = response.body_iterator
    try:
      # Read the hello frame so the generator body is provably running —
      # the deferred-teardown window the bug lived in.
      first = await gen.__anext__()
      assert "system_stream_open" in first
      _assert_pool_drained(baseline, "while the system SSE stream is open")
    finally:
      await gen.aclose()
  finally:
    db.close()


@pytest.mark.asyncio
async def test_chat_stream_releases_db_connection_while_open():
  from app.routes.chats_stream import stream_chat

  chat_id = "sse-pool-test"
  baseline = engine.pool.checkedout()
  db = _pinned_session(baseline)
  setup = SessionLocal()
  setup.add(models.Chat(id=chat_id, title="sse pool test"))
  setup.commit()
  setup.close()

  # A live broadcast with one buffered event, so the endpoint streams
  # (with no broadcast it would 204 before ever reaching the stream).
  bc = create_broadcast(chat_id)
  bc.publish({"type": "text", "content": "hello"})

  try:
    response = await stream_chat(
      request=_ConnectedRequest(),
      chat_id=chat_id,
      principal=Principal(owner=models.Owner(username="test"), app_id=None),
      db=db,
    )
    gen = response.body_iterator
    try:
      first = await gen.__anext__()
      assert first.startswith("data:")
      _assert_pool_drained(baseline, "while the chat SSE stream is open")
    finally:
      await gen.aclose()
  finally:
    db.close()
    remove_broadcast(chat_id)
