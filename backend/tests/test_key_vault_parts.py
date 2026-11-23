"""Keys with several parts: each part sealed on its own, sent by its recipe.

A key holds named parts (secret ones sealed, plain ones such as an id kept as
text) and a send recipe: one header or query parameter as before, ``basic``
for a username and password, or ``slots`` placing parts in headers, query
parameters and a JSON or form body. Callers never choose where a part goes
or see a secret part come back.
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest


VAULT_PATH = Path(__file__).parents[1] / "vault" / "keyvault.py"
SPEC = importlib.util.spec_from_file_location("mobius_key_vault_parts", VAULT_PATH)
kv = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = kv
SPEC.loader.exec_module(kv)

SECRET = "fake-secret-0123456789/abc+def"
OTHER_SECRET = "fake-token-9876543210-zyx"
ACCOUNT = "AC-fake-account-id"
SITE = {"host": "echo.example.com", "methods": ["GET", "POST"], "paths": ["/anything/*"]}
INSTALL = f"{7:032x}"  # app 7's installation id
APP = ("app", f"7:{INSTALL}")  # app 7 as the vault's caller
GRANTS = {"apps": [{"id": 7, "install": INSTALL, "sites": [SITE]}], "agents": True, "sites": [SITE]}
BASIC = {"type": "basic", "username": "account", "password": "token"}
BASIC_PARTS = [
  {"name": "account", "value": ACCOUNT, "secret": False, "label": "Account SID"},
  {"name": "token", "value": SECRET, "label": "Auth token"},
]


class _Echo(BaseHTTPRequestHandler):
  """Answers with everything it received: method, target, headers and body."""

  def log_message(self, *args):
    pass

  def _answer(self):
    size = int(self.headers.get("Content-Length") or 0)
    body = self.rfile.read(size).decode() if size else None
    payload = json.dumps({
      "method": self.command, "path": self.path, "headers": dict(self.headers), "body": body,
    }).encode()
    self.send_response(200)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(payload)))
    self.end_headers()
    self.wfile.write(payload)

  do_GET = do_POST = _answer


@pytest.fixture
def vault(tmp_path):
  return kv.Vault(tmp_path / "vault")


@pytest.fixture
def client(vault):
  upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
  threading.Thread(target=upstream.serve_forever, daemon=True).start()
  port = upstream.server_address[1]
  short_dir = Path(tempfile.mkdtemp(prefix="kvp-"))
  socket_path = short_dir / kv.SOCKET_NAME
  connect = staticmethod(lambda host, timeout: http.client.HTTPConnection("127.0.0.1", port, timeout=timeout))
  server = kv.VaultServer(str(socket_path), type("PartsVault", (kv.VaultHandler,), {"vault": vault, "connect": connect}))
  threading.Thread(target=server.serve_forever, daemon=True).start()
  try:
    with httpx.Client(transport=httpx.HTTPTransport(uds=str(socket_path)), base_url="http://vault") as session:
      yield session
  finally:
    server.shutdown()
    server.server_close()
    upstream.shutdown()
    shutil.rmtree(short_dir, ignore_errors=True)


def _borrow(client, key, *, method="GET", path="/anything/x", caller=f"app:7:{INSTALL}", **kwargs):
  headers = {"X-Vault-Caller": caller, **kwargs.pop("headers", {})}
  return client.request(method, f"/use/{key}/echo.example.com{path}", headers=headers, **kwargs)


def _seen(vault, key, monkeypatch, **borrow):
  """What the site received, read before redaction (the vault's own request)."""
  sent = {}
  build = kv.build_upstream_request

  def spy(**kwargs):
    request, body = build(**kwargs)
    sent.update(request=request, body=body)
    return request, body
  monkeypatch.setattr(kv, "build_upstream_request", spy)
  return sent


# -- storage ---------------------------------------------------------------------

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "key_vault_store_v3.json").read_text())


def test_a_store_the_previous_vault_wrote_opens_with_every_value(tmp_path):
  folder = tmp_path / "vault"
  folder.mkdir()
  (folder / "master.key").write_bytes(bytes.fromhex(FIXTURE["master_key_hex"]))
  (folder / "keys.json").write_text(json.dumps(FIXTURE["store"]))
  v = kv.Vault(folder)
  for name, value in FIXTURE["values"].items():
    assert v.reveal(name, ("owner", "")) == value
  listed = {key["name"]: key for key in v.list_keys()}
  assert listed["maps"]["parts"] == [{"name": "value", "label": "", "secret": True, "multiline": True}]
  assert listed["weather"]["grants"]["apps"][0]["install"] == f"{7:032x}"
  # Loading it rewrote nothing, and no copy was made: it is still what the previous vault reads.
  assert json.loads((folder / "keys.json").read_text()) == FIXTURE["store"]
  assert sorted(p.name for p in folder.glob("keys*")) == ["keys.json"]


