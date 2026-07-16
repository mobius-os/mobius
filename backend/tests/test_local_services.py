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
  declared = {
    slug: {**entry, "access": "upstream_auth"}
    for slug, entry in services.items()
  }
  path.write_text(json.dumps({"version": 1, "services": declared}))


def test_stock_instance_exposes_no_local_service(client):
  root = client.get("/services", follow_redirects=False)
  response = client.get("/services/tandoor/", follow_redirects=False)

  assert root.status_code == 404
  assert response.status_code == 404
  assert response.json()["detail"] == "Local service not found."


def test_surface_is_inert_without_explicit_origin(
  client, auth, clean_local_services_config, monkeypatch,
):
  monkeypatch.delenv("MOBIUS_SERVICE_TANDOOR_ORIGIN", raising=False)
  write_config(clean_local_services_config, {
    "tandoor": {"upstream": "http://127.0.0.1:8123"},
  })
  disabled = client.get("/api/local-services/tandoor/surface", headers=auth)
  assert disabled.status_code == 409

  write_config(clean_local_services_config, {
    "tandoor": {
      "upstream": "http://127.0.0.1:8123", "public_surface": True,
    },
  })
  missing = client.get("/api/local-services/tandoor/surface", headers=auth)
  assert missing.status_code == 503
  assert "invalid" in missing.json()["detail"]


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


