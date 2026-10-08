"""Database engine and session configuration.

Served from the editable platform checkout. main.py imports this at module load
to set up the engine and migrations; if a local edit breaks it, normal boot
falls back to the baked platform while preserving the checkout for operator
repair. For ad-hoc DB queries use raw stdlib `sqlite3` instead of changing
this module.
"""

import logging
import os
import sys
import threading
import time
from contextvars import ContextVar, Token
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from app import sqlite_policy
from app.config import get_settings


_log = logging.getLogger(__name__)
sqlite_policy.install_adapters()
_request_label: ContextVar[str] = ContextVar(
  "mobius_database_request_label", default="background",
)
_checkout_warn_seconds = float(os.environ.get("DB_CHECKOUT_WARN_SECONDS", "2"))
_pool_metrics_lock = threading.Lock()
_pool_metrics = {
  "checked_out": 0,
  "checkouts": 0,
  "long_checkouts": 0,
  "max_checkout_ms": 0,
  "last_long_checkout": None,
}


def _assert_test_database_isolated() -> None:
  """Refuse to construct an application engine in an unmarked test process.

  Pytest imports a test module before it registers a module-level
  ``pytest_plugins`` declaration. An out-of-tree probe can therefore import
  ``app.database`` while it still inherits the live service environment, then
  load the normal fixtures whose schema reset drops production tables. Every
  supported test entrypoint marks its database as disposable before Python
  starts; anything else must fail before SQLAlchemy creates an engine.
  """
  running_tests = (
    os.environ.get("MOBIUS_TEST_RUNTIME") == "1"
    or "pytest" in sys.modules
    or any("pytest" in Path(arg).name for arg in sys.argv[:2])
  )
  if (
    running_tests
    and os.environ.get("MOBIUS_TEST_DATABASE_ISOLATED") != "1"
  ):
    raise RuntimeError(
      "Refusing to create a database engine in an unisolated test process. "
      "Use scripts/wt-pytest.sh or the disposable test Compose service."
    )


def set_database_request_label(label: str) -> Token:
  return _request_label.set(label)


def reset_database_request_label(token: Token) -> None:
  _request_label.reset(token)


class IntegerOutOfRange(ValueError):
  """A bound integer (usually an id from a URL) exceeds the database range."""


