"""Per-turn time context injected into the agent's user message.

Locks in the contract that the agent gets a clock every turn (issue: the
agent only ever saw an IANA timezone NAME, and only on turn 1).
"""

from app.chat import _build_time_context, _human_elapsed, _is_cli_slash_command


def test_includes_timezone_label_and_clock():
  out = _build_time_context("Europe/London")
  assert out.startswith("[Context — current time:")
  assert "(Europe/London)" in out
  # A HH:MM clock is present.
  assert ":" in out


def test_distinct_zones_render_distinct_local_times():
  london = _build_time_context("Europe/London")
  tokyo = _build_time_context("Asia/Tokyo")
  # Same instant, different wall-clock — the strings must differ.
  assert london != tokyo


def test_missing_timezone_falls_back_to_utc():
  out = _build_time_context(None)
  assert "(UTC)" in out


def test_invalid_timezone_does_not_raise_and_keeps_label():
  out = _build_time_context("Not/AZone")
  assert "(Not/AZone)" in out


def test_human_elapsed_buckets_and_quiet_window():
  # Under ~2 minutes → quiet (None), so back-to-back turns stay clean.
  assert _human_elapsed(5) is None
  assert _human_elapsed(None) is None
  assert _human_elapsed(60 * 5) == "5 minutes ago"
  assert _human_elapsed(3600 * 3) == "3 hours ago"
  assert _human_elapsed(86400 * 3) == "3 days ago"
  assert "weeks ago" in _human_elapsed(86400 * 21)
  assert "months ago" in _human_elapsed(86400 * 90)


def test_elapsed_clause_only_when_present():
  assert "user's last message was" not in _build_time_context("UTC", None)
  out = _build_time_context("UTC", "3 days ago")
  assert "user's last message was 3 days ago" in out


def test_goal_slash_command_is_detected_without_matching_paths():
  assert not _is_cli_slash_command("/goal say PONG")
  assert not _is_cli_slash_command("\n/goal clear")
  assert not _is_cli_slash_command("/")
  assert not _is_cli_slash_command("/data/apps/x is broken")
  assert not _is_cli_slash_command("please run /goal later")
