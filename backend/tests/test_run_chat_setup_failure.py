"""A setup-time crash in the detached turn task must surface, not spin.

The 2026-08-04 outage: an exception raised before the agent process started
died inside the fire-and-forget task — the run row stayed 'running' forever
and the owner saw an eternal spinner. run_chat now publishes the terminal
failure and durably fails the run.
"""

import asyncio
from concurrent.futures import Future

import pytest

from app import chat as chat_mod
from app.agent_admission import AgentTurnDeferred
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
  admitted = False

  async def admit(_data_dir):
    nonlocal admitted
    admitted = True

  async def cancelled_impl(*_args, **_kwargs):
    raise asyncio.CancelledError()

  monkeypatch.setattr(chat_mod, "require_agent_turn_admission", admit)
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
  assert admitted is True


@pytest.mark.asyncio
async def test_disk_admission_deferral_persists_resumable_pause(
  chat, monkeypatch, caplog,
):
  """The incident case: a disk-pressure deferral must leave a DURABLE, RESUMABLE
  pause card (so a reopen shows the reason + Resume affordance), not a transient
  broadcast that reloads as a saved user message with an empty assistant reply."""
  async def defer(_data_dir):
    raise AgentTurnDeferred(
      "This turn is waiting for storage headroom because only 512 MiB remains.",
      resource="storage",
    )

  async def must_not_start(*_args, **_kwargs):
    raise AssertionError("provider turn started despite failed admission")

  monkeypatch.setattr(chat_mod, "require_agent_turn_admission", defer)
  monkeypatch.setattr(chat_mod, "_run_chat_impl", must_not_start)

  submitted = []

  class Writer:
    def submit(self, command):
      submitted.append(command)
      ack = Future()
      ack.set_result(True)
      return ack

  monkeypatch.setattr(chat_mod, "get_writer", lambda: Writer())

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
      run_gen=chat_mod.current_run_generation(chat.id), run_token="tok-disk",
    )
  finally:
    remove_broadcast(chat.id)

  # A durable, resumable pause was persisted for THIS run — not a bare failed
  # finish that would reload as an empty assistant bubble.
  assert len(submitted) == 1
  recovered = submitted[0]
  assert recovered.run_token == "tok-disk"
  block = recovered.interruption_block
  assert "only 512 MiB remains" in block["message"]
  assert block["message"].endswith("Your message is saved.")
  assert block["resumable"] is True
  assert block["pause"]["kind"] == "storage"
  # Recovery persisted, so the failed-finish fallback is NOT taken.
  assert finished == []
  # The live viewer still gets a RESUMABLE error card + terminal done.
  err = next(event for event in events if event.get("type") == "error")
  assert err["resumable"] is True
  assert err["message"].endswith("Your message is saved.")
  assert [event.get("type") for event in events][-1] == "done"
  # One concise breadcrumb, no traceback allocated while disk is constrained.
  deferral_logs = [
    record for record in caplog.records
    if "chat turn deferred before the agent started" in record.message
  ]
  assert len(deferral_logs) == 1
  assert deferral_logs[0].exc_info is None
