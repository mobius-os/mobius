"""One service snapshot drives discovery, execution, and explicit retirement."""
import copy
import json
from contextlib import contextmanager
from pathlib import Path

import pytest

from app import providers, runtime_identity
from app.mobius_catalogue import parse_catalogue


def contract():
  return {
    'contract_version': 1, 'default_model': 'new-vision',
    'replacements': {'retired-vision': 'new-vision'},
    'data': [{'id': 'new-vision', 'name': 'New vision', 'owned_by': 'mobius',
              'context_window': 200000, 'effort_levels': ['low', 'high'],
              'default_effort': 'low', 'input_modalities': ['text', 'image'],
              'supports_parallel_tool_calls': False,
              'supports_search_tool': True,
              'supports_reasoning_summaries': True}],
  }


@pytest.fixture
def discovery(monkeypatch):
  provider = providers.MobiusProvider()
  monkeypatch.setitem(providers.PROVIDERS, 'mobius', provider)
  monkeypatch.setitem(providers.DEFAULT_MODELS, 'mobius', None)
  monkeypatch.setitem(providers.DEFAULT_BACKGROUND_MODELS, 'mobius', None)
  state = {'catalogue': contract(), 'calls': 0,
           'identity': {'linked': True, 'issuer': 'https://account.test',
                        'subject': 'one', 'instance_id': 'test', 'key_generation': 1}}
  monkeypatch.setattr(provider, '_identity', lambda: copy.deepcopy(state['identity']))

  @contextmanager
  def client(**kwargs):
    class Response:
      def get(self, route):
        assert route == '/v1/models'
        state['calls'] += 1
        return self
      def raise_for_status(self):
        if state.get('error'):
          raise OSError('offline')
      def json(self):
        if hook := state.get('hook'):
          hook()
        return copy.deepcopy(state['catalogue'])
    yield Response()
  monkeypatch.setattr(runtime_identity, 'broker_client', client)
  providers.invalidate_model_cache()
  return provider, state


def test_one_snapshot_updates_runtime_picker_and_explicit_successor(discovery, tmp_path):
  provider, state = discovery
  provider._refresh_catalogue(str(tmp_path))
  assert providers.DEFAULT_MODELS['mobius'] == 'new-vision'
  assert provider.switch_efforts == {'low', 'high'}
  assert provider.resolve_model('retired-vision') == 'new-vision'
  assert provider.resolve_model('unknown') is None
  saved = {'model': 'retired-vision', 'effort': 'low'}
  assert providers.effective_agent_settings(str(tmp_path), saved, 'mobius')['model'] == 'new-vision'
  assert saved['model'] == 'retired-vision'
  assert providers.provider_of_model('retired-vision') == 'mobius'
  assert providers.effective_agent_settings(str(tmp_path), saved, 'claude')['model'] is None
  provider.build_env({}, str(tmp_path))
  runtime = json.loads((tmp_path / 'cli-auth/mobius/catalog.json').read_text())['models'][0]
  assert runtime['slug'] == 'new-vision'
  assert runtime['context_window'] == 200000
  assert runtime['input_modalities'] == ['text', 'image']
  assert runtime['default_reasoning_level'] == 'low'
  assert runtime['supports_search_tool'] is True
  assert providers._fallback_models('mobius')[0]['context_window'] == 180000


@pytest.mark.asyncio
async def test_refresh_adds_removes_models_and_offline_keeps_last_good(discovery, tmp_path):
  provider, state = discovery
  path = str(tmp_path)
  _, first = await providers._fetch_model_entries(path, 'mobius')
  state['catalogue']['data'][0]['name'] = 'Renamed remotely'
  _, cached = await providers._fetch_model_entries(path, 'mobius')
  assert cached == first and state['calls'] == 1
  _, fresh = await providers._fetch_model_entries(path, 'mobius', force_refresh=True)
  assert fresh[0]['label'] == 'Renamed remotely'
  state['catalogue']['data'][0]['id'] = 'replacement-vision'
  state['catalogue']['default_model'] = 'replacement-vision'
  state['catalogue']['replacements'] = {'new-vision': 'replacement-vision'}
  _, fresh = await providers._fetch_model_entries(path, 'mobius', force_refresh=True)
  assert [row['id'] for row in fresh] == ['replacement-vision']
  assert provider.resolve_model('new-vision') == 'replacement-vision'
  assert providers.DEFAULT_MODELS['mobius'] == 'replacement-vision'
  state['error'] = True
  _, offline = await providers._fetch_model_entries(path, 'mobius', force_refresh=True)
  assert offline == fresh
  assert provider.declaration['models'][0]['label'] == fresh[0]['label']


