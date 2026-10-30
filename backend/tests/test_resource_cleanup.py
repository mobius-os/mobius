"""Owner cleanup for the Settings Storage and Memory box: what it keeps and frees."""

import asyncio
import os
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta

from app import agent_scratch, capacity, config, models, resource_cleanup, resource_overview
from app.browser_profiles import clear_closed_chat_profiles, closed_chat_profile_usage
from app.provider_session_retention import stale_session_usage
from test_app_fixtures import create_local_app

DAY = 86400


def _write(path, size, *, age_days=0.0):
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_bytes(b"x" * size)
  stamp = time.time() - age_days * DAY
  os.utime(path, (stamp, stamp))
  return path


def _age(path, age_days):
  stamp = time.time() - age_days * DAY
  os.utime(path, (stamp, stamp))


# --- old agent sessions ------------------------------------------------------


def _clear_sessions(monkeypatch, tmp_path, *, owners=None, idle=lambda _chat_id: True):
  monkeypatch.setattr(resource_cleanup, "_session_owners", lambda _ids: dict(owners or {}))
  monkeypatch.setattr(resource_cleanup, "_chat_turn_running", lambda chat_id: not idle(chat_id))
  return asyncio.run(resource_cleanup._clear_agent_sessions(str(tmp_path)))


def test_old_session_clear_keeps_recent_sessions_and_removes_old_ones_with_their_folder(
  tmp_path, monkeypatch,
):
  project = tmp_path / "cli-auth" / "claude" / "projects" / "-data"
  _write(project / "old.jsonl", 1000, age_days=5)
  _write(project / "old" / "tool-results" / "r.txt", 500, age_days=5)
  _age(project / "old" / "tool-results", 5)
  _age(project / "old", 5)
  _write(project / "recent.jsonl", 700, age_days=0)
  # An old transcript whose session still wrote a tool result today is in use.
  _write(project / "resumed.jsonl", 300, age_days=5)
  _write(project / "resumed" / "tool-results" / "fresh.txt", 10, age_days=0)

  preview = stale_session_usage(tmp_path, max_age_days=2)
  assert preview == {"bytes": 1500, "sessions": 1}

  result = _clear_sessions(monkeypatch, tmp_path)

  assert result["items"] == 1 and result["bytes"] >= 1500
  assert sorted(path.name for path in project.iterdir()) == ["recent.jsonl", "resumed", "resumed.jsonl"]


def test_old_session_of_a_running_chat_is_never_cleared(tmp_path, monkeypatch):
  project = tmp_path / "cli-auth" / "claude" / "projects" / "-data"
  _write(project / "resuming.jsonl", 1000, age_days=5)
  _write(project / "idle.jsonl", 400, age_days=5)

  preview = stale_session_usage(tmp_path, max_age_days=2, in_use=frozenset({"resuming"}))
  assert preview == {"bytes": 400, "sessions": 1}
  _clear_sessions(
    monkeypatch, tmp_path, owners={"resuming": "chat-r", "idle": "chat-i"},
    idle=lambda chat_id: chat_id != "chat-r",
  )

  assert (project / "resuming.jsonl").exists()
  assert not (project / "idle.jsonl").exists()


def test_a_chat_that_starts_while_another_session_is_deleted_keeps_its_own(
  tmp_path, monkeypatch,
):
  """Each session is checked and moved under its own chat's run-start lock."""
  from app import chat_queue, provider_session_retention

  project = tmp_path / "cli-auth" / "claude" / "projects" / "-data"
  _write(project / "first.jsonl", 1000, age_days=5)
  _write(project / "second.jsonl", 400, age_days=5)
  owners = {"first": "chat-1", "second": "chat-2"}
  started = set()
  real_detach = provider_session_retention.detach_claude_session

  def detach_then_other_chat_starts(transcript, side):
    assert chat_queue.get_lock(owners[transcript.stem]).locked()
    moved = real_detach(transcript, side)
    started.update(set(owners.values()) - {owners[transcript.stem]})
    return moved

  monkeypatch.setattr(provider_session_retention, "detach_claude_session", detach_then_other_chat_starts)
  result = _clear_sessions(
    monkeypatch, tmp_path, owners=owners, idle=lambda chat_id: chat_id not in started,
  )

  assert result["items"] == 1
  kept = [path.stem for path in project.glob("*.jsonl")]
  assert len(kept) == 1 and owners[kept[0]] in started


