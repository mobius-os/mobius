"""The device-asset relay is bounded, owner-only, and never retains bytes."""

from unittest.mock import patch

from test_app_fixtures import create_local_app

from app.routes.app_runtime import _is_https_device_asset_url, _parse_content_range


def _create_device_asset_app(client, auth):
  return create_local_app(
    client,
    auth,
    name="Device Assets",
    capabilities={
      "device.asset-cache": {
        "version": 1,
        "reason": "Keep a public model in this browser.",
        "limits": {
          "max_bytes": 256 * 1024 * 1024,
          "max_asset_bytes": 256 * 1024 * 1024,
          "max_chunk_bytes": 8 * 1024 * 1024,
        },
      },
    },
  )


class _FakeCloser:
  def __init__(self):
    self.closed = False

  async def aclose(self):
    self.closed = True


class _FakeUpstream(_FakeCloser):
  def __init__(self, body: bytes):
    super().__init__()
    self.body = body

  async def aiter_raw(self):
    yield self.body[:2]
    yield self.body[2:]


def test_content_range_parser_requires_an_exact_byte_form():
  assert _parse_content_range("bytes 8-15/100") == (8, 15, 100)
  assert _parse_content_range("bytes 0-0/*") == (0, 0, None)
  assert _parse_content_range("items 8-15/100") is None
  assert _parse_content_range("bytes */100") is None
  assert _parse_content_range("bytes 15-8/100") is None
  assert _parse_content_range("bytes 8-15/15") is None


def test_device_asset_urls_cannot_downgrade_or_embed_credentials():
  assert _is_https_device_asset_url("https://assets.example/model.bin") is True
  assert _is_https_device_asset_url("http://assets.example/model.bin") is False
  assert _is_https_device_asset_url("https://owner:secret@assets.example/model.bin") is False


def test_device_asset_relay_requires_a_reviewed_capability(client, auth):
  app = create_local_app(client, auth, name="No Device Storage")

  response = client.get(
    f"/api/apps/{app['id']}/device-assets/relay",
    headers=auth,
    params={"url": "https://assets.example/model.bin", "offset": 0, "length": 5},
  )

  assert response.status_code == 403


def test_device_asset_relay_is_shell_owned_not_app_token_callable(client, auth):
  app = _create_device_asset_app(client, auth)
  token_response = client.post(
    "/api/auth/app-token",
    headers=auth,
    json={"app_id": app["id"]},
  )
  assert token_response.status_code == 200

  response = client.get(
    f"/api/apps/{app['id']}/device-assets/relay",
    headers={
      "Authorization": f"Bearer {token_response.json()['token']}",
      "Origin": "null",
      "Sec-Fetch-Site": "cross-site",
    },
    params={"url": "https://assets.example/model.bin", "offset": 0, "length": 5},
  )

  assert response.status_code == 403


def test_device_asset_relay_rejects_non_https_and_oversize_ranges(client, auth):
  app = _create_device_asset_app(client, auth)
  endpoint = f"/api/apps/{app['id']}/device-assets/relay"

  insecure = client.get(
    endpoint,
    headers=auth,
    params={"url": "http://assets.example/model.bin", "offset": 0, "length": 5},
  )
  assert insecure.status_code == 400

  oversize = client.get(
    endpoint,
    headers=auth,
    params={
      "url": "https://assets.example/model.bin",
      "offset": 0,
      "length": 8 * 1024 * 1024 + 1,
    },
  )
  assert oversize.status_code == 413


def test_device_asset_relay_streams_one_exact_range_without_caching(client, auth):
  app = _create_device_asset_app(client, auth)
  upstream = _FakeUpstream(b"hello")
  http_client = _FakeCloser()

  async def fake_open(url, offset, length):
    assert url == "https://assets.example/model.bin"
    assert (offset, length) == (5, 5)
    return http_client, upstream, 100

  with patch(
    "app.routes.app_runtime._open_device_asset_range",
    side_effect=fake_open,
  ):
    response = client.get(
      f"/api/apps/{app['id']}/device-assets/relay",
      headers=auth,
      params={
        "url": "https://assets.example/model.bin",
        "offset": 5,
        "length": 5,
      },
    )

  assert response.status_code == 200, response.text
  assert response.content == b"hello"
  assert response.headers["cache-control"] == "no-store"
  assert response.headers["x-mobius-asset-total"] == "100"
  assert upstream.closed is True
  assert http_client.closed is True


def test_device_asset_relay_rejects_an_upstream_beyond_reviewed_size(client, auth):
  app = _create_device_asset_app(client, auth)
  upstream = _FakeUpstream(b"hello")
  http_client = _FakeCloser()

  async def fake_open(*_args):
    return http_client, upstream, 256 * 1024 * 1024 + 1

  with patch(
    "app.routes.app_runtime._open_device_asset_range",
    side_effect=fake_open,
  ):
    response = client.get(
      f"/api/apps/{app['id']}/device-assets/relay",
      headers=auth,
      params={
        "url": "https://assets.example/model.bin",
        "offset": 0,
        "length": 5,
      },
    )

  assert response.status_code == 413
  assert upstream.closed is True
  assert http_client.closed is True
