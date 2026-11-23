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

Every session this server issues belongs to the launcher that signed its owner
in (``session_issuer``). A session from the other launcher must never carry
over a switch: tokens are refused under any other issuer, and the first boot on
a new issuer signs everyone out (``settle_sessions``). ``published_issuer`` is
what ``/api/health`` tells anyone who asks, such as the staging phone app.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.runtime_identity import broker_client, broker_socket_path

log = logging.getLogger(__name__)

PRODUCTION = "production"
STAGING = "staging"
SIDE_FILE = "launcher-side"
# The issuer the sessions in the database were issued under, recorded at boot.
SESSION_ISSUER_FILE = "session-issuer"
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


def _chosen_side(data_dir: Path) -> str:
  """The side the next boot will run, by the broker's rule."""
  try:
    chosen = (data_dir / SIDE_FILE).read_text(encoding="utf-8").strip()
  except (OSError, UnicodeError):
    chosen = ""
  return STAGING if chosen == STAGING and boot_side().available else PRODUCTION


def session_issuer() -> str:
  """The launcher every session of this boot belongs to; "" with local sign-in."""
  from app.config import get_settings

  settings = get_settings()
  return settings.mobius_sso_issuer if settings.mobius_sso_enabled else ""


def is_developer_instance() -> bool:
  """Whether this instance can run on a second launcher, or still does."""
  side = boot_side()
  return side.available or side.side == STAGING


def published_issuer() -> str | None:
  """The launcher this instance sends people to sign in with, for anyone.

  None when it has no launcher sign-in, and while a switch to the other side
  waits for its restart: the current issuer would be about to stop being true.
  """
  from app.config import get_settings

  issuer = session_issuer()
  if not issuer or _chosen_side(Path(get_settings().data_dir)) != boot_side().side:
    return None
  return issuer


def settle_sessions(db, data_dir: Path) -> bool:
  """Sign everyone out when this boot's issuer is not the one sessions had.

  Bumping the owner's token epoch is "sign out everywhere": it ends every
  owner token, browser-access and install session, handoff, delegation and
  service token at once. The record is written only after the bump commits,
  so a boot that dies in between bumps again rather than not at all. Tokens
  also carry their issuer (see ``app.auth``), so even a missed bump cannot
  carry a session over a switch.

  An ordinary instance's first boot only records its issuer. A developer's
  instance has no record of where its older sessions came from, so its first
  boot signs everyone out once. Returns whether sessions were ended.
  """
  from app import auth, models
  from app.storage_io import atomic_write

  current = session_issuer()
  path = data_dir / SESSION_ISSUER_FILE
  try:
    recorded: str | None = path.read_text(encoding="utf-8").strip()
  except FileNotFoundError:
    recorded = None
  except (OSError, UnicodeError):
    # Where earlier sessions came from is unknown: treat it as a switch.
    recorded = "unreadable"
  if recorded == current:
    return False
  revoke = recorded is not None or is_developer_instance()
  if revoke:
    db.query(models.Owner).update(
      {models.Owner.token_epoch: models.Owner.token_epoch + 1},
      synchronize_session=False,
    )
    db.commit()
    log.warning("launcher sign-in changed; every earlier session was ended")
    # The Host controller drains a cutover with this file; keep it usable.
    for owner in db.query(models.Owner).all():
      try:
        auth.write_service_token(owner.username, owner.token_epoch)
      except OSError as exc:
        log.warning("Could not refresh the service token: %s", exc)
  atomic_write(path, current + "\n")
  return revoke
