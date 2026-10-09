"""A rejected request cannot rewrite accepted schedule or lifecycle state."""

import asyncio
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest

from app import app_cron, app_git, app_setup, fs_locks, install, models
from app.app_cron import ScheduleChoice
from app.main import app as api
from app.routes import app_schedules
from tests.test_app_schedule_ownership import scheduled_app, _fake_crontab


class _ObservedLifecycleLock(asyncio.Lock):
  def __init__(self):
    super().__init__()
    self.waiting = Event()

  async def acquire(self):
    if self.locked():
      self.waiting.set()
    return await super().acquire()


@pytest.fixture(autouse=True)
def isolated_lifecycle_lock(monkeypatch):
  # Concurrent requests share one event loop, as the production worker does.
  monkeypatch.setattr(fs_locks, "_lifecycle_lock", _ObservedLifecycleLock())


def test_postcommit_failure_keeps_accepted_zone_for_startup_retry(
  db, scheduled_app, tmp_path, monkeypatch,
):
  app = scheduled_app
  root = Path(app.source_dir)
  manifest = {"schedule": {"default": "0 6 * * *", "job": "fetch.sh"}}
  (root / "mobius.json").write_text(json.dumps(manifest))
  app_git.commit_local(root, "Accept scheduled fixture")
  app.source_commit = app_git.head_sha(root, app_git.LOCAL_BRANCH)
  from app.applied_app_runtime import prepare_runtime, publish_runtime
  publish_runtime(app, prepare_runtime(root, app.source_commit))
  db.commit()
  fail = _fake_crontab(tmp_path, monkeypatch)
  monkeypatch.setenv("MOBIUS_APP_BASE", str(root.parent))
  monkeypatch.setattr(install, "owner_timezone", lambda db: "Asia/Tokyo")
  monkeypatch.setattr(app_setup, "request_run", lambda: None)

  async def no_skills(*args):
    pass

  monkeypatch.setattr(install, "_sync_app_skills", no_skills)
  fail.touch()
  warnings = []
  asyncio.run(install._run_post_commit_effects(
    db, app=app, mode="install",
    candidate=SimpleNamespace(manifest=manifest, bundled_job=True), warnings=warnings,
  ))
  assert any("cron: registration failed" in warning for warning in warnings)
  assert db.get(models.App, app.id) is not None
  choice = app_cron.read_schedule_choice(app.id)
  assert choice.timezone == "Asia/Tokyo"
  assert choice.source == "manifest"
  fail.unlink()
  assert app_schedules.reconcile_app_cron_supervision(db) == (1, [], True)
  assert app_schedules._app_zone_declaration(app) == ("Asia/Tokyo", "0 6 * * *")


@pytest.mark.parametrize("unreadable", ["choice.json", "init-cron.sh"])
def test_unknown_snapshot_refuses_save_before_mutating_any_file(
  client, auth, scheduled_app, monkeypatch, unreadable,
):
  app_id = scheduled_app.id
  old = ScheduleChoice(source="owner", cron="0 6 * * *", job="fetch.sh")
  app_cron.record_schedule_choice(app_id, old)
  state = app_cron.schedule_state_dir(app_id)
  init = state / "init-cron.sh"
  init.write_text("old declaration")
  before = {p: p.read_bytes() for p in state.iterdir()}
  read_bytes = Path.read_bytes

  def denied(path):
    if path == state / unreadable:
      raise PermissionError("snapshot denied")
    return read_bytes(path)

  monkeypatch.setattr(Path, "read_bytes", denied)

  def register(*args, **kwargs):
    pytest.fail("registration must not start without a complete snapshot")

  monkeypatch.setattr(app_cron, "register_cron", register)
  response = client.post(
    f"/api/apps/{app_id}/schedule", headers=auth,
    json={"cron": "0 7 * * *", "job": "fetch.sh"},
  )
  assert response.status_code == 500
  assert {p: read_bytes(p) for p in state.iterdir()} == before


