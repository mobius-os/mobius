"""Zone-aware cron schedules — durable IANA identity, materialized entries.

Cron itself evaluates wall-clock time in the server's local timezone, which
is not a durable way to express "5:00 AM in Europe/Belgrade": the server's
offset to that zone changes at daylight-saving transitions, in either zone.

The durable schedule identity is therefore an IANA timezone name plus a
zone-local daily cron expression, declared in the app's ``init-cron.sh``
(``SCHEDULE_TZ`` / ``SCHEDULE_SOURCE``). The crontab line cron actually runs
is a *materialization* of that identity into the server's current clock,
recomputed at boot and periodically at runtime so an offset change in either
zone reschedules the entry instead of silently drifting the wall time.

Only simple daily expressions (numeric minute + hour, ``* * *`` date fields)
may carry a timezone: shifting an expression across an offset can cross a day
boundary, which is well-defined for a daily job and ambiguous for date-pinned
ones.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_DAILY_CRON_RE = re.compile(
  r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s*$"
)
# Written by init-cron-scaffold.sh; parsed (never executed) from init-cron.sh.
_DECL_TZ_RE = re.compile(r'^SCHEDULE_TZ="([A-Za-z0-9_+/-]+)"\s*$', re.M)
_DECL_SOURCE_RE = re.compile(r'^SCHEDULE_SOURCE="([^"\n]+)"\s*$', re.M)


def valid_timezone(name: str) -> bool:
  """True when ``name`` is a resolvable IANA timezone identifier."""
  if not isinstance(name, str) or not name or len(name) > 64:
    return False
  if not re.fullmatch(r"[A-Za-z0-9_+/-]+", name):
    return False
  try:
    ZoneInfo(name)
  except Exception:
    return False
  return True


def parse_daily_cron(expr: str) -> tuple[int, int] | None:
  """Returns (minute, hour) for a plain daily cron, else None."""
  m = _DAILY_CRON_RE.match(expr or "")
  if not m:
    return None
  minute, hour = int(m.group(1)), int(m.group(2))
  if minute > 59 or hour > 23:
    return None
  return minute, hour


def materialize_zone_cron(
  zone_cron: str,
  tz_name: str,
  *,
  now: datetime | None = None,
  server_tz=None,
) -> str:
  """Expresses a zone-local daily cron in the server's current clock.

  ``now`` and ``server_tz`` exist for tests; production uses the real clock
  and the process-local timezone. The conversion is exact for the current
  offset pair — the caller re-materializes when offsets change, which is the
  whole durability contract.

  Raises ValueError for a non-daily expression or unknown timezone.
  """
  parsed = parse_daily_cron(zone_cron)
  if parsed is None:
    raise ValueError(
      "A timezone-owned schedule must be a plain daily cron "
      f"('m h * * *'), got: {zone_cron!r}"
    )
  minute, hour = parsed
  if not valid_timezone(tz_name):
    raise ValueError(f"Unknown IANA timezone: {tz_name!r}")
  tz = ZoneInfo(tz_name)
  moment = (now or datetime.now(tz)).astimezone(tz)
  local = moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
  server = local.astimezone(server_tz)
  return f"{server.minute} {server.hour} * * *"


def parse_zone_declaration(init_cron_text: str) -> tuple[str, str] | None:
  """Extracts (timezone, zone_cron) from init-cron.sh text, if declared.

  Returns None when either variable is absent or invalid — an app whose
  declaration was hand-edited into an inconsistent state falls back to plain
  server-local scheduling rather than guessing.
  """
  tz_match = _DECL_TZ_RE.search(init_cron_text or "")
  source_match = _DECL_SOURCE_RE.search(init_cron_text or "")
  if not tz_match or not source_match:
    return None
  tz_name = tz_match.group(1)
  zone_cron = source_match.group(1).strip()
  if not valid_timezone(tz_name) or parse_daily_cron(zone_cron) is None:
    return None
  return tz_name, zone_cron


def server_timezone_name() -> str:
  """The server clock's IANA identity: TZ env, /etc/localtime, else UTC.

  Containers conventionally run UTC with neither signal present; "UTC" is a
  valid IANA identifier, so the fallback stays truthful for them.
  """
  tz_env = os.environ.get("TZ", "").strip()
  if tz_env and valid_timezone(tz_env):
    return tz_env
  try:
    target = Path("/etc/localtime").resolve()
    parts = target.parts
    if "zoneinfo" in parts:
      name = "/".join(parts[parts.index("zoneinfo") + 1:])
      if valid_timezone(name):
        return name
  except OSError:
    pass
  return "UTC"
