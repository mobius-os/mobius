"""Tests for event processing (events.py)."""

from typing import get_args

from app.chat import _ChatEventSink
from app.events import (
  EventType,
  build_assistant_message,
  finalize_blocks,
  process_event,
)


def test_text_event_creates_block():
  blocks = []
  changed = process_event({"type": "text", "content": "hello"}, blocks)
  assert changed
  assert blocks == [{"type": "text", "content": "hello"}]


def test_text_events_concatenate():
  blocks = []
  process_event({"type": "text", "content": "hello "}, blocks)
  process_event({"type": "text", "content": "world"}, blocks)
  assert len(blocks) == 1
  assert blocks[0]["content"] == "hello world"


def test_tool_start_creates_block():
  blocks = []
  process_event({"type": "tool_start", "tool": "Bash", "input": "ls"}, blocks)
  assert blocks[0] == {
    "type": "tool", "tool": "Bash", "input": "ls",
    "output": "", "status": "running",
  }


def test_tool_end_marks_done():
  blocks = [{"type": "tool", "tool": "Bash", "input": "ls",
             "output": "", "status": "running"}]
  process_event({"type": "tool_end"}, blocks)
  assert blocks[0]["status"] == "done"


def test_tool_output_fills_last_running():
  blocks = [
    {"type": "tool", "tool": "Bash", "input": "ls",
     "output": "", "status": "done"},
    {"type": "tool", "tool": "Read", "input": "file.py",
     "output": "", "status": "running"},
  ]
  process_event({"type": "tool_output", "content": "file contents"}, blocks)
  assert blocks[0]["output"] == ""
  assert blocks[1]["output"] == "file contents"


def test_build_assistant_message():
  blocks = [
    {"type": "text", "content": "Here is the result:"},
    {"type": "tool", "tool": "Bash", "input": "ls",
     "output": "file.py", "status": "done"},
    {"type": "text", "content": "\nDone."},
  ]
  msg = build_assistant_message(blocks)
  assert msg["role"] == "assistant"
  assert msg["content"] == "Here is the result:\nDone."
  assert msg["blocks"] == blocks


def test_finalize_blocks_completes_running_tools():
  blocks = [
    {"type": "tool", "tool": "Bash", "input": "ls",
     "output": "", "status": "running"},
    {"type": "text", "content": "partial"},
  ]
  finalize_blocks(blocks)
  assert blocks[0]["status"] == "done"


def test_question_event_creates_block():
  blocks = []
  questions = [
    {"question": "Color?", "header": "Prefs",
     "multiSelect": False, "options": [
       {"label": "Red", "description": "warm"},
       {"label": "Blue", "description": "cool"},
     ]},
  ]
  changed = process_event({"type": "question", "questions": questions}, blocks)
  assert changed
  assert blocks == [{"type": "question", "questions": questions}]


def test_question_coalesces_partial_then_full():
  """Partial question followed by full question replaces, not appends."""
  blocks = []
  partial = [{"question": "First?", "options": []}]
  full = [
    {"question": "First?", "options": [{"label": "A"}, {"label": "B"}]},
    {"question": "Second?", "options": [{"label": "X"}, {"label": "Y"}]},
  ]
  process_event({"type": "question", "questions": partial}, blocks)
  assert len(blocks) == 1
  assert len(blocks[0]["questions"]) == 1

  process_event({"type": "question", "questions": full}, blocks)
  assert len(blocks) == 1  # still one block, not two
  assert len(blocks[0]["questions"]) == 2
  assert blocks[0]["questions"][1]["question"] == "Second?"


def test_question_after_text_appends():
  """A brand-new question (no prior question block to match) appends."""
  blocks = [{"type": "text", "content": "hello"}]
  process_event({"type": "question", "questions": [{"question": "Q?"}]}, blocks)
  assert len(blocks) == 2
  assert blocks[0]["type"] == "text"
  assert blocks[1]["type"] == "question"


def test_question_partial_then_full_with_text_between_does_not_duplicate():
  """The user-visible duplicate-card bug: --include-partial-messages
  can deliver two partial events for the same AskUserQuestion call
  with a text token landing between them.  Dedup must match by
  identity (question id), not by 'is the last block a question'.
  """
  blocks = []
  partial = [{"id": "klix_scope", "question": "What change?", "options": []}]
  process_event({"type": "question", "questions": partial}, blocks)
  process_event({"type": "text", "content": "thinking..."}, blocks)
  full = [{
    "id": "klix_scope",
    "question": "What change?",
    "options": [{"label": "Fix"}, {"label": "Skip"}],
  }]
  process_event({"type": "question", "questions": full}, blocks)

  question_blocks = [b for b in blocks if b.get("type") == "question"]
  assert len(question_blocks) == 1, (
    f"expected one question block, got {len(question_blocks)}"
  )
  assert question_blocks[0]["questions"][0]["options"] == [
    {"label": "Fix"}, {"label": "Skip"},
  ]
  # Text block survives the coalesce, in its original position.
  assert any(b.get("type") == "text" for b in blocks)