def test_service_must_explicitly_delegate_access_to_upstream_auth(
  client, clean_local_services_config,
):
  clean_local_services_config.write_text(json.dumps({
    "version": 1,
    "services": {
      "recipes": {"upstream": "http://127.0.0.1:8123"},
    },
  }))

  response = client.get("/services/recipes/")

  assert response.status_code == 503
  assert "configuration is invalid" in response.json()["detail"]


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
          ("location", "http://127.0.0.1:8123/services/recipes/welcome/"),
          ("set-cookie", "sessionid=abc; Path=/; Domain=127.0.0.1; HttpOnly"),
          ("set-cookie", "csrftoken=xyz"),
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
    headers={
      "host": "attacker.invalid",
      "authorization": "Bearer must-not-reach-upstream",
      "x-forwarded-host": "attacker.invalid",
      "content-type": "application/x-www-form-urlencoded",
    },
  )

  request = seen["request"]
  assert str(request.url) == (
    "http://127.0.0.1:8123/services/recipes/accounts/login/"
    "?next=%2Fservices%2Frecipes%2F"
  )
  assert request.content == b"username=owner"
  public = urlsplit(get_settings().frontend_origin)
  assert request.headers["host"] == public.netloc
  assert request.headers["x-forwarded-host"] == public.netloc
  assert request.headers["x-forwarded-proto"] == urlsplit(
    get_settings().frontend_origin
  ).scheme
  assert request.headers["x-script-name"] == "/services/recipes"
  assert request.headers["x-forwarded-prefix"] == "/services/recipes"
  assert "authorization" not in request.headers
  assert response.status_code == 201
  assert response.content == b"proxied"
  assert response.headers["location"] == (
    f"{public.scheme}://{public.netloc}/services/recipes/welcome/"
  )
  assert response.headers.get_list("set-cookie") == [
    "sessionid=abc; Path=/services/recipes/; HttpOnly",
    "csrftoken=xyz; Path=/services/recipes/",
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


def test_surface_requires_distinct_origin_and_owner(
  client, auth, clean_local_services_config, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "domain", "localhost")
  monkeypatch.setenv("MOBIUS_SERVICE_TANDOOR_ORIGIN", "http://tandoor.localhost")
  write_config(clean_local_services_config, {
    "tandoor": {
      "upstream": "http://127.0.0.1:8123",
      "public_surface": True,
    },
  })
  assert client.get("/api/local-services/tandoor/surface").status_code == 401
  response = client.get("/api/local-services/tandoor/surface", headers=auth)
  assert response.status_code == 200
  assert response.json()["url"] == (
    "http://tandoor.localhost/services/tandoor/_mobius/surface"
  )

  adapter = client.get(
    "/services/tandoor/_mobius/surface",
    headers={"host": "tandoor.localhost"},
  )
  assert adapter.status_code == 200
  assert "x-frame-options" not in adapter.headers
  assert "child.contentDocument" in adapter.text
  assert "moebius:service-ready" in adapter.text
  assert adapter.headers["content-security-policy"].endswith(
    f"frame-ancestors 'self' {get_settings().frontend_origin}"
  )
  protected = client.get(
    "/services/tandoor/_mobius/surface",
    headers={"host": "localhost"},
  )
  assert protected.status_code == 404
  assert protected.headers["x-frame-options"] == "SAMEORIGIN"


@pytest.mark.parametrize("path", [
  "/", "/shell/", "/api/health", "/recover", "/services/recipes/",
])
def test_dedicated_surface_host_never_serves_other_platform_paths(
  client, clean_local_services_config, monkeypatch, path,
):
  monkeypatch.setattr(get_settings(), "domain", "localhost")
  monkeypatch.setenv("MOBIUS_SERVICE_TANDOOR_ORIGIN", "http://tandoor.localhost")
  write_config(clean_local_services_config, {
    "tandoor": {
      "upstream": "http://127.0.0.1:8123", "public_surface": True,
    },
  })
  response = client.get(path, headers={"host": "tandoor.localhost"})
  assert response.status_code == 404
  assert "<!doctype html>" not in response.text.lower()


def test_public_surface_drops_xfo_scopes_ancestors_and_host_only_cookies(
  client, clean_local_services_config, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "domain", "localhost")
  monkeypatch.setenv("MOBIUS_SERVICE_TANDOOR_ORIGIN", "http://tandoor.localhost")
  write_config(clean_local_services_config, {
    "tandoor": {
      "upstream": "http://127.0.0.1:8123",
      "public_surface": True,
    },
  })

  class FakeAsyncClient:
    def __init__(self, *args, **kwargs): pass
    def build_request(self, method, url, *, headers, content):
      return httpx.Request(method, url, headers=headers, content=content)
    async def send(self, request, *, stream):
      return httpx.Response(
        200,
        headers=[
          ("x-frame-options", "SAMEORIGIN"),
          ("content-security-policy", "default-src 'self'; frame-ancestors 'self'"),
          ("set-cookie", "session=abc; Domain=.localhost; Path=/services/tandoor; HttpOnly"),
        ],
        stream=httpx.ByteStream(b"ok"), request=request,
      )
    async def aclose(self): pass

  monkeypatch.setattr(local_services_module.httpx, "AsyncClient", FakeAsyncClient)
  public = client.get(
    "/services/tandoor/", headers={"host": "tandoor.localhost"},
  )
  ordinary = client.get(
    "/services/tandoor/", headers={"host": "localhost"},
  )
  assert "x-frame-options" not in public.headers
  policies = public.headers.get_list("content-security-policy")
  assert "default-src 'self'" in policies
  assert f"frame-ancestors 'self' {get_settings().frontend_origin}" in policies
  assert all(policy != "frame-ancestors 'self'" for policy in policies)
  assert "domain=" not in public.headers["set-cookie"].lower()
  assert ordinary.headers["x-frame-options"] == "SAMEORIGIN"
  assert "domain=" not in ordinary.headers["set-cookie"].lower()
  assert "path=/services/tandoor" in ordinary.headers["set-cookie"].lower()


def test_production_surface_rejects_http_localhost_fallback(
  client, auth, clean_local_services_config, monkeypatch,
):
  monkeypatch.setattr(get_settings(), "domain", "mobius.example.com")
  monkeypatch.setattr(
    get_settings(), "frontend_origin", "https://mobius.example.com",
  )
  monkeypatch.setenv(
    "MOBIUS_SERVICE_TANDOOR_ORIGIN", "http://tandoor.localhost",
  )
  write_config(clean_local_services_config, {
    "tandoor": {
      "upstream": "http://127.0.0.1:8123", "public_surface": True,
    },
  })

  response = client.get("/api/local-services/tandoor/surface", headers=auth)
  assert response.status_code == 503
  assert "invalid" in response.json()["detail"]
