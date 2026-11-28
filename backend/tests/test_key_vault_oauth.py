"""Sign-ins (``oauth2_refresh``): the vault holds an OAuth client and the
refresh token signing in gives, renews access tokens itself, and never hands
any of them to the caller.

A local stand-in plays the provider's token and revoke endpoints and an
echoing site, first behind the vault socket, then behind the server's routes.
"""

from __future__ import annotations

import base64
import http.client
import importlib.util
import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app import auth as auth_mod
from app import key_vault, models, push


VAULT_PATH = Path(__file__).parents[1] / "vault" / "keyvault.py"
SPEC = importlib.util.spec_from_file_location("mobius_key_vault_oauth", VAULT_PATH)
kv = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = kv
SPEC.loader.exec_module(kv)

CLIENT_ID = "1234-fake.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-fake-client-secret"
REFRESH = "1//fake-refresh-token-0"
RECIPE = {
  "type": "oauth2_refresh", "client_id": "client_id", "client_secret": "client_secret",
  "refresh_token": "refresh_token", "token_endpoint": "https://oauth2.googleapis.com/token",
  "revoke_endpoint": "https://oauth2.googleapis.com/revoke",
}
CLIENT_PARTS = [{"name": "client_id", "label": "Client ID", "secret": False},
                {"name": "client_secret", "label": "Client secret"}]
SITE = {"host": "gmail.googleapis.com", "methods": ["GET", "POST"], "paths": ["/gmail/*"]}
GRANTS = {"apps": [], "agents": True, "sites": [SITE]}
AGENT = "agent:c1"
REDIRECT = "https://mobius.example.com/apps/mail/signed-in"


def _id_token(claims: dict) -> str:
  body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
  return f"eyJhbGciOiJSUzI1NiJ9.{body}.c2ln"


class _Provider(BaseHTTPRequestHandler):
  """The provider's token and revoke endpoints, and an echoing API site.

  ``rotate`` hands back a new refresh token with every renewal (and refuses
  one spent before, as providers that rotate do); ``expires_in`` sets how
  long its access tokens last; ``refuse`` answers renewals with that error.
  """

  token_requests: list = []
  revoked: list = []
  spent: set = set()
  rotate = False
  expires_in = 3599
  refuse = None
  site_status = 200
  slow = 0.0
  echo = False

  def log_message(self, *args):
    pass

  def _json(self, status, payload):
    data = json.dumps(payload).encode()
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(data)))
    self.end_headers()
    self.wfile.write(data)

  def _answer(self):
    cls = type(self)
    size = int(self.headers.get("Content-Length") or 0)
    body = self.rfile.read(size).decode() if size else ""
    form = dict(urllib.parse.parse_qsl(body))
    if self.path == "/token":
      cls.token_requests.append({"headers": dict(self.headers), "form": form})
      time.sleep(cls.slow)
      if cls.refuse:
        return self._json(400, {"error": cls.refuse, "error_description": f"Bad client {CLIENT_SECRET}."})
      if form.get("grant_type") == "authorization_code":
        if form.get("code") == "no-offline":
          return self._json(200, {"access_token": "ya29.fake-access-no-offline", "expires_in": 3599})
        scope, email = "https://www.googleapis.com/auth/gmail.readonly openid email", "owner@example.com"
        if cls.echo:
          # A provider (or a site posing as one) handing the secrets back.
          scope, email = f"openid {REFRESH}", f"{CLIENT_SECRET}@example.com"
        return self._json(200, {
          "access_token": "ya29.fake-access-from-code", "expires_in": 3599, "refresh_token": REFRESH,
          "scope": scope, "id_token": _id_token({"email": email, "sub": "1"}), "token_type": "Bearer",
        })
      used = form.get("refresh_token")
      if cls.rotate and used in cls.spent:
        return self._json(400, {"error": "invalid_grant", "error_description": "Token reused."})
      cls.spent.add(used)
      answer = {"access_token": f"ya29.fake-access-{len(cls.token_requests)}", "expires_in": cls.expires_in}
      if cls.rotate:
        answer["refresh_token"] = f"1//fake-refresh-token-{len(cls.token_requests)}"
      return self._json(200, answer)
    if self.path == "/revoke":
      cls.revoked.append(form)
      return self._json(200, {})
    self._json(cls.site_status, {"path": self.path, "headers": dict(self.headers), "body": body})

  do_GET = do_POST = _answer


