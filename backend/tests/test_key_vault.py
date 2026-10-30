"""Key vault contract: apps use keys by name and never get a value back.

The owner and agents may reveal a key; apps only ever borrow one through the
vault. The vault program is standalone (it imports nothing from the server),
so it is loaded from its file rather than imported from app.
"""

from __future__ import annotations

import contextlib
import http.client
import importlib.util
import json
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest


VAULT_PATH = Path(__file__).parents[1] / "vault" / "keyvault.py"
SPEC = importlib.util.spec_from_file_location("mobius_key_vault", VAULT_PATH)
kv = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = kv
SPEC.loader.exec_module(kv)

DUMMY = "dummy-key-0123456789/abc+def"
HEADER_SLOT = {"type": "header", "name": "Authorization", "prefix": "Bearer "}
WEATHER = 7
ECHO_SITE = {"host": "echo.example.com", "methods": ["GET", "POST"], "paths": ["/anything/*"]}


def _grants(**overrides):
  return {"apps": [WEATHER], "agents": True, "sites": [ECHO_SITE], **overrides}


@pytest.fixture
def vault(tmp_path):
  v = kv.Vault(tmp_path / "vault")
  v.put_key("echo", {"value": DUMMY, "inject": HEADER_SLOT, "grants": _grants()})
  return v


def test_value_is_encrypted_at_rest_and_reloads(tmp_path, vault):
  for path in (vault.store_path, vault.usage_path):
    assert DUMMY.encode() not in path.read_bytes()
  assert stat.S_IMODE(vault.dir.stat().st_mode) == 0o700
  assert stat.S_IMODE(vault.store_path.stat().st_mode) == 0o600
  reloaded = kv.Vault(tmp_path / "vault")
  _, value = reloaded.authorize("echo", ("agent", ""), "GET", "echo.example.com", "/anything/x")
  assert value == DUMMY


def test_vault_reasserts_folder_modes_at_start(tmp_path, vault):
  vault.dir.chmod(0o755)
  vault.store_path.chmod(0o644)
  kv.Vault(vault.dir)
  assert stat.S_IMODE(vault.dir.stat().st_mode) == 0o700
  assert stat.S_IMODE(vault.store_path.stat().st_mode) == 0o600


def test_listing_never_carries_value_or_ciphertext(vault):
  [listed] = vault.list_keys()
  assert set(listed) >= {"name", "inject", "grants", "uses", "last_used"}
  assert not {"value", "nonce", "sealed"} & set(listed)
  assert DUMMY not in json.dumps(listed)


def test_new_key_without_sites_is_denied_everywhere(vault):
  vault.put_key("bare", {"value": DUMMY, "inject": HEADER_SLOT, "grants": {"agents": True}})
  with pytest.raises(kv.VaultError, match="not an allowed site"):
    vault.authorize("bare", ("agent", ""), "GET", "echo.example.com", "/anything/x")