def test_question_partial_then_full_matches_by_text_when_id_missing():
  """Fallback path: defensive runner that omits the SDK id still
  dedups by the first question's text.
  """
  blocks = []
  partial = [{"question": "Color?", "options": []}]
  process_event({"type": "question", "questions": partial}, blocks)
  process_event({"type": "tool_start", "tool": "Bash", "input": "ls"}, blocks)
  full = [{"question": "Color?", "options": [{"label": "Red"}]}]
  process_event({"type": "question", "questions": full}, blocks)

  question_blocks = [b for b in blocks if b.get("type") == "question"]
  assert len(question_blocks) == 1
  assert question_blocks[0]["questions"][0]["options"] == [{"label": "Red"}]


def test_question_different_ids_append_as_separate_blocks():
  """Two distinct AskUserQuestion calls (different ids) must remain
  separate blocks, even if a text block sits between them.
  """
  blocks = []
  q1 = [{"id": "scope", "question": "What change?", "options": []}]
  process_event({"type": "question", "questions": q1}, blocks)
  process_event({"type": "text", "content": "I see — next: "}, blocks)
  q2 = [{"id": "mode", "question": "Which mode?", "options": []}]
  process_event({"type": "question", "questions": q2}, blocks)

  question_blocks = [b for b in blocks if b.get("type") == "question"]
  assert len(question_blocks) == 2
  assert question_blocks[0]["questions"][0]["id"] == "scope"
  assert question_blocks[1]["questions"][0]["id"] == "mode"


def test_question_block_in_built_message():
  blocks = [
    {"type": "text", "content": "Let me ask:"},
    {"type": "question", "questions": [{"question": "Color?"}]},
  ]
  msg = build_assistant_message(blocks)
  assert msg["content"] == "Let me ask:"
  assert any(b["type"] == "question" for b in msg["blocks"])


def test_question_does_not_affect_tool_blocks():
  """A question event should not interfere with existing tool blocks."""
  blocks = [
    {"type": "tool", "tool": "Bash", "input": "ls",
     "output": "", "status": "running"},
  ]
  process_event(
    {"type": "question", "questions": [{"question": "Color?"}]},
    blocks,
  )
  assert len(blocks) == 2
  assert blocks[0]["status"] == "running"
  assert blocks[1]["type"] == "question"
  # tool_end still marks the running tool as done.
  process_event({"type": "tool_end"}, blocks)
  assert blocks[0]["status"] == "done"


def test_unknown_event_returns_false():
  blocks = []
  changed = process_event({"type": "unknown"}, blocks)
  assert not changed
  assert blocks == []


# --- error events (round-3 hardening) -------------------------------
# Error events must persist into the assistant transcript (so users
# see what went wrong on scroll-back), and consecutive errors must
# coalesce into one block (so a flaky run doesn't stack a wall of
# duplicate errors). The signature here is `process_event(event,
# blocks)` — not `(state, event)` — matching the rest of this file.


def test_process_error_event_appends_block():
  blocks = []
  changed = process_event({"type": "error", "message": "boom"}, blocks)
  assert changed
  assert any(
    b.get("type") == "error" and b.get("message") == "boom"
    for b in blocks
  )


def test_process_error_event_coalesces_duplicates():
  """Repeated error events collapse to one (no stacked blocks)."""
  blocks = [{"type": "text", "content": "partial"}]
  process_event({"type": "error", "message": "first"}, blocks)
  process_event({"type": "error", "message": "second"}, blocks)
  error_blocks = [b for b in blocks if b.get("type") == "error"]
  assert len(error_blocks) == 1
  assert error_blocks[0]["message"] == "second"
  # Text block preserved.
  assert any(b.get("type") == "text" for b in blocks)


def test_immediate_save_types_are_event_types():
  """_IMMEDIATE_SAVE_TYPES stays a subset of the exported vocabulary."""
  assert _ChatEventSink._IMMEDIATE_SAVE_TYPES <= set(get_args(EventType))
