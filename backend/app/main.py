"""FastAPI application factory.

In production the single container serves both the API and the frontend
static files.  API routes are registered first; the frontend SPA is
mounted last as a catch-all so that client-side routing works.
"""

import ipaddress
import json
import logging
import mimetypes
import os
import re
import shlex
import time
from contextlib import asynccontextmanager
from datetime import timezone
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

# Do this before importing FastAPI, SQLAlchemy, or any app module that may
# create worker threads. See allocator.limit_glibc_arenas for the observed
# per-thread 64 MiB arena failure mode on the production host.
from app.allocator import limit_glibc_arenas

limit_glibc_arenas()

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import (
  Base,
  SessionLocal,
  engine,
  reset_database_request_label,
  set_database_request_label,
)
from app.schema_migrations import mapped_schema_gaps, run_migrations
from app.http_caching import strip_range
from app.frontend_assets import (
  baked_frontend_dir,
  live_frontend_dir,
  resolve_frontend_dir,
)
from app.memory_observability import record_memory_checkpoint
from app.response_policy import (
  CHAT_EMBED_CSP,
  PUBLISHED_SITE_CSP,
  absolute_csp_origin,
  app_frame_csp,
  shell_csp,
  static_embed_csp,
)
from app.storage_io import atomic_write
from app import activity, models
# providers and push are on the agent's write surface; deferred into
# lifespan with try/except so a SyntaxError in either doesn't prevent
# uvicorn boot. See the
# wrapped imports in lifespan() below.
from app.routes import (
  admin_router, apps_router, auth_router,
  chat_embed_router, chat_logs_router, chat_router, chats_router, chats_stream_router,
  secure_inputs_router,
  connectors_router, connectors_public_router,
  chat_waits_router,
  debug_router, delegations_router, fs_router, goal_plans_router, github_router, media_router,
  identity_router,
  local_services_router, notifications_router, notify_router, proxy_router, push_router,
  screen_control_router,
  public_apps_router,
  secrets_router, self_reminders_router, settings_router, skills_router,
  client_error_router, client_signal_router, standalone_router, storage_router,
  theme_router, uploads_router, platform_router, community_router,
  contribution_relay_router,
  published_router,
  connect_router,
  projects_router,
)

_BOOT_ID = os.environ.get("MOBIUS_BOOT_ID") or f"{os.getpid()}-{time.time_ns()}"
# One boot verdict feeds startup ownership, middleware, and every health probe.
# These stay fixed until restart: Recovery may repair the database externally,
# but only a clean boot can coherently start the skipped database owners.
_DATABASE_BOOT_RESULT = None


def _set_database_boot_state(result) -> None:
  global _DATABASE_BOOT_RESULT
  _DATABASE_BOOT_RESULT = result


def _database_degraded_payload() -> dict | None:
  result = _DATABASE_BOOT_RESULT
  if result is None or result.serviceable:
    return None
  if result.failure_reason:
    return {"reason": result.failure_reason}
  if result.schema_gaps:
    return {
      "reason": "schema_mismatch",
      "schema_gaps": list(result.schema_gaps),
    }
  return None


def _database_init_error_is_transient(exc: OperationalError) -> bool:
  """Retry only connection loss or SQLite lock contention at boot.

  Deterministic migration/DDL errors are also ``OperationalError`` instances.
  Retrying those ten times only delays the same degraded verdict and obscures
  the first useful traceback. SQLite lock errors and invalidated connections
  can genuinely clear without changing the release, so retain the bounded
  retry for those cases.
  """
  if exc.connection_invalidated:
    return True
  if engine.dialect.name != "sqlite":
    return False
  detail = str(exc.orig or exc).lower()
  return "locked" in detail or "busy" in detail


def _install_pm_commit_launcher(source: Path, target: Path) -> bool:
  """Point the stable command path at the helper in the served checkout."""
  if not source.is_file():
    raise FileNotFoundError(source)
  launcher = (
    f"#!/bin/sh\nexec {shlex.quote(str(source))} \"$@\"\n"
  ).encode()
  try:
    if target.read_bytes() == launcher and target.stat().st_mode & 0o111:
      return False
  except FileNotFoundError:
    pass
  atomic_write(target, launcher)
  target.chmod(0o755)
  return True


def _init_db():
  """Create missing tables, then migrate existing ones, with retries.

  Creating tables first lets a migration move legacy data into a newly
  introduced table before retiring its old columns. ``create_all`` never
  mutates existing tables, so column upgrades remain owned by
  ``run_migrations``.
  """
  from app.startup import DatabaseBootResult

  for attempt in range(10):
    try:
      Base.metadata.create_all(bind=engine)
      run_migrations(engine)
      gaps = mapped_schema_gaps(engine)
      if gaps:
        # A mapped column with no migration fails at first query, not at
        # boot. Surface it loudly here and through /api/health(+/strict)
        # instead of letting turns fail one by one.
        print(
          "CRITICAL: database is missing ORM-declared schema: "
          + ", ".join(gaps)
        )
      return DatabaseBootResult(schema_gaps=tuple(gaps))
    except OperationalError as e:
      if attempt < 9 and _database_init_error_is_transient(e):
        delay = min(2 ** attempt, 10)
        print(f"DB init retry {attempt + 1}/10 in {delay}s: {e}")
        time.sleep(delay)
      else:
        raise


def _assert_provider_defaults(provider_names) -> None:
  """Validate SQLAlchemy provider defaults against the registry.

  `provider_names` is passed in instead of imported at module scope
  so a broken providers.py doesn't crash main.py at import time.
  """
  owner_default = models.Owner.provider.default.arg
  chat_default = models.Chat.provider.default.arg
  assert owner_default in provider_names, (
    "models.Owner.provider default must be in providers.PROVIDER_NAMES"
  )
  assert chat_default in provider_names, (
    "models.Chat.provider default must be in providers.PROVIDER_NAMES"
  )


@asynccontextmanager
async def lifespan(app):
  _log = logging.getLogger(__name__)
  record_memory_checkpoint("lifespan_start")
  from app.startup import (
    StartupContext,
    run_startup_plan,
  )
  startup_context = StartupContext(
    app=app,
    settings=settings,
    boot_id=_BOOT_ID,
    init_db=_init_db,
    install_pm_commit_launcher=_install_pm_commit_launcher,
    assert_provider_defaults=_assert_provider_defaults,
    logger=_log,
  )
  database_boot = await run_startup_plan(startup_context)
  _set_database_boot_state(database_boot)
  from app.runtime_supervisors import RuntimeSupervisors
  supervisors = RuntimeSupervisors(
    settings=settings,
    logger=_log,
    restart_authorization=startup_context.restart_authorization,
    restart_fallback_chats=startup_context.restart_fallback_chats,
  )
  await supervisors.start_process_services()
  record_memory_checkpoint("startup_frontend_watcher_started")
  if database_boot.serviceable:
    await supervisors.start_database_services()
    record_memory_checkpoint("startup_ready")
  try:
    yield
  finally:
    record_memory_checkpoint("shutdown_begin")
    try:
      from app.public_app_transport import close_public_fetch_clients
      await close_public_fetch_clients()
    except Exception as exc:
      _log.error("public fetch client shutdown failed: %s", exc, exc_info=True)
    # Preserve the final partial request-error windows across graceful restarts.
    # This is one bounded batch append, not one write per response.
    activity.flush_request_errors()
    # Supervisors stop before the persistence actor they monitor.
    await supervisors.stop()
    # Drain + join the chat-writer actor so any in-flight persistence
    # completes before the process exits. Wrapped: a stop failure must
    # not mask the rest of shutdown.
    try:
      from app.chat_writer import stop_writer
      stop_writer()
    except Exception as exc:
      _log.error("chat writer stop failed: %s", exc, exc_info=True)

settings = get_settings()

