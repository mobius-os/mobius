"""Keys routes: apps borrow keys through an attested handshake, never read them.

A real vault runs on a short socket path with a local upstream in place of
HTTPS, so these tests exercise the whole path an app's request takes.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import zlib
import shutil
import sys
import tempfile
import threading
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from app import auth as auth_mod
from app import key_vault, models, push


VAULT_PATH = Path(__file__).parents[1] / "vault" / "keyvault.py"
SPEC = importlib.util.spec_from_file_location("mobius_key_vault_routes", VAULT_PATH)
kv = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = kv
SPEC.loader.exec_module(kv)

DUMMY = "dummy-route-key-0123456789"
SITE = {"host": "api.example.com", "methods": ["GET", "POST"], "paths": ["/v1/*"]}
# A model provider an app declares; an app's own key starts allowed there.
PROVIDER = "https://models.example.net/v1"
PROVIDER_SITE = {"host": "models.example.net", "methods": ["POST"], "paths": ["/v1/responses"]}


# Answers a site may send that must never act on the Möbius origin.
_HOSTILE_HEADERS = {
  "Clear-Site-Data": '"cookies", "storage"',
  "Refresh": "0; url=https://evil.example/",
  "Link": "<https://evil.example/x.js>; rel=preload; as=script",
  "Set-Cookie": "session=planted; Path=/",
  "X-Frame-Options": "ALLOWALL",
  "Access-Control-Allow-Origin": "*",
  "X-Vault-Denied": "1",
}
_KEPT_HEADERS = {
  "ETag": '"v1"', "Retry-After": "30", "RateLimit-Remaining": "9",
  "X-RateLimit-Limit": "10", "Content-Language": "en",
}


class _Echo(BaseHTTPRequestHandler):
  def log_message(self, *args):
    pass

  def _special(self) -> bool:
    """Answers for the header, redirect and truncation tests."""
    if self.path == "/v1/hostile-headers":
      self.send_response(200)
      for name, value in {**_HOSTILE_HEADERS, **_KEPT_HEADERS}.items():
        self.send_header(name, value)
      self.send_header("Content-Length", "2")
      self.end_headers()
      self.wfile.write(b"ok")
      return True
    if self.path.startswith("/v1/redirect-"):
      self.send_response(307)
      self.send_header("Location", {
        "/v1/redirect-relative": "/api/apps/1/storage/x",
        "/v1/redirect-path": "next?page=2",
        "/v1/redirect-absolute": "https://elsewhere.example/landing",
      }[self.path])
      self.send_header("Content-Length", "0")
      self.end_headers()
      return True
    if self.path == "/v1/truncated":
      # Promises more than it sends, then hangs up mid-answer.
      self.send_response(200)
      self.send_header("Content-Type", "text/plain")
      self.send_header("Content-Length", "100000")
      self.end_headers()
      self.wfile.write(b"x" * 1000)
      self.wfile.flush()
      self.close_connection = True
      return True
    return False

  def _answer(self):
    if self._special():
      return
    length = int(self.headers.get("Content-Length") or 0)
    body = json.dumps({
      "path": self.path, "headers": dict(self.headers),
      "body": self.rfile.read(length).decode() if length else "",
    }).encode()
    # Sites may compress even when asked for identity encoding.
    encoding = {"/v1/gzip": "gzip", "/v1/deflate": "deflate", "/v1/brotli": "br"}.get(self.path)
    if encoding == "gzip":
      body = gzip.compress(body)
    elif encoding == "deflate":
      body = zlib.compress(body)
    self.send_response(200)
    self.send_header("Content-Type", "text/html")
    self.send_header("Set-Cookie", "tracker=1")
    if encoding:
      self.send_header("Content-Encoding", encoding)
    self.send_header("Content-Length", str(len(body)))
    self.end_headers()
    self.wfile.write(body)

  do_GET = do_POST = _answer


@pytest.fixture
def vault(monkeypatch):
  upstream = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
  threading.Thread(target=upstream.serve_forever, daemon=True).start()
  # Unix socket paths are limited to ~108 bytes; pytest's tmp_path can exceed it.
  folder = Path(tempfile.mkdtemp(prefix="kvr-"))
  store = kv.Vault(folder / "store")
  handler = type("RouteVault", (kv.VaultHandler,), {
    "vault": store,
    "connect": staticmethod(lambda host, timeout: HTTPConnection(
      "127.0.0.1", upstream.server_address[1], timeout=timeout,
    )),
  })
  server = kv.VaultServer(str(folder / "vault.sock"), handler)
  threading.Thread(target=server.serve_forever, daemon=True).start()
  monkeypatch.setattr(key_vault, "vault_socket", lambda: folder / "vault.sock")
  monkeypatch.setattr(key_vault, "vault_dir", lambda: folder / "store")
  yield store
  server.shutdown()
  server.server_close()
  upstream.shutdown()
  shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
def pushes(monkeypatch):
  sent = []

  async def record(db, owner_id, **fields):
    sent.append(fields)
  monkeypatch.setattr(push, "notify_owner_async", record)
  return sent


def _app(db, slug, app_id=None, provider_key=None, base_url=PROVIDER, protocol=None):
  """An installed app; ``provider_key`` declares its model provider's secret name."""
  contract = None
  if provider_key is not None:
    contract = {"model_provider": {
      "name": slug.title(), "base_url": base_url, "secret_name": provider_key,
      "models": [{"id": f"{slug}-model"}], "default_model": f"{slug}-model",
      **({"protocol": protocol} if protocol else {}),
    }}
  app = models.App(
    id=app_id,
    source_dir=f"/tmp/mobius-tests/{slug}", name=slug.title(), slug=slug, description="test",
    jsx_source="export default function App() { return null }",
    compiled_path=f"/tmp/{slug}.js", capability_contract=contract,
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  return app


def _app_auth(db, app, **kwargs):
  owner = db.query(models.Owner).first()
  token = auth_mod.create_app_token(app.id, owner.username, owner.token_epoch, app.token_nonce, **kwargs)
  return {"Authorization": f"Bearer {token}"}


def _agent_auth(db):
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token("chat-keys", owner.username, owner.token_epoch)
  return {"Authorization": f"Bearer {token}"}


def _helper_auth(db):
  """A delegated helper's bearer, for a live delegation."""
  from app.chat_writer import create_chat
  db.add(create_chat(id="helper-chat", title="Helper"))
  db.add(create_chat(id="parent-chat", title="Parent"))
  db.add(models.Delegation(
    id="key-helper", parent_chat_id="parent-chat", parent_root_run_id="parent-run",
    task_key="key-helper", child_chat_id="helper-chat", provider="claude", scope="write",
    cwd="/tmp", prompt_sha256="0" * 64,
  ))
  db.commit()
  owner = db.query(models.Owner).first()
  token = auth_mod.create_agent_token(
    "helper-chat", owner.username, owner.token_epoch,
    delegation_id="key-helper", delegation_chat="helper-chat",
  )
  return {"Authorization": f"Bearer {token}"}


ASK = {"key": "weather_api", "reason": "Show the forecast", "sites": [SITE],
       "inject": {"type": "header", "name": "Authorization", "prefix": "Bearer "}}


def test_handshake_from_request_to_use_without_seeing_the_value(client, auth, db, vault, pushes):
  weather = _app(db, "weather")
  app_auth = _app_auth(db, weather)

  # Before approval the app is told to wait, and the owner is told once.
  assert client.post("/api/keys/requests", headers=app_auth, json=ASK).json()["status"] == "pending"
  assert client.post("/api/keys/requests", headers=app_auth, json=ASK).json()["status"] == "pending"
  assert [p["title"] for p in pushes] == ["Weather asks to use a key"]
  denied = client.get("/api/keys/use/weather_api/api.example.com/v1/today", headers=app_auth)
  # The same refusal as for a key it was not given: an app learns no key names.
  assert denied.status_code == 403 and denied.headers["X-Vault-Denied"] == "1"

  # The owner sees the request with the app's own name, then approves it.
  [request] = client.get("/api/keys", headers=auth).json()["requests"]
  assert request["app"] == weather.id and request["app_name"] == "Weather"
  # What the Settings card sends: the value plus the slot and sites it showed.
  assert client.post(f"/api/keys/requests/{request['id']}/approve", headers=auth, json={
    "value": DUMMY, "ticket": request["ticket"], "inject": request["inject"], "sites": request["sites"],
  }).status_code == 200
  assert client.post("/api/keys/requests", headers=app_auth, json=ASK).json()["status"] == "granted"

  used = client.post(
    "/api/keys/use/weather_api/api.example.com/v1/today?units=metric",
    headers={**app_auth, "Content-Type": "application/json", "X-Units": "c"},
    content=b'{"city":"Paris"}',
  )
  assert used.status_code == 200
  assert DUMMY not in used.text
  echoed = json.loads(used.content)
  echoed["headers"] = {k.title(): v for k, v in echoed["headers"].items()}
  assert echoed["path"] == "/v1/today?units=metric"
  assert echoed["body"] == '{"city":"Paris"}'
  # The vault added the key (redacted in the echo); the app's own Möbius
  # token never travelled to the third party.
  assert echoed["headers"]["Authorization"] == "Bearer [redacted by key vault]"
  assert echoed["headers"]["X-Units"] == "c"
  assert "Cookie" not in echoed["headers"]
  # The third party's answer can never act on this origin.
  assert "set-cookie" not in used.headers
  assert used.headers["Content-Security-Policy"].startswith("sandbox")
  assert used.headers["X-Content-Type-Options"] == "nosniff"
  assert vault.usage(key="weather_api")[0]["caller"] == f"app:{weather.id}"


def test_apps_never_read_values_but_the_owner_and_agents_do(client, auth, db, vault):
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [], "agents": True, "sites": [SITE]}})
  weather = _app(db, "weather")
  for headers in (_app_auth(db, weather), _app_auth(db, weather, service="private")):
    assert client.get("/api/keys/shared/value", headers=headers).status_code == 403
  assert client.get("/api/keys/shared/value", headers=auth).json()["value"] == DUMMY
  agent = client.get("/api/keys/shared/value", headers=_agent_auth(db))
  assert agent.json().get("value") == DUMMY, agent.text
  callers = [e["caller"] for e in vault.usage(key="shared") if e["outcome"] == "revealed"]
  assert callers == ["agent:chat-keys", "owner"]


