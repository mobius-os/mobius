from sqlalchemy import create_engine, inspect, text

from app.database import run_migrations


def test_run_migrations_adds_manifest_url_to_existing_apps_table(tmp_path):
  db_path = tmp_path / "legacy.db"
  eng = create_engine(f"sqlite:///{db_path}")
  with eng.connect() as conn:
    conn.execute(text(
      "CREATE TABLE apps ("
      "id INTEGER PRIMARY KEY, "
      "name VARCHAR(255) NOT NULL"
      ")"
    ))
    conn.commit()

  run_migrations(eng)
  run_migrations(eng)

  inspector = inspect(eng)
  cols = {c["name"] for c in inspector.get_columns("apps")}
  indexes = {i["name"] for i in inspector.get_indexes("apps")}

  assert "manifest_url" in cols
  assert "ix_apps_manifest_url" in indexes
