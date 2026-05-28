"""Tests for CodexProvider event parsing + the codex_appserver translator.

`parse_line` is now Codex-only: after the Claude chat path migrated
to the SDK, `ClaudeProvider.parse_line` no longer exists, so the
pass-through contract documented here applies solely to
`CodexProvider`. Lines arrive from `codex_appserver_runner.py`
already shaped as Möbius events. The underlying translation from
app-server JSON-RPC notifications lives in `app.codex_appserver`
and is exercised directly below.
"""

import json
from app.providers import CodexProvider

provider = CodexProvider()


def test_parse_line_passes_through_runner_event():
  line = json.dumps({"type": "text", "content": "Hello"})
  result = provider.parse_line(line)
  assert result == [{"type": "text", "content": "Hello"}]


def test_parse_line_drops_unrecognized_envelope():
  # Raw JSON-RPC notifications never reach the provider any more —
  # the runner script translates them upstream. Anything that
  # isn't already a Möbius event is dropped here.
  line = json.dumps({"method": "turn/started"})
  assert provider.parse_line(line) == []


def test_invalid_json_returns_none():
  result = provider.parse_line("not json")
  assert result == []


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
