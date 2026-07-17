"""Warm frontend watcher publication behavior."""

import asyncio
import fcntl
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

import app.frontend_watcher as fw


def _write_build(root, marker):
  (root / "assets").mkdir(parents=True, exist_ok=True)
  (root / "index.html").write_text(f"<title>{marker}</title>", encoding="utf-8")
  (root / "sw.js").write_text("// service worker", encoding="utf-8")
  (root / "manifest.webmanifest").write_text("{}", encoding="utf-8")
  (root / "assets" / f"index-{marker}.js").write_text(
    f"// {marker}", encoding="utf-8",
  )


@pytest.fixture
def fw_dirs(tmp_path, monkeypatch):
  frontend = tmp_path / "frontend"
  frontend.mkdir()
  dirs = {
    "frontend": frontend,
    "dist": frontend / "dist",
    "staging": frontend / ".dist-staging",
    "rebuild": frontend / ".dist-rebuild",
    "next": frontend / ".dist-next",
    "old": frontend / ".dist-old",
    "attic": frontend / ".assets-attic",
    "cache": frontend / ".vite-cache",
    "tmp": frontend / ".vite-tmp",
  }
  monkeypatch.setattr(fw, "_FRONTEND_DIR", frontend)
  monkeypatch.setattr(fw, "_DIST_DIR", dirs["dist"])
  monkeypatch.setattr(fw, "_STAGING_DIST_DIR", dirs["staging"])
  monkeypatch.setattr(fw, "_REBUILD_DIST_DIR", dirs["rebuild"])
  monkeypatch.setattr(fw, "_NEXT_DIST_DIR", dirs["next"])
  monkeypatch.setattr(fw, "_OLD_DIST_DIR", dirs["old"])
  monkeypatch.setattr(fw, "_ATTIC_DIR", dirs["attic"])
  monkeypatch.setattr(fw, "_CACHE_DIR", dirs["cache"])
  monkeypatch.setattr(fw, "_TMP_DIR", dirs["tmp"])
  monkeypatch.setattr(
    fw,
    "_BUILT_GLOBAL_CHECK",
    Path(__file__).resolve().parents[2]
    / "frontend" / "scripts" / "check-built-globals.mjs",
  )
  # Keep build comparisons hermetic when the test happens to run in an image
  # that has the production vendor tree mounted at /app/static/vendor.
  monkeypatch.setattr(fw, "_copy_vendor", lambda _dest: None)
  yield dirs
  shutil.rmtree(frontend, ignore_errors=True)


def test_publish_rejects_broken_staging_without_touching_dist(fw_dirs):
  dist, staging, nxt, old = (
    fw_dirs["dist"], fw_dirs["staging"], fw_dirs["next"], fw_dirs["old"]
  )
  _write_build(dist, "old")
  (staging / "assets").mkdir(parents=True)
  (staging / "index.html").write_text("<title>broken</title>",
                                      encoding="utf-8")
  (staging / "manifest.webmanifest").write_text("{}", encoding="utf-8")
  (staging / "assets" / "index-broken.js").write_text(
    "// broken", encoding="utf-8",
  )

  with pytest.raises(RuntimeError, match="sw.js"):
    fw._publish_built_dir(staging, "broken staging")

  assert (dist / "assets" / "index-old.js").is_file()
  assert not (dist / "assets" / "index-broken.js").exists()
  assert not nxt.exists()
  assert not old.exists()


def test_publish_rejects_undeclared_built_identifier_without_touching_dist(
  fw_dirs,
):
  """Vite accepts free identifiers; the publisher must not serve them."""
  dist, staging, nxt, old = (
    fw_dirs["dist"], fw_dirs["staging"], fw_dirs["next"], fw_dirs["old"]
  )
  _write_build(dist, "old")
  _write_build(staging, "broken-global")
  (staging / "assets" / "index-broken-global.js").write_text(
    "export function cleanup() { clearTimeout(ioBounceTimer) }",
    encoding="utf-8",
  )

  with pytest.raises(
    fw._BuiltGlobalValidationError,
    match=r"ioBounceTimer.*index-broken-global\.js",
  ):
    fw._publish_built_dir(staging, "undeclared global")

  assert (dist / "assets" / "index-old.js").is_file()
  assert not (dist / "assets" / "index-broken-global.js").exists()
  assert not nxt.exists()
  assert not old.exists()


def test_publish_rejects_undeclared_identifier_in_root_runtime(fw_dirs):
  staging = fw_dirs["staging"]
  _write_build(staging, "broken-root-global")
  (staging / "mobius-runtime.js").write_text(
    "export function cleanup() { clearTimeout(rootBounceTimer) }",
    encoding="utf-8",
  )

  with pytest.raises(
    fw._BuiltGlobalValidationError,
    match=r"rootBounceTimer.*mobius-runtime\.js",
  ):
    fw._publish_built_dir(staging, "undeclared root global")

  assert not fw_dirs["next"].exists()
  assert not fw_dirs["dist"].exists()


