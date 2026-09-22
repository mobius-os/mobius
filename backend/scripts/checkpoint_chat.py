#!/usr/bin/env python3
"""Read or append this run's platform-owned chat continuity checkpoint."""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _settings() -> tuple[str, str]:
  base = (os.environ.get("API_BASE_URL") or "").rstrip("/")
  token = os.environ.get("AGENT_TOKEN") or ""
  if not base or not token:
    raise RuntimeError("API_BASE_URL and AGENT_TOKEN are required")
  return base, token


def _call(method: str, path: str, payload: dict | None = None) -> dict:
  base, token = _settings()
  request = Request(
    base + path,
    method=method,
    data=(json.dumps(payload).encode("utf-8") if payload is not None else None),
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
    },
  )
  try:
    with urlopen(request, timeout=15) as response:
      raw = response.read()
  except HTTPError as exc:
    detail = exc.read().decode("utf-8", "replace")
    raise RuntimeError(f"checkpoint request failed ({exc.code}): {detail}") from exc
  except URLError as exc:
    raise RuntimeError(f"checkpoint request failed: {exc.reason}") from exc
  result = json.loads(raw) if raw else {}
  if not isinstance(result, dict):
    raise RuntimeError("checkpoint request returned an invalid response")
  return result


def _parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description=__doc__)
  commands = parser.add_subparsers(dest="command", required=True)
  read = commands.add_parser("read")
  read.add_argument("--after-revision", type=int)
  read.add_argument("--limit", type=int, default=5)
  read.add_argument("--full", action="store_true")
  save = commands.add_parser("save")
  save.add_argument("--checkpoint-id", required=True)
  save.add_argument("--expected-revision", required=True, type=int)
  save.add_argument("--digest", required=True)
  save.add_argument("--summary")
  save.add_argument("--title")
  save.add_argument(
    "--source-cursor",
    help='Exact JSON object returned by read, e.g. {"message_count":2,"prefix_hash":"..."}',
  )
  return parser


def main() -> int:
  args = _parser().parse_args()
  if args.command == "read":
    query = {"limit": args.limit}
    if args.after_revision is not None:
      query["after_revision"] = args.after_revision
    if args.full:
      query["full"] = "true"
    result = _call("GET", "/api/chat/continuity?" + urlencode(query))
  else:
    payload = {
      "checkpoint_id": args.checkpoint_id,
      "expected_revision": args.expected_revision,
      "digest": args.digest,
    }
    if args.summary is not None:
      payload["summary"] = args.summary
    if args.title is not None:
      payload["title"] = args.title
    if args.source_cursor is not None:
      payload["source_cursor"] = json.loads(args.source_cursor)
    result = _call("POST", "/api/chat/continuity/checkpoints", payload)
  print(json.dumps(result, ensure_ascii=False, indent=2))
  return 0


if __name__ == "__main__":
  try:
    raise SystemExit(main())
  except RuntimeError as exc:
    print(str(exc), file=sys.stderr)
    raise SystemExit(1)
