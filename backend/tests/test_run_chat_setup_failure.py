"""A setup-time crash in the detached turn task must surface, not spin.

The 2026-08-04 outage: an exception raised before the agent process started
died inside the fire-and-forget task — the run row stayed 'running' forever
and the owner saw an eternal spinner. run_chat now publishes the terminal
failure and durably fails the run.
"""

import asyncio

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


@pytest.mark.asyncio
async def test_disk_admission_deferral_parks_for_automatic_retry(
  chat, monkeypatch, caplog,
):
  """Disk pressure becomes a truthful automatic wait, not a dead Resume."""
  async def defer(_data_dir):
    raise AgentTurnDeferred(
      "This turn is waiting for storage headroom because only 512 MiB remains.",
      resource="storage",
    )

  async def must_not_start(*_args, **_kwargs):
    raise AssertionError("provider turn started despite failed admission")

  monkeypatch.setattr(chat_mod, "require_agent_turn_admission", defer)
  monkeypatch.setattr(chat_mod, "_run_chat_impl", must_not_start)

  recovered = []

  async def record_recover(
    chat_id, run_token, *, message="", kind=None, resumable=True,
    parked_until=None, park_reason=None,
  ):
    recovered.append({
      "chat_id": chat_id, "run_token": run_token,
      "message": message, "kind": kind, "resumable": resumable,
      "parked_until": parked_until, "park_reason": park_reason,
    })

  monkeypatch.setattr(chat_mod, "_recover_wedged_run_strict", record_recover)

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

  # Recovery persists the pause instead of finishing with an empty reply.
  assert len(recovered) == 1
  assert recovered[0]["run_token"] == "tok-disk"
  assert "only 512 MiB remains" in recovered[0]["message"]
  assert recovered[0]["message"].endswith("Your message is saved.")
  assert recovered[0]["resumable"] is False
  assert recovered[0]["kind"] == "storage"
  assert recovered[0]["park_reason"] == "storage"
  assert recovered[0]["parked_until"] is not None
  # Recovery persisted, so the failed-finish fallback is not taken.
  assert finished == []
  # The live viewer gets the same automatic resource wait and terminal event.
  err = next(event for event in events if event.get("type") == "error")
  assert err["pause"]["kind"] == "storage"
  assert "resumable" not in err
  assert err["message"].endswith("Your message is saved.")
  assert [event.get("type") for event in events][-1] == "done"
  # One concise breadcrumb, no traceback allocated while disk is constrained.
  deferral_logs = [
    record for record in caplog.records
    if "chat turn deferred before the agent started" in record.message
  ]
  assert len(deferral_logs) == 1
  assert deferral_logs[0].exc_info is None


@pytest.mark.asyncio
async def test_disk_admission_recovery_failure_falls_back_to_failed_run(
  chat, monkeypatch,
):
  async def defer(_data_dir):
    raise AgentTurnDeferred("Storage headroom is still constrained.", resource="storage")

  async def fail_recovery(*_args, **_kwargs):
    raise RuntimeError("writer unavailable")

  finished = []

  async def record_finish(chat_id, run_token="", terminal_status="completed"):
    finished.append((chat_id, run_token, terminal_status))

  monkeypatch.setattr(chat_mod, "require_agent_turn_admission", defer)
  monkeypatch.setattr(chat_mod, "_recover_wedged_run_strict", fail_recovery)
  monkeypatch.setattr(chat_mod, "_finish_run_strict", record_finish)
  bc = create_broadcast(chat.id)
  try:
    await chat_mod.run_chat(
      [], chat_id=chat.id, session_id=None, provider_id="codex",
      run_gen=chat_mod.current_run_generation(chat.id), run_token="tok-fallback",
    )
  finally:
    remove_broadcast(chat.id)

  assert finished == [(chat.id, "tok-fallback", "failed")]


@pytest.mark.asyncio
async def test_stale_disk_deferral_does_not_close_successor_broadcast(
  chat, monkeypatch,
):
  start_gen = chat_mod.current_run_generation(chat.id)

  async def defer_after_successor_started(_data_dir):
    chat_mod.bump_run_generation(chat.id)
    raise AgentTurnDeferred(
      "Storage headroom is still constrained.", resource="storage",
    )

  async def recover_stale_run(*_args, **_kwargs):
    return None

  finished = []
  monkeypatch.setattr(
    chat_mod, "require_agent_turn_admission", defer_after_successor_started,
  )
  monkeypatch.setattr(
    chat_mod, "_recover_wedged_run_strict", recover_stale_run,
  )
  monkeypatch.setattr(
    chat_mod, "_publish_chat_run_finished", finished.append,
  )
  bc = create_broadcast(chat.id)
  events = []
  monkeypatch.setattr(bc, "publish", events.append)
  try:
    await chat_mod.run_chat(
      [], chat_id=chat.id, session_id=None, provider_id="codex",
      run_gen=start_gen, run_token="tok-stale",
    )
  finally:
    remove_broadcast(chat.id)

  assert events == []
  assert finished == []
  assert bc.running is True
  assert bc.completed_at is None
