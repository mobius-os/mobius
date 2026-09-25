"""Agent scratch stays on the bounded data volume and does not outlive its run.

The container's /tmp is an overlay upperdir: no quota, statvfs reporting host
capacity, and not a tmpfs, so nothing ever clears it. Moving scratch to the
data volume bounds it — but that volume also holds SQLite, so scratch without
a lifecycle would take durable data down with it. These cover both halves.
"""

import asyncio
import os
import threading
import time

import pytest

from app import agent_scratch, config, models
from app.browser_profiles import BrowserSessionScan, BrowserSessionTarget
from app.runner_registry import registry


@pytest.fixture(autouse=True)
def _complete_empty_browser_inventory(monkeypatch):
  monkeypatch.setattr(
    agent_scratch,
    "browser_session_targets_for_chat",
    lambda _chat_id: BrowserSessionScan(frozenset(), True),
  )


def _pin_data_dir(monkeypatch, tmp_path):
  monkeypatch.setattr(
    config, "get_settings", lambda: type("S", (), {"data_dir": str(tmp_path)})()
  )
  return tmp_path


def test_scratch_root_follows_the_configured_volume(monkeypatch, tmp_path):
  """A hardcoded path would satisfy a single-volume assertion, so move the
  volume and require the root to move with it."""
  _pin_data_dir(monkeypatch, tmp_path / "volume-a")
  first = config.agent_scratch_root()
  _pin_data_dir(monkeypatch, tmp_path / "volume-b")
  second = config.agent_scratch_root()

  assert first != second
  assert (tmp_path / "volume-a") in first.parents
  assert (tmp_path / "volume-b") in second.parents


def test_scratch_for_chat_is_usable_before_the_subprocess_starts(
  monkeypatch, tmp_path
):
  """The path is exported straight into a subprocess environment, so it has to
  exist by then rather than on first write."""
  _pin_data_dir(monkeypatch, tmp_path)

  assert agent_scratch.scratch_for_chat("chat-1").is_dir()


def test_scratch_is_isolated_per_chat(monkeypatch, tmp_path):
  """Sweeping is per chat, so two chats must not share one directory."""
  _pin_data_dir(monkeypatch, tmp_path)

  assert agent_scratch.scratch_for_chat("a") != agent_scratch.scratch_for_chat("b")


@pytest.mark.asyncio
async def test_release_if_idle_removes_only_the_finished_chats_scratch(
  monkeypatch, tmp_path, db
):
  """Turn completion should not wait for the broad crash-recovery sweep."""
  _pin_data_dir(monkeypatch, tmp_path)
  finished = agent_scratch.scratch_for_chat("finished-chat")
  sibling = agent_scratch.scratch_for_chat("other-chat")

  assert await agent_scratch.release_if_idle("finished-chat") is not None

  assert not finished.exists()
  assert sibling.is_dir()


@pytest.mark.asyncio
async def test_release_if_idle_preserves_a_newer_running_run(
  monkeypatch, tmp_path, db, chat
):
  """A stale finished event cannot delete scratch adopted by a successor."""
  _pin_data_dir(monkeypatch, tmp_path)
  chat_id = getattr(chat, "id", chat)
  db.add_all([
    models.ChatRun(
      id="run-finished", root_run_id="run-finished", chat_id=chat_id,
      status="completed",
    ),
    models.ChatRun(
      id="run-successor", root_run_id="run-successor", chat_id=chat_id,
      status="running",
    ),
  ])
  db.commit()
  scratch = agent_scratch.scratch_for_chat(chat_id)

  assert await agent_scratch.release_if_idle(chat_id) is None
  assert scratch.is_dir()


@pytest.mark.parametrize("status", ["parked", "resume_pending"])
@pytest.mark.asyncio
async def test_release_if_idle_reclaims_scratch_between_physical_runs(
  monkeypatch, tmp_path, db, chat, status
):
  """A durable continuation recreates scratch when its next process starts."""
  _pin_data_dir(monkeypatch, tmp_path)
  chat_id = getattr(chat, "id", chat)
  db.add(models.ChatRun(
    id=f"run-{status}", root_run_id=f"run-{status}", chat_id=chat_id,
    status=status,
  ))
  db.commit()
  scratch = agent_scratch.scratch_for_chat(chat_id)

  assert await agent_scratch.release_if_idle(chat_id) is not None
  assert not scratch.exists()


