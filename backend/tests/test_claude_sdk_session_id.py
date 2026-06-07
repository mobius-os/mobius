"""Tests for Claude session-id robustness: phantom rejection + resumable.

Two failure modes break `claude --resume <Chat.session_id>`, and these
tests lock in the fixes:

1. PHANTOM session ids. The codex plugin's SessionStart hook emits a
   `HookEventMessage` (a `SystemMessage` subclass) carrying a session
   id that gets a `session-env/<id>` dir but never a transcript
   `.jsonl`. The runner's early-persist is type-gated so only real
   conversation messages (StreamEvent/Assistant/User/Result) advance
   `Chat.session_id` — the phantom is never persisted.

2. MISSING transcript. A correctly-stored id can still have no
   transcript (CLI ~30-day cleanup, or a pre-fix phantom already on an
   existing chat). `_resumable` detects that so chat.py can reseed from
   the DB transcript instead of letting `--resume` hard-fail.

Pure unit tests: no live SDK, no Docker. The phantom test drives
`run_claude_sdk_turn` through the same `ClaudeSDKClient` monkeypatch
seam the existing runner tests use, with a fake client that yields a
hand-built message sequence.
"""

from __future__ import annotations

import asyncio

from claude_agent_sdk.types import (
  HookEventMessage,
  ResultMessage,
  StreamEvent,
)

from app import claude_sdk_runner
from app.claude_sdk_runner import _resumable, run_claude_sdk_turn
from app.database import SessionLocal


class _ChatBus:
  """Minimal ChatBroadcast stand-in (publish-only)."""

  chat_id = "chat-phantom"
  run_token = "run-1"

  def __init__(self) -> None:
    self.events: list[dict] = []

  def publish(self, event: dict) -> None:
    self.events.append(event)


def _real_stream() -> StreamEvent:
  return StreamEvent(
    uuid="evt-real",
    session_id="REAL",
    event={
      "type": "content_block_delta",
      "delta": {"type": "text_delta", "text": "working"},
    },
  )


def _real_result() -> ResultMessage:
  return ResultMessage(
    subtype="success",
    duration_ms=10,
    duration_api_ms=8,
    is_error=False,
    num_turns=1,
    session_id="REAL",
    stop_reason="end_turn",
    total_cost_usd=0.01,
    usage={"input_tokens": 1, "output_tokens": 1},
  )


def _phantom_hook() -> HookEventMessage:
  """A SessionStart HookEventMessage carrying a phantom session id.

  This is the exact message the codex plugin emits on a resumed turn:
  a SystemMessage subclass with a `session_id` that never gets a
  transcript `.jsonl`.
  """
  return HookEventMessage(
    subtype="hook_event",
    data={"hook_event_name": "SessionStart"},
    hook_event_name="SessionStart",
    session_id="PHANTOM",
  )


def test_phantom_session_id_never_persisted(monkeypatch):
  """The phantom hook id is never persisted; REAL is the final id.

  Drives the runner with [HookEventMessage(PHANTOM), StreamEvent(REAL),
  ResultMessage(REAL)] and records every PersistSessionId the runner
  submits. The early-persist type-gate must skip the SystemMessage so
  PHANTOM is never written, and REAL is the persisted + returned id.
  """
  persisted: list[str] = []

  async def _record_persist(db, chat_id, session_id):
    del db, chat_id
    persisted.append(session_id)

  monkeypatch.setattr(
    claude_sdk_runner, "_persist_session_id", _record_persist
  )

  class _FakeClient:
    def __init__(self, options):
      del options

    async def connect(self):
      return None

    async def query(self, message):
      del message

    async def disconnect(self):
      return None

    async def receive_response(self):
      yield _phantom_hook()
      yield _real_stream()
      yield _real_result()

  monkeypatch.setattr(claude_sdk_runner, "ClaudeSDKClient", _FakeClient)

  db = SessionLocal()
  try:
    result = asyncio.run(
      run_claude_sdk_turn(
        "hello",
        session_id=None,
        base_env={},
        cwd="/tmp",
        chat_id="chat-phantom",
        skill_text="system",
        bc=_ChatBus(),
        pending_questions={},
        db=db,
      )
    )

    # The phantom id is never persisted; only REAL is.
    assert "PHANTOM" not in persisted
    assert persisted == ["REAL"]
    # The returned session id is the resumable one.
    assert result["session_id"] == "REAL"
  finally:
    db.close()