@pytest.mark.asyncio
async def test_restart_cache_is_bound_to_broker_account(discovery, monkeypatch, tmp_path):
  provider, state = discovery
  provider._refresh_catalogue(str(tmp_path))
  state['error'] = True
  fresh = providers.MobiusProvider()
  monkeypatch.setattr(fresh, '_identity', lambda: state['identity'])
  monkeypatch.setitem(providers.PROVIDERS, 'mobius', fresh)
  _, cached = await providers._fetch_model_entries(str(tmp_path), 'mobius', force_refresh=True)
  assert cached[0]['id'] == 'new-vision'
  state['identity']['subject'] = 'different-account'
  providers.mobius_account_changed()
  _, other = await providers._fetch_model_entries(str(tmp_path), 'mobius', force_refresh=True)
  assert other == []
  assert fresh.declaration['models'] == []
  assert providers.DEFAULT_MODELS['mobius'] is None


def test_account_change_discards_inflight_catalogue(discovery, tmp_path):
  provider, state = discovery
  state['hook'] = provider.forget_account_reads
  with pytest.raises(ValueError, match='account changed'):
    provider._refresh_catalogue(str(tmp_path))
  assert provider.declaration['models'] == []
  assert not (tmp_path / 'cache/mobius-models.json').exists()


@pytest.mark.parametrize('mutation', [
  lambda p: p.update(contract_version=2),
  lambda p: p.update(data=[]),
  lambda p: p.update(default_model='not-present'),
  lambda p: p['data'].append(copy.deepcopy(p['data'][0])),
  lambda p: p['data'][0].update(context_window=True),
  lambda p: p['data'][0].update(input_modalities=['video']),
  lambda p: p['data'][0].update(default_effort='unsupported'),
  lambda p: p['data'][0].update(effort_levels=[], default_effort={'bad': 'shape'}),
  lambda p: p.update(replacements={'retired': 'missing'}),
  lambda p: p.update(replacements={'new-vision': 'new-vision'}),
])
def test_malformed_refresh_cannot_replace_working_snapshot(discovery, tmp_path, mutation):
  provider, state = discovery
  provider._refresh_catalogue(str(tmp_path))
  before = copy.deepcopy(provider.declaration)
  mutation(state['catalogue'])
  with pytest.raises(ValueError):
    provider._refresh_catalogue(str(tmp_path))
  assert provider.declaration == before


def test_foreign_model_cannot_be_a_service_alias(discovery, tmp_path):
  provider, state = discovery
  state['catalogue']['data'][0]['id'] = 'gpt-5.4'
  state['catalogue']['default_model'] = 'gpt-5.4'
  state['catalogue']['replacements'] = {}
  with pytest.raises(ValueError, match='collides'):
    provider._refresh_catalogue(str(tmp_path))
  assert provider.declaration['models'] == []


def test_empty_effort_scale_is_valid_and_writes_runtime_catalogue(discovery, tmp_path):
  provider, state = discovery
  state['catalogue']['data'][0].update(effort_levels=[], default_effort=None)
  provider._refresh_catalogue(str(tmp_path))
  provider.build_env({}, str(tmp_path))
  runtime = json.loads((tmp_path / 'cli-auth/mobius/catalog.json').read_text())
  assert runtime['models'][0]['supported_reasoning_levels'] == []

@pytest.mark.asyncio
@pytest.mark.parametrize("efforts,expected_effort", [(["low", "high"], "low"), ([], None)])
async def test_running_agent_keeps_frozen_catalogue_and_cleans_it_up(discovery, tmp_path, monkeypatch, efforts, expected_effort):
  from app import codex_sdk_runner
  provider, state = discovery
  state["catalogue"]["data"][0].update(effort_levels=efforts, default_effort=efforts[0] if efforts else None)
  captured = {}
  async def run(**kwargs):
    options = kwargs['provider_config_overrides']
    path_option = next(o for o in options if o.startswith('model_catalog_json='))
    path = Path(json.loads(path_option.split('=', 1)[1]))
    captured['path'] = path
    before = path.read_bytes()
    assert kwargs['agent_settings']['model'] == 'new-vision'
    assert kwargs['base_env']['MOBIUS_AGENT_MODEL'] == 'new-vision'
    assert kwargs['agent_settings']['effort'] == expected_effort
    state['catalogue']['data'][0].update(name='Next release', effort_levels=['high'], default_effort='high')
    provider._refresh_catalogue(str(tmp_path))
    assert path.read_bytes() == before
    assert kwargs['agent_settings']['effort'] == expected_effort
    assert json.loads(before)['models'][0]['display_name'] == 'New vision'
    return {'session_id': None, 'error': None}
  monkeypatch.setattr(codex_sdk_runner, '_run_codex_sdk_turn', run)
  result = await codex_sdk_runner.run_codex_sdk_turn(
    user_message='synthetic', session_id=None, base_env={}, cwd=str(tmp_path),
    chat_id='synthetic', bc=None, pending_questions={}, db=None,
    provider_id='mobius', data_dir=str(tmp_path),
    agent_settings={'model': 'retired-vision', 'effort': 'low'},
  )
  assert result['error'] is None
  assert not captured['path'].exists()


