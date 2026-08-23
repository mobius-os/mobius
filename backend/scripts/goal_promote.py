#!/usr/bin/env python3
"""Attach the current physical agent turn to a platform-owned Goal."""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def _settings() -> tuple[str, str, str, str]:
  base = (os.environ.get("API_BASE_URL") or "").rstrip("/")
  token = os.environ.get("AGENT_TOKEN") or ""
  chat_id = os.environ.get("CHAT_ID") or ""
  run_id = os.environ.get("MOBIUS_RUN_TOKEN") or ""
  missing = [
    name for name, value in (
      ("API_BASE_URL", base),
      ("AGENT_TOKEN", token),
      ("CHAT_ID", chat_id),
      ("MOBIUS_RUN_TOKEN", run_id),
    ) if not value
  ]
  if missing:
    raise SystemExit(f"missing environment: {', '.join(missing)}")
  return base, token, chat_id, run_id


def promote_goal(objective: str) -> dict:
  objective = objective.strip()
  if not objective:
    raise SystemExit("goal promotion failed: objective is empty")

  base, token, chat_id, run_id = _settings()
  request = Request(
    f"{base}/api/chats/{quote(chat_id)}/goal",
    data=json.dumps({"objective": objective}).encode("utf-8"),
    method="POST",
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
    },
  )
  try:
    with urlopen(request, timeout=30) as response:
      raw = response.read()
    try:
      payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
      raise SystemExit("goal promotion failed: malformed response")
  except HTTPError as exc:
    raw = exc.read().decode("utf-8", errors="replace")
    try:
      detail = json.loads(raw).get("detail", raw)
    except json.JSONDecodeError:
      detail = raw
    raise SystemExit(
      f"goal promotion failed ({exc.code}): {detail}"
    ) from exc
  except URLError as exc:
    raise SystemExit(f"goal promotion failed: {exc.reason}") from exc

  if (
    not isinstance(payload, dict)
    or payload.get("objective") != objective
    or payload.get("state") not in {"promoted", "active"}
    or not isinstance(payload.get("root_run_id"), str)
    or payload.get("run_id") != run_id
  ):
    raise SystemExit("goal promotion failed: activation was not verified")
  return payload


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Promote the current request into a platform-owned Goal.",
  )
  parser.add_argument(
    "objective",
    help="concise outcome and observable completion condition",
  )
  result = promote_goal(parser.parse_args().objective)
  if result["state"] == "active":
    print("Goal promotion verified: this turn already owns the Goal.")
  else:
    print("Goal promotion verified: this turn now owns the Goal.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
