"""SSRF-safe transport for Common federation's outbound requests.

Common accepts peer locations from signed and unsigned protocol data.  Every
outbound Common request therefore crosses this one boundary: resolve and
validate with the platform's canonical policy, connect to that exact address,
preserve the original Host/SNI identity, reject redirects, ignore ambient
proxies, and cap the response before buffering it.
"""

from __future__ import annotations

import asyncio
import json as json_module
from collections.abc import Mapping
from typing import Any, Literal

import httpx

from app.net_utils import validate_url_safe

DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class FederationTransportError(Exception):
  """A peer response violated the bounded federation transport contract."""


def _json_object(body: bytes, content_type: str) -> None:
  media_type = content_type.split(";", 1)[0].strip().lower()
  if media_type != "application/json" and not media_type.endswith("+json"):
    raise FederationTransportError("Peer response is not JSON.")
  try:
    value = json_module.loads(body)
  except (UnicodeDecodeError, json_module.JSONDecodeError) as exc:
    raise FederationTransportError("Peer response contains invalid JSON.") from exc
  if not isinstance(value, dict):
    raise FederationTransportError("Peer response must be a JSON object.")


async def federation_request(
  method: str,
  url: str,
  *,
  json: Any = None,
  params: Mapping[str, Any] | None = None,
  max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
  response_format: Literal["json", "binary"] = "json",
  timeout_seconds: float = 10.0,
) -> httpx.Response:
  """Send one bounded request to a validated, DNS-pinned public URL.

  Redirect responses are failures rather than a second implicit request.  The
  caller may intentionally make another call, but it must pass through this
  function and validation again.  JSON responses are syntax-, media-type-,
  and top-level-shape checked before they leave the transport boundary.
  """
  if max_response_bytes < 1:
    raise ValueError("max_response_bytes must be positive")
  if response_format not in ("json", "binary"):
    raise ValueError("response_format must be 'json' or 'binary'")

  original_url = str(httpx.URL(url, params=params)) if params else url
  # getaddrinfo is blocking.  Keep attacker-controlled DNS away from the
  # server's async event loop while retaining the canonical shared policy.
  pinned_url, host_header, sni_host = await asyncio.to_thread(
    validate_url_safe, original_url
  )
  # This request object is only the safe, unpinned URL attached to the returned
  # response for status reporting. Do not serialize a potentially large
  # envelope twice; the actual request below owns its body.
  public_request = httpx.Request(method, original_url)

  async with httpx.AsyncClient(
    follow_redirects=False,
    timeout=timeout_seconds,
    trust_env=False,
  ) as client:
    request = client.build_request(method, pinned_url, json=json)
    request.headers["host"] = host_header
    request.extensions["sni_hostname"] = sni_host
    upstream = await client.send(request, stream=True)
    try:
      if 300 <= upstream.status_code < 400:
        raise FederationTransportError("Federation redirects are not allowed.")

      declared_length = upstream.headers.get("content-length")
      if declared_length is not None:
        try:
          if int(declared_length) > max_response_bytes:
            raise FederationTransportError("Peer response exceeds the allowed size.")
        except ValueError:
          pass

      body = bytearray()
      async for chunk in upstream.aiter_bytes():
        room = max_response_bytes + 1 - len(body)
        if room <= 0:
          break
        body.extend(chunk[:room])
        if len(body) > max_response_bytes:
          raise FederationTransportError("Peer response exceeds the allowed size.")
    finally:
      await upstream.aclose()

  # aiter_bytes already decoded content encodings; retain representation
  # metadata without asking HTTPX to decode the buffered body a second time.
  headers = upstream.headers.copy()
  for name in ("content-encoding", "content-length", "transfer-encoding"):
    headers.pop(name, None)
  response = httpx.Response(
    upstream.status_code,
    headers=headers,
    content=bytes(body),
    request=public_request,
  )
  if 200 <= response.status_code < 300 and response_format == "json":
    _json_object(response.content, response.headers.get("content-type", ""))
  return response
