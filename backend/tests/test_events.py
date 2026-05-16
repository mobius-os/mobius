"""Tests for event processing (events.py)."""

from app.events import process_event, build_assistant_message, finalize_blocks


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
  """Question after a text block appends (not coalesces)."""
  blocks = [{"type": "text", "content": "hello"}]
  process_event({"type": "question", "questions": [{"question": "Q?"}]}, blocks)
  assert len(blocks) == 2
  assert blocks[0]["type"] == "text"
  assert blocks[1]["type"] == "question"


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
