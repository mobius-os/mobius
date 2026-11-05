"""Provider-neutral per-turn token-usage normalization.

Claude reports one aggregate usage dict on its terminal ResultMessage. Codex
reports a sequence of ThreadTokenUsage updates containing:

- ``last``: the latest model call;
- ``total``: cumulative usage for the provider thread.

For Codex, the first update implies the pre-turn baseline
(``first.total - first.last``). Subtracting that baseline from the final total
produces the sum of every model call in this Möbius turn — the quantity needed
to compare harness context efficiency.

A Claude helper hosted inside a shared Claude Code process has no turn
aggregate; see ``normalize_claude_helper_usage``.
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


# How much of a run's usage the stored counters describe:
# - final: the provider's complete aggregate for a run that completed;
# - partial: a lower bound (a run that ended before the provider reported
#   every call, or a sum of calls whose counters may be incomplete);
# - unknown: usage recorded in a shape that is not this run's total;
# - missing: no usage recorded (in flight, failed early, or before tracking).
# Unknown and missing never read as a zero, and unknown never adds to totals.
USAGE_QUALITIES = ("final", "partial", "unknown", "missing")
_AGGREGATE_CALCULATIONS = ("result_aggregate", "thread_delta")
_LOWER_BOUND_CALCULATIONS = ("last_call_fallback", "helper_call_sum")


def run_usage_quality(usage: Any, status: str | None) -> str:
  """Classify one durable run's ``usage_json``; see ``USAGE_QUALITIES``.

  ``status`` is the run's status. Only a completed run's aggregate is final:
  a stopped or failed run may have cut a model call short before its usage
  arrived.
  """
  if usage is None:
    return "missing"
  calculation = usage.get("calculation") if isinstance(usage, dict) else None
  if calculation in _AGGREGATE_CALCULATIONS:
    return "final" if status == "completed" else "partial"
  if calculation in _LOWER_BOUND_CALCULATIONS:
    return "partial"
  return "unknown"


def summarize_chat_run_tokens(runs: Any) -> dict:
  """Small provider-neutral token totals for one chat's durable runs.

  ``totals`` are observed sums of final and partial runs: each field is None
  when no such run reported it. ``coverage.runs_by_quality`` says whether
  those sums are a complete aggregate (every run ``final``) or a lower bound.
  """
  rows = list(runs or [])
  qualities = [
    run_usage_quality(getattr(run, "usage_json", None), getattr(run, "status", None))
    for run in rows
  ]
  counted = [
    run for run, quality in zip(rows, qualities) if quality in ("final", "partial")
  ]
  totals = {}
  for field in CHAT_TOKEN_FIELDS:
    values = [
      int(getattr(run, field))
      for run in counted
      if getattr(run, field, None) is not None
    ]
    totals[field] = sum(values) if values else None
  return {
    "coverage": {
      "runs": len(rows),
      "runs_with_usage": len(rows) - qualities.count("missing"),
      "runs_by_quality": {q: qualities.count(q) for q in USAGE_QUALITIES},
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


def claude_call_input_tokens(usage: dict[str, Any]) -> int:
  """One Claude model call's full context input.

  Anthropic splits a call's input into uncached, cache-write, and cache-read
  counters; their sum is the context the model actually read.
  """
  return sum(
    _count(usage.get(field))
    for field in (
      "input_tokens",
      "cache_creation_input_tokens",
      "cache_read_input_tokens",
    )
  )


def context_usage_event(
  provider: str,
  input_tokens: int,
  context_window: int | None = None,
) -> dict:
  """The provider-neutral live context-occupancy event for one model call.

  Durable runs record the same quantity as ``latest_model_input_tokens`` /
  the last Codex model call once a turn settles; this event lets the composer
  gauge follow it during the turn. ``context_window`` is present only when the
  provider reports it mid-turn (Codex); the shell keeps its known ceiling
  otherwise. The broadcast log retains only the newest one for reconnects.
  """
  return {
    "type": "context_usage",
    "provider": provider,
    "input_tokens": _count(input_tokens),
    "context_window": _count(context_window) or None,
  }


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
  latest_input_total = (
    claude_call_input_tokens(latest_model_usage)
    if latest_model_usage else None
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


def normalize_claude_helper_usage(
  call_usages: list[dict[str, Any]],
  *,
  duration_ms: int,
) -> dict | None:
  """Usage of one Claude helper turn hosted as a Claude Code background task.

  A hosted helper has no turn aggregate (its task notification's total is the
  last model call only), so this sums the per-call usage of the turn's own
  model calls. Claude can relay a call before its final output count arrives,
  so the sum is a lower bound (``helper_call_sum`` is never final).
  """
  if not call_usages:
    return None
  summed = {
    field: sum(_count(usage.get(field)) for usage in call_usages)
    for field in (
      "input_tokens", "cache_creation_input_tokens",
      "cache_read_input_tokens", "output_tokens",
    )
  }
  return {
    **normalize_claude_usage(summed, latest_model_usage=call_usages[-1]),
    "scope": "helper_turn",
    "calculation": "helper_call_sum",
    "duration_ms": duration_ms,
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


def codex_call_input_tokens(call: Any) -> int:
  """One Codex model call's context input (its ``input_tokens`` includes cache)."""
  return _codex_breakdown(call)["input_tokens"]


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
) -> dict | None:
  """Derive one turn aggregate plus exact per-model-call billing inputs."""
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