def test_callers_need_their_own_grant(vault):
  with pytest.raises(kv.VaultError, match="may not use"):
    vault.authorize("echo", ("app", "8"), "GET", "echo.example.com", "/anything/x")
  vault.authorize("echo", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")
  vault.put_key("echo", {"inject": HEADER_SLOT, "grants": _grants(agents=False)})
  with pytest.raises(kv.VaultError, match="may not use"):
    vault.authorize("echo", ("agent", "chat-1"), "GET", "echo.example.com", "/anything/x")
  # The owner may always use their own key, still only on its sites.
  vault.authorize("echo", ("owner", ""), "GET", "echo.example.com", "/anything/x")


def test_missing_or_malformed_caller_is_refused():
  for header in (None, "", "app", "app:weather", "app:bad slug", "guest"):
    with pytest.raises(kv.VaultError):
      kv.parse_caller(header)
  assert kv.parse_caller("agent:abc-123") == ("agent", "abc-123")
  assert kv.parse_caller("app:7") == ("app", "7")
  assert kv.parse_caller("owner") == ("owner", "")


def test_apps_are_granted_by_numeric_id_only(tmp_path):
  v = kv.Vault(tmp_path / "vault")
  for apps in (["weather"], [True], [0]):
    with pytest.raises(kv.VaultError, match="app id"):
      v.put_key("k", {"value": DUMMY, "inject": HEADER_SLOT, "grants": {"apps": apps}})


def test_owner_and_agents_may_reveal_but_apps_never(vault):
  assert vault.reveal("echo", ("agent", "chat-1")) == DUMMY
  assert vault.reveal("echo", ("owner", "")) == DUMMY
  with pytest.raises(kv.VaultError, match="Apps cannot read"):
    vault.reveal("echo", ("app", str(WEATHER)))
  outcomes = [(e["caller"], e["outcome"]) for e in vault.usage(key="echo")[:3]]
  assert outcomes == [("app:7", "denied"), ("owner", "revealed"), ("agent:chat-1", "revealed")]
  assert DUMMY not in vault.usage_path.read_text()


# -- the handshake: an app asks once, the owner answers ------------------------

ASK = {"key": "maps", "reason": "Show traffic", "app_name": "Commute",
       "sites": [{"host": "maps.example.com", "methods": ["GET"], "paths": ["/v1/*"]}],
       "inject": {"type": "query", "name": "key"}}


def test_asking_is_idempotent_and_reports_creation_once(vault):
  first = vault.request_access(9, ASK)
  assert first["status"] == "pending" and first.get("created") is True
  again = vault.request_access(9, ASK)
  assert again["status"] == "pending" and "created" not in again
  [listed] = vault.list_requests()
  assert listed["app"] == 9 and listed["key_exists"] is False and listed["reason"] == "Show traffic"


def _ticket(vault, request_id):
  return next(r["ticket"] for r in vault.list_requests() if r["id"] == request_id)


def test_approving_a_new_key_creates_it_and_grants_only_that_app(vault):
  request = vault.request_access(9, ASK)
  ticket = _ticket(vault, request["id"])
  with pytest.raises(kv.VaultError, match="needs a value"):
    vault.approve_request(request["id"], {"ticket": ticket})
  vault.approve_request(request["id"], {"value": DUMMY, "ticket": ticket})
  assert vault.list_requests() == []
  assert vault.request_access(9, ASK)["status"] == "granted"
  inject, value = vault.authorize("maps", ("app", "9"), "GET", "maps.example.com", "/v1/route")
  assert inject == ASK["inject"] and value == DUMMY
  with pytest.raises(kv.VaultError, match="may not use"):
    vault.authorize("maps", ("app", "10"), "GET", "maps.example.com", "/v1/route")


def test_approving_an_existing_key_adds_only_the_app(vault):
  """The key's sites are every grantee's: approving one app never widens them."""
  asked = {**ASK, "key": "echo", "sites": [
    {"host": "echo.example.com", "methods": ["GET", "DELETE"], "paths": ["/anything/x*", "/other"]},
    ASK["sites"][0],
  ]}
  request = vault.request_access(9, asked)
  [card] = vault.list_requests()
  outside = [
    {"host": "echo.example.com", "methods": ["DELETE"], "paths": ["/anything/x*", "/other"]},
    {"host": "echo.example.com", "methods": ["GET"], "paths": ["/other"]},
    {"host": "maps.example.com", "methods": ["GET"], "paths": ["/v1/*"]},
  ]
  assert card["outside_sites"] == outside
  ticket = _ticket(vault, request["id"])
  for change in ({"sites": asked["sites"]}, {"value": "other"}, {"inject": ASK["inject"]}):
    with pytest.raises(kv.VaultError, match="already exists"):
      vault.approve_request(request["id"], {**change, "ticket": ticket})
  assert vault.approve_request(request["id"], {"ticket": ticket})["outside"] == outside
  [record] = [k for k in vault.list_keys() if k["name"] == "echo"]
  assert record["grants"] == _grants(apps=[WEATHER, 9])
  assert record["inject"] == HEADER_SLOT  # the app cannot change how the key is sent
  assert vault.reveal("echo", ("owner", "")) == DUMMY
  vault.authorize("echo", ("app", "9"), "GET", "echo.example.com", "/anything/x")
  with pytest.raises(kv.VaultError, match="not an allowed site"):
    vault.authorize("echo", ("app", "9"), "GET", "maps.example.com", "/v1/route")


def test_a_narrower_path_is_within_a_wider_one_only_when_every_match_is():
  within = kv._pattern_within
  assert within("/v1/*", "/v1/*") and within("/v1/a*", "/v1/*") and within("/v1/x", "/*")
  assert not within("/v1/*", "/v1/a*") and not within("/v2/x", "/v1/*")
  assert not within("/v1/*", "/v1/?")   # a ? matches one character, not any text


def test_an_approval_answers_only_the_request_it_was_shown(vault):
  shown = vault.request_access(9, ASK)
  ticket = _ticket(vault, shown["id"])
  vault.forget_app(9)                       # purged; the id goes to another app
  again = vault.request_access(9, ASK)
  assert again["id"] == shown["id"]
  for body in ({"value": DUMMY, "ticket": ticket}, {"value": DUMMY}):
    with pytest.raises(kv.VaultError, match="changed since it was shown"):
      vault.approve_request(shown["id"], body)
  assert vault.request_access(9, ASK)["status"] == "pending"


def test_denied_requests_stay_denied_without_new_requests(vault):
  request = vault.request_access(9, ASK)
  vault.deny_request(request["id"])
  assert vault.request_access(9, ASK) == {"status": "denied", "key": "maps", "id": request["id"]}
  assert vault.forget_request(request["id"]) is True
  assert vault.request_access(9, ASK)["created"] is True


def test_a_grant_from_the_key_editor_settles_the_request(vault):
  vault.request_access(9, {**ASK, "key": "echo"})
  vault.put_key("echo", {"inject": HEADER_SLOT, "grants": _grants(apps=[WEATHER, 9])})
  assert vault.list_requests() == []


def test_method_and_path_must_both_match_a_site_rule(vault):
  with pytest.raises(kv.VaultError, match="DELETE is not allowed"):
    vault.authorize("echo", ("agent", ""), "DELETE", "echo.example.com", "/anything/x")
  with pytest.raises(kv.VaultError, match="not an allowed path"):
    vault.authorize("echo", ("agent", ""), "GET", "echo.example.com", "/admin")
  with pytest.raises(kv.VaultError, match="not an allowed site"):
    vault.authorize("echo", ("agent", ""), "GET", "evil.example.net", "/anything/x")


@pytest.mark.parametrize("path", [
  "/anything/../admin", "/anything/%2e%2e/admin", "/anything%2fadmin",
  "/anything/%5c..", "/anything//x", "/anything/./x",
])
def test_ambiguous_paths_are_refused_before_rule_matching(path):
  with pytest.raises(kv.VaultError):
    kv.normalized_request_path(path)


def test_wildcard_site_matches_subdomains_only():
  grants = {"sites": [{"host": "*.example.com", "methods": ["GET"], "paths": ["/*"]}]}
  assert kv.site_allows(grants, "api.example.com", "GET", "/x") is None
  assert kv.site_allows(grants, "example.com", "GET", "/x")
  assert kv.site_allows(grants, "evilexample.com", "GET", "/x")


def test_header_slot_replaces_any_caller_supplied_copy():
  request = kv.build_upstream_request(
    method="GET", host="echo.example.com", raw_path="/anything/x", raw_query="a=1",
    caller_headers=[
      ("authorization", "Bearer attacker"), ("X-Vault-Caller", "agent"),
      ("Host", "elsewhere.test"), ("Accept-Encoding", "gzip"), ("X-Trace", "1"),
    ],
    inject=HEADER_SLOT, value=DUMMY,
  )
  headers = [(k.lower(), v) for k, v in request.headers]
  assert [v for k, v in headers if k == "authorization"] == [f"Bearer {DUMMY}"]
  assert ("host", "echo.example.com") in headers
  assert ("accept-encoding", "identity") in headers
  assert ("x-trace", "1") in headers
  assert not any(k == "x-vault-caller" for k, _ in headers)
  assert request.target == "/anything/x?a=1"


def test_query_slot_replaces_any_caller_supplied_parameter():
  request = kv.build_upstream_request(
    method="GET", host="echo.example.com", raw_path="/anything/x",
    raw_query="api_key=attacker&q=1", caller_headers=[],
    inject={"type": "query", "name": "api_key"}, value=DUMMY,
  )
  query = request.target.split("?", 1)[1]
  assert "attacker" not in query
  assert query.count("api_key=") == 1


@pytest.mark.parametrize("answers", [
  ["127.0.0.1"], ["10.0.0.5"], ["169.254.169.254"], ["8.8.8.8", "192.168.1.1"], ["::1"],
  ["64:ff9b::7f00:1"], ["64:ff9b::a9fe:a9fe"],   # NAT64 of loopback and link-local
])
def test_destinations_resolving_to_non_public_addresses_are_refused(monkeypatch, answers):
  monkeypatch.setattr(kv.socket, "getaddrinfo", lambda *a, **k: [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in answers
  ])
  with pytest.raises(kv.VaultError, match="non-public"):
    kv.resolve_public_address("echo.example.com")


def test_public_destination_is_pinned_to_the_resolved_address(monkeypatch):
  monkeypatch.setattr(kv.socket, "getaddrinfo", lambda *a, **k: [
    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.215.14", 443)),
  ])
  connection = kv.open_pinned_connection("echo.example.com", 5)
  assert connection._pinned_address == "93.184.215.14"
  assert connection.host == "echo.example.com"
  # A NAT64 address of a public IPv4 host is pinned as given.
  monkeypatch.setattr(kv.socket, "getaddrinfo", lambda *a, **k: [
    (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("64:ff9b::5db8:d70e", 443, 0, 0)),
  ])
  assert kv.resolve_public_address("echo.example.com") == "64:ff9b::5db8:d70e"


def test_redaction_covers_common_echo_encodings():
  forms = kv.secret_forms(DUMMY)
  import base64, urllib.parse
  for encoded in (
    DUMMY, urllib.parse.quote(DUMMY, safe=""), json.dumps(DUMMY)[1:-1],
    base64.b64encode(DUMMY.encode()).decode(),
  ):
    assert encoded.encode() not in kv.redact(f"x{encoded}y".encode(), forms)


@pytest.mark.parametrize("body, message", [
  ({"value": "   ", "inject": HEADER_SLOT}, "can't be empty"),
  ({"value": DUMMY, "inject": {"type": "header", "name": "Host"}}, "can't carry"),
  ({"value": DUMMY, "inject": {"type": "cookie", "name": "k"}}, "header or a query"),
  ({"value": DUMMY, "inject": HEADER_SLOT,
    "grants": {"sites": [{"host": "x.example.com", "methods": [], "paths": ["/*"]}]}}, "method"),
  ({"value": DUMMY, "inject": HEADER_SLOT,
    "grants": {"sites": [{"host": "localhost", "methods": ["GET"], "paths": ["/*"]}]}}, "not a site"),
])
def test_invalid_key_records_are_rejected(tmp_path, body, message):
  v = kv.Vault(tmp_path / "vault")
  with pytest.raises(kv.VaultError, match=message):
    v.put_key("bad", body)


def test_revoke_removes_the_key_and_logs_without_value(vault):
  assert vault.revoke_key("echo") is True
  assert vault.list_keys() == []
  with pytest.raises(kv.VaultError, match="No key"):
    vault.authorize("echo", ("agent", ""), "GET", "echo.example.com", "/anything/x")
  assert vault.usage()[0]["outcome"] == "revoked"


# -- the use port end to end, with a local upstream in place of HTTPS ---------

class _Echo(BaseHTTPRequestHandler):
  hits: list = []

  def log_message(self, *args):
    pass

  def do_GET(self):
    type(self).hits.append(self.path)
    if self.path.startswith("/anything/redirect"):
      self.send_response(302)
      self.send_header("Location", f"https://elsewhere.test/?k={self.headers['Authorization']}")
      self.send_header("Content-Length", "0")
      self.end_headers()
      return
    body = json.dumps({"path": self.path, "headers": dict(self.headers)}).encode()
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)


