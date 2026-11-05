"""One-time code that proves the person creating the owner runs this server.

A self-hosted Möbius with no owner yet accepts a username and password from
whoever reaches its setup page first, and that owner gets the agent's full
authority. Publishing a domain (and its HTTPS certificate, which lands in public
certificate-transparency logs) can bring scanners within minutes. The code
closes that window: it is printed to the server log and written to
``<data_dir>/setup-code`` (mode 0600), which only someone with host or
deployment access can read, and creating the owner requires it.

Managed deployments never use this path: their owner is bound through managed
sign-in, and local setup is refused outright.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from app.config import get_settings

log = logging.getLogger("moebius.auth")

FILENAME = "setup-code"
# Unambiguous uppercase letters and digits (no 0/O, 1/I/L), so a code copied by
# eye from a log is typed correctly. 16 symbols from 31 is about 79 bits.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_LENGTH = 16


def code_path() -> Path:
  return Path(get_settings().data_dir) / FILENAME


def _normalize(value: str) -> str:
  # ASCII only: the alphabet is ASCII, and compare_digest rejects other text.
  return "".join(ch for ch in value.upper() if ch.isascii() and ch.isalnum())


def _format(raw: str) -> str:
  return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def _read(path: Path) -> str | None:
  try:
    value = _normalize(path.read_text(encoding="utf-8"))
  except FileNotFoundError:
    return None
  return value if len(value) == _LENGTH else None


def current(*, announce: bool = False) -> str:
  """Return the pending setup code, creating and logging it when absent.

  The code survives restarts until setup succeeds, so the operator can read it
  from either the log or the file at any time. ``announce`` logs an existing
  code again (used once per boot); a newly created code is always logged.
  """
  path = code_path()
  code = _read(path)
  created = False
  if code is None:
    code = "".join(secrets.choice(_ALPHABET) for _ in range(_LENGTH))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{FILENAME}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      handle.write(_format(code) + "\n")
    os.replace(tmp, path)
    created = True
  if created or announce:
    log.warning(
      "\n\n  Möbius setup code: %s\n"
      "  Enter it on the setup page to create the owner account.\n"
      "  It is also saved in %s.\n",
      _format(code), path,
    )
  return code


def matches(candidate: str | None) -> bool:
  """Constant-time check of an entered code against the pending one."""
  expected = _read(code_path())
  if expected is None or not isinstance(candidate, str) or not candidate.isascii():
    return False
  return secrets.compare_digest(_normalize(candidate), expected)


def retire() -> None:
  """Delete the code once the owner exists; it must never work twice."""
  try:
    code_path().unlink()
  except FileNotFoundError:
    pass
