import asyncio

import httpx

from app.pinned_http_pool import PinnedFetchClientPool


def test_reuse_is_host_isolated_and_bounded():
  async def run():
    pool = PinnedFetchClientPool(max_clients=1)
    async with pool.lease('one.example', 'one.example') as first:
      async with pool.lease('one.example', 'one.example') as same:
        assert first is same
      async with pool.lease('two.example', 'two.example') as second:
        assert first is not second
        assert not first.is_closed
      assert second.is_closed
    async with pool.lease('three.example', 'three.example'):
      assert first.is_closed
    assert pool.snapshot()['clients'] == 1
    assert pool.snapshot()['active_requests'] == 0
    await pool.close()
    assert pool.snapshot()['clients'] == 0
  asyncio.run(run())


def test_cookies_and_request_credentials_never_carry_to_next_request():
  async def run():
    pool = PinnedFetchClientPool()
    async with pool.lease('api.example', 'api.example') as client:
      request = client.build_request('GET', 'https://8.8.8.8/first?key=secret')
      request.headers['Authorization'] = 'Bearer per-request'
      response = httpx.Response(200, headers={'set-cookie': 'identity=secret; Path=/'}, request=request)
      client.cookies.extract_cookies(response)
      next_request = client.build_request('GET', 'https://8.8.8.8/second')
      assert 'cookie' not in next_request.headers
      assert 'authorization' not in next_request.headers
      assert not list(client.cookies.jar)
      assert 'secret' not in str(next_request.url)
    await pool.close()
  asyncio.run(run())


def test_failure_and_cancellation_release_capacity():
  async def run():
    pool = PinnedFetchClientPool(max_active=1)
    for error in (ValueError, asyncio.CancelledError):
      try:
        async with pool.lease('api.example', 'api.example'):
          raise error()
      except error:
        pass
      async with pool.lease('api.example', 'api.example'):
        assert pool.snapshot()['active_requests'] == 1
    await pool.close()
  asyncio.run(run())