def test_an_app_cannot_borrow_another_apps_grant(client, auth, db, vault):
  vault.put_key("maps", {"value": DUMMY, "inject": ASK["inject"],
                         "grants": {"apps": [], "agents": False, "sites": [SITE]}})
  weather, other = _app(db, "weather"), _app(db, "other")
  vault.put_key("maps", {"inject": ASK["inject"],
                         "grants": {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": False, "sites": [SITE]}})
  path = "/api/keys/use/maps/api.example.com/v1/x"
  # A forged vault caller header is dropped; the token decides.
  forged = {**_app_auth(db, other), "X-Vault-Caller": f"app:{weather.id}"}
  assert client.get(path, headers=forged).status_code == 403
  assert client.get(path, headers=_app_auth(db, weather)).status_code == 200


def test_an_app_asks_for_exact_sites_only(client, auth, db, vault, pushes):
  app_auth = _app_auth(db, _app(db, "tenant"))
  wildcard = {**ASK, "sites": [{**SITE, "host": "*.supabase.co"}]}
  refused = client.post("/api/keys/requests", headers=app_auth, json=wildcard)
  assert refused.status_code == 400 and "exact sites" in refused.text
  assert client.get("/api/keys", headers=auth).json()["requests"] == [] and pushes == []


def test_only_apps_ask_and_only_the_owner_manages(client, auth, db, vault):
  weather = _app(db, "weather")
  app_auth = _app_auth(db, weather)
  assert client.post("/api/keys/requests", headers=auth, json=ASK).status_code == 403
  assert client.get("/api/keys", headers=app_auth).status_code in (401, 403)
  assert client.put("/api/keys/x", headers=app_auth, json={}).status_code in (401, 403)
  asked = client.post("/api/keys/requests", headers=app_auth, json=ASK).json()
  assert client.post(f"/api/keys/requests/{asked['id']}/approve", headers=app_auth,
                     json={"value": DUMMY}).status_code in (401, 403)


def test_unavailable_vault_is_reported_not_crashed(client, auth, monkeypatch):
  monkeypatch.setattr(key_vault, "vault_socket", lambda: Path("/nonexistent/vault.sock"))
  body = client.get("/api/keys", headers=auth).json()
  assert body["available"] is False and body["keys"] == [] and body["requests"] == []
  assert client.get("/api/keys/x/value", headers=auth).status_code == 503


def test_an_agent_replacing_a_saved_key_tells_the_owner(client, auth, db, vault, pushes):
  agent = _agent_auth(db)
  body = {"value": DUMMY, "inject": ASK["inject"], "grants": {"sites": [SITE]}}
  # Setting a key up for the first time is ordinary agent work: no alert.
  assert client.put("/api/keys/fresh", headers=agent, json=body).status_code == 200
  # The owner replacing their own key is not news to them.
  assert client.put("/api/keys/fresh", headers=auth, json={**body, "value": "second-dummy-key"}).status_code == 200
  assert pushes == []
  assert client.put("/api/keys/fresh", headers=agent, json={**body, "value": "third-dummy-key"}).status_code == 200
  [notice] = pushes
  assert notice["title"] == "An agent changed a saved key"
  assert "replaced its value" in notice["body"]
  assert "fresh" in notice["body"] and "third" not in notice["body"]


def test_an_agent_adding_a_site_tells_the_owner_and_is_logged_as_the_agent(client, auth, db, vault, pushes):
  agent = _agent_auth(db)
  vault.put_key("maps", {"value": DUMMY, "inject": ASK["inject"],
                         "grants": {"apps": [], "agents": True, "sites": [SITE]}})
  [page] = client.get("/api/keys", headers=auth).json()["keys"]
  wider = {"inject": ASK["inject"], "revision": page["revision"], "grants": {"apps": [], "agents": True, "sites": [
    SITE, {"host": "other.example.net", "methods": ["POST"], "paths": ["/*"]}]}}
  assert client.put("/api/keys/maps", headers=agent, json=wider).status_code == 200
  [notice] = pushes
  assert "added sites" in notice["body"] and "replaced" not in notice["body"]
  assert vault.usage(key="maps")[0]["caller"] == "agent:chat-keys"


def test_only_the_owners_own_input_lets_an_app_use_a_key(client, auth, db, vault):
  weather = _app(db, "weather")
  vault.put_key("maps", {"value": DUMMY, "inject": ASK["inject"],
                         "grants": {"apps": [], "agents": True, "sites": [SITE]}})
  asked = client.post("/api/keys/requests", headers=_app_auth(db, weather), json={**ASK, "key": "maps"}).json()
  [card] = client.get("/api/keys", headers=auth).json()["requests"]
  agent = _agent_auth(db)
  refused = client.post(f"/api/keys/requests/{asked['id']}/approve", headers=agent,
                        json={"sites": [SITE], "ticket": card["ticket"]})
  assert refused.status_code == 403
  [listed] = [a for a in client.get("/api/apps/", headers=auth).json() if a["id"] == weather.id]
  grant = {"inject": ASK["inject"], "installations": {str(weather.id): listed["storage_generation"]},
           "grants": {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": True, "sites": [SITE]}}
  assert client.put("/api/keys/maps", headers=agent, json=grant).status_code == 403
  assert vault.list_keys()[0]["grants"]["apps"] == []
  assert client.post(f"/api/keys/requests/{asked['id']}/approve", headers=auth,
                     json={"sites": [SITE], "ticket": card["ticket"]}).status_code == 200
  assert vault.list_keys()[0]["grants"]["apps"] == [{"id": weather.id, "sites": [kv.validate_site(SITE)]}]
  # Narrowing an app's sites or taking it off a key only takes access away.
  narrowed = {"inject": ASK["inject"], "grants": {"apps": [{"id": weather.id, "sites": []}], "agents": True, "sites": [SITE]}}
  assert client.put("/api/keys/maps", headers=agent, json=narrowed).status_code == 200
  assert client.put("/api/keys/maps", headers=agent, json={**narrowed, "grants": {**narrowed["grants"], "apps": []}}).status_code == 200


def test_a_granted_app_asks_for_more_sites_and_the_owner_is_told(client, auth, db, vault, pushes):
  weather = _app(db, "weather")
  vault.put_key("maps", {"value": DUMMY, "inject": ASK["inject"],
                         "grants": {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": False, "sites": []}})
  tiles = {"host": "tiles.example.com", "methods": ["GET"], "paths": ["/*"]}
  ask = {"key": "maps", "reason": "Map tiles", "sites": [SITE, tiles]}
  answer = client.post("/api/keys/requests", headers=_app_auth(db, weather), json=ask).json()
  assert answer["status"] == "granted" and answer["sites_request"] == "pending"
  assert answer["sites_not_allowed"] == [tiles] and "created" not in answer
  assert [p["title"] for p in pushes] == ["Weather asks to use a key on more sites"]
  [request] = client.get("/api/keys", headers=auth).json()["requests"]
  assert request["granted"] is True and request["sites"] == [tiles]
  assert client.post(f"/api/keys/requests/{request['id']}/approve", headers=auth,
                     json={"sites": [tiles], "ticket": request["ticket"]}).status_code == 200
  assert client.post("/api/keys/requests", headers=_app_auth(db, weather), json=ask).json() == {
    "status": "granted", "key": "maps",
  }


def test_a_slow_site_is_not_reported_as_a_stopped_vault(client, auth, db, vault, monkeypatch):
  async def slow(*args, **kwargs):
    raise key_vault.VaultTimeout("The site took too long to answer through the key vault.")
  monkeypatch.setattr(key_vault, "vault_use", slow)
  response = client.get("/api/keys/use/maps/api.example.com/v1/x", headers=auth)
  assert response.status_code == 504 and "too long" in response.text


def test_guest_lineage_cannot_read_or_use_keys(client, auth, db, vault):
  from app.browser_access import BrowserLineage
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [], "agents": True, "sites": [SITE]}})
  owner = db.query(models.Owner).first()
  smuggled = auth_mod.create_access_token(
    {"sub": owner.username}, token_epoch=owner.token_epoch,
    browser=BrowserLineage("g" * 32, "s" * 32),
  )
  response = client.get("/api/keys/shared/value", headers={"Authorization": f"Bearer {smuggled}"})
  assert response.status_code in (401, 403) and DUMMY not in response.text


def test_guest_lineage_cannot_read_an_apps_secret_through_the_old_route(client, auth, db, vault):
  # An authenticated shared-browser guest (a live lineage), not a token the
  # browser-access check would already refuse, so the route's own rule decides.
  from app.browser_access import BrowserLineage
  from app.deps import Principal, get_principal
  from app.main import app as server
  weather = _app(db, "weather")
  vault.save_owned(weather.id, "token", DUMMY)
  owner = db.query(models.Owner).first()
  server.dependency_overrides[get_principal] = lambda: Principal(
    owner=owner, app_id=None, browser=BrowserLineage("g" * 32, "s" * 32))
  try:
    response = client.get(f"/api/apps/{weather.id}/secrets/token")
  finally:
    server.dependency_overrides.pop(get_principal, None)
  assert response.status_code == 403 and DUMMY not in response.text


def test_forgetting_an_app_needs_no_vault_when_nothing_was_ever_stored(monkeypatch, tmp_path):
  import asyncio
  monkeypatch.setattr(key_vault, "vault_dir", lambda: tmp_path / "never-used")
  asyncio.run(key_vault.forget_app(7))  # no store, no socket: nothing to forget
  (tmp_path / "never-used").mkdir()
  (tmp_path / "never-used" / "keys.json").write_text("{}")
  monkeypatch.setattr(key_vault, "vault_socket", lambda: tmp_path / "missing.sock")
  with pytest.raises(key_vault.VaultUnavailable):
    asyncio.run(key_vault.forget_app(7))


@pytest.mark.parametrize("path", ["/v1/gzip", "/v1/deflate"])
def test_a_compressed_echo_never_carries_the_key_to_the_app(client, auth, db, vault, path):
  weather = _app(db, "weather")
  vault.put_key("echoed", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": False, "sites": [SITE]}})
  used = client.get(f"/api/keys/use/echoed/api.example.com{path}", headers=_app_auth(db, weather))
  assert used.status_code == 200
  assert DUMMY not in used.text and DUMMY.encode() not in used.content
  assert json.loads(used.content)["headers"]["Authorization"] == "Bearer [redacted by key vault]"
  assert "content-encoding" not in used.headers


def test_an_encoding_the_vault_cannot_read_is_refused(client, auth, db, vault):
  weather = _app(db, "weather")
  vault.put_key("echoed", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": False, "sites": [SITE]}})
  refused = client.get("/api/keys/use/echoed/api.example.com/v1/brotli", headers=_app_auth(db, weather))
  assert refused.status_code == 502 and refused.headers["X-Vault-Denied"] == "1"
  assert DUMMY not in refused.text


# -- the retired per-app secret store ------------------------------------------

def _legacy_secret(app_id, name, value):
  from app.config import get_settings
  folder = Path(get_settings().data_dir) / "app-secrets" / str(app_id)
  folder.mkdir(parents=True, exist_ok=True)
  (folder / name).write_bytes(key_vault._legacy_fernet().encrypt(value.encode()))


def test_saved_app_secrets_move_into_the_vault_once(client, auth, db, vault):
  import asyncio
  first = _app(db, "first", provider_key="api_key")
  second = _app(db, "second", provider_key="api_key")
  _legacy_secret(first.id, "api_key", "first-secret")
  _legacy_secret(second.id, "api_key", "second-secret")   # same name, another app
  _legacy_secret(second.id, "bot.token", "x")              # dotted and short
  _legacy_secret(999, "api_key", "orphan-secret")          # a purged app's id
  live = {first.id, second.id}

  moved = asyncio.run(key_vault.import_legacy_app_secrets(live))
  assert moved == {
    f"{first.id}/api_key": f"app{first.id}-api_key",
    f"{second.id}/api_key": f"app{second.id}-api_key",
    f"{second.id}/bot.token": f"app{second.id}-bot-token",
  }
  keys = {k["name"]: k for k in vault.list_keys()}
  assert _ids(keys[f"app{first.id}-api_key"]["grants"]) == [first.id]
  assert _ids(keys[f"app{second.id}-api_key"]["grants"]) == [second.id]
  assert vault.reveal(f"app{second.id}-api_key", ("owner", "")) == "second-secret"
  # Each provider key starts allowed on its provider's site only; others on none.
  assert keys[f"app{first.id}-api_key"]["grants"]["sites"] == [PROVIDER_SITE]
  assert keys[f"app{second.id}-bot-token"]["grants"]["sites"] == []
  # Model providers and the adapter find each app's own value by its old name.
  assert key_vault.reveal_model_provider_key(second.id, "api_key", PROVIDER) == "second-secret"
  assert key_vault.reveal_model_provider_key(first.id, "api_key", PROVIDER) == "first-secret"
  assert key_vault.reveal_model_provider_key(999, "api_key", PROVIDER) is None

  # A key the owner deletes afterwards is never brought back.
  vault.revoke_key(f"app{first.id}-api_key")
  assert asyncio.run(key_vault.import_legacy_app_secrets(live)) == {}
  assert f"app{first.id}-api_key" not in {k["name"] for k in vault.list_keys()}


def test_a_purged_apps_unimported_secrets_never_reach_an_app_reusing_its_id(client, auth, db, vault):
  import asyncio
  from app.config import get_settings
  gone = _app(db, "gone")
  _legacy_secret(gone.id, "api_key", "old-apps-secret")
  # The first import cannot read the value, so it stays pending for a retry.
  folder = Path(get_settings().data_dir) / "app-secrets" / str(gone.id)
  (folder / "api_key").write_bytes(b"not a token yet")
  assert asyncio.run(key_vault.import_legacy_app_secrets({gone.id})) == {}
  _legacy_secret(gone.id, "api_key", "old-apps-secret")
  asyncio.run(key_vault.forget_app(gone.id))   # the purge, before the id is freed
  assert not folder.exists()
  # A new app that receives the same id inherits nothing on the next start.
  assert asyncio.run(key_vault.import_legacy_app_secrets({gone.id})) == {}
  assert key_vault.reveal_model_provider_key(gone.id, "api_key", PROVIDER) is None
  assert vault.list_keys() == []


@pytest.mark.parametrize("reinstalled", [False, True])
def test_a_save_waiting_on_app_removal_never_grants_the_freed_id(client, auth, db, vault, reinstalled):
  """A save authorized before the app was removed (or reinstalled) waits for
  the removal's lock, then is refused, so no key outlives the app."""
  import asyncio
  from app import fs_locks
  app = _app(db, "racing")
  app_id, nonce = app.id, app.token_nonce

  async def scenario():
    order = []
    purge_holds_lock = asyncio.Event()

    async def remove():
      async with fs_locks.app_storage_lock(app_id):
        purge_holds_lock.set()
        await asyncio.sleep(0.05)          # the save is queued on the lock now
        await key_vault.forget_app(app_id)
        if reinstalled:
          app.token_nonce = "rotated-by-reinstall"
        else:
          db.delete(app)
        db.commit()
        order.append("removed")

    async def save():
      await purge_holds_lock.wait()
      try:
        async with key_vault.for_live_app(app_id, nonce):
          await key_vault.store_app_key(app_id, "api_key", "late")
          order.append("saved")
      except key_vault.AppGone:
        order.append("refused")

    await asyncio.gather(remove(), save())
    return order

  assert asyncio.run(scenario()) == ["removed", "refused"]
  assert vault.list_keys() == []


def test_the_deprecated_secret_routes_use_the_vault(client, auth, db, vault):
  weather = _app(db, "weather")
  app_auth = _app_auth(db, weather)
  path = f"/api/apps/{weather.id}/secrets/provider-key"
  assert client.head(path, headers=app_auth).status_code == 404
  saved = client.put(path, headers=app_auth, json={"value": "saved-by-app"})
  assert saved.status_code == 204 and saved.headers["Deprecation"] == "true"
  assert client.head(path, headers=app_auth).status_code == 204
  # No app token reads a value back; the owner and agents still can.
  assert client.get(path, headers=app_auth).status_code == 403
  assert client.get(path, headers=_agent_auth(db)).text == "saved-by-app"
  [record] = vault.list_keys()
  assert record["name"] == f"app{weather.id}-provider-key" and _ids(record["grants"]) == [weather.id]
  assert record["grants"]["sites"] == []   # borrowing waits for the owner's sites
  # Another app cannot touch it.
  other = _app(db, "other")
  assert client.head(path, headers=_app_auth(db, other)).status_code == 403
  # Deleting a shared key only withdraws this app's grant.
  vault.put_key(record["name"], {"inject": record["inject"],
                                 "grants": {**record["grants"], "apps": [{"id": weather.id, "sites": []}, {"id": other.id, "sites": []}]}})
  assert client.delete(path, headers=app_auth).status_code == 204
  assert [_ids(k["grants"]) for k in vault.list_keys()] == [[other.id]]
  assert client.head(path, headers=app_auth).status_code == 404


def test_borrowing_a_key_never_lets_an_app_replace_it(client, auth, db, vault):
  weather = _app(db, "weather")
  owner_record = {"value": DUMMY, "inject": ASK["inject"],
                  "grants": {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": False, "sites": [SITE]}}
  vault.put_key("provider-key", owner_record)                    # the owner's key, lent to the app
  vault.put_key(f"app{weather.id}-provider-key", {**owner_record, "grants": {"apps": [], "sites": []}})
  path = f"/api/apps/{weather.id}/secrets/provider-key"
  app_auth = _app_auth(db, weather)
  # The old routes do not even see a key the app only borrows.
  assert client.head(path, headers=app_auth).status_code == 404
  assert client.put(path, headers=app_auth, json={"value": "app-chosen"}).status_code == 204
  assert vault.reveal("provider-key", ("owner", "")) == DUMMY
  assert vault.reveal(f"app{weather.id}-provider-key", ("owner", "")) == DUMMY
  # The app's value went to a free name of its own instead.
  [own] = [k for k in vault.list_keys() if k["origin"] == {"app": weather.id, "name": "provider-key"}]
  assert own["name"] == f"app{weather.id}-provider-key-2"
  assert vault.reveal(own["name"], ("owner", "")) == "app-chosen"
  assert client.delete(path, headers=app_auth).status_code == 204
  assert vault.reveal("provider-key", ("owner", "")) == DUMMY


def test_distinct_old_names_stay_distinct_keys(client, auth, db, vault):
  import asyncio
  app = _app(db, "names")
  _legacy_secret(app.id, "api.key", "dotted-value")
  _legacy_secret(app.id, "api-key", "dashed-value")
  long_a, long_b = "y" * 63 + "1", "y" * 63 + "2"
  vault.put_key(long_a, {"value": DUMMY, "inject": ASK["inject"], "grants": {}})
  vault.put_key(long_b, {"value": DUMMY, "inject": ASK["inject"], "grants": {}})
  _legacy_secret(app.id, long_a, "long-a-value")
  _legacy_secret(app.id, long_b, "long-b-value")
  moved = asyncio.run(key_vault.import_legacy_app_secrets({app.id}))
  assert len(set(moved.values())) == 4
  keys = vault.list_keys()
  for name, value in (("api.key", "dotted-value"), ("api-key", "dashed-value"),
                      (long_a, "long-a-value"), (long_b, "long-b-value")):
    own = key_vault.owned_key_in(keys, app.id, name)
    assert vault.reveal(own["name"], ("owner", "")) == value
  assert vault.reveal(long_a, ("owner", "")) == DUMMY    # the occupied names were untouched


def test_simultaneous_saves_leave_one_key_per_app_secret(client, auth, db, vault):
  import asyncio
  app = _app(db, "racer", provider_key="provider-key")

  async def save_all():
    return await asyncio.gather(*(
      key_vault.store_app_key(app.id, "provider-key", f"value-{n}") for n in range(8)
    ))

  names = asyncio.run(save_all())
  owned = [k for k in vault.list_keys() if k["origin"] == {"app": app.id, "name": "provider-key"}]
  assert len(owned) == 1 and set(names) == {owned[0]["name"]}
  winner = vault.reveal(owned[0]["name"], ("owner", ""))
  assert winner in {f"value-{n}" for n in range(8)}
  assert key_vault.reveal_model_provider_key(app.id, "provider-key", PROVIDER) == winner


def test_a_borrowed_key_reaches_only_a_model_provider_on_its_sites(client, auth, db, vault):
  app = _app(db, "router")
  vault.put_key("shared-provider", {"value": DUMMY, "inject": ASK["inject"], "grants": {
    "apps": [{"id": app.id, "sites": [SITE]}], "agents": False, "sites": [SITE],
  }})
  reveal = key_vault.reveal_model_provider_key
  assert reveal(app.id, "shared-provider", "https://api.example.com/v1") == DUMMY
  # The app chooses its provider's address; the owner chose the key's sites.
  assert reveal(app.id, "shared-provider", "https://attacker.example/v1") is None
  assert reveal(app.id, "shared-provider", "https://api.example.com:8443/v1") is None
  assert reveal(app.id, "shared-provider", "https://api.example.com/v2") is None
  assert key_vault.model_provider_key_sync(app.id, "shared-provider", "https://attacker.example/v1") is None
  other = _app(db, "bystander")
  assert reveal(other.id, "shared-provider", "https://api.example.com/v1") is None


def test_an_apps_own_provider_key_never_starts_on_every_subdomain(client, auth, db, vault):
  """The manifest's provider address is the app's word: a wildcard there must
  not become the site a new key starts on."""
  app = _app(db, "tenant-provider", provider_key="api_key", base_url="https://*.workers.dev/v1")
  path = f"/api/apps/{app.id}/secrets/api_key"
  assert client.put(path, headers=_app_auth(db, app), json={"value": DUMMY}).status_code == 204
  [record] = client.get("/api/keys", headers=auth).json()["keys"]
  assert record["grants"]["sites"] == []


def test_narrowing_an_apps_own_provider_key_stops_its_provider(client, auth, db, vault):
  """An app-saved provider key obeys its sites like a borrowed one: narrowed
  to another site in Settings › Keys, it is no longer sent to the provider."""
  provider = "https://other.example.com/v1"
  app = _app(db, "own-provider", provider_key="api_key", base_url=provider)
  path = f"/api/apps/{app.id}/secrets/api_key"
  assert client.put(path, headers=_app_auth(db, app), json={"value": DUMMY}).status_code == 204
  [record] = client.get("/api/keys", headers=auth).json()["keys"]
  # A new provider key starts allowed on its provider's own endpoint only.
  assert record["grants"]["sites"] == [
    {"host": "other.example.com", "methods": ["POST"], "paths": ["/v1/responses"]},
  ]
  reveal = key_vault.reveal_model_provider_key
  assert reveal(app.id, "api_key", provider) == DUMMY
  assert reveal(app.id, "api_key", "https://elsewhere.example.com/v1") is None
  # The provider runs for the app, so it follows the app's own sites.
  safe = [{"host": "safe.example.com", "methods": ["POST"], "paths": ["/v1/*"]}]
  narrowed = client.put(f"/api/keys/{record['name']}", headers=auth, json={
    "inject": record["inject"], "revision": record["revision"],
    "grants": {**record["grants"], "apps": [{"id": app.id, "sites": safe}]},
  })
  assert narrowed.status_code == 200, narrowed.text
  assert key_vault.model_provider_key_sync(app.id, "api_key", provider) is None
  assert reveal(app.id, "api_key", provider) is None
  # Still the app's own key: narrowing changed where it goes, not whose it is.
  assert key_vault.owned_key_in(vault.list_keys(), app.id, "api_key") is not None


def test_an_owner_filled_key_is_no_longer_the_apps_to_replace_delete_or_send(client, auth, db, vault):
  """An app that created a key for itself loses it once the owner
  fills in the value: it can no longer swap the value, delete the key, or
  have the platform send it to the app's own model provider host."""
  attacker = "https://attacker.example/v1"
  app = _app(db, "squatter", provider_key="OPENAI_API_KEY", base_url=attacker)
  app_auth = _app_auth(db, app)
  path = f"/api/apps/{app.id}/secrets/OPENAI_API_KEY"
  assert client.put(path, headers=app_auth, json={"value": "app-dummy"}).status_code == 204
  [record] = vault.list_keys()
  assert record["name"] == f"app{app.id}-OPENAI_API_KEY" and record["origin"]["app"] == app.id
  # The owner fills in the real value from Settings › Keys, keeping the grants.
  filled = client.put(f"/api/keys/{record['name']}", headers=auth, json={
    "value": "owners-real-key", "inject": record["inject"], "grants": record["grants"],
  })
  assert filled.status_code == 200
  assert key_vault.reveal_model_provider_key(app.id, "OPENAI_API_KEY", attacker) is None
  assert client.head(path, headers=app_auth).status_code == 404
  assert client.delete(path, headers=app_auth).status_code == 404
  assert client.put(path, headers=app_auth, json={"value": "app-chosen"}).status_code == 204
  assert vault.reveal(record["name"], ("owner", "")) == "owners-real-key"
  # The app's provider now gets only the value the app saved itself.
  assert key_vault.reveal_model_provider_key(app.id, "OPENAI_API_KEY", attacker) == "app-chosen"


def test_wiping_an_apps_data_erases_its_own_secrets_but_not_borrowed_keys(client, auth, db, vault):
  from app.config import get_settings
  app = _app(db, "wiped")
  app_auth = _app_auth(db, app)
  assert client.put(f"/api/apps/{app.id}/secrets/token", headers=app_auth,
                    json={"value": "apps-own"}).status_code == 204
  _legacy_secret(app.id, "old_token", "legacy-value")
  vault.put_key("lent", {"value": DUMMY, "inject": ASK["inject"],
                         "grants": {"apps": [{"id": app.id, "sites": [SITE]}], "agents": False, "sites": [SITE]}})
  assert client.delete(f"/api/apps/{app.id}/data", headers=auth).status_code == 204
  keys = {k["name"]: k for k in vault.list_keys()}
  assert "token" not in keys
  assert _ids(keys["lent"]["grants"]) == [app.id]
  assert not (Path(get_settings().data_dir) / "app-secrets" / str(app.id)).exists()


def test_adding_a_key_never_replaces_one_with_the_same_name(client, auth, db, vault):
  app = _app(db, "holder")
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [{"id": app.id, "sites": [SITE]}], "agents": False, "sites": [SITE]}})
  added = client.put("/api/keys/shared", headers=auth, json={
    "value": "other", "inject": ASK["inject"],
    "grants": {"apps": [], "agents": True, "sites": []}, "create_only": True,
  })
  assert added.status_code == 409
  [kept] = vault.list_keys()
  assert vault.reveal("shared", ("owner", "")) == DUMMY and _ids(kept["grants"]) == [app.id]


def test_a_settings_save_from_a_stale_page_is_refused(client, auth, db, vault):
  import asyncio
  app = _app(db, "purged")
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [{"id": app.id, "sites": [SITE]}], "agents": False, "sites": [SITE]}})
  [page] = client.get("/api/keys", headers=auth).json()["keys"]
  asyncio.run(key_vault.forget_app(app.id))
  saved = client.put("/api/keys/shared", headers=auth, json={
    "inject": page["inject"], "grants": {**page["grants"], "agents": True},
    "revision": page["revision"],
  })
  assert saved.status_code == 409
  assert vault.list_keys()[0]["grants"]["apps"] == []


def test_a_multi_line_secret_moves_into_the_vault(client, auth, db, vault):
  import asyncio
  app = _app(db, "pem")
  pem = "-----BEGIN PRIVATE KEY-----\nMIIBVQIBADANBg\n-----END PRIVATE KEY-----"
  _legacy_secret(app.id, "service_key", pem)
  assert asyncio.run(key_vault.import_legacy_app_secrets({app.id})) == {f"{app.id}/service_key": f"app{app.id}-service_key"}
  assert vault.reveal(f"app{app.id}-service_key", ("owner", "")) == pem


# -- an app id is not an installation ------------------------------------------

def _purge(db, app):
  """The aged-tombstone purge: the app's grants go, then its id is freed."""
  import asyncio
  asyncio.run(key_vault.forget_app(app.id))
  db.delete(app)
  db.commit()


def test_a_grant_chosen_on_a_stale_page_never_reaches_an_app_reusing_the_id(client, auth, db, vault):
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [], "agents": False, "sites": [SITE]}})
  chosen = _app(db, "chosen")
  # The owner's page: the key, and the apps it offers to grant.
  [page] = client.get("/api/keys", headers=auth).json()["keys"]
  offered = {a["id"]: a["storage_generation"] for a in client.get("/api/apps/", headers=auth).json()}
  app_id = chosen.id
  # The chosen app had no grant, so its purge leaves the key's revision alone.
  _purge(db, chosen)
  newcomer = _app(db, "newcomer", app_id)
  saved = client.put("/api/keys/shared", headers=auth, json={
    "inject": page["inject"], "grants": {**page["grants"], "apps": [{"id": app_id, "sites": [SITE]}]},
    "revision": page["revision"], "installations": {str(app_id): offered[app_id]},
  })
  assert saved.status_code == 409
  assert vault.list_keys()[0]["grants"]["apps"] == []
  use = client.get("/api/keys/use/shared/api.example.com/v1/x", headers=_app_auth(db, newcomer))
  assert use.status_code == 403