def test_the_store_stays_readable_by_an_older_vault_until_a_key_needs_parts(tmp_path):
  v = kv.Vault(tmp_path / "vault")
  v.put_key("echo", {"value": SECRET, "inject": {"type": "header", "name": "X-Key"}, "grants": GRANTS})
  stored = json.loads(v.store_path.read_text())
  assert stored["version"] == 3 and set(stored["keys"]["echo"]) >= {"nonce", "sealed"}
  v.put_key("pair", {"parts": BASIC_PARTS, "inject": BASIC, "grants": GRANTS})
  assert json.loads(v.store_path.read_text())["version"] == kv.STORE_VERSION == 4
  v.revoke_key("pair")
  assert json.loads(v.store_path.read_text())["version"] == 3
  reopened = kv.Vault(tmp_path / "vault")
  assert reopened.reveal("echo", ("owner", "")) == SECRET


def test_a_damaged_old_record_fails_only_its_own_use(tmp_path):
  folder = tmp_path / "vault"
  v = kv.Vault(folder)
  v.put_key("good", {"value": SECRET, "inject": {"type": "header", "name": "X-Key"}, "grants": GRANTS})
  v.put_key("bad", {"value": OTHER_SECRET, "inject": {"type": "header", "name": "X-Key"}, "grants": GRANTS})
  store = json.loads(v.store_path.read_text())
  del store["keys"]["bad"]["sealed"]
  v.store_path.write_text(json.dumps(store))
  reopened = kv.Vault(folder)
  assert reopened.reveal("good", ("owner", "")) == SECRET
  with pytest.raises(kv.VaultError, match="can't be decrypted"):
    reopened.reveal("bad", ("owner", ""))


def test_each_part_is_sealed_under_its_own_name(vault):
  vault.put_key("pair", {"parts": BASIC_PARTS, "inject": BASIC, "grants": GRANTS})
  stored = vault.store_path.read_text()
  assert SECRET not in stored and ACCOUNT in stored  # a plain part is kept as text
  vault.put_key("other", {"parts": [{"name": "token", "value": OTHER_SECRET}, {"name": "account", "value": "x", "secret": False}],
                          "inject": BASIC, "grants": GRANTS})
  # A part sealed for another key or another part never opens in its place.
  vault._records["pair"]["parts"][1]["sealed"] = vault._records["other"]["parts"][0]["sealed"]
  vault._records["pair"]["parts"][1]["nonce"] = vault._records["other"]["parts"][0]["nonce"]
  with pytest.raises(kv.VaultError, match="can't be decrypted"):
    vault.reveal("pair", ("owner", ""), "token")


def test_secret_parts_are_never_listed_but_plain_ones_are(vault):
  vault.put_key("pair", {"parts": BASIC_PARTS, "inject": BASIC, "grants": GRANTS})
  [listed] = vault.list_keys()
  assert listed["parts"] == [
    {"name": "account", "label": "Account SID", "secret": False, "text": ACCOUNT},
    {"name": "token", "label": "Auth token", "secret": True, "multiline": False},
  ]
  assert SECRET not in json.dumps(listed)
  assert listed["multiline"] is False


def test_reading_names_the_part_and_follows_the_usual_read_rules(vault):
  vault.put_key("pair", {"parts": BASIC_PARTS, "inject": BASIC, "grants": GRANTS})
  assert vault.reveal("pair", ("agent", "c1"), "token") == SECRET
  assert vault.reveal("pair", ("owner", ""), "account") == ACCOUNT
  with pytest.raises(kv.VaultError, match="no part named value") as missing:
    vault.reveal("pair", ("owner", ""))
  assert missing.value.status == 404
  with pytest.raises(kv.VaultError, match="Apps cannot read"):
    vault.reveal("pair", APP, "token")
  vault.put_key("pair", {"grants": {**GRANTS, "agents": False}})
  with pytest.raises(kv.VaultError, match="switched off"):
    vault.reveal("pair", ("agent", "c1"), "token")
  assert any(e["outcome"] == "revealed" and e.get("reason") == "part token" for e in vault.usage(key="pair"))


