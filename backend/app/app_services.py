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
import contextlib
import json
import logging
import os
import re
import signal
import sys
import weakref
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException

from app import auth, models
from app.applied_app_runtime import AppliedRuntimeUnavailable, hold_runtime, runtime_root
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
_global_slots = {
  "private": asyncio.Semaphore(8),
  "public": asyncio.Semaphore(8),
  "tools": asyncio.Semaphore(8),
}
_app_slots: weakref.WeakValueDictionary[tuple[int, str], asyncio.Semaphore] = (
  weakref.WeakValueDictionary()
)


def _reject_json_constant(value: str):
  raise ValueError(f"invalid JSON constant: {value}")


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


def request_actor(db, principal, caller=None) -> dict:
  """Who is calling an app's service, as the request's `actor` states it.

  `access` is "read" for a read-only helper (or one whose delegation is gone),
  so the app can refuse to change anything on its behalf. The owner, the app
  itself, a top-level agent run, and a write helper get "write".
  """
  access = "write"
  if principal.delegation_id is not None:
    delegation = db.get(models.Delegation, principal.delegation_id)
    access = delegation.scope if delegation is not None else "read"
  return {
    "scope": principal.scope,
    "app_id": principal.app_id,
    "app_slug": caller.slug if caller is not None else None,
    "delegated": principal.delegation_id is not None,
    "access": access,
  }


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
  # The same allowlist as scheduled app jobs: a reviewed service may run a
  # provider CLI (Memory's recall navigator does) exactly as its job can.
  allowed = {
    "PATH", "LANG", "LC_ALL", "TZ", "HOME",
    "DATA_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
  }
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
      is_service=True,
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
  if not tasks:
    # Admission cancellation has not installed pipe readers yet. Drain the
    # now-stopped child as well as reaping it; wait() alone can block on a full
    # asyncio pipe buffer even after SIGKILL.
    await process.communicate()
    return
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
  app, owner, request_envelope: dict, *,
  timeout_seconds: float = SERVICE_TIMEOUT_SECONDS,
  lane: str | None = None,
) -> tuple[int, object, dict[str, str], str | None]:
  service = service_contract(
    app, access="public" if request_envelope.get("public") else "self",
  )
  try:
    request_bytes = json.dumps(
      request_envelope, ensure_ascii=False, separators=(",", ":"),
      allow_nan=False,
    ).encode("utf-8")
  except (TypeError, ValueError, RecursionError) as exc:
    raise HTTPException(400, "App service request contains invalid JSON data.") from exc
  if len(request_bytes) > MAX_REQUEST_BYTES:
    raise HTTPException(413, "App service request is too large.")

  # A service owns its persistence semantics. Serialize one app's private
  # requests so simple file-backed services do not need a platform-specific
  # lock API, and serialize its public requests on a separate lane. A private
  # federation write may synchronously cause the peer to call this instance's
  # public service (for example, to verify the sender's identity). Sharing one
  # lane for both directions deadlocks that callback behind the write waiting
  # for it. Public services still own their own file/SQLite locking where the
  # two lanes can touch the same state.
  #
  # Agent tool calls use a third lane that is not serialized per app: one
  # agent's long tool call (a Memory search can take minutes) must not stall
  # the app's own screen or another chat's call. A tool owns its concurrency,
  # as a public service already must.
  if lane is None:
    lane = "public" if request_envelope.get("public") else "private"
  slot = (
    contextlib.nullcontext() if lane == "tools"
    else _app_slots.setdefault((app.id, lane), asyncio.Semaphore(1))
  )
  # Backlog for one app must not reserve all platform execution capacity while
  # waiting for that app's serialized request. Count only executable requests.
  # Queued requests already own an accepted revision. Pin before admission so
  # pruning or a migration drain cannot overlook a request waiting to run.
  pin = hold_runtime(app.id)
  try:
    async with slot, _global_slots[lane]:
      entry = service_entry(app, service)
      try:
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
          sys.executable,
          str(entry),
          stdin=asyncio.subprocess.PIPE,
          stdout=asyncio.subprocess.PIPE,
          stderr=asyncio.subprocess.PIPE,
          cwd=str(entry.parent),
          env=service_environment(app, owner),
          start_new_session=True,
        ))
        try:
          process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
          # Admission can be cancelled after the OS child exists but before
          # asyncio returns its handle. Finish recovery before releasing the
          # runtime pin, even if shutdown cancels this request again.

          async def recover_spawn() -> None:
            try:
              process = await asyncio.shield(spawn)
            except OSError:
              pass
            else:
              await _stop_process(process)

          recovery = asyncio.create_task(recover_spawn())
          while not recovery.done():
            try:
              await asyncio.shield(recovery)
            except asyncio.CancelledError:
              continue
          raise
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
          timeout=timeout_seconds,
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
  finally:
    pin.close()
  try:
    response = json.loads(stdout, parse_constant=_reject_json_constant)
  except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
    raise HTTPException(502, "App service returned invalid JSON.") from exc
  if not isinstance(response, dict) or set(response) - {
    "status", "body", "body_base64", "headers", "media_type",
  }:
    raise HTTPException(502, "App service returned an invalid response envelope.")
  status = response.get("status", 200)
  if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
    raise HTTPException(502, "App service returned an invalid status.")
  if status >= 500 and stderr:
    detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
    if detail:
      # A handled app failure still exits its adapter successfully, so preserve
      # its bounded diagnostics without exposing them in the HTTP response.
      log.warning("App service %s returned %d: %s", app.slug, status, detail)
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
