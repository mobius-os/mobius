"""Focused coverage for resource facts and pressure interpretation."""

from types import SimpleNamespace

from app.resource_pressure import (
  MIB,
  assess_memory_pressure,
  assess_resource_pressure,
  resource_facts,
  resource_status,
)


def _facts(*, total, free, working_set, limit, some=0.0, full=0.0):
  return {
    "disk": {
      "available": True,
      "total_bytes": total,
      "used_bytes": total - free,
      "free_bytes": free,
    },
    "memory": {
      "available": True,
      "working_set_bytes": working_set,
      "limit_bytes": limit,
      "pressure": {
        "some": {"avg60": some},
        "full": {"avg60": full},
      },
    },
  }


def test_resource_facts_reuses_existing_memory_snapshot():
  memory = {
    "available": True,
    "current_bytes": 900,
    "working_set_bytes": 400,
    "limit_bytes": 1000,
    "inactive_file_bytes": 500,
    "pressure": {"some": {"avg60": 0.0}},
    "internal_field": "not part of the facts contract",
  }

  facts = resource_facts(
    "/data",
    disk_usage=lambda _path: SimpleNamespace(
      total=1000, used=700, free=300,
    ),
    memory=memory,
  )

  assert facts["disk"] == {
    "available": True,
    "path": "/data",
    "total_bytes": 1000,
    "used_bytes": 700,
    "free_bytes": 300,
  }
  assert facts["memory"]["working_set_bytes"] == 400
  assert facts["memory"]["inactive_file_bytes"] == 500
  assert "internal_field" not in facts["memory"]
  assert facts["captured_at"]


def test_small_volume_is_critical_below_absolute_floor():
  pressure = assess_resource_pressure(_facts(
    total=512 * MIB,
    free=31 * MIB,
    working_set=450 * MIB,
    limit=954 * MIB,
  ))

  assert pressure["state"] == "critical"
  assert pressure["disk"]["state"] == "critical"
  assert pressure["disk"]["critical_below_bytes"] == 32 * MIB
  assert pressure["memory"]["state"] == "normal"
  assert pressure["reasons"][0]["code"] == "disk_free_below_critical"


def test_disk_thresholds_are_proportional_but_bounded_on_large_volumes():
  pressure = assess_resource_pressure(_facts(
    total=1000 * 1024**3,
    free=1500 * MIB,
    working_set=1,
    limit=100,
  ))

  assert pressure["disk"]["constrained_below_bytes"] == 2 * 1024**3
  assert pressure["disk"]["critical_below_bytes"] == 1024**3
  assert pressure["disk"]["state"] == "constrained"


def test_reclaimable_file_cache_does_not_create_false_memory_pressure():
  facts = _facts(
    total=10 * 1024**3,
    free=5 * 1024**3,
    working_set=450 * MIB,
    limit=954 * MIB,
  )
  facts["memory"].update({
    "current_bytes": 900 * MIB,
    "inactive_file_bytes": 450 * MIB,
  })

  pressure = assess_resource_pressure(facts)

  assert pressure["memory"]["state"] == "normal"
  assert pressure["memory"]["working_set_ratio"] < 0.5
  assert pressure["state"] == "normal"


def test_memory_ratio_and_sustained_psi_raise_pressure():
  constrained = assess_resource_pressure(_facts(
    total=10 * 1024**3,
    free=5 * 1024**3,
    working_set=800,
    limit=1000,
  ))
  critical = assess_resource_pressure(_facts(
    total=10 * 1024**3,
    free=5 * 1024**3,
    working_set=500,
    limit=1000,
    full=2.5,
  ))

  assert constrained["state"] == "constrained"
  assert constrained["memory"]["state"] == "constrained"
  assert critical["state"] == "critical"
  assert critical["memory"]["reason"]["full_avg60"] == 2.5


def test_finite_memory_exposes_current_headroom():
  pressure = assess_memory_pressure({
    "available": True,
    "working_set_bytes": 400 * MIB,
    "limit_bytes": 1024 * MIB,
    "pressure": {},
  })

  assert pressure["state"] == "normal"
  assert pressure["headroom_bytes"] == 624 * MIB


def test_memory_without_a_cgroup_limit_stays_unknown():
  """An unlimited cgroup has no ratio, so nothing may be inferred from PSI."""
  pressure = assess_memory_pressure({
    "available": True,
    "working_set_bytes": 400 * MIB,
    "limit_bytes": None,
    "pressure": {"some": {"avg60": 1.25}},
  })

  assert pressure["state"] == "unknown"


def test_unknown_resource_does_not_hide_known_pressure():
  facts = {
    "disk": {"available": False},
    "memory": {
      "available": True,
      "working_set_bytes": 950,
      "limit_bytes": 1000,
      "pressure": {},
    },
  }

  pressure = assess_resource_pressure(facts)

  assert pressure["state"] == "critical"
  assert {reason["code"] for reason in pressure["reasons"]} == {
    "disk_facts_unavailable",
    "memory_pressure_critical",
  }


def test_resource_status_keeps_facts_and_pressure_separate():
  status = resource_status(
    "/data",
    disk_usage=lambda _path: SimpleNamespace(
      total=10 * 1024**3,
      used=5 * 1024**3,
      free=5 * 1024**3,
    ),
    memory={
      "available": True,
      "working_set_bytes": 400,
      "limit_bytes": 1000,
      "pressure": {},
    },
  )

  assert set(status) == {"facts", "pressure"}
  assert status["pressure"]["state"] == "normal"