@pytest.fixture(autouse=True)
def provider():
  _Provider.token_requests, _Provider.revoked, _Provider.spent = [], [], set()
  _Provider.rotate, _Provider.expires_in, _Provider.refuse, _Provider.site_status, _Provider.slow = (
    False, 3599, None, 200, 0.0)
  _Provider.echo = False
  upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Provider)
  threading.Thread(target=upstream.serve_forever, daemon=True).start()
  yield upstream
  upstream.shutdown()


def _serve(vault, provider):
  """The vault socket over ``vault``, every host reaching the stand-in."""
  reached = []

  def connect(host, timeout):
    reached.append(host)
    return http.client.HTTPConnection("127.0.0.1", provider.server_address[1], timeout=timeout)
  folder = Path(tempfile.mkdtemp(prefix="kvo-"))
  server = kv.VaultServer(str(folder / kv.SOCKET_NAME),
                          type("OAuthVault", (kv.VaultHandler,), {"vault": vault, "connect": staticmethod(connect)}))
  threading.Thread(target=server.serve_forever, daemon=True).start()
  return server, folder, reached


@pytest.fixture
def vault(tmp_path):
  return kv.Vault(tmp_path / "vault")


@pytest.fixture
def vc(vault, provider):
  server, folder, reached = _serve(vault, provider)
  try:
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(folder / kv.SOCKET_NAME)),
                      base_url="http://vault", timeout=30) as session:
      session.reached = reached
      session.server = server
      yield session
  finally:
    server.shutdown()
    server.server_close()
    shutil.rmtree(folder, ignore_errors=True)


def _key(vault, name="gmail", *, signed_in=True, recipe=None, grants=None, client_secret=CLIENT_SECRET):
  parts = [{"name": "client_id", "value": CLIENT_ID, "secret": False, "label": "Client ID"},
           {"name": "client_secret", "value": client_secret, "label": "Client secret"}]
  if signed_in:
    parts.append({"name": "refresh_token", "value": REFRESH, "label": "Sign-in"})
  vault.put_key(name, {"parts": parts, "inject": recipe or RECIPE, "grants": grants or GRANTS})


def _use(vc, key="gmail", path="/gmail/v1/users/me/labels", caller=AGENT, host="gmail.googleapis.com", headers=None):
  return vc.get(f"/use/{key}/{host}{path}", headers={"X-Vault-Caller": caller, **(headers or {})})


def _stored(vault, name="gmail", part="refresh_token"):
  return vault._open(name, vault._records[name], part)


# -- the recipe --------------------------------------------------------------------

@pytest.mark.parametrize("endpoint", [
  "http://oauth2.googleapis.com/token", "https://oauth2.googleapis.com:8443/token",
  "https://oauth2.googleapis.com:443/token", "https://user@oauth2.googleapis.com/token",
  "https://oauth2.googleapis.com/token?client=x", "https://oauth2.googleapis.com/token#x",
  "https://oauth2.googleapis.com", "https://*.googleapis.com/token", "https://127.0.0.1/token",
  "https://oauth2.googleapis.com/a/../token", "https://oauth2.googleapis.com/a%2Ftoken",
  "https://oauth2.googleapis.com/" + "t" * 600, None,
])
def test_a_sign_in_endpoint_is_https_on_an_exact_host_with_only_a_path(endpoint):
  with pytest.raises(kv.VaultError, match="https address with a path") as refused:
    kv.validate_inject({**RECIPE, "token_endpoint": endpoint})
  assert refused.value.status == 400


@pytest.mark.parametrize("change, message", [
  ({"client_secret": "client_id"}, "different parts"),
  ({"client_auth": "header"}, "post"),
  ({"client_secret": None}, "client secret"),
  ({"refresh_token": "bad name"}, "refresh token"),
])
def test_invalid_sign_in_recipes_are_refused(change, message):
  with pytest.raises(kv.VaultError, match=message):
    kv.validate_inject({**RECIPE, **change})


