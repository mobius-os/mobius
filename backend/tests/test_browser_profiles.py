import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

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


def test_profile_defaults_follow_data_capacity_not_provider(monkeypatch, tmp_path):
  monkeypatch.setattr(
    browser_profiles.shutil,
    "disk_usage",
    lambda path: SimpleNamespace(total=512 * 1024**2),
  )
  expected = (128 * 1024**2, 96 * 1024**2)
  assert browser_profiles.default_browser_profile_quota(tmp_path) == expected
  for name in (
    "RAILWAY_ENVIRONMENT",
    "RAILWAY_ENVIRONMENT_ID",
    "RAILWAY_PROJECT_ID",
    "RAILWAY_SERVICE_ID",
  ):
    monkeypatch.setenv(name, "railway-test")
  assert browser_profiles.default_browser_profile_quota(tmp_path) == expected


def test_profile_defaults_are_capped_and_fall_back_on_probe_failure(
  monkeypatch, tmp_path,
):
  monkeypatch.setattr(
    browser_profiles.shutil,
    "disk_usage",
    lambda path: SimpleNamespace(total=16 * 1024**3),
  )
  assert browser_profiles.default_browser_profile_quota(tmp_path) == (
    2 * 1024**3, 1536 * 1024**2,
  )

  def fail(_path):
    raise OSError("unavailable")

  monkeypatch.setattr(browser_profiles.shutil, "disk_usage", fail)
  assert browser_profiles.default_browser_profile_quota(tmp_path) == (
    2 * 1024**3, 1536 * 1024**2,
  )

  monkeypatch.setattr(
    browser_profiles.shutil,
    "disk_usage",
    lambda path: SimpleNamespace(total=0),
  )
  assert browser_profiles.default_browser_profile_quota(tmp_path) == (
    2 * 1024**3, 1536 * 1024**2,
  )


def test_profile_quota_environment_overrides_capacity(monkeypatch, tmp_path):
  monkeypatch.setattr(
    browser_profiles.shutil,
    "disk_usage",
    lambda path: SimpleNamespace(total=512 * 1024**2),
  )
  monkeypatch.setenv("AGENT_BROWSER_PROFILE_MAX_BYTES", "42")
  monkeypatch.setenv("AGENT_BROWSER_PROFILE_LOW_WATER_BYTES", "31")

  result = enforce_browser_profile_quota(tmp_path, {}, set())

  assert result["max_bytes"] == 42
  assert result["low_water_bytes"] == 31


def test_profile_sweep_interval_is_hourly_and_bounded(monkeypatch):
  monkeypatch.delenv("AGENT_BROWSER_PROFILE_SWEEP_SECONDS", raising=False)
  assert browser_profiles.browser_profile_sweep_seconds() == 3600
  monkeypatch.setenv("AGENT_BROWSER_PROFILE_SWEEP_SECONDS", "10")
  assert browser_profiles.browser_profile_sweep_seconds() == 60
  monkeypatch.setenv("AGENT_BROWSER_PROFILE_SWEEP_SECONDS", "7200")
  assert browser_profiles.browser_profile_sweep_seconds() == 7200


def _fake_browser_process(
  proc, pid, *, chat_id, session, namespace=None, socket_dir=None,
  executable="agent-browser-linux-x64",
):
  process = proc / str(pid)
  process.mkdir(parents=True)
  (process / "cmdline").write_bytes(
    f"/usr/local/lib/{executable}\0".encode()
  )
  values = {
    "CHAT_ID": chat_id,
    "AGENT_BROWSER_SESSION": session,
  }
  if namespace is not None:
    values["AGENT_BROWSER_NAMESPACE"] = namespace
  if socket_dir is not None:
    values["AGENT_BROWSER_SOCKET_DIR"] = socket_dir
  (process / "environ").write_bytes("".join(
    f"{key}={value}\0" for key, value in values.items()
  ).encode())