def test_sessions_left_aside_by_an_interrupted_clear_are_reclaimed_next_time(tmp_path, monkeypatch):
  project = tmp_path / "cli-auth" / "claude" / "projects" / "-data"
  _write(project / ".gone.jsonl.released-abc", 600, age_days=5)
  _write(project / ".gone.released-abc" / "tool-results" / "r.txt", 200, age_days=5)

  result = _clear_sessions(monkeypatch, tmp_path)

  assert result["bytes"] >= 800
  assert list(project.iterdir()) == []


def test_old_session_clear_never_follows_a_linked_project_outside_claude(tmp_path, monkeypatch):
  outside = tmp_path / "elsewhere"
  _write(outside / "keep.jsonl", 100, age_days=30)
  projects = tmp_path / "cli-auth" / "claude" / "projects"
  projects.mkdir(parents=True)
  (projects / "linked").symlink_to(outside, target_is_directory=True)

  assert stale_session_usage(tmp_path, max_age_days=2)["sessions"] == 0
  _clear_sessions(monkeypatch, tmp_path)

  assert (outside / "keep.jsonl").exists()


# --- chat work files ---------------------------------------------------------


def test_work_files_preview_counts_only_idle_chats_with_no_recent_change(monkeypatch, tmp_path):
  monkeypatch.setattr(
    config, "get_settings", lambda: type("S", (), {"data_dir": str(tmp_path)})(),
  )
  root = agent_scratch.agent_scratch_root()
  idle, running, recent = (str(uuid.uuid4()) for _ in range(3))
  _write(root / idle / "big.bin", 4096, age_days=1)
  _age(root / idle, 1)
  _write(root / running / "big.bin", 2048, age_days=1)
  _age(root / running, 1)
  _write(root / recent / "new.bin", 1024)
  monkeypatch.setattr(agent_scratch, "has_running_run", lambda _db, _chat_id: False)
  monkeypatch.setattr(
    agent_scratch, "browser_session_targets_for_chat",
    lambda _chat_id: type("Scan", (), {"idle": True})(),
  )

  usage = agent_scratch.idle_scratch_usage(idle_seconds=15 * 60, alive_chat_ids={running})

  assert usage == {"bytes": 4096, "chats": 1, "kept_chats": 2}


# --- agent browsers ----------------------------------------------------------


def _ago(minutes):
  return datetime.now(UTC).replace(tzinfo=None) - timedelta(minutes=minutes)


def test_browser_clear_deletes_exactly_the_previewed_closed_chat_profiles(tmp_path, monkeypatch):
  monkeypatch.setattr("app.browser_profiles._active_profile_names", lambda _root: set())
  closed, running, recent, started = (str(uuid.uuid4()) for _ in range(4))
  root = tmp_path / "agent-browser-profiles"
  for chat_id, size in ((closed, 3000), (running, 2000), (recent, 500), (started, 700)):
    _write(root / f"chat-{chat_id}" / "Default" / "Cookies", size)
  _write(root / "reflection" / "Default" / "Cookies", 1000)
  chats = {
    closed: {"running": False, "activity_at": _ago(120)},
    running: {"running": True, "activity_at": _ago(120)},
    recent: {"running": False, "activity_at": _ago(5)},
    started: {"running": False, "activity_at": _ago(120)},
  }

  preview = closed_chat_profile_usage(tmp_path, chats, set())
  # ``started`` begins a turn after the preview, just before the clear reaches it.
  result = clear_closed_chat_profiles(
    tmp_path, chats, set(), still_in_use=lambda chat_id: chat_id == started,
  )

  assert preview == {"bytes": 3700, "profiles": 2}
  assert result == {"bytes": 3000, "profiles": 1}
  assert not (root / f"chat-{closed}").exists()
  for kept in (running, recent, started):
    assert (root / f"chat-{kept}").exists()
  assert (root / "reflection").exists()


