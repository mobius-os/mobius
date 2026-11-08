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


def test_a_chat_already_handed_to_another_interrupter_is_left_to_it():
  _, _, handle = _live_turn("storage-already-handed")
  chat_mod._park_handoff_chats.add("storage-already-handed")

  assert _pause() == 0
  assert handle.stop_calls == 0
  chat_mod._park_handoff_chats.discard("storage-already-handed")
  registry.unregister("storage-already-handed", handle.kind)


def test_unstoppable_turn_is_left_for_the_wedged_run_sweep():
  _, _, _ = _live_turn("storage-stuck")
  registry.unregister("storage-stuck", _Handle("storage-stuck").kind)
  stuck = _Handle("storage-stuck", stops=False)
  registry.register(stuck)

  assert _pause() == 0
  assert _run("storage-stuck")["status"] == "running"
  registry.unregister("storage-stuck", stuck.kind)
