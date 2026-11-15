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


def _apps(*ids, sites=None):
  return [{"id": app_id, "sites": [ECHO_SITE] if sites is None else sites} for app_id in ids]


def _grants(**overrides):
  if "apps" in overrides and all(isinstance(a, int) for a in overrides["apps"]):
    overrides["apps"] = _apps(*overrides["apps"])
  return {"apps": _apps(WEATHER), "agents": True, "sites": [ECHO_SITE], **overrides}


@pytest.fixture
def vault(tmp_path):
  v = kv.Vault(tmp_path / "vault")
  v.put_key("echo", {"value": DUMMY, "inject": HEADER_SLOT, "grants": _grants()})
  return v


def test_value_is_encrypted_at_rest_and_reloads(tmp_path, vault):
  for path in (vault.store_path, vault.audit_path, vault.stats_path):
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
  assert DUMMY not in vault.audit_path.read_text()


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
  # What the Settings card sends: the value plus the slot and sites it showed.
  vault.approve_request(request["id"], {"value": DUMMY, "ticket": ticket,
                                        "inject": ASK["inject"], "sites": ASK["sites"]})
  assert vault.list_requests() == []
  assert vault.request_access(9, ASK)["status"] == "granted"
  inject, value = vault.authorize("maps", ("app", "9"), "GET", "maps.example.com", "/v1/route")
  assert inject == ASK["inject"] and value == DUMMY
  with pytest.raises(kv.VaultError, match="may not use"):
    vault.authorize("maps", ("app", "10"), "GET", "maps.example.com", "/v1/route")


def test_approval_applies_only_what_the_owners_answer_carries(vault):
  # The app proposed a query slot and its own sites; the answer repeats neither.
  request = vault.request_access(9, ASK)
  vault.approve_request(request["id"], {"value": DUMMY, "ticket": _ticket(vault, request["id"])})
  [record] = [k for k in vault.list_keys() if k["name"] == "maps"]
  assert record["inject"] == kv.DEFAULT_INJECT
  assert record["grants"]["apps"] == [{"id": 9, "sites": []}] and record["grants"]["sites"] == []
  with pytest.raises(kv.VaultError, match="not an allowed site"):
    vault.authorize("maps", ("app", "9"), "GET", "maps.example.com", "/v1/route")


@pytest.mark.parametrize("header", ["User-Agent", "referer", "Origin", "Content-Type", "X-Forwarded-For"])
def test_a_key_is_never_sent_in_a_header_sites_log_as_metadata(header):
  with pytest.raises(kv.VaultError, match="can't carry a key"):
    kv.validate_inject({"type": "header", "name": header})


def test_approving_sites_for_one_app_never_widens_another_app_or_agents(vault):
  request = vault.request_access(9, {**ASK, "key": "echo"})
  ticket = _ticket(vault, request["id"])
  for change in ({"value": "other"}, {"inject": ASK["inject"]}):
    with pytest.raises(kv.VaultError, match="already exists"):
      vault.approve_request(request["id"], {**change, "ticket": ticket})
  vault.approve_request(request["id"], {"sites": ASK["sites"], "ticket": ticket})
  [record] = [k for k in vault.list_keys() if k["name"] == "echo"]
  assert record["grants"]["apps"] == [*_apps(WEATHER), {"id": 9, "sites": ASK["sites"]}]
  assert record["grants"]["sites"] == [ECHO_SITE]
  assert record["inject"] == HEADER_SLOT  # the app cannot change how the key is sent
  vault.authorize("echo", ("app", "9"), "GET", "maps.example.com", "/v1/route")
  for caller in (("app", str(WEATHER)), ("agent", "c1"), ("owner", "")):
    with pytest.raises(kv.VaultError, match="not an allowed site"):
      vault.authorize("echo", caller, "GET", "maps.example.com", "/v1/route")
  with pytest.raises(kv.VaultError, match="not an allowed site"):
    vault.authorize("echo", ("app", "9"), "GET", "echo.example.com", "/anything/x")


