#!/usr/bin/env python3
"""Consume owner credentials as stdin JSON and return a fixed outcome code."""

from __future__ import annotations

import json
from pathlib import Path
import sys


BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
  sys.path.insert(0, str(BACKEND_ROOT))

from app import auth, models
from app.config import get_settings
from app.database import SessionLocal
from app.routes.auth import _write_service_token


def main() -> int:
  try:
    values = json.load(sys.stdin)
  except Exception:
    return 2
  if not isinstance(values, dict):
    return 2

  db = SessionLocal()
  try:
    owner = db.query(models.Owner).one_or_none()
    if owner is None:
      return 3
    if get_settings().mobius_sso_enabled:
      return 4

    current_password = values.get("current_password", "")
    new_username = values.get("new_username", "").strip()
    new_password = values.get("new_password", "")
    confirm_password = values.get("confirm_password", "")
    if not auth.verify_password(current_password, owner.hashed_password):
      return 5
    if not 1 <= len(new_username) <= 64:
      return 6
    if not new_password.strip() or len(new_password) > 1024:
      return 7
    if new_password != confirm_password:
      return 8

    owner.username = new_username
    owner.hashed_password = auth.hash_password(new_password)
    owner.token_epoch += 1
    epoch = owner.token_epoch
    db.commit()
  except Exception:
    db.rollback()
    return 9
  finally:
    values.clear()
    db.close()

  try:
    _write_service_token(new_username, epoch)
  except OSError:
    return 10
  return 0


if __name__ == "__main__":
  sys.exit(main())
