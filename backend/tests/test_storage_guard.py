"""The storage guard keeps a filling data volume recoverable.

Each test drives the guard over a simulated volume and records what it did,
so the escalation order (reclaim, stop writers, pause turns, release the
reserve) and its one-notice-per-episode behaviour are pinned without touching
real processes or the real disk.
"""

from types import SimpleNamespace

import pytest

from app import disk_reserve, resource_pressure, storage_guard

MIB = 1024 * 1024
TOTAL = 8 * 1024 * MIB
# For an 8 GiB volume: constrained < 819 MiB, critical < 409 MiB,
# exhausted < 61 MiB; the reserve is 245 MiB.
CONSTRAINED = 600 * MIB
CRITICAL = 300 * MIB
EXHAUSTED = 30 * MIB
COMFORTABLE = 2048 * MIB


class Volume:
  def __init__(self, free):
    self.free = free

  def __call__(self, _path):
    return SimpleNamespace(total=TOTAL, used=TOTAL - self.free, free=self.free)


class Recorder:
  def __init__(
    self, volume, *, reclaim_frees=0, reserve=245 * MIB, release_frees=False,
  ):
    self.volume = volume
    self.reclaim_frees = reclaim_frees
    self.reserve = reserve
    self.release_frees = release_frees
    self.calls: list[tuple] = []

  async def reclaim(self, _data_dir, tier):
    self.calls.append(("reclaim", tier))
    self.volume.free += self.reclaim_frees
    return self.reclaim_frees

  def stop(self, spare_markers):
    self.calls.append(("stop", spare_markers))
    return 2

  async def pause(self, spare_chats):
    self.calls.append(("pause", spare_chats))
    return 1

  async def notify(self, title, body):
    self.calls.append(("notify", title, body))

  def arm(self, data_dir, *, target_bytes, disk_usage):
    self.calls.append(("arm", target_bytes))
    return disk_reserve.ArmOutcome("armed", target_bytes)

  def release(self, _data_dir):
    self.calls.append(("release",))
    released, self.reserve = self.reserve, 0
    if self.release_frees:
      self.volume.free += released
    return released

  def held(self, _data_dir):
    return self.reserve

  def names(self):
    return [call[0] for call in self.calls]


@pytest.fixture
def no_continued_turns(monkeypatch):
  monkeypatch.setattr(storage_guard, "_continued", {})


def _guard(tmp_path, volume, recorder, clock=lambda: 0.0):
  effects = storage_guard.GuardEffects(
    disk_usage=volume,
    reclaim=recorder.reclaim,
    stop_agent_commands=recorder.stop,
    pause_live_turns=recorder.pause,
    notify=recorder.notify,
    arm_reserve=recorder.arm,
    release_reserve=recorder.release,
    reserve_held=recorder.held,
  )
  return storage_guard.StorageGuard(tmp_path, effects=effects, clock=clock)


@pytest.mark.asyncio
async def test_healthy_volume_arms_the_reserve_and_touches_nothing_else(tmp_path, no_continued_turns):
  volume = Volume(COMFORTABLE)
  recorder = Recorder(volume)

  outcome = await _guard(tmp_path, volume, recorder).tick()

  assert outcome.level == "normal"
  assert recorder.names() == ["arm"]


@pytest.mark.asyncio
async def test_reserve_is_not_armed_when_it_would_push_the_volume_into_pressure(
  tmp_path, no_continued_turns,
):
  # Free is above the warning line, but not by a whole reserve.
  volume = Volume(900 * MIB)
  recorder = Recorder(volume)

  await _guard(tmp_path, volume, recorder).tick()

  assert recorder.names() == []


def _failing_arm(state, attempts):
  def arm(data_dir, *, target_bytes, disk_usage):
    attempts.append(target_bytes)
    return disk_reserve.ArmOutcome(state, 0, "simulated")
  return arm