def test_rollback_preserves_raw_provenance_and_file_modes(monkeypatch):
  state = app_cron.schedule_state_dir(9130)
  state.mkdir(parents=True)
  before = {}
  for name, data, mode in (
    ("choice.json", b"not yet valid json\n", 0o640),
    ("init-cron.sh", b"old declaration\n", 0o750),
  ):
    path = state / name
    path.write_bytes(data)
    path.chmod(mode)
    before[path] = data, mode
  with pytest.raises(RuntimeError, match="write failed"):
    with app_cron.schedule_choice_rollback(9130):
      for path in before:
        path.write_text("new")
        path.chmod(0o600)
      raise RuntimeError("write failed")
  for path, (data, mode) in before.items():
    assert path.read_bytes() == data
    assert path.stat().st_mode & 0o7777 == mode


def test_rollback_attempts_both_files_and_preserves_original_error(monkeypatch, caplog):
  state = app_cron.schedule_state_dir(9131)
  state.mkdir(parents=True)
  (state / "choice.json").write_text("old choice")
  (state / "init-cron.sh").write_text("old declaration")
  restore = app_cron._restore_schedule_file

  def fail_provenance(path, data, mode):
    if path.name == "choice.json":
      raise PermissionError("rollback denied")
    restore(path, data, mode)

  monkeypatch.setattr(app_cron, "_restore_schedule_file", fail_provenance)
  with pytest.raises(RuntimeError, match="registration failed"):
    with app_cron.schedule_choice_rollback(9131):
      (state / "init-cron.sh").write_text("new declaration")
      raise RuntimeError("registration failed")
  assert (state / "init-cron.sh").read_text() == "old declaration"
  assert "Could not roll back schedule file choice.json" in caplog.text


async def _entered(event):
  assert await asyncio.to_thread(event.wait, 5), "worker did not enter"


def test_failed_save_finishes_before_next_success_without_blocking_loop(
  auth, scheduled_app, monkeypatch,
):
  app_id = scheduled_app.id
  init = app_cron.schedule_state_dir(app_id) / "init-cron.sh"
  entered, release = Event(), Event()
  calls = []

  def register(slug, cron, *args, **kwargs):
    calls.append(cron)
    init.parent.mkdir(parents=True, exist_ok=True)
    init.write_text(cron)
    if cron == "0 7 * * *":
      entered.set()
      assert release.wait(5)
      raise app_cron.CronInfrastructureError(500, "first save failed")

  monkeypatch.setattr(app_cron, "register_cron", register)

  async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
      url = f"/api/apps/{app_id}/schedule"
      assert (await client.post(url, headers=auth, json={"cron": "0 6 * * *"})).status_code == 200
      first = asyncio.create_task(client.post(url, headers=auth, json={"cron": "0 7 * * *"}))
      second = None
      try:
        await _entered(entered)
        second = asyncio.create_task(client.post(url, headers=auth, json={"cron": "0 8 * * *"}))
        await _entered(fs_locks.install_uninstall_lock().waiting)
        # A real unrelated HTTP read proves the event loop remains available.
        assert (await client.get("/api/owner/timezone", headers=auth)).status_code == 200
        assert not second.done()
        assert calls == ["0 6 * * *", "0 7 * * *"]
      finally:
        release.set()
        assert (await first).status_code == 500
        if second is not None:
          assert (await second).status_code == 200

  asyncio.run(run())
  assert app_cron.read_schedule_choice(app_id).cron == "0 8 * * *"
  assert init.read_text() == "0 8 * * *"


