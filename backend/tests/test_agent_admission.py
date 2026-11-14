"""Provider turns pause only for measured critical resource pressure."""

import asyncio

import pytest

from app import agent_admission
from app.agent_admission import (
  START_HOLD_SECONDS,
  AgentTurnDeferred,
  TurnStartHolds,
  agent_turn_deferral,
  require_agent_turn_admission,
)
from app.resource_pressure import assess_resource_pressure


@pytest.fixture(autouse=True)
def _isolated_start_holds(monkeypatch):
  """Admitted turns here never end, so keep their holds out of other tests."""
  monkeypatch.setattr(agent_admission, "START_HOLDS", TurnStartHolds())


def _status(state: str, free: int = 512 * 1024 * 1024) -> dict:
  return {
    "facts": {"disk": {"free_bytes": free}},
    "pressure": {"disk": {
      "state": state,
      "critical_below_bytes": 1024 * 1024 * 1024,
    }},
  }


@pytest.mark.asyncio
async def test_unpressured_storage_starts_without_sweeping():
  swept = False

  async def sweep():
    nonlocal swept
    swept = True
    return {}

  await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: _status("normal", 2 * 1024 * 1024 * 1024),
    scratch_sweeper=sweep,
  )

  assert swept is False


@pytest.mark.asyncio
async def test_unavailable_storage_telemetry_fails_open_without_sweeping():
  swept = False

  async def sweep():
    nonlocal swept
    swept = True
    return {}

  await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: {
      "facts": {"disk": {"free_bytes": None}},
      "pressure": {"disk": {"state": "unknown"}},
    },
    scratch_sweeper=sweep,
  )

  assert swept is False


@pytest.mark.asyncio
async def test_constrained_storage_starts_without_sweeping():
  swept = False

  async def sweep():
    nonlocal swept
    swept = True
    return {}

  await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: _status("constrained", 1995 * 1024 * 1024),
    scratch_sweeper=sweep,
  )

  assert swept is False


@pytest.mark.asyncio
async def test_idle_scratch_reclaim_can_restore_critical_admission():
  statuses = iter([
    _status("critical"),
    _status("constrained", 2 * 1024 * 1024 * 1024),
  ])
  sweeps = 0

  async def sweep():
    nonlocal sweeps
    sweeps += 1
    return {"removed": 1, "bytes": 1024}

  await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: next(statuses),
    scratch_sweeper=sweep,
  )

  assert sweeps == 1


@pytest.mark.asyncio
async def test_persistent_critical_pressure_defers_with_truthful_reason():
  async def sweep():
    return {"removed": 0, "bytes": 0}

  with pytest.raises(
    AgentTurnDeferred,
    match="512 MiB free; safety floor 1024 MiB",
  ):
    await require_agent_turn_admission(
      "/data",
      status_reader=lambda _path: _status("critical"),
      scratch_sweeper=sweep,
    )


@pytest.mark.asyncio
async def test_cleanup_error_still_returns_the_resource_deferral():
  async def sweep():
    raise OSError("scratch disappeared during cleanup")

  with pytest.raises(
    AgentTurnDeferred,
    match="512 MiB free; safety floor 1024 MiB",
  ):
    await require_agent_turn_admission(
      "/data",
      status_reader=lambda _path: _status("critical"),
      scratch_sweeper=sweep,
    )


def _memory_status(
  state: str,
  *signals: str,
  working_set_ratio: float = 0.5,
) -> dict:
  return {
    "facts": {"disk": {"free_bytes": 8 * 1024 * 1024 * 1024}},
    "pressure": {
      "disk": {"state": "normal"},
      "memory": {
        "state": state,
        "working_set_ratio": working_set_ratio,
        "critical_at_ratio": 0.9,
        "reason": {"signals": list(signals)} if signals else None,
      },
    },
  }


@pytest.mark.asyncio
async def test_constrained_memory_still_admits_new_turns():
  await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: _memory_status(
      "constrained", "PSI some avg60 1.8 >= 1.0",
    ),
    scratch_sweeper=None,
  )


@pytest.mark.asyncio
async def test_critical_memory_defers_new_turns_and_names_the_signal():
  """Starting another agent under critical memory invites the OOM killer."""
  async def sweep():
    return {"removed": 0, "bytes": 0}

  with pytest.raises(
    AgentTurnDeferred,
    match=r"memory headroom because unreclaimable footprint is 94% of the limit",
  ):
    await require_agent_turn_admission(
      "/data",
      status_reader=lambda _path: _memory_status(
        "critical",
        "unreclaimable footprint is 94% of the limit (threshold 90%)",
        working_set_ratio=0.94,
      ),
      scratch_sweeper=sweep,
    )

  with pytest.raises(AgentTurnDeferred, match="continue automatically"):
    await require_agent_turn_admission(
      "/data",
      status_reader=lambda _path: _memory_status(
        "critical", working_set_ratio=0.94,
      ),
      scratch_sweeper=sweep,
    )


