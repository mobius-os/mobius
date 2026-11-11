"""The storage guard pauses live turns through the restart drain's core.

A turn paused because the data volume is filling must keep everything a
restart pause keeps (partials, the pending queue, the exact run) but park as a
*storage* wait that the continuation sweep resumes once admission succeeds,
without closing the restart drain gate and without touching spared chats.
"""

import asyncio

from app import chat as chat_mod
from app.database import SessionLocal
from app.runner_registry import registry

from tests.test_drain_for_restart import _Handle, _chat, _drain_writer, _live_turn, _run


def _pause(spare=frozenset()):
  return asyncio.run(chat_mod.pause_live_turns_for_storage(spare))


def test_storage_pause_parks_the_exact_run_as_a_storage_wait():
  _, _, handle = _live_turn("storage-pause-1", pending=[
    {"role": "user", "content": "queued follow-up", "ts": 2},
  ])

  paused = _pause()
  _drain_writer()

  assert paused == 1
  assert handle.stop_calls == 1
  run = _run("storage-pause-1")
  assert run["status"] == "parked"
  assert run["park_reason"] == "storage"
  assert run["restart_nonce"] in (None, "")
  state = _chat("storage-pause-1")
  # Partials survive, the calm storage card is appended, the queue is kept.
  blocks = state["messages"][-1]["blocks"]
  assert any(b.get("content") == "partial answer" for b in blocks)
  assert any(b.get("message") == chat_mod.STORAGE_PAUSE_MESSAGE for b in blocks)
  assert [m["content"] for m in state["pending"]] == ["queued follow-up"]


def test_storage_pause_does_not_close_the_restart_drain_gate():
  _live_turn("storage-pause-gate")

  _pause()
  _drain_writer()

  assert chat_mod.draining is False


def test_storage_pause_spares_a_chat_the_owner_continued():
  _, _, continued_handle = _live_turn("storage-continued-chat")
  _, _, other_handle = _live_turn("storage-other-chat")

  paused = _pause(frozenset({"storage-continued-chat"}))
  _drain_writer()

  assert paused == 1
  assert continued_handle.stop_calls == 0
  assert other_handle.stop_calls == 1
  assert _run("storage-continued-chat")["status"] == "running"
  registry.unregister("storage-continued-chat", continued_handle.kind)


def test_restart_drain_owns_every_turn_while_it_runs(monkeypatch):
  _, _, handle = _live_turn("storage-during-restart")
  monkeypatch.setattr(chat_mod, "draining", True)

  assert _pause() == 0
  assert handle.stop_calls == 0
  registry.unregister("storage-during-restart", handle.kind)


def test_a_turn_already_handed_to_another_interrupter_is_left_to_it():
  cid = "storage-already-handed"
  _, sink, handle = _live_turn(cid)
  chat_mod._park_handoffs[cid] = chat_mod._ParkHandoff(
    generation=chat_mod.current_run_generation(cid), run_token=sink.run_token,
  )

  assert _pause() == 0
  assert handle.stop_calls == 0
  chat_mod._park_handoffs.pop(cid, None)
  registry.unregister(cid, handle.kind)


def test_unstoppable_turn_is_left_for_the_wedged_run_sweep():
  _, _, _ = _live_turn("storage-stuck")
  registry.unregister("storage-stuck", _Handle("storage-stuck").kind)
  stuck = _Handle("storage-stuck", stops=False)
  registry.register(stuck)

  assert _pause() == 0
  assert _run("storage-stuck")["status"] == "running"
  registry.unregister("storage-stuck", stuck.kind)


def _stale_handoff(chat_id: str) -> None:
  """A handoff its turn never consumed: that turn ended before the mark."""
  chat_mod._park_handoffs[chat_id] = chat_mod._ParkHandoff(
    generation=chat_mod.current_run_generation(chat_id) - 1,
    run_token=f"ended-{chat_id}",
  )


def test_a_stale_handoff_does_not_shield_a_later_turn_from_the_storage_pause():
  cid = "storage-stale-handoff"
  _, _, handle = _live_turn(cid)
  _stale_handoff(cid)

  assert _pause() == 1
  _drain_writer()

  assert handle.stop_calls == 1
  assert _run(cid)["park_reason"] == "storage"


def test_a_stale_handoff_does_not_suppress_a_later_turns_limit_park():
  cid = "storage-stale-limit"
  _, sink, handle = _live_turn(cid)
  _stale_handoff(cid)

  kwargs = chat_mod._park_exit(sink, {"api_error_status": 429}, None)

  assert kwargs["parked"] is True
  registry.unregister(cid, handle.kind)


def test_a_stale_handoff_is_never_taken_by_a_later_turns_terminal():
  """A later Stop still clears its own run instead of draining for restart."""
  cid = "storage-stale-stop"
  _stale_handoff(cid)
  later_turn = chat_mod.current_run_generation(cid)

  assert chat_mod._take_park_handoff(cid, later_turn) is False
  assert chat_mod._take_park_handoff(cid, later_turn - 1) is True
  assert cid not in chat_mod._park_handoffs


