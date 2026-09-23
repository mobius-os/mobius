"""Provider-plan usage snapshots for Settings and the chat brain.

Reads are the common case. Mutations are deliberately narrow: Codex reset
redemption rides its official app-server client, while Claude's guarded
extra-usage and limit-reset controls mirror private routes used by Claude Code.
Every irreversible reset claim is revalidated against a fresh provider offer.
"""

from __future__ import annotations

import asyncio
import copy
import concurrent.futures as _cf
import functools
import hashlib
import json
import logging
import os
import shutil
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app import providers
from app.runtime_identity import broker_request
from app.storage_io import atomic_write

log = logging.getLogger(__name__)

_CLAUDE_EXTRA_USAGE_URL = (
  "https://api.anthropic.com/api/oauth/organizations/"
  "{organization_uuid}/overage_spend_limit"
)
_CLAUDE_RESET_USAGE_URL = (
  "https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1"
)
_CLAUDE_RESET_URL = (
  "https://api.anthropic.com/api/organizations/"
  "{organization_uuid}/reset_rate_limits"
)
_CLAUDE_RESET_PROGRAM = "cedar_ember"
_PROVIDER_TIMEOUT_SECONDS = 12.0
_PROVIDER_USAGE_FRESH_SECONDS = 2.0
_PROVIDER_USAGE_STALE_SECONDS = 10 * 60.0
# Claude's usage-only endpoint can briefly return no windows while ordinary
# chat authentication and model discovery remain healthy. Retry only this
# observed cold-read failure; Codex already owns a bounded protocol timeout and
# Möbius can truthfully have no measurable balance yet.
_CLAUDE_COLD_RETRY_DELAYS = (0.25, 0.75)

# One browser query is shared across panes, but several browser sessions can
# still open the same provider at once. Keep the external probe single-flight
# per installation/provider and retain one recent successful observation for
# short outages. Production has one configured data_dir and three providers,
# so these maps are intrinsically bounded by the provider registry.

@dataclass
class _CachedProviderUsage:
  observed_at: float
  checked_at: float
  snapshot: dict[str, Any]
  stale: bool = False


_provider_usage_cache: dict[tuple[str, str], _CachedProviderUsage] = {}
_provider_usage_locks: dict[tuple[str, str], asyncio.Lock] = {}
_claude_reset_locks: dict[tuple[str, str], asyncio.Lock] = {}


class ClaudeResetOfferChanged(RuntimeError):
  """The confirmed Claude reset no longer matches the provider's offer."""

_CLAUDE_WINDOW_LABELS = {
  "five_hour": "5-hour",
  "seven_day": "Weekly",
  "seven_day_opus": "Opus weekly",
  "seven_day_sonnet": "Sonnet weekly",
  "seven_day_oauth_apps": "Connected apps weekly",
  "seven_day_overage_included": "Extra usage weekly",
  "monthly": "Monthly",
  "monthly_agent_sdk": "Agent SDK monthly",
  "agent_sdk_monthly": "Agent SDK monthly",
}
_CLAUDE_WINDOW_ORDER = tuple(_CLAUDE_WINDOW_LABELS)

_PLAN_LABELS = {
  "free": "Free plan",
  "go": "Go plan",
  "plus": "Plus plan",
  "pro": "Pro plan",
  "prolite": "Pro plan",
  "max": "Max plan",
  "team": "Team plan",
  "business": "Business plan",
  "self_serve_business_usage_based": "Business plan",
  "enterprise": "Enterprise plan",
  "enterprise_cbp_usage_based": "Enterprise plan",
  "edu": "Education plan",
  "api": "API billing",
  "api_key": "API billing",
}


def plan_label(raw: Any) -> str | None:
  if hasattr(raw, "value"):
    raw = raw.value
  if not isinstance(raw, str) or not raw.strip():
    return None
  value = raw.strip().lower().replace("-", "_").replace(" ", "_")
  if value == "unknown":
    return None
  known = _PLAN_LABELS.get(value)
  if known:
    return known
  return f"{value.replace('_', ' ').title()} plan"


def _percent(raw: Any, *, precision: int = 1) -> float | int | None:
  if isinstance(raw, bool):
    return None
  try:
    value = float(raw)
  except (TypeError, ValueError):
    return None
  if not 0 <= value <= 100:
    return None
  rounded = round(value, precision)
  return int(rounded) if rounded.is_integer() else rounded


def _reset_iso(raw: Any) -> str | None:
  if raw is None or isinstance(raw, bool):
    return None
  if isinstance(raw, (int, float)):
    seconds = float(raw)
    if seconds > 10_000_000_000:
      seconds /= 1000
    try:
      return datetime.fromtimestamp(seconds, tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
      return None
  if not isinstance(raw, str) or not raw.strip():
    return None
  value = raw.strip()
  try:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
  except ValueError:
    return None
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=UTC)
  return parsed.astimezone(UTC).isoformat()


