"""Memory recall citations: identification, parsing, and survival on read.

The behaviour under test is what lets an owner tell three states apart —
the turn recalled these notes / it looked and Memory had nothing / it never
looked. The third is the absence of a citation, so the tests below care as
much about what is NOT stamped as about what is.
"""

import json

from app.chat_transcript import (
  _compact_activity_item,
  _compact_activity_run,
  _distinctive_activity,
  compact_messages_for_detail,
  legacy_memory_recall_output_ids,
  project_legacy_memory_recalls,
)
from app.chat import _ChatEventSink
from app.events import process_event
from app.memory_recall import (
  MAX_RECALL_NOTES,
  RECALL_EMPTY,
  RECALL_FAILED,
  RECALL_HIT,
  RECALL_SEARCHING,
  recall_from_command,
  recall_from_result,
  recall_from_tool_block,
)

MEMORY_CMD = 'python3 /data/apps/memory/memory_search.py "what does he prefer" "chat-1"'
WRAPPED_MEMORY_CMD = (
  "/bin/bash -lc 'python3 /data/apps/memory/memory_search.py "
  '"what does he prefer" "$CHAT_ID"\''
)

# Synthetic notes. Fixtures here become a public diff, so they must never carry
# anything from a real owner's graph — a memory note is personal by definition.
HIT_OUTPUT = """Relevant memories:
- Apps render in a sandboxed frame: Each mini-app runs isolated. [notes/apps-render-in-a-sandboxed-frame.md]
- Theme variables are shared: Colors come from one stylesheet. [notes/theme-variables-are-shared.md]
FILES: notes/apps-render-in-a-sandboxed-frame.md, notes/theme-variables-are-shared.md
MOBIUS_MEMORY_RESULT_V1:{"status":"hit","notes":[{"id":"apps-render-in-a-sandboxed-frame","path":"notes/apps-render-in-a-sandboxed-frame.md","title":"Apps render in a sandboxed frame","excerpt":"Each mini-app runs isolated."},{"id":"theme-variables-are-shared","path":"notes/theme-variables-are-shared.md","title":"Theme variables are shared","excerpt":"Colors come from one stylesheet."}]}"""
EMPTY_OUTPUT = """No relevant memories.
MOBIUS_MEMORY_RESULT_V1:{"status":"empty"}"""
FAILED_OUTPUT = """Memory lookup failed.
MOBIUS_MEMORY_RESULT_V1:{"status":"failed"}"""


# --- identification -------------------------------------------------------

def test_a_memory_search_command_is_identified_as_a_lookup():
  assert recall_from_command(MEMORY_CMD) == {
    "status": RECALL_SEARCHING,
    "app_slug": "memory",
    "query": "what does he prefer",
  }


def test_a_command_merely_mentioning_memory_search_is_not_a_lookup():
  # Identification gates everything downstream, so a false positive here would
  # mint citations from an unrelated command's output.
  # Every one of these is an ordinary thing to do WHILE working on Memory, and
  # each names the script without running it.
  assert recall_from_command("grep -rn memory_search.py /data/platform") is None
  assert recall_from_command("cat /data/apps/memory/memory_search.py") is None
  assert recall_from_command("wc -l memory_search.py") is None
  assert recall_from_command("ls -la /data/apps/memory/memory_search.py") is None
  assert recall_from_command("vim memory_search.py") is None
  assert recall_from_command("python3 -m py_compile app/memory_search.py") is None
  assert recall_from_command("echo memory_search.python") is None
  assert recall_from_command("ls /data/apps/memory/") is None
  assert recall_from_command("") is None
  assert recall_from_command(None) is None


def test_the_documented_simple_invocation_is_recognized():
  assert recall_from_command(MEMORY_CMD) is not None
  assert recall_from_command(
    'MEMORY_READER_PROVIDER=none python3 -u '
    '/data/apps/memory-2/memory_search.py "q" "chat-1"'
  ) == {
    "status": RECALL_SEARCHING,
    "app_slug": "memory-2",
    "query": "q",
  }


def test_codex_login_shell_wrapper_preserves_the_same_lookup_identity():
  assert recall_from_command(WRAPPED_MEMORY_CMD) == {
    "status": RECALL_SEARCHING,
    "app_slug": "memory",
    "query": "what does he prefer",
  }