# --- overview ----------------------------------------------------------------


def _status(*, free, total, disk_state, critical_below=200, memory_state="normal"):
  return {
    "facts": {
      "disk": {"available": True, "free_bytes": free, "total_bytes": total},
      "memory": {"working_set_bytes": 1000, "limit_bytes": 4000},
    },
    "pressure": {
      "disk": {"state": disk_state, "critical_below_bytes": critical_below},
      "memory": {"state": memory_state, "critical_at_ratio": 0.9},
    },
  }


def test_storage_level_is_shown_only_when_the_owner_should_act():
  gib = 1024**3
  healthy = resource_overview._storage(_status(free=40 * gib, total=50 * gib, disk_state="normal"))
  getting_low = resource_overview._storage(_status(free=4 * gib, total=50 * gib, disk_state="normal"))
  low = resource_overview._storage(_status(free=gib, total=50 * gib, disk_state="constrained"))
  paused = resource_overview._storage(_status(free=gib // 2, total=50 * gib, disk_state="critical"))

  assert healthy["level"] is None
  assert getting_low["level"] == "getting_low"
  assert low["level"] == "low"
  assert paused["level"] == "paused"


def test_overview_groups_cleanable_folders_and_names_kept_ones_plainly(monkeypatch):
  monkeypatch.setattr(resource_overview, "latest_snapshot", lambda: {
    "domains_captured_at": "2026-10-26T15:00:00+00:00",
    "domains": {
      "agent_scratch": {"bytes": 900},
      "provider_telemetry_claude": {"bytes": 800},
      "provider_telemetry_codex": {"bytes": 50},
      "browser_profiles": {"bytes": 100},
      "database": {"bytes": 500},
      "logs": {"bytes": 0},
    },
  })

  usage = resource_overview._usage(resource_overview.latest_snapshot())

  assert [(row["target"], row["bytes"]) for row in usage["cleanable"]] == [
    ("work_files", 900), ("agent_sessions", 850), ("agent_browsers", 100),
  ]
  assert usage["kept"] == [
    {"id": "database", "label": "Database", "bytes": 500, "incomplete": False},
  ]
  assert usage["measured_at"] == "2026-10-26T15:00:00+00:00"


def test_unmeasured_and_partly_measured_folders_are_flagged_not_shown_as_small():
  before_first_pass = resource_overview._usage({})
  assert [row["bytes"] for row in before_first_pass["cleanable"]] == [None, None, None]
  assert before_first_pass["kept"] == [] and before_first_pass["measured_at"] is None

  partial = resource_overview._usage({"domains": {
    "agent_scratch": {"bytes": 0, "truncated": True},
    "chats": {"bytes": 0, "timed_out": True, "truncated": True},
  }})
  work_files = next(row for row in partial["cleanable"] if row["target"] == "work_files")
  assert work_files["incomplete"] is True
  assert partial["kept"] == [
    {"id": "chats", "label": "Chats and uploads", "bytes": 0, "incomplete": True},
  ]


def test_running_turns_count_typed_queue_and_every_helper_stop_would_cancel(monkeypatch):
  import app.delegations as delegations

  lead = type("Row", (), {"id": "lead", "title": "Lead", "pending_messages": [
    {"content": "typed"}, {"content": "helper result", "hidden": True},
  ]})()
  helpers = [
    type("D", (), {"parent_chat_id": "lead", "status": status})()
    for status in ("running", "paused", "completed")
  ]

  class Query:
    def __init__(self, rows):
      self.rows = rows

    def filter(self, *_args):
      return self

    order_by = filter

    def all(self):
      return self.rows

  class Db:
    def query(self, *columns):
      return Query(helpers if columns[0] is models.Delegation else [lead])

  monkeypatch.setattr(delegations, "derived_status", lambda _db, row, **_k: (row.status, None, None))

  assert resource_overview._running_turns(Db(), {"lead"}) == [
    {"chat_id": "lead", "title": "Lead", "queued": 1, "helpers": 2},
  ]


# --- capacity cache ----------------------------------------------------------


def test_cheap_ticks_keep_the_last_folder_sizes_without_changing_the_tick(monkeypatch):
  monkeypatch.setattr(capacity, "_LATEST_SNAPSHOT", None)
  capacity.set_latest_snapshot({"captured_at": "t1", "domains": {"chats": {"bytes": 5}},
                                "domains_captured_at": "t1"})
  tick = {"captured_at": "t2"}

  capacity.set_latest_snapshot(tick)

  assert tick == {"captured_at": "t2"}
  cached = capacity.latest_snapshot()
  assert cached["domains"] == {"chats": {"bytes": 5}} and cached["domains_captured_at"] == "t1"


def test_remeasure_after_a_cleanup_falls_back_when_du_fails(monkeypatch, tmp_path):
  _write(tmp_path / "agent-scratch" / "a.bin", 2048)
  monkeypatch.setattr(capacity, "_LATEST_SNAPSHOT", {
    "domains": {"agent_scratch": {"bytes": 9999}, "chats": {"bytes": 7}},
    "domains_captured_at": "t1",
  })
  monkeypatch.setattr(capacity, "capacity_snapshot", lambda *_a, **_k: {"captured_at": "t2"})

  def no_du(_paths):
    raise subprocess.SubprocessError("du missing")

  monkeypatch.setattr(capacity, "_du_domain_sizes", no_du)

  snapshot = capacity.remeasure_domains(tmp_path, ["agent_scratch"])

  assert 2048 <= snapshot["domains"]["agent_scratch"]["bytes"] < 9999
  assert snapshot["domains"]["chats"] == {"bytes": 7}


def test_resources_route_requires_the_owner_and_rejects_unknown_cleanups(client, auth):
  assert client.get("/api/resources").status_code == 401
  body = client.get("/api/resources", headers=auth).json()
  assert {"storage", "memory", "cleanable", "kept", "running", "measured_at"} <= set(body)
  assert client.get("/api/resources/cleanup/database", headers=auth).status_code == 404
  assert client.post("/api/resources/cleanup/database", headers=auth).status_code == 404


def test_resources_routes_refuse_app_tokens_and_cross_site_posts(client, auth):
  created = create_local_app(client, auth, name="resource-reader", description="test")
  app_token = client.post(
    "/api/auth/app-token", json={"app_id": created["id"]}, headers=auth,
  ).json()["token"]
  app_auth = {"Authorization": f"Bearer {app_token}"}

  assert client.get("/api/resources", headers=app_auth).status_code in (401, 403)
  assert client.post("/api/resources/cleanup/work_files", headers=app_auth).status_code in (401, 403)
  cross_site = client.post(
    "/api/resources/cleanup/work_files", headers={**auth, "Sec-Fetch-Site": "cross-site"},
  )
  assert cross_site.status_code == 403


def test_a_second_cleanup_while_one_runs_is_refused_not_overlapped():
  async def hold_and_request():
    async with resource_cleanup._CLEANUP_LOCK:
      try:
        await resource_cleanup.run_cleanup("work_files", "/nonexistent")
      except resource_cleanup.CleanupBusy:
        return "busy"
    return "ran"

  assert asyncio.run(hold_and_request()) == "busy"
