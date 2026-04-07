"""Pydantic request and response schemas."""

from datetime import datetime

from pydantic import BaseModel


class SetupRequest(BaseModel):
  username: str
  password: str


class SetupStatus(BaseModel):
  configured: bool


class TokenResponse(BaseModel):
  access_token: str
  token_type: str = "bearer"


class AppCreate(BaseModel):
  name: str
  description: str = ""
  jsx_source: str
  chat_id: str | None = None


class AppUpdate(BaseModel):
  name: str | None = None
  description: str | None = None
  jsx_source: str | None = None
  # None means "omit from update" (the field is not changed).
  # To explicitly clear chat_id, pass an empty string ("").
  chat_id: str | None = None


class AppOut(BaseModel):
  id: int
  name: str
  description: str
  compiled_path: str
  chat_id: str | None = None
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


class SendMessage(BaseModel):
  content: str
  attachments: list[dict] | None = None
  timezone: str | None = None


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
  source_type: str  # 'agent' | 'app'
  source_id: str | None = None


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