def test_replacing_one_part_keeps_the_others(vault):
  vault.put_key("pair", {"parts": BASIC_PARTS, "inject": BASIC, "grants": GRANTS})
  vault.put_key("pair", {"parts": [{"name": "token", "value": OTHER_SECRET}]})
  assert vault.reveal("pair", ("owner", ""), "token") == OTHER_SECRET
  assert vault.reveal("pair", ("owner", ""), "account") == ACCOUNT
  [listed] = vault.list_keys()
  assert [(p["name"], p["label"], p["secret"]) for p in listed["parts"]] == [
    ("account", "Account SID", False), ("token", "Auth token", True),
  ]
  assert listed["grants"] == kv.validate_grants(GRANTS)


def test_parts_the_recipe_sends_cannot_be_removed(vault):
  vault.put_key("pair", {"parts": [*BASIC_PARTS, {"name": "spare", "value": "x"}], "inject": BASIC, "grants": GRANTS})
  with pytest.raises(kv.VaultError, match="sent with token"):
    vault.put_key("pair", {"remove_parts": ["token"]})
  vault.put_key("pair", {"remove_parts": ["spare"]})
  assert [p["name"] for p in vault.list_keys()[0]["parts"]] == ["account", "token"]
  with pytest.raises(kv.VaultError, match="no part nope"):
    vault.put_key("pair", {"remove_parts": ["nope"]})


@pytest.mark.parametrize("body, message", [
  ({"parts": [{"name": "account", "value": ACCOUNT, "secret": False}],
    "inject": {"type": "query", "name": "id"}}, "at least one secret part"),
  ({"parts": BASIC_PARTS, "inject": {"type": "header", "name": "X-Key"}}, "sent with value"),
  ({"parts": [{"name": "a", "value": "1"}, {"name": "a", "value": "2"}],
    "inject": {"type": "header", "name": "X-Key"}}, "given twice"),
  ({"parts": [{"name": f"p{i}", "value": "v"} for i in range(9)],
    "inject": {"type": "header", "name": "X-Key"}}, "at most 8 parts"),
  ({"parts": [{"name": "id", "value": "two\nlines", "secret": False}, {"name": "value", "value": "s"}],
    "inject": {"type": "header", "name": "X-Key"}}, "one line"),
  ({"parts": BASIC_PARTS, "inject": {"type": "basic", "username": "token", "password": "token"}}, "two different"),
  ({"parts": BASIC_PARTS, "inject": {"type": "slots", "slots": [
    {"type": "header", "name": "Host", "template": "{token}"}]}}, "can't carry a key"),
  ({"parts": BASIC_PARTS, "inject": {"type": "slots", "slots": [
    {"type": "header", "name": "X-A", "template": "no parts"}]}}, "at least one part"),
  ({"parts": BASIC_PARTS, "inject": {"type": "slots", "slots": [
    {"type": "header", "name": "X-A", "template": "{token} {"}]}}, "braces only"),
  ({"parts": BASIC_PARTS, "inject": {"type": "slots", "slots": [
    {"type": "body", "format": "json", "field": "a", "part": "token"},
    {"type": "body", "format": "form", "field": "b", "part": "account"}]}}, "one format"),
  ({"parts": BASIC_PARTS, "inject": {"type": "slots", "slots": [
    {"type": "query", "name": "k", "part": "token"},
    {"type": "query", "name": "k", "part": "account"}]}}, "same header or query"),
  ({"parts": BASIC_PARTS, "inject": {"type": "slots", "slots": []}}, "between one and"),
])
def test_invalid_parts_and_recipes_are_refused(vault, body, message):
  with pytest.raises(kv.VaultError, match=message):
    vault.put_key("bad", {**body, "grants": GRANTS})
  assert vault.list_keys() == []


# -- sending ---------------------------------------------------------------------

def test_basic_sends_the_pair_and_redacts_it_from_the_answer(vault, client, monkeypatch):
  vault.put_key("pair", {"parts": BASIC_PARTS, "inject": BASIC, "grants": GRANTS})
  sent = _seen(vault, "pair", monkeypatch)
  answer = _borrow(client, "pair", headers={"Authorization": "Basic attacker"})
  assert answer.status_code == 200
  expected = "Basic " + base64.b64encode(f"{ACCOUNT}:{SECRET}".encode()).decode()
  assert [v for k, v in sent["request"].headers if k.lower() == "authorization"] == [expected]
  assert answer.json()["headers"]["Authorization"] == "Basic [redacted by key vault]"
  assert SECRET not in answer.text


