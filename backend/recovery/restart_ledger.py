#!/usr/bin/env python3
"""Frozen supervisor half of planned-restart continuation.

The platform process may *request* a restart, but it cannot authenticate the
cause of the next boot by writing a transcript row or database flag itself. This
helper runs from the baked, root-owned recovery bundle and maintains a
root-owned ledger under ``/data/.restart-ledger``:

* ``accept`` validates and consumes one untrusted platform request, records the
  exact run identities, and is called immediately before the entrypoint poller
  terminates pid 1.
* ``begin-boot`` binds that accepted request to exactly the next boot id.
* ``harden`` restores root ownership after the entrypoint's broad compatibility
  chown. If that cannot be proven, authorization is deleted and continuation
  fails closed to manual recovery.

No ``app.*`` imports are used. The platform can suppress a continuation by
deleting its own request, but it cannot forge the root-owned acknowledgement.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any


DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
INTENT_PATH = DATA_DIR / ".restart-continuation-intent.json"
REQUEST_PATH = DATA_DIR / ".platform-restart-requested"
LEDGER_DIR = DATA_DIR / ".restart-ledger"
ACCEPTED_PATH = LEDGER_DIR / "accepted.json"
ACK_PATH = LEDGER_DIR / "ack.json"
BOOT_PATH = LEDGER_DIR / "boot-id"

PROTOCOL_VERSION = 1
MAX_REQUEST_BYTES = 64 * 1024
MAX_RUNS = 64
MAX_REQUEST_AGE_SECONDS = 120
MAX_ACCEPTED_AGE_SECONDS = 10 * 60
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{8,160}$")
SUPERVISOR_UID = 0
SUPERVISOR_GID = 0


def _valid_token(value: Any) -> bool:
  return isinstance(value, str) and bool(_TOKEN_RE.fullmatch(value))


def _remove(path: Path) -> bool:
  try:
    if path.is_dir() and not path.is_symlink():
      shutil.rmtree(path)
    else:
      path.unlink(missing_ok=True)
  except OSError:
    pass
  try:
    path.lstat()
  except FileNotFoundError:
    return True
  except OSError:
    return False
  return False


def _fsync_dir(path: Path) -> None:
  try:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
  except OSError:
    return
  try:
    os.fsync(fd)
  finally:
    os.close(fd)


def _atomic_write(path: Path, payload: bytes, mode: int) -> None:
  tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
  flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
  flags |= getattr(os, "O_NOFOLLOW", 0)
  fd = os.open(tmp, flags, mode)
  try:
    offset = 0
    while offset < len(payload):
      offset += os.write(fd, payload[offset:])
    os.fsync(fd)
    os.fchmod(fd, mode)
    if hasattr(os, "fchown"):
      os.fchown(fd, SUPERVISOR_UID, SUPERVISOR_GID)
  finally:
    os.close(fd)
  os.replace(tmp, path)
  _fsync_dir(path.parent)


def _write_json(path: Path, value: dict[str, Any], mode: int) -> None:
  payload = json.dumps(
    value, sort_keys=True, separators=(",", ":"),
  ).encode("utf-8")
  _atomic_write(path, payload, mode)


def _read_bounded_json(path: Path) -> dict[str, Any] | None:
  try:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_REQUEST_BYTES:
      return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
      raw = os.read(fd, MAX_REQUEST_BYTES + 1)
    finally:
      os.close(fd)
    if len(raw) > MAX_REQUEST_BYTES:
      return None
    value = json.loads(raw.decode("utf-8"))
    return value if isinstance(value, dict) else None
  except (OSError, UnicodeError, json.JSONDecodeError):
    return None


def _trusted_ledger_dir() -> bool:
  try:
    st = LEDGER_DIR.lstat()
  except OSError:
    return False
  return bool(
    stat.S_ISDIR(st.st_mode)
    and not stat.S_ISLNK(st.st_mode)
    and st.st_uid == SUPERVISOR_UID
    and st.st_gid == SUPERVISOR_GID
    and not (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
  )


def _recreate_ledger_dir() -> None:
  if LEDGER_DIR.exists() or LEDGER_DIR.is_symlink():
    _remove(LEDGER_DIR)
  LEDGER_DIR.mkdir(mode=0o755, parents=False, exist_ok=False)
  os.chown(LEDGER_DIR, SUPERVISOR_UID, SUPERVISOR_GID)
  os.chmod(LEDGER_DIR, 0o755)
  _fsync_dir(DATA_DIR)


def _prepare_ledger_dir() -> None:
  if not _trusted_ledger_dir():
    _recreate_ledger_dir()


def _valid_runs(value: Any) -> list[dict[str, str]] | None:
  if not isinstance(value, list) or len(value) > MAX_RUNS:
    return None
  seen: set[tuple[str, str]] = set()
  runs: list[dict[str, str]] = []
  for item in value:
    if not isinstance(item, dict):
      return None
    chat_id = item.get("chat_id")
    run_token = item.get("run_token")
    if not _valid_token(chat_id) or not _valid_token(run_token):
      return None
    key = (chat_id, run_token)
    if key in seen:
      return None
    seen.add(key)
    runs.append({"chat_id": chat_id, "run_token": run_token})
  return runs


def _valid_intent(
  intent: dict[str, Any] | None,
  request: dict[str, Any] | None,
  boot_id: str,
  now: float,
) -> dict[str, Any] | None:
  if not intent or not request:
    return None
  if intent.get("version") != PROTOCOL_VERSION:
    return None
  nonce = intent.get("nonce")
  source_boot_id = intent.get("source_boot_id")
  if (
    not _valid_token(nonce)
    or source_boot_id != boot_id
    or request.get("nonce") != nonce
    or request.get("source_boot_id") != boot_id
  ):
    return None
  try:
    created_at = float(intent.get("created_at"))
  except (TypeError, ValueError):
    return None
  if created_at > now + 5 or now - created_at > MAX_REQUEST_AGE_SECONDS:
    return None
  runs = _valid_runs(intent.get("runs"))
  if runs is None:
    return None
  return {
    "version": PROTOCOL_VERSION,
    "nonce": nonce,
    "source_boot_id": source_boot_id,
    "created_at": created_at,
    "accepted_at": now,
    "runs": runs,
  }


def begin_boot(boot_id: str, *, now: float | None = None) -> bool:
  """Bind a previously accepted restart to this one boot, or retire it."""
  if not _valid_token(boot_id):
    raise ValueError("invalid boot id")
  current = time.time() if now is None else now
  _prepare_ledger_dir()
  accepted = _read_bounded_json(ACCEPTED_PATH)
  if not _remove(ACK_PATH):
    raise OSError("could not retire the prior boot acknowledgement")
  authorized_payload: dict[str, Any] | None = None
  if accepted:
    try:
      accepted_at = float(accepted.get("accepted_at"))
    except (TypeError, ValueError):
      accepted_at = 0
    runs = _valid_runs(accepted.get("runs"))
    if (
      accepted.get("version") == PROTOCOL_VERSION
      and _valid_token(accepted.get("nonce"))
      and _valid_token(accepted.get("source_boot_id"))
      and accepted.get("source_boot_id") != boot_id
      and runs is not None
      and accepted_at <= current + 5
      and current - accepted_at <= MAX_ACCEPTED_AGE_SECONDS
    ):
      authorized_payload = {
        **accepted,
        "runs": runs,
        "target_boot_id": boot_id,
      }
  # Consume before acknowledging. If the volume refuses this deletion, the
  # accepted record must not be reusable by another boot; fail closed without
  # creating an acknowledgement for this one.
  if not _remove(ACCEPTED_PATH):
    raise OSError("could not consume the accepted restart intent")
  authorized = authorized_payload is not None
  if authorized_payload is not None:
    _write_json(ACK_PATH, authorized_payload, 0o444)
  _atomic_write(BOOT_PATH, f"{boot_id}\n".encode("utf-8"), 0o444)

  # Any platform-authored request not already accepted by the supervisor
  # belongs to an unrelated prior boot and must never authorize this one.
  _remove(INTENT_PATH)
  _remove(REQUEST_PATH)
  return authorized


def harden(boot_id: str) -> bool:
  """Restore ledger ownership after the entrypoint's compatibility chown."""
  if not _valid_token(boot_id):
    return False
  try:
    if LEDGER_DIR.is_symlink() or not LEDGER_DIR.is_dir():
      return False
    if BOOT_PATH.read_text(encoding="utf-8").strip() != boot_id:
      _remove(ACK_PATH)
      return False
    for path in (BOOT_PATH, ACK_PATH, ACCEPTED_PATH):
      if not path.exists():
        continue
      st = path.lstat()
      if not stat.S_ISREG(st.st_mode):
        _remove(path)
        continue
      os.chown(path, SUPERVISOR_UID, SUPERVISOR_GID)
      os.chmod(path, 0o444 if path != ACCEPTED_PATH else 0o600)
    os.chown(LEDGER_DIR, SUPERVISOR_UID, SUPERVISOR_GID)
    os.chmod(LEDGER_DIR, 0o755)
    return _trusted_ledger_dir()
  except OSError:
    _remove(ACK_PATH)
    return False


