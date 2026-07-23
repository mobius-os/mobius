"""Typed runtime contracts shared across runners and chat plumbing."""

from typing import NotRequired, TypedDict

from app.events import EventType


class RunnerResult(TypedDict):
  """Return shape for one provider turn run."""

  session_id: str | None
  cost_usd: float | None
  error: str | None
  usage: NotRequired[dict | None]
  usage_metrics: NotRequired[dict | None]
  terminal_status: NotRequired[str | None]
  final_message_phase: NotRequired[str | None]


class ChatEventBase(TypedDict):
  """Minimum event shape shared by all chat stream events."""

  type: EventType


class TextEvent(ChatEventBase):
  content: str


class ToolStartEvent(ChatEventBase):
  tool: str
  input: str


class ToolInputEvent(ChatEventBase):
  input: str


class ToolOutputEvent(ChatEventBase):
  content: str


class SessionInitEvent(ChatEventBase):
  session_id: str


class QuestionEvent(ChatEventBase):
  questions: list[dict]
  # The PendingQuestion id the runner stamps on the event so the answer
  # routes can match the exact open question by identity, and the
  # save-before-broadcast QuestionCommit can persist it before the card
  # is shown. Optional: a defensive runner that omits it still dedups by
  # question_block_key (see app.events).
  question_id: NotRequired[str]


class ErrorEvent(ChatEventBase):
  message: str


class DoneEvent(ChatEventBase):
  cost_usd: float | int


ChatEvent = ChatEventBase