def test_slots_place_several_parts_and_drop_the_callers_copies(vault, client, monkeypatch):
  vault.put_key("algolia", {
    "parts": [{"name": "app", "value": ACCOUNT, "secret": False}, {"name": "secret", "value": SECRET}],
    "inject": {"type": "slots", "slots": [
      {"type": "header", "name": "X-App-Id", "template": "{app}"},
      {"type": "header", "name": "X-Api-Key", "template": "Key {secret}"},
      {"type": "query", "name": "app", "part": "app"},
    ]},
    "grants": GRANTS,
  })
  sent = _seen(vault, "algolia", monkeypatch)
  answer = _borrow(client, "algolia", path="/anything/x?app=attacker&q=1",
                   headers={"X-Api-Key": "attacker", "x-app-id": "attacker"})
  headers = [(k.lower(), v) for k, v in sent["request"].headers]
  assert [v for k, v in headers if k == "x-app-id"] == [ACCOUNT]
  assert [v for k, v in headers if k == "x-api-key"] == [f"Key {SECRET}"]
  assert sent["request"].target == f"/anything/x?q=1&app={ACCOUNT}"
  echoed = answer.json()
  # The whole header the vault built around the secret is hidden, not just the secret.
  assert echoed["headers"]["X-Api-Key"] == "[redacted by key vault]"
  assert echoed["headers"]["X-App-Id"] == ACCOUNT  # plain parts are not redacted
  assert "attacker" not in answer.text and SECRET not in answer.text


def _json_body_key(vault):
  vault.put_key("plaid", {
    "parts": [{"name": "client", "value": ACCOUNT, "secret": False}, {"name": "secret", "value": SECRET}],
    "inject": {"type": "slots", "slots": [
      {"type": "body", "format": "json", "field": "client_id", "part": "client"},
      {"type": "body", "format": "json", "field": "secret", "part": "secret"},
    ]},
    "grants": GRANTS,
  })


def test_a_json_body_gets_its_fields_with_everything_else_as_written(vault, client, monkeypatch):
  _json_body_key(vault)
  sent = _seen(vault, "plaid", monkeypatch)
  body = b'{"amount": 1.10, "big": 12345678901234567890, "client_id": "attacker", "n": {"a": [1, true, null]}}'
  answer = _borrow(client, "plaid", method="POST", content=body,
                   headers={"Content-Type": "application/json; charset=utf-8"})
  assert answer.status_code == 200
  assert sent["body"] == (
    '{"amount":1.10,"big":12345678901234567890,"n":{"a":[1,true,null]},'
    f'"client_id":"{ACCOUNT}","secret":"{SECRET}"}}'
  ).encode()
  echoed = answer.json()
  assert int(echoed["headers"]["Content-Length"]) == len(sent["body"])
  assert SECRET not in answer.text and "attacker" not in answer.text


def test_no_body_becomes_one_carrying_just_the_fields(vault, client, monkeypatch):
  _json_body_key(vault)
  sent = _seen(vault, "plaid", monkeypatch)
  answer = _borrow(client, "plaid", method="POST")
  assert answer.status_code == 200
  assert json.loads(sent["body"]) == {"client_id": ACCOUNT, "secret": SECRET}
  assert ("Content-Type", "application/json") in sent["request"].headers


@pytest.mark.parametrize("body, content_type, message", [
  (b'{"a": 1, "a": 2}', "application/json", "names a member twice"),
  (b'[1, 2]', "application/json", "top level is an object"),
  (b'{"a": NaN}', "application/json", "not valid JSON"),
  (b'{"a": 1}', "text/plain", "Content-Type: application/json"),
  (b'{"a": 1}', None, "Content-Type: application/json"),
])
def test_a_body_the_vault_cannot_place_fields_in_is_refused(vault, client, body, content_type, message):
  _json_body_key(vault)
  headers = {"Content-Type": content_type} if content_type else {}
  answer = _borrow(client, "plaid", method="POST", content=body, headers=headers)
  assert answer.status_code == 400 and message in answer.json()["reason"]
  assert vault.usage(key="plaid")[0]["outcome"] == "denied"