@pytest.mark.asyncio
async def test_failed_arm_is_not_retried_every_tick(tmp_path, no_continued_turns):
  volume = Volume(COMFORTABLE)
  attempts = []
  now = [0.0]
  guard = _guard(tmp_path, volume, Recorder(volume), clock=lambda: now[0])
  guard.effects.arm_reserve = _failing_arm("no_room", attempts)

  await guard.tick()
  await guard.tick()
  now[0] += storage_guard.ARM_RETRY_SECS + 1
  await guard.tick()

  assert len(attempts) == 2
  assert guard.reserve_state == "released"


@pytest.mark.asyncio
async def test_a_filesystem_that_cannot_hold_a_reserve_is_not_rewritten_again(
  tmp_path, no_continued_turns,
):
  """Each attempt may write the whole reserve; the filesystem will not change."""
  volume = Volume(COMFORTABLE)
  attempts = []
  now = [0.0]
  guard = _guard(tmp_path, volume, Recorder(volume), clock=lambda: now[0])
  guard.effects.arm_reserve = _failing_arm("unsupported", attempts)

  await guard.tick()
  for _ in range(3):
    now[0] += storage_guard.ARM_RETRY_SECS + 1
    await guard.tick()

  assert len(attempts) == 1
  assert guard.reserve_state == "unavailable"
  assert storage_guard.reserve_report(tmp_path)["state"] in ("released", "unavailable")


@pytest.mark.asyncio
async def test_warning_line_reclaims_disposable_data_once_per_episode(tmp_path, no_continued_turns):
  volume = Volume(CONSTRAINED)
  recorder = Recorder(volume)
  guard = _guard(tmp_path, volume, recorder)

  await guard.tick()
  await guard.tick()

  assert recorder.names() == ["reclaim"]


@pytest.mark.asyncio
async def test_deeper_pressure_reclaims_again_with_the_emergency_sweep(
  tmp_path, no_continued_turns,
):
  volume = Volume(CONSTRAINED)
  recorder = Recorder(volume)
  guard = _guard(tmp_path, volume, recorder)

  await guard.tick()
  volume.free = CRITICAL
  await guard.tick()
  await guard.tick()
  volume.free = CONSTRAINED
  await guard.tick()

  reclaims = [call[1] for call in recorder.calls if call[0] == "reclaim"]
  assert reclaims == ["constrained", "critical"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
  ("tier", "idle_seconds"),
  [("constrained", 24 * 60 * 60), ("critical", 15 * 60)],
)
async def test_scratch_keeps_its_day_of_retention_until_storage_is_critical(
  tmp_path, monkeypatch, tier, idle_seconds,
):
  """A chat idle for minutes, such as a parent waiting on its helpers, keeps
  its clones at the warning line; only critical pressure outranks that."""
  from app import agent_scratch, provider_session_retention

  sweeps = []

  async def sweep(*, idle_seconds):
    sweeps.append(idle_seconds)
    return {"bytes": 0}

  monkeypatch.setattr(agent_scratch, "sweep_idle_scratch", sweep)
  monkeypatch.setattr(
    provider_session_retention, "sweep_stale_provider_sessions", lambda _d: {},
  )

  await storage_guard._reclaim_disposable(str(tmp_path), tier)

  assert sweeps == [idle_seconds]


@pytest.mark.asyncio
async def test_reclaim_that_frees_enough_stops_writers_but_pauses_no_turns(
  tmp_path, no_continued_turns,
):
  volume = Volume(CRITICAL)
  recorder = Recorder(volume, reclaim_frees=400 * MIB)

  outcome = await _guard(tmp_path, volume, recorder).tick()

  assert outcome.level == "constrained"
  # Writers go first, even when reclaim turns out to be enough.
  assert recorder.names() == ["stop", "reclaim"]


