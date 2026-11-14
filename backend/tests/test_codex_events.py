"""Focused contracts for Codex error wording and limit signals."""

from types import SimpleNamespace

import pytest

from app.codex_events import (
  _codex_user_error,
  _extract_rate_limit_reset,
  _rate_limit_credits_depleted,
)


def test_mobius_insufficient_credits_code_does_not_claim_a_balance_or_trial():
  error = (
    'unexpected status 402 Payment Required: '
    '{"error":{"code":"insufficient_credits"}}'
  )

  message = _codex_user_error(error)

  assert "insufficient_credits" in message
  assert "maximum request cost" not in message
  assert "[Open Settings](/shell/?settings=mobius-models)" in message
  assert "trial" not in message.lower()


def test_mobius_max_request_cost_reason_takes_priority_over_generic_code():
  error = (
    'unexpected status 402 Payment Required: {"error":{"message":'
    '"not enough credits for the maximum request cost",'
    '"code":"insufficient_credits"}}'
  )

  message = _codex_user_error(error)

  assert "not enough credits for the maximum request cost" in message
  assert "(insufficient_credits)" not in message
  assert "[Open Settings](/shell/?settings=mobius-models)" in message
  assert "trial" not in message.lower()


def test_provider_limit_error_and_structured_reset_remain_separate_from_credits():
  error = "rate_limit_reached: wait until the provider reset"
  snapshot = SimpleNamespace(
    primary=SimpleNamespace(resets_at=1_800_000_000, used_percent=100),
    secondary=None,
    rate_limit_reached_type="rate_limit_reached",
  )

  assert _codex_user_error(error) == error
  assert _extract_rate_limit_reset(snapshot) == (1_800_000_000, True)


@pytest.mark.parametrize("reached_type, depleted", [
  ("workspace_owner_credits_depleted", True),
  ("workspace_member_credits_depleted", True),
  ("workspace_owner_usage_limit_reached", False),
  ("rate_limit_reached", False),
  (None, False),
])
def test_only_credit_depletion_reached_types_mark_credits_depleted(
  reached_type, depleted,
):
  types = pytest.importorskip("openai_codex.generated.v2_all")
  snapshot = types.RateLimitSnapshot.model_validate(
    {"rateLimitReachedType": reached_type},
  )
  assert _rate_limit_credits_depleted(snapshot) is depleted
  assert _rate_limit_credits_depleted(None) is False
