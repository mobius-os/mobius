"""The security-header middleware stamps the standard headers on every response.

These are resource-load-agnostic (clickjacking / MIME-sniff / TLS / referrer); there
is deliberately no CSP so apps can still load web images + external embeds — see
.pm/172 for why a CSP wouldn't close the owner-token exfil anyway.
"""

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