def test_a_new_grant_names_the_installation_the_owner_chose(client, auth, db, vault):
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [], "agents": False, "sites": [SITE]}})
  weather = _app(db, "weather")
  [page] = client.get("/api/keys", headers=auth).json()["keys"]
  grant = {"inject": page["inject"], "grants": {**page["grants"], "apps": [{"id": weather.id, "sites": [SITE]}]}}
  assert client.put("/api/keys/shared", headers=auth, json=grant).status_code == 409
  [listed] = [a for a in client.get("/api/apps/", headers=auth).json() if a["id"] == weather.id]
  saved = client.put("/api/keys/shared", headers=auth, json={
    **grant, "installations": {str(weather.id): listed["storage_generation"]},
  })
  assert saved.status_code == 200, saved.text
  assert client.get("/api/keys/use/shared/api.example.com/v1/x",
                    headers=_app_auth(db, weather)).status_code == 200
  # A grant the key already holds needs no installation to keep.
  [page] = client.get("/api/keys", headers=auth).json()["keys"]
  kept = client.put("/api/keys/shared", headers=auth, json={
    "inject": page["inject"], "grants": {**page["grants"], "agents": True},
    "revision": page["revision"],
  })
  assert kept.status_code == 200 and _ids(vault.list_keys()[0]["grants"]) == [weather.id]


