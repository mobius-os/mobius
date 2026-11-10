"""The storage guard keeps a filling data volume recoverable.

Each test drives the guard over a simulated volume and records what it did,
so the escalation order (reclaim, stop writers, pause turns, release the
reserve) and its one-notice-per-episode behaviour are pinned without touching
real processes or the real disk.
"""

from types import SimpleNamespace

import pytest

from app import disk_reserve, storage_guard

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
  def __init__(self, volume, *, reclaim_frees=0):
    self.volume = volume
    self.reclaim_frees = reclaim_frees
    self.calls: list[tuple] = []

  async def reclaim(self, _data_dir):
    self.calls.append(("reclaim",))
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
    return 245 * MIB

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


@pytest.mark.asyncio
async def test_failed_arm_is_not_retried_every_tick(tmp_path, no_continued_turns):
  volume = Volume(COMFORTABLE)
  recorder = Recorder(volume)
  attempts = []

  def unsupported(data_dir, *, target_bytes, disk_usage):
    attempts.append(target_bytes)
    return disk_reserve.ArmOutcome("unsupported", 0, "compressed away")

  now = [0.0]
  guard = _guard(tmp_path, volume, recorder, clock=lambda: now[0])
  guard.effects.arm_reserve = unsupported

  await guard.tick()
  await guard.tick()
  now[0] += storage_guard.ARM_RETRY_SECS + 1
  await guard.tick()

  assert len(attempts) == 2
  assert storage_guard.reserve_report(tmp_path)["state"] in ("released", "unavailable")
  assert guard.reserve_state == "unavailable"


@pytest.mark.asyncio
async def test_warning_line_reclaims_disposable_data_once_per_episode(tmp_path, no_continued_turns):
  volume = Volume(CONSTRAINED)
  recorder = Recorder(volume)
  guard = _guard(tmp_path, volume, recorder)

  await guard.tick()
  await guard.tick()

  assert recorder.names() == ["reclaim"]


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
  # Writers are stopped before the reserve is freed, so they cannot take it;
  # the reserve is freed before turns are paused, so the pauses can commit.
  assert recorder.names() == ["stop", "release", "reclaim", "pause", "notify"]
  stop = next(call for call in recorder.calls if call[0] == "stop")
  pause = next(call for call in recorder.calls if call[0] == "pause")
  assert stop[1] == frozenset()
  # The continued turn itself keeps running: pausing it would undo the
  # owner's explicit choice and make them start over.
  assert pause[1] == frozenset({"continued-chat"})
  assert outcome.reserve_released_bytes == 245 * MIB


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


def test_startup_releases_the_reserve_only_on_a_critical_volume(tmp_path, monkeypatch):
  disk_reserve.arm(
    tmp_path, target_bytes=1 * MIB, disk_usage=Volume(COMFORTABLE),
  )

  monkeypatch.setattr(storage_guard, "resource_status", _status_for(COMFORTABLE))
  assert storage_guard.release_reserve_at_boot(tmp_path) == 0
  assert disk_reserve.held_bytes(tmp_path) > 0

  monkeypatch.setattr(storage_guard, "resource_status", _status_for(CRITICAL))
  assert storage_guard.release_reserve_at_boot(tmp_path) > 0
  assert disk_reserve.held_bytes(tmp_path) == 0


def _status_for(free):
  from app.resource_pressure import resource_status

  def read(data_dir, **kwargs):
    kwargs["disk_usage"] = Volume(free)
    return resource_status(data_dir, **kwargs)

  return read



def test_container_start_releases_the_reserve_before_any_boot_write():
  """On a full volume the image's boot transaction cannot commit and the
  container refuses to start; the entrypoint frees the reserve first, using
  the server's own critical check so a healthy volume keeps it."""
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
