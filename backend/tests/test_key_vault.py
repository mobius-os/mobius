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
  return [{"id": app_id, "sites": [ECHO_SITE] if sites is None else sites, "paused": False} for app_id in ids]


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
  assert record["grants"]["apps"] == _apps(9, sites=[]) and record["grants"]["sites"] == []
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
  assert record["grants"]["apps"] == [*_apps(WEATHER), *_apps(9, sites=ASK["sites"])]
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
  assert listed["grants"]["apps"] == _apps(WEATHER)
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
  echoes = kv.secret_echoes(DUMMY)
  import base64, urllib.parse
  for encoded in (
    DUMMY, urllib.parse.quote(DUMMY, safe=""), json.dumps(DUMMY)[1:-1],
    base64.b64encode(DUMMY.encode()).decode(),
  ):
    assert encoded.encode() not in kv.redact(f"x{encoded}y".encode(), echoes)


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
def _vault_server(vault, connect):
  """A vault answering on a socket path, its sites reached through ``connect``."""
  # Unix socket paths are limited to ~108 bytes; pytest's tmp_path can exceed it.
  short_dir = Path(tempfile.mkdtemp(prefix="kv-"))
  socket_path = short_dir / kv.SOCKET_NAME
  handler = type("TestVault", (kv.VaultHandler,), {"vault": vault, "connect": staticmethod(connect)})
  server = kv.VaultServer(str(socket_path), handler)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  try:
    yield socket_path
  finally:
    server.shutdown()
    server.server_close()
    shutil.rmtree(short_dir, ignore_errors=True)


@contextlib.contextmanager
def _vault_socket(vault, connect):
  with _vault_server(vault, connect) as socket_path, httpx.Client(
    transport=httpx.HTTPTransport(uds=str(socket_path)), base_url="http://vault",
  ) as client:
    yield client


def _raw_exchange(socket_path, request: bytes, timeout: float = 10) -> bytes:
  """Send exact bytes to the vault and read its answer until it closes."""
  with socket.socket(socket.AF_UNIX) as conn:
    conn.settimeout(timeout)
    conn.connect(str(socket_path))
    conn.sendall(request)
    answer = b""
    while chunk := conn.recv(65536):
      answer += chunk
  return answer


@contextlib.contextmanager
def _raw_site(reply):
  """A site that answers every request by running ``reply(connection)``.

  Yields the ``connect`` the vault uses to reach it, and the request heads it
  received.
  """
  listener = socket.socket()
  listener.bind(("127.0.0.1", 0))
  listener.listen(8)
  heads = []

  def serve():
    while True:
      try:
        conn, _ = listener.accept()
      except OSError:
        return
      with conn:
        head = b""
        while b"\r\n\r\n" not in head and (chunk := conn.recv(65536)):
          head += chunk
        heads.append(head)
        with contextlib.suppress(OSError):
          reply(conn)

  threading.Thread(target=serve, daemon=True).start()
  port = listener.getsockname()[1]
  try:
    yield (lambda host, timeout: http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)), heads
  finally:
    listener.close()


