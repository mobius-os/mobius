"""FastAPI application factory.

In production the single container serves both the API and the frontend
static files.  API routes are registered first; the frontend SPA is
mounted last as a catch-all so that client-side routing works.
"""

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from datetime import timezone
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.database import Base, SessionLocal, engine, run_migrations
from app import models
# providers and push are on the agent's write surface; deferred into
# lifespan with try/except so a SyntaxError in either doesn't prevent
# uvicorn boot (and thereby kill the recovery surface). See the
# wrapped imports in lifespan() below.
from app.routes import (
  admin_router, apps_router, auth_router,
  chat_logs_router, chat_router, chats_router, chats_stream_router,
  debug_router, fs_router, generate_router,
  notifications_router, notify_router, proxy_router, push_router,
  recover_router, self_reminders_router, settings_router,
  standalone_router, storage_router,
  theme_router, uploads_router,
)


def _init_db():
  """Run migrations and create tables, retrying on transient failures."""
  for attempt in range(10):
    try:
      run_migrations(engine)
      Base.metadata.create_all(bind=engine)
      return
    except OperationalError as e:
      if attempt < 9:
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
  import asyncio as _asyncio
  import logging as _logging
  _log = _logging.getLogger(__name__)
  # Wrapped: providers.py is on the agent's write surface. A broken
  # providers.py shouldn't take down the server — log and skip the
  # defaults check so the recovery surface stays reachable.
  try:
    from app.providers import PROVIDER_NAMES
    _assert_provider_defaults(PROVIDER_NAMES)
  except Exception as exc:
    _log.error("provider defaults check skipped: %s", exc, exc_info=True)
  _init_db()
  # Crash recovery: a process death (OOM / SIGKILL — a recurring
  # failure mode on this host) mid-turn leaves the chat's durable
  # run marker set but the in-memory registry empty. Reconcile those
  # stranded chats now, before the server accepts requests, so a
  # mid-turn crash resolves cleanly on reopen instead of spinning
  # "running" forever and stranding queued messages. Wrapped like the
  # other lifespan steps: a failure here must not brick the recovery
  # surface. Runs single-threaded pre-serving, so no queue-lock
  # contention — see reconcile_interrupted_chats for the argument.
  try:
    from app.chat import reconcile_interrupted_chats
    from app.database import SessionLocal as _ReconcileSession
    _rc_db = _ReconcileSession()
    try:
      reconcile_interrupted_chats(_rc_db)
    finally:
      _rc_db.close()
  except Exception as exc:
    _log.error("startup chat reconciliation failed: %s", exc, exc_info=True)
    # Expose the failure through /api/debug/status so operators and
    # tests can detect it without tailing logs. The never-crash-boot
    # contract is preserved: we only set a flag, never raise.
    app.state.reconciliation_failed = True
  # Discard any `*.js.staging` bundle left by a crash between a recompile's
  # commit and its atomic promote (see compiler.recompile_app_bundle). A leaked
  # staging file is never served; reaping it just keeps the compiled dir clean.
  try:
    from app.compiler import reap_staging_bundles
    reap_staging_bundles()
  except Exception as exc:
    _log.error("staging-bundle reap failed: %s", exc, exc_info=True)
  # Recompile any live App row whose compiled bundle is missing/empty. A crash
  # between the install's db.commit() and its post-commit os.replace leaves a
  # durable row pointing at a bundle that was never written (the staging copy
  # reaped just above), so the app 404s forever with no self-heal — this heals
  # it from the stored jsx_source. Runs AFTER the reap (so a half-promoted
  # staging file is gone before we decide a bundle is missing) and before the
  # server serves requests. Wrapped + per-app error-isolated so neither a bad
  # source nor a compile failure can brick boot or the recovery surface.
  try:
    from app.compiler import reconcile_missing_bundles
    from app.database import SessionLocal as _BundleSession
    _bn_db = _BundleSession()
    try:
      await reconcile_missing_bundles(_bn_db)
    finally:
      _bn_db.close()
  except Exception as exc:
    _log.error("missing-bundle reconcile wiring failed: %s", exc, exc_info=True)
  # Start the single-writer chat-persistence actor AFTER db init and
  # crash reconciliation. Order is load-bearing: reconcile_interrupted_chats
  # must run BEFORE the actor exists — recovery has to work even when
  # persistence is degraded, so it never routes through the actor.
  # start_writer catches its own startup failure (marks the writer fatal
  # rather than raising), so a writer that can't start can't brick boot
  # or the recovery surface. The actor is LIVE: it is the chat-persistence
  # path the C2 write routes/runners submit every transcript write through.
  try:
    from app.chat_writer import start_writer
    start_writer()
  except Exception as exc:
    _log.error("chat writer start wiring failed: %s", exc, exc_info=True)
  # Wrapped: push.py is on the agent's write surface. VAPID init is
  # nice-to-have (no push notifications without it) but not boot-critical.
  try:
    from app.push import init_vapid
    init_vapid()
  except Exception as exc:
    _log.error("init_vapid failed: %s", exc, exc_info=True)
  # First-boot auto-install of the curated app-store mini-app so a
  # fresh container shows the store in the drawer immediately. The
  # bootstrap module is idempotent (no-op if slug='store' already
  # exists) and swallows its own failures — a GitHub blip must not
  # crash lifespan and brick the recovery surface.
  try:
    from app.bootstrap import ensure_store_installed
    from app.database import SessionLocal as _BootstrapSession
    _bs_db = _BootstrapSession()
    try:
      await ensure_store_installed(_bs_db)
    finally:
      _bs_db.close()
  except Exception as exc:
    _log.error("bootstrap store install wiring failed: %s", exc, exc_info=True)
  # Backfill source_dir for legacy app rows. The file watcher resolves
  # /data/apps/<slug>/index.jsx → app.id via exact source_dir match;
  # rows with NULL (older builds, or apps imported without going
  # through register_app.py) would silently never auto-recompile.
  # Derive the same slug shape register_app.py uses and persist it.
  #
  # Wrapped: app/routes/apps.py is on the agent's write surface. The
  # routes/__init__.py _load() scaffold stubs apps_router on import
  # failure, but this direct import bypasses that — without the
  # try/except a SyntaxError in apps.py would crash lifespan and take
  # /recover/chat down with it (the exact failure mode the scaffold
  # was built to prevent).
  try:
    from pathlib import Path as _Path
    from app.database import SessionLocal
    from app import models as _models
    _db = SessionLocal()
    try:
      legacy = _db.query(_models.App).filter(
        _models.App.source_dir.is_(None)
      ).all()
      changed = False
      for _a in legacy:
        # Derive from the UNIQUE slug (the migration assigns one) — NOT the raw
        # name, which would give two legacy rows named "News" the same
        # /data/apps/news tree. Skip a dir another app already claims so the
        # repair never creates a shared source tree.
        if not _a.slug:
          continue
        candidate = str(_Path(settings.data_dir) / "apps" / _a.slug)
        if _db.query(_models.App).filter(
          _models.App.id != _a.id, _models.App.source_dir == candidate
        ).first():
          continue
        _a.source_dir = candidate
        changed = True
      if changed:
        _db.commit()
    finally:
      _db.close()
  except Exception as exc:
    _log.error("source_dir backfill failed: %s", exc, exc_info=True)
  # Start the JSX file watcher so direct edits to /data/apps/*/index.jsx
  # auto-recompile and refresh the served bundle — agents don't need to
  # re-run register_app.py just to push a code change.
  # Wrapped: app/app_watcher.py is on the agent's write surface; a
  # failure must not crash lifespan.
  _observer = None
  _handler = None
  try:
    from app.app_watcher import start_watcher
    _observer, _handler = start_watcher(_asyncio.get_running_loop())
  except Exception as exc:
    _log.error("start_watcher failed: %s", exc, exc_info=True)
  try:
    yield
  finally:
    # Drain + join the chat-writer actor so any in-flight persistence
    # completes before the process exits. Wrapped: a stop failure must
    # not mask the rest of shutdown.
    try:
      from app.chat_writer import stop_writer
      stop_writer()
    except Exception as exc:
      _log.error("chat writer stop failed: %s", exc, exc_info=True)
    # Drain pending debounce timers first so they can't post coroutines
    # to a loop that's about to close, then stop+join the observer.
    if _handler is not None:
      try:
        _handler.close()
      except Exception as exc:
        _log.error("watcher handler.close failed: %s", exc, exc_info=True)
    if _observer is not None:
      # Split stop/join into independent try blocks so a stop()
      # failure doesn't skip join() — otherwise the watchdog thread
      # would never be reaped on shutdown. In practice both are very
      # unlikely to raise, but structurally a shared try would let
      # one fault swallow the other.
      try:
        _observer.stop()
      except Exception as exc:
        _log.error("watcher observer.stop failed: %s", exc, exc_info=True)
      try:
        _observer.join(timeout=2)
      except Exception as exc:
        _log.error("watcher observer.join failed: %s", exc, exc_info=True)

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


