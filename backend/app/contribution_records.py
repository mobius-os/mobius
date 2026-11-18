"""Bounded, atomic access to Contribute's app-owned review records.

This module is deliberately independent of FastAPI routers. Contribution
services and the autopilot state machine share it without importing one
another's HTTP surfaces, which keeps record durability below the trust
boundaries that authorize GitHub actions.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

from app.config import get_settings
from app.storage_io import atomic_write

_CONTRIBUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
MAX_RECORD_BYTES = 64 * 1024


def now_iso() -> str:
  return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def records_dir(app_id: int) -> Path:
  """The folder holding one app's contribution records."""
  return Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"


def record_paths(app_id: int, record_id: str) -> tuple[Path, Path]:
  if not _CONTRIBUTION_ID.match(record_id):
    raise HTTPException(status_code=400, detail="Invalid contribution id.")
  base = records_dir(app_id)
  return base / f"{record_id}.json", base / f"{record_id}.diff"


def read_record(path: Path) -> dict:
  try:
    with path.open("rb") as handle:
      raw = handle.read(MAX_RECORD_BYTES + 1)
  except FileNotFoundError:
    raise HTTPException(status_code=404, detail="Contribution not found.")
  except OSError:
    raise HTTPException(status_code=400, detail="Contribution record is invalid.")
  if len(raw) > MAX_RECORD_BYTES:
    raise HTTPException(status_code=400, detail="Contribution record is too large.")
  try:
    data = json.loads(raw)
  except (UnicodeDecodeError, ValueError):
    raise HTTPException(status_code=400, detail="Contribution record is invalid.")
  if not isinstance(data, dict):
    raise HTTPException(status_code=400, detail="Contribution record is invalid.")
  return data


def write_record(path: Path, record: dict) -> None:
  atomic_write(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")