def test_a_form_body_gets_its_field_and_keeps_the_rest(vault, client, monkeypatch):
  vault.put_key("oauth", {
    "parts": [{"name": "value", "value": SECRET}],
    "inject": {"type": "slots", "slots": [{"type": "body", "format": "form", "field": "client_secret", "part": "value"}]},
    "grants": GRANTS,
  })
  sent = _seen(vault, "oauth", monkeypatch)
  answer = _borrow(client, "oauth", method="POST", content=b"grant_type=client_credentials&client_secret=x&scope=a+b",
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
  assert answer.status_code == 200
  assert sent["body"] == b"grant_type=client_credentials&scope=a+b&client_secret=" + \
    SECRET.replace("/", "%2F").replace("+", "%2B").encode()
  assert SECRET not in answer.text and "%2F" not in answer.text.split("client_secret=")[-1][:40]


def test_a_part_with_line_breaks_never_goes_in_a_header_but_may_go_in_a_body(vault):
  pem = "-----BEGIN FAKE-----\nabc\n-----END FAKE-----"
  vault.put_key("hdr", {"parts": [{"name": "value", "value": pem}],
                        "inject": {"type": "slots", "slots": [{"type": "header", "name": "X-K", "template": "{value}"}]},
                        "grants": GRANTS})
  with pytest.raises(kv.VaultError, match="line breaks"):
    vault.authorize("hdr", APP, "GET", "echo.example.com", "/anything/x")
  vault.put_key("body", {"parts": [{"name": "value", "value": pem}],
                         "inject": {"type": "slots", "slots": [{"type": "body", "format": "json", "field": "k", "part": "value"}]},
                         "grants": GRANTS})
  loan = vault.authorize("body", APP, "POST", "echo.example.com", "/anything/x")
  assert loan.values == {"value": pem}


def test_only_the_parts_a_recipe_sends_are_opened(vault):
  vault.put_key("pair", {"parts": [*BASIC_PARTS, {"name": "spare", "value": OTHER_SECRET}], "inject": BASIC, "grants": GRANTS})
  loan = vault.authorize("pair", ("agent", "c1"), "GET", "echo.example.com", "/anything/x")
  assert loan.values == {"account": ACCOUNT, "token": SECRET}
  assert loan.secret == frozenset({"token"})


def test_redaction_covers_every_secret_and_the_longest_first():
  echoes = kv.secret_echoes("abc-secret-one", "abc-secret-one-and-more", "zz-second-zz")
  out = kv.redact(b"[abc-secret-one-and-more] [zz-second-zz] [abc-secret-one]", echoes)
  assert out == b"[[redacted by key vault]] [[redacted by key vault]] [[redacted by key vault]]"


# -- asking and approving -------------------------------------------------------

def _ask(**overrides):
  return {"key": "twilio", "reason": "Send texts", "sites": [SITE], "inject": BASIC,
          "parts": [{"name": "account", "label": "Account SID", "secret": False},
                    {"name": "token", "label": "Auth token"}], **overrides}


def test_a_request_proposes_parts_and_the_owner_pastes_each(vault):
  answer = vault.request_for_agents(_ask(), actor="agent:c1")
  assert answer["status"] == "pending"
  [request] = vault.list_requests()
  assert request["parts"] == [{"name": "account", "label": "Account SID", "secret": False},
                              {"name": "token", "label": "Auth token", "secret": True}]
  assert request["inject"] == BASIC
  with pytest.raises(kv.VaultError, match="each part"):
    vault.approve_request(request["id"], {"ticket": request["ticket"], "sites": [SITE], "parts": {"token": SECRET}})
  vault.approve_request(request["id"], {"ticket": request["ticket"], "sites": [SITE],
                                        "parts": {"account": ACCOUNT, "token": SECRET}})
  [key] = vault.list_keys()
  assert key["inject"] == BASIC and key["grants"]["agents"] is True
  assert [(p["name"], p["secret"]) for p in key["parts"]] == [("account", False), ("token", True)]
  assert vault.reveal("twilio", ("owner", ""), "token") == SECRET


def test_approving_an_existing_key_never_takes_part_values(vault):
  vault.put_key("twilio", {"parts": BASIC_PARTS, "inject": BASIC, "grants": {"apps": [], "agents": False, "sites": []}})
  vault.request_for_agents(_ask(), actor="agent:c1")
  [request] = vault.list_requests()
  assert request["parts"] is None  # an existing key is never re-pasted on the card
  with pytest.raises(kv.VaultError, match="already exists"):
    vault.approve_request(request["id"], {"ticket": request["ticket"], "sites": [SITE], "parts": {"token": "x"}})


@pytest.mark.parametrize("overrides, message", [
  ({"inject": None}, "Say how a key with several parts is sent"),
  ({"inject": {"type": "basic", "username": "account", "password": "missing"}}, "sends missing"),
  ({"parts": None, "inject": BASIC}, "sends account, token"),
  ({"parts": [{"name": "account", "secret": False}]}, "at least one secret part"),
])
def test_a_request_whose_recipe_does_not_fit_its_parts_is_refused(vault, overrides, message):
  with pytest.raises(kv.VaultError, match=message):
    vault.request_for_agents(_ask(**overrides), actor="agent:c1")
  assert vault.list_requests() == []


def test_a_recipe_that_does_not_fit_an_existing_key_is_never_set(vault):
  vault.put_key("twilio", {"value": SECRET, "inject": {"type": "header", "name": "X-K"},
                           "grants": {"apps": [], "agents": False, "sites": []}})
  vault.request_for_agents(_ask(parts=None), actor="agent:c1")
  [request] = vault.list_requests()
  assert request["sets_inject"] is False and request["inject_kept"] is True


# -- the sealed-card consumer ---------------------------------------------------

STORE_KEY = Path(__file__).parents[1] / "scripts" / "store-key.py"


def _store_key(monkeypatch, vault, args, fields):
  import io
  spec = importlib.util.spec_from_file_location("mobius_store_key_parts", STORE_KEY)
  store_key = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(store_key)
  short_dir = Path(tempfile.mkdtemp(prefix="kvs-"))
  server = kv.VaultServer(str(short_dir / kv.SOCKET_NAME), type("StoreKeyVault", (kv.VaultHandler,), {"vault": vault}))
  threading.Thread(target=server.serve_forever, daemon=True).start()
  try:
    monkeypatch.setattr(store_key, "VAULT_SOCKET", short_dir / kv.SOCKET_NAME)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(fields)))
    return store_key.main(args)
  finally:
    server.shutdown()
    server.server_close()
    shutil.rmtree(short_dir, ignore_errors=True)


