"""One capacity-monitor tick: snapshot -> history -> forecast -> alert.

Factored out of ``runtime_supervisors`` so the sample/forecast/alert-suppression
decision is unit-testable without an event loop, a DB session, or push
delivery. A synchronous caller may provide ``notify`` directly. The async
supervisor persists the notification itself, then acknowledges that durable
row through ``record_alert_delivery``; a failed persistence attempt therefore
retries instead of being suppressed as though it succeeded.
"""

from __future__ import annotations

from typing import Any, Callable

from app.capacity import TIER_RANK, capacity_snapshot, set_latest_snapshot
from app.capacity_history import (
  alert_body,
  alert_notification_id,
  evaluate_alert,
  forecast as compute_forecast,
  load_alert_state,
  load_samples,
  record_sample,
  sample_from_snapshot,
  save_alert_state,
)


def run_capacity_tick(
  data_dir: str,
  *,
  snapshot_fn: Callable[..., dict[str, Any]] = capacity_snapshot,
  include_domains: bool = True,
  notify: Callable[..., None] | None = None,
  day: str | None = None,
) -> dict[str, Any]:
  """Run one monitor tick and return what happened (for tests + logging).

  * takes a capacity snapshot (with per-domain attribution by default),
  * appends a compact row to the bounded history ring,
  * computes the forecast over the retained window,
  * caches the enriched snapshot for the operator status route,
  * emits an alert via ``notify`` ONLY on tier escalation (storm-suppressed),
  * records the new tier only after synchronous delivery succeeds; callers
    doing async delivery acknowledge it with :func:`record_alert_delivery`,
  * records recovery immediately so a later escalation is re-armed.

  ``notify`` is a callback ``(*, tier, notification_id, title, body, snapshot,
  forecast)`` — the loop supplies one that opens a DB session and calls
  ``push.notify_owner``; tests supply a fake.
  """
  snapshot = snapshot_fn(data_dir, include_domains=include_domains)
  record_sample(data_dir, sample_from_snapshot(snapshot))
  forecast_result = compute_forecast(load_samples(data_dir, limit=1000))
  snapshot["forecast"] = forecast_result
  set_latest_snapshot(snapshot)

  tier = snapshot.get("alert_tier")
  previous_tier = (load_alert_state(data_dir) or {}).get("tier")
  alerted = evaluate_alert(tier, previous_tier)
  notification_id = alert_notification_id(tier, day=day) if (alerted and tier) else None
  body = alert_body(snapshot, forecast_result) if alerted else None
  delivered = False
  if alerted and notify is not None:
    try:
      notify(
        tier=tier,
        notification_id=notification_id,
        title="Storage capacity alert",
        body=body,
        snapshot=snapshot,
        forecast=forecast_result,
      )
      record_alert_delivery(data_dir, tier)
      delivered = True
    except Exception:
      # Keep the prior tier baseline so this exact idempotent alert retries on
      # the next tick. Delivery failure must not break the monitor loop, but it
      # also must not masquerade as a delivered notification.
      pass

  # Recovery silently re-arms. Escalation is recorded only by an acknowledged
  # delivery above (or by the async supervisor after its durable row exists).
  if TIER_RANK.get(tier or "none", 0) < TIER_RANK.get(previous_tier or "none", 0):
    save_alert_state(data_dir, {"tier": tier})

  return {
    "tier": tier,
    "previous_tier": previous_tier,
    "alerted": alerted,
    "delivered": delivered,
    "notification_id": notification_id,
    "body": body,
    "forecast": forecast_result,
    "snapshot": snapshot,
  }


def record_alert_delivery(data_dir: str, tier: str | None) -> None:
  """Acknowledge a durably persisted alert so later ticks may suppress it."""
  if TIER_RANK.get(tier or "none", 0) > 0:
    save_alert_state(data_dir, {"tier": tier})
