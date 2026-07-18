"""Writer-fatal auto-recovery: the supervised in-process respawn (design §1).

`start_writer` was always BUILT to replace a fatal singleton, but nothing
runtime called it — a thread-fatal writer stayed a zombie (failing every ack)
until an external restart. `supervise_writer` closes that: the 60s supervisor
loop respawns a dead actor. The 60s cadence is the only backoff — a genuinely
broken DB re-fatals on the fresh actor's boot SELECT 1 probe, so the design is
one bounded respawn per tick, never a tight spin.

The autouse `fresh_db` fixture (conftest) starts a healthy writer per test, so
the baseline is a serving actor; tests that break it restore a healthy one so
the shared process singleton isn't poisoned for siblings.
"""

from app import chat_writer
from app.chat_writer import get_writer
from app.database import SessionLocal


def test_supervise_writer_respawns_fatal_writer():
  """Fatal → one supervisor tick → ready again, with no manual start_writer.

  This is the fail-before/pass-after: without the tick the fatal actor stays a
  zombie; `supervise_writer` respawns it and the fresh actor serves.
  """
  writer = get_writer()
  assert chat_writer.writer_needs_respawn() is False

  # Drive the actor fatal — it now fails every ack rather than committing.
  writer._go_fatal()
  assert chat_writer.writer_needs_respawn() is True
  assert chat_writer.is_writer_ready() is False

  # One tick respawns it — no caller passes start_writer explicitly.
  assert chat_writer.supervise_writer() is True
  fresh = get_writer()
  assert fresh is not writer  # a new actor replaced the fatal singleton
  assert fresh._session_ready.wait(timeout=5), chat_writer.writer_readiness()
  assert chat_writer.is_writer_ready() is True


def test_supervise_writer_is_noop_on_healthy_writer():
  """A serving writer is never respawned — that would orphan its live thread."""
  writer = get_writer()
  assert chat_writer.writer_needs_respawn() is False
  assert chat_writer.supervise_writer() is False
  assert get_writer() is writer


def test_supervise_writer_respawns_dead_thread():
  writer = get_writer()
  writer.stop(timeout=5)
  writer._stopping = False
  assert chat_writer.writer_needs_respawn() is True

  assert chat_writer.supervise_writer() is True
  fresh = get_writer()
  assert fresh is not writer
  assert fresh._session_ready.wait(timeout=5)
  assert chat_writer.is_writer_ready() is True


def test_supervise_writer_leaves_stopping_writer_alone():
  """A deliberately-stopping writer is a shutdown, not a fault to respawn."""
  writer = get_writer()
  writer._stopping = True
  try:
    assert chat_writer.writer_needs_respawn() is False
    assert chat_writer.supervise_writer() is False
    assert get_writer() is writer
  finally:
    writer._stopping = False


def test_supervise_writer_rerespawns_broken_db_once_per_tick(monkeypatch):
  """Still-broken DB → each tick respawns exactly once, no tight spin.

  `supervise_writer` respawns via `start_writer()` with no factory, which
  resolves `app.database.SessionLocal`. Patch that to a session whose boot
  probe raises, so every respawn re-fatals — modeling a genuinely-broken DB —
  and assert each tick makes exactly one bounded attempt (the loop's 60s sleep
  is the only backoff).
  """
  import time as _time
  from app import database as db_mod

  class _UnusableSession:
    """Opens, but cannot execute a single statement (broken connection)."""

    def execute(self, *_a, **_k):
      raise RuntimeError("connection refused on first use")

    def rollback(self):
      pass

    def close(self):
      pass

  monkeypatch.setattr(db_mod, "SessionLocal", lambda: _UnusableSession())

  try:
    # Break the current writer so the first tick has work to do.
    get_writer()._go_fatal()
    assert chat_writer.writer_needs_respawn() is True

    first = get_writer()
    assert chat_writer.supervise_writer() is True  # one respawn this tick
    assert get_writer() is not first  # replaced with a fresh actor

    # The fresh actor re-fatals asynchronously on the broken factory's boot
    # probe; the NEXT tick attempts again — one bounded respawn per call.
    for _ in range(100):
      if chat_writer.writer_needs_respawn():
        break
      _time.sleep(0.02)
    assert chat_writer.writer_needs_respawn() is True

    second = get_writer()
    assert chat_writer.supervise_writer() is True  # a second, bounded, attempt
    assert get_writer() is not second
  finally:
    # Restore a healthy writer on the REAL factory so sibling tests survive.
    chat_writer.stop_writer(timeout=5)
    chat_writer.start_writer(SessionLocal)
    assert get_writer()._session_ready.wait(timeout=5)