def test_cancelled_save_holds_lifecycle_until_worker_rollback_finishes(
  auth, scheduled_app, monkeypatch,
):
  entered, release = Event(), Event()
  app_id = scheduled_app.id

  def register(*args, **kwargs):
    entered.set()
    assert release.wait(5)
    raise app_cron.CronInfrastructureError(500, "failed after disconnect")

  monkeypatch.setattr(app_cron, "register_cron", register)

  async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
      request = asyncio.create_task(client.post(
        f"/api/apps/{app_id}/schedule", headers=auth, json={"cron": "0 7 * * *"},
      ))
      try:
        await _entered(entered)
        request.cancel()
        await asyncio.sleep(0)
        request.cancel()
        await asyncio.sleep(0)
        assert fs_locks.install_uninstall_lock().locked()
        assert not request.done()
      finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
          await request
    assert not fs_locks.install_uninstall_lock().locked()

  asyncio.run(run())
  assert app_cron.read_schedule_choice(app_id) is None


@pytest.mark.parametrize("change", ["deleted", "replaced"])
def test_waiting_save_rechecks_app_identity_before_registration(
  auth, scheduled_app, db, monkeypatch, change,
):
  from datetime import datetime, UTC
  from app.resource_access import live_app_or_404
  checked = Event()

  def observed(db, app_id):
    row = live_app_or_404(db, app_id)
    checked.set()
    return row

  monkeypatch.setattr(app_schedules, "live_app_or_404", observed)

  def register(*args, **kwargs):
    pytest.fail("a removed app must not be registered")

  monkeypatch.setattr(app_cron, "register_cron", register)

  async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
      lock = fs_locks.install_uninstall_lock()
      await lock.acquire()
      request = asyncio.create_task(client.post(
        f"/api/apps/{scheduled_app.id}/schedule", headers=auth, json={"cron": "0 7 * * *"},
      ))
      try:
        await _entered(checked)
        if change == "deleted":
          scheduled_app.deleted_at = datetime.now(UTC).replace(tzinfo=None)
        else:
          scheduled_app.token_nonce = "replacement-instance"
        db.commit()
      finally:
        lock.release()
      assert (await request).status_code == 404

  asyncio.run(run())
  assert app_cron.read_schedule_choice(scheduled_app.id) is None


@pytest.mark.parametrize("competing", ["timezone", "accepted-update", "delete"])
def test_failed_owner_save_serializes_competing_lifecycle_mutations(
  auth, scheduled_app, db, monkeypatch, competing,
):
  app_id = scheduled_app.id
  entered, release = Event(), Event()
  previous = ScheduleChoice(
    source="manifest", cron="0 6 * * *", job="fetch.sh", manifest_default="0 6 * * *",
  )
  app_cron.record_schedule_choice(app_id, previous)
  calls = []

  def register(slug, cron, *args, **kwargs):
    if cron == "0 7 * * *":
      entered.set()
      assert release.wait(5)
      raise app_cron.CronInfrastructureError(500, "save failed")
    calls.append((cron, kwargs.get("timezone")))

  def unregister(source):
    # The failed owner's choice must be gone before teardown observes it.
    assert app_cron.read_schedule_choice(app_id) == previous
    calls.append(("deleted", None))

  monkeypatch.setattr(app_cron, "register_cron", register)
  monkeypatch.setattr(install, "_unregister_cron", unregister)
  monkeypatch.setattr(app_setup, "request_run", lambda: None)

  async def no_skills(*args):
    pass

  monkeypatch.setattr(install, "_sync_app_skills", no_skills)

  async def accepted_update():
    # This is the same boundary held by Store/apply around post-commit work.
    async with fs_locks.install_uninstall_lock():
      await install._run_post_commit_effects(
        db, app=scheduled_app, mode="update", warnings=[],
        candidate=SimpleNamespace(
          manifest={"schedule": {"default": "0 8 * * *", "job": "fetch.sh"}},
          bundled_job=True,
        ),
      )

  async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
      request = asyncio.create_task(client.post(
        f"/api/apps/{app_id}/schedule", headers=auth, json={"cron": "0 7 * * *"},
      ))
      other = None
      try:
        await _entered(entered)
        if competing == "timezone":
          operation = client.put("/api/owner/timezone", headers=auth, json={"timezone": "Asia/Tokyo"})
        elif competing == "delete":
          operation = client.delete(f"/api/apps/{app_id}", headers=auth)
        else:
          operation = accepted_update()
        other = asyncio.create_task(operation)
        await _entered(fs_locks.install_uninstall_lock().waiting)
        assert (await client.get("/api/owner/timezone", headers=auth)).status_code == 200
        assert not other.done()
        assert calls == []
      finally:
        release.set()
        assert (await request).status_code == 500
        if other is not None:
          response = await other
          if competing != "accepted-update":
            assert response.status_code in (200, 204), response.text

  asyncio.run(run())
  choice = app_cron.read_schedule_choice(app_id)
  if competing == "timezone":
    assert choice.source == "manifest"
    assert choice.timezone == "Asia/Tokyo"
    assert calls == [("* * * * *", "Asia/Tokyo")]
  elif competing == "accepted-update":
    assert choice.source == "manifest"
    assert choice.cron == "0 8 * * *"
    assert calls == [("deleted", None), ("0 8 * * *", None)]
  else:
    db.expire_all()
    assert db.get(models.App, app_id).deleted_at is not None
    assert calls == [("deleted", None)]