@pytest.mark.asyncio
async def test_release_if_idle_preserves_a_starting_run_without_a_row(
  monkeypatch, tmp_path
):
  """Programmatic starts claim the registry before StartTurn reaches SQLite."""
  _pin_data_dir(monkeypatch, tmp_path)
  scratch = agent_scratch.scratch_for_chat("starting-chat")
  assert registry.mark_starting("starting-chat") is True

  assert await agent_scratch.release_if_idle("starting-chat") is None
  assert scratch.is_dir()


@pytest.mark.asyncio
async def test_release_if_idle_requires_a_complete_empty_browser_inventory(
  monkeypatch, tmp_path
):
  _pin_data_dir(monkeypatch, tmp_path)
  scratch = agent_scratch.scratch_for_chat("browser-chat")
  monkeypatch.setattr(
    agent_scratch,
    "browser_session_targets_for_chat",
    lambda _chat_id: BrowserSessionScan(frozenset(), False),
  )

  assert await agent_scratch.release_if_idle("browser-chat") is None
  assert scratch.is_dir()

  monkeypatch.setattr(
    agent_scratch,
    "browser_session_targets_for_chat",
    lambda _chat_id: BrowserSessionScan(frozenset({
      BrowserSessionTarget(session="chat-browser-chat"),
    }), True),
  )
  assert await agent_scratch.release_if_idle("browser-chat") is None
  assert scratch.is_dir()


@pytest.mark.asyncio
async def test_cancelled_reclaim_cannot_delete_a_successors_canonical_scratch(
  monkeypatch, tmp_path
):
  _pin_data_dir(monkeypatch, tmp_path)
  original = agent_scratch.scratch_for_chat("chat-cancel")
  entered = threading.Event()
  finish = threading.Event()
  real_reclaim = agent_scratch._reclaim_detached

  def blocked_reclaim(path):
    entered.set()
    assert finish.wait(timeout=2)
    return real_reclaim(path)

  monkeypatch.setattr(agent_scratch, "_reclaim_detached", blocked_reclaim)
  task = asyncio.create_task(agent_scratch.release_if_idle("chat-cancel"))
  try:
    for _ in range(100):
      if entered.is_set():
        break
      await asyncio.sleep(0.01)
    assert entered.is_set()
    assert not original.exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
      await task
    successor = agent_scratch.scratch_for_chat("chat-cancel")
  finally:
    finish.set()

  for _ in range(100):
    if not any(tmp_path.joinpath("agent-scratch").glob(".chat-cancel.released-*")):
      break
    await asyncio.sleep(0.01)
  assert successor.is_dir()


@pytest.mark.asyncio
async def test_sweep_reclaims_scratch_for_a_chat_with_no_run_in_flight(
  monkeypatch, tmp_path, db
):
  _pin_data_dir(monkeypatch, tmp_path)
  idle = agent_scratch.scratch_for_chat("idle-chat")
  (idle / "leftover.bin").write_bytes(b"x" * 2048)

  result = await agent_scratch.sweep_idle_scratch(
    now=time.time() + agent_scratch.IDLE_RETENTION_SECONDS + 1
  )

  assert not idle.exists()
  assert result["removed"] == 1
  assert result["bytes"] >= 2048


@pytest.mark.asyncio
async def test_sweep_never_deletes_scratch_of_a_run_still_in_fight(
  monkeypatch, tmp_path, db, chat
):
  """Deleting a live run's scratch would corrupt a turn already in progress."""
  _pin_data_dir(monkeypatch, tmp_path)
  chat_id = getattr(chat, "id", chat)
  db.add(
    models.ChatRun(
      id="run-live", root_run_id="run-live", chat_id=chat_id, status="running"
    )
  )
  db.commit()
  live = agent_scratch.scratch_for_chat(chat_id)

  result = await agent_scratch.sweep_idle_scratch(
    now=time.time() + agent_scratch.IDLE_RETENTION_SECONDS + 1
  )

  assert live.is_dir()
  assert result["removed"] == 0


