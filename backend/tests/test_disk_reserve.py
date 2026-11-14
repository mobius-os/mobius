"""The recovery reserve holds real disk space that only recovery may use."""

import errno
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import disk_reserve

MIB = 1024 * 1024


def _roomy(_path):
  return SimpleNamespace(total=10 * 1024 * MIB, used=0, free=8 * 1024 * MIB)


def test_reserve_holds_real_allocated_blocks(tmp_path):
  outcome = disk_reserve.arm(tmp_path, target_bytes=2 * MIB, disk_usage=_roomy)

  assert outcome.state == "armed"
  path = disk_reserve.reserve_path(tmp_path)
  assert path.stat().st_blocks * 512 >= 2 * MIB * 0.98
  assert disk_reserve.held_bytes(tmp_path) == outcome.held_bytes
  assert not path.with_name(path.name + ".partial").exists()


def test_arming_an_armed_reserve_changes_nothing(tmp_path):
  disk_reserve.arm(tmp_path, target_bytes=2 * MIB, disk_usage=_roomy)
  inode = disk_reserve.reserve_path(tmp_path).stat().st_ino

  outcome = disk_reserve.arm(tmp_path, target_bytes=2 * MIB, disk_usage=_roomy)

  assert outcome.state == "already_armed"
  assert disk_reserve.reserve_path(tmp_path).stat().st_ino == inode


def test_unsupported_fallocate_falls_back_to_incompressible_bytes(tmp_path):
  outcome = disk_reserve.arm(
    tmp_path, target_bytes=2 * MIB, disk_usage=_roomy,
    fallocate=lambda _fd, _length: errno.EOPNOTSUPP,
  )

  assert outcome.state == "armed"
  head = disk_reserve.reserve_path(tmp_path).read_bytes()[:4096]
  # Zeros would let a compressing filesystem hold nothing at all.
  assert head.count(0) < len(head) // 2


def test_space_the_filesystem_does_not_hold_is_reported_not_pretended(
  tmp_path, monkeypatch,
):
  monkeypatch.setattr(disk_reserve, "_allocated_bytes", lambda _path: 0)

  outcome = disk_reserve.arm(
    tmp_path, target_bytes=2 * MIB, disk_usage=_roomy,
    fallocate=lambda _fd, _length: 0,
  )

  assert outcome.state == "unsupported"
  assert not outcome.holds_reserve
  assert list(disk_reserve.reserve_path(tmp_path).parent.iterdir()) == []


def test_arming_without_room_creates_nothing(tmp_path):
  def tight(_path):
    return SimpleNamespace(total=100 * MIB, used=99 * MIB, free=1 * MIB)

  outcome = disk_reserve.arm(tmp_path, target_bytes=2 * MIB, disk_usage=tight)

  assert outcome.state == "no_room"
  assert disk_reserve.held_bytes(tmp_path) == 0


def test_disk_filling_while_arming_leaves_no_partial_file(tmp_path):
  outcome = disk_reserve.arm(
    tmp_path, target_bytes=2 * MIB, disk_usage=_roomy,
    fallocate=lambda _fd, _length: errno.ENOSPC,
  )

  assert outcome.state == "no_room"
  assert list(disk_reserve.reserve_path(tmp_path).parent.iterdir()) == []


def test_release_returns_the_space_and_clears_a_crashed_partial(tmp_path):
  disk_reserve.arm(tmp_path, target_bytes=2 * MIB, disk_usage=_roomy)
  path = disk_reserve.reserve_path(tmp_path)
  path.with_name(path.name + ".partial").write_bytes(b"x")

  freed = disk_reserve.release(tmp_path)

  assert freed >= 2 * MIB * 0.98
  assert list(path.parent.iterdir()) == []
  assert disk_reserve.release(tmp_path) == 0


def test_resized_volume_replaces_the_reserve(tmp_path):
  disk_reserve.arm(tmp_path, target_bytes=1 * MIB, disk_usage=_roomy)

  outcome = disk_reserve.arm(tmp_path, target_bytes=4 * MIB, disk_usage=_roomy)

  assert outcome.state == "armed"
  assert disk_reserve.held_bytes(tmp_path) >= 4 * MIB * 0.98


def test_reserve_size_is_bounded_for_small_and_large_volumes():
  assert disk_reserve.reserve_target_bytes(500 * MIB) == 32 * MIB
  assert disk_reserve.reserve_target_bytes(4 * 1024 * MIB) == int(4 * 1024 * MIB * 0.03)
  assert disk_reserve.reserve_target_bytes(1024 * 1024 * MIB) == 256 * MIB


def test_reserve_never_becomes_owner_content():
  """It lives where the /data safety repository and backups already skip."""
  scripts = Path(__file__).resolve().parents[1] / "scripts"
  sys.path.insert(0, str(scripts))
  try:
    import init_data_repo
    from importlib import util
    spec = util.spec_from_file_location("backup_data", scripts / "backup-data.py")
    backup = util.module_from_spec(spec)
    spec.loader.exec_module(backup)
  finally:
    sys.path.remove(str(scripts))

  relative = disk_reserve.reserve_path("/data").relative_to("/data")
  assert relative.parts[0] == "run"
  assert "/run/" in init_data_repo.DATA_GITIGNORE.splitlines()
  assert "run" in backup.EXCLUDE_NAMES
  assert os.path.basename(str(relative)) == disk_reserve.RESERVE_FILENAME
