"""Tests for event processing (events.py)."""

from typing import get_args

from app.chat import _ChatEventSink
from app.events import (
  EventType,
  blocks_have_renderable_content,
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


def test_text_final_repairs_truncated_stream():
  # A dropped delta left the persisted text truncated ("I "); the authoritative
  # text_final replaces it with the complete text (not append — no doubling).
  blocks = []
  process_event({"type": "text", "content": "I "}, blocks)
  process_event({"type": "text_final", "content": "I am here."}, blocks)
  assert len(blocks) == 1
  assert blocks[0] == {"type": "text", "content": "I am here."}


def test_text_final_idempotent_when_nothing_lost():
  blocks = []
  process_event({"type": "text", "content": "hello world"}, blocks)
  changed = process_event(
    {"type": "text_final", "content": "hello world"}, blocks
  )
  assert changed is False
  assert blocks == [{"type": "text", "content": "hello world"}]


def test_text_final_replaces_only_trailing_text_item():
  # text, tool, text ordering — text_final for the second item replaces the
  # trailing text block, leaving the first text and the tool intact.
  blocks = []
  process_event({"type": "text", "content": "first"}, blocks)
  process_event({"type": "tool_start", "tool": "Bash", "input": "ls"}, blocks)
  process_event({"type": "text", "content": "seco"}, blocks)
  process_event({"type": "text_final", "content": "second"}, blocks)
  assert [b["type"] for b in blocks] == ["text", "tool", "text"]
  assert blocks[0]["content"] == "first"
  assert blocks[2]["content"] == "second"


def test_text_final_materialises_when_all_deltas_lost():
  # Every delta for the item was dropped, so there's no trailing text block —
  # the authoritative text is materialised as a new block rather than lost.
  blocks = []
  process_event({"type": "tool_start", "tool": "Bash", "input": "ls"}, blocks)
  process_event({"type": "tool_end"}, blocks)
  process_event({"type": "text_final", "content": "recovered text"}, blocks)
  assert blocks[-1] == {"type": "text", "content": "recovered text"}


def test_text_final_replaces_pending_boundary():
  # A text_boundary awaiting text, then text_final — the boundary becomes the
  # authoritative text block.
  blocks = [{"type": "text", "content": "a"}, {"type": "text_boundary"}]
  process_event({"type": "text_final", "content": "b"}, blocks)
  assert blocks == [
    {"type": "text", "content": "a"},
    {"type": "text", "content": "b"},
  ]


def test_text_final_empty_is_noop():
  blocks = [{"type": "text", "content": "kept"}]
  changed = process_event({"type": "text_final", "content": ""}, blocks)
  assert changed is False
  assert blocks == [{"type": "text", "content": "kept"}]


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


def test_skill_loaded_stamps_skill_onto_skill_tool_block():
  """A skill_loaded event stamps the skill name onto the most recent
  Skill tool block so the persisted transcript carries the chip data."""
  blocks = [
    {"type": "tool", "tool": "Skill", "input": "humanizer",
     "output": "", "status": "running"},
  ]
  changed = process_event(
    {"type": "skill_loaded", "skill": "humanizer"}, blocks,
  )
  assert changed
  assert blocks[0]["skill"] == "humanizer"


def test_skill_loaded_without_skill_block_is_noop():
  """No Skill tool block to attach to → no change, no crash."""
  blocks = [{"type": "tool", "tool": "Bash", "input": "ls",
             "output": "", "status": "done"}]
  changed = process_event(
    {"type": "skill_loaded", "skill": "humanizer"}, blocks,
  )
  assert changed is False
  assert "skill" not in blocks[0]


def test_skill_loaded_empty_name_is_noop():
  blocks = [{"type": "tool", "tool": "Skill", "input": "",
             "output": "", "status": "running"}]
  changed = process_event({"type": "skill_loaded", "skill": ""}, blocks)
  assert changed is False
  assert "skill" not in blocks[0]


def test_task_start_enriches_matching_task_block():
  """task_start stamps a running subagent entry on the Task tool block matched
  by tool_use_id, in the frozen {task_id: {description, status, summary}} shape
  (card 247)."""
  blocks = [
    {"type": "tool", "tool": "Task", "input": "review the diff",
     "output": "", "status": "running", "tool_use_id": "toolu_1"},
  ]
  changed = process_event({
    "type": "task_start", "task_id": "task_A", "tool_use_id": "toolu_1",
    "description": "Review the diff for races", "task_type": "general",
  }, blocks)
  assert changed
  assert blocks[0]["subagent"] == {
    "task_A": {
      "description": "Review the diff for races",
      "status": "running",
      "summary": None,
    },
  }


def test_task_done_updates_status_and_summary_on_same_block():
  """task_done flips the entry to its terminal status and lands the summary,
  keeping the description task_start set."""
  blocks = [
    {"type": "tool", "tool": "Task", "input": "review the diff",
     "output": "", "status": "running", "tool_use_id": "toolu_1"},
  ]
  process_event({
    "type": "task_start", "task_id": "task_A", "tool_use_id": "toolu_1",
    "description": "Review the diff for races",
  }, blocks)
  changed = process_event({
    "type": "task_done", "task_id": "task_A", "tool_use_id": "toolu_1",
    "status": "done", "summary": "Found one race in the queue drain.",
  }, blocks)
  assert changed
  assert blocks[0]["subagent"]["task_A"] == {
    "description": "Review the diff for races",
    "status": "done",
    "summary": "Found one race in the queue drain.",
  }


def test_task_done_killed_status_persists():
  """A task stopped via TaskStop reports "killed"; the terminal status persists
  verbatim (not normalized to done)."""
  blocks = [
    {"type": "tool", "tool": "Task", "input": "long job",
     "output": "", "status": "running", "tool_use_id": "toolu_9"},
  ]
  process_event({
    "type": "task_start", "task_id": "task_K", "tool_use_id": "toolu_9",
    "description": "Long job",
  }, blocks)
  changed = process_event({
    "type": "task_done", "task_id": "task_K", "tool_use_id": "toolu_9",
    "status": "killed", "summary": None,
  }, blocks)
  assert changed
  assert blocks[0]["subagent"]["task_K"]["status"] == "killed"
  assert blocks[0]["subagent"]["task_K"]["summary"] is None


def test_task_done_without_prior_start_still_records_terminal_entry():
  """task_done arriving with no matching task_start (start dropped) still
  materializes a full frozen-shape entry so the chip is not left half-built."""
  blocks = [
    {"type": "tool", "tool": "Task", "input": "job",
     "output": "", "status": "running", "tool_use_id": "toolu_5"},
  ]
  changed = process_event({
    "type": "task_done", "task_id": "task_B", "tool_use_id": "toolu_5",
    "status": "failed", "summary": "boom",
  }, blocks)
  assert changed
  assert blocks[0]["subagent"]["task_B"] == {
    "description": "",
    "status": "failed",
    "summary": "boom",
  }


def test_task_event_unknown_tool_use_id_is_noop():
  """No tool block matches the tool_use_id → no enrichment, no phantom block."""
  blocks = [
    {"type": "tool", "tool": "Task", "input": "job",
     "output": "", "status": "running", "tool_use_id": "toolu_1"},
  ]
  changed = process_event({
    "type": "task_start", "task_id": "task_X", "tool_use_id": "toolu_MISSING",
    "description": "orphan",
  }, blocks)
  assert changed is False
  assert "subagent" not in blocks[0]
  assert len(blocks) == 1


def test_task_progress_is_live_only_no_persist():
  """task_progress never persists — it returns False so no transcript save is
  triggered and it stamps nothing on the block."""
  blocks = [
    {"type": "tool", "tool": "Task", "input": "job",
     "output": "", "status": "running", "tool_use_id": "toolu_1"},
  ]
  changed = process_event({
    "type": "task_progress", "task_id": "task_A", "tool_use_id": "toolu_1",
    "last_tool_name": "Read", "usage": {"input_tokens": 10},
  }, blocks)
  assert changed is False
  assert "subagent" not in blocks[0]


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


def test_tool_sources_attach_to_most_recent_websearch_tool():
  blocks = [
    {"type": "tool", "tool": "WebSearch", "input": "old",
     "output": "", "status": "done"},
    {"type": "tool", "tool": "WebSearch", "input": "new",
     "output": "", "status": "running"},
  ]
  sources = [{
    "title": "Example",
    "url": "https://example.com/page",
    "snippet": "Result text",
  }]

  changed = process_event(
    {"type": "tool_sources", "sources": sources}, blocks,
  )

  assert changed
  assert "sources" not in blocks[0]
  assert blocks[1]["sources"] == sources


def test_tool_sources_noop_without_matching_block_or_sources():
  blocks = [{"type": "tool", "tool": "Bash", "input": "ls",
             "output": "", "status": "running"}]

  assert process_event({"type": "tool_sources", "sources": []}, blocks) is False
  assert process_event(
    {"type": "tool_sources", "sources": [{"url": "https://e.com"}]},
    blocks,
  ) is False
  assert "sources" not in blocks[0]


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


def test_build_assistant_message_separates_distinct_text_blocks():
  blocks = [
    {"type": "text", "content": "I reverted the setting."},
    {"type": "text", "content": "Yes, the app is back."},
  ]
  msg = build_assistant_message(blocks)
  assert msg["content"] == "I reverted the setting.\n\nYes, the app is back."


def test_text_deltas_are_concatenated_exactly():
  blocks = []
  process_event({"type": "text", "content": "I reverted it."}, blocks)
  process_event({"type": "text", "content": "Yes, it works."}, blocks)
  assert blocks[0]["content"] == "I reverted it.Yes, it works."

  blocks = []
  process_event({"type": "text", "content": "Use `Foo."}, blocks)
  process_event({"type": "text", "content": "Bar`, U.S."}, blocks)
  process_event({"type": "text", "content": "A., or API:GET /v1."}, blocks)
  assert blocks[0]["content"] == "Use `Foo.Bar`, U.S.A., or API:GET /v1."


def test_thinking_events_coalesce_with_duration():
  blocks = []
  process_event({"type": "thinking", "content": "plan ", "ts": 1000}, blocks)
  process_event({"type": "thinking", "content": "then act", "ts": 2450}, blocks)

  assert len(blocks) == 1
  assert blocks[0]["type"] == "thinking"
  assert blocks[0]["content"] == "plan then act"
  assert blocks[0]["duration_ms"] == 1450

  msg = build_assistant_message(blocks)
  assert msg["content"] == ""
  assert msg["blocks"] == [{
    "type": "thinking",
    "content": "plan then act",
    "duration_ms": 1450,
  }]
  assert "_thinking_start_ts" in blocks[0]
  assert "_thinking_start_ts" not in msg["blocks"][0]


def test_thinking_segment_identity_preserves_semantic_paragraphs():
  blocks = []
  process_event({
    "type": "thinking", "content": "**Planning ", "ts": 1000,
    "segment_id": "summary:0",
  }, blocks)
  process_event({
    "type": "thinking", "content": "the fix**", "ts": 1100,
    "segment_id": "summary:0",
  }, blocks)
  process_event({
    "type": "thinking", "content": "**Writing tests**", "ts": 1200,
    "segment_id": "summary:1",
  }, blocks)

  assert blocks[0]["content"] == (
    "**Planning the fix**\n\n**Writing tests**"
  )
  assert blocks[0]["_thinking_segment_id"] == "summary:1"
  persisted = build_assistant_message(blocks)["blocks"][0]
  assert persisted["content"] == blocks[0]["content"]
  assert "_thinking_segment_id" not in persisted


def test_thinking_event_after_tool_starts_fresh_block():
  blocks = []
  process_event({"type": "thinking", "content": "first", "ts": 1000}, blocks)
  process_event({"type": "tool_start", "tool": "Bash", "input": "ls"}, blocks)
  process_event({"type": "thinking", "content": "second", "ts": 2000}, blocks)

  thinking_blocks = [b for b in blocks if b.get("type") == "thinking"]
  assert len(thinking_blocks) == 2
  assert [b["content"] for b in thinking_blocks] == ["first", "second"]
  assert [b["duration_ms"] for b in thinking_blocks] == [0, 0]


def test_thinking_survives_interleaved_unknown_event():
  # A provider `ping` heartbeat is forwarded as an "unknown_sdk_event" and lands
  # BETWEEN two thinking_delta chunks (it can even split mid-word). It must NOT
  # close the thinking run, else one reasoning pass fragments into many tiny
  # "Thought for 1 second" blocks. Regression guard for that exact bug.
  blocks = []
  process_event({"type": "thinking", "content": "The sl", "ts": 1000}, blocks)
  process_event(
    {"type": "unknown_sdk_event", "kind": "stream:ping", "raw": {}}, blocks
  )
  process_event({"type": "thinking", "content": "iders move", "ts": 2200}, blocks)

  assert len(blocks) == 1
  assert blocks[0]["type"] == "thinking"
  assert blocks[0]["content"] == "The sliders move"
  assert blocks[0]["duration_ms"] == 1200


def test_thinking_survives_interleaved_usage_and_signature():
  # The full bookkeeping set is transparent to thinking coalescing: a `usage`
  # event and a signature-style unknown_sdk_event between thinking chunks still
  # yield one block. Only a real new content block (text/tool_start/…) splits it.
  blocks = []
  process_event({"type": "thinking", "content": "a", "ts": 1000}, blocks)
  process_event({"type": "usage", "input_tokens": 5, "output_tokens": 7}, blocks)
  process_event(
    {"type": "unknown_sdk_event",
     "kind": "stream:content_block_delta:signature_delta", "raw": {}},
    blocks,
  )
  process_event({"type": "thinking", "content": "b", "ts": 1600}, blocks)

  thinking_blocks = [b for b in blocks if b.get("type") == "thinking"]
  assert len(thinking_blocks) == 1
  assert thinking_blocks[0]["content"] == "ab"
  assert thinking_blocks[0]["duration_ms"] == 600


def test_thinking_split_by_text_boundary_then_text():
  # The interrupting set is COMPLETE, not over-broad: a real text item between
  # two thinking passes still splits them (thinking, text, thinking).
  blocks = []
  process_event({"type": "thinking", "content": "before", "ts": 1000}, blocks)
  process_event({"type": "text_boundary"}, blocks)
  process_event({"type": "text", "content": "answer"}, blocks)
  process_event({"type": "thinking", "content": "after", "ts": 2000}, blocks)

  assert [b["type"] for b in blocks] == ["thinking", "text", "thinking"]
  assert [b["content"] for b in blocks] == ["before", "answer", "after"]


def test_finalize_blocks_keeps_thinking_and_strips_transients():
  blocks = []
  process_event({"type": "thinking", "content": "a", "ts": 1000}, blocks)
  process_event({"type": "thinking", "content": "b", "ts": 1600}, blocks)

  finalize_blocks(blocks)

  assert blocks == [{
    "type": "thinking",
    "content": "ab",
    "duration_ms": 600,
  }]


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


def test_process_error_event_carries_whitelisted_extras_on_append():
  """The park/resume extras ride the error event onto the persisted block.

  Item 4 could only mark notes resumable at boot reconcile because this path
  stripped everything but `message`; the whitelist passthrough makes the
  stalled/drain/limit notes live-resumable and the parked card live by
  construction (design §2.4).
  """
  blocks = []
  process_event({
    "type": "error",
    "message": "rate limited",
    "resumable": True,
    "pause": {"kind": "usage_limit", "resets_at": "2026-07-11T01:40:00+00:00"},
  }, blocks)
  err = next(b for b in blocks if b.get("type") == "error")
  assert err["resumable"] is True
  assert err["pause"] == {
    "kind": "usage_limit", "resets_at": "2026-07-11T01:40:00+00:00",
  }


def test_process_error_event_extras_are_whitelist_only():
  """An unexpected event key must NOT leak into the durable transcript."""
  blocks = []
  process_event({
    "type": "error",
    "message": "boom",
    "resumable": True,
    "surprise_key": "nope",
  }, blocks)
  err = next(b for b in blocks if b.get("type") == "error")
  assert err["resumable"] is True
  assert "surprise_key" not in err


def test_process_error_event_coalesce_latest_event_wins():
  """Coalescing is latest-event-wins for the whitelisted extras: a later
  error event REPLACES the block's extras wholesale — keys it omits are
  removed. A park error followed by a different terminal error must degrade
  to a plain error, not keep rendering a stale "resets at …" card for a
  park that never got scheduled (or was superseded)."""
  blocks = []
  process_event({
    "type": "error",
    "message": "rate limited",
    "resumable": True,
    "pause": {"kind": "usage_limit", "resets_at": "2026-07-11T01:40:00+00:00"},
  }, blocks)
  process_event({
    "type": "error", "message": "final text", "resumable": True,
  }, blocks)
  error_blocks = [b for b in blocks if b.get("type") == "error"]
  assert len(error_blocks) == 1
  assert error_blocks[0]["message"] == "final text"
  # The follow-up carried `resumable` and nothing else: pause descriptor gone.
  assert error_blocks[0]["resumable"] is True
  assert "pause" not in error_blocks[0]
  # A fully bare error strips everything whitelisted.
  process_event({"type": "error", "message": "bare"}, blocks)
  assert "resumable" not in error_blocks[0]
  # And a later event can re-establish extras it explicitly carries.
  process_event({
    "type": "error", "message": "again", "pause": {"kind": "rate_limit"},
  }, blocks)
  assert error_blocks[0]["pause"] == {"kind": "rate_limit"}
  assert "resumable" not in error_blocks[0]


def test_immediate_save_types_are_event_types():
  """_IMMEDIATE_SAVE_TYPES stays a subset of the exported vocabulary."""
  assert _ChatEventSink._IMMEDIATE_SAVE_TYPES <= set(get_args(EventType))


def test_both_runners_emit_text_boundary():
  """Both SDK runners must emit `text_boundary` so a stream with multiple
  assistant message items renders as separate paragraphs on EITHER provider.

  This guards the provider-asymmetric half-fix class: `text_boundary` shipped
  for the Codex runner but not the Claude runner for a while, so Claude text
  resuming after an AskUserQuestion glued together as "answer1.answer2". A
  source-level check is intentional — it catches the omission without driving
  each SDK end-to-end.
  """
  from pathlib import Path

  app_dir = Path(__file__).resolve().parents[1] / "app"
  for runner in ("claude_sdk_runner.py", "codex_sdk_runner.py"):
    src = (app_dir / runner).read_text()
    assert '"text_boundary"' in src, (
      f"{runner} never publishes a text_boundary event — consecutive "
      f"assistant message items will concatenate without a paragraph break "
      f"on this provider. Keep the streaming event vocabulary symmetric "
      f"across runners (see events.process_event)."
    )


def test_text_boundary_reducer_splits_consecutive_text():
  """A text_boundary between two text chunks yields TWO text blocks (so the
  post-AskUserQuestion 'answer1' / 'answer2' no longer glue as 'answer1answer2')."""
  blocks = []
  process_event({"type": "text", "content": "answer1"}, blocks)
  process_event({"type": "text_boundary"}, blocks)
  process_event({"type": "text", "content": "answer2"}, blocks)
  text_blocks = [b for b in blocks if b.get("type") == "text"]
  assert [b["content"] for b in text_blocks] == ["answer1", "answer2"]
  # the marker was consumed (replaced by the second text), not left behind
  assert all(b.get("type") != "text_boundary" for b in blocks)


def test_text_boundary_on_empty_is_noop():
  """A leading boundary (no prior non-empty text) does nothing — guards the
  first-block case so a turn never opens with a stray marker."""
  blocks = []
  changed = process_event({"type": "text_boundary"}, blocks)
  assert changed is False
  assert blocks == []


def test_text_boundary_marker_never_persists():
  """A DANGLING boundary (marker, no following text) is stripped from both the
  mid-turn snapshot (build_assistant_message) and the finalized blocks
  (finalize_blocks) — it is a live-stream-only signal, never a stored block."""
  blocks = []
  process_event({"type": "text", "content": "x"}, blocks)
  process_event({"type": "text_boundary"}, blocks)
  assert blocks[-1] == {"type": "text_boundary"}  # present mid-stream
  msg = build_assistant_message(blocks)
  assert all(b.get("type") != "text_boundary" for b in msg["blocks"])
  assert msg["content"] == "x"
  finalize_blocks(blocks)
  assert all(b.get("type") != "text_boundary" for b in blocks)


def test_blocks_have_renderable_content_empty_and_whitespace():
  """An empty/whitespace-only pre-steer segment is NOT renderable (card 166)."""
  assert blocks_have_renderable_content([]) is False
  assert blocks_have_renderable_content(None) is False
  assert blocks_have_renderable_content(
    [{"type": "text", "content": ""}]
  ) is False
  assert blocks_have_renderable_content(
    [{"type": "text", "content": "   "}]
  ) is False
  assert blocks_have_renderable_content(
    [{"type": "text", "content": "\n"}]
  ) is False
  # Only the internal boundary marker, no real text.
  assert blocks_have_renderable_content(
    [{"type": "text_boundary"}]
  ) is False


def test_blocks_have_renderable_content_real_token_and_blocks():
  """A single real token, or any non-text block, IS renderable."""
  assert blocks_have_renderable_content(
    [{"type": "text", "content": "I "}]
  ) is True
  assert blocks_have_renderable_content(
    [{"type": "text", "content": "I"}]
  ) is True
  assert blocks_have_renderable_content(
    [{"type": "tool", "tool": "Bash", "status": "running"}]
  ) is True
  # Whitespace text plus a real tool block still counts (the tool is real).
  assert blocks_have_renderable_content(
    [
      {"type": "text", "content": " "},
      {"type": "tool", "tool": "Bash", "status": "done"},
    ]
  ) is True
