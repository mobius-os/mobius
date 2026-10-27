"""Claude's restored session estimates are not additive per-turn costs.

Keep the provider snapshot beside the derived turn cost. This is also enough
to repair older records without reading private provider transcripts or
guessing prices. Unknown baselines and counter regressions stay unknown.
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


def turn_cost(total: float | None, baseline: float | None) -> float | None:
  total, baseline = _amount(total), _amount(baseline)
  if total is None or baseline is None:
    return None
  if total < baseline and not math.isclose(total, baseline, abs_tol=1e-9):
    return None
  return max(0.0, total - baseline)