def test_a_grant_waiting_on_app_removal_is_refused(client, auth, db, vault):
  """A grant checked before the app's purge waits for the purge's lock, then
  is checked again against the app that now has the id."""
  import asyncio
  from fastapi import HTTPException
  from app import fs_locks
  from app.app_compile_contract import app_storage_generation
  from app.deps import Principal
  from app.routes import keys as keys_routes
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [], "agents": False, "sites": [SITE]}})
  app = _app(db, "racing")
  app_id, installation = app.id, app_storage_generation(app.token_nonce)
  [page] = vault.list_keys()
  principal = Principal(owner=db.query(models.Owner).first(), app_id=None)
  body = {"inject": page["inject"], "grants": {**page["grants"], "apps": [{"id": app_id, "sites": [SITE]}]},
          "revision": page["revision"], "installations": {str(app_id): installation}}

  async def scenario():
    purge_holds_lock = asyncio.Event()

    async def purge():
      async with fs_locks.app_storage_lock(app_id):
        purge_holds_lock.set()
        await asyncio.sleep(0.05)          # the grant is queued on the lock now
        await key_vault.forget_app(app_id)
        db.delete(app)
        db.commit()
        _app(db, "newcomer", app_id)

    async def grant():
      await purge_holds_lock.wait()
      try:
        await keys_routes.put_key("shared", body, principal=principal, db=db)
      except HTTPException as exc:
        return exc.status_code
      return 200

    return (await asyncio.gather(purge(), grant()))[1]

  assert asyncio.run(scenario()) == 409
  assert vault.list_keys()[0]["grants"]["apps"] == []


