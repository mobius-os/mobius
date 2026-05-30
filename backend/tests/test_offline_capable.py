"""offline_capable capability flag + the X-Mobius-Offline response header.

The flag (Tier 3) lets the agent declare an app safe to run offline; the
header (Tier 4a) is how the service worker learns which app-code responses
to cache. These move together — the flag is the source of truth, the header
is its wire form.
"""

JSX = "export default function A(){ return null }"


def _create(client, auth, **extra):
  body = {"name": "App", "jsx_source": JSX, **extra}
  r = client.post("/api/apps/", headers=auth, json=body)
  assert r.status_code == 201, r.text
  return r.json()


def test_create_defaults_offline_capable_false(client, auth):
  assert _create(client, auth)["offline_capable"] is False


def test_create_can_set_offline_capable(client, auth):
  assert _create(client, auth, offline_capable=True)["offline_capable"] is True


def test_patch_offline_capable_toggles_and_persists(client, auth):
  app_id = _create(client, auth)["id"]
  r = client.patch(f"/api/apps/{app_id}", headers=auth,
                   json={"offline_capable": True})
  assert r.status_code == 200, r.text
  assert r.json()["offline_capable"] is True
  # A patch that omits the field must leave it unchanged.
  r2 = client.patch(f"/api/apps/{app_id}", headers=auth, json={"name": "X"})
  assert r2.json()["offline_capable"] is True


def test_appout_includes_flag_in_list(client, auth):
  _create(client, auth, offline_capable=True)
  apps = client.get("/api/apps/", headers=auth).json()
  assert apps and all("offline_capable" in a for a in apps)


def test_frame_header_only_when_capable(client, auth):
  cap = _create(client, auth, offline_capable=True)["id"]
  plain = _create(client, auth)["id"]
  assert client.get(f"/api/apps/{cap}/frame").headers.get("X-Mobius-Offline") == "1"
  assert client.get(f"/api/apps/{plain}/frame").headers.get("X-Mobius-Offline") is None


def test_module_header_only_when_capable(client, auth, owner_token):
  cap = _create(client, auth, offline_capable=True)["id"]
  plain = _create(client, auth)["id"]

  def tok(app_id):
    r = client.post("/api/auth/app-token", json={"app_id": app_id}, headers=auth)
    return r.json()["token"]

  rc = client.get(f"/api/apps/{cap}/module?token={tok(cap)}")
  rp = client.get(f"/api/apps/{plain}/module?token={tok(plain)}")
  assert rc.headers.get("X-Mobius-Offline") == "1"
  assert rp.headers.get("X-Mobius-Offline") is None
