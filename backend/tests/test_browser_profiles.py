from datetime import UTC, datetime, timedelta

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