def test_publish_accepts_declared_and_browser_globals(fw_dirs):
  staging = fw_dirs["staging"]
  _write_build(staging, "valid-globals")
  (staging / "assets" / "index-valid-globals.js").write_text(
    "const timer = setTimeout(() => window.fetch(new URL('/ok', location)), 1);"
    " export function cleanup() { clearTimeout(timer) }",
    encoding="utf-8",
  )

  assert fw._publish_built_dir(staging, "valid globals") is True
  assert (fw_dirs["dist"] / "assets" / "index-valid-globals.js").is_file()


@pytest.mark.asyncio
async def test_settle_coalesces_many_staged_builds(monkeypatch):
  calls = []
  events = []
  monkeypatch.setattr(fw, "_DEBOUNCE_SECS", 0.02)
  monkeypatch.setattr(
    fw, "_publish_built_dir",
    lambda source, reason: calls.append((source, reason)) or True,
  )
  monkeypatch.setattr(fw, "_publish_system_event", lambda event: events.append(
    event,
  ))
  handler = fw._FrontendHandler(asyncio.get_running_loop(),
                                start_threads=False)
  try:
    for i in range(10):
      with handler._state_lock:
        handler._staging_dirty = True
      await handler._reschedule_publish(f"build-{i}")
      await asyncio.sleep(0.001)
    for _ in range(20):
      if calls:
        break
      await asyncio.sleep(0.01)
  finally:
    handler.close()

  assert len(calls) == 1
  assert calls[0][0] == fw._STAGING_DIST_DIR
  assert calls[0][1] == "settle:build-9"
  assert events == [{"type": "shell_rebuilt"}]


@pytest.mark.asyncio
async def test_apply_now_forces_immediate_publish(monkeypatch):
  calls = []
  events = []
  monkeypatch.setattr(fw, "_DEBOUNCE_SECS", 60.0)
  monkeypatch.setattr(
    fw, "_publish_built_dir",
    lambda source, reason: calls.append((source, reason)) or True,
  )
  monkeypatch.setattr(fw, "_publish_system_event", lambda event: events.append(
    event,
  ))
  handler = fw._FrontendHandler(asyncio.get_running_loop(),
                                start_threads=False)
  try:
    with handler._state_lock:
      handler._staging_dirty = True
    await handler._reschedule_publish("settled later")

    assert handler.publish_now("shell_apply_now") is True
    await asyncio.sleep(0)
  finally:
    handler.close()

  assert calls == [(fw._STAGING_DIST_DIR, "shell_apply_now")]
  assert events == [{"type": "shell_rebuilt"}]


def test_notify_shell_apply_now_calls_publish_hook(client, auth, monkeypatch):
  calls = []
  monkeypatch.setattr(
    fw,
    "publish_now",
    lambda reason="shell_apply_now": calls.append(reason) or True,
  )

  response = client.post(
    "/api/notify",
    headers=auth,
    json={"type": "shell_apply_now"},
  )

  assert response.status_code == 204, response.text
  assert calls == ["shell_apply_now"]


def test_identical_publish_is_skipped_without_event_or_attic(
  fw_dirs, monkeypatch,
):
  """vite --watch rebuilds staging on every (re)start with fresh mtimes even
  when the output is byte-identical to the served dist. Publishing that would
  reload every idle client per container restart and burn an attic slot per
  boot (repeated no-op restarts would consume the bounded stale-tab window)."""
  events = []
  monkeypatch.setattr(fw, "_publish_system_event", events.append)
  dist, staging, attic = fw_dirs["dist"], fw_dirs["staging"], fw_dirs["attic"]
  _write_build(dist, "same")
  _write_build(staging, "same")

  assert fw._publish_built_dir(staging, "boot rebuild") is False

  assert (dist / "assets" / "index-same.js").is_file()
  assert not fw_dirs["next"].exists()
  assert not (attic.exists() and list(attic.glob("gen-*")))
  assert events == []


def test_identical_publish_skips_the_expensive_global_scan(fw_dirs, monkeypatch):
  _write_build(fw_dirs["dist"], "same")
  _write_build(fw_dirs["staging"], "same")
  monkeypatch.setattr(
    fw,
    "_validate_built_globals",
    lambda _built: pytest.fail("identical output should not be reparsed"),
  )

  assert fw._publish_built_dir(fw_dirs["staging"], "no-op rebuild") is False


