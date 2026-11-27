"""Reader summaries upgrade without rewriting historical work evidence."""
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app import models, schema_migrations as migrations


@pytest.mark.parametrize("source", ["fresh", "previous-release"])
def test_work_report_schema_converges_from_fresh_and_frozen_release(tmp_path, source):
  path = tmp_path / "upgrade.db"
  if source == "previous-release":
    with sqlite3.connect(path) as conn:
      conn.executescript((Path(__file__).parent / "fixtures/schema_0013.sql").read_text())
  eng = create_engine(f"sqlite:///{path}")
  try:
    models.Base.metadata.create_all(eng)
    migrations.run_migrations(eng)
    history = migrations.schema_migration_history(eng)
    migrations.run_migrations(eng)
    assert migrations.schema_migration_history(eng) == history
    assert "0111_claim_work_report" in {row["version"] for row in history}
    assert migrations.mapped_schema_gaps(eng) == []
    assert "work_report" in {c["name"] for c in inspect(eng).get_columns("agent_work_claims")}
  finally:
    eng.dispose()


def test_existing_claim_evidence_is_unchanged_and_report_stays_absent(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
  try:
    with eng.begin() as conn:
      conn.execute(text("CREATE TABLE agent_work_claims (id TEXT, outcome TEXT)"))
      conn.execute(text("INSERT INTO agent_work_claims VALUES ('old', 'Verified only in isolation')"))
    migrations._add_claim_work_report(eng)
    migrations._add_claim_work_report(eng)
    with eng.connect() as conn:
      assert conn.execute(text("SELECT outcome, work_report FROM agent_work_claims")).one() == (
        "Verified only in isolation", None)
  finally:
    eng.dispose()
