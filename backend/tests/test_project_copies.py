"""Sharing source never transfers live project authority or runs recipient code."""
import base64
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import HTTPException
from app import models
from app.config import get_settings
from app.routes import project_copies as copies
from app.timeutil import now_naive_utc


def fixture_project(client, auth):
  project = client.post('/api/projects', headers=auth, json={'name': 'Original', 'template_id': 'blank'}).json()
  root = Path(get_settings().data_dir) / 'projects' / project['id']
  (root / 'index.html').write_text('<h1>Original</h1>')
  return project, root


def create_link(client, auth, project):
  base = '/api/project-copies/projects/' + project['id']
  review = client.get(base + '/preview', headers=auth)
  assert review.status_code == 200, review.text
  shared = client.post(base + '/shares', headers=auth, json={'paths': ['index.html'], 'digest': review.json()['digest']})
  assert shared.status_code == 200, shared.text
  return shared.json()


def test_review_excludes_credentials_runtime_data_and_symlinks(client, auth):
  project, root = fixture_project(client, auth)
  (root / '.env').write_text('SECRET=private')
  (root / 'data').mkdir()
  (root / 'data' / 'customer.json').write_text('{}')
  (root / '.git').mkdir()
  (root / '.git' / 'config').write_text('credential')
  (root / 'linked.txt').symlink_to(root / '.env')
  response = client.get('/api/project-copies/projects/' + project['id'] + '/preview', headers=auth)
  assert response.status_code == 200
  assert [row['path'] for row in response.json()['files']] == ['index.html']
  assert 'content' not in response.json()['files'][0]


def test_snapshot_is_review_bound_immutable_revocable_and_hash_only(client, auth, db):
  project, root = fixture_project(client, auth)
  base = '/api/project-copies/projects/' + project['id']
  review = client.get(base + '/preview', headers=auth).json()
  (root / 'index.html').write_text('changed')
  assert client.post(base + '/shares', headers=auth, json={'paths': ['index.html'], 'digest': review['digest']}).status_code == 409
  link = create_link(client, auth, project)
  token = link['copy_url'].split('#')[1]
  row = db.get(models.ProjectSourceCopy, link['id'])
  assert row.token_hash != token
  (root / 'index.html').write_text('later')
  package = client.post('/api/project-copies/package', json={'token': token})
  assert package.status_code == 200
  assert base64.b64decode(package.json()['files'][0]['content']) == b'changed'
  assert package.headers['cache-control'] == 'no-store'
  assert client.delete('/api/project-copies/shares/' + link['id'], headers=auth).status_code == 204
  assert client.post('/api/project-copies/package', json={'token': token}).status_code == 404


def test_expired_or_deleted_project_copy_is_unavailable(client, auth, db):
  project, _ = fixture_project(client, auth)
  link = create_link(client, auth, project)
  row = db.get(models.ProjectSourceCopy, link['id'])
  row.expires_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  assert client.post('/api/project-copies/package', json={'token': link['copy_url'].split('#')[1]}).status_code == 404


@pytest.mark.parametrize('path', ['../escape', '/absolute', 'a/../b', '.env', '.env.local', '.git/config', 'data/users.json', 'key.pem', 'a\\b', 'a//b'])
def test_untrusted_copy_cannot_write_reserved_or_escaping_paths(path):
  with pytest.raises(HTTPException):
    copies.validate_package({'format':'mobius-project-copy-v1', 'name':'Copy', 'project_type':'blank', 'files':[{'path':path, 'content':'YQ=='}]})


def test_import_creates_independent_source_without_chats_members_or_builds(client, auth, db, monkeypatch):
  package = {'format':'mobius-project-copy-v1', 'name':'Copy', 'project_type':'malicious:builder', 'files':[{'path':'index.html', 'content':base64.b64encode(b'<h1>copy</h1>').decode()}]}
  async def fetch(url):
    return package
  monkeypatch.setattr(copies, 'fetch_package', fetch)
  response = client.post('/api/project-copies/import', headers=auth, json={'url':'https://example.com/project-copy#test', 'digest':copies.digest(package)})
  assert response.status_code == 200, response.text
  result = response.json()
  project = db.get(models.Project, result['id'])
  assert project.project_type == 'blank'
  assert project.chat_id is None and project.artifacts_json is None and project.source_app_id is None
  assert db.query(models.Chat).filter_by(project_id=project.id).count() == 0
  assert db.query(models.ProjectMember).filter_by(project_id=project.id).count() == 0
  assert (Path(get_settings().data_dir) / project.root_path / 'index.html').read_text() == '<h1>copy</h1>'


def test_metadata_has_no_file_contents_and_deleted_project_links_stop(client, auth, db):
  project, _ = fixture_project(client, auth)
  link = create_link(client, auth, project)
  body = {'token': link['copy_url'].split('#')[1]}
  meta = client.post('/api/project-copies/metadata', json=body)
  assert meta.status_code == 200
  assert meta.json()['files'] == [{'path':'index.html', 'size':17}]
  db.get(models.Project, project['id']).deleted_at = now_naive_utc()
  db.commit()
  assert client.post('/api/project-copies/package', json=body).status_code == 404


