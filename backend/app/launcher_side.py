"""Which launcher this boot uses: the default one, or a developer's staging.

An ordinary instance has one launcher configuration and nothing here applies
to it. A developer's instance also carries a staging configuration under
``MOBIUS_STAGING_*``. One word in the data directory says which of the two the
next boot uses, and changing it takes a restart: the launcher address is read
once, at start.

The identity broker owns that decision (``select_launcher_side`` in
``backend/runtime/identity_broker.py``): it starts first, as root, and holds
the credentials. This server only asks what was decided and follows it, so the
two can never disagree.
"""

from __future__ import annotations

import os
import time
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.runtime_identity import broker_client, broker_socket_path

PRODUCTION = "production"
STAGING = "staging"
SIDE_FILE = "launcher-side"
BROKER_RETRIES = 3
STAGING_ENV = (
  "MOBIUS_STAGING_ISSUER",
  "MOBIUS_STAGING_CLIENT_SECRET",
  "MOBIUS_STAGING_BOOTSTRAP",
)


@dataclass(frozen=True)
class LauncherSide:
  """What this boot found: whether staging is configured, and the side in use."""

  available: bool
  side: str


def _broker_decision() -> dict:
  """The broker's answer, or nothing when it has none (not running, or older)."""
  try:
    with broker_client(timeout=2.0) as client:
      response = client.get("/identity/launcher-side")
    value = response.json() if response.status_code == 200 else None
  except (httpx.HTTPError, ValueError, OSError):
    return {}
  return value if isinstance(value, dict) else {}


def _follow_broker(environ: MutableMapping[str, str]) -> LauncherSide:
  staging_configured = bool(environ.get("MOBIUS_STAGING_ISSUER"))
  # Keeps the staging credential out of what this server passes on. Only the
  # image's start script can keep it out of this process's own environment.
  for name in STAGING_ENV:
    environ.pop(name, None)
  decision = _broker_decision()
  # A missed answer would leave this server on the default launcher beside a
  # broker on staging, so a running broker is asked again before the boot
  # settles. Without its socket there is no broker yet (the boot steps that
  # load settings before it starts), and waiting would only cost seconds.
  broker_running = os.path.exists(broker_socket_path())
  for _ in range(BROKER_RETRIES if staging_configured and broker_running else 0):
    if decision:
      break
    time.sleep(1.0)
    decision = _broker_decision()
  available = decision.get("available") is True
  launcher = decision.get("launcher")
  if decision.get("side") != STAGING or not isinstance(launcher, str) or not launcher:
    return LauncherSide(available=available, side=PRODUCTION)
  environ["MOBIUS_IDENTITY_ISSUER"] = launcher
  environ["MOBIUS_SSO_ISSUER"] = launcher
  return LauncherSide(available=True, side=STAGING)


_boot_side: LauncherSide | None = None


def boot_side() -> LauncherSide:
  """The side this server started on. The first call follows the broker."""
  global _boot_side
  if _boot_side is None:
    _boot_side = _follow_broker(os.environ)
  return _boot_side


def choose_side(side: str, data_dir: Path) -> None:
  """Record the side the next boot uses. Production is the absence of a choice."""
  path = data_dir / SIDE_FILE
  if side == STAGING:
    path.write_text(STAGING + "\n", encoding="utf-8")
  else:
    path.unlink(missing_ok=True)