app.add_middleware(_BodySizeLimitMiddleware, max_bytes=_MAX_REQUEST_BODY_BYTES)

app.add_middleware(
  CORSMiddleware,
  # "null" is the origin of sandboxed iframes (allow-same-origin absent).
  # All sensitive endpoints are independently protected by JWT.
  allow_origins=[settings.frontend_origin, "null"],
  allow_credentials=False,
  allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
  allow_headers=["Authorization", "Content-Type"],
)

# -- API routes --------------------------------------------------------
app.include_router(auth_router)
app.include_router(apps_router)
app.include_router(storage_router)
app.include_router(fs_router)
app.include_router(chat_router)
app.include_router(chats_router)
app.include_router(chats_stream_router)
app.include_router(chat_logs_router)
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
app.include_router(proxy_router)
app.include_router(recover_router)

# Recovery chat — frozen, isolated from production chat code so it
# stays reachable when the agent breaks chat.py / providers.py /
# auth.py. See app/recover_chat.py for the design.
from app.recover_chat import router as recover_chat_router  # noqa: E402
app.include_router(recover_chat_router)

# Recovery OAuth — frozen, isolated from routes/auth.py so the
# recovery surface can connect/reconnect a provider even when the
# main-app auth routes are broken by an agent edit. See
# app/recover_oauth.py for the design.
from app.recover_oauth import router as recover_oauth_router  # noqa: E402
app.include_router(recover_oauth_router)
app.include_router(settings_router)
app.include_router(uploads_router)
app.include_router(generate_router)
app.include_router(push_router)
app.include_router(notifications_router)
app.include_router(debug_router)
app.include_router(theme_router)
app.include_router(admin_router)
app.include_router(self_reminders_router)
# Standalone PWA surface at /apps/<slug>/{,manifest.json,icon-N.png}.
# Registered AFTER the API routers but BEFORE the SPA catch-all
# (which mounts conditionally below at /{path:path}) so its explicit
# routes win.
app.include_router(standalone_router)


