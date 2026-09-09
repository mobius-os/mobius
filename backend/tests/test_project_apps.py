"""A packaged Project app works without shell-specific format or provider code."""
import asyncio
import json
import os
import shutil
from pathlib import Path

import pytest
from app import models, project_builders
from app.timeutil import now_naive_utc


def test_game_project_app_discovery_build_and_independent_source(client, auth, db):
  example = Path(__file__).resolve().parents[2] / 'examples/project-apps/game-studio'
  source = Path(os.environ['DATA_DIR']) / 'apps' / 'renamed-game-studio'
  shutil.copytree(example, source)
  manifest = json.loads((source / 'mobius.json').read_text())
  provider = models.App(name=manifest['name'], description=manifest['description'],
    jsx_source='', slug='renamed-game-studio', source_dir=str(source),
    version=manifest['version'], project_templates_json=manifest['project_templates'])
  db.add(provider)
  db.commit()
  key = 'renamed-game-studio:game'
  templates = client.get('/api/projects/templates', headers=auth).json()
  template = next(t for t in templates if t['key'] == key)
  assert template['kind'] == 'game'
  assert template['source_app_id'] == provider.id

  def create(name):
    response = client.post('/api/projects', headers=auth,
      json={'name': name, 'template_id': key})
    assert response.status_code == 200, response.text
    return response.json()

  project = create('First game')
  project_id = project['id']
  assert project['artifacts'][0]['builder'] == 'game'
  row = db.get(models.Project, project_id)
  from app.routes.projects import _project_root
  root = _project_root(row)
  initial = (root / 'scene.game.json').read_bytes()
  scene = json.loads(initial)
  scene['title'] = 'My custom level'
  (root / 'scene.game.json').write_text(json.dumps(scene))
  asyncio.run(project_builders.run_build(project_id, 'game'))
  url = f'/api/projects/{project_id}/artifacts/game/output/index.html'
  output = client.get(url, headers=auth)
  assert output.status_code == 200, output.text
  assert '<canvas' in output.text and 'My custom level' in output.text
  last_good = output.content
  second = create('Second game')
  assert (_project_root(db.get(models.Project, second['id'])) / 'scene.game.json').read_bytes() == initial
  assert (source / 'templates/scene.game.json').read_bytes() == initial

  (root / 'scene.game.json').write_text('invalid json')
  asyncio.run(project_builders.run_build(project_id, 'game'))
  artifact = client.get(f'/api/projects/{project_id}/artifacts', headers=auth).json()['artifacts'][0]
  assert artifact['status'] == 'error' and artifact['has_output']
  assert client.get(url, headers=auth).content == last_good
  assert not list((root / 'artifacts/game').glob('.build-*'))

  (root / 'scene.game.json').write_bytes(initial)
  provider.deleted_at = now_naive_utc()
  db.commit()
  assert key not in [t['key'] for t in client.get('/api/projects/templates', headers=auth).json()]
  asyncio.run(project_builders.run_build(project_id, 'game'))
  assert client.get(url, headers=auth).content == last_good
  assert (root / 'scene.game.json').read_bytes() == initial
  assert 'provider is unavailable' in (root / 'artifacts/game/build.log').read_text()


def test_failed_publication_restores_previous_creation(tmp_path, monkeypatch):
  output = tmp_path / 'output'
  output.mkdir()
  (output / 'index.html').write_text('previous')
  staged = tmp_path / 'staged'
  staged.mkdir()
  (staged / 'index.html').write_text('new')
  rename = Path.rename
  def fail_new(self, target):
    if self == staged:
      raise OSError('publication failed')
    return rename(self, target)
  monkeypatch.setattr(Path, 'rename', fail_new)
  with pytest.raises(OSError, match='publication failed'):
    project_builders._publish_build_output(staged, output)
  assert (output / 'index.html').read_text() == 'previous'