@contextlib.contextmanager
def _vault_socket(vault, connect):
  # Unix socket paths are limited to ~108 bytes; pytest's tmp_path can exceed it.
  short_dir = Path(tempfile.mkdtemp(prefix="kv-"))
  socket_path = short_dir / kv.SOCKET_NAME
  handler = type("TestVault", (kv.VaultHandler,), {"vault": vault, "connect": staticmethod(connect)})
  server = kv.VaultServer(str(socket_path), handler)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  try:
    with httpx.Client(
      transport=httpx.HTTPTransport(uds=str(socket_path)), base_url="http://vault",
    ) as client:
      yield client
  finally:
    server.shutdown()
    server.server_close()
    shutil.rmtree(short_dir, ignore_errors=True)


@pytest.fixture
def client(vault):
  _Echo.hits = []
  upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
  threading.Thread(target=upstream.serve_forever, daemon=True).start()
  local = lambda host, timeout: http.client.HTTPConnection(  # noqa: E731
    "127.0.0.1", upstream.server_address[1], timeout=timeout,
  )
  with _vault_socket(vault, local) as client:
    yield client
  upstream.shutdown()


def test_use_injects_the_key_and_redacts_the_echo(vault, client):
  response = client.get(
    "/use/echo/echo.example.com/anything/hello",
    headers={"X-Vault-Caller": "app:7"},
  )
  assert response.status_code == 200
  assert DUMMY not in response.text
  echoed = response.json()["headers"]
  assert echoed["Authorization"] == "Bearer [redacted by key vault]"
  assert "X-Vault-Caller" not in echoed
  assert response.headers["X-Vault-Key"] == "echo"
  entry = vault.usage(key="echo")[0]
  assert entry["outcome"] == "used" and entry["caller"] == "app:7"
  assert DUMMY not in vault.usage_path.read_text()


