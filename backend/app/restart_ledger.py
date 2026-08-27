"""Platform-facing half of the planned-restart authorization protocol."""

from __future__ import annotations

import json
import os
import secrets
import stat
import time
from pathlib import Path
from typing import Any

from app.config import get_settings


PROTOCOL_VERSION = 1
MAX_ACK_BYTES = 64 * 1024
MAX_CUTOVER_CHALLENGE_AGE_SECONDS = 120
_CUTOVER_ID_MAX = 160


def current_boot_id() -> str:
  return os.environ.get("MOBIUS_BOOT_ID", "")


def new_nonce() -> str:
  return secrets.token_urlsafe(32)


def _paths() -> tuple[Path, Path, Path]:
  root = Path(get_settings().data_dir)
  return (
    root / ".restart-continuation-intent.json",
    root / ".platform-restart-requested",
    root / ".restart-ledger",
  )


def _read_trusted_json(
  path: Path,
  *,
  mode: int,
  trusted_uid: int = 0,
  trusted_gid: int = 0,
) -> dict[str, Any] | None:
  """Read one small immutable supervisor record, failing closed."""
  try:
    st = path.lstat()
    if (
      not stat.S_ISREG(st.st_mode)
      or st.st_uid != trusted_uid
      or st.st_gid != trusted_gid
      or stat.S_IMODE(st.st_mode) != mode
      or st.st_size > MAX_ACK_BYTES
    ):
      return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
      raw = os.read(fd, MAX_ACK_BYTES + 1)
    finally:
      os.close(fd)
    value = json.loads(raw.decode("utf-8"))
    return value if isinstance(value, dict) else None
  except (OSError, UnicodeError, json.JSONDecodeError):
    return None


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
  payload = json.dumps(
    value, sort_keys=True, separators=(",", ":"),
  ).encode("utf-8")
  tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
  flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
  flags |= getattr(os, "O_NOFOLLOW", 0)
  try:
    fd = os.open(tmp, flags, 0o600)
    try:
      offset = 0
      while offset < len(payload):
        offset += os.write(fd, payload[offset:])
      os.fsync(fd)
    finally:
      os.close(fd)
    os.replace(tmp, path)
  except Exception:
    tmp.unlink(missing_ok=True)
    raise


def request_restart(
  *,
  boot_id: str,
  nonce: str,
  runs: list[dict[str, str]],
  now: float | None = None,
) -> None:
  """Publish intent first, then the sentinel the frozen poller consumes.

  ``runs`` is retained in protocol v1 for supervisors baked before the
  nonce-only simplification. Newer supervisors ignore the extra field and
  attest only the nonce; older supervisors require it before accepting the
  restart. Keeping the field lets a platform-only update cross either frozen
  image generation safely.
  """
  intent_path, request_path, _ = _paths()
  created_at = time.time() if now is None else now
  normalized = [
    {"chat_id": str(item["chat_id"]), "run_token": str(item["run_token"])}
    for item in runs
  ]
  _atomic_json(intent_path, {
    "version": PROTOCOL_VERSION,
    "nonce": nonce,
    "source_boot_id": boot_id,
    "created_at": created_at,
    "runs": normalized,
  })
  _atomic_json(request_path, {
    "nonce": nonce,
    "source_boot_id": boot_id,
  })


def publish_cutover_intent(
  *,
  boot_id: str,
  nonce: str,
  cutover_id: str,
  runs: list[dict[str, str]],
  now: float | None = None,
) -> None:
  """Publish a Host-accepted cutover intent without self-terminating.

  Unlike :func:`request_restart`, this deliberately does not create the
  entrypoint sentinel.  The root Host helper must accept the exact cutover id;
  Docker/Compose then owns the stop and the immediately following boot.
  """
  if not cutover_id or len(cutover_id) > _CUTOVER_ID_MAX:
    raise ValueError("invalid cutover id")
  intent_path, _request_path, _ledger_dir = _paths()
  created_at = time.time() if now is None else now
  normalized = [
    {"chat_id": str(item["chat_id"]), "run_token": str(item["run_token"])}
    for item in runs
  ]
  _atomic_json(intent_path, {
    "version": PROTOCOL_VERSION,
    "action": "external_cutover",
    "cutover_id": cutover_id,
    "nonce": nonce,
    "source_boot_id": boot_id,
    "created_at": created_at,
    "runs": normalized,
  })


