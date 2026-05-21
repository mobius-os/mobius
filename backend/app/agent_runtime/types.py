"""SDK-shaped message + content-block types for the agent runtime.

These mirror `claude_agent_sdk`'s public types so a future swap from the
CLI subprocess backend to the official SDK is a backend change, not an
interface change. The `query()` function in this package yields these
types regardless of which backend produced them.

Keep parity with:
  https://github.com/anthropics/claude-agent-sdk-python  (Python types)
  https://github.com/nothflare/claude-agent-sdk-docs     (TS reference)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union


# ── Content blocks (inside AssistantMessage / UserMessage) ───────────

@dataclass
class TextBlock:
  text: str


@dataclass
class ToolUseBlock:
  id: str
  name: str
  input: dict


@dataclass
class ToolResultBlock:
  tool_use_id: str
  content: Any  # str or list of content dicts (text/image)
  is_error: bool = False


ContentBlock = Union[TextBlock, ToolUseBlock, ToolResultBlock]


# ── Top-level messages yielded by query() ────────────────────────────

@dataclass
class AssistantMessage:
  content: list[ContentBlock]
  session_id: str
  model: Optional[str] = None
  parent_tool_use_id: Optional[str] = None


@dataclass
class UserMessage:
  content: list[ContentBlock]
  session_id: str
  parent_tool_use_id: Optional[str] = None


@dataclass
class SystemMessage:
  subtype: str  # 'init' | 'compact_boundary' | future variants
  session_id: str
  data: dict = field(default_factory=dict)


@dataclass
class ResultMessage:
  subtype: str  # 'success' | 'error_max_turns' | 'error_during_execution' | ...
  session_id: str
  is_error: bool
  duration_ms: int
  num_turns: int
  result: Optional[str] = None
  total_cost_usd: float = 0.0
  usage: dict = field(default_factory=dict)
  stop_reason: Optional[str] = None


@dataclass
class PartialAssistantMessage:
  """Token-by-token streaming event. Emitted when include_partial_messages=True.

  `event` carries the underlying API stream event (content_block_start /
  content_block_delta / content_block_stop / message_start / message_delta /
  message_stop) as the API SDK delivers it.
  """
  event: dict
  session_id: str
  parent_tool_use_id: Optional[str] = None


Message = Union[
  AssistantMessage,
  UserMessage,
  SystemMessage,
  ResultMessage,
  PartialAssistantMessage,
]
