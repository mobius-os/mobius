"""Rehearse release-1 transcript conversion on a disposable database copy.

  DATABASE_URL=sqlite:////tmp/copy.db DATA_DIR=/tmp/rehearsal SECRET_KEY=... \\
    python3 scripts/rehearse-transcript-conversion.py /tmp/copy.db > result.json

Never point it at a live database: it converts every chat, simulates the
previous release's writes, and finally applies the next release's column drop.
It needs only a ``chats`` table with ``id``, ``title`` and ``messages``; the
rest of the schema this release adds is created first. Prints one JSON report:
timings, peak memory, database growth relative to transcript bytes, exactness
of every converted chat, and the cost of the per-commit legacy mirror.
"""

import hashlib
import json
import os
import resource
import sys
import time
from pathlib import Path


def main(path: str) -> dict:
  database = Path(path)
  wal = Path(f"{path}-wal")
  os.environ.setdefault("MOBIUS_TEST_DATABASE_ISOLATED", "1")
  from sqlalchemy import text
  from app import models, transcript_rows as rows
  from app.database import SessionLocal, engine
  from app.schema_migrations import _add_transcript_rows

  report: dict = {"database": str(database)}

  def size() -> int:
    with engine.connect() as conn:
      return conn.exec_driver_sql("PRAGMA page_count").scalar() * conn.exec_driver_sql("PRAGMA page_size").scalar()

  with engine.connect() as conn:
    chats, transcript_bytes, largest = conn.exec_driver_sql(
      "SELECT COUNT(*), SUM(length(CAST(messages AS BLOB))), MAX(length(CAST(messages AS BLOB))) FROM chats"
    ).one()
    before = {chat_id: hashlib.sha256(bytes(raw)).hexdigest() for chat_id, raw in
              conn.exec_driver_sql("SELECT id, CAST(messages AS BLOB) FROM chats")}
  report.update(chats=chats, transcript_bytes=transcript_bytes, largest_transcript_bytes=largest,
                bytes_before=size())

  started = time.monotonic()
  _add_transcript_rows(engine)
  report["migration_seconds"] = round(time.monotonic() - started, 3)

  max_wal = 0
  started = time.monotonic()
  with SessionLocal() as db:
    db.info[rows.WRITER_SESSION] = True
    after = None
    slowest = (0.0, None)
    while (after := rows.next_unconverted(db, after)) is not None:
      one = time.monotonic()
      rows.convert(db, after)
      db.commit()
      slowest = max(slowest, (time.monotonic() - one, after))
      max_wal = max(max_wal, wal.stat().st_size if wal.exists() else 0)
    report["slowest_chat_conversion_seconds"] = round(slowest[0], 3)
    with engine.connect() as conn:
      report["slowest_chat_legacy_bytes"] = conn.execute(text(
        "SELECT length(CAST(messages AS BLOB)) FROM chats WHERE id = :id"), {"id": slowest[1]}).scalar()
  report["conversion_seconds"] = round(time.monotonic() - started, 3)
  report["max_wal_bytes"] = max_wal
  report["peak_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
  report["bytes_after_conversion"] = size()
  report["growth_bytes"] = report["bytes_after_conversion"] - report["bytes_before"]
  report["growth_per_transcript_byte"] = round(report["growth_bytes"] / transcript_bytes, 3)

  # Every chat: rows decode to the legacy value; legacy bytes are untouched.
  exact = legacy_unchanged = damaged = 0
  with engine.connect() as conn:
    for chat_id, raw in conn.exec_driver_sql("SELECT id, CAST(messages AS BLOB) FROM chats"):
      bodies = [json.loads(b) for (b,) in conn.execute(text(
        "SELECT body FROM chat_messages WHERE chat_id = :id ORDER BY seq"), {"id": chat_id})]
      legacy_unchanged += hashlib.sha256(bytes(raw)).hexdigest() == before[chat_id]
      if conn.execute(text("SELECT 1 FROM chat_transcript_damage WHERE chat_id = :id"),
                      {"id": chat_id}).first():
        damaged += 1
        continue
      exact += json.dumps(json.loads(bytes(raw)), sort_keys=True) == json.dumps(bodies, sort_keys=True)
  report.update(chats_exact=exact, chats_damaged=damaged, legacy_bytes_unchanged=legacy_unchanged,
                every_chat_exact=exact + damaged == chats)

  # Release 1's worst per-commit cost: mirroring the largest chat.
  with engine.connect() as conn:
    biggest = conn.exec_driver_sql(
      "SELECT id FROM chats ORDER BY length(CAST(messages AS BLOB)) DESC LIMIT 1").scalar()
  with SessionLocal() as db:
    rows._changed(db, biggest)
    started = time.monotonic()
    db.commit()
    report["largest_chat_mirror_seconds"] = round(time.monotonic() - started, 3)
  with engine.connect() as conn:
    legacy = conn.execute(text("SELECT messages FROM chats WHERE id = :id"), {"id": biggest}).scalar()
    bodies = [b for (b,) in conn.execute(text(
      "SELECT body FROM chat_messages WHERE chat_id = :id ORDER BY seq"), {"id": biggest})]
    report["largest_chat_mirror_byte_exact"] = legacy == "[" + ", ".join(bodies) + "]"
    report["largest_chat_converted_after_mirror"] = conn.execute(text(
      "SELECT 1 FROM chat_transcript_state WHERE chat_id = :id"), {"id": biggest}).first() is not None

  # The previous release's writes: exactly those chats are marked, deletes clean up.
  with engine.connect() as conn:
    sample = [r[0] for r in conn.exec_driver_sql("SELECT id FROM chats ORDER BY id LIMIT 15")]
  changed, deleted = sample[:10], sample[10:]
  with engine.begin() as conn:
    for chat_id in changed:
      conn.execute(text("UPDATE chats SET messages = json_insert(messages, '$[#]', json('{\"role\": \"user\", \"content\": \"previous\"}')) WHERE id = :id"), {"id": chat_id})
    for chat_id in deleted:
      conn.execute(text("DELETE FROM chats WHERE id = :id"), {"id": chat_id})
  with SessionLocal() as db:
    unconverted = set()
    after = None
    while (after := rows.next_unconverted(db, after)) is not None:
      unconverted.add(after)
    leftovers = sum(db.execute(text(f"SELECT COUNT(*) FROM {t} WHERE chat_id IN ({','.join(repr(d) for d in deleted)})")).scalar()
                    for t in ("chat_messages", "chat_search_entries", "chat_transcript_state"))
    db.info[rows.WRITER_SESSION] = True
    started = time.monotonic()
    for chat_id in sorted(unconverted):
      rows.convert(db, chat_id)
      db.commit()
    report["reconversion_seconds"] = round(time.monotonic() - started, 3)
    reconverted_ok = all(rows.read_all(db, c)[-1] == {"role": "user", "content": "previous"} for c in changed)
  report.update(previous_writes_marked_exactly=unconverted == set(changed),
                deleted_leftover_rows=leftovers, reconverted_exactly=reconverted_ok)

  # The next release's contract: clear and drop the legacy column.
  started = time.monotonic()
  with engine.begin() as conn:
    conn.exec_driver_sql("DROP TRIGGER chats_messages_written")
    conn.exec_driver_sql("UPDATE chats SET messages = '[]'")
    conn.exec_driver_sql("ALTER TABLE chats DROP COLUMN messages")
  report["release2_drop_seconds"] = round(time.monotonic() - started, 3)
  report["bytes_after_release2_drop_before_vacuum"] = size()
  return report


if __name__ == "__main__":
  print(json.dumps(main(sys.argv[1]), indent=2))