def test_store_key_saves_each_card_field_as_a_part(monkeypatch, vault):
  fields = {"account": ACCOUNT, "token": SECRET}
  args = ["twilio", "--part", "account:plain:Account SID", "--part", "token::Auth token", "--basic", "account", "token"]
  assert _store_key(monkeypatch, vault, args, fields) == 0
  [key] = vault.list_keys()
  assert key["inject"] == BASIC
  assert key["parts"] == [
    {"name": "account", "label": "Account SID", "secret": False, "text": ACCOUNT},
    {"name": "token", "label": "Auth token", "secret": True, "multiline": False},
  ]
  # Rotating one part on the existing key keeps the other.
  assert _store_key(monkeypatch, vault, ["twilio", "--part", "token", "--replace"], {"token": OTHER_SECRET}) == 0
  assert vault.reveal("twilio", ("owner", ""), "token") == OTHER_SECRET
  assert vault.reveal("twilio", ("owner", ""), "account") == ACCOUNT


def test_store_key_takes_slots_and_refuses_a_missing_field(monkeypatch, vault):
  slots = json.dumps([{"type": "header", "name": "X-Id", "template": "{id}"},
                      {"type": "header", "name": "X-Key", "template": "{secret}"}])
  args = ["algolia", "--part", "id:plain", "--part", "secret", "--slots", slots]
  assert _store_key(monkeypatch, vault, args, {"id": ACCOUNT}) == 2
  assert vault.list_keys() == []
  assert _store_key(monkeypatch, vault, args, {"id": ACCOUNT, "secret": SECRET}) == 0
  assert vault.list_keys()[0]["inject"]["type"] == "slots"


