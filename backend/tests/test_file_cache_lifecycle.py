"""Cache advice follows existing teardown, never the owner's result stream."""
import asyncio
from types import SimpleNamespace

import pytest

from app import file_cache, codex_sdk_runner


@pytest.mark.asyncio
@pytest.mark.parametrize('outcome', ['success', 'error', 'cancel'])
async def test_codex_cache_advice_follows_teardown_and_preserves_outcome(monkeypatch, outcome):
  events = []
  async def acquire(_):
    return SimpleNamespace(release=lambda: events.append('ownership_released'))
  async def turn(**kwargs):
    try:
      if outcome == 'error':
        raise RuntimeError('original provider error')
      if outcome == 'cancel':
        raise asyncio.CancelledError()
      return {'result': 'original'}
    finally:
      events.append('provider_teardown')
  async def cleanup(provider):
    assert provider == 'codex'
    events.append('cache_advice')
  monkeypatch.setattr('app.codex_session_lock.acquire_codex_session_activity_async', acquire)
  monkeypatch.setattr(codex_sdk_runner, '_run_codex_sdk_turn', turn)
  monkeypatch.setattr(file_cache, 'reclaim_provider_cache', cleanup)
  call = codex_sdk_runner.run_codex_sdk_turn(
    user_message='test', session_id=None, base_env={}, cwd='/tmp',
    chat_id='cache-test', bc=None, pending_questions={}, db=None, data_dir='/tmp',
  )
  if outcome == 'success':
    assert await call == {'result': 'original'}
  else:
    with pytest.raises(RuntimeError if outcome == 'error' else asyncio.CancelledError):
      await call
  assert events == ['provider_teardown', 'ownership_released', 'cache_advice']


@pytest.mark.asyncio
async def test_claude_cache_advice_follows_disconnect(monkeypatch):
  from tests.test_claude_sdk_runner import _FakeClient, _install_fake_client, _run_turn
  clients = _install_fake_client(monkeypatch, _FakeClient)
  calls = []
  async def cleanup(provider):
    assert all(client.disconnected for client in clients)
    calls.append(provider)
  monkeypatch.setattr(file_cache, 'reclaim_provider_cache', cleanup)
  await _run_turn('claude-cache-test')
  assert calls == ['claude']