def test_denied_use_never_reaches_upstream(vault, client):
  response = client.get(
    "/use/echo/echo.example.com/admin", headers={"X-Vault-Caller": "agent"},
  )
  assert response.status_code == 403
  assert response.headers["X-Vault-Denied"] == "1"
  assert _Echo.hits == []
  assert vault.usage(key="echo")[0]["outcome"] == "denied"


def test_redirects_are_returned_not_followed(client):
  response = client.get(
    "/use/echo/echo.example.com/anything/redirect", headers={"X-Vault-Caller": "agent"},
  )
  assert response.status_code == 302
  assert DUMMY not in response.headers["Location"]
  assert _Echo.hits == ["/anything/redirect"]


def test_owner_operations_and_handshake_over_the_socket(client):
  added = client.put("/keys/second", json={
    "value": "another-dummy-key", "inject": {"type": "query", "name": "key"},
  })
  assert added.json() == {"name": "second", "created": True}
  listed = client.get("/keys").text
  assert "second" in listed and DUMMY not in listed and "another-dummy-key" not in listed
  assert client.put("/keys/x", json={"inject": HEADER_SLOT}).status_code == 400
  revealed = client.get("/keys/second/value", headers={"X-Vault-Caller": "agent:c1"})
  assert revealed.json() == {"name": "second", "value": "another-dummy-key"}
  assert client.get("/keys/second/value", headers={"X-Vault-Caller": "app:7"}).status_code == 403
  assert client.get("/keys/second/value").status_code == 403
  # Only an attested app may ask.
  assert client.post("/requests", json=ASK, headers={"X-Vault-Caller": "agent:c1"}).status_code == 403
  asked = client.post("/requests", json=ASK, headers={"X-Vault-Caller": "app:9"}).json()
  assert asked["status"] == "pending"
  assert [r["id"] for r in client.get("/requests").json()["requests"]] == [asked["id"]]
  [listed] = client.get("/requests").json()["requests"]
  approved = client.post(f"/requests/{asked['id']}/approve",
                         json={"value": "maps-dummy-key", "ticket": listed["ticket"]})
  assert approved.json()["granted"] == 9
  assert client.delete("/keys/second").status_code == 200
  assert client.delete("/keys/second").status_code == 404


