"""Shared helpers for autonomous goal-loop runner behavior."""

from __future__ import annotations

import inspect
import json
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

from app.chat_writer import (
  ClearGoal,
  CompleteGoal,
  IncrementGoalTurn,
  PauseGoal,
  RecordGoalTokens,
  ResetGoalProgress,
  ResumeGoal,
  await_ack as _await_ack,
  get_writer,
)


DEFAULT_CLAUDE_GOAL_TURN_BACKSTOP = 25
DEFAULT_CODEX_GOAL_TOKEN_BUDGET = 200000
CODEX_GOAL_SENTINEL_PREFIX = "MOBIUS_UPDATE_GOAL:"


def _goal_continue_message(condition: str, reason: str) -> str:
  """Frames an autonomous continuation as same-session feedback."""
  return f"[goal] Not met: {reason}. Keep working toward: {condition}"


def _goal_turn_cap(condition: str, settings: dict | None = None) -> int:
  """Return the smaller of a stop clause and the configured backstop."""
  settings = settings or {}
  try:
    backstop = int(
      settings.get(
        "goal_turn_backstop",
        DEFAULT_CLAUDE_GOAL_TURN_BACKSTOP,
      )
    )
  except (TypeError, ValueError):
    backstop = DEFAULT_CLAUDE_GOAL_TURN_BACKSTOP
  backstop = max(0, backstop)
  match = re.search(r"\bstop\s+after\s+(\d+)\b", condition, re.I)
  requested = int(match.group(1)) if match else backstop
  return max(0, min(requested, backstop))


def _goal_token_budget(goal: dict | None, settings: dict | None = None) -> int:
  """Return the Codex token budget from goal, condition, settings, default."""
  settings = settings or {}
  condition = str((goal or {}).get("condition") or "")
  match = re.search(r"\btoken\s+budget\s+(\d+)\b", condition, re.I)
  candidates = [
    (goal or {}).get("token_budget"),
    int(match.group(1)) if match else None,
    settings.get("goal_token_budget"),
    DEFAULT_CODEX_GOAL_TOKEN_BUDGET,
  ]
  for candidate in candidates:
    try:
      budget = int(candidate)
    except (TypeError, ValueError):
      continue
    if budget > 0:
      return budget
  return DEFAULT_CODEX_GOAL_TOKEN_BUDGET


def _goal_elapsed_time_s(goal: dict | None, now: float | None = None) -> float:
  """Return elapsed seconds since this goal run started."""
  if now is None:
    now = time.time()
  started_at = (goal or {}).get("run_started_at") or (goal or {}).get(
    "started_at"
  )
  if isinstance(started_at, (int, float)):
    return max(0.0, now - float(started_at))
  if isinstance(started_at, str) and started_at:
    try:
      parsed = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
      if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
      return max(0.0, now - parsed.timestamp())
    except ValueError:
      return 0.0
  return 0.0


def _goal_token_count(usage: dict | None) -> int:
  """Extract a best-effort token count from provider usage objects."""
  if not isinstance(usage, dict):
    return 0
  direct_keys = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "total_tokens",
    "tokens",
  )
  total = 0
  for key in direct_keys:
    value = usage.get(key)
    if isinstance(value, (int, float)):
      total += int(value)
  if total:
    return total
  nested_total = 0
  for value in usage.values():
    if isinstance(value, dict):
      nested_total += _goal_token_count(value)
  return nested_total


def _codex_goal_turn_message(message: str, goal: dict, token_budget: int) -> str:
  """Inject Codex goal instructions into a turn message."""
  condition = str(goal.get("condition") or "").strip()
  spent = int(goal.get("token_spend") or 0)
  instructions = (
    "[goal]\n"
    "Active autonomous goal condition:\n"
    f"{condition}\n\n"
    "You must keep working toward this goal across turns until it is met, "
    "paused, cleared, or the token budget is exhausted.\n"
    f"Token budget: {token_budget}. Tokens spent so far: {spent}.\n"
    "When and only when the condition is fully satisfied, self-report "
    "completion by emitting one final line exactly in this form:\n"
    f'{CODEX_GOAL_SENTINEL_PREFIX} {{"status":"complete",'
    '"reason":"brief reason"}}\n'
    "This sentinel is the equivalent of calling update_goal(status="
    "'complete'); do not emit it for partial progress.\n"
    "[/goal]"
  )
  return f"{instructions}\n\n{message}"


def _parse_codex_goal_update(text: str) -> dict | None:
  """Parse a Codex model self-report sentinel from assistant text."""
  for line in (text or "").splitlines():
    stripped = line.strip()
    if not stripped.startswith(CODEX_GOAL_SENTINEL_PREFIX):
      continue
    raw = stripped[len(CODEX_GOAL_SENTINEL_PREFIX):].strip()
    try:
      parsed = json.loads(raw)
    except json.JSONDecodeError:
      return {
        "status": "invalid",
        "reason": raw[:500] or "invalid update_goal JSON",
      }
    return {
      "status": str(parsed.get("status") or "").strip().lower(),
      "reason": str(parsed.get("reason") or "").strip()[:500],
    }
  return None


