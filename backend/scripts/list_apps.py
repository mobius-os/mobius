#!/usr/bin/env python3
"""Print the compact app identity list an agent needs before a build."""

import json
import os
import sys
import urllib.error
import urllib.request


def main() -> None:
  if len(sys.argv) != 1:
    print("Usage: list_apps.py", file=sys.stderr)
    raise SystemExit(2)
  token = os.environ.get("AGENT_TOKEN")
  if not token:
    print("AGENT_TOKEN environment variable is not set.", file=sys.stderr)
    raise SystemExit(1)
  base = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
  request = urllib.request.Request(
    f"{base}/api/apps/",
    headers={"Authorization": f"Bearer {token}"},
  )
  try:
    with urllib.request.urlopen(request, timeout=30) as response:
      apps = json.loads(response.read())
  except urllib.error.HTTPError as exc:
    body = exc.read().decode(errors="replace")
    print(f"Could not list apps ({exc.code}): {body}", file=sys.stderr)
    raise SystemExit(1) from exc
  except (urllib.error.URLError, json.JSONDecodeError) as exc:
    print(f"Could not list apps: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc
  compact = [
    {"id": app.get("id"), "name": app.get("name"), "slug": app.get("slug")}
    for app in apps
    if isinstance(app, dict)
  ]
  print(json.dumps(compact, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
  main()
