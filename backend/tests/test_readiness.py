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


def test_readiness_reports_not_ready_when_session_not_yet_open(client):
  """A live worker thread is NOT enough to be ready — the DB session must
  also have opened.

  `start()` publishes the actor (and spawns the worker thread) BEFORE that
  thread's `_run` opens its DB session, so there is a window in which the
  writer is the published singleton with a genuinely-alive thread yet still
  cannot persist a single command. `_session_ready` (set in `_run` right
  after `self._db = self._session_factory()` succeeds) closes that window:
  readiness must report not-ready, AFTER the thread-alive check but BEFORE
  the fatal check, whenever the thread is alive but the session hasn't opened.

  We simulate that window by clearing `_session_ready` on a started/published
  writer whose thread is alive, then assert `writer_readiness()` returns
  `(False, "writer session not ready")`. Restoring the event afterwards keeps
  the process singleton healthy for sibling tests.
  """
  writer = get_writer()
  # The fixture's writer is already serving, so its thread is alive and the
  # session opened — `_session_ready` is set. Clear it to reproduce the
  # publish-before-session-open window without racing a real start().
  assert writer._thread is not None and writer._thread.is_alive()
  writer._session_ready.clear()
  try:
    ready, reason = chat_writer.writer_readiness()
    assert ready is False
    assert reason == "writer session not ready"

    # The same state must surface at the HTTP probe: thread alive but session
    # not open is still not-ready, so /api/ready answers 503.
    r = client.get("/api/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["ready"] is False
    assert body["reason"] == "writer session not ready"
  finally:
    # Restore so the shared singleton is ready again for sibling tests.
    writer._session_ready.set()

  assert client.get("/api/ready").status_code == 200


def test_recreate_session_drops_readiness_for_the_whole_window(client):
  """`_recreate_session` must hold readiness not-ready for the WHOLE recreate.

  Recreate is the mid-loop DB-error recovery path: it tears down the poisoned
  session and opens a fresh one. While `_db` is gone the writer cannot persist,
  so `_session_ready` must be CLEAR for that window and only re-set once the
  replacement session actually opens. If the replacement factory raises (or
  hangs), the event must stay clear so the writer keeps reporting not-ready
  instead of advertising a session it doesn't have — a raise propagates to the
  outer handler's `_go_fatal`.

  Happy path: after a recreate that opens a fresh session, `_session_ready` is
  SET again. Failure path: a factory that raises during recreate leaves the
  event CLEAR and `writer_readiness()` reporting `(False, ...)`. We restore the
  real factory and re-set the event in `finally` so the shared singleton stays
  healthy for sibling tests.
  """
  writer = get_writer()
  assert writer._thread is not None and writer._thread.is_alive()
  real_factory = writer._session_factory
  try:
    # Happy path: a real recreate opens a fresh session and leaves readiness set.
    writer._recreate_session()
    assert writer._session_ready.is_set()
    assert chat_writer.writer_readiness() == (True, None)

    # Failure path: a raising factory leaves the event clear (and `_db` None),
    # so the writer keeps looking not-ready. `_recreate_session` re-raises so
    # the actor's outer handler can `_go_fatal`.
    def _boom():
      raise RuntimeError("session factory unavailable during recreate")

    writer._session_factory = _boom
    try:
      writer._recreate_session()
      raise AssertionError("expected _recreate_session to re-raise")
    except RuntimeError:
      pass
    assert not writer._session_ready.is_set()
    ready, reason = chat_writer.writer_readiness()
    assert ready is False
    assert reason  # a short explanation, not empty
  finally:
    # Restore the real factory + a healthy open session so sibling tests that
    # share the process singleton aren't poisoned by the simulated failure.
    writer._session_factory = real_factory
    writer._recreate_session()
    assert writer._session_ready.is_set()

  assert client.get("/api/ready").status_code == 200


def test_recreate_does_not_report_ready_on_a_session_that_cannot_execute(client):
  """A factory that RETURNS a broken-but-non-raising session must not be ready.

  Readiness means "provably usable", not "the factory returned an object". The
  recreate path probes the fresh session with `SELECT 1` before re-advertising
  ready; a session whose `execute` raises (a connection that opens lazily and
  fails on first use) leaves the event CLEAR and `_recreate_session` re-raises
  so the actor's outer handler can `_go_fatal`. We restore the real factory in
  `finally` so the shared singleton stays healthy for sibling tests.
  """
  writer = get_writer()
  real_factory = writer._session_factory

  class _UnusableSession:
    """Looks like a session, but cannot run a single statement."""

    def execute(self, *_a, **_k):
      raise RuntimeError("connection refused on first use")

    def rollback(self):
      pass

    def close(self):
      pass

  try:
    writer._session_factory = lambda: _UnusableSession()
    try:
      writer._recreate_session()
      raise AssertionError("expected the SELECT 1 probe to re-raise")
    except RuntimeError:
      pass
    assert not writer._session_ready.is_set()
    ready, reason = chat_writer.writer_readiness()
    assert ready is False
    assert reason
  finally:
    writer._session_factory = real_factory
    writer._recreate_session()
    assert writer._session_ready.is_set()

  assert client.get("/api/ready").status_code == 200


def test_run_boot_probe_does_not_report_ready_on_unusable_session(client):
  """The COLD-START (`_run`) probe, distinct from `_recreate_session`'s.

  `_run` opens the session at boot and probes SELECT 1 before advertising
  ready; a session that RETURNS but raises on first execute must drive the
  writer fatal at boot (its own `except BaseException` + return path, separate
  control flow from recreate). We start a fresh writer on such a factory, wait
  for the boot thread to probe + go fatal, and assert it never reports ready.
  Restore a healthy writer in `finally` so the shared singleton survives.
  """
  import time as _time

  class _UnusableSession:
    def execute(self, *_a, **_k):
      raise RuntimeError("connection refused on first use")

    def rollback(self):
      pass

    def close(self):
      pass

  chat_writer.stop_writer(timeout=5)
  try:
    chat_writer.start_writer(lambda: _UnusableSession())
    # The boot thread runs _run → probe raises → _go_fatal, asynchronously.
    for _ in range(100):
      if not chat_writer.is_writer_ready():
        break
      _time.sleep(0.02)
    ready, reason = chat_writer.writer_readiness()
    assert ready is False
    assert reason
    assert client.get("/api/ready").status_code == 503
  finally:
    chat_writer.stop_writer(timeout=5)
    chat_writer.start_writer(SessionLocal)

  assert client.get("/api/ready").status_code == 200