def test_a_sign_in_recipe_is_stored_as_the_owner_approved_it():
  recipe = kv.validate_inject({**RECIPE, "token_endpoint": "https://OAuth2.GoogleApis.com/token"})
  assert recipe == {**RECIPE, "client_auth": "post", "token_endpoint": "https://oauth2.googleapis.com/token"}
  assert kv.recipe_parts(recipe) == ["client_id", "client_secret", "refresh_token"]
  # Signing in fills the refresh token, so the client alone fits the recipe.
  assert kv.recipe_fits(recipe, ["client_id", "client_secret"]) == []
  assert kv.recipe_fits(recipe, ["client_secret"]) == ["client_id"]


def test_the_client_secret_and_refresh_token_are_always_secret(vault):
  with pytest.raises(kv.VaultError, match="stays secret"):
    vault.put_key("gmail", {"inject": RECIPE, "parts": [
      {"name": "client_id", "value": CLIENT_ID, "secret": False},
      {"name": "client_secret", "value": CLIENT_SECRET, "secret": False},
      {"name": "refresh_token", "value": REFRESH}]})
  # The client id is what the consent page shows: it stays plain.
  with pytest.raises(kv.VaultError, match="isn't secret"):
    vault.put_key("gmail", {"inject": RECIPE, "parts": [
      {"name": "client_id", "value": CLIENT_ID}, {"name": "client_secret", "value": CLIENT_SECRET}]})
  with pytest.raises(kv.VaultError, match="isn't secret"):
    vault.request_for_agents({"key": "gmail", "reason": "r", "sites": [SITE], "inject": RECIPE,
                              "parts": [{"name": "client_id"}, {"name": "client_secret"}]}, actor=AGENT)


# -- asking, approving and borrowing before a sign-in ------------------------------

def test_a_sign_in_key_is_asked_for_and_approved_with_only_its_client(vault):
  vault.request_for_agents({"key": "gmail", "reason": "Read your mail", "sites": [SITE],
                            "inject": RECIPE, "parts": CLIENT_PARTS}, actor=AGENT)
  [request] = vault.list_requests()
  # The card asks for the client id and secret, never for a refresh token.
  assert [part["name"] for part in request["parts"]] == ["client_id", "client_secret"]
  assert request["inject"]["token_endpoint"] == "https://oauth2.googleapis.com/token"
  vault.approve_request(request["id"], {"ticket": request["ticket"], "sites": [SITE],
                                        "parts": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}})
  [key] = vault.list_keys()
  assert [(p["name"], p["secret"]) for p in key["parts"]] == [("client_id", False), ("client_secret", True)]
  assert vault.sign_in_status("gmail", ("agent", "c1")) == {
    "signed_in": False, "token_host": "oauth2.googleapis.com", "client_id": CLIENT_ID}


def test_a_request_never_asks_the_owner_to_paste_the_refresh_token(vault):
  with pytest.raises(kv.VaultError, match="Signing in fills refresh_token"):
    vault.request_for_agents({"key": "gmail", "reason": "r", "sites": [SITE], "inject": RECIPE,
                              "parts": [*CLIENT_PARTS, {"name": "refresh_token"}]}, actor=AGENT)


def test_a_borrow_before_anyone_signs_in_says_so_with_a_code(vault, vc):
  _key(vault, signed_in=False)
  answer = _use(vc)
  assert answer.status_code == 409
  assert answer.json() == {"error": "denied", "reason": "This key isn't signed in yet.", "code": "not_signed_in"}
  assert _Provider.token_requests == []


# -- renewing ------------------------------------------------------------------------