def test_resumable_true_when_transcript_present(tmp_path):
  """A stored id with its `<id>.jsonl` under projects/-data resumes."""
  proj = tmp_path / "projects" / "-data"
  proj.mkdir(parents=True)
  (proj / "abc123.jsonl").write_text("{}\n")
  assert _resumable("abc123", "/data", str(tmp_path)) is True


def test_resumable_false_when_transcript_missing(tmp_path):
  """A stored id with no transcript file is not resumable."""
  (tmp_path / "projects" / "-data").mkdir(parents=True)
  assert _resumable("abc123", "/data", str(tmp_path)) is False


def test_resumable_false_for_empty_inputs(tmp_path):
  """No id and no config dir both short-circuit to False."""
  assert _resumable(None, "/data", str(tmp_path)) is False
  assert _resumable("abc123", "/data", "") is False


def test_resumable_cwd_encoding_nested(tmp_path):
  """A nested cwd encodes to its dashed project dir (news-2 case)."""
  proj = tmp_path / "projects" / "-data-apps-news-2"
  proj.mkdir(parents=True)
  (proj / "sid.jsonl").write_text("{}\n")
  assert _resumable("sid", "/data/apps/news-2", str(tmp_path)) is True
  # Same id under the /data project dir must NOT match a /data/apps cwd.
  assert _resumable("sid", "/data", str(tmp_path)) is False


class _FakeChatRow:
  """Stand-in for a Chat ORM row carrying only `.messages`."""

  def __init__(self, messages):
    self.messages = messages


def test_resumed_context_block_round_trips_transcript():
  """The reseed block carries the chat's user/assistant turns in order.

  This is what chat.py prepends to `user_message` when the stored
  session has no resumable transcript — the agent continues a fresh
  session with its own prior conversation as context.
  """
  from app.chat import _build_resumed_context

  row = _FakeChatRow([
    {"role": "user", "content": "build me a notes app"},
    {"role": "assistant", "content": "Done — notes app is live."},
    {"role": "user", "content": "add tags"},
  ])
  block = _build_resumed_context(row)
  assert block is not None
  assert "<resumed_context>" in block and "</resumed_context>" in block
  # Order preserved, oldest-first, with speaker labels.
  i_first = block.index("build me a notes app")
  i_mid = block.index("Done — notes app is live.")
  i_last = block.index("add tags")
  assert i_first < i_mid < i_last
  assert "User: build me a notes app" in block
  assert "Assistant: Done" in block


def test_resumed_context_skips_non_conversation_rows():
  """Compaction/system rows and blank content are not reseeded."""
  from app.chat import _build_resumed_context

  row = _FakeChatRow([
    {"role": "system", "content": "ignored"},
    {"kind": "compaction", "role": "assistant", "content": "summary"},
    {"role": "user", "content": "real question"},
    {"role": "assistant", "content": ""},
  ])
  block = _build_resumed_context(row)
  assert block is not None
  assert "real question" in block
  assert "ignored" not in block


def test_resumed_context_none_when_empty():
  """A chat with no usable transcript yields no reseed block."""
  from app.chat import _build_resumed_context

  assert _build_resumed_context(_FakeChatRow([])) is None
  assert _build_resumed_context(None) is None


def test_resumed_context_truncates_to_budget():
  """A huge history is truncated to the most-recent budget of turns.

  Oldest turns drop first so the block can't blow the context window;
  the most recent turn always survives.
  """
  from app.chat import _RESUME_CONTEXT_CHAR_BUDGET, _build_resumed_context

  big = "x" * 4000
  msgs = [{"role": "user", "content": f"{i} {big}"} for i in range(20)]
  msgs.append({"role": "user", "content": "MOST_RECENT marker"})
  block = _build_resumed_context(_FakeChatRow(msgs))
  assert block is not None
  assert "MOST_RECENT marker" in block
  # The oldest turn was dropped, and the block respects the budget
  # (plus the fixed wrapper prose).
  assert "0 xxxx" not in block
  assert len(block) < _RESUME_CONTEXT_CHAR_BUDGET + 2000