def _humanize_window_id(window_id: str) -> str:
  return window_id.replace("_", " ").strip().title()


def _window(
  window_id: str,
  kind: str,
  label: str,
  used_percent: Any,
  resets_at: Any,
) -> dict[str, Any] | None:
  used = _percent(used_percent)
  if used is None:
    return None
  return {
    "id": window_id,
    "kind": kind,
    "label": label,
    "used_percent": used,
    "resets_at": _reset_iso(resets_at),
  }


def normalize_claude_usage(
  payload: Any,
  *,
  subscription_type: Any = None,
) -> dict[str, Any]:
  """Normalize Claude's evolving usage document into stable display windows."""
  source = payload if isinstance(payload, dict) else {}
  ordered_ids = [
    *[key for key in _CLAUDE_WINDOW_ORDER if key in source],
    *[key for key in source if key not in _CLAUDE_WINDOW_LABELS],
  ]
  windows: list[dict[str, Any]] = []
  for window_id in ordered_ids:
    raw = source.get(window_id)
    if not isinstance(raw, dict):
      continue
    normalized = _window(
      window_id,
      "weekly" if window_id == "seven_day" else "other",
      _CLAUDE_WINDOW_LABELS.get(window_id, _humanize_window_id(window_id)),
      raw.get("utilization", raw.get("used_percentage")),
      raw.get("resets_at", raw.get("resetsAt")),
    )
    if normalized is not None:
      windows.append(normalized)
  # Extra usage is a separate paid allowance. It is not a plan-window reset:
  # a subscription can be at its session limit while this budget is enabled.
  # Keep only the decision-grade facts the recovery UI needs; amounts remain
  # provider-owned billing data and can be absent for unlimited accounts.
  raw_extra = source.get("extra_usage")
  raw_extra = raw_extra if isinstance(raw_extra, dict) else {}
  extra_enabled = raw_extra.get("is_enabled") is True
  extra_used = _percent(raw_extra.get("utilization"))
  # Enabled and available are separate facts. Without provider utilization,
  # the UI may report the setting but must not promise a chargeable retry.
  extra_available = False
  if extra_enabled:
    extra_available = None if extra_used is None else extra_used < 100
  extra_usage = {
    "enabled": extra_enabled,
    "available": extra_available,
    "used_percent": extra_used,
    "manageable": isinstance(raw_extra.get("is_enabled"), bool),
  }
  return {
    "state": "ready" if windows else "unavailable",
    "plan_label": plan_label(subscription_type),
    "windows": windows,
    "credit_balance": None,
    "extra_usage": extra_usage,
    "reset_credits": _claude_reset_credits(source.get("cedar_ember")),
  }


def _claude_reset_credits(summary: Any) -> dict[str, Any] | None:
  """Normalize Claude Code's private ``cedar_ember`` reset offer.

  The provider chooses the next grant and reports whether it is usable now.
  Möbius never guesses eligibility from the visible usage percentages: only a
  currently selected, provider-marked usable grant becomes redeemable.
  """
  if not isinstance(summary, dict) or not isinstance(summary.get("eligible"), bool):
    return None
  raw_grants = summary.get("grants")
  raw_grants = raw_grants if isinstance(raw_grants, list) else []
  grants: list[dict[str, Any]] = []
  total = 0
  for raw in raw_grants:
    if not isinstance(raw, dict):
      continue
    grant_id = raw.get("id")
    resets_left = raw.get("resets_left")
    if (
      not isinstance(grant_id, str)
      or not grant_id
      or isinstance(resets_left, bool)
      or not isinstance(resets_left, int)
      or resets_left < 0
    ):
      continue
    total += resets_left
    grants.append({
      "id": grant_id,
      "title": raw.get("label") if isinstance(raw.get("label"), str) else None,
      "resets_left": resets_left,
      "expires_at": _reset_iso(raw.get("ends_at")),
      "usable_now": raw.get("usable_now") is True,
      "use_requires_limit": raw.get("use_requires_limit") is not False,
      "paused": raw.get("paused") is True,
      "clears": [
        value for value in raw.get("clears", [])
        if isinstance(value, str)
      ] if isinstance(raw.get("clears"), list) else [],
    })
  next_id = summary.get("next_grant_id")
  selected = next((grant for grant in grants if grant["id"] == next_id), None)
  redeemable = bool(
    summary.get("eligible") is True
    and selected is not None
    and selected["resets_left"] > 0
    and selected["usable_now"] is True
    and selected["paused"] is False
  )
  return {
    "available_count": total,
    "credits": grants,
    "eligible": summary.get("eligible") is True,
    "ineligible_reason": (
      summary.get("ineligible_reason")
      if isinstance(summary.get("ineligible_reason"), str) else None
    ),
    "at_limit": summary.get("at_limit") is True,
    "next_credit_id": selected["id"] if selected is not None else None,
    "redeemable": redeemable,
    "weekly_resets_at": _reset_iso(summary.get("weekly_resets_at")),
    "cooldown_until": _reset_iso(summary.get("cooldown_until")),
  }


