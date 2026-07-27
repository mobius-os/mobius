"""Provider-neutral per-turn token-usage normalization.

Claude reports one aggregate usage dict on its terminal ResultMessage. Codex
reports a sequence of ThreadTokenUsage updates containing:

- ``last``: the latest model call;
- ``total``: cumulative usage for the provider thread.

For Codex, the first update implies the pre-turn baseline
(``first.total - first.last``). Subtracting that baseline from the final total
produces the sum of every model call in this Möbius turn — the quantity needed
to compare harness context efficiency.
"""

from __future__ import annotations

from typing import Any


def _count(value: Any) -> int:
  """Return a non-negative integer counter; unknown SDK values become zero."""
  if isinstance(value, bool):
    return 0
  try:
    return max(0, int(value or 0))
  except (TypeError, ValueError):
    return 0


def _plain(value: Any) -> Any:
  """Convert generated SDK models into JSON-safe plain values."""
  if value is None:
    return None
  if hasattr(value, "model_dump"):
    return value.model_dump(mode="json", by_alias=True)
  if isinstance(value, dict):
    return {str(k): _plain(v) for k, v in value.items()}
  if isinstance(value, (list, tuple)):
    return [_plain(v) for v in value]
  if isinstance(value, (str, int, float, bool)):
    return value
  return str(value)


def normalize_claude_usage(
  usage: dict[str, Any] | None,
  model_usage: dict[str, Any] | None = None,
) -> dict | None:
  """Normalize Claude's terminal turn aggregate.

  Anthropic reports uncached input, cache creation, and cache reads as separate
  counters. ``input_tokens`` below intentionally adds all three so it measures
  total context processed/re-fed for the turn, matching the harness-efficiency
  quantity Codex exposes.
  """
  if not usage:
    return None
  uncached = _count(usage.get("input_tokens"))
  cache_write = _count(usage.get("cache_creation_input_tokens"))
  cache_read = _count(usage.get("cache_read_input_tokens"))
  output = _count(usage.get("output_tokens"))
  input_total = uncached + cache_write + cache_read
  context_windows = [
    _count(details.get("contextWindow"))
    for details in (model_usage or {}).values()
    if isinstance(details, dict)
  ]
  return {
    "provider": "claude",
    "scope": "turn",
    "calculation": "result_aggregate",
    "input_tokens": input_total,
    "uncached_input_tokens": uncached,
    "output_tokens": output,
    "cache_read_input_tokens": cache_read,
    "cache_creation_input_tokens": cache_write,
    "reasoning_output_tokens": _count(usage.get("reasoning_tokens")),
    "total_tokens": input_total + output,
    "model_context_window": max(context_windows, default=0) or None,
    "provider_usage": _plain(usage),
    "provider_model_usage": _plain(model_usage),
  }


_CODEX_FIELDS = (
  "input_tokens",
  "cached_input_tokens",
  "output_tokens",
  "reasoning_output_tokens",
  "total_tokens",
)


def _codex_breakdown(value: Any) -> dict[str, int]:
  if value is None:
    return {field: 0 for field in _CODEX_FIELDS}
  def read(field: str) -> Any:
    if not isinstance(value, dict):
      return getattr(value, field, None)
    camel = field.split("_")[0] + "".join(
      part.title() for part in field.split("_")[1:]
    )
    return value.get(field, value.get(camel))
  return {
    field: _count(read(field))
    for field in _CODEX_FIELDS
  }


def _member(value: Any, field: str) -> Any:
  if isinstance(value, dict):
    camel = field.split("_")[0] + "".join(
      part.title() for part in field.split("_")[1:]
    )
    return value.get(field, value.get(camel))
  return getattr(value, field, None)


def _subtract_counts(
  current: dict[str, int],
  baseline: dict[str, int],
) -> dict[str, int]:
  return {
    field: max(0, current[field] - baseline[field])
    for field in _CODEX_FIELDS
  }


