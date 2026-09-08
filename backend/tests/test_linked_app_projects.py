"""Linked Projects edit one app; theme access never expands collaborator authority."""
import json
import os
from pathlib import Path

import pytest

from app import models
from app.project_templates import LINKED_APP_GUIDANCE
from app.schema_migrations import _link_app_project_runtime
from tests.test_project_collaboration import _invite, _redeem


@pytest.fixture
def linked(client, auth, db):
  source = Path(os.environ['DATA_DIR']) / 'apps' / 'linked-clock'
  source.mkdir(parents=True)
  (source / 'index.jsx').write_text('export default function App() { return null }')
  (source / 'mobius.json').write_text(json.dumps({'name': 'Clock', 'entry': 'index.jsx'}))
  app = models.App(name='Clock', slug='linked-clock', source_dir=str(source), jsx_source='last-working', system_app=False)
  db.add(app)
  db.commit()
  response = client.post('/api/projects/import', headers=auth, json={'kind': 'app', 'source_id': str(app.id)})
  assert response.status_code == 200, response.text
  return app, response.json(), source


def test_linked_import_has_one_app_no_duplicate_preview_and_explicit_update_guidance(client, auth, db, linked):
  app, project, source = linked
  assert project['artifacts'] == []
  assert project['template']['previews'] == []
  assert project['template']['guidance'] == LINKED_APP_GUIDANCE
  duplicate = client.post(f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={'id': 'copy', 'name': 'Copy', 'builder': 'app', 'source': 'index.jsx'})
  assert duplicate.status_code == 409
  assert 'Build & update app' in duplicate.text
  opened = client.get(f"/api/projects/{project['id']}/file?path=index.jsx", headers=auth).json()
  saved = client.put(f"/api/projects/{project['id']}/file?path=index.jsx", headers=auth,
    json={'content': '// saved draft', 'expected_revision': opened['revision']})
  assert saved.status_code == 200
  assert (source / 'index.jsx').read_text() == '// saved draft'
  db.refresh(app)
  assert app.jsx_source == 'last-working'


def test_linked_preview_migration_preserves_sources_other_outputs_and_old_copy_projects(client, auth, db, linked):
  app, project, source = linked
  row = db.get(models.Project, project['id'])
  row.artifacts_json = [{'id': 'app', 'builder': 'app'}, {'id': 'guide', 'builder': 'website'}]
  output = source / 'artifacts/app/output/index.html'
  output.parent.mkdir(parents=True)
  output.write_text('old preview kept for recovery')
  copy = models.Project(id='legacy-copy', name='Copy', project_type='app', root_path='projects/copy',
    template_snapshot_json={'imported_from': {'kind': 'app', 'id': str(app.id)}},
    artifacts_json=[{'id': 'app', 'builder': 'app'}])
  db.add(copy)
  db.commit()
  previous_time = row.updated_at
  _link_app_project_runtime(db.get_bind())
  _link_app_project_runtime(db.get_bind())
  db.expire_all()
  assert row.artifacts_json == [{'id': 'guide', 'builder': 'website'}]
  assert row.template_snapshot_json['retired_app_previews'] == ['app']
  assert row.template_snapshot_json['guidance'] == LINKED_APP_GUIDANCE
  assert row.updated_at == previous_time
  assert copy.artifacts_json == [{'id': 'app', 'builder': 'app'}]
  assert (source / 'index.jsx').is_file()
  assert output.read_text() == 'old preview kept for recovery'
  assert client.get(f"/api/projects/{project['id']}/artifacts/app/output/index.html", headers=auth).status_code == 410
  assert client.get(f"/api/projects/{project['id']}/artifacts", headers=auth).json()['artifacts'][0]['id'] == 'guide'


def test_theme_is_effective_read_only_and_confined_to_active_project_members(client, auth, db, linked):
  app, project, source = linked
  _, secret = _invite(client, auth, project['id'], role='editor')
  session, guest = _redeem(client, secret)
  url = f"/api/projects/{project['id']}/theme"
  expected = client.get('/api/theme', headers=auth).json()
  assert client.get(url, headers=guest).json() == expected
  assert client.get(url).status_code == 401
  assert client.put(url, headers=guest, json={'css': 'bad'}).status_code == 404
  assert client.get(url, headers=guest).json() == expected
  assert client.get('/api/storage/shared/theme.css', headers=guest).status_code == 403
  assert client.post('/api/apps/apply', headers=guest, json={'source_dir': str(source)}).status_code == 403
  assert client.get(f'/api/apps/{app.id}', headers=guest).status_code == 403
  # Shared edits still target the actual source folder.
  file_url = f"/api/projects/{project['id']}/file?path=index.jsx"
  revision = client.get(file_url, headers=guest).json()['revision']
  assert client.put(file_url, headers=guest, json={'content': '// collaborator draft', 'expected_revision': revision}).status_code == 200
  assert (source / 'index.jsx').read_text() == '// collaborator draft'
  other = client.post('/api/projects', headers=auth, json={'name': 'Private', 'template_id': 'blank'}).json()
  assert client.get(f"/api/projects/{other['id']}/theme", headers=guest).status_code == 404
  assert client.delete(f"/api/projects/{project['id']}/members/{session['member_id']}", headers=auth).status_code == 204
  assert client.get(url, headers=guest).status_code in (401, 403)
