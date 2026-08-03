"""Read-only provider-plan usage snapshots for the Settings surface."""

from __future__ import annotations

import asyncio
import concurrent.futures as _cf
import functools
import logging
import os
import shutil
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app import providers

log = logging.getLogger(__name__)

_CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_PROVIDER_TIMEOUT_SECONDS = 12.0

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


def _used_percent(raw: Any) -> float | int | None:
  if isinstance(raw, bool):
    return None
  try:
    value = float(raw)
  except (TypeError, ValueError):
    return None
  if not 0 <= value <= 100:
    return None
  rounded = round(value, 1)
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
  label: str,
  used_percent: Any,
  resets_at: Any,
) -> dict[str, Any] | None:
  used = _used_percent(used_percent)
  if used is None:
    return None
  return {
    "id": window_id,
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
      _CLAUDE_WINDOW_LABELS.get(window_id, _humanize_window_id(window_id)),
      raw.get("utilization", raw.get("used_percentage")),
      raw.get("resets_at", raw.get("resetsAt")),
    )
    if normalized is not None:
      windows.append(normalized)
  return {
    "state": "ready" if windows else "unavailable",
    "plan_label": plan_label(subscription_type),
    "windows": windows,
    "credit_balance": None,
  }


def _codex_window_label(raw: dict[str, Any], fallback: str) -> str:
  duration = raw.get("window_duration_mins", raw.get("windowDurationMins"))
  if duration == 300:
    return "5-hour"
  if duration == 10_080:
    return "Weekly"
  if isinstance(duration, (int, float)) and duration > 0:
    hours = duration / 60
    if hours.is_integer():
      return f"{int(hours)}-hour"
  return fallback


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
    response = await client.get(_CLAUDE_USAGE_URL, headers=headers)
    response.raise_for_status()
    return normalize_claude_usage(
      response.json(),
      subscription_type=subscription_type,
    )


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

  raw = limits.model_dump(mode="json", by_alias=False)
  return normalize_codex_usage(raw, plan_type=_codex_plan_type(account))


def _unavailable(plan_label: str | None = None) -> dict[str, Any]:
  return {
    "state": "unavailable",
    "plan_label": plan_label,
    "windows": [],
    "credit_balance": None,
  }


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
  except Exception as exc:  # best-effort read; Settings must still open
    log.warning("%s plan usage unavailable: %s", provider_id, exc)
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
) -> dict[str, Any]:
  """Read one provider's plan usage only when its disclosure is opened."""
  return await _provider_snapshot(provider_id, data_dir)
