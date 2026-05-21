"""Options for the agent runtime — mirrors ClaudeAgentOptions.

Fields the mobius backend actually uses today are required to behave
the same way regardless of backend (CLI subprocess vs. SDK). Fields we
don't use yet are still defined so the SDK swap doesn't need a new
type.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional


# can_use_tool callback: (tool_name, input_dict) → permission decision.
# For "allow" the decision MAY include `updatedInput` which replaces
# the tool's input (used by AskUserQuestion to inject the user's
# answer). Not exposed yet — LEVEL-1 wrapper handles AskUserQuestion
# internally with kill-on-question.
CanUseToolDecision = dict
CanUseToolCallback = Callable[[str, dict], Awaitable[CanUseToolDecision]]


PermissionMode = Literal[
  'default', 'acceptEdits', 'auto', 'bypassPermissions', 'dontAsk', 'plan',
]


@dataclass
class AgentOptions:
  # System prompt (one of these — file wins if both set).
  system_prompt: Optional[str] = None
  system_prompt_file: Optional[str] = None

  # Session lifecycle.
  resume: Optional[str] = None      # existing session_id to resume
  session_id: Optional[str] = None  # explicit UUID for a NEW session

  # Model selection.
  model: Optional[str] = None
  effort: Optional[str] = None

  # Permissions.
  permission_mode: Optional[PermissionMode] = None
  allowed_tools: Optional[list[str]] = None
  disallowed_tools: Optional[list[str]] = None

  # Tool-approval callback. Not consumed by LEVEL-1 wrapper; defined
  # so chat.py can wire it once the SDK swap lands.
  can_use_tool: Optional[CanUseToolCallback] = None

  # Streaming.
  include_partial_messages: bool = True

  # Sandboxing.
  cwd: Optional[str] = None
  env: dict[str, str] = field(default_factory=dict)

  # Provider selection — not in upstream SDK but mobius needs to pick
  # claude vs. codex. Internal to our wrapper.
  provider: Optional[str] = None  # 'claude' | 'codex' — None = default
