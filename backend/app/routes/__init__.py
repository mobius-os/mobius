"""Route registry."""

from app.routes.ai import router as ai_router
from app.routes.apps import router as apps_router
from app.routes.auth import router as auth_router
from app.routes.chat import router as chat_router
from app.routes.chats import router as chats_router
from app.routes.chats_stream import router as chats_stream_router
from app.routes.proxy import router as proxy_router
from app.routes.notify import router as notify_router
from app.routes.recover import router as recover_router
from app.routes.settings import router as settings_router
from app.routes.storage import router as storage_router
from app.routes.uploads import router as uploads_router
from app.routes.generate import router as generate_router
from app.routes.push import router as push_router
from app.routes.notifications import router as notifications_router
from app.routes.debug import router as debug_router
from app.routes.theme import router as theme_router

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
