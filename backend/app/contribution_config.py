"""Runtime-resolved contribution relay configuration.

The relay routing an owner tunes while testing — which repository reviewed
contributions target, and which repositories the anonymous-owner guard treats
as allowed test targets — is operational configuration, not container topology.
Reading it live from ``/data`` means changing it is an ordinary settings write,
never a container recreate: baking it into container environment
(docker-compose) once forced a chat to recreate the production container just to
point the relay at a test fork, which stranded the running turn mid-recreate.

Each value resolves from the ``/data`` override file first, then the process
environment (the compose-provided default). A missing override uses the
environment. A present but unreadable or malformed override stops publication
rather than silently falling back to a different repository.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from app.config import get_settings

TARGET_REPO_ENV = "MOBIUS_CONTRIBUTION_TARGET_REPO"
TEST_REPOSITORIES_ENV = "MOBIUS_CONTRIBUTION_RELAY_TEST_REPOSITORIES"

_OVERRIDE_RELPATH = "shared/contribution-relay.json"


class ContributionConfigError(RuntimeError):
  """The owner-authored runtime override exists but cannot be trusted."""


def _override_path() -> Path:
  return Path(get_settings().data_dir) / _OVERRIDE_RELPATH


def _override(key: str) -> str | None:
  """Return an owner-set value, or None when that key/file is absent."""
  try:
    raw = _override_path().read_text(encoding="utf-8")
  except FileNotFoundError:
    return None
  except (OSError, ValueError) as exc:
    raise ContributionConfigError(
      "the contribution relay override could not be read"
    ) from exc
  try:
    data = json.loads(raw)
  except ValueError as exc:
    raise ContributionConfigError(
      "the contribution relay override is not valid JSON"
    ) from exc
  if not isinstance(data, dict):
    raise ContributionConfigError(
      "the contribution relay override must be a JSON object"
    )
  if key not in data:
    return None
  value = data[key]
  if not isinstance(value, str):
    raise ContributionConfigError(
      f"the contribution relay override field {key!r} must be a string"
    )
  return value.strip()


def target_repo() -> str:
  """The GitHub repository reviewed contributions are opened against.

  Resolved live so the owner can retarget or deliberately clear the relay
  target without recreating the container.
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
