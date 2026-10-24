"""Call this Möbius instance's API as the current chat agent, for agent command-line tools."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def agent_credentials() -> tuple[str, str]:
  """The API base URL and agent token every chat agent run carries."""
  api = (os.environ.get("API_BASE_URL") or "").rstrip("/")
  token = os.environ.get("AGENT_TOKEN") or ""
  if not api or not token:
    raise SystemExit("API_BASE_URL and AGENT_TOKEN are required.")
  return api, token


def _error_detail(raw: bytes, fallback: str) -> str:
  """The server's reason in plain words; validation lists keep only their messages."""
  try:
    detail = json.loads(raw.decode("utf-8")).get("detail")
  except Exception:
    return raw.decode("utf-8", errors="replace") or fallback
  if isinstance(detail, list):
    messages = [str(item.get("msg", item)).removeprefix("Value error, ") for item in detail if isinstance(item, dict)]
    return "; ".join(messages) or fallback
  return str(detail)


def agent_request(
  api: str,
  token: str,
  method: str,
  path: str,
  payload: dict | None = None,
  *,
  label: str,
  timeout: float = 60,
) -> object | None:
  """One JSON request; an HTTP or network failure exits with its reason."""
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
    with urllib.request.urlopen(request, timeout=timeout) as response:
      raw = response.read()
  except urllib.error.HTTPError as exc:
    raw = exc.read()
    raise SystemExit(f"{label} request failed ({exc.code}): {_error_detail(raw, exc.reason)}")
  except OSError as exc:
    raise SystemExit(f"{label} request failed: {exc}")
  if not raw:
    return None
  return json.loads(raw.decode("utf-8"))