@app.get("/api/health")
def health(response: Response):
  """Returns a simple health check response.

  `Cache-Control: no-store` so the client's reachability probe
  (`useOnlineStatus`) can never be answered from any HTTP cache or heuristic
  freshness — the probe must reflect a real network round-trip. The probe
  already sends `cache: 'no-store'`, but the response carrying the directive
  too is belt-and-suspenders against an intermediary or a stale-200 path
  (a suspected contributor to the Android offline-probe-returns-true anomaly).
  """
  response.headers["Cache-Control"] = "no-store"
  return {"status": "ok"}


@app.get("/api/ready")
def ready(response: Response):
  """Readiness probe: 200 only when chat persistence can actually serve.

  Distinct from `/api/health` (liveness — the process is up and answering
  HTTP). The single-writer chat-persistence actor can fail to start, go
  fatal, or be stopping while the process still answers `/api/health` 200;
  in that window every chat write fails. A deploy (and `deploy-prod.sh`'s
  health gate) must NOT green on a process that can't persist a chat, so
  this route returns 503 until the writer is genuinely ready.

  `is_writer_ready()` (via `writer_readiness`) owns the predicate: the
  writer singleton exists, its worker thread is alive, and the actor is
  neither fatal nor stopping. The route only maps the verdict to a status
  code and surfaces the reason. Startup ordering is fine — the lifespan
  runs `start_writer()` before uvicorn serves, so there is no cold-start
  window where this false-fails.
  """
  response.headers["Cache-Control"] = "no-store"
  from app.chat_writer import writer_readiness
  is_ready, reason = writer_readiness()
  if is_ready:
    return {"ready": True}
  response.status_code = 503
  return {"ready": False, "reason": reason}


@app.get("/api/version")
def version():
  """Returns the git commit the running image was built from.

  Baked at `docker build` time via the `BUILD_SHA` build-arg (Dockerfile +
  deploy-prod.sh); "unknown" for a local `docker compose up` that didn't pass
  it. Lets a deploy verify the SERVED backend matches the intended commit —
  the backend analogue of the frontend bundle-hash check (bundle-info.sh /
  verify-fresh.sh, which only see the shell bundle, not the backend).
  """
  return {"sha": get_settings().build_sha}