def test_a_turn_that_never_stops_is_interrupted_once_and_kept_as_one_entry():
  """The guard ticks every second; an unstoppable turn's handoff stays one."""
  cid = "storage-stuck-repeat"
  _, _, _ = _live_turn(cid)
  registry.unregister(cid, _Handle(cid).kind)
  stuck = _Handle(cid, stops=False)
  registry.register(stuck)

  for _ in range(5):
    assert _pause() == 0

  assert stuck.stop_calls == 1
  assert list(chat_mod._park_handoffs) == [cid]
  registry.unregister(cid, stuck.kind)


# --- an owner's Stop and the storage pause never take a turn from each other -


class _GatedHandle(_Handle):
  """A provider that stops only once the test opens its gate."""

  def __init__(self, chat_id: str):
    super().__init__(chat_id)
    self.stopping = asyncio.Event()
    self.gate = asyncio.Event()

  async def stop(self, timeout: float = 2.0) -> bool:
    self.stopping.set()
    await self.gate.wait()
    return await super().stop(timeout)


def _gated_live_turn(chat_id: str) -> _GatedHandle:
  _, _, plain = _live_turn(chat_id)
  registry.unregister(chat_id, plain.kind)
  handle = _GatedHandle(chat_id)
  registry.register(handle)
  return handle


async def _stop(chat_id: str):
  db = SessionLocal()
  try:
    return await chat_mod.stop_chat_for(chat_id, db=db)
  finally:
    db.close()


def test_storage_pause_leaves_a_turn_the_owners_stop_is_still_ending():
  """Stop has recorded the turn as stopped and waits for its provider; a
  storage tick in that window must not re-record and park it, or the work the
  owner stopped resumes by itself once space returns."""
  cid = "storage-vs-stop-first"
  handle = _gated_live_turn(cid)
  stopped_generation = chat_mod.current_run_generation(cid)

  async def scenario():
    stop = asyncio.create_task(_stop(cid))
    await handle.stopping.wait()
    pause = asyncio.create_task(chat_mod.pause_live_turns_for_storage(frozenset()))
    await asyncio.sleep(0.05)
    handle.gate.set()
    return await pause, await stop

  paused, outcome = asyncio.run(scenario())
  _drain_writer()

  assert paused == 0
  assert outcome.stopped is True
  assert handle.stop_calls == 1
  # The dying turn's terminal transition finds Stop's own record and closes
  # its run as stopped; nothing parked it in the meantime.
  assert chat_mod._clear_after_terminal_generation[cid] == stopped_generation
  assert chat_mod._clear_after_terminal_status[cid] == "stopped"
  assert chat_mod.current_run_generation(cid) == stopped_generation + 1
  assert cid not in chat_mod._park_handoffs
  assert _run(cid)["status"] == "running"


def test_storage_pause_never_overwrites_a_stop_whose_provider_did_not_stop():
  """Stop timed out and released the chat, but its turn is still live."""
  cid = "storage-vs-stuck-stop"
  _live_turn(cid)
  registry.unregister(cid, _Handle(cid).kind)
  stuck = _Handle(cid, stops=False)
  registry.register(stuck)
  stopped_generation = chat_mod.current_run_generation(cid)

  assert asyncio.run(_stop(cid)).stopped is False
  assert _pause() == 0

  assert stuck.stop_calls == 1
  assert chat_mod._clear_after_terminal_generation[cid] == stopped_generation
  assert chat_mod._clear_after_terminal_status[cid] == "stopped"
  assert cid not in chat_mod._park_handoffs
  registry.unregister(cid, stuck.kind)


def test_owner_stop_during_a_storage_pause_ends_the_parked_run():
  """A Stop pressed while the pause is interrupting the turn waits for it,
  then ends the parked storage wait instead of leaving it to resume."""
  cid = "storage-vs-stop-second"
  handle = _gated_live_turn(cid)

  async def scenario():
    pause = asyncio.create_task(chat_mod.pause_live_turns_for_storage(frozenset()))
    await handle.stopping.wait()
    stop = asyncio.create_task(_stop(cid))
    await asyncio.sleep(0.05)
    handle.gate.set()
    return await pause, await stop

  paused, outcome = asyncio.run(scenario())
  _drain_writer()

  assert paused == 1
  assert outcome.stopped is True
  assert outcome.receipt is not None
  assert _run(cid)["status"] == "stopped"


def test_a_turn_without_a_run_token_is_never_matched_to_a_handoff():
  cid = "storage-tokenless-handoff"
  chat_mod._park_handoffs[cid] = chat_mod._ParkHandoff(
    generation=chat_mod.current_run_generation(cid), run_token=None,
  )

  assert chat_mod._handoff_owns_run(cid, None) is False
  assert chat_mod._handoff_owns_run(cid, "") is False
  chat_mod._park_handoffs.pop(cid, None)
