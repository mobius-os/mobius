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


class ChatEvent(TypedDict):
  """Minimum event shape shared by all chat stream events."""

  type: EventType
