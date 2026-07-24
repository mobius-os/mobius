"""Focused coverage for low-overhead memory accounting."""

from pathlib import Path

import app.memory_observability as memory_observability
from app.memory_observability import (
  allocation_report,
  cgroup_memory_snapshot,
  estimate_payload_bytes,
  memory_map_summary,
  process_memory_snapshot,
)


def test_process_memory_snapshot_reads_current_linux_process():
  snapshot = process_memory_snapshot()

  assert snapshot["available"] is True
  assert snapshot["rss_bytes"] > 0
  assert snapshot["anonymous_bytes"] >= 0
  assert snapshot["threads"] >= 1
  assert snapshot["uptime_seconds"] >= 0


def test_cgroup_snapshot_separates_reclaimable_file_cache(tmp_path: Path):
  proc_root = tmp_path / "proc"
  cgroup_root = tmp_path / "cgroup"
  (proc_root / "self").mkdir(parents=True)
  group = cgroup_root / "mobius"
  group.mkdir(parents=True)
  (proc_root / "self" / "cgroup").write_text(
    "0::/mobius\n", encoding="utf-8",
  )
  (group / "memory.current").write_text("1000\n", encoding="utf-8")
  (group / "memory.max").write_text("2000\n", encoding="utf-8")
  (group / "memory.swap.current").write_text("30\n", encoding="utf-8")
  (group / "memory.stat").write_text(
    "\n".join([
      "anon 300",
      "file 600",
      "inactive_file 400",
      "active_file 200",
      "kernel 100",
      "slab 80",
    ]) + "\n",
    encoding="utf-8",
  )
  (group / "memory.pressure").write_text(
    "some avg10=0.10 avg60=0.20 avg300=0.30 total=10\n"
    "full avg10=0.00 avg60=0.00 avg300=0.00 total=2\n",
    encoding="utf-8",
  )

  snapshot = cgroup_memory_snapshot(
    proc_root=proc_root,
    cgroup_root=cgroup_root,
  )

  assert snapshot == {
    "available": True,
    "current_bytes": 1000,
    "working_set_bytes": 600,
    "limit_bytes": 2000,
    "swap_current_bytes": 30,
    "anon_bytes": 300,
    "file_bytes": 600,
    "inactive_file_bytes": 400,
    "active_file_bytes": 200,
    "kernel_bytes": 100,
    "slab_bytes": 80,
    "pressure": {
      "some": {
        "avg10": 0.1, "avg60": 0.2, "avg300": 0.3, "total": 10,
      },
      "full": {
        "avg10": 0.0, "avg60": 0.0, "avg300": 0.0, "total": 2,
      },
    },
  }


def test_payload_estimate_is_cycle_safe_and_does_not_serialize():
  payload = {"text": "é", "items": [b"abc", 7]}
  payload["cycle"] = payload

  # UTF-8 key/value bytes plus three bytes and one scalar estimate.
  assert estimate_payload_bytes(payload) == (
    len("text".encode()) + len("é".encode())
    + len("items".encode()) + 3 + 8
    + len("cycle".encode())
  )


def test_memory_map_summary_keeps_heap_distinct_from_native_mappings(
  tmp_path: Path,
):
  proc_root = tmp_path / "proc"
  process = proc_root / "123"
  process.mkdir(parents=True)
  process.joinpath("smaps").write_text(
    "\n".join([
      "1000-2000 rw-p 00000000 00:00 0 [heap]",
      "Rss:                 100 kB",
      "Pss:                  90 kB",
      "Anonymous:            95 kB",
      "Private_Dirty:        80 kB",
      "Swap:                  2 kB",
      "3000-4000 rw-p 00000000 00:00 0",
      "Rss:                  50 kB",
      "Pss:                  50 kB",
      "Anonymous:            50 kB",
      "Private_Dirty:        40 kB",
      "Swap:                  1 kB",
      "5000-6000 r-xp 00000000 00:01 1 /usr/lib/libexample.so",
      "Rss:                  20 kB",
      "Pss:                  10 kB",
      "Anonymous:             0 kB",
      "Private_Dirty:         0 kB",
      "Swap:                  0 kB",
    ]) + "\n",
    encoding="utf-8",
  )

  result = memory_map_summary(123, proc_root=proc_root)
  groups = {row["category"]: row for row in result["groups"]}

  assert groups["heap"]["pss_bytes"] == 90 * 1024
  assert groups["anonymous_mappings"]["anonymous_bytes"] == 50 * 1024
  assert groups["file_mappings"]["rss_bytes"] == 20 * 1024


def test_zero_allocation_limit_does_not_materialize_heap_snapshot(monkeypatch):
  status = {"enabled": True, "current_bytes": 123, "peak_bytes": 456}
  monkeypatch.setattr(memory_observability, "tracing_status", lambda: status)

  assert allocation_report(limit=0) == status
