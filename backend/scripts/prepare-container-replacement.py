#!/usr/bin/env python3
"""Ask the running Möbius worker to drain before host-owned cutover."""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main() -> int:
  if len(sys.argv) != 2:
    return 2
  token = Path("/data/service-token.txt").read_text(encoding="utf-8").strip()
  body = json.dumps({"operation_id": sys.argv[1]}).encode("utf-8")
  request = urllib.request.Request(
    "http://127.0.0.1:8000/api/admin/rebuild/prepare",
    data=body,
    method="POST",
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
      "Accept": "application/json",
    },
  )
  try:
    with urllib.request.urlopen(request, timeout=10) as response:
      return 0 if response.status == 202 else 1
  except (OSError, urllib.error.HTTPError):
    return 1


if __name__ == "__main__":
  raise SystemExit(main())
