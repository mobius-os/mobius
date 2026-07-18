"""The security-header middleware stamps the standard headers on every response.

These are resource-load-agnostic (clickjacking / MIME-sniff / TLS / referrer).
The backend deliberately has no global resource CSP; exact opaque/static/service
documents supply their own policies while the bundled proxy owns shell CSP.
"""

from pathlib import Path

from fastapi import Response
from fastapi.testclient import TestClient

from app import main
from app.main import _PUBLISHED_SITE_CSP, _STATIC_EMBED_CSP, app


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


def test_bundled_caddy_mirrors_published_site_sandbox():
  caddyfile = Path(__file__).resolve().parents[2] / "Caddyfile"
  lines = [line.strip() for line in caddyfile.read_text(encoding="utf-8").splitlines()]
  # /sites/ stays inside @notFrameableEmbed so it keeps X-Frame-Options
  # SAMEORIGIN; its shell CSP is then overridden with the sandbox policy.
  assert "@publishedSite path /sites/*" in lines
  assert "/sites/*" not in (
    "@notFrameableEmbed not path /shell/embed/chat /app-embeds/by-id/* /recover*"
  ), "published sites must keep the SAMEORIGIN frame boundary"
  pub_csp = next(
    line for line in lines
    if line.startswith("header @publishedSite >Content-Security-Policy ")
  )
  assert "sandbox allow-scripts" in pub_csp
  assert "allow-same-origin" not in pub_csp
  assert "default-src" not in pub_csp  # external-asset sites must keep loading
  # The override must appear AFTER the shell-CSP line so it wins for /sites/.
  shell_idx = next(
    i for i, line in enumerate(lines)
    if line.startswith("header @notFrameableEmbed >Content-Security-Policy ")
  )
  pub_idx = next(
    i for i, line in enumerate(lines)
    if line.startswith("header @publishedSite >Content-Security-Policy ")
  )
  assert pub_idx > shell_idx


def test_standard_security_headers_present():
  h = _headers()
  assert h.get("x-content-type-options") == "nosniff"
  assert h.get("x-frame-options") == "SAMEORIGIN"
  assert h.get("referrer-policy") == "strict-origin-when-cross-origin"
  assert h.get("permissions-policy") == "camera=(), geolocation=()"
  assert "strict-transport-security" in h


def test_no_csp_so_apps_keep_loading_web_resources():
  # Deliberately no global Content-Security-Policy — ordinary app resource
  # freedom is separate from the exact document policies tested below.
  assert "content-security-policy" not in _headers()


def test_embedded_chat_allows_opaque_origin_app_ancestor():
  # Mini-apps intentionally run in an iframe without allow-same-origin. A chat
  # embed is nested below that opaque-origin ancestor, so X-Frame-Options:
  # SAMEORIGIN would make Chromium reject the otherwise same-site document.
  # The route itself is inert: no chat id or credential is accepted in its URL.
  h = _headers("/shell/embed/chat")
  assert "x-frame-options" not in h
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


def test_bundled_caddy_mirrors_exact_embed_frame_exception():
  """The compose proxy must not silently re-add either frame blocker."""
  caddyfile = Path(__file__).resolve().parents[2] / "Caddyfile"
  lines = [line.strip() for line in caddyfile.read_text(encoding="utf-8").splitlines()]
  assert "@chatEmbed path /shell/embed/chat" in lines
  assert "@staticEmbed path /app-embeds/by-id/*" in lines
  # /recover* is excluded so recoveryd's stricter X-Frame-Options DENY +
  # frame-ancestors 'none' pass through the proxy instead of being replaced
  # with the shell's weaker SAMEORIGIN/'self' policy.
  assert (
    "@notFrameableEmbed not path /shell/embed/chat /app-embeds/by-id/* /recover*"
    in lines
  )
  assert 'header @notFrameableEmbed >X-Frame-Options "SAMEORIGIN"' in lines
  assert not any(
    line.startswith("X-Frame-Options ") for line in lines
  ), "X-Frame-Options must remain on the exact non-embed matcher"
  ordinary_csp = next(
    line for line in lines
    if line.startswith("header @notFrameableEmbed >Content-Security-Policy ")
  )
  embed_csp = next(
    line for line in lines
    if line.startswith("header @chatEmbed >Content-Security-Policy ")
  )
  assert "frame-ancestors 'self'" in ordinary_csp
  assert "frame-src 'self' {$MOBIUS_SERVICE_GATEWAY_ORIGIN}" in ordinary_csp
  assert "frame-src *" not in ordinary_csp
  assert "frame-ancestors" not in embed_csp
  assert "frame-src 'self'" in embed_csp
  assert "MOBIUS_SERVICE_GATEWAY_ORIGIN" not in embed_csp
  # Both the shell and the embedded chat render upload previews and the image
  # lightbox via URL.createObjectURL; a policy without blob: breaks those.
  assert "img-src 'self' data: blob:" in ordinary_csp
  assert "img-src 'self' data: blob:" in embed_csp
  assert "https://cdn.openai.com" in ordinary_csp
  assert "https://cdn.openai.com" in embed_csp
  static_csp = next(
    line for line in lines
    if line.startswith("header @staticEmbed >Content-Security-Policy ")
  )
  assert "sandbox allow-scripts" in static_csp
  assert "allow-same-origin" not in static_csp
  assert "frame-ancestors" not in static_csp
  assert "MOBIUS_SERVICE_GATEWAY_ORIGIN" not in static_csp
  service_csp = next(
    line for line in lines
    if line.startswith("?Content-Security-Policy ")
  )
  assert "frame-ancestors 'self' {$FRONTEND_ORIGIN}" in service_csp
  assert "{$MOBIUS_SERVICE_GATEWAY_ORIGIN} {" in lines
  assert "@serviceSurface path /services/*" in lines
  assert "respond \"Not found\" 404" in lines
  assert "-X-Frame-Options" in lines
  for name in (
    "X-Content-Type-Options", "Referrer-Policy", "Permissions-Policy",
    "Strict-Transport-Security",
  ):
    assert any(line.startswith(f">{name} ") for line in lines), (
      f"{name} must replace, not duplicate, the backend value after proxying"
    )


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
