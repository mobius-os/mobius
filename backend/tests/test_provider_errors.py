"""One provider-error classifier shared by live turns and compaction."""

import time

import pytest

from app import codex_events
from app.provider_errors import (
  ProviderErrorKind as Kind,
  classify_provider_error,
  is_quota_limit,
  is_workspace_credits_exhausted,
)


@pytest.mark.parametrize("status,kind", [
  (413, Kind.TOO_LARGE),
  (429, Kind.USAGE_LIMIT),
  (401, Kind.AUTH),
])
def test_http_status_classifies_without_text(status, kind):
  assert classify_provider_error(None, status=status) is kind


@pytest.mark.parametrize("error_type,kind", [
  ("billing_error", Kind.CREDITS),
  ("rate_limit", Kind.USAGE_LIMIT),
  ("authentication_failed", Kind.AUTH),
])
def test_claude_error_type_classifies_without_text(error_type, kind):
  assert classify_provider_error("unrelated wording", error_type=error_type) is kind


def test_structured_fields_win_over_text():
  assert classify_provider_error("rate limit exceeded", status=401) is Kind.AUTH
  assert classify_provider_error("Unauthorized", status=429) is Kind.USAGE_LIMIT
  assert classify_provider_error(
    "rate limit exceeded", error_type="billing_error",
  ) is Kind.CREDITS


WORKSPACE_CREDITS = "Your workspace is out of credits. Add credits to continue."


def test_exhausted_workspace_credits_win_over_a_reached_limit_status():
  # Codex reports depleted workspace credits as a reached rate limit (429);
  # no reset time refills them, so they are credits, not a usage limit.
  assert classify_provider_error(WORKSPACE_CREDITS, status=429) is Kind.CREDITS


@pytest.mark.parametrize("text,exhausted", [
  (WORKSPACE_CREDITS, True),
  ("  your workspace is OUT OF CREDITS. add credits to continue.  ", True),
  ("Your workspace is out of credits.", False),
  ("Payment failed: card declined. Add credits to continue.", False),
  ("Credit balance is too low", False),
  (None, False),
])
def test_workspace_credits_refusal_is_matched_exactly(text, exhausted):
  assert is_workspace_credits_exhausted(text) is exhausted
  if exhausted:
    assert classify_provider_error(text) is Kind.CREDITS


def test_unmapped_structured_fields_fall_back_to_text():
  assert classify_provider_error("rate limit", status=500) is Kind.USAGE_LIMIT
  assert classify_provider_error("Unauthorized", error_type="unknown") is Kind.AUTH


@pytest.mark.parametrize("text,kind", [
  ("request body is too large", Kind.TOO_LARGE),
  ("request body too large", Kind.TOO_LARGE),
  ("request_body_too_large", Kind.TOO_LARGE),
  ("413 Request Entity Too Large", Kind.TOO_LARGE),
  ("unexpected status 413 Payload Too Large", Kind.TOO_LARGE),
  ("context_length_exceeded", Kind.TOO_LARGE),
  ("Your workspace is out of credits.", Kind.CREDITS),
  ("insufficient_credits", Kind.CREDITS),
  ("insufficient_quota: You exceeded your current quota", Kind.CREDITS),
  ("Credit balance is too low", Kind.CREDITS),
  ("billing_error", Kind.CREDITS),
  ("not enough credits for the maximum request cost", Kind.CREDITS),
  ("Error: rate limit exceeded", Kind.USAGE_LIMIT),
  ("rate_limit_exceeded", Kind.USAGE_LIMIT),
  ("usage_limit_reached", Kind.USAGE_LIMIT),
  ("usage limit reached, resets at ...", Kind.USAGE_LIMIT),
  ("HTTP 429 Too Many Requests", Kind.USAGE_LIMIT),
  ("model overloaded, try again", Kind.USAGE_LIMIT),
  ("quota exceeded", Kind.USAGE_LIMIT),
  ("You've hit your weekly limit · resets Jul 4, 3am (UTC)", Kind.USAGE_LIMIT),
  ("You've hit your session limit · resets 2:20am (UTC)", Kind.USAGE_LIMIT),
  ("You've hit your 5-hour limit · resets 2:30pm (UTC)", Kind.USAGE_LIMIT),
  ("You've hit your limit · resets 5pm", Kind.USAGE_LIMIT),
  (
    "API Error: Server is temporarily limiting requests "
    "(not your usage limit) · Rate limited",
    Kind.USAGE_LIMIT,
  ),
  ("Invalid authentication credentials", Kind.AUTH),
  ("authentication_failed", Kind.AUTH),
  ("invalid_api_key", Kind.AUTH),
  ("Unauthorized", Kind.AUTH),
  ("not logged in", Kind.AUTH),
  ("some ordinary failure", Kind.OTHER),
  ("connection reset by peer", Kind.OTHER),
  ("ValueError: list index out of range (limit check)", Kind.OTHER),
  ("Execution interrupted.", Kind.OTHER),
  ("request id req_14290 failed", Kind.OTHER),
  ("", Kind.OTHER),
  (None, Kind.OTHER),
])
def test_text_classification(text, kind):
  assert classify_provider_error(text) is kind


@pytest.mark.parametrize("text,is_quota", [
  ("You've hit your 5-hour limit · resets 2:30pm (UTC)", True),
  ("You've hit your Opus limit · resets 3pm", True),
  ("You've hit your weekly limit", True),
  ("usage_limit_reached", True),
  ("quota exceeded", True),
  ("Server is temporarily limiting requests. Try again in 30 minutes.", False),
  ("429 too many requests", False),
  ("model overloaded, try again", False),
  ("ValueError: list index out of range (limit check)", False),
  ("", False),
  (None, False),
])
def test_quota_limit_is_the_accounts_own_allowance_not_a_throttle(
  text, is_quota,
):
  assert is_quota_limit(text) is is_quota


def test_size_and_credit_refusals_are_not_limits_that_reset():
  # Both can mention a limit or quota; neither goes away by waiting.
  assert classify_provider_error(
    "payload too large: request exceeds the rate limit tier",
  ) is Kind.TOO_LARGE
  assert classify_provider_error(
    "insufficient_quota (quota exceeded)",
  ) is Kind.CREDITS


@pytest.mark.parametrize("text", [
  "not enough credits for the maximum request cost",
  "insufficient_credits",
])
def test_codex_mobius_credit_wording_agrees_with_classifier(text):
  # codex_events picks the Möbius subscription's own wording for these exact
  # broker refusals; every other caller must see them as out of credits.
  assert codex_events._codex_user_error(text) in {
    codex_events.MOBIUS_MAX_REQUEST_COST_MESSAGE,
    codex_events.MOBIUS_INSUFFICIENT_CREDITS_MESSAGE,
  }
  assert classify_provider_error(text) is Kind.CREDITS


def test_long_limit_heavy_text_classifies_in_linear_time():
  # Codex stderr is unbounded and chat classifies on the event loop; a
  # pattern spanning "limit" ... "resets" took about 5s on 48KB of this.
  text = "limit " * 8_000
  start = time.perf_counter()
  assert classify_provider_error(text) is Kind.OTHER
  assert classify_provider_error(text + "resets 5pm") is Kind.USAGE_LIMIT
  assert time.perf_counter() - start < 1.0