def _codex_window_label(raw: dict[str, Any], fallback: str) -> str:
  duration = raw.get("window_duration_mins", raw.get("windowDurationMins"))
  if duration == 300:
    return "5-hour"
  if duration == 10_080:
    return "7-day"
  if isinstance(duration, (int, float)) and duration > 0:
    hours = duration / 60
    if hours.is_integer():
      return f"{int(hours)}-hour"
  return fallback


def _codex_window_kind(raw: dict[str, Any], window_id: str) -> str:
  duration = raw.get("window_duration_mins", raw.get("windowDurationMins"))
  if duration == 10_080 or (duration is None and window_id == "secondary"):
    return "weekly"
  return "other"


def normalize_codex_usage(
  payload: Any,
  *,
  plan_type: Any = None,
) -> dict[str, Any]:
  """Normalize Codex account/rate-limit protocol data for Settings."""
  source = payload if isinstance(payload, dict) else {}
  limits = source.get("rate_limits", source.get("rateLimits"))
  limits = limits if isinstance(limits, dict) else {}
  plan = plan_type or limits.get("plan_type", limits.get("planType"))
  windows: list[dict[str, Any]] = []
  for key, fallback in (("primary", "Current window"), ("secondary", "Weekly")):
    raw = limits.get(key)
    if not isinstance(raw, dict):
      continue
    normalized = _window(
      key,
      _codex_window_kind(raw, key),
      _codex_window_label(raw, fallback),
      raw.get("used_percent", raw.get("usedPercent")),
      raw.get("resets_at", raw.get("resetsAt")),
    )
    if normalized is not None:
      windows.append(normalized)

  credit_balance = None
  credits = limits.get("credits")
  if isinstance(credits, dict):
    if credits.get("unlimited") is True:
      credit_balance = "Unlimited credits"
    elif credits.get("has_credits", credits.get("hasCredits")) is True:
      balance = credits.get("balance")
      if isinstance(balance, str) and balance.strip():
        credit_balance = f"{balance.strip()} credits"

  return {
    "state": "ready" if windows else "unavailable",
    "plan_label": plan_label(plan),
    "windows": windows,
    "credit_balance": credit_balance,
    "reset_credits": _codex_reset_credits(
      source.get("rate_limit_reset_credits", source.get("rateLimitResetCredits"))
    ),
  }


def _epoch_to_iso(value: Any) -> str | None:
  """Codex reports credit timestamps as Unix seconds; the UI wants ISO-8601."""
  try:
    seconds = int(value)
  except (TypeError, ValueError):
    return None
  return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def _codex_reset_credits(summary: Any) -> dict[str, Any] | None:
  """Surface banked rate-limit resets for Settings.

  The read RPC always carries ``available_count``; the ``credits`` detail rows
  (with expiry) are present only when Codex's backend chooses to include them,
  so callers must render gracefully from the count alone. Only rows the backend
  still marks redeemable are forwarded — a ``redeemed``/``redeeming`` row would
  invite a click that can only fail.
  """
  if not isinstance(summary, dict):
    return None
  available = summary.get("available_count", summary.get("availableCount"))
  try:
    available = int(available)
  except (TypeError, ValueError):
    return None
  if available < 0:
    return None

  rows: list[dict[str, Any]] = []
  raw_rows = summary.get("credits")
  if isinstance(raw_rows, list):
    for raw in raw_rows:
      if not isinstance(raw, dict):
        continue
      status = raw.get("status")
      if status not in (None, "available", "unknown"):
        continue
      credit_id = raw.get("id")
      rows.append({
        "id": credit_id if isinstance(credit_id, str) else None,
        "title": raw.get("title"),
        "description": raw.get("description"),
        "expires_at": _epoch_to_iso(raw.get("expires_at", raw.get("expiresAt"))),
        "granted_at": _epoch_to_iso(raw.get("granted_at", raw.get("grantedAt"))),
      })

  return {"available_count": available, "credits": rows}


def _units(raw: Any) -> float | None:
  if isinstance(raw, bool):
    return None
  try:
    value = float(raw)
  except (TypeError, ValueError):
    return None
  return value if value >= 0 else None


