"""The working agent's continuity saves: note format, authority, and rescue."""

from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text

from app import auth as auth_module, models
from app.chat_continuity import apply_checkpoint, note_path
from app.chat_notes import extract_chat_summary, extract_full_digest, extract_section
from app.chat_writer import StartTurn, get_writer
from app.config import get_settings
from app.memory import parse_frontmatter
from app.schema_migrations import (
  _drop_chat_note_backup,
  _retire_chat_continuity_journal,
  _swap_chat_note_sections,
)


def _start(chat, run_id="continuity-run"):
  get_writer().submit(StartTurn(
    chat_id=chat.id,
    run_token=run_id,
    user_msg={"role": "user", "content": "continue", "ts": 10},
    title_source="continue",
  )).result(timeout=5)
  token = auth_module.create_agent_token(chat.id, "test", 0, run_id=run_id)
  return {"Authorization": f"Bearer {token}"}


def _note(chat) -> str:
  return note_path(get_settings().data_dir, chat.id).read_text(encoding="utf-8")


def _save(client, headers, **fields):
  return client.post("/api/chat/continuity/checkpoints", headers=headers, json=fields)


def test_saves_replace_the_summary_append_to_the_digest_and_name_the_chat(
  client, chat, db,
):
  agent = _start(chat)

  assert _save(client, agent, title="  Fixing the\nsync bug ",
               chat_summary="Found the cause.", digest_entry="Cause: stale cursor.").status_code == 204
  assert _save(client, agent, chat_summary="Fix shipped.", digest_entry="Fixed and tested.").status_code == 204
  # Saves written for the old plain field names are refused, never misfiled.
  assert _save(client, agent, summary="Old meaning.").status_code == 422
  assert _save(client, agent, digest="Old meaning.").status_code == 422

  note = _note(chat)
  assert parse_frontmatter(note)["description"] == "Fixing the sync bug"
  assert extract_section(note, "Summary") == "Fix shipped."
  history = extract_full_digest(note)
  assert history.index("Cause: stale cursor.") < history.index("Fixed and tested.")
  db.expire_all()
  assert db.get(models.Chat, chat.id).title == "Fixing the sync bug"


def test_batched_delta_preserves_omitted_fields_and_prior_evidence(client, chat):
  agent = _start(chat)
  assert _save(client, agent, title="Investigating sync", chat_summary="Repairing sync.",
               digest_entry="Preserve offline edits.").status_code == 204
  before = _note(chat)
  delta = "Cause: stale cursor. Replaced cursor ownership. Offline replay passed."
  assert _save(client, agent, digest_entry=delta).status_code == 204
  after = _note(chat)
  assert parse_frontmatter(after)["description"] == parse_frontmatter(before)["description"]
  assert extract_section(after, "Summary") == extract_section(before, "Summary")
  history = extract_full_digest(after)
  assert history.startswith(extract_full_digest(before))
  assert history.count(delta) == 1
  assert history.count("### ") == 2  # Initial evidence plus one combined delta.
  # A summary-only change must not append an empty or repeated Digest entry.
  assert _save(client, agent, chat_summary="Repair verified.").status_code == 204
  assert extract_full_digest(_note(chat)) == history


def test_a_name_the_owner_chose_always_wins(client, chat, db):
  chat.title = "Owner title"
  chat.title_locked = True
  db.commit()
  agent = _start(chat)

  assert _save(client, agent, title="Generated title", chat_summary="Working.").status_code == 204

  db.expire_all()
  assert db.get(models.Chat, chat.id).title == "Owner title"
  assert parse_frontmatter(_note(chat))["description"] == "Owner title"


def test_only_the_chats_live_run_can_save(client, auth, chat, db):
  _start(chat, "current-run")
  db.add(models.ChatRun(id="stale-run", chat_id=chat.id, status="running"))
  db.commit()
  stale = auth_module.create_agent_token(chat.id, "test", 0, run_id="stale-run")

  response = _save(client, {"Authorization": f"Bearer {stale}"}, chat_summary="Must not land.")
  assert response.status_code == 409
  assert not note_path(get_settings().data_dir, chat.id).exists()
  # An owner browser session is not a run and cannot impersonate one.
  assert _save(client, auth, chat_summary="Nope.").status_code in {401, 403}


def test_existing_notes_keep_their_history_and_other_sections():
  legacy = (
    "---\ntype: chat\ndescription: Old name\nsource_message_count: 4\n"
    "source_messages_sha256: abc\n---\n## Summary\nOld summary\n\n"
    "## Digest\nEarlier history.\n\n## Facts & intent\n- intent: keep\n"
  )

  note = apply_checkpoint(
    legacy, name="New name", digest="New entry.",
    now=datetime(2026, 9, 24, 21, 0),
  )

  meta = parse_frontmatter(note)
  assert meta["description"] == "New name" and "source_message_count" not in meta
  assert extract_section(note, "Summary") == "Old summary"
  assert extract_full_digest(note) == (
    "Earlier history.\n\n### 2026-09-24 21:00 UTC\n\nNew entry."
  )
  assert extract_section(note, "Facts & intent") == "- intent: keep"


def test_agent_markdown_cannot_add_or_split_note_sections():
  note = apply_checkpoint(
    None, name="Chat", summary="Now:\n## Digest\nfake",
    digest="Result\n## Related\n- not a section",
    now=datetime(2026, 9, 24, 21, 0),
  )
  note = apply_checkpoint(note, name="Chat", digest="Second entry.\n  ## Digest\nindented")

  assert note.count("\n## Digest") == 1 and "\n## Related" not in note
  assert "\n  ## Digest" not in note
  assert extract_section(note, "Summary") == "Now:\n### Digest\nfake"
  history = extract_full_digest(note)
  assert "### Related\n- not a section" in history and history.endswith("Second entry.\n### Digest\nindented")


