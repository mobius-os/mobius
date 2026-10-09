"""Bounded request/response execution for app-owned server policy.

The platform owns authentication, accepted-runtime identity, process limits,
and revocation. The app owns the request paths and domain behavior behind one
small JSON protocol. An optional response `diagnostics` object may contain
`route` (a code-authored matched route template, never the concrete path),
`error_type` (exception class name only), and `upstream_status` (HTTP integer).
These fields are local tracing metadata only, not forwarded to HTTP callers.
Never include request values, ids, exception messages, stderr, or response bodies.
This is deliberately not a framework or an import hook:
every request runs in its own process from one reviewed Python entrypoint and
receives one JSON reply. An entry that declares ``MOBIUS_PRELOAD = True`` has
its module setup run once and each request forked from it (``service_preload``);
every other entry, and any request no preloaded host can take, is spawned.
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
import weakref
from datetime import timedelta
from pathlib import Path

from fastapi import HTTPException

from app import app_python_env, auth, models, service_preload, tracing
from app.applied_app_runtime import AppliedRuntimeUnavailable, hold_runtime, runtime_root
from app.browser_access import BrowserLineage, require_live
from app.config import get_settings
from app.manifest_contract import SERVICE_REQUEST_MAX_BYTES


log = logging.getLogger(__name__)
MAX_REQUEST_BYTES = SERVICE_REQUEST_MAX_BYTES
MAX_RESPONSE_BYTES = SERVICE_REQUEST_MAX_BYTES
MAX_ERROR_BYTES = 16 * 1024
SERVICE_TIMEOUT_SECONDS = 15
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}$")
# The only headers an app service may add. A service answers on the shell
# origin, so anything else (Set-Cookie, Clear-Site-Data, NEL/Report-To,
# Service-Worker-Allowed, framing, transport) would act on the owner's whole
# session or on the platform's own response contract, and is dropped. The
# platform sets the content type from `media_type`.
_SERVICE_RESPONSE_HEADERS = frozenset({
  "cache-control", "content-disposition", "content-language", "etag",
  "last-modified", "vary",
})
# Cache-Control directives that let a shared cache store a response to an
# authorized request (RFC 9111 section 3.5), unless private/no-store also apply.
_SHARED_CACHE_DIRECTIVES = frozenset({
  "public", "s-maxage", "must-revalidate", "proxy-revalidate",
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

  Trusted helpers get "write"; an orphaned or retired delegated identity gets
  "read" defensively so app services do not mistake it for an active helper.
  """
  access = "write"
  if principal.delegation_id is not None:
    delegation = db.get(models.Delegation, principal.delegation_id)
    access = (
      "write" if delegation is not None
      and delegation.scope == "write"
      and delegation.interrupted_at is None
      else "read"
    )
  actor = {
    "scope": principal.scope,
    "app_id": principal.app_id,
    "app_slug": caller.slug if caller is not None else None,
    "delegated": principal.delegation_id is not None,
    "access": access,
  }
  if principal.browser is not None:
    actor.update(browser_grant_id=principal.browser.grant_id,
                 browser_session_id=principal.browser.session_id)
  return actor


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


def service_python_env(app, entry: Path) -> Path | None:
  """The accepted service's own Python env, None when it declares none."""
  try:
    return app_python_env.resolve_env(get_settings().data_dir, app.id, entry.parent)
  except app_python_env.PythonEnvUnavailable as exc:
    log.warning("App service %s cannot start: %s", app.slug, exc)
    raise HTTPException(503, str(exc)) from exc


def service_environment(app, owner, service: dict, *, public: bool, browser: BrowserLineage | None = None) -> dict[str, str]:
  """The environment of one invocation; its APP_TOKEN's authority follows the caller.

  A public invocation acts for an anonymous visitor, so its token has the narrow
  public-service scope (auth.create_app_token), which cannot start or drive the
  owner's agents. This bounds what the platform does on the owner's behalf, not
  file access: a service is owner-installed reviewed code.
  """
  allowed = {"PATH", "LANG", "LC_ALL", "TZ", "HOME"}
  if service.get("access", "self") != "public":
    # A service reached only through an authenticated caller may run a provider
    # CLI (Memory's recall navigator does), so it receives the same credential
    # locations as its scheduled job.
    allowed |= {"DATA_DIR", "CLAUDE_CONFIG_DIR", "CODEX_HOME"}
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
      service="public" if public else "private",
      browser=browser,
    ),
  })
  return env