def test_a_borrow_renews_once_and_sends_the_token_without_showing_it(vault, vc):
  _key(vault)
  first = _use(vc, headers={"Authorization": "Bearer forged"})
  second = _use(vc)
  assert first.status_code == second.status_code == 200, first.text
  assert first.json()["headers"]["Authorization"] == "Bearer [redacted by key vault]"
  assert "ya29" not in first.text + second.text and "forged" not in first.text
  [renewal] = _Provider.token_requests
  assert renewal["form"] == {"grant_type": "refresh_token", "refresh_token": REFRESH,
                             "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
  assert vc.reached.count("oauth2.googleapis.com") == 1


def test_a_basic_client_is_sent_in_the_authorization_header(vault, vc):
  _key(vault, recipe={**RECIPE, "client_auth": "basic"})
  assert _use(vc).status_code == 200
  [renewal] = _Provider.token_requests
  assert "client_secret" not in renewal["form"] and "client_id" not in renewal["form"]
  pair = base64.b64decode(renewal["headers"]["Authorization"].removeprefix("Basic ")).decode()
  assert pair == f"{urllib.parse.quote_plus(CLIENT_ID)}:{urllib.parse.quote_plus(CLIENT_SECRET)}"


def test_a_rotated_refresh_token_is_saved_and_audited_as_the_vaults_own_change(vault, vc):
  _Provider.rotate = True
  _key(vault)
  revision = vault._records["gmail"]["revision"]
  assert _use(vc).status_code == 200
  assert _stored(vault) == "1//fake-refresh-token-1"
  assert vault._records["gmail"]["revision"] == revision + 1
  [renewed] = [e for e in vault.usage(key="gmail") if e["outcome"] == "sign-in renewed"]
  assert renewed["caller"] == "platform:oauth-refresh"
  assert "fake-refresh" not in vault.audit_path.read_text()
  # The token the renewal bought is cached under the renewed sign-in too.
  assert _use(vc).status_code == 200
  assert len(_Provider.token_requests) == 1


def test_simultaneous_borrows_never_spend_a_rotating_refresh_token_twice(vault, vc):
  """Tokens that are stale at once make every borrow renew: one at a time,
  each with the refresh token the one before it saved."""
  _Provider.rotate, _Provider.expires_in, _Provider.slow = True, 30, 0.05
  _key(vault)
  answers = []
  threads = [threading.Thread(target=lambda: answers.append(_use(vc).status_code)) for _ in range(5)]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join()
  assert answers == [200] * 5
  spent = [request["form"]["refresh_token"] for request in _Provider.token_requests]
  assert len(spent) == 5 and len(set(spent)) == 5
  assert _stored(vault) == "1//fake-refresh-token-5"


def test_a_refused_refresh_token_asks_for_a_new_sign_in(vault, vc):
  _Provider.refuse = "invalid_grant"
  _key(vault)
  answer = _use(vc)
  assert answer.status_code == 409
  assert answer.json()["code"] == "reauth_required"
  assert answer.json()["reason"] == "The sign-in for this key has expired or was revoked; sign in again."


def test_a_refused_sign_in_is_remembered_until_the_key_changes(vault, vc):
  _Provider.refuse = "invalid_grant"
  _key(vault)
  assert [_use(vc).json()["code"] for _ in range(3)] == ["reauth_required"] * 3
  assert len(_Provider.token_requests) == 1
  # Any change to the key's parts tries the provider again.
  vault.put_key("gmail", {"parts": [{"name": "client_secret", "value": CLIENT_SECRET + "-new"}]})
  assert _use(vc).json()["code"] == "reauth_required"
  assert len(_Provider.token_requests) == 2


def test_signing_in_again_clears_a_refused_sign_in(vault, vc):
  _Provider.refuse = "invalid_grant"
  _key(vault)
  assert _use(vc).json()["code"] == "reauth_required"
  _Provider.refuse = None
  # The same refresh token comes back from the new sign-in: still cleared.
  assert _exchange(vc).status_code == 200
  assert _use(vc).status_code == 200


def test_another_token_failure_is_a_bad_gateway_without_the_clients_secret(vault, vc):
  _Provider.refuse = "invalid_client"
  _key(vault)
  answer = _use(vc)
  assert answer.status_code == 502 and "code" not in answer.json()
  # Fixed words and the provider's error code: its free text never reaches the caller.
  assert answer.json()["reason"] == "oauth2.googleapis.com refused the token (400, invalid_client)."
  assert CLIENT_SECRET not in answer.text and "Bad client" not in answer.text
  assert vault.usage(key="gmail")[0]["outcome"] == "failed"


@pytest.mark.parametrize("error", [CLIENT_SECRET, REFRESH, "client_secret_is_abc", "invalid_client"])
def test_a_token_refusal_relays_only_a_standard_error_code_that_is_no_secret(vault, vc, error):
  _key(vault, client_secret="invalid_client" if error == "invalid_client" else CLIENT_SECRET)
  _Provider.refuse = error
  answer = _use(vc)
  assert answer.status_code == 502
  assert answer.json()["reason"] == "oauth2.googleapis.com refused the token (400)."


def test_a_token_the_site_refuses_is_dropped_and_renewed(vault, vc):
  _key(vault)
  _Provider.site_status = 401
  assert _use(vc).status_code == 401
  _Provider.site_status = 200
  assert _use(vc).status_code == 200
  assert len(_Provider.token_requests) == 2


@pytest.mark.parametrize("host, path", [
  ("oauth2.googleapis.com", "/token"), ("oauth2.googleapis.com", "/Token/"), ("oauth2.googleapis.com", "/revoke"),
  ("iamcredentials.googleapis.com", "/v1/projects/-/serviceAccounts/x:generateAccessToken"),
])
def test_a_sign_in_token_never_reaches_its_own_token_endpoint(vault, host, path):
  site = {"host": host, "methods": ["POST"], "paths": ["/*"]}
  _key(vault, grants={"apps": [], "agents": True, "sites": [site]})
  with pytest.raises(kv.VaultError, match="new credentials|reached only by the vault") as refused:
    vault.authorize("gmail", ("agent", "c1"), "POST", host, path)
  assert refused.value.status == 403


def test_a_sign_in_token_still_reaches_the_token_hosts_other_paths(vault):
  site = {"host": "oauth2.googleapis.com", "methods": ["GET"], "paths": ["/*"]}
  _key(vault, grants={"apps": [], "agents": True, "sites": [site]})
  assert vault.authorize("gmail", ("agent", "c1"), "GET", "oauth2.googleapis.com", "/tokeninfo").inject["type"] == "oauth2_refresh"


# -- signing in and out --------------------------------------------------------------

def _exchange(vc, caller=AGENT, key="gmail", **body):
  return vc.post(f"/keys/{key}/oauth2/exchange", headers={"X-Vault-Caller": caller},
                     json={"code": "4/fake-code", "redirect_uri": REDIRECT, **body})


def test_signing_in_keeps_the_refresh_token_and_answers_no_token(vault, vc):
  _key(vault, signed_in=False)
  verifier = "v" * 43
  answer = _exchange(vc, code_verifier=verifier)
  assert answer.status_code == 200, answer.text
  assert answer.json() == {"signed_in": True, "email": "owner@example.com",
                           "scope": "https://www.googleapis.com/auth/gmail.readonly openid email"}
  [request] = _Provider.token_requests
  assert request["form"] == {"grant_type": "authorization_code", "code": "4/fake-code", "redirect_uri": REDIRECT,
                             "code_verifier": verifier, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}
  assert _stored(vault) == REFRESH
  assert [p for p in vault.list_keys()[0]["parts"] if p["name"] == "refresh_token"] == [
    {"name": "refresh_token", "label": "Sign-in", "secret": True, "multiline": False}]
  [signed] = [e for e in vault.usage(key="gmail") if e["outcome"] == "signed in"]
  assert signed["caller"] == AGENT
  # The access token the sign-in bought is used at once, never shown.
  used = _use(vc)
  assert used.status_code == 200 and "ya29" not in used.text
  assert len(_Provider.token_requests) == 1


def test_a_sign_in_answer_never_relays_a_secret_the_provider_echoes(vault, vc):
  _Provider.echo = True
  _key(vault, signed_in=False)
  answer = _exchange(vc)
  assert answer.status_code == 200
  assert answer.json() == {"signed_in": True, "scope": None, "email": None}
  assert REFRESH not in answer.text and CLIENT_SECRET not in answer.text


@pytest.mark.parametrize("scope, email", [
  ("openid <script>", "owner@example.com\nX"), ("x" * 300, "not-an-address"), (["openid"], "a@b"),
])
def test_a_sign_in_answer_relays_only_a_wellformed_scope_and_email(scope, email):
  answer = {"scope": scope, "id_token": _id_token({"email": email})}
  assert kv.signed_in_answer(answer) == {"signed_in": True, "scope": None, "email": None}
  good = {"scope": "openid https://www.googleapis.com/auth/calendar", "id_token": _id_token({"email": "a.b+c@example.co.uk"})}
  assert kv.signed_in_answer(good)["email"] == "a.b+c@example.co.uk"


def test_an_agent_cannot_sign_in_a_key_kept_from_agents(vault, vc):
  _key(vault, grants={"apps": [], "agents": False, "sites": [SITE]})
  assert vc.get("/keys/gmail/oauth2", headers={"X-Vault-Caller": AGENT}).status_code == 403
  assert _exchange(vc).status_code == 403
  out = vc.post("/keys/gmail/oauth2/disconnect", headers={"X-Vault-Caller": AGENT})
  assert out.status_code == 403 and "Agents are switched off" in out.json()["error"]
  assert _Provider.token_requests == [] and _Provider.revoked == []
  assert vault.sign_in_status("gmail", ("owner", ""))["signed_in"] is True


def test_a_sign_in_without_a_lasting_refresh_token_explains_itself(vault, vc):
  _key(vault, signed_in=False)
  answer = _exchange(vc, code="no-offline")
  assert answer.status_code == 502
  assert "offline access" in answer.json()["error"] and "ya29" not in answer.text
  assert vault.sign_in_status("gmail", ("owner", ""))["signed_in"] is False


@pytest.mark.parametrize("body, message", [
  ({"code": ""}, "the code"), ({"code": "a b"}, "the code"), ({"code": "x" * 2049}, "the code"),
  ({"redirect_uri": "http://mobius.example.com/x"}, "redirect_uri"), ({"redirect_uri": None}, "redirect_uri"),
  ({"code_verifier": "short"}, "code_verifier"),
])
def test_a_sign_in_is_finished_only_with_a_wellformed_answer(vault, vc, body, message):
  _key(vault, signed_in=False)
  answer = _exchange(vc, **body)
  assert answer.status_code == 400 and message in answer.json()["error"]
  assert _Provider.token_requests == []


def test_signing_out_revokes_and_removes_the_sign_in(vault, vc):
  _key(vault)
  assert _use(vc).status_code == 200
  answer = vc.post("/keys/gmail/oauth2/disconnect", headers={"X-Vault-Caller": "owner"})
  assert answer.json() == {"signed_in": False, "revoked": True}
  assert _Provider.revoked == [{"token": REFRESH, "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}]
  assert vault.sign_in_status("gmail", ("owner", ""))["signed_in"] is False
  # The cached access token goes with it.
  assert _use(vc).json()["code"] == "not_signed_in"


def test_a_renewal_finishing_after_a_sign_out_never_brings_the_sign_in_back(vault):
  _key(vault)
  inject = vault._records["gmail"]["inject"]
  assert vault.save_sign_in("gmail", inject, None, caller=("owner", ""))
  assert not vault.save_sign_in("gmail", inject, "1//late", caller=kv.OAUTH_RENEWER, replaces=REFRESH)
  assert vault.sign_in_status("gmail", ("owner", ""))["signed_in"] is False


def _app_grant(paused=False, sites=(SITE,)):
  return {"apps": [{"id": 7, "install": "a" * 32, "sites": list(sites), "paused": paused}],
          "agents": False, "sites": []}


@pytest.mark.parametrize("grants", [
  None, _app_grant(paused=True), _app_grant(sites=()),
], ids=["no-grant", "paused", "no-sites"])
def test_an_app_signs_in_only_a_key_it_may_use(vault, vc, grants):
  _key(vault, signed_in=False, grants=grants or GRANTS)
  app = f"app:7:{'a' * 32}"
  refused = _exchange(vc, caller=app)
  unknown = _exchange(vc, caller=app, key="nonexistent")
  assert refused.status_code == unknown.status_code == 403 and refused.json() == unknown.json()
  assert vc.get("/keys/gmail/oauth2", headers={"X-Vault-Caller": app}).status_code == 403
  assert _Provider.token_requests == []


def test_an_app_with_a_live_grant_signs_in_and_borrows(vault, vc):
  _key(vault, signed_in=False, grants=_app_grant())
  app = f"app:7:{'a' * 32}"
  assert vc.get("/keys/gmail/oauth2", headers={"X-Vault-Caller": app}).json()["signed_in"] is False
  assert _exchange(vc, caller=app).status_code == 200
  assert _use(vc, caller=app).status_code == 200
  [signed] = [e for e in vault.usage(key="gmail") if e["outcome"] == "signed in"]
  assert signed["caller"] == app


def test_a_key_sent_another_way_has_no_sign_in(vault, vc):
  vault.put_key("plain", {"value": "plain-key-0123456789", "inject": kv.DEFAULT_INJECT, "grants": GRANTS})
  answer = vc.get("/keys/plain/oauth2", headers={"X-Vault-Caller": "owner"})
  assert answer.status_code == 409 and "isn't sent as a sign-in" in answer.json()["error"]


def test_store_key_takes_a_sign_in_recipe(monkeypatch, vault):
  import io
  spec = importlib.util.spec_from_file_location("mobius_store_key_oauth", Path(__file__).parents[1] / "scripts" / "store-key.py")
  store_key = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(store_key)
  folder = Path(tempfile.mkdtemp(prefix="kvk-"))
  server = kv.VaultServer(str(folder / kv.SOCKET_NAME), type("StoreKeyVault", (kv.VaultHandler,), {"vault": vault}))
  threading.Thread(target=server.serve_forever, daemon=True).start()
  try:
    monkeypatch.setattr(store_key, "VAULT_SOCKET", folder / kv.SOCKET_NAME)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET})))
    assert store_key.main(["gmail", "--part", "client_id:plain:Client ID", "--part", "client_secret::Client secret",
                           "--recipe", json.dumps(RECIPE)]) == 0
  finally:
    server.shutdown()
    server.server_close()
    shutil.rmtree(folder, ignore_errors=True)
  [key] = vault.list_keys()
  assert key["inject"]["type"] == "oauth2_refresh"
  assert vault.sign_in_status("gmail", ("owner", ""))["signed_in"] is False