def test_shell_composition_and_non_memory_paths_are_rejected_conservatively():
  assert recall_from_command(
    'python3 /data/apps/memory/memory_search.py "q"'
  ) is None
  assert recall_from_command(
    'cd /x && python3 /data/apps/memory/memory_search.py "q"'
  ) is None
  assert recall_from_command('python3 ./memory_search.py "q"') is None
  assert recall_from_command('python3 /a/b/memory_search.py "q"') is None
  assert recall_from_command(
    'python3 /data/apps/memory/memory_search.py "q" > /tmp/result'
  ) is None
  assert recall_from_command(
    "/bin/bash -lc 'python3 /data/apps/memory/memory_search.py "
    '"q" "$CHAT_ID" && printf forged\''
  ) is None


def test_trailing_arguments_and_newline_commands_cannot_mint_recall_metadata():
  assert recall_from_command(MEMORY_CMD + ' "unexpected"') is None
  assert recall_from_command(
    MEMORY_CMD + '\nprintf \'MOBIUS_MEMORY_RESULT_V1:{"status":"hit"}\\n\''
  ) is None


# --- parsing --------------------------------------------------------------

def test_a_successful_lookup_cites_the_notes_it_opened():
  recall = recall_from_result(HIT_OUTPUT, 0)
  assert recall["status"] == RECALL_HIT
  assert [note["id"] for note in recall["notes"]] == [
    "apps-render-in-a-sandboxed-frame", "theme-variables-are-shared",
  ]
  assert recall["notes"][0]["title"] == "Apps render in a sandboxed frame"
  assert recall["notes"][0]["excerpt"] == "Each mini-app runs isolated."


def test_a_citation_keeps_the_graph_node_id_when_it_differs_from_the_file():
  recall = recall_from_result(
    'MOBIUS_MEMORY_RESULT_V1:{"status":"hit","notes":['
    '{"id":"canonical-node","path":"notes/readable-filename.md",'
    '"title":"Canonical node"}]}',
    0,
  )

  assert recall["notes"] == [{
    "id": "canonical-node",
    "path": "notes/readable-filename.md",
    "title": "Canonical node",
  }]


def test_an_unsafe_or_missing_graph_node_id_falls_back_to_the_file_stem():
  recall = recall_from_result(
    'MOBIUS_MEMORY_RESULT_V1:{"status":"hit","notes":['
    '{"id":"../escape","path":"notes/safe-fallback.md"},'
    '{"path":"notes/legacy-note.md"}]}',
    0,
  )

  assert [note["id"] for note in recall["notes"]] == [
    "safe-fallback", "legacy-note",
  ]


def test_a_lookup_that_found_nothing_says_so():
  assert recall_from_result(EMPTY_OUTPUT, 0) == {"status": RECALL_EMPTY}


def test_a_carved_output_keeps_the_structured_tail_result():
  carved = "presentation head\n…[large middle carved]…\n" + HIT_OUTPUT.splitlines()[-1]
  recall = recall_from_result(carved, 0)
  assert [note["id"] for note in recall["notes"]] == [
    "apps-render-in-a-sandboxed-frame", "theme-variables-are-shared",
  ]


def test_unreadable_or_failed_results_are_explicit_failures():
  for body in ("", "   ", "some unrelated text", "Traceback (most recent call last):"):
    assert recall_from_result(body, 0) == {"status": RECALL_FAILED}
  assert recall_from_result(FAILED_OUTPUT, 1) == {"status": RECALL_FAILED}
  assert recall_from_result(HIT_OUTPUT, 1) == {"status": RECALL_FAILED}


def test_a_citation_path_may_not_escape_the_graph():
  recall = recall_from_result(
    'MOBIUS_MEMORY_RESULT_V1:{"status":"hit","notes":['
    '{"path":"../../etc/passwd"},{"path":"/abs/x.md"},'
    '{"path":"notes/../secret.md"},{"path":"notes/ok.md"}]}',
    0,
  )
  assert [note["path"] for note in recall["notes"]] == ["notes/ok.md"]


def test_repeated_and_excessive_citations_are_bounded():
  notes = [{"path": "notes/dup.md"}, {"path": "notes/dup.md"}] + [
    {"path": f"notes/n{i}.md"} for i in range(40)
  ]
  recall = recall_from_result(
    "MOBIUS_MEMORY_RESULT_V1:" + json.dumps({"status": "hit", "notes": notes}),
    0,
  )
  assert len(recall["notes"]) == MAX_RECALL_NOTES
  assert recall["notes"][0]["path"] == "notes/dup.md"
  assert len({note["path"] for note in recall["notes"]}) == len(recall["notes"])


def test_the_last_structured_result_line_wins():
  recall = recall_from_result(
    'MOBIUS_MEMORY_RESULT_V1:{"status":"hit","notes":[{"path":"notes/stale.md"}]}\n'
    'MOBIUS_MEMORY_RESULT_V1:{"status":"hit","notes":[{"path":"notes/real.md"}]}',
    0,
  )
  assert [note["path"] for note in recall["notes"]] == ["notes/real.md"]


