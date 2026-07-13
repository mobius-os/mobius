"""The platform-owned turn-end chat-summary publisher and parse helpers."""

import importlib.util
import json
import os
import sqlite3
import types
from pathlib import Path

import pytest

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


def test_fires_on_stop_handoff(tmp_path):
  # A Stop that no fresh claim raced past is a chat truly at rest — often the
  # day's last touch — so the guarantee fires there too.
  assert chat._should_ensure_chat_note(
    _settings(on=True), "c1",
    chat_queue.TerminalDisposition.STOP_HANDOFF_CLEARED,
    str(tmp_path), note_mtime_before=0.0,
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


def test_fires_on_limit_parked(tmp_path):
  # The parked response is the chat's final durable state until a later
  # resume. It must be summarized immediately, without retrying the exhausted
  # provider.
  assert chat._should_ensure_chat_note(
    _settings(on=True), "c1",
    chat_queue.TerminalDisposition.LIMIT_PARKED,
    str(tmp_path), 0.0,
  )


@pytest.mark.asyncio
async def test_limit_publisher_forces_provider_free_summary(monkeypatch):
  captured = {}

  class Proc:
    returncode = 0

    async def communicate(self):
      return b"", b""

  async def spawn(*args, **kwargs):
    captured.update(kwargs)
    return Proc()

  monkeypatch.setattr(chat.asyncio, "create_subprocess_exec", spawn)
  await chat._ensure_chat_note("/tmp/data", "c1", deterministic=True)
  assert captured["env"]["CHAT_NOTE_PROVIDER"] == "deterministic"


def test_still_fires_if_a_legacy_writer_touched_the_note(tmp_path):
  # A legacy agent/tool write cannot take ownership away from the platform.
  # chat_note.py snapshots that content and publishes with its durable CAS.
  before = chat._chat_note_mtime(str(tmp_path), "c1")  # 0.0 — absent at start
  _note(tmp_path, "c1")
  assert chat._chat_note_mtime(str(tmp_path), "c1") > before
  assert chat._should_ensure_chat_note(
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
  good = (
    "---\ntype: chat\ndescription: x\n---\n"
    "## Digest\nshort\n\n## Summary\nbody"
  )
  assert cn._looks_like_note(good)
  assert cn._looks_like_note("  \n" + good)  # leading whitespace tolerated
  assert not cn._looks_like_note("Sure! Here is the note: ...")
  assert not cn._looks_like_note("---\ntype: chat\n---\nno summary header")


def test_build_prompt_includes_existing_note_to_grow():
  cn = _load_chat_note()
  p = cn._build_prompt("user: hi", "---\n## Summary\nold")
  assert "user: hi" in p
  # The contract: a growing summary, lightly curated — grow + dedupe; noise is
  # what gets trimmed, never informative content.
  assert "grow it" in p.lower()
  assert "dedupe" in p.lower()
  assert "old" in p


def test_render_transcript_keeps_visible_blocks_and_excludes_tool_secrets():
  cn = _load_chat_note()
  raw = json.dumps([
    {"role": "user", "content": "Keep the early requirement"},
    {
      "role": "assistant",
      "content": "",
      "blocks": [
        {
          "type": "question",
          "questions": [{"question": "Which color?"}],
          "answers": {"Which color?": "Blue"},
        },
        {"type": "error", "message": "The preview failed"},
        {"type": "tool", "input": "token=SECRET", "output": "SECRET"},
        {"type": "thinking", "content": "SECRET"},
      ],
    },
  ])

  rendered = cn._render_transcript(raw)

  assert "Keep the early requirement" in rendered
  assert "Question: Which color?" in rendered
  assert "Answer to Which color?: Blue" in rendered
  assert "Error: The preview failed" in rendered
  assert "SECRET" not in rendered


def test_claude_summary_prompt_receives_complete_transcript(monkeypatch):
  cn = _load_chat_note()
  monkeypatch.setattr(cn, "_configured_provider", lambda: "claude")
  captured = {}
  valid = (
    "---\ntype: chat\ndescription: long chat\n---\n"
    "## Digest\ncomplete\n\n## Summary\ncomplete\n\n"
    "## Facts & intent\n- intent: test"
  )

  def fake_run(cmd, **_kwargs):
    captured["prompt"] = cmd[cmd.index("-p") + 1]
    return types.SimpleNamespace(stdout=valid, stderr="", returncode=0)

  monkeypatch.setattr(cn.subprocess, "run", fake_run)
  early = "EARLY-CONTEXT-MARKER"
  transcript = early + ("x" * 20_000) + "LATE-CONTEXT-MARKER"

  assert cn._looks_like_note(cn._summarize(transcript, ""))
  assert early in captured["prompt"]
  assert "LATE-CONTEXT-MARKER" in captured["prompt"]


def test_clean_note_output_keeps_a_clean_note_intact():
  cn = _load_chat_note()
  note = (
    "---\ntype: chat\ndescription: a chat\n---\n"
    "## Summary\nbody\n\n## Facts & intent\n- intent: x"
  )
  assert cn._clean_note_output(note) == note


def test_clean_note_output_trims_phantom_turn_and_repeat():
  cn = _load_chat_note()
  # Exactly the prod cruft: a hallucinated Human: turn + a repeated note block.
  raw = (
    "---\ntype: chat\ndescription: capital trivia\n---\n"
    "## Digest\nCapital questions.\n\n"
    "## Summary\nThe user asked the capital of Japan.\n\n"
    "## Facts & intent\n- intent: quick lookup\n"
    "Human: In one word, what is the capital of France?\n\n"
    "---\ntype: chat\ndescription: capital trivia\n---\n"
    "## Digest\nrepeat\n\n## Summary\nrepeat"
  )
  cleaned = cn._clean_note_output(raw)
  assert "Human:" not in cleaned
  assert "repeat" not in cleaned
  assert cleaned.count("## Summary") == 1
  assert cleaned.endswith("- intent: quick lookup")
  assert cn._looks_like_note(cleaned)


def test_clean_note_output_preserves_human_label_inside_body():
  # A `Human:`-prefixed line in the MIDDLE of the note (a quoted log line) is
  # real content, not a hallucinated trailing turn — must survive.
  cn = _load_chat_note()
  note = (
    "---\ntype: chat\ndescription: support log\n---\n"
    "## Summary\nThe partner quoted a log line:\n"
    "Human: where did my data go\n"
    "and we traced it.\n\n## Facts & intent\n- intent: debug"
  )
  cleaned = cn._clean_note_output(note)
  assert "Human: where did my data go" in cleaned
  assert cleaned.endswith("- intent: debug")


def test_sync_title_only_patches_from_note_without_summarizing(tmp_path, monkeypatch):
  # --sync-title reads the note's gist and PATCHes the title, NO summarizer run.
  cn = _load_chat_note()
  mem = tmp_path / "shared" / "memory"
  monkeypatch.setattr(cn, "MEMORY_DIR", mem)
  patched = {}
  monkeypatch.setattr(cn, "_patch_title",
                      lambda cid, desc: patched.update(cid=cid, desc=desc))
  note = mem / "chats" / "c9" / "index.md"
  note.parent.mkdir(parents=True)
  note.write_text("---\ntype: chat\ndescription: building a brew timer\n---\n## Summary\nx")
  monkeypatch.setattr(cn.sys, "argv", ["chat_note.py", "c9", "--sync-title"])
  assert cn.run() == 0
  assert patched == {"cid": "c9", "desc": "building a brew timer"}
  # the note is untouched (the summarizer never ran)
  assert "building a brew timer" in note.read_text()


def test_sync_title_only_noop_when_note_absent(tmp_path, monkeypatch):
  cn = _load_chat_note()
  monkeypatch.setattr(cn, "MEMORY_DIR", tmp_path / "shared" / "memory")
  called = []
  monkeypatch.setattr(cn, "_patch_title", lambda *a: called.append(a))
  monkeypatch.setattr(cn.sys, "argv", ["chat_note.py", "nope", "--sync-title"])
  assert cn.run() == 0
  assert called == []


def test_dead_claude_falls_back_to_complete_local_note(tmp_path, monkeypatch):
  cn = _load_chat_note()
  monkeypatch.setattr(cn, "_configured_provider", lambda: "claude")
  junk = types.SimpleNamespace(
    stdout="no note here", stderr="Credit balance is too low", returncode=1
  )
  monkeypatch.setattr(cn.subprocess, "run", lambda *a, **k: junk)
  note = cn._summarize("user: hi\n\nassistant: hello", "")
  assert cn._looks_like_note(note)
  assert "user: hi" in note
  assert "assistant: hello" in note


def _snapshot_db(cn, tmp_path):
  db_path = tmp_path / "ultimate.db"
  con = sqlite3.connect(db_path)
  con.execute(
    "create table chats ("
    "id text primary key, messages text, updated_at text, "
    "run_status text, deleted_at text)"
  )
  con.execute("create table owner (provider text)")
  con.execute("insert into owner values ('codex')")
  con.execute(
    "insert into chats values (?, ?, ?, null, null)",
    (
      "c1",
      json.dumps([
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
      ]),
      "2026-07-13 10:00:00.000000",
    ),
  )
  con.commit()
  con.close()
  cn.DB = db_path
  cn.MEMORY_DIR = tmp_path / "memory"
  return db_path


def _valid_note(description="ours", summary="current"):
  return (
    f"---\ntype: chat\ndescription: {description}\n---\n"
    f"## Digest\n{summary}\n\n## Summary\n{summary}\n\n"
    "## Facts & intent\n- intent: test"
  )


def test_two_backstops_publish_only_one_revision(tmp_path):
  cn = _load_chat_note()
  _snapshot_db(cn, tmp_path)
  transcript, revision = cn._read_chat_snapshot("c1")
  note = cn._note_path("c1")
  _existing, note_revision = cn._read_note_snapshot(note)

  assert cn._publish_if_current(
    "c1", revision, note_revision, note, _valid_note("first"),
  )
  assert not cn._publish_if_current(
    "c1", revision, note_revision, note, _valid_note("stale second"),
  )
  assert "description: first" in note.read_text()


def test_new_turn_or_delete_makes_summary_publication_stale(tmp_path):
  cn = _load_chat_note()
  db_path = _snapshot_db(cn, tmp_path)
  _transcript, revision = cn._read_chat_snapshot("c1")
  note = cn._note_path("c1")
  _existing, note_revision = cn._read_note_snapshot(note)
  con = sqlite3.connect(db_path)
  con.execute(
    "update chats set run_status='running', updated_at=? where id='c1'",
    ("2026-07-13 10:00:01.000000",),
  )
  con.commit()
  con.close()

  assert not cn._publish_if_current(
    "c1", revision, note_revision, note, _valid_note("stale"),
  )
  assert not note.exists()

  con = sqlite3.connect(db_path)
  con.execute(
    "update chats set run_status=null, deleted_at='2026-07-13', "
    "updated_at=? where id='c1'",
    (revision,),
  )
  con.commit()
  con.close()
  assert not cn._publish_if_current(
    "c1", revision, note_revision, note, _valid_note("deleted"),
  )
  assert not note.exists()


def test_note_hash_cas_detects_same_mtime_replacement(tmp_path):
  cn = _load_chat_note()
  _snapshot_db(cn, tmp_path)
  _transcript, revision = cn._read_chat_snapshot("c1")
  note = cn._note_path("c1")
  note.parent.mkdir(parents=True)
  note.write_text(_valid_note("old"))
  timestamp = note.stat().st_mtime
  _old, note_revision = cn._read_note_snapshot(note)
  note.write_text(_valid_note("racer"))
  os.utime(note, (timestamp, timestamp))

  assert not cn._publish_if_current(
    "c1", revision, note_revision, note, _valid_note("stale"),
  )
  assert "description: racer" in note.read_text()


def test_note_write_failure_does_not_advance_chat_revision(
  tmp_path, monkeypatch,
):
  cn = _load_chat_note()
  db_path = _snapshot_db(cn, tmp_path)
  _transcript, revision = cn._read_chat_snapshot("c1")
  note = cn._note_path("c1")
  _old, note_revision = cn._read_note_snapshot(note)

  def fail_write(*args, **kwargs):
    raise OSError("disk full")

  monkeypatch.setattr(cn, "_atomic_write_text", fail_write)
  with pytest.raises(OSError, match="disk full"):
    cn._publish_if_current(
      "c1", revision, note_revision, note, _valid_note("never"),
    )
  con = sqlite3.connect(db_path)
  current = con.execute(
    "select updated_at from chats where id='c1'",
  ).fetchone()[0]
  con.close()
  assert current == revision


def test_clean_note_output_preserves_horizontal_rule_in_body():
  # A bare `---` horizontal rule in the body is NOT a repeated frontmatter
  # block (its next line isn't a frontmatter key) — content after it stays.
  cn = _load_chat_note()
  note = (
    "---\ntype: chat\ndescription: design notes\n---\n"
    "## Summary\nfirst part\n\n---\n\nsecond part\n\n"
    "## Facts & intent\n- intent: design"
  )
  cleaned = cn._clean_note_output(note)
  assert "second part" in cleaned
  assert "## Facts & intent" in cleaned
  assert cleaned.endswith("- intent: design")
