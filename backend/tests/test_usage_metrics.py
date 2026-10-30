"""Provider-neutral token accounting regression tests."""

from types import SimpleNamespace

from app.usage_metrics import (
  normalize_claude_helper_task_usage,
  normalize_claude_usage,
  normalize_codex_usage,
)


def _breakdown(
  *,
  input_tokens: int,
  cached_input_tokens: int,
  cache_write_input_tokens: int = 0,
  output_tokens: int,
  reasoning_output_tokens: int,
  total_tokens: int,
):
  return SimpleNamespace(
    input_tokens=input_tokens,
    cached_input_tokens=cached_input_tokens,
    cache_write_input_tokens=cache_write_input_tokens,
    output_tokens=output_tokens,
    reasoning_output_tokens=reasoning_output_tokens,
    total_tokens=total_tokens,
  )


def test_claude_input_includes_cache_reads_and_writes():
  usage = normalize_claude_usage({
    "input_tokens": 100,
    "cache_creation_input_tokens": 20,
    "cache_read_input_tokens": 30,
    "output_tokens": 40,
  }, {
    "claude-main": {
      "contextWindow": 200_000,
      "inputTokens": 150,
      "outputTokens": 40,
    },
  }, {
    "input_tokens": 40,
    "cache_creation_input_tokens": 10,
    "cache_read_input_tokens": 100,
  })

  assert usage is not None
  assert usage["input_tokens"] == 150
  assert usage["uncached_input_tokens"] == 100
  assert usage["cache_creation_input_tokens"] == 20
  assert usage["cache_read_input_tokens"] == 30
  assert usage["output_tokens"] == 40
  assert usage["total_tokens"] == 190
  assert usage["model_context_window"] == 200_000
  assert usage["latest_model_input_tokens"] == 150
  assert usage["provider_model_usage"]["claude-main"]["inputTokens"] == 150


def test_codex_thread_delta_sums_every_model_call_in_the_turn():
  # The first notification says this thread had 800 tokens before the turn:
  # first.total (1,100) - first.last (300). The final cumulative total is
  # 1,900, so this turn processed 1,100 tokens across all model calls.
  first = SimpleNamespace(
    last=_breakdown(
      input_tokens=200,
      cached_input_tokens=100,
      output_tokens=100,
      reasoning_output_tokens=50,
      total_tokens=300,
    ),
    total=_breakdown(
      input_tokens=1_000,
      cached_input_tokens=400,
      output_tokens=100,
      reasoning_output_tokens=50,
      total_tokens=1_100,
    ),
    model_context_window=200_000,
  )
  final = SimpleNamespace(
    last=_breakdown(
      input_tokens=300,
      cached_input_tokens=150,
      output_tokens=100,
      reasoning_output_tokens=50,
      total_tokens=400,
    ),
    total=_breakdown(
      input_tokens=1_700,
      cached_input_tokens=800,
      output_tokens=200,
      reasoning_output_tokens=100,
      total_tokens=1_900,
    ),
    model_context_window=200_000,
  )

  usage = normalize_codex_usage(first, final)

  assert usage is not None
  assert usage["calculation"] == "thread_delta"
  assert usage["input_tokens"] == 900
  assert usage["cache_read_input_tokens"] == 500
  assert usage["uncached_input_tokens"] == 400
  assert usage["output_tokens"] == 200
  assert usage["reasoning_output_tokens"] == 100
  assert usage["total_tokens"] == 1_100
  assert usage["model_context_window"] == 200_000
  assert usage["coverage"] == "final"

  # A turn that ended before Codex completed it may have cut a call short
  # whose usage never arrived: the same delta is only a lower bound.
  from app.usage_metrics import run_usage_quality
  stopped = normalize_codex_usage(first, final, completed=False)
  assert stopped["calculation"] == "thread_delta"
  assert stopped["total_tokens"] == 1_100
  assert run_usage_quality(stopped) == "partial"