@pytest.mark.asyncio
async def test_critical_memory_alone_never_sweeps_paused_chats_scratch():
  """Scratch cleanup frees disk, not memory, so memory pressure keeps it."""
  swept = False

  async def sweep():
    nonlocal swept
    swept = True
    return {}

  with pytest.raises(AgentTurnDeferred):
    await require_agent_turn_admission(
      "/data",
      status_reader=lambda _path: _memory_status(
        "critical", working_set_ratio=0.94,
      ),
      scratch_sweeper=sweep,
    )
  assert swept is False


@pytest.mark.asyncio
async def test_psi_only_critical_memory_is_diagnostic_not_an_admission_veto():
  await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: _memory_status(
      "critical",
      "PSI full avg60 2.0 >= 2.0",
      working_set_ratio=0.5,
    ),
  )


@pytest.mark.asyncio
async def test_warned_storage_override_admits_the_turn_under_storage_pressure():
  await require_agent_turn_admission(
    "/data", owner_overrides=frozenset({"storage"}),
    status_reader=lambda _path: _status("critical"), scratch_sweeper=_no_sweep,
  )
  with pytest.raises(AgentTurnDeferred) as waiting:
    await require_agent_turn_admission(
      "/data", status_reader=lambda _path: _status("critical"),
      scratch_sweeper=_no_sweep,
    )
  assert waiting.value.resource == "storage", "later turns retain normal admission"


async def _no_sweep():
  return {}


@pytest.mark.asyncio
async def test_storage_override_does_not_override_critical_memory():
  status = _status("critical")
  status["pressure"]["memory"] = _memory_status(
    "critical", working_set_ratio=0.94,
  )["pressure"]["memory"]
  with pytest.raises(AgentTurnDeferred) as waiting:
    await require_agent_turn_admission(
      "/data", owner_overrides=frozenset({"storage"}),
      status_reader=lambda _path: status, scratch_sweeper=_no_sweep,
    )
  assert waiting.value.resource == "memory"


@pytest.mark.asyncio
async def test_critical_process_headroom_defers_new_turns_before_the_task_cap():
  """A burst of agents past the thread cap took every route down; wait instead."""
  async def sweep():
    raise AssertionError("scratch cleanup frees no threads")

  status = {
    "facts": {"disk": {"free_bytes": 8 * 1024 * 1024 * 1024}},
    "pressure": {
      "disk": {"state": "normal"},
      "memory": {"state": "normal", "working_set_ratio": 0.4, "critical_at_ratio": 0.9},
      "processes": {"state": "critical", "current_tasks": 870, "limit_tasks": 1000},
    },
  }
  with pytest.raises(AgentTurnDeferred, match=r"870 of its 1000 processes") as raised:
    await require_agent_turn_admission(
      "/data", status_reader=lambda _path: status, scratch_sweeper=sweep,
    )
  assert raised.value.resource == "processes"

  status["pressure"]["processes"]["state"] = "constrained"
  await require_agent_turn_admission(
    "/data", status_reader=lambda _path: status, scratch_sweeper=sweep,
  )


def _tasks_status(current: int, limit: int = 1000) -> dict:
  facts = {
    "disk": {"free_bytes": 8 * 1024 * 1024 * 1024},
    "processes": {"available": True, "current_tasks": current, "limit_tasks": limit},
  }
  return {"facts": facts, "pressure": assess_resource_pressure(facts)}


async def _sweep_frees_no_threads():
  raise AssertionError("scratch cleanup frees no threads")


@pytest.mark.asyncio
async def test_simultaneous_burst_cannot_all_pass_one_pre_burst_task_count():
  """Every turn in a burst reads the same count, taken before any CLI starts.

  Each admitted turn holds a per-turn task budget, so the burst stops short of
  the cap instead of every member seeing 700 of 1000 and starting.
  """
  holds = TurnStartHolds()
  status = _tasks_status(700)
  assert status["pressure"]["processes"]["state"] == "constrained"

  async def admit():
    try:
      return await require_agent_turn_admission(
        "/data", status_reader=lambda _path: status, scratch_sweeper=_sweep_frees_no_threads,
        holds=holds,
      )
    except AgentTurnDeferred as deferred:
      return deferred

  outcomes = await asyncio.gather(*(admit() for _ in range(10)))
  deferred = [o for o in outcomes if isinstance(o, AgentTurnDeferred)]
  admitted = len(outcomes) - len(deferred)
  # 700 + 4 * 40 = 860 crosses the 85% critical ratio.
  assert admitted == 4
  assert {d.resource for d in deferred} == {"processes"}
  assert "700 of its 1000 processes and threads in use and 4 more agent turns still starting" in str(deferred[0])
  assert holds.starting_turns() == 4