def _first_units(source: dict[str, Any], keys: tuple[str, ...]) -> float | None:
  for key in keys:
    value = _units(source.get(key))
    if value is not None:
      return value
  return None


def normalize_mobius_usage(payload: Any) -> dict[str, Any]:
  """Normalize the subscription's API-credit balance into one gauge."""
  source = payload if isinstance(payload, dict) else {}
  balance = source.get("balance")
  balance = balance if isinstance(balance, dict) else source
  plan = source.get("plan")
  plan = plan if isinstance(plan, dict) else {}
  raw_plan_label = plan.get("label")
  mobius_plan_label = (
    raw_plan_label.strip()
    if isinstance(raw_plan_label, str) and raw_plan_label.strip()
    else "Möbius"
  )

  used_percent = _percent(
    balance.get("used_percent", balance.get("usedPercent"))
  )
  remaining_percent = _percent(
    balance.get("remaining_percent", balance.get("remainingPercent")),
    precision=2,
  )
  remaining = _first_units(balance, ("spendable_units", "remaining_units"))
  used = _first_units(balance, ("used_units", "spent_units", "consumed_units"))
  total = _first_units(
    balance,
    ("total_units", "granted_units", "credit_limit_units", "limit_units"),
  )
  grants = balance.get("grants")
  grants = grants if isinstance(grants, list) else []
  eligible_grants = [
    grant for grant in grants
    if isinstance(grant, dict)
    and grant.get("revoked") is not True
  ]
  active_grants = [
    grant for grant in eligible_grants
    if (_first_units(grant, ("available_units",)) or 0) > 0
  ]
  if total is None:
    grant_totals = [
      _first_units(
        grant,
        ("original_units", "granted_units", "amount_units", "total_units"),
      )
      for grant in eligible_grants
    ]
    known_totals = [value for value in grant_totals if value is not None]
    if known_totals:
      total = sum(known_totals)
  if total is None and used is not None and remaining is not None:
    total = used + remaining
  if used is None and total is not None and remaining is not None:
    used = max(0, total - remaining)
  if remaining_percent is None and remaining is not None and total and total > 0:
    remaining_percent = _percent((remaining / total) * 100, precision=2)
  if used_percent is None:
    if used is not None and total is not None and total > 0:
      used_percent = _percent((used / total) * 100)

  window = (
    _window("api_credits", "api_credits", "API credits", used_percent, None)
    if used_percent is not None else None
  )
  if window is not None:
    window["remaining_percent"] = remaining_percent
    expiries = [
      normalized
      for grant in active_grants
      if (normalized := _reset_iso(grant.get("expires_at"))) is not None
    ]
    window["expires_at"] = min(expiries) if expiries else None
  return {
    "state": "ready" if window is not None else "unavailable",
    "plan_label": mobius_plan_label,
    "windows": [window] if window is not None else [],
    "credit_balance": None,
  }


async def _fetch_claude_usage(data_dir: str) -> dict[str, Any]:
  subscription_type = providers.claude_subscription_type(data_dir)
  token = await providers.claude_access_token(data_dir)
  headers = {
    "Authorization": f"Bearer {token}",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
  }
  async with httpx.AsyncClient(timeout=5.0) as client:
    response = await client.get(_CLAUDE_RESET_USAGE_URL, headers=headers)
    response.raise_for_status()
    return normalize_claude_usage(
      response.json(),
      subscription_type=subscription_type,
    )


async def set_claude_extra_usage(
  data_dir: str,
  *,
  enabled: bool,
) -> dict[str, Any]:
  """Toggle an already-provisioned Claude extra-usage allowance.

  Claude Code 2.1.273 uses this exact route for its reversible overage
  switch. First-time setup is deliberately excluded because it also chooses
  a spend limit and payment method, which remain provider-owned.
  """
  token = await providers.claude_access_token(data_dir)
  organization_uuid = providers.claude_organization_uuid(data_dir)
  headers = {
    "Authorization": f"Bearer {token}",
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "oauth-2025-04-20",
    "Content-Type": "application/json",
  }
  async with httpx.AsyncClient(timeout=12.0) as client:
    response = await client.put(
      _CLAUDE_EXTRA_USAGE_URL.format(
        organization_uuid=organization_uuid,
      ),
      headers=headers,
      json={"is_enabled": enabled},
    )
    response.raise_for_status()
  _provider_usage_cache.pop((str(Path(data_dir).resolve()), "claude"), None)
  return await read_provider_usage("claude", data_dir)