def test_codex_counter_reset_uses_labelled_latest_call_fallback():
  first = {
    "last": {
      "inputTokens": 100,
      "cachedInputTokens": 50,
      "outputTokens": 20,
      "reasoningOutputTokens": 10,
      "totalTokens": 120,
    },
    "total": {
      "inputTokens": 1_000,
      "cachedInputTokens": 500,
      "outputTokens": 200,
      "reasoningOutputTokens": 100,
      "totalTokens": 1_200,
    },
  }
  final = {
    "last": {
      "inputTokens": 80,
      "cachedInputTokens": 40,
      "outputTokens": 20,
      "reasoningOutputTokens": 10,
      "totalTokens": 100,
    },
    # Lower than the inferred 1,080-token baseline: the thread counter reset.
    "total": {
      "inputTokens": 80,
      "cachedInputTokens": 40,
      "outputTokens": 20,
      "reasoningOutputTokens": 10,
      "totalTokens": 100,
    },
  }

  usage = normalize_codex_usage(first, final)

  assert usage is not None
  assert usage["calculation"] == "last_call_fallback"
  assert usage["input_tokens"] == 80
  assert usage["coverage"] == "partial"
  assert usage["total_tokens"] == 100


def test_codex_partial_counter_rollback_uses_latest_call_fallback():
  first = {
    "last": {
      "inputTokens": 200,
      "cachedInputTokens": 100,
      "outputTokens": 100,
      "reasoningOutputTokens": 50,
      "totalTokens": 300,
    },
    "total": {
      "inputTokens": 1_000,
      "cachedInputTokens": 400,
      "outputTokens": 100,
      "reasoningOutputTokens": 50,
      "totalTokens": 1_100,
    },
  }
  final = {
    "last": {
      "inputTokens": 80,
      "cachedInputTokens": 40,
      "outputTokens": 20,
      "reasoningOutputTokens": 10,
      "totalTokens": 100,
    },
    # Still above the inferred 800-token baseline, but below the first
    # cumulative total. Treating this as a delta would silently report only 50
    # tokens even though the latest call alone used 100.
    "total": {
      "inputTokens": 820,
      "cachedInputTokens": 350,
      "outputTokens": 90,
      "reasoningOutputTokens": 40,
      "totalTokens": 850,
    },
  }

  usage = normalize_codex_usage(first, final)

  assert usage is not None
  assert usage["calculation"] == "last_call_fallback"
  assert usage["input_tokens"] == 80
  assert usage["total_tokens"] == 100


def test_codex_impossible_initial_total_uses_latest_call_fallback():
  first = {
    "last": {
      "inputTokens": 100,
      "cachedInputTokens": 50,
      "outputTokens": 20,
      "reasoningOutputTokens": 10,
      "totalTokens": 120,
    },
    # A cumulative total cannot be smaller than the call it contains.
    "total": {
      "inputTokens": 80,
      "cachedInputTokens": 40,
      "outputTokens": 10,
      "reasoningOutputTokens": 5,
      "totalTokens": 90,
    },
  }
  final = {
    "last": {
      "inputTokens": 60,
      "cachedInputTokens": 30,
      "outputTokens": 20,
      "reasoningOutputTokens": 10,
      "totalTokens": 80,
    },
    "total": {
      "inputTokens": 140,
      "cachedInputTokens": 70,
      "outputTokens": 30,
      "reasoningOutputTokens": 15,
      "totalTokens": 170,
    },
  }

  usage = normalize_codex_usage(first, final)

  assert usage is not None
  assert usage["calculation"] == "last_call_fallback"
  assert usage["input_tokens"] == 60
  assert usage["total_tokens"] == 80


def test_codex_impossible_final_total_uses_latest_call_fallback():
  first = {
    "last": {
      "inputTokens": 20,
      "cachedInputTokens": 10,
      "outputTokens": 10,
      "reasoningOutputTokens": 5,
      "totalTokens": 30,
    },
    "total": {
      "inputTokens": 100,
      "cachedInputTokens": 50,
      "outputTokens": 40,
      "reasoningOutputTokens": 20,
      "totalTokens": 140,
    },
  }
  final = {
    # The cumulative snapshot moved forward from the first notification, but
    # it still cannot be smaller than the latest call it claims to contain.
    "last": {
      "inputTokens": 180,
      "cachedInputTokens": 90,
      "outputTokens": 60,
      "reasoningOutputTokens": 30,
      "totalTokens": 240,
    },
    "total": {
      "inputTokens": 160,
      "cachedInputTokens": 80,
      "outputTokens": 50,
      "reasoningOutputTokens": 25,
      "totalTokens": 210,
    },
  }

  usage = normalize_codex_usage(first, final)

  assert usage is not None
  assert usage["calculation"] == "last_call_fallback"
  assert usage["input_tokens"] == 180
  assert usage["total_tokens"] == 240


