#!/usr/bin/env python3
"""Review or accept a Store app's local runtime declaration via Möbius.

The running server owns the App row and the ``app_updated`` broadcast, so this
stays a thin client over the owner API rather than touching the database.

Environment:
  AGENT_TOKEN    required; owner bearer token for the Möbius API.
  API_BASE_URL   backend base URL (default http://localhost:8000).

Usage:
  accept_local_runtime_capabilities.py <app-id>
  accept_local_runtime_capabilities.py <app-id> --accept-digest <sha256>
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.request


def _request(
  base_url: str,
  token: str,
  app_id: int,
  accept_digest: str | None,
) -> dict:
  suffix = f"/api/apps/{app_id}/runtime-capabilities"
  data = None
  method = "GET"
  headers = {"Authorization": f"Bearer {token}"}
  if accept_digest is not None:
    suffix += "/accept"
    data = json.dumps({"accept_digest": accept_digest}).encode()
    method = "POST"
    headers["Content-Type"] = "application/json"
  request = urllib.request.Request(
    f"{base_url.rstrip('/')}{suffix}",
    data=data,
    headers=headers,
    method=method,
  )
  try:
    with urllib.request.urlopen(request, timeout=30) as response:
      result = json.loads(response.read())
  except urllib.error.HTTPError as exc:
    body = exc.read().decode(errors="replace")
    try:
      detail = json.loads(body).get("detail", body)
    except json.JSONDecodeError:
      detail = body
    raise ValueError(str(detail)) from exc
  except urllib.error.URLError as exc:
    raise ValueError(f"Could not reach the Möbius server: {exc.reason}") from exc
  except json.JSONDecodeError as exc:
    raise ValueError("Möbius returned an invalid capability review.") from exc
  if not isinstance(result, dict):
    raise ValueError("Möbius returned an invalid capability review.")
  return result


def main(argv: list[str] | None = None) -> None:
  parser = argparse.ArgumentParser()
  parser.add_argument("--app-id", required=True, type=int)
  parser.add_argument("--accept-digest")
  args = parser.parse_args(argv)

  token = os.environ.get("AGENT_TOKEN")
  if not token:
    parser.exit(2, "error: AGENT_TOKEN environment variable is not set.\n")
  base_url = os.environ.get("API_BASE_URL", "http://localhost:8000")
  try:
    report = _request(base_url, token, args.app_id, args.accept_digest)
  except ValueError as exc:
    parser.exit(2, f"error: {exc}\n")
  print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
  main()
