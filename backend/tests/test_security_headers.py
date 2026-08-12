"""The origin owns response policy; Caddy passes it through unchanged."""

from pathlib import Path

from fastapi import Response
from fastapi.testclient import TestClient

from app import main
from app.main import (
  _PUBLISHED_SITE_CSP, _SHELL_CSP, _STATIC_EMBED_CSP, app,
)
from app.response_policy import CHAT_EMBED_CSP, app_frame_csp


def _headers(path="/api/health"):
  return TestClient(app).get(path).headers


def _publish_site(token, rel, body):
  import os
  from pathlib import Path
  p = Path(os.environ.get("DATA_DIR", "/tmp")) / "published" / token / rel
  p.parent.mkdir(parents=True, exist_ok=True)
  p.write_text(body, encoding="utf-8")


def test_published_site_runs_at_opaque_origin_but_keeps_frame_boundary():
  # A published /sites/ page must be sandboxed to an opaque origin (so its JS
  # cannot read the shell's localStorage/JWT) while keeping X-Frame-Options.
  token = "a1b2c3d4" * 4  # distinct 32-hex token; avoids other tests' fixtures
  _publish_site(token, "index.html", "<h1>live</h1>")
  h = TestClient(app).get(f"/sites/{token}/").headers
  assert h.get("content-security-policy") == _PUBLISHED_SITE_CSP
  assert "sandbox " in _PUBLISHED_SITE_CSP
  assert "allow-same-origin" not in _PUBLISHED_SITE_CSP
  # Do NOT lock resources to 'self' — /sites/ also serves external-asset sites.
  assert "default-src" not in _PUBLISHED_SITE_CSP
  # Frame boundary is kept for published pages (unlike the opaque embed).
  assert h.get("x-frame-options") == "SAMEORIGIN"
  assert h.get("x-content-type-options") == "nosniff"


def test_published_site_sandbox_survives_a_404_and_a_500(monkeypatch):
  # The boundary must ride the generic-404 and unhandled-500 paths too, not
  # only a successful serve — an error response must never drop the sandbox.
  h404 = TestClient(app).get("/sites/deadbeefdeadbeef/nope.html").headers
  assert h404.get("content-security-policy") == _PUBLISHED_SITE_CSP

  from app.routes import published as published_mod
  monkeypatch.setattr(
    published_mod, "_serve",
    lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("serve boom")),
  )
  r500 = TestClient(app, raise_server_exceptions=False).get(
    "/sites/ffffffffffffffff/"
  )
  assert r500.status_code == 500
  assert r500.headers.get("content-security-policy") == _PUBLISHED_SITE_CSP


def test_bundled_caddy_does_not_override_published_site_sandbox():
  caddyfile = Path(__file__).resolve().parents[2] / "Caddyfile"
  primary = caddyfile.read_text(encoding="utf-8").split(
    "# Full backend web services", 1,
  )[0]
  assert "reverse_proxy app:8000" in primary
  assert "Content-Security-Policy" not in primary
  assert "X-Frame-Options" not in primary
  assert "sandbox allow-scripts" in _PUBLISHED_SITE_CSP
  assert "allow-same-origin" not in _PUBLISHED_SITE_CSP
  # external-asset sites must keep loading
  assert "default-src" not in _PUBLISHED_SITE_CSP


def test_standard_security_headers_present():
  h = _headers()
  assert h.get("x-content-type-options") == "nosniff"
  assert h.get("x-frame-options") == "SAMEORIGIN"
  assert h.get("referrer-policy") == "strict-origin-when-cross-origin"
  assert h.get("permissions-policy") == "camera=(self), geolocation=(self)"
  assert "strict-transport-security" in h


def test_direct_shell_response_receives_the_origin_owned_policy():
  policy = _headers().get("content-security-policy")
  assert policy == _SHELL_CSP
  assert "https://esm.sh" in policy
  assert "img-src 'self' data: blob:" in policy
  assert "'wasm-unsafe-eval'" in policy
  assert " 'unsafe-eval'" not in policy
  assert "worker-src 'self';" in policy
  assert "script-src" in policy and "blob:" not in policy.split("script-src")[1].split(";")[0]
  assert "cross-origin-opener-policy" not in _headers()
  assert "cross-origin-embedder-policy" not in _headers()


