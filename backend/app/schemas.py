"""Pydantic request and response schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.providers import PROVIDER_NAMES


class SetupRequest(BaseModel):
  username: str
  password: str


class SetupStatus(BaseModel):
  configured: bool


class TokenResponse(BaseModel):
  access_token: str
  token_type: str = "bearer"


# Declared storage-access level. Used on two sides of the same coin:
#   cross_app_access  — what THIS app's token can do against others
#   share_with_apps   — what others can do against THIS app
# Cross-app traffic passes only when BOTH sides permit. Defaults to
# 'none' on both; the agent opts an app in when the partner asks.
ShareLevel = Literal["none", "read", "write"]


class AppCreate(BaseModel):
  name: str
  description: str = ""
  # Required — the full JSX source of the app's default export.
  # Prefer `register_app.py`, which handles schema + compile + DB
  # writes in one call. Hitting this endpoint directly is only
  # needed for copying apps between instances.
  jsx_source: str
  chat_id: str | None = None
  # Absolute directory under /data/apps/ where this app's index.jsx
  # lives. Passed by register_app.py so the file watcher can resolve
  # file events to apps without slugify-guessing the name.
  source_dir: str | None = None
  cross_app_access: ShareLevel = "none"
  share_with_apps: ShareLevel = "none"


class AppUpdate(BaseModel):
  # Drawer rename uses `name`; 500-char cap matches ChatPatch.title
  # so a runaway agent can't bloat the apps list response.
  name: str | None = Field(default=None, max_length=500)
  description: str | None = None
  jsx_source: str | None = None
  # None means "omit from update" (the field is not changed).
  # To explicitly clear chat_id, pass an empty string ("").
  chat_id: str | None = None
  source_dir: str | None = None
  # Drawer pin toggle. True sets pinned_at = now, False clears it.
  pinned: bool | None = None
  cross_app_access: ShareLevel | None = None
  share_with_apps: ShareLevel | None = None


class AppOut(BaseModel):
  id: int
  name: str
  description: str
  compiled_path: str
  chat_id: str | None = None
  source_dir: str | None = None
  pinned_at: datetime | None = None
  cross_app_access: ShareLevel = "none"
  share_with_apps: ShareLevel = "none"
  # URL slug for the standalone PWA install at /apps/<slug>/. Null
  # only for legacy rows from before the slug column existed; lazy-
  # backfilled on first access via standalone routes (see
  # routes/apps.py:ensure_slug).
  slug: str | None = None
  created_at: datetime
  updated_at: datetime

  model_config = {"from_attributes": True}


class ProviderCodeRequest(BaseModel):
  code: str


class AppTokenRequest(BaseModel):
  app_id: int


class ChatMessage(BaseModel):
  role: str
  content: str


class ChatRequest(BaseModel):
  messages: list[ChatMessage]
  chat_id: str = ""


class ChatStopRequest(BaseModel):
  chat_id: str = ""


class AgentSettingsOverride(BaseModel):
  """Per-chat agent settings override. Unknown fields are rejected
  (422) so a typo'd or experimental key can't silently land in the
  persisted chat row + every GET response. To add a new field,
  declare it explicitly below."""

  model_config = ConfigDict(extra="forbid")

  model: str | None = None
  # Union of both SDKs' effort enums:
  #   Codex `ReasoningEffort` (openai-codex 0.131+): none, minimal,
  #     low, medium, high, xhigh.
  #   Claude `EffortLevel` (claude-agent-sdk): low, medium, high,
  #     xhigh, max (xhigh + max are Opus-tier only).
  # The picker enforces per-provider scoping. Runners forward the
  # value as-is to the SDK; a model/effort mismatch (e.g. `max` on a
  # non-Opus Claude) surfaces as a 400 at turn time, not at PATCH.
  # Acceptable per the platform's "reversibility over prevention"
  # philosophy.
  effort: Literal[
    "none", "minimal", "low", "medium", "high", "xhigh", "max"
  ] | None = None
  # Per-provider memory of the last-picked effort. The enums are
  # NOT comparable across providers — Codex `medium` is roughly
  # Claude `low`, not Claude `medium` — so the picker has to
  # remember each provider's last value separately and swap
  # `effort` to that value when the user switches providers.
  # Frontend writes the full dict each PATCH; backend stores it
  # verbatim under this key.
  effort_by_provider: dict[str, str] | None = None


class ChatPatch(BaseModel):
  """Partial-update payload for chat runtime settings."""

  agent_settings_json: AgentSettingsOverride | None = None
  clear_agent_settings: bool = False
  provider: str | None = None
  # Drawer rename uses this. Empty string is rejected so a misfired
  # blur on an empty input can't blank the title in the sidebar.
  # 500-char cap defends against runaway-agent megabyte payloads
  # bloating the chats list response on every refresh.
  title: str | None = Field(default=None, max_length=500)
  # Drawer pin toggle. True sets pinned_at = now, False clears it.
  pinned: bool | None = None

  @field_validator("provider")
  @classmethod
  def validate_provider(cls, value: str | None) -> str | None:
    """Reject unknown provider IDs at request-deserialize time."""
    if value is not None and value not in PROVIDER_NAMES:
      raise ValueError(f"unknown provider: {value}")
    return value


class SendMessage(BaseModel):
  content: str
  attachments: list[dict] | None = None
  timezone: str | None = None
  viewport: dict | None = None
  hidden: bool = False
  # When `hidden=True` and the user is answering an AskUserQuestion,
  # frontend includes the resolved answers here. Backend writes them
  # into the LAST assistant message's question block in the same
  # transaction that appends the hidden user message — eliminating the
  # POST /question-answers + POST /messages race that left answers
  # missing on mid-stream remounts.
  answers: dict | None = None


class PushKeys(BaseModel):
  p256dh: str
  auth: str


class PushSubscribeRequest(BaseModel):
  endpoint: str
  keys: PushKeys


class PushUnsubscribeRequest(BaseModel):
  endpoint: str


class NotificationAction(BaseModel):
  action: str
  title: str
  target: str | None = None


class NotificationSendRequest(BaseModel):
  title: str
  body: str | None = None
  icon: str | None = None
  target: str | None = None
  actions: list[NotificationAction] | None = None
  # Defaults to 'agent' so the common agent-authored curl works
  # with just {title, body}. Apps should pass 'app' + their id.
  source_type: str = "agent"
  source_id: str | None = None


class SettingsUpdate(BaseModel):
  """Owner-level settings updates."""

  gemini_api_key: str | None = None
  provider: str | None = None

  @field_validator("provider")
  @classmethod
  def validate_provider(cls, value: str | None) -> str | None:
    """Reject unknown provider IDs at request-deserialize time."""
    if value is not None and value not in PROVIDER_NAMES:
      raise ValueError(f"unknown provider: {value}")
    return value


class NotificationOut(BaseModel):
  id: str
  source_type: str
  source_id: str | None
  title: str
  body: str | None
  icon: str | None
  target: str | None
  actions: list | None
  sent_at: datetime
  clicked_at: datetime | None

  model_config = {"from_attributes": True}
