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


# ── translator tests (codex_appserver module) ──────────────────────

from app.codex_appserver import (
  translate_item_started,
  translate_item_completed,
  translate_notification,
)


def test_translate_agent_message_delta_emits_text():
  events = translate_notification({
    "method": "item/agentMessage/delta",
    "params": {"delta": "Hello"},
  })
  assert events == [{"type": "text", "content": "Hello"}]


def test_translate_camelcase_command_execution():
  events = translate_item_completed({
    "type": "commandExecution",
    "command": "/bin/bash -lc 'ls /tmp'",
    "aggregated_output": "a.txt\nb.txt",
  })
  assert {"type": "tool_output", "content": "a.txt\nb.txt"} in events
  assert {"type": "tool_end"} in events


def test_translate_snakecase_alias_command_execution():
  """Legacy snake_case from codex exec --json must also work."""
  events = translate_item_completed({
    "type": "command_execution",
    "aggregated_output": "x",
  })
  assert {"type": "tool_output", "content": "x"} in events
  assert {"type": "tool_end"} in events


def test_translate_snakecase_alias_file_change_kind_path():
  """file_change (snake_case) emits per-change kind+path summary."""
  events = translate_item_completed({
    "type": "file_change",
    "changes": [
      {"kind": "add", "path": "/tmp/a"},
      {"kind": "update", "path": "/tmp/b"},
    ],
  })
  assert any(
    e.get("type") == "tool_output"
    and "add /tmp/a" in e.get("content", "")
    and "update /tmp/b" in e.get("content", "")
    for e in events
  )


def test_translate_snakecase_alias_web_search_query_backfill():
  """web_search query backfill works for snake_case alias too."""
  events = translate_item_completed({
    "type": "web_search",
    "query": "what is mobius",
  })
  assert {"type": "tool_input", "input": "what is mobius"} in events


def test_translate_thread_started_emits_session_init():
  events = translate_notification({
    "method": "thread/started",
    "params": {"thread": {"id": "abc-123"}},
  })
  assert events == [{"type": "session_init", "session_id": "abc-123"}]


def test_translate_turn_completed_emits_done():
  events = translate_notification({
    "method": "turn/completed",
    "params": {"turn": {}},
  })
  assert events[0]["type"] == "done"


def test_translate_drops_unknown_notification():
  events = translate_notification({
    "method": "thread/status/changed",
    "params": {"status": "active"},
  })
  assert events == []
