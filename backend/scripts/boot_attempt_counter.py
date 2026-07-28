#!/usr/bin/env python3
"""Locked owner for the platform boot-attempt counter.

The entrypoint, health probe, and Railway component supervisor run
concurrently. Every mutation therefore carries the current boot id and passes
through this file's lock; a late writer from an older boot cannot overwrite a
newer boot, and a component rollback cannot undo a health reset.
"""

from __future__ import annotations

import argparse
import fcntl
import os
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Counter:
  count: int
  boot_id: str | None


def _read(path: Path) -> Counter:
  try:
    parts = path.read_text(encoding="utf-8").split()
  except OSError:
    return Counter(0, None)
  try:
    count = int(parts[0])
  except (IndexError, ValueError):
    count = 0
  if count < 0:
    count = 0
  # Legacy records have only "count timestamp". They remain valid inputs and
  # acquire the current boot id on the next begin.
  boot_id = parts[2] if len(parts) >= 3 else None
  return Counter(count, boot_id)


def _write(path: Path, count: int, boot_id: str) -> None:
  timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
  payload = f"{count} {timestamp} {boot_id}\n".encode()
  tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{time.time_ns()}")
  try:
    existing = path.lstat()
  except FileNotFoundError:
    existing = None
  fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  try:
    if existing is not None and stat.S_ISREG(existing.st_mode):
      os.fchmod(fd, stat.S_IMODE(existing.st_mode))
      if os.geteuid() == 0:
        os.fchown(fd, existing.st_uid, existing.st_gid)
    with os.fdopen(fd, "wb") as stream:
      fd = -1
      stream.write(payload)
      stream.flush()
      os.fsync(stream.fileno())
  finally:
    if fd >= 0:
      os.close(fd)
  try:
    os.replace(tmp, path)
    dir_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
      os.fsync(dir_fd)
    finally:
      os.close(dir_fd)
  finally:
    try:
      tmp.unlink()
    except FileNotFoundError:
      pass


@contextmanager
def _locked(path: Path):
  path.parent.mkdir(parents=True, exist_ok=True)
  lock_path = path.with_name(f"{path.name}.lock")
  fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
  try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    yield
  finally:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def begin(path: Path, boot_id: str) -> tuple[int, int]:
  with _locked(path):
    prior = _read(path).count
    current = prior + 1
    _write(path, current, boot_id)
    return prior, current


def reset(path: Path, boot_id: str) -> bool:
  with _locked(path):
    current = _read(path)
    if current.boot_id != boot_id:
      return False
    _write(path, 0, boot_id)
    return True


def rollback(
  path: Path,
  boot_id: str,
  expected_current: int,
  prior: int,
) -> bool:
  with _locked(path):
    current = _read(path)
    if (
      current.boot_id != boot_id
      or current.count != expected_current
    ):
      return False
    _write(path, prior, boot_id)
    return True


def _boot_id(value: str) -> str:
  if not value or any(char.isspace() for char in value):
    raise argparse.ArgumentTypeError("boot id must be one non-empty token")
  return value


def _non_negative(value: str) -> int:
  parsed = int(value)
  if parsed < 0:
    raise argparse.ArgumentTypeError("counter values must be non-negative")
  return parsed


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser()
  subparsers = parser.add_subparsers(dest="command", required=True)

  begin_parser = subparsers.add_parser("begin")
  begin_parser.add_argument("path", type=Path)
  begin_parser.add_argument("boot_id", type=_boot_id)

  reset_parser = subparsers.add_parser("reset")
  reset_parser.add_argument("path", type=Path)
  reset_parser.add_argument("boot_id", type=_boot_id)

  rollback_parser = subparsers.add_parser("rollback")
  rollback_parser.add_argument("path", type=Path)
  rollback_parser.add_argument("boot_id", type=_boot_id)
  rollback_parser.add_argument("expected_current", type=_non_negative)
  rollback_parser.add_argument("prior", type=_non_negative)

  args = parser.parse_args(argv)
  if args.command == "begin":
    prior, current = begin(args.path, args.boot_id)
    print(prior, current)
    return 0
  if args.command == "reset":
    print("1" if reset(args.path, args.boot_id) else "0")
    return 0
  if args.command == "rollback":
    applied = rollback(
      args.path,
      args.boot_id,
      args.expected_current,
      args.prior,
    )
    print("1" if applied else "0")
    return 0
  raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
  sys.exit(main())
