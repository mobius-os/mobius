"""Serviceability-probe tests for `GET /api/ready`.

`/api/health` is reachability only — it answers 200 even when the database
schema or single-writer chat-persistence actor cannot serve. `/api/ready`
closes both gaps: 200 only when startup found no ORM schema mismatch and the
writer is started, alive, and neither fatal nor stopping.

The autouse `fresh_db` fixture (conftest) starts a real writer actor per test
bound to the test DB, so the happy path sees a genuinely-ready writer. The
not-ready test drives the actor fatal exactly as
`test_terminal_completion.py` does (`get_writer()._go_fatal()`), then RESTORES
a healthy writer (stop + start on the test session factory, mirroring
conftest's setup) so it does not poison sibling tests that share the process
singleton.
"""

from pathlib import Path

from app import chat_writer, main as main_module
from app.chat_writer import get_writer
from app.database import SessionLocal


def _wait_for_healthy_writer():
  """Wait for an explicit recovery restart's asynchronous DB boot probe."""
  writer = get_writer()
  assert writer._session_ready.wait(timeout=5), chat_writer.writer_readiness()
  assert chat_writer.writer_readiness() == (True, None)


def test_ready_returns_200_when_writer_running(client):
  """With the writer running (the fixture's default), /api/ready is 200."""
  r = client.get("/api/ready")
  assert r.status_code == 200
  assert r.json() == {"ready": True}
  # Liveness is unaffected and stays simple.
  h = client.get("/api/health")
  assert h.status_code == 200
  body = h.json()
  assert body["status"] == "ok"
  assert body["boot_id"]


def test_schema_gap_fails_serviceability_but_not_reachability(
  client, monkeypatch,
):
  """A mapped-column gap must keep every deployment probe fail-closed."""
  gap = "apps.paused_capabilities"
  main_module._SCHEMA_GAPS[:] = [gap]
  monkeypatch.setattr(main_module, "orm_schema_gaps", lambda _engine: [gap])
  try:
    ready = client.get("/api/ready")
    assert ready.status_code == 503
    assert ready.json() == {
      "ready": False,
      "reason": "schema_mismatch",
      "schema_gaps": [gap],
    }
    strict = client.get("/api/health/strict")
    assert strict.status_code == 503
    assert strict.json() == {
      "status": "schema_mismatch",
      "schema_gaps": [gap],
    }
    reachable = client.get("/api/health")
    assert reachable.status_code == 200
    assert reachable.json()["status"] == "schema_mismatch"
  finally:
    main_module._SCHEMA_GAPS.clear()


def test_external_schema_repair_restores_readiness_without_restart(
  client, monkeypatch,
):
  """Recovery can close a boot-detected gap while this process stays up."""
  main_module._SCHEMA_GAPS[:] = ["apps.paused_capabilities"]
  monkeypatch.setattr(main_module, "orm_schema_gaps", lambda _engine: [])
  try:
    ready = client.get("/api/ready")
    assert ready.status_code == 200
    assert ready.json() == {"ready": True}
    assert main_module._SCHEMA_GAPS == []
    assert client.get("/api/health").json()["status"] == "ok"
  finally:
    main_module._SCHEMA_GAPS.clear()


def test_deployment_healthchecks_use_the_serviceability_probe():
  """Managed, self-hosted, and image defaults must enforce one contract."""
  root = Path(__file__).resolve().parents[2]
  assert 'healthcheckPath = "/api/ready"' in (
    root / "railway.toml"
  ).read_text(encoding="utf-8")
  assert 'http://localhost:8000/api/ready' in (
    root / "docker-compose.yml"
  ).read_text(encoding="utf-8")
  assert 'http://localhost:8000/api/ready' in (
    root / "Dockerfile"
  ).read_text(encoding="utf-8")
  assert 'http://127.0.0.1:8000/api/ready' in (
    root / "backend/scripts/verify_test_runtime.py"
  ).read_text(encoding="utf-8")


def test_writer_probe_transactions_end_before_readiness():
  """Boot and recreate must release SELECT 1 before advertising readiness."""
  actions = []
  actor = None

  class _ProbeSession:
    def __init__(self, name):
      self.name = name

    def execute(self, *_args, **_kwargs):
      actions.append((f"{self.name}:execute", actor._session_ready.is_set()))

    def rollback(self):
      actions.append((f"{self.name}:rollback", actor._session_ready.is_set()))

    def close(self):
      actions.append((f"{self.name}:close", actor._session_ready.is_set()))

  actor = chat_writer.ChatWriterActor(lambda: _ProbeSession("boot"))
  actor.start()
  try:
    assert actor._session_ready.wait(timeout=5)
    assert actions[:2] == [
      ("boot:execute", False),
      ("boot:rollback", False),
    ]
  finally:
    actor.stop(timeout=5)

  actions.clear()
  old = _ProbeSession("old")
  fresh = _ProbeSession("fresh")
  actor = chat_writer.ChatWriterActor(lambda: fresh)
  actor._db = old
  actor._session_ready.set()
  actor._recreate_session()
  assert actions == [
    ("old:rollback", False),
    ("old:close", False),
    ("fresh:execute", False),
    ("fresh:rollback", False),
  ]
  assert actor._session_ready.is_set()


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
  _wait_for_healthy_writer()

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
    _wait_for_healthy_writer()

  assert client.get("/api/ready").status_code == 200