def _claude_reset_intent_path(data_dir: str, organization_uuid: str) -> Path:
  account = hashlib.sha256(organization_uuid.encode("utf-8")).hexdigest()[:32]
  return (
    Path(data_dir) / ".provider-usage" / "claude-reset-intents"
    / f"{account}.json"
  )


def _load_claude_reset_intent(path: Path) -> dict[str, Any] | None:
  try:
    source = json.loads(path.read_text(encoding="utf-8"))
  except FileNotFoundError:
    return None
  except (OSError, ValueError):
    return {"invalid": True}
  if not isinstance(source, dict) or source.get("version") != 1:
    return {"invalid": True}
  request_id = source.get("request_id")
  credit_id = source.get("credit_id")
  resets_left_before = source.get("resets_left_before")
  if (
    not isinstance(request_id, str)
    or not request_id
    or not isinstance(credit_id, str)
    or not credit_id
    or isinstance(resets_left_before, bool)
    or not isinstance(resets_left_before, int)
    or resets_left_before <= 0
  ):
    return {"invalid": True}
  return {
    "version": 1,
    "request_id": request_id,
    "credit_id": credit_id,
    "resets_left_before": resets_left_before,
  }


def _write_claude_reset_intent(
  path: Path,
  *,
  request_id: str,
  credit_id: str,
  resets_left_before: int,
) -> None:
  atomic_write(
    path,
    json.dumps({
      "version": 1,
      "request_id": request_id,
      "credit_id": credit_id,
      "resets_left_before": resets_left_before,
      "created_at": datetime.now(UTC).isoformat(),
    }, sort_keys=True) + "\n",
    mode=0o600,
  )


def _clear_claude_reset_intent(path: Path) -> None:
  with suppress(FileNotFoundError):
    path.unlink()


def _claude_reset_offer(
  snapshot: Any,
  credit_id: str,
  *,
  require_redeemable: bool,
) -> int | None:
  if not isinstance(snapshot, dict):
    return None
  summary = snapshot.get("reset_credits")
  if not isinstance(summary, dict):
    return None
  if require_redeemable and (
    summary.get("redeemable") is not True
    or summary.get("next_credit_id") != credit_id
  ):
    return None
  credits = summary.get("credits")
  if not isinstance(credits, list):
    return None
  for credit in credits:
    if not isinstance(credit, dict) or credit.get("id") != credit_id:
      continue
    resets_left = credit.get("resets_left")
    if (
      not isinstance(resets_left, bool)
      and isinstance(resets_left, int)
      and resets_left >= 0
    ):
      return resets_left
  return None


def _unknown_claude_reset() -> dict[str, Any]:
  return {
    "outcome": "unknown",
    "reason": "pending_reconciliation",
    "resets_left": None,
    "cleared": [],
    "weekly_resets_at": None,
    "pending": True,
  }


def _claude_reset_result(payload: Any) -> dict[str, Any] | None:
  source = payload if isinstance(payload, dict) else {}
  outcome = source.get("result")
  allowed = {
    "reset", "already_used", "not_limited", "cooldown", "ineligible",
    "unavailable",
  }
  if outcome not in allowed:
    return None
  return {
    "outcome": outcome,
    "reason": source.get("reason") if isinstance(source.get("reason"), str) else None,
    "resets_left": (
      source.get("resets_left")
      if (
        not isinstance(source.get("resets_left"), bool)
        and isinstance(source.get("resets_left"), int)
      ) else None
    ),
    "cleared": [
      value for value in source.get("cleared", []) if isinstance(value, str)
    ] if isinstance(source.get("cleared"), list) else [],
    "weekly_resets_at": _reset_iso(source.get("weekly_resets_at")),
  }


