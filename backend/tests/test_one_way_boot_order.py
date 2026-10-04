"""The legacy-column exception is narrow and closes before the writer boots."""

import sqlite3

import pytest
from sqlalchemy import create_engine

from app import main as main_module, one_way_upgrades
from app import startup


def _engine(tmp_path, name="legacy.db"):
  path = tmp_path / name
  return path, create_engine(f"sqlite:///{path}")


def _stub_init(monkeypatch, engine, gaps):
  monkeypatch.setattr(main_module, "engine", engine)
  monkeypatch.setattr(main_module.Base.metadata, "create_all", lambda **_kwargs: None)
  monkeypatch.setattr(main_module, "run_migrations", lambda _engine: None)
  monkeypatch.setattr(main_module, "mapped_schema_gaps", lambda _engine: list(gaps))


def test_legacy_gap_is_deferred_but_unrelated_gap_blocks_gate(tmp_path, monkeypatch):
  path, engine = _engine(tmp_path)
  with sqlite3.connect(path) as conn:
    conn.execute("CREATE TABLE chats(id TEXT PRIMARY KEY, messages JSON NOT NULL)")
  _stub_init(monkeypatch, engine, ["chats.messages_v1", "apps.connections_manage"])

  result = main_module._init_db()

  assert result.schema_gaps == ("apps.connections_manage",)
  assert not result.serviceable  # The startup plan never enters its gate.
  with sqlite3.connect(path) as conn:
    assert "messages" in [row[1] for row in conn.execute("PRAGMA table_info(chats)")]


def test_only_expected_gap_is_deferred_until_activation(tmp_path, monkeypatch):
  path, engine = _engine(tmp_path)
  with sqlite3.connect(path) as conn:
    conn.execute("CREATE TABLE chats(id TEXT PRIMARY KEY, messages JSON NOT NULL)")
  _stub_init(monkeypatch, engine, ["chats.messages_v1"])

  result = main_module._init_db()

  assert result.serviceable
  assert result.existing_tables == frozenset({"chats"})
  with sqlite3.connect(path) as conn:
    assert "messages_v1" not in [row[1] for row in conn.execute("PRAGMA table_info(chats)")]


def test_active_step_cannot_defer_missing_new_column(tmp_path, monkeypatch):
  from app import compat
  monkeypatch.setattr(compat, "COMPAT_LEVEL", 1)
  path, engine = _engine(tmp_path)
  with sqlite3.connect(path) as conn:
    conn.execute("CREATE TABLE chats(id TEXT PRIMARY KEY, messages JSON NOT NULL)")
    conn.execute("CREATE TABLE platform_upgrades(level INTEGER PRIMARY KEY, state TEXT)")
    conn.execute("INSERT INTO platform_upgrades VALUES(1, 'active')")
    conn.execute("CREATE TABLE chat_messages(chat_id TEXT, seq INTEGER)")
    conn.execute("CREATE TABLE chat_transcript_state(chat_id TEXT, message_count INTEGER)")
  _stub_init(monkeypatch, engine, ["chats.messages_v1"])

  result = main_module._init_db()

  assert result.schema_gaps == ("chats.messages_v1",)
  assert not result.serviceable


def test_damaged_floor_refuses_before_schema_writes(tmp_path, monkeypatch):
  path, engine = _engine(tmp_path)
  with sqlite3.connect(path) as conn:
    conn.execute("CREATE TABLE chats(id TEXT PRIMARY KEY, messages JSON NOT NULL)")
    conn.execute("CREATE TABLE platform_compat(id INTEGER PRIMARY KEY, floor INTEGER)")
  _stub_init(monkeypatch, engine, ["chats.messages_v1"])
  before = path.read_bytes()

  result = main_module._init_db()

  assert result.failure_reason == "compat_floor_damaged"
  assert path.read_bytes() == before


def test_gate_rechecks_schema_after_activation_before_writer(monkeypatch):
  from app import database, schema_migrations

  calls = []
  monkeypatch.setattr(one_way_upgrades, "run_gate", lambda *_args: calls.append("gate"))
  monkeypatch.setattr(schema_migrations, "mapped_schema_gaps", lambda _engine: ["chats.messages_v1"])
  context = type("Context", (), {
    "database_boot": startup.DatabaseBootResult(existing_tables=frozenset({"chats"})),
  })()

  with pytest.raises(one_way_upgrades.StepRefusal) as exc:
    startup._complete_one_way_upgrades(context)

  assert calls == ["gate"]
  assert exc.value.database_failure_reason == "schema_mismatch"
  assert dict(exc.value.database_failure_detail)["schema_gaps"] == ("chats.messages_v1",)


@pytest.mark.parametrize("missing", ["chat_messages", "chat_transcript_state"])
@pytest.mark.parametrize("ledger_present", [True, False])
def test_active_authority_missing_refuses_before_create_all(
  tmp_path, monkeypatch, missing, ledger_present,
):
  path, engine = _engine(tmp_path, "partial-restore.db")
  with sqlite3.connect(path) as conn:
    conn.execute("CREATE TABLE chats(id TEXT PRIMARY KEY, messages_v1 JSON NOT NULL)")
    conn.execute("CREATE TABLE chat_messages(chat_id TEXT, seq INTEGER, body JSON)")
    conn.execute("INSERT INTO chat_messages VALUES('chat', 0, '{\"content\":\"preserved\"}')")
    conn.execute("CREATE TABLE chat_transcript_state(chat_id TEXT, message_count INTEGER)")
    conn.execute("INSERT INTO chat_transcript_state VALUES('chat', 1)")
    conn.execute("CREATE TABLE platform_compat(id INTEGER PRIMARY KEY, floor INTEGER)")
    conn.execute("INSERT INTO platform_compat VALUES(1, 1)")
    if ledger_present:
      conn.execute("CREATE TABLE platform_upgrades(level INTEGER PRIMARY KEY, state TEXT)")
      conn.execute("INSERT INTO platform_upgrades VALUES(1, 'active')")
    conn.execute(f"DROP TABLE {missing}")
  monkeypatch.setattr(main_module, "engine", engine)

  def forbidden(*_args, **_kwargs):
    pytest.fail("A partial restore must be refused before any schema write")

  monkeypatch.setattr(main_module.Base.metadata, "create_all", forbidden)
  monkeypatch.setattr(main_module, "run_migrations", forbidden)
  before = path.read_bytes()

  result = main_module._init_db()

  assert not result.serviceable
  assert result.failure_reason == "upgrade_authority_missing"
  assert dict(result.failure_detail)["missing_tables"] == (missing,)
  assert path.read_bytes() == before
  with sqlite3.connect(path) as conn:
    assert missing not in {row[0] for row in conn.execute(
      "SELECT name FROM sqlite_master WHERE type='table'"
    )}
