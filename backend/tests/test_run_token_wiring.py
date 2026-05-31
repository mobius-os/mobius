"""Task 4: per-turn run_token allocation + dormant sink plumbing.

Milestone B introduces a process-scoped `run_token` allocated once per
turn and threaded into `_ChatEventSink`. The sink STORES the token but
does NOT yet route anything through the writer actor — this is
forward-plumbing only, so these tests assert the token is allocated,
distinct per turn, and parked on the sink without changing the existing
persist path.
"""

from unittest import mock

from app.chat import _ChatEventSink
from app.chat_writer import alloc_run_token


class _FakeBroadcast:
  """Minimal broadcast stub — the sink only calls publish/mark_completed."""

  def __init__(self):
    self.events: list = []

  def publish(self, event):
    self.events.append(event)

  def mark_completed(self):
    pass


def test_sink_stores_run_token_passed_at_construction():
  # The sink parks the per-turn token on `self.run_token` and stamps it
  # on every writer-actor command so the actor coalesces/fences this
  # turn's snapshots under (chat_id, run_token). C2 dropped the `db` arg
  # — the actor owns the session now.
  bc = _FakeBroadcast()
  token = alloc_run_token()
  sink = _ChatEventSink(bc, "chat-1", run_token=token)
  assert sink.run_token == token


def test_sink_run_token_defaults_when_omitted():
  # Backward-compatible construction: callers that don't pass a token
  # (e.g. legacy/test code) still get a working sink with run_token None.
  bc = _FakeBroadcast()
  sink = _ChatEventSink(bc, "chat-1")
  assert sink.run_token is None


def test_initial_and_continuation_turns_get_distinct_tokens():
  # The scheduler allocates one token per turn; an initial send and any
  # continuation must receive DISTINCT tokens. We capture the token each
  # sink is built with across two sequential turns and assert they differ.
  captured: list = []
  real_init = _ChatEventSink.__init__

  def spy_init(self, bc, chat_id, run_token=None):
    captured.append(run_token)
    real_init(self, bc, chat_id, run_token=run_token)

  bc = _FakeBroadcast()
  with mock.patch.object(_ChatEventSink, "__init__", spy_init):
    # Simulate two turns each allocating their own token and constructing
    # a sink with it (the exact pattern the scheduler + runner follow).
    for _ in range(2):
      turn_token = alloc_run_token()
      _ChatEventSink(bc, "chat-1", run_token=turn_token)

  assert len(captured) == 2
  assert captured[0] is not None and captured[1] is not None
  assert captured[0] != captured[1], (
    "initial and continuation turns must get distinct run_tokens"
  )
