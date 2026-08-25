"""Provider-neutral token accounting regression tests."""

from types import SimpleNamespace

from app.usage_metrics import normalize_claude_usage, normalize_codex_usage


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

  usage = normalize_codex_usage(first, final, model="gpt-5.6-sol")

  assert usage is not None
  assert usage["model"] == "gpt-5.6-sol"
  assert usage["calculation"] == "thread_delta"
  assert usage["input_tokens"] == 900
  assert usage["cache_read_input_tokens"] == 500
  assert usage["uncached_input_tokens"] == 400
  assert usage["output_tokens"] == 200
  assert usage["reasoning_output_tokens"] == 100
  assert usage["total_tokens"] == 1_100
  assert usage["model_context_window"] == 200_000


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


def test_codex_cache_writes_are_normalized_without_guessing_cost():
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