async def redeem_claude_reset(
  data_dir: str,
  *,
  credit_id: str,
  expected_resets_left: int,
) -> dict[str, Any]:
  """Validate and redeem one Claude reset as a durable account operation.

  The intent is persisted before the private provider request. An ambiguous
  response therefore leaves one stable request id to reconcile or replay,
  rather than allowing a retry to become a second irreversible claim.
  """
  organization_uuid = providers.claude_organization_uuid(data_dir)
  account_key = (str(Path(data_dir).resolve()), organization_uuid)
  intent_path = _claude_reset_intent_path(data_dir, organization_uuid)
  lock = _claude_reset_locks.setdefault(account_key, asyncio.Lock())
  async with lock:
    intent = _load_claude_reset_intent(intent_path)
    if intent is not None and intent.get("invalid") is True:
      return _unknown_claude_reset()

    current = await read_provider_usage("claude", data_dir, force_refresh=True)
    if intent is not None:
      pending_credit_id = intent["credit_id"]
      before = intent["resets_left_before"]
      now = _claude_reset_offer(
        current, pending_credit_id, require_redeemable=False,
      )
      if now is not None and now < before:
        _clear_claude_reset_intent(intent_path)
        return {
          "outcome": "reset",
          "reason": "reconciled_after_interruption",
          "resets_left": now,
          "cleared": [],
          "weekly_resets_at": None,
          "reconciled": True,
        }
      if credit_id != pending_credit_id:
        return _unknown_claude_reset()
      request_id = intent["request_id"]
      claim_credit_id = pending_credit_id
    else:
      resets_left = _claude_reset_offer(
        current, credit_id, require_redeemable=True,
      )
      if resets_left is None or resets_left != expected_resets_left:
        raise ClaudeResetOfferChanged(
          "Claude's reset offer changed before it could be claimed"
        )
      token = await providers.claude_access_token(data_dir)
      request_id = str(uuid.uuid4())
      claim_credit_id = credit_id
      _write_claude_reset_intent(
        intent_path,
        request_id=request_id,
        credit_id=claim_credit_id,
        resets_left_before=resets_left,
      )

    # Token retrieval is side-effect free. Do it after a pending intent has
    # been selected so retries can never mint a replacement request id.
    if intent is not None:
      token = await providers.claude_access_token(data_dir)
    headers = {
      "Authorization": f"Bearer {token}",
      "anthropic-version": "2023-06-01",
      "anthropic-beta": "oauth-2025-04-20",
      "Content-Type": "application/json",
    }
    try:
      async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post(
          _CLAUDE_RESET_URL.format(organization_uuid=organization_uuid),
          headers=headers,
          json={
            "program": _CLAUDE_RESET_PROGRAM,
            "grant_id": claim_credit_id,
            "request_id": request_id,
          },
        )
        response.raise_for_status()
        result = _claude_reset_result(response.json())
    except httpx.HTTPStatusError as exc:
      if exc.response.status_code < 500 and exc.response.status_code != 408:
        _clear_claude_reset_intent(intent_path)
        raise
      return _unknown_claude_reset()
    except (httpx.RequestError, ValueError):
      return _unknown_claude_reset()

    if result is None:
      return _unknown_claude_reset()
    _clear_claude_reset_intent(intent_path)
    _provider_usage_cache.pop((str(Path(data_dir).resolve()), "claude"), None)
    return result


def _codex_plan_type(account_response: Any) -> Any:
  account = getattr(account_response, "account", None)
  account = getattr(account, "root", account)
  return getattr(account, "plan_type", None)


def _read_codex_client(client: Any) -> tuple[Any, Any]:
  """Start and read one Codex client entirely on its owned worker."""
  from openai_codex.generated.v2_all import GetAccountRateLimitsResponse

  client.start()
  client.initialize()
  account = client.account_read()
  limits = client.request(
    "account/rateLimits/read",
    None,
    response_model=GetAccountRateLimitsResponse,
  )
  return account, limits


async def _run_on_codex_client(data_dir: str, work: Any, *, timeout_error: str) -> Any:
  """Run one blocking Codex app-server interaction on a bounded, reaped client.

  ``work`` is a sync callable that receives a started ``CodexClient`` and owns
  the whole request; it runs on worker one while worker two stays free to close
  the transport and unblock the interaction on timeout.
  """
  from openai_codex.client import CodexClient, CodexConfig

  codex_bin = shutil.which("codex")
  if not codex_bin:
    raise RuntimeError("codex CLI not found")
  env = dict(os.environ)
  env["CODEX_HOME"] = str(Path(data_dir) / "cli-auth" / "codex")
  client = CodexClient(CodexConfig(
    codex_bin=codex_bin,
    cwd=data_dir,
    env=env,
    client_name="mobius_settings",
    client_title="Möbius Settings",
  ))

  # Keep Settings' short-lived client off the process-wide default executor.
  # Live Codex turns may each hold one default worker while waiting for a
  # notification; queuing start/read/close behind them made this probe leak an
  # app-server exactly when the system was busiest. Worker one owns the whole
  # blocking interaction; worker two remains available to close the transport
  # and unblock it on timeout.
  executor = _cf.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="mobius-codex-usage",
  )
  loop = asyncio.get_running_loop()

  def in_worker(fn, /, *args):
    return loop.run_in_executor(executor, functools.partial(fn, *args))

  task = in_worker(work, client)
  try:
    return await asyncio.wait_for(
      asyncio.shield(task),
      timeout=_PROVIDER_TIMEOUT_SECONDS,
    )
  except TimeoutError:
    await in_worker(client.close)
    with suppress(Exception):
      await asyncio.wait_for(task, timeout=2.0)
    raise RuntimeError(timeout_error)
  finally:
    try:
      await in_worker(client.close)
    finally:
      executor.shutdown(wait=False, cancel_futures=True)