def test_codex_cost_usd_prices_known_models():
  from app.usage_metrics import codex_cost_usd

  # gpt-5.6-sol: $5 uncached / $0.50 cached-read / $30 output per 1M tokens.
  metrics = {
    "uncached_input_tokens": 200_000,
    "cache_read_input_tokens": 800_000,
    "output_tokens": 100_000,
  }
  # (200000*5 + 800000*0.5 + 100000*30) / 1e6 = 4.4
  assert codex_cost_usd("gpt-5.6-sol", metrics) == 4.4

  # Each column priced in isolation, 1M tokens each.
  assert codex_cost_usd(
    "gpt-5.6-terra", {"uncached_input_tokens": 1_000_000}
  ) == 2.5
  assert codex_cost_usd(
    "gpt-5.6-sol", {"cache_read_input_tokens": 1_000_000}
  ) == 0.5
  assert codex_cost_usd(
    "gpt-5.6-luna", {"output_tokens": 1_000_000}
  ) == 6.0
  assert codex_cost_usd(
    "gpt-5.4-mini", {"uncached_input_tokens": 1_000_000}
  ) == 0.75


def test_codex_cache_writes_are_normalized_and_priced():
  from app.usage_metrics import codex_cost_usd

  usage = SimpleNamespace(
    last=_breakdown(
      input_tokens=1_000,
      cached_input_tokens=100,
      cache_write_input_tokens=600,
      output_tokens=50,
      reasoning_output_tokens=20,
      total_tokens=1_050,
    ),
    total=_breakdown(
      input_tokens=1_000,
      cached_input_tokens=100,
      cache_write_input_tokens=600,
      output_tokens=50,
      reasoning_output_tokens=20,
      total_tokens=1_050,
    ),
    model_context_window=1_050_000,
  )
  metrics = normalize_codex_usage(usage, usage, [usage.last])
  assert metrics["uncached_input_tokens"] == 300
  assert metrics["cache_read_input_tokens"] == 100
  assert metrics["cache_creation_input_tokens"] == 600

  # Sol cache writes cost 1.25 × its $5/M uncached-input rate.
  assert codex_cost_usd("gpt-5.6-sol", {
    "cache_creation_input_tokens": 1_000_000,
  }) == 6.25


def test_codex_long_context_is_priced_per_model_call():
  from app.usage_metrics import codex_cost_usd

  long_call = {
    "input_tokens": 300_000,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "output_tokens": 100_000,
  }
  # Sol's full >272K request: input 2× ($10/M), output 1.5× ($45/M).
  assert codex_cost_usd("gpt-5.6-sol", {
    "model_calls": [long_call],
  }) == 7.5

  # Two ordinary calls must not be mistaken for one 400K-token request.
  ordinary_call = {
    "input_tokens": 200_000,
    "cached_input_tokens": 0,
    "cache_write_input_tokens": 0,
    "output_tokens": 0,
  }
  assert codex_cost_usd("gpt-5.6-sol", {
    "model_calls": [ordinary_call, ordinary_call],
  }) == 2.0


def test_codex_cost_usd_none_for_unpriced_or_missing():
  from app.usage_metrics import codex_cost_usd

  metrics = {"uncached_input_tokens": 1_000_000}
  # Unpriced model (e.g. the codex-spark tier is absent) is left uncharged,
  # never guessed — same as the pre-existing cost_usd=None behavior.
  assert codex_cost_usd("gpt-5.3-codex-spark", metrics) is None
  assert codex_cost_usd(None, metrics) is None
  assert codex_cost_usd("gpt-5.6-sol", None) is None
  assert codex_cost_usd("gpt-5.6-sol", {}) is None