def test_a_request_card_left_open_never_approves_an_app_reusing_the_id(client, auth, db, vault):
  asker = _app(db, "asker")
  assert client.post("/api/keys/requests", headers=_app_auth(db, asker), json=ASK).status_code == 200
  [card] = client.get("/api/keys", headers=auth).json()["requests"]
  app_id = asker.id
  _purge(db, asker)
  newcomer = _app(db, "newcomer", app_id)
  newcomer_auth = _app_auth(db, newcomer)
  assert client.post("/api/keys/requests", headers=newcomer_auth, json=ASK).json()["id"] == card["id"]
  stale = client.post(f"/api/keys/requests/{card['id']}/approve", headers=auth,
                      json={"value": DUMMY, "ticket": card.get("ticket")})
  assert stale.status_code == 409
  assert client.post("/api/keys/requests", headers=newcomer_auth, json=ASK).json()["status"] == "pending"


async def _as_app(route, app_id, principal, db):
  """One app-token call that acts for the app: save, delete, or ask."""
  from fastapi import HTTPException
  from app.routes import app_secrets_compat
  from app.routes import keys as keys_routes
  try:
    if route == "save":
      await app_secrets_compat.put_secret(
        app_id, "api_key", app_secrets_compat.SecretWrite(value="late-value"), db=db, principal=principal,
      )
    elif route == "delete":
      await app_secrets_compat.delete_secret(app_id, "api_key", db=db, principal=principal)
    else:
      await keys_routes.request_key(body=ASK, principal=principal, db=db)
  except HTTPException as exc:
    return exc.status_code
  return 200


@pytest.mark.parametrize("route", ["save", "delete", "ask"])
@pytest.mark.parametrize("reused", ["before_the_lookup", "while_waiting_for_the_lock"])
def test_an_app_token_never_acts_for_an_app_reusing_its_id(client, auth, db, vault, pushes, route, reused):
  """The app's token was authenticated, then the app was purged and its id
  reused: before the route looked the app up, or while it waited for the
  app's lock. Either way the call is refused, and the newcomer's key and
  requests are untouched."""
  import asyncio
  from app import fs_locks
  from app.deps import Principal
  app = _app(db, "leaving")
  app_id = app.id
  principal = Principal(owner=db.query(models.Owner).first(), app_id=app_id,
                        app_instance_id=app.token_nonce, scope="app")
  asyncio.run(key_vault.store_app_key(app_id, "api_key", "old-apps-value"))

  async def reuse_the_id():
    await key_vault.forget_app(app_id)
    db.delete(app)
    db.commit()
    _app(db, "newcomer", app_id)
    await key_vault.store_app_key(app_id, "api_key", "newcomers-value")

  async def scenario():
    if reused == "before_the_lookup":
      await reuse_the_id()
      return await _as_app(route, app_id, principal, db)
    purge_holds_lock = asyncio.Event()

    async def purge():
      async with fs_locks.app_storage_lock(app_id):
        purge_holds_lock.set()
        await asyncio.sleep(0.05)          # the call found its app and waits here
        await reuse_the_id()

    async def call():
      await purge_holds_lock.wait()
      return await _as_app(route, app_id, principal, db)

    return (await asyncio.gather(purge(), call()))[1]

  assert asyncio.run(scenario()) == 404
  [kept] = vault.list_keys()
  assert vault.reveal(kept["name"], ("owner", "")) == "newcomers-value"
  assert vault.list_requests() == []


def test_a_borrow_whose_body_outlives_the_app_never_uses_the_newcomers_grant(client, auth, db, vault):
  """The token is checked before the body arrives. If the app is purged and
  its id reused meanwhile, the borrow is refused, not sent with the grant the
  owner gave the newcomer."""
  import asyncio
  from fastapi import HTTPException
  from starlette.requests import Request
  from app.deps import Principal
  from app.routes import keys as keys_routes
  app = _app(db, "leaving")
  app_id = app.id
  principal = Principal(owner=db.query(models.Owner).first(), app_id=app_id,
                        app_instance_id=app.token_nonce, scope="app")
  path = "/api/keys/use/shared/api.example.com/v1/x"

  async def body_arrives_after_reuse():
    await key_vault.forget_app(app_id)
    db.delete(app)
    db.commit()
    _app(db, "newcomer", app_id)
    vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                             "grants": {"apps": [{"id": app_id, "sites": [SITE]}], "agents": False, "sites": [SITE]}})
    return {"type": "http.request", "body": b"{}", "more_body": False}

  request = Request({
    "type": "http", "method": "POST", "path": path, "raw_path": path.encode(),
    "query_string": b"", "headers": [(b"content-type", b"application/json")],
  }, body_arrives_after_reuse)

  async def borrow():
    try:
      await keys_routes.use_key("shared", "api.example.com", "v1/x", request, principal=principal)
    except HTTPException as exc:
      return exc.status_code
    return 200

  assert asyncio.run(borrow()) == 404
  assert [e for e in vault.usage() if e["caller"].startswith("app")] == []


def test_an_app_is_never_purged_while_a_borrow_it_started_is_on_its_way_to_the_vault(
  client, auth, db, vault, monkeypatch,
):
  """A borrow passed every check and is on its way to the vault, which knows
  the app only by its reusable id. Purging the app under its lock, reusing
  the id and granting only the newcomer a key must not let the old borrow
  arrive with the newcomer's grant: the purge waits its turn instead."""
  import asyncio
  from starlette.requests import Request
  from app import fs_locks
  from app.deps import Principal
  from app.routes import keys as keys_routes
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [], "agents": False, "sites": [SITE]}})
  app = _app(db, "leaving")
  app_id = app.id
  principal = Principal(owner=db.query(models.Owner).first(), app_id=app_id,
                        app_instance_id=app.token_nonce, scope="app")
  path = "/api/keys/use/shared/api.example.com/v1/x"
  sending, resume = asyncio.Event(), asyncio.Event()
  send_to_vault = key_vault.vault_use

  async def paused_on_the_way_to_the_vault(*args, **kwargs):
    sending.set()
    await resume.wait()
    return await send_to_vault(*args, **kwargs)
  monkeypatch.setattr(key_vault, "vault_use", paused_on_the_way_to_the_vault)

  async def body():
    return {"type": "http.request", "body": b"", "more_body": False}

  request = Request({
    "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
    "query_string": b"", "headers": [],
  }, body)

  async def purge_and_reuse_the_id():
    async with fs_locks.app_storage_lock(app_id):
      await key_vault.forget_app(app_id)
      db.delete(app)
      db.commit()
      _app(db, "newcomer", app_id)
      vault.put_key("shared", {"inject": ASK["inject"],
                               "grants": {"apps": [{"id": app_id, "sites": [SITE]}], "agents": False, "sites": [SITE]}})

  async def scenario():
    borrow = asyncio.create_task(
      keys_routes.use_key("shared", "api.example.com", "v1/x", request, principal=principal),
    )
    await sending.wait()
    try:
      await purge_and_reuse_the_id()
      purged_meanwhile = True
    except RuntimeError:
      purged_meanwhile = False
    resume.set()
    answer = await borrow
    if not purged_meanwhile:
      # Once the borrow has its answer, the purge goes through.
      await purge_and_reuse_the_id()
    return answer.status_code, purged_meanwhile

  # The leaving app holds no grant of its own, so the vault refuses it.
  assert asyncio.run(scenario()) == (403, False)
  assert [e["outcome"] for e in vault.usage() if e["caller"].startswith("app")] == ["denied"]
  assert db.query(models.App).filter(models.App.id == app_id).one().slug == "newcomer"


def _paused_use_request(path):
  from starlette.requests import Request

  async def body():
    return {"type": "http.request", "body": b"", "more_body": False}
  return Request({
    "type": "http", "method": "GET", "path": path, "raw_path": path.encode(),
    "query_string": b"", "headers": [],
  }, body)


def test_a_cancelled_borrow_keeps_the_app_until_the_vault_has_answered(
  client, auth, db, vault, monkeypatch,
):
  """The vault accepted a borrow and has not authorized it yet when its
  caller is cancelled. The vault still acts on it, so the app stays unpurged
  (its id unfreed) until the vault has answered, not merely until the caller
  stopped waiting."""
  import asyncio
  import threading
  from app import fs_locks
  from app.deps import Principal
  from app.routes import keys as keys_routes
  monkeypatch.setattr(key_vault, "_borrows_in_flight", {})
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [], "agents": False, "sites": [SITE]}})
  app = _app(db, "leaving")
  app_id = app.id
  principal = Principal(owner=db.query(models.Owner).first(), app_id=app_id,
                        app_instance_id=app.token_nonce, scope="app")
  accepted, authorize_now = threading.Event(), threading.Event()
  authorize = vault.authorize

  def paused_before_authorizing(*args, **kwargs):
    accepted.set()
    authorize_now.wait(10)
    return authorize(*args, **kwargs)
  monkeypatch.setattr(vault, "authorize", paused_before_authorizing)

  async def purge_and_reuse_the_id():
    async with fs_locks.app_storage_lock(app_id):
      await key_vault.forget_app(app_id)
      db.delete(app)
      db.commit()
      _app(db, "newcomer", app_id)
      vault.put_key("shared", {"inject": ASK["inject"],
                               "grants": {"apps": [{"id": app_id, "sites": [SITE]}], "agents": False, "sites": [SITE]}})

  async def scenario():
    borrow = asyncio.create_task(keys_routes.use_key(
      "shared", "api.example.com", "v1/x",
      _paused_use_request("/api/keys/use/shared/api.example.com/v1/x"),
      principal=principal,
    ))
    assert await asyncio.to_thread(accepted.wait, 10)
    borrow.cancel()
    for _ in range(5):
      await asyncio.sleep(0)
    try:
      await purge_and_reuse_the_id()
      purged_while_the_vault_held_it = True
    except RuntimeError:
      purged_while_the_vault_held_it = False
    authorize_now.set()
    try:
      await borrow
      caller_cancelled = False
    except asyncio.CancelledError:
      caller_cancelled = True
    await purge_and_reuse_the_id()
    return purged_while_the_vault_held_it, caller_cancelled

  assert asyncio.run(scenario()) == (False, True)
  # The leaving app holds no grant of its own, so the vault refused it.
  assert [e["outcome"] for e in vault.usage() if e["caller"].startswith("app")] == ["denied"]
  assert key_vault._borrows_in_flight == {}


