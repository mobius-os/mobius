"""A setup-time crash in the detached turn task must surface, not spin.

The 2026-08-04 outage: an exception raised before the agent process started
died inside the fire-and-forget task — the run row stayed 'running' forever
and the owner saw an eternal spinner. run_chat now publishes the terminal
failure and durably fails the run.
"""

import asyncio

import pytest

from app import chat as chat_mod
from app.broadcast import create_broadcast, remove_broadcast


@pytest.mark.asyncio
async def test_setup_exception_publishes_error_and_fails_run(chat, monkeypatch):
  async def broken_impl(*_args, **_kwargs):
    raise RuntimeError("no such column: apps.connections_manage")

  monkeypatch.setattr(chat_mod, "_run_chat_impl", broken_impl)

  finished = []

  async def record_finish(chat_id, run_token="", terminal_status="completed"):
    finished.append((chat_id, run_token, terminal_status))

  monkeypatch.setattr(chat_mod, "_finish_run_strict", record_finish)

  bc = create_broadcast(chat.id)
  events = []
  real_publish = bc.publish

  def recording_publish(event):
    events.append(event)
    return real_publish(event)

  monkeypatch.setattr(bc, "publish", recording_publish)
  try:
    await chat_mod.run_chat(
      [], chat_id=chat.id, session_id=None, provider_id="codex",
      run_gen=chat_mod.current_run_generation(chat.id), run_token="tok-1",
    )
  finally:
    remove_broadcast(chat.id)

  kinds = [event.get("type") for event in events]
  assert "error" in kinds, kinds
  assert kinds[-1] == "done"
  message = next(e["message"] for e in events if e.get("type") == "error")
  assert "RuntimeError" in message
  assert finished == [(chat.id, "tok-1", "failed")]


@pytest.mark.asyncio
async def test_setup_cancellation_still_propagates(chat, monkeypatch):
  async def cancelled_impl(*_args, **_kwargs):
    raise asyncio.CancelledError()

  monkeypatch.setattr(chat_mod, "_run_chat_impl", cancelled_impl)
  create_broadcast(chat.id)
  try:
    with pytest.raises(asyncio.CancelledError):
      await chat_mod.run_chat(
        [], chat_id=chat.id, session_id=None, provider_id="codex",
        run_gen=chat_mod.current_run_generation(chat.id), run_token="tok-2",
      )
  finally:
    remove_broadcast(chat.id)
