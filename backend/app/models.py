"""SQLAlchemy ORM models.

FROZEN at runtime (chmod 444 root-owned per protected-files.txt).
main.py and many route modules import these at module load; if I'm
broken the server can't boot and /recover/chat is unreachable.

To add a column to an existing table: edit me on the host repo and
rebuild. For per-chat fields you can usually skip a migration by
adding to `Chat.agent_settings_json` (a JSON column intentionally
included as the no-migration escape hatch). For app-scoped data
you'd otherwise add a column for, use per-app storage at
`/data/apps/<app_id>/...` via the storage API.
"""

from datetime import UTC, datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, JSON

from app.database import Base


class Owner(Base):
  """Single owner account for this installation."""

  __tablename__ = "owner"

  id = Column(Integer, primary_key=True)
  username = Column(String(64), nullable=False, unique=True)
  hashed_password = Column(String(255), nullable=False)
  gemini_api_key_enc = Column(Text, nullable=True, default=None)
  # Must stay in sync with providers.PROVIDER_NAMES.
  provider = Column(String(32), nullable=False, default="claude")
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class Chat(Base):
  """A chat conversation with the agent."""

  __tablename__ = "chats"

  id = Column(String(64), primary_key=True)
  title = Column(String(256), nullable=False, default="New chat")
  messages = Column(JSON, nullable=False, default=list)
  pending_messages = Column(JSON, nullable=False, default=list)
  uploads = Column(JSON, nullable=False, default=list)
  generated_images = Column(JSON, nullable=False, default=list)
  deleted_at = Column(DateTime, nullable=True, default=None)
  session_id = Column(String(128), nullable=True, default=None)
  # Must stay in sync with providers.PROVIDER_NAMES.
  provider = Column(String(32), nullable=False, default="claude")
  # Per-chat overrides for the agent runtime (model, effort, future
  # fields like thinking budget). When null, the chat uses the global
  # default from /data/shared/agent-settings.json. Stored as JSON
  # rather than dedicated columns so new fields can land without a
  # migration. Read in `chat.py:_run_chat_impl` and merged over the
  # file-loaded defaults; written by `PATCH /api/chats/{id}` from the
  # `/` slash picker (see `frontend/.../SlashPicker.jsx`).
  agent_settings_json = Column(JSON, nullable=True, default=None)
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))
  updated_at = Column(
    DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
  )


class App(Base):
  """A mini-app created and managed by the agent."""

  __tablename__ = "apps"

  id = Column(Integer, primary_key=True, index=True)
  name = Column(String(128), nullable=False)
  description = Column(Text, nullable=False, default="")
  jsx_source = Column(Text, nullable=False, default="")
  compiled_path = Column(String(512), nullable=False, default="")
  # Absolute directory under /data/apps/ holding this app's source
  # files (typically `/data/apps/<dirname>`).  Stored explicitly so
  # the file watcher can map a modified `index.jsx` back to its DB
  # row without slugify-guessing the name.  Null for apps created
  # before this column existed.
  source_dir = Column(String(512), nullable=True, default=None)
  # Chat that last created or modified this app.  Null for apps created
  # before this column was added.  Used to route app errors back to the
  # correct chat so the agent can fix them.
  chat_id = Column(String(64), nullable=True, default=None)
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))
  updated_at = Column(
    DateTime, default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
  )


class PushSubscription(Base):
  """Browser push subscription for Web Push delivery."""

  __tablename__ = "push_subscriptions"

  id = Column(String(64), primary_key=True)
  owner_id = Column(Integer, ForeignKey("owner.id"), nullable=False)
  endpoint = Column(Text, nullable=False, unique=True)
  p256dh = Column(Text, nullable=False)
  auth = Column(Text, nullable=False)
  created_at = Column(DateTime, default=lambda: datetime.now(UTC))


class Notification(Base):
  """Record of a sent push notification."""

  __tablename__ = "notifications"

  id = Column(String(64), primary_key=True)
  owner_id = Column(Integer, ForeignKey("owner.id"), nullable=False)
  source_type = Column(String(16), nullable=False)
  source_id = Column(String(64), nullable=True)
  title = Column(String(256), nullable=False)
  body = Column(Text, nullable=True)
  icon = Column(Text, nullable=True)
  target = Column(Text, nullable=True)
  actions = Column(JSON, nullable=True)
  sent_at = Column(DateTime, default=lambda: datetime.now(UTC))
  clicked_at = Column(DateTime, nullable=True)