# -- through the server's routes -----------------------------------------------------

@pytest.fixture
def routed(monkeypatch, provider, tmp_path):
  """A real vault behind the server's routes, every host reaching the stand-in."""
  store = kv.Vault(tmp_path / "store")
  server, folder, _ = _serve(store, provider)
  monkeypatch.setattr(key_vault, "vault_socket", lambda: folder / kv.SOCKET_NAME)
  monkeypatch.setattr(key_vault, "vault_dir", lambda: tmp_path / "store")
  yield store
  server.shutdown()
  server.server_close()
  shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def pushes(monkeypatch):
  sent = []

  async def record(db, owner_id, **fields):
    sent.append(fields)
  monkeypatch.setattr(push, "notify_owner_async", record)
  return sent


def _app(db, slug="mail"):
  app = models.App(source_dir=f"/tmp/mobius-tests/{slug}", name=slug.title(), slug=slug, description="test",
                   jsx_source="export default function App() { return null }", compiled_path=f"/tmp/{slug}.js")
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def _app_auth(db, app, **kwargs):
  owner = db.query(models.Owner).first()
  return {"Authorization": f"Bearer {auth_mod.create_app_token(app.id, owner.username, owner.token_epoch, app.token_nonce, **kwargs)}"}


def _granted(store, app, paused=False):
  _key(store, signed_in=False, grants={"apps": [{"id": app.id, "install": app.install_id, "sites": [SITE],
                                                 "paused": paused}], "agents": False, "sites": []})


