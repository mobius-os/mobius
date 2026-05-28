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


def test_translate_drops_thread_status_changed():
  """thread/status/changed is intentionally suppressed (redundant with
  turn/completed). It must not surface as an unknown_sdk_event."""
  events = translate_notification({
    "method": "thread/status/changed",
    "params": {"status": "active"},
  })
  assert events == []


# ── named-event handlers added in PR 2B ────────────────────────────


def test_translate_item_started_reasoning_emits_thinking():
  events = translate_item_started({
    "type": "reasoning",
    "textDelta": "Thinking about the next step...",
  })
  assert events == [
    {"type": "thinking", "content": "Thinking about the next step..."},
  ]


def test_translate_item_started_empty_reasoning_is_silent():
  events = translate_item_started({"type": "reasoning"})
  assert events == []


def test_translate_token_usage_emits_usage_event():
  events = translate_notification({
    "method": "thread/tokenUsage/updated",
    "params": {
      "usage": {
        "input_tokens": 100,
        "output_tokens": 50,
        "cache_read_input_tokens": 25,
      },
    },
  })
  assert len(events) == 1
  assert events[0]["type"] == "usage"
  assert events[0]["input_tokens"] == 100
  assert events[0]["output_tokens"] == 50
  assert events[0]["cache_read_input_tokens"] == 25


def test_translate_rate_limit_event():
  events = translate_notification({
    "method": "account/rateLimits/updated",
    "params": {"rateLimits": {"status": "allowed_warning"}},
  })
  assert events[0]["type"] == "rate_limit"
  assert events[0]["status"] == "allowed_warning"


def test_translate_config_warning_as_warning_event():
  events = translate_notification({
    "method": "configWarning",
    "params": {"message": "missing config"},
  })
  assert events == [{
    "type": "warning",
    "source": "configWarning",
    "message": "missing config",
  }]


def test_translate_deprecation_notice_as_warning_event():
  events = translate_notification({
    "method": "deprecationNotice",
    "params": {"notice": "field X is deprecated"},
  })
  assert events[0]["type"] == "warning"
  assert events[0]["source"] == "deprecationNotice"
  assert "deprecated" in events[0]["message"]


def test_translate_mcp_startup_failure_emits_warning():
  events = translate_notification({
    "method": "mcpServer/startupStatus/updated",
    "params": {"status": "error", "error": "spawn EACCES"},
  })
  assert events[0]["type"] == "warning"
  assert "spawn EACCES" in events[0]["message"]


def test_translate_mcp_startup_healthy_is_silent():
  events = translate_notification({
    "method": "mcpServer/startupStatus/updated",
    "params": {"status": "ready"},
  })
  assert events == []


def test_translate_thread_compacted_emits_compaction_event():
  events = translate_notification({
    "method": "thread/compacted",
    "params": {"turn": 7},
  })
  assert events == [{"type": "compaction_event", "turn": 7}]


def test_translate_model_rerouted():
  events = translate_notification({
    "method": "model/rerouted",
    "params": {"reason": "tier overflow"},
  })
  assert events == [{"type": "model_rerouted", "reason": "tier overflow"}]


def test_translate_unknown_notification_emits_unknown_event(monkeypatch):
  """A method we don't recognize must surface as unknown_sdk_event."""
  monkeypatch.setenv("MOBIUS_EMIT_UNKNOWN", "1")
  events = translate_notification({
    "method": "totallyMadeUp/event",
    "params": {"hello": "world"},
  })
  assert len(events) == 1
  assert events[0]["type"] == "unknown_sdk_event"
  assert events[0]["kind"] == "totallyMadeUp/event"
  assert events[0]["raw"]["method"] == "totallyMadeUp/event"


def test_translate_unknown_notification_silent_when_disabled(monkeypatch):
  """MOBIUS_EMIT_UNKNOWN=0 suppresses the unknown emission."""
  monkeypatch.setenv("MOBIUS_EMIT_UNKNOWN", "0")
  events = translate_notification({
    "method": "totallyMadeUp/event",
    "params": {},
  })
  assert events == []


def test_translate_unknown_item_type_emits_unknown_event(monkeypatch):
  """A novel item/started type still surfaces (was silently dropped before)."""
  monkeypatch.setenv("MOBIUS_EMIT_UNKNOWN", "1")
  events = translate_item_started({"type": "plan", "title": "First steps"})
  assert len(events) == 1
  assert events[0]["type"] == "unknown_sdk_event"
  assert events[0]["kind"].startswith("item/started:")