def test_retention_scrubs_expired_payloads(client, auth, db):
  from app.project_retention import purge_expired_project_tombstones
  project, _ = fixture_project(client, auth)
  link = create_link(client, auth, project)
  row = db.get(models.ProjectSourceCopy, link['id'])
  row.expires_at = now_naive_utc() - timedelta(seconds=1)
  db.commit()
  purge_expired_project_tombstones(db)
  db.refresh(row)
  assert row.package_json is None


@pytest.mark.asyncio
async def test_remote_copy_uses_bounded_pinned_transport_and_token_not_url(monkeypatch):
  import httpx
  package = {'format':'mobius-project-copy-v1', 'name':'Copy', 'project_type':'blank', 'files':[{'path':'x.txt', 'content':'YQ=='}]}
  seen = {}
  async def transport(method, url, **kwargs):
    seen.update(method=method, url=url, **kwargs)
    return httpx.Response(200, json=package)
  monkeypatch.setattr(copies, 'federation_request', transport)
  token = 'a' * 43
  assert await copies.fetch_package('https://example.com/project-copy#' + token) == package
  assert seen == {'method':'POST', 'url':'https://example.com/api/project-copies/package', 'json':{'token':token}, 'max_response_bytes':copies.MAX_PACKAGE}


@pytest.mark.asyncio
@pytest.mark.parametrize('url', ['https://user:pass@example.com/project-copy#' + 'a'*43, 'file:///project-copy#' + 'a'*43, 'https://example.com/other#'+'a'*43, 'http://[invalid'])
async def test_malformed_copy_link_rejected_before_network(url, monkeypatch):
  async def forbidden(*args, **kwargs):
    raise AssertionError('network must not run')
  monkeypatch.setattr(copies, 'federation_request', forbidden)
  with pytest.raises(HTTPException):
    await copies.fetch_package(url)


def test_remote_import_digest_mismatch_creates_nothing(client, auth, db, monkeypatch):
  package = {'format':'mobius-project-copy-v1', 'name':'Copy', 'project_type':'blank', 'files':[{'path':'x.txt', 'content':'YQ=='}]}
  async def fetch(url): return package
  monkeypatch.setattr(copies, 'fetch_package', fetch)
  before = db.query(models.Project).count()
  response = client.post('/api/project-copies/import', headers=auth, json={'url':'https://example.com/project-copy#test', 'digest':'0'*64})
  assert response.status_code == 409
  assert db.query(models.Project).count() == before


def test_import_resolves_only_recipient_local_template(client, auth, db, monkeypatch):
  package = {'format':'mobius-project-copy-v1', 'name':'Copy', 'project_type':'local:document', 'files':[{'path':'main.tex', 'content':'YQ=='}]}
  async def fetch(url): return package
  monkeypatch.setattr(copies, 'fetch_package', fetch)
  def template(db, key):
    assert key == 'local:document'
    return {'id': key, 'name':'Local document', 'guidance':'Trusted local guidance', 'files':{}}, None
  monkeypatch.setattr(copies, '_template_by_id', template)
  result = client.post('/api/project-copies/import', headers=auth, json={'url':'https://example.com/project-copy#test', 'digest':copies.digest(package)})
  assert result.status_code == 200, result.text
  project = db.get(models.Project, result.json()['id'])
  assert project.project_type == 'local:document'
  assert project.template_snapshot_json['guidance'] == 'Trusted local guidance'
  assert project.artifacts_json is None


def test_package_cannot_supply_build_commands():
  package = {'format':'mobius-project-copy-v1', 'name':'Copy', 'project_type':'app', 'files':[{'path':'index.jsx', 'content':'YQ=='}], 'template': {'script':'arbitrary command'}}
  with pytest.raises(HTTPException):
    copies.validate_package(package)


def test_oversized_binary_source_rejected():
  package = {'format':'mobius-project-copy-v1', 'name':'Copy', 'project_type':'blank', 'files':[{'path':'photo.png', 'content':base64.b64encode(b'x' * (copies.MAX_SOURCE+1)).decode()}]}
  with pytest.raises(HTTPException) as result:
    copies.validate_package(package)
  assert result.value.status_code == 413


def test_retry_import_reuses_same_project_without_network_and_conflicts_on_new_digest(client, auth, db, monkeypatch):
  package = {'format':'mobius-project-copy-v1', 'name':'Copy', 'project_type':'blank', 'files':[{'path':'x.txt', 'content':'YQ=='}]}
  async def fetch(url): return package
  monkeypatch.setattr(copies, 'fetch_package', fetch)
  body = {'url':'https://example.com/project-copy#test', 'digest':copies.digest(package), 'recovery_request_id':'same-owner-operation'}
  first = client.post('/api/project-copies/import', headers=auth, json=body)
  assert first.status_code == 200
  async def offline(url): raise AssertionError('retry should not require network')
  monkeypatch.setattr(copies, 'fetch_package', offline)
  retry = client.post('/api/project-copies/import', headers=auth, json=body)
  assert retry.status_code == 200 and retry.json()['id'] == first.json()['id']
  different = client.post('/api/project-copies/import', headers=auth, json={**body, 'digest':'0'*64})
  assert different.status_code == 409


