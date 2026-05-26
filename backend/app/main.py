"""FastAPI application factory.

In production the single container serves both the API and the frontend
static files.  API routes are registered first; the frontend SPA is
mounted last as a catch-all so that client-side routing works.
"""

import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.database import Base, engine, run_migrations
from app import models
# providers and push are on the agent's write surface; deferred into
# lifespan with try/except so a SyntaxError in either doesn't prevent
# uvicorn boot (and thereby kill the recovery surface). See the
# wrapped imports in lifespan() below.
from app.routes import (
  ai_router, apps_router, auth_router,
  chat_router, chats_router, chats_stream_router,
  debug_router, generate_router, notifications_router,
  notify_router, proxy_router, push_router,
  recover_router, settings_router, storage_router,
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
  # Wrapped: push.py is on the agent's write surface. VAPID init is
  # nice-to-have (no push notifications without it) but not boot-critical.
  try:
    from app.push import init_vapid
    init_vapid()
  except Exception as exc:
    _log.error("init_vapid failed: %s", exc, exc_info=True)
  # Seed a Hello World app on first boot (no-op if apps already exist).
  # Wrapped: scripts/seed_hello.py is on the agent's write surface, and
  # a SyntaxError or runtime failure here would kill lifespan startup
  # and take /recover/chat down with it.
  try:
    from scripts.seed_hello import seed as seed_hello
    await seed_hello()
  except Exception as exc:
    _log.error("seed_hello failed: %s", exc, exc_info=True)
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
    from app.routes.apps import _derive_source_dir
    from app.database import SessionLocal
    from app import models as _models
    _db = SessionLocal()
    try:
      legacy = _db.query(_models.App).filter(
        _models.App.source_dir.is_(None)
      ).all()
      for _a in legacy:
        _a.source_dir = _derive_source_dir(settings.data_dir, _a.name)
      if legacy:
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
app.include_router(chat_router)
app.include_router(chats_router)
app.include_router(chats_stream_router)
app.include_router(ai_router)
app.include_router(notify_router)
app.include_router(proxy_router)
app.include_router(recover_router)

# Recovery chat — frozen, isolated from production chat code so it
# stays reachable when the agent breaks chat.py / providers.py /
# auth.py. See app/recover_chat.py for the design.
from app.recover_chat import router as recover_chat_router  # noqa: E402
app.include_router(recover_chat_router)
app.include_router(settings_router)
app.include_router(uploads_router)
app.include_router(generate_router)
app.include_router(push_router)
app.include_router(notifications_router)
app.include_router(debug_router)
app.include_router(theme_router)


@app.get("/api/health")
def health():
  """Returns a simple health check response."""
  return {"status": "ok"}


# -- Frontend static files (single-container mode) ---------------------
# Prefer the agent-editable build at /data/shell/dist/ if it exists and
# is complete (both assets/ and index.html must be present).
# Fall back to the baked-in build at /app/static/ on any error.
_live_dir = Path(settings.data_dir) / "shell" / "dist"
_baked_dir = Path(__file__).parent.parent / "static"


def _is_complete_build(d: Path) -> bool:
  """Returns True only if the directory looks like a complete Vite build."""
  return d.is_dir() and (d / "assets").is_dir() and (d / "index.html").is_file()


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
      return FileResponse(str(file))
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
    # Always inject theme CSS (default or override) so colors are
    # consistent from the first paint.
    from fastapi.responses import HTMLResponse
    html = (_static_dir / "index.html").read_text(encoding="utf-8")
    html = inject_theme_into_html(html, settings.data_dir)
    return HTMLResponse(html)