@pytest.mark.asyncio
async def test_start_hold_ends_when_its_turn_ends_or_has_had_time_to_start():
  now = [0.0]
  holds = TurnStartHolds(clock=lambda: now[0])
  status = _tasks_status(820)

  async def admit():
    return await require_agent_turn_admission(
      "/data", status_reader=lambda _path: status, scratch_sweeper=_sweep_frees_no_threads,
      holds=holds,
    )

  first = await admit()
  with pytest.raises(AgentTurnDeferred, match="1 more agent turn still starting"):
    await admit()
  first.release()
  first.release()
  assert holds.starting_turns() == 0

  await admit()
  now[0] = START_HOLD_SECONDS
  # By now the measured count includes the started turn; its hold lapses.
  assert holds.starting_turns() == 0
  await admit()


@pytest.mark.asyncio
async def test_resume_check_counts_starting_turns_without_taking_a_hold():
  """The continuation sweep asks before resuming; asking must not reserve."""
  holds = TurnStartHolds()
  status = _tasks_status(820)

  def reader(_path):
    return status

  assert await agent_turn_deferral("/data", status_reader=reader, holds=holds) is None
  assert holds.starting_turns() == 0
  holds.hold()
  waiting = await agent_turn_deferral("/data", status_reader=reader, holds=holds)
  assert waiting is not None and waiting.resource == "processes"


@pytest.mark.asyncio
async def test_unknown_task_cap_never_holds_turns_for_starting_ones():
  """Uncapped hosts fail open even while many turns are starting."""
  holds = TurnStartHolds()
  for _ in range(50):
    holds.hold()
  status = _tasks_status(700, limit=None)
  await require_agent_turn_admission(
    "/data", status_reader=lambda _path: status, scratch_sweeper=_sweep_frees_no_threads,
    holds=holds,
  )


@pytest.mark.asyncio
async def test_warned_process_override_admits_one_turn_and_still_holds_its_start():
  """Continue anyway is the owner's way out when the cap is held elsewhere."""
  holds = TurnStartHolds()
  status = _tasks_status(900)
  hold = await require_agent_turn_admission(
    "/data", owner_overrides=frozenset({"processes"}),
    status_reader=lambda _path: status, scratch_sweeper=_sweep_frees_no_threads,
    holds=holds,
  )
  assert holds.starting_turns() == 1, "an overridden turn still counts while it starts"
  hold.release()
  with pytest.raises(AgentTurnDeferred) as waiting:
    await require_agent_turn_admission(
      "/data", status_reader=lambda _path: status,
      scratch_sweeper=_sweep_frees_no_threads, holds=holds,
    )
  assert waiting.value.resource == "processes", "later turns retain normal admission"


@pytest.mark.asyncio
async def test_each_override_lifts_only_its_own_floor():
  status = _tasks_status(900)
  status["facts"]["disk"]["free_bytes"] = 945 * 1024**2
  status["pressure"]["disk"] = {"state": "critical", "critical_below_bytes": 1024**3}
  with pytest.raises(AgentTurnDeferred) as storage_lifted:
    await require_agent_turn_admission(
      "/data", owner_overrides=frozenset({"storage"}),
      status_reader=lambda _path: status, scratch_sweeper=_sweep_frees_no_threads,
      holds=TurnStartHolds(),
    )
  assert storage_lifted.value.resource == "processes"
  with pytest.raises(AgentTurnDeferred) as processes_lifted:
    await require_agent_turn_admission(
      "/data", owner_overrides=frozenset({"processes"}),
      status_reader=lambda _path: status, scratch_sweeper=_no_sweep,
      holds=TurnStartHolds(),
    )
  assert processes_lifted.value.resource == "storage"


@pytest.mark.asyncio
async def test_process_override_does_not_override_critical_memory():
  status = _tasks_status(900)
  status["pressure"]["memory"] = _memory_status(
    "critical", working_set_ratio=0.94,
  )["pressure"]["memory"]
  with pytest.raises(AgentTurnDeferred) as waiting:
    await require_agent_turn_admission(
      "/data", owner_overrides=frozenset({"processes", "storage"}),
      status_reader=lambda _path: status, scratch_sweeper=_sweep_frees_no_threads,
      holds=TurnStartHolds(),
    )
  assert waiting.value.resource == "memory"