def test_publish_failure_restores_dirty_flag(monkeypatch):
  """A transient publish failure must not strand the edit: the dirty flag is
  restored so the next publish_now (shell_apply_now) can retry — without this
  the flag stayed cleared, the poll loop never re-marked it (signature already
  matched), and the last edit was unpublishable until an unrelated change."""
  events = []
  monkeypatch.setattr(fw, "_publish_system_event", events.append)

  def boom(source, reason):
    raise RuntimeError("transient publish failure")

  monkeypatch.setattr(fw, "_publish_built_dir", boom)
  loop = asyncio.new_event_loop()
  try:
    handler = fw._FrontendHandler(loop, start_threads=False)
    with handler._state_lock:
      handler._staging_dirty = True

    assert handler._publish_dirty_sync("settle:edit") is False
    assert handler._staging_dirty is True
    assert events == [
      {"type": "shell_rebuild_failed", "error": "transient publish failure"},
    ]

    monkeypatch.setattr(
      fw, "_publish_built_dir", lambda source, reason: True,
    )
    assert handler._publish_dirty_sync("shell_apply_now") is True
    assert handler._staging_dirty is False
  finally:
    loop.close()


def test_incomplete_watched_build_retries_before_warning(monkeypatch, caplog):
  """A slow Vite build must not look broken during a quiet transform gap."""
  events = []
  retries = []
  monkeypatch.setattr(fw, "_publish_system_event", events.append)
  monkeypatch.setattr(fw, "_INCOMPLETE_GRACE_SECS", 60.0)

  def incomplete(source, reason):
    raise fw._IncompleteBuild("index.html and sw.js are not ready")

  monkeypatch.setattr(fw, "_publish_built_dir", incomplete)
  loop = asyncio.new_event_loop()
  try:
    handler = fw._FrontendHandler(loop, start_threads=False)
    handler._schedule_publish_from_thread = retries.append
    with handler._state_lock:
      handler._staging_dirty = True

    assert handler._publish_dirty_sync("settle:initial build") is False
    assert handler._staging_dirty is True
    assert events == []
    assert retries == ["incomplete staging"]

    # A genuinely stuck generation eventually warns once, while retaining the
    # dirty flag so a later completed Vite output can still publish.
    monkeypatch.setattr(fw, "_INCOMPLETE_GRACE_SECS", 0.0)
    assert handler._publish_dirty_sync("settle:retry") is False
    assert events == [{
      "type": "shell_rebuild_failed",
      "error": "index.html and sw.js are not ready",
    }]
    assert "frontend staging remained incomplete" in caplog.text
    assert "index.html and sw.js are not ready" in caplog.text
    assert handler._publish_dirty_sync("settle:retry again") is False
    assert len(events) == 1

    monkeypatch.setattr(fw, "_publish_built_dir", lambda source, reason: True)
    assert handler._publish_dirty_sync("settle:complete") is True
    assert handler._incomplete_since is None
    assert handler._incomplete_notified is False
  finally:
    loop.close()


def test_vite_demand_build_is_bounded_and_one_shot(fw_dirs, monkeypatch):
  monkeypatch.delenv("NODE_OPTIONS", raising=False)
  env = fw._vite_env()
  cmd = fw._vite_build_cmd(fw_dirs["staging"])

  assert env["MOBIUS_VITE_CACHE"] == str(fw_dirs["cache"])
  assert env["TMPDIR"] == str(fw_dirs["tmp"])
  assert "--max-old-space-size=384" in env["NODE_OPTIONS"]
  assert cmd[0] == str(fw_dirs["frontend"] / "node_modules" / ".bin" / "vite")
  assert "--watch" not in cmd
  assert ".dist-staging" in cmd


def test_source_filter_and_scandir_exclude_generated_trees(fw_dirs):
  frontend = fw_dirs["frontend"]
  (frontend / "src").mkdir()
  (frontend / "public").mkdir()
  (frontend / "node_modules").mkdir()
  (frontend / "dist").mkdir()
  (frontend / "vite.config.js").write_text("", encoding="utf-8")

  root_names = {
    entry.name for entry in fw._source_tree_scandir(frontend, str(frontend))
  }

  assert root_names == {"public", "src", "vite.config.js"}
  assert fw._is_frontend_source_path(frontend / "src" / "Shell.jsx")
  assert fw._is_frontend_source_path(frontend / "public" / "icon.svg")
  assert fw._is_frontend_source_path(frontend / "vite.config.js")
  assert not fw._is_frontend_source_path(frontend / "dist" / "index.js")
  assert not fw._is_frontend_source_path(frontend / "node_modules" / "x.js")


