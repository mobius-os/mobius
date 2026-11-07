"""Upgrade recovery of legacy claim revocations never depends on current ownership."""
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from app import models, schema_migrations as migrations
from app.chat_writer import create_chat


@pytest.mark.parametrize("source", ["fresh", "previous-release"])
def test_revocation_outbox_converges_from_fresh_and_frozen_release(tmp_path, source):
  path = tmp_path / "upgrade.db"
  if source == "previous-release":
    fixture = Path(__file__).parent / "fixtures" / "schema_0013.sql"
    with sqlite3.connect(path) as conn:
      conn.executescript(fixture.read_text())
  eng = create_engine(f"sqlite:///{path}")
  try:
    models.Base.metadata.create_all(eng)
    migrations.run_migrations(eng)
    history = migrations.schema_migration_history(eng)
    migrations.run_migrations(eng)
    assert migrations.schema_migration_history(eng) == history
    assert {"0101_claim_revocation_outbox", "0102_claim_revocation_outcomes"} <= {
      row["version"] for row in history
    }
    assert migrations.mapped_schema_gaps(eng) == []
  finally:
    eng.dispose()


def _legacy_claims(eng):
  models.Base.metadata.create_all(eng)
  with Session(eng) as db:
    owner = models.Owner(username="owner", hashed_password="unused")
    db.add(owner)
    db.add_all([create_chat(id="old", title="Old executor", messages=[]),
                create_chat(id="new", title="New executor", messages=[])])
    db.flush()
    for identity in ("pending", "completed", "released", "acknowledged", "reacquired"):
      row = models.AgentWorkClaim(
        id=identity, owner_id=owner.id, work_key="test:" + identity,
        summary="Shared action", owner_chat_id="new", owner_run_id="new-run",
        acquisition_id="acquisition-" + identity, previous_owner_chat_id="old",
        revision=3, transfer_notice_revision=2, notification_revision=1,
        takeover_reason="The previous executor handed this over.",
      )
      if identity == "completed":
        row.completed_at = row.updated_at = datetime(2026, 1, 1)
      if identity == "released":
        row.released_at = row.updated_at = datetime(2026, 1, 1)
      if identity == "acknowledged":
        row.notification_revision = 2
      if identity == "reacquired":
        row.previous_owner_chat_id = None
        row.transfer_notice_revision = None
      db.add(row)
    db.commit()
  models.AgentWorkClaimRevocation.__table__.drop(eng)


def test_upgrade_backfills_pending_notices_even_after_finish_or_release(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
  try:
    _legacy_claims(eng)
    migrations._add_claim_revocation_outbox(eng)
    # Migration retries cannot overwrite a durable snapshot or receipt.
    with eng.begin() as conn:
      conn.execute(text("UPDATE agent_work_claim_revocations SET delivered_at='2026-02-01' "
                        "WHERE claim_id='pending'"))
      conn.execute(text("UPDATE agent_work_claims SET takeover_reason='later mutable reason'"))
    migrations._add_claim_revocation_outbox(eng)
    migrations._add_claim_revocation_outcomes(eng)
    with Session(eng) as db:
      rows = db.query(models.AgentWorkClaimRevocation).order_by(
        models.AgentWorkClaimRevocation.claim_id,
      ).all()
      assert [row.claim_id for row in rows] == ["completed", "pending", "released"]
      assert all(row.revision == 2 and row.recipient_chat_id == "old" for row in rows)
      assert all("previous executor" in row.body for row in rows)
      assert next(row for row in rows if row.claim_id == "pending").delivered_at is not None
  finally:
    eng.dispose()


def test_interrupted_backfill_retries_before_recording_the_migration(tmp_path, monkeypatch):
  eng = create_engine(f"sqlite:///{tmp_path / 'interrupted.db'}")
  try:
    _legacy_claims(eng)
    version = "0101_claim_revocation_outbox"
    monkeypatch.setattr(migrations, "_SCHEMA_MIGRATIONS", (
      (version, migrations._add_claim_revocation_outbox),
    ))

    def fail_backfill(_conn, _cursor, statement, _params, _context, _many):
      if statement.startswith("INSERT INTO agent_work_claim_revocations"):
        raise RuntimeError("interrupted backfill")

    event.listen(eng, "before_cursor_execute", fail_backfill)
    with pytest.raises(RuntimeError, match="interrupted backfill"):
      migrations.run_migrations(eng)
    event.remove(eng, "before_cursor_execute", fail_backfill)
    assert migrations.schema_migration_history(eng) == []
    migrations.run_migrations(eng)
    assert [row["version"] for row in migrations.schema_migration_history(eng)] == [version]
    migrations._add_claim_revocation_outcomes(eng)
    with Session(eng) as db:
      assert db.query(models.AgentWorkClaimRevocation).count() == 3
  finally:
    eng.dispose()


def test_outcome_upgrade_preserves_delivery_and_non_delivery_closures(tmp_path):
  eng = create_engine(f"sqlite:///{tmp_path / 'outcomes.db'}")
  try:
    _legacy_claims(eng)
    migrations._add_claim_revocation_outbox(eng)
    with eng.begin() as conn:
      conn.execute(text("UPDATE agent_work_claim_revocations SET delivered_at='2026-02-01' "
                        "WHERE claim_id='pending'"))
    migrations._add_claim_revocation_outcomes(eng)
    with eng.begin() as conn:
      conn.execute(text("UPDATE agent_work_claim_revocations SET outcome='superseded' "
                        "WHERE claim_id='released'"))
    migrations._add_claim_revocation_outcomes(eng)
    with Session(eng) as db:
      rows = {row.claim_id: row for row in db.query(models.AgentWorkClaimRevocation).all()}
      assert rows['pending'].outcome == 'delivered'
      assert rows['pending'].delivered_at is not None
      assert rows['released'].outcome == 'superseded'
      assert rows['released'].delivered_at is None
      assert rows['completed'].outcome is None
  finally:
    eng.dispose()


def test_interrupted_outcome_backfill_retries_before_ledger_completion(tmp_path, monkeypatch):
  eng = create_engine(f"sqlite:///{tmp_path / 'interrupted-outcomes.db'}")
  try:
    _legacy_claims(eng)
    migrations._add_claim_revocation_outbox(eng)
    with eng.begin() as conn:
      conn.execute(text("UPDATE agent_work_claim_revocations SET delivered_at='2026-02-01' "
                        "WHERE claim_id='pending'"))
    version = "0102_claim_revocation_outcomes"
    monkeypatch.setattr(migrations, "_SCHEMA_MIGRATIONS", (
      (version, migrations._add_claim_revocation_outcomes),
    ))

    def fail_backfill(_conn, _cursor, statement, _params, _context, _many):
      if statement.startswith("UPDATE agent_work_claim_revocations SET outcome"):
        raise RuntimeError("interrupted outcome backfill")

    event.listen(eng, "before_cursor_execute", fail_backfill)
    with pytest.raises(RuntimeError, match="interrupted outcome backfill"):
      migrations.run_migrations(eng)
    event.remove(eng, "before_cursor_execute", fail_backfill)
    assert migrations.schema_migration_history(eng) == []
    migrations.run_migrations(eng)
    assert [row["version"] for row in migrations.schema_migration_history(eng)] == [version]
    with Session(eng) as db:
      assert db.get(models.AgentWorkClaimRevocation, ('pending', 2)).outcome == 'delivered'
  finally:
    eng.dispose()
