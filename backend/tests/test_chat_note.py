"""The platform-owned turn-end chat-summary publisher and parse helpers."""

import importlib.util
import json
import os
import sqlite3
import types
from pathlib import Path

import pytest

from app import chat, chat_queue, models


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


def test_active_goal_checkpoint_reads_the_exact_current_run(
  client, owner_token, db,
):
  owner_auth = {"Authorization": f"Bearer {owner_token}"}
  chat_id = client.post(
    "/api/chats", json={"title": "Dynamic Goal"}, headers=owner_auth,
  ).json()["id"]
  run = models.ChatRun(
    id="dynamic-goal-run", root_run_id="dynamic-goal-run", chat_id=chat_id,
    status="running", provider="claude",
  )
  db.add(run)
  db.commit()

  assert not chat._run_owns_active_goal(
    db, chat_id=chat_id, run_token=run.id,
  )
  run.goal_objective = "Discover and repair every represented defect"
  db.commit()
  assert chat._run_owns_active_goal(
    db, chat_id=chat_id, run_token=run.id,
  )
  run.status = "completed"
  db.commit()
  assert not chat._run_owns_active_goal(
    db, chat_id=chat_id, run_token=run.id,
  )


def test_fires_on_limit_parked(tmp_path):
  # The parked response is the chat's final durable state until a later
  # resume. It must be summarized immediately, without retrying the exhausted
  # provider.
  assert chat._should_ensure_chat_note(
    _settings(on=True), "c1",
    chat_queue.TerminalDisposition.LIMIT_PARKED,
    str(tmp_path), 0.0,
  )


def test_fires_when_owner_question_is_parked(tmp_path):
  assert chat._should_ensure_chat_note(
    _settings(on=True), "c1",
    chat_queue.TerminalDisposition.QUESTION_PARKED,
    str(tmp_path), 0.0,
  )