SIGN_IN = {"code": "4/fake-code", "redirect_uri": REDIRECT}


def test_an_app_signs_in_through_the_server_and_borrows_without_seeing_a_token(client, auth, db, routed, pushes):
  mail = _app(db)
  _granted(routed, mail)
  app_auth = _app_auth(db, mail)
  status = client.get("/api/keys/gmail/oauth2", headers=app_auth)
  assert status.json() == {"signed_in": False, "token_host": "oauth2.googleapis.com", "client_id": CLIENT_ID}
  # Before signing in, a borrow's refusal reaches the app with its code.
  early = client.get("/api/keys/use/gmail/gmail.googleapis.com/gmail/v1/users/me/labels", headers=app_auth)
  assert early.status_code == 409 and early.json()["code"] == "not_signed_in"
  signed = client.post("/api/keys/gmail/oauth2/exchange", headers=app_auth, json=SIGN_IN)
  assert signed.status_code == 200, signed.text
  assert signed.json()["signed_in"] is True and signed.json()["email"] == "owner@example.com"
  assert "ya29" not in signed.text and REFRESH not in signed.text and "id_token" not in signed.text
  used = client.get("/api/keys/use/gmail/gmail.googleapis.com/gmail/v1/users/me/labels", headers=app_auth)
  assert used.status_code == 200
  assert used.json()["headers"]["Authorization"] == "Bearer [redacted by key vault]"
  out = client.post("/api/keys/gmail/oauth2/disconnect", headers=app_auth)
  assert out.json() == {"signed_in": False, "revoked": True}
  # An app signing in its own granted key tells no one: the owner was there.
  assert pushes == []


