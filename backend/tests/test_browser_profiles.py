import asyncio
from datetime import UTC, datetime, timedelta

from app import browser_profiles, chat
from app.browser_profiles import enforce_browser_profile_quota


def _profile(root, chat_id, *, cache_bytes, durable_bytes):
  profile = root / "agent-browser-profiles" / f"chat-{chat_id}"
  cache = profile / "Default" / "Cache"
  cache.mkdir(parents=True)
  (cache / "cache.bin").write_bytes(b"c" * cache_bytes)
  durable = profile / "Default" / "IndexedDB"
  durable.mkdir(parents=True)
  (durable / "state.bin").write_bytes(b"d" * durable_bytes)
  return profile


def _named_profile(root, name, *, cache_bytes, durable_bytes):
  profile = root / "agent-browser-profiles" / name
  cache = profile / "Default" / "Cache"
  cache.mkdir(parents=True)
  (cache / "cache.bin").write_bytes(b"c" * cache_bytes)
  durable = profile / "Default" / "IndexedDB"
  durable.mkdir(parents=True)
  (durable / "state.bin").write_bytes(b"d" * durable_bytes)
  return profile


def test_railway_defaults_fit_below_managed_volume_limit(monkeypatch):
  for name in (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
  ):
    monkeypatch.delenv(name, raising=False)
  assert browser_profiles.default_browser_profile_quota() == (
    2 * 1024**3, 1536 * 1024**2,
  )

  monkeypatch.setenv("RAILWAY_PROJECT_ID", "project-test")
  assert browser_profiles.default_browser_profile_quota() == (
    128 * 1024**2, 96 * 1024**2,
  )


def test_profile_sweep_interval_is_hourly_and_bounded(monkeypatch):
  monkeypatch.delenv("AGENT_BROWSER_PROFILE_SWEEP_SECONDS", raising=False)
  assert browser_profiles.browser_profile_sweep_seconds() == 3600
  monkeypatch.setenv("AGENT_BROWSER_PROFILE_SWEEP_SECONDS", "10")
  assert browser_profiles.browser_profile_sweep_seconds() == 60
  monkeypatch.setenv("AGENT_BROWSER_PROFILE_SWEEP_SECONDS", "7200")
  assert browser_profiles.browser_profile_sweep_seconds() == 7200


def test_browser_sessions_for_chat_attributes_custom_detached_session(tmp_path):
  proc = tmp_path / "proc"
  matching = proc / "101"
  matching.mkdir(parents=True)
  (matching / "cmdline").write_bytes(
    b"/usr/local/lib/agent-browser-linux-x64\0"
  )
  (matching / "environ").write_bytes(
    b"CHAT_ID=chat-a\0AGENT_BROWSER_SESSION=custom-preview-3\0"
  )

  foreign = proc / "102"
  foreign.mkdir()
  (foreign / "cmdline").write_bytes(b"/opt/agent-browser-linux-x64\0")
  (foreign / "environ").write_bytes(
    b"CHAT_ID=chat-b\0AGENT_BROWSER_SESSION=foreign-preview\0"
  )

  unrelated = proc / "103"
  unrelated.mkdir()
  (unrelated / "cmdline").write_bytes(b"/usr/bin/python3\0")
  (unrelated / "environ").write_bytes(
    b"CHAT_ID=chat-a\0AGENT_BROWSER_SESSION=not-a-browser\0"
  )

  assert browser_profiles.browser_sessions_for_chat(
    "chat-a", proc_root=proc,
  ) == {"custom-preview-3"}


def test_terminal_browser_cleanup_closes_inherited_and_custom_sessions(
  monkeypatch,
):
  monkeypatch.setattr(
    browser_profiles,
    "browser_sessions_for_chat",
    lambda _chat_id: {"custom-preview-3"},
  )
  calls = []

  class FakeProcess:
    async def wait(self):
      return 0

  async def fake_create_subprocess_exec(*args, **_kwargs):
    calls.append(args)
    return FakeProcess()

  monkeypatch.setattr(
    chat.asyncio,
    "create_subprocess_exec",
    fake_create_subprocess_exec,
  )

  asyncio.run(chat._close_browser_session("chat-a"))

  assert calls == [
    ("agent-browser", "--session", "chat-chat-a", "close"),
    ("agent-browser", "--session", "custom-preview-3", "close"),
  ]


def test_quota_prunes_regenerable_cache_before_profile(tmp_path):
  old = "11111111-1111-1111-1111-111111111111"
  profile = _profile(tmp_path, old, cache_bytes=80, durable_bytes=20)
  now = datetime.now(UTC).replace(tzinfo=None)
  result = enforce_browser_profile_quota(
    tmp_path,
    {old: {"activity_at": now - timedelta(days=40),
           "deleted_at": None, "run_status": None}},
    set(),
    now=now,
    max_bytes=90,
    low_water_bytes=20,
    inactive_days=30,
  )

  assert profile.exists()
  assert not (profile / "Default" / "Cache").exists()
  assert (profile / "Default" / "IndexedDB" / "state.bin").exists()
  assert result["cache_dirs_pruned"] == 1
  assert result["profiles_pruned"] == 0


