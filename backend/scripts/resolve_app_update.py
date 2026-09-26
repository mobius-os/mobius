#!/usr/bin/env python3
"""Finish a pending Store app update from its private resolution checkout."""

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.request

# The resolver's checkout lives inside the app's git directory; either path
# names the same pending update.
_CHECKOUT_SUFFIX = (".git", "mobius-pending-update", "worktree")


def _post(path: str, payload: dict) -> dict:
  token = os.environ.get("AGENT_TOKEN")
  if not token:
    print("AGENT_TOKEN environment variable is not set.", file=sys.stderr)
    raise SystemExit(1)
  base = os.environ.get("API_BASE_URL", "http://localhost:8000").rstrip("/")
  request = urllib.request.Request(
    f"{base}/api/apps/{path}",
    data=json.dumps(payload).encode(),
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
    },
    method="POST",
  )
  try:
    with urllib.request.urlopen(request, timeout=300) as response:
      return json.loads(response.read())
  except urllib.error.HTTPError as exc:
    body = exc.read().decode(errors="replace")
    try:
      detail = json.loads(body).get("detail", body)
    except json.JSONDecodeError:
      detail = body
    rendered = (
      json.dumps(detail, ensure_ascii=False)
      if isinstance(detail, dict) else detail
    )
    print(f"App update was not finished ({exc.code}): {rendered}", file=sys.stderr)
    raise SystemExit(1) from exc
  except urllib.error.URLError as exc:
    print(f"App update was not finished: {exc.reason}", file=sys.stderr)
    raise SystemExit(1) from exc


def main() -> None:
  parser = argparse.ArgumentParser(
    description=(
      "Install a committed update resolution, merging any edits made to the "
      "live app meanwhile."
    ),
  )
  parser.add_argument("source_dir", help="/data/apps/<slug> or its resolution checkout")
  # Resolver chats started on an earlier release may still use these.
  parser.add_argument("--finalize", action="store_true", help=argparse.SUPPRESS)
  parser.add_argument("--reviewed-tree", help=argparse.SUPPRESS)
  parser.add_argument("--review", action="store_true", help=argparse.SUPPRESS)
  parser.add_argument("--policy", help=argparse.SUPPRESS)
  args = parser.parse_args()
  if args.review or args.policy:
    print(
      "Policy and review steps no longer exist. Reread "
      "/data/shared/skills/resolving-app-git.md: reconcile, commit, then run "
      "this command with only the app path.",
      file=sys.stderr,
    )
    raise SystemExit(2)

  try:
    path = Path(args.source_dir).resolve(strict=True)
  except (OSError, RuntimeError) as exc:
    print(f"Cannot resolve app source directory: {exc}", file=sys.stderr)
    raise SystemExit(1) from exc
  if path.parts[-3:] == _CHECKOUT_SUFFIX:
    path = path.parents[2]
  if not path.is_dir():
    print("App source path is not a directory.", file=sys.stderr)
    raise SystemExit(1)

  result = _post("resolve-update", {"source_dir": str(path)})
  print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
  main()
