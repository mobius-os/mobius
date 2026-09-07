"""App data reset preserves linked Project sources and their recovery window."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app import models
from app.config import get_settings
from app.timeutil import now_naive_utc


def _fixture(db, *, source_project=False, deleted=False, absolute=False):
  data = Path(get_settings().data_dir)
  source = data / "apps" / "pages"
  source.mkdir(parents=True, exist_ok=True)
  (source / "index.jsx").write_text("source stays")
  app = models.App(name="Pages", slug="pages", source_dir=str(source))
  db.add(app)
  db.commit()
  storage = data / "apps" / str(app.id)
  storage.mkdir(parents=True, exist_ok=True)
  (storage / "state.json").write_text('{"keep": true}')
  root = source if source_project else storage / "sources" / "document"
  root.mkdir(parents=True, exist_ok=True)
  (root / "main.tex").write_text("editable document")
  project = models.Project(
    id="project-with-source", name="Document", root_path=(
      str(root) if absolute else root.relative_to(data).as_posix()
    ),
    template_snapshot_json={"imported_from": {
      "management": "linked", "kind": "app" if source_project else "artifact",
      "id": str(app.id) if source_project else "document",
    }},
    deleted_at=now_naive_utc() if deleted else None,
  )
  db.add(project)
  db.commit()
  return app, project, source, storage, root


@pytest.mark.parametrize("deleted", [False, True])
@pytest.mark.parametrize("absolute", [False, True])
def test_data_reset_refuses_active_and_recoverable_linked_sources_before_side_effects(
  client, auth, db, monkeypatch, deleted, absolute,
):
  from app.routes import apps
  app, project, source, storage, root = _fixture(db, deleted=deleted, absolute=absolute)
  nonce = app.token_nonce
  revoke = AsyncMock()
  monkeypatch.setattr(apps, "_revoke_app_publish_tokens", revoke)
  response = client.delete(f"/api/apps/{app.id}/data", headers=auth)
  assert response.status_code == 409, response.text
  assert response.json()["detail"]["code"] == "app_data_has_project_source"
  assert response.json()["detail"]["project_id"] == project.id
  assert (root / "main.tex").read_text() == "editable document"
  assert (storage / "state.json").exists()
  assert (source / "index.jsx").exists()
  revoke.assert_not_awaited()
  db.refresh(app)
  assert app.token_nonce == nonce


@pytest.mark.parametrize("deleted", [False, True])
def test_source_only_app_project_does_not_block_runtime_data_reset(
  client, auth, db, deleted,
):
  app, _, source, storage, root = _fixture(db, source_project=True, deleted=deleted)
  nonce = app.token_nonce
  response = client.delete(f"/api/apps/{app.id}/data", headers=auth)
  assert response.status_code == 204, response.text
  assert not storage.exists()
  assert (root / "main.tex").read_text() == "editable document"
  assert (source / "index.jsx").exists()
  db.refresh(app)
  assert app.token_nonce != nonce


def test_project_in_neighboring_numeric_app_storage_does_not_block_reset(client, auth, db):
  app, project, _, storage, root = _fixture(db)
  data = Path(get_settings().data_dir)
  neighbor = data / "apps" / f"{app.id}0" / "sources" / "document"
  neighbor.mkdir(parents=True)
  (neighbor / "main.tex").write_text("other app")
  project.root_path = neighbor.relative_to(data).as_posix()
  db.commit()
  response = client.delete(f"/api/apps/{app.id}/data", headers=auth)
  assert response.status_code == 204, response.text
  assert not storage.exists()
  assert (neighbor / "main.tex").read_text() == "other app"