@pytest.mark.asyncio
async def test_crash_recovery_sweep_reclaims_parked_scratch(
  monkeypatch, tmp_path, db, chat
):
  """A missed finish event must not retain scratch until a later resume."""
  _pin_data_dir(monkeypatch, tmp_path)
  chat_id = getattr(chat, "id", chat)
  db.add(models.ChatRun(
    id="run-parked", root_run_id="run-parked", chat_id=chat_id,
    status="parked",
  ))
  db.commit()
  scratch = agent_scratch.scratch_for_chat(chat_id)

  result = await agent_scratch.sweep_idle_scratch(
    now=time.time() + agent_scratch.IDLE_RETENTION_SECONDS + 1,
  )

  assert not scratch.exists()
  assert result["removed"] == 1


@pytest.mark.asyncio
async def test_sweep_spares_scratch_created_before_its_run_row_exists(
  monkeypatch, tmp_path, db
):
  """Scratch and the ChatRun row are created around the same moment and are not
  ordered against each other, so a run starting now must not delete the scratch
  of one that has not registered yet."""
  _pin_data_dir(monkeypatch, tmp_path)
  starting = agent_scratch.scratch_for_chat("just-started")

  result = await agent_scratch.sweep_idle_scratch()

  assert starting.is_dir()
  assert result["kept_recent"] == 1
  assert result["removed"] == 0


@pytest.mark.asyncio
async def test_sweep_reports_zero_before_any_scratch_exists(
  monkeypatch, tmp_path, db
):
  """First run on a fresh volume: absence is an ordinary result, not an error."""
  _pin_data_dir(monkeypatch, tmp_path / "never-written")

  assert (await agent_scratch.sweep_idle_scratch())["removed"] == 0


@pytest.mark.asyncio
async def test_scratch_outlives_its_turns_until_a_day_without_changes(
  monkeypatch, tmp_path, db
):
  """A turn that ends on an owner question comes back to its prepared files."""
  _pin_data_dir(monkeypatch, tmp_path)
  paused = agent_scratch.scratch_for_chat("paused-chat")
  prepared = paused / "pr" / "body.md"
  prepared.parent.mkdir()
  prepared.write_text("prepared before asking the owner")
  hour_ago = time.time() - 3600
  os.utime(paused, (hour_ago, hour_ago))  # only a nested file changed since

  result = await agent_scratch.sweep_idle_scratch()

  assert prepared.read_text() == "prepared before asking the owner"
  assert result["removed"] == 0
  result = await agent_scratch.sweep_idle_scratch(
    now=time.time() + agent_scratch.IDLE_RETENTION_SECONDS + 1,
  )
  assert not paused.exists()


@pytest.mark.asyncio
async def test_sweep_reclaims_old_loose_files_in_the_scratch_root(
  monkeypatch, tmp_path, db
):
  _pin_data_dir(monkeypatch, tmp_path)
  root = agent_scratch.scratch_for_chat("any-chat").parent
  old = root / "old-preview.png"
  old.write_bytes(b"x" * 512)
  stale = time.time() - agent_scratch.IDLE_RETENTION_SECONDS - 60
  os.utime(old, (stale, stale))
  fresh = root / "fresh.log"
  fresh.write_text("in use")

  result = await agent_scratch.sweep_idle_scratch()

  assert not old.exists()
  assert fresh.exists()
  assert result["bytes"] >= 512


@pytest.mark.asyncio
async def test_disk_pressure_admission_sweeps_with_the_start_race_grace(
  monkeypatch, tmp_path
):
  from app import agent_admission

  seen = []

  async def sweep(**kwargs):
    seen.append(kwargs)
    return {"removed": 0, "bytes": 0, "kept_recent": 0}

  monkeypatch.setattr(agent_scratch, "sweep_idle_scratch", sweep)
  full = {
    "facts": {"disk": {"free_bytes": 1}},
    "pressure": {"disk": {"state": "critical", "critical_below_bytes": 2}},
  }
  statuses = iter([full, {}])
  await agent_admission.require_agent_turn_admission(
    tmp_path, status_reader=lambda _d: next(statuses),
  )
  assert seen == [{"idle_seconds": agent_scratch.START_RACE_GRACE_SECONDS}]
