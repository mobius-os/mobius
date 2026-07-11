#!/usr/bin/env python3
"""Signals a build milestone to the current building chat.

Usage:
  build_phase.py "<milestone label>"

Environment:
  AGENT_TOKEN   JWT bearer token for the Ultimate API.
  API_BASE_URL  Base URL of the backend (default: http://localhost:8000).

POSTs a chat-scoped `build_phase` event so the building chat renders a live
milestone rail. Best-effort by design: a failed POST notes the reason and
still exits 0 — a progress signal must never break a build.
"""

import json
import os
import sys
import urllib.error
import urllib.request


def _post_build_phase(base: str, token: str, label: str) -> None:
  """POST the build_phase event; note failures but never raise."""
  data = json.dumps({"type": "build_phase", "label": label}).encode()
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
  if len(sys.argv) != 2 or not sys.argv[1].strip():
    print('Usage: build_phase.py "<milestone label>"', file=sys.stderr)
    sys.exit(1)

  label = sys.argv[1].strip()
  token = os.environ.get("AGENT_TOKEN")
  if not token:
    # A milestone signal must never break a build: with no token there is
    # nothing to authenticate the POST, so note it and exit 0 anyway.
    print("AGENT_TOKEN not set; skipping build_phase.", file=sys.stderr)
    return

  base = os.environ.get("API_BASE_URL", "http://localhost:8000")
  _post_build_phase(base, token, label)


if __name__ == "__main__":
  main()
