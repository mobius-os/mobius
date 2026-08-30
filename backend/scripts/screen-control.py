#!/usr/bin/env python3
"""Operate the owner-shared live screen for the current chat agent."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def _environment() -> tuple[str, str, str]:
  api = (os.environ.get("API_BASE_URL") or "").rstrip("/")
  token = os.environ.get("AGENT_TOKEN") or ""
  chat_id = os.environ.get("CHAT_ID") or ""
  if not api or not token or not chat_id:
    raise SystemExit("API_BASE_URL, AGENT_TOKEN, and CHAT_ID are required.")
  if not re.fullmatch(r"[A-Za-z0-9_-]+", chat_id):
    raise SystemExit("CHAT_ID has an unexpected shape.")
  return api, token, chat_id


def _request(
  api: str,
  token: str,
  method: str,
  path: str,
  payload: dict | None = None,
) -> object | None:
  data = None if payload is None else json.dumps(payload).encode("utf-8")
  request = urllib.request.Request(
    api + path,
    data=data,
    method=method,
    headers={
      "Authorization": f"Bearer {token}",
      **({"Content-Type": "application/json"} if data is not None else {}),
    },
  )
  try:
    with urllib.request.urlopen(request, timeout=60) as response:
      raw = response.read()
  except urllib.error.HTTPError as exc:
    raw = exc.read()
    try:
      detail = json.loads(raw.decode("utf-8")).get("detail")
    except Exception:
      detail = raw.decode("utf-8", errors="replace") or exc.reason
    raise SystemExit(f"screen-control request failed ({exc.code}): {detail}")
  except OSError as exc:
    raise SystemExit(f"screen-control request failed: {exc}")
  if not raw:
    return None
  return json.loads(raw.decode("utf-8"))


def _command(api: str, token: str, chat_id: str, payload: dict) -> object:
  return _request(
    api,
    token,
    "POST",
    f"/api/screen-control/chats/{chat_id}/commands",
    payload,
  )


def _write_screenshot(chat_id: str, result: object) -> Path:
  if not isinstance(result, dict):
    raise SystemExit("Shared browser returned an invalid screenshot response.")
  data_url = result.get("dataUrl")
  if not isinstance(data_url, str):
    raise SystemExit("Shared browser returned no screenshot data.")
  header, separator, encoded = data_url.partition(",")
  if not separator or not header.startswith("data:image/") or ";base64" not in header:
    raise SystemExit("Shared browser returned an unsupported screenshot encoding.")
  mime = str(result.get("mimeType") or "image/jpeg").lower()
  suffix = ".png" if mime == "image/png" else ".jpg"
  media_dir = Path("/data/chats") / chat_id / "media"
  media_dir.mkdir(parents=True, exist_ok=True)
  output = media_dir / f"live-screen-{time.time_ns()}{suffix}"
  try:
    decoded = base64.b64decode(encoded, validate=True)
  except (ValueError, TypeError) as exc:
    raise SystemExit(f"Shared browser returned malformed image data: {exc}")
  if not decoded:
    raise SystemExit("Shared browser returned an empty screenshot.")
  output.write_bytes(decoded)
  return output


def main() -> None:
  parser = argparse.ArgumentParser(description=__doc__)
  sub = parser.add_subparsers(dest="action", required=True)
  sub.add_parser("status")
  sub.add_parser("snapshot")
  sub.add_parser("screenshot")
  click = sub.add_parser("click")
  click.add_argument("ref")
  click_at = sub.add_parser("click-at")
  click_at.add_argument("x", type=float)
  click_at.add_argument("y", type=float)
  type_cmd = sub.add_parser("type")
  type_cmd.add_argument("text")
  type_cmd.add_argument("--ref")
  type_cmd.add_argument("--append", action="store_true")
  scroll = sub.add_parser("scroll")
  scroll.add_argument("delta_y", type=float)
  scroll.add_argument("--delta-x", type=float, default=0)
  scroll.add_argument("--x", type=float)
  scroll.add_argument("--y", type=float)
  press = sub.add_parser("press")
  press.add_argument("key")
  sub.add_parser("stop")
  args = parser.parse_args()
  api, token, chat_id = _environment()

  if args.action == "status":
    result = _request(api, token, "GET", f"/api/screen-control/chats/{chat_id}")
  elif args.action == "stop":
    _request(api, token, "DELETE", f"/api/screen-control/chats/{chat_id}")
    print("Shared-screen session stopped.")
    return
  elif args.action == "snapshot":
    result = _command(api, token, chat_id, {"action": "snapshot"})
  elif args.action == "screenshot":
    result = _command(api, token, chat_id, {"action": "screenshot"})
    output = _write_screenshot(chat_id, result)
    print(f"SCREENSHOT: {output}")
    print(f"![live screen](/api/chats/{chat_id}/media/{output.name})")
    return
  elif args.action == "click":
    result = _command(api, token, chat_id, {"action": "click", "ref": args.ref})
  elif args.action == "click-at":
    result = _command(api, token, chat_id, {
      "action": "click", "x": args.x, "y": args.y,
    })
  elif args.action == "type":
    payload = {
      "action": "type", "text": args.text, "replace": not args.append,
    }
    if args.ref:
      payload["ref"] = args.ref
    result = _command(api, token, chat_id, payload)
  elif args.action == "scroll":
    if (args.x is None) != (args.y is None):
      parser.error("--x and --y must be supplied together")
    payload = {
      "action": "scroll", "deltaX": args.delta_x, "deltaY": args.delta_y,
    }
    if args.x is not None:
      payload.update({"x": args.x, "y": args.y})
    result = _command(api, token, chat_id, payload)
  else:
    result = _command(api, token, chat_id, {"action": "press", "key": args.key})
  print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
  main()
