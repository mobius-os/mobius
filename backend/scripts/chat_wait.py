#!/usr/bin/env python3
"""Declare, list, or cancel a durable wait for the current chat.

A declared wait is the sanctioned form of "I'll continue when X finishes":
the platform runs the check on its interval and resumes this chat when the
condition is met (or the deadline expires), surviving server restarts.
Command checks must be silent on an ordinary unmet exit 1. Exit 0 is met; any
other exit or diagnostic output wakes the chat immediately as ``check_failed``.

  chat_wait.py declare 'gate PR #851 merged' \
    --command 'gh pr view 851 --repo owner/repo --json state -q .state | grep -qx MERGED' \
    --interval 300 --deadline 86400
  chat_wait.py declare 'check back on the build in 20 minutes' --in 1200
  chat_wait.py list
  chat_wait.py cancel <wait-id>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  sub = parser.add_subparsers(dest="action", required=True)

  declare = sub.add_parser("declare", help="arm one durable wait")
  declare.add_argument("description", help="what this wait is for, in plain words")
  declare.add_argument(
    "--command",
    help=(
      "read-only silent-on-unmet check: 0=met, silent 1=not yet; "
      "any other result wakes the chat as check_failed"
    ),
  )
  declare.add_argument(
    "--in", dest="delay_secs", type=int,
    help="timer wait: resume after this many seconds (no command)",
  )
  declare.add_argument(
    "--interval", dest="interval_secs", type=int, default=None,
    help="seconds between checks (default 300, min 60)",
  )
  declare.add_argument(
    "--deadline", dest="deadline_secs", type=int, default=None,
    help="seconds until the wait expires and wakes the chat anyway "
         "(default 86400, max 604800)",
  )

  sub.add_parser("list", help="list this chat's armed waits")

  cancel = sub.add_parser("cancel", help="cancel one armed wait")
  cancel.add_argument("wait_id")

  args = parser.parse_args()
  _, _, chat_id = _settings()

  if args.action == "declare":
    if bool(args.command) == bool(args.delay_secs):
      raise SystemExit("declare needs exactly one of --command or --in")
    payload = {
      "description": args.description,
      "kind": "command" if args.command else "timer",
      "command": args.command,
      "delay_secs": args.delay_secs,
      "interval_secs": args.interval_secs,
      "deadline_secs": args.deadline_secs,
    }
    result = _call("POST", "/api/chat-waits", payload)
    print(json.dumps(result, indent=2))
    print(
      f"\nWait armed. This chat will resume on its own when the condition "
      f"is met or its check fails (probed now, then every "
      f"{result.get('interval_secs')}s; deadline "
      f"{result.get('deadline_at')}). Safe to end the turn.",
      file=sys.stderr,
    )
  elif args.action == "list":
    result = _call("GET", f"/api/chat-waits?chat_id={quote(chat_id)}")
    print(json.dumps(result, indent=2))
  else:
    result = _call("POST", f"/api/chat-waits/{quote(args.wait_id)}/cancel")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
  main()
