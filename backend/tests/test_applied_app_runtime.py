"""Applied source files are immutable to editing and retained while in use."""

import fcntl
import shutil
from pathlib import Path

import pytest

from app import applied_app_runtime as runtime, models
from app.config import get_settings


def _legacy_app(db):
  source = Path(get_settings().data_dir) / "apps" / "legacy-runtime-test"
  source.mkdir(parents=True, exist_ok=True)
  (source / "job.sh").write_text("deployed script")
  row = models.App(name="Legacy", slug="legacy-runtime-test", description="",
                   source_dir=str(source), jsx_source="export default () => null")
  db.add(row)
  db.commit()
  return row, source


def test_legacy_baseline_freezes_deployed_files_once_and_never_refreezes(db):
  row, source = _legacy_app(db)
  (source / ".git").mkdir()
  (source / ".git" / "config").write_text("repository-only fixture")
  (source / "link.sh").symlink_to(source / "job.sh")
  migrated, warnings = runtime.bootstrap_legacy_runtimes(db)
  assert migrated == 1
  assert warnings == []
  baseline = runtime.runtime_root(row)
  assert (baseline / "job.sh").read_text() == "deployed script"
  assert not (baseline / ".git").exists()
  assert not (baseline / "link.sh").is_symlink()

  (source / "job.sh").write_text("later dirty edit")
  assert runtime.bootstrap_legacy_runtimes(db) == (0, [])
  assert (baseline / "job.sh").read_text() == "deployed script"
  shutil.rmtree(baseline)
  assert runtime.bootstrap_legacy_runtimes(db) == (0, [])
  with pytest.raises(runtime.AppliedRuntimeUnavailable):
    runtime.runtime_root(row)


def test_legacy_migration_receipt_does_not_recapture_after_partial_failure(db, monkeypatch):
  row, source = _legacy_app(db)
  original = runtime.shutil.copytree

  def fail(*args, **kwargs):
    raise OSError("simulated migration copy failure")

  monkeypatch.setattr(runtime.shutil, "copytree", fail)
  count, warnings = runtime.bootstrap_legacy_runtimes(db)
  assert count == 0
  assert "simulated migration copy failure" in warnings[0]
  monkeypatch.setattr(runtime.shutil, "copytree", original)
  (source / "job.sh").write_text("post-migration edit")
  assert runtime.bootstrap_legacy_runtimes(db) == (0, [])
  with pytest.raises(runtime.AppliedRuntimeUnavailable):
    runtime.runtime_root(row)


@pytest.mark.parametrize("reader", ["job", "static"])
def test_runtime_pruning_waits_for_readers_then_keeps_current_and_previous(db, reader):
  row, _ = _legacy_app(db)
  revisions = [str(index) * 40 for index in range(1, 5)]
  row.runtime_revision = revisions[-1]
  db.commit()
  for revision in revisions:
    root = runtime.runtime_parent(row.id) / revision
    root.mkdir(parents=True)
    (root / "job.sh").write_text(revision)
  if reader == "job":
    lock_dir = Path(get_settings().data_dir) / "run" / "app-job-locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    pin = (lock_dir / f"{row.id}.lock").open("a")
    fcntl.flock(pin, fcntl.LOCK_EX)
  else:
    pin = runtime.hold_static_runtime(row.id)
  try:
    assert runtime.prune_runtime(row, previous_revision=revisions[-2]) == 0
    assert all((runtime.runtime_parent(row.id) / revision).is_dir() for revision in revisions)
  finally:
    pin.close()
  assert runtime.prune_runtime(row, previous_revision=revisions[-2]) == 2
  assert {path.name for path in runtime.runtime_parent(row.id).iterdir()} == set(revisions[-2:])


def test_migration_preserves_deployed_ignored_static_but_pins_accepted_scripts(db):
  from app import app_git

  row, source = _legacy_app(db)
  app_git.commit_local(source, "accepted source")
  row.source_commit = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  (source / "static").mkdir()
  (source / "static" / "asset.txt").write_text("deployed static")
  (source / "job.sh").write_text("unapplied script")
  db.commit()
  assert runtime.bootstrap_legacy_runtimes(db) == (1, [])
  root = runtime.runtime_root(row)
  assert (root / "job.sh").read_text() == "deployed script"
  assert (root / "static" / "asset.txt").read_text() == "deployed static"
  (source / "static" / "asset.txt").write_text("later edited static")
  assert (root / "static" / "asset.txt").read_text() == "deployed static"


def test_same_source_commit_with_new_generated_assets_gets_new_runtime_pointer(db):
  from app import app_git

  row, source = _legacy_app(db)
  app_git.commit_local(source, "accepted package source")
  row.source_commit = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  first = runtime.publish_runtime(row, runtime.prepare_runtime(
    source, row.source_commit, static_assets={"asset.txt": b"first"},
  ))
  db.commit()
  source_commit = row.source_commit
  second = runtime.publish_runtime(row, runtime.prepare_runtime(
    source, row.source_commit, static_assets={"asset.txt": b"second"},
  ))
  db.commit()
  assert source_commit == row.source_commit
  assert first != second
  assert runtime.runtime_root(row) == second
  assert (first / "static" / "asset.txt").read_bytes() == b"first"
  assert (second / "static" / "asset.txt").read_bytes() == b"second"


def test_static_response_releases_pin_when_send_fails(tmp_path):
  import asyncio
  from starlette.responses import FileResponse
  from app.main import _RuntimePinnedResponse

  target = tmp_path / "asset.txt"
  target.write_text("accepted asset")
  pin = (tmp_path / "pin").open("a")
  response = _RuntimePinnedResponse(FileResponse(target), pin)

  async def failed_send(_message):
    raise OSError("client disconnected")

  async def receive():
    return {"type": "http.disconnect"}

  with pytest.raises(OSError, match="client disconnected"):
    asyncio.run(response({"type": "http", "method": "GET", "headers": [],
                          "extensions": {}}, receive, failed_send))
  assert pin.closed


def test_preserving_absent_package_manifest_removes_draft_manifest(db):
  from app import app_git

  row, source = _legacy_app(db)
  (source / "mobius.json").write_text('{"schedule":{"job":"draft.sh"}}')
  app_git.commit_local(source, "draft source manifest")
  row.source_commit = app_git.head_sha(source, app_git.LOCAL_BRANCH)
  staged = runtime.prepare_runtime(source, row.source_commit, runtime_manifest=None)
  root = runtime.publish_runtime(row, staged)
  db.commit()
  assert not (root / "mobius.json").exists()
