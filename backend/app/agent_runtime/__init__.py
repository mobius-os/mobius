"""mobius agent runtime — SDK-shaped wrapper around the Claude CLI.

Public surface mirrors `claude_agent_sdk`:
  from app.agent_runtime import (
    query, AgentOptions,
    AssistantMessage, UserMessage, ResultMessage,
    SystemMessage, PartialAssistantMessage,
    TextBlock, ToolUseBlock, ToolResultBlock,
  )

When the SDK migration lands, only the import line above changes:
  from claude_agent_sdk import (query, ClaudeAgentOptions as AgentOptions, ...)

All consumer code (chat.py and friends) stays the same.
"""

from app.agent_runtime.options import (
  AgentOptions,
  CanUseToolCallback,
  CanUseToolDecision,
  PermissionMode,
)
from app.agent_runtime.query import query
from app.agent_runtime.types import (
  AssistantMessage,
  ContentBlock,
  Message,
  PartialAssistantMessage,
  ResultMessage,
  SystemMessage,
  TextBlock,
  ToolResultBlock,
  ToolUseBlock,
  UserMessage,
)

__all__ = [
  "AgentOptions",
  "AssistantMessage",
  "CanUseToolCallback",
  "CanUseToolDecision",
  "ContentBlock",
  "Message",
  "PartialAssistantMessage",
  "PermissionMode",
  "ResultMessage",
  "SystemMessage",
  "TextBlock",
  "ToolResultBlock",
  "ToolUseBlock",
  "UserMessage",
  "query",
]
