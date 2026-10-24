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


class _Echo(BaseHTTPRequestHandler):
  def log_message(self, *args):
    pass

  def _answer(self):
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


def _app(db, slug):
  app = models.App(
    source_dir=f"/tmp/mobius-tests/{slug}", name=slug.title(), slug=slug, description="test",
    jsx_source="export default function App() { return null }",
    compiled_path=f"/tmp/{slug}.js",
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
  assert denied.status_code == 404 and denied.headers["X-Vault-Denied"] == "1"

  # The owner sees the request with the app's own name, then approves it.
  [request] = client.get("/api/keys", headers=auth).json()["requests"]
  assert request["app"] == weather.id and request["app_name"] == "Weather"
  assert client.post(f"/api/keys/requests/{request['id']}/approve", headers=auth,
                     json={"value": DUMMY}).status_code == 200
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
                         "grants": {"apps": [weather.id], "agents": False, "sites": [SITE]}})
  path = "/api/keys/use/maps/api.example.com/v1/x"
  # A forged vault caller header is dropped; the token decides.
  forged = {**_app_auth(db, other), "X-Vault-Caller": f"app:{weather.id}"}
  assert client.get(path, headers=forged).status_code == 403
  assert client.get(path, headers=_app_auth(db, weather)).status_code == 200


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
  assert notice["title"] == "An agent replaced a saved key"
  assert "fresh" in notice["body"] and "third" not in notice["body"]


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
                           "grants": {"apps": [weather.id], "agents": False, "sites": [SITE]}})
  used = client.get(f"/api/keys/use/echoed/api.example.com{path}", headers=_app_auth(db, weather))
  assert used.status_code == 200
  assert DUMMY not in used.text and DUMMY.encode() not in used.content
  assert json.loads(used.content)["headers"]["Authorization"] == "Bearer [redacted by key vault]"
  assert "content-encoding" not in used.headers


def test_an_encoding_the_vault_cannot_read_is_refused(client, auth, db, vault):
  weather = _app(db, "weather")
  vault.put_key("echoed", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [weather.id], "agents": False, "sites": [SITE]}})
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
  first, second = _app(db, "first"), _app(db, "second")
  _legacy_secret(first.id, "api_key", "first-secret")
  _legacy_secret(second.id, "api_key", "second-secret")   # same name, another app
  _legacy_secret(second.id, "bot.token", "x")              # dotted and short
  _legacy_secret(999, "api_key", "orphan-secret")          # a purged app's id
  live = {first.id, second.id}

  moved = asyncio.run(key_vault.import_legacy_app_secrets(live))
  assert moved == {
    f"{first.id}/api_key": "api_key",
    f"{second.id}/api_key": f"app{second.id}-api_key",
    f"{second.id}/bot.token": "bot-token",
  }
  keys = {k["name"]: k for k in vault.list_keys()}
  assert keys["api_key"]["grants"]["apps"] == [first.id]
  assert keys[f"app{second.id}-api_key"]["grants"]["apps"] == [second.id]
  assert vault.reveal(f"app{second.id}-api_key", ("owner", "")) == "second-secret"
  # Model providers and the adapter find each app's own value by its old name.
  assert key_vault.reveal_for_app_platform_use(second.id, "api_key") == "second-secret"
  assert key_vault.reveal_for_app_platform_use(first.id, "api_key") == "first-secret"
  assert key_vault.reveal_for_app_platform_use(999, "api_key") is None

  # A key the owner deletes afterwards is never brought back.
  vault.revoke_key("api_key")
  assert asyncio.run(key_vault.import_legacy_app_secrets(live)) == {}
  assert "api_key" not in {k["name"] for k in vault.list_keys()}


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
  assert key_vault.reveal_for_app_platform_use(gone.id, "api_key") is None
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
  assert record["name"] == "provider-key" and record["grants"]["apps"] == [weather.id]
  assert record["grants"]["sites"] == []   # borrowing waits for the owner's sites
  # Another app cannot touch it.
  other = _app(db, "other")
  assert client.head(path, headers=_app_auth(db, other)).status_code == 403
  # Deleting a shared key only withdraws this app's grant.
  vault.put_key("provider-key", {"inject": record["inject"],
                                 "grants": {**record["grants"], "apps": [weather.id, other.id]}})
  assert client.delete(path, headers=app_auth).status_code == 204
  assert [k["grants"]["apps"] for k in vault.list_keys()] == [[other.id]]
  assert client.head(path, headers=app_auth).status_code == 404


def test_borrowing_a_key_never_lets_an_app_replace_it(client, auth, db, vault):
  weather = _app(db, "weather")
  owner_record = {"value": DUMMY, "inject": ASK["inject"],
                  "grants": {"apps": [weather.id], "agents": False, "sites": [SITE]}}
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
  for name, value in (("api.key", "dotted-value"), ("api-key", "dashed-value"),
                      (long_a, "long-a-value"), (long_b, "long-b-value")):
    assert key_vault.reveal_for_app_platform_use(app.id, name) == value
  assert vault.reveal(long_a, ("owner", "")) == DUMMY    # the occupied names were untouched


def test_simultaneous_saves_leave_one_key_per_app_secret(client, auth, db, vault):
  import asyncio
  app = _app(db, "racer")

  async def save_all():
    return await asyncio.gather(*(
      key_vault.store_app_key(app.id, "provider-key", f"value-{n}") for n in range(8)
    ))

  names = asyncio.run(save_all())
  owned = [k for k in vault.list_keys() if k["origin"] == {"app": app.id, "name": "provider-key"}]
  assert len(owned) == 1 and set(names) == {owned[0]["name"]}
  winner = vault.reveal(owned[0]["name"], ("owner", ""))
  assert winner in {f"value-{n}" for n in range(8)}
  assert key_vault.reveal_for_app_platform_use(app.id, "provider-key") == winner