def test_a_recipe_must_send_a_secret_part(vault):
  plain_only = {"type": "slots", "slots": [{"type": "query", "name": "id", "part": "account"}]}
  with pytest.raises(kv.VaultError, match="without any secret part"):
    vault.put_key("bad", {"parts": BASIC_PARTS, "inject": plain_only, "grants": GRANTS})
  with pytest.raises(kv.VaultError, match="no secret part"):
    vault.request_for_agents(_ask(inject=plain_only), actor="agent:c1")
  vault.put_key("pair", {"parts": [*BASIC_PARTS, {"name": "spare", "value": OTHER_SECRET}], "inject": BASIC, "grants": GRANTS})
  # Making the password it sends plain is refused too, though another part stays secret.
  with pytest.raises(kv.VaultError, match="is the secret itself"):
    vault.put_key("pair", {"parts": [{"name": "token", "value": "x", "secret": False}]})
  # A slots key whose only sent secret turns plain is sent with nothing secret.
  slots = {"type": "slots", "slots": [{"type": "header", "name": "X-Key", "template": "{key}"}]}
  vault.put_key("hdr", {"parts": [{"name": "key", "value": SECRET}, {"name": "spare", "value": OTHER_SECRET}],
                        "inject": slots, "grants": GRANTS})
  with pytest.raises(kv.VaultError, match="without any secret part"):
    vault.put_key("hdr", {"parts": [{"name": "key", "secret": False}]})


def test_with_nothing_secret_redaction_changes_nothing():
  assert kv.redact(b"an ordinary answer", kv.secret_echoes()) == b"an ordinary answer"


def test_parts_to_remove_are_named_by_text(vault):
  vault.put_key("pair", {"parts": BASIC_PARTS, "inject": BASIC, "grants": GRANTS})
  with pytest.raises(kv.VaultError, match="Name the parts"):
    vault.put_key("pair", {"remove_parts": [["token"]]})


# -- hardening found in review ------------------------------------------------------

@pytest.mark.parametrize("inject, parts", [
  (BASIC, [{"name": "account", "value": ACCOUNT, "secret": False}, {"name": "token", "value": SECRET, "secret": False},
           {"name": "spare", "value": OTHER_SECRET}]),
  ({"type": "aws_sigv4", "access_key_id": "id", "secret_access_key": "s", "region": "us-east-1", "service": "s3"},
   [{"name": "id", "value": "AKIDEXAMPLE", "secret": False}, {"name": "s", "value": SECRET, "secret": False},
    {"name": "spare", "value": OTHER_SECRET}]),
])
def test_the_part_that_is_the_secret_can_never_be_plain(vault, inject, parts):
  with pytest.raises(kv.VaultError, match="is the secret itself"):
    vault.put_key("k", {"parts": parts, "inject": inject, "grants": GRANTS})


def test_a_request_cannot_propose_the_secret_as_plain(vault):
  with pytest.raises(kv.VaultError, match="is the secret itself"):
    vault.request_for_agents(_ask(parts=[{"name": "account", "secret": False},
                                         {"name": "token", "secret": False}, {"name": "pin"}]), actor="agent:c1")


def test_a_short_secret_part_is_refused_where_an_echo_could_reveal_it(vault):
  with pytest.raises(kv.VaultError, match="shorter than 8"):
    vault.put_key("pin", {"parts": [{"name": "account", "value": ACCOUNT, "secret": False},
                                    {"name": "token", "value": "4821"}], "inject": BASIC, "grants": GRANTS})
  # A single value sent as before keeps its old rules.
  vault.put_key("legacy", {"value": "abc", "inject": {"type": "header", "name": "X-Key"}, "grants": GRANTS})


def test_a_label_or_secrecy_change_keeps_the_value(vault):
  vault.put_key("pair", {"parts": [*BASIC_PARTS, {"name": "region", "value": "eu-west-1", "secret": False}],
                         "inject": BASIC, "grants": GRANTS})
  vault.put_key("pair", {"parts": [{"name": "account", "label": "Account SID (live)"}, {"name": "region", "secret": True}]})
  listed = {p["name"]: p for p in vault.list_keys()[0]["parts"]}
  assert listed["account"]["label"] == "Account SID (live)" and listed["account"]["text"] == ACCOUNT
  assert listed["region"]["secret"] is True and "text" not in listed["region"]
  assert vault.reveal("pair", ("owner", ""), "region") == "eu-west-1"
  with pytest.raises(kv.VaultError, match="needs a value"):
    vault.put_key("pair", {"parts": [{"name": "nope", "label": "x"}]})


def test_a_label_cannot_reorder_how_the_card_reads(vault):
  with pytest.raises(kv.VaultError, match="plain text"):
    vault.put_key("k", {"parts": [{"name": "value", "value": SECRET, "label": "API key‮"}],
                        "inject": {"type": "header", "name": "X-Key"}, "grants": GRANTS})