def normalize_codex_usage(
  first_usage: Any | None,
  final_usage: Any | None,
) -> dict | None:
  """Derive one Möbius-turn aggregate from Codex thread usage updates."""
  if final_usage is None:
    return None
  first_usage = first_usage or final_usage
  first_total = _codex_breakdown(_member(first_usage, "total"))
  first_last = _codex_breakdown(_member(first_usage, "last"))
  baseline = _subtract_counts(first_total, first_last)
  final_total = _codex_breakdown(_member(final_usage, "total"))
  final_last = _codex_breakdown(_member(final_usage, "last"))

  # A cumulative provider counter must contain its own latest call and never
  # move backwards between notifications. If an SDK/server reset violates
  # either invariant, the only honest bounded fallback is the latest call;
  # retain the calculation label so benchmark consumers can exclude it.
  counter_reset = any(
    first_total[field] < first_last[field]
    or final_total[field] < first_total[field]
    or final_total[field] < final_last[field]
    for field in _CODEX_FIELDS
  )
  if counter_reset:
    turn = final_last
    calculation = "last_call_fallback"
  else:
    turn = _subtract_counts(final_total, baseline)
    calculation = "thread_delta"

  input_total = turn["input_tokens"]
  cached = min(turn["cached_input_tokens"], input_total)
  return {
    "provider": "codex",
    "scope": "turn",
    "calculation": calculation,
    "input_tokens": input_total,
    "uncached_input_tokens": max(0, input_total - cached),
    "output_tokens": turn["output_tokens"],
    "cache_read_input_tokens": cached,
    "cache_creation_input_tokens": 0,
    "reasoning_output_tokens": turn["reasoning_output_tokens"],
    "total_tokens": turn["total_tokens"],
    "model_context_window": _count(
      _member(final_usage, "model_context_window")
    ) or None,
    "provider_thread_total": final_total,
    "provider_usage": {
      "first": _plain(first_usage),
      "final": _plain(final_usage),
    },
  }


# OpenAI Codex per-token USD rates as (uncached_input, cached_input_read,
# output) dollars per 1,000,000 tokens. Sourced from OpenAI's published API
# pricing (July 2026); cached reads are the standard 90%-discounted input rate.
# The separate long-context surcharge tier is intentionally NOT modeled — these
# are the standard-context rates, so a turn that crosses the long-context
# threshold is a small, bounded underestimate rather than a wrong number.
# Update these as OpenAI revises pricing; a model absent from this table is left
# uncharged (cost None) rather than mispriced.
CODEX_MODEL_RATES: dict[str, tuple[float, float, float]] = {
  "gpt-5.6-sol": (5.00, 0.50, 30.00),
  "gpt-5.6-terra": (2.50, 0.25, 15.00),
  "gpt-5.6-luna": (1.00, 0.10, 6.00),
  "gpt-5.5": (5.00, 0.50, 30.00),
  "gpt-5.4": (2.50, 0.25, 15.00),
  "gpt-5.4-mini": (0.75, 0.075, 4.50),
}


def codex_cost_usd(model: str | None, usage_metrics: dict | None) -> float | None:
  """Best-effort USD cost for one Codex turn from its normalized usage.

  Codex, unlike Claude, reports token counts but no dollar cost, so Möbius
  derives it from the rate card above. Output tokens already include reasoning
  tokens (OpenAI counts reasoning within completion), so output is billed once.
  Returns None when the model is unpriced or usage is missing — an unpriced turn
  is left uncharged rather than charged a guess, exactly matching the prior
  cost_usd=None behavior for those cases.
  """
  if not model or not usage_metrics:
    return None
  rates = CODEX_MODEL_RATES.get(model)
  if rates is None:
    return None
  in_rate, cached_rate, out_rate = rates
  uncached = max(0, _count(usage_metrics.get("uncached_input_tokens")))
  cached = max(0, _count(usage_metrics.get("cache_read_input_tokens")))
  output = max(0, _count(usage_metrics.get("output_tokens")))
  cost = (
    uncached * in_rate + cached * cached_rate + output * out_rate
  ) / 1_000_000
  return round(cost, 6)
