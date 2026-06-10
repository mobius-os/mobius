"""Tests for the streaming P1 fixes.

Covers:
  P1 — Error turns with zero assistant blocks are persisted (finalize()
       synthesises an error block when _last_error is set and blocks are empty).
  P6 — broadcast.event_log is capped at _EVENT_LOG_MAX and adjacent
       text-delta events are coalesced in the log.
"""

import asyncio
import copy
import unittest.mock as mock

import pytest

from app.broadcast import ChatBroadcast, _EVENT_LOG_MAX


# ---------------------------------------------------------------------------
# P1 — error-turn persistence
# ---------------------------------------------------------------------------

class _FakeWriter:
  """Captures the last Finalize snapshot submitted, without any DB."""

  def __init__(self):
    from concurrent.futures import Future
    self._ack: Future | None = None
    self.submitted: list = []

  def submit(self, cmd):
    from concurrent.futures import Future
    from app.chat_writer import Finalize
    f = Future()
    f.set_result(True)
    if isinstance(cmd, Finalize):
      self.submitted.append(copy.deepcopy(cmd.snapshot))
    return f


class _FakeBroadcast:
  """Minimal broadcast stub — records published events."""
  def __init__(self):
    self.events: list = []

  def publish(self, event: dict) -> None:
    self.events.append(event)


def _make_sink(chat_id="chat1", run_token="tok1"):
  """Build a _ChatEventSink with a fake writer and broadcast."""
  from app import chat as chat_mod

  bc = _FakeBroadcast()
  sink = chat_mod._ChatEventSink(bc, chat_id=chat_id, run_token=run_token)
  return sink, bc


@pytest.fixture(autouse=True)
def _patch_writer(monkeypatch):
  """Replace get_writer() with a per-test _FakeWriter instance."""
  from app import chat as chat_mod
  writer = _FakeWriter()
  monkeypatch.setattr(chat_mod, "get_writer", lambda: writer)
  return writer


def test_finalize_noop_on_truly_empty_turn(_patch_writer):
  """finalize() is a no-op when neither blocks nor error were recorded."""
  sink, _ = _make_sink()
  asyncio.run(sink.finalize())
  assert _patch_writer.submitted == [], (
    "finalize() should not submit anything for an empty turn"
  )


def test_finalize_submits_blocks_normally(_patch_writer):
  """finalize() submits a snapshot containing the accumulated blocks."""
  sink, _ = _make_sink()
  sink.assistant_blocks = [{"type": "text", "text": "hello"}]
  asyncio.run(sink.finalize())
  assert len(_patch_writer.submitted) == 1
  blocks = _patch_writer.submitted[0].get("blocks") or []
  assert any(b.get("type") == "text" for b in blocks)


def test_finalize_synthesizes_error_block_when_blocks_empty(_patch_writer):
  """finalize() persists a synthetic error block when _last_error is set
  but assistant_blocks is empty at finalize time.

  The normal publish({"type":"error"}) path calls process_event which adds
  an error block to assistant_blocks — so finalize() picks it up normally.
  _last_error is a safety net for edge cases where blocks were reset (e.g.
  a split_for_steer failure that reverted blocks) but the error was still
  recorded. We simulate this by setting _last_error directly while leaving
  assistant_blocks empty.
  """
  sink, bc = _make_sink()
  # Directly set _last_error as if an error was published but blocks were
  # subsequently cleared (e.g. by split_for_steer revert or future codepath).
  sink._last_error = "Authentication failed."
  assert sink.assistant_blocks == [], "assistant_blocks must be empty for this test"

  asyncio.run(sink.finalize())

  assert len(_patch_writer.submitted) == 1, (
    "finalize() must submit a Finalize command even when blocks are empty "
    "if _last_error is set"
  )
  snapshot = _patch_writer.submitted[0]
  blocks = snapshot.get("blocks") or []
  assert any(
    b.get("type") == "error" and b.get("message") == "Authentication failed."
    for b in blocks
  ), f"synthesised snapshot must contain the error block; got {blocks}"


def test_publish_error_records_last_error(_patch_writer):
  """publish() records the error message in _last_error so finalize()
  can use it if needed."""
  sink, _ = _make_sink()
  sink.publish({"type": "error", "message": "Provider timeout."})
  assert sink._last_error == "Provider timeout."


def test_publish_error_also_populates_assistant_blocks(_patch_writer):
  """An error event via publish() also goes through process_event which
  appends the error block to assistant_blocks — so the normal finalize()
  path persists it without needing the _last_error fallback."""
  sink, _ = _make_sink()
  sink.publish({"type": "error", "message": "Rate limited."})
  assert any(
    b.get("type") == "error" for b in sink.assistant_blocks
  ), "process_event must add the error block to assistant_blocks"


