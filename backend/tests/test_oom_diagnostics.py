"""OOM diagnostics ring, victim diff, and capture assembly."""

from pathlib import Path

import app.oom_diagnostics as oom


def test_record_and_read_roundtrip(tmp_path):
  assert oom.record_oom_event(tmp_path, {"kind": "oom_kill", "n": 1}) is True
  assert oom.record_oom_event(tmp_path, {"kind": "oom_kill", "n": 2}) is True
  events = oom.recent_oom_events(tmp_path)
  assert [e["n"] for e in events] == [1, 2]


def test_ring_trims_at_high_water(tmp_path):
  total = oom._EVENTS_HIGH_WATER + 25
  for i in range(total):
    oom.record_oom_event(tmp_path, {"n": i})
  events = oom.recent_oom_events(tmp_path, limit=0)
  # Amortized trim keeps the file bounded by the high-water mark and always
  # retains the most recent event.
  assert len(events) <= oom._EVENTS_HIGH_WATER
  assert events[-1]["n"] == total - 1


def test_recent_events_missing_file_is_empty(tmp_path):
  assert oom.recent_oom_events(tmp_path) == []


def test_recent_events_skips_corrupt_lines(tmp_path):
  path = oom._events_path(tmp_path)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text('{"n": 1}\nnot json\n{"n": 2}\n', encoding="utf-8")
  assert [e["n"] for e in oom.recent_oom_events(tmp_path)] == [1, 2]


def test_vanished_processes_identifies_victims():
  pre = {"processes": [
    {"pid": 1, "name": "backend", "rss_bytes": 10},
    {"pid": 2, "name": "agent", "rss_bytes": 999},
  ]}
  post = {"processes": [{"pid": 1, "name": "backend", "rss_bytes": 10}]}
  assert oom._vanished_processes(pre, post) == [
    {"pid": 2, "name": "agent", "rss_bytes": 999},
  ]


def test_vanished_processes_handles_missing_samples():
  post = {"processes": [{"pid": 1, "name": "a", "rss_bytes": 1}]}
  assert oom._vanished_processes(None, post) == []
  assert oom._vanished_processes(post, None) == []


def test_lightweight_sample_reads_cgroup_procs(tmp_path):
  proc_root = tmp_path / "proc"
  cgroup_root = tmp_path / "cgroup"
  # Minimal cgroup-v2 membership so _cgroup_dir resolves to cgroup_root.
  (proc_root / "self").mkdir(parents=True)
  (proc_root / "self" / "cgroup").write_text("0::/\n", encoding="utf-8")
  cgroup_root.mkdir(parents=True)
  (cgroup_root / "cgroup.procs").write_text("7\n9\n", encoding="utf-8")
  for pid, rss_pages, comm in ((7, 100, "backend"), (9, 250, "agent")):
    d = proc_root / str(pid)
    d.mkdir()
    (d / "statm").write_text(f"200 {rss_pages} 5 0 0 0 0\n", encoding="utf-8")
    (d / "comm").write_text(f"{comm}\n", encoding="utf-8")
  sample = oom.lightweight_process_sample(
    proc_root=proc_root, cgroup_root=cgroup_root,
  )
  by_pid = {p["pid"]: p for p in sample["processes"]}
  assert by_pid[7]["name"] == "backend"
  assert by_pid[9]["rss_bytes"] == 250 * oom._PAGE_SIZE
  assert "ts" in sample


def test_capture_event_shape(monkeypatch):
  monkeypatch.setattr(oom, "cgroup_memory_snapshot", lambda: {"current_bytes": 1})
  monkeypatch.setattr(
    oom, "process_inventory", lambda limit=30: {"available": True, "groups": []},
  )
  monkeypatch.setattr(oom, "active_turn_summary", lambda: {"available": False})
  pre = {"processes": [{"pid": 2, "name": "agent", "rss_bytes": 9}]}
  post = {"processes": []}
  event = oom.capture_oom_event(
    oom_kill_count=3,
    kills_since_last=1,
    seconds_since_boot=4.2,
    pre_sample=pre,
    post_sample=post,
  )
  assert event["kind"] == "oom_kill"
  assert event["oom_kill_count"] == 3
  assert event["kills_since_last"] == 1
  assert event["likely_victims"] == [{"pid": 2, "name": "agent", "rss_bytes": 9}]
  assert event["cgroup"] == {"current_bytes": 1}
