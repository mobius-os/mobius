"""Route registry — crash-tolerant import scaffold.

`main.py` does a single `from app.routes import (...)` for its router names.
A `SyntaxError` (or any other ImportError) in any one of those unprotected route
modules would otherwise kill uvicorn at boot.

The defense lives here, in the one place that decides what gets
exposed. Each router is loaded through `_load(name)`: on success we return the
requested router; on failure we record it and return an empty `APIRouter` so
`main.py` can finish importing. The entrypoint then calls
`require_all_routers_loaded()` and selects the baked platform floor if anything
failed. The empty fallback is deliberate: if source changes in the tiny window
between probe and serve, one broken module must not install a global catch-all
that shadows `/api/health` and every healthy router.

Keep this registry deliberately small. If the editable copy itself breaks, the
root-owned entrypoint's import probe selects the baked platform fallback.
"""

import logging

from fastapi import APIRouter

log = logging.getLogger(__name__)
_ROUTER_IMPORT_FAILURES: set[str] = set()


def _load(name: str, attr: str = "router") -> APIRouter:
  """Return one router attribute, recording and isolating import failures."""
  key = name if attr == "router" else f"{name}.{attr}"
  try:
    mod = __import__(f"app.routes.{name}", fromlist=[attr])
    return getattr(mod, attr)
  except Exception as exc:
    log.error(
      "Failed to import app.routes.%s: %s",
      key, exc, exc_info=True,
    )
    _ROUTER_IMPORT_FAILURES.add(key)
    return APIRouter()


def router_import_failures() -> tuple[str, ...]:
  """Stable public boot verdict shared by the entrypoint and diagnostics."""
  return tuple(sorted(_ROUTER_IMPORT_FAILURES))


def require_all_routers_loaded() -> None:
  """Fail the boot probe when `_load` had to isolate any broken router."""
  failures = router_import_failures()
  if failures:
    raise RuntimeError(f"Router imports failed: {', '.join(failures)}")


admin_router = _load("admin")
apps_router = _load("apps")
auth_router = _load("auth")
chat_router = _load("chat")
chat_embed_router = _load("chat_embed")
chats_router = _load("chats")
app_chat_router = _load("chats", "app_chat_router")
chats_stream_router = _load("chats_stream")
secure_inputs_router = _load("secure_inputs")
chat_logs_router = _load("chat_logs")
connectors_router = _load("connectors")
connectors_public_router = _load("connectors", "public_router")
proxy_router = _load("proxy")
public_apps_router = _load("public_apps")
local_services_router = _load("local_services")
notify_router = _load("notify")
screen_control_router = _load("screen_control")
settings_router = _load("settings")
storage_router = _load("storage")
fs_router = _load("fs")
uploads_router = _load("uploads")
media_router = _load("media")
secrets_router = _load("secrets")
github_router = _load("github")
identity_router = _load("identity")
push_router = _load("push")
notifications_router = _load("notifications")
debug_router = _load("debug")
delegations_router = _load("delegations")
chat_waits_router = _load("chat_waits")
goal_plans_router = _load("goal_plans")
theme_router = _load("theme")
self_reminders_router = _load("self_reminders")
skills_router = _load("skills")
standalone_router = _load("standalone")
client_error_router = _load("client_error")
client_signal_router = _load("client_signal")
community_router = _load("community")
contribution_relay_router = _load("contribution_relay")
platform_router = _load("platform")
published_router = _load("published")
connect_router = _load("connect")
projects_router = _load("projects")

__all__ = [
  "admin_router",
  "auth_router",
  "apps_router",
  "storage_router",
  "fs_router",
  "chat_router",
  "chat_embed_router",
  "chats_router",
  "app_chat_router",
  "chats_stream_router",
  "secure_inputs_router",
  "chat_logs_router",
  "connectors_router",
  "connectors_public_router",
  "proxy_router",
  "public_apps_router",
  "local_services_router",
  "notify_router",
  "screen_control_router",
  "settings_router",
  "uploads_router",
  "media_router",
  "secrets_router",
  "github_router",
  "identity_router",
  "push_router",
  "notifications_router",
  "debug_router",
  "delegations_router",
  "chat_waits_router",
  "goal_plans_router",
  "theme_router",
  "self_reminders_router",
  "skills_router",
  "standalone_router",
  "client_error_router",
  "client_signal_router",
  "community_router",
  "contribution_relay_router",
  "platform_router",
  "published_router",
  "connect_router",
  "projects_router",
]
