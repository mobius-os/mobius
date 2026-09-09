"""Tests for multi-domain capacity attribution (#3) + forecast/alerting (#7)."""

from __future__ import annotations

import os

import pytest

from app import capacity, capacity_history
from app.resource_pressure import GIB, MIB


# --- capacity.py: alert ladder ----------------------------------------------


@pytest.mark.parametrize(
  "free,expected",
  [
    (10 * GIB, None),
    (8 * GIB, None),           # exactly at 8 GiB is not below the tier
    (8 * GIB - 1, "notice"),
    (6 * GIB, "notice"),
    (5 * GIB - 1, "warning"),
    (3 * GIB, "warning"),
    (2 * GIB - 1, "critical"),
    (500 * MIB, "critical"),
    (None, None),
  ],
)
def test_data_alert_tier(free, expected):
  assert capacity.data_alert_tier(free) == expected


# --- capacity.py: bounded, symlink-safe, size-only dir walk -----------------


def test_dir_size_counts_regular_files_only(tmp_path):
  (tmp_path / "a.bin").write_bytes(b"x" * 1000)
  sub = tmp_path / "sub"
  sub.mkdir()
  (sub / "b.bin").write_bytes(b"y" * 500)
  # A symlink must never be followed or counted as a file.
  try:
    os.symlink(tmp_path / "a.bin", tmp_path / "link")
  except OSError:
    pass
  result = capacity._dir_size(tmp_path)
  assert result["exists"] is True
  expected = (tmp_path / "a.bin").stat().st_blocks * 512
  expected += (sub / "b.bin").stat().st_blocks * 512
  assert result["bytes"] == expected
  assert result["truncated"] is False


def test_dir_size_missing_dir():
  result = capacity._dir_size(capacity.Path("/nonexistent/xyz"))
  assert result == {"exists": False, "bytes": 0, "entries": 0, "truncated": False}


def test_data_domains_sizes_cli_auth_without_leaking_names(tmp_path):
  codex = tmp_path / "cli-auth" / "codex"
  codex.mkdir(parents=True)
  (codex / "state_7.sqlite").write_bytes(b"z" * 4096)
  domains = capacity.data_domains(tmp_path)
  expected = (codex / "state_7.sqlite").stat().st_blocks * 512
  assert domains["provider_telemetry_codex"]["bytes"] >= expected
  # Only aggregate byte totals are exposed — never filenames/contents.
  assert set(domains["provider_telemetry_codex"]) == {
    "exists", "bytes", "entries", "truncated",
  }
  assert domains["provider_telemetry_claude"]["exists"] is False


def test_data_domains_includes_large_operational_trees(tmp_path):
  expected = {
    "browser_profiles": "agent-browser-profiles",
    "contributions": "contrib",
    "work": "work",
    "worktrees": "worktrees",
    "compiled": "compiled",
  }
  for index, directory in enumerate(expected.values(), start=1):
    path = tmp_path / directory
    path.mkdir()
    (path / "payload").write_bytes(b"x" * index)

  domains = capacity.data_domains(tmp_path)

  for domain, directory in expected.items():
    payload = tmp_path / directory / "payload"
    assert domains[domain]["bytes"] >= payload.stat().st_blocks * 512


def test_du_domain_sizes_marks_only_unfinished_paths_truncated(
  tmp_path, monkeypatch,
):
  first = tmp_path / "first"
  second = tmp_path / "second"
  first.mkdir()
  second.mkdir()

  class TimedOutDu:
    def __init__(self, *_args, **_kwargs):
      self.killed = False

    def communicate(self, timeout=None):
      if timeout is not None:
        raise capacity.subprocess.TimeoutExpired("du", timeout)
      return f"4096\t{first}\n", ""

    def kill(self):
      self.killed = True

  monkeypatch.setattr(capacity.subprocess, "Popen", TimedOutDu)

  result = capacity._du_domain_sizes({"first": first, "second": second})

  assert result["first"]["bytes"] == 4096
  assert result["first"]["truncated"] is False
  assert result["second"]["truncated"] is True
  assert result["second"]["timed_out"] is True


# --- capacity.py: snapshot ---------------------------------------------------


def _fake_status(free_by_path):
  def reader(path, **_k):
    free = free_by_path.get(path, 40 * GIB)
    return {
      "facts": {"disk": {
        "available": True, "path": path,
        "free_bytes": free, "total_bytes": 45 * GIB, "used_bytes": 45 * GIB - free,
      }},
      "pressure": {"disk": {
        "state": "normal", "free_ratio": free / (45 * GIB),
        "constrained_below_bytes": 2 * GIB, "critical_below_bytes": 1 * GIB,
      }},
    }
  return reader


def test_capacity_snapshot_reports_mounts_and_tier():
  reader = _fake_status({"/": 20 * GIB, "/data": 3 * GIB})
  snap = capacity.capacity_snapshot("/data", status_reader=reader)
  assert snap["mounts"]["host_root"]["free_bytes"] == 20 * GIB
  assert snap["mounts"]["data"]["free_bytes"] == 3 * GIB
  assert snap["data_free_bytes"] == 3 * GIB
  assert snap["alert_tier"] == "warning"  # 3 GiB is below 5, above 2
  assert snap["alert_ladder"] == {"notice": 8 * GIB, "warning": 5 * GIB, "critical": 2 * GIB}
  assert "domains" not in snap