def test_cancelled_teardown_does_not_release_lifecycle_while_cron_worker_runs(
  auth, scheduled_app, monkeypatch,
):
  from app.routes import apps
  entered, release = Event(), Event()

  def teardown(source):
    entered.set()
    assert release.wait(5)

  monkeypatch.setattr(apps, "_drop_cron_only", teardown)

  async def run():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
      request = asyncio.create_task(client.delete(f"/api/apps/{scheduled_app.id}", headers=auth))
      try:
        await _entered(entered)
        request.cancel()
        await asyncio.sleep(0)
        assert fs_locks.install_uninstall_lock().locked()
        assert not request.done()
      finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
          await request
    assert not fs_locks.install_uninstall_lock().locked()

  asyncio.run(run())


def test_failed_timezone_convergence_restores_both_files_for_next_report(
  db, scheduled_app, monkeypatch,
):
  app_id = scheduled_app.id
  old = ScheduleChoice(source="manifest", cron="0 6 * * *", job="fetch.sh")
  app_cron.record_schedule_choice(app_id, old)
  init = app_cron.schedule_state_dir(app_id) / "init-cron.sh"
  init.write_text("original declaration")

  def register(*args, **kwargs):
    init.write_text("failed zone declaration")
    raise app_cron.CronInfrastructureError(500, "write failed")

  monkeypatch.setattr(app_cron, "register_cron", register)
  asyncio.run(install.converge_manifest_schedule_zones(db, "Asia/Tokyo"))
  assert app_cron.read_schedule_choice(app_id) == old
  assert init.read_text() == "original declaration"

  monkeypatch.setattr(app_cron, "register_cron", lambda *args, **kwargs: None)
  asyncio.run(install.converge_manifest_schedule_zones(db, "Asia/Tokyo"))
  assert app_cron.read_schedule_choice(app_id).timezone == "Asia/Tokyo"


def test_log_directory_failure_cannot_install_a_rejected_schedule(
  client, auth, scheduled_app, tmp_path, monkeypatch,
):
  _fake_crontab(tmp_path, monkeypatch)
  monkeypatch.setenv("MOBIUS_APP_BASE", str(Path(scheduled_app.source_dir).parent))
  url = f"/api/apps/{scheduled_app.id}/schedule"
  assert client.post(url, headers=auth, json={"cron": "0 6 * * *"}).status_code == 200
  state = app_cron.schedule_state_dir(scheduled_app.id)
  before = {p: p.read_bytes() for p in state.iterdir()}
  before_live = (tmp_path / "crontab.txt").read_bytes()
  unavailable = tmp_path / "not-a-directory"
  unavailable.touch()
  monkeypatch.setenv("DATA_DIR", str(unavailable))

  rejected = client.post(url, headers=auth, json={"cron": "0 7 * * *"})

  assert rejected.status_code == 500
  assert {p: p.read_bytes() for p in state.iterdir()} == before
  assert (tmp_path / "crontab.txt").read_bytes() == before_live