def _borrow(path="/anything/x", caller="app:7", extra=b"") -> bytes:
  return (f"GET /use/echo/echo.example.com{path} HTTP/1.1\r\nHost: vault\r\n"
          f"X-Vault-Caller: {caller}\r\n").encode("latin-1") + extra + b"\r\n"


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
  # Only an attested app or agent asks; the owner and the platform answer or set keys up.
  for caller in ("owner", "platform:x"):
    assert client.post("/requests", json=ASK, headers={"X-Vault-Caller": caller}).status_code == 403
  asked = client.post("/requests", json=ASK, headers={"X-Vault-Caller": "app:9"}).json()
  assert asked["status"] == "pending"
  assert [r["id"] for r in client.get("/requests").json()["requests"]] == [asked["id"]]
  [listed] = client.get("/requests").json()["requests"]
  answer = {"value": "maps-dummy-key", "ticket": listed["ticket"]}
  # Letting an app use a key is the owner's own decision, at the vault too.
  assert client.post(f"/requests/{asked['id']}/approve", headers={"X-Vault-Caller": "agent:c1"},
                     json=answer).status_code == 403
  approved = client.post(f"/requests/{asked['id']}/approve", headers=owner, json=answer)
  assert approved.json()["granted"] == 9
  assert [(e["caller"], e["outcome"]) for e in client.get("/usage?key=maps").json()["usage"][:2]] == [
    ("owner", "granted app:9"), ("owner", "added"),
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
  import base64
  echoes = kv.secret_echoes(DUMMY)
  escaped = "".join(f"\\u{ord(ch):04x}" for ch in DUMMY).encode()  # the longest echo
  body = (b'{"a":"' + DUMMY.encode() + b'","b":"' + escaped + b'","c":"'
          + base64.b64encode(b"Bearer " + DUMMY.encode()) + b'","tail":"ok"}')
  redactor = kv.StreamRedactor(echoes)
  out = b"".join(redactor.feed(body[i:i + piece]) for i in range(0, len(body), piece)) + redactor.finish()
  assert out == kv.redact(body, echoes)
  assert DUMMY.encode() not in out and escaped not in out


@pytest.mark.parametrize("echo", [
  "fake\\/Key+with\\/slash0123==",                       # JSON with escaped slashes
  "fake%2fKey%2bwith%2fslash0123%3d%3d",                   # lowercase percent-encoding
  "fake&#x2F;Key&#x2B;with&#x2F;slash0123&#x3D;&#x3D;",    # HTML hex entities
  "fake&#47;Key&#43;with&#47;slash0123&#61;&#61;",         # HTML decimal entities
  "fake\\u002fKey\\u002bwith\\u002fslash0123\\u003d\\u003d",  # JSON unicode escapes
])
def test_redaction_covers_how_sites_escape_an_echoed_key(echo):
  echoes = kv.secret_echoes("fake/Key+with/slash0123==")
  assert kv.redact(b'{"echo":"' + echo.encode() + b'"}', echoes) == b'{"echo":"' + kv.REDACTED + b'"}'


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
  assert saved == {"name": "app9-OPENAI_API_KEY", "created": True}
  [record] = [k for k in vault.list_keys() if k["name"] == saved["name"]]
  # The owner (or an agent's store-key.py) fills in the real value; the
  # grants it does not mention stay as they are.
  vault.put_key(saved["name"], {"value": "owners-real-key"})
  [record] = [k for k in vault.list_keys() if k["name"] == saved["name"]]
  assert record["origin"] is None and _ids(record["grants"]) == []
  # The app can neither delete nor replace it; its next save is a key of its own.
  assert vault.release_owned(9, "OPENAI_API_KEY") is None
  assert vault.save_owned(9, "OPENAI_API_KEY", "app-chosen") == {
    "name": "app9-OPENAI_API_KEY-2", "created": True,
  }
  assert vault.reveal(saved["name"], ("owner", "")) == "owners-real-key"
  assert vault.erase_owned(9) == ["app9-OPENAI_API_KEY-2"]
  assert vault.reveal(saved["name"], ("owner", "")) == "owners-real-key"


def test_the_apps_own_save_keeps_its_ownership(vault):
  vault.save_owned(9, "api.key", "first")
  assert vault.save_owned(9, "api.key", "second") == {"name": "app9-api-key", "created": False}
  assert {k["name"]: k["origin"] for k in vault.list_keys()}["app9-api-key"] == {"app": 9, "name": "api.key"}


def test_an_apps_own_save_never_takes_a_plain_name_another_app_may_ask_for(vault):
  """A plain name would let an app plant a value under the name another app
  asks for, and the owner's approval would lend the planter's value."""
  assert vault.save_owned(9, "maps", DUMMY) == {"name": "app9-maps", "created": True}
  assert "maps" not in {k["name"] for k in vault.list_keys()}
  # A key that already exists under the asked name shows who saved it.
  vault.save_owned(9, "weather", DUMMY)
  vault.request_access(10, {**ASK, "key": "app9-weather"})
  vault.request_access(10, ASK)
  cards = {r["key"]: r for r in vault.list_requests()}
  assert cards["app9-weather"]["key_exists"] is True
  assert cards["app9-weather"]["key_origin"] == {"app": 9, "name": "weather"}
  assert cards["maps"]["key_exists"] is False and cards["maps"]["key_origin"] is None


PROVIDER_SITE = {"host": "models.example.net", "methods": ["POST"], "paths": ["/v1/responses"]}


def test_a_new_own_key_starts_on_the_sites_the_server_names_and_a_resave_keeps_the_owners(vault):
  created = vault.save_owned(9, "api_key", DUMMY, sites=[PROVIDER_SITE])
  assert created == {"name": "app9-api_key", "created": True}
  record = next(k for k in vault.list_keys() if k["name"] == "app9-api_key")
  assert record["grants"]["sites"] == [PROVIDER_SITE]
  narrowed = {"host": "safe.example.com", "methods": ["POST"], "paths": ["/v1/*"]}
  vault.put_key("app9-api_key", {"inject": record["inject"],
                                 "grants": {**record["grants"], "sites": [narrowed]}})
  vault.save_owned(9, "api_key", "rotated-dummy", sites=[PROVIDER_SITE])
  record = next(k for k in vault.list_keys() if k["name"] == "app9-api_key")
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
  assert {r["name"] for r in results} == {"app9-api-key"}
  assert sum(r["created"] for r in results) == 1
  owned = [k for k in vault.list_keys() if k["origin"] == {"app": 9, "name": "api.key"}]
  assert [k["name"] for k in owned] == ["app9-api-key"]


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
  name = vault.save_owned(9, "OPENAI_API_KEY", "app-dummy")["name"]
  site = "api.openai.com:POST:/v1/*"
  assert _run_store_key(monkeypatch, vault, [name, "--site", site, "--replace"], "owners-real-key") == 0
  with pytest.raises(kv.VaultError, match="may not use"):
    vault.authorize(name, ("app", "9"), "POST", "api.openai.com", "/v1/responses")
  _, value = vault.authorize(name, ("agent", "c1"), "POST", "api.openai.com", "/v1/responses")
  assert value == "owners-real-key"


def test_store_key_never_replaces_an_existing_key_unless_asked(monkeypatch, vault):
  """store-key.py writes to the vault socket as the owner, past the server's
  route checks, so replacing a key is an explicit choice (--replace), and a
  replacement applies only to the key as it was read."""
  def value_of(name):
    return vault.reveal(name, ("owner", ""))

  assert _run_store_key(monkeypatch, vault, ["fresh"], "first-value") == 0
  assert value_of("fresh") == "first-value"
  # Exit 3: the key exists and is left exactly as it was.
  [before] = [k for k in vault.list_keys() if k["name"] == "fresh"]
  assert _run_store_key(monkeypatch, vault, ["fresh", "--site", "evil.example.com"], "second") == 3
  assert value_of("fresh") == "first-value"
  assert [k for k in vault.list_keys() if k["name"] == "fresh"] == [before]
  assert _run_store_key(monkeypatch, vault, ["fresh", "--replace"], "second-value") == 0
  assert value_of("fresh") == "second-value"

  listed = vault.list_keys

  def changed_after_listing():
    keys = listed()
    vault.put_key("fresh", {"value": "owners-newer-value"})
    return keys
  monkeypatch.setattr(vault, "list_keys", changed_after_listing)
  # A replacement from a stale read never undoes the change made meanwhile.
  assert _run_store_key(monkeypatch, vault, ["fresh", "--replace"], "stale-value") == 4
  assert value_of("fresh") == "owners-newer-value"

  def created_after_listing():
    keys = listed()
    vault.put_key("late", {"value": "someone-elses", "inject": HEADER_SLOT, "grants": _grants()})
    return [k for k in keys if k["name"] != "late"]
  monkeypatch.setattr(vault, "list_keys", created_after_listing)
  # A key created since the list was read is never overwritten as new.
  assert _run_store_key(monkeypatch, vault, ["late"], "mine") == 3
  assert value_of("late") == "someone-elses"


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
    # Followed by more than redaction's fixed tail, so the event itself goes out.
    padding = b": " + b"." * kv.secret_echoes(DUMMY).longest + b"\n\n"
    first = b"data: first " + self.headers["Authorization"].encode() + b"\n\n" + padding
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
        assert first.startswith(b"data: first Bearer " + kv.REDACTED + b"\n\n")
        assert DUMMY.encode() not in first
        _Slow.release.set()
        rest = b"".join(pieces)
      assert b"data: last" in first + rest and DUMMY.encode() not in first + rest
  finally:
    _Slow.release.set()
    upstream.shutdown()
  assert vault.usage(key="echo")[0]["outcome"] == "used"


# -- an update changes only what it names --------------------------------------

def test_replacing_only_the_value_keeps_who_may_use_the_key_and_how_it_is_sent(vault):
  [before] = vault.list_keys()
  # An agent rotating the value sends nothing else, as store-key.py may.
  vault.put_key("echo", {"value": "rotated-dummy"}, actor="agent:c1")
  [after] = vault.list_keys()
  assert after["grants"] == before["grants"] and after["inject"] == before["inject"]
  _, value = vault.authorize("echo", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")
  assert value == "rotated-dummy"
  # Changing only how it is sent keeps the grants too.
  vault.put_key("echo", {"inject": {"type": "query", "name": "key"}})
  assert vault.list_keys()[0]["grants"] == before["grants"]


def test_a_new_key_needs_a_value_and_where_it_is_sent(tmp_path):
  v = kv.Vault(tmp_path / "vault")
  with pytest.raises(kv.VaultError, match="needs a value"):
    v.put_key("fresh", {"inject": HEADER_SLOT})
  with pytest.raises(kv.VaultError, match="needs where it is sent") as refused:
    v.put_key("fresh", {"value": DUMMY})
  assert refused.value.status == 400 and v.list_keys() == []


# -- a change takes effect only once it is on disk -------------------------------

@pytest.fixture
def full_disk(monkeypatch):
  """Make writes of the named store files fail as on a full disk."""
  real = kv._atomic_write
  failing = set()

  def write(path, data):
    if path.name in failing:
      raise OSError(28, "No space left on device")
    return real(path, data)

  monkeypatch.setattr(kv, "_atomic_write", write)
  return failing


def _as_reloaded(vault):
  reloaded = kv.Vault(vault.dir)
  return (reloaded.list_keys(), reloaded.list_requests()) == (vault.list_keys(), vault.list_requests())


def test_a_change_whose_write_fails_never_takes_effect(vault, full_disk):
  full_disk.add("keys.json")
  with pytest.raises(OSError):
    vault.put_key("echo", {"grants": _grants(apps=[WEATHER, 9])})
  with pytest.raises(kv.VaultError, match="may not use"):
    vault.authorize("echo", ("app", "9"), "GET", "echo.example.com", "/anything/x")
  with pytest.raises(OSError):
    vault.revoke_key("echo")
  assert [k["name"] for k in vault.list_keys()] == ["echo"]
  full_disk.clear()
  full_disk.add("requests.json")
  with pytest.raises(OSError):
    vault.request_access(9, ASK)
  assert vault.list_requests() == []
  assert _as_reloaded(vault)


def test_a_retried_purge_after_a_failed_write_is_saved(vault, full_disk):
  full_disk.add("keys.json")
  with pytest.raises(OSError):
    vault.forget_app(WEATHER)
  # Still granted, exactly as on disk, so the purge is not mistaken for done.
  vault.authorize("echo", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")
  full_disk.clear()
  assert vault.forget_app(WEATHER)["keys"] == ["echo"]
  assert _ids(kv.Vault(vault.dir).list_keys()[0]["grants"]) == []


def test_a_change_spanning_two_files_keeps_memory_equal_to_disk(vault, full_disk):
  request = vault.request_access(9, {**ASK, "key": "echo"})
  full_disk.add("requests.json")
  with pytest.raises(OSError):
    vault.approve_request(request["id"], {"sites": ASK["sites"], "ticket": _ticket(vault, request["id"])})
  # The grant was saved, the settled request was not: both stay as on disk.
  assert 9 in _ids(vault.list_keys()[0]["grants"])
  assert _as_reloaded(vault)


def test_a_nested_change_is_audited_only_once_the_outer_change_is_saved(vault, full_disk):
  """save_owned creates its key through put_key inside its own change: the
  inner change's audit line must wait for the outer save, which fails here."""
  full_disk.add("keys.json")
  with pytest.raises(OSError):
    vault.save_owned(WEATHER, "api.key", DUMMY)
  assert [k["name"] for k in vault.list_keys()] == ["echo"]
  assert [e for e in vault.usage(limit=None) if e["outcome"] == "added" and e["key"] != "echo"] == []
  full_disk.clear()
  saved = vault.save_owned(WEATHER, "api.key", DUMMY)
  assert [e["outcome"] for e in vault.usage(key=saved["name"])] == ["added"]


def test_a_saved_change_whose_audit_fails_still_reports_success(vault, monkeypatch, capsys):
  """The store write is what makes a change take effect; an audit line that
  fails afterwards is reported on stderr, never as a failed change."""
  real = vault._append

  def append(path, rotate_bytes, fields):
    if path == vault.audit_path:
      raise OSError(28, "No space left on device")
    return real(path, rotate_bytes, fields)

  monkeypatch.setattr(vault, "_append", append)
  with _vault_socket(vault, None) as client:
    answer = client.put("/keys/second", headers={"X-Vault-Caller": "owner"},
                        json={"value": DUMMY, "inject": HEADER_SLOT})
  assert answer.status_code == 200 and answer.json() == {"name": "second", "created": True}
  assert [k["name"] for k in kv.Vault(vault.dir).list_keys()] == ["echo", "second"]
  assert "saved, but could not audit 'added' on 'second' by 'owner'" in capsys.readouterr().err


def test_a_failed_write_answers_a_json_error_over_the_socket(vault, full_disk):
  full_disk.add("keys.json")
  with _vault_socket(vault, None) as client:
    answer = client.put("/keys/second", headers={"X-Vault-Caller": "owner"},
                        json={"value": DUMMY, "inject": HEADER_SLOT})
  assert answer.status_code == 500 and "could not write" in answer.json()["error"]
  assert [k["name"] for k in vault.list_keys()] == ["echo"]


# -- redaction ------------------------------------------------------------------

def test_redaction_holds_back_the_same_tail_whether_or_not_it_begins_the_key():
  """When text arrives must never tell a caller whether its echo begins the key."""
  key = "sk-live-FAKE0123456789abcdef"
  echoes = kv.secret_echoes(key)
  hold = echoes.longest - 1
  for guess in (b"sk-live-FAKE0", b"sk-live-FAKE1"):   # a prefix of the key, and not
    redactor = kv.StreamRedactor(echoes)
    tail = b"echo:" + guess
    # Whatever the tail is, exactly the bytes before it go out now ...
    assert redactor.feed(b"x" * hold + tail) == b"x" * len(tail)
    assert redactor.feed(b"") == b""
    # ... and the tail itself with the next piece or at the end.
    assert redactor.finish() == b"x" * (hold - len(tail)) + tail


KEY_WITH_PUNCTUATION = "sk-Ab+c/d&e<f>g=h1234567890"


def _go_json(text):
  return json.dumps(text)[1:-1].replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")


def _b64(prefix, urlsafe=False):
  import base64
  encode = base64.urlsafe_b64encode if urlsafe else base64.b64encode
  return lambda k: encode(prefix + k.encode())


@pytest.mark.parametrize("encode, kept", [
  # ``kept``: what may stay readable around the redaction, at most the
  # encoded neighbouring bytes and the characters base64 shares with them.
  pytest.param(lambda k: _go_json(k).encode(), 0, id="go-json-escapes"),
  pytest.param(lambda k: __import__("urllib.parse").parse.quote(k).encode(), 0, id="percent-keeping-slash"),
  pytest.param(lambda k: k.replace("+", "%2b").replace("/", "\\/").replace("&", "&amp;").replace("<", "&#60;").encode(),
               0, id="mixed-escapes"),
  pytest.param(lambda k: k.upper().encode(), 0, id="upper-cased"),
  pytest.param(lambda k: k.encode().hex().encode(), 0, id="hex"),
  pytest.param(lambda k: k.encode().hex().upper().encode(), 0, id="hex-upper"),
  pytest.param(lambda k: k.encode("utf-16-le"), 0, id="utf-16-le"),
  pytest.param(lambda k: k.encode("utf-16-be"), 0, id="utf-16-be"),
  pytest.param(_b64(b""), 4, id="base64-aligned"),
  pytest.param(_b64(b"Bearer "), 10 + 4, id="base64-offset-1"),
  pytest.param(_b64(b"ab"), 3 + 4, id="base64-offset-2"),
  pytest.param(_b64(b"x:", urlsafe=True), 3 + 4, id="base64url-offset-2"),
])
def test_redaction_covers_mixed_escapes_and_whole_reencodings(encode, kept):
  echo = encode(KEY_WITH_PUNCTUATION)
  out = kv.redact(b"<" + echo + b">", kv.secret_echoes(KEY_WITH_PUNCTUATION))
  assert kv.REDACTED in out
  assert len(out) - len(kv.REDACTED) - 2 <= kept


def test_redaction_of_a_long_value_stays_fast():
  import secrets as _secrets
  import string
  import time
  value = "".join(_secrets.choice(string.punctuation + string.ascii_letters) for _ in range(kv.MAX_VALUE_LENGTH))
  started = time.monotonic()
  redactor = kv.StreamRedactor(kv.secret_echoes(value))
  for _ in range(20):
    redactor.feed(b"x" * 65535 + value[:1].encode())
  assert time.monotonic() - started < 10


# -- what may be sent ------------------------------------------------------------

def test_a_value_a_header_cannot_carry_is_refused_cleanly(vault):
  vault.put_key("echo", {"value": "pässwört€-dummy"})
  with pytest.raises(kv.VaultError, match="characters a header can't carry") as refused:
    vault.authorize("echo", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")
  assert refused.value.status == 400
  with _raw_site(lambda conn: conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")) as (connect, heads):
    with _vault_server(vault, connect) as path:
      answer = _raw_exchange(path, _borrow())
  assert answer.startswith(b"HTTP/1.1 400") and b"can't carry" in answer and heads == []


def test_text_http_cannot_carry_is_refused_not_crashed(vault):
  with _raw_site(lambda conn: conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")) as (connect, heads):
    with _vault_server(vault, connect) as path:
      answer = _raw_exchange(path, _borrow("/anything/x?q=\xe9"))
  assert answer.startswith(b"HTTP/1.1 400") and b"percent-encode" in answer
  assert vault.usage(key="echo")[0]["outcome"] == "denied"


@pytest.mark.parametrize("header", ["X-Original-URL", "X-Rewrite-URL", "x-override-url"])
def test_a_header_naming_another_path_never_reaches_the_site(header):
  request = kv.build_upstream_request(
    method="GET", host="echo.example.com", raw_path="/anything/x", raw_query="",
    caller_headers=[(header, "/admin/delete-all"), ("X-Trace", "1")], inject=HEADER_SLOT, value=DUMMY,
  )
  assert [k for k, _ in request.headers if k.lower() == header.lower()] == []
  assert ("X-Trace", "1") in request.headers


def test_a_folded_header_line_is_refused_before_reaching_the_site(vault):
  with pytest.raises(kv.VaultError, match="folded"):
    kv.build_upstream_request(
      method="GET", host="echo.example.com", raw_path="/anything/x", raw_query="",
      caller_headers=[("X-Foo", "a\r\n Transfer-Encoding: chunked")], inject=HEADER_SLOT, value=DUMMY,
    )
  with _raw_site(lambda conn: conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")) as (connect, heads):
    with _vault_server(vault, connect) as path:
      answer = _raw_exchange(path, _borrow(extra=b"X-Foo: a\r\n Transfer-Encoding: chunked\r\n"))
  assert answer.startswith(b"HTTP/1.1 400") and heads == []


@pytest.mark.parametrize("path", [
  "/anything/..;/admin", "/anything/.;x/y", "/anything/%2e%2e;/admin",
  "/anything/%252e%252e/admin", "/anything/x%25", "/anything/%C0%AE%C0%AE/admin", "/anything/%ff",
])
def test_paths_a_site_could_decode_further_than_the_rule_are_refused(path):
  with pytest.raises(kv.VaultError):
    kv.normalized_request_path(path)


def test_path_parameters_and_valid_utf8_still_pass_the_path_rule():
  assert kv.normalized_request_path("/anything/x;v=1") == "/anything/x;v=1"
  assert kv.normalized_request_path("/anything/caf%C3%A9") == "/anything/café"


# -- the site's answer -------------------------------------------------------------

def test_an_unsupported_transfer_coding_is_refused():
  for value in ("gzip, chunked", "gzip", "chunked, chunked"):
    with pytest.raises(kv.VaultError, match="transfer encoding") as refused:
      kv.Inflater([("Transfer-Encoding", value)])
    assert refused.value.status == 502
  assert kv.Inflater([("Transfer-Encoding", "Chunked")]).encoding is None


def test_raw_deflate_whose_first_piece_is_one_byte_still_decodes():
  import zlib as _zlib
  raw = _zlib.compressobj(wbits=-15)
  blob = raw.compress(b'{"hello":"world"}') + raw.flush()
  assert _inflate([("Content-Encoding", "deflate")], blob, piece=1)[1] == b'{"hello":"world"}'
  zlib_framed = _zlib.compress(b"framed")
  assert _inflate([("Content-Encoding", "deflate")], zlib_framed, piece=1)[1] == b"framed"


def test_a_compressed_answer_that_stops_short_is_an_error():
  import gzip as _gzip
  blob = _gzip.compress(b"a whole answer" * 50)
  with pytest.raises(kv.VaultError, match="ended early"):
    _inflate([("Content-Encoding", "gzip")], blob[:len(blob) // 2])


def test_the_sites_status_text_and_vault_headers_never_speak_for_the_vault(vault):
  def reply(conn):
    conn.sendall(b"HTTP/1.1 103 Early Hints\r\nLink: </a.css>; rel=preload\r\n\r\n"
                 b"HTTP/1.1 401 bad key " + DUMMY.encode() + b"\r\nX-Vault-Denied: 1\r\n"
                 b"X-Vault-Key: other\r\nContent-Length: 2\r\n\r\nno")
  with _raw_site(reply) as (connect, _), _vault_server(vault, connect) as path:
    answer = _raw_exchange(path, _borrow())
  head, _, body = answer.partition(b"\r\n\r\n")
  lines = head.split(b"\r\n")
  # The interim 103 was skipped; the final answer went out with its standard phrase.
  assert lines[0] == b"HTTP/1.1 401 Unauthorized"
  assert not any(line.lower().startswith(b"x-vault-denied") for line in lines)
  assert [line for line in lines if line.lower().startswith(b"x-vault-key")] == [b"X-Vault-Key: echo"]
  assert body == b"2\r\nno\r\n0\r\n\r\n" and DUMMY.encode() not in answer
  assert vault.usage(key="echo")[0]["status"] == 401


def test_an_answer_that_stops_short_of_its_length_ends_as_an_error(vault):
  def reply(conn):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n" + b"x" * 10)
  with _raw_site(reply) as (connect, _), _vault_server(vault, connect) as path:
    answer = _raw_exchange(path, _borrow())
  assert answer.startswith(b"HTTP/1.1 200") and not answer.endswith(b"0\r\n\r\n")
  entry = vault.usage(key="echo")[0]
  assert entry["outcome"] == "failed" and "broke off" in entry["reason"]


def test_a_borrow_ends_at_its_deadline_however_steadily_the_site_trickles(vault, monkeypatch):
  import time
  monkeypatch.setattr(kv, "BORROW_DEADLINE_SECS", 1)

  def trickle(conn):
    conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
    for _ in range(20):
      conn.sendall(b"1\r\nx\r\n")
      time.sleep(0.2)
    conn.sendall(b"0\r\n\r\n")

  started = time.monotonic()
  with _raw_site(trickle) as (connect, _), _vault_server(vault, connect) as path:
    answer = _raw_exchange(path, _borrow())
  assert time.monotonic() - started < 3
  assert not answer.endswith(b"0\r\n\r\n")
  entry = vault.usage(key="echo")[0]
  assert entry["outcome"] == "failed" and "longer than 1 seconds" in entry["reason"]


@pytest.mark.parametrize("trickle", ["header lines", "a chunk-size line"])
def test_a_site_trickling_its_framing_is_cut_off_at_the_deadline(vault, monkeypatch, trickle):
  """Each read on the site gets a fresh timeout, so only cutting the
  connection at the deadline stops a site that sends its head, or a chunk's
  size, a few bytes at a time."""
  import time
  monkeypatch.setattr(kv, "BORROW_DEADLINE_SECS", 1)

  def reply(conn):
    if trickle == "header lines":
      conn.sendall(b"HTTP/1.1 200 OK\r\n")
      for i in range(12):
        conn.sendall(b"X-Slow-%d: y\r\n" % i)
        time.sleep(0.3)
      conn.sendall(b"Content-Length: 2\r\n\r\nok")
    else:
      conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
      for _ in range(12):
        conn.sendall(b"0")
        time.sleep(0.3)
      conn.sendall(b"1\r\nx\r\n0\r\n\r\n")

  started = time.monotonic()
  with _raw_site(reply) as (connect, _), _vault_server(vault, connect) as path:
    answer = _raw_exchange(path, _borrow())
  assert time.monotonic() - started < 2.5
  assert not answer.endswith(b"0\r\n\r\n")
  if trickle == "header lines":
    assert answer.startswith(b"HTTP/1.1 504")
  entry = vault.usage(key="echo")[0]
  assert entry["outcome"] == "failed" and "longer than 1 seconds" in entry["reason"]


def test_a_site_dribbling_its_tls_handshake_is_cut_off_at_the_deadline(vault, monkeypatch):
  """The deadline covers connecting too: a site that answers the ClientHello a
  byte at a time (each read with a fresh timeout) is cut off like any stage."""
  import time
  monkeypatch.setattr(kv, "BORROW_DEADLINE_SECS", 1)
  listener = socket.socket()
  listener.bind(("127.0.0.1", 0))
  listener.listen(1)
  port = listener.getsockname()[1]

  def dribble():
    conn, _ = listener.accept()
    with conn, contextlib.suppress(OSError):
      conn.recv(65536)  # the ClientHello
      conn.sendall(b"\x16\x03\x03\x40\x00")  # a handshake record of 16 KiB...
      for _ in range(12):
        time.sleep(0.3)
        conn.sendall(b"\x00")  # ...that never finishes arriving

  threading.Thread(target=dribble, daemon=True).start()

  def pinned_to_this_site(host, timeout):
    connection = kv.PinnedHTTPSConnection(host, "127.0.0.1", timeout)
    connection.port = port
    return connection

  started = time.monotonic()
  try:
    with _vault_server(vault, pinned_to_this_site) as path:
      answer = _raw_exchange(path, _borrow(), timeout=30)
  finally:
    listener.close()
  assert time.monotonic() - started < 2.5
  assert answer.startswith(b"HTTP/1.1 504")
  entry = vault.usage(key="echo")[0]
  assert entry["outcome"] == "failed" and "longer than 1 seconds" in entry["reason"]


def test_a_caller_that_stops_reading_is_cut_off_and_frees_its_slot(vault, monkeypatch):
  import time
  monkeypatch.setattr(kv, "MAX_CONCURRENT_BORROWS", 1)
  monkeypatch.setattr(kv, "CALLER_SEND_TIMEOUT_SECS", 1)

  answered = []

  def firehose(conn):
    answered.append(conn)
    if len(answered) > 1:
      conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
      return
    conn.sendall(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
    piece = b"%x\r\n" % 65536 + b"x" * 65536 + b"\r\n"
    for _ in range(300):
      conn.sendall(piece)
    conn.sendall(b"0\r\n\r\n")

  with _raw_site(firehose) as (connect, _), _vault_server(vault, connect) as path:
    with socket.socket(socket.AF_UNIX) as stalled:
      stalled.connect(str(path))
      stalled.sendall(_borrow())
      assert stalled.recv(12) == b"HTTP/1.1 200"
      time.sleep(2.5)  # reads nothing more; the deadline is still minutes away
      after = _raw_exchange(path, _borrow(), timeout=30)
  assert after.startswith(b"HTTP/1.1 200")
  reasons = [entry.get("reason") for entry in vault.usage(key="echo")]
  assert "The caller stopped reading the answer." in reasons


def test_a_caller_reading_slowly_but_steadily_is_never_taken_for_a_stalled_one(vault, monkeypatch):
  """A small compressed piece can inflate to megabytes; the send timeout must
  measure a stall, not how long the whole piece takes a slow reader."""
  import gzip
  import time
  monkeypatch.setattr(kv, "CALLER_SEND_TIMEOUT_SECS", 1)
  body = gzip.compress(b'{"row": 12345, "name": "example"},\n' * 75_000)  # about 2.6 MB inflated

  def site(conn):
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: %d\r\n\r\n" % len(body) + body)

  with _raw_site(site) as (connect, _), _vault_server(vault, connect) as path:
    with socket.socket(socket.AF_UNIX) as caller:
      caller.settimeout(30)
      caller.connect(str(path))
      caller.sendall(_borrow())
      answer = b""
      while chunk := caller.recv(65536):
        answer += chunk
        time.sleep(0.05)  # steady, never pausing longer than the timeout
  assert answer.endswith(b"0\r\n\r\n"), "the answer must arrive complete"
  borrow = vault.usage(key="echo")[0]  # newest first
  assert borrow["outcome"] == "used" and not borrow.get("reason")


def test_one_caller_beyond_its_own_cap_is_busy_while_others_still_borrow(vault, monkeypatch):
  monkeypatch.setattr(kv, "MAX_BORROWS_PER_CALLER", 1)
  release = threading.Event()

  def hold(conn):
    release.wait(10)
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

  with _raw_site(hold) as (connect, heads), _vault_server(vault, connect) as path:
    first = []
    holder = threading.Thread(target=lambda: first.append(_raw_exchange(path, _borrow())))
    holder.start()
    while not heads:
      threading.Event().wait(0.02)
    same_app = _raw_exchange(path, _borrow())
    other = []
    agent = threading.Thread(target=lambda: other.append(_raw_exchange(path, _borrow(caller="agent:chat-1"))))
    agent.start()
    agent.join(0.3)  # waits at the site (served one at a time) unless refused
    release.set()
    holder.join(10)
    agent.join(10)
  assert same_app.startswith(b"HTTP/1.1 429") and b"This caller already has 1" in same_app
  assert first[0].startswith(b"HTTP/1.1 200") and other[0].startswith(b"HTTP/1.1 200")


def test_borrows_beyond_the_cap_are_answered_busy(vault, monkeypatch):
  monkeypatch.setattr(kv, "MAX_CONCURRENT_BORROWS", 1)
  release = threading.Event()

  def hold(conn):
    release.wait(10)
    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")

  with _raw_site(hold) as (connect, heads), _vault_server(vault, connect) as path:
    first = []
    holder = threading.Thread(target=lambda: first.append(_raw_exchange(path, _borrow())))
    holder.start()
    while not heads:
      threading.Event().wait(0.02)
    busy = _raw_exchange(path, _borrow())
    release.set()
    holder.join(10)
    after = _raw_exchange(path, _borrow())
  assert busy.startswith(b"HTTP/1.1 429") and b"busy" in busy
  assert first[0].startswith(b"HTTP/1.1 200") and after.startswith(b"HTTP/1.1 200")


# -- the store at start and when damaged ----------------------------------------------

def test_a_master_key_that_opens_no_stored_key_refuses_to_start(vault):
  (vault.dir / "master.key").write_bytes(os.urandom(32))
  with pytest.raises(SystemExit, match="opens none of the stored keys"):
    kv.Vault(vault.dir)


def test_a_key_that_cannot_be_decrypted_answers_a_clean_error(vault):
  vault.put_key("other", {"value": DUMMY, "inject": HEADER_SLOT, "grants": _grants()})
  vault._records["echo"]["sealed"] = vault._records["other"]["sealed"]  # sealed for another name
  with pytest.raises(kv.VaultError, match="can't be decrypted") as refused:
    vault.reveal("echo", ("owner", ""))
  assert refused.value.status == 500
  with _vault_socket(vault, None) as client:
    answer = client.get("/keys/echo/value", headers={"X-Vault-Caller": "owner"})
    used = client.get("/use/echo/echo.example.com/anything/x", headers={"X-Vault-Caller": "app:7"})
  assert answer.status_code == 500 and answer.json() == {"error": "This key can't be decrypted."}
  assert used.status_code == 500 and vault.usage(key="echo")[0]["outcome"] == "failed"


def test_a_damaged_requests_file_is_set_aside_and_the_vault_starts(vault):
  vault.request_access(9, ASK)
  vault.requests_path.write_text('{"requests": {"9-maps": "half a reque')
  reloaded = kv.Vault(vault.dir)
  assert reloaded.list_requests() == [] and not reloaded.requests_path.exists()
  [aside] = vault.dir.glob("requests.json.damaged-*")
  assert "half a reque" in aside.read_text()
  assert reloaded.request_access(9, ASK)["status"] == "pending"


def test_a_version_one_store_merges_duplicates_and_drops_what_no_longer_holds(tmp_path):
  folder = tmp_path / "old"
  v = kv.Vault(folder)
  v.put_key("dupes", {"value": DUMMY, "inject": HEADER_SLOT, "grants": _grants()})
  v.put_key("nosites", {"value": DUMMY, "inject": HEADER_SLOT, "grants": _grants()})
  v.put_key("orphan", {"value": DUMMY, "inject": HEADER_SLOT, "grants": _grants()})
  payload = json.loads(v.store_path.read_text())
  keys = payload["keys"]
  keys["dupes"]["grants"]["apps"] = [3, 3]
  keys["nosites"]["grants"] = {"apps": [3], "agents": True}
  keys["orphan"]["grants"]["apps"] = []
  keys["orphan"]["origin"] = {"app": 9, "name": "x"}
  payload["version"] = 1
  v.store_path.write_text(json.dumps(payload))
  migrated = {k["name"]: k for k in kv.Vault(folder).list_keys()}
  assert migrated["dupes"]["grants"]["apps"] == _apps(3)
  assert migrated["nosites"]["grants"]["apps"] == _apps(3, sites=[])
  assert migrated["nosites"]["grants"]["sites"] == []
  assert migrated["orphan"]["origin"] is None


# -- the owner's decisions --------------------------------------------------------

def test_an_agent_cannot_switch_agents_on_or_widen_an_app_but_can_narrow(vault):
  vault.put_key("echo", {"grants": _grants(agents=False)})
  with pytest.raises(kv.VaultError, match="Only you can let agents") as refused:
    vault.put_key("echo", {"grants": _grants(agents=True)}, actor="agent:c1")
  assert refused.value.status == 403
  wider = {**ECHO_SITE, "paths": ["/*"]}
  for grants in (_grants(agents=False, apps=[WEATHER, 9]), _grants(agents=False, apps=_apps(WEATHER, sites=[wider]))):
    with pytest.raises(kv.VaultError, match="Only you can let an app"):
      vault.put_key("echo", {"grants": grants}, actor="agent:c1")
  # Narrowing, or listing the same reach in another order, only takes access away.
  narrow = {**ECHO_SITE, "paths": ["/anything/public/*"]}
  vault.put_key("echo", {"grants": _grants(agents=False, apps=_apps(WEATHER, sites=[narrow]))}, actor="agent:c1")
  vault.put_key("echo", {"grants": _grants(agents=False, apps=[])}, actor="agent")
  # The owner's own input is unchanged.
  vault.put_key("echo", {"grants": _grants(agents=True, apps=[WEATHER, 9])}, actor="owner")
  assert _ids(vault.list_keys()[0]["grants"]) == [WEATHER, 9]


def test_only_the_owner_approves_a_request_at_the_vault(vault):
  request = vault.request_access(9, ASK)
  answer = {"value": DUMMY, "ticket": _ticket(vault, request["id"])}
  for actor in ("agent:c1", "agent", "platform:import"):
    with pytest.raises(kv.VaultError, match="Only the owner") as refused:
      vault.approve_request(request["id"], answer, actor=actor)
    assert refused.value.status == 403
  assert vault.approve_request(request["id"], answer, actor="owner")["granted"] == 9


def test_a_deny_checks_the_requests_ticket_when_given(vault):
  request = vault.request_access(9, ASK)
  with pytest.raises(kv.VaultError, match="changed since") as refused:
    vault.deny_request(request["id"], {"ticket": "from-an-old-card"})
  assert refused.value.status == 409 and vault.list_requests()[0]["status"] == "pending"
  vault.deny_request(request["id"], {"ticket": _ticket(vault, request["id"])})
  assert vault.list_requests()[0]["status"] == "denied"
  with _vault_socket(vault, None) as client:
    again = vault.request_access(10, ASK)
    stale = client.post(f"/requests/{again['id']}/deny", headers={"X-Vault-Caller": "owner"},
                        json={"ticket": "stale"})
  assert stale.status_code == 409


def test_one_ask_cannot_fill_the_requests_file(vault):
  site = lambda i: {"host": f"h{i}.example.com", "methods": ["GET"],  # noqa: E731
                    "paths": [f"/{i:03d}/{j:02d}/" + "a" * 190 for j in range(20)]}
  with pytest.raises(kv.VaultError, match="fewer sites") as refused:
    vault.request_access(9, {"key": "big", "reason": "r" * 300, "sites": [site(i) for i in range(50)]})
  assert refused.value.status == 400
  modest = [{"host": f"h{i}.example.com", "methods": ["GET"], "paths": ["/v1/*"]} for i in range(20)]
  for n in range(kv.MAX_REQUESTS_PER_APP):
    vault.request_access(9, {"key": f"k{n}", "reason": "r" * 1000, "sites": modest})
  assert vault.requests_path.stat().st_size < 100_000


# -- the socket's own input -------------------------------------------------------

@pytest.mark.parametrize("length", [b"-1", b"abc", b"1e3"])
def test_an_admin_request_with_a_bad_content_length_is_refused_at_once(vault, length):
  with _vault_server(vault, None) as path:
    answer = _raw_exchange(path, b"PUT /keys/K2 HTTP/1.1\r\nHost: v\r\nX-Vault-Caller: owner\r\n"
                                 b"Content-Length: " + length + b"\r\n\r\n" + b" " * 4096, timeout=3)
  assert answer.startswith(b"HTTP/1.1 400") and b"Content-Length" in answer


def test_an_invalid_key_name_is_logged_as_invalid_not_as_its_text(vault, client):
  for caller, status in (("app:7", 403), ("agent:c1", 404)):
    answer = client.get("/use/bad%20name%7B%7D/echo.example.com/anything/x", headers={"X-Vault-Caller": caller})
    assert answer.status_code == status and "bad name" not in answer.text
  logged = [e for e in vault.usage(limit=None) if e.get("method") == "GET"]
  assert {e["key"] for e in logged} == {"(invalid name)"}
  assert "bad name" not in vault.usage_path.read_text()


# -- pausing an app on a key (the "Used by" switch) ---------------------------

def _paused(vault, app_id, paused, *, actor="owner"):
  [record] = [k for k in vault.list_keys() if k["name"] == "echo"]
  apps = [{**grant, "paused": paused} if grant["id"] == app_id else grant for grant in record["grants"]["apps"]]
  vault.put_key("echo", {"grants": {**record["grants"], "apps": apps}}, actor=actor)


def test_a_paused_app_is_refused_like_an_app_without_a_grant_and_the_owner_sees_why(vault, client):
  _paused(vault, WEATHER, True)
  paused = client.get("/use/echo/echo.example.com/anything/x", headers={"X-Vault-Caller": "app:7"})
  unknown = client.get("/use/no-such-key/echo.example.com/anything/x", headers={"X-Vault-Caller": "app:7"})
  # The app hears exactly what it hears for a key it was never given.
  assert paused.status_code == unknown.status_code == 403
  assert paused.json() == unknown.json() == {"error": "denied", "reason": "app:7 may not use this key"}
  assert _Echo.hits == []
  [logged] = vault.usage(key="echo", limit=1)
  assert logged["outcome"] == "denied" and logged["reason"] == "app:7 is paused for this key"


def test_pausing_keeps_the_apps_sites_and_unpausing_restores_use_without_a_new_request(vault):
  _paused(vault, WEATHER, True)
  [record] = vault.list_keys()
  assert record["grants"]["apps"] == [{"id": WEATHER, "sites": [ECHO_SITE], "paused": True}]
  with pytest.raises(kv.VaultError, match="may not use"):
    vault.authorize("echo", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")
  # The app can tell the owner it was switched off; asking queues nothing.
  ask = {"key": "echo", "reason": "Forecast", "sites": [ECHO_SITE]}
  assert vault.request_access(WEATHER, ask) == {"status": "paused", "key": "echo"}
  assert vault.list_requests() == []
  _paused(vault, WEATHER, False)
  assert vault.authorize("echo", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")[1] == DUMMY
  assert vault.request_access(WEATHER, ask) == {"status": "granted", "key": "echo"}


def test_a_paused_app_asking_for_more_sites_still_hears_paused(vault):
  _paused(vault, WEATHER, True)
  extra = {"host": "tiles.example.com", "methods": ["GET"], "paths": ["/*"]}
  answer = vault.request_access(WEATHER, {"key": "echo", "reason": "Tiles", "sites": [ECHO_SITE, extra]})
  assert answer["status"] == "paused" and answer["sites_not_allowed"] == [extra]
  assert answer["sites_request"] == "pending"
  # Approving the sites leaves the switch where the owner put it.
  [request] = vault.list_requests()
  vault.approve_request(request["id"], {"sites": [extra], "ticket": request["ticket"]})
  [record] = vault.list_keys()
  assert record["grants"]["apps"][0]["paused"] is True
  with pytest.raises(kv.VaultError, match="may not use"):
    vault.authorize("echo", ("app", str(WEATHER)), "GET", "tiles.example.com", "/a.png")


def test_an_agent_may_pause_an_app_but_only_the_owner_switches_it_back_on(vault):
  _paused(vault, WEATHER, True, actor="agent:c1")
  with pytest.raises(kv.VaultError, match="switch back on") as refused:
    _paused(vault, WEATHER, False, actor="agent:c1")
  assert refused.value.status == 403
  _paused(vault, WEATHER, False)
  before = kv.validate_grants(_grants())
  paused = kv.validate_grants(_grants(apps=[{"id": WEATHER, "sites": [ECHO_SITE], "paused": True}]))
  assert kv.widened_apps(before, paused) == [] and kv.widened_apps(paused, before) == [WEATHER]


def test_paused_must_be_true_or_false(vault):
  for paused in ("yes", 1, "false"):
    with pytest.raises(kv.VaultError, match="true or false"):
      kv.validate_grants({"apps": [{"id": 3, "paused": paused}]})
  assert kv.validate_grants({"apps": [{"id": 3, "paused": None}]})["apps"][0]["paused"] is False


def test_a_store_saved_before_pausing_existed_loads_every_app_live(tmp_path, vault):
  payload = json.loads(vault.store_path.read_text())
  for grant in payload["keys"]["echo"]["grants"]["apps"]:
    del grant["paused"]
  vault.store_path.write_text(json.dumps(payload))
  reloaded = kv.Vault(vault.dir)
  [record] = reloaded.list_keys()
  assert record["grants"]["apps"] == _apps(WEATHER)
  assert reloaded.authorize("echo", ("app", str(WEATHER)), "GET", "echo.example.com", "/anything/x")[1] == DUMMY


# -- agents ask for a key on a new site, by card -------------------------------

TILES = {"host": "tiles.example.com", "methods": ["GET"], "paths": ["/*"]}


def _agent_ask(**overrides):
  return {"key": "echo", "reason": "Fetch map tiles", "sites": [TILES], **overrides}


def test_agents_ask_once_per_key_and_approval_adds_the_owners_sites_and_switches_agents_on(vault):
  vault.put_key("echo", {"grants": _grants(agents=False)})
  first = vault.request_for_agents(_agent_ask(), actor="agent:c1")
  assert first == {"status": "pending", "key": "echo", "id": "agent-echo", "created": True}
  # Another chat asking for the same key joins the same card.
  assert vault.request_for_agents(_agent_ask(), actor="agent:c2") == {
    "status": "pending", "key": "echo", "id": "agent-echo",
  }
  [card] = vault.list_requests()
  assert card["requester"] == "agent" and card["app"] is None and card["app_name"] == "Your agent"
  assert card["key_exists"] is True and card["granted"] is False and card["sites"] == [TILES]
  vault.approve_request("agent-echo", {"sites": card["sites"], "ticket": card["ticket"]})
  [record] = vault.list_keys()
  assert record["grants"]["agents"] is True and record["grants"]["sites"] == [ECHO_SITE, TILES]
  # Apps gain nothing from the agents' card.
  assert record["grants"]["apps"] == _apps(WEATHER)
  vault.authorize("echo", ("agent", "c2"), "GET", "tiles.example.com", "/a.png")
  assert vault.request_for_agents(_agent_ask(), actor="agent:c1") == {"status": "granted", "key": "echo"}
  outcomes = [(e["caller"], e["outcome"]) for e in vault.usage(key="echo") if e["outcome"] != "updated"][:2]
  assert outcomes == [("owner", "granted agents"), ("agent:c1", "requested")]


def test_approving_an_agents_ask_for_a_new_key_creates_it_for_the_owner_and_agents(vault):
  asked = vault.request_for_agents(_agent_ask(key="tiles", inject={"type": "query", "name": "key"}),
                                   actor="agent:c1")
  [card] = vault.list_requests()
  assert card["key_exists"] is False and card["inject"] == {"type": "query", "name": "key"}
  with pytest.raises(kv.VaultError, match="needs a value"):
    vault.approve_request(asked["id"], {"ticket": card["ticket"], "sites": [TILES]})
  vault.approve_request(asked["id"], {"value": DUMMY, "ticket": card["ticket"], "sites": [TILES]})
  [record] = [k for k in vault.list_keys() if k["name"] == "tiles"]
  assert record["inject"] == kv.DEFAULT_INJECT
  assert record["grants"] == {"apps": [], "agents": True, "sites": [TILES]}
  assert vault.list_requests() == []


def test_an_agent_partly_covered_keeps_its_sites_and_hears_which_are_missing(vault):
  answer = vault.request_for_agents(_agent_ask(sites=[ECHO_SITE, TILES]), actor="agent:c1")
  assert answer == {"status": "granted", "key": "echo", "id": "agent-echo",
                    "sites_not_allowed": [TILES], "sites_request": "pending", "created": True}
  [card] = vault.list_requests()
  assert card["granted"] is True and card["sites"] == [TILES]
  # The owner adding the site on the key's page settles the card too.
  vault.put_key("echo", {"grants": _grants(sites=[ECHO_SITE, TILES])})
  assert vault.list_requests() == []


def test_denying_an_agents_ask_answers_denied_until_the_owner_forgets_it(vault):
  vault.request_for_agents(_agent_ask(), actor="agent:c1")
  [card] = vault.list_requests()
  vault.deny_request("agent-echo", {"ticket": card["ticket"]})
  assert vault.request_for_agents(_agent_ask(), actor="agent:c1")["sites_request"] == "denied"
  assert vault.forget_request("agent-echo")
  assert [e["outcome"] for e in vault.usage(key="echo")][:2] == ["forgot request agents", "denied agents"]


def test_the_owners_agents_together_hold_at_most_ten_open_requests(vault):
  for index in range(kv.MAX_REQUESTS_PER_APP):
    vault.request_for_agents(_agent_ask(key=f"k{index}"), actor=f"agent:c{index}")
  with pytest.raises(kv.VaultError, match="Your agents have too many") as refused:
    vault.request_for_agents(_agent_ask(key="one-more"), actor="agent:other")
  assert refused.value.status == 429
  # Apps keep their own allowance.
  assert vault.request_access(9, ASK)["status"] == "pending"


def test_an_agents_request_survives_a_restart_and_older_requests_load_as_apps(vault):
  vault.request_for_agents(_agent_ask(), actor="agent:c1")
  vault.request_access(9, ASK)
  payload = json.loads(vault.requests_path.read_text())
  del payload["requests"]["9-maps"]["requester"]  # saved before agents could ask
  vault.requests_path.write_text(json.dumps(payload))
  reloaded = kv.Vault(vault.dir)
  assert not list(vault.dir.glob("requests.json.damaged-*"))
  assert {r["id"]: r["requester"] for r in reloaded.list_requests()} == {"agent-echo": "agent", "9-maps": "app"}
  # A purge touches app requests only.
  reloaded.forget_app(9)
  assert [r["id"] for r in reloaded.list_requests()] == ["agent-echo"]


def test_an_agents_request_names_the_chat_that_asked_and_another_chat_does_not_move_it(vault):
  """The card is answered in the chat whose agent asked, so the request
  keeps that chat; a second chat asking for the same key joins the card."""
  vault.request_for_agents(_agent_ask(), actor="agent:chat-1")
  vault.request_for_agents(_agent_ask(), actor="agent:chat-2")
  vault.request_for_agents(_agent_ask(key="tiles"), actor="agent")
  vault.request_access(9, ASK)
  chats = {r["id"]: r["chat_id"] for r in vault.list_requests()}
  assert chats == {"agent-echo": "chat-1", "agent-tiles": None, "9-maps": None}
  assert all("chat" not in r for r in vault.list_requests())
  # It survives a restart, and a request stored before chats were kept has none.
  payload = json.loads(vault.requests_path.read_text())
  del payload["requests"]["agent-tiles"]["chat"]
  vault.requests_path.write_text(json.dumps(payload))
  reloaded = kv.Vault(vault.dir)
  assert {r["id"]: r["chat_id"] for r in reloaded.list_requests()} == chats


def test_an_agent_asks_for_sites_over_the_socket(vault, client):
  agent = {"X-Vault-Caller": "agent:c1"}
  asked = client.post("/requests", json=_agent_ask(), headers=agent)
  assert asked.json()["id"] == "agent-echo", asked.text
  [card] = client.get("/requests").json()["requests"]
  assert client.post("/requests/agent-echo/approve", headers=agent,
                     json={"ticket": card["ticket"], "sites": [TILES]}).status_code == 403
  approved = client.post("/requests/agent-echo/approve", headers={"X-Vault-Caller": "owner"},
                         json={"ticket": card["ticket"], "sites": [TILES]})
  assert approved.json()["granted"] == "agents"


# -- agents never widen where a key goes themselves ----------------------------

def test_an_agent_cannot_add_sites_for_itself_at_the_vault(vault):
  with pytest.raises(kv.VaultError, match="ask with the request_key tool") as refused:
    vault.put_key("echo", {"grants": _grants(sites=[ECHO_SITE, TILES])}, actor="agent:c1")
  assert refused.value.status == 403
  with pytest.raises(kv.VaultError, match="ask with the request_key tool"):
    vault.put_key("fresh", {"value": DUMMY, "inject": HEADER_SLOT, "grants": {"agents": True, "sites": [TILES]}},
                  actor="agent:c1")
  # Setting a key up without sites, narrowing and pausing stay the agents'.
  vault.put_key("fresh", {"value": DUMMY, "inject": HEADER_SLOT, "grants": {"agents": True}}, actor="agent:c1")
  vault.put_key("echo", {"grants": _grants(sites=[])}, actor="agent:c1")
  [record] = [k for k in vault.list_keys() if k["name"] == "echo"]
  assert record["grants"]["sites"] == []


def test_an_agent_refused_a_key_is_pointed_to_asking_and_others_are_not(vault, client):
  path = "/use/echo/tiles.example.com/a.png"
  agent = client.get(path, headers={"X-Vault-Caller": "agent:c1"})
  assert agent.status_code == 403 and agent.json()["ask"] == "the request_key tool"
  missing = client.get("/use/nothing/tiles.example.com/a.png", headers={"X-Vault-Caller": "agent:c1"})
  assert missing.status_code == 404 and missing.json()["ask"] == "the request_key tool"
  for caller in ("app:7", "owner"):
    assert "ask" not in client.get(path, headers={"X-Vault-Caller": caller}).json()
  # A request the relay cannot carry is no reason to ask the owner.
  bad = client.get("/use/echo/echo.example.com/anything/%2e%2e/x", headers={"X-Vault-Caller": "agent:c1"})
  assert bad.status_code == 403 and "ask" not in bad.json()
  assert _Echo.hits == []
