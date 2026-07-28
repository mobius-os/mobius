"""Zone-aware cron schedules — durable IANA identity, truthful wall clocks.

Cron itself evaluates wall-clock time in the server's local timezone, which
is not a durable way to express "5:00 AM in Europe/Belgrade": the server's
offset to that zone changes at daylight-saving transitions, in either zone.

The durable schedule identity is therefore an IANA timezone name plus a
zone-local daily cron expression, declared in the app's ``init-cron.sh``
(``SCHEDULE_TZ`` / ``SCHEDULE_SOURCE``). Its live crontab materialization runs
the supervised job gate every minute; the gate compares real instants with the
declared zone clock and claims at most one run per local date.

Only simple daily expressions (numeric minute + hour, ``* * *`` date fields)
may carry a timezone. DST edge behavior is explicit:

* an ambiguous wall time runs once, at its first occurrence (``fold=0``);
* a nonexistent wall time runs at the first valid minute after the gap;
* a civil date with no valid minute at or after the requested time is skipped.

That policy gives a daily schedule one deterministic launch on ordinary and
DST-transition dates without pretending a static server-local cron expression
can preserve an IANA wall clock.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_DAILY_CRON_RE = re.compile(
  r"^\s*(\d{1,2})\s+(\d{1,2})\s+\*\s+\*\s+\*\s*$"
)
# Written by init-cron-scaffold.sh; parsed (never executed) from init-cron.sh.
_DECL_TZ_RE = re.compile(r'^SCHEDULE_TZ="([A-Za-z0-9_+/-]+)"\s*$', re.M)
_DECL_SOURCE_RE = re.compile(r'^SCHEDULE_SOURCE="([^"\n]+)"\s*$', re.M)
WALL_CLOCK_CRON = "* * * * *"


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
) -> str:
  """Return the honest live cadence for a zone-local daily schedule.

  The actual wall-clock decision belongs to the supervised job gate. A static
  offset expression is intentionally never returned: it would drift or misfire
  at the next target-zone or server-zone transition.
  """
  if parse_daily_cron(zone_cron) is None:
    raise ValueError(
      "A timezone-owned schedule must be a plain daily cron "
      f"('m h * * *'), got: {zone_cron!r}"
    )
  if not valid_timezone(tz_name):
    raise ValueError(f"Unknown IANA timezone: {tz_name!r}")
  return WALL_CLOCK_CRON


def _valid_wall_instants(local: datetime, tz: ZoneInfo) -> list[datetime]:
  """UTC instants that round-trip to ``local``, ordered earliest first."""
  instants: set[datetime] = set()
  for fold in (0, 1):
    candidate = local.replace(tzinfo=tz, fold=fold)
    instant = candidate.astimezone(timezone.utc)
    round_trip = instant.astimezone(tz)
    if (
      round_trip.replace(tzinfo=None) == local
      and round_trip.fold == fold
    ):
      instants.add(instant)
  return sorted(instants)


def wall_clock_occurrence(
  local_date: date,
  zone_cron: str,
  tz_name: str,
) -> datetime | None:
  """The UTC instant selected for one local civil date.

  Ambiguous times choose the first occurrence. Nonexistent times advance to
  the first valid minute on the same civil date. ``None`` means the remainder
  of that civil date does not exist in the zone.
  """
  parsed = parse_daily_cron(zone_cron)
  if parsed is None:
    raise ValueError(
      "A timezone-owned schedule must be a plain daily cron "
      f"('m h * * *'), got: {zone_cron!r}"
    )
  if not valid_timezone(tz_name):
    raise ValueError(f"Unknown IANA timezone: {tz_name!r}")
  minute, hour = parsed
  tz = ZoneInfo(tz_name)
  requested = datetime(
    local_date.year, local_date.month, local_date.day, hour, minute,
  )
  candidate = requested
  while candidate.date() == local_date:
    instants = _valid_wall_instants(candidate, tz)
    if instants:
      return instants[0]
    candidate += timedelta(minutes=1)
  return None


def due_wall_clock_date(
  zone_cron: str,
  tz_name: str,
  *,
  now: datetime | None = None,
) -> date | None:
  """The local date due at ``now``'s UTC minute, otherwise ``None``."""
  moment = now or datetime.now(timezone.utc)
  if moment.tzinfo is None:
    raise ValueError("now must be timezone-aware")
  moment = moment.astimezone(timezone.utc).replace(second=0, microsecond=0)
  tz = ZoneInfo(tz_name) if valid_timezone(tz_name) else None
  if tz is None:
    raise ValueError(f"Unknown IANA timezone: {tz_name!r}")
  local_date = moment.astimezone(tz).date()
  occurrence = wall_clock_occurrence(local_date, zone_cron, tz_name)
  return local_date if occurrence == moment else None


def parse_zone_declaration(init_cron_text: str) -> tuple[str, str] | None:
  """Extracts (timezone, zone_cron) from init-cron.sh text, if declared.

  Returns ``None`` only when both variables are absent. A partial or invalid
  platform-managed declaration raises ``ValueError`` so reconciliation fails
  closed instead of turning its every-minute gate into an every-minute job.
  """
  tz_match = _DECL_TZ_RE.search(init_cron_text or "")
  source_match = _DECL_SOURCE_RE.search(init_cron_text or "")
  if not tz_match and not source_match:
    return None
  if not tz_match or not source_match:
    raise ValueError("Incomplete IANA wall-clock schedule declaration")
  tz_name = tz_match.group(1)
  zone_cron = source_match.group(1).strip()
  if not valid_timezone(tz_name):
    raise ValueError(f"Unknown IANA timezone: {tz_name!r}")
  if parse_daily_cron(zone_cron) is None:
    raise ValueError(f"Invalid zone-local daily cron: {zone_cron!r}")
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
