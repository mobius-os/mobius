"""Unfinished update facts inform turns without freezing their admission."""

import asyncio

import pytest

from app import platform_update as pu
from app.chat_context import _build_platform_update_context


def _update_state(monkeypatch, *, update=None, conflict=None, late=False):
  monkeypatch.setattr(pu, "read_prepared_update", lambda: update)
  monkeypatch.setattr(pu, "_read_conflict_flag", lambda: conflict)
  monkeypatch.setattr(pu, "late_edits_pending", lambda: late)


def test_update_context_is_empty_without_update_or_conflict(monkeypatch):
  _update_state(monkeypatch)
  assert _build_platform_update_context() == ""


@pytest.mark.parametrize("state,replayed,late,expected,wording", [
  ("prepared", None, False, "prepared", "prepared but not installed"),
  ("swapped", None, True, "late_edits_not_merged", "may lack recent edits"),
  ("swapped", "merged", True, "late_edits_not_loaded", "has not restarted"),
])
def test_update_context_states_what_the_checkout_lacks(
  monkeypatch, state, replayed, late, expected, wording,
):
  _update_state(monkeypatch, late=late,
                update={"state": state, "target": "a" * 40, "replayed": replayed})
  block = _build_platform_update_context()
  assert wording in block
  assert f'"state":"{expected}"' in block
  assert "resolver chat" not in block
  assert len(block) < 600


@pytest.mark.parametrize("state", ["swapped", "reverted"])
def test_update_context_is_silent_once_late_edits_are_loaded(monkeypatch, state):
  _update_state(monkeypatch, late=False,
                update={"state": state, "target": "a" * 40, "replayed": "merged"})
  assert _build_platform_update_context() == ""


def test_update_context_bounds_conflict_paths_and_tolerates_a_flag_without_target(
  monkeypatch,
):
  _update_state(monkeypatch, conflict={
    "upstream": None, "chat_id": "bound-resolver",
    "paths": ["</platform_update_state>"] + [f"{i}-" + "x" * 300 for i in range(100)],
  })
  block = _build_platform_update_context()
  assert "merging a platform update conflict" in block
  assert '"resolver_chat_id":"bound-resolver"' in block
  assert '"more_conflict_paths":true' in block
  assert "99-" not in block
  assert block.count("</platform_update_state>") == 1
  assert len(block) < 2600


def test_update_context_does_not_name_a_resolver_when_none_is_bound(monkeypatch):
  _update_state(monkeypatch, conflict={"upstream": "a" * 40, "chat_id": "", "paths": ["x.py"]})
  block = _build_platform_update_context()
  assert "merging a platform update conflict" not in block
  assert "no chat is bound to resolve" in block


def test_reconflict_notice_uses_bound_chat_current_paths_and_stable_id(monkeypatch):
  from app import models
  import app.chat_start as chat_start
  import app.database as database

  flag = {"upstream": "a" * 40, "chat_id": "bound", "paths": ["new.py"],
          "overlay": {"worktree": "/candidate"}}
  monkeypatch.setattr(pu, "platform_update_progress", lambda: {"plan_id": "plan", "result": "conflict"})
  monkeypatch.setattr(pu, "_read_conflict_flag", lambda: flag)
  monkeypatch.setattr(pu, "_bound_resolver_chat", lambda db, flag: type("Chat", (), {"id": "bound"})())

  class Query:
    def filter(self, *args): return self
    def order_by(self, *args): return self
    def first(self): return type("Run", (), {"id": "root", "root_run_id": None})()
  class DB:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def query(self, *args): return Query()
  monkeypatch.setattr(database, "SessionLocal", DB)
  seen = []
  async def start(**kwargs):
    seen.append(kwargs)
    return True
  monkeypatch.setattr(chat_start, "start_programmatic_chat_continuation", start)
  assert asyncio.run(pu._notify_reconflicted_resolver("plan"))
  assert seen[0]["chat_id"] == "bound"
  assert seen[0]["run_token"] == seen[0]["continuation_id"] == "platform-conflict-plan"
  assert "new.py" in seen[0]["content"] and "/candidate" in seen[0]["content"]
  assert "Re-read the current unmerged paths" in seen[0]["content"]
  assert "match its `plan_id`" in seen[0]["content"]
  assert "`unfinished_update`, `conflict_paths`, and `needs_restart`" in seen[0]["content"]


