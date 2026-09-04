"""Narrow client for the root-owned community-registry broker surface.

The browser never receives the central capability.  This module sends the
exact body accepted by the local API over the root-owned Unix socket; the
identity broker binds method, path, body digest, and request id before minting
the short-lived central capability.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx


DEFAULT_SOCKET = "/run/mobius-identity-broker.sock"
COMMUNITY_PREFIX = "/v1/community"
COMMUNITY_BASE_URL = os.environ.get(
  "MOBIUS_COMMUNITY_REGISTRY_URL", "https://www.mobius.you",
).rstrip("/")
MAX_RESPONSE_BYTES = 10_000_000
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_PUBLIC_APP_READS = (
  re.compile(r"/v1/community/apps"),
  re.compile(r"/v1/community/apps/[A-Za-z0-9_:-]{8,200}"),
  re.compile(
    r"/v1/community/apps/[A-Za-z0-9_:-]{8,200}/revisions/"
    r"[A-Za-z0-9_:-]{8,200}"
  ),
)


@dataclass(frozen=True)
class CommunityBrokerError(Exception):
  status_code: int
  detail: str
  code: str = "community_unavailable"
  retry_after: int | None = None


def canonical_body(value: Any | None) -> bytes:
  if value is None:
    return b""
  return json.dumps(
    value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
  ).encode("utf-8")


def bound_request_id(
  method: str, path: str, body: bytes, idempotency_key: str | None,
) -> str:
  material = b"mobius-community-bff-v1\0"
  material += method.upper().encode("ascii") + b"\0" + path.encode("utf-8")
  material += b"\0" + (idempotency_key or "").encode("utf-8") + b"\0" + body
  return "community:" + hashlib.sha256(material).hexdigest()


def _public_read_target(method: str, path: str, target: str) -> str | None:
  if method != "GET":
    return None
  if path == "/v1/community/editorial/spotlight" or any(
    pattern.fullmatch(path) for pattern in _PUBLIC_APP_READS
  ):
    return "/api/store/v1" + target.removeprefix(COMMUNITY_PREFIX)
  return None


def _decode_response(response: httpx.Response) -> tuple[Any, dict[str, str]]:
  if len(response.content) > MAX_RESPONSE_BYTES:
    raise CommunityBrokerError(502, "The community service response was too large.")
  try:
    payload = response.json() if response.content else {}
  except ValueError as exc:
    raise CommunityBrokerError(
      502, "The community service returned an invalid response.",
    ) from exc
  response_headers = {
    key.lower(): value for key, value in response.headers.items()
    if key.lower() in {"retry-after", "etag", "last-modified"}
  }
  return payload, response_headers


class CommunityBrokerClient:
  def __init__(
    self,
    *,
    socket_path: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
  ) -> None:
    self.socket_path = socket_path or os.environ.get(
      "MOBIUS_IDENTITY_BROKER_SOCKET", DEFAULT_SOCKET,
    )
    self.transport = transport

  async def request(
    self,
    method: str,
    path: str,
    *,
    body: Any | None = None,
    params: dict[str, str | int | None] | None = None,
    idempotency_key: str | None = None,
  ) -> tuple[Any, int, dict[str, str]]:
    method = method.upper()
    if not path.startswith(COMMUNITY_PREFIX + "/") and path != "/identity":
      raise ValueError("community broker path is outside its allow-list")
    if method not in {"GET", "POST", "PUT"}:
      raise ValueError("community broker method is not allowed")
    if method != "GET" and path != "/identity":
      if not _IDEMPOTENCY_KEY.fullmatch(str(idempotency_key or "")):
        raise CommunityBrokerError(
          400, "A valid Idempotency-Key is required.", "invalid_idempotency_key",
        )

    filtered_params = sorted(
      (key, value) for key, value in (params or {}).items() if value is not None
    )
    target = path
    if filtered_params:
      target += "?" + urlencode(filtered_params)
    encoded = canonical_body(body)
    request_id = bound_request_id(method, target, encoded, idempotency_key)
    headers = {
      "Accept": "application/json",
      "X-Mobius-Request-Id": request_id,
    }
    if encoded:
      headers["Content-Type"] = "application/json"
    if idempotency_key:
      headers["Idempotency-Key"] = idempotency_key
    broker_transport = self.transport or httpx.AsyncHTTPTransport(
      uds=self.socket_path,
    )
    try:
      async with httpx.AsyncClient(
        transport=broker_transport,
        base_url="http://mobius-identity-broker",
        timeout=45.0,
        follow_redirects=False,
      ) as client:
        response = await client.request(
          method, target, content=encoded if encoded else None, headers=headers,
        )
      payload, response_headers = _decode_response(response)
      public_target = _public_read_target(method, path, target)
      if (
        response.status_code == 401
        and isinstance(payload, dict)
        and payload.get("error") == "a mobius.you account must be linked"
        and public_target is not None
      ):
        async with httpx.AsyncClient(
          transport=self.transport,
          base_url=COMMUNITY_BASE_URL,
          timeout=45.0,
          follow_redirects=False,
        ) as client:
          response = await client.request("GET", public_target, headers=headers)
        payload, response_headers = _decode_response(response)
    except httpx.HTTPError as exc:
      raise CommunityBrokerError(
        503, "The Möbius community service could not be reached.",
      ) from exc
    if response.is_error:
      error = payload.get("error") if isinstance(payload, dict) else None
      if isinstance(error, dict):
        detail = str(error.get("message") or "The community request failed.")
        code = str(error.get("code") or "community_error")
        retry_after = error.get("retry_after")
      else:
        detail = str(
          payload.get("detail") or payload.get("error")
          or "The community request failed."
        ) if isinstance(payload, dict) else "The community request failed."
        code = "community_error"
        retry_after = None
      try:
        retry_after = int(retry_after or response_headers.get("retry-after") or 0) or None
      except (TypeError, ValueError):
        retry_after = None
      raise CommunityBrokerError(
        response.status_code, detail, code, retry_after,
      )
    return payload, response.status_code, response_headers


community_broker = CommunityBrokerClient()
