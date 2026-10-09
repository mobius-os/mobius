"""In-container helper for scripts/test-transcript-rollback.sh.

Runs inside a Möbius image against its own baked code, so each release reads
and writes the shared database exactly as that release does. Commands print
JSON. "previous" means an image without per-message rows; "candidate" means
the release that keeps ``chats.messages`` as a mirror of those rows.

  python3 transcript_rollback_probe.py <command>

  seed          previous: typed fixture chats, a damaged and a deleted one
  dump          either: every chat's decoded transcript, keyed by chat id
  pending       candidate: chats whose rows are not yet authoritative
  write-new     candidate: writer-command edits, a new chat, a rename, a purge
  write-old     previous: an append, a new chat, a rename, a purge
  unconverted   any image, server stopped: ids without a conversion marker
  stores-rows   any image, server stopped: whether this release keeps message rows
  mirror-exact  candidate: converted chats whose legacy bytes are not exactly
                '[' + ', '.join(row bodies) + ']' (must be none)

Callers compare dumps as canonical text (json.dumps with sorted keys), so
1 / 1.0 / True, -0.0 / 0.0 and NaN are all distinguished.
"""

import json
import os
import sqlite3
import sys
from datetime import timedelta

# Each round of the driver touches its own chats, so rounds compose.
ROUND = int(os.environ.get("PROBE_ROUND", "1"))

TYPED = [
  {"role": "user", "content": "héllo ☃ roundtrip", "ts": 1, "cid": "c-1"},
  {"role": "assistant", "id": "a-1", "content": "floats", "ts": 2,
   "blocks": [{"type": "text", "content": "x"}],
   "values": [1.0, -0.0, 1e308, 0.1, float("nan"), float("inf"), float("-inf")]},
  {"role": "user", "content": "big", "ts": 3, "n": 2**63 + 1, "lone": "\ud800"},
  "a bare string item", 7, None,
  {"role": "assistant", "hidden": True, "content": "hidden reminder", "ts": 4},
]
BULK = 400


def database_path():
  from app.config import get_settings
  return get_settings().database_url.replace("sqlite:///", "", 1)


def candidate():
  from app import models
  return hasattr(models.Chat, "legacy_messages")


def session():
  from app.database import SessionLocal
  return SessionLocal()


def new_chat(chat_id, title, messages):
  from app import models
  if candidate():
    from app.chat_writer import create_chat
    return create_chat(id=chat_id, title=title, messages=messages)
  return models.Chat(id=chat_id, title=title, messages=messages)


def seed():
  with session() as db:
    db.add(new_chat("typed", "Typed fixture", TYPED))
    for i in range(BULK):
      db.add(new_chat(f"bulk-{i:04d}", f"Bulk {i}", [
        {"role": "user", "content": f"bulk question {i}", "ts": 10 + i},
        {"role": "assistant", "id": f"bulk-a-{i}", "content": "answer " * 50, "ts": 11 + i},
      ]))
    db.add(new_chat("damaged", "Damaged", []))
    db.add(new_chat("tombstoned", "Deleted soon", [{"role": "user", "content": "deleted prose"}]))
    db.commit()
  with sqlite3.connect(database_path()) as conn:
    conn.execute("UPDATE chats SET messages = ? WHERE id = 'damaged'", (b"{not json",))
  return {"seeded": BULK + 3}


def dump():
  out = {}
  if candidate():
    from app import models, transcript_rows
    with session() as db:
      for chat in db.query(models.Chat).order_by(models.Chat.id):
        out[chat.id] = {"title": chat.title, "messages": list(transcript_rows.history(chat))}
  else:
    with sqlite3.connect(database_path()) as conn:
      for chat_id, title, raw in conn.execute("SELECT id, title, CAST(messages AS BLOB) FROM chats ORDER BY id"):
        try:
          messages = json.loads(raw)
        except ValueError:
          messages = {"undecodable": bytes(raw).decode("latin-1")}
        out[chat_id] = {"title": title, "messages": messages}
  return out


def pending():
  from app import transcript_rows
  with session() as db:
    return {"pending": transcript_rows.unconverted_count(db)}