def test_embedded_chat_allows_opaque_origin_app_ancestor():
  # Mini-apps intentionally run in an iframe without allow-same-origin. A chat
  # embed is nested below that opaque-origin ancestor, so X-Frame-Options:
  # SAMEORIGIN would make Chromium reject the otherwise same-site document.
  # The route itself is inert: no chat id or credential is accepted in its URL.
  h = _headers("/shell/embed/chat")
  assert "x-frame-options" not in h
  assert h.get("content-security-policy") == CHAT_EMBED_CSP
  assert h.get("x-content-type-options") == "nosniff"
  assert h.get("strict-transport-security")


def test_frame_exception_is_exactly_scoped_to_embed_document():
  assert _headers("/shell/embed/chat/other").get("x-frame-options") == "SAMEORIGIN"


def test_static_embed_policy_authoritatively_replaces_route_headers(monkeypatch):
  monkeypatch.setattr(
    main,
    "_serve_app_static_asset",
    lambda *_args, **_kwargs: Response(
      status_code=418,
      headers={
        "Content-Security-Policy": "default-src 'none'",
        "X-Frame-Options": "DENY",
      },
    ),
  )

  response = TestClient(app).get("/app-embeds/by-id/999/index.html")

  assert response.status_code == 418
  assert response.headers["content-security-policy"] == _STATIC_EMBED_CSP
  assert "x-frame-options" not in response.headers


def test_static_embed_names_loopback_delivery_origin_for_local_tools(monkeypatch):
  monkeypatch.setattr(
    main,
    "_serve_app_static_asset",
    lambda *_args, **_kwargs: Response(status_code=200),
  )

  response = TestClient(app, base_url="http://127.0.0.1:8123").get(
    "/app-embeds/by-id/999/index.html",
  )

  policy = response.headers["content-security-policy"]
  assert "http://127.0.0.1:8123" in policy
  assert main.settings.frontend_origin.rstrip("/") in policy
  assert "allow-same-origin" not in policy


def test_static_embed_policy_survives_unhandled_route_exception(monkeypatch):
  def _raise(*_args, **_kwargs):
    raise RuntimeError("static asset failure")

  monkeypatch.setattr(main, "_serve_app_static_asset", _raise)

  response = TestClient(
    app,
    raise_server_exceptions=False,
  ).get("/app-embeds/by-id/999/index.html")

  assert response.status_code == 500
  assert response.headers["content-security-policy"] == _STATIC_EMBED_CSP
  assert "x-frame-options" not in response.headers


def test_bundled_caddy_keeps_only_gateway_specific_response_policy():
  """The primary proxy is pass-through; gateway host behavior stays exact."""
  caddyfile = Path(__file__).resolve().parents[2] / "Caddyfile"
  text = caddyfile.read_text(encoding="utf-8")
  primary, gateway = text.split("# Full backend web services", 1)
  assert "header" not in primary
  assert "reverse_proxy app:8000" in primary
  assert "@serviceSurface path /services/*" in gateway
  assert "?Content-Security-Policy" in gateway
  assert "frame-ancestors 'self' {$FRONTEND_ORIGIN}" in gateway
  assert "-X-Frame-Options" in gateway
  assert 'respond "Not found" 404' in gateway
  assert ">X-Content-Type-Options" not in gateway
  assert ">Permissions-Policy" not in gateway
  # Direct origin policies retain the exact scoped differences Caddy used to
  # mirror by hand.
  assert "frame-ancestors 'self'" in _SHELL_CSP
  assert "frame-ancestors" not in CHAT_EMBED_CSP
  assert "sandbox allow-scripts" in _STATIC_EMBED_CSP
  assert "allow-same-origin" not in _STATIC_EMBED_CSP
  service_csp = next(
    line for line in (line.strip() for line in gateway.splitlines())
    if line.startswith("?Content-Security-Policy ")
  )
  assert "frame-ancestors 'self' {$FRONTEND_ORIGIN}" in service_csp
  assert "{$MOBIUS_SERVICE_GATEWAY_ORIGIN} {" in gateway


def test_backend_owns_complete_app_frame_policy():
  """Direct managed and proxied installs receive one exact frame policy."""
  gateway = "https://services.example.test"
  origin = main.settings.frontend_origin.rstrip("/")
  policy = app_frame_csp(origin, gateway)

  assert "sandbox allow-scripts" in policy
  assert "allow-popups-to-escape-sandbox" in policy
  assert "allow-same-origin" not in policy
  assert "'wasm-unsafe-eval'" in policy
  assert " 'unsafe-eval'" not in policy
  assert f"frame-src {origin} {gateway}" in policy

  # The frame's origin is opaque, so 'self' matches nothing in fetch
  # directives. Every intentional same-site resource source must name the
  # origin explicitly, independent of the deployment's reverse proxy.
  for directive in (
    "default-src", "script-src", "style-src", "font-src", "connect-src",
    "img-src", "frame-src",
  ):
    sources = policy.split(f"{directive} ", 1)[1].split(";", 1)[0]
    assert origin in sources
    assert "'self'" not in sources


