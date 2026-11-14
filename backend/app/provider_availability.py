"""Per-provider quota/availability signal for unattended provider selection.

Written only inside the ``chat_writer`` actor (the single serialized persistence
owner): a turn that parks on a usage/rate limit records a bounded retry
check, and an explicit provider-success acknowledgement clears only a limit no
newer than that run.
Read by
``background_agents.resolve_background_provider`` to skip a provider that is
currently out of quota when choosing which background/app agent to run, and
passed to app tool calls (``app_tools``) so an app that runs a provider CLI
itself can skip it too.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.models import ProviderAvailability
from app.timeutil import now_naive_utc

# Park reasons that suppress selection until a bounded retry check.
LIMIT_REASONS = ("usage_limit", "rate_limit")


def provider_within_quota(db, provider: str) -> bool:
  """Whether quota observations permit another selection, not proof of quota."""
  if not provider:
    return True
  row = db.get(ProviderAvailability, provider)
  if row is None or row.limited_until is None:
    return True
  return now_naive_utc() >= row.limited_until


def limited_providers(db) -> list[str]:
  """Providers whose quota observation currently suppresses selection."""
  now = now_naive_utc()
  return sorted(
    row.provider for row in db.query(ProviderAvailability)
    .filter(ProviderAvailability.limited_until > now)
  )


def mark_provider_limited(
  db, provider: str | None, until: datetime | None, reason: str,
) -> None:
  """Record a usage/rate limit with its bounded retry time (monotonic max).

  A shorter window never shortens a longer live cooldown. The caller supplies
  a bounded retry deadline, not a possibly unbounded provider-reported reset.
  Passing it permits another attempt; it does not prove restored quota.
  ``reason`` is stored for observability (which limit type parked the provider).
  """
  if not provider or reason not in LIMIT_REASONS or until is None:
    return
  row = db.get(ProviderAvailability, provider)
  if row is None:
    row = ProviderAvailability(provider=provider)
    db.add(row)
  if row.limited_until is None or until > row.limited_until:
    row.limited_until = until
  row.unavailable_reason = reason
  row.updated_at = now_naive_utc()


def clear_provider_availability_after_success(
  db, provider: str | None, run_started_at: datetime | None,
) -> None:
  """Drop a limit proven stale by one successful admitted provider run.

  The run must have started at or after the limit observation. Otherwise an
  older overlapping run could finish after a newer run parks and incorrectly
  erase the newer quota signal.
  """
  if not provider or run_started_at is None:
    return
  row = db.get(ProviderAvailability, provider)
  if row is None:
    return
  if run_started_at.tzinfo is not None:
    run_started_at = run_started_at.astimezone(UTC).replace(tzinfo=None)
  if row.updated_at is None or run_started_at >= row.updated_at:
    db.delete(row)
