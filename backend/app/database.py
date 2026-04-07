"""Database engine and session configuration."""

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


def _make_engine():
  """Creates the SQLAlchemy engine, ensuring the DB directory exists."""
  settings = get_settings()
  if settings.database_url.startswith("sqlite:////"):
    db_path = Path(settings.database_url.replace("sqlite:////", "/"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
  connect_args = (
    {"check_same_thread": False}
    if settings.database_url.startswith("sqlite")
    else {}
  )
  return create_engine(
    settings.database_url, connect_args=connect_args
  )


engine = _make_engine()
SessionLocal = sessionmaker(
  autocommit=False, autoflush=False, bind=engine
)


class Base(DeclarativeBase):
  pass


def run_migrations(eng) -> None:
  """Run additive schema migrations on startup.

  Uses SQLAlchemy's database-agnostic inspector so this works for both
  SQLite and PostgreSQL.  Safe to call on every boot — no-ops if already
  up to date.  Skips entirely on fresh installs (no tables yet) since
  create_all will build the correct schema from scratch.
  """
  from sqlalchemy import inspect as sa_inspect, text
  inspector = sa_inspect(eng)
  tables = inspector.get_table_names()
  if "apps" not in tables:
    return  # fresh install — create_all handles it
  apps_cols = {c["name"] for c in inspector.get_columns("apps")}
  if "chat_id" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN chat_id VARCHAR(64) NULL"))
      conn.commit()
  if "chats" in tables:
    chats_cols = {c["name"] for c in inspector.get_columns("chats")}
    _add = []
    if "uploads" not in chats_cols:
      _add.append("ALTER TABLE chats ADD COLUMN uploads JSON NOT NULL DEFAULT '[]'")
    if "generated_images" not in chats_cols:
      _add.append("ALTER TABLE chats ADD COLUMN generated_images JSON NOT NULL DEFAULT '[]'")
    if "deleted_at" not in chats_cols:
      _add.append("ALTER TABLE chats ADD COLUMN deleted_at DATETIME")
    if "session_id" not in chats_cols:
      _add.append("ALTER TABLE chats ADD COLUMN session_id VARCHAR(128)")
    if _add:
      with eng.connect() as conn:
        for stmt in _add:
          conn.execute(text(stmt))
        conn.commit()


def get_db():
  """Yields a database session and closes it after the request."""
  db = SessionLocal()
  try:
    yield db
  finally:
    db.close()