def test_finalize_uses_blocks_not_last_error_when_both_present(_patch_writer):
  """When both assistant_blocks and _last_error are set, the real blocks
  are used (no synthetic block injected — the error was already processed
  into the block list by process_event via the PersistError path)."""
  sink, bc = _make_sink()
  sink.assistant_blocks = [{"type": "text", "text": "partial"}, {"type": "error", "message": "blip"}]
  sink._last_error = "blip"  # set as if publish() ran

  asyncio.run(sink.finalize())

  assert len(_patch_writer.submitted) == 1
  blocks = _patch_writer.submitted[0].get("blocks") or []
  # Must contain the real text block — synthetic-only path was NOT taken.
  assert any(b.get("type") == "text" for b in blocks), (
    "the real assistant_blocks must be used when non-empty"
  )


# ---------------------------------------------------------------------------
# P6 — event_log cap and text-delta coalescing
# ---------------------------------------------------------------------------

def test_event_log_capped_at_max():
  """event_log never grows beyond _EVENT_LOG_MAX entries."""
  bc = ChatBroadcast("cap-test")
  for i in range(_EVENT_LOG_MAX + 500):
    bc.publish({"type": "tool_end", "index": i})
  assert len(bc.event_log) <= _EVENT_LOG_MAX, (
    f"event_log grew to {len(bc.event_log)}, expected <= {_EVENT_LOG_MAX}"
  )


def test_event_log_drops_oldest_when_capped():
  """When the cap is hit the OLDEST (first) entry is dropped."""
  bc = ChatBroadcast("oldest-drop")
  # Fill to exactly the cap with distinct sentinel events.
  for i in range(_EVENT_LOG_MAX):
    bc.publish({"type": "tool_end", "seq": i})
  assert len(bc.event_log) == _EVENT_LOG_MAX
  # One more — the oldest (seq=0) must be gone, newest retained.
  bc.publish({"type": "tool_end", "seq": _EVENT_LOG_MAX})
  assert len(bc.event_log) == _EVENT_LOG_MAX
  seqs = [e["seq"] for e in bc.event_log]
  assert 0 not in seqs, "oldest entry (seq=0) should have been evicted"
  assert _EVENT_LOG_MAX in seqs, "newest entry must be present"


def test_text_delta_events_coalesced_in_log():
  """Adjacent text-delta events for the same block index are merged into
  one log entry. Subscribers still receive each delta individually."""
  bc = ChatBroadcast("coalesce-test")
  received: list = []
  catch_up, q = bc.subscribe()

  bc.publish({"type": "text_delta", "index": 0, "text": "foo"})
  bc.publish({"type": "text_delta", "index": 0, "text": "bar"})
  bc.publish({"type": "text_delta", "index": 0, "text": "baz"})

  # Log has one coalesced entry.
  assert len(bc.event_log) == 1
  assert bc.event_log[0]["text"] == "foobarbaz"

  # But subscribers received all three deltas individually.
  while not q.empty():
    received.append(q.get_nowait())
  assert len(received) == 3
  assert received[0]["text"] == "foo"
  assert received[1]["text"] == "bar"
  assert received[2]["text"] == "baz"


def test_text_delta_coalesce_boundary_resets_on_different_index():
  """Coalescing does not merge text-deltas for different block indexes."""
  bc = ChatBroadcast("boundary-test")
  bc.publish({"type": "text_delta", "index": 0, "text": "A"})
  bc.publish({"type": "text_delta", "index": 1, "text": "B"})
  bc.publish({"type": "text_delta", "index": 0, "text": "C"})

  # Second delta is for a different index — new log entry; third is for
  # index 0 again but the run was broken by index 1 — another new entry.
  assert len(bc.event_log) == 3


def test_text_delta_coalesce_boundary_resets_on_non_delta():
  """A non-text-delta event breaks the coalesce run."""
  bc = ChatBroadcast("non-delta-break")
  bc.publish({"type": "text_delta", "index": 0, "text": "X"})
  bc.publish({"type": "tool_start", "index": 1})
  bc.publish({"type": "text_delta", "index": 0, "text": "Y"})

  assert len(bc.event_log) == 3


def test_subscribe_catch_up_after_coalesce():
  """A late subscriber's catch-up includes the coalesced log entry with
  the merged text — semantically equivalent to all the raw deltas."""
  bc = ChatBroadcast("catch-up-coalesce")
  for chunk in ("hello ", "world"):
    bc.publish({"type": "text_delta", "index": 0, "text": chunk})
  # Subscribe AFTER the deltas.
  catch_up, _ = bc.subscribe()
  assert len(catch_up) == 1
  assert catch_up[0]["text"] == "hello world"
