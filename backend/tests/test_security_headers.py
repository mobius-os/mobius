"""The security-header middleware stamps the standard headers on every response.

These are resource-load-agnostic (clickjacking / MIME-sniff / TLS / referrer); there
is deliberately no CSP so apps can still load web images + external embeds — see
.pm/172 for why a CSP wouldn't close the owner-token exfil anyway.
"""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app


def _headers(path="/api/health"):
  return TestClient(app).get(path).headers


def test_standard_security_headers_present():
  h = _headers()
  assert h.get("x-content-type-options") == "nosniff"
  assert h.get("x-frame-options") == "SAMEORIGIN"
  assert h.get("referrer-policy") == "strict-origin-when-cross-origin"
  assert h.get("permissions-policy") == "camera=(), geolocation=()"
  assert "strict-transport-security" in h


def test_no_csp_so_apps_keep_loading_web_resources():
  # Deliberately no Content-Security-Policy — web images / external embeds must
  # not be restricted by a header. See .pm/172 (the owner-token exfil is the
  # documented same-origin trade-off; a CSP can't close it).
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


def test_bundled_caddy_mirrors_exact_embed_frame_exception():
  """The compose proxy must not silently re-add either frame blocker."""
  caddyfile = Path(__file__).resolve().parents[2] / "Caddyfile"
  lines = [line.strip() for line in caddyfile.read_text(encoding="utf-8").splitlines()]
  assert "@chatEmbed path /shell/embed/chat" in lines
  assert "@notChatEmbed not path /shell/embed/chat" in lines
  assert 'header @notChatEmbed X-Frame-Options "SAMEORIGIN"' in lines
  assert not any(
    line.startswith("X-Frame-Options ") for line in lines
  ), "X-Frame-Options must remain on the exact non-embed matcher"
  ordinary_csp = next(
    line for line in lines
    if line.startswith("header @notChatEmbed Content-Security-Policy ")
  )
  embed_csp = next(
    line for line in lines
    if line.startswith("header @chatEmbed Content-Security-Policy ")
  )
  assert "frame-ancestors 'self'" in ordinary_csp
  assert "frame-ancestors" not in embed_csp
  assert "frame-src 'self'" in embed_csp


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
