"""Local sign-out preserves unrelated data and cannot lose a race to sign-in."""
import asyncio
import json

import pytest

from app import providers
from app.routes import auth as routes


@pytest.fixture
def provider_home(tmp_path, monkeypatch):
  monkeypatch.setattr(routes, 'get_settings', lambda: type('Settings', (), {'data_dir': str(tmp_path)})())
  monkeypatch.setattr(routes, '_provider_login_locks', {'claude': asyncio.Lock(), 'codex': asyncio.Lock()})
  monkeypatch.setattr(providers, '_claude_refresh_lock', asyncio.Lock())
  monkeypatch.setattr(routes, '_active_pkce', {'state': 'test', 'ts': 0})
  monkeypatch.setattr(routes, '_codex_login_procs', {})
  monkeypatch.setattr(routes, '_codex_login_status', {})
  for provider in ('claude', 'codex'):
    home = tmp_path / 'cli-auth' / provider
    home.mkdir(parents=True)
    (home / 'config.toml').write_text('keep configuration')
  (tmp_path / 'cli-auth/claude/.credentials.json').write_text(json.dumps({
    'claudeAiOauth': {'accessToken': 'synthetic-token'},
    'mcpOAuth': {'server': 'synthetic-mcp'},
    'organizationUuid': 'keep-organization',
  }))
  (tmp_path / 'cli-auth/codex/auth.json').write_text('{"tokens": "synthetic"}')
  (tmp_path / 'chat.txt').write_text('keep chat')
  return tmp_path


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_disconnect_removes_only_selected_signin_and_is_idempotent(client, auth, provider_home, provider):
  for _ in range(2):
    response = client.post(f'/api/auth/provider/{provider}/disconnect', headers=auth)
    assert response.status_code == 200, response.text
  claude = json.loads((provider_home / 'cli-auth/claude/.credentials.json').read_text())
  assert ('claudeAiOauth' in claude) == (provider != 'claude')
  assert claude['mcpOAuth'] == {'server': 'synthetic-mcp'}
  assert claude['organizationUuid'] == 'keep-organization'
  assert (provider_home / 'cli-auth/codex/auth.json').exists() == (provider != 'codex')
  for pid in ('claude', 'codex'):
    assert (provider_home / f'cli-auth/{pid}/config.toml').read_text() == 'keep configuration'
  assert (provider_home / 'chat.txt').read_text() == 'keep chat'
  assert providers.get_provider(provider).check_auth(str(provider_home)) is not None


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_disconnect_requires_owner_and_same_origin(client, auth, provider_home, provider):
  url = f'/api/auth/provider/{provider}/disconnect'
  assert client.post(url).status_code in (401, 403)
  assert client.post(url, headers={**auth, 'Sec-Fetch-Site': 'cross-site'}).status_code == 403
  assert (provider_home / 'cli-auth/codex/auth.json').exists()
  assert 'claudeAiOauth' in (provider_home / 'cli-auth/claude/.credentials.json').read_text()


@pytest.mark.parametrize('provider', ['mobius', 'unknown'])
def test_disconnect_does_not_take_over_app_owned_or_unknown_connections(client, auth, provider_home, provider):
  assert client.post(f'/api/auth/provider/{provider}/disconnect', headers=auth).status_code == 400
  assert (provider_home / 'cli-auth/codex/auth.json').exists()


def test_disconnect_claude_invalidates_pending_code(client, auth, provider_home):
  assert client.post('/api/auth/provider/claude/disconnect', headers=auth).status_code == 200
  assert routes._active_pkce is None
  assert client.post('/api/auth/provider/code', headers=auth, json={'code': 'test'}).status_code == 400


@pytest.mark.parametrize('contents', ['not json', '[]'])
def test_disconnect_does_not_destroy_unreadable_sibling_credentials(client, auth, provider_home, contents):
  path = provider_home / 'cli-auth/claude/.credentials.json'
  path.write_text(contents)
  assert client.post('/api/auth/provider/claude/disconnect', headers=auth).status_code == 500
  assert path.read_text() == contents


def test_codex_disconnect_stops_login_before_removing_signin(client, auth, provider_home):
  path = provider_home / 'cli-auth/codex/auth.json'
  class Login:
    returncode = None
    def kill(self):
      assert path.exists()
      self.returncode = -9
    async def wait(self):
      assert path.exists()
  routes._codex_login_procs['active'] = Login()
  routes._codex_login_status['result'] = 'complete'
  assert client.post('/api/auth/provider/codex/disconnect', headers=auth).status_code == 200
  assert not path.exists()
  assert not routes._codex_login_procs
  assert not routes._codex_login_status


def test_codex_disconnect_failure_does_not_claim_success_or_delete_credentials(client, auth, provider_home):
  class Login:
    returncode = None
    def kill(self):
      pass
    async def wait(self):
      raise asyncio.TimeoutError
  routes._codex_login_procs['active'] = Login()
  assert client.post('/api/auth/provider/codex/disconnect', headers=auth).status_code == 409
  assert (provider_home / 'cli-auth/codex/auth.json').exists()
  assert 'active' in routes._codex_login_procs


def test_disconnect_waits_for_claude_refresh_so_it_cannot_restore_tokens(provider_home, monkeypatch):
  path = provider_home / 'cli-auth/claude/.credentials.json'
  async def scenario():
    started, finish = asyncio.Event(), asyncio.Event()
    async def refresh(oauth):
      started.set()
      await finish.wait()
      return {'accessToken': 'refreshed', 'expiresAt': 9999999999999}
    monkeypatch.setattr(providers, '_refresh_claude_access_token', refresh)
    refreshing = asyncio.create_task(providers.claude_access_token(str(provider_home)))
    await started.wait()
    disconnecting = asyncio.create_task(routes.provider_disconnect('claude'))
    await asyncio.sleep(0)
    assert not disconnecting.done()
    finish.set()
    await asyncio.gather(refreshing, disconnecting)
    assert 'claudeAiOauth' not in json.loads(path.read_text())
  asyncio.run(scenario())


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_disconnect_waits_for_inflight_login_before_removal(provider_home, monkeypatch, provider):
  async def scenario():
    started, finish = asyncio.Event(), asyncio.Event()
    async def sign_in(*args):
      started.set()
      await finish.wait()
      return {'ok': True}
    if provider == 'claude':
      monkeypatch.setattr(routes, '_exchange_claude_code', sign_in)
      login = routes.provider_code.__wrapped__(None, routes.schemas.ProviderCodeRequest(code='test'))
    else:
      monkeypatch.setattr(routes, '_start_codex_login', sign_in)
      login = routes.codex_login_start()
    logging_in = asyncio.create_task(login)
    await started.wait()
    disconnecting = asyncio.create_task(routes.provider_disconnect(provider))
    await asyncio.sleep(0)
    assert not disconnecting.done()
    finish.set()
    await asyncio.gather(logging_in, disconnecting)
    assert providers.get_provider(provider).check_auth(str(provider_home)) is not None
  asyncio.run(scenario())


@pytest.mark.parametrize('scope', ['app', 'chat_embed', 'project_collaborator'])
def test_scoped_tokens_cannot_disconnect_owner_providers(client, owner_token, provider_home, scope):
  from app.auth import create_access_token
  token = create_access_token({'sub': 'test', 'scope': scope, 'app_id': 1})
  response = client.post('/api/auth/provider/codex/disconnect', headers={'Authorization': f'Bearer {token}'})
  assert response.status_code in (401, 403)
  assert (provider_home / 'cli-auth/codex/auth.json').exists()
