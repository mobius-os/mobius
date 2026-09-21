"""Per-provider quota/availability signal for unattended provider selection.

Written only inside the ``chat_writer`` actor (the single serialized persistence
owner): a turn that parks on a usage/rate limit records the provider's reset
time, and a successful provider admission clears it. Read by
``background_agents.resolve_background_provider`` to skip a provider that is
currently out of quota when choosing which background/app agent to run.
"""

from __future__ import annotations

from datetime import datetime

from app.models import ProviderAvailability
from app.timeutil import now_naive_utc

# Park reasons that carry a provider reset time.
LIMIT_REASONS = ("usage_limit", "rate_limit")


def provider_within_quota(db, provider: str) -> bool:
  """True unless ``provider`` is currently usage/rate-limited past its reset."""
  if not provider:
    return True
  row = db.get(ProviderAvailability, provider)
  if row is None or row.limited_until is None:
    return True
  return now_naive_utc() >= row.limited_until


def mark_provider_limited(
  db, provider: str | None, until: datetime | None, reason: str,
) -> None:
  """Record a usage/rate limit with its reset time (monotonic max).

  A shorter window never shortens a longer live limit — the latest known reset
  wins so a stale short window can't declare a still-limited provider healthy.
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


def clear_provider_availability(db, provider: str | None) -> None:
  """Drop any limit block after a successful provider admission."""
  if not provider:
    return
  row = db.get(ProviderAvailability, provider)
  if row is not None:
    db.delete(row)
