"""The vision-agent replacement changes selections, never past executions."""
import json
from pathlib import Path

from sqlalchemy import create_engine, text

from app.schema_migrations import _migrate_three_mobius_agent_selections


def test_retired_selections_migrate_without_touching_history_or_other_providers(tmp_path, monkeypatch):
  monkeypatch.setenv("DATA_DIR", str(tmp_path))
  settings_path = tmp_path / "shared" / "agent-settings.json"
  settings_path.parent.mkdir()
  settings_path.write_text(json.dumps({
    "model": "prism", "effort": "high", "unrelated": {"model": "spark"},
    "background_agents": {"providers": [
      {"provider": "mobius", "model": "spark", "enabled": True},
      {"provider": "codex", "model": "gpt-test"},
      {"provider": "custom", "model": "spark"},
    ]},
  }))
  settings_path.chmod(0o640)
  eng = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE chats (id TEXT PRIMARY KEY, provider TEXT, agent_settings_json JSON, messages TEXT)"))
    conn.execute(text("CREATE TABLE delegations (id TEXT PRIMARY KEY, provider TEXT, model TEXT)"))
    conn.execute(text("CREATE TABLE owner (id INTEGER PRIMARY KEY, model_prefs_json JSON)"))
    conn.execute(text("CREATE TABLE execution_history (model TEXT)"))
    for id, provider, model in [("a", "mobius", "spark"), ("b", "mobius", "prism"),
                                ("c", "mobius", "evolve"), ("d", "mobius", "reflect"),
                                ("e", "mobius", "inkling"), ("f", "custom", "spark"),
                                ("g", "claude", "claude-test"), ("h", "mobius", "future")]:
      conn.execute(text("INSERT INTO chats VALUES (:id, :provider, :settings, :messages)"),
                   {"id": id, "provider": provider, "settings": json.dumps({"model": model, "effort": "high"}),
                    "messages": '[{"text":"Spark and Prism","model":"spark"}]'})
      conn.execute(text("INSERT INTO delegations VALUES (:id, :provider, :model)"),
                   {"id": id, "provider": provider, "model": model})
    conn.execute(text("INSERT INTO owner VALUES (1, :prefs)"), {"prefs": json.dumps({"hidden_ids": ["spark", "flow", "prism", "claude-test"]})})
    conn.execute(text("INSERT INTO execution_history VALUES ('prism')"))
  _migrate_three_mobius_agent_selections(eng)
  once = settings_path.read_bytes()
  _migrate_three_mobius_agent_selections(eng)
  assert settings_path.read_bytes() == once
  assert settings_path.stat().st_mode & 0o777 == 0o640
  with eng.connect() as conn:
    rows = {r.id: json.loads(r.agent_settings_json) for r in conn.execute(text("SELECT * FROM chats"))}
    assert {k: v["model"] for k, v in rows.items()} == dict(zip("abcdefgh", ["flow", "flow", "flow", "reflect", "flow", "spark", "claude-test", "future"]))
    assert all(v["effort"] == "high" for v in rows.values())
    assert set(conn.execute(text("SELECT messages FROM chats")).scalars()) == {'[{"text":"Spark and Prism","model":"spark"}]'}
    assert conn.execute(text("SELECT model FROM execution_history")).scalar_one() == "prism"
    assert dict(conn.execute(text("SELECT id, model FROM delegations")).fetchall()) == {k: v["model"] for k, v in rows.items()}
    assert json.loads(conn.execute(text("SELECT model_prefs_json FROM owner")).scalar_one())["hidden_ids"] == ["flow", "claude-test"]
  saved = json.loads(once)
  assert saved["model"] == "flow"
  assert saved["unrelated"] == {"model": "spark"}
  assert [r["model"] for r in saved["background_agents"]["providers"]] == ["flow", "gpt-test", "spark"]
  eng.dispose()


def test_invalid_settings_and_empty_database_are_preserved(tmp_path, monkeypatch):
  monkeypatch.setenv("DATA_DIR", str(tmp_path))
  path = tmp_path / "shared" / "agent-settings.json"
  path.parent.mkdir()
  path.write_bytes(b'{invalid spark')
  eng = create_engine("sqlite://")
  _migrate_three_mobius_agent_selections(eng)
  assert path.read_bytes() == b'{invalid spark'
  eng.dispose()


def test_catalogue_has_only_new_visible_names_and_honest_modalities():
  catalogue = json.loads((Path(__file__).parents[1] / "app" / "mobius_models.json").read_text())
  assert [r["label"] for r in catalogue["models"]] == ["GLM 5.3 Flash", "DeepSeek 4.1 Flash"]
  by_id = {r["id"]: r for r in catalogue["models"]}
  assert set(by_id) == {"flow", "reflect"}
  assert catalogue["default_model"] == "flow"
  assert by_id["flow"]["input_modalities"] == ["text", "image"]
  assert by_id["reflect"]["input_modalities"] == ["text", "image"]
  assert all(r["auto_compact_token_limit"] < r["context_window"] <= 1048576 for r in by_id.values())


def test_failed_settings_replace_preserves_file_and_can_resume(tmp_path, monkeypatch):
  import os
  import pytest

  monkeypatch.setenv("DATA_DIR", str(tmp_path))
  path = tmp_path / "shared" / "agent-settings.json"
  path.parent.mkdir()
  original = b'{"model": "spark", "effort": "high"}\n'
  path.write_bytes(original)
  eng = create_engine(f"sqlite:///{tmp_path / 'retry.db'}")
  with eng.begin() as conn:
    conn.execute(text("CREATE TABLE chats (id TEXT PRIMARY KEY, provider TEXT, agent_settings_json JSON)"))
    conn.execute(text("INSERT INTO chats VALUES ('a', 'mobius', :settings)"),
                 {"settings": '{"model": "prism"}'})

  def failed_replace(*args):
    raise OSError("simulated disk failure")

  with monkeypatch.context() as failure:
    failure.setattr(os, "replace", failed_replace)
    with pytest.raises(OSError, match="simulated disk failure"):
      _migrate_three_mobius_agent_selections(eng)
  assert path.read_bytes() == original
  assert list(path.parent.glob(".agent-settings.*")) == []
  with eng.connect() as conn:
    assert json.loads(conn.execute(text("SELECT agent_settings_json FROM chats")).scalar_one()) == {"model": "flow"}
  _migrate_three_mobius_agent_selections(eng)
  assert json.loads(path.read_text()) == {"model": "flow", "effort": "high"}
  eng.dispose()


def test_other_provider_global_default_is_not_reinterpreted(tmp_path, monkeypatch):
  monkeypatch.setenv("DATA_DIR", str(tmp_path))
  path = tmp_path / "shared" / "agent-settings.json"
  path.parent.mkdir()
  original = b'{"provider":"custom","model":"spark"}\n'
  path.write_bytes(original)
  eng = create_engine("sqlite://")
  _migrate_three_mobius_agent_selections(eng)
  assert path.read_bytes() == original
  eng.dispose()