def test_capacity_snapshot_include_domains_uses_injected_reader():
  reader = _fake_status({"/": 20 * GIB, "/data": 40 * GIB})
  snap = capacity.capacity_snapshot(
    "/data", include_domains=True, status_reader=reader,
    domains_reader=lambda _dd: {"chats": {"bytes": 123, "exists": True}},
  )
  assert snap["domains"] == {"chats": {"bytes": 123, "exists": True}}
  assert snap["alert_tier"] is None  # 40 GiB free


# --- capacity_history.py: ring ----------------------------------------------


def test_record_and_load_ring(tmp_path):
  for i in range(5):
    assert capacity_history.record_sample(
      tmp_path, {"ts": float(i), "data_free_bytes": 10 * GIB - i}
    )
  samples = capacity_history.load_samples(tmp_path)
  assert [s["ts"] for s in samples] == [0.0, 1.0, 2.0, 3.0, 4.0]
  assert capacity_history.load_samples(tmp_path, limit=2)[0]["ts"] == 3.0


def test_ring_trims_at_high_water(tmp_path, monkeypatch):
  monkeypatch.setattr(capacity_history, "_HISTORY_HIGH_WATER", 10)
  monkeypatch.setattr(capacity_history, "_HISTORY_TRIM_TARGET", 5)
  for i in range(30):
    capacity_history.record_sample(tmp_path, {"ts": float(i), "data_free_bytes": i})
  samples = capacity_history.load_samples(tmp_path)
  # Strictly bounded: never grows without limit.
  assert len(samples) <= 11
  assert samples[-1]["ts"] == 29.0


# --- capacity_history.py: forecast ------------------------------------------


def test_forecast_insufficient_history():
  assert capacity_history.forecast([])["status"] == "insufficient_history"
  two = [{"ts": 0, "data_free_bytes": 10}, {"ts": 600, "data_free_bytes": 9}]
  assert capacity_history.forecast(two)["status"] == "insufficient_history"


def test_forecast_declining_gives_time_to_exhaustion():
  # 1 GiB lost per 600 s over 30 min, currently 3 GiB free -> ~30 min to zero.
  samples = [
    {"ts": float(i * 300), "data_free_bytes": int(5 * GIB - i * GIB)}
    for i in range(3)  # 5G, 4G, 3G at t=0,300,600
  ]
  result = capacity_history.forecast(samples)
  assert result["status"] == "ok"
  assert result["rate_bytes_per_sec"] < 0
  # 3 GiB / (GiB/300s) = 900 s
  assert result["time_to_exhaustion_seconds"] == pytest.approx(900, rel=0.05)


def test_forecast_rising_has_no_exhaustion():
  samples = [
    {"ts": float(i * 300), "data_free_bytes": int(3 * GIB + i * GIB)}
    for i in range(3)
  ]
  result = capacity_history.forecast(samples)
  assert result["status"] == "ok"
  assert result["rate_bytes_per_sec"] > 0
  assert result["time_to_exhaustion_seconds"] is None


# --- capacity_history.py: alert suppression ---------------------------------


@pytest.mark.parametrize(
  "current,previous,expected",
  [
    ("notice", None, True),      # first entry into a tier alerts
    ("notice", "notice", False), # same tier: already alerted, suppressed
    ("warning", "notice", True), # escalation alerts
    ("critical", "warning", True),
    ("notice", "warning", False),  # recovery: quiet, baseline updates
    (None, "critical", False),     # full recovery: quiet
  ],
)
def test_evaluate_alert_escalation_only(current, previous, expected):
  assert capacity_history.evaluate_alert(current, previous) is expected


def test_alert_notification_id_is_per_tier_per_day():
  assert capacity_history.alert_notification_id("critical", day="20260901") == (
    "capacity-critical-20260901"
  )
  assert capacity_history.alert_notification_id(
    "warning", day="20260901"
  ) != capacity_history.alert_notification_id("critical", day="20260901")


def test_alert_body_includes_top_domain_and_forecast():
  snap = {
    "data_free_bytes": int(1.5 * GIB),
    "alert_tier": "critical",
    "domains": {
      "database": {"bytes": 1 * GIB},
      "provider_telemetry_codex": {"bytes": 3 * GIB},
    },
  }
  body = capacity_history.alert_body(snap, {"time_to_exhaustion_seconds": 3600})
  assert "critical" in body
  assert "provider_telemetry_codex" in body  # the largest domain
  assert "1.0h" in body


def test_alert_body_does_not_rank_partial_domain_totals():
  snap = {
    "data_free_bytes": int(1.5 * GIB),
    "alert_tier": "critical",
    "domains": {
      "provider_telemetry_codex": {"bytes": 8 * GIB, "truncated": False},
      "contributions": {"bytes": 3 * GIB, "truncated": True},
    },
  }

  body = capacity_history.alert_body(snap, None)

  assert "Largest domain" not in body
  assert "scan-limited" in body
  assert "contributions" in body