async def _maybe_await(value: Any) -> Any:
  """Awaits a value only when the callback returned an awaitable."""
  if inspect.isawaitable(value):
    return await value
  return value


async def _default_goal_evaluator(
  condition: str,
  latest_assistant_output: str,
  recent_context: str,
) -> dict:
  """Evaluate goal completion with a cheap Anthropic one-shot call."""
  prompt = (
    "Return only JSON with keys met and reason.\n"
    f"Goal condition:\n{condition}\n\n"
    f"Recent context:\n{recent_context[-6000:]}\n\n"
    f"Latest assistant output:\n{latest_assistant_output[-6000:]}"
  )
  try:
    if not os.environ.get("ANTHROPIC_API_KEY"):
      raise ModuleNotFoundError
    from anthropic import AsyncAnthropic
    client = AsyncAnthropic()
    resp = await client.messages.create(
      model="claude-haiku-4-5-20251001",
      max_tokens=256,
      temperature=0,
      messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(
      block.text for block in resp.content if getattr(block, "text", None)
    )
  except ModuleNotFoundError:
    text = await _default_goal_evaluator_http(prompt)
  try:
    parsed = json.loads(text)
  except json.JSONDecodeError:
    parsed = {"met": False, "reason": text.strip() or "invalid JSON"}
  return {
    "met": bool(parsed.get("met")),
    "reason": str(parsed.get("reason") or "").strip()[:500],
  }


async def _default_goal_evaluator_http(prompt: str) -> str:
  """Fallback evaluator transport using existing backend dependencies."""
  import httpx
  from app.config import get_settings

  headers = {"anthropic-version": "2023-06-01"}
  api_key = os.environ.get("ANTHROPIC_API_KEY")
  if api_key:
    headers["x-api-key"] = api_key
  else:
    creds_path = (
      get_settings().data_dir
      and os.path.join(
        get_settings().data_dir,
        "cli-auth",
        "claude",
        ".credentials.json",
      )
    )
    if not creds_path or not os.path.exists(creds_path):
      raise RuntimeError("Anthropic credentials missing for goal evaluator")
    with open(creds_path, encoding="utf-8") as fh:
      oauth = (json.load(fh).get("claudeAiOauth") or {})
    token = oauth.get("accessToken")
    if not token:
      raise RuntimeError("Anthropic credentials malformed for goal evaluator")
    headers["Authorization"] = f"Bearer {token}"
    headers["anthropic-beta"] = "oauth-2025-04-20"
  async with httpx.AsyncClient(timeout=30.0) as client:
    resp = await client.post(
      "https://api.anthropic.com/v1/messages",
      headers=headers,
      json={
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 256,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
      },
    )
    resp.raise_for_status()
  payload = resp.json()
  return "".join(
    part.get("text", "")
    for part in payload.get("content", [])
    if isinstance(part, dict) and part.get("type") == "text"
  )


async def _clear_goal(chat_id: str, run_token: str | None) -> None:
  """Clear stored goal state through the writer actor."""
  await _await_ack(get_writer().submit(ClearGoal(
    chat_id=chat_id, run_token=run_token or "",
  )))


async def _complete_goal(
  chat_id: str,
  run_token: str | None,
  *,
  turns: int,
  reason: str,
  token_spend: int,
) -> dict:
  """Persist an achieved record and clear the active goal."""
  return await _await_ack(get_writer().submit(CompleteGoal(
    chat_id=chat_id,
    run_token=run_token or "",
    turns=turns,
    reason=reason,
    token_spend=token_spend,
  )))


async def _increment_goal_turn(
  chat_id: str, run_token: str | None, reason: str,
) -> int:
  """Increment stored goal turns through the writer actor."""
  result = await _await_ack(get_writer().submit(IncrementGoalTurn(
    chat_id=chat_id, run_token=run_token or "", reason=reason,
  )))
  return int((result or {}).get("turns") or 0)


async def _record_goal_tokens(
  chat_id: str, run_token: str | None, token_delta: int,
) -> int:
  """Add token usage to the stored active goal through the actor."""
  if token_delta <= 0:
    return 0
  result = await _await_ack(get_writer().submit(RecordGoalTokens(
    chat_id=chat_id, run_token=run_token or "", token_delta=token_delta,
  )))
  return int((result or {}).get("token_spend") or 0)


async def _reset_goal_progress(
  chat_id: str, run_token: str | None,
) -> dict:
  """Reset per-run goal counters when a stored goal resumes."""
  return await _await_ack(get_writer().submit(ResetGoalProgress(
    chat_id=chat_id, run_token=run_token or "",
  )))


async def _pause_goal(chat_id: str, run_token: str | None) -> dict:
  """Mark the stored active goal as paused through the actor."""
  return await _await_ack(get_writer().submit(PauseGoal(
    chat_id=chat_id, run_token=run_token or "",
  )))


async def _resume_goal(chat_id: str, run_token: str | None) -> dict:
  """Mark the stored active goal active and reset per-run counters."""
  return await _await_ack(get_writer().submit(ResumeGoal(
    chat_id=chat_id, run_token=run_token or "",
  )))
