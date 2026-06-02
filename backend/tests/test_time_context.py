"""Per-turn time context injected into the agent's user message.

Locks in the contract that the agent gets a clock every turn (issue: the
agent only ever saw an IANA timezone NAME, and only on turn 1).
"""

from app.chat import _build_time_context


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
