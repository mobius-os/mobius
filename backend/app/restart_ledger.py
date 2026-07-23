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


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
  payload = json.dumps(
    value, sort_keys=True, separators=(",", ":"),
  ).encode("utf-8")
  tmp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
  flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
  flags |= getattr(os, "O_NOFOLLOW", 0)
  fd = os.open(tmp, flags, 0o600)
  try:
    offset = 0
    while offset < len(payload):
      offset += os.write(fd, payload[offset:])
    os.fsync(fd)
  finally:
    os.close(fd)
  os.replace(tmp, path)


def request_restart(
  *,
  boot_id: str,
  nonce: str,
  runs: list[dict[str, str]],
  now: float | None = None,
) -> None:
  """Publish intent first, then the sentinel the frozen poller consumes."""
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


def authorized_runs(
  boot_id: str | None = None,
  *,
  trusted_uid: int = 0,
  trusted_gid: int = 0,
) -> dict[str, tuple[str, str]]:
  """Return ``run_token -> (chat_id, nonce)`` for this authenticated boot."""
  expected_boot = boot_id if boot_id is not None else current_boot_id()
  if not expected_boot:
    return {}
  _, _, ledger_dir = _paths()
  ack_path = ledger_dir / "ack.json"
  try:
    dir_st = ledger_dir.lstat()
    ack_st = ack_path.lstat()
    if (
      not stat.S_ISDIR(dir_st.st_mode)
      or stat.S_ISLNK(dir_st.st_mode)
      or dir_st.st_uid != trusted_uid
      or dir_st.st_gid != trusted_gid
      or dir_st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
      or not stat.S_ISREG(ack_st.st_mode)
      or ack_st.st_uid != trusted_uid
      or ack_st.st_gid != trusted_gid
      or ack_st.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
      or ack_st.st_size > MAX_ACK_BYTES
    ):
      return {}
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(ack_path, flags)
    try:
      raw = os.read(fd, MAX_ACK_BYTES + 1)
    finally:
      os.close(fd)
    value = json.loads(raw.decode("utf-8"))
  except (OSError, UnicodeError, json.JSONDecodeError):
    return {}
  if (
    not isinstance(value, dict)
    or value.get("version") != PROTOCOL_VERSION
    or value.get("target_boot_id") != expected_boot
    or not isinstance(value.get("nonce"), str)
    or not isinstance(value.get("runs"), list)
  ):
    return {}
  nonce = value["nonce"]
  result: dict[str, tuple[str, str]] = {}
  for item in value["runs"]:
    if not isinstance(item, dict):
      return {}
    chat_id = item.get("chat_id")
    run_token = item.get("run_token")
    if not isinstance(chat_id, str) or not isinstance(run_token, str):
      return {}
    if run_token in result:
      return {}
    result[run_token] = (chat_id, nonce)
  return result
