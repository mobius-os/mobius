"""Claude's restored session estimates are not additive per-turn costs.

Keep the provider snapshot beside the derived turn cost. This is also enough
to repair older records without reading private provider transcripts or
guessing prices. Unknown baselines and counter regressions stay unknown.
The historical repair in schema_migrations.py keeps a frozen copy of these
rules; change both together.
"""

from __future__ import annotations

import math


def _amount(value) -> float | None:
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return None
  value = float(value)
  return value if math.isfinite(value) and value >= 0 else None


def session_cost_snapshot(usage: dict | None, cost_usd: float | None) -> float | None:
  """Read an explicit snapshot, or verify an old raw result against its models."""
  if not isinstance(usage, dict):
    return None
  recorded = usage.get("provider_cost")
  if isinstance(recorded, dict) and recorded.get("scope") == "session":
    total = _amount(recorded.get("total_usd"))
    baseline = _amount(recorded.get("baseline_usd"))
    if (total is not None and baseline is not None and total < baseline
        and not math.isclose(total, baseline, rel_tol=1e-9, abs_tol=1e-9)):
      return None  # A crash/reset's zeroed total is not a new baseline.
    return total
  models = usage.get("provider_model_usage")
  if not isinstance(models, dict) or not models:
    return None
  amounts = [_amount(row.get("costUSD")) if isinstance(row, dict) else None
             for row in models.values()]
  cost = _amount(cost_usd)
  if cost is None or any(value is None for value in amounts):
    return None
  total = sum(amounts)
  return total if math.isclose(total, cost, rel_tol=1e-9, abs_tol=1e-9) else None


_COUNTERS = (
  ("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"),
  ("cacheReadInputTokens", "cache_read_input_tokens"),
  ("cacheCreationInputTokens", "cache_creation_input_tokens"),
)


def _count(value) -> int | None:
  if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    return None
  return value


def _model_counters(usage) -> dict[str, tuple[int, ...]] | None:
  models = usage.get("provider_model_usage") if isinstance(usage, dict) else None
  if not isinstance(models, dict) or not models:
    return None
  counters = {}
  for name, model in models.items():
    values = tuple(_count(model.get(key)) if isinstance(model, dict) else None
                   for key, _ in _COUNTERS)
    if None in values:
      return None
    counters[name] = values
  return counters


def counters_cover_turn(usage: dict | None, baseline_usage: dict | None) -> bool:
  """Whether the restored totals are exactly the baseline's, plus this turn.

  Every model counter must be non-decreasing, and one model's increase must
  cover this turn's own main-loop usage (subagents and Claude Code's background
  calls can only add to it). A session total restored from an older save, such
  as after the CLI was killed before exiting, fails this and stays unknown
  rather than silently under-counting.
  """
  now, before = _model_counters(usage), _model_counters(baseline_usage)
  turn = usage.get("provider_usage") if isinstance(usage, dict) else None
  if now is None or before is None or not isinstance(turn, dict):
    return False
  own = tuple(_count(turn.get(key)) for _, key in _COUNTERS)
  if None in own:
    return False
  if any(name not in now or any(a < b for a, b in zip(now[name], counts))
         for name, counts in before.items()):
    return False
  zero = (0,) * len(_COUNTERS)
  return any(
    all(a - b >= spent for a, b, spent in zip(counts, before.get(name, zero), own))
    for name, counts in now.items()
  )


def turn_cost(total: float | None, baseline: float | None) -> float | None:
  total, baseline = _amount(total), _amount(baseline)
  if total is None or baseline is None:
    return None
  if total < baseline and not math.isclose(total, baseline, abs_tol=1e-9):
    return None
  return max(0.0, total - baseline)
