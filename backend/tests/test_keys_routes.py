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
# A model provider an app declares; an app's own key may be sent to it.
PROVIDER = "https://models.example.net/v1"


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


def _app(db, slug, app_id=None):
  app = models.App(
    id=app_id,
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
                     json={"value": DUMMY, "ticket": request["ticket"]}).status_code == 200
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
  assert key_vault.reveal_model_provider_key(second.id, "api_key", PROVIDER) == "second-secret"
  assert key_vault.reveal_model_provider_key(first.id, "api_key", PROVIDER) == "first-secret"
  assert key_vault.reveal_model_provider_key(999, "api_key", PROVIDER) is None

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
    assert key_vault.reveal_model_provider_key(app.id, name, PROVIDER) == value
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
  assert key_vault.reveal_model_provider_key(app.id, "provider-key", PROVIDER) == winner


def test_a_borrowed_key_reaches_only_a_model_provider_on_its_sites(client, auth, db, vault):
  app = _app(db, "router")
  vault.put_key("shared-provider", {"value": DUMMY, "inject": ASK["inject"], "grants": {
    "apps": [app.id], "agents": False, "sites": [SITE],
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


def test_an_owner_filled_key_is_no_longer_the_apps_to_replace_delete_or_send(client, auth, db, vault):
  """An app that created a key under a plain name loses it once the owner
  fills in the value: it can no longer swap the value, delete the key, or
  have the platform send it to the app's own model provider host."""
  app = _app(db, "squatter")
  app_auth = _app_auth(db, app)
  path = f"/api/apps/{app.id}/secrets/OPENAI_API_KEY"
  assert client.put(path, headers=app_auth, json={"value": "app-dummy"}).status_code == 204
  [record] = vault.list_keys()
  assert record["name"] == "OPENAI_API_KEY" and record["origin"]["app"] == app.id
  # The owner fills in the real value from Settings › Keys, keeping the grants.
  filled = client.put("/api/keys/OPENAI_API_KEY", headers=auth, json={
    "value": "owners-real-key", "inject": record["inject"], "grants": record["grants"],
  })
  assert filled.status_code == 200
  attacker = "https://attacker.example/v1"
  assert key_vault.reveal_model_provider_key(app.id, "OPENAI_API_KEY", attacker) is None
  assert client.head(path, headers=app_auth).status_code == 404
  assert client.delete(path, headers=app_auth).status_code == 404
  assert client.put(path, headers=app_auth, json={"value": "app-chosen"}).status_code == 204
  assert vault.reveal("OPENAI_API_KEY", ("owner", "")) == "owners-real-key"
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
                         "grants": {"apps": [app.id], "agents": False, "sites": [SITE]}})
  assert client.delete(f"/api/apps/{app.id}/data", headers=auth).status_code == 204
  keys = {k["name"]: k for k in vault.list_keys()}
  assert "token" not in keys
  assert keys["lent"]["grants"]["apps"] == [app.id]
  assert not (Path(get_settings().data_dir) / "app-secrets" / str(app.id)).exists()


def test_adding_a_key_never_replaces_one_with_the_same_name(client, auth, db, vault):
  app = _app(db, "holder")
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [app.id], "agents": False, "sites": [SITE]}})
  added = client.put("/api/keys/shared", headers=auth, json={
    "value": "other", "inject": ASK["inject"],
    "grants": {"apps": [], "agents": True, "sites": []}, "create_only": True,
  })
  assert added.status_code == 409
  [kept] = vault.list_keys()
  assert vault.reveal("shared", ("owner", "")) == DUMMY and kept["grants"]["apps"] == [app.id]


def test_a_settings_save_from_a_stale_page_is_refused(client, auth, db, vault):
  import asyncio
  app = _app(db, "purged")
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [app.id], "agents": False, "sites": [SITE]}})
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
  assert asyncio.run(key_vault.import_legacy_app_secrets({app.id})) == {f"{app.id}/service_key": "service_key"}
  assert vault.reveal("service_key", ("owner", "")) == pem


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
    "inject": page["inject"], "grants": {**page["grants"], "apps": [app_id]},
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
  grant = {"inject": page["inject"], "grants": {**page["grants"], "apps": [weather.id]}}
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
  assert kept.status_code == 200 and vault.list_keys()[0]["grants"]["apps"] == [weather.id]


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
  body = {"inject": page["inject"], "grants": {**page["grants"], "apps": [app_id]},
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
                             "grants": {"apps": [app_id], "agents": False, "sites": [SITE]}})
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
                               "grants": {"apps": [app_id], "agents": False, "sites": [SITE]}})

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
                               "grants": {"apps": [app_id], "agents": False, "sites": [SITE]}})

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


def test_approving_an_app_never_sends_a_shared_key_to_new_sites(client, auth, db, vault):
  """K lets A use api.example.com; B asks for K on another site with POST and
  DELETE. Allow lets B use K where it already works and nowhere else, and
  A gains nothing."""
  first, second = _app(db, "first"), _app(db, "second")
  vault.put_key("shared", {"value": DUMMY, "inject": ASK["inject"],
                           "grants": {"apps": [first.id], "agents": False, "sites": [SITE]}})
  elsewhere = {"host": "evil.example.net", "methods": ["POST", "DELETE"], "paths": ["/*"]}
  asked = client.post("/api/keys/requests", headers=_app_auth(db, second),
                      json={**ASK, "key": "shared", "sites": [elsewhere]})
  assert asked.json()["status"] == "pending"
  [card] = client.get("/api/keys", headers=auth).json()["requests"]
  # Settings sends only the ticket when the key exists.
  approved = client.post(f"/api/keys/requests/{card['id']}/approve", headers=auth,
                         json={"ticket": card.get("ticket")})
  assert approved.status_code == 200, approved.text
  for app in (first, second):
    sent = client.post("/api/keys/use/shared/evil.example.net/collect", headers=_app_auth(db, app))
    assert sent.status_code == 403 and sent.headers["X-Vault-Denied"] == "1"
  assert client.get("/api/keys/use/shared/api.example.com/v1/x",
                    headers=_app_auth(db, second)).status_code == 200
  assert vault.list_keys()[0]["grants"]["sites"] == [kv.validate_site(SITE)]
  outside = [{"host": "evil.example.net", "methods": ["DELETE", "POST"], "paths": ["/*"]}]
  assert card.get("outside_sites") == outside and approved.json()["outside"] == outside
