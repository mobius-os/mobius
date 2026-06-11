"""Per-chat read-trace persistence (memory_trace.py): write/merge semantics,
the injected-vs-explicit split, filename sanitization, fire-and-forget
guarantees, and bounded retention. Fully isolated via tmp_path."""

import json
import os
import time

from app import memory_trace


def _trace(tmp_path, chat_id):
  path = memory_trace._trace_path(tmp_path, chat_id)
  return json.loads(path.read_text(encoding="utf-8"))


# --- injected trace ------------------------------------------------------


def test_record_injected_writes_node_ids_not_paths(tmp_path):
  memory_trace.record_injected(
    tmp_path, "chat1", ["index.md", "notes/foo.md", "inbox.md"]
  )
  trace = _trace(tmp_path, "chat1")
  assert trace["chat_id"] == "chat1"
  # Paths map to graph node ids; inbox.md is a buffer, not a node.
  assert trace["nodes_injected"] == ["index", "foo"]
  assert trace["nodes_read"] == []
  assert trace["dates"] and trace["updated"]


def test_record_injected_merges_and_dedupes(tmp_path):
  memory_trace.record_injected(tmp_path, "c", ["notes/a.md", "notes/b.md"])
  memory_trace.record_injected(tmp_path, "c", ["notes/b.md", "notes/c.md"])
  trace = _trace(tmp_path, "c")
  assert trace["nodes_injected"] == ["a", "b", "c"]
  # Same-day double-write records the date once.
  assert len(trace["dates"]) == 1


def test_record_injected_skips_buffers_and_empty(tmp_path):
  # Nothing graph-shaped to record → no file at all.
  memory_trace.record_injected(tmp_path, "c", ["inbox.md", "recent-chats.md"])
  assert not memory_trace._trace_path(tmp_path, "c").exists()
  # An empty chat id is a no-op, not a crash.
  memory_trace.record_injected(tmp_path, "", ["notes/a.md"])
  d = memory_trace.trace_dir(tmp_path)
  assert not d.is_dir() or not list(d.glob("*.json"))


# --- explicit-read trace -------------------------------------------------


def test_record_note_read_merges_into_same_file(tmp_path):
  memory_trace.record_injected(tmp_path, "c", ["notes/a.md"])
  memory_trace.record_note_read(tmp_path, "c", "deep-note")
  memory_trace.record_note_read(tmp_path, "c", "deep-note")  # dedupes
  memory_trace.record_note_read(tmp_path, "c", "a-moc")
  trace = _trace(tmp_path, "c")
  # One file carries both signals, kept apart: injected vs dug-for.
  assert trace["nodes_injected"] == ["a"]
  assert trace["nodes_read"] == ["deep-note", "a-moc"]


def test_record_note_read_alone_initializes_both_fields(tmp_path):
  memory_trace.record_note_read(tmp_path, "c", "foo")
  trace = _trace(tmp_path, "c")
  assert trace["nodes_read"] == ["foo"]
  assert trace["nodes_injected"] == []


# --- robustness ----------------------------------------------------------


def test_chat_id_is_sanitized_for_filenames(tmp_path):
  memory_trace.record_note_read(tmp_path, "../../evil/../x", "n")
  files = list(memory_trace.trace_dir(tmp_path).glob("*.json"))
  assert len(files) == 1
  # The traversal characters are flattened; nothing escapes the dir.
  assert "/" not in files[0].stem and ".." not in files[0].stem
  assert json.loads(files[0].read_text())["nodes_read"] == ["n"]


def test_corrupt_trace_file_is_replaced_not_fatal(tmp_path):
  path = memory_trace._trace_path(tmp_path, "c")
  path.parent.mkdir(parents=True)
  path.write_text("{not json", encoding="utf-8")
  memory_trace.record_note_read(tmp_path, "c", "n")
  assert _trace(tmp_path, "c")["nodes_read"] == ["n"]


def test_record_is_fire_and_forget_on_unwritable_dir(tmp_path):
  # Occupy the trace-dir path with a FILE so mkdir/open must fail.
  (tmp_path / "shared" / "memory").mkdir(parents=True)
  (tmp_path / "shared" / "memory" / "read-trace").write_text("squat")
  memory_trace.record_injected(tmp_path, "c", ["notes/a.md"])  # no raise
  memory_trace.record_note_read(tmp_path, "c", "n")  # no raise


# --- bounded retention ---------------------------------------------------


def test_prune_traces_removes_only_old_files(tmp_path):
  memory_trace.record_note_read(tmp_path, "old", "n")
  memory_trace.record_note_read(tmp_path, "fresh", "n")
  old = memory_trace._trace_path(tmp_path, "old")
  stale = time.time() - 15 * 86400
  os.utime(old, (stale, stale))
  removed = memory_trace.prune_traces(tmp_path, max_age_days=14)
  assert removed == 1
  assert not old.exists()
  assert memory_trace._trace_path(tmp_path, "fresh").exists()


def test_prune_traces_tolerates_missing_dir(tmp_path):
  assert memory_trace.prune_traces(tmp_path) == 0