_read_bounded = service_preload.read_bounded


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


def _response_headers(value, *, public: bool) -> dict[str, str]:
  if value is None:
    return {}
  if not isinstance(value, dict) or len(value) > 16:
    raise ValueError("response headers must be a bounded object")
  headers: dict[str, str] = {}
  for name, raw in value.items():
    lower = name.lower() if isinstance(name, str) else ""
    if lower not in _SERVICE_RESPONSE_HEADERS:
      continue
    if not isinstance(raw, str) or len(raw) > 4096 or "\r" in raw or "\n" in raw:
      raise ValueError("response contains an invalid header")
    if lower == "cache-control" and not public:
      directives = {part.split("=", 1)[0].strip().lower() for part in raw.split(",")}
      if directives & _SHARED_CACHE_DIRECTIVES and not directives & {"private", "no-store"}:
        # An owner-authenticated response must never be stored by a shared cache.
        continue
    headers[name] = raw
  return headers


async def _run_spawned(
  python: str, entry: Path, environment: dict[str, str], request_bytes: bytes,
  timeout_seconds: float,
) -> tuple[bytes, bytes, int]:
  """Run one request in a fresh interpreter; return (stdout, stderr, exit code)."""
  try:
    spawn = asyncio.create_task(asyncio.create_subprocess_exec(
      python,
      str(entry),
      stdin=asyncio.subprocess.PIPE,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.PIPE,
      cwd=str(entry.parent),
      env=environment,
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
  return stdout, stderr, returncode


# Invocation tasks, not a second job queue: existing subprocess cancellation owns
# process cleanup. A revoked grant cancels only its attributed in-flight calls.
_browser_calls: dict[str, set[asyncio.Task]] = {}


def _actor_browser(actor: dict) -> BrowserLineage | None:
  return BrowserLineage.of(actor.get("browser_grant_id"), actor.get("browser_session_id"))


def _validate_browser_call(owner, browser: BrowserLineage | None):
  if browser is not None:
    from app.database import SessionLocal
    with SessionLocal() as db:
      require_live(db, browser, owner.id)


def browser_grant_has_active_calls(grant_id: str) -> bool:
  """Whether attributed invocation cleanup is still outstanding."""
  return any(not task.done() for task in _browser_calls.get(grant_id, ()))


async def cancel_browser_grant_calls(grant_id: str) -> None:
  tasks = tuple(_browser_calls.get(grant_id, ()))
  for task in tasks:
    if task is not asyncio.current_task():
      task.cancel()
  await asyncio.gather(*(task for task in tasks if task is not asyncio.current_task()), return_exceptions=True)


# Only platform-authored error labels are recorded; never export HTTP detail,
# stderr, a request path, or an app response body.
_SERVICE_FAILURES = {
  "App service could not start.": "start_failed",
  "App service failed before accepting its request.": "request_not_accepted",
  "App service exceeded its execution limits.": "execution_limit",
  "App service failed.": "process_failed",
  "App service returned invalid JSON.": "invalid_json",
  "App service returned an invalid response envelope.": "invalid_envelope",
  "App service returned an invalid status.": "invalid_status",
  "App service returned an invalid binary response.": "invalid_binary_response",
  "App service returned an invalid media type.": "invalid_media_type",
  "App service returned invalid binary data.": "invalid_binary_data",
  "App service returned too much binary data.": "response_limit",
}


async def invoke_service(
  app, owner, request_envelope: dict, *,
  timeout_seconds: float = SERVICE_TIMEOUT_SECONDS,
  lane: str | None = None,
) -> tuple[int, object, dict[str, str], str | None]:
  tracing.annotate(None, {"mobius.app.slug": getattr(app, "slug", None)})
  try:
    result = await _invoke_service(
      app, owner, request_envelope, timeout_seconds=timeout_seconds, lane=lane,
    )
  except HTTPException as exc:
    if exc.status_code >= 500:
      tracing.annotate(None, {"mobius.service.error_type":
        _SERVICE_FAILURES.get(exc.detail, "service_boundary_error")
        if isinstance(exc.detail, str) else "service_boundary_error"})
    raise
  except Exception as exc:
    tracing.annotate(None, {"mobius.service.error_type": type(exc).__name__})
    raise
  return result


async def _invoke_service(
  app, owner, request_envelope: dict, *,
  timeout_seconds: float = SERVICE_TIMEOUT_SECONDS,
  lane: str | None = None,
) -> tuple[int, object, dict[str, str], str | None]:
  public = request_envelope.get("public") is True
  service = service_contract(app, access="public" if public else "self")
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
    lane = "public" if public else "private"
  slot = (
    contextlib.nullcontext() if lane == "tools"
    else _app_slots.setdefault((app.id, lane), asyncio.Semaphore(1))
  )
  # Backlog for one app must not reserve all platform execution capacity while
  # waiting for that app's serialized request. Count only executable requests.
  # Queued requests already own an accepted revision. Pin before admission so
  # pruning or a migration drain cannot overlook a request waiting to run.
  browser = _actor_browser(request_envelope.get("actor") or {})
  _validate_browser_call(owner, browser)
  grant_id = browser.grant_id if browser is not None else None
  task = asyncio.current_task()
  pin = hold_runtime(app.id)
  if grant_id is not None:
    _browser_calls.setdefault(grant_id, set()).add(task)
  try:
    async with slot, _global_slots[lane]:
      _validate_browser_call(owner, browser)
      entry = service_entry(app, service)
      python_env = service_python_env(app, entry)
      python = app_python_env.python_for(python_env)
      # The preload host and its request children inherit this PATH too.
      environment = app_python_env.activated_environment(
        service_environment(
          app, owner, service, public=public,
          browser=browser,
        ), python_env,
      )
      outcome = None
      host = service_preload.ready_host(app, python, entry, environment)
      if host is not None:
        try:
          outcome = await service_preload.run(
            host, environment, request_bytes, timeout_seconds=timeout_seconds,
            max_stdout=MAX_RESPONSE_BYTES, max_stderr=MAX_ERROR_BYTES,
          )
        except service_preload.PreloadUnavailable:
          pass
        except (TimeoutError, ValueError) as exc:
          raise HTTPException(503, "App service exceeded its execution limits.") from exc
        except OSError as exc:
          raise HTTPException(502, "App service failed before accepting its request.") from exc
      if outcome is None:
        outcome = await _run_spawned(
          python, entry, environment, request_bytes, timeout_seconds,
        )
      stdout, stderr, returncode = outcome
      if returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
        log.warning("App service %s failed: %s", app.slug, detail or "no diagnostics")
        raise HTTPException(502, "App service failed.")
  finally:
    pin.close()
    if grant_id is not None:
      calls = _browser_calls.get(grant_id)
      if calls is not None:
        calls.discard(task)
        if not calls:
          _browser_calls.pop(grant_id, None)
  try:
    response = json.loads(stdout, parse_constant=_reject_json_constant)
  except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
    raise HTTPException(502, "App service returned invalid JSON.") from exc
  if not isinstance(response, dict) or set(response) - {
    "status", "body", "body_base64", "headers", "media_type", "diagnostics",
  }:
    raise HTTPException(502, "App service returned an invalid response envelope.")
  status = response.get("status", 200)
  if isinstance(status, bool) or not isinstance(status, int) or not 200 <= status <= 599:
    raise HTTPException(502, "App service returned an invalid status.")
  # App-authored shape only. These optional diagnostics are not returned to
  # callers and cannot change the response or make an otherwise valid call fail.
  diagnostics = response.get("diagnostics")
  if isinstance(diagnostics, dict):
    route = diagnostics.get("route")
    error_type = diagnostics.get("error_type")
    upstream_status = diagnostics.get("upstream_status")
    safe = {}
    if isinstance(route, str) and len(route) <= 256 and re.fullmatch(
      r"/[A-Za-z0-9_/{:}.-]*", route,
    ):
      safe["mobius.service.route"] = route
    if status >= 500:
      if isinstance(error_type, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,127}", error_type):
        safe["mobius.service.error_type"] = error_type
      else:
        safe["mobius.service.error_type"] = "app_http_error"
      if type(upstream_status) is int and 100 <= upstream_status <= 599:
        safe["mobius.service.upstream_status"] = upstream_status
    tracing.annotate(None, safe)
  elif status >= 500:
    tracing.annotate(None, {"mobius.service.error_type": "app_http_error"})
  if status >= 500 and stderr:
    detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
    if detail:
      # A handled app failure still exits its adapter successfully, so preserve
      # its bounded diagnostics without exposing them in the HTTP response.
      log.warning("App service %s returned %d: %s", app.slug, status, detail)
  try:
    headers = _response_headers(
      response.get("headers"), public=bool(request_envelope.get("public")),
    )
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
