#!/usr/bin/env python3
"""Print compact app identities, optionally narrowed by an exact field."""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def _args() -> argparse.Namespace:
  parser = argparse.ArgumentParser(
    description=(
      "List live apps. Exact filters narrow the compact result without "
      "turning a non-unique display name into an identity."
    ),
  )
  filters = parser.add_mutually_exclusive_group()
  filters.add_argument("--id", type=int, dest="app_id")
  filters.add_argument("--slug")
  filters.add_argument("--source-dir")
  filters.add_argument("--chat-id")
  filters.add_argument(
    "--name",
    help="exact display-name search; may return multiple apps",
  )
  parser.add_argument(
    "--with-source-dir",
    action="store_true",
    help="include each app's source_dir in the compact result",
  )
  return parser.parse_args()


def _matches(app: dict, args: argparse.Namespace) -> bool:
  if args.app_id is not None:
    return app.get("id") == args.app_id
  if args.slug is not None:
    return app.get("slug") == args.slug
  if args.source_dir is not None:
    return app.get("source_dir") == args.source_dir
  if args.chat_id is not None:
    return app.get("chat_id") == args.chat_id
  if args.name is not None:
    return app.get("name") == args.name
  return True


def main() -> None:
  args = _args()
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
  compact = []
  for app in apps:
    if not isinstance(app, dict) or not _matches(app, args):
      continue
    item = {
      "id": app.get("id"),
      "name": app.get("name"),
      "slug": app.get("slug"),
    }
    if args.with_source_dir:
      item["source_dir"] = app.get("source_dir")
    compact.append(item)
  print(json.dumps(compact, ensure_ascii=False, separators=(",", ":")))


if __name__ == "__main__":
  main()
