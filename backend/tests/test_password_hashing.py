"""Tests password-hash format compatibility and long-password coverage."""

import os

import bcrypt

os.environ.setdefault("SECRET_KEY", "test-secret-key-exactly-32-chars!!")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/mobius_pwtrunc/test.db")
os.environ.setdefault("DATA_DIR", "/tmp/mobius_pwtrunc")

from app import auth

LONG_ASCII = "a" * 100  # 100 bytes (> 72)
LONG_UNICODE = "🔒" * 30  # 120 UTF-8 bytes (> 72), multi-byte


def test_long_ascii_password_round_trips():
  h = auth.hash_password(LONG_ASCII)
  assert h.startswith(auth.PASSWORD_HASH_PREFIX)
  assert auth.verify_password(LONG_ASCII, h) is True


def test_long_unicode_password_round_trips():
  h = auth.hash_password(LONG_UNICODE)
  assert auth.verify_password(LONG_UNICODE, h) is True


def test_new_hash_uses_password_bytes_after_bcrypts_old_limit():
  h = auth.hash_password(LONG_ASCII)
  prefix = LONG_ASCII.encode()[:72].decode()
  assert auth.verify_password(prefix, h) is False


def test_legacy_bcrypt_hash_still_verifies_and_requests_upgrade():
  legacy = bcrypt.hashpw(
    LONG_ASCII.encode()[:72], bcrypt.gensalt(rounds=4)
  ).decode()
  prefix = LONG_ASCII.encode()[:72].decode()

  assert auth.verify_password(LONG_ASCII, legacy) is True
  assert auth.verify_password(prefix, legacy) is True
  assert auth.password_needs_rehash(legacy) is True
  assert auth.password_needs_rehash(auth.hash_password("current")) is False


def test_malformed_hash_fails_closed():
  assert auth.verify_password("anything", "not-a-hash") is False