@pytest.mark.parametrize("may_have_reached_vault, purge_allowed", [
  (True, False),
  (False, True),
])
def test_a_borrow_the_vault_may_still_act_on_keeps_the_app_unpurged(
  client, auth, db, monkeypatch, may_have_reached_vault, purge_allowed,
):
  """A relay that failed after its request may have reached the vault leaves
  the vault able to act on it later, so the app stays unpurged for the rest
  of this process. One that never connected releases the app at once."""
  import asyncio
  from fastapi import HTTPException
  from app.deps import Principal
  from app.routes import keys as keys_routes
  monkeypatch.setattr(key_vault, "_borrows_in_flight", {})
  app = _app(db, "leaving")
  principal = Principal(owner=db.query(models.Owner).first(), app_id=app.id,
                        app_instance_id=app.token_nonce, scope="app")

  async def vault_fails(*args, **kwargs):
    raise key_vault.VaultUnavailable(
      "no answer", may_have_reached_vault=may_have_reached_vault,
    )
  monkeypatch.setattr(key_vault, "vault_use", vault_fails)

  with pytest.raises(HTTPException) as failed:
    asyncio.run(keys_routes.use_key(
      "shared", "api.example.com", "v1/x",
      _paused_use_request("/api/keys/use/shared/api.example.com/v1/x"),
      principal=principal,
    ))
  assert failed.value.status_code == 503
  try:
    key_vault.refuse_purge_while_borrowing(app.id)
    allowed = True
  except RuntimeError:
    allowed = False
  assert allowed is purge_allowed


def test_approving_one_apps_sites_never_widens_another_app_or_the_agents(client, auth, db, vault):
  """K lets A use api.example.com; B asks for K on another site. Allow gives
  B exactly the sites the owner's answer carries, and A and the agents gain
  nothing."""
  first, second = _app(db, "first"), _app(db, "second")
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [{"id": first.id, "sites": [SITE]}], "agents": True, "sites": [SITE]}})
  elsewhere = {"host": "other.example.net", "methods": ["POST"], "paths": ["/*"]}
  asked = client.post("/api/keys/requests", headers=_app_auth(db, second),
                      json={**ASK, "key": "shared", "sites": [elsewhere]})
  assert asked.json()["status"] == "pending"
  [card] = client.get("/api/keys", headers=auth).json()["requests"]
  approved = client.post(f"/api/keys/requests/{card['id']}/approve", headers=auth,
                         json={"ticket": card["ticket"], "sites": card["sites"]})
  assert approved.status_code == 200, approved.text
  sent = client.post("/api/keys/use/shared/other.example.net/collect", headers=_app_auth(db, first))
  assert sent.status_code == 403 and sent.headers["X-Vault-Denied"] == "1"
  assert client.post("/api/keys/use/shared/other.example.net/collect", headers=_agent_auth(db)).status_code == 403
  assert client.get("/api/keys/use/shared/api.example.com/v1/x",
                    headers=_app_auth(db, second)).status_code == 403
  assert vault.list_keys()[0]["grants"]["sites"] == [kv.validate_site(SITE)]

def _ids(grants):
  """The app ids a key is granted to (each grant also carries its own sites)."""
  return [grant["id"] for grant in grants["apps"]]


# -- who may use, read and change keys -----------------------------------------

@pytest.fixture
def shared_browser(client, monkeypatch):
  """The test client on the HTTPS origin shared-browser access requires."""
  from app.config import get_settings
  from app.routes.browser_access import _limiter
  _limiter.reset()
  monkeypatch.setattr(get_settings(), "frontend_origin", "https://shared.example")
  client.base_url = "https://shared.example"
  client.headers["Origin"] = "https://shared.example"
  return client


def test_a_guest_minted_app_token_never_borrows_or_asks_for_keys(shared_browser, auth, db, vault, pushes):
  """A shared-browser guest opens an app the owner lent a key to and mints the
  app's token: that token acts for the guest, so it neither borrows the
  owner's key nor asks the owner for one."""
  from app.database import SessionLocal
  from tests.browser_access_fixtures import link_grant
  client = shared_browser
  with SessionLocal() as session:
    _grant, invite = link_grant(session, session.query(models.Owner).one(), "Guest")
  redeemed = client.post("/api/connect/browser-access/session/redeem", json={"invite": invite})
  assert redeemed.status_code == 200, redeemed.text
  guest = {"Authorization": "Bearer " + redeemed.json()["access_token"]}
  weather = _app(db, "weather")
  vault.put_key("paid", {"value": DUMMY, "inject": ASK["inject"],
                         "grants": {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": False, "sites": []}})
  minted = client.post("/api/auth/app-token", json={"app_id": weather.id}, headers=guest)
  assert minted.status_code == 200, minted.text
  guest_app = {"Authorization": "Bearer " + minted.json()["token"]}

  used = client.post("/api/keys/use/paid/api.example.com/v1/anything", headers=guest_app, content=b"x")
  assert used.status_code == 403 and DUMMY not in used.text
  asked = client.post("/api/keys/requests", headers=guest_app, json={**ASK, "key": "other"})
  assert asked.status_code == 403
  assert vault.usage(key="paid") and all(e["caller"] != f"app:{weather.id}" for e in vault.usage(key="paid"))
  assert vault.list_requests() == [] and pushes == []
  # The owner's own app token still borrows.
  assert client.post("/api/keys/use/paid/api.example.com/v1/anything",
                     headers=_app_auth(db, weather), content=b"x").status_code == 200


def test_an_agent_cannot_switch_agent_use_back_on(client, auth, db, vault, monkeypatch):
  """The owner switched agents off a key; an agent switching them back on
  would read the value. The server refuses before the vault is asked, and the
  vault refuses too."""
  vault.put_key("private", {"value": DUMMY, "inject": ASK["inject"],
                            "grants": {"apps": [], "agents": False, "sites": [SITE]}})
  agent = _agent_auth(db)
  [page] = client.get("/api/keys", headers=agent).json()["keys"]
  writes = []
  real_call = key_vault.vault_call

  async def record_writes(method, path, payload=None, **kwargs):
    if method != "GET":
      writes.append(path)
    return await real_call(method, path, payload, **kwargs)
  monkeypatch.setattr(key_vault, "vault_call", record_writes)
  flip = client.put("/api/keys/private", headers=agent, json={
    "inject": page["inject"], "revision": page["revision"], "grants": {**page["grants"], "agents": True},
  })
  assert flip.status_code == 403 and "agents use this key" in flip.json()["detail"]
  assert writes == []
  assert client.get("/api/keys/private/value", headers=agent).status_code == 403
  with pytest.raises(kv.VaultError) as refused:
    vault.put_key("private", {"grants": {**page["grants"], "agents": True}}, actor="agent:chat-keys")
  assert refused.value.status == 403
  # The owner may switch them back on.
  assert client.put("/api/keys/private", headers=auth, json={
    "revision": page["revision"], "grants": {**page["grants"], "agents": True},
  }).status_code == 200
  assert client.get("/api/keys/private/value", headers=agent).json()["value"] == DUMMY


def test_a_delegated_helper_borrows_a_key_but_never_reads_its_value(client, auth, db, vault):
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [], "agents": True, "sites": [SITE]}})
  weather = _app(db, "weather")
  vault.save_owned(weather.id, "token", "apps-own-value")
  helper = _helper_auth(db)
  read = client.get("/api/keys/shared/value", headers=helper)
  assert read.status_code == 403 and "Delegated helpers" in read.text and DUMMY not in read.text
  old_route = client.get(f"/api/apps/{weather.id}/secrets/token", headers=helper)
  assert old_route.status_code == 403 and "apps-own-value" not in old_route.text
  assert [e for e in vault.usage() if e["outcome"] == "revealed"] == []
  used = client.get("/api/keys/use/shared/api.example.com/v1/x", headers=helper)
  assert used.status_code == 200
  assert json.loads(used.content)["headers"]["Authorization"] == "Bearer [redacted by key vault]"
  # The owner's top-level agent still reads.
  assert client.get("/api/keys/shared/value", headers=_agent_auth(db)).json()["value"] == DUMMY


