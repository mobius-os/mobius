"""Database engine and session configuration.

Served from the editable platform checkout. main.py imports this at module load
to set up the engine and migrations; if a local edit breaks it, normal boot
falls back to the baked platform while preserving the checkout for operator
repair. For ad-hoc DB queries use raw stdlib `sqlite3` instead of changing
this module.
"""

import logging
import os
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


def set_database_request_label(label: str) -> Token:
  return _request_label.set(label)


def reset_database_request_label(token: Token) -> None:
  _request_label.reset(token)


def _make_engine():
  """Creates the SQLAlchemy engine, ensuring the DB directory exists."""
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
