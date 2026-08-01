"""Programmatic chat starts share one durable lifecycle protocol."""

from types import SimpleNamespace

import pytest

from app import chat_start
from app.chat_writer import StartTurn


class _Writer:
  def __init__(self):
    self.commands = []

  def submit(self, command):
    self.commands.append(command)
    return "ack"


def _install_start_fakes(monkeypatch, *, generations=(7, 7)):
  writer = _Writer()
  generation_values = iter(generations)
  discarded = []
  created_broadcasts = []
  removed_broadcasts = []
  system_events = []
  scheduled = []

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
    return {
      "history": ["prepared-message"],
      "session_id": "session-1",
      "provider": "claude",
    }

  async def fake_run_chat(*_args, **_kwargs):
    return None

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
  )

  assert started is True
  assert len(state.writer.commands) == 1
  command = state.writer.commands[0]
  assert isinstance(command, StartTurn)
  assert command.chat_id == "chat-1"
  assert command.run_token == "run-1"
  assert command.title_source == "Resolve conflict"
  assert command.default_provider == "claude"
  assert command.user_msg == {
    "role": "user", "content": "Fix the files", "ts": 123456,
  }
  assert state.created_broadcasts == ["chat-1"]
  assert state.removed_broadcasts == []
  assert len(state.scheduled) == 1
  assert state.system_events == [
    {"type": "chat_run_started", "chatId": "chat-1"}
  ]
  assert state.discarded == []


@pytest.mark.asyncio
async def test_programmatic_start_does_nothing_when_claim_is_busy(monkeypatch):
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
