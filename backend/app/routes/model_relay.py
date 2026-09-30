"""Loopback relay between the agent engines and app model providers.

Every app-secret model provider runs through this relay: `responses`
declarations from the Codex engine and `anthropic_messages` declarations from
the Claude engine. The agent process holds only a relay token
(``providers.model_relay_token``); the relay checks it, attaches the provider's
real key, repairs request shapes that compatible providers reject, and streams
the response back unchanged. It never rewrites responses.

The route is reachable only over a direct loopback connection: every public
request arrives through a proxy peer, and a request carrying proxy forwarding
headers is refused even from loopback.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger("mobius.model_relay")

router = APIRouter(prefix="/api/model-relay", include_in_schema=False)

_PROXY_HEADERS = ("x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip")
_TIMEOUT = httpx.Timeout(connect=15.0, read=600.0, write=60.0, pool=15.0)
_http_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
  global _http_client
  if _http_client is None or _http_client.is_closed:
    _http_client = httpx.AsyncClient(timeout=_TIMEOUT)
  return _http_client


def _messages_error(status: int, kind: str, message: str) -> JSONResponse:
  return JSONResponse(
    {"type": "error", "error": {"type": kind, "message": message}},
    status_code=status,
  )


def _responses_error(status: int, kind: str, message: str) -> JSONResponse:
  return JSONResponse(
    {"error": {"type": kind, "message": message, "code": None, "param": None}},
    status_code=status,
  )


def _content_blocks(content: Any) -> list[Any]:
  if content is None:
    return []
  if isinstance(content, str):
    return [{"type": "text", "text": content}] if content else []
  return list(content) if isinstance(content, list) else [content]


def _merge_user_content(first: Any, second: Any) -> list[Any]:
  # A user turn answering tool calls must lead with its tool_result blocks.
  blocks = _content_blocks(first) + _content_blocks(second)
  is_result = [isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks]
  return (
    [b for b, r in zip(blocks, is_result) if r]
    + [b for b, r in zip(blocks, is_result) if not r]
  )


def normalize_messages_request(payload: dict[str, Any]) -> dict[str, Any]:
  """Fold mid-conversation ``system`` turns into user turns.

  The Claude engine sends harness context as role ``system`` entries inside
  ``messages``; Messages-compatible providers commonly accept only ``user``
  and ``assistant`` there (Together answers 400 "unknown variant `system`").
  The folded content keeps its position, and adjacent user turns merge
  because the format expects alternating roles. Top-level ``system`` is
  untouched.
  """
  messages = payload.get("messages")
  if not isinstance(messages, list) or not any(
    isinstance(m, dict) and m.get("role") == "system" for m in messages
  ):
    return payload
  folded: list[Any] = []
  for message in messages:
    if isinstance(message, dict) and message.get("role") == "system":
      message = {**message, "role": "user"}
    previous = folded[-1] if folded else None
    if (
      isinstance(message, dict) and message.get("role") == "user"
      and isinstance(previous, dict) and previous.get("role") == "user"
    ):
      folded[-1] = {
        **previous,
        "content": _merge_user_content(previous.get("content"), message.get("content")),
      }
    else:
      folded.append(message)
  return {**payload, "messages": folded}


def normalize_responses_request(payload: dict[str, Any]) -> dict[str, Any]:
  """Omit ``content: null`` from replayed reasoning items.

  With ``store: false`` the Codex engine sends prior output items back as
  input. Together streams reasoning items with ``content: null`` and then
  rejects that same item as input (400 "did not match any variant of untagged
  enum ResponseInput"). An absent ``content`` means the same thing in the
  Responses schema.
  """
  items = payload.get("input")
  if not isinstance(items, list) or not any(map(_has_null_reasoning_content, items)):
    return payload
  return {**payload, "input": [
    {k: v for k, v in item.items() if k != "content"}
    if _has_null_reasoning_content(item) else item
    for item in items
  ]}


def _has_null_reasoning_content(item: Any) -> bool:
  return (
    isinstance(item, dict) and item.get("type") == "reasoning"
    and "content" in item and item["content"] is None
  )


@dataclass(frozen=True)
class _Protocol:
  """How one wire protocol crosses the relay."""

  upstream_path: str  # appended to the declared base_url
  forwarded_headers: tuple[str, ...]
  key_headers: Callable[[str], dict[str, str]]
  normalize: Callable[[dict[str, Any]], dict[str, Any]]
  error: Callable[[int, str, str], JSONResponse]


_PROTOCOLS: dict[str, _Protocol] = {
  # Paths match what each engine appends to the base URL it is given.
  "anthropic_messages": _Protocol(
    "/v1/messages",
    ("anthropic-version", "anthropic-beta", "content-type", "content-encoding", "accept", "user-agent"),
    lambda key: {"x-api-key": key},
    normalize_messages_request,
    _messages_error,
  ),
  "responses": _Protocol(
    "/responses",
    ("openai-beta", "content-type", "content-encoding", "accept", "user-agent"),
    lambda key: {"authorization": f"Bearer {key}"},
    normalize_responses_request,
    _responses_error,
  ),
}


def _is_direct_loopback(request: Request) -> bool:
  if any(request.headers.get(name) for name in _PROXY_HEADERS):
    return False
  host = request.client.host if request.client else ""
  try:
    return host == "localhost" or ipaddress.ip_address(host).is_loopback
  except ValueError:
    return False


def _presented_token(request: Request) -> str:
  key = request.headers.get("x-api-key", "")
  if key:
    return key
  auth = request.headers.get("authorization", "")
  return auth[7:] if auth.lower().startswith("bearer ") else ""


@router.post("/{provider_id}/v1/messages")
async def relay_messages(provider_id: str, request: Request):
  return await _relay(provider_id, request, "anthropic_messages")


@router.post("/{provider_id}/v1/responses")
async def relay_responses(provider_id: str, request: Request):
  return await _relay(provider_id, request, "responses")


async def _relay(provider_id: str, request: Request, protocol_name: str):
  from app.app_secret_crypto import decrypt_app_secret
  from app.config import get_settings
  from app.providers import (
    AppModelProvider, PROVIDERS, model_relay_token, sync_app_model_providers,
  )

  protocol = _PROTOCOLS[protocol_name]
  if not _is_direct_loopback(request):
    return protocol.error(404, "not_found_error", "Not found.")
  data_dir = get_settings().data_dir
  sync_app_model_providers(data_dir)
  provider = PROVIDERS.get(provider_id)
  if not isinstance(provider, AppModelProvider) or provider.protocol != protocol_name:
    return protocol.error(404, "not_found_error", "This model connection is not installed.")
  if not hmac.compare_digest(_presented_token(request), model_relay_token(provider_id)):
    return protocol.error(401, "authentication_error", "Invalid model relay token.")
  unavailable = provider.check_auth(data_dir)
  if unavailable:
    return protocol.error(403, "permission_error", unavailable)
  key = decrypt_app_secret(
    Path(data_dir) / "app-secrets" / str(provider.app_id) / provider.declaration["secret_name"],
  ).strip()

  body = await request.body()
  try:
    payload = json.loads(body)
  except (UnicodeDecodeError, json.JSONDecodeError):
    payload = None  # forwarded untouched, with its content-encoding
  headers = {
    name: request.headers[name]
    for name in protocol.forwarded_headers if name in request.headers
  }
  if isinstance(payload, dict):
    body = json.dumps(protocol.normalize(payload)).encode()
    headers.pop("content-encoding", None)
  headers.update(protocol.key_headers(key))
  url = f"{provider.declaration['base_url'].rstrip('/')}{protocol.upstream_path}"
  if request.url.query:
    url = f"{url}?{request.url.query}"
  client = _client()
  try:
    upstream = await client.send(
      client.build_request("POST", url, content=body, headers=headers), stream=True,
    )
  except httpx.HTTPError as exc:
    log.warning("model relay %s upstream failed: %s", provider_id, type(exc).__name__)
    return protocol.error(502, "api_error", f"{provider.name} could not be reached.")

  async def body_chunks():
    # Close upstream even when the agent disconnects mid-stream (a Stop);
    # a background task would only run after a completed send.
    try:
      # Decoded bytes: httpx negotiated any content-encoding, so none is forwarded.
      async for chunk in upstream.aiter_bytes():
        yield chunk
    finally:
      await upstream.aclose()

  return StreamingResponse(
    body_chunks(),
    status_code=upstream.status_code,
    headers={"content-type": upstream.headers.get("content-type", "application/json")},
  )
