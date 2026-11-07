"""A changed target base or Stop after reservation is never a merge dispatch."""
import asyncio

import pytest
from fastapi import HTTPException

from app import contribution_review_merge_actions as merge_actions


@pytest.mark.parametrize("queued", [False, True])
def test_final_base_drift_is_not_dispatched_for_merge_and_queue(monkeypatch, queued):
  calls = []
  def moved(*_args):
    calls.append("base")
    raise HTTPException(409, "The target base moved")
  monkeypatch.setattr(merge_actions.reviews, "assert_current_base", moved)
  monkeypatch.setattr(merge_actions.reviews, "perform_merge", lambda *_args: pytest.fail("merged"))
  monkeypatch.setattr(merge_actions.reviews, "enqueue", lambda *_args: pytest.fail("queued"))
  result = asyncio.run(merge_actions.dispatch_merge(None, None, {}, {},
    {"isMergeQueueEnabled": queued}, lambda: calls.append("authorized")))
  assert result.state == "not_dispatched"
  assert calls == ["authorized", "base"]


def test_stop_during_final_base_read_is_not_dispatched(monkeypatch):
  stopped = []
  monkeypatch.setattr(merge_actions.reviews, "assert_current_base", lambda *_args: stopped.append(1))
  monkeypatch.setattr(merge_actions.reviews, "perform_merge", lambda *_args: pytest.fail("merged"))
  def authorize():
    if stopped:
      raise HTTPException(409, "Stopped")
  result = asyncio.run(merge_actions.dispatch_merge(None, None, {}, {},
    {"isMergeQueueEnabled": False}, authorize))
  assert result.state == "not_dispatched" and stopped == [1]


def test_cancellation_during_final_base_read_releases_only_unspent_claim(monkeypatch):
  import threading
  from types import SimpleNamespace
  entered, finish = threading.Event(), threading.Event()
  released = []
  def base(*_):
    entered.set()
    assert finish.wait(5)
  monkeypatch.setattr(merge_actions.reviews, "assert_current_base", base)
  monkeypatch.setattr(merge_actions.reviews, "arm_merge", lambda *a: None)
  monkeypatch.setattr(merge_actions.reviews, "disarm_merge", lambda *a: released.append(a[-1]))
  monkeypatch.setattr(merge_actions.reviews, "perform_merge", lambda *a: pytest.fail("mutation after cancel"))
  row = SimpleNamespace(id="cancelled", outcomes_json={"example/project#7": {"state": "merging"}})
  target = {"repo": "example/project", "number": 7}
  db = SimpleNamespace(refresh=lambda row: None)
  async def scenario():
    task = asyncio.create_task(merge_actions._reserve_and_dispatch(db, row, target, {}, {}, {}, None,
      gh=None, cwd=None, authorize=lambda: None))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
      await task
  asyncio.run(scenario())
  assert len(released) == 1 and "cancelled" in released[0].lower()
