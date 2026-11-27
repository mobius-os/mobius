"""The model relay's translating path for app providers declared ``gemini``.

The Codex engine sends an OpenAI Responses request to the loopback relay. For
a Gemini provider the relay checks that the model is one the app declared,
reads the key for that exact model endpoint, sends the translated request over
HTTPS to ``<base_url>/models/<model>:streamGenerateContent``, and streams the
answer back as Responses events. The key is sent only to the declared base
URL, exactly as for the forwarding protocols.
"""

from __future__ import annotations

import functools
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import anyio
import httpx
from fastapi import Request
from fastapi.responses import JSONResponse

from app.gemini_request import qualified_tool_names, to_gemini_request
from app.gemini_stream import ResponsesStream
from app.routes.model_relay import _RelayResponse, _client, _responses_error

log = logging.getLogger("mobius.model_relay")

_ERROR_KINDS = {400: "invalid_request_error", 401: "authentication_error",
                403: "permission_error", 404: "not_found_error", 429: "rate_limit_exceeded"}
_MAX_ERROR_BYTES = 64 * 1024


def gemini_endpoint(model: str) -> str:
  """The path under ``base_url`` one Gemini turn calls (key sites check it)."""
  return f"/models/{model}:streamGenerateContent"


async def relay_gemini(provider: Any, request: Request, data_dir: str):
  payload = await _json_payload(request)
  if payload is None:
    return _responses_error(400, "invalid_request_error", "The request body must be a JSON object.")
  model = payload.get("model")
  if model not in {row["id"] for row in provider.declaration["models"]}:
    return _responses_error(404, "not_found_error", f"{provider.name} does not offer this model.")
  endpoint = gemini_endpoint(model)
  credential = await anyio.to_thread.run_sync(functools.partial(
    provider.read_relay_key, data_dir, endpoint=endpoint,
  ))
  if credential.unavailable or credential.key is None:
    return _responses_error(403, "permission_error", credential.unavailable or "Model key unavailable.")
  body = await anyio.to_thread.run_sync(to_gemini_request, payload)
  base_url = credential.base_url or provider.declaration["base_url"]
  url = f"{base_url.rstrip('/')}{endpoint}?alt=sse"
  headers = credential.headers or {"x-goog-api-key": credential.key}
  return await _send(provider, url, headers, body, payload)


async def _json_payload(request: Request) -> dict[str, Any] | None:
  try:
    payload = json.loads(await request.body())
  except (UnicodeDecodeError, json.JSONDecodeError):
    return None
  return payload if isinstance(payload, dict) else None


async def _send(provider: Any, url: str, auth: dict, body: dict, payload: dict):
  client = _client()
  headers = {"content-type": "application/json", **auth}
  try:
    upstream = await client.send(
      client.build_request("POST", url, json=body, headers=headers), stream=True,
    )
  except httpx.HTTPError as exc:
    log.warning("model relay %s upstream failed: %s", provider.name, type(exc).__name__)
    return _responses_error(502, "api_error", f"{provider.name} could not be reached.")
  if upstream.status_code != 200:
    return await _upstream_error(upstream, provider.name)
  stream = ResponsesStream(payload["model"], qualified_tool_names(payload.get("tools") or []))
  return _RelayResponse(
    upstream, _translated(upstream, stream), headers={"content-type": "text/event-stream"},
  )


async def _upstream_error(upstream: httpx.Response, name: str) -> JSONResponse:
  """Google's error status and message, in the Responses error shape."""
  try:
    raw = b""
    async for piece in upstream.aiter_bytes():
      raw += piece
      if len(raw) > _MAX_ERROR_BYTES:
        break
  finally:
    await upstream.aclose()
  message = _error_message(raw) or f"{name} answered {upstream.status_code}."
  response = _responses_error(
    upstream.status_code, _ERROR_KINDS.get(upstream.status_code, "api_error"), message,
  )
  if upstream.headers.get("retry-after"):
    response.headers["retry-after"] = upstream.headers["retry-after"]
  return response


def _error_message(raw: bytes) -> str | None:
  try:
    error = json.loads(raw).get("error")
  except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
    return None
  return error.get("message") if isinstance(error, dict) else None


async def _translated(upstream: httpx.Response, stream: ResponsesStream) -> AsyncIterator[bytes]:
  for event in stream.start():
    yield event
  async for line in upstream.aiter_lines():
    chunk = _sse_data(line)
    if chunk is None:
      continue
    for event in stream.feed(chunk):
      yield event
  for event in stream.finish():
    yield event


def _sse_data(line: str) -> dict[str, Any] | None:
  if not line.startswith("data:"):
    return None
  try:
    chunk = json.loads(line[5:].strip())
  except json.JSONDecodeError:
    return None
  return chunk if isinstance(chunk, dict) else None
