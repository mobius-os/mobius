"""Programmatic chat starts share one durable lifecycle protocol."""

from app import transcript_rows
from app.chat_writer import create_chat
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app import chat_start
from app.chat_writer import StartTurn, StartTurnBlockedByPendingQuestion


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", [False, True], ids=["continuation", "activity"])
async def test_continuation_observes_shutdown_after_waiting_for_its_lock(
  monkeypatch, activity,
):
  from app import chat as chat_mod, chat_queue

  monkeypatch.setattr(chat_mod, "draining", False)

  @asynccontextmanager
  async def lock_that_crosses_shutdown():
    monkeypatch.setattr(chat_mod, "draining", True)
    yield

  monkeypatch.setattr(chat_queue, "get_lock", lambda _id: lock_that_crosses_shutdown())
  monkeypatch.setattr(
    chat_start, "get_writer",
    lambda: pytest.fail("Shutdown must not start or retire a continuation"),
  )
  common = dict(chat_id="draining-chat", root_run_id="root", run_token="physical")
  if activity:
    started = await chat_start.start_programmatic_activity_continuation(
      **common, source_work_id="root", activity_id="helper-result",
    )
  else:
    started = await chat_start.start_programmatic_chat_continuation(
      **common, content="Continue", continuation_id="saved", reason="wait",
    )
  assert started is False


@pytest.mark.asyncio
@pytest.mark.parametrize("activity", [False, True], ids=["continuation", "activity"])
@pytest.mark.parametrize("draining", [False, True], ids=["crash", "planned-restart"])
async def test_orphan_cleanup_preserves_planned_restart_but_fails_ambiguous_crash(
  db, monkeypatch, activity, draining,
):
  from app import chat as chat_mod, models

  chat_id, run_id = "restart-parent", "restart-physical"
  messages = [{"role": "user", "content": "Finish the saved work."}]
  pending = [{"role": "user", "content": "Keep this queued.", "ts": 1}]
  db.add(create_chat(
    id=chat_id, title="Restart parent", provider="codex",
    auto_resume_on_restart=True, messages=messages, pending_messages=pending,
    live_assistant={
      "id": run_id, "role": "assistant",
      "blocks": [{"type": "text", "content": "Already working."}],
    },
  ))
  db.flush()
  db.add(models.ChatRun(
    id=run_id, root_run_id="root-work", chat_id=chat_id, status="running",
    provider="codex", provider_execution_admitted=True,
    restart_nonce="accepted-planned-restart",
  ))
  db.commit()
  # The runner has stopped, but the drain has not yet parked its exact row.
  monkeypatch.setattr(chat_mod, "draining", draining)
  monkeypatch.setattr(chat_mod, "is_chat_running", lambda _chat_id: False)
  monkeypatch.setattr(
    chat_mod, "_schedule_continuation",
    lambda **_kwargs: pytest.fail("An admitted orphan must not replay tools"),
  )
  common = dict(chat_id=chat_id, root_run_id="root-work", run_token=run_id)
  if activity:
    started = await chat_start.start_programmatic_activity_continuation(
      **common, source_work_id="root-work", activity_id="helper-result",
    )
  else:
    started = await chat_start.start_programmatic_chat_continuation(
      **common, content="Continue", continuation_id="saved-continuation",
      reason="wait", hidden=True,
    )

  assert started is False
  db.expire_all()
  physical = db.get(models.ChatRun, run_id)
  assert physical.status == ("running" if draining else "failed")
  assert physical.restart_nonce == ("accepted-planned-restart" if draining else None)
  assert transcript_rows.read_all(db, chat_id) == messages
  assert db.get(models.Chat, chat_id).pending_messages == pending

  if draining:
    # Authenticated boot recovery must still find the exact unfinished run.
    monkeypatch.setattr(chat_mod, "draining", False)
    result = chat_mod.reconcile_startup_chats(
      db, restart_authorization="accepted-planned-restart",
    )
    assert result.restart_parks == [chat_id]
    assert result.manual == []
    db.expire_all()
    physical = db.get(models.ChatRun, run_id)
    assert physical.status == "parked"
    assert physical.park_reason == "restart"
    assert db.get(models.Chat, chat_id).pending_messages == pending


class _Writer:
  def __init__(self):
    self.commands = []

  def submit(self, command):
    self.commands.append(command)
    return "ack"


