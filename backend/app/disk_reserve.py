"""Disk space held back so a full data volume can always be recovered.

Möbius keeps one file of real, allocated blocks in the data volume's runtime
directory. Nothing else can use that space. The storage guard releases it only
when free space is nearly exhausted (at runtime or at boot), which gives the
database room to commit and the owner room for one cleanup turn. Once the disk
has comfortable room again the guard re-arms it.

The file is the whole state: present and fully allocated means armed. No
database row records it, because the database may be unable to write exactly
when the reserve matters. ``<data>/run`` is already outside the ``/data``
safety repository and the backup set, so the reserve never becomes owner
content.

A reserve only helps if its blocks are real. ``fallocate(2)`` allocates them
instantly on ext4, XFS and btrfs. On a filesystem without it, or one that
compresses or deduplicates allocated-but-unwritten space itself, the file is
filled with random bytes instead, which cannot be compressed away; if even that
does not show up as allocated blocks, :func:`arm` reports ``unsupported`` rather
than pretending the space is held.

The reserve protects the filesystem's own block accounting, which is what
refuses a write with ENOSPC. It cannot see below the filesystem: on thin
provisioning (a sparse zvol, a thin LVM volume) whose backing pool fills up,
writes fail whatever ``statvfs`` and the reserve report.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from app.resource_pressure import MIB


RESERVE_FILENAME = "recovery-reserve"
_PARTIAL_SUFFIX = ".partial"

# Big enough for the database to commit and one cleanup turn to run, small
# enough not to matter on a healthy disk: 3% of the volume, between 32 MiB and
# 256 MiB.
_RESERVE_FRACTION = 0.03
_RESERVE_FLOOR = 32 * MIB
_RESERVE_CEILING = 256 * MIB

# Filesystems round allocation to their block size; a few blocks short of the
# target still holds the space this module promises.
_ALLOCATION_TOLERANCE = 0.98
_FILL_CHUNK = 1 * MIB

_FALLBACK_ERRNOS = frozenset({errno.EOPNOTSUPP, errno.ENOSYS, errno.EINVAL})


ArmState = Literal["armed", "already_armed", "no_room", "unsupported", "failed"]


@dataclass(frozen=True)
class ArmOutcome:
  """What :func:`arm` achieved. ``held_bytes`` is what the file now holds."""

  state: ArmState
  held_bytes: int
  detail: str = ""

  @property
  def holds_reserve(self) -> bool:
    return self.state in ("armed", "already_armed")


def reserve_path(data_dir: str | Path) -> Path:
  return Path(data_dir) / "run" / RESERVE_FILENAME


def reserve_target_bytes(total_bytes: int) -> int:
  """The reserve size for a volume of ``total_bytes``."""
  return min(
    _RESERVE_CEILING,
    max(_RESERVE_FLOOR, int(total_bytes * _RESERVE_FRACTION)),
  )


def _allocated_bytes(path: Path) -> int:
  try:
    return os.stat(path).st_blocks * 512
  except FileNotFoundError:
    return 0


def held_bytes(data_dir: str | Path) -> int:
  """Allocated bytes the reserve holds right now (0 when released)."""
  return _allocated_bytes(reserve_path(data_dir))


def holds(allocated: int, target_bytes: int) -> bool:
  """Whether ``allocated`` bytes count as a reserve of ``target_bytes``."""
  return allocated >= int(target_bytes * _ALLOCATION_TOLERANCE)


def _libc_fallocate() -> Callable[[int, int], int] | None:
  """``fallocate(fd, 0, 0, length)`` returning 0 or an errno, if libc has it.

  ``os.posix_fallocate`` is not used: glibc silently emulates it by writing
  zeros where the filesystem lacks support, which is slow and is exactly the
  compressible fill this module must detect.
  """
  name = ctypes.util.find_library("c")
  if not name:
    return None
  try:
    libc = ctypes.CDLL(name, use_errno=True)
    raw = libc.fallocate
  except (OSError, AttributeError):
    return None
  raw.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_longlong, ctypes.c_longlong)
  raw.restype = ctypes.c_int

  def fallocate(fd: int, length: int) -> int:
    if raw(fd, 0, 0, length) == 0:
      return 0
    return ctypes.get_errno() or errno.EIO

  return fallocate


def _fill_incompressible(fd: int, length: int) -> None:
  os.lseek(fd, 0, os.SEEK_SET)
  remaining = length
  while remaining > 0:
    chunk = os.urandom(min(_FILL_CHUNK, remaining))
    written = os.write(fd, chunk)
    remaining -= written
  os.fsync(fd)


def _unlink_quietly(path: Path) -> None:
  try:
    path.unlink()
  except FileNotFoundError:
    pass


def arm(
  data_dir: str | Path,
  *,
  target_bytes: int,
  disk_usage: Callable[[str | Path], Any] = shutil.disk_usage,
  fallocate: Callable[[int, int], int] | None = None,
) -> ArmOutcome:
  """Create the reserve with ``target_bytes`` of real blocks.

  The caller owns the policy of *when* to arm (it must leave the disk out of
  pressure afterwards); this refuses only when the space is physically absent.
  A reserve of a different size (the volume was resized) is replaced. The
  file appears under its final name only once fully allocated, so a crash
  mid-arm leaves a ``.partial`` file that the next arm or release removes.
  ``fallocate`` substitutes the libc call (tests simulate filesystems).
  """
  path = reserve_path(data_dir)
  partial = path.with_name(path.name + _PARTIAL_SUFFIX)
  current = _allocated_bytes(path)
  if holds(current, target_bytes) and current <= target_bytes * 2:
    return ArmOutcome("already_armed", current)
  path.parent.mkdir(parents=True, exist_ok=True)
  _unlink_quietly(partial)
  # A wrongly sized reserve counts as free space for this decision: it is
  # released below before the new one is allocated.
  free = int(disk_usage(path.parent).free) + current
  if free < target_bytes:
    return ArmOutcome("no_room", current, f"{free} bytes free")
  _unlink_quietly(path)

  allocate = fallocate or _libc_fallocate()
  try:
    fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
  except OSError as exc:
    return ArmOutcome("failed", 0, f"open: {exc}")
  try:
    try:
      result = allocate(fd, target_bytes) if allocate else errno.ENOSYS
      if result == errno.ENOSPC:
        raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
      if result and result not in _FALLBACK_ERRNOS:
        raise OSError(result, os.strerror(result))
      if result == 0:
        os.fsync(fd)
      if result or not holds(_allocated_bytes(partial), target_bytes):
        # Unsupported, or allocated space the filesystem compressed away.
        _fill_incompressible(fd, target_bytes)
    finally:
      os.close(fd)
    allocated = _allocated_bytes(partial)
    if not holds(allocated, target_bytes):
      _unlink_quietly(partial)
      return ArmOutcome(
        "unsupported", 0,
        f"filesystem shows {allocated} of {target_bytes} bytes allocated",
      )
    os.replace(partial, path)
  except OSError as exc:
    _unlink_quietly(partial)
    if exc.errno == errno.ENOSPC:
      return ArmOutcome("no_room", 0, "disk filled while arming")
    return ArmOutcome("failed", 0, str(exc))
  return ArmOutcome("armed", allocated)


def release(data_dir: str | Path) -> int:
  """Delete the reserve; return the allocated bytes it held (0 if absent)."""
  path = reserve_path(data_dir)
  held = _allocated_bytes(path)
  _unlink_quietly(path)
  _unlink_quietly(path.with_name(path.name + _PARTIAL_SUFFIX))
  return held
