#!/usr/bin/env python3
"""Accept and publish one coherent mini-app source revision."""

import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request


def _usage() -> None:
  print("Usage: apply_app.py <source-dir>", file=sys.stderr)


def main() -> None:
  if len(sys.argv) != 2:
    _usage()
    raise SystemExit(2)
  try:
    source_dir = str(Path(sys.argv[1]).resolve(strict=True))
  except (OSError, RuntimeError) as exc:
    print(f"Cannot resolve app source directory: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc
  if not Path(source_dir).is_dir():
    print("App source path is not a directory.", file=sys.stderr)
    raise SystemExit(1)

  token = os.environ.get("AGENT_TOKEN")
  if not token:
    print("AGENT_TOKEN environment variable is not set.", file=sys.stderr)
    raise SystemExit(1)
  base = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
  payload = {
    "source_dir": source_dir,
    "chat_id": os.environ.get("CHAT_ID") or None,
  }
  request = urllib.request.Request(
    f"{base}/api/apps/apply",
    data=json.dumps(payload).encode(),
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
    },
    method="POST",
  )
  try:
    with urllib.request.urlopen(request, timeout=120) as response:
      result = json.loads(response.read())
  except urllib.error.HTTPError as exc:
    body = exc.read().decode(errors="replace")
    try:
      detail = json.loads(body).get("detail", body)
    except json.JSONDecodeError:
      detail = body
    print(
      f"App apply failed ({exc.code}): "
      f"{json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else detail}",
      file=sys.stderr,
    )
    raise SystemExit(1) from exc
  except urllib.error.URLError as exc:
    print(f"App apply failed: {exc.reason}", file=sys.stderr)
    raise SystemExit(1) from exc
  print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
  main()
