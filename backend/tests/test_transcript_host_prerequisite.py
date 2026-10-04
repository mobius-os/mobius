"""First storage activation requires the installed Host's actual recovery owner."""
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app import config, one_way_upgrades as upgrades
from tests.test_transcript_upgrade import legacy


@pytest.fixture
def control(tmp_path, monkeypatch):
  monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path), mobius_host_recovery_required=True))
  directory = tmp_path / "mobius-rebuild"
  directory.mkdir(mode=0o755)
  monkeypatch.setattr(upgrades, "HOST_RECOVERY_PROOF", directory / "boot-proof.json")
  monkeypatch.setenv("MOBIUS_BOOT_ID", "current-test-boot")
  original = Path.lstat
  def owned(path):
    info = original(path)
    if path == directory or directory in path.parents:
      return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
    return info
  monkeypatch.setattr(Path, "lstat", owned)
  return directory


def receipt(control, **fields):
  value = {"worker_revision": 4, "active_worker": {
    "revision": 4, "sha256": "a" * 64, "rollback_floor_level": 1,
  }, **fields}
  (control / "boot-proof.json").write_text(json.dumps({"boot_id": "current-test-boot", "active_worker": value["active_worker"]}))
  (control / "boot-proof.json").chmod(0o644)


@pytest.mark.parametrize("active", [None, {}, {"revision": 2},
  {"revision": 2, "sha256": "a" * 64, "rollback_floor_level": 1},
  {"revision": 4, "sha256": "not-a-verified-hash", "rollback_floor_level": 1},
  {"revision": 4, "sha256": "a" * 64, "rollback_floor_level": 0},
  {"revision": 4, "sha256": "a" * 64, "rollback_floor_level": True},
])
def test_old_or_trial_only_host_cannot_prepare_or_raise_floor(tmp_path, control, active):
  receipt(control, active_worker=active)
  raw = '[{"role":"user","content":"untouched"}]'
  path, seen = legacy(tmp_path, {"one": raw})
  with pytest.raises(upgrades.StepRefusal) as error:
    upgrades.run_gate(str(path), seen.existing_tables)
  assert error.value.database_failure_reason == "host_helper_outdated"
  with sqlite3.connect(path) as db:
    assert db.execute("SELECT floor FROM platform_compat").fetchone() == (0,)
    assert db.execute("SELECT messages FROM chats").fetchone() == (raw,)
    assert db.execute("SELECT COUNT(*) FROM upgrade_units").fetchone() == (0,)


def test_missing_receipt_of_installed_host_refuses(control):
  with pytest.raises(upgrades.StepRefusal):
    upgrades.assert_host_recovery_supports(1)


def test_verified_active_host_allows_conversion_and_original_archive(tmp_path, control):
  receipt(control)
  raw = '[{"role":"user","content":"preserved"}]'
  path, seen = legacy(tmp_path, {"one": raw})
  upgrades.run_gate(str(path), seen.existing_tables)
  with sqlite3.connect(path) as db:
    assert db.execute("SELECT floor FROM platform_compat").fetchone() == (1,)
    assert upgrades.verified_legacy_copy(db, 1, "one") == raw.encode()


def test_non_root_or_writable_receipt_never_confirms_installed_worker(control, monkeypatch):
  receipt(control)
  (control / "boot-proof.json").chmod(0o666)
  with pytest.raises(upgrades.StepRefusal):
    upgrades.assert_host_recovery_supports(1)
  (control / "boot-proof.json").chmod(0o644)
  monkeypatch.undo()
  monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(data_dir=str(control.parent), mobius_host_recovery_required=True))
  with pytest.raises(upgrades.StepRefusal):
    upgrades.assert_host_recovery_supports(1)


def test_no_host_controller_does_not_invent_one(tmp_path, monkeypatch):
  monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(data_dir=str(tmp_path), mobius_host_recovery_required=False))
  upgrades.assert_host_recovery_supports(1)
  assert not (tmp_path / "mobius-rebuild").exists()


def test_configured_host_cannot_be_exempted_by_deleted_control_directory(tmp_path, control):
  control.rmdir()
  with pytest.raises(upgrades.StepRefusal):
    upgrades.assert_host_recovery_supports(1)


def test_broken_legacy_control_symlink_is_not_an_unmanaged_deployment(tmp_path, monkeypatch):
  (tmp_path / "mobius-rebuild").symlink_to(tmp_path / "missing")
  monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(
    data_dir=str(tmp_path), mobius_host_recovery_required=False))
  with pytest.raises(upgrades.StepRefusal):
    upgrades.assert_host_recovery_supports(1)


def test_stale_status_is_not_this_boots_worker_proof(control):
  (control / "status.json").write_text(json.dumps({"active_worker": {
    "revision": 4, "sha256": "a" * 64, "rollback_floor_level": 1}}))
  with pytest.raises(upgrades.StepRefusal):
    upgrades.assert_host_recovery_supports(1)


def test_host_proof_from_previous_container_boot_is_not_authority(control, monkeypatch):
  receipt(control)
  monkeypatch.setenv("MOBIUS_BOOT_ID", "different-boot")
  with pytest.raises(upgrades.StepRefusal):
    upgrades.assert_host_recovery_supports(1)


def test_actual_active_worker_proof_precedes_gate_and_floor_aware_recovery(tmp_path, control, monkeypatch):
  from tests.test_transcript_host_cutover import _host, _floor
  host = _host()
  state = control / "private-host-state"
  state.mkdir(mode=0o700)
  monkeypatch.setattr(host, "STATE_DIR", state)
  monkeypatch.setattr(host, "WORKERS", state / "workers")
  monkeypatch.setattr(host, "WORKER_INDEX", state / "workers.json")
  source = Path(host.__file__).read_bytes()
  assert host.seed_worker(source) == "installed: revision 4"
  active = host.active_worker_receipt(state)
  assert active["revision"] == 4
  receipt(control, active_worker=active)
  raw = '[ {"role":"user", "content":"original"} ]'
  path, seen = legacy(tmp_path, {"one": raw})
  upgrades.run_gate(str(path), seen.existing_tables)
  assert _floor(host, path) == 1
  calls = []
  monkeypatch.setattr(host, "fence_app", lambda *_: True)
  monkeypatch.setattr(host, "rollback_image_level", lambda *_: 0)
  monkeypatch.setattr(host, "read_database_floor", lambda *_: _floor(host, path))
  monkeypatch.setattr(host, "write_status", lambda *a, **kw: calls.append(kw))
  monkeypatch.setattr(host, "compose", lambda *a, **kw: calls.append(a))
  monkeypatch.setattr(host, "TRANSACTION", tmp_path / "transaction.json")
  monkeypatch.setattr(host, "restart_ledger", lambda *a, **kw: True)
  monkeypatch.setattr(host, "wait_ready", lambda *a, **kw: ("timeout", None))
  # No stopped app container is found, so nothing may be started.
  monkeypatch.setattr(host, "app_container_any", lambda *_: "")
  assert host.rollback({"data_dir": str(tmp_path)}, "op", "a" * 40,
    "health_check_failed", "candidate unhealthy") == 1
  assert calls[-1]["code"] == "newer_version_required"
  assert not any(isinstance(call, tuple) and "up" in call for call in calls)
  with sqlite3.connect(path) as db:
    assert upgrades.verified_legacy_copy(db, 1, "one") == raw.encode()