@pytest.mark.asyncio
async def test_unknown_model_fails_before_prompt_dispatch(discovery, tmp_path, monkeypatch):
  from app import codex_sdk_runner
  async def forbidden(**kwargs):
    pytest.fail('must not dispatch an unavailable model')
  monkeypatch.setattr(codex_sdk_runner, '_run_codex_sdk_turn', forbidden)
  result = await codex_sdk_runner.run_codex_sdk_turn(
    user_message='synthetic', session_id=None, base_env={}, cwd=str(tmp_path),
    chat_id='synthetic', bc=None, pending_questions={}, db=None,
    provider_id='mobius', data_dir=str(tmp_path), agent_settings={'model': 'unknown'},
  )
  assert result['prompt_sent'] is False
  assert 'unavailable' in result['error']


def test_disk_cache_keeps_only_public_metadata_and_private_scope_digest(discovery, tmp_path):
  import stat
  provider, state = discovery
  state['catalogue']['unexpected'] = 'must not persist'
  state['catalogue']['data'][0]['private_future_field'] = 'must not persist'
  provider._refresh_catalogue(str(tmp_path))
  path = tmp_path / 'cache/mobius-models.json'
  saved = json.loads(path.read_text())
  assert stat.S_IMODE(path.stat().st_mode) == 0o600
  assert isinstance(saved['scope'], str) and len(saved['scope']) == 64
  assert 'must not persist' not in path.read_text()
  assert 'https://account.test' not in path.read_text()


@pytest.mark.asyncio
async def test_changed_account_without_local_event_drops_old_registry(discovery, tmp_path):
  provider, state = discovery
  await providers._fetch_model_entries(str(tmp_path), 'mobius')
  state['identity']['subject'] = 'second'
  state['error'] = True
  _, entries = await providers._fetch_model_entries(str(tmp_path), 'mobius', force_refresh=True)
  assert entries == []
  assert provider.declaration['models'] == []


@pytest.mark.asyncio
async def test_cold_failure_retries_soon_not_after_full_registry_ttl(discovery, tmp_path):
  import time
  provider, state = discovery
  state['error'] = True
  await providers._fetch_model_entries(str(tmp_path), 'mobius')
  stamp, rows = providers._model_registry_cache['mobius']
  remaining = stamp + providers._MODEL_CACHE_TTL_SECONDS - time.monotonic()
  assert rows == []
  assert 0 < remaining <= providers._MODEL_REFRESH_RETRY_SECONDS


def test_unmapped_selection_never_falls_back_to_service_default(discovery, tmp_path):
  provider, state = discovery
  provider._refresh_catalogue(str(tmp_path))
  assert providers.snapshot_chat_agent_settings(
    str(tmp_path), 'mobius', model='unknown', fallback_model='new-vision',
  ) is None


def test_cached_ids_get_same_collision_validation_as_live(discovery, tmp_path, monkeypatch):
  provider, state = discovery
  provider._refresh_catalogue(str(tmp_path))
  monkeypatch.setitem(providers.KNOWN_MODELS, 'codex', ['new-vision'])
  state['error'] = True
  fresh = providers.MobiusProvider()
  monkeypatch.setattr(fresh, '_identity', lambda: state['identity'])
  with pytest.raises(OSError):
    fresh._refresh_catalogue(str(tmp_path))
  assert fresh.declaration['models'] == []


