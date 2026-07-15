"""Warm frontend watcher publication behavior."""

import asyncio
import shutil

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
  boot (three restarts could evict a generation an unreloaded tab needs)."""
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


def test_incomplete_watched_build_retries_before_warning(monkeypatch):
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
    assert handler._publish_dirty_sync("settle:retry again") is False
    assert len(events) == 1

    monkeypatch.setattr(fw, "_publish_built_dir", lambda source, reason: True)
    assert handler._publish_dirty_sync("settle:complete") is True
    assert handler._incomplete_since is None
    assert handler._incomplete_notified is False
  finally:
    loop.close()


def test_vite_watch_uses_polling_and_staging_output(fw_dirs, monkeypatch):
  monkeypatch.setenv("CHOKIDAR_USEPOLLING", "0")
  monkeypatch.delenv("CHOKIDAR_INTERVAL", raising=False)
  monkeypatch.delenv("NODE_OPTIONS", raising=False)
  env = fw._vite_env()
  cmd = fw._vite_build_cmd(fw_dirs["staging"], watch=True)

  assert env["MOBIUS_VITE_CACHE"] == str(fw_dirs["cache"])
  assert env["TMPDIR"] == str(fw_dirs["tmp"])
  assert env["CHOKIDAR_USEPOLLING"] == "1"
  assert env["CHOKIDAR_INTERVAL"] == "500"
  assert "--max-old-space-size=512" in env["NODE_OPTIONS"]
  assert "--watch" in cmd
  assert ".dist-staging" in cmd


def test_vite_env_preserves_operator_resource_overrides(fw_dirs, monkeypatch):
  monkeypatch.setenv("CHOKIDAR_INTERVAL", "900")
  monkeypatch.setenv("NODE_OPTIONS", "--trace-warnings --max_old_space_size=768")

  env = fw._vite_env()

  assert env["CHOKIDAR_INTERVAL"] == "900"
  assert env["NODE_OPTIONS"] == "--trace-warnings --max_old_space_size=768"