def test_startup_build_skips_fresh_dist_but_recovers_newer_source(fw_dirs):
  src = fw_dirs["frontend"] / "src"
  src.mkdir()
  source_file = src / "main.jsx"
  source_file.write_text("export default 1\n", encoding="utf-8")
  _write_build(fw_dirs["dist"], "fresh")

  assert fw._startup_build_needed() is False
  signature, _ = fw._source_snapshot()
  assert fw._source_stamp_path().read_text(encoding="utf-8").strip() == signature

  dist_ns = (fw_dirs["dist"] / "index.html").stat().st_mtime_ns
  os.utime(source_file, ns=(dist_ns + 1, dist_ns + 1))
  assert fw._startup_build_needed() is True

  shutil.rmtree(fw_dirs["dist"])
  assert fw._startup_build_needed() is True


def test_edit_during_demand_build_requests_one_rerun(fw_dirs, monkeypatch):
  (fw_dirs["frontend"] / "src").mkdir()
  monkeypatch.setattr(fw, "_DEBOUNCE_SECS", 0.0)
  loop = asyncio.new_event_loop()
  handler = fw._FrontendHandler(loop, start_threads=False)
  calls = []

  def build(reason):
    calls.append(reason)
    if len(calls) == 1:
      handler._request_build(str(fw_dirs["frontend"] / "src" / "second.js"))
    else:
      handler._closed.set()
      handler._build_requested.set()

  monkeypatch.setattr(handler, "_run_demand_build", build)
  thread = threading.Thread(target=handler._build_loop)
  try:
    thread.start()
    handler._request_build(str(fw_dirs["frontend"] / "src" / "first.js"))
    thread.join(timeout=2)
  finally:
    handler._closed.set()
    handler._build_requested.set()
    thread.join(timeout=2)
    loop.close()

  assert calls == [
    str(fw_dirs["frontend"] / "src" / "first.js"),
    str(fw_dirs["frontend"] / "src" / "second.js"),
  ]


def test_idle_observer_requests_build_for_source_edit(fw_dirs, monkeypatch):
  src = fw_dirs["frontend"] / "src"
  src.mkdir()
  monkeypatch.setattr(fw, "_DEBOUNCE_SECS", 0.01)
  calls = []
  called = threading.Event()

  def build(_handler, reason):
    calls.append(reason)
    called.set()

  monkeypatch.setattr(fw._FrontendHandler, "_run_demand_build", build)
  loop = asyncio.new_event_loop()
  handler = fw._FrontendHandler(loop)
  try:
    assert called.wait(timeout=2)
    called.clear()
    changed = src / "changed.js"
    changed.write_text("export default 1\n", encoding="utf-8")
    assert called.wait(timeout=3)
  finally:
    handler.close()
    loop.close()

  assert calls[0] == "startup"
  assert calls[-1] == str(changed)


def test_demand_watcher_has_a_cross_process_singleton_lease(fw_dirs):
  first = fw._acquire_watch_lock()
  try:
    with pytest.raises(RuntimeError, match="already active"):
      fw._acquire_watch_lock()
  finally:
    fcntl.flock(first, fcntl.LOCK_UN)
    first.close()

  # Releasing the owner (including process exit) makes the lease reusable.
  second = fw._acquire_watch_lock()
  fcntl.flock(second, fcntl.LOCK_UN)
  second.close()


@pytest.mark.asyncio
async def test_supervisor_retries_a_contended_watcher(monkeypatch):
  calls = 0

  class Handler:
    def close(self):
      pass

    def health(self):
      return {
        "running": True, "pid": 123, "rss_bytes": 1024,
        "staging_dirty": False, "lease_path": "/tmp/watch.lock",
      }

  def start(_loop):
    nonlocal calls
    calls += 1
    if calls == 1:
      raise RuntimeError("frontend warm watcher already active")
    return None, Handler()

  monkeypatch.setattr(fw, "start_watcher", start)
  monkeypatch.setattr(fw, "_WATCH_LEASE_RETRY_INITIAL", 0.01)
  supervisor = fw._FrontendSupervisor(asyncio.get_running_loop())
  try:
    await supervisor.start()
    for _ in range(50):
      if supervisor.health()["status"] == "running":
        break
      await asyncio.sleep(0.005)
    assert calls == 2
    assert supervisor.health()["status"] == "running"
    assert supervisor.health()["pid"] == 123
  finally:
    supervisor.close()


def test_vite_env_preserves_operator_resource_overrides(fw_dirs, monkeypatch):
  monkeypatch.setenv("CHOKIDAR_INTERVAL", "900")
  monkeypatch.setenv("NODE_OPTIONS", "--trace-warnings --max_old_space_size=768")

  env = fw._vite_env()

  assert env["CHOKIDAR_INTERVAL"] == "900"
  assert env["NODE_OPTIONS"] == "--trace-warnings --max_old_space_size=768"
