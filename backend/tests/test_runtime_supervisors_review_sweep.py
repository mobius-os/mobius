"""The review lifecycle tick runs in the periodic sweeper, off the event loop, and never takes the lease sweep down."""
import asyncio
import logging
import threading
from types import SimpleNamespace

import pytest

from app.broadcast import SystemBroadcast
from app.runtime_supervisors import RuntimeSupervisors


def _supervisors():
  return RuntimeSupervisors(
    settings=SimpleNamespace(data_dir="/tmp"),
    logger=logging.getLogger("test.runtime-supervisors.review-sweep"),
    restart_authorization=None,
    restart_fallback_chats=[],
  )


class _Session:
  def __init__(self):
    self.created_on = threading.get_ident()

  def __enter__(self):
    return self

  def __exit__(self, *_args):
    return False


def _quiet_everything_else(monkeypatch):
  """The same neutral stubs the lease-sweep test uses, so only the sweeper under test runs."""
  import app.broadcast as broadcast_module
  import app.chat as chat_module
  import app.contribution_autopilot as autopilot_module
  import app.contribution_autopilot_recovery as recovery_module
  import app.delegations as delegations_module
  import app.runtime_supervisors as supervisors_module

  async def no_chats(*_args, **_kwargs):
    return chat_module.ContinuationSweepResult()

  async def nothing(*_args, **_kwargs):
    return None

  async def zero():
    return 0

  monkeypatch.setattr(broadcast_module, "get_system_broadcast", lambda: SystemBroadcast())
  monkeypatch.setattr(supervisors_module, "SessionLocal", _Session)
  monkeypatch.setattr(supervisors_module, "AUTOPILOT_LEASE_RECOVERY_INTERVAL_SECS", 0)
  monkeypatch.setattr(supervisors_module, "DELEGATION_STARTUP_RECOVERY_INTERVAL_SECS", 3600)
  monkeypatch.setattr(supervisors_module, "DELEGATION_WAKE_RECOVERY_INTERVAL_SECS", 3600)
  monkeypatch.setattr(chat_module, "sweep_reset_parks", no_chats)
  monkeypatch.setattr(delegations_module, "reconcile_unstarted_delegations", nothing)
  monkeypatch.setattr(autopilot_module, "sweep_expired_leases", lambda db: 0)
  monkeypatch.setattr(recovery_module, "recover_resolved_blocks", zero)


@pytest.mark.asyncio
async def test_the_sweeper_ticks_review_lifecycles_off_the_event_loop(monkeypatch):
  from app.routes import contribution_reviews as routes
  _quiet_everything_else(monkeypatch)
  main_thread, loop, ticked = threading.get_ident(), asyncio.get_running_loop(), asyncio.Event()

  def tick(db):
    assert db.created_on == threading.get_ident() != main_thread  # a session made and used in the worker
    loop.call_soon_threadsafe(ticked.set)
    return 0

  monkeypatch.setattr(routes, "sweep_review_lifecycles", tick)
  supervisors = _supervisors()
  await supervisors._start_chat_supervisors()
  await asyncio.wait_for(ticked.wait(), timeout=2)
  assert "autopilot-lease-recovery" in supervisors._tasks
  await supervisors.stop()


@pytest.mark.asyncio
async def test_a_failing_review_tick_neither_stops_the_sweeper_nor_the_lease_sweep(monkeypatch, caplog):
  import app.contribution_autopilot as autopilot_module
  from app.routes import contribution_reviews as routes
  _quiet_everything_else(monkeypatch)
  loop, leases, reviews = asyncio.get_running_loop(), [], asyncio.Event()

  def lease_sweep(db):
    leases.append(1)
    return 0

  def broken_tick(db):
    loop.call_soon_threadsafe(reviews.set)
    raise RuntimeError("a review row could not be read")

  monkeypatch.setattr(autopilot_module, "sweep_expired_leases", lease_sweep)
  monkeypatch.setattr(routes, "sweep_review_lifecycles", broken_tick)
  supervisors = _supervisors()
  with caplog.at_level(logging.ERROR, logger="test.runtime-supervisors.review-sweep"):
    await supervisors._start_chat_supervisors()
    await asyncio.wait_for(reviews.wait(), timeout=2)
    reviews.clear()
    await asyncio.wait_for(reviews.wait(), timeout=2)  # it ran again: the loop survived the failure
    await supervisors.stop()
  assert len(leases) >= 2
  assert "review lifecycle sweep failed" in caplog.text