def _make_engine():
  """Creates the SQLAlchemy engine, ensuring the DB directory exists."""
  _assert_test_database_isolated()
  settings = get_settings()
  is_sqlite = settings.database_url.startswith("sqlite")
  if settings.database_url.startswith("sqlite:////"):
    db_path = Path(settings.database_url.replace("sqlite:////", "/"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
  connect_args = {"check_same_thread": False} if is_sqlite else {}
  # Pool hardening for Postgres (Railway et al.). Defaults (QueuePool
  # 5 + 10 overflow) stay, but pre_ping validates a connection before
  # handing it out — Railway silently drops idle Postgres connections,
  # and without this the first query on a stale one raises instead of
  # transparently reconnecting. pool_recycle caps connection age below
  # any server-side idle timeout. Omitted for SQLite, whose pool is
  # process-local and never sees these failure modes.
  # SQLite uses NullPool so temporarily-held request sessions cannot exhaust
  # an artificial QueuePool ceiling. Postgres retains bounded QueuePool reuse.
  pool_kwargs = (
    {"poolclass": NullPool}
    if is_sqlite
    else {"pool_pre_ping": True, "pool_recycle": 1800}
  )
  eng = create_engine(
    settings.database_url, connect_args=connect_args, **pool_kwargs
  )

  @event.listens_for(eng, "checkout")
  def _track_checkout(_dbapi_conn, connection_record, _connection_proxy):
    label = _request_label.get()
    connection_record.info["mobius_checkout"] = (time.monotonic(), label)
    with _pool_metrics_lock:
      _pool_metrics["checked_out"] += 1
      _pool_metrics["checkouts"] += 1

  @event.listens_for(eng, "checkin")
  def _track_checkin(_dbapi_conn, connection_record):
    started = connection_record.info.pop("mobius_checkout", None)
    if not started:
      return
    started_at, label = started
    elapsed_ms = max(0, round((time.monotonic() - started_at) * 1000))
    is_long = elapsed_ms >= round(_checkout_warn_seconds * 1000)
    with _pool_metrics_lock:
      _pool_metrics["checked_out"] = max(0, _pool_metrics["checked_out"] - 1)
      _pool_metrics["max_checkout_ms"] = max(
        _pool_metrics["max_checkout_ms"], elapsed_ms,
      )
      if is_long:
        _pool_metrics["long_checkouts"] += 1
        _pool_metrics["last_long_checkout"] = {
          "request": label,
          "duration_ms": elapsed_ms,
        }
    if is_long:
      _log.warning(
        "Database connection checked out for %dms by %s",
        elapsed_ms,
        label,
      )
  @event.listens_for(eng, "handle_error")
  def _reject_out_of_range_integer(context):
    # sqlite3 refuses to bind an int outside 64 bits with a bare OverflowError
    # (e.g. GET /api/apps/99999999999999999999). No row can carry such a
    # value, so report it as invalid input instead of an internal error.
    if isinstance(context.original_exception, OverflowError):
      raise IntegerOutOfRange(
        "A number in this request is too large.",
      ) from context.original_exception

  if is_sqlite:
    # NullPool opens a fresh connection per session, so this runs constantly
    # under load. The policy itself lives in sqlite_policy so the standalone
    # scripts writing this same database apply it identically.
    @event.listens_for(eng, "connect")
    def _set_sqlite_pragmas(dbapi_conn, _record):
      cur = dbapi_conn.cursor()
      for pragma in sqlite_policy.connection_pragmas():
        cur.execute(pragma)
      cur.close()
  return eng


engine = _make_engine()


def reclaim_startup_database_file_cache() -> dict | None:
  """Advise away clean pages of the main SQLite file after a healthy boot.

  Migrations and startup reconciliation read much of the database once; on
  hosts that charge page cache to the container those clean pages stay
  billable long after boot. This is a one-shot startup policy, not pool or
  per-request cleanup. Only an ordinary absolute filename from the active
  engine qualifies: opening a connection to ask SQLite for its filename could
  checkpoint WAL when that last connection closes, so URI, relative and
  in-memory configurations get no advice. WAL/SHM files, attached databases
  and SQLite's own page-cache settings stay untouched.
  """
  if engine.dialect.name != "sqlite":
    return None
  filename = engine.url.database
  if (
    not filename
    or engine.url.query.get("uri")
    or not Path(filename).is_absolute()
  ):
    return None
  from app.file_cache import reclaim_file_cache

  return reclaim_file_cache([filename])


_wal_anchor = None


def open_wal_anchor() -> bool:
  """Hold one idle SQLite connection for the server's lifetime.

  The engine uses NullPool, so every session closes its connection. When the
  last connection to a WAL database closes, SQLite checkpoints the log,
  fsyncs, and deletes the -wal/-shm files; the next connection recreates
  them. Under Möbius's mostly-serial traffic that happened on nearly every
  transaction (measured on /data: ~6 ms per small write transaction versus
  ~2 ms with another connection open). An idle anchor keeps the log alive
  across sessions. It never opens a transaction, so it never pins a read
  snapshot; automatic checkpoints and journal_size_limit still bound the
  log's size. It is opened after startup's schema work and is not part of
  the pool, so pool metrics and NullPool's no-ceiling property are unchanged.
  Idempotent; returns whether an anchor is held.
  """
  global _wal_anchor
  if _wal_anchor is not None:
    return True
  if engine.dialect.name != "sqlite":
    return False
  filename = engine.url.database
  if not filename or filename == ":memory:" or engine.url.query.get("uri"):
    return False
  import sqlite3
  try:
    conn = sqlite3.connect(filename, check_same_thread=False)
    for pragma in sqlite_policy.connection_pragmas():
      conn.execute(pragma).close()
  except sqlite3.Error:
    _log.warning("WAL anchor connection could not be opened", exc_info=True)
    return False
  _wal_anchor = conn
  return True


def close_wal_anchor() -> None:
  global _wal_anchor
  conn, _wal_anchor = _wal_anchor, None
  if conn is not None:
    try:
      conn.close()
    except Exception:
      _log.warning("WAL anchor connection did not close cleanly", exc_info=True)


def checked_out_connections() -> int:
  """Return live DB checkouts without depending on a concrete pool class."""
  with _pool_metrics_lock:
    return _pool_metrics["checked_out"]


SessionLocal = sessionmaker(
  autocommit=False, autoflush=False, bind=engine
)


def database_pool_snapshot() -> dict:
  """Owner-safe pool pressure and checkout-lifetime diagnostics."""
  pool = engine.pool

  def call_metric(name: str):
    method = getattr(pool, name, None)
    if not callable(method):
      return None
    try:
      return int(method())
    except (TypeError, ValueError):
      return None

  with _pool_metrics_lock:
    tracked_checked_out = _pool_metrics["checked_out"]
    lifetime = {
      "checkouts": _pool_metrics["checkouts"],
      "long_checkouts": _pool_metrics["long_checkouts"],
      "max_checkout_ms": _pool_metrics["max_checkout_ms"],
      "last_long_checkout": _pool_metrics["last_long_checkout"],
    }
  pool_checked_out = call_metric("checkedout")
  current = {
    # NullPool deliberately exposes no checkedout() method. The event-backed
    # counter is the authoritative cross-pool value in that case.
    "checked_out": (
      tracked_checked_out if pool_checked_out is None else pool_checked_out
    ),
    "checked_in": call_metric("checkedin"),
    "size": call_metric("size"),
    "overflow": call_metric("overflow"),
  }
  return {
    "type": type(pool).__name__,
    "current": {key: value for key, value in current.items() if value is not None},
    "lifetime": lifetime,
  }


class Base(DeclarativeBase):
  pass


def get_db():
  """Yields a database session and closes it after the request."""
  db = SessionLocal()
  try:
    yield db
  finally:
    db.close()