@pytest.mark.asyncio
async def test_critical_volume_stops_writers_before_anything_slower(tmp_path, no_continued_turns):
  volume = Volume(CRITICAL)
  recorder = Recorder(volume)

  outcome = await _guard(tmp_path, volume, recorder).tick()

  assert outcome.level == "critical"
  # Reclaim and a graceful pause both take seconds; writers stop first.
  assert recorder.names() == ["stop", "reclaim", "pause", "notify"]
  assert outcome.commands_stopped == 2 and outcome.turns_paused == 1
  # Nothing is released while there is still headroom above the last line.
  assert "release" not in recorder.names()


@pytest.mark.asyncio
async def test_critical_volume_spares_a_turn_the_owner_continued_anyway(
  tmp_path, monkeypatch,
):
  from app.process_groups import run_marker

  monkeypatch.setattr(storage_guard, "_continued", {})
  monkeypatch.setattr(storage_guard, "_is_live_run", lambda chat_id, run_token: True)
  storage_guard.spare_continued_turn("continued-chat", "continued-run")
  volume = Volume(CRITICAL)
  recorder = Recorder(volume)

  await _guard(tmp_path, volume, recorder).tick()

  stop = next(call for call in recorder.calls if call[0] == "stop")
  pause = next(call for call in recorder.calls if call[0] == "pause")
  assert stop[1] == frozenset({run_marker("continued-run")})
  assert pause[1] == frozenset({"continued-chat"})