def test_an_agent_may_narrow_reorder_or_recase_sites_without_widening(client, auth, db, vault, pushes):
  """Taking access away, or restating it in another order or case, is no
  widening: the agent's save goes through and the owner is not alerted."""
  weather = _app(db, "weather")
  tiles = {"host": "tiles.example.com", "methods": ["GET"], "paths": ["/*"]}
  vault.put_key("maps", {"value": DUMMY, "inject": ASK["inject"], "grants": {
    "apps": [{"id": weather.id, "sites": [SITE, tiles]}], "agents": True, "sites": [SITE, tiles]}})
  agent = _agent_auth(db)
  recased = {"host": "API.Example.COM", "methods": ["post", "get"], "paths": ["/v1/*"]}
  narrowed = {"host": "api.example.com", "methods": ["GET"], "paths": ["/v1/today"]}
  for sites in ([tiles, SITE], [recased, tiles], [narrowed]):
    [page] = client.get("/api/keys", headers=agent).json()["keys"]
    saved = client.put("/api/keys/maps", headers=agent, json={
      "revision": page["revision"],
      "grants": {"apps": [{"id": weather.id, "sites": sites}], "agents": True, "sites": sites},
    })
    assert saved.status_code == 200, saved.text
  assert pushes == []
  [page] = vault.list_keys()
  assert page["grants"]["apps"][0]["sites"] == [kv.validate_site(narrowed)]
  # Widening back is still the owner's decision.
  refused = client.put("/api/keys/maps", headers=agent, json={
    "grants": {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": True, "sites": [narrowed]},
  })
  assert refused.status_code == 403 and "reach more sites" in refused.text
  # Adding a site for the owner and agents goes through, and the owner hears of it.
  added = client.put("/api/keys/maps", headers=agent, json={
    "grants": {"apps": [{"id": weather.id, "sites": [narrowed]}], "agents": True, "sites": [narrowed, tiles]},
  })
  assert added.status_code == 200
  [notice] = pushes
  assert "added sites" in notice["body"] and "replaced" not in notice["body"]


def test_an_agents_value_only_save_keeps_every_grant(client, auth, db, vault, pushes):
  weather = _app(db, "weather")
  grants = {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": True, "sites": [SITE]}
  vault.put_key("rotating", {"value": DUMMY, "inject": ASK["inject"], "grants": grants})
  saved = client.put("/api/keys/rotating", headers=_agent_auth(db), json={"value": "rotated-dummy-key"})
  assert saved.status_code == 200, saved.text
  [record] = vault.list_keys()
  assert record["grants"] == kv.validate_grants(grants) and record["inject"] == ASK["inject"]
  [notice] = pushes
  assert "replaced its value" in notice["body"] and "added sites" not in notice["body"]


def test_an_agent_save_never_undoes_a_change_the_owner_made_meanwhile(client, auth, db, vault, pushes, monkeypatch):
  """An agent's save lands only on the key it was checked against. The owner
  removing a site while the save is on its way makes it fail (409) instead of
  silently restoring the site; an agent creating a key never replaces one the
  owner created meanwhile."""
  import contextlib
  tiles = {"host": "tiles.example.com", "methods": ["GET"], "paths": ["/*"]}
  vault.put_key("maps", {"value": DUMMY, "inject": ASK["inject"],
                         "grants": {"apps": [], "agents": True, "sites": [SITE, tiles]}})
  real = key_vault.for_granted_apps

  @contextlib.asynccontextmanager
  async def owner_changes_keys_meanwhile(name, app_ids, installations):
    if name == "maps":
      [record] = [k for k in vault.list_keys() if k["name"] == "maps"]
      vault.put_key("maps", {"revision": record["revision"], "grants": {**record["grants"], "sites": [SITE]}})
    else:
      vault.put_key(name, {"value": "owners-new-key", "inject": ASK["inject"], "create_only": True})
    async with real(name, app_ids, installations):
      yield
  monkeypatch.setattr(key_vault, "for_granted_apps", owner_changes_keys_meanwhile)
  agent = _agent_auth(db)
  stale = client.put("/api/keys/maps", headers=agent, json={
    "value": "agents-new-key", "grants": {"apps": [], "agents": True, "sites": [SITE, tiles]},
  })
  assert stale.status_code == 409
  [record] = vault.list_keys()
  assert record["grants"]["sites"] == [kv.validate_site(SITE)]
  assert vault.reveal("maps", ("owner", "")) == DUMMY
  created = client.put("/api/keys/fresh", headers=agent, json={
    "value": "agents-new-key", "inject": ASK["inject"], "grants": {"agents": True, "sites": [SITE]},
  })
  assert created.status_code == 409
  assert vault.reveal("fresh", ("owner", "")) == "owners-new-key"
  assert pushes == []


def test_owner_and_agents_never_write_an_apps_secret_through_the_old_route(client, auth, db, vault, pushes):
  """The deprecated routes are the app's own: a value the owner or an agent
  wrote there would become the app's key without the owner's grant."""
  weather = _app(db, "weather")
  path = f"/api/apps/{weather.id}/secrets/token"
  assert client.put(path, headers=_app_auth(db, weather), json={"value": "apps-own-value"}).status_code == 204
  for headers in (auth, _agent_auth(db)):
    for write in (client.put(path, headers=headers, json={"value": "chosen-elsewhere"}),
                  client.delete(path, headers=headers)):
      assert write.status_code == 403 and "Settings › Keys" in write.json()["detail"]
    created = client.put(f"/api/apps/{weather.id}/secrets/new_one", headers=headers, json={"value": "x" * 12})
    assert created.status_code == 403
  [record] = vault.list_keys()
  assert record["origin"] == {"app": weather.id, "name": "token"} and _ids(record["grants"]) == [weather.id]
  assert vault.reveal(record["name"], ("owner", "")) == "apps-own-value"
  # Reading stays open to the owner and agents.
  assert client.get(path, headers=_agent_auth(db)).text == "apps-own-value"
  assert pushes == []


def test_denying_a_request_names_the_exact_request_the_owner_saw(client, auth, db, vault):
  weather, other = _app(db, "weather"), _app(db, "other")
  for app in (weather, other):
    assert client.post("/api/keys/requests", headers=_app_auth(db, app), json=ASK).status_code == 200
  cards = {c["app"]: c for c in client.get("/api/keys", headers=auth).json()["requests"]}
  card = cards[weather.id]
  stale = client.post(f"/api/keys/requests/{card['id']}/deny", headers=auth, json={"ticket": "0" * 16})
  assert stale.status_code == 409
  assert client.post("/api/keys/requests", headers=_app_auth(db, weather), json=ASK).json()["status"] == "pending"
  assert client.post(f"/api/keys/requests/{card['id']}/deny", headers=auth,
                     json={"ticket": card["ticket"]}).status_code == 200
  assert client.post("/api/keys/requests", headers=_app_auth(db, weather), json=ASK).json()["status"] == "denied"
  # A page that sends no ticket still denies.
  assert client.post(f"/api/keys/requests/{cards[other.id]['id']}/deny", headers=auth).status_code == 200


def test_a_vault_that_cannot_write_or_decrypt_says_so(client, auth, db, vault, monkeypatch):
  """A full disk or a key that no longer decrypts is the vault failing, not
  the vault missing: each answers with the vault's own reason, never a crash
  or "not running"."""
  weather = _app(db, "weather")
  vault.put_key("k", {"value": DUMMY, "inject": ASK["inject"],
                      "grants": {"apps": [], "agents": False, "sites": [SITE]}})
  vault.save_owned(weather.id, "token", "apps-own-value")
  [owned] = [k for k in vault.list_keys() if k["origin"]]
  # Agents switched off: the old route's read is refused, not crashed.
  vault.put_key(owned["name"], {"grants": {**owned["grants"], "agents": False}})
  old_route = f"/api/apps/{weather.id}/secrets/token"
  assert client.get(old_route, headers=_agent_auth(db)).status_code == 403

  def disk_full(*_args):
    raise OSError(28, "No space left on device")
  with monkeypatch.context() as patch:
    patch.setattr(vault, "_write_store", disk_full)
    failed = client.put("/api/keys/k", headers=auth, json={"value": "new-dummy-value"})
  assert failed.status_code == 500 and "could not write its files" in failed.json()["detail"]
  assert vault.reveal("k", ("owner", "")) == DUMMY

  for name in ("k", owned["name"]):
    vault._records[name]["sealed"] = vault._records[name]["sealed"][::-1]
  for path in ("/api/keys/k/value", old_route):
    unreadable = client.get(path, headers=auth)
    assert unreadable.status_code == 500 and "can't be decrypted" in unreadable.json()["detail"]


# -- what a borrow sends and answers -------------------------------------------

def test_override_headers_never_leave_the_server(client, auth, db, vault, monkeypatch):
  """Headers some servers read as the real path or method are dropped before
  the vault, which drops them too."""
  weather = _app(db, "weather")
  vault.put_key("k", {"value": DUMMY, "inject": ASK["inject"],
                      "grants": {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": False, "sites": []}})
  relayed = []
  real_use = key_vault.vault_use

  async def capture(method, target, *, headers, **kwargs):
    relayed.extend(name.lower() for name, _ in headers)
    return await real_use(method, target, headers=headers, **kwargs)
  monkeypatch.setattr(key_vault, "vault_use", capture)
  overrides = {name: "/admin" for name in ("X-Original-URL", "X-Rewrite-URL", "X-Override-URL")}
  overrides.update({name: "DELETE" for name in ("X-HTTP-Method-Override", "X-HTTP-Method", "X-Method-Override")})
  used = client.get("/api/keys/use/k/api.example.com/v1/x",
                    headers={**_app_auth(db, weather), **overrides, "X-Units": "c"})
  assert used.status_code == 200
  assert "x-units" in relayed and not {name.lower() for name in overrides} & set(relayed)
  echoed = {name.lower() for name in json.loads(used.content)["headers"]}
  assert not {name.lower() for name in overrides} & echoed


def test_a_sites_answer_never_acts_on_the_mobius_origin(client, auth, db, vault):
  """Only headers that describe the content pass; cookies, Clear-Site-Data,
  Refresh, Link preloads and the like never reach the browser."""
  weather = _app(db, "weather")
  vault.put_key("k", {"value": DUMMY, "inject": ASK["inject"],
                      "grants": {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": False, "sites": []}})
  used = client.get("/api/keys/use/k/api.example.com/v1/hostile-headers", headers=_app_auth(db, weather))
  assert used.status_code == 200 and used.content == b"ok"
  for name, value in _HOSTILE_HEADERS.items():
    assert used.headers.get(name) != value, name
  assert "set-cookie" not in used.headers and "x-vault-denied" not in used.headers
  for name, value in _KEPT_HEADERS.items():
    assert used.headers.get(name) == value, name
  assert used.headers["X-Vault-Key"] == "k"
  assert used.headers["Content-Security-Policy"].startswith("sandbox")
  assert used.headers["X-Content-Type-Options"] == "nosniff" and used.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("kind, where", [
  ("relative", "https://api.example.com/api/apps/1/storage/x"),
  ("path", "https://api.example.com/v1/next?page=2"),
  ("absolute", "https://elsewhere.example/landing"),
])
def test_a_sites_redirect_is_reported_never_followed(client, auth, db, vault, kind, where):
  """A browser would follow Location, and a relative one would send the app's
  request back into Möbius; the app reads where it points instead."""
  from starlette.middleware.cors import CORSMiddleware
  from app.main import app as server
  weather = _app(db, "weather")
  vault.put_key("k", {"value": DUMMY, "inject": ASK["inject"],
                      "grants": {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": False, "sites": []}})
  moved = client.get(f"/api/keys/use/k/api.example.com/v1/redirect-{kind}",
                     headers=_app_auth(db, weather), follow_redirects=False)
  assert moved.status_code == 307 and "location" not in moved.headers
  assert moved.headers["X-Vault-Location"] == where
  [cors] = [m for m in server.user_middleware if m.cls is CORSMiddleware]
  assert "X-Vault-Location" in cors.kwargs["expose_headers"]


def test_an_answer_that_breaks_off_reaches_the_app_incomplete(client, auth, db, vault):
  """The site hangs up mid-answer: over a real connection the app sees an
  incomplete body, never a shorter one that looks complete."""
  import http.client
  import socket
  import time
  import uvicorn
  from app.main import app as server_app
  weather = _app(db, "weather")
  vault.put_key("k", {"value": DUMMY, "inject": ASK["inject"],
                      "grants": {"apps": [{"id": weather.id, "sites": [SITE]}], "agents": False, "sites": []}})
  listener = socket.socket()
  listener.bind(("127.0.0.1", 0))
  port = listener.getsockname()[1]
  server = uvicorn.Server(uvicorn.Config(server_app, lifespan="off", log_level="critical"))
  thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
  thread.start()
  try:
    for _ in range(200):
      if server.started:
        break
      time.sleep(0.05)
    assert server.started
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    connection.request("GET", "/api/keys/use/k/api.example.com/v1/truncated", headers=_app_auth(db, weather))
    answer = connection.getresponse()
    assert answer.status == 200 and answer.getheader("Transfer-Encoding") == "chunked"
    with pytest.raises(http.client.IncompleteRead):
      answer.read()
    connection.close()
  finally:
    server.should_exit = True
    thread.join(10)
    listener.close()
  [entry] = [e for e in vault.usage(key="k") if e["caller"] == f"app:{weather.id}"]
  assert entry["outcome"] == "failed"


def _use_request(raw_path: bytes, *, query: bytes = b"", headers=(), receive=None):
  from starlette.requests import Request

  async def empty():
    return {"type": "http.request", "body": b"", "more_body": False}
  return Request({
    "type": "http", "method": "POST", "path": raw_path.decode("latin-1"), "raw_path": raw_path,
    "query_string": query, "headers": list(headers),
  }, receive or empty)


def _app_principal(db, app):
  from app.deps import Principal
  return Principal(owner=db.query(models.Owner).first(), app_id=app.id,
                   app_instance_id=app.token_nonce, scope="app")


def test_an_oversized_body_is_refused_before_it_is_buffered(client, auth, db, vault):
  import asyncio
  from fastapi import HTTPException
  from app.routes import keys as keys_routes
  app = _app(db, "uploader")
  path = b"/api/keys/use/k/api.example.com/v1/upload"
  received = []

  async def endless():
    received.append(1)
    return {"type": "http.request", "body": b"x" * (1024 * 1024), "more_body": True}

  def use(request):
    with pytest.raises(HTTPException) as refused:
      asyncio.run(keys_routes.use_key("k", "api.example.com", "v1/upload", request,
                                      principal=_app_principal(db, app)))
    return refused.value.status_code

  declared = [(b"content-length", str(keys_routes.MAX_USE_BODY + 1).encode())]
  assert use(_use_request(path, headers=declared, receive=endless)) == 413
  assert received == []
  # A chunked body is read only until it passes the limit.
  assert use(_use_request(path, headers=[(b"transfer-encoding", b"chunked")], receive=endless)) == 413
  assert len(received) == keys_routes.MAX_USE_BODY // (1024 * 1024) + 1


@pytest.mark.parametrize("raw_path, query", [
  (b"/api/keys/use/k/api.example.com/v1/caf\xc3\xa9", b""),
  (b"/api/keys/use/k/api.example.com/v1/x", b"city=Z\xfcrich"),
  (b"/api/keys/use/k/api.example.com/v1/x", b"a=1\x7f"),
])
def test_raw_non_ascii_bytes_in_a_borrow_are_a_bad_request(client, auth, db, vault, raw_path, query):
  import asyncio
  from fastapi import HTTPException
  from app.routes import keys as keys_routes
  app = _app(db, "visitor")
  with pytest.raises(HTTPException) as refused:
    asyncio.run(keys_routes.use_key("k", "api.example.com", "v1/x", _use_request(raw_path, query=query),
                                    principal=_app_principal(db, app)))
  assert refused.value.status_code == 400 and "Percent-encode" in refused.value.detail


# -- model providers: the paths the relay really calls --------------------------

@pytest.mark.parametrize("protocol, base_url, real_paths, wrong_path", [
  ("responses", "https://models.example.net/v1", ["/v1/responses"], "/v1/messages"),
  ("anthropic_messages", "https://models.example.net",
   ["/v1/messages", "/v1/messages/count_tokens"], "/responses"),
])
def test_a_provider_key_is_checked_on_the_paths_its_protocol_calls(
  client, auth, db, vault, protocol, base_url, real_paths, wrong_path,
):
  """An app's own provider key starts on exactly the endpoints the relay
  calls for its protocol; narrowed to the main one it keeps working, and a
  grant for another path never lets the platform send it."""
  app = _app(db, "provider", provider_key="api_key", base_url=base_url, protocol=protocol)
  assert client.put(f"/api/apps/{app.id}/secrets/api_key", headers=_app_auth(db, app),
                    json={"value": DUMMY}).status_code == 204
  [record] = vault.list_keys()
  site = {"host": "models.example.net", "methods": ["POST"], "paths": real_paths}
  assert record["grants"]["apps"] == [{"id": app.id, "sites": [site]}]
  destination = {"protocol": protocol}
  reveal = key_vault.reveal_model_provider_key
  for endpoint in key_vault.PROVIDER_ENDPOINTS[protocol]:
    assert reveal(app.id, "api_key", base_url, **destination, endpoint=endpoint) == DUMMY

  def grant_only(path):
    [page] = vault.list_keys()
    saved = client.put(f"/api/keys/{page['name']}", headers=auth, json={
      "revision": page["revision"],
      "grants": {**page["grants"], "apps": [{"id": app.id, "sites": [{**site, "paths": [path]}]}]},
    })
    assert saved.status_code == 200, saved.text

  grant_only(real_paths[0])
  assert reveal(app.id, "api_key", base_url, **destination) == DUMMY
  assert key_vault.model_provider_key_sync(app.id, "api_key", base_url, **destination) == record["name"]
  grant_only(wrong_path)
  assert reveal(app.id, "api_key", base_url, **destination) is None
  assert key_vault.model_provider_key_sync(app.id, "api_key", base_url, **destination) is None


def test_the_anthropic_side_endpoint_needs_its_own_site(client, auth, db, vault):
  base_url = "https://models.example.net"
  app = _app(db, "claude-like")
  vault.put_key("lent", {"value": DUMMY, "inject": ASK["inject"], "grants": {"apps": [{"id": app.id, "sites": [
    {"host": "models.example.net", "methods": ["POST"], "paths": ["/v1/messages"]}]}], "agents": False, "sites": []}})
  reveal = key_vault.reveal_model_provider_key
  assert reveal(app.id, "lent", base_url, "anthropic_messages") == DUMMY
  assert reveal(app.id, "lent", base_url, "anthropic_messages", "/v1/messages/count_tokens") is None
  # An endpoint the protocol never calls is never checked, let alone sent to.
  assert reveal(app.id, "lent", base_url, "anthropic_messages", "/v1/other") is None


# -- the retired per-app store: saves and deletes before the import ------------

def _ledger():
  return key_vault._read_ledger(key_vault._legacy_root())


def test_a_secret_saved_again_before_the_import_keeps_the_newer_value(client, auth, db, vault):
  import asyncio
  app = _app(db, "weather")
  _legacy_secret(app.id, "token", "OLD-legacy-value")
  path = f"/api/apps/{app.id}/secrets/token"
  assert client.put(path, headers=_app_auth(db, app), json={"value": "NEW-after-upgrade"}).status_code == 204
  assert asyncio.run(key_vault.import_legacy_app_secrets({app.id})) == {}
  owned = key_vault.owned_key_in(vault.list_keys(), app.id, "token")
  assert vault.reveal(owned["name"], ("owner", "")) == "NEW-after-upgrade"
  assert _ledger()[f"{app.id}/token"] == {"superseded_by": owned["name"]}
  assert asyncio.run(key_vault.import_legacy_app_secrets({app.id})) == {}
  assert vault.reveal(owned["name"], ("owner", "")) == "NEW-after-upgrade"


def test_a_secret_saved_after_the_import_replaces_the_imported_value(client, auth, db, vault):
  import asyncio
  app = _app(db, "weather")
  _legacy_secret(app.id, "token", "OLD-legacy-value")
  assert asyncio.run(key_vault.import_legacy_app_secrets({app.id})) == {f"{app.id}/token": f"app{app.id}-token"}
  path = f"/api/apps/{app.id}/secrets/token"
  assert client.put(path, headers=_app_auth(db, app), json={"value": "NEW-after-import"}).status_code == 204
  assert asyncio.run(key_vault.import_legacy_app_secrets({app.id})) == {}
  assert vault.reveal(f"app{app.id}-token", ("owner", "")) == "NEW-after-import"


def test_a_secret_deleted_before_the_import_is_never_brought_back(client, auth, db, vault):
  import asyncio
  app = _app(db, "weather")
  _legacy_secret(app.id, "gone", "deleted-by-app")
  path = f"/api/apps/{app.id}/secrets/gone"
  assert client.delete(path, headers=_app_auth(db, app)).status_code == 204
  assert _ledger()[f"{app.id}/gone"] == {"deleted_by_app": True}
  assert client.delete(path, headers=_app_auth(db, app)).status_code == 404
  assert asyncio.run(key_vault.import_legacy_app_secrets({app.id})) == {}
  assert key_vault.owned_key_in(vault.list_keys(), app.id, "gone") is None and vault.list_keys() == []


def test_a_secret_deleted_after_the_import_stays_deleted(client, auth, db, vault):
  import asyncio
  app = _app(db, "weather")
  _legacy_secret(app.id, "gone", "deleted-by-app")
  assert asyncio.run(key_vault.import_legacy_app_secrets({app.id})) == {f"{app.id}/gone": f"app{app.id}-gone"}
  assert client.delete(f"/api/apps/{app.id}/secrets/gone", headers=_app_auth(db, app)).status_code == 204
  assert asyncio.run(key_vault.import_legacy_app_secrets({app.id})) == {}
  assert vault.list_keys() == []