@pytest.mark.parametrize('outcome', ['cancel', 'missing_output', 'replace'])
def test_rebuild_publishes_only_complete_output(client, auth, monkeypatch, outcome):
  project = client.post('/api/projects', headers=auth,
    json={'name': 'Build fixture', 'template_id': 'blank'}).json()
  project_id = project['id']
  saved = client.put(f'/api/projects/{project_id}/file?path=index.html', headers=auth,
    json={'content': 'old', 'expected_revision': None})
  assert saved.status_code == 200
  created = client.post(f'/api/projects/{project_id}/artifacts', headers=auth,
    json={'id': 'site', 'name': 'Site', 'builder': 'website', 'source': 'index.html'})
  assert created.status_code == 201
  asyncio.run(project_builders.run_build(project_id, 'site'))
  async def build(**kwargs):
    if outcome == 'cancel':
      raise asyncio.CancelledError()
    if outcome == 'replace':
      (kwargs['output_dir'] / 'index.html').write_text('new')
  monkeypatch.setitem(project_builders.BUILDERS, 'website', build)
  if outcome == 'cancel':
    with pytest.raises(asyncio.CancelledError):
      asyncio.run(project_builders.run_build(project_id, 'site'))
  else:
    asyncio.run(project_builders.run_build(project_id, 'site'))
  output = client.get(f'/api/projects/{project_id}/artifacts/site/output/index.html', headers=auth)
  assert output.status_code == 200
  assert output.text == ('new' if outcome == 'replace' else 'old')
  artifact = client.get(f'/api/projects/{project_id}/artifacts', headers=auth).json()['artifacts'][0]
  assert artifact['status'] == ('ok' if outcome == 'replace' else 'error')
  assert artifact['has_output']


def test_core_app_project_builds_without_any_provider(client, auth, db):
  templates = client.get('/api/projects/templates', headers=auth).json()
  assert [t['key'] for t in templates] == ['blank', 'app']
  created = client.post('/api/projects', headers=auth,
    json={'name': 'Independent app', 'template_id': 'app'})
  assert created.status_code == 200, created.text
  project = created.json()
  assert project['source_app_id'] is None
  assert project['template']['kind'] == 'mini-app'
  assert project['artifacts'][0]['builder'] == 'app'
  from app.routes.projects import _project_root
  root = _project_root(db.get(models.Project, project['id']))
  assert (root / 'mobius.json').is_file()
  source = root / 'index.jsx'
  source.write_text("import {useState} from 'react'; export default function App(){const [n,setN]=useState(0);return <button onClick={()=>setN(n+1)}>Core app {n}</button>}")
  (root / 'node_modules/private').mkdir(parents=True)
  (root / 'node_modules/private/secret.txt').write_text('do not publish')
  (root / 'linked.txt').symlink_to(root / 'node_modules/private/secret.txt')
  asyncio.run(project_builders.run_build(project['id'], 'app'))
  artifacts = client.get(f"/api/projects/{project['id']}/artifacts", headers=auth).json()['artifacts']
  log = (root / 'artifacts/app/build.log').read_text()
  assert artifacts[0]['status'] == 'ok', log
  output = root / 'artifacts/app/output'
  assert not (output / 'node_modules').exists()
  assert not (output / 'linked.txt').exists()
  assert not list(output.glob('mobius-preview-*'))
  result = client.get(f"/api/projects/{project['id']}/artifacts/app/output/index.html", headers=auth)
  assert result.status_code == 200
  assert 'Core app' in result.text and '<script type="module">' in result.text
  second = client.post('/api/projects', headers=auth,
    json={'name': 'Second app', 'template_id': 'app'}).json()
  assert 'Core app' not in (_project_root(db.get(models.Project, second['id'])) / 'index.jsx').read_text()


def test_retired_provider_template_preserves_existing_build_and_import_contract(client, auth, db):
  from sqlalchemy.orm.attributes import flag_modified
  example = Path(__file__).resolve().parents[2] / 'examples/project-apps/game-studio'
  source = Path(os.environ['DATA_DIR']) / 'apps/game'
  shutil.copytree(example, source)
  template = json.loads((source / 'mobius.json').read_text())['project_templates'][0]
  app = models.App(name='Game', description='Example', jsx_source='', slug='game', source_dir=str(source), project_templates_json=[template])
  db.add(app); db.commit()
  project = client.post('/api/projects', headers=auth, json={'name':'Existing game','template_id':'game:game'}).json()
  app.project_templates_json = [{**template, 'retired': True}]
  flag_modified(app, 'project_templates_json'); db.commit()
  assert 'game:game' not in [t['key'] for t in client.get('/api/projects/templates', headers=auth).json()]
  assert client.post('/api/projects', headers=auth, json={'name':'New game','template_id':'game:game'}).status_code == 422
  from app.routes.projects import _installed_template
  assert _installed_template(db, 'game:game') is not None, 'source imports retain their editable format'
  asyncio.run(project_builders.run_build(project['id'], 'game'))
  artifact = client.get(f"/api/projects/{project['id']}/artifacts", headers=auth).json()['artifacts'][0]
  assert artifact['status'] == 'ok'
