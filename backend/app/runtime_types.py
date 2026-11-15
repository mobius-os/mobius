"""Typed runtime contracts shared across runners and chat plumbing."""

from typing import Literal, NotRequired, TypedDict

from app.events import EventType


class SessionStart(TypedDict):
  """How a provider call started: what session state, and cost, it restored."""

  kind: Literal["fresh", "resumed", "forked"]
  # The resumed session, or the source session a fork restored.
  session_id: NotRequired[str]


class RunnerResult(TypedDict):
  """Return shape for one provider turn run."""

  session_id: str | None
  cost_usd: float | None
  # Claude returns a restored session estimate; the durable accounting owner
  # resolves it to a turn cost before logging, broadcasting or summing it.
  cost_scope: NotRequired[Literal["turn", "session"]]
  # Only the runner knows which snapshot a session-scoped cost restored.
  session_start: NotRequired[SessionStart]
  error: str | None
  usage: NotRequired[dict | None]
  usage_metrics: NotRequired[dict | None]
  terminal_status: NotRequired[str | None]
  final_message_phase: NotRequired[str | None]
  # False when the turn ended before its provider received the prompt, so
  # nothing the turn carried (peer notes, Wait results) was delivered.
  prompt_sent: NotRequired[bool]
  # Only unexpected main-process death correlated with an OOM increase during
  # this attempt, observed before runner teardown. Never a provider API error.
  oom_killed: NotRequired[bool]
  api_error_status: NotRequired[int]
  # The provider reported depleted workspace credits, which no reset refills.
  credits_depleted: NotRequired[bool]
  # A token-context rejection is distinct from an HTTP request-byte limit.
  context_window_exceeded: NotRequired[bool]
  # The provider's safety check declined the turn; retrying it unchanged on
  # the same model is refused again.
  provider_refusal: NotRequired[bool]


def prompt_not_sent_result(session_id: str | None) -> RunnerResult:
  """The result of a turn superseded before its provider received the prompt."""
  return {"session_id": session_id, "cost_usd": None, "error": None, "prompt_sent": False}


class ChatEvent(TypedDict):
  """Minimum event shape shared by all chat stream events."""

  type: EventType
