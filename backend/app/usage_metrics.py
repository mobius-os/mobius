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

  # A cumulative provider counter should never go backwards. If an SDK/server
  # reset makes it do so, the only honest bounded fallback is the latest call;
  # retain the calculation label so benchmark consumers can exclude it.
  if any(final_total[field] < baseline[field] for field in _CODEX_FIELDS):
    turn = _codex_breakdown(_member(final_usage, "last"))
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