def test_codex_cost_usd_flows_from_normalized_usage():
  # End-to-end: real normalize_codex_usage output feeds the pricer, proving the
  # field names line up (uncached_input_tokens / cache_read_input_tokens /
  # output_tokens) rather than only testing a hand-built dict.
  from app.usage_metrics import codex_cost_usd, normalize_codex_usage

  usage = SimpleNamespace(
    last=_breakdown(
      input_tokens=1_000, cached_input_tokens=0,
      output_tokens=500, reasoning_output_tokens=200, total_tokens=1_500,
    ),
    total=_breakdown(
      input_tokens=1_000, cached_input_tokens=0,
      output_tokens=500, reasoning_output_tokens=200, total_tokens=1_500,
    ),
    model_context_window=200_000,
  )
  metrics = normalize_codex_usage(usage, usage)
  assert metrics["uncached_input_tokens"] == 1_000
  assert metrics["cache_read_input_tokens"] == 0
  assert metrics["output_tokens"] == 500
  # gpt-5.6-terra: 1000 uncached * $2.50 + 500 output * $15, per 1M = 0.01.
  assert codex_cost_usd("gpt-5.6-terra", metrics) == round(
    (1_000 * 2.5 + 500 * 15.0) / 1_000_000, 6
  )


# ------------------------------------------------------- hosted Claude helpers


def _helper_usage(final=None, progress=None, attributable=True):
  return normalize_claude_helper_task_usage(
    final=final, progress=progress, attributable=attributable,
    turn_duration_ms=4_000,
  )


def test_helper_task_notification_counters_are_the_final_task_aggregate():
  usage = _helper_usage(
    final={"total_tokens": 1_200, "tool_uses": 0, "duration_ms": 900},
    progress={"total_tokens": 800, "tool_uses": 3, "duration_ms": 500},
  )
  assert usage["scope"] == "helper_task"
  assert (usage["coverage"], usage["calculation"]) == ("final", "task_notification")
  # The notification wins per counter; a reported zero is a measurement.
  assert (usage["total_tokens"], usage["tool_uses"], usage["duration_ms"]) == (
    1_200, 0, 900,
  )
  # Claude reports no split for a task: those fields stay unknown, not zero.
  for field in (
    "input_tokens", "output_tokens", "cache_read_input_tokens",
    "cache_creation_input_tokens", "reasoning_output_tokens",
  ):
    assert usage[field] is None, field


def test_helper_progress_snapshot_alone_is_labelled_partial():
  usage = _helper_usage(progress={"total_tokens": 0, "tool_uses": 2, "duration_ms": 10})
  assert (usage["coverage"], usage["calculation"], usage["total_tokens"]) == (
    "partial", "task_progress", 0,
  )
  assert usage["counter_sources"]["total_tokens"] == "task_progress"


def test_helper_without_any_reported_counter_has_no_usage():
  assert _helper_usage() is None
  assert _helper_usage(final={}, progress={"total_tokens": None}) is None


def test_malformed_helper_counters_stay_unknown_instead_of_false_zero():
  usage = _helper_usage(
    final={"total_tokens": "lots", "tool_uses": True, "duration_ms": -5},
    progress={"total_tokens": 300},
  )
  # The final total is unusable, so the progress lower bound stands, labelled.
  assert (usage["total_tokens"], usage["coverage"]) == (300, "partial")
  assert usage["tool_uses"] is None
  assert usage["duration_ms"] == 4_000  # this turn's measured wall clock

  unknown = _helper_usage(final={"total_tokens": "lots", "tool_uses": 4})
  assert (unknown["total_tokens"], unknown["coverage"], unknown["calculation"]) == (
    None, "unknown", None,
  )
  assert unknown["tool_uses"] == 4


def test_resumed_helper_task_counters_are_kept_but_never_claimed_for_the_turn():
  usage = _helper_usage(
    final={"total_tokens": 5_000, "tool_uses": 9, "duration_ms": 70_000},
    attributable=False,
  )
  assert (usage["coverage"], usage["calculation"]) == ("unknown", None)
  assert usage["total_tokens"] is None and usage["tool_uses"] is None
  assert usage["duration_ms"] == 4_000
  assert usage["provider_task_usage"]["notification"]["total_tokens"] == 5_000


