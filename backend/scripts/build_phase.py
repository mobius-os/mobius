#!/usr/bin/env python3
"""Signals a build milestone to the current building chat.

Usage:
  build_phase.py "<milestone label>"

Environment:
  AGENT_TOKEN   JWT bearer token for the Ultimate API.
  API_BASE_URL  Base URL of the backend (default: http://localhost:8000).
  CHAT_ID       The building chat this turn runs in (set by the runner).

POSTs a chat-scoped `build_phase` event so the building chat renders a live
milestone rail. Best-effort by design: EVERY failure — malformed invocation,
missing env, failed POST — notes the reason to stderr and still exits 0,
because a progress signal must never break the build step it is part of.
"""

import json
import os
import sys
import urllib.error
import urllib.request


def _post_build_phase(
  base: str, token: str, chat_id: str, label: str
) -> None:
  """POST the build_phase event; note failures but never raise."""
  data = json.dumps({
    "type": "build_phase",
    "chatId": chat_id,
    "label": label,
  }).encode()
  req = urllib.request.Request(
    f"{base}/api/notify",
    data=data,
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
    },
    method="POST",
  )
  try:
    with urllib.request.urlopen(req):
      pass
  except (urllib.error.URLError, OSError) as exc:
    print(f"build_phase notify failed: {exc}", file=sys.stderr)


def main() -> None:
  args = sys.argv[1:]
  label = args[0].strip() if args else ""
  if len(args) != 1 or not label:
    print('Usage: build_phase.py "<milestone label>"', file=sys.stderr)
    return

  token = os.environ.get("AGENT_TOKEN")
  if not token:
    print("AGENT_TOKEN not set; skipping build_phase.", file=sys.stderr)
    return

  # The event routes to the broadcast of THIS turn's chat — the backend
  # drops a build_phase with no chat to feed, so skip the POST entirely.
  chat_id = os.environ.get("CHAT_ID")
  if not chat_id:
    print("CHAT_ID not set; skipping build_phase.", file=sys.stderr)
    return

  base = os.environ.get("API_BASE_URL", "http://localhost:8000")
  _post_build_phase(base, token, chat_id, label)


if __name__ == "__main__":
  main()