def test_retirement_rescues_journal_saves_into_the_note(tmp_path, monkeypatch):
  """Instances that briefly ran the journal tables keep every saved entry."""
  monkeypatch.setenv("DATA_DIR", str(tmp_path))
  eng = create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE chats (id TEXT PRIMARY KEY, title TEXT)"))
    conn.execute(text(
      "CREATE TABLE chat_runs (id TEXT PRIMARY KEY, delivered_message_count INTEGER,"
      " delivered_prefix_hash TEXT)"
    ))
    conn.execute(text(
      "CREATE TABLE chat_continuity (chat_id TEXT PRIMARY KEY, current_summary TEXT)"
    ))
    conn.execute(text(
      "CREATE TABLE chat_continuity_entries (chat_id TEXT, revision INTEGER,"
      " digest TEXT, legacy_markdown TEXT, created_at TIMESTAMP)"
    ))
    conn.execute(text("INSERT INTO chats VALUES ('c1', 'Chat one'), ('c2', 'Chat two')"))
    conn.execute(text(
      "INSERT INTO chat_continuity VALUES ('c1', 'Current state.'), ('c2', 'Two now.')"
    ))
    conn.execute(text(
      "INSERT INTO chat_continuity_entries VALUES "
      "('c1', 1, 'x', '---\ndescription: Old\n---\n## Digest\nd\n\n## Summary\nOld history.\n"
      "\n## Facts & intent\n- intent: keep\n', "
      "'2026-09-24 19:00:00'),"
      "('c1', 2, 'Saved entry.', NULL, '2026-09-24T20:00:00'),"
      "('c2', 1, 'x', 'Section-less\n## Odd heading\nold note', '2026-09-24 19:00:00')"
    ))

  _retire_chat_continuity_journal(eng)
  _swap_chat_note_sections(eng)  # Later migrations still apply in order.

  note = Path(tmp_path, "shared/memory/chats/c1/index.md").read_text(encoding="utf-8")
  assert parse_frontmatter(note)["description"] == "Chat one"
  assert extract_section(note, "Summary") == "Current state."
  assert extract_full_digest(note) == (
    "Old history.\n\n### 2026-09-24 20:00 UTC\n\nSaved entry."
  )
  assert extract_section(note, "Facts & intent") == "- intent: keep"
  two = Path(tmp_path, "shared/memory/chats/c2/index.md").read_text(encoding="utf-8")
  assert extract_section(two, "Summary") == "Two now."
  assert extract_full_digest(two) == "Section-less\n### Odd heading\nold note"


def test_section_swap_keeps_every_note_readable_backed_up_and_rerunnable(
  tmp_path, monkeypatch,
):
  monkeypatch.setenv("DATA_DIR", str(tmp_path))
  chats = tmp_path / "shared" / "memory" / "chats"
  old_notes = {
    "current": (
      "---\ntype: chat\ndescription: Current\n"
      'recovery_coverage: {"message_count": 2, "messages_sha256": "m", '
      '"summary_sha256": "s"}\n---\n\n## Digest\n\nShort now.\n\n'
      "## Summary\n\nOld entry.\n\n## Summary\n\nNested legacy prose.\n\n"
      "## Facts & intent\n\n- keep\n"
    ),
    "history-only": (
      "---\ndescription: Old\n---\n## Summary\nOnly history.\n\n"
      "## Summary\n\nA recap heading inside the history.\n"
    ),
    "loose": "Section-less legacy note.\n",
  }
  for chat_id, text in old_notes.items():
    (chats / chat_id).mkdir(parents=True)
    (chats / chat_id / "index.md").write_text(text, encoding="utf-8")
  (chats / "undecodable").mkdir()
  (chats / "undecodable" / "index.md").write_bytes(b"## Digest\n\xff short\n\n## Summary\nold\n")

  _swap_chat_note_sections(None)
  first = {c: (chats / c / "index.md").read_text(encoding="utf-8") for c in old_notes}
  _swap_chat_note_sections(None)  # A crash before the ledger row reruns it.

  for chat_id, text in old_notes.items():
    assert (chats / chat_id / "index.md").read_text(encoding="utf-8") == first[chat_id]
    backup = tmp_path / "backups" / "chat-notes-before-0083" / chat_id / "index.md"
    assert backup.read_text(encoding="utf-8") == text
  current = first["current"]
  assert extract_section(current, "Summary") == "Short now."
  assert extract_full_digest(current) == (
    "Old entry.\n\n## Summary\n\nNested legacy prose."
  )
  assert extract_section(current, "Facts & intent") == "- keep"
  assert '"digest_sha256": "s"' in current and "summary_sha256" not in current
  # A heading inside the history is never mistaken for the short summary.
  assert extract_chat_summary(first["history-only"]) is None
  assert extract_full_digest(first["history-only"]) == (
    "Only history.\n\n## Summary\n\nA recap heading inside the history."
  )
  assert first["loose"] == old_notes["loose"]
  assert (chats / "undecodable" / "index.md").read_bytes() == (
    b"## Summary\n\xff short\n\n## Digest\nold\n"
  )

  _drop_chat_note_backup(None)
  assert not (tmp_path / "backups" / "chat-notes-before-0083").exists()
