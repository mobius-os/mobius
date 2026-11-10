"""The storage guard pauses live turns through the restart drain's core.

A turn paused because the data volume is filling must keep everything a
restart pause keeps (partials, the pending queue, the exact run) but park as a
*storage* wait that the continuation sweep resumes once admission succeeds,
without closing the restart drain gate and without touching spared chats.
"""

import asyncio

from app import chat as chat_mod
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