def test_fires_on_provider_free_completion(tmp_path):
  assert chat._should_ensure_chat_note(
    _settings(on=True), "c1",
    chat_queue.TerminalDisposition.PROVIDER_FREE_COMPLETED,
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


@pytest.mark.asyncio
async def test_goal_checkpoint_publisher_uses_the_active_mode(monkeypatch):
  captured = {}

  class Proc:
    returncode = 0

    async def communicate(self):
      return b"", b""

  async def spawn(*args, **kwargs):
    captured["args"] = args
    return Proc()

  monkeypatch.setattr(chat.asyncio, "create_subprocess_exec", spawn)
  await chat._ensure_chat_note(
    "/tmp/data", "c1", active_goal_checkpoint=True,
  )
  assert captured["args"][-1] == "--active-goal-checkpoint"


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


def test_summary_name_policy_balances_first_turn_recency_and_stability():
  cn = _load_chat_note()
  prompt = " ".join(cn.SYSTEM_PROMPT.lower().split())
  assert (
    "on the first publication, replace the raw opening-message fallback"
    in prompt
  )
  assert "use sentence case" in prompt
  assert "title case" not in prompt
  assert (
    "capitalize the first word plus real proper nouns and product names"
    in prompt
  )
  assert "that formatting correction is not topic churn" in prompt
  assert "give recent work more weight" in prompt
  assert "keep the existing name through ordinary follow-up turns" in prompt
  assert "substantially moved to a different main topic" in prompt
  assert "current topic rather than the chat's opening topic" in prompt


def test_generated_chat_name_capitalizes_without_damaging_product_casing():
  cn = _load_chat_note()
  assert cn._normalize_chat_name("dialing in sour espresso") == (
    "Dialing in sour espresso"
  )
  assert cn._normalize_chat_name("iPhone camera workflow") == (
    "iPhone camera workflow"
  )
  assert cn._normalize_chat_name("  2026   planning notes ") == (
    "2026 Planning notes"
  )


def test_deterministic_note_preserves_an_existing_generated_name():
  cn = _load_chat_note()
  existing = (
    "---\ntype: chat\ndescription: Existing current topic\n---\n"
    "## Digest\nold\n\n## Summary\nold"
  )
  note = cn._deterministic_note(
    "user: unrelated raw prompt text\n\nassistant: completed",
    existing,
  )
  assert "description: Existing current topic" in note
  assert "description: unrelated raw prompt text" not in note


def test_deterministic_note_names_a_goal_without_its_command_marker():
  cn = _load_chat_note()
  note = cn._deterministic_note(
    "user: /goal review the complete feature\n\nassistant: working",
    "",
  )
  assert "description: review the complete feature" in note
  assert "description: /goal" not in note


def test_deterministic_note_preserves_summary_with_internal_h2():
  cn = _load_chat_note()
  existing = (
    "---\ntype: chat\ndescription: Useful name\n---\n"
    "## Digest\nold\n\n## Summary\nDecision\n\n## Design\nDetail\n\n"
    "## Facts & intent\n- intent: ship\n"
  )
  note = cn._deterministic_note("user: new evidence", existing)
  assert "Decision\n\n## Design\nDetail" in note
  assert "### Undistilled latest transcript\n\nuser: new evidence" in note


def test_read_transcript_excludes_derived_provider_handoffs(tmp_path):
  cn = _load_chat_note()
  database = tmp_path / "chat.db"
  con = sqlite3.connect(database)
  con.execute(
    "create table chats (id text primary key, messages text, "
    "updated_at text, provider text, deleted_at text)"
  )
  con.execute(
    "create table chat_runs ("
    "id text primary key, chat_id text, status text, started_at text)"
  )
  con.execute(
    "insert into chats (id, messages, updated_at, provider, deleted_at) "
    "values (?, ?, ?, 'codex', null)",
    ("c1", json.dumps([
      {"role": "user", "content": "original request"},
      {
        "role": "assistant", "kind": "compaction",
        "content": "derived handoff must not recurse",
      },
      {"role": "assistant", "content": "real response"},
    ]), "2026-07-13 10:00:00.000000"),
  )
  con.commit()
  con.close()
  cn.DB = database

  transcript = cn._read_chat_snapshot("c1").transcript
  assert "original request" in transcript
  assert "real response" in transcript
  assert "derived handoff" not in transcript


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


def test_render_transcript_labels_automatic_continuation_as_product_event():
  cn = _load_chat_note()
  raw = json.dumps([{
    "role": "user",
    "kind": "auto_continuation",
    "continuation_reason": "restart",
    "content": "continue",
  }])

  rendered = cn._render_transcript(raw)

  assert rendered == "automatic continuation (restart): continue"
  assert "user: continue" not in rendered


def test_render_transcript_excludes_hidden_product_control_messages():
  cn = _load_chat_note()
  raw = json.dumps([
    {"role": "user", "content": "Address every issue"},
    {
      "role": "user", "hidden": True,
      "content": "/goal Address every issue and verify the result",
    },
    {"role": "assistant", "content": "All checks pass."},
  ])

  rendered = cn._render_transcript(raw)

  assert "Address every issue" in rendered
  assert "All checks pass." in rendered
  assert "/goal" not in rendered


def test_claude_summary_prompt_receives_complete_transcript(monkeypatch):
  cn = _load_chat_note()
  monkeypatch.setattr(cn, "_configured_provider", lambda _provider=None: "claude")
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
  monkeypatch.setattr(cn, "_configured_provider", lambda _provider=None: "claude")
  junk = types.SimpleNamespace(
    stdout="no note here", stderr="Credit balance is too low", returncode=1
  )
  monkeypatch.setattr(cn.subprocess, "run", lambda *a, **k: junk)
  note = cn._summarize("user: hi\n\nassistant: hello", "")
  assert cn._looks_like_note(note)
  assert "user: hi" in note
  assert "assistant: hello" in note


def test_codex_uses_hardened_tool_free_summarizer(monkeypatch):
  cn = _load_chat_note()
  monkeypatch.setattr(cn, "_configured_provider", lambda _provider=None: "codex")
  captured = {}

  def summarize(prompt):
    captured["prompt"] = prompt
    return _valid_note("semantic codex", "distilled state")

  monkeypatch.setattr(cn, "_run_codex_tool_free", summarize)
  note = cn._summarize("user: hi\n\nassistant: hello", "")
  assert "description: semantic codex" in note
  assert "distilled state" in note
  assert "user: hi" in captured["prompt"]
  assert "SUMMARY NOTE" in captured["prompt"]
  assert "untrusted conversation data" in captured["prompt"]


def test_codex_wrapper_reuses_compaction_runner(tmp_path, monkeypatch):
  cn = _load_chat_note()
  from app import compaction

  captured = {}

  async def run(prompt, **kwargs):
    captured["prompt"] = prompt
    captured.update(kwargs)
    return "semantic note"

  monkeypatch.setattr(compaction, "_run_codex_summarize_turn", run)
  monkeypatch.setattr(cn, "DATA_DIR", tmp_path)
  monkeypatch.setattr(cn, "MODEL", "gpt-test")
  assert cn._run_codex_tool_free("summarize this") == "semantic note"
  assert captured == {
    "prompt": "summarize this",
    "data_dir": str(tmp_path),
    "model": "gpt-test",
    "effort": None,
  }


def test_dead_codex_falls_back_to_complete_local_note(monkeypatch):
  cn = _load_chat_note()
  monkeypatch.setattr(cn, "_configured_provider", lambda _provider=None: "codex")

  def fail(_prompt):
    raise RuntimeError("provider unavailable")

  monkeypatch.setattr(cn, "_run_codex_tool_free", fail)
  note = cn._summarize("user: hi\n\nassistant: hello", "")
  assert cn._looks_like_note(note)
  assert "user: hi" in note
  assert "assistant: hello" in note


def _snapshot_db(cn, tmp_path):
  db_path = tmp_path / "ultimate.db"
  con = sqlite3.connect(db_path)
  con.execute(
    "create table chats ("
    "id text primary key, messages text, updated_at text, provider text, "
    "deleted_at text, pending_question_id text)"
  )
  con.execute(
    "create table chat_runs ("
    "id text primary key, chat_id text, status text, started_at text, "
    "goal_objective text)"
  )
  con.execute("create table owner (provider text)")
  con.execute("insert into owner values ('claude')")
  con.execute(
    "insert into chats values (?, ?, ?, 'codex', null, null)",
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
  cn.DATA_DIR = tmp_path
  cn.MEMORY_DIR = tmp_path / "memory"
  return db_path


def _valid_note(description="ours", summary="current"):
  return (
    f"---\ntype: chat\ndescription: {description}\n---\n"
    f"## Digest\n{summary}\n\n## Summary\n{summary}\n\n"
    "## Facts & intent\n- intent: test"
  )


def test_authenticated_chat_provider_wins_over_stale_owner_default(tmp_path):
  cn = _load_chat_note()
  db_path = _snapshot_db(cn, tmp_path)
  codex_home = tmp_path / "cli-auth" / "codex"
  codex_home.mkdir(parents=True)
  (codex_home / "auth.json").write_text("{}")

  con = sqlite3.connect(db_path)
  assert con.execute("select provider from owner").fetchone()[0] == "claude"
  con.close()
  snapshot = cn._read_chat_snapshot("c1")

  assert snapshot.provider == "codex"
  assert cn._configured_provider(snapshot.provider) == "codex"


def _pause_active_goal_at_question(db_path, *, objective="Ship it"):
  con = sqlite3.connect(db_path)
  con.execute(
    "update chats set pending_question_id='q-open' where id='c1'"
  )
  con.execute(
    "insert into chat_runs values (?, ?, ?, ?, ?)",
    (
      "goal-run",
      "c1",
      "running",
      "2026-07-13 10:00:01.000000",
      objective,
    ),
  )
  con.commit()
  con.close()


def test_active_goal_question_is_a_summary_checkpoint(tmp_path):
  cn = _load_chat_note()
  db_path = _snapshot_db(cn, tmp_path)
  _pause_active_goal_at_question(db_path)

  assert cn._read_chat_snapshot("c1") is None
  snapshot = cn._read_chat_snapshot(
    "c1", active_goal_checkpoint=True,
  )

  assert snapshot is not None
  assert snapshot.active_goal_checkpoint == cn.ActiveGoalCheckpoint(
    "goal-run", "q-open",
  )
  note = cn._note_path("c1")
  _existing, note_revision = cn._read_note_snapshot(note)
  assert cn._publish_if_current(
    "c1",
    snapshot.updated_at,
    note_revision,
    note,
    _valid_note("Goal checkpoint"),
    snapshot.active_goal_checkpoint,
  )
  assert "description: Goal checkpoint" in note.read_text()


def test_active_non_goal_question_is_not_a_summary_checkpoint(tmp_path):
  cn = _load_chat_note()
  db_path = _snapshot_db(cn, tmp_path)
  _pause_active_goal_at_question(db_path, objective=None)

  assert cn._read_chat_snapshot(
    "c1", active_goal_checkpoint=True,
  ) is None


def test_answering_the_goal_question_makes_checkpoint_publication_stale(tmp_path):
  cn = _load_chat_note()
  db_path = _snapshot_db(cn, tmp_path)
  _pause_active_goal_at_question(db_path)
  snapshot = cn._read_chat_snapshot(
    "c1", active_goal_checkpoint=True,
  )
  note = cn._note_path("c1")
  _existing, note_revision = cn._read_note_snapshot(note)

  con = sqlite3.connect(db_path)
  con.execute(
    "update chats set pending_question_id=null where id='c1'"
  )
  con.commit()
  con.close()

  assert not cn._publish_if_current(
    "c1",
    snapshot.updated_at,
    note_revision,
    note,
    _valid_note("Stale checkpoint"),
    snapshot.active_goal_checkpoint,
  )
  assert not note.exists()


def test_unauthenticated_claude_falls_back_without_spawning(
  tmp_path, monkeypatch,
):
  cn = _load_chat_note()
  monkeypatch.setattr(cn, "DATA_DIR", tmp_path)

  def unexpected_spawn(*_args, **_kwargs):
    pytest.fail("unauthenticated Claude CLI must not be spawned")

  monkeypatch.setattr(cn.subprocess, "run", unexpected_spawn)
  note = cn._summarize("user: hi\n\nassistant: hello", "", "claude")

  assert cn._looks_like_note(note)
  assert "user: hi" in note
  assert "assistant: hello" in note


def test_first_summary_publication_replaces_the_opening_message_title(
  tmp_path, monkeypatch,
):
  """The raw first-message title is only a fallback until a note is ready."""
  cn = _load_chat_note()
  monkeypatch.setattr(cn, "MEMORY_DIR", tmp_path / "memory")
  monkeypatch.setattr(
    cn,
    "_read_chat_snapshot",
    lambda _cid, **_kwargs: cn.ChatSnapshot(
      "transcript", "r1", [], "codex",
    ),
  )
  monkeypatch.setattr(cn, "_read_note_snapshot", lambda _note: ("", "missing"))
  summarized = {}

  def summarize(_transcript, _existing, provider):
    summarized["provider"] = provider
    return _valid_note("Current work")

  monkeypatch.setattr(cn, "_summarize", summarize)
  monkeypatch.setattr(cn, "_publish_if_current", lambda *args: True)
  patched = []
  monkeypatch.setattr(
    cn, "_patch_title", lambda cid, name: patched.append((cid, name)),
  )
  monkeypatch.setattr(cn.sys, "argv", ["chat_note.py", "c1"])

  assert cn.run() == 0
  assert patched == [("c1", "Current work")]
  assert summarized["provider"] == "codex"


def test_incremental_cursor_prevents_repeated_fallback_transcripts():
  cn = _load_chat_note()
  messages = [
    {"role": "user", "content": "old request"},
    {"role": "assistant", "content": "old answer"},
    {"role": "user", "content": "new request"},
    {"role": "assistant", "content": "new answer"},
  ]
  snapshot = cn.ChatSnapshot(
    cn._render_transcript(json.dumps(messages)), "r1", messages,
  )
  existing = cn._set_source_cursor(
    _valid_note(summary="curated"), snapshot.cursor(2),
  )

  previous_cursor = cn._source_cursor(existing)
  delta = snapshot.transcript_after(
    cn._incremental_start(snapshot, previous_cursor),
  )
  note = cn._set_source_cursor(
    cn._deterministic_note(delta, existing), snapshot.cursor(),
  )

  summary = cn._existing_section(note, "Summary")
  assert summary.count("curated") == 1
  assert summary.count("new request") == 1
  assert "old request" not in summary
  assert cn._source_cursor(note) == snapshot.cursor()
  assert snapshot.transcript_after(cn._source_cursor(note).message_count) == ""


def test_source_cursor_is_host_owned_frontmatter():
  cn = _load_chat_note()
  cursor = cn.SourceCursor(12, "a" * 64)
  note = cn._set_source_cursor(
    _valid_note(summary="source_message_count: 999"), cursor,
  )
  assert "source_message_count: 12" in note.split("---", 2)[1]
  assert "source_messages_sha256: " + "a" * 64 in note.split("---", 2)[1]
  assert "source_message_count: 999" in cn._existing_section(note, "Summary")
  assert cn._source_cursor(note) == cursor
  updated = cn._set_source_cursor(note, cn.SourceCursor(14, "b" * 64))
  assert updated.split("---", 2)[1].count("source_message_count:") == 1
  assert updated.split("---", 2)[1].count("source_messages_sha256:") == 1
  assert cn._source_cursor(updated) == cn.SourceCursor(14, "b" * 64)


def test_cursor_requires_an_unchanged_prefix_before_incremental_summary():
  cn = _load_chat_note()
  original = [
    {"role": "user", "content": "request"},
    {"role": "assistant", "content": "answer"},
  ]
  cursor = cn.ChatSnapshot("", "r1", original).cursor()
  edited = cn.ChatSnapshot("", "r2", [
    {"role": "user", "content": "edited request"},
    {"role": "assistant", "content": "answer"},
    {"role": "user", "content": "follow-up"},
  ])
  same_count_edit = cn.ChatSnapshot("", "r2", [
    {"role": "user", "content": "edited request"},
    {"role": "assistant", "content": "answer"},
  ])

  assert cn._incremental_start(edited, cursor) is None
  assert same_count_edit.cursor() != cursor


def test_two_backstops_publish_only_one_revision(tmp_path):
  cn = _load_chat_note()
  _snapshot_db(cn, tmp_path)
  snapshot = cn._read_chat_snapshot("c1")
  revision = snapshot.updated_at
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
  revision = cn._read_chat_snapshot("c1").updated_at
  note = cn._note_path("c1")
  _existing, note_revision = cn._read_note_snapshot(note)
  con = sqlite3.connect(db_path)
  con.execute(
    "insert into chat_runs values "
    "('run-c1', 'c1', 'running', '2026-07-13 10:00:01.000000', null)"
  )
  con.execute(
    "update chats set updated_at=? where id='c1'",
    ("2026-07-13 10:00:01.000000",),
  )
  con.commit()
  con.close()

  assert not cn._publish_if_current(
    "c1", revision, note_revision, note, _valid_note("stale"),
  )
  assert not note.exists()

  con = sqlite3.connect(db_path)
  con.execute("delete from chat_runs where chat_id='c1'")
  con.execute(
    "update chats set deleted_at='2026-07-13', updated_at=? where id='c1'",
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
  revision = cn._read_chat_snapshot("c1").updated_at
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
  revision = cn._read_chat_snapshot("c1").updated_at
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