def accept(boot_id: str, *, now: float | None = None) -> bool:
  """Consume one restart request and record exact externally accepted runs."""
  current = time.time() if now is None else now
  _prepare_ledger_dir()
  try:
    ledger_boot = BOOT_PATH.read_text(encoding="utf-8").strip()
  except OSError:
    ledger_boot = ""
  intent = _read_bounded_json(INTENT_PATH)
  request = _read_bounded_json(REQUEST_PATH)
  accepted = (
    _valid_intent(intent, request, boot_id, current)
    if ledger_boot == boot_id else None
  )
  _remove(ACCEPTED_PATH)
  if accepted is not None:
    _write_json(ACCEPTED_PATH, accepted, 0o600)
  _remove(INTENT_PATH)
  _remove(REQUEST_PATH)
  return accepted is not None


def _main(argv: list[str]) -> int:
  if len(argv) != 3:
    print("usage: restart_ledger.py begin-boot|harden|accept BOOT_ID", file=sys.stderr)
    return 2
  command, boot_id = argv[1:]
  try:
    if command == "begin-boot":
      result = begin_boot(boot_id)
    elif command == "harden":
      result = harden(boot_id)
    elif command == "accept":
      result = accept(boot_id)
    else:
      return 2
  except Exception as exc:
    print(f"restart ledger {command} failed: {exc}", file=sys.stderr)
    return 1
  print(f"{command}={'accepted' if result else 'none'}")
  # `accept=none` still represents a valid restart request (for example a
  # Recovery restore); it simply grants no automatic chat continuation.
  return 0


if __name__ == "__main__":
  raise SystemExit(_main(sys.argv))
