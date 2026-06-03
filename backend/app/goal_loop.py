"""Shared helpers for autonomous goal-loop runner behavior."""

from __future__ import annotations

import inspect
import json
import os
import re
from typing import Any

from app.chat_writer import (
  ClearGoal,
  IncrementGoalTurn,
  await_ack as _await_ack,
  get_writer,
)


def _goal_continue_message(condition: str, reason: str) -> str:
  """Frames an autonomous continuation as same-session feedback."""
  return f"[goal] Not met: {reason}. Keep working toward: {condition}"


def _goal_turn_cap(condition: str) -> int:
  """Return the smaller of a user stop clause and the hard safety cap."""
  match = re.search(r"\bstop\s+after\s+(\d+)\b", condition, re.I)
  requested = int(match.group(1)) if match else 25
  return max(0, min(requested, 25))


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


async def _increment_goal_turn(
  chat_id: str, run_token: str | None, reason: str,
) -> int:
  """Increment stored goal turns through the writer actor."""
  result = await _await_ack(get_writer().submit(IncrementGoalTurn(
    chat_id=chat_id, run_token=run_token or "", reason=reason,
  )))
  return int((result or {}).get("turns") or 0)
