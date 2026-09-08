"""Provider turns pause only for measured critical resource pressure."""

import pytest

from app.agent_admission import AgentTurnDeferred, require_agent_turn_admission


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

  result = await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: _status("normal", 2 * 1024 * 1024 * 1024),
    scratch_sweeper=sweep,
  )

  assert swept is False
  assert result is None


@pytest.mark.asyncio
async def test_unavailable_storage_telemetry_fails_open_without_sweeping():
  swept = False

  async def sweep():
    nonlocal swept
    swept = True
    return {}

  result = await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: {
      "facts": {"disk": {"free_bytes": None}},
      "pressure": {"disk": {"state": "unknown"}},
    },
    scratch_sweeper=sweep,
  )

  assert result is None
  assert swept is False


@pytest.mark.asyncio
async def test_constrained_storage_starts_without_sweeping():
  swept = False

  async def sweep():
    nonlocal swept
    swept = True
    return {}

  result = await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: _status("constrained", 1995 * 1024 * 1024),
    scratch_sweeper=sweep,
  )

  assert swept is False
  assert result is None


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

  result = await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: next(statuses),
    scratch_sweeper=sweep,
  )

  assert sweeps == 1
  assert result is None


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
async def test_normal_pressure_does_not_invent_per_turn_storage_claims():
  async def sweep():
    return {"removed": 0, "bytes": 0}

  result = await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: _status("normal", 10_000 * 1024 * 1024),
    scratch_sweeper=sweep,
  )

  assert result is None


@pytest.mark.asyncio
async def test_concurrent_turns_do_not_turn_healthy_disk_into_a_hidden_cap():
  free = 10_000 * 1024 * 1024

  async def sweep():
    return {"removed": 0, "bytes": 0}

  first = await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: _status("normal", free),
    scratch_sweeper=sweep,
  )
  second = await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: _status("normal", free),
    scratch_sweeper=sweep,
  )

  assert first is None
  assert second is None


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
  result = await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: _memory_status(
      "constrained", "PSI some avg60 1.8 >= 1.0",
    ),
    scratch_sweeper=None,
  )
  assert result is None


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


@pytest.mark.asyncio
async def test_psi_only_critical_memory_is_diagnostic_not_an_admission_veto():
  result = await require_agent_turn_admission(
    "/data",
    status_reader=lambda _path: _memory_status(
      "critical",
      "PSI full avg60 2.0 >= 2.0",
      working_set_ratio=0.5,
    ),
  )

  assert result is None


@pytest.mark.asyncio
async def test_cleanup_error_does_not_replace_resource_wait():
  async def sweep():
    raise OSError("scratch vanished during cleanup")

  with pytest.raises(AgentTurnDeferred, match="512 MiB free") as exc:
    await require_agent_turn_admission(
      "/data",
      status_reader=lambda _path: _status("critical"),
      scratch_sweeper=sweep,
    )

  assert exc.value.resource == "storage"