def test_a_paused_or_ungranted_app_cannot_sign_a_key_in(client, auth, db, routed):
  mail, other = _app(db), _app(db, "other")
  _granted(routed, mail, paused=True)
  for app in (mail, other):
    answer = client.post("/api/keys/gmail/oauth2/exchange", headers=_app_auth(db, app), json=SIGN_IN)
    assert answer.status_code == 403
  assert _Provider.token_requests == []


def _helper_auth(db):
  from app.chat_writer import create_chat
  db.add(create_chat(id="helper-chat", title="Helper"))
  db.add(create_chat(id="parent-chat", title="Parent"))
  db.add(models.Delegation(
    id="oauth-helper", parent_chat_id="parent-chat", parent_root_run_id="parent-run",
    task_key="oauth-helper", child_chat_id="helper-chat", provider="claude", scope="write",
    cwd="/tmp", prompt_sha256="0" * 64,
  ))
  db.commit()
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token("helper-chat", owner.username, owner.token_epoch,
                                      delegation_id="oauth-helper", delegation_chat="helper-chat")
  return {"Authorization": f"Bearer {token}"}


def test_a_delegated_helper_may_check_but_never_sign_in_or_out(client, auth, db, routed):
  _key(routed, signed_in=False)
  helper = _helper_auth(db)
  assert client.get("/api/keys/gmail/oauth2", headers=helper).status_code == 200
  for action in ("exchange", "disconnect"):
    answer = client.post(f"/api/keys/gmail/oauth2/{action}", headers=helper, json=SIGN_IN)
    assert answer.status_code == 403 and "Delegated helpers" in answer.text
  assert _Provider.token_requests == []