# --- the block carries it through persistence ------------------------------

def _tool_blocks(recall_in, recall_out, output=HIT_OUTPUT, recall_on_start=False):
  blocks: list = []
  start = {"type": "tool_start", "tool": "Bash", "input": MEMORY_CMD,
           "tool_use_id": "t1"}
  if recall_on_start and recall_in is not None:
    start["recall"] = recall_in
  process_event(start, blocks)
  event_in = {"type": "tool_input", "tool_use_id": "t1", "input": MEMORY_CMD}
  if not recall_on_start and recall_in is not None:
    event_in["recall"] = recall_in
  process_event(event_in, blocks)
  event_out = {"type": "tool_output", "tool_use_id": "t1", "content": output}
  if recall_out is not None:
    event_out["recall"] = recall_out
  process_event(event_out, blocks)
  return blocks


def test_the_lookup_marker_reaches_the_persisted_block_and_then_settles():
  blocks = _tool_blocks(
    {"status": RECALL_SEARCHING},
    {"status": RECALL_HIT, "notes": [{"id": "a", "path": "notes/a.md", "title": "A"}]},
  )
  assert blocks[0]["recall"]["status"] == RECALL_HIT
  assert blocks[0]["recall"]["notes"][0]["id"] == "a"


def test_codex_tool_start_carries_the_lookup_marker_without_tool_input():
  blocks = _tool_blocks(
    {"status": RECALL_SEARCHING},
    {"status": RECALL_HIT, "notes": [{"id": "a", "path": "notes/a.md"}]},
    recall_on_start=True,
  )
  assert blocks[0]["recall"]["status"] == RECALL_HIT


def test_the_claude_path_does_not_double_stamp_a_single_lookup():
  # Simulate a runner that supplies the memory_search command on BOTH the
  # tool_start and a following tool_input for the same tool_use_id. The block
  # must be stamped once: the second phase sees the block already carries a
  # recall marker and is skipped, so no duplicate/overwriting stamp occurs.
  sink = object.__new__(_ChatEventSink)
  sink.assistant_blocks = []
  start = {"type": "tool_start", "tool": "Bash", "input": MEMORY_CMD,
           "tool_use_id": "t1"}
  sink._stamp_memory_recall(start)
  process_event(start, sink.assistant_blocks)
  assert sink.assistant_blocks[0]["recall"]["status"] == RECALL_SEARCHING
  follow = {"type": "tool_input", "tool_use_id": "t1", "input": MEMORY_CMD}
  sink._stamp_memory_recall(follow)
  assert "recall" not in follow
  process_event(follow, sink.assistant_blocks)
  assert sink.assistant_blocks[0]["recall"]["status"] == RECALL_SEARCHING


def test_partial_output_does_not_settle_the_lookup_before_completion():
  blocks: list = []
  process_event({
    "type": "tool_start", "tool": "Bash", "input": MEMORY_CMD,
    "tool_use_id": "t1", "recall": {"status": RECALL_SEARCHING},
  }, blocks)
  process_event({"type": "tool_output", "tool_use_id": "t1", "content": "partial"}, blocks)
  assert blocks[0]["recall"]["status"] == RECALL_SEARCHING
  process_event({
    "type": "tool_output", "tool_use_id": "t1", "content": HIT_OUTPUT,
    "recall": {"status": RECALL_HIT, "notes": [{"id": "a", "path": "notes/a.md"}]},
  }, blocks)
  assert blocks[0]["recall"]["status"] == RECALL_HIT


def _sink_lifecycle(events):
  sink = object.__new__(_ChatEventSink)
  sink.assistant_blocks = []
  for event in events:
    sink._stamp_memory_recall(event)
    process_event(event, sink.assistant_blocks)
  return sink.assistant_blocks[0]["recall"]


def test_claude_and_codex_lifecycles_settle_to_identical_recall_metadata():
  final = {
    "type": "tool_output", "tool_use_id": "t1", "content": HIT_OUTPUT,
    "output_complete": True, "output_exit_code": 0,
  }
  codex = _sink_lifecycle([
    {"type": "tool_start", "tool": "Bash", "input": MEMORY_CMD,
     "tool_use_id": "t1"},
    {"type": "tool_output", "tool_use_id": "t1", "content": "partial"},
    dict(final),
  ])
  claude = _sink_lifecycle([
    {"type": "tool_start", "tool": "Bash", "input": "",
     "tool_use_id": "t1"},
    {"type": "tool_input", "input": MEMORY_CMD, "tool_use_id": "t1"},
    dict(final),
  ])
  assert codex == claude
  assert codex["status"] == RECALL_HIT
  assert codex["query"] == "what does he prefer"
  assert codex["app_slug"] == "memory"
  assert [note["id"] for note in codex["notes"]] == [
    "apps-render-in-a-sandboxed-frame", "theme-variables-are-shared",
  ]
  assert {note["app_slug"] for note in codex["notes"]} == {"memory"}