def _real_peer_address(request: Request) -> str:
  """Rate-limit key: actual TCP peer address, never X-Forwarded-For.

  Port 8000 is only exposed inside the Docker network (not published to the
  host), so the only peer that can reach it is Caddy. Trusting
  X-Forwarded-For would let any client that injects that header bypass
  per-IP limits; the real peer address is simpler and correct.
  """
  return request.client.host if request.client else "127.0.0.1"


limiter = Limiter(
  key_func=_real_peer_address, default_limits=["120/minute"]
)

app = FastAPI(
  title="Möbius",
  description="Self-hosted AI agent platform.",
  version="0.1.0",
  lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Global request-body backstop. Endpoints that read raw bodies stream-cap
# themselves (storage PUT 50 MB, icon 12 MB via storage_io.read_capped_body),
# but FastAPI buffers the WHOLE body for Pydantic-parsed endpoints (e.g. a
# create with a huge jsx_source) before validation — an unbounded body there
# could OOM the memory-tight host (Codex review round-9 #4, round-10 #5). The
# cap sits ABOVE every legitimate route limit (storage 50 MB, uploads 20 MB) so
# it only ever stops abuse.
_MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024


class _BodySizeLimitMiddleware:
  """ASGI middleware that bounds the request body — including chunked bodies
  with no Content-Length.

  A declared Content-Length over the cap is rejected with 413 before the app
  runs. Otherwise the body stream is wrapped with a running byte counter; once
  it crosses the cap we stop feeding the app and signal `http.disconnect`, so
  the app aborts (a Pydantic endpoint sees a truncated body and 422s) rather
  than buffering an unbounded body into memory. Pure ASGI (not
  BaseHTTPMiddleware) so it never itself buffers the body.
  """

  def __init__(self, app, max_bytes: int):
    self.app = app
    self.max_bytes = max_bytes

  async def __call__(self, scope, receive, send):
    if scope["type"] != "http":
      return await self.app(scope, receive, send)
    for name, value in scope.get("headers") or []:
      if name == b"content-length":
        try:
          if int(value) > self.max_bytes:
            return await self._reject(send)
        except ValueError:
          pass
        break
    received = 0
    disconnected = False

    async def limited_receive():
      nonlocal received, disconnected
      if disconnected:
        return {"type": "http.disconnect"}
      message = await receive()
      if message["type"] == "http.request":
        received += len(message.get("body", b""))
        if received > self.max_bytes:
          disconnected = True
          return {"type": "http.disconnect"}
      return message

    return await self.app(scope, limited_receive, send)

  async def _reject(self, send):
    await send({
      "type": "http.response.start",
      "status": 413,
      "headers": [(b"content-type", b"application/json")],
    })
    await send({
      "type": "http.response.body",
      "body": b'{"detail":"Request body too large."}',
    })


# Standard security headers are origin-owned so Railway and the bundled Caddy
# expose one contract. Camera/location remain unavailable to opaque app frames,
# while ``self`` leaves room for a future shell-owned capability provider after
# the owner grants browser permission.
_SECURITY_HEADERS = [
  (b"x-content-type-options", b"nosniff"),
  (b"x-frame-options", b"SAMEORIGIN"),
  (b"referrer-policy", b"strict-origin-when-cross-origin"),
  (b"permissions-policy", b"camera=(self), geolocation=(self)"),
  (b"strict-transport-security",
   b"max-age=31536000; includeSubDomains; preload"),
]
_SECURITY_HEADER_NAMES = frozenset(name for name, _ in _SECURITY_HEADERS)
_X_FRAME_OPTIONS = b"x-frame-options"
_CONTENT_SECURITY_POLICY = b"content-security-policy"
_OPAQUE_STATIC_EMBED_PREFIX = "/app-embeds/by-id/"
_PUBLISHED_SITE_PREFIX = "/sites/"
_APP_FRAME_PATH = re.compile(r"^/api/apps/[^/]+/frame$")
_ARTIFACT_OUTPUT_PATH = re.compile(
  r"^/api/projects/[^/]+/artifacts/[^/]+/output/"
)

# This isolation boundary must always be enforced, never Report-Only: browsers
# ignore the CSP sandbox directive in a Report-Only policy. The sandbox omits
# allow-same-origin, so the document's active origin is opaque and CSP 'self'
# matches none of its own relative subresources on WebKit. Name the configured
# absolute origin explicitly in every fetch directive. This does not weaken the
# credential boundary: packaged code already executes in the opaque document,
# and it still cannot reach the shell's localStorage, cookies, or owner token.
_STATIC_EMBED_CSP = static_embed_csp(settings.frontend_origin)
_SHELL_CSP = shell_csp(os.environ.get("MOBIUS_SERVICE_GATEWAY_ORIGIN", ""))
_SERVICE_GATEWAY_ORIGIN = os.environ.get("MOBIUS_SERVICE_GATEWAY_ORIGIN", "")
_BROWSER_API_ORIGIN = os.environ.get("API_BASE_URL", "")
_APP_FRAME_CSP = app_frame_csp(
  settings.frontend_origin,
  _SERVICE_GATEWAY_ORIGIN,
  # Only an explicitly configured browser-reachable API origin belongs in a
  # frame policy. settings.api_base_url defaults to backend-local localhost
  # for agents and jobs, which must not become the viewer's localhost.
  _BROWSER_API_ORIGIN,
)

def _loopback_delivery_origin(scope) -> str | None:
  """Return the exact loopback origin serving a loopback request, if any."""
  headers = dict(scope.get("headers") or ())
  try:
    client = scope.get("client")
    peer = client[0] if isinstance(client, (list, tuple)) and client else None
    peer_is_loopback = (
      peer == "localhost"
      or (isinstance(peer, str) and ipaddress.ip_address(peer).is_loopback)
    )
    if not peer_is_loopback:
      return None
    authority = headers.get(b"host", b"").decode("ascii")
    scheme = str(scope.get("scheme") or "http")
    origin = absolute_csp_origin(f"{scheme}://{authority}")
    if origin is None:
      return None
    hostname = urlparse(origin).hostname
    is_loopback = (
      hostname == "localhost"
      or (hostname is not None and ipaddress.ip_address(hostname).is_loopback)
    )
  except (UnicodeDecodeError, ValueError, TypeError):
    return None
  if not is_loopback:
    return None
  return origin


def _app_frame_csp_for_scope(scope) -> str:
  """Let the loopback test harness exercise the real opaque app frame."""
  delivery_origin = _loopback_delivery_origin(scope)
  if delivery_origin is None:
    return _APP_FRAME_CSP
  return app_frame_csp(
    settings.frontend_origin,
    _SERVICE_GATEWAY_ORIGIN,
    _BROWSER_API_ORIGIN,
    delivery_origin,
  )


# Published sites (`/sites/<token>/`) are public snapshots of the owner's own
# agent-authored artifacts and Web Studio builds. The `sandbox` directive
# (WITHOUT allow-same-origin) forces the top-level document into an opaque
# origin so its JS cannot read the shell origin's localStorage/cookies/JWT —
# the credential boundary this closes. Unlike a packaged embed we do NOT lock
# resource loading to `'self'`: `/sites/` also serves multi-file Web Studio
# sites that may legitimately pull external assets, and the opaque-origin
# sandbox is the actual isolation, not resource confinement. We keep the
# sandbox capability set minimal (no modals/downloads/pointer-lock, never
# allow-popups-to-escape-sandbox) and add cheap defense-in-depth
# (object-src/base-uri/frame-ancestors). Residual accepted: a compromised
# external script a published page chose to include can read that page's own
# share token + public artifact data, but never the shell origin or the owner
# JWT. Must be enforcing, never Report-Only. X-Frame-Options SAMEORIGIN is
# KEPT (published pages open top-level; no cross-site framing need).
_PUBLISHED_SITE_CSP = PUBLISHED_SITE_CSP


# Built website artifacts render in sandboxed iframes without
# ``allow-same-origin``, so their documents cannot reach the shell's owner
# credentials. Keep the build-output namespace on the Projects isolation
# policy instead of inheriting the broader shell policy.
_ARTIFACT_OUTPUT_CSP = (
  "default-src 'self'; img-src 'self' data:; font-src 'self' data:; "
  "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
  "frame-ancestors 'self'"
)


def _is_public_service_surface(scope) -> bool:
  """Whether the gateway host may frame this registered service route."""
  path = scope.get("path") or ""
  if not path.startswith("/services/"):
    return False
  try:
    from app.routes.local_services import is_public_service_surface_request
    return is_public_service_surface_request(scope)
  except Exception:
    return False


class _SecurityHeadersMiddleware:
  """Authoritatively sets the platform security headers on every response. Pure
  ASGI so it never buffers a streaming body. It strips any same-named header a
  route may have set first and replaces it with the platform value, so no route
  can weaken the HSTS/MIME/etc. wall. Document policies are selected by exact
  origin-owned namespaces. The shared service gateway is the sole exception:
  its host adapter supplies a topology-specific frame policy."""

  def __init__(self, app):
    self.app = app

  async def __call__(self, scope, receive, send):
    if scope["type"] != "http":
      return await self.app(scope, receive, send)

    # Frameability and sandboxing are one policy for these namespaces; keep the
    # decisions adjacent so no response can receive only part of the boundary.
    path = scope.get("path") or ""
    opaque_static_embed = path.startswith(_OPAQUE_STATIC_EMBED_PREFIX)
    published_site = path.startswith(_PUBLISHED_SITE_PREFIX)
    chat_embed = path == "/shell/embed/chat"
    app_frame = bool(_APP_FRAME_PATH.fullmatch(path))
    artifact_output = bool(_ARTIFACT_OUTPUT_PATH.match(path))
    service_surface = _is_public_service_surface(scope)
    response_headers = list(_SECURITY_HEADERS)
    replaced_header_names = _SECURITY_HEADER_NAMES
    if opaque_static_embed or chat_embed or service_surface:
      response_headers = [
        (name, value) for name, value in _SECURITY_HEADERS
        if name != _X_FRAME_OPTIONS
      ]
    if not service_surface:
      if opaque_static_embed:
        csp = _STATIC_EMBED_CSP
      elif published_site:
        csp = _PUBLISHED_SITE_CSP
      elif chat_embed:
        csp = CHAT_EMBED_CSP
      elif app_frame:
        csp = _app_frame_csp_for_scope(scope)
      elif artifact_output:
        csp = _ARTIFACT_OUTPUT_CSP
      else:
        csp = _SHELL_CSP
      response_headers.append((
        _CONTENT_SECURITY_POLICY,
        csp.encode("ascii"),
      ))
      replaced_header_names = replaced_header_names | {
        _CONTENT_SECURITY_POLICY
      }

    response_started = False

    async def _send(message):
      nonlocal response_started
      if message["type"] == "http.response.start":
        response_started = True
        headers = [
          (k, v) for k, v in message.get("headers", [])
          if k.lower() not in replaced_header_names
        ]
        headers.extend(response_headers)
        message["headers"] = headers
      await send(message)

    try:
      return await self.app(scope, receive, _send)
    except Exception:
      # Starlette's unhandled-error response is outside user middleware. Send
      # one through this wrapper before re-raising so direct and proxied generic
      # errors cannot diverge on the response policy.
      if not response_started:
        response = Response(
          "Internal Server Error",
          status_code=500,
          media_type="text/plain",
        )
        await response(scope, receive, _send)
      raise


class _DatabaseRequestContextMiddleware:
  """Attributes connection checkout time to the owning HTTP request."""

  def __init__(self, app):
    self.app = app

  async def __call__(self, scope, receive, send):
    if scope["type"] != "http":
      return await self.app(scope, receive, send)
    label = f'{scope.get("method", "?")} {scope.get("path", "/")}'
    token = set_database_request_label(label)
    try:
      return await self.app(scope, receive, send)
    finally:
      reset_database_request_label(token)


class _RequestErrorTelemetryMiddleware:
  """Aggregate failed responses by matched route without retaining raw URLs.

  Successful requests do no logging or aggregation. For failures, the activity
  module keeps bounded in-memory minute counters and writes compact summaries,
  so a retry loop remains observable without amplifying its CPU or disk cost.
  FastAPI leaves the matched route template and path params in the ASGI scope;
  those templates contain no user paths or query values.
  """

  def __init__(self, app):
    self.app = app

  async def __call__(self, scope, receive, send):
    if scope["type"] != "http":
      return await self.app(scope, receive, send)
    status = None

    async def _send(message):
      nonlocal status
      if message["type"] == "http.response.start":
        status = int(message.get("status", 500))
      await send(message)

    try:
      return await self.app(scope, receive, _send)
    except Exception:
      status = status or 500
      raise
    finally:
      if status is not None and status >= 400:
        matched = scope.get("route")
        route = getattr(matched, "path", None) or "<unmatched>"
        raw_app_id = (scope.get("path_params") or {}).get("app_id")
        try:
          app_id = int(raw_app_id) if raw_app_id is not None else None
        except (TypeError, ValueError):
          app_id = None
        activity.record_request_error(
          scope.get("method", "?"), route, status, app_id,
        )


class _ServiceSurfaceHostMiddleware:
  """Prevent the service gateway host from becoming another Möbius origin."""

  def __init__(self, app):
    self.app = app

  async def __call__(self, scope, receive, send):
    if scope["type"] == "http":
      from app.routes.local_services import service_surface_host_allows_path
      if not service_surface_host_allows_path(scope):
        await send({
          "type": "http.response.start",
          "status": 404,
          "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        })
        await send({"type": "http.response.body", "body": b"Not found"})
        return
    return await self.app(scope, receive, send)


_DATABASE_DEGRADED_API_PATHS = frozenset({
  "/api/health",
  "/api/health/strict",
  "/api/ready",
  "/api/version",
  "/api/browser-bootstrap",
  "/api/admin/restart",
})


class _DatabaseServiceabilityMiddleware:
  """Reject ordinary API work after an incompatible database boot.

  Readiness keeps a new deployment out of rotation, but an already-running
  reverse proxy can still reach an unhealthy replacement directly. Centralize
  the degraded boundary here so requests receive one deterministic 503 instead
  of executing arbitrary ORM queries and surfacing whichever missing column
  they happen to touch first. Static shell assets and the bounded diagnostic /
  restart endpoints remain available; Recovery repairs the database externally
  and a normal restart performs the skipped startup phase.
  """

  def __init__(self, app):
    self.app = app

  async def __call__(self, scope, receive, send):
    path = scope.get("path", "")
    degraded = _database_degraded_payload()
    if (
      scope["type"] == "http"
      and degraded
      and path.startswith("/api/")
      and path not in _DATABASE_DEGRADED_API_PATHS
    ):
      body = json.dumps({
        "detail": "database is not serviceable; use Recovery, then restart",
        **degraded,
      }, separators=(",", ":")).encode()
      await send({
        "type": "http.response.start",
        "status": 503,
        "headers": [
          (b"content-type", b"application/json"),
          (b"cache-control", b"no-store"),
          (b"content-length", str(len(body)).encode()),
        ],
      })
      await send({"type": "http.response.body", "body": body})
      return
    return await self.app(scope, receive, send)


app.add_middleware(_BodySizeLimitMiddleware, max_bytes=_MAX_REQUEST_BODY_BYTES)

app.add_middleware(
  CORSMiddleware,
  # "null" is the origin of sandboxed iframes (allow-same-origin absent).
  # All sensitive endpoints are independently protected by JWT.
  allow_origins=[settings.frontend_origin, "null"],
  allow_credentials=False,
  allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
  # The app runtime uses X-Mobius-Version to opt into ETag reads, then
  # If-Match / If-None-Match for conflict-safe writes. Sandboxed app frames
  # have the opaque `null` origin, so Chromium preflights these non-simple
  # headers; omitting them here makes the runtime's versioned request fail
  # before it ever reaches the authenticated storage route.
  allow_headers=[
    "Authorization",
    "Content-Type",
    "X-Mobius-Embed-Instance",
    "X-Mobius-Stream-Snapshot",
    "X-Mobius-Version",
    "If-Match",
    "If-None-Match",
    # Connection mutations echo the row generation in this header. The
    # Connections mini-app runs in a sandboxed (opaque-origin) frame, so
    # omitting it here fails every toggle/re-check/remove at preflight
    # ("Failed to fetch") while same-origin shell calls sail through.
    "X-Mobius-Connector-Generation",
  ],
  # ETag is not CORS-safelisted. Expose it so getWithVersion() can actually
  # return the version token that the storage route intentionally emits.
  expose_headers=["ETag"],
)


class _OpaqueOriginCorsMiddleware:
  """Answers a sandboxed app frame with `*` rather than the literal `null`.

  A frame without `allow-same-origin` sends `Origin: null`, and CORSMiddleware
  echoes the matched value back, so the response says
  `Access-Control-Allow-Origin: null`. Chromium treats that as a match; WebKit
  does not, and blocks the response before the page sees it. The request never
  reaches this server, so the failure looks like the network is down — on iOS
  every direct API call from an app frame failed this way, while the same app's
  storage worked because that path goes through the shell instead.

  `*` is the same header the opaque-frame asset routes already emit, and it is
  legal here only because `allow_credentials=False`: no cookie or other ambient
  credential rides along, so this widens nothing an attacker could use without
  first holding a bearer token that lives in localStorage, out of reach of any
  other origin. State-changing routes keep their own `reject_cross_site` guard.

  Placed outside CORSMiddleware (added later == outer) so it can rewrite that
  middleware's header on both the preflight and the real response.
  """

  def __init__(self, app):
    self.app = app

  async def __call__(self, scope, receive, send):
    if scope["type"] != "http":
      return await self.app(scope, receive, send)
    origin = None
    for key, value in scope.get("headers") or ():
      if key == b"origin":
        origin = value
        break
    if origin != b"null":
      return await self.app(scope, receive, send)

    async def send_wrapper(message):
      if message["type"] == "http.response.start":
        headers = [
          (k, b"*" if k.lower() == b"access-control-allow-origin" else v)
          for k, v in message.get("headers") or ()
        ]
        message = {**message, "headers": headers}
      await send(message)

    return await self.app(scope, receive, send_wrapper)


app.add_middleware(_OpaqueOriginCorsMiddleware)

# Security remains outside CORS and request-size enforcement so its headers
# land on those generated responses. Request context is outermost so every
# request receives one diagnostic label before middleware can touch the DB.
# A managed deployment may expose the application directly on more than one
# hostname without the bundled Caddyfile. Reserve each configured service host
# before routing so it can never serve the shell, APIs, or another
# service prefix.
app.add_middleware(_ServiceSurfaceHostMiddleware)
app.add_middleware(_DatabaseServiceabilityMiddleware)
app.add_middleware(_SecurityHeadersMiddleware)
app.add_middleware(_DatabaseRequestContextMiddleware)
app.add_middleware(_RequestErrorTelemetryMiddleware)

# -- API routes --------------------------------------------------------
app.include_router(auth_router)
app.include_router(apps_router)
app.include_router(storage_router)
app.include_router(fs_router)
app.include_router(projects_router)
app.include_router(chat_router)
app.include_router(chat_embed_router)
app.include_router(chats_router)
app.include_router(chats_stream_router)
app.include_router(secure_inputs_router)
app.include_router(delegations_router)
app.include_router(chat_waits_router)
app.include_router(goal_plans_router)
app.include_router(chat_logs_router)
app.include_router(connectors_router)
app.include_router(connectors_public_router)
# App-attributed chat contract (design §1) — a SECOND router defined in
# routes/chats.py under /api/app-chats, so it's imported directly rather
# than via routes/__init__'s `_load` (which only returns `.router`).
# Guarded: a broken chats.py already degraded chats_router to a stub
# above, and shouldn't take the whole app down here either.
try:
  from app.routes.chats import app_chat_router  # noqa: E402
  app.include_router(app_chat_router)
except Exception as _exc:  # pragma: no cover - defensive boot guard
  logging.getLogger(__name__).error(
    "app_chat_router not mounted: %s", _exc, exc_info=True,
  )
app.include_router(notify_router)
app.include_router(screen_control_router)
app.include_router(proxy_router)
app.include_router(public_apps_router)
app.include_router(local_services_router)
app.include_router(connect_router)
app.include_router(client_error_router)
app.include_router(client_signal_router)
app.include_router(community_router)
app.include_router(contribution_relay_router)
app.include_router(settings_router)
app.include_router(platform_router)
app.include_router(uploads_router)
app.include_router(media_router)
app.include_router(secrets_router)
app.include_router(github_router)
app.include_router(identity_router)
app.include_router(push_router)
app.include_router(notifications_router)
app.include_router(debug_router)
app.include_router(theme_router)
app.include_router(admin_router)
app.include_router(self_reminders_router)
app.include_router(skills_router)
# Standalone PWA surface at /apps/<slug>/{,manifest.json,icon-N.png}.
# Registered AFTER the API routers but BEFORE the SPA catch-all
# (which mounts conditionally below at /{path:path}) so its explicit
# routes win.
app.include_router(standalone_router)
app.include_router(published_router)  # /sites/<token>/ — before the SPA catch-all


@app.get("/api/health")
def health(response: Response):
  """Returns a simple health check response.

  `Cache-Control: no-store` so the client's reachability probe
  (`useOnlineStatus`) can never be answered from any HTTP cache or heuristic
  freshness — the probe must reflect a real network round-trip. The probe
  already sends `cache: 'no-store'`, but the response carrying the directive
  too is belt-and-suspenders against an intermediary or a stale-200 path
  (a suspected contributor to the Android offline-probe-returns-true anomaly).
  `boot_id` is a per-worker marker the Settings restart flow uses to avoid
  reloading while the old process is still briefly answering before SIGTERM.
  """
  response.headers["Cache-Control"] = "no-store"
  degraded = _database_degraded_payload()
  payload = {
    "status": degraded["reason"] if degraded else "ok",
    "target": "mobius",
    "mode": "degraded" if degraded else "normal",
    "build_sha": settings.build_sha,
    "boot_id": _BOOT_ID,
    # The managed account service uses this baked-image witness only for the
    # one-time Railway bootstrap. Served source can be newer than the running
    # image, so build_sha alone cannot prove the root cutover supervisor exists.
    "container_replacement_handoff": None,
  }
  from app.deployment_control import managed_cutover_ready
  if managed_cutover_ready():
    payload["container_replacement_handoff"] = "external-cutover-v1"
  if degraded:
    # Still HTTP 200: database failure must never masquerade as device offline.
    # The strict and readiness variants below carry the 5xx service verdict.
    payload.update(degraded)
  return payload


@app.get("/api/health/strict")
def health_strict(response: Response):
  """Database-focused serviceability probe retained for diagnostics.

  Distinct from `/api/health` (reachability — must stay 200 whenever the
  process answers, or the shell would flip devices to offline UI): this
  variant fails when database initialization fails or mapped schema is absent.
  Deployment healthchecks use `/api/ready`, which includes this database
  contract plus the chat-persistence writer contract.
  """
  response.headers["Cache-Control"] = "no-store"
  degraded = _database_degraded_payload()
  if degraded:
    response.status_code = 503
    return {"status": degraded["reason"], **degraded}
  return {"status": "ok", "boot_id": _BOOT_ID}


@app.get(
  "/api/browser-bootstrap",
  response_class=HTMLResponse,
  include_in_schema=False,
)
def browser_bootstrap():
  """Stable same-origin document for authenticated browser automation setup."""
  return HTMLResponse(
    "<!doctype html><meta charset=\"utf-8\">"
    "<title>Möbius browser bootstrap</title>",
    headers={"Cache-Control": "no-store"},
  )


@app.get("/api/ready")
def ready(response: Response):
  """Readiness probe: 200 only when chats can actually be served.

  Distinct from `/api/health` (reachability — the process is answering HTTP),
  this route also requires a successfully initialized database with every
  mapped table and column, plus a usable
  single-writer chat-persistence actor. A deploy must not green while a mapped
  column is absent or every chat write will fail, even though the process can
  still answer ordinary HTTP requests.

  After the schema gate, `writer_readiness()` owns the writer predicate: the
  writer singleton exists, its worker thread is alive, and the actor is
  neither fatal nor stopping. The route only maps the verdict to a status
  code and surfaces the reason. Startup ordering is fine — the lifespan
  runs `start_writer()` before uvicorn serves, so there is no cold-start
  window where this false-fails.
  """
  response.headers["Cache-Control"] = "no-store"
  degraded = _database_degraded_payload()
  if degraded:
    response.status_code = 503
    return {
      "ready": False,
      **degraded,
    }
  from app.chat_writer import writer_readiness
  is_ready, reason = writer_readiness()
  if is_ready:
    return {"ready": True}
  response.status_code = 503
  return {"ready": False, "reason": reason}


def _served_platform_identity(data_dir: str) -> dict:
  """The ACTUALLY-SERVED backend identity, distinct from the image ``build_sha``.

  The served backend is normally ``/data/platform/app``, which persists across
  image deploys. On a broken platform tree, entrypoint falls back to the baked
  floor. The entrypoint writes ``/tmp/serving-source`` (``platform``|``baked``)
  and ``/tmp/serving-sha`` at boot so this route reports the tree actually
  selected for uvicorn. Never raises — every field degrades to
  ``unknown``/``None``.
  """
  import os
  import subprocess

  out = {"serving_source": "unknown", "served_sha": None, "platform_sha": None,
         "platform_dirty": None, "baked_sha": None}
  try:
    sentinel = Path(
      os.environ.get("MOBIUS_SERVING_SOURCE_FILE", "/tmp/serving-source")
    ).read_text(encoding="utf-8").strip()
    if sentinel:
      out["serving_source"] = sentinel
  except Exception:  # incl. UnicodeError, which is not an OSError — never raise
    pass
  try:
    served_sha = Path(
      os.environ.get("MOBIUS_SERVING_SHA_FILE", "/tmp/serving-sha")
    ).read_text(encoding="utf-8").strip()
    out["served_sha"] = served_sha or None
  except Exception:
    pass
  repo = Path(data_dir) / "platform"
  try:
    out["baked_sha"] = (repo / ".baked-sha").read_text(encoding="utf-8").strip() or None
  except Exception:
    pass
  if out["serving_source"] == "platform" and (repo / ".git").exists():
    out["platform_sha"] = out["served_sha"]
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(repo.parent)}

    def _git(*args):
      return subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True, timeout=5, env=env)

    try:
      if not out["platform_sha"]:
        head = _git("rev-parse", "HEAD")
        if head.returncode == 0:
          out["platform_sha"] = head.stdout.strip() or None
      # dirty filters .baked-sha churn + untracked dotfiles, mirroring step-3b.
      st = _git("-c", "core.fileMode=false", "status", "--porcelain")
      if st.returncode == 0:
        dirty = [ln for ln in st.stdout.splitlines()
                 if ln.strip() and not ln.rstrip().endswith(".baked-sha")
                 and not ln.startswith("?? .")]
        out["platform_dirty"] = bool(dirty)
    except Exception:
      pass
  return out


def _served_frontend_identity() -> dict:
  """Identity of the frontend bundle ACTUALLY being served.

  The whole-repo platform serves the per-request-resolved static dir —
  ``/data/platform/frontend/dist`` when it is a complete build, else the baked
  ``/app/static`` floor. Vite injects a content-hashed asset name into
  ``index.html``, so hashing the served ``index.html`` yields an identity that
  changes on every rebuild the watcher swaps in: the frontend analogue of
  ``served_sha``. Resolving per request (not a boot-time snapshot) means a dist
  that appears after boot is reflected here without a restart. ``frontend_source``
  says which tree is live. Never raises.
  """
  import hashlib

  static_dir = _resolve_static_dir()
  out = {"served_frontend": None,
         "frontend_source": "baked" if static_dir == _baked_dir else "platform"}
  try:
    html = (static_dir / "index.html").read_bytes()
    out["served_frontend"] = hashlib.sha256(html).hexdigest()[:16]
  except Exception:  # missing/unreadable dist — degrade, never raise
    pass
  return out


@app.get("/api/version")
def version():
  """Returns the build identity the running image was built from.

  - ``sha``: the git commit baked at `docker build` time via the `BUILD_SHA`
    build-arg (Dockerfile + deploy-prod.sh); "unknown" for a local
    `docker compose up` that didn't pass it. Lets a deploy verify the SERVED
    backend matches the intended commit — the backend analogue of the
    frontend bundle-hash check (bundle-info.sh / verify-fresh.sh).
  - ``served_frontend``: a content hash of the ``index.html`` in the frontend
    dir ACTUALLY being served (``frontend_source`` = ``platform`` or ``baked``).
    Changes whenever the watcher swaps a fresh ``vite build`` into the served
    ``dist`` — the frontend analogue of ``served_sha``. Poll it to confirm a
    frontend edit went live.

  A full GitHub-release check + one-click update is a follow-up; this exposes
  the local build identity cleanly so the image-pull path is self-verifying.
  """
  settings = get_settings()
  return {"sha": settings.build_sha,
          "build_date": settings.build_date,
          # Browser setup verifies this dedicated test-container marker before
          # any write. Localhost is not sufficient evidence because a preview
          # proxy can still forward to the live app.
          "test_runtime": os.environ.get("MOBIUS_TEST_RUNTIME") == "1",
          **_served_platform_identity(settings.data_dir),
          **_served_frontend_identity()}


@app.api_route(
  "/api/{path:path}",
  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
  include_in_schema=False,
)
def unknown_api(path: str):
  """Return a real API 404 instead of letting deleted endpoints fall through.

  The SPA catch-all below intentionally serves index.html for client routes,
  but `/api/*` misses are not client routes. Keeping this explicit makes
  removed backend surfaces disappear cleanly for every HTTP method. The
  prime example is the old `/api/ai` provider proxy, dropped 2026-06-05
  once apps moved to reaching models through the agent (`window.mobius.chat`,
  or a bundled server-side script run via `/api/apps/{id}/run-job`) rather
  than a synchronous in-backend completion endpoint.
  """
  raise HTTPException(status_code=404, detail="Not found.")


@app.get("/", include_in_schema=False)
def root_redirect():
  """Redirects the bare domain to the Möbius shell at `/shell/`.

  The PWA manifest's `scope` is `/shell/` so per-app sub-PWAs at
  `/apps/<slug>/` aren't absorbed into Möbius's install identity
  (the platform suppresses install prompts for in-scope URLs).
  Redirecting `/` keeps bookmarks and the bare-domain entry point
  working — users land where the shell actually lives.
  """
  from fastapi.responses import RedirectResponse
  return RedirectResponse(url="/shell/", status_code=308)


# -- Frontend static files (single-container mode) ---------------------
# Prefer the agent-editable whole-repo build at /data/platform/frontend/dist/
# if it exists and is complete (Vite root files + assets/ must be present).
# Fall back to the baked-in build at /app/static/ on any error.
_live_dir = live_frontend_dir(settings.data_dir)
# The baked SPA is at the IMAGE path /app/static, NOT relative to __file__.
# Under the clone serve model __file__ is /data/platform/backend/app/main.py, so
# `__file__.parent.parent / "static"` would resolve to /data/platform/backend/
# static (nonexistent) and the baked-frontend fallback would be dead
# whenever /data/platform/frontend/dist is incomplete. Resolve it absolutely
# (overridable via MOBIUS_BAKED_STATIC_DIR for non-standard image layouts).
_baked_dir = baked_frontend_dir()


def _resolve_static_dir() -> Path:
  """Return the frontend dir serving this request: live dist if complete, else
  the baked floor. Kept as the historical import seam for tests/callers."""
  return resolve_frontend_dir(settings.data_dir)


# The asset attic — frontend_watcher hardlinks each OUTGOING generation's
# content-hashed assets here on a dist swap. A sibling of ``dist`` so the
# hardlinks stay on one filesystem. Request-time /assets resolution serves a
# dist miss from these retained old generations so an unreloaded tab never 404s
# its chunk graph after a swap.
_ATTIC_DIR = _live_dir.parent / ".assets-attic"


def _resolve_asset_file(asset_path: str) -> Path | None:
  """Resolve ``/assets/<asset_path>`` to a file on disk, or None on a miss.

  Searches, in order, the served build's ``assets``, the attic's retained old
  generations, then the baked floor. Vite content-hashes every asset name, so
  those names never collide across generations — the union always yields the
  right bytes for whichever generation the client loaded, and the search order
  only affects which byte-identical copy answers first. A miss returns None so
  the caller emits a plain 404, never the SPA HTML: a JS module served as
  ``text/html`` is MIME-rejected by the browser and poisons the cache-first
  service worker (exactly the missing-``three.core.js`` failure). Each
  candidate is containment-checked against its root so ``..`` cannot escape.
  """
  roots = [_resolve_static_dir() / "assets"]
  try:
    roots.extend(p for p in _ATTIC_DIR.glob("gen-*/assets") if p.is_dir())
  except OSError:
    pass
  roots.append(_baked_dir / "assets")  # per-request corruption/boot floor
  for root in roots:
    try:
      root_r = root.resolve()
      target = (root_r / asset_path).resolve()
    except OSError:
      continue
    if target == root_r or root_r not in target.parents:
      continue  # the dir itself, or a `..` traversal escaping the root
    if target.is_file():
      return target
  return None


# Root-served worker scripts: `sw.js` caches the shell, `sw-push.js` owns Web
# Push (see frontend/public/sw-push.js). Same delivery contract at every use
# site below.
_SERVICE_WORKER_SCRIPTS = frozenset({"sw.js", "sw-push.js"})

# Small, same-origin worker-class scripts that are deliberately NOT precached
# (mirrors the frontend precache-policy.mjs UNPRECACHED_WORKERS list). They use
# stable URLs, so each load must revalidate instead of retaining old executable
# bytes under HTTP heuristic freshness. Dedicated workers also run under the
# Content-Security-Policy of their OWN response: a stale response is how
# on-device Pocket TTS stayed WebAssembly-blocked after shell_csp restored the
# 'wasm-unsafe-eval' source.
_UNPRECACHED_WORKER_SCRIPTS = frozenset({
  "speech/pocket-tts-worker.js",
  "speech/soundtouch-processor.js",
})

# The push worker's scope. It exists only to name a URL prefix inside the
# shell's PWA scope, and must never resolve to a document — a page here would
# be controlled by a worker with no fetch handler, so it would boot the shell
# with no precache and no offline fallback. Mirrored in the frontend's
# swNavigationPolicy denylist for the offline path.
_PUSH_WORKER_SCOPE = "shell/push"


def _is_static_asset_path(path: str) -> bool:
  """True for paths that must 404 on a miss rather than fall through to
  the SPA HTML.

  A module/asset URL served as `200 text/html` (the SPA fallback) is
  rejected by the browser's strict module-MIME check AND poisons a
  cache-first service worker — this is exactly how a missing
  `three.core.js` surfaced as "failed to load dynamic module". The HTML
  fallback is only meaningful for app routes, which have no file
  extension. We keep the set narrow (code/style assets) so a missing
  image still degrades gracefully instead of 404-ing a real route.

  The extension check matches code/asset URLs ANYWHERE (not just under
  `vendor/`/`assets/`) on purpose: a module miss outside those namespaces
  must also 404 rather than poison the SW with text/html. SPA client
  routes are extensionless by convention here, so this never 404s a real
  route — but if a future client route needs a `.js`/`.json` suffix,
  drop that extension from the set.
  """
  if path == "index.html":
    return False
  return (
    # First path segment — catches both `vendor` and `vendor/<file>`
    # without over-matching a route like `vendorfoo`.
    path.split("/", 1)[0] in {"vendor", "assets"}
    or path in _SERVICE_WORKER_SCRIPTS
    or path.rsplit(".", 1)[-1] in {
      "js", "mjs", "css", "html", "map", "wasm", "json",
    }
  )


_RESERVED_TOP_LEVEL_APP_ALIASES = {
  "api",
  "app",
  "app-assets",
  "apps",
  "assets",
  "chat",
  # Keep the retired recovery path reserved so a mini-app cannot squat on an
  # owner's old break-glass bookmark and impersonate a privileged surface.
  "recover",
  "shell",
  "vendor",
  *_SERVICE_WORKER_SCRIPTS,
}


def _public_static_headers(path: str) -> dict[str, str]:
  """Headers required when public shell assets cross an opaque app origin.

  Sandboxed app frames intentionally have the effective origin ``null`` and
  import both ``/mobius-runtime.js`` and the public modules under ``/vendor``.
  The nested chat embed inherits that opaque origin and loads the Vite shell
  JavaScript and CSS under ``/assets``.  All three namespaces are also fetched
  and cached by the shell service worker without an Origin header.  CORS
  middleware can decorate a direct opaque-origin request, but it cannot repair
  that already-cached response when the worker later returns it to the frame.
  Make the public executable assets intrinsically cross-origin readable so both
  the HTTP cache and service-worker cache preserve the contract.
  """
  if (
    path == "mobius-runtime.js"
    or path.split("/", 1)[0] in {"assets", "vendor"}
  ):
    return {"Access-Control-Allow-Origin": "*"}
  return {}


def _top_level_app_slug_alias(path: str) -> str | None:
  """Return an app slug for legacy top-level app URLs like `/cuberun`.

  Standalone apps are canonical at `/apps/<slug>/`, but older install
  experiments and shortcuts used `/<slug>`. If the root-scoped shell SW does
  not intercept that navigation, FastAPI's SPA fallback would otherwise serve
  the Mobius shell at `/<slug>`, which looks like the app opened a copy of
  Mobius. Redirect exact single-segment app slugs to the canonical standalone
  URL before serving the SPA.
  """
  slug = path.strip("/")
  if not slug or "/" in slug:
    return None
  if not all(ch.isalnum() or ch in "-_" for ch in slug):
    return None
  if slug in _RESERVED_TOP_LEVEL_APP_ALIASES:
    return None
  db = SessionLocal()
  try:
    # Only LIVE apps redirect — a tombstoned (soft-deleted) app's `/<slug>`
    # shouldn't bounce to a now-404 standalone route (feature 110).
    exists = (
      db.query(models.App.id)
      .filter(models.App.slug == slug, models.App.deleted_at.is_(None))
      .first()
    )
    return slug if exists else None
  finally:
    db.close()


def _app_source_dir_for_static_asset(
  *, slug: str | None = None, app_id: int | None = None,
) -> str | None:
  db = SessionLocal()
  try:
    # Tombstoned apps don't serve their /app-assets/ static files either —
    # consistent with the frame/module/standalone routes (feature 110).
    query = db.query(models.App.source_dir).filter(
      models.App.deleted_at.is_(None)
    )
    if app_id is not None:
      row = query.filter(models.App.id == app_id).first()
    elif slug is not None:
      row = query.filter(models.App.slug == slug).first()
    else:
      row = None
    return row[0] if row else None
  finally:
    db.close()


# A content-hash segment in the filename (main.8f3a2b1c.js,
# commando.f3b9c2e1a4.ttf) marks the asset immutable: a re-install that
# changes the bytes ships a different name, so the URL itself is the
# validator. Mirrored by isImmutableAppAsset in frontend/src/
# sw-cache-policy.js — keep the two in sync.
#
# The lookahead requires at least one ALPHABETIC hex digit (a-f) so an
# all-DIGIT segment isn't mistaken for a content hash: a date-stamped name
# like IMG-20260612.png or report.20260101.html is replaced in place on a
# re-upload and MUST keep revalidate semantics — marking it immutable would
# pin a year-stale copy in every client's cache. A real content/Vite hash
# always mixes in a-f (it's hex of a digest), so this never misfires on a
# genuine content hash.
_HASHED_ASSET_NAME = re.compile(
  r"[.-](?=[0-9a-f]*[a-f])[0-9a-f]{8,}\.", re.IGNORECASE
)


def _client_copy_is_fresh(request: Request, etag: str, mtime: float) -> bool:
  """True when conditional headers prove the client's copy is current.

  If-None-Match takes precedence over If-Modified-Since when both are
  present (RFC 7232 section 6); the date check is the fallback for
  clients that dropped the ETag.
  """
  if_none_match = request.headers.get("if-none-match")
  if if_none_match is not None:
    if if_none_match.strip() == "*":
      return True
    candidates = [
      tag.strip().removeprefix("W/") for tag in if_none_match.split(",")
    ]
    return etag in candidates
  if_modified_since = request.headers.get("if-modified-since")
  if if_modified_since is not None:
    try:
      since = parsedate_to_datetime(if_modified_since)
    except (TypeError, ValueError):
      return False
    if since.tzinfo is None:
      since = since.replace(tzinfo=timezone.utc)
    # HTTP dates have one-second resolution, so compare whole seconds.
    return int(mtime) <= since.timestamp()
  return False


def _serve_app_static_asset(
  source_dir: str | None, asset_path: str, request: Request,
):
  if not source_dir:
    raise HTTPException(status_code=404, detail="Not found.")

  root = (Path(source_dir) / "static").resolve()
  try:
    target = (root / (asset_path or "index.html")).resolve()
  except OSError:
    raise HTTPException(status_code=404, detail="Not found.")
  if target == root or target.is_dir():
    target = (target / "index.html").resolve()
  if root not in target.parents or not target.is_file():
    raise HTTPException(status_code=404, detail="Not found.")

  try:
    stat = target.stat()
  except OSError:
    raise HTTPException(status_code=404, detail="Not found.")

  # Asset files under a slug change only on app re-install, so
  # hashed-named files are cacheable forever (the new name busts the
  # cache) and everything else revalidates — but a revalidation is now
  # a bodiless 304 instead of a full re-download (CubeRun re-shipped
  # ~19MB of models/textures on every open before this).
  hashed = bool(_HASHED_ASSET_NAME.search(target.name))
  etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
  headers = {
    "Cache-Control": (
      "public, max-age=31536000, immutable"
      if hashed
      else "no-cache, must-revalidate"
    ),
    "ETag": etag,
    "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
    "X-Content-Type-Options": "nosniff",
  }
  if _client_copy_is_fresh(request, etag, stat.st_mtime):
    return Response(status_code=304, headers=headers)
  media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
  if not hashed:
    # Revalidating (non-hashed) assets get the full body unconditionally,
    # ignoring any Range header (RFC 9110 lets a server do that). Serving a
    # 206 slice of a `no-cache` + ETag asset poisoned Chromium's HTTP cache:
    # the stored slice revalidated 304 and was then served as a status-200
    # full response — 1 byte long. CubeRun's `Range: bytes=0-0` probe turned
    # the game's index.html into the single character '<' for every later
    # open (the 2026-06-12 black-screen outage). Strip Range so FileResponse
    # streams the full body off disk (no whole-file read into memory) and
    # answers HEAD header-only with the true Content-Length.
    strip_range(request)
  # Hashed (immutable) files keep Range/206 support for media seeking —
  # safe because Chromium never revalidates an immutable entry, so the
  # partial-slice-as-200 trap above can't fire for them.
  return FileResponse(str(target), media_type=media_type, headers=headers)


# HEAD is registered alongside GET because client-side asset probes ("are
# the files installed?") want existence + headers without the body; a 405
# pushes well-meaning probes into `Range: bytes=0-0` fallbacks, which is
# exactly the poisoning trigger described in _serve_app_static_asset.
@app.api_route(
  "/app-assets/by-id/{app_id}/{asset_path:path}",
  methods=["GET", "HEAD"],
  include_in_schema=False,
)
async def app_owned_asset_by_id(app_id: int, asset_path: str, request: Request):
  """Serve durable static assets owned by an installed app.

  Imported apps like CubeRun can keep a built static site under
  /data/apps/<slug>/static instead of copying it into the platform frontend.
  This route is public like standalone app shells; it serves only files below
  the installed app's source_dir/static.
  """
  return _serve_app_static_asset(
    await run_in_threadpool(_app_source_dir_for_static_asset, app_id=app_id),
    asset_path,
    request,
  )


@app.api_route(
  "/app-embeds/by-id/{app_id}/{asset_path:path}",
  methods=["GET", "HEAD"],
  include_in_schema=False,
)
async def app_owned_opaque_embed_by_id(
  app_id: int, asset_path: str, request: Request,
):
  """Serve a packaged static document under a permanently opaque origin.

  This namespace is intentionally frameable, including by an external site,
  so every response carries CSP sandbox without allow-same-origin. Relative
  assets stay below the same alias. Ordinary /app-assets remains protected by
  SAMEORIGIN and is never the document-navigation surface.
  """
  return _serve_app_static_asset(
    await run_in_threadpool(_app_source_dir_for_static_asset, app_id=app_id),
    asset_path,
    request,
  )


@app.api_route(
  "/app-assets/{slug}/{asset_path:path}",
  methods=["GET", "HEAD"],
  include_in_schema=False,
)
async def app_owned_asset(slug: str, asset_path: str, request: Request):
  """Serve durable static assets owned by an installed app slug."""
  if not slug or not all(ch.isalnum() or ch in "-_" for ch in slug):
    raise HTTPException(status_code=404, detail="Not found.")
  return _serve_app_static_asset(
    await run_in_threadpool(_app_source_dir_for_static_asset, slug=slug),
    asset_path,
    request,
  )


# Register the frontend serving routes whenever any static tree exists as a
# floor: the baked build is the guaranteed one inside the image, so this is
# effectively always-on in production; a bare local checkout has neither and
# skips the SPA fallback. WHICH tree serves each request — and where each
# /assets file comes from — is resolved per request (_resolve_static_dir /
# _resolve_asset_file), never frozen here at module load.
if _baked_dir.is_dir() or _live_dir.is_dir():
  from app.theme import get_bg_color, theme_data

  # /assets is a request-time handler, NOT a StaticFiles mount: it serves the
  # live build, then the attic's retained old generations, then a plain 404 —
  # never the SPA HTML. A missing chunk MUST be a 404, not a mystery text/html
  # payload (the browser MIME-rejects a module served as HTML and it poisons
  # the cache-first service worker). The mount had to bind one directory at
  # module load; this resolves per request, so a post-boot dist and a mid-swap
  # old generation both serve without a restart.
  @app.api_route(
    "/assets/{asset_path:path}", methods=["GET", "HEAD"], include_in_schema=False
  )
  async def serve_asset(request: Request, asset_path: str):
    target = await run_in_threadpool(_resolve_asset_file, asset_path)
    if target is None:
      raise HTTPException(status_code=404, detail="Not found.")
    media_type = (
      mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    )
    # Vite content-hashes every /assets filename, so the URL itself is the
    # validator — a changed asset ships a new name. That makes the bytes
    # safely immutable: cache hard, skip the revalidation round-trip.
    return FileResponse(
      str(target),
      media_type=media_type,
      headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
      },
    )

  @app.get("/{path:path}")
  async def spa_fallback(request: Request, path: str):
    """Serves the SPA index.html for any non-API, non-asset path."""
    if path == _PUSH_WORKER_SCOPE or path.startswith(f"{_PUSH_WORKER_SCOPE}/"):
      raise HTTPException(status_code=404, detail="Not found.")
    # Resolve which build serves THIS request (live dist if complete, else the
    # baked floor) once, up front — per request, never a module-load snapshot.
    static_dir = _resolve_static_dir()
    # An explicitly public app owns its exact top-level slug. It still runs in
    # the ordinary opaque sandbox, but the tiny parent host carries only a
    # short-lived exact-app public capability — never the owner's session.
    try:
      from app.routes.public_apps import public_app_page_for_path
      public_page = await run_in_threadpool(public_app_page_for_path, path)
    except Exception:
      logging.getLogger(__name__).exception(
        "public app host resolution failed for path=%s", path,
      )
      public_page = None
    if public_page is not None:
      return public_page
    app_slug = await run_in_threadpool(_top_level_app_slug_alias, path)
    if app_slug:
      from fastapi.responses import RedirectResponse
      return RedirectResponse(
        url=f"/apps/{app_slug}/",
        status_code=307,
        headers={"Cache-Control": "no-store"},
      )

    # Dynamically update manifest background to match theme.
    if path == "manifest.webmanifest":
      import json
      from fastapi.responses import JSONResponse
      try:
        manifest = json.loads(
          (static_dir / "manifest.webmanifest").read_text()
        )
      except OSError:
        # The dist swap has a microsecond two-rename window where the resolved
        # static dir can vanish; mirror the index.html guard below — a 503
        # asks the client to retry rather than 500ing a manifest fetch that
        # raced the publish.
        return Response(status_code=503, headers={"Retry-After": "1"})
      bg = get_bg_color(settings.data_dir)
      manifest["background_color"] = bg
      manifest["theme_color"] = bg
      return JSONResponse(
        manifest,
        media_type="application/manifest+json",
        # Revalidate on every fetch so an installed PWA picks up a new
        # theme_color after the owner changes the theme. On standalone
        # Android the OS derives the system/gesture-nav bar tint from the
        # manifest theme_color, so a browser-heuristic-cached manifest pins
        # the bar to the OLD --bg even though the page's own meta theme-color
        # (pre-paint + applyTheme) already followed the change — that lag was
        # the residual "gesture bar lighter than the app" report in card 164.
        # The manifest is NOT in the SW precache (vite.config.js globIgnores),
        # so the HTTP cache was the only stale layer left; no-cache keeps the
        # body cheap (304 when unchanged). Matches the per-app standalone
        # manifest (routes/standalone.py) and index.html/sw.js. This is the
        # delivery-path piece the reverted pre-paint-only #9 (2d882be) never
        # addressed; the meta theme-color sync it tried is already covered.
        headers={"Cache-Control": "no-cache, must-revalidate"},
      )

    file = static_dir / path
    if not file.is_file() and static_dir != _baked_dir and path != "index.html":
      # A complete live build can still omit an image-installed vendor asset,
      # or a new public file until that build refreshes. Pick the baked copy
      # before response policy so executable fallbacks cannot bypass the cache
      # and Range invariants enforced below.
      baked = _baked_dir / path
      if baked.is_file():
        file = baked
    if file.is_file() and path != "index.html":
      # The service worker MUST be served with `Cache-Control:
      # no-cache` so the browser revalidates it on every page load.
      # Without this header the browser caches sw.js by HTTP
      # heuristic (10% of last-modified age), which for a daily-
      # updated SW can be hours — old SW keeps serving the old
      # precached bundle even after deploys. Users reported the
      # PWA "not updating despite multiple refreshes" because of
      # this. `no-cache` (not `no-store`) still lets the browser
      # cache the response body but forces revalidation via
      # If-None-Match on every request, so a 304 keeps the
      # download cheap when nothing changed.
      headers = _public_static_headers(path)
      if path in _SERVICE_WORKER_SCRIPTS or path in _UNPRECACHED_WORKER_SCRIPTS:
        headers["Cache-Control"] = "no-cache, must-revalidate"
        # A worker script is a REVALIDATING response (no-cache + the mtime ETag
        # FileResponse sets), so it must never answer a 206. A
        # `Range: bytes=0-0` probe would otherwise let Chromium store the
        # 1-byte slice and later serve it as a status-200 full body — a
        # one-byte service worker. Stripping Range keeps the full-body 200
        # (same class as the /app-assets + /module fix; see http_caching).
        strip_range(request)
      return FileResponse(str(file), headers=headers or None)
    # Static asset namespaces 404 on a miss — they must never receive the
    # SPA HTML below (a module URL served as text/html is MIME-rejected by
    # the browser and poisons the cache-first service worker). Only app
    # routes get the HTML fallback.
    if _is_static_asset_path(path):
      raise HTTPException(status_code=404, detail="Not found.")
    # Theme-as-data: serialize the effective theme into the page's
    # `__mobius-theme__` JSON slot so the client's pre-paint script can
    # paint it flash-free (src/lib/applyTheme.js). The server no longer
    # injects a <style> block — it hands the client DATA, not pre-rendered
    # HTML, so there is exactly one theme <style> (the client's).
    #
    # Slot-injection security: the payload is owner-controlled CSS embedded
    # inside `<script type="application/json">`. The HTML parser ends that
    # script element at the first literal `</`, so an embedded `</script>`
    # (or `</`-anything) in the theme CSS would break out of the slot.
    # Escaping `</` -> `<\/` defuses that (JSON treats `\/` as `/`, so the
    # parsed value is identical). U+2028/U+2029 are valid in JSON strings
    # but are JS line terminators inside a <script>, so they must be
    # `\u`-escaped too. This is the mandatory slot-XSS defense.
    import json
    from fastapi.responses import HTMLResponse
    try:
      html = (static_dir / "index.html").read_text(encoding="utf-8")
    except FileNotFoundError:
      # The served dist is absent only during the frontend watcher's dist swap
      # (a two-rename window of a few microseconds — see
      # frontend_watcher._replace_dist). Report transient-unavailable so the
      # client retries into the settled build rather than seeing a 500.
      raise HTTPException(
        status_code=503, detail="Frontend rebuilding, retry.",
        headers={"Retry-After": "1"},
      )
    payload = (
      json.dumps(theme_data(settings.data_dir))
      .replace("</", "<\\/")
      .replace("\u2028", "\\u2028")
      .replace("\u2029", "\\u2029")
    )
    html = html.replace(
      '<script type="application/json" id="__mobius-theme__"></script>',
      f'<script type="application/json" id="__mobius-theme__">{payload}</script>',
    )
    # index.html MUST be served with `Cache-Control: no-cache` so the
    # browser revalidates on every page load. Without it, the browser
    # heuristically caches HTML for hours and the user's PWA keeps
    # loading the OLD <script src="/assets/index-{old-hash}.js">
    # references — they reload, see old code, blame the deploy. The
    # asset bundles themselves are content-hashed and immutable, so
    # the cost of revalidating index.html is one round-trip; with the
    # ETag the body usually comes back as 304. Paired with the
    # equivalent header on /sw.js (above) so neither side of the
    # shell-entry can pin the user to a stale build.
    return HTMLResponse(
      html,
      headers={"Cache-Control": "no-cache, must-revalidate"},
    )