@pytest.mark.asyncio
async def test_exhausted_volume_stops_continued_commands_then_releases_the_reserve(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(storage_guard, "_continued", {})
  monkeypatch.setattr(storage_guard, "_is_live_run", lambda chat_id, run_token: True)
  storage_guard.spare_continued_turn("continued-chat", "continued-run")
  volume = Volume(EXHAUSTED)
  recorder = Recorder(volume)

  outcome = await _guard(tmp_path, volume, recorder).tick()

  assert outcome.level == "exhausted"
  # Writers, the continued turn's included, are stopped before the reserve
  # is freed, so they cannot take it;
  # the reserve is freed before turns are paused, so the pauses can commit.
  assert recorder.names() == ["stop", "release", "reclaim", "pause", "notify"]
  stop = next(call for call in recorder.calls if call[0] == "stop")
  pause = next(call for call in recorder.calls if call[0] == "pause")
  assert stop[1] == frozenset()
  # The continued turn itself keeps running: pausing it would undo the
  # owner's explicit choice and make them start over.
  assert pause[1] == frozenset({"continued-chat"})
  assert outcome.reserve_released_bytes == 245 * MIB


def _continued_guard(tmp_path, monkeypatch, volume, recorder):
  monkeypatch.setattr(storage_guard, "_continued", {})
  monkeypatch.setattr(storage_guard, "_is_live_run", lambda chat_id, run_token: True)
  storage_guard.spare_continued_turn("continued-chat", "continued-run")
  return _guard(tmp_path, volume, recorder)


def _stop_spares(recorder):
  return [call[1] for call in recorder.calls if call[0] == "stop"]


@pytest.mark.asyncio
async def test_exhausted_volume_without_a_reserve_spares_the_owners_cleanup(
  tmp_path, monkeypatch,
):
  """With nothing left to release, stopping the continued turn's commands
  buys nothing while its cleanup is freeing space; Continue anyway must still
  be able to free space at exactly this level."""
  from app.process_groups import run_marker

  volume = Volume(EXHAUSTED)
  recorder = Recorder(volume, reserve=0)
  guard = _continued_guard(tmp_path, monkeypatch, volume, recorder)

  for _ in range(5):
    await guard.tick()
    volume.free += 1 * MIB

  spared = frozenset({run_marker("continued-run")})
  assert _stop_spares(recorder) == [spared] * 5
  assert "release" in recorder.names()


@pytest.mark.asyncio
async def test_exhausted_volume_stops_continued_commands_while_space_still_falls(
  tmp_path, monkeypatch,
):
  from app.process_groups import run_marker

  volume = Volume(EXHAUSTED)
  recorder = Recorder(volume, reserve=0)
  guard = _continued_guard(tmp_path, monkeypatch, volume, recorder)
  spared = frozenset({run_marker("continued-run")})

  await guard.tick()
  # The turn's own transcript writes a little every tick; that is not a fill.
  volume.free -= 256 * 1024
  await guard.tick()
  volume.free -= 8 * MIB
  await guard.tick()
  await guard.tick()

  assert _stop_spares(recorder) == [spared, spared, frozenset(), spared]


@pytest.mark.asyncio
async def test_released_reserve_being_eaten_counts_as_space_still_falling(
  tmp_path, monkeypatch,
):
  """The next tick compares against the space the release left, so a writer
  taking the freed reserve is caught even though free space is still above
  where the release found it."""
  from app.process_groups import run_marker

  volume = Volume(10 * MIB)
  recorder = Recorder(volume, reserve=16 * MIB, release_frees=True)
  guard = _continued_guard(tmp_path, monkeypatch, volume, recorder)
  spared = frozenset({run_marker("continued-run")})

  await guard.tick()
  volume.free -= 8 * MIB
  await guard.tick()
  await guard.tick()

  assert _stop_spares(recorder) == [frozenset(), frozenset(), spared]


@pytest.mark.asyncio
async def test_owner_hears_once_per_level_and_again_after_recovery(tmp_path, no_continued_turns):
  volume = Volume(CRITICAL)
  recorder = Recorder(volume)
  guard = _guard(tmp_path, volume, recorder)

  await guard.tick()
  await guard.tick()
  volume.free = EXHAUSTED
  await guard.tick()
  await guard.tick()
  volume.free = COMFORTABLE
  await guard.tick()
  volume.free = CRITICAL
  await guard.tick()

  notices = [call[1] for call in recorder.calls if call[0] == "notify"]
  assert notices == [
    "Möbius paused work to keep storage from filling",
    "Möbius storage is full",
    "Möbius paused work to keep storage from filling",
  ]


@pytest.mark.asyncio
async def test_guard_looks_every_second_under_pressure(tmp_path, no_continued_turns):
  volume = Volume(COMFORTABLE)
  recorder = Recorder(volume)
  guard = _guard(tmp_path, volume, recorder)

  await guard.tick()
  assert guard.interval() == storage_guard.NORMAL_TICK_SECS
  volume.free = CONSTRAINED
  await guard.tick()
  assert guard.interval() == storage_guard.PRESSURE_TICK_SECS


def test_startup_releases_the_reserve_only_on_an_exhausted_volume(tmp_path):
  """A critical volume, which can stay critical for days, keeps its reserve
  through restarts; the boot releases it at the running guard's own line."""
  disk_reserve.arm(
    tmp_path, target_bytes=1 * MIB, disk_usage=Volume(COMFORTABLE),
  )

  for free in (COMFORTABLE, CRITICAL):
    assert storage_guard.release_reserve_at_boot(tmp_path, disk_usage=Volume(free)) == 0
    assert disk_reserve.held_bytes(tmp_path) > 0

  assert storage_guard.release_reserve_at_boot(
    tmp_path, disk_usage=Volume(EXHAUSTED),
  ) > 0
  assert disk_reserve.held_bytes(tmp_path) == 0


def test_the_storage_guard_reads_no_cgroup_facts(tmp_path, monkeypatch):
  """It looks every second; only the disk is its business."""
  def unexpected(*_args, **_kwargs):
    raise AssertionError("the storage guard read a cgroup fact")

  monkeypatch.setattr(resource_pressure, "cgroup_memory_snapshot", unexpected)
  monkeypatch.setattr(resource_pressure, "cgroup_tasks_snapshot", unexpected)
  monkeypatch.setattr(resource_pressure, "resource_facts", unexpected)

  assert storage_guard.StorageGuard(
    tmp_path, effects=storage_guard.GuardEffects(disk_usage=Volume(CRITICAL)),
  )._measure()[0] == "critical"
  assert resource_pressure.disk_is_critical(tmp_path, disk_usage=Volume(CRITICAL))
  assert storage_guard.release_reserve_at_boot(tmp_path, disk_usage=Volume(EXHAUSTED)) == 0



def test_container_start_releases_the_reserve_before_any_boot_write():
  """On a full volume the image's boot transaction cannot commit and the
  container refuses to start; the entrypoint frees the reserve first, using
  the server's own check so any other volume keeps it."""
  from pathlib import Path

  entrypoint = (
    Path(__file__).resolve().parents[1] / "scripts" / "entrypoint.sh"
  ).read_text()
  release = entrypoint.index("release_reserve_at_boot(")
  # When the check itself cannot run, the reserve is released anyway.
  fallback = entrypoint.index(f"rm -f {disk_reserve.reserve_path('/data')}\n")
  assert release < fallback < entrypoint.index("begin-boot")
  assert release < entrypoint.index("managed-cutover-ready")
  assert release < entrypoint.index("_platform_serve_checkout() {")
  assert release < entrypoint.index("_platform_boot activate")


@pytest.mark.asyncio
async def test_jumping_straight_to_full_sends_one_notice_not_two(tmp_path, no_continued_turns):
  """Releasing the reserve lifts an exhausted volume back to critical; the
  owner already has the full-storage notice and must not get a second push."""
  volume = Volume(EXHAUSTED)
  recorder = Recorder(volume)
  guard = _guard(tmp_path, volume, recorder)

  await guard.tick()
  volume.free = CRITICAL
  await guard.tick()

  notices = [call for call in recorder.calls if call[0] == "notify"]
  assert [call[1] for call in notices] == ["Möbius storage is full"]
  # Decimal megabytes, as the owner reads them.
  assert f"({EXHAUSTED // 1_000_000} MB free)" in notices[0][2]


def test_continued_turns_forget_chats_that_went_idle(monkeypatch):
  alive = {"chat-a"}
  monkeypatch.setattr(storage_guard, "_continued", {})
  monkeypatch.setattr(
    storage_guard, "_is_live_run", lambda chat_id, run_token: chat_id in alive,
  )
  storage_guard.spare_continued_turn("chat-a", "run-a")
  storage_guard.spare_continued_turn("chat-b", "run-b")

  assert storage_guard.continued_turns() == {"chat-a": "run-a"}


def test_continue_anyway_covers_only_the_run_it_admitted(monkeypatch):
  """A later, ordinary turn in the same live chat is paused like any other."""
  from app import chat as chat_mod

  live_sink = SimpleNamespace(run_token="run-a")
  monkeypatch.setattr(storage_guard, "_continued", {})
  monkeypatch.setattr(chat_mod.registry, "is_alive", lambda chat_id: True)
  monkeypatch.setattr(chat_mod, "get_active_sink", lambda chat_id: live_sink)
  storage_guard.spare_continued_turn("chat-a", "run-a")

  assert storage_guard.continued_turns() == {"chat-a": "run-a"}

  live_sink.run_token = "run-b"

  assert storage_guard.continued_turns() == {}


def test_the_test_runtime_starts_no_acting_storage_guard(monkeypatch):
  """Entering the app lifespan in tests arms no reserve and never ticks."""
  import os
  from pathlib import Path

  from fastapi.testclient import TestClient
  from app.main import app

  ticks = []

  async def record_tick(self):
    ticks.append(self)

  monkeypatch.setattr(storage_guard.StorageGuard, "tick", record_tick)
  reserve = disk_reserve.reserve_path(Path(os.environ["DATA_DIR"]))

  with TestClient(app) as client:
    assert client.get("/api/health").status_code == 200
    task = app.state.runtime_supervisors._tasks["storage-guard"]
    assert not task.done()

  assert ticks == []
  assert not reserve.exists()
