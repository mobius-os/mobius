"""SQLite connections install their lock handler before lock-taking pragmas."""

from types import SimpleNamespace

from app import database


def test_sqlite_connection_pragma_order(monkeypatch, tmp_path):
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

  assert connection.connection_cursor.statements == [
    "PRAGMA busy_timeout=5000",
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=FULL",
  ]