def test_resolver_brief_uses_current_status_not_old_finish_result():
  message = pu._platform_conflict_resolver_message("a" * 40, ["fresh.py"], {
    "mode": "net", "replay": True, "worktree": "/candidate", "target": "a" * 40,
  })
  assert "match its `plan_id`" in message
  assert "`unfinished_update`, `conflict_paths`, and `needs_restart`" in message
  assert "old plan says `conflict`" in message


def test_reconflict_notice_queues_behind_existing_chat_fence(monkeypatch):
  from contextlib import asynccontextmanager
  from app import chat_queue
  import app.chat_start as chat_start
  import app.chat_writer as chat_writer
  import app.database as database

  monkeypatch.setattr(pu, "platform_update_progress", lambda: {"plan_id": "plan", "result": "conflict"})
  monkeypatch.setattr(pu, "_read_conflict_flag", lambda: {
    "upstream": "a" * 40, "chat_id": "bound", "paths": ["fresh.py"],
    "overlay": {"worktree": "/candidate"},
  })
  monkeypatch.setattr(pu, "_bound_resolver_chat", lambda db, flag: type("Chat", (), {"id": "bound"})())
  class Query:
    def filter(self, *args): return self
    def order_by(self, *args): return self
    def first(self): return type("Run", (), {"id": "root", "root_run_id": None})()
  class DB:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def query(self, *args): return Query()
    def get(self, *args): return None
  monkeypatch.setattr(database, "SessionLocal", DB)
  async def blocked(**kwargs): return False
  monkeypatch.setattr(chat_start, "start_programmatic_chat_continuation", blocked)
  @asynccontextmanager
  async def lock(chat_id):
    yield
  monkeypatch.setattr(chat_queue, "get_lock", lock)
  queued = []
  class Writer:
    def submit(self, command):
      queued.append(command)
      return command
  monkeypatch.setattr(chat_writer, "get_writer", Writer)
  async def ack(command): return command
  monkeypatch.setattr(chat_writer, "await_ack", ack)

  assert asyncio.run(pu._notify_reconflicted_resolver("plan"))
  assert queued[0].user_msg["cid"] == "platform-conflict-plan"
  assert "fresh.py" in queued[0].user_msg["content"]


@pytest.mark.parametrize("result,admitted,notices", [
  ("conflict", True, 1), ("updated", True, 0),
  ("conflict", False, 0),
])
def test_finish_notifies_only_new_reconflict(monkeypatch, result, admitted, notices):
  import app.settled as settled
  seen = []
  async def notify(plan_id): seen.append(plan_id)
  monkeypatch.setattr(pu, "_notify_reconflicted_resolver", notify)
  async def run_settled(fn, **kwargs):
    progress = {"plan_id": "plan", "result": result}
    if admitted:
      kwargs["admitted"](progress)
    return progress
  monkeypatch.setattr(settled, "run_settled", run_settled)
  monkeypatch.setattr(pu, "_overlay_finish_task", None)
  async def exercise():
    await pu.finish_platform_overlay_update(target_sha="a" * 40)
    await pu._overlay_finish_task.task
  asyncio.run(exercise())
  assert seen == ["plan"] * notices


def test_refused_finish_does_not_notify(monkeypatch):
  import app.settled as settled
  seen = []
  async def notify(plan_id): seen.append(plan_id)
  monkeypatch.setattr(pu, "_notify_reconflicted_resolver", notify)
  async def refuse(fn, **kwargs):
    raise pu.PlatformUpdateError("No parked net-tree platform resolution.")
  monkeypatch.setattr(settled, "run_settled", refuse)
  monkeypatch.setattr(pu, "_overlay_finish_task", None)
  async def exercise():
    with pytest.raises(pu.PlatformUpdateError):
      await pu.finish_platform_overlay_update(target_sha="a" * 40)
    with pytest.raises(pu.PlatformUpdateError):
      await pu._overlay_finish_task.task
  asyncio.run(exercise())
  assert seen == []