def test_alert_state_roundtrip(tmp_path):
  capacity_history.save_alert_state(tmp_path, {"tier": "warning"})
  assert capacity_history.load_alert_state(tmp_path) == {"tier": "warning"}
  assert capacity_history.load_alert_state(tmp_path / "empty") == {}


# --- capacity_monitor.py: one full tick, storm-suppressed --------------------


def _snap(free):
  return {
    "captured_at": "t",
    "mounts": {"host_root": {"free_bytes": 20 * GIB}, "data": {"free_bytes": free}},
    "data_free_bytes": free,
    "data_total_bytes": 45 * GIB,
    "alert_tier": capacity.data_alert_tier(free),
    "alert_ladder": {},
    "domains": {"provider_telemetry_codex": {"bytes": 3 * GIB}},
  }


def test_run_capacity_tick_alerts_on_escalation_only(tmp_path):
  from app import capacity_monitor
  from app.capacity import latest_snapshot

  calls = []

  def tick(free):
    return capacity_monitor.run_capacity_tick(
      str(tmp_path),
      snapshot_fn=lambda _dd, include_domains=True: _snap(free),
      notify=lambda **kw: calls.append(kw),
      day="20260901",
    )

  r1 = tick(int(1.5 * GIB))          # first critical -> alert
  assert r1["tier"] == "critical" and r1["alerted"] is True
  assert calls[-1]["notification_id"] == "capacity-critical-20260901"
  assert "provider_telemetry_codex" in calls[-1]["body"]

  r2 = tick(int(1.4 * GIB))          # still critical -> suppressed
  assert r2["alerted"] is False
  assert len(calls) == 1

  r3 = tick(int(40 * GIB))           # recovery -> no alert, baseline re-armed
  assert r3["tier"] is None and r3["alerted"] is False

  r4 = tick(int(3 * GIB))            # drop to warning -> alert again
  assert r4["tier"] == "warning" and r4["alerted"] is True
  assert calls[-1]["notification_id"] == "capacity-warning-20260901"

  assert "forecast" in (latest_snapshot() or {})


def test_failed_alert_delivery_retries_same_tier_on_next_tick(tmp_path):
  from app import capacity_monitor

  attempts = []

  def failed_delivery(**kwargs):
    attempts.append(kwargs)
    raise RuntimeError("notification persistence unavailable")

  first = capacity_monitor.run_capacity_tick(
    str(tmp_path),
    snapshot_fn=lambda _dd, include_domains=True: _snap(int(1.5 * GIB)),
    notify=failed_delivery,
    day="20260901",
  )
  assert first["alerted"] is True
  assert first["delivered"] is False

  delivered = []
  second = capacity_monitor.run_capacity_tick(
    str(tmp_path),
    snapshot_fn=lambda _dd, include_domains=True: _snap(int(1.4 * GIB)),
    notify=lambda **kwargs: delivered.append(kwargs),
    day="20260901",
  )
  assert second["alerted"] is True
  assert second["delivered"] is True
  assert second["notification_id"] == first["notification_id"]
  assert len(attempts) == 1
  assert len(delivered) == 1

  third = capacity_monitor.run_capacity_tick(
    str(tmp_path),
    snapshot_fn=lambda _dd, include_domains=True: _snap(int(1.3 * GIB)),
    notify=lambda **kwargs: delivered.append(kwargs),
    day="20260901",
  )
  assert third["alerted"] is False
  assert len(delivered) == 1


def test_async_alert_stays_pending_until_durable_delivery_is_acknowledged(tmp_path):
  from app import capacity_monitor

  first = capacity_monitor.run_capacity_tick(
    str(tmp_path),
    snapshot_fn=lambda _dd, include_domains=True: _snap(int(3 * GIB)),
    notify=None,
    day="20260901",
  )
  second = capacity_monitor.run_capacity_tick(
    str(tmp_path),
    snapshot_fn=lambda _dd, include_domains=True: _snap(int(3 * GIB)),
    notify=None,
    day="20260901",
  )
  assert first["alerted"] is True
  assert second["alerted"] is True
  assert first["delivered"] is second["delivered"] is False

  capacity_monitor.record_alert_delivery(str(tmp_path), "warning")
  after_ack = capacity_monitor.run_capacity_tick(
    str(tmp_path),
    snapshot_fn=lambda _dd, include_domains=True: _snap(int(3 * GIB)),
    notify=None,
    day="20260901",
  )
  assert after_ack["alerted"] is False


# --- operator endpoint ------------------------------------------------------


def test_debug_capacity_endpoint(client, auth):
  r = client.get("/api/debug/capacity?refresh=1", headers=auth)
  assert r.status_code == 200
  body = r.json()
  assert "mounts" in body
  assert set(body["mounts"]) == {"host_root", "data"}
  assert "alert_ladder" in body
  assert "domains" in body  # refresh=1 forces the domain walk
  # Operator-scoped: unauthenticated is rejected.
  assert client.get("/api/debug/capacity").status_code == 401