async def _fetch_codex_usage(data_dir: str) -> dict[str, Any]:
  """Read Codex plan limits with a bounded, explicitly reaped app-server."""
  from openai_codex.client import CodexClient, CodexConfig

  codex_bin = shutil.which("codex")
  if not codex_bin:
    raise RuntimeError("codex CLI not found")
  env = dict(os.environ)
  env["CODEX_HOME"] = str(Path(data_dir) / "cli-auth" / "codex")
  client = CodexClient(CodexConfig(
    codex_bin=codex_bin,
    cwd=data_dir,
    env=env,
    client_name="mobius_settings",
    client_title="Möbius Settings",
  ))

  # Keep Settings' short-lived client off the process-wide default executor.
  # Live Codex turns may each hold one default worker while waiting for a
  # notification; queuing start/read/close behind them made this probe leak an
  # app-server exactly when the system was busiest. Worker one owns the whole
  # blocking read; worker two remains available to close the transport and
  # unblock it on timeout.
  executor = _cf.ThreadPoolExecutor(
    max_workers=2,
    thread_name_prefix="mobius-codex-usage",
  )
  loop = asyncio.get_running_loop()

  def in_worker(fn, /, *args):
    return loop.run_in_executor(executor, functools.partial(fn, *args))

  from app.codex_session_lock import acquire_codex_session_activity_async
  ownership = await acquire_codex_session_activity_async(data_dir)
  task = in_worker(_read_codex_client, client)
  try:
    account, limits = await asyncio.wait_for(
      asyncio.shield(task),
      timeout=_PROVIDER_TIMEOUT_SECONDS,
    )
  except TimeoutError:
    await in_worker(client.close)
    with suppress(Exception):
      await asyncio.wait_for(task, timeout=2.0)
    raise RuntimeError("codex usage read timed out")
  finally:
    try:
      await in_worker(client.close)
    finally:
      executor.shutdown(wait=False, cancel_futures=True)
      ownership.release()

  raw = limits.model_dump(mode="json", by_alias=False)
  return normalize_codex_usage(raw, plan_type=_codex_plan_type(account))


def _consume_codex_reset(credit_id: str | None) -> Any:
  """Build the worker that redeems one banked reset over the official RPC."""

  def work(client: Any) -> dict[str, Any]:
    from openai_codex.generated.v2_all import (
      ConsumeAccountRateLimitResetCreditParams,
      ConsumeAccountRateLimitResetCreditResponse,
    )

    client.start()
    client.initialize()
    params = ConsumeAccountRateLimitResetCreditParams(
      credit_id=credit_id or None,
      # One logical attempt per HTTP redeem; the UI confirms before sending, so
      # a fresh key per call is correct and a user retry is a new attempt.
      idempotency_key=str(uuid.uuid4()),
    )
    response = client.request(
      "account/rateLimitResetCredit/consume",
      params.model_dump(mode="json", by_alias=True, exclude_none=True),
      response_model=ConsumeAccountRateLimitResetCreditResponse,
    )
    return {"outcome": response.outcome.value}

  return work


async def redeem_codex_reset(
  data_dir: str,
  credit_id: str | None = None,
) -> dict[str, Any]:
  """Redeem a banked Codex rate-limit reset via the official consume RPC.

  Returns the backend outcome (``reset``, ``nothingToReset``, ``noCredit``,
  ``alreadyRedeemed``). Redeeming is immediate and irreversible, so the caller
  must have already confirmed intent.
  """
  return await _run_on_codex_client(
    data_dir,
    _consume_codex_reset(credit_id),
    timeout_error="codex reset redeem timed out",
  )


async def _fetch_mobius_usage() -> dict[str, Any]:
  payload = await broker_request("GET", "/v1/balance", timeout=5.0)
  return normalize_mobius_usage(payload)


def _unavailable(plan_label: str | None = None) -> dict[str, Any]:
  return {
    "state": "unavailable",
    "plan_label": plan_label,
    "windows": [],
    "credit_balance": None,
  }


def _cache_key(provider_id: str, data_dir: str) -> tuple[str, str]:
  return (str(Path(data_dir).resolve()), provider_id)


def _cached_usage(
  key: tuple[str, str],
  *,
  max_age: float,
) -> dict[str, Any] | None:
  cached = _provider_usage_cache.get(key)
  if cached is None:
    return None
  if time.monotonic() - cached.checked_at > max_age:
    return None
  snapshot = copy.deepcopy(cached.snapshot)
  snapshot["stale"] = cached.stale
  return snapshot


