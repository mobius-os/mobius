"""Tests for CodexProvider event parsing."""

import json
from app.providers import CodexProvider

provider = CodexProvider()


def test_thread_started_emits_session_init():
  line = '{"type":"thread.started","thread_id":"abc-123"}'
  result = provider.parse_line(line)
  assert result == {"type": "session_init", "session_id": "abc-123"}


def test_agent_message_emits_text():
  line = json.dumps({
    "type": "item.completed",
    "item": {"id": "item_0", "type": "agent_message", "text": "Hello"},
  })
  result = provider.parse_line(line)
  assert result == {"type": "text", "content": "Hello"}


def test_command_started_emits_tool_start():
  line = json.dumps({
    "type": "item.started",
    "item": {
      "id": "item_0", "type": "command_execution",
      "command": "/bin/bash -lc 'echo hi'",
      "aggregated_output": "", "exit_code": None,
      "status": "in_progress",
    },
  })
  result = provider.parse_line(line)
  assert result == {
    "type": "tool_start",
    "tool": "Bash",
    "input": "echo hi",
  }


def test_command_completed_emits_tool_output_and_end():
  line = json.dumps({
    "type": "item.completed",
    "item": {
      "id": "item_0", "type": "command_execution",
      "command": "/bin/bash -lc 'echo hi'",
      "aggregated_output": "hi\n", "exit_code": 0,
      "status": "completed",
    },
  })
  result = provider.parse_line(line)
  assert isinstance(result, list)
  assert result[0] == {"type": "tool_output", "content": "hi"}
  assert result[1] == {"type": "tool_end"}


def test_turn_completed_emits_done():
  line = json.dumps({
    "type": "turn.completed",
    "usage": {
      "input_tokens": 100, "cached_input_tokens": 80,
      "output_tokens": 10, "reasoning_output_tokens": 0,
    },
  })
  result = provider.parse_line(line)
  assert result["type"] == "done"
  assert result["cost_usd"] == 0


def test_turn_started_returns_none():
  line = '{"type":"turn.started"}'
  result = provider.parse_line(line)
  assert result is None


def test_invalid_json_returns_none():
  result = provider.parse_line("not json")
  assert result is None
