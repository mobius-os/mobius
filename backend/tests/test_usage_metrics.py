"""Provider-neutral token accounting regression tests."""

from types import SimpleNamespace

from app.usage_metrics import normalize_claude_usage, normalize_codex_usage


def _breakdown(
  *,
  input_tokens: int,
  cached_input_tokens: int,
  output_tokens: int,
  reasoning_output_tokens: int,
  total_tokens: int,
):
  return SimpleNamespace(
    input_tokens=input_tokens,
    cached_input_tokens=cached_input_tokens,
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
  })

  assert usage is not None
  assert usage["input_tokens"] == 150
  assert usage["uncached_input_tokens"] == 100
  assert usage["cache_creation_input_tokens"] == 20
  assert usage["cache_read_input_tokens"] == 30
  assert usage["output_tokens"] == 40
  assert usage["total_tokens"] == 190
  assert usage["model_context_window"] == 200_000
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
