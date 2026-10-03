"""Declare or cancel this chat's durable wait (control library).

Command checks must be silent on an ordinary unmet exit 1. Exit 0 is met; any
other exit or diagnostic output wakes the chat immediately as ``check_failed``.

Agents call this through the Möbius control tools. When a provider cannot
surface those tools, the one command-line fallback for every control is
`python3 /data/platform/backend/scripts/mobius_control_mcp.py call <tool>
--args-json '<json>'` (or `--args-json -` to read the JSON from stdin).
"""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def _settings() -> tuple[str, str, str]:
  base = (os.environ.get("API_BASE_URL") or "").rstrip("/")
  token = os.environ.get("AGENT_TOKEN") or ""
  chat_id = os.environ.get("CHAT_ID") or ""
  missing = [
    name for name, value in (
      ("API_BASE_URL", base),
      ("AGENT_TOKEN", token),
      ("CHAT_ID", chat_id),
    ) if not value
  ]
  if missing:
    raise SystemExit(f"missing environment: {', '.join(missing)}")
  return base, token, chat_id


def _call(method: str, path: str, payload: dict | None = None) -> dict:
  base, token, _ = _settings()
  request = Request(
    f"{base}{path}",
    data=json.dumps(payload).encode("utf-8") if payload is not None else None,
    method=method,
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
    },
  )
  try:
    with urlopen(request, timeout=30) as response:
      return json.loads(response.read() or b"{}")
  except HTTPError as exc:
    detail = exc.read().decode("utf-8", errors="replace")[:500]
    raise SystemExit(f"request failed ({exc.code}): {detail}")
  except URLError as exc:
    raise SystemExit(f"request failed: {exc.reason}")


def declare_wait(
  description: str,
  *,
  command: str | None = None,
  condition_owner: str | None = None,
  delay_secs: int | None = None,
  interval_secs: int | None = None,
  deadline_secs: int | None = None,
  github_checks: dict | None = None,
  check_description: str | None = None,
  on_ready: str | None = None,
  owner_chat_id: str | None = None,
) -> dict:
  """Arm one bounded wait through the chat-bound platform API."""
  if sum(value is not None for value in (command, delay_secs, github_checks)) != 1:
    raise SystemExit("declare needs exactly one of command, delay_secs, or github_checks")
  if command and not (condition_owner or "").strip():
    raise SystemExit("command waits need --owner")
  if (command or github_checks is not None) and deadline_secs is None:
    raise SystemExit("command waits need --deadline")
  return _call("POST", "/api/chat-waits", {
    "description": description,
    "condition_owner": condition_owner,
    "kind": "github_checks" if github_checks is not None else "command" if command else "timer",
    "github_checks": github_checks,
    "check_description": check_description,
    "on_ready": on_ready,
    "owner_chat_id": owner_chat_id,
    "command": command,
    "delay_secs": delay_secs,
    "interval_secs": interval_secs,
    "deadline_secs": deadline_secs,
  })


def cancel_wait(wait_id: str) -> dict:
  """Cancel one exact wait through the same chat-bound platform API."""
  return _call("POST", f"/api/chat-waits/{quote(wait_id)}/cancel")