def _install_start_fakes(
  monkeypatch, *, generations=(7, 7), ack_result=None,
):
  writer = _Writer()
  generation_values = iter(generations)
  discarded = []
  created_broadcasts = []
  removed_broadcasts = []
  system_events = []
  scheduled = []
  run_calls = []

  monkeypatch.setattr(
    chat_start,
    "require_programmatic_chat_model",
    lambda _chat_id, _provider: "explicit-model",
  )
  monkeypatch.setattr(chat_start, "mark_starting", lambda _chat_id: True)
  monkeypatch.setattr(
    chat_start,
    "current_run_generation",
    lambda _chat_id: next(generation_values),
  )
  monkeypatch.setattr(
    chat_start, "discard_starting", lambda chat_id: discarded.append(chat_id),
  )
  monkeypatch.setattr(chat_start, "alloc_run_token", lambda: "run-1")
  monkeypatch.setattr(chat_start, "get_writer", lambda: writer)

  async def fake_await_ack(ack):
    assert ack == "ack"
    return ack_result if ack_result is not None else {
      "history": ["prepared-message"],
      "session_id": "session-1",
      "provider": "claude",
    }

  def fake_run_chat(*args, **kwargs):
    run_calls.append((args, kwargs))

    async def finish():
      return None

    return finish()

  def fake_create_task(coro):
    scheduled.append(coro)
    coro.close()
    return SimpleNamespace()

  monkeypatch.setattr(chat_start, "await_ack", fake_await_ack)
  monkeypatch.setattr(chat_start, "run_chat", fake_run_chat)
  monkeypatch.setattr(chat_start.asyncio, "create_task", fake_create_task)
  monkeypatch.setattr(
    chat_start,
    "create_broadcast",
    lambda chat_id: created_broadcasts.append(chat_id),
  )
  monkeypatch.setattr(
    chat_start,
    "remove_broadcast",
    lambda chat_id: removed_broadcasts.append(chat_id),
  )
  monkeypatch.setattr(
    chat_start,
    "get_system_broadcast",
    lambda: SimpleNamespace(publish=system_events.append),
  )
  return SimpleNamespace(
    writer=writer,
    discarded=discarded,
    created_broadcasts=created_broadcasts,
    removed_broadcasts=removed_broadcasts,
    system_events=system_events,
    scheduled=scheduled,
    run_calls=run_calls,
  )


@pytest.mark.asyncio
async def test_programmatic_start_owns_writer_fence_broadcast_and_spawn(
  monkeypatch,
):
  state = _install_start_fakes(monkeypatch)
  monkeypatch.setattr(chat_start.time, "time", lambda: 123.456)

  started = await chat_start.start_programmatic_chat_turn(
    chat_id="chat-1",
    title="Resolve conflict",
    content="Fix the files",
    provider="claude",
    initiated_by_app_id=42,
  )

  assert started is True
  assert len(state.writer.commands) == 1
  command = state.writer.commands[0]
  assert isinstance(command, StartTurn)
  assert command.chat_id == "chat-1"
  assert command.run_token == "run-1"
  assert command.title_source == "Resolve conflict"
  assert command.default_provider == "claude"
  assert command.initiated_by_app_id == 42
  assert command.user_msg == {
    "role": "user", "content": "Fix the files", "ts": 123456,
  }
  assert state.created_broadcasts == ["chat-1"]
  assert state.removed_broadcasts == []
  assert len(state.scheduled) == 1
  assert state.run_calls == [
    ((["prepared-message"],), {
      "chat_id": "chat-1",
      "session_id": "session-1",
      "provider_id": "claude",
      "run_gen": 7,
      "run_token": "run-1",
    })
  ]
  assert state.system_events == [
    {"type": "chat_run_started", "chatId": "chat-1"}
  ]
  assert state.discarded == []


@pytest.mark.asyncio
async def test_programmatic_start_does_nothing_when_claim_is_busy(monkeypatch):
  monkeypatch.setattr(
    chat_start,
    "require_programmatic_chat_model",
    lambda _chat_id, _provider: "explicit-model",
  )
  monkeypatch.setattr(chat_start, "mark_starting", lambda _chat_id: False)
  monkeypatch.setattr(
    chat_start,
    "get_writer",
    lambda: (_ for _ in ()).throw(AssertionError("writer must not run")),
  )

  assert await chat_start.start_programmatic_chat_turn(
    chat_id="busy", title="t", content="c", provider="codex",
  ) is False


