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


CHAT_TOKEN_FIELDS = (
  "input_tokens",
  "output_tokens",
  "cache_read_input_tokens",
  "cache_creation_input_tokens",
  "reasoning_output_tokens",
  "total_tokens",
)


def summarize_chat_run_tokens(runs: Any) -> dict:
  """Small provider-neutral token totals for one chat's durable runs."""
  rows = list(runs or [])
  totals = {}
  for field in CHAT_TOKEN_FIELDS:
    values = [
      int(getattr(run, field))
      for run in rows
      if getattr(run, field, None) is not None
    ]
    totals[field] = sum(values) if values else None
  return {
    "coverage": {
      "runs": len(rows),
      "runs_with_usage": sum(
        getattr(run, "usage_json", None) is not None for run in rows
      ),
    },
    "totals": totals,
  }


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
  latest_model_usage: dict[str, Any] | None = None,
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
  latest_input_total = None
  if latest_model_usage:
    latest_input_total = sum(
      _count(latest_model_usage.get(field))
      for field in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
      )
    )
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
    # ResultMessage usage is a turn aggregate. AssistantMessage usage belongs
    # to one model call, so its input total is the honest context occupancy at
    # the end of the turn rather than a sum that can exceed the context window.
    "latest_model_input_tokens": latest_input_total,
    "provider_usage": _plain(usage),
    "provider_model_usage": _plain(model_usage),
  }


_CODEX_FIELDS = (
  "input_tokens",
  "cached_input_tokens",
  "cache_write_input_tokens",
  "output_tokens",
  "reasoning_output_tokens",
  "total_tokens",
)


def _codex_breakdown(value: Any) -> dict[str, int]:
  if value is None:
    return {field: 0 for field in _CODEX_FIELDS}
  def read(field: str) -> Any:
    camel = field.split("_")[0] + "".join(
      part.title() for part in field.split("_")[1:]
    )
    if not isinstance(value, dict):
      direct = getattr(value, field, None)
      if direct is not None:
        return direct
      extra = getattr(value, "model_extra", None)
      if isinstance(extra, dict):
        return extra.get(field, extra.get(camel))
      return None
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
  call_usages: list[Any] | None = None,
  *,
  model: str | None = None,
) -> dict | None:
  """Derive one turn aggregate plus its model and billing inputs."""
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
  cache_write = min(
    turn["cache_write_input_tokens"],
    max(0, input_total - cached),
  )
  model_calls = [
    _codex_breakdown(call)
    for call in (call_usages or [])
    if call is not None
  ]
  return {
    "provider": "codex",
    "model": model,
    "scope": "turn",
    "calculation": calculation,
    "input_tokens": input_total,
    "uncached_input_tokens": max(0, input_total - cached - cache_write),
    "output_tokens": turn["output_tokens"],
    "cache_read_input_tokens": cached,
    "cache_creation_input_tokens": cache_write,
    "reasoning_output_tokens": turn["reasoning_output_tokens"],
    "total_tokens": turn["total_tokens"],
    "model_context_window": _count(
      _member(final_usage, "model_context_window")
    ) or None,
    "provider_thread_total": final_total,
    "model_calls": model_calls,
    "provider_usage": {
      "first": _plain(first_usage),
      "final": _plain(final_usage),
    },
  }
