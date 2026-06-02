"""Readiness-probe tests for `GET /api/ready`.

`/api/health` is liveness only — it answers 200 even when the single-writer
chat-persistence actor can't serve, so a deploy could green while every chat
write fails. `/api/ready` closes that gap: 200 only when the writer is
started, its worker thread is alive, and the actor is neither fatal nor
stopping (the `chat_writer.writer_readiness` predicate).

The autouse `fresh_db` fixture (conftest) starts a real writer actor per test
bound to the test DB, so the happy path sees a genuinely-ready writer. The
not-ready test drives the actor fatal exactly as
`test_terminal_completion.py` does (`get_writer()._go_fatal()`), then RESTORES
a healthy writer (stop + start on the test session factory, mirroring
conftest's setup) so it does not poison sibling tests that share the process
singleton.
"""

from app import chat_writer
from app.chat_writer import get_writer
from app.database import SessionLocal


def test_ready_returns_200_when_writer_running(client):
  """With the writer running (the fixture's default), /api/ready is 200."""
  r = client.get("/api/ready")
  assert r.status_code == 200
  assert r.json() == {"ready": True}
  # Liveness is unaffected and stays simple.
  h = client.get("/api/health")
  assert h.status_code == 200
  assert h.json() == {"status": "ok"}


def test_ready_returns_503_when_writer_fatal_then_recovers(client):
  """A fatal actor flips /api/ready to 503; restoring a healthy writer
  returns it to 200. The restore is the point — a fatal singleton left
  behind would fail every sibling test's chat write."""
  # Sanity: ready before we break it.
  assert client.get("/api/ready").status_code == 200

  # Drive the actor fatal (same seam test_terminal_completion uses). It now
  # fails every ack instead of committing, so it is NOT ready to serve.
  get_writer()._go_fatal()

  r = client.get("/api/ready")
  assert r.status_code == 503
  body = r.json()
  assert body["ready"] is False
  assert body["reason"]  # a short explanation, not empty

  # /api/health is liveness-only and must stay 200 even while not ready —
  # that is exactly the gap /api/ready exists to close.
  assert client.get("/api/health").status_code == 200

  # Restore a healthy writer so sibling tests aren't poisoned by the fatal
  # singleton. Mirror conftest's setup: stop the dead actor, start a fresh
  # one bound to the test session factory.
  chat_writer.stop_writer(timeout=5)
  chat_writer.start_writer(SessionLocal)

  assert client.get("/api/ready").status_code == 200
