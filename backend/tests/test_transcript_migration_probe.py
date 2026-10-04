"""Readiness evidence must preserve data without pretending host-only is deployment proof."""

import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine

from app.database import Base
from app.schema_migrations import _create_chat_search_tables

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/test-transcript-migration.py"


def probe_module():
    spec = importlib.util.spec_from_file_location("migration_probe", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_copy(tmp_path, values):
  # Step-level hybrid fixture, not a complete previous-release image database.
    path = tmp_path / "source.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    _create_chat_search_tables(engine)
    with engine.begin() as con:
        con.exec_driver_sql("ALTER TABLE chats RENAME COLUMN messages_v1 TO messages")
        for cid, raw in values.items():
            con.exec_driver_sql(
                "INSERT INTO chats(id,title,title_locked,messages,has_messages,pending_messages,uploads,"
                "provider,auto_resume_on_limit,auto_resume_on_restart,created_at,updated_at) "
                "VALUES(?,'fixture',0,?,1,'[]','[]','claude',0,1,'2026-01-01','2026-01-01')",
                (cid, raw),
            )
    engine.dispose()
    return path


def invoke(source, output, *extra):
    return subprocess.run([sys.executable, str(SCRIPT), str(source), str(output), *extra],
                          capture_output=True, text=True, timeout=30)


def test_real_gate_preserves_typed_values_archives_and_source_bytes(tmp_path):
    raw = json.dumps([True, 1, 1.0, 2**80, {"role": "assistant", "id": "same"},
                      {"role": "user", "id": "same"}, None], indent=2)
    source = legacy_copy(tmp_path, {"one": raw, "damaged": "[truncated"})
    before = source.read_bytes()
    output = tmp_path / "proof"
    result = invoke(source, output, "--post-tasks")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["chats"] == 2 and report["messages"] == 8
    assert report["damaged_chats"] == 1
    assert report["all_message_values_types_and_positions_equal"]
    assert report["all_original_bytes_archived_exactly"]
    assert report["application_gate_within_budget"]
    assert report["post_batches"] > 0
    assert "simulated" in report["image_capability"]
    assert source.read_bytes() == before
    assert "same" not in result.stdout and "truncated" not in result.stdout
    assert json.loads((output / "result.json").read_text()) == report


def test_budget_failure_keeps_successful_conversion_evidence_without_changing_budget(tmp_path):
    source = legacy_copy(tmp_path, {"one": '[{"role":"user"}]'})
    result = invoke(source, tmp_path / "proof", "--readiness-budget-seconds", "0.000000001")
    assert result.returncode == 3, result.stderr
    report = json.loads(result.stdout)
    assert report["all_original_bytes_archived_exactly"]
    assert report["readiness_budget_seconds"] == 0.000000001
    assert not report["application_gate_within_budget"]


def test_existing_proof_is_not_overwritten(tmp_path):
    source = legacy_copy(tmp_path, {"one": "[]"})
    output = tmp_path / "proof"
    output.mkdir()
    (output / "result.json").write_text("keep")
    result = invoke(source, output)
    assert result.returncode != 0
    assert (output / "result.json").read_text() == "keep"
    assert not (output / "migration.db").exists()


def test_foreign_input_is_refused_before_any_output(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as con:
        con.execute("CREATE TABLE unrelated(id INTEGER)")
    output = tmp_path / "proof"
    result = invoke(source, output)
    assert result.returncode != 0
    assert not output.exists()


def test_configured_serving_database_and_symlink_are_refused_without_output(tmp_path, monkeypatch):
    source = legacy_copy(tmp_path, {"one": "[]"})
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{source}")
    link = tmp_path / "link.db"
    link.symlink_to(source)
    before = source.read_bytes()
    for path in (source, link):
        output = tmp_path / "proof"
        result = invoke(path, output)
        assert result.returncode != 0
        assert "configured serving database" in result.stderr
        assert not output.exists()
    assert source.read_bytes() == before


def test_backup_and_verification_retain_one_snapshot_when_source_changes(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE chats(id TEXT, messages TEXT)")
        con.execute("INSERT INTO chats VALUES('one','old')")
    target = tmp_path / "copy.db"
    probe = probe_module()
    with probe.copied_snapshot(source, target) as snapshot:
        with sqlite3.connect(source) as writer:
            writer.execute("UPDATE chats SET messages='new'")
        assert snapshot.execute("SELECT messages FROM chats").fetchone() == ("old",)
        with sqlite3.connect(target) as copied:
            assert copied.execute("SELECT messages FROM chats").fetchone() == ("old",)
    with sqlite3.connect(source) as after:
        assert after.execute("SELECT messages FROM chats").fetchone() == ("new",)