@pytest.mark.asyncio
async def test_programmatic_start_preserves_hidden_product_event_identity(
  monkeypatch,
):
  state = _install_start_fakes(monkeypatch)
  monkeypatch.setattr(chat_start.time, "time", lambda: 123.456)

  assert await chat_start.start_programmatic_chat_turn(
    chat_id="parent",
    title="Delegation results",
    content="child completed",
    provider="codex",
    hidden=True,
    message_kind="delegation_result",
    source_work_id="goal-1",
  ) is True

  assert state.writer.commands[0].user_msg == {
    "role": "user",
    "content": "child completed",
    "ts": 123456,
    "hidden": True,
    "kind": "delegation_result",
    "source_work_id": "goal-1",
  }


@pytest.mark.asyncio
async def test_programmatic_start_yields_to_pending_owner_question(monkeypatch):
  state = _install_start_fakes(
    monkeypatch,
    ack_result=StartTurnBlockedByPendingQuestion("owner-decision"),
  )

  started = await chat_start.start_programmatic_chat_turn(
    chat_id="blocked", title="t", content="c", provider="codex",
  )

  assert started is False
  assert len(state.writer.commands) == 1
  assert state.created_broadcasts == []
  assert state.scheduled == []
  assert state.system_events == []
  assert state.discarded == ["blocked"]


@pytest.mark.asyncio
async def test_programmatic_start_yields_when_stop_wins_commit_race(monkeypatch):
  state = _install_start_fakes(monkeypatch, generations=(4, 5))

  started = await chat_start.start_programmatic_chat_turn(
    chat_id="stopped", title="t", content="c", provider="codex",
  )

  assert started is False
  assert len(state.writer.commands) == 1
  assert state.created_broadcasts == []
  assert state.scheduled == []
  assert state.system_events == []
  assert state.discarded == ["stopped"]


@pytest.mark.asyncio
async def test_programmatic_start_cleans_transient_owners_when_spawn_fails(
  monkeypatch,
):
  state = _install_start_fakes(monkeypatch)

  def fail_create_task(_coro):
    raise RuntimeError("task scheduler unavailable")

  monkeypatch.setattr(chat_start.asyncio, "create_task", fail_create_task)

  with pytest.raises(RuntimeError, match="task scheduler unavailable"):
    await chat_start.start_programmatic_chat_turn(
      chat_id="chat-1", title="t", content="c", provider="codex",
    )

  assert state.created_broadcasts == ["chat-1"]
  assert state.removed_broadcasts == ["chat-1"]
  assert state.system_events == []
  assert state.discarded == ["chat-1"]


@pytest.mark.asyncio
async def test_programmatic_start_does_not_reclaim_a_scheduled_run(
  monkeypatch,
):
  state = _install_start_fakes(monkeypatch)

  def fail_publish(_event):
    raise RuntimeError("system broadcast unavailable")

  monkeypatch.setattr(
    chat_start,
    "get_system_broadcast",
    lambda: SimpleNamespace(publish=fail_publish),
  )

  with pytest.raises(RuntimeError, match="system broadcast unavailable"):
    await chat_start.start_programmatic_chat_turn(
      chat_id="chat-1", title="t", content="c", provider="codex",
    )

  assert len(state.scheduled) == 1
  assert state.created_broadcasts == ["chat-1"]
  assert state.removed_broadcasts == []
  assert state.discarded == []


@pytest.mark.asyncio
async def test_programmatic_start_releases_the_claim_when_cancelled(monkeypatch):
  """A cancellation at the ack must not wedge the chat as 'starting'.

  ``asyncio.CancelledError`` derives from BaseException, so an ``except
  Exception`` cleanup silently skips ``discard_starting`` -- and the registry
  has no TTL, so every later start for that chat returns False until the
  process restarts.  The only suspension point in the boundary is this await,
  which is precisely where a cancelled caller lands.
  """
  state = _install_start_fakes(monkeypatch)

  async def cancelled_await_ack(_ack):
    raise asyncio.CancelledError()

  monkeypatch.setattr(chat_start, "await_ack", cancelled_await_ack)

  with pytest.raises(asyncio.CancelledError):
    await chat_start.start_programmatic_chat_turn(
      chat_id="chat-1", title="t", content="c", provider="claude",
    )

  # The claim is released, and nothing transient was left behind.
  assert state.discarded == ["chat-1"]
  assert state.created_broadcasts == []
  assert state.removed_broadcasts == []
  assert state.scheduled == []
  assert state.system_events == []
