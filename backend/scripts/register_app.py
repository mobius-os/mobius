#!/usr/bin/env python3
"""Registers a mini-app from a JSX file via the Ultimate API.

Usage:
  register_app.py <name> <description> <jsx_file_path>

Environment:
  AGENT_TOKEN   JWT bearer token for the Ultimate API.
  API_BASE_URL  Base URL of the Ultimate backend (default: http://localhost:8000).

Prints the created or updated app JSON to stdout.
"""

import json
import os
import sys
import urllib.error
import urllib.request


def _call(url: str, token: str, method: str, data: dict | None = None):
  body = json.dumps(data).encode() if data is not None else None
  req = urllib.request.Request(
    url,
    data=body,
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
    },
    method=method,
  )
  try:
    with urllib.request.urlopen(req) as resp:
      body = resp.read()
      return json.loads(body) if body else None
  except urllib.error.HTTPError as exc:
    print(f"API error {exc.code}: {exc.read().decode()}", file=sys.stderr)
    sys.exit(1)


def _find_existing(apps: list, source_dir: str, name: str):
  """Find the app this (re-)registration refers to, by STABLE identity.

  Identity is source_dir, not the display name. A core app renamed in place
  (Memory Graph -> Memory, feature 097) keeps the same /data/apps/<slug>/ source
  dir, so matching on name would miss the existing row and CREATE a duplicate.
  The file watcher already keys edits on the exact source_dir, so it is the
  canonical per-app key. Fall back to the name only for legacy rows that
  predate source_dir being populated (NULL/absent in the API response), where
  there is no stable key to compare.
  """
  by_dir = next(
    (a for a in apps if a.get("source_dir") == source_dir), None
  )
  if by_dir is not None:
    return by_dir
  return next(
    (a for a in apps if not a.get("source_dir") and a.get("name") == name),
    None,
  )


def _notify(token: str, base: str, event_type: str, **kwargs):
  """Best-effort notification — failures are not fatal."""
  try:
    data = {"type": event_type, **kwargs}
    _call(f"{base}/api/notify", token, "POST", data)
  except SystemExit:
    pass  # notify is best-effort; don't abort on failure


def main() -> None:
  if len(sys.argv) != 4:
    print(
      "Usage: register_app.py <name> <description> <jsx_file_path>",
      file=sys.stderr,
    )
    sys.exit(1)

  name, description, jsx_path = sys.argv[1], sys.argv[2], sys.argv[3]

  try:
    with open(jsx_path, encoding="utf-8") as f:
      jsx_source = f.read()
  except OSError as exc:
    print(f"Cannot read JSX file: {exc}", file=sys.stderr)
    sys.exit(1)

  # Absolute directory of the JSX file — sent to the API so the file
  # watcher can resolve `<app_dir>/index.jsx` change events back to
  # this app's DB row exactly, without slugify-guessing the name.
  source_dir = os.path.dirname(os.path.abspath(jsx_path))

  token = os.environ.get("AGENT_TOKEN")
  if not token:
    print("AGENT_TOKEN environment variable is not set.", file=sys.stderr)
    sys.exit(1)

  base = os.environ.get("API_BASE_URL", "http://localhost:8000")
  # Tag the app with the current chat so errors can be routed back to it.
  chat_id = os.environ.get("CHAT_ID") or None

  # Trailing slash required — FastAPI redirects /api/apps → /api/apps/ and
  # urllib does not follow POST redirects, so use the canonical URL directly.
  apps = _call(f"{base}/api/apps/", token, "GET")
  # Match on the stable source_dir, not the display name: a rename keeps the
  # source dir but changes the name, and a name-only match would create a
  # duplicate row for the renamed app (feature 097).
  existing = _find_existing(apps, source_dir, name)

  if existing:
    app = _call(
      f"{base}/api/apps/{existing['id']}",
      token,
      "PATCH",
      {
        "description": description,
        "jsx_source": jsx_source,
        "chat_id": chat_id,
        "source_dir": source_dir,
      },
    )
  else:
    app = _call(
      f"{base}/api/apps/",
      token,
      "POST",
      {
        "name": name,
        "description": description,
        "jsx_source": jsx_source,
        "chat_id": chat_id,
        "source_dir": source_dir,
      },
    )

  print(json.dumps(app))
  _notify(token, base, "app_updated", appId=str(app["id"]))


if __name__ == "__main__":
  main()
