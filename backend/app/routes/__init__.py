"""Route registry — crash-tolerant import scaffold.

`main.py` does a single `from app.routes import (...)` for its router names.
A `SyntaxError` (or any other ImportError) in any one of those unprotected route
modules would otherwise kill uvicorn at boot instead of leaving the remaining
owner API available.

The defense lives here, in the one place that decides what gets
exposed. Each router is loaded through `_load(name)`: on success
we return the module's real `router`; on any import failure we log
loudly and return a stub `APIRouter` that 503s every path with an actionable
message. `main.py` keeps importing cleanly because every expected
name still exists.

Keep this registry deliberately small. If the editable copy itself breaks, the
root-owned entrypoint's import probe selects the baked platform fallback.
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
      "Use the deployment's external Recovery action to repair it."
    )

    @stub.api_route(
      "/{rest_of_path:path}",
      methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    )
    async def _broken(rest_of_path: str):
      raise HTTPException(503, detail=detail)

    return stub


admin_router = _load("admin")
apps_router = _load("apps")
auth_router = _load("auth")
chat_router = _load("chat")
chat_embed_router = _load("chat_embed")
chats_router = _load("chats")
chats_stream_router = _load("chats_stream")
chat_logs_router = _load("chat_logs")
connectors_router = _load("connectors")
try:
  from app.routes.connectors import public_router as connectors_public_router
except Exception:  # pragma: no cover - mirrors _load's stub behavior
  connectors_public_router = APIRouter()
proxy_router = _load("proxy")
local_services_router = _load("local_services")
notify_router = _load("notify")
settings_router = _load("settings")
storage_router = _load("storage")
fs_router = _load("fs")
uploads_router = _load("uploads")
media_router = _load("media")
secrets_router = _load("secrets")
github_router = _load("github")
push_router = _load("push")
notifications_router = _load("notifications")
debug_router = _load("debug")
delegations_router = _load("delegations")
theme_router = _load("theme")
self_reminders_router = _load("self_reminders")
skills_router = _load("skills")
standalone_router = _load("standalone")
client_error_router = _load("client_error")
client_signal_router = _load("client_signal")
platform_router = _load("platform")
published_router = _load("published")

__all__ = [
  "admin_router",
  "auth_router",
  "apps_router",
  "storage_router",
  "fs_router",
  "chat_router",
  "chat_embed_router",
  "chats_router",
  "chats_stream_router",
  "chat_logs_router",
  "connectors_router",
  "connectors_public_router",
  "proxy_router",
  "local_services_router",
  "notify_router",
  "settings_router",
  "uploads_router",
  "media_router",
  "secrets_router",
  "github_router",
  "push_router",
  "notifications_router",
  "debug_router",
  "delegations_router",
  "theme_router",
  "self_reminders_router",
  "skills_router",
  "standalone_router",
  "client_error_router",
  "client_signal_router",
  "platform_router",
  "published_router",
]