def _snapshot_resets_are_current(
  snapshot: dict[str, Any],
  *,
  now: datetime | None = None,
) -> bool:
  """Never carry an observation across a provider allowance reset."""
  current = now or datetime.now(UTC)
  for window in snapshot.get("windows", []):
    if not isinstance(window, dict):
      continue
    normalized = _reset_iso(window.get("resets_at"))
    if normalized is None:
      continue
    if datetime.fromisoformat(normalized) <= current:
      return False
  return True


async def _fresh_provider_snapshot(
  provider_id: str,
  data_dir: str,
) -> dict[str, Any]:
  delays = _CLAUDE_COLD_RETRY_DELAYS if provider_id == "claude" else ()
  snapshot = await _provider_snapshot(provider_id, data_dir)
  for delay in delays:
    if snapshot.get("state") != "unavailable":
      break
    await asyncio.sleep(delay)
    snapshot = await _provider_snapshot(provider_id, data_dir)
  return snapshot


async def _provider_snapshot(provider_id: str, data_dir: str) -> dict[str, Any]:
  provider = providers.PROVIDERS[provider_id]
  if provider.check_auth(data_dir) is not None:
    return {
      "state": "disconnected",
      "plan_label": None,
      "windows": [],
      "credit_balance": None,
    }
  try:
    if provider_id == "claude":
      return await _fetch_claude_usage(data_dir)
    if provider_id == "codex":
      return await _fetch_codex_usage(data_dir)
    if provider_id == "mobius":
      return await _fetch_mobius_usage()
  except Exception as exc:  # best-effort read; Settings must still open
    log.warning("%s plan usage unavailable: %s", provider_id, exc)
    if provider_id == "mobius":
      plan = "Möbius"
    else:
      subscription = (
        providers.claude_subscription_type(data_dir)
        if provider_id == "claude"
        else providers.codex_subscription_type(data_dir)
      )
      plan = plan_label(subscription)
    return _unavailable(plan)
  return _unavailable()


def configured_plan_labels(data_dir: str) -> dict[str, str]:
  """Read the locally stored plan labels without contacting either provider."""
  return {
    "claude": (
      plan_label(providers.claude_subscription_type(data_dir))
      or "Claude plan"
    ),
    "codex": (
      plan_label(providers.codex_subscription_type(data_dir))
      or "Codex plan"
    ),
  }


async def read_provider_usage(
  provider_id: str,
  data_dir: str,
  *,
  force_refresh: bool = False,
) -> dict[str, Any]:
  """Return one coalesced provider usage observation with bounded fallback.

  A connected provider's usage service is advisory and can fail independently
  from chat authentication. A recent successful observation is safer and more
  useful than erasing the gauge after one failed probe, but it must never live
  past either the stale bound or a provider reset.
  """
  key = _cache_key(provider_id, data_dir)
  cached = None if force_refresh else _cached_usage(
    key, max_age=_PROVIDER_USAGE_FRESH_SECONDS,
  )
  if cached is not None:
    return cached

  lock = _provider_usage_locks.setdefault(key, asyncio.Lock())
  async with lock:
    # Another request may have refreshed while this one waited.
    cached = None if force_refresh else _cached_usage(
      key, max_age=_PROVIDER_USAGE_FRESH_SECONDS,
    )
    if cached is not None:
      return cached

    snapshot = await _fresh_provider_snapshot(provider_id, data_dir)
    if snapshot.get("state") == "ready":
      ready = copy.deepcopy(snapshot)
      ready["observed_at"] = datetime.now(UTC).isoformat()
      ready["stale"] = False
      checked_at = time.monotonic()
      _provider_usage_cache[key] = _CachedProviderUsage(
        observed_at=checked_at,
        checked_at=checked_at,
        snapshot=ready,
      )
      return copy.deepcopy(ready)

    if snapshot.get("state") == "disconnected":
      _provider_usage_cache.pop(key, None)
      return snapshot

    prior = _provider_usage_cache.get(key)
    now = time.monotonic()
    if (
      not force_refresh
      and prior is not None
      and prior.snapshot.get("state") == "ready"
      and now - prior.observed_at <= _PROVIDER_USAGE_STALE_SECONDS
      and _snapshot_resets_are_current(prior.snapshot)
    ):
      # Suppress a second browser waiting on the same failed live probe while
      # preserving the original observation age for the stale ceiling.
      prior.checked_at = now
      prior.stale = True
      fallback = copy.deepcopy(prior.snapshot)
      fallback["stale"] = True
      return fallback

    unavailable = copy.deepcopy(snapshot)
    unavailable["stale"] = False
    _provider_usage_cache[key] = _CachedProviderUsage(
      observed_at=now,
      checked_at=now,
      snapshot=unavailable,
    )
    return unavailable