def test_browser_sessions_for_chat_preserves_opaque_session_values(tmp_path):
  proc = tmp_path / "proc"
  long_name = "preview-" + ("x" * 256)
  _fake_browser_process(
    proc, 101, chat_id="chat-a", session="custom:colon",
  )
  _fake_browser_process(
    proc, 102, chat_id="chat-a", session="unicode-ø-世界",
  )
  _fake_browser_process(proc, 103, chat_id="chat-a", session=long_name)
  _fake_browser_process(
    proc, 106, chat_id="chat-a", session="../escape",
    namespace="custom/ns", socket_dir="/tmp/ab-sockets",
  )
  _fake_browser_process(proc, 107, chat_id="chat-a", session="-x")
  _fake_browser_process(
    proc, 108, chat_id="chat-a", session="arm-preview",
    executable="agent-browser-linux-arm64",
  )
  _fake_browser_process(
    proc, 109, chat_id="chat-a", session="lookalike",
    executable="agent-browser-linux-x64-wrapper",
  )

  foreign = proc / "104"
  foreign.mkdir()
  (foreign / "cmdline").write_bytes(b"/opt/agent-browser-linux-x64\0")
  (foreign / "environ").write_bytes(
    b"CHAT_ID=chat-b\0AGENT_BROWSER_SESSION=foreign-preview\0"
  )

  unrelated = proc / "105"
  unrelated.mkdir()
  (unrelated / "cmdline").write_bytes(b"/usr/bin/python3\0")
  (unrelated / "environ").write_bytes(
    b"CHAT_ID=chat-a\0AGENT_BROWSER_SESSION=not-a-browser\0"
  )

  scan = browser_profiles.browser_session_targets_for_chat(
    "chat-a", proc_root=proc,
  )
  assert scan.complete is True
  assert scan.targets == frozenset({
    browser_profiles.BrowserSessionTarget(session="custom:colon"),
    browser_profiles.BrowserSessionTarget(session="unicode-ø-世界"),
    browser_profiles.BrowserSessionTarget(session=long_name),
    browser_profiles.BrowserSessionTarget(
      session="../escape",
      namespace="custom/ns",
      socket_dir="/tmp/ab-sockets",
    ),
    browser_profiles.BrowserSessionTarget(session="-x"),
    browser_profiles.BrowserSessionTarget(session="arm-preview"),
  })


def test_terminal_browser_cleanup_closes_inherited_and_custom_sessions(
  monkeypatch,
):
  long_name = "preview-" + ("x" * 256)
  monkeypatch.setenv("AGENT_BROWSER_NAMESPACE", "stale-parent-namespace")
  monkeypatch.setenv("AGENT_BROWSER_SOCKET_DIR", "/stale/parent/socket-dir")
  monkeypatch.setattr(
    browser_profiles,
    "browser_session_targets_for_chat",
    lambda _chat_id: browser_profiles.BrowserSessionScan(frozenset({
      browser_profiles.BrowserSessionTarget(session="custom:colon"),
      browser_profiles.BrowserSessionTarget(session="unicode-ø-世界"),
      browser_profiles.BrowserSessionTarget(session=long_name),
      browser_profiles.BrowserSessionTarget(
        session="../escape",
        namespace="custom/ns",
        socket_dir="/tmp/ab-sockets",
      ),
      browser_profiles.BrowserSessionTarget(session="-x"),
    }), True),
  )
  calls = []

  class FakeProcess:
    async def wait(self):
      return 0

  async def fake_create_subprocess_exec(*args, **kwargs):
    calls.append((args, kwargs["env"]))
    return FakeProcess()

  monkeypatch.setattr(
    chat.asyncio,
    "create_subprocess_exec",
    fake_create_subprocess_exec,
  )

  asyncio.run(chat._close_browser_session("chat-a"))

  assert all(args == ("agent-browser", "close") for args, _env in calls)
  routes = {
    env["AGENT_BROWSER_SESSION"]: env
    for _args, env in calls
  }
  assert set(routes) == {
    "chat-chat-a", "custom:colon", "unicode-ø-世界", long_name,
    "../escape", "-x",
  }
  assert "AGENT_BROWSER_NAMESPACE" not in routes["chat-chat-a"]
  assert "AGENT_BROWSER_SOCKET_DIR" not in routes["chat-chat-a"]
  assert routes["../escape"]["AGENT_BROWSER_NAMESPACE"] == "custom/ns"
  assert routes["../escape"]["AGENT_BROWSER_SOCKET_DIR"] == "/tmp/ab-sockets"


def test_terminal_browser_cleanup_kills_timed_out_close_process(monkeypatch):
  monkeypatch.setattr(
    browser_profiles, "browser_session_targets_for_chat",
    lambda _chat_id: browser_profiles.BrowserSessionScan(frozenset(), True),
  )
  monkeypatch.setattr(chat, "_BROWSER_CLOSE_WAIT_TIMEOUT", 0.01)
  monkeypatch.setattr(chat, "_BROWSER_CLOSE_KILL_GRACE", 0.01)

  class WedgedProcess:
    returncode = None

    def __init__(self):
      self.terminate_calls = 0
      self.kill_calls = 0
      self.wait_calls = 0

    async def wait(self):
      self.wait_calls += 1
      if self.kill_calls:
        self.returncode = -9
        return self.returncode
      await asyncio.Future()

    def terminate(self):
      self.terminate_calls += 1

    def kill(self):
      self.kill_calls += 1

  proc = WedgedProcess()

  async def fake_create_subprocess_exec(*_args, **_kwargs):
    return proc

  monkeypatch.setattr(
    chat.asyncio, "create_subprocess_exec", fake_create_subprocess_exec,
  )

  asyncio.run(chat._close_browser_session("chat-a"))

  assert proc.terminate_calls == 1
  assert proc.kill_calls == 1
  assert proc.wait_calls == 3
  assert proc.returncode == -9


