"""Weekly-allowance budget meter for background autopilot runs.

The Contribute app's autopilot runs a background agent every time a PR gets a
review or a failing check. Left uncapped it could burn the owner's whole weekly
model allowance on its own. This module is the meter that keeps autopilot inside
a configurable share (default 10%) of that allowance.

Two measurement paths, because "the tokens available this week" is only directly
observable on subscription plans:

1. Utilization accounting (primary — Claude subscription). The SDK reports
   ``utilization`` — the percent of the weekly allowance consumed so far — and a
   ``resets_at`` on every RateLimitEvent. Per autopilot run we accrue the
   utilization delta observed across the run as "points" into the window keyed by
   ``resets_at``; the window rolls over on its own when ``resets_at`` changes. The
   cap is ``percent`` points. Concurrent foreground chats can inflate a run's
   delta — that error is deliberately conservative (it over-counts autopilot,
   never the owner's own use).

2. Token accounting (fallback — API-key setups with no utilization signal).
   Terminal ``usage`` token counts accrue into an ISO-week bucket; the cap is
   ``weekly_tokens``.

The autopilot-vs-foreground attribution is by chat: a run counts as autopilot
spend iff its ``chat_id`` is registered as some contribution's
``followup_chat_id`` (the ``ContributionAutopilot`` row IS the registry). Accrual
is called from the runner's terminal path (``chat._complete_turn``) — not from
the app's ``/complete`` endpoint — so a crashed round still gets counted.

Trust note: enforcement reads DB rows written only by platform code. The owner's
``autopilot_budget`` SETTING lives in agent-settings.json — a knob, not a meter;
worst-case tampering only changes the owner's own cap.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app import models
from app.timeutil import now_naive_utc

log = logging.getLogger("mobius.agent_budget")

DEFAULT_PERCENT = 10.0
DEFAULT_WEEKLY_TOKENS = 2_000_000
# Background work never eats the owner's final tokens, whatever autopilot's own
# share is: once the weekly allowance is >=90% consumed by anything, defer.
HEADROOM_CEILING = 90.0
# Windows for a provider older than this many days past their key are pruned on
# write so the table can't grow without bound.
_PRUNE_AFTER_DAYS = 21


def read_budget_setting(data_dir: str) -> dict:
  """Resolve the owner's ``autopilot_budget`` block, with defaults.

  ``percent`` 0 disables autopilot spend entirely; a missing block uses the
  10%% default. ``weekly_tokens`` is only consulted on the token fallback path.
  """
  from app import providers

  raw = providers._load_agent_settings(data_dir).get("autopilot_budget")
  block = raw if isinstance(raw, dict) else {}
  percent = block.get("percent")
  try:
    percent = float(percent)
  except (TypeError, ValueError):
    percent = DEFAULT_PERCENT
  percent = max(0.0, min(100.0, percent))
  tokens = block.get("weekly_tokens")
  try:
    tokens = int(tokens)
  except (TypeError, ValueError):
    tokens = DEFAULT_WEEKLY_TOKENS
  return {"percent": percent, "weekly_tokens": max(0, tokens)}


def _trailing_week_key(when: datetime | None = None) -> str:
  """ISO-week bucket key for the token fallback path (rolls weekly)."""
  when = when or now_naive_utc()
  iso = when.isocalendar()
  return "trailing:%04d-W%02d" % (iso[0], iso[1])


def is_autopilot_chat(db: Session, chat_id: str | None) -> bool:
  """True iff this chat is a contribution's dedicated autopilot chat."""
  if not chat_id:
    return False
  return (
    db.query(models.ContributionAutopilot.record_id)
    .filter(models.ContributionAutopilot.followup_chat_id == chat_id)
    .first()
    is not None
  )


def record_observation(
  db: Session, provider: str | None, utilization, resets_at,
) -> None:
  """Upsert the last-observed weekly-allowance utilization for a provider.

  Feeds the headroom ceiling, which reads live utilization rather than the
  accrued meter so a lost accrual write can't defeat it. Best-effort: a write
  failure is logged and swallowed (metering never breaks a turn).
  """
  if not provider or not isinstance(utilization, (int, float)):
    return
  try:
    row = (
      db.query(models.AgentBudgetObservation)
      .filter(models.AgentBudgetObservation.provider == provider)
      .first()
    )
    if row is None:
      row = models.AgentBudgetObservation(provider=provider)
      db.add(row)
    row.utilization = float(utilization)
    if resets_at:
      row.resets_at = str(resets_at)
    row.observed_at = now_naive_utc()
    db.commit()
  except Exception:
    db.rollback()
    log.warning("record_observation failed provider=%s", provider, exc_info=True)


def _sum_usage_tokens(usage: dict | None) -> int:
  if not isinstance(usage, dict):
    return 0
  total = 0
  for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens"):
    value = usage.get(key)
    if isinstance(value, (int, float)):
      total += int(value)
  return total


def _prune_stale_windows(db: Session, provider: str, keep_key: str) -> None:
  cutoff = now_naive_utc() - timedelta(days=_PRUNE_AFTER_DAYS)
  (
    db.query(models.AgentBudgetWindow)
    .filter(
      models.AgentBudgetWindow.provider == provider,
      models.AgentBudgetWindow.window_key != keep_key,
      models.AgentBudgetWindow.updated_at < cutoff,
    )
    .delete(synchronize_session=False)
  )


