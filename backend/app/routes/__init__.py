"""Route registry — crash-tolerant import scaffold.

`main.py` does a single `from app.routes import (...)` with ~15 names.
A `SyntaxError` (or any other ImportError) in any one of those
unprotected route modules would otherwise kill uvicorn at boot and
take the always-reachable `/recover/chat` endpoint down with it.

The defense lives here, in the one place that decides what gets
exposed. Each router is loaded through `_load(name)`: on success
we return the module's real `router`; on any import failure we log
loudly and return a stub `APIRouter` that 503s every path with a
message pointing at `/recover/chat`. The frozen `main.py` keeps
importing cleanly because every expected name still exists.

This file is itself frozen (see `protected-files.txt`) — it must
be unbreakable for the scaffold to be meaningful.
"""

import logging

from fastapi import APIRouter, HTTPException

log = logging.getLogger(__name__)


def _load(name: str) -> APIRouter:
  """Imports `app.routes.<name>` and returns its `router`, or a 503
  stub on any import failure."""
  try:
    mod = __import__(f"app.routes.{name}", fromlist=["router"])
    return mod.router
  except Exception as exc:
    log.error(
      "Failed to import app.routes.%s: %s",
      name, exc, exc_info=True,
    )
    stub = APIRouter()
    detail = (
      f"Router '{name}' failed to load at boot. "
      f"Use /recover/chat to repair."
    )

    @stub.api_route(
      "/{rest_of_path:path}",
      methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    )
    async def _broken(rest_of_path: str):
      raise HTTPException(503, detail=detail)

    return stub


ai_router = _load("ai")
apps_router = _load("apps")
auth_router = _load("auth")
chat_router = _load("chat")
chats_router = _load("chats")
chats_stream_router = _load("chats_stream")
proxy_router = _load("proxy")
notify_router = _load("notify")
# `recover.py` is also wrapped for symmetry / defense in depth, even
# though it's in the frozen island and should never fail to import.
recover_router = _load("recover")
settings_router = _load("settings")
storage_router = _load("storage")
uploads_router = _load("uploads")
generate_router = _load("generate")
push_router = _load("push")
notifications_router = _load("notifications")
debug_router = _load("debug")
theme_router = _load("theme")

__all__ = [
  "auth_router",
  "apps_router",
  "storage_router",
  "chat_router",
  "chats_router",
  "chats_stream_router",
  "ai_router",
  "proxy_router",
  "recover_router",
  "notify_router",
  "settings_router",
  "uploads_router",
  "generate_router",
  "push_router",
  "notifications_router",
  "debug_router",
  "theme_router",
]
