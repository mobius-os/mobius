"""bcrypt>=5 raises ValueError on >72-byte passwords instead of silently
truncating like bcrypt 4.x did. The password-checking surface in auth.py
(main login) truncates to the first 72 bytes to preserve that historical
contract.

A >72-byte password (any password with >=18 emoji, or an ASCII password
longer than 72 chars) must hash + verify without crashing or silently
failing.
"""

import os

os.environ.setdefault("SECRET_KEY", "test-secret-key-exactly-32-chars!!")
os.environ.setdefault("DATABASE_URL", "sqlite:////tmp/mobius_pwtrunc/test.db")
os.environ.setdefault("DATA_DIR", "/tmp/mobius_pwtrunc")

from app import auth

LONG_ASCII = "a" * 100  # 100 bytes (> 72)
LONG_UNICODE = "🔒" * 30  # 120 UTF-8 bytes (> 72), multi-byte


def test_long_ascii_password_round_trips():
  h = auth.hash_password(LONG_ASCII)
  assert auth.verify_password(LONG_ASCII, h) is True


def test_long_unicode_password_round_trips():
  h = auth.hash_password(LONG_UNICODE)
  assert auth.verify_password(LONG_UNICODE, h) is True


def test_truncation_contract_matches_bcrypt_4x():
  """A >72-byte password and its first-72-byte prefix verify against the same
  hash — the silent-truncation contract bcrypt 4.x had, preserved explicitly
  so existing hashes keep verifying."""
  h = auth.hash_password(LONG_ASCII)
  prefix = LONG_ASCII.encode()[:72].decode()
  assert auth.verify_password(prefix, h) is True