def write_run_cost(db: Session, run_token: str | None, cost_usd) -> None:
  """The reserved ``chat_runs.cost_usd`` producer — for EVERY run.

  Completes the platform's planned per-run cost attribution and gives the meter
  an auditable second source alongside utilization/token accrual.
  """
  if not run_token or not isinstance(cost_usd, (int, float)):
    return
  try:
    run = (
      db.query(models.ChatRun)
      .filter(models.ChatRun.id == run_token)
      .first()
    )
    if run is not None:
      run.cost_usd = float(cost_usd)
      db.commit()
  except Exception:
    db.rollback()
    log.warning("write_run_cost failed run=%s", run_token, exc_info=True)


def accrue_run(
  db: Session,
  *,
  chat_id: str | None,
  provider: str | None,
  run_token: str | None = None,
  util_first=None,
  util_last=None,
  resets_at=None,
  usage: dict | None = None,
  cost_usd=None,
) -> None:
  """Terminal-path hook: record cost, refresh observation, accrue autopilot spend.

  Called once per turn from ``chat._complete_turn``. Always writes the run's
  cost and refreshes the per-provider utilization observation. Only when the
  chat is a registered autopilot chat does it accrue against the window ledger.
  Fully best-effort — metering must never fail a turn.
  """
  write_run_cost(db, run_token, cost_usd)
  record_observation(db, provider, util_last, resets_at)
  if not is_autopilot_chat(db, chat_id) or not provider:
    return
  try:
    points = 0.0
    if isinstance(util_first, (int, float)) and isinstance(util_last, (int, float)):
      points = max(0.0, float(util_last) - float(util_first))
    tokens = _sum_usage_tokens(usage)
    # Utilization path when we saw a resets_at; otherwise the token bucket.
    if resets_at:
      window_key = str(resets_at)
    else:
      window_key = _trailing_week_key()
    row = (
      db.query(models.AgentBudgetWindow)
      .filter(
        models.AgentBudgetWindow.provider == provider,
        models.AgentBudgetWindow.window_key == window_key,
      )
      .first()
    )
    if row is None:
      row = models.AgentBudgetWindow(
        provider=provider, window_key=window_key,
        points_spent=0.0, tokens_spent=0, runs=0,
      )
      db.add(row)
    row.points_spent = float(row.points_spent or 0.0) + points
    row.tokens_spent = int(row.tokens_spent or 0) + tokens
    row.runs = int(row.runs or 0) + 1
    row.updated_at = now_naive_utc()
    _prune_stale_windows(db, provider, window_key)
    db.commit()
  except Exception:
    db.rollback()
    log.warning("accrue_run failed chat=%s provider=%s", chat_id, provider,
                exc_info=True)


def _current_window_key(db: Session, provider: str) -> tuple[str, str | None]:
  """The window key spend accrues into now, plus the window's reset time.

  On the utilization path the key is the provider's last-observed ``resets_at``;
  absent that (token fallback / no observation yet), the ISO-week bucket.
  """
  obs = (
    db.query(models.AgentBudgetObservation)
    .filter(models.AgentBudgetObservation.provider == provider)
    .first()
  )
  resets_at = obs.resets_at if obs and obs.resets_at else None
  if resets_at:
    return str(resets_at), str(resets_at)
  return _trailing_week_key(), None


def budget_status(db: Session, data_dir: str, provider: str) -> dict:
  """Whether autopilot may spend now, and the numbers behind the verdict.

  Consulted by ``/respond`` before it claims (a denied round costs nothing) and
  surfaced read-only on ``/api/github/status``. Two independent gates:

    1. Headroom ceiling — last observed utilization >= 90%% defers regardless of
       autopilot's own share, so background work never eats the final tokens.
    2. Share cap — accrued window points >= ``percent`` (or tokens >=
       ``weekly_tokens`` on the fallback path) defers until the window resets.
  """
  setting = read_budget_setting(data_dir)
  percent = setting["percent"]
  weekly_tokens = setting["weekly_tokens"]
  window_key, resume_at = _current_window_key(db, provider)
  row = (
    db.query(models.AgentBudgetWindow)
    .filter(
      models.AgentBudgetWindow.provider == provider,
      models.AgentBudgetWindow.window_key == window_key,
    )
    .first()
  )
  points_spent = float(row.points_spent) if row else 0.0
  tokens_spent = int(row.tokens_spent) if row else 0
  obs = (
    db.query(models.AgentBudgetObservation)
    .filter(models.AgentBudgetObservation.provider == provider)
    .first()
  )
  utilization = float(obs.utilization) if obs and obs.utilization is not None else None

  reason = None
  paused = False
  if percent <= 0:
    paused, reason = True, "disabled"
  elif utilization is not None and utilization >= HEADROOM_CEILING:
    paused, reason = True, "headroom"
  elif resume_at and points_spent >= percent:
    paused, reason = True, "share"
  elif not resume_at and weekly_tokens and tokens_spent >= weekly_tokens:
    paused, reason = True, "share"

  return {
    "paused": paused,
    "reason": reason,
    "percent": percent,
    "points_spent": round(points_spent, 3),
    "tokens_spent": tokens_spent,
    "weekly_tokens": weekly_tokens,
    "utilization": utilization,
    "resume_at": resume_at,
  }


def may_spend(db: Session, data_dir: str, provider: str) -> dict:
  """``budget_status`` shaped for the /respond gate: adds ``allowed``."""
  status = budget_status(db, data_dir, provider)
  status["allowed"] = not status["paused"]
  return status