@app.api_route(
  "/api/{path:path}",
  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
  include_in_schema=False,
)
def unknown_api(path: str):
  """Return a real API 404 instead of letting deleted endpoints fall through.

  The SPA catch-all below intentionally serves index.html for client routes,
  but `/api/*` misses are not client routes. Keeping this explicit makes
  removed backend surfaces, such as the old `/api/ai`, disappear cleanly for
  every HTTP method.
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
# Prefer the agent-editable build at /data/shell/dist/ if it exists and
# is complete (both assets/ and index.html must be present).
# Fall back to the baked-in build at /app/static/ on any error.
_live_dir = Path(settings.data_dir) / "shell" / "dist"
_baked_dir = Path(__file__).parent.parent / "static"


def _is_complete_build(d: Path) -> bool:
  """Returns True only if the directory looks like a complete Vite build."""
  return d.is_dir() and (d / "assets").is_dir() and (d / "index.html").is_file()


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
    or path == "sw.js"
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
  "recover",
  "shell",
  "sw.js",
  "vendor",
}


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
_HASHED_ASSET_NAME = re.compile(r"[.-][0-9a-f]{8,}\.", re.IGNORECASE)


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
  etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
  headers = {
    "Cache-Control": (
      "public, max-age=31536000, immutable"
      if _HASHED_ASSET_NAME.search(target.name)
      else "no-cache, must-revalidate"
    ),
    "ETag": etag,
    "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
    "X-Content-Type-Options": "nosniff",
  }
  if _client_copy_is_fresh(request, etag, stat.st_mtime):
    return Response(status_code=304, headers=headers)
  return FileResponse(str(target), headers=headers)


@app.get("/app-assets/by-id/{app_id}/{asset_path:path}", include_in_schema=False)
async def app_owned_asset_by_id(app_id: int, asset_path: str, request: Request):
  """Serve durable static assets owned by an installed app.

  Imported apps like CubeRun can keep a built static site under
  /data/apps/<slug>/static instead of copying it into /data/shell, which is
  intentionally refreshed on deploy. This route is public like standalone app
  shells; it serves only files below the installed app's source_dir/static.
  """
  return _serve_app_static_asset(
    await asyncio.to_thread(_app_source_dir_for_static_asset, app_id=app_id),
    asset_path,
    request,
  )


@app.get("/app-assets/{slug}/{asset_path:path}", include_in_schema=False)
async def app_owned_asset(slug: str, asset_path: str, request: Request):
  """Serve durable static assets owned by an installed app slug."""
  if not slug or not all(ch.isalnum() or ch in "-_" for ch in slug):
    raise HTTPException(status_code=404, detail="Not found.")
  return _serve_app_static_asset(
    await asyncio.to_thread(_app_source_dir_for_static_asset, slug=slug),
    asset_path,
    request,
  )


_static_dir = _live_dir if _is_complete_build(_live_dir) else _baked_dir
if _static_dir.is_dir():
  try:
    # Serve static assets (JS, CSS, images) at their exact paths.
    app.mount(
      "/assets",
      StaticFiles(directory=str(_static_dir / "assets")),
      name="assets",
    )
  except Exception:
    # Live build is corrupt — fall back to the baked-in build silently.
    _static_dir = _baked_dir
    app.mount(
      "/assets",
      StaticFiles(directory=str(_static_dir / "assets")),
      name="assets",
    )

  from app.theme import get_bg_color, inject_theme_into_html

  @app.get("/{path:path}")
  async def spa_fallback(request: Request, path: str):
    """Serves the SPA index.html for any non-API, non-asset path."""
    app_slug = await asyncio.to_thread(_top_level_app_slug_alias, path)
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
      manifest = json.loads(
        (_static_dir / "manifest.webmanifest").read_text()
      )
      bg = get_bg_color(settings.data_dir)
      manifest["background_color"] = bg
      manifest["theme_color"] = bg
      return JSONResponse(manifest, media_type="application/manifest+json")

    file = _static_dir / path
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
      headers = (
        {"Cache-Control": "no-cache, must-revalidate"}
        if path == "sw.js"
        else None
      )
      return FileResponse(str(file), headers=headers)
    # _static_dir resolution is all-or-nothing at startup — when the
    # agent's live build (/data/shell/dist) is selected, any file
    # that lives ONLY in the baked build (/app/static) would
    # otherwise fall through to the HTML response. /vendor/three/*
    # is the canonical example: the npm-install vendor copy lands in
    # /app/static at image build time, but Vite doesn't include
    # vendor in /data/shell/dist. Falling back to the baked dir for
    # files-not-in-live keeps mini-app imports working without
    # forcing the rebuild script to mirror the entire vendor tree.
    if _static_dir != _baked_dir and path != "index.html":
      baked = _baked_dir / path
      if baked.is_file():
        return FileResponse(str(baked))
    # Static asset namespaces 404 on a miss — they must never receive the
    # SPA HTML below (a module URL served as text/html is MIME-rejected by
    # the browser and poisons the cache-first service worker). Only app
    # routes get the HTML fallback.
    if _is_static_asset_path(path):
      raise HTTPException(status_code=404, detail="Not found.")
    # Always inject theme CSS (default or override) so colors are
    # consistent from the first paint.
    from fastapi.responses import HTMLResponse
    html = (_static_dir / "index.html").read_text(encoding="utf-8")
    html = inject_theme_into_html(html, settings.data_dir)
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