def test_a_version_one_store_keeps_each_apps_reach_when_migrated(tmp_path):
  folder = tmp_path / "old"
  v = kv.Vault(folder)
  v.put_key("echo", {"value": DUMMY, "inject": HEADER_SLOT, "grants": _grants()})
  payload = json.loads(v.store_path.read_text())
  payload["keys"]["echo"]["grants"]["apps"] = [WEATHER]  # how version 1 stored a grant
  payload["version"] = 1
  v.store_path.write_text(json.dumps(payload))
  migrated = kv.Vault(folder)
  assert json.loads(migrated.store_path.read_text())["version"] == kv.STORE_VERSION
  [listed] = migrated.list_keys()
  assert listed["grants"]["apps"] == [{"id": WEATHER, "sites": [ECHO_SITE]}]
  assert migrated.authorize("echo", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")[1] == DUMMY


def test_a_granted_app_can_ask_for_more_sites_for_itself_only(vault):
  extra = {"host": "tiles.example.com", "methods": ["GET"], "paths": ["/*"]}
  ask = {"key": "echo", "reason": "Map tiles", "sites": [ECHO_SITE, extra]}
  answer = vault.request_access(WEATHER, ask)
  assert answer["status"] == "granted" and answer["sites_request"] == "pending" and answer["created"] is True
  assert answer["sites_not_allowed"] == [extra]
  again = vault.request_access(WEATHER, ask)
  assert again["sites_request"] == "pending" and "created" not in again
  # Meanwhile the app keeps the sites it already had.
  vault.authorize("echo", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")
  [request] = vault.list_requests()
  assert request["granted"] is True and request["sites"] == [extra]
  vault.approve_request(request["id"], {"sites": [extra], "ticket": request["ticket"]})
  vault.authorize("echo", ("app", str(WEATHER)), "GET", "tiles.example.com", "/a.png")
  with pytest.raises(kv.VaultError, match="not an allowed site"):
    vault.authorize("echo", ("agent", "c1"), "GET", "tiles.example.com", "/a.png")
  assert vault.request_access(WEATHER, ask) == {"status": "granted", "key": "echo"}


def test_one_app_cannot_crowd_out_other_apps_requests(vault):
  for index in range(kv.MAX_REQUESTS_PER_APP):
    vault.request_access(9, {**ASK, "key": f"k{index}"})
  with pytest.raises(kv.VaultError, match="This app has too many"):
    vault.request_access(9, {**ASK, "key": "one-more"})
  assert vault.request_access(10, ASK)["status"] == "pending"


def test_an_app_learns_nothing_about_key_names_it_was_not_given(vault):
  refusals = []
  for name in ("echo", "no-such-key"):
    with pytest.raises(kv.VaultError) as caught:
      vault.authorize(name, ("app", "99"), "GET", "echo.example.com", "/anything/x")
    refusals.append((caught.value.status, caught.value.reason))
  assert refusals[0] == refusals[1] == (403, "app:99 may not use this key")


def test_the_agents_switch_keeps_agents_from_reading_as_well_as_using(vault):
  vault.put_key("echo", {"inject": HEADER_SLOT, "grants": _grants(agents=False)})
  with pytest.raises(kv.VaultError, match="Agents are switched off"):
    vault.reveal("echo", ("agent", "chat-1"))
  with pytest.raises(kv.VaultError, match="may not use"):
    vault.authorize("echo", ("agent", "chat-1"), "GET", "echo.example.com", "/anything/x")
  assert vault.reveal("echo", ("owner", "")) == DUMMY
  assert vault.reveal("echo", ("platform", "app-7")) == DUMMY


def test_refused_traffic_cannot_push_reads_and_changes_out_of_the_log(vault, monkeypatch):
  monkeypatch.setattr(kv, "USAGE_ROTATE_BYTES", 4096)
  vault.reveal("echo", ("agent", "chat-1"))
  for _ in range(200):
    vault.log_traffic("x" * 5000, "app:9", "GET", "h" * 5000 + ".example.com", "/p" * 5000, "denied")
  outcomes = [e["outcome"] for e in vault.usage(key="echo", limit=500)]
  assert "revealed" in outcomes and "added" in outcomes
  assert max(len(line) for line in vault.usage_path.read_text().splitlines()) < 1200


def test_use_counts_survive_log_rotation_and_restart(tmp_path, vault, monkeypatch):
  monkeypatch.setattr(kv, "USAGE_ROTATE_BYTES", 2048)
  vault.put_key("quiet", {"value": DUMMY, "inject": HEADER_SLOT, "grants": _grants()})
  vault.log_traffic("quiet", "agent:c1", "GET", "echo.example.com", "/anything/x", "used", status=200)
  for _ in range(100):
    vault.log_traffic("echo", "app:7", "GET", "echo.example.com", "/anything/x", "used", status=200)
  vault.log_traffic("no-such-key", "app:9", "GET", "echo.example.com", "/x", "denied")
  reloaded = {k["name"]: k for k in kv.Vault(tmp_path / "vault").list_keys()}
  assert reloaded["quiet"]["uses"] == 1 and reloaded["quiet"]["last_used"] is not None
  assert reloaded["echo"]["uses"] == 100
  assert "no-such-key" not in json.loads((tmp_path / "vault" / "stats.json").read_text())["keys"]
  vault.revoke_key("quiet")
  assert "quiet" not in json.loads((tmp_path / "vault" / "stats.json").read_text())["keys"]


def test_a_store_from_before_counters_counts_its_remaining_log_once(tmp_path, vault):
  vault.log_traffic("echo", "app:7", "GET", "echo.example.com", "/anything/x", "used", status=200)
  (tmp_path / "vault" / "stats.json").unlink()
  [record] = kv.Vault(tmp_path / "vault").list_keys()
  assert record["uses"] == 1


def test_a_grant_from_the_key_editor_settles_the_request_once_its_sites_are_given(vault):
  vault.request_access(9, {**ASK, "key": "echo"})
  vault.put_key("echo", {"inject": HEADER_SLOT, "grants": _grants(apps=[*_apps(WEATHER), {"id": 9, "sites": []}])})
  # Granted, but not on the sites it asked for: the ask stays, as one for more sites.
  [left] = vault.list_requests()
  assert left["granted"] is True
  vault.put_key("echo", {"inject": HEADER_SLOT, "grants": _grants(apps=[*_apps(WEATHER), {"id": 9, "sites": ASK["sites"]}])})
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
  sites = [{"host": "*.example.com", "methods": ["GET"], "paths": ["/*"]}]
  assert kv.site_allows(sites, "api.example.com", "GET", "/x") is None
  assert kv.site_allows(sites, "example.com", "GET", "/x")
  assert kv.site_allows(sites, "evilexample.com", "GET", "/x")


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


def test_a_ranged_request_cannot_slice_an_echoed_key_past_redaction():
  request = kv.build_upstream_request(
    method="GET", host="echo.example.com", raw_path="/anything/x", raw_query="",
    caller_headers=[("Range", "bytes=10-13"), ("If-Range", '"etag"'), ("X-Trace", "1")],
    inject={"type": "query", "name": "api_key"}, value=DUMMY,
  )
  names = {k.lower() for k, _ in request.headers}
  assert "range" not in names and "if-range" not in names
  assert "x-trace" in names


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


class _MethodOverrideSite(BaseHTTPRequestHandler):
  """A site that, like Google's JSON APIs, runs the method an override header names."""
  ran: list = []

  def log_message(self, *args):
    pass

  def do_POST(self):
    effective = self.command
    for header in ("X-HTTP-Method-Override", "X-HTTP-Method", "X-Method-Override"):
      effective = self.headers.get(header) or effective
    type(self).ran.append((effective, self.headers.get("Authorization")))
    self.rfile.read(int(self.headers.get("Content-Length") or 0))
    self.send_response(200)
    self.send_header("Content-Length", "0")
    self.end_headers()


@pytest.mark.parametrize("override", ["X-HTTP-Method-Override", "X-HTTP-Method", "x-method-override"])
def test_a_post_only_grant_cannot_override_the_method_the_site_runs(vault, override):
  vault.put_key("echo", {"value": DUMMY, "inject": HEADER_SLOT, "grants": _grants(
    sites=[{"host": "echo.example.com", "methods": ["POST"], "paths": ["/anything/*"]}],
  )})
  _MethodOverrideSite.ran = []
  site = ThreadingHTTPServer(("127.0.0.1", 0), _MethodOverrideSite)
  threading.Thread(target=site.serve_forever, daemon=True).start()
  local = lambda host, timeout: http.client.HTTPConnection(  # noqa: E731
    "127.0.0.1", site.server_address[1], timeout=timeout,
  )
  try:
    with _vault_socket(vault, local) as client:
      response = client.post(
        "/use/echo/echo.example.com/anything/item", content=b"{}",
        headers={"X-Vault-Caller": "app:7", override: "PATCH"},
      )
  finally:
    site.shutdown()
    site.server_close()
  assert response.status_code == 200, response.text
  # The site received the key, so it ran exactly the method the grant allows.
  assert _MethodOverrideSite.ran == [("POST", f"Bearer {DUMMY}")]


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
  owner = {"X-Vault-Caller": "owner"}
  # Every change names who made it, and apps never change keys.
  assert client.put("/keys/second", json={"value": "another-dummy-key", "inject": HEADER_SLOT}).status_code == 403
  assert client.put("/keys/second", headers={"X-Vault-Caller": "app:7"},
                    json={"value": "another-dummy-key", "inject": HEADER_SLOT}).status_code == 403
  added = client.put("/keys/second", headers=owner, json={
    "value": "another-dummy-key", "inject": {"type": "query", "name": "key"}, "grants": {"agents": True},
  })
  assert added.json() == {"name": "second", "created": True}
  listed = client.get("/keys").text
  assert "second" in listed and DUMMY not in listed and "another-dummy-key" not in listed
  assert client.put("/keys/x", headers=owner, json={"inject": HEADER_SLOT}).status_code == 400
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
  approved = client.post(f"/requests/{asked['id']}/approve", headers={"X-Vault-Caller": "agent:c1"},
                         json={"value": "maps-dummy-key", "ticket": listed["ticket"]})
  assert approved.json()["granted"] == 9
  assert [(e["caller"], e["outcome"]) for e in client.get("/usage?key=maps").json()["usage"][:2]] == [
    ("agent:c1", "granted app:9"), ("agent:c1", "added"),
  ]
  assert client.delete("/keys/second", headers=owner).status_code == 200
  assert client.delete("/keys/second", headers=owner).status_code == 404


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
  assert _ids(record["grants"]) == []
  assert [r["app"] for r in vault.list_requests()] == [9]
  with pytest.raises(kv.VaultError, match="may not use"):
    vault.authorize("echo", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")


def _inflate(headers, payload, piece=7):
  inflater = kv.Inflater(headers)
  out = b"".join(inflater.feed(payload[i:i + piece]) for i in range(0, len(payload), piece))
  return inflater.headers, out + inflater.finish()


def test_decoding_is_bounded_and_strips_the_encoding(monkeypatch):
  import gzip as _gzip
  import zlib as _zlib
  headers, body = _inflate([("Content-Encoding", "gzip"), ("X-A", "1")], _gzip.compress(b"hello"))
  assert body == b"hello" and headers == [("X-A", "1")]
  raw = _zlib.compressobj(wbits=-15)
  assert _inflate([("Content-Encoding", "deflate")], raw.compress(b"raw deflate") + raw.flush())[1] == b"raw deflate"
  assert _inflate([("Content-Encoding", "identity")], b"x") == ([], b"x")
  monkeypatch.setattr(kv, "MAX_RESPONSE_BYTES", 1000)
  with pytest.raises(kv.VaultError, match="too large"):
    _inflate([("Content-Encoding", "gzip")], _gzip.compress(b"a" * 5000))
  with pytest.raises(kv.VaultError, match="too large"):
    _inflate([], b"a" * 1001)
  with pytest.raises(kv.VaultError, match="unsupported"):
    kv.Inflater([("Content-Encoding", "gzip, br")])
  with pytest.raises(kv.VaultError, match="could not be decompressed"):
    _inflate([("Content-Encoding", "gzip")], b"not gzip at all")


@pytest.mark.parametrize("piece", [1, 2, 5, 13, 1000])
def test_an_echo_split_across_pieces_is_still_redacted(piece):
  forms = kv.secret_forms(DUMMY)
  body = b'{"a":"' + DUMMY.encode() + b'","b":"' + forms[0] + b'","tail":"ok"}'
  redactor = kv.StreamRedactor(forms)
  out = b"".join(redactor.feed(body[i:i + piece]) for i in range(0, len(body), piece)) + redactor.finish()
  assert out == kv.redact(body, forms)
  assert DUMMY.encode() not in out


@pytest.mark.parametrize("echo", [
  "fake\\/Key+with\\/slash0123==",                       # JSON with escaped slashes
  "fake%2fKey%2bwith%2fslash0123%3d%3d",                   # lowercase percent-encoding
  "fake&#x2F;Key&#x2B;with&#x2F;slash0123&#x3D;&#x3D;",    # HTML hex entities
  "fake&#47;Key&#43;with&#47;slash0123&#61;&#61;",         # HTML decimal entities
  "fake\\u002fKey\\u002bwith\\u002fslash0123\\u003d\\u003d",  # JSON unicode escapes
])
def test_redaction_covers_how_sites_escape_an_echoed_key(echo):
  forms = kv.secret_forms("fake/Key+with/slash0123==")
  assert kv.redact(b'{"echo":"' + echo.encode() + b'"}', forms) == b'{"echo":"' + kv.REDACTED + b'"}'


def test_create_only_refuses_an_occupied_name_and_an_update_never_moves_origin(vault):
  with pytest.raises(kv.VaultError, match="already exists"):
    vault.put_key("echo", {"value": "other", "inject": HEADER_SLOT, "create_only": True})
  assert vault.reveal("echo", ("owner", "")) == DUMMY
  # An app's own key is granted to it, as save_owned creates it.
  own = {"apps": [{"id": 7, "sites": []}], "agents": True, "sites": []}
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
  assert record["origin"] is None and _ids(record["grants"]) == []
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


PROVIDER_SITE = {"host": "models.example.net", "methods": ["POST"], "paths": ["/v1/responses"]}


def test_a_new_own_key_starts_on_the_sites_the_server_names_and_a_resave_keeps_the_owners(vault):
  created = vault.save_owned(9, "api_key", DUMMY, sites=[PROVIDER_SITE])
  assert created == {"name": "api_key", "created": True}
  record = next(k for k in vault.list_keys() if k["name"] == "api_key")
  assert record["grants"]["sites"] == [PROVIDER_SITE]
  narrowed = {"host": "safe.example.com", "methods": ["POST"], "paths": ["/v1/*"]}
  vault.put_key("api_key", {"inject": record["inject"],
                            "grants": {**record["grants"], "sites": [narrowed]}})
  vault.save_owned(9, "api_key", "rotated-dummy", sites=[PROVIDER_SITE])
  record = next(k for k in vault.list_keys() if k["name"] == "api_key")
  assert record["grants"]["sites"] == [narrowed]


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
                                "grants": {**record["grants"], "apps": _apps(9, 11, sites=[])}})
  assert vault.release_owned(9, "api.key") == {"name": saved["name"], "deleted": False}
  [kept] = [k for k in vault.list_keys() if k["name"] == saved["name"]]
  assert _ids(kept["grants"]) == [11] and kept["inject"] == HEADER_SLOT
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
  vault.put_key(shared, {"inject": HEADER_SLOT, "grants": {**record["grants"], "apps": _apps(WEATHER, 9, sites=[])}})
  assert vault.erase_owned(WEATHER) == [alone]
  keys = {k["name"]: k for k in vault.list_keys()}
  assert alone not in keys
  assert _ids(keys[shared]["grants"]) == [9]
  # "echo" is only lent to WEATHER: the owner's grant is not app data.
  assert _ids(keys["echo"]["grants"]) == [WEATHER]


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
                       "grants": {"apps": [{"id": 5, "sites": [ECHO_SITE]}, {"id": WEATHER, "sites": [ECHO_SITE]}], "agents": True, "sites": [ECHO_SITE]}})
  assert _origin(vault, name) == {"app": 5, "name": "api_key"}
  vault.forget_app(5)
  assert _origin(vault, name) is None
  # A new app that reuses id 5 asks, and the owner approves it as usual.
  asked = vault.request_access(5, {**ASK, "key": name, "sites": [ECHO_SITE]})
  vault.approve_request(asked["id"], {"ticket": _ticket(vault, asked["id"])})
  assert 5 in _ids(next(k for k in vault.list_keys() if k["name"] == name)["grants"])
  # It borrows the key; it does not own it, so saving its own secret makes a new key.
  assert _origin(vault, name) is None
  assert vault.save_owned(5, "api_key", "other-dummy-key")["name"] != name
  _, value = vault.authorize(name, ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")
  assert value == DUMMY


def test_an_origin_lasts_only_while_its_app_holds_a_grant(vault):
  # Released while shared: the key stays for the other app, without an owner.
  shared = vault.save_owned(5, "token", DUMMY)["name"]
  vault.put_key(shared, {"inject": HEADER_SLOT, "grants": {"apps": [{"id": 5, "sites": []}, {"id": WEATHER, "sites": []}], "agents": True, "sites": []}})
  vault.release_owned(5, "token")
  assert _origin(vault, shared) is None
  # The owner removing the creating app from the grants also ends its ownership.
  kept = vault.save_owned(6, "token", DUMMY)["name"]
  vault.put_key(kept, {"inject": HEADER_SLOT, "grants": {"apps": [{"id": WEATHER, "sites": []}], "agents": True, "sites": []}})
  assert _origin(vault, kept) is None


def test_a_new_key_request_without_a_slot_is_sent_as_a_bearer_token(vault):
  asked = vault.request_access(9, {k: v for k, v in ASK.items() if k != "inject"})
  vault.approve_request(asked["id"], {"value": DUMMY, "ticket": _ticket(vault, asked["id"])})
  assert next(k for k in vault.list_keys() if k["name"] == ASK["key"])["inject"] == HEADER_SLOT


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
    vault.log_traffic("echo", "agent", "GET", "echo.example.com", "/anything/x", "used")
  assert vault.list_keys()[0]["uses"] == 600


def _ids(grants):
  """The app ids a key is granted to (each grant also carries its own sites)."""
  return [grant["id"] for grant in grants["apps"]]


class _Slow(BaseHTTPRequestHandler):
  protocol_version = "HTTP/1.1"
  release = None

  def log_message(self, *args):
    pass

  def do_GET(self):
    self.send_response(200)
    self.send_header("Content-Type", "text/event-stream")
    self.send_header("Transfer-Encoding", "chunked")
    self.end_headers()
    first = b"data: first " + self.headers["Authorization"].encode() + b"\n\n"
    self.wfile.write(b"%x\r\n%s\r\n" % (len(first), first))
    self.wfile.flush()
    type(self).release.wait(20)
    last = b"data: last\n\n"
    self.wfile.write(b"%x\r\n%s\r\n0\r\n\r\n" % (len(last), last))
    self.wfile.flush()


def test_answers_stream_back_as_they_arrive_with_the_key_hidden(vault):
  _Slow.release = threading.Event()
  upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
  threading.Thread(target=upstream.serve_forever, daemon=True).start()
  local = lambda host, timeout: http.client.HTTPConnection(  # noqa: E731
    "127.0.0.1", upstream.server_address[1], timeout=timeout,
  )
  try:
    with _vault_socket(vault, local) as client:
      with client.stream("GET", "/use/echo/echo.example.com/anything/events",
                         headers={"X-Vault-Caller": "agent:c1"}) as response:
        pieces = response.iter_bytes()
        first = next(pieces)
        # The first event arrived while the site is still holding the rest.
        assert not _Slow.release.is_set()
        assert first.startswith(b"data: first") and DUMMY.encode() not in first
        _Slow.release.set()
        rest = b"".join(pieces)
      assert b"data: last" in first + rest and DUMMY.encode() not in first + rest
  finally:
    _Slow.release.set()
    upstream.shutdown()
  assert vault.usage(key="echo")[0]["outcome"] == "used"
