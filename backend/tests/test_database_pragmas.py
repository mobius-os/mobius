"""SQLite connections install their lock handler before lock-taking pragmas."""

import importlib.util
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from app import database, sqlite_policy


def test_explicit_datetime_adapter_replaces_deprecated_python_default(
  tmp_path,
):
  sqlite_policy.install_adapters()
  value = datetime(2026, 8, 19, 12, 34, 56, 123456)

  with sqlite3.connect(tmp_path / "adapter.db") as connection:
    connection.execute("CREATE TABLE events (occurred_at TEXT NOT NULL)")
    connection.execute("INSERT INTO events VALUES (?)", (value,))
    stored = connection.execute(
      "SELECT occurred_at FROM events"
    ).fetchone()[0]

  assert stored == "2026-08-19 12:34:56.123456"


def _capture_connect_pragmas(monkeypatch, tmp_path):
  """Run _make_engine with a fake engine and return the pragmas a new
  connection executes."""
  listeners = {}

  def _listens_for(_engine, event_name):
    def _register(callback):
      listeners[event_name] = callback
      return callback

    return _register

  class _Cursor:
    def __init__(self):
      self.statements = []

    def execute(self, statement):
      self.statements.append(statement)

    def close(self):
      return None

  class _Connection:
    def __init__(self):
      self.connection_cursor = _Cursor()

    def cursor(self):
      return self.connection_cursor

  monkeypatch.setattr(database.event, "listens_for", _listens_for)
  monkeypatch.setattr(database, "create_engine", lambda *_a, **_k: object())
  monkeypatch.setattr(
    database,
    "get_settings",
    lambda: SimpleNamespace(
      database_url=f"sqlite:///{tmp_path / 'pragma.db'}",
    ),
  )

  database._make_engine()
  connection = _Connection()
  listeners["connect"](connection, object())
  return connection.connection_cursor.statements


def test_sqlite_connection_pragma_order(monkeypatch, tmp_path):
  """busy_timeout must precede journal_mode=WAL: WAL takes locks, and with no
  busy handler installed it fails immediately instead of waiting."""
  assert _capture_connect_pragmas(monkeypatch, tmp_path) == [
    "PRAGMA busy_timeout=5000",
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=FULL",
    f"PRAGMA journal_size_limit={sqlite_policy.RETAINED_JOURNAL_LIMIT_BYTES}",
  ]


def test_sqlite_journal_limit_reaches_a_real_connection(tmp_path):
  """The list above only proves what we emit. SQLite silently ignores a
  malformed or out-of-range pragma, so read the value back off a live
  connection: without a limit the default is -1 and the WAL is never
  truncated, retaining its high-water allocation forever."""
  con = sqlite3.connect(tmp_path / "pragma.db")
  try:
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
      f"PRAGMA journal_size_limit={sqlite_policy.RETAINED_JOURNAL_LIMIT_BYTES}"
    )

    assert con.execute("PRAGMA journal_size_limit").fetchone()[0] == (
      sqlite_policy.RETAINED_JOURNAL_LIMIT_BYTES
    )
  finally:
    con.close()


def test_standalone_writer_applies_the_same_policy_as_the_engine(tmp_path):
  """chat_note.py opens this database directly, outside SQLAlchemy. Because
  journal_size_limit is per-connection, a writer that skips it leaves the WAL's
  retained allocation in place no matter what the engine's connections declare.
  Exercise its configurator rather than reading its source, so the test tracks
  the behaviour and not the wiring."""
  spec = importlib.util.spec_from_file_location(
    "chat_note_under_test",
    Path(__file__).resolve().parents[1] / "scripts" / "chat_note.py",
  )
  chat_note = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(chat_note)

  con = sqlite3.connect(tmp_path / "note.db")
  try:
    con.execute("PRAGMA journal_mode=WAL")
    chat_note._apply_sqlite_policy(con)

    assert con.execute("PRAGMA journal_size_limit").fetchone()[0] == (
      sqlite_policy.RETAINED_JOURNAL_LIMIT_BYTES
    )
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == (
      sqlite_policy.BUSY_TIMEOUT_MS
    )
  finally:
    con.close()
