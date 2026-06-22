"""The turn-end chat-note guarantee: the gate that decides whether the platform
writes a chat's memory note when the agent skipped it (chat.py), plus the
tool-free summarizer's parse helpers (scripts/chat_note.py)."""

import importlib.util
import types
from pathlib import Path

from app import chat, chat_queue


def _settings(on=True):
  return types.SimpleNamespace(ensure_chat_note=on)


def _note(tmp_path, chat_id="c1", body="x"):
  p = tmp_path / "shared" / "memory" / "chats" / chat_id / "index.md"
  p.parent.mkdir(parents=True, exist_ok=True)
  p.write_text(body)
  return p


# --- the gate -----------------------------------------------------------


def test_fires_when_settled_and_note_absent(tmp_path):
  # Agent skipped the note: it's absent before AND after, the chat settled.
  assert chat._should_ensure_chat_note(
    _settings(on=True), "c1",
    chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED,
    str(tmp_path), note_mtime_before=0.0,
  )


def test_skips_when_feature_off(tmp_path):
  assert not chat._should_ensure_chat_note(
    _settings(on=False), "c1",
    chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED,
    str(tmp_path), 0.0,
  )


def test_skips_without_chat_id(tmp_path):
  assert not chat._should_ensure_chat_note(
    _settings(on=True), "",
    chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED,
    str(tmp_path), 0.0,
  )


def test_skips_on_non_settled_dispositions(tmp_path):
  for d in (
    chat_queue.TerminalDisposition.CONTINUATION_PROMOTED,
    chat_queue.TerminalDisposition.FAILED_LEAVE_MARKER,
    chat_queue.TerminalDisposition.STALE_NO_ACTION,
  ):
    assert not chat._should_ensure_chat_note(
      _settings(on=True), "c1", d, str(tmp_path), 0.0
    ), d


def test_skips_when_agent_wrote_the_note_this_turn(tmp_path):
  # The note did not exist at turn START (before=0); the agent created it
  # during the turn → its mtime now advances past `before` → the platform must
  # NOT also write it.
  before = chat._chat_note_mtime(str(tmp_path), "c1")  # 0.0 — absent at start
  _note(tmp_path, "c1")  # the agent writes it this turn
  assert chat._chat_note_mtime(str(tmp_path), "c1") > before
  assert not chat._should_ensure_chat_note(
    _settings(on=True), "c1",
    chat_queue.TerminalDisposition.EMPTY_TERMINAL_CLEARED,
    str(tmp_path), note_mtime_before=before,
  )


def test_chat_note_mtime_missing_is_zero(tmp_path):
  assert chat._chat_note_mtime(str(tmp_path), "nope") == 0.0


# --- the summarizer's parse helpers ------------------------------------


def _load_chat_note():
  path = Path(__file__).resolve().parent.parent / "scripts" / "chat_note.py"
  spec = importlib.util.spec_from_file_location("chat_note", path)
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)
  return mod


def test_looks_like_note_accepts_valid_and_rejects_junk():
  cn = _load_chat_note()
  good = "---\ntype: chat\ndescription: x\n---\n## Summary\nbody"
  assert cn._looks_like_note(good)
  assert cn._looks_like_note("  \n" + good)  # leading whitespace tolerated
  assert not cn._looks_like_note("Sure! Here is the note: ...")
  assert not cn._looks_like_note("---\ntype: chat\n---\nno summary header")


def test_build_prompt_includes_existing_note_to_grow():
  cn = _load_chat_note()
  p = cn._build_prompt("user: hi", "---\n## Summary\nold")
  assert "user: hi" in p
  assert "grow this, never shrink" in p.lower()
  assert "old" in p