# OpenAI Codex per-token USD rates as (uncached_input, cached_input_read,
# output) dollars per 1,000,000 tokens. Sourced from OpenAI's published API
# pricing (July 2026); cached reads are the standard 90%-discounted input rate.
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

_CACHE_WRITE_MODELS = frozenset({
  "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
})
_LONG_CONTEXT_MODELS = _CACHE_WRITE_MODELS
_LONG_CONTEXT_INPUT_THRESHOLD = 272_000


def _codex_call_cost(
  model: str,
  counts: dict[str, Any],
  rates: tuple[float, float, float],
  *,
  long_context_eligible: bool = True,
) -> float:
  """Price one upstream model call, including request-scoped surcharges.

  ``long_context_eligible=False`` prices the same arithmetic WITHOUT the
  long-context surcharge, for callers holding turn totals rather than one
  request. The surcharge is request-scoped, so a threshold test against a sum
  over many calls is not the same question and would over-charge.
  """
  in_rate, cached_rate, out_rate = rates
  input_total = _count(counts.get("input_tokens"))
  cached = min(_count(counts.get("cached_input_tokens")), input_total)
  cache_write = min(
    _count(counts.get("cache_write_input_tokens")),
    max(0, input_total - cached),
  )
  uncached = max(0, input_total - cached - cache_write)
  cache_write_rate = in_rate * (1.25 if model in _CACHE_WRITE_MODELS else 1.0)
  if (
    long_context_eligible
    and model in _LONG_CONTEXT_MODELS
    and input_total > _LONG_CONTEXT_INPUT_THRESHOLD
  ):
    in_rate *= 2
    cached_rate *= 2
    cache_write_rate *= 2
    out_rate *= 1.5
  return (
    uncached * in_rate
    + cached * cached_rate
    + cache_write * cache_write_rate
    + _count(counts.get("output_tokens")) * out_rate
  ) / 1_000_000


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
  model_calls = usage_metrics.get("model_calls")
  if isinstance(model_calls, list) and model_calls:
    return round(sum(
      _codex_call_cost(model, call, rates)
      for call in model_calls if isinstance(call, dict)
    ), 6)

  # No per-call breakdown — only turn TOTALS. Route through `_codex_call_cost`
  # anyway so the rate arithmetic and the cache-write premium live in exactly
  # one place; this branch previously inlined a second copy that had already
  # drifted (different key names, no `min()` clamps).
  #
  # But opt OUT of the long-context surcharge: it is request-scoped, and these
  # totals are a SUM over every call in the turn. Ten 100k-token requests sum
  # past the 272k threshold while no single request ever crossed it, so testing
  # the sum would over-charge. Under-charging a genuinely long single request is
  # the safer error, and matches the behavior this fallback has always had.
  # The normalized turn keys carry the three input classes DISJOINTLY, while
  # `_codex_call_cost` expects an inclusive `input_tokens` it subdivides.
  uncached = max(0, _count(usage_metrics.get("uncached_input_tokens")))
  cached = max(0, _count(usage_metrics.get("cache_read_input_tokens")))
  cache_write = max(
    0, _count(usage_metrics.get("cache_creation_input_tokens"))
  )
  return round(_codex_call_cost(model, {
    "input_tokens": uncached + cached + cache_write,
    "cached_input_tokens": cached,
    "cache_write_input_tokens": cache_write,
    "output_tokens": max(0, _count(usage_metrics.get("output_tokens"))),
  }, rates, long_context_eligible=False), 6)
