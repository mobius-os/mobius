"""A setup-time crash in the detached turn task must surface, not spin.

The 2026-08-04 outage: an exception raised before the agent process started
died inside the fire-and-forget task — the run row stayed 'running' forever
and the owner saw an eternal spinner. run_chat now saves the failure as a
readable, resumable error in the transcript and closes the run.
"""

import asyncio
from concurrent.futures import Future

import pytest

from app import chat as chat_mod
from app.agent_admission import AgentTurnDeferred
from app.broadcast import create_broadcast, remove_broadcast


class _Writer:
  def __init__(self, *, fail: bool = False, on_submit=None):
    self.submitted = []
    self.fail = fail
    self.on_submit = on_submit

  def submit(self, command):
    self.submitted.append(command)
    if self.on_submit is not None:
      self.on_submit()
    ack = Future()
    if self.fail:
      ack.set_exception(RuntimeError("writer unavailable"))
    else:
      ack.set_result(True)
    return ack


async def _run_broken_setup(chat, monkeypatch, writer, *, error=None):
  async def admitted(_data_dir):
    pass

  setup_started = False

  async def broken_impl(*_args, **_kwargs):
    nonlocal setup_started
    setup_started = True
    raise error or AttributeError("'Chat' object has no attribute 'messages'")

  # Exercise setup recovery, not the host's storage/memory admission policy.
  monkeypatch.setattr(chat_mod, "require_agent_turn_admission", admitted)
  monkeypatch.setattr(chat_mod, "_run_chat_impl", broken_impl)
  monkeypatch.setattr(chat_mod, "get_writer", lambda: writer)
  finished = []

  async def record_finish(chat_id, run_token="", terminal_status="completed"):
    finished.append((chat_id, run_token, terminal_status))

  monkeypatch.setattr(chat_mod, "_finish_run_strict", record_finish)
  bc = create_broadcast(chat.id)
  events = []
  real_publish = bc.publish

  def recording_publish(event):
    assert writer.submitted, "terminal events must follow durable recovery"
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
  assert setup_started, "injected setup failure must be reached"
  return events, finished


@pytest.mark.asyncio
async def test_setup_exception_leaves_a_readable_error_in_the_transcript(chat, monkeypatch):
  """The live error alone flashed past and reloaded as an unanswered message;
  the owner could not read or report the cause. It must be saved, readable,
  and resumable, and the run must be closed rather than left spinning."""
  writer = _Writer()
  events, finished = await _run_broken_setup(chat, monkeypatch, writer)

  kinds = [event.get("type") for event in events]
  assert "error" in kinds, kinds
  assert kinds[-1] == "done"
  assert len(writer.submitted) == 1
  recovered = writer.submitted[0]
  assert isinstance(recovered, chat_mod.RecoverWedgedRun)
  assert recovered.run_token == "tok-1"
  assert recovered.terminal_status == "failed"
  assert recovered.parked_until is None
  block = recovered.interruption_block
  assert "AttributeError" in block["message"]
  assert "has no attribute 'messages'" not in block["message"]
  assert block["resumable"] is True
  assert "pause" not in block
  live = next(e for e in events if e.get("type") == "error")
  assert live["message"] == block["message"]
  assert finished == []


@pytest.mark.asyncio
async def test_failed_helper_setup_wakes_parent_with_settled_result(db, monkeypatch):
  """The owning setup path, not a standalone writer command, delivers the
  failed child's durable result to its waiting parent."""
  from app import chat_start, delegations, models
  from app.chat_writer import get_writer
  from tests.goal_fixtures import goal_run
  from tests.test_delegations import _seed_delegation

  parent_id, child_id, delegation_id = _seed_delegation(
    db, suffix="setup-failed-delivery", child_status="running",
  )
  root_id = db.get(models.Delegation, delegation_id).parent_root_run_id
  parent = db.get(models.Chat, parent_id)
  parent.agent_settings_json = {"model": "claude-sonnet-4-6"}
  db.add(goal_run(
    db, id=root_id, root_run_id=root_id, chat_id=parent_id,
    status="completed", provider="claude",
  ))
  db.commit()

  async def admitted(_data_dir):
    pass

  setup_started = False

  async def broken_impl(*_args, **_kwargs):
    nonlocal setup_started
    setup_started = True
    raise RuntimeError("private setup detail")

  starts = []

  async def record_start(**kwargs):
    starts.append(kwargs)
    return True

  monkeypatch.setattr(chat_mod, "require_agent_turn_admission", admitted)
  monkeypatch.setattr(chat_mod, "_run_chat_impl", broken_impl)
  monkeypatch.setattr(
    chat_start, "start_programmatic_activity_continuation", record_start,
  )
  # Deliberately keep the real writer, real SQLite row and real wake hook.
  assert get_writer() is not None
  await chat_mod.run_chat(
    [], chat_id=child_id, session_id=None, provider_id="claude",
    run_gen=chat_mod.current_run_generation(child_id),
    run_token="child-run-setup-failed-delivery",
  )
  assert setup_started, "injected helper setup failure must be reached"

  db.expire_all()
  child_run = db.get(models.ChatRun, "child-run-setup-failed-delivery")
  assert child_run.status == "failed"
  assert starts == [{
    "chat_id": parent_id,
    "root_run_id": root_id,
    "run_token": delegations._activity_continuation_run_id(
      db, db.get(models.Delegation, delegation_id),
    ),
    "source_work_id": root_id,
    "activity_id": delegation_id,
    "_transition_lock_held": True,
  }]
  result = delegations.build_delegation_result_context(
    db, parent_id, source_work_id=root_id,
  )
  assert result.delegation_ids == (delegation_id,)
  assert "RuntimeError" in result.text
  assert "private setup detail" not in result.text


@pytest.mark.asyncio
async def test_setup_exception_still_fails_the_run_when_the_error_cannot_be_saved(
  chat, monkeypatch,
):
  events, finished = await _run_broken_setup(chat, monkeypatch, _Writer(fail=True))

  assert [event.get("type") for event in events][-1] == "done"
  assert finished == [(chat.id, "tok-1", "failed")]


@pytest.mark.asyncio
async def test_setup_error_payload_never_enters_transcript_or_live_events(chat, monkeypatch):
  private_payload = "synthetic-credential-and-private-query"
  writer = _Writer()
  events, _ = await _run_broken_setup(
    chat, monkeypatch, writer, error=RuntimeError(private_payload),
  )
  assert "RuntimeError" in writer.submitted[0].interruption_block["message"]
  assert private_payload not in repr(writer.submitted)
  assert private_payload not in repr(events)


@pytest.mark.asyncio
@pytest.mark.parametrize("writer_fails", [False, True])
async def test_setup_failure_does_not_end_a_successor_stream(chat, monkeypatch, writer_fails):
  generation = chat_mod.current_run_generation(chat.id)
  def successor_started():
    monkeypatch.setattr(chat_mod, "current_run_generation", lambda _chat_id: generation + 1)

  published_finishes = []
  monkeypatch.setattr(chat_mod, "_publish_chat_run_finished", published_finishes.append)
  writer = _Writer(fail=writer_fails, on_submit=successor_started)
  events, _ = await _run_broken_setup(chat, monkeypatch, writer)
  assert events == []
  assert published_finishes == []


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
