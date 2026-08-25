"""A sandboxed app frame must be answered with `*`, not the literal `null`.

Mini-app frames drop `allow-same-origin`, so the browser sends `Origin: null`.
CORSMiddleware echoes the matched value, producing
`Access-Control-Allow-Origin: null`. Chromium accepts that as a match; WebKit
does not and blocks the response before the page sees it, so on iOS every
direct API call from an app frame failed as if the network were down while the
same app's storage kept working (that path goes through the shell, not the
frame). Answering `*` is what the opaque-frame asset routes already do.

`*` is legal here only because `allow_credentials=False` — nothing ambient
rides along, so a response still requires a bearer token the frame already
holds and no other origin can obtain.
"""


from app.main import settings


def _origin(response):
  return response.headers.get("access-control-allow-origin")


def test_sandboxed_frame_preflight_is_answered_with_a_wildcard(client):
  r = client.options(
    "/api/github/status",
    headers={
      "Origin": "null",
      "Access-Control-Request-Method": "GET",
      "Access-Control-Request-Headers": "authorization",
    },
  )
  assert r.status_code == 200
  # The literal "null" is exactly what WebKit refuses to match.
  assert _origin(r) == "*"


def test_sandboxed_frame_can_preflight_secret_existence_head(client):
  """Encrypted app secrets deliberately expose existence through HEAD.
  Authorization makes the otherwise-safelisted method preflight, so HEAD must
  be present in Access-Control-Allow-Methods or the setup screen can save a key
  but can never recognize it after remounting."""
  r = client.options(
    "/api/apps/1/secrets/provider-key",
    headers={
      "Origin": "null",
      "Access-Control-Request-Method": "HEAD",
      "Access-Control-Request-Headers": "authorization",
    },
  )
  assert r.status_code == 200
  allowed = r.headers.get("access-control-allow-methods", "").upper()
  assert "HEAD" in {method.strip() for method in allowed.split(",")}


def test_connector_mutation_preflight_allows_the_generation_header(client):
  """The Connections app's frame is opaque-origin: every toggle/re-check
  sends X-Mobius-Connector-Generation, which the browser preflights. If the
  header falls out of the allow-list, mutations die client-side as
  "Failed to fetch" while same-origin shell calls keep working."""
  r = client.options(
    "/api/connectors/1",
    headers={
      "Origin": "null",
      "Access-Control-Request-Method": "PATCH",
      "Access-Control-Request-Headers":
        "authorization,content-type,x-mobius-connector-generation",
    },
  )
  assert r.status_code == 200
  allowed = r.headers.get("access-control-allow-headers", "").lower()
  assert "x-mobius-connector-generation" in allowed


def test_sandboxed_frame_gets_a_wildcard_on_the_real_response_too(client, auth):
  # A preflight alone is not enough: WebKit checks the actual response as well.
  r = client.get("/api/apps/", headers={"Origin": "null", **auth})
  assert r.status_code == 200
  assert _origin(r) == "*"


def test_an_unauthenticated_sandboxed_request_is_still_refused(client):
  # Widening the CORS answer must not widen who may read anything: the bearer
  # token remains the gate, and it lives where no other origin can reach it.
  r = client.get("/api/apps/", headers={"Origin": "null"})
  assert r.status_code == 401


def test_the_ordinary_shell_origin_is_untouched(client, auth):
  # Assert the echoed origin exactly. `!= "*"` would also pass if the header
  # vanished altogether — and it did: sending a hardcoded shell origin that is
  # not the CONFIGURED one is simply an unmatched origin, so CORSMiddleware
  # omits the header and the loose assertion held for the wrong reason. Drive
  # this from the configured origin so it proves the shell still gets its echo.
  r = client.get("/api/apps/", headers={"Origin": settings.frontend_origin, **auth})
  assert _origin(r) == settings.frontend_origin


def test_requests_without_an_origin_are_untouched(client, auth):
  r = client.get("/api/apps/", headers=auth)
  assert r.status_code == 200
  assert _origin(r) is None
