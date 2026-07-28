"""Zone-aware schedule materialization — including DST boundaries.

The durable schedule identity is (IANA timezone, zone-local daily cron);
the crontab entry is a server-local materialization recomputed when either
zone's UTC offset changes. These tests pin the conversion on both sides of
the 2026 European transitions (spring forward 2026-03-29, fall back
2026-10-25) and in both directions of ownership.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import cron_tz

UTC = ZoneInfo("UTC")
BELGRADE = ZoneInfo("Europe/Belgrade")


def test_parse_daily_cron_accepts_plain_daily():
  assert cron_tz.parse_daily_cron("0 5 * * *") == (0, 5)
  assert cron_tz.parse_daily_cron("30 23 * * *") == (30, 23)


@pytest.mark.parametrize("expr", [
  "0 5 * * 1",       # weekday-pinned
  "0 5 1 * *",       # date-pinned
  "*/5 5 * * *",     # stepped minute
  "0 24 * * *",      # invalid hour
  "60 5 * * *",      # invalid minute
  "0 5 * *",         # too few fields
  "",
])
def test_parse_daily_cron_rejects_non_daily(expr):
  assert cron_tz.parse_daily_cron(expr) is None


def test_materialize_summer_offset_on_utc_server():
  # 2026-07-01: Belgrade is UTC+2 → 5:00 local = 3:00 server.
  now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
  assert cron_tz.materialize_zone_cron(
    "0 5 * * *", "Europe/Belgrade", now=now, server_tz=UTC,
  ) == "0 3 * * *"


def test_materialize_winter_offset_on_utc_server():
  # 2026-12-01: Belgrade is UTC+1 → 5:00 local = 4:00 server.
  now = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
  assert cron_tz.materialize_zone_cron(
    "0 5 * * *", "Europe/Belgrade", now=now, server_tz=UTC,
  ) == "0 4 * * *"


def test_materialize_across_spring_forward_boundary():
  # Europe's 2026 spring transition: 2026-03-29 02:00 CET → 03:00 CEST.
  before = datetime(2026, 3, 28, 12, 0, tzinfo=UTC)
  after = datetime(2026, 3, 29, 12, 0, tzinfo=UTC)
  materialize = lambda now: cron_tz.materialize_zone_cron(
    "0 5 * * *", "Europe/Belgrade", now=now, server_tz=UTC,
  )
  assert materialize(before) == "0 4 * * *"   # +1 before the switch
  assert materialize(after) == "0 3 * * *"    # +2 after — entry rescheduled


def test_materialize_across_fall_back_boundary():
  # Europe's 2026 fall transition: 2026-10-25 03:00 CEST → 02:00 CET.
  before = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
  after = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
  materialize = lambda now: cron_tz.materialize_zone_cron(
    "0 5 * * *", "Europe/Belgrade", now=now, server_tz=UTC,
  )
  assert materialize(before) == "0 3 * * *"
  assert materialize(after) == "0 4 * * *"


def test_materialize_when_server_itself_observes_dst():
  # Server clock in Berlin, schedule owned in UTC: the SERVER side of the
  # conversion moves at ITS transition, the schedule's identity does not.
  berlin = ZoneInfo("Europe/Berlin")
  materialize = lambda now: cron_tz.materialize_zone_cron(
    "0 5 * * *", "UTC", now=now, server_tz=berlin,
  )
  assert materialize(datetime(2026, 7, 1, 12, 0, tzinfo=UTC)) == "0 7 * * *"
  assert materialize(datetime(2026, 12, 1, 12, 0, tzinfo=UTC)) == "0 6 * * *"


def test_materialize_wraps_across_the_day_boundary():
  # 00:30 in Kathmandu (UTC+5:45) is 18:45 the previous day in UTC; a
  # daily job simply wraps — date fields are '*' by contract.
  now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
  assert cron_tz.materialize_zone_cron(
    "30 0 * * *", "Asia/Kathmandu", now=now, server_tz=UTC,
  ) == "45 18 * * *"


def test_materialize_rejects_non_daily_and_unknown_zone():
  now = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
  with pytest.raises(ValueError):
    cron_tz.materialize_zone_cron("0 5 * * 1", "UTC", now=now, server_tz=UTC)
  with pytest.raises(ValueError):
    cron_tz.materialize_zone_cron(
      "0 5 * * *", "Not/AZone", now=now, server_tz=UTC,
    )


def test_parse_zone_declaration_round_trip():
  text = (
    "#!/bin/sh\n"
    'ENTRY="0 3 * * * python3 /app/scripts/app-job-runner.py 4 '
    '/data/apps/memory/fetch.sh"\n'
    "# --- Zone-aware schedule identity (platform-managed).\n"
    'SCHEDULE_TZ="Europe/Belgrade"\n'
    'SCHEDULE_SOURCE="0 5 * * *"\n'
  )
  assert cron_tz.parse_zone_declaration(text) == (
    "Europe/Belgrade", "0 5 * * *",
  )


@pytest.mark.parametrize("text", [
  "",
  'SCHEDULE_TZ="Europe/Belgrade"\n',                     # half a declaration
  'SCHEDULE_SOURCE="0 5 * * *"\n',                       # half a declaration
  'SCHEDULE_TZ="Nope"\nSCHEDULE_SOURCE="0 5 * * *"\n',   # unknown zone
  'SCHEDULE_TZ="UTC"\nSCHEDULE_SOURCE="0 5 * * 1"\n',    # non-daily source
])
def test_parse_zone_declaration_rejects_incomplete(text):
  assert cron_tz.parse_zone_declaration(text) is None


def test_server_timezone_name_prefers_valid_tz_env(monkeypatch):
  monkeypatch.setenv("TZ", "Europe/Belgrade")
  assert cron_tz.server_timezone_name() == "Europe/Belgrade"
  monkeypatch.setenv("TZ", "Total/Nonsense")
  # Invalid TZ falls through to /etc/localtime or the UTC default —
  # either way a valid IANA identifier comes back.
  assert cron_tz.valid_timezone(cron_tz.server_timezone_name())