def test_app_frame_policy_allows_a_distinct_configured_api_origin():
  policy = app_frame_csp(
    "https://app.example.test",
    api_origin="http://localhost:8000",
  )
  assert "connect-src https://app.example.test http://localhost:8000" in policy
  assert "script-src https://app.example.test" in policy
  assert "img-src https://app.example.test data: blob:" in policy


def test_app_frame_policy_does_not_infer_the_backend_localhost_default():
  policy = app_frame_csp("https://app.example.test", api_origin="")
  assert "connect-src https://app.example.test;" in policy
  assert "localhost" not in policy


def test_app_frame_policy_drops_malformed_gateway_origin():
  policy = app_frame_csp(
    main.settings.frontend_origin,
    "https://services.example.test; script-src *",
  )
  assert "services.example.test" not in policy
  assert "script-src *" not in policy


def test_compose_keeps_optional_service_gateway_inert_by_default():
  compose = (
    Path(__file__).resolve().parents[2] / "docker-compose.yml"
  ).read_text(encoding="utf-8")
  assert (
    "MOBIUS_SERVICE_GATEWAY_ORIGIN=${MOBIUS_SERVICE_GATEWAY_ORIGIN:-}"
    in compose
  )
  assert (
    "MOBIUS_SERVICE_GATEWAY_ORIGIN=${MOBIUS_SERVICE_GATEWAY_ORIGIN:-http://services.invalid}"
    in compose
  )
  assert "https://tandoor.${DOMAIN}" not in compose


def test_opaque_embed_preflight_allows_scoped_instance_header():
  response = TestClient(app).options(
    "/api/chats/exact-chat",
    headers={
      "Origin": "null",
      "Access-Control-Request-Method": "GET",
      "Access-Control-Request-Headers": (
        "authorization,x-mobius-embed-instance"
      ),
    },
  )
  assert response.status_code == 200
  allowed = response.headers["access-control-allow-headers"].lower()
  assert "x-mobius-embed-instance" in allowed


def test_opaque_embed_preflight_allows_stream_snapshot_header():
  response = TestClient(app).options(
    "/api/chats/exact-chat/stream",
    headers={
      "Origin": "null",
      "Access-Control-Request-Method": "GET",
      "Access-Control-Request-Headers": (
        "authorization,x-mobius-embed-instance,x-mobius-stream-snapshot"
      ),
    },
  )
  assert response.status_code == 200
  allowed = response.headers["access-control-allow-headers"].lower()
  assert "x-mobius-stream-snapshot" in allowed


def test_opaque_app_preflight_allows_versioned_storage_requests():
  """Sandboxed apps can perform the runtime's versioned read/write flow.

  App frames intentionally have Origin:null. The runtime opts into an ETag
  read with X-Mobius-Version, then may send If-Match or If-None-Match on a
  conditional write. Every one of those headers must survive the browser's
  CORS preflight, and the returned ETag must be readable by app JavaScript.
  """
  client = TestClient(app)
  response = client.options(
    "/api/storage/apps/62/visited.json",
    headers={
      "Origin": "null",
      "Access-Control-Request-Method": "PUT",
      "Access-Control-Request-Headers": (
        "authorization,content-type,x-mobius-version,if-match,if-none-match"
      ),
    },
  )

  assert response.status_code == 200
  allowed = {
    header.strip().lower()
    for header in response.headers["access-control-allow-headers"].split(",")
  }
  assert {
    "authorization",
    "content-type",
    "x-mobius-version",
    "if-match",
    "if-none-match",
  } <= allowed
  # Starlette correctly puts Access-Control-Expose-Headers on the actual
  # response, not the preflight response. Check that half of the contract on a
  # simple opaque-origin request.
  actual = client.get("/api/health", headers={"Origin": "null"})
  assert actual.status_code == 200
  exposed = {
    header.strip().lower()
    for header in actual.headers["access-control-expose-headers"].split(",")
  }
  assert "etag" in exposed
