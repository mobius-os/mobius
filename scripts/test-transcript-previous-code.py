"""Run the previous release's own code against databases this release wrote.

  python3 scripts/test-transcript-previous-code.py <previous-git-ref>

A Docker-free companion to scripts/test-transcript-rollback.sh for a checkout
that has the previous release in its history. It exports that release's
backend, then alternates processes running each release's code on one
disposable SQLite file:

1. this release creates the schema, seeds typed chats and converts them, then
   leaves the database in each interesting state: mid-conversion (a chat the
   previous release wrote and this one has not converted) and with a damaged
   value converted to its placeholder;
2. the previous release boots its own ``_init_db`` (create_all, migration
   ledger, mapped-schema check) and must find the database serviceable, read
   every chat exactly as this release's rows hold it, then write: an append,
   a new chat, a rename and a purge;
3. this release must find exactly the chats the previous one changed or
   created unconverted, nothing left of the purged chat, every transcript
   read (before converting) equal to the previous release's view, and the
   same again after converting.

Prints one JSON report and exits non-zero on the first broken contract.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

THIS_SEED = r'''
import json, sqlite3
from app.database import Base, engine, SessionLocal
from app.schema_migrations import run_migrations
from app.chat_writer import create_chat
from app import models, transcript_rows as rows
Base.metadata.create_all(bind=engine)
run_migrations(engine)
typed = [{"role": "user", "content": "héllo", "ts": 1, "cid": "c1"},
         {"role": "assistant", "id": "a1", "content": "x", "ts": 2,
          "v": [1.0, -0.0, 2**63 + 1, float("nan"), float("inf"), float("-inf")]},
         "bare", 7, None]
with SessionLocal() as db:
  db.add(create_chat(id="typed", title="Typed", messages=typed))
  for i in range(30):
    db.add(create_chat(id=f"bulk-{i:02d}", title=f"Bulk {i}",
                       messages=[{"role": "user", "content": f"q{i}", "ts": i}]))
  db.add(create_chat(id="damaged", title="Damaged", messages=[]))
  db.add(create_chat(id="purge-me", title="Purge", messages=[{"role": "user", "content": "secret"}]))
  db.commit()
  rows.append(db, "typed", {"role": "assistant", "id": "a2", "content": "appended by this release", "ts": 3})
  db.commit()
path = engine.url.database
with sqlite3.connect(path) as conn:
  # Mid-conversion: written by the previous release, not yet converted here.
  conn.execute("UPDATE chats SET messages = ? WHERE id = 'bulk-00'",
               (json.dumps([{"role": "user", "content": "previous wrote this"}]),))
  conn.execute("UPDATE chats SET messages = ? WHERE id = 'damaged'", (b"{broken",))
with SessionLocal() as db:
  db.info[rows.WRITER_SESSION] = True
  rows.convert(db, "damaged")
  db.commit()
print(json.dumps({"unconverted": rows.unconverted_count(SessionLocal())}))
'''

PREVIOUS_ROUND = r'''
import json
from datetime import timedelta
from app.main import _init_db
boot = _init_db()
assert boot.serviceable, boot
from app.database import SessionLocal
from app import models
from app.chat_retention import purge_expired_chat_tombstones
from app.timeutil import now_naive_utc
from sqlalchemy.orm.attributes import flag_modified
with SessionLocal() as db:
  view = {c.id: {"title": c.title, "messages": c.messages} for c in db.query(models.Chat)}
  chat = db.get(models.Chat, "bulk-01")
  chat.messages = list(chat.messages) + [{"role": "assistant", "content": "previous appended", "ts": 99}]
  flag_modified(chat, "messages")
  db.add(models.Chat(id="from-previous", title="Previous", messages=[{"role": "user", "content": "born"}]))
  db.get(models.Chat, "bulk-02").title = "Renamed by previous"
  db.get(models.Chat, "purge-me").deleted_at = now_naive_utc() - timedelta(days=30)
  db.commit()
  purge_expired_chat_tombstones(db)
  after = {c.id: {"title": c.title, "messages": c.messages} for c in db.query(models.Chat)}
print(json.dumps({"view": view, "after": after}))
'''

THIS_VIEW = r'''
import json
from app.database import SessionLocal
from app import models, transcript_rows as rows
from sqlalchemy import text
with SessionLocal() as db:
  unconverted, after = [], None
  while (after := rows.next_unconverted(db, after)) is not None:
    unconverted.append(after)
  leftovers = {t: db.execute(text(f"SELECT COUNT(*) FROM {t} WHERE chat_id = 'purge-me'")).scalar()
               for t in ("chat_messages", "chat_search_entries", "chat_transcript_state", "chat_transcript_damage")}
  # Before converting, readers serve each unconverted chat from its legacy value.
  before = {c.id: {"title": c.title, "messages": rows.read_all(db, c)} for c in db.query(models.Chat)}
  db.rollback()
  db.info[rows.WRITER_SESSION] = True
  for chat_id in list(unconverted):
    rows.convert(db, chat_id)
    db.commit()
  view = {c.id: {"title": c.title, "messages": rows.read_all(db, c)} for c in db.query(models.Chat)}
print(json.dumps({"unconverted": unconverted, "leftovers": leftovers, "view": view,
                  "before": before}))
'''

THIS_MIRROR = r'''
import json, sqlite3
from app.database import engine
differ = []
with sqlite3.connect(engine.url.database) as conn:
  for chat_id, raw in conn.execute("SELECT c.id, CAST(c.messages AS BLOB) FROM chats c "
                                   "JOIN chat_transcript_state s ON s.chat_id = c.id"):
    bodies = [b for (b,) in conn.execute(
      "SELECT body FROM chat_messages WHERE chat_id = ? ORDER BY seq", (chat_id,))]
    if bytes(raw) != ("[" + ", ".join(bodies) + "]").encode():
      differ.append(chat_id)
print(json.dumps({"differ": differ}))
'''

THIS_ROWS = r'''
import json
from app.database import SessionLocal
from app import models, transcript_rows as rows
with SessionLocal() as db:
  converted = {c.id: {"title": c.title, "messages": rows.read_all(db, c)}
               for c in db.query(models.Chat) if rows.is_converted(db, c.id)}
print(json.dumps(converted))
'''


def run(backend: Path, env: dict, code: str) -> dict:
  result = subprocess.run([sys.executable, "-c", code], cwd=backend, env={**env, "PYTHONPATH": str(backend)},
                          capture_output=True, text=True)
  if result.returncode:
    sys.exit(f"{backend}: {result.stderr[-4000:]}")
  return json.loads(result.stdout.strip().splitlines()[-1])


def canonical(view: dict) -> dict:
  """Text form, so 1 / 1.0 / True, -0.0 / 0.0 and NaN are all distinguished."""
  return {k: json.dumps(v, sort_keys=True) for k, v in view.items()}


def main(previous_ref: str) -> dict:
  work = Path(tempfile.mkdtemp(prefix="transcript-previous-"))
  try:
    return _run_rounds(previous_ref, work)
  finally:
    shutil.rmtree(work, ignore_errors=True)


def _run_rounds(previous_ref: str, work: Path) -> dict:
  previous = work / "previous"
  previous.mkdir()
  archive = subprocess.run(["git", "-C", str(ROOT), "archive", previous_ref, "backend"],
                           capture_output=True, check=True).stdout
  subprocess.run(["tar", "-x", "-C", str(previous)], input=archive, check=True)
  env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST")}
  env.update(DATABASE_URL=f"sqlite:///{work / 'shared.db'}", DATA_DIR=str(work / "data"),
             SECRET_KEY=secrets.token_hex(32), MOEBIUS_SKIP_BOOTSTRAP="1")
  (work / "data").mkdir()
  this_backend, previous_backend = ROOT / "backend", previous / "backend"
  report = {"previous_ref": previous_ref}

  report["seed"] = run(this_backend, env, THIS_SEED)
  report["mirror_not_byte_exact"] = run(this_backend, env, THIS_MIRROR)["differ"]
  expected = canonical(run(this_backend, env, THIS_ROWS))
  previous_round = run(previous_backend, env, PREVIOUS_ROUND)
  seen = canonical(previous_round["view"])
  mismatched = sorted(k for k, v in expected.items() if seen.get(k) != v)
  report["previous_reads_every_converted_chat_exactly"] = not mismatched
  report["previous_sees_unconverted_legacy"] = previous_round["view"]["bulk-00"]["messages"] == [
    {"role": "user", "content": "previous wrote this"}]
  returned = run(this_backend, env, THIS_VIEW)
  report["unconverted_after_previous"] = returned["unconverted"]
  report["purge_leftovers"] = returned["leftovers"]
  report["nothing_lost"] = canonical(returned["view"]) == canonical(previous_round["after"])
  report["reads_exact_before_converting"] = (
    canonical(returned["before"]) == canonical(previous_round["after"]))
  report["mirror_not_byte_exact_after_return"] = run(this_backend, env, THIS_MIRROR)["differ"]
  ok = (not mismatched and report["previous_sees_unconverted_legacy"]
        and not report["mirror_not_byte_exact"] and not report["mirror_not_byte_exact_after_return"]
        and returned["unconverted"] == ["bulk-00", "bulk-01", "from-previous"]
        and set(returned["leftovers"].values()) == {0} and report["nothing_lost"]
        and report["reads_exact_before_converting"])
  report["ok"] = ok
  if mismatched:
    report["mismatched"] = mismatched[:5]
  return report


if __name__ == "__main__":
  outcome = main(sys.argv[1])
  print(json.dumps(outcome, indent=2))
  sys.exit(0 if outcome["ok"] else 1)