@pytest.mark.parametrize('path', ['a\nb', 'a\tb', 'C:/absolute', 'nested/.GIT/config', 'nested/.secret-key'])
def test_source_copy_rejects_control_characters_and_nested_secrets(path):
  assert not copies.eligible(path)


def test_committed_import_survives_response_failure_and_retry_keeps_files(client, db, monkeypatch):
  import asyncio
  package = {'format':'mobius-project-copy-v1', 'name':'Copy', 'project_type':'blank', 'files':[{'path':'x.txt', 'content':'YQ=='}]}
  async def fetch(url): return package
  monkeypatch.setattr(copies, 'fetch_package', fetch)
  body = copies.CopyImport(url='https://example.com/project-copy#test', digest=copies.digest(package), recovery_request_id='response-lost-after-commit')
  original_refresh = db.refresh
  def failed_refresh(*args, **kwargs): raise RuntimeError('response failed after commit')
  monkeypatch.setattr(db, 'refresh', failed_refresh)
  with pytest.raises(RuntimeError, match='response failed after commit'):
    asyncio.run(copies.import_copy(body, None, db))
  project = db.query(models.Project).filter_by(name='Copy').one()
  root = Path(get_settings().data_dir) / project.root_path
  assert (root / 'x.txt').read_bytes() == b'a'
  monkeypatch.setattr(db, 'refresh', original_refresh)
  async def no_fetch(url): raise AssertionError('committed retry must not fetch')
  monkeypatch.setattr(copies, 'fetch_package', no_fetch)
  retried = asyncio.run(copies.import_copy(body, None, db))
  assert retried['id'] == project.id
  assert (root / 'x.txt').read_bytes() == b'a'
  assert db.query(models.Project).filter_by(name='Copy').count() == 1


@pytest.mark.parametrize('failure', ['write', 'commit'])
def test_failed_import_removes_only_its_new_root(client, auth, db, monkeypatch, failure):
  import asyncio
  import uuid
  original, original_root = fixture_project(client, auth)
  package = {'format':'mobius-project-copy-v1', 'name':'Copy', 'project_type':'blank', 'files':[{'path':'x.txt', 'content':'YQ=='}]}
  async def fetch(url): return package
  monkeypatch.setattr(copies, 'fetch_package', fetch)
  body = copies.CopyImport(url='https://example.com/project-copy#test', digest=copies.digest(package), recovery_request_id='failed-' + failure)
  new_id = str(uuid.uuid5(uuid.NAMESPACE_URL, 'mobius:source-copy:' + body.recovery_request_id))
  if failure == 'write':
    original_write = Path.write_bytes
    def fail_write(path, data):
      if path.name == 'x.txt': raise OSError('test write failure')
      return original_write(path, data)
    monkeypatch.setattr(Path, 'write_bytes', fail_write)
  else:
    def fail_commit(): raise RuntimeError('test commit failure')
    monkeypatch.setattr(db, 'commit', fail_commit)
  with pytest.raises((OSError, RuntimeError), match='test .* failure'):
    asyncio.run(copies.import_copy(body, None, db))
  assert not (Path(get_settings().data_dir) / 'projects' / new_id).exists()
  assert db.get(models.Project, new_id) is None
  assert db.get(models.Project, original['id']) is not None
  assert (original_root / 'index.html').read_text() == '<h1>Original</h1>'


def test_deleted_import_retry_returns_conflict_without_network(client, db, monkeypatch):
  import asyncio
  package = {'format':'mobius-project-copy-v1', 'name':'Copy', 'project_type':'blank', 'files':[{'path':'x.txt', 'content':'YQ=='}]}
  async def fetch(url): return package
  monkeypatch.setattr(copies, 'fetch_package', fetch)
  body = copies.CopyImport(url='https://example.com/project-copy#test', digest=copies.digest(package), recovery_request_id='deleted-import')
  imported = asyncio.run(copies.import_copy(body, None, db))
  project = db.get(models.Project, imported['id'])
  project.deleted_at = now_naive_utc()
  db.commit()
  async def no_fetch(url): raise AssertionError('deleted retry must not fetch')
  monkeypatch.setattr(copies, 'fetch_package', no_fetch)
  with pytest.raises(HTTPException) as result:
    asyncio.run(copies.import_copy(body, None, db))
  assert result.value.status_code == 409
  assert project.deleted_at is not None


@pytest.mark.asyncio
async def test_copy_fetch_rejects_loopback_through_real_transport(monkeypatch):
  import httpx
  async def no_connect(*args, **kwargs): raise AssertionError('loopback must be rejected before connecting')
  monkeypatch.setattr(httpx.AsyncClient, 'send', no_connect)
  with pytest.raises(HTTPException) as result:
    await copies.fetch_package('http://127.0.0.1/project-copy#' + 'a' * 43)
  assert result.value.status_code == 400
