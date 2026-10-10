"""An idle anchor keeps SQLite's WAL alive across NullPool sessions."""

from pathlib import Path

from sqlalchemy import text

from app import database
from app.database import SessionLocal, close_wal_anchor, engine, open_wal_anchor


def _wal_path() -> Path:
  return Path(f"{engine.url.database}-wal")


def _write_once() -> None:
  with SessionLocal() as db:
    db.execute(text("CREATE TABLE IF NOT EXISTS wal_anchor_probe (n INTEGER)"))
    db.execute(text("INSERT INTO wal_anchor_probe VALUES (1)"))
    db.commit()


def test_last_session_close_tears_down_wal_without_anchor():
  close_wal_anchor()
  _write_once()
  assert not _wal_path().exists()


def test_anchor_keeps_wal_between_sessions_and_releases_it_on_close():
  try:
    assert open_wal_anchor() is True
    assert open_wal_anchor() is True  # idempotent
    _write_once()
    assert _wal_path().exists()
    assert database.checked_out_connections() == 0  # not part of the pool
  finally:
    close_wal_anchor()
  _write_once()
  assert not _wal_path().exists()