def test_a_basic_username_with_a_colon_is_refused(vault):
  vault.put_key("pair", {"parts": [{"name": "account", "value": "a:b", "secret": False}, {"name": "token", "value": SECRET}],
                         "inject": BASIC, "grants": GRANTS})
  with pytest.raises(kv.VaultError, match="colon"):
    vault.authorize("pair", APP, "GET", "echo.example.com", "/anything/x")


@pytest.mark.parametrize("header", ["X-HTTP-Method-Override", "X-Original-URL", "Range"])
def test_headers_the_vault_strips_cannot_be_slots(header):
  with pytest.raises(kv.VaultError, match="can't carry a key"):
    kv.validate_inject({"type": "slots", "slots": [{"type": "header", "name": header, "template": "/admin/{p}"}]})


def test_asking_again_with_another_recipe_is_refused_while_one_waits(vault):
  vault.request_for_agents(_ask(inject=None, parts=None), actor="agent:c1")
  with pytest.raises(kv.VaultError, match="already waiting with another way") as refused:
    vault.request_for_agents(_ask(), actor="agent:c1")
  assert refused.value.status == 409
  assert vault.request_for_agents(_ask(inject=None, parts=None), actor="agent:c1")["status"] == "pending"


def test_a_request_whose_key_was_deleted_meanwhile_is_still_answerable(vault):
  vault.put_key("twilio", {"value": SECRET, "inject": {"type": "header", "name": "X-K"},
                           "grants": {"apps": [], "agents": False, "sites": []}})
  vault.request_for_agents(_ask(), actor="agent:c1")
  vault.revoke_key("twilio")
  [request] = vault.list_requests()
  assert [p["name"] for p in request["parts"]] == ["account", "token"]
  vault.approve_request(request["id"], {"ticket": request["ticket"], "sites": [SITE],
                                        "parts": {"account": ACCOUNT, "token": SECRET}})
  assert vault.reveal("twilio", ("owner", ""), "token") == SECRET


@pytest.mark.parametrize("body, message", [
  (b'{"a":"\\ud800"}', None),
  (b'[' * 5000 + b']' * 5000, "nested too deeply"),
])
def test_unusual_json_is_kept_or_refused_never_crashes(body, message):
  inject = kv.validate_inject({"type": "slots", "slots": [{"type": "body", "format": "json", "field": "s", "part": "s"}]})
  if message is None:
    out, _ = kv.recipe_body(inject, {"s": "x"}, body, [("Content-Type", "application/json")])
    assert json.loads(out) == {"a": "\ud800", "s": "x"}
    return
  with pytest.raises(kv.VaultError, match=message):
    kv.recipe_body(inject, {"s": "x"}, b'{"a":' + body + b'}', [("Content-Type", "application/json")])


def test_a_form_that_is_not_utf8_is_refused_not_mangled():
  inject = kv.validate_inject({"type": "slots", "slots": [{"type": "body", "format": "form", "field": "s", "part": "s"}]})
  with pytest.raises(kv.VaultError, match="as UTF-8"):
    kv.recipe_body(inject, {"s": "x"}, b"name=Ren%E9", [("Content-Type", "application/x-www-form-urlencoded")])
  with pytest.raises(kv.VaultError, match="as UTF-8"):
    kv.recipe_body(inject, {"s": "x"}, b"name=Ren%E9", [("Content-Type", "application/x-www-form-urlencoded")])


def test_a_store_whose_label_does_not_match_what_it_holds_is_rewritten_at_start(tmp_path):
  folder = tmp_path / "vault"
  v = kv.Vault(folder)
  v.put_key("echo", {"value": SECRET, "inject": {"type": "header", "name": "X-Key"}, "grants": GRANTS})
  payload = json.loads(v.store_path.read_text())
  [part] = kv.Vault(folder)._records["echo"]["parts"]
  payload["keys"]["echo"] = {**{k: val for k, val in payload["keys"]["echo"].items() if k not in ("nonce", "sealed", "multiline")},
                             "parts": [part]}
  payload["version"] = 4  # labelled 4 though every key is a single value
  v.store_path.write_text(json.dumps(payload))
  kv.Vault(folder)
  assert json.loads(v.store_path.read_text())["version"] == 3
