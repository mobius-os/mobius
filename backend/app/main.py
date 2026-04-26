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
from app.push import init_vapid
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


@asynccontextmanager
async def lifespan(app):
  _init_db()
  init_vapid()
  # Seed a Hello World app on first boot (no-op if apps already exist).
  from scripts.seed_hello import seed as seed_hello
  await seed_hello()
  yield

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
    # Always inject theme CSS (default or override) so colors are
    # consistent from the first paint.
    from fastapi.responses import HTMLResponse
    html = (_static_dir / "index.html").read_text(encoding="utf-8")
    html = inject_theme_into_html(html, settings.data_dir)
    return HTMLResponse(html)