def test_non_public_destination_is_logged_as_a_refusal(vault):
  def refuse(host, timeout):
    raise kv.VaultError(403, f"{host} resolves to a non-public address")
  with _vault_socket(vault, refuse) as client:
    response = client.get(
      "/use/echo/echo.example.com/anything/x", headers={"X-Vault-Caller": "agent"},
    )
  assert response.status_code == 403 and response.json()["error"] == "denied"
  assert vault.usage(key="echo")[0]["outcome"] == "denied"


def test_a_purged_app_loses_every_grant_and_request(vault):
  vault.request_access(WEATHER, ASK)
  vault.request_access(9, ASK)
  assert vault.forget_app(WEATHER) == {"keys": ["echo"], "deleted": [], "requests": 1}
  [record] = vault.list_keys()
  assert record["grants"]["apps"] == []
  assert [r["app"] for r in vault.list_requests()] == [9]
  with pytest.raises(kv.VaultError, match="may not use"):
    vault.authorize("echo", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")


def test_decoding_is_bounded_and_strips_the_encoding(monkeypatch):
  import gzip as _gzip
  headers, body = kv.decoded_body([("Content-Encoding", "gzip"), ("X-A", "1")], _gzip.compress(b"hello"))
  assert body == b"hello" and headers == [("X-A", "1")]
  assert kv.decoded_body([("Content-Encoding", "identity")], b"x") == ([], b"x")
  monkeypatch.setattr(kv, "MAX_RESPONSE_BYTES", 1000)
  with pytest.raises(kv.VaultError, match="too large"):
    kv.decoded_body([("Content-Encoding", "gzip")], _gzip.compress(b"a" * 5000))
  with pytest.raises(kv.VaultError, match="unsupported"):
    kv.decoded_body([("Content-Encoding", "gzip, br")], b"")
  with pytest.raises(kv.VaultError, match="could not be decompressed"):
    kv.decoded_body([("Content-Encoding", "gzip")], b"not gzip")


def test_create_only_refuses_an_occupied_name_and_an_update_never_moves_origin(vault):
  with pytest.raises(kv.VaultError, match="already exists"):
    vault.put_key("echo", {"value": "other", "inject": HEADER_SLOT, "create_only": True})
  assert vault.reveal("echo", ("owner", "")) == DUMMY
  # An app's own key is granted to it, as save_owned creates it.
  own = {"apps": [7], "agents": True, "sites": []}
  vault.put_key("mine", {"value": DUMMY, "inject": HEADER_SLOT, "grants": own, "create_only": True,
                         "origin": {"app": 7, "name": "api.key"}})
  # Changing how the key is sent keeps it the app's own.
  vault.put_key("mine", {"inject": HEADER_SLOT, "grants": own, "origin": {"app": 8, "name": "stolen"}})
  assert {k["name"]: k["origin"] for k in vault.list_keys()}["mine"] == {"app": 7, "name": "api.key"}
  # A value written under another origin never hands the key to that app.
  vault.put_key("mine", {"value": "next", "inject": HEADER_SLOT, "grants": own,
                         "origin": {"app": 8, "name": "stolen"}})
  assert {k["name"]: k["origin"] for k in vault.list_keys()}["mine"] is None


def test_an_owner_value_ends_the_apps_ownership_of_the_key(vault):
  saved = vault.save_owned(9, "OPENAI_API_KEY", "app-dummy")
  assert saved == {"name": "OPENAI_API_KEY", "created": True}
  [record] = [k for k in vault.list_keys() if k["name"] == "OPENAI_API_KEY"]
  # The owner (or an agent's store-key.py) fills in the real value, keeping
  # the key's current grants as the Settings page and store-key.py do.
  vault.put_key("OPENAI_API_KEY", {"value": "owners-real-key", "inject": record["inject"],
                                   "grants": record["grants"]})
  [record] = [k for k in vault.list_keys() if k["name"] == "OPENAI_API_KEY"]
  assert record["origin"] is None and record["grants"]["apps"] == []
  # The app can neither delete nor replace it; its next save is a key of its own.
  assert vault.release_owned(9, "OPENAI_API_KEY") is None
  assert vault.save_owned(9, "OPENAI_API_KEY", "app-chosen") == {
    "name": "app9-OPENAI_API_KEY", "created": True,
  }
  assert vault.reveal("OPENAI_API_KEY", ("owner", "")) == "owners-real-key"
  assert vault.erase_owned(9) == ["app9-OPENAI_API_KEY"]
  assert vault.reveal("OPENAI_API_KEY", ("owner", "")) == "owners-real-key"


def test_the_apps_own_save_keeps_its_ownership(vault):
  vault.save_owned(9, "api.key", "first")
  assert vault.save_owned(9, "api.key", "second") == {"name": "api-key", "created": False}
  assert {k["name"]: k["origin"] for k in vault.list_keys()}["api-key"] == {"app": 9, "name": "api.key"}


def test_save_owned_finds_or_creates_by_origin_under_one_lock(vault):
  results = []
  threads = [
    threading.Thread(target=lambda n=n: results.append(vault.save_owned(9, "api.key", f"v{n}")))
    for n in range(12)
  ]
  for thread in threads:
    thread.start()
  for thread in threads:
    thread.join()
  assert {r["name"] for r in results} == {"api-key"}
  assert sum(r["created"] for r in results) == 1
  owned = [k for k in vault.list_keys() if k["origin"] == {"app": 9, "name": "api.key"}]
  assert [k["name"] for k in owned] == ["api-key"]


def test_releasing_an_owned_key_keeps_the_owners_current_sharing(vault):
  saved = vault.save_owned(9, "api.key", DUMMY)
  [record] = [k for k in vault.list_keys() if k["name"] == saved["name"]]
  # The owner shares the key and changes how it is sent after the app saved it.
  vault.put_key(saved["name"], {"inject": HEADER_SLOT,
                                "grants": {**record["grants"], "apps": [9, 11]}})
  assert vault.release_owned(9, "api.key") == {"name": saved["name"], "deleted": False}
  [kept] = [k for k in vault.list_keys() if k["name"] == saved["name"]]
  assert kept["grants"]["apps"] == [11] and kept["inject"] == HEADER_SLOT
  assert vault.reveal(saved["name"], ("owner", "")) == DUMMY
  # It is no longer the app's own key, so a second release finds nothing.
  assert vault.release_owned(9, "api.key") is None


def test_releasing_an_unshared_owned_key_deletes_it_and_never_touches_borrowed_keys(vault):
  saved = vault.save_owned(9, "api.key", DUMMY)
  assert vault.release_owned(9, "api.key") == {"name": saved["name"], "deleted": True}
  assert saved["name"] not in {k["name"] for k in vault.list_keys()}
  # "echo" is granted to WEATHER but was never created by it.
  assert vault.release_owned(WEATHER, "echo") is None
  assert "echo" in {k["name"] for k in vault.list_keys()}


def test_erasing_an_apps_data_erases_only_its_own_keys(vault):
  alone = vault.save_owned(WEATHER, "token", DUMMY)["name"]
  shared = vault.save_owned(WEATHER, "shared", DUMMY)["name"]
  [record] = [k for k in vault.list_keys() if k["name"] == shared]
  vault.put_key(shared, {"inject": HEADER_SLOT, "grants": {**record["grants"], "apps": [WEATHER, 9]}})
  assert vault.erase_owned(WEATHER) == [alone]
  keys = {k["name"]: k for k in vault.list_keys()}
  assert alone not in keys
  assert keys[shared]["grants"]["apps"] == [9]
  # "echo" is only lent to WEATHER: the owner's grant is not app data.
  assert keys["echo"]["grants"]["apps"] == [WEATHER]


def test_a_purge_erases_the_apps_own_keys(vault):
  own = vault.save_owned(WEATHER, "token", DUMMY)["name"]
  assert vault.forget_app(WEATHER) == {"keys": ["echo"], "deleted": [own], "requests": 0}
  assert [k["name"] for k in vault.list_keys()] == ["echo"]


def _origin(vault, name):
  return next(k for k in vault.list_keys() if k["name"] == name).get("origin")


def test_a_purged_creator_leaves_no_ownership_for_an_app_that_reuses_its_id(vault):
  # App 5 saves its own key; the owner shares it with Weather on a site.
  name = vault.save_owned(5, "api_key", DUMMY)["name"]
  vault.put_key(name, {"inject": HEADER_SLOT,
                       "grants": {"apps": [5, WEATHER], "agents": True, "sites": [ECHO_SITE]}})
  assert _origin(vault, name) == {"app": 5, "name": "api_key"}
  vault.forget_app(5)
  assert _origin(vault, name) is None
  # A new app that reuses id 5 asks, and the owner approves it as usual.
  asked = vault.request_access(5, {**ASK, "key": name, "sites": [ECHO_SITE]})
  vault.approve_request(asked["id"], {"ticket": _ticket(vault, asked["id"])})
  assert 5 in next(k for k in vault.list_keys() if k["name"] == name)["grants"]["apps"]
  # It borrows the key; it does not own it, so saving its own secret makes a new key.
  assert _origin(vault, name) is None
  assert vault.save_owned(5, "api_key", "other-dummy-key")["name"] != name
  _, value = vault.authorize(name, ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")
  assert value == DUMMY


def test_an_origin_lasts_only_while_its_app_holds_a_grant(vault):
  # Released while shared: the key stays for the other app, without an owner.
  shared = vault.save_owned(5, "token", DUMMY)["name"]
  vault.put_key(shared, {"inject": HEADER_SLOT, "grants": {"apps": [5, WEATHER], "agents": True, "sites": []}})
  vault.release_owned(5, "token")
  assert _origin(vault, shared) is None
  # The owner removing the creating app from the grants also ends its ownership.
  kept = vault.save_owned(6, "token", DUMMY)["name"]
  vault.put_key(kept, {"inject": HEADER_SLOT, "grants": {"apps": [WEATHER], "agents": True, "sites": []}})
  assert _origin(vault, kept) is None


def test_only_one_vault_runs_on_a_folder(tmp_path):
  fd = kv.hold_single_instance_lock(tmp_path / "store")
  try:
    with pytest.raises(SystemExit, match="another vault"):
      kv.hold_single_instance_lock(tmp_path / "store")
  finally:
    os.close(fd)
  os.close(kv.hold_single_instance_lock(tmp_path / "store"))


STORE_KEY = Path(__file__).parents[1] / "scripts" / "store-key.py"


def _run_store_key(monkeypatch, vault, args, value):
  import io
  spec = importlib.util.spec_from_file_location("mobius_store_key", STORE_KEY)
  store_key = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(store_key)
  short_dir = Path(tempfile.mkdtemp(prefix="kvs-"))
  handler = type("StoreKeyVault", (kv.VaultHandler,), {"vault": vault})
  server = kv.VaultServer(str(short_dir / kv.SOCKET_NAME), handler)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  try:
    monkeypatch.setattr(store_key, "VAULT_SOCKET", short_dir / kv.SOCKET_NAME)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"value": value})))
    return store_key.main(args)
  finally:
    server.shutdown()
    server.server_close()
    shutil.rmtree(short_dir, ignore_errors=True)


