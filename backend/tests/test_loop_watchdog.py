"""Self-silence watchdog for the periodic sweep loops (design §4).

All three sweep loops sleep 60s on the one event loop, so a starved loop
cannot run the task meant to diagnose its own starvation. `loop_lateness_
warning` compares the actual wake gap to the scheduled period and returns a
WARNING only when the loop woke more than two periods late — a multi-cycle
stall, not scheduler jitter. It is a pure function so the decision is testable
without driving a real loop.
"""

import asyncio
from unittest.mock import Mock

from app.main import _sleep_with_lag_warning, loop_lateness_warning


def test_no_warning_for_on_time_or_jittery_wake():
  """An on-time or mildly-late wake produces no warning."""
  assert loop_lateness_warning(60.0, 60.0) is None
  assert loop_lateness_warning(60.0, 60.4) is None  # sub-second jitter
  assert loop_lateness_warning(60.0, 119.0) is None  # <2 periods late


def test_warning_fires_once_on_multi_period_stall():
  """A wake more than two periods late warns once, naming the observed gap."""
  warning = loop_lateness_warning(60.0, 300.0)
  assert warning is not None
  assert "starved" in warning
  assert "300" in warning  # the observed gap is surfaced


def test_lateness_threshold_is_strictly_greater_than_two_periods():
  """The boundary: exactly two periods late is tolerated; a hair beyond warns."""
  # Two periods late means observed == period * 3 (60 scheduled + 120 late).
  assert loop_lateness_warning(60.0, 180.0) is None
  assert loop_lateness_warning(60.0, 180.1) is not None


def test_threshold_scales_with_the_period_argument():
  """A different scheduled period rescales the threshold, not a hardcoded 60s."""
  assert loop_lateness_warning(10.0, 29.0) is None  # <2 periods late
  assert loop_lateness_warning(10.0, 31.0) is not None  # >2 periods late


def test_injected_clock_warns_once_for_one_large_gap():
  times = iter((10.0, 310.0, 400.0, 460.4))
  logger = Mock()

  async def _sleep(_period):
    return None

  async def _run():
    first = await _sleep_with_lag_warning(
      _sleep, lambda: next(times), logger,
    )
    second = await _sleep_with_lag_warning(
      _sleep, lambda: next(times), logger,
    )
    return first, second

  first, second = asyncio.run(_run())
  assert first is not None
  assert second is None
  logger.warning.assert_called_once()
