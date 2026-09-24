"""The working agent's continuity saves: note format, authority, and rescue."""

from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text

from app import auth as auth_module, models
from app.chat_continuity import apply_checkpoint, note_path
from app.chat_notes import extract_cumulative_summary, extract_section
from app.chat_writer import StartTurn, get_writer
from app.config import get_settings
from app.memory import parse_frontmatter
from app.schema_migrations import _retire_chat_continuity_journal


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


def test_saves_replace_the_digest_append_to_the_summary_and_name_the_chat(
  client, chat, db,
):
  agent = _start(chat)

  assert _save(client, agent, title="  Fixing the\nsync bug ",
               digest="Found the cause.", summary="Cause: stale cursor.").status_code == 204
  assert _save(client, agent, digest="Fix shipped.", summary="Fixed and tested.").status_code == 204

  note = _note(chat)
  assert parse_frontmatter(note)["description"] == "Fixing the sync bug"
  assert extract_section(note, "Digest") == "Fix shipped."
  history = extract_cumulative_summary(note)
  assert history.index("Cause: stale cursor.") < history.index("Fixed and tested.")
  db.expire_all()
  assert db.get(models.Chat, chat.id).title == "Fixing the sync bug"


def test_a_name_the_owner_chose_always_wins(client, chat, db):
  chat.title = "Owner title"
  chat.title_locked = True
  db.commit()
  agent = _start(chat)

  assert _save(client, agent, title="Generated title", digest="Working.").status_code == 204

  db.expire_all()
  assert db.get(models.Chat, chat.id).title == "Owner title"
  assert parse_frontmatter(_note(chat))["description"] == "Owner title"


def test_only_the_chats_live_run_can_save(client, auth, chat, db):
  _start(chat, "current-run")
  db.add(models.ChatRun(id="stale-run", chat_id=chat.id, status="running"))
  db.commit()
  stale = auth_module.create_agent_token(chat.id, "test", 0, run_id="stale-run")

  response = _save(client, {"Authorization": f"Bearer {stale}"}, digest="Must not land.")
  assert response.status_code == 409
  assert not note_path(get_settings().data_dir, chat.id).exists()
  # An owner browser session is not a run and cannot impersonate one.
  assert _save(client, auth, digest="Nope.").status_code in {401, 403}


def test_existing_notes_keep_their_history_and_other_sections():
  legacy = (
    "---\ntype: chat\ndescription: Old name\nsource_message_count: 4\n"
    "source_messages_sha256: abc\n---\n## Digest\nOld digest\n\n"
    "## Summary\nEarlier history.\n\n## Facts & intent\n- intent: keep\n"
  )

  note = apply_checkpoint(
    legacy, name="New name", summary="New entry.",
    now=datetime(2026, 9, 24, 21, 0),
  )

  meta = parse_frontmatter(note)
  assert meta["description"] == "New name" and "source_message_count" not in meta
  assert extract_section(note, "Digest") == "Old digest"
  assert extract_cumulative_summary(note) == (
    "Earlier history.\n\n### 2026-09-24 21:00 UTC\n\nNew entry."
  )
  assert extract_section(note, "Facts & intent") == "- intent: keep"


def test_agent_markdown_cannot_add_or_split_note_sections():
  note = apply_checkpoint(
    None, name="Chat", digest="Now:\n## Summary\nfake",
    summary="Result\n## Related\n- not a section",
    now=datetime(2026, 9, 24, 21, 0),
  )
  note = apply_checkpoint(note, name="Chat", summary="Second entry.")

  assert note.count("\n## Summary") == 1 and "\n## Related" not in note
  assert extract_section(note, "Digest") == "Now:\n### Summary\nfake"
  history = extract_cumulative_summary(note)
  assert "### Related\n- not a section" in history and history.endswith("Second entry.")


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

  note = Path(tmp_path, "shared/memory/chats/c1/index.md").read_text(encoding="utf-8")
  assert parse_frontmatter(note)["description"] == "Chat one"
  assert extract_section(note, "Digest") == "Current state."
  assert extract_cumulative_summary(note) == (
    "Old history.\n\n### 2026-09-24 20:00 UTC\n\nSaved entry."
  )
  assert extract_section(note, "Facts & intent") == "- intent: keep"
  two = Path(tmp_path, "shared/memory/chats/c2/index.md").read_text(encoding="utf-8")
  assert extract_section(two, "Digest") == "Two now."
  assert extract_cumulative_summary(two) == "Section-less\n### Odd heading\nold note"