def test_terminal_browser_cleanup_bounds_wait_after_sigkill(monkeypatch, caplog):
  monkeypatch.setattr(
    browser_profiles, "browser_session_targets_for_chat",
    lambda _chat_id: browser_profiles.BrowserSessionScan(frozenset(), True),
  )
  monkeypatch.setattr(chat, "_BROWSER_CLOSE_WAIT_TIMEOUT", 0.01)
  monkeypatch.setattr(chat, "_BROWSER_CLOSE_KILL_GRACE", 0.01)
  monkeypatch.setattr(chat, "_BROWSER_CLOSE_KILL_WAIT_TIMEOUT", 0.01)

  class NeverReapedProcess:
    returncode = None

    def __init__(self):
      self.terminate_calls = 0
      self.kill_calls = 0
      self.wait_calls = 0

    async def wait(self):
      self.wait_calls += 1
      await asyncio.Future()

    def terminate(self):
      self.terminate_calls += 1

    def kill(self):
      self.kill_calls += 1

  proc = NeverReapedProcess()

  async def fake_create_subprocess_exec(*_args, **_kwargs):
    return proc

  monkeypatch.setattr(
    chat.asyncio, "create_subprocess_exec", fake_create_subprocess_exec,
  )

  asyncio.run(asyncio.wait_for(
    chat._close_browser_session("chat-a"), timeout=0.5,
  ))

  assert proc.terminate_calls == 1
  assert proc.kill_calls == 1
  assert proc.wait_calls == 3
  assert "did not reap after SIGKILL" in caplog.text


def test_terminal_browser_cleanup_bounds_wait_when_process_disappears_before_term(
  monkeypatch, caplog,
):
  monkeypatch.setattr(
    browser_profiles, "browser_session_targets_for_chat",
    lambda _chat_id: browser_profiles.BrowserSessionScan(frozenset(), True),
  )
  monkeypatch.setattr(chat, "_BROWSER_CLOSE_WAIT_TIMEOUT", 0.01)
  monkeypatch.setattr(chat, "_BROWSER_CLOSE_KILL_WAIT_TIMEOUT", 0.01)

  class GoneWithWedgedWatcher:
    returncode = None

    def __init__(self):
      self.terminate_calls = 0
      self.kill_calls = 0
      self.wait_calls = 0

    async def wait(self):
      self.wait_calls += 1
      await asyncio.Future()

    def terminate(self):
      self.terminate_calls += 1
      raise ProcessLookupError

    def kill(self):
      self.kill_calls += 1

  proc = GoneWithWedgedWatcher()

  async def fake_create_subprocess_exec(*_args, **_kwargs):
    return proc

  monkeypatch.setattr(
    chat.asyncio, "create_subprocess_exec", fake_create_subprocess_exec,
  )

  asyncio.run(asyncio.wait_for(
    chat._close_browser_session("chat-a"), timeout=0.5,
  ))

  assert proc.wait_calls == 2
  assert proc.terminate_calls == 1
  assert proc.kill_calls == 0
  assert "did not reap after disappearing before SIGTERM" in caplog.text


def test_quota_prunes_regenerable_cache_before_profile(tmp_path):
  old = "11111111-1111-1111-1111-111111111111"
  profile = _profile(tmp_path, old, cache_bytes=80, durable_bytes=20)
  now = datetime.now(UTC).replace(tzinfo=None)
  result = enforce_browser_profile_quota(
    tmp_path,
    {old: {"activity_at": now - timedelta(days=40),
           "deleted_at": None, "running": False}},
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
            "deleted_at": None, "running": True}},
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
               "deleted_at": None, "running": False},
      old: {"activity_at": now - timedelta(days=90),
            "deleted_at": None, "running": False},
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


def test_quota_retires_oldest_recent_profile_when_grace_exceeds_budget(
  tmp_path,
):
  older = "55555555-5555-5555-5555-555555555555"
  newer = "66666666-6666-6666-6666-666666666666"
  older_profile = _profile(
    tmp_path, older, cache_bytes=0, durable_bytes=40,
  )
  newer_profile = _profile(
    tmp_path, newer, cache_bytes=0, durable_bytes=70,
  )
  now = datetime.now(UTC).replace(tzinfo=None)

  result = enforce_browser_profile_quota(
    tmp_path,
    {
      older: {"activity_at": now - timedelta(days=2),
              "deleted_at": None, "running": False},
      newer: {"activity_at": now - timedelta(days=1),
              "deleted_at": None, "running": False},
    },
    set(),
    now=now,
    max_bytes=100,
    low_water_bytes=70,
    inactive_days=30,
  )

  assert not older_profile.exists()
  assert newer_profile.exists()
  assert result["profiles_pruned"] == 1
  assert result["bytes_after"] == 70
  assert result["over_quota_bytes"] == 0


