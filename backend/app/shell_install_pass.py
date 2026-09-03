"""Durable data contract for the iOS Home Screen shell sign-in handoff."""

import hashlib
from datetime import timedelta

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.database import Base
from app.timeutil import now_naive_utc


COOKIE_NAME = "mobius_shell_install"
COOKIE_PATH = "/api/auth/shell-install-pass/redeem"
GRANT_TTL = timedelta(minutes=30)
SESSION_TTL = timedelta(days=30)


def hash_secret(secret: str) -> str:
  """Hash the browser-visible opaque secret before durable storage."""
  return hashlib.sha256(secret.encode("utf-8")).hexdigest()


class ShellInstallPassGrant(Base):
  """Opaque one-use session copied into a newly installed iOS shell.

  This protocol has its own table so neither its raw opaque cookie nor a
  mobius.you handoff ``jti`` can ever be interpreted by the other redemption
  path. ``create_all`` adds the table to existing installations without an
  in-place column migration.
  """

  __tablename__ = "shell_install_pass_grants"

  id = Column(Integer, primary_key=True, autoincrement=True)
  token_hash = Column(String(64), nullable=False, unique=True, index=True)
  owner_id = Column(Integer, ForeignKey("owner.id"), nullable=False, index=True)
  owner_epoch = Column(Integer, nullable=False)
  created_at = Column(DateTime, nullable=False, default=now_naive_utc)
  expires_at = Column(DateTime, nullable=False, index=True)
  consumed_at = Column(DateTime, nullable=True, default=None, index=True)