def test_a_name_an_app_took_first_never_lends_it_the_owners_value(monkeypatch, vault):
  """An app saves a dummy under the name the owner will use; when an agent
  later stores the owner's real key there (store-key.py keeps the existing
  grants), the app must not be able to borrow the owner's value."""
  vault.save_owned(9, "OPENAI_API_KEY", "app-dummy")
  site = "api.openai.com:POST:/v1/*"
  assert _run_store_key(monkeypatch, vault, ["OPENAI_API_KEY", "--site", site], "owners-real-key") == 0
  with pytest.raises(kv.VaultError, match="may not use"):
    vault.authorize("OPENAI_API_KEY", ("app", "9"), "POST", "api.openai.com", "/v1/responses")
  _, value = vault.authorize("OPENAI_API_KEY", ("agent", "c1"), "POST", "api.openai.com", "/v1/responses")
  assert value == "owners-real-key"


def test_an_app_holds_at_most_sixteen_keys_of_its_own(vault):
  for n in range(kv.MAX_OWNED_PER_APP):
    vault.save_owned(9, f"secret{n}", DUMMY)
  with pytest.raises(kv.VaultError, match="at most 16") as refused:
    vault.save_owned(9, "one-more", DUMMY)
  assert refused.value.status == 413
  assert vault.save_owned(9, "secret0", "replaced")["created"] is False
  assert vault.save_owned(10, "secret0", DUMMY)["created"] is True


