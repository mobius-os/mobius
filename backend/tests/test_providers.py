"""Tests for provider event parsing (providers.py)."""

import json

from app.providers import ClaudeProvider


def _assistant_event(blocks):
  return json.dumps({"type": "assistant", "message": {"content": blocks}})


def _stream_event(content_block_start):
  return json.dumps({
    "type": "stream_event",
    "event": {
      "type": "content_block_start",
      "content_block": content_block_start,
    },
  })


provider = ClaudeProvider()


def test_ask_user_question_emits_question_event():
  line = _assistant_event([{
    "type": "tool_use",
    "name": "AskUserQuestion",
    "id": "toolu_1",
    "input": {
      "questions": [{
        "question": "Color?",
        "header": "Prefs",
        "multiSelect": False,
        "options": [
          {"label": "Red", "description": "warm"},
          {"label": "Blue", "description": "cool"},
        ],
      }],
    },
  }])
  result = provider.parse_line(line)
  assert len(result) == 1
  assert result[0]["type"] == "question"
  assert result[0]["questions"][0]["question"] == "Color?"


def test_ask_user_question_suppresses_tool_start():
  line = _stream_event({
    "type": "tool_use",
    "name": "AskUserQuestion",
    "id": "toolu_1",
    "input": {},
  })
  result = provider.parse_line(line)
  assert isinstance(result, list)
  assert result == []


def test_normal_tool_emits_tool_start():
  line = _stream_event({
    "type": "tool_use",
    "name": "Bash",
    "id": "toolu_2",
    "input": {},
  })
  result = provider.parse_line(line)
  assert isinstance(result, list)
  assert result[0]["type"] == "tool_start"
  assert result[0]["tool"] == "Bash"


def test_normal_tool_emits_tool_input():
  line = _assistant_event([{
    "type": "tool_use",
    "name": "Bash",
    "id": "toolu_3",
    "input": {"command": "ls -la"},
  }])
  result = provider.parse_line(line)
  assert len(result) == 1
  assert result[0]["type"] == "tool_input"
  assert result[0]["tool"] == "Bash"


def test_mixed_tools_separate_correctly():
  line = _assistant_event([
    {
      "type": "tool_use",
      "name": "AskUserQuestion",
      "id": "toolu_1",
      "input": {
        "questions": [{"question": "Name?", "options": [
          {"label": "A"}, {"label": "B"},
        ]}],
      },
    },
    {
      "type": "tool_use",
      "name": "Bash",
      "id": "toolu_2",
      "input": {"command": "echo hi"},
    },
  ])
  result = provider.parse_line(line)
  assert len(result) == 2
  assert result[0]["type"] == "question"
  assert result[1]["type"] == "tool_input"


def test_partial_ask_user_question_empty_questions_skipped():
  """Partial assistant events with empty questions array are skipped.

  --include-partial-messages causes the CLI to emit intermediate
  assistant events before the tool input is fully assembled.  The
  first partial has questions: [] or missing question text — it
  must not produce a question event (which would render an empty
  QuestionCard).
  """
  # Empty questions array.
  line = _assistant_event([{
    "type": "tool_use",
    "name": "AskUserQuestion",
    "id": "toolu_1",
    "input": {"questions": []},
  }])
  result = provider.parse_line(line)
  assert isinstance(result, list)
  assert result == []

  # Questions present but question text missing.
  line = _assistant_event([{
    "type": "tool_use",
    "name": "AskUserQuestion",
    "id": "toolu_1",
    "input": {"questions": [{"options": [{"label": "A"}]}]},
  }])
  result = provider.parse_line(line)
  assert isinstance(result, list)
  assert result == []

  # No input at all (earliest partial).
  line = _assistant_event([{
    "type": "tool_use",
    "name": "AskUserQuestion",
    "id": "toolu_1",
    "input": {},
  }])
  result = provider.parse_line(line)
  assert isinstance(result, list)
  assert result == []


def test_complete_ask_user_question_still_emits():
  """A complete AskUserQuestion (with question text) still emits."""
  line = _assistant_event([{
    "type": "tool_use",
    "name": "AskUserQuestion",
    "id": "toolu_1",
    "input": {
      "questions": [{
        "question": "Pick a color",
        "options": [{"label": "Red"}, {"label": "Blue"}],
      }],
    },
  }])
  result = provider.parse_line(line)
  assert len(result) == 1
  assert result[0]["type"] == "question"
  assert result[0]["questions"][0]["question"] == "Pick a color"
