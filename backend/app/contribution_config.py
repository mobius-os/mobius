"""Runtime-resolved contribution relay configuration.

The relay routing an owner tunes while testing — which repository reviewed
contributions target, and which repositories the anonymous-owner guard treats
as allowed test targets — is operational configuration, not container topology.
Reading it live from ``/data`` means changing it is an ordinary settings write,
never a container recreate: baking it into container environment
(docker-compose) once forced a chat to recreate the production container just to
point the relay at a test fork, which stranded the running turn mid-recreate.

Each value resolves from the ``/data`` override file first, then the process
environment (the compose-provided default). A missing, unreadable, or malformed
override degrades to the environment rather than breaking a submission, and the
override file mirrors the environment's string format for each key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.config import get_settings

TARGET_REPO_ENV = "MOBIUS_CONTRIBUTION_TARGET_REPO"
TEST_REPOSITORIES_ENV = "MOBIUS_CONTRIBUTION_RELAY_TEST_REPOSITORIES"

_OVERRIDE_RELPATH = "shared/contribution-relay.json"


def _override_path() -> Path:
  return Path(get_settings().data_dir) / _OVERRIDE_RELPATH


def _override(key: str) -> str | None:
  """Return the owner-set runtime value for ``key``, or None to defer to env.

  A present non-blank string wins. Anything else degrades to None so the caller
  falls through to the environment rather than breaking or blocking a
  submission on a bad edit: a missing or unreadable file, non-UTF-8 bytes,
  malformed or non-object JSON, an absent key, a non-string value (a number,
  bool, list, or null), or a blank string.
  """
  try:
    raw = _override_path().read_text(encoding="utf-8")
  except (OSError, ValueError):
    # OSError: missing / unreadable file. ValueError: non-UTF-8 bytes
    # (UnicodeDecodeError is a ValueError).
    return None
  try:
    data = json.loads(raw)
  except ValueError:
    return None
  if not isinstance(data, dict):
    return None
  value = data.get(key)
  if not isinstance(value, str):
    return None
  return value.strip() or None


def target_repo() -> str:
  """The GitHub repository reviewed contributions are opened against.

  Resolved live so the owner can retarget the relay without recreating the
  container. Returns the empty string when nothing is configured in either the
  override or the environment; the caller treats that as "no target chosen".
  """
  override = _override("target_repo")
  if override is not None:
    return override
  return os.environ.get(TARGET_REPO_ENV, "").strip()


def test_repositories() -> set[str]:
  """Casefolded repositories the anonymous-owner guard treats as allowed test
  targets.

  Resolved live (override then environment) from the same comma-separated
  string format the environment variable uses.
  """
  override = _override("test_repositories")
  raw = override if override is not None else os.environ.get(
    TEST_REPOSITORIES_ENV, "",
  )
  return {item.strip().casefold() for item in raw.split(",") if item.strip()}