def test_quota_never_prunes_a_live_chat_profile(tmp_path):
  live = "22222222-2222-2222-2222-222222222222"
  profile = _profile(tmp_path, live, cache_bytes=100, durable_bytes=20)
  now = datetime.now(UTC).replace(tzinfo=None)
  result = enforce_browser_profile_quota(
    tmp_path,
    {live: {"activity_at": now - timedelta(days=90),
            "deleted_at": None, "run_status": "running"}},
    {live},
    now=now,
    max_bytes=1,
    low_water_bytes=0,
    inactive_days=30,
  )

  assert profile.exists()
  assert result["reclaimed_bytes"] == 0


def test_quota_prunes_recent_closed_cache_before_old_durable_profile(tmp_path):
  recent = "33333333-3333-3333-3333-333333333333"
  old = "44444444-4444-4444-4444-444444444444"
  recent_profile = _profile(tmp_path, recent, cache_bytes=80, durable_bytes=20)
  old_profile = _profile(tmp_path, old, cache_bytes=0, durable_bytes=20)
  now = datetime.now(UTC).replace(tzinfo=None)

  result = enforce_browser_profile_quota(
    tmp_path,
    {
      recent: {"activity_at": now - timedelta(days=1),
               "deleted_at": None, "run_status": None},
      old: {"activity_at": now - timedelta(days=90),
            "deleted_at": None, "run_status": None},
    },
    set(),
    now=now,
    max_bytes=100,
    low_water_bytes=40,
    inactive_days=30,
  )

  assert recent_profile.exists()
  assert not (recent_profile / "Default" / "Cache").exists()
  assert old_profile.exists()
  # The old profile's empty Cache directory is removed too; both operations
  # are safe, while only the recent cache contributes reclaimed bytes.
  assert result["cache_dirs_pruned"] == 2
  assert result["profiles_pruned"] == 0


def test_quota_counts_and_prunes_cache_from_closed_named_profile(tmp_path):
  profile = _named_profile(
    tmp_path, "reflection", cache_bytes=100, durable_bytes=20,
  )
  now = datetime.now(UTC).replace(tzinfo=None)

  result = enforce_browser_profile_quota(
    tmp_path, {}, set(), now=now, max_bytes=90, low_water_bytes=20,
    inactive_days=30, active_profile_names=set(),
  )

  assert profile.exists()
  assert not (profile / "Default" / "Cache").exists()
  assert (profile / "Default" / "IndexedDB" / "state.bin").exists()
  assert result["profile_count"] == 1
  assert result["non_chat_profile_count"] == 1
  assert result["cache_dirs_pruned"] == 1


def test_quota_never_prunes_live_named_profile(tmp_path):
  profile = _named_profile(
    tmp_path, "reflection", cache_bytes=100, durable_bytes=20,
  )
  now = datetime.now(UTC).replace(tzinfo=None)

  result = enforce_browser_profile_quota(
    tmp_path, {}, set(), now=now, max_bytes=1, low_water_bytes=0,
    inactive_days=0, active_profile_names={"reflection"},
  )

  assert profile.exists()
  assert (profile / "Default" / "Cache").exists()
  assert result["reclaimed_bytes"] == 0


def test_quota_ignores_symlinked_profile_directory(tmp_path):
  root = tmp_path / "agent-browser-profiles"
  root.mkdir()
  outside = tmp_path / "outside"
  outside.mkdir()
  (outside / "state.bin").write_bytes(b"x" * 100)
  (root / "linked").symlink_to(outside, target_is_directory=True)

  result = enforce_browser_profile_quota(
    tmp_path, {}, set(), max_bytes=1, low_water_bytes=0,
    inactive_days=0, active_profile_names=set(),
  )

  assert outside.exists()
  assert (outside / "state.bin").exists()
  assert result["profile_count"] == 0


def test_quota_ignores_symlinked_cache_directory(tmp_path):
  profile = tmp_path / "agent-browser-profiles" / "reflection"
  profile.mkdir(parents=True)
  outside_cache = tmp_path / "outside-cache"
  outside_cache.mkdir()
  (outside_cache / "cache.bin").write_bytes(b"c" * 100)
  default = profile / "Default"
  default.mkdir()
  (default / "Cache").symlink_to(outside_cache, target_is_directory=True)
  durable = default / "IndexedDB"
  durable.mkdir()
  (durable / "state.bin").write_bytes(b"d" * 20)

  result = enforce_browser_profile_quota(
    tmp_path, {}, set(), max_bytes=1, low_water_bytes=0,
    inactive_days=30, active_profile_names=set(),
  )

  assert outside_cache.exists()
  assert (outside_cache / "cache.bin").exists()
  assert (default / "Cache").is_symlink()
  assert result["cache_dirs_pruned"] == 0
