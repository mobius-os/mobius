"""Database engine and session configuration.

FROZEN at runtime (chmod 444 root-owned per protected-files.txt).
main.py imports this at module load to set up the engine + run
migrations; if I'm broken the server can't boot and /recover/chat
is unreachable. (The recovery surface itself uses raw sqlite3
and doesn't depend on me, but main.py still does.)

To edit me, change the source on the host repo and rebuild the
container image. For ad-hoc DB queries the agent should use raw
`sqlite3` from stdlib — that path doesn't touch this file at all.
"""

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


def _make_engine():
  """Creates the SQLAlchemy engine, ensuring the DB directory exists."""
  settings = get_settings()
  is_sqlite = settings.database_url.startswith("sqlite")
  if settings.database_url.startswith("sqlite:////"):
    db_path = Path(settings.database_url.replace("sqlite:////", "/"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
  connect_args = {"check_same_thread": False} if is_sqlite else {}
  eng = create_engine(settings.database_url, connect_args=connect_args)
  if is_sqlite:
    # SQLite under concurrent writes:
    # - WAL lets readers run while a single writer writes (no
    #   blanket lock the way the default DELETE journal does).
    # - busy_timeout waits up to N ms for a lock instead of
    #   immediately raising "database is locked" when two
    #   coroutines try to commit in the same window.
    # - synchronous=NORMAL keeps durability for the WAL but skips
    #   the per-commit fsync that FULL does; safe for chat data.
    @event.listens_for(eng, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):
      cur = dbapi_conn.cursor()
      cur.execute("PRAGMA journal_mode=WAL")
      cur.execute("PRAGMA busy_timeout=5000")
      cur.execute("PRAGMA synchronous=NORMAL")
      cur.close()
  return eng


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
  if "source_dir" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN source_dir VARCHAR(512) NULL"))
      conn.commit()
  if "pinned_at" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN pinned_at DATETIME NULL"))
      conn.commit()
  if "share_with_apps" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN share_with_apps VARCHAR(16) "
        "NOT NULL DEFAULT 'none'"
      ))
      conn.commit()
  if "cross_app_access" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN cross_app_access VARCHAR(16) "
        "NOT NULL DEFAULT 'none'"
      ))
      conn.commit()
  if "offline_capable" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN offline_capable BOOLEAN "
        "NOT NULL DEFAULT 0"
      ))
      conn.commit()
  # Slug column: split into three independent idempotent gates so a
  # crash anywhere in the sequence leaves a recoverable state. The
  # previous shape gated the backfill on "column missing", which
  # meant a mid-loop crash would commit the ALTER but skip the
  # backfill+index on every subsequent boot — leaving NULL slugs
  # forever and silently degrading the three-dots menu on every
  # legacy app. Each gate below re-checks its own precondition.
  if "slug" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text(
        "ALTER TABLE apps ADD COLUMN slug VARCHAR(128) NULL"
      ))
      conn.commit()
  # Backfill: runs whenever any row has a NULL slug. Idempotent —
  # already-populated rows are filtered out by the WHERE clause and
  # their slugs are read into `taken` so we don't collide with them.
  from app.routes.apps import _slugify_for_source_dir
  with eng.connect() as conn:
    null_rows = conn.execute(
      text("SELECT id, name FROM apps WHERE slug IS NULL ORDER BY id")
    ).fetchall()
    if null_rows:
      existing = conn.execute(
        text("SELECT slug FROM apps WHERE slug IS NOT NULL")
      ).fetchall()
      taken: set[str] = {r[0] for r in existing if r[0]}
      for row in null_rows:
        base = _slugify_for_source_dir(row[1])
        candidate = base
        suffix = 2
        while candidate in taken:
          candidate = f"{base}-{suffix}"
          suffix += 1
        taken.add(candidate)
        conn.execute(
          text("UPDATE apps SET slug = :s WHERE id = :i"),
          {"s": candidate, "i": row[0]},
        )
      conn.commit()
  # Unique index: separate gate so a crashed backfill on a prior boot
  # doesn't leave us indexless forever. `IF NOT EXISTS` handles the
  # happy-path re-run case at zero cost.
  with eng.connect() as conn:
    conn.execute(text(
      "CREATE UNIQUE INDEX IF NOT EXISTS ix_apps_slug ON apps (slug)"
    ))
    conn.commit()
  if "icon_png" not in apps_cols:
    with eng.connect() as conn:
      conn.execute(text("ALTER TABLE apps ADD COLUMN icon_png BLOB NULL"))
      conn.commit()
  if "chats" in tables:
    chats_cols = {c["name"] for c in inspector.get_columns("chats")}
    _add = []
    if "uploads" not in chats_cols:
      _add.append("ALTER TABLE chats ADD COLUMN uploads JSON NOT NULL DEFAULT '[]'")
    if "pending_messages" not in chats_cols:
      _add.append(
        "ALTER TABLE chats ADD COLUMN pending_messages JSON NOT NULL DEFAULT '[]'"
      )
    if "generated_images" not in chats_cols:
      _add.append("ALTER TABLE chats ADD COLUMN generated_images JSON NOT NULL DEFAULT '[]'")
    if "deleted_at" not in chats_cols:
      _add.append("ALTER TABLE chats ADD COLUMN deleted_at DATETIME")
    if "session_id" not in chats_cols:
      _add.append("ALTER TABLE chats ADD COLUMN session_id VARCHAR(128)")
    if "provider" not in chats_cols:
      _add.append(
        "ALTER TABLE chats ADD COLUMN provider VARCHAR(32) "
        "NOT NULL DEFAULT 'claude'"
      )
    if "agent_settings_json" not in chats_cols:
      # Nullable JSON blob holding per-chat overrides for the agent
      # runtime (model, effort, ...). Null means "fall back to the
      # global default in /data/shared/agent-settings.json".
      _add.append(
        "ALTER TABLE chats ADD COLUMN agent_settings_json JSON"
      )
    if "pinned_at" not in chats_cols:
      # NOT NULL = pinned. Drawer sort key (see routes/chats.py).
      _add.append("ALTER TABLE chats ADD COLUMN pinned_at DATETIME NULL")
    if _add:
      with eng.connect() as conn:
        for stmt in _add:
          conn.execute(text(stmt))
        conn.commit()

  if "owner" in tables:
    owner_cols = {c["name"] for c in inspector.get_columns("owner")}
    _add_owner = []
    if "provider" not in owner_cols:
      _add_owner.append(
        "ALTER TABLE owner ADD COLUMN provider VARCHAR(32) "
        "NOT NULL DEFAULT 'claude'"
      )
    if "model_prefs_json" not in owner_cols:
      # Nullable JSON blob holding the owner's model-picker
      # preferences (e.g. hidden model IDs). Null = "show
      # everything" — no backfill needed; the picker treats
      # absence as the default state. See models.Owner for the
      # schema.
      _add_owner.append(
        "ALTER TABLE owner ADD COLUMN model_prefs_json JSON"
      )
    if "walkthrough_completed_at" not in owner_cols:
      # NULL = "show the walkthrough." No backfill: existing owners
      # of this single-owner-per-install platform will see the
      # walkthrough exactly once on their next sign-in, which is
      # the explicitly chosen rollout for the new onboarding.
      _add_owner.append(
        "ALTER TABLE owner ADD COLUMN walkthrough_completed_at DATETIME"
      )
    if _add_owner:
      with eng.connect() as conn:
        for stmt in _add_owner:
          conn.execute(text(stmt))
        conn.commit()


def get_db():
  """Yields a database session and closes it after the request."""
  db = SessionLocal()
  try:
    yield db
  finally:
    db.close()