@pytest.mark.asyncio
async def test_helper_host_owns_catalogue_after_originating_turn_ends(tmp_path):
  from types import SimpleNamespace
  from app import helper_hosts
  source = tmp_path / 'turn-catalogue.json'
  source.write_text('{"models": []}')
  option = f'model_catalog_json={json.dumps(str(source))}'
  config = SimpleNamespace(config_overrides=[option], launch_args_override=['codex', '-c', option])
  key = helper_hosts.HostKey('parent', 'mobius', str(tmp_path), 'snapshot-v1')
  host = helper_hosts.CodexHelperHost(key, sdk={}, config=config)
  owned = Path(json.loads(host._config.config_overrides[0].split('=', 1)[1]))
  source.unlink()
  assert owned.read_text() == '{"models": []}'
  assert host._config.launch_args_override[-1] == host._config.config_overrides[0]
  assert config.config_overrides == [option]
  await host.close()
  assert not owned.exists()


def test_stale_unlinked_read_cannot_clear_new_account_snapshot(discovery, monkeypatch, tmp_path):
  provider, state = discovery
  declaration = parse_catalogue(contract())
  def old_identity():
    provider.forget_account_reads()
    provider._publish_catalogue(declaration)
    return {'linked': False}
  monkeypatch.setattr(provider, '_identity', old_identity)
  with pytest.raises(ValueError, match='not linked'):
    provider._refresh_catalogue(str(tmp_path))
  assert provider.declaration == declaration


def test_cold_runtime_config_is_valid_but_has_no_invented_default(tmp_path):
  import tomllib
  provider = providers.MobiusProvider()
  provider.build_env({}, str(tmp_path))
  config = tomllib.loads((tmp_path / 'cli-auth/mobius/config.toml').read_text())
  assert 'model' not in config
  assert provider.declaration['models'] == []


def test_background_retirement_does_not_turn_unknown_into_default(discovery, tmp_path):
  provider, state = discovery
  provider._refresh_catalogue(str(tmp_path))
  row = providers._clean_background_choice({'provider': 'mobius', 'model': 'unknown'})
  assert row['model'] == 'unknown'
  assert providers.snapshot_chat_agent_settings(
    str(tmp_path), 'mobius', model=row['model'], fallback_model='new-vision',
  ) is None


def test_cold_background_default_hydrates_without_opening_picker(discovery, tmp_path, monkeypatch):
  from app import background_agents
  provider, state = discovery
  monkeypatch.setattr(background_agents, 'resolve_background_provider', lambda *a, **kw: {
    'provider': 'mobius', 'model': None, 'effort': 'low',
  })
  choice = background_agents.resolve_background_chat_choice(str(tmp_path), None)
  assert choice['agent_settings']['model'] == 'new-vision'
  assert state['calls'] == 1
  background_agents.resolve_background_chat_choice(str(tmp_path), None)
  assert state['calls'] == 1


def test_cold_background_outage_is_explicit_not_a_wrong_default(discovery, tmp_path, monkeypatch):
  from app import background_agents
  provider, state = discovery
  state['error'] = True
  monkeypatch.setattr(background_agents, 'resolve_background_provider', lambda *a, **kw: {
    'provider': 'mobius', 'model': None, 'effort': 'low',
  })
  with pytest.raises(RuntimeError, match='catalogue is unavailable'):
    background_agents.resolve_background_chat_choice(str(tmp_path), None)


def test_registry_route_exposes_same_default_and_replacements(discovery, client, auth):
  response = client.get('/api/models?refresh=true', headers=auth)
  assert response.status_code == 200
  body = response.json()
  assert body['defaults']['mobius'] == 'new-vision'
  assert body['replacements']['mobius'] == {'retired-vision': 'new-vision'}
  assert [row['id'] for row in body['providers']['mobius']] == ['new-vision']


def test_registry_refresh_cannot_pair_old_rows_with_new_default(discovery, client, auth, monkeypatch):
  provider, state = discovery
  old = providers._live_model_entries('mobius', parse_catalogue(contract())['models'])
  async def concurrent_refresh(*args, **kwargs):
    updated = contract()
    updated['data'][0]['id'] = 'next-model'
    updated['default_model'] = 'next-model'
    updated['replacements'] = {'new-vision': 'next-model'}
    provider._publish_catalogue(parse_catalogue(updated))
    # Another provider finished discovery after the Mobius cached read.
    return {'mobius': old}
  monkeypatch.setattr(providers, 'list_models', concurrent_refresh)
  body = client.get('/api/models', headers=auth).json()
  assert [row['id'] for row in body['providers']['mobius']] == ['next-model']
  assert body['defaults']['mobius'] == 'next-model'
  assert body['replacements']['mobius'] == {'new-vision': 'next-model'}