def _helper(final=None, progress=None, attributable=True):
  return normalize_claude_helper_task_usage(
    final=final, progress=progress, attributable=attributable, turn_duration_ms=5,
  )


def test_usage_quality_labels_new_and_older_shapes_honestly():
  from app.usage_metrics import run_usage_quality

  claude = normalize_claude_usage({"input_tokens": 1, "output_tokens": 1})
  assert run_usage_quality(claude) == "final"
  assert run_usage_quality({"calculation": "thread_delta"}) == "final"
  # Codex's counter-reset fallback is the latest call only: a lower bound.
  assert run_usage_quality({"calculation": "last_call_fallback"}) == "partial"
  assert run_usage_quality(_helper(final={"total_tokens": 9})) == "final"
  assert run_usage_quality(_helper(progress={"total_tokens": 0})) == "partial"
  assert run_usage_quality(_helper(progress={"tool_uses": 2})) == "unknown"
  assert run_usage_quality(
    _helper(final={"total_tokens": 9}, attributable=False),
  ) == "unknown"
  # The pre-label hosted-helper shape and other unrecognized shapes are never final.
  assert run_usage_quality({"total_tokens": 9, "duration_ms": 1}) == "unknown"
  assert run_usage_quality({}) == "unknown"
  assert run_usage_quality({"coverage": "made-up", "calculation": "thread_delta"}) == "unknown"
  assert run_usage_quality("legacy") == "unknown"
  # Shapes written before the four labels keep an honest classification.
  assert run_usage_quality({"coverage": "unattributed", "total_tokens": None}) == "unknown"
  assert run_usage_quality(None) == "missing"


def _run(usage, **counts):
  from app.usage_metrics import CHAT_TOKEN_FIELDS
  return SimpleNamespace(
    **{field: counts.get(field) for field in CHAT_TOKEN_FIELDS}, usage_json=usage,
  )


def test_summary_separates_observed_sums_from_run_quality():
  from app.usage_metrics import summarize_chat_run_tokens

  final = _run({"calculation": "thread_delta"}, input_tokens=100,
               cache_read_input_tokens=80, output_tokens=10, total_tokens=110)
  partial_zero = _run(_helper(progress={"total_tokens": 0}), total_tokens=0)
  final_zero = _run(_helper(final={"total_tokens": 0}), total_tokens=0)
  unattributed = _run(_helper(final={"total_tokens": 500}, attributable=False))
  missing = _run(None)

  only_final = summarize_chat_run_tokens([final, final_zero])
  assert only_final["totals"]["total_tokens"] == 110
  # Cached input is inside input_tokens; it is never added a second time.
  assert only_final["totals"]["input_tokens"] == 100
  assert only_final["coverage"]["runs_by_quality"]["final"] == 2

  mixed = summarize_chat_run_tokens(
    [final, partial_zero, final_zero, unattributed, missing],
  )
  assert mixed["coverage"]["runs"] == 5
  assert mixed["coverage"]["runs_with_usage"] == 4
  assert mixed["coverage"]["runs_by_quality"] == {
    "final": 2, "partial": 1, "unknown": 1, "missing": 1,
  }
  # The unattributed 500 is evidence, never part of the observed sum.
  assert mixed["totals"]["total_tokens"] == 110

  # A partial zero and a final zero are distinguishable in coverage.
  assert summarize_chat_run_tokens([partial_zero])["coverage"]["runs_by_quality"]["partial"] == 1
  assert summarize_chat_run_tokens([final_zero])["coverage"]["runs_by_quality"]["final"] == 1
  # Unattributed evidence alone: usage present on every run, still no total.
  alone = summarize_chat_run_tokens([unattributed])
  assert alone["coverage"]["runs_with_usage"] == alone["coverage"]["runs"] == 1
  assert alone["coverage"]["runs_by_quality"]["unknown"] == 1
  assert alone["totals"]["total_tokens"] is None
  empty = summarize_chat_run_tokens([])
  assert empty["totals"]["total_tokens"] is None
  assert set(empty["coverage"]["runs_by_quality"].values()) == {0}
