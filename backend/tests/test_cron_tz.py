"""IANA wall-clock scheduling, including DST gaps and folds."""

from datetime import date, datetime, timezone

import pytest

from app import cron_tz

UTC = timezone.utc


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


def test_materialization_is_a_gate_not_a_static_offset_snapshot():
  assert cron_tz.materialize_zone_cron(
    "0 5 * * *", "Europe/Belgrade",
  ) == "* * * * *"
  assert cron_tz.materialize_zone_cron(
    "30 0 * * *", "Asia/Kathmandu",
  ) == "* * * * *"


def test_ordinary_wall_clock_occurrence_tracks_seasonal_offset():
  # The durable identity stays 05:00 Belgrade; its real UTC instant changes.
  assert cron_tz.wall_clock_occurrence(
    date(2026, 7, 1), "0 5 * * *", "Europe/Belgrade",
  ) == datetime(2026, 7, 1, 3, 0, tzinfo=UTC)
  assert cron_tz.wall_clock_occurrence(
    date(2026, 12, 1), "0 5 * * *", "Europe/Belgrade",
  ) == datetime(2026, 12, 1, 4, 0, tzinfo=UTC)


def test_nonexistent_wall_time_runs_at_first_valid_minute_after_gap():
  # 2026-03-29 jumps from 01:59:59 UTC / 02:59:59 CET to 03:00 CEST.
  # The declared 02:30 does not exist, so the explicit policy selects 03:00.
  occurrence = cron_tz.wall_clock_occurrence(
    date(2026, 3, 29), "30 2 * * *", "Europe/Belgrade",
  )
  assert occurrence == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
  assert cron_tz.due_wall_clock_date(
    "30 2 * * *", "Europe/Belgrade", now=occurrence,
  ) == date(2026, 3, 29)
  assert cron_tz.due_wall_clock_date(
    "30 2 * * *", "Europe/Belgrade",
    now=datetime(2026, 3, 29, 1, 30, tzinfo=UTC),
  ) is None


def test_ambiguous_wall_time_runs_once_at_first_fold():
  # 02:30 occurs at both 00:30 UTC (CEST, fold=0) and 01:30 UTC (CET,
  # fold=1). The first occurrence is selected and the repeated one is not due.
  occurrence = cron_tz.wall_clock_occurrence(
    date(2026, 10, 25), "30 2 * * *", "Europe/Belgrade",
  )
  assert occurrence == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
  assert cron_tz.due_wall_clock_date(
    "30 2 * * *", "Europe/Belgrade", now=occurrence,
  ) == date(2026, 10, 25)
  assert cron_tz.due_wall_clock_date(
    "30 2 * * *", "Europe/Belgrade",
    now=datetime(2026, 10, 25, 1, 30, tzinfo=UTC),
  ) is None


def test_occurrence_can_cross_the_utc_day_boundary():
  assert cron_tz.wall_clock_occurrence(
    date(2026, 7, 2), "30 0 * * *", "Asia/Kathmandu",
  ) == datetime(2026, 7, 1, 18, 45, tzinfo=UTC)


def test_civil_date_with_no_remaining_valid_minute_is_skipped():
  # Pacific/Apia skipped 2011-12-30 when it moved across the date line.
  assert cron_tz.wall_clock_occurrence(
    date(2011, 12, 30), "0 0 * * *", "Pacific/Apia",
  ) is None


def test_wall_clock_functions_reject_bad_contracts():
  with pytest.raises(ValueError):
    cron_tz.materialize_zone_cron("0 5 * * 1", "UTC")
  with pytest.raises(ValueError):
    cron_tz.materialize_zone_cron("0 5 * * *", "Not/AZone")
  with pytest.raises(ValueError):
    cron_tz.due_wall_clock_date(
      "0 5 * * *", "UTC", now=datetime(2026, 7, 1, 5, 0),
    )


def test_parse_zone_declaration_round_trip():
  text = (
    "#!/bin/sh\n"
    'ENTRY="* * * * * python3 /app/scripts/app-job-runner.py '
    '--wall-clock Europe/Belgrade 0\\ 5\\ \\*\\ \\*\\ \\* 4 '
    '/data/apps/memory/fetch.sh"\n'
    "# Zone-aware schedule identity (platform-managed).\n"
    'SCHEDULE_TZ="Europe/Belgrade"\n'
    'SCHEDULE_SOURCE="0 5 * * *"\n'
  )
  assert cron_tz.parse_zone_declaration(text) == (
    "Europe/Belgrade", "0 5 * * *",
  )


def test_parse_zone_declaration_returns_none_when_identity_is_absent():
  assert cron_tz.parse_zone_declaration("") is None


@pytest.mark.parametrize("text", [
  'SCHEDULE_TZ="Europe/Belgrade"\n',                     # half a declaration
  'SCHEDULE_SOURCE="0 5 * * *"\n',                       # half a declaration
  'SCHEDULE_TZ="Nope"\nSCHEDULE_SOURCE="0 5 * * *"\n',   # unknown zone
  'SCHEDULE_TZ="UTC"\nSCHEDULE_SOURCE="0 5 * * 1"\n',    # non-daily source
])
def test_parse_zone_declaration_rejects_malformed_identity(text):
  with pytest.raises(ValueError):
    cron_tz.parse_zone_declaration(text)


def test_server_timezone_name_prefers_valid_tz_env(monkeypatch):
  monkeypatch.setenv("TZ", "Europe/Belgrade")
  assert cron_tz.server_timezone_name() == "Europe/Belgrade"
  monkeypatch.setenv("TZ", "Total/Nonsense")
  assert cron_tz.valid_timezone(cron_tz.server_timezone_name())
