"""Security and proxy-contract tests for owner-configured local services."""

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from app.config import get_settings
from app.routes import local_services as local_services_module


@pytest.fixture(autouse=True)
def clean_local_services_config():
  path = Path(os.environ["DATA_DIR"]) / "local-services.json"
  path.unlink(missing_ok=True)
  yield path
  path.unlink(missing_ok=True)


def write_config(path: Path, services: dict):
  path.write_text(json.dumps({"version": 1, "services": services}))


def test_stock_instance_exposes_no_local_service(client):
  root = client.get("/services", follow_redirects=False)
  response = client.get("/services/tandoor/", follow_redirects=False)

  assert root.status_code == 404
  assert response.status_code == 404
  assert response.json()["detail"] == "Local service not found."


def test_bare_configured_service_normalizes_to_trailing_slash(
  client, clean_local_services_config,
):
  write_config(clean_local_services_config, {
    "recipes": {"upstream": "http://127.0.0.1:8123"},
  })

  response = client.get("/services/recipes", follow_redirects=False)

  assert response.status_code == 307
  assert response.headers["location"] == "/services/recipes/"


@pytest.mark.parametrize("upstream", [
  "https://127.0.0.1:8123",
  "http://localhost:8123",
  "http://10.0.0.8:8123",
  "http://127.0.0.1:8123/admin",
  "http://user:pass@127.0.0.1:8123",
])
def test_non_loopback_or_ambiguous_targets_fail_closed(
  client, clean_local_services_config, monkeypatch, upstream,
):
  write_config(clean_local_services_config, {
    "recipes": {"upstream": upstream},
  })

  class MustNotConnect:
    def __init__(self, *args, **kwargs):
      raise AssertionError("invalid configuration attempted an outbound connection")

  monkeypatch.setattr(local_services_module.httpx, "AsyncClient", MustNotConnect)
  response = client.get("/services/recipes/")

  assert response.status_code == 503
  assert response.json()["detail"].startswith(
    "Local service 'recipes' is unavailable"
  )


def test_malformed_configuration_does_not_affect_platform_health(
  client, clean_local_services_config,
):
  clean_local_services_config.write_text("{ definitely not JSON")

  service = client.get("/services/recipes/")
  health = client.get("/api/health")

  assert service.status_code == 503
  assert health.status_code == 200


def test_proxy_preserves_path_query_headers_body_and_repeated_cookies(
  client, clean_local_services_config, monkeypatch,
):
  write_config(clean_local_services_config, {
    "recipes": {"upstream": "http://127.0.0.1:8123"},
  })
  seen = {}

  class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
      seen["client_options"] = kwargs

    def build_request(self, method, url, *, headers, content):
      request = httpx.Request(method, url, headers=headers, content=content)
      seen["request"] = request
      return request

    async def send(self, request, *, stream):
      assert stream is True
      return httpx.Response(
        201,
        headers=[
          ("content-type", "text/plain"),
          ("set-cookie", "sessionid=abc; Path=/services/recipes; HttpOnly"),
          ("set-cookie", "csrftoken=xyz; Path=/services/recipes"),
          ("connection", "close"),
          ("content-length", "999"),
        ],
        stream=httpx.ByteStream(b"proxied"),
        request=request,
      )

    async def aclose(self):
      seen["closed"] = True

  monkeypatch.setattr(
    local_services_module.httpx, "AsyncClient", FakeAsyncClient,
  )
  response = client.post(
    "/services/recipes/accounts/login/?next=%2Fservices%2Frecipes%2F",
    content=b"username=owner",
    headers={"host": "mobius.test", "content-type": "application/x-www-form-urlencoded"},
  )

  request = seen["request"]
  assert str(request.url) == (
    "http://127.0.0.1:8123/services/recipes/accounts/login/"
    "?next=%2Fservices%2Frecipes%2F"
  )
  assert request.content == b"username=owner"
  assert request.headers["host"] == "mobius.test"
  assert request.headers["x-forwarded-host"] == "mobius.test"
  assert request.headers["x-forwarded-proto"] == urlsplit(
    get_settings().frontend_origin
  ).scheme
  assert request.headers["x-script-name"] == "/services/recipes"
  assert response.status_code == 201
  assert response.content == b"proxied"
  assert response.headers.get_list("set-cookie") == [
    "sessionid=abc; Path=/services/recipes; HttpOnly",
    "csrftoken=xyz; Path=/services/recipes",
  ]
  assert "connection" not in response.headers
  assert "content-length" not in response.headers
  assert seen["closed"] is True


def test_unreachable_service_degrades_to_scoped_502(
  client, clean_local_services_config, monkeypatch,
):
  write_config(clean_local_services_config, {
    "recipes": {"upstream": "http://127.0.0.1:8123"},
  })

  class FailingAsyncClient:
    def __init__(self, *args, **kwargs):
      pass

    def build_request(self, method, url, *, headers, content):
      return httpx.Request(method, url, headers=headers, content=content)

    async def send(self, request, *, stream):
      raise httpx.ConnectError("offline", request=request)

    async def aclose(self):
      pass

  monkeypatch.setattr(
    local_services_module.httpx, "AsyncClient", FailingAsyncClient,
  )
  response = client.get("/services/recipes/")

  assert response.status_code == 502
  assert response.text == "The local service 'recipes' is not available right now."
  assert "127.0.0.1" not in response.text