def test_triggered_quota_finishes_at_low_water_after_partial_cache_reclaim(
  tmp_path,
):
  older = "88888888-8888-8888-8888-888888888888"
  newer = "99999999-9999-9999-9999-999999999999"
  older_profile = _profile(
    tmp_path, older, cache_bytes=20, durable_bytes=30,
  )
  newer_profile = _profile(
    tmp_path, newer, cache_bytes=0, durable_bytes=60,
  )
  now = datetime.now(UTC).replace(tzinfo=None)

  result = enforce_browser_profile_quota(
    tmp_path,
    {
      older: {"activity_at": now - timedelta(days=2),
              "deleted_at": None, "running": False},
      newer: {"activity_at": now - timedelta(days=1),
              "deleted_at": None, "running": False},
    },
    set(),
    now=now,
    max_bytes=100,
    low_water_bytes=70,
    inactive_days=30,
  )

  # Cache pruning lowers 110 to 90, below the high-water ceiling but above the
  # low-water target. The already-triggered sweep completes the cycle instead
  # of leaving the next small write to retrigger it.
  assert not older_profile.exists()
  assert newer_profile.exists()
  assert result["cache_dirs_pruned"] == 2
  assert result["profiles_pruned"] == 1
  assert result["bytes_after"] == 60


def test_quota_reports_when_live_profiles_alone_exceed_the_budget(tmp_path):
  live = "77777777-7777-7777-7777-777777777777"
  profile = _profile(tmp_path, live, cache_bytes=80, durable_bytes=20)
  now = datetime.now(UTC).replace(tzinfo=None)

  result = enforce_browser_profile_quota(
    tmp_path,
    {live: {"activity_at": now, "deleted_at": None,
            "running": True}},
    {live},
    now=now,
    max_bytes=50,
    low_water_bytes=25,
    inactive_days=30,
  )

  assert profile.exists()
  assert result["reclaimed_bytes"] == 0
  assert result["bytes_after"] == 100
  assert result["max_bytes"] == 50
  assert result["low_water_bytes"] == 25
  assert result["over_quota_bytes"] == 50


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


def test_quota_preserves_recent_inactive_named_profile_under_pressure(tmp_path):
  profile = _named_profile(
    tmp_path, "reflection", cache_bytes=0, durable_bytes=20,
  )
  now = datetime.now(UTC).replace(tzinfo=None)

  result = enforce_browser_profile_quota(
    tmp_path, {}, set(), now=now, max_bytes=10, low_water_bytes=0,
    inactive_days=30, active_profile_names=set(),
  )

  assert profile.exists()
  assert result["profiles_pruned"] == 0
  assert result["bytes_after"] == 20
  assert result["over_quota_bytes"] == 10


def test_quota_prunes_named_profile_after_its_full_grace(tmp_path):
  profile = _named_profile(
    tmp_path, "reflection", cache_bytes=0, durable_bytes=20,
  )
  now = datetime.now(UTC).replace(tzinfo=None) + timedelta(days=31)

  result = enforce_browser_profile_quota(
    tmp_path, {}, set(), now=now, max_bytes=10, low_water_bytes=0,
    inactive_days=30, active_profile_names=set(),
  )

  assert not profile.exists()
  assert result["profiles_pruned"] == 1
  assert result["bytes_after"] == 0
  assert result["over_quota_bytes"] == 0


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
    inactive_days=30, active_profile_names={"reflection"},
  )

  assert outside_cache.exists()
  assert (outside_cache / "cache.bin").exists()
  assert (default / "Cache").is_symlink()
  assert result["cache_dirs_pruned"] == 0
  assert result["over_quota_bytes"] == 19


def test_quota_does_not_count_or_reclaim_symlinked_file_targets(tmp_path):
  profile = tmp_path / "agent-browser-profiles" / "reflection"
  durable = profile / "Default" / "IndexedDB"
  durable.mkdir(parents=True)
  (durable / "state.bin").write_bytes(b"d" * 20)
  outside = tmp_path / "outside.bin"
  outside.write_bytes(b"x" * 100)
  (durable / "outside.bin").symlink_to(outside)

  result = enforce_browser_profile_quota(
    tmp_path, {}, set(), max_bytes=10, low_water_bytes=0,
    inactive_days=30, active_profile_names={"reflection"},
  )

  assert outside.read_bytes() == b"x" * 100
  assert (durable / "outside.bin").is_symlink()
  assert result["bytes_before"] == 20
  assert result["bytes_after"] == 20
  assert result["reclaimed_bytes"] == 0
  assert result["over_quota_bytes"] == 10