def unconverted():
  with sqlite3.connect(database_path()) as conn:
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "chat_transcript_state" not in tables:
      return {"unconverted": None}
    rows = conn.execute("SELECT id FROM chats c WHERE NOT EXISTS "
                        "(SELECT 1 FROM chat_transcript_state s WHERE s.chat_id = c.id) ORDER BY id")
    leftovers = {table: conn.execute(f"SELECT COUNT(*) FROM {table} WHERE chat_id = 'bulk-{ROUND * 10 + 5:04d}'").fetchone()[0]
                 for table in ("chat_messages", "chat_search_entries", "chat_transcript_damage")}
    return {"unconverted": [row[0] for row in rows], "purged_leftovers": leftovers}


def purge(chat_id):
  from app import models
  from app.chat_retention import purge_expired_chat_tombstones
  from app.timeutil import now_naive_utc
  with session() as db:
    chat = db.get(models.Chat, chat_id)
    chat.deleted_at = now_naive_utc() - timedelta(days=30)
    db.commit()
    purge_expired_chat_tombstones(db)


def write_new():
  from app import chat_writer, models
  chat_writer.start_writer()
  submit = lambda cmd: chat_writer.wait_ack(chat_writer.get_writer().submit(cmd))
  with session() as db:
    typed = db.get(models.Chat, "typed")
    from app import transcript_rows
    messages = list(transcript_rows.history(typed))
  submit(chat_writer.ReplaceTranscript(chat_id="typed", messages=messages + [
    {"role": "assistant", "id": f"a-new-{ROUND}", "content": "written by candidate", "ts": 99 + ROUND},
  ]))
  submit(chat_writer.ReplaceTranscript(chat_id=f"bulk-{ROUND * 10 + 1:04d}", messages=[
    {"role": "user", "content": "truncated by candidate", "ts": 5},
  ]))
  with session() as db:
    db.add(new_chat(f"from-candidate-{ROUND}", "Candidate chat", [{"role": "user", "content": "candidate born"}]))
    db.get(models.Chat, f"bulk-{ROUND * 10 + 2:04d}").title = "Renamed by candidate"
    db.commit()
  purge("tombstoned" if ROUND == 1 else f"bulk-{ROUND * 10 + 6:04d}")
  chat_writer.stop_writer()
  return {"written": "candidate"}


def write_old():
  """The previous release's own writes. A pre-rows release edits the legacy
  column directly, so exactly the chats it touched lose their conversion
  marker. A previous release that already stores rows writes through its own
  writer and keeps every chat converted. Returns the marks to expect."""
  from app import models
  appended = {"role": "user", "content": "appended by previous", "ts": 500}
  target = f"bulk-{ROUND * 10 + 3:04d}"
  if candidate():
    from app import chat_writer, transcript_rows
    chat_writer.start_writer()
    with session() as db:
      messages = list(transcript_rows.history(db.get(models.Chat, target)))
    chat_writer.wait_ack(chat_writer.get_writer().submit(
      chat_writer.ReplaceTranscript(chat_id=target, messages=messages + [appended])))
    chat_writer.stop_writer()
    expected = []
  else:
    from sqlalchemy.orm.attributes import flag_modified
    with session() as db:
      chat = db.get(models.Chat, target)
      chat.messages = list(chat.messages) + [appended]
      flag_modified(chat, "messages")
      db.commit()
    expected = [target, f"from-previous-{ROUND}"]
  with session() as db:
    db.add(new_chat(f"from-previous-{ROUND}", "Previous chat", [{"role": "user", "content": "previous born"}]))
    db.get(models.Chat, f"bulk-{ROUND * 10 + 4:04d}").title = "Renamed by previous"
    db.commit()
  purge(f"bulk-{ROUND * 10 + 5:04d}")
  return {"written": "previous", "expect_unconverted": sorted(expected)}


def mirror_exact():
  with sqlite3.connect(database_path()) as conn:
    differ = []
    for chat_id, raw in conn.execute(
      "SELECT c.id, CAST(c.messages AS BLOB) FROM chats c "
      "JOIN chat_transcript_state s ON s.chat_id = c.id ORDER BY c.id"
    ):
      bodies = [b for (b,) in conn.execute(
        "SELECT body FROM chat_messages WHERE chat_id = ? ORDER BY seq", (chat_id,))]
      if bytes(raw) != ("[" + ", ".join(bodies) + "]").encode():
        differ.append(chat_id)
    return {"differ": differ}


COMMANDS = {"mirror-exact": mirror_exact, "seed": seed, "dump": dump, "pending": pending, "write-new": write_new,
            "write-old": write_old, "unconverted": unconverted,
            "stores-rows": lambda: {"rows": candidate()}}

if __name__ == "__main__":
  print(json.dumps(COMMANDS[sys.argv[1]](), sort_keys=True))
