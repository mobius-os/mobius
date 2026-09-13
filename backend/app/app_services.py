"""Bounded request/response execution for app-owned server policy.

The platform owns authentication, accepted-runtime identity, process limits,
and revocation. The app owns the request paths and domain behavior behind one
small JSON protocol. This is deliberately not a framework or an import hook:
one request starts one reviewed Python entrypoint and receives one JSON reply.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import signal
import sys
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException

from app import auth
from app.applied_app_runtime import AppliedRuntimeUnavailable, runtime_root
from app.config import get_settings
from app.manifest_contract import SERVICE_REQUEST_MAX_BYTES


log = logging.getLogger(__name__)
MAX_REQUEST_BYTES = SERVICE_REQUEST_MAX_BYTES
MAX_RESPONSE_BYTES = SERVICE_REQUEST_MAX_BYTES
MAX_ERROR_BYTES = 16 * 1024
SERVICE_TIMEOUT_SECONDS = 15
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}$")
_FORBIDDEN_HEADERS = frozenset({
  "connection", "content-length", "content-type", "keep-alive", "proxy-authenticate",
  "proxy-authorization", "set-cookie", "te", "trailer",
  "transfer-encoding", "upgrade",
})
_global_slots = asyncio.Semaphore(8)
_app_slots: dict[int, asyncio.Semaphore] = {}


def service_contract(app, *, access: str) -> dict:
  contract = app.capability_contract if isinstance(app.capability_contract, dict) else {}
  service = contract.get("service")
  if not isinstance(service, dict) or service.get("protocol") != "json-v1":
    raise HTTPException(404, "App service not found.")
  accepted_access = service.get("access", "self")
  if access == "public" and accepted_access != "public":
    raise HTTPException(404, "Public app service not found.")
  if access == "apps" and accepted_access not in {"apps", "public"}:
    raise HTTPException(403, "This app service is private.")
  entry = service.get("entry")
  if not isinstance(entry, str) or not entry.endswith(".py"):
    raise HTTPException(503, "Accepted app service declaration is invalid.")
  return service


def service_entry(app, service: dict) -> Path:
  try:
    root = runtime_root(app)
  except AppliedRuntimeUnavailable as exc:
    raise HTTPException(503, str(exc)) from exc
  entry = root / service["entry"]
  try:
    if entry.is_symlink() or not entry.is_file() or entry.parent != root:
      raise HTTPException(503, "Accepted app service entry is unavailable.")
  except OSError as exc:
    raise HTTPException(503, "Accepted app service entry is unavailable.") from exc
  return entry


def service_environment(app, owner) -> dict[str, str]:
  allowed = {"PATH", "LANG", "LC_ALL", "TZ", "HOME"}
  env = {key: value for key, value in os.environ.items() if key in allowed}
  settings = get_settings()
  env.update({
    "APP_ID": str(app.id),
    "APP_SLUG": str(app.slug),
    "APP_STORAGE_DIR": str(Path(settings.data_dir) / "apps" / str(app.id)),
    "API_BASE_URL": settings.api_base_url,
    "INSTANCE_DOMAIN": settings.domain,
    "INSTANCE_ORIGIN": settings.frontend_origin.rstrip("/"),
    "APP_TOKEN": auth.create_app_token(
      app.id,
      owner.username,
      owner.token_epoch,
      app_nonce=app.token_nonce,
      expires_delta=timedelta(minutes=5),
    ),
  })
  return env


async def _read_bounded(stream, limit: int) -> bytes:
  chunks: list[bytes] = []
  total = 0
  while True:
    chunk = await stream.read(64 * 1024)
    if not chunk:
      return b"".join(chunks)
    total += len(chunk)
    if total > limit:
      raise ValueError("output limit exceeded")
    chunks.append(chunk)


async def _write_request(stream, body: bytes) -> None:
  stream.write(body)
  await stream.drain()
  stream.close()
  await stream.wait_closed()


async def _stop_process(process, *tasks) -> None:
  try:
    os.killpg(process.pid, signal.SIGKILL)
  except ProcessLookupError:
    pass
  await process.wait()
  for task in tasks:
    task.cancel()
  await asyncio.gather(*tasks, return_exceptions=True)


def _response_headers(value) -> dict[str, str]:
  if value is None:
    return {}
  if not isinstance(value, dict) or len(value) > 16:
    raise ValueError("response headers must be a bounded object")
  headers: dict[str, str] = {}
  for name, raw in value.items():
    if (
      not isinstance(name, str)
      or _HEADER_NAME.fullmatch(name) is None
      or name.lower() in _FORBIDDEN_HEADERS
      or not isinstance(raw, str)
      or len(raw) > 4096
      or "\r" in raw
      or "\n" in raw
    ):
      raise ValueError("response contains an invalid header")
    headers[name] = raw
  return headers


async def invoke_service(
  app, owner, request_envelope: dict,
) -> tuple[int, object, dict[str, str], str | None]:
  service = service_contract(
    app, access="public" if request_envelope.get("public") else "self",
  )
  entry = service_entry(app, service)
  request_bytes = json.dumps(
    request_envelope, ensure_ascii=False, separators=(",", ":"),
  ).encode("utf-8")
  if len(request_bytes) > MAX_REQUEST_BYTES:
    raise HTTPException(413, "App service request is too large.")

  # A service owns its persistence semantics. Serialize one app's requests so
  # simple file-backed services do not need a platform-specific lock API.
  slot = _app_slots.setdefault(app.id, asyncio.Semaphore(1))
  async with _global_slots, slot:
    try:
      process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(entry),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(entry.parent),
        env=service_environment(app, owner),
        start_new_session=True,
      )
    except OSError as exc:
      raise HTTPException(502, "App service could not start.") from exc
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_task = asyncio.create_task(_read_bounded(process.stdout, MAX_RESPONSE_BYTES))
    stderr_task = asyncio.create_task(_read_bounded(process.stderr, MAX_ERROR_BYTES))
    write_task = asyncio.create_task(_write_request(process.stdin, request_bytes))
    try:
      _written, stdout, stderr, returncode = await asyncio.wait_for(
        asyncio.gather(write_task, stdout_task, stderr_task, process.wait()),
        timeout=SERVICE_TIMEOUT_SECONDS,
      )
    except (TimeoutError, ValueError) as exc:
      await _stop_process(process, write_task, stdout_task, stderr_task)
      raise HTTPException(503, "App service exceeded its execution limits.")
    except OSError as exc:
      await _stop_process(process, write_task, stdout_task, stderr_task)
      raise HTTPException(502, "App service failed before accepting its request.") from exc
    except asyncio.CancelledError:
      await _stop_process(process, write_task, stdout_task, stderr_task)
      raise
    try:
      os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
      pass
    if returncode != 0:
      detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
      log.warning("App service %s failed: %s", app.slug, detail or "no diagnostics")
      raise HTTPException(502, "App service failed.")
  try:
    response = json.loads(stdout)
  except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise HTTPException(502, "App service returned invalid JSON.") from exc
  if not isinstance(response, dict) or set(response) - {
    "status", "body", "body_base64", "headers", "media_type",
  }:
    raise HTTPException(502, "App service returned an invalid response envelope.")
  status = response.get("status", 200)
  if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
    raise HTTPException(502, "App service returned an invalid status.")
  try:
    headers = _response_headers(response.get("headers"))
  except ValueError as exc:
    raise HTTPException(502, str(exc)) from exc
  encoded = response.get("body_base64")
  media_type = response.get("media_type")
  if encoded is not None:
    if response.get("body") is not None or not isinstance(encoded, str):
      raise HTTPException(502, "App service returned an invalid binary response.")
    if not isinstance(media_type, str) or _MEDIA_TYPE.fullmatch(media_type) is None:
      raise HTTPException(502, "App service returned an invalid media type.")
    try:
      body = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
      raise HTTPException(502, "App service returned invalid binary data.") from exc
    if len(body) > MAX_RESPONSE_BYTES:
      raise HTTPException(502, "App service returned too much binary data.")
    return status, body, headers, media_type
  if media_type is not None:
    raise HTTPException(502, "App service returned an invalid media type.")
  return status, response.get("body"), headers, None


async def invoke_policy(app, owner, path: str, body: dict) -> dict:
  """Invoke one authenticated app-owned policy and normalize its error shape."""
  status, result, _headers, media_type = await invoke_service(app, owner, {
    "schema": 1,
    "method": "POST",
    "path": path,
    "query": {},
    "headers": {},
    "body": body,
    "public": False,
    "actor": {"scope": "platform"},
  })
  if media_type is not None:
    raise HTTPException(502, "App policy returned binary data.")
  if status >= 400:
    detail = result.get("detail") if isinstance(result, dict) else None
    raise HTTPException(status, detail or "App policy rejected the request.")
  if not isinstance(result, dict):
    raise HTTPException(502, "App policy returned an invalid result.")
  return result