def test_an_agent_signing_a_key_in_tells_the_owner(client, auth, db, routed, pushes):
  _key(routed, signed_in=False)
  owner = db.query(models.Owner).first()
  agent = {"Authorization": f"Bearer {auth_mod.create_agent_token('chat-keys', owner.username, owner.token_epoch)}"}
  assert client.post("/api/keys/gmail/oauth2/exchange", headers=agent, json=SIGN_IN).status_code == 200
  assert [p["body"] for p in pushes] == ["An agent signed it in for “gmail”. Review it in Settings › Keys."]


def test_a_refusal_with_a_code_keeps_it_through_the_server(client, auth, db, routed):
  _key(routed)
  _Provider.refuse = "invalid_grant"
  used = client.get("/api/keys/use/gmail/gmail.googleapis.com/gmail/v1/x", headers=auth)
  assert used.status_code == 409 and used.json()["code"] == "reauth_required"
  out = client.post("/api/keys/gmail/oauth2/disconnect", headers=auth)
  assert out.status_code == 200
  again = client.post("/api/keys/gmail/oauth2/exchange", headers=auth, json={**SIGN_IN, "code": "a b"})
  assert again.status_code == 400


def test_an_app_asks_for_a_sign_in_key_with_only_its_client(client, auth, db, routed):
  mail = _app(db)
  asked = client.post("/api/keys/requests", headers=_app_auth(db, mail), json={
    "key": "gmail", "reason": "Read your mail", "sites": [SITE], "inject": RECIPE, "parts": CLIENT_PARTS,
  })
  assert asked.status_code == 200 and asked.json()["status"] == "pending", asked.text
  [request] = client.get("/api/keys", headers=auth).json()["requests"]
  assert [part["name"] for part in request["parts"]] == ["client_id", "client_secret"]
  approved = client.post(f"/api/keys/requests/{request['id']}/approve", headers=auth, json={
    "ticket": request["ticket"], "sites": [SITE], "inject": RECIPE,
    "parts": {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET},
  })
  assert approved.status_code == 200, approved.text
  assert client.get("/api/keys/gmail/oauth2", headers=_app_auth(db, mail)).json()["signed_in"] is False