def test_an_edit_from_a_stale_copy_is_refused(vault):
  [echo] = vault.list_keys()
  stale = {"inject": HEADER_SLOT, "grants": echo["grants"], "revision": echo["revision"]}
  # The app is purged while the owner's page still shows it.
  vault.forget_app(WEATHER)
  with pytest.raises(kv.VaultError, match="changed since") as refused:
    vault.put_key("echo", stale)
  assert refused.value.status == 409
  assert vault.list_keys()[0]["grants"]["apps"] == []
  [echo] = vault.list_keys()
  vault.put_key("echo", {"inject": HEADER_SLOT, "grants": {**echo["grants"], "agents": False},
                         "revision": echo["revision"]})
  # A key deleted meanwhile is not recreated from the stale page either.
  [echo] = vault.list_keys()
  vault.revoke_key("echo")
  with pytest.raises(kv.VaultError, match="changed since"):
    vault.put_key("echo", {"value": "again", "inject": HEADER_SLOT, "revision": echo["revision"]})


def test_values_with_line_breaks_are_kept_but_never_sent_in_a_header(vault):
  pem = "-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBg\n-----END PRIVATE KEY-----"
  vault.put_key("pem", {"value": pem, "inject": HEADER_SLOT, "grants": _grants()})
  assert vault.reveal("pem", ("agent", "c1")) == pem
  with pytest.raises(kv.VaultError, match="line breaks") as refused:
    vault.authorize("pem", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")
  assert refused.value.status == 400
  vault.put_key("pem", {"inject": {"type": "query", "name": "key"}, "grants": _grants()})
  assert vault.authorize("pem", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")[1] == pem


def test_usage_counts_cover_the_whole_log(vault):
  for _ in range(600):
    vault.log_usage("echo", "agent", "GET", "echo.example.com", "/anything/x", "used")
  assert vault.list_keys()[0]["uses"] == 600