def request_managed_cutover(
  *, boot_id: str, cutover_id: str, now: float | None = None,
) -> None:
  """Ask the baked root poller to open one managed-platform handoff."""
  if not boot_id or not cutover_id or len(cutover_id) > _CUTOVER_ID_MAX:
    raise ValueError("invalid managed cutover")
  root = Path(get_settings().data_dir)
  _atomic_json(root / ".managed-cutover-request.json", {
    "version": PROTOCOL_VERSION,
    "action": "managed_cutover",
    "cutover_id": cutover_id,
    "source_boot_id": boot_id,
    "created_at": time.time() if now is None else now,
  })


def accepted_cutover_receipt(
  cutover_id: str,
  *,
  boot_id: str | None = None,
  now: float | None = None,
  trusted_uid: int = 0,
  trusted_gid: int = 0,
) -> bool:
  """Whether root accepted this cutover for the current source boot."""
  expected_boot = boot_id if boot_id is not None else current_boot_id()
  if not expected_boot or not cutover_id:
    return False
  _, _, ledger_dir = _paths()
  value = _read_trusted_json(
    ledger_dir / "cutover-receipt.json",
    mode=0o444,
    trusted_uid=trusted_uid,
    trusted_gid=trusted_gid,
  )
  if not value:
    return False
  try:
    accepted_at = float(value.get("accepted_at"))
  except (TypeError, ValueError):
    return False
  current = time.time() if now is None else now
  return bool(
    value.get("version") == PROTOCOL_VERSION
    and value.get("action") == "external_cutover"
    and value.get("cutover_id") == cutover_id
    and value.get("source_boot_id") == expected_boot
    and accepted_at <= current + 5
    and current - accepted_at <= 120
  )


def authorized_cutover_challenge(
  cutover_id: str,
  *,
  boot_id: str | None = None,
  now: float | None = None,
  trusted_uid: int = 0,
  trusted_gid: int = 0,
) -> bool:
  """Whether the root supervisor opened this cutover on the current boot."""
  expected_boot = boot_id if boot_id is not None else current_boot_id()
  if not expected_boot or not cutover_id:
    return False
  _, _, ledger_dir = _paths()
  value = _read_trusted_json(
    ledger_dir / "cutover-challenge.json",
    mode=0o444,
    trusted_uid=trusted_uid,
    trusted_gid=trusted_gid,
  )
  if not value:
    return False
  try:
    created_at = float(value.get("created_at"))
  except (TypeError, ValueError):
    return False
  current = time.time() if now is None else now
  return bool(
    value.get("version") == PROTOCOL_VERSION
    and value.get("source_boot_id") == expected_boot
    and value.get("cutover_id") == cutover_id
    and created_at <= current + 5
    and current - created_at <= MAX_CUTOVER_CHALLENGE_AGE_SECONDS
  )


def authorized_restart_nonce(
  boot_id: str | None = None,
  *,
  trusted_uid: int = 0,
  trusted_gid: int = 0,
) -> str | None:
  """Return the planned-restart nonce accepted for this exact boot."""
  expected_boot = boot_id if boot_id is not None else current_boot_id()
  if not expected_boot:
    return None
  _, _, ledger_dir = _paths()
  ack_path = ledger_dir / "ack.json"
  try:
    dir_st = ledger_dir.lstat()
    if (
      not stat.S_ISDIR(dir_st.st_mode)
      or stat.S_ISLNK(dir_st.st_mode)
      or dir_st.st_uid != trusted_uid
      or dir_st.st_gid != trusted_gid
      or dir_st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
      return None
  except OSError:
    return None
  value = _read_trusted_json(
    ack_path,
    mode=0o444,
    trusted_uid=trusted_uid,
    trusted_gid=trusted_gid,
  )
  if value is None:
    return None
  if (
    not isinstance(value, dict)
    or value.get("version") != PROTOCOL_VERSION
    or value.get("target_boot_id") != expected_boot
    or not isinstance(value.get("nonce"), str)
    or not value["nonce"]
    or len(value["nonce"]) > 160
  ):
    return None
  return value["nonce"]
