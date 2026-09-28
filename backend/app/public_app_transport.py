"""Bounded, observable transport for anonymous app network access."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
import hashlib
import threading
import time
from urllib.parse import parse_qsl, urlsplit

from fastapi import HTTPException
from fastapi.responses import Response

from app.net_utils import validate_url_safe
from app.pinned_http_pool import PinnedFetchClientPool
from app.routes.proxy import _capped_response


_usage_lock = threading.Lock()
_usage: dict[int, dict] = {}

_fetch_clients = PinnedFetchClientPool()


async def close_public_fetch_clients() -> None:
  await _fetch_clients.close()


def _record_usage(app_id: int, *, elapsed: float, response_bytes: int, failed: bool):
  with _usage_lock:
    row = _usage.setdefault(app_id, {
      "requests": 0,
      "failures": 0,
      "response_bytes": 0,
      "upstream_seconds": 0.0,
      "started_at": datetime.now(UTC).isoformat(),
      "last_request_at": None,
    })
    row["requests"] += 1
    row["failures"] += int(failed)
    row["response_bytes"] += max(0, response_bytes)
    row["upstream_seconds"] += max(0.0, elapsed)
    row["last_request_at"] = datetime.now(UTC).isoformat()


def public_app_usage_snapshot() -> dict[str, dict]:
  """Cheap per-process counters for spotting a public app's server footprint."""
  with _usage_lock:
    return {
      "apps": {str(app_id): deepcopy(row) for app_id, row in _usage.items()},
      "transport": _fetch_clients.snapshot(),
    }


def _query_allowed(raw_query: str, contract: dict | None) -> bool:
  """Apply one explicit, bounded query contract.

  Prefix-only URL checks are not enough for endpoints such as GraphQL, where
  the query string selects the operation. Rules therefore default to no query
  parameters and may separately allow arbitrary values, exact values, or
  SHA-256-bound values without embedding large static queries in a manifest.
  """
  try:
    pairs = parse_qsl(raw_query, keep_blank_values=True, strict_parsing=True)
  except ValueError:
    return False
  if len(pairs) > 16 or len({name for name, _value in pairs}) != len(pairs):
    return False
  if not isinstance(contract, dict):
    contract = {}
  allowed = contract.get("allow", [])
  exact = contract.get("exact", {})
  digests = contract.get("sha256", {})
  if (
    not isinstance(allowed, list)
    or not isinstance(exact, dict)
    or not isinstance(digests, dict)
    or not all(isinstance(name, str) for name in allowed)
    or not all(isinstance(values, list) for values in exact.values())
    or not all(isinstance(values, list) for values in digests.values())
  ):
    return False
  values = dict(pairs)
  permitted = set(allowed) | set(exact) | set(digests)
  if set(values) - permitted:
    return False
  for name, accepted in exact.items():
    if values.get(name) not in accepted:
      return False
  for name, accepted in digests.items():
    value = values.get(name)
    if value is None or hashlib.sha256(value.encode()).hexdigest() not in accepted:
      return False
  return True


def _target_allowed(url: str, rules: list[dict]) -> bool:
  try:
    parsed = urlsplit(url)
    port = parsed.port
  except ValueError:
    return False
  if parsed.scheme != "https" or not parsed.hostname:
    return False
  host = parsed.hostname.lower()
  origin = f"https://{host if port in (None, 443) else f'{host}:{port}'}"
  path = parsed.path or "/"
  lowered_path = path.lower()
  # Do not let an allowlisted prefix be escaped through a server that decodes
  # encoded path separators/dot segments before routing the request.
  if (
    "\\" in path
    or any(segment in (".", "..") for segment in path.split("/"))
    or any(encoded in lowered_path for encoded in ("%2e", "%2f", "%5c"))
  ):
    return False
  for rule in rules:
    if not isinstance(rule, dict) or rule.get("origin") != origin:
      continue
    prefix = rule.get("path_prefix")
    if not isinstance(prefix, str):
      continue
    if (path == prefix or prefix == "/" or (
      prefix.endswith("/") and path.startswith(prefix)
    ) or path.startswith(prefix + "/")) and _query_allowed(
      parsed.query, rule.get("query"),
    ):
      return True
  return False


async def fetch_public_url(
  app_id: int,
  url: str,
  rules: list[dict],
) -> Response:
  """Fetch one declared public URL through the bounded shared transport."""
  if not _target_allowed(url, rules):
    raise HTTPException(status_code=403, detail="URL is not allowed for this public app.")

  pinned_url, host_header, sni_host = await asyncio.to_thread(
    validate_url_safe, url,
  )
  started = time.monotonic()
  try:
    async with _fetch_clients.lease(host_header, sni_host) as client:
      upstream = client.build_request("GET", pinned_url)
      upstream.headers["host"] = host_header
      upstream.extensions["sni_hostname"] = sni_host
      response = await _capped_response(
        client, upstream, forward_cache_headers=True,
      )
  except Exception:
    _record_usage(
      app_id, elapsed=time.monotonic() - started, response_bytes=0, failed=True,
    )
    raise
  body = getattr(response, "body", b"") or b""
  _record_usage(
    app_id,
    elapsed=time.monotonic() - started,
    response_bytes=len(body),
    failed=response.status_code >= 400,
  )
  return response
