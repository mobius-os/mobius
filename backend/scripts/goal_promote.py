#!/usr/bin/env python3
"""Queue a verified hidden native-Goal activation for the current chat.

The working agent decides whether an ordinary owner request deserves durable
Goal mode.  This helper performs only the state transition: it reserves a
hidden ``/goal`` message behind the currently running turn using the ordinary
chat-send boundary.  When that turn settles, the existing queue handoff starts
the provider's native Goal loop.  No classifier and no second Goal engine live
here.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def _settings() -> tuple[str, str, str]:
  base = (os.environ.get("API_BASE_URL") or "").rstrip("/")
  token = os.environ.get("AGENT_TOKEN") or ""
  chat_id = os.environ.get("CHAT_ID") or ""
  missing = [
    name for name, value in (
      ("API_BASE_URL", base), ("AGENT_TOKEN", token), ("CHAT_ID", chat_id),
    ) if not value
  ]
  if missing:
    raise SystemExit(f"missing environment: {', '.join(missing)}")
  return base, token, chat_id


def _request(method: str, path: str, body=None):
  base, token, _ = _settings()
  data = None if body is None else json.dumps(body).encode("utf-8")
  request = Request(
    f"{base}{path}", data=data, method=method,
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
    },
  )
  try:
    with urlopen(request, timeout=30) as response:
      raw = response.read()
      return json.loads(raw) if raw else None
  except HTTPError as exc:
    raw = exc.read().decode("utf-8", errors="replace")
    try:
      detail = json.loads(raw).get("detail", raw)
    except json.JSONDecodeError:
      detail = raw
    raise SystemExit(
      f"goal promotion failed ({exc.code}): {detail}"
    ) from exc
  except URLError as exc:
    raise SystemExit(f"goal promotion failed: {exc.reason}") from exc


def _latest_visible_owner_cid(detail: dict) -> str:
  for message in reversed(list(detail.get("messages") or [])):
    if (
      isinstance(message, dict)
      and message.get("role") == "user"
      and not message.get("hidden")
      and isinstance(message.get("cid"), str)
      and message["cid"]
    ):
      return message["cid"]
  raise SystemExit(
    "goal promotion failed: no visible owner request identifies this turn"
  )


def _activation_cid(chat_id: str, source_cid: str, objective: str) -> str:
  """Make a retry of one decision idempotent without merging later requests."""
  return str(uuid.uuid5(
    uuid.NAMESPACE_URL,
    f"mobius:auto-goal:v1:{chat_id}:{source_cid}:{objective}",
  ))


def _activation_is_durable(
  runtime: dict, detail: dict, *, cid: str, objective: str,
) -> str | None:
  if runtime.get("active_goal_objective") == objective:
    return "active"
  for message in list(runtime.get("pending_messages") or []):
    if (
      isinstance(message, dict)
      and message.get("cid") == cid
      and message.get("hidden") is True
      and message.get("content") == f"/goal {objective}"
    ):
      return "queued"
  for message in list(detail.get("messages") or []):
    if (
      isinstance(message, dict)
      and message.get("cid") == cid
      and message.get("hidden") is True
      and message.get("content") == f"/goal {objective}"
    ):
      return "started"
  return None


def promote_goal(objective: str) -> str:
  objective = objective.strip()
  if not objective:
    raise SystemExit("goal promotion failed: objective is empty")

  _, _, chat_id = _settings()
  runtime = _request("GET", f"/api/chats/{quote(chat_id)}/runtime") or {}
  active = runtime.get("active_goal_objective")
  if active:
    if active == objective:
      return "active"
    raise SystemExit(
      "goal promotion failed: this chat already has a different active Goal"
    )
  if runtime.get("running") is not True:
    raise SystemExit(
      "goal promotion failed: automatic promotion must run during the "
      "owner request it is promoting"
    )

  detail = _request("GET", f"/api/chats/{quote(chat_id)}?limit=100") or {}
  source_cid = _latest_visible_owner_cid(detail)
  cid = _activation_cid(chat_id, source_cid, objective)
  _request(
    "POST", f"/api/chats/{quote(chat_id)}/messages",
    {
      "content": f"/goal {objective}",
      "hidden": True,
      "cid": cid,
    },
  )

  # The send acknowledgement is not enough: verify the exact hidden activation
  # now exists in the active Goal, durable queue, or transcript.  The cid makes
  # this proof safe across a lost response and retry.
  runtime = _request("GET", f"/api/chats/{quote(chat_id)}/runtime") or {}
  detail = _request("GET", f"/api/chats/{quote(chat_id)}?limit=100") or {}
  state = _activation_is_durable(
    runtime, detail, cid=cid, objective=objective,
  )
  if state is None:
    raise SystemExit(
      "goal promotion failed: the hidden native-Goal activation was not "
      "durably verified"
    )
  return state


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Promote the current ordinary request into a native Goal.",
  )
  parser.add_argument(
    "objective",
    help="concise outcome and observable completion condition",
  )
  args = parser.parse_args()
  state = promote_goal(args.objective)
  if state == "active":
    print("Goal promotion verified: the native Goal is active.")
  elif state == "queued":
    print(
      "Goal promotion verified: hidden native activation is queued behind "
      "this turn."
    )
  else:
    print("Goal promotion verified: hidden native activation has started.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