def test_an_ordinary_command_gains_no_recall_field():
  blocks = _tool_blocks(None, None, output="total 0\n")
  assert "recall" not in blocks[0]


# --- survival through the read-side projection -----------------------------

def test_consulting_memory_is_its_own_activity_beat():
  assert _distinctive_activity({"type": "tool", "tool": "Bash",
                                "recall": {"status": RECALL_HIT, "notes": []}})
  assert not _distinctive_activity({"type": "tool", "tool": "Bash"})


def test_a_failed_lookup_remains_an_activity_without_citations():
  assert _distinctive_activity({
    "type": "tool", "tool": "Bash", "recall": {"status": RECALL_FAILED},
  })


def test_the_compacted_line_still_knows_what_it_recalled():
  # Without this the beat renders live and reverts to "Ran a command" on the
  # next chat load, which is worse for trust than never having shown it.
  item = _compact_activity_item({
    "type": "tool", "tool": "Bash", "status": "done",
    "input": MEMORY_CMD,
    "recall": {"status": RECALL_HIT, "notes": [{"id": "a", "path": "notes/a.md"}]},
  })
  assert item["recall"]["notes"][0]["id"] == "a"


def test_legacy_codex_block_recovers_its_question_and_results_on_read():
  block = {
    "type": "tool",
    "tool": "Bash",
    "status": "done",
    "input": WRAPPED_MEMORY_CMD,
    "output": HIT_OUTPUT,
    "output_exit_code": 0,
  }
  recall = recall_from_tool_block(block)
  assert recall["status"] == RECALL_HIT
  assert recall["query"] == "what does he prefer"
  assert recall["app_slug"] == "memory"
  assert _distinctive_activity(block)

  item = _compact_activity_item(block)
  assert item["recall"] == recall

  projected = compact_messages_for_detail(
    [{"role": "assistant", "blocks": [block]}],
    message_offset=0,
  )
  assert projected[0]["blocks"][0]["recall"] == recall
  assert "recall" not in block, "read projection never rewrites stored history"


def test_a_deferred_legacy_output_is_unknown_instead_of_a_false_failure():
  block = {
    "type": "tool",
    "tool": "Bash",
    "status": "done",
    "input": WRAPPED_MEMORY_CMD,
    "tool_use_id": "legacy-memory",
    "output_truncated": True,
    "output_exit_code": 0,
  }

  assert recall_from_tool_block(block) is None
  assert not _distinctive_activity(block)


def test_a_legacy_sidecar_tail_recovers_the_real_memory_result_before_compaction():
  block = {
    "type": "tool",
    "tool": "Bash",
    "status": "done",
    "input": WRAPPED_MEMORY_CMD,
    "tool_use_id": "legacy-memory",
    "output_truncated": True,
    "output_exit_code": 0,
  }
  messages = [{"role": "assistant", "blocks": [block]}]

  assert legacy_memory_recall_output_ids(messages) == {"legacy-memory"}
  recovered = project_legacy_memory_recalls(
    messages,
    output_tails={"legacy-memory": HIT_OUTPUT},
  )
  recall = recovered[0]["blocks"][0]["recall"]
  assert recall["status"] == RECALL_HIT
  assert recall["query"] == "what does he prefer"
  assert [note["id"] for note in recall["notes"]] == [
    "apps-render-in-a-sandboxed-frame", "theme-variables-are-shared",
  ]
  assert "recall" not in block, "sidecar recovery never rewrites stored history"

  compact = compact_messages_for_detail(recovered, message_offset=0)
  assert compact[0]["blocks"][0]["recall"] == recall


def test_a_real_legacy_process_error_remains_visible_without_stdout():
  block = {
    "type": "tool",
    "tool": "Bash",
    "status": "done",
    "input": WRAPPED_MEMORY_CMD,
    "output_exit_code": 1,
  }

  assert recall_from_tool_block(block)["status"] == RECALL_FAILED


def test_a_run_with_no_lookup_carries_no_recall_key():
  blocks = [(0, {"type": "tool", "tool": "Bash", "status": "done"})]
  assert "recall" not in _compact_activity_run(blocks, message_index=0)
