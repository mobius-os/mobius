"""A response that cannot hand off restart dispatch reports uncertainty, not a stuck claim."""
import asyncio

import pytest
from starlette.background import BackgroundTask

from app.routes.chats_stream import ClaimedRestartResponse


@pytest.mark.parametrize('failure', ['send', 'background', None])
def test_response_settles_the_claim_at_its_actual_dispatch_boundary(monkeypatch, failure):
  from app import platform_restart
  settled = []
  dispatched = []
  monkeypatch.setattr(platform_restart, 'settle_undispatched_execution', lambda action: settled.append(action))

  async def dispatch():
    dispatched.append(True)
    if failure == 'background':
      raise RuntimeError('dispatch failed')

  async def send(message):
    if failure == 'send':
      raise RuntimeError('response send failed')

  async def receive():
    return {'type': 'http.request'}

  response = ClaimedRestartResponse(action_id='exact-action', content={'answer_turn': 'none'},
                                    background=BackgroundTask(dispatch))
  if failure:
    with pytest.raises(RuntimeError):
      asyncio.run(response({'type': 'http'}, receive, send))
  else:
    asyncio.run(response({'type': 'http'}, receive, send))
  assert settled == ['exact-action']
  assert dispatched == ([] if failure == 'send' else [True])


def test_failed_settlement_does_not_hide_original_send_failure(monkeypatch):
  from app import platform_restart

  def fail_settle(_):
    raise RuntimeError('database unavailable')

  async def send(_):
    raise ValueError('original send failure')

  monkeypatch.setattr(platform_restart, 'settle_undispatched_execution', fail_settle)
  response = ClaimedRestartResponse(action_id='exact-action', content={})
  with pytest.raises(ValueError, match='original send failure'):
    asyncio.run(response({'type': 'http'}, None, send))
