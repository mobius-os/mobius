"""Runtime publication pointer upgrades preserve accepted source and app data."""

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from app import models
from app.schema_migrations import _add_app_runtime_revision


def test_existing_app_upgrade_adds_unpublished_runtime_without_guessing(tmp_path):
  engine = create_engine(f"sqlite:///{tmp_path / 'runtime-upgrade.db'}")
  models.Base.metadata.create_all(engine)
  with Session(engine) as session:
    session.add(models.App(id=1, name="Existing", slug="existing", description="",
                           source_dir="/data/apps/existing", source_commit="a" * 40,
                           jsx_source="export default () => null"))
    session.commit()
  with engine.begin() as connection:
    connection.execute(text("ALTER TABLE apps DROP COLUMN runtime_revision"))
  _add_app_runtime_revision(engine)
  _add_app_runtime_revision(engine)
  with Session(engine) as session:
    app = session.get(models.App, 1)
    assert app.runtime_revision is None
    assert app.source_commit == "a" * 40
    assert app.source_dir == "/data/apps/existing"
    app.runtime_revision = "b" * 64
    session.commit()
  with Session(engine) as session:
    assert session.get(models.App, 1).runtime_revision == "b" * 64


def test_fresh_schema_already_contains_nullable_runtime_pointer(tmp_path):
  engine = create_engine(f"sqlite:///{tmp_path / 'runtime-fresh.db'}")
  models.Base.metadata.create_all(engine)
  _add_app_runtime_revision(engine)
  column = next(c for c in inspect(engine).get_columns('apps') if c['name'] == 'runtime_revision')
  assert column['nullable'] is True
