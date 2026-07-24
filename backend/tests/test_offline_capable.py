"""offline_capable capability flag + the X-Mobius-Offline response header.

The flag (Tier 3) lets the agent declare an app safe to run offline; the
header (Tier 4a) is how the service worker learns which app-code responses
to cache. These move together — the flag is the source of truth, the header
is its wire form.
"""

import json
from pathlib import Path

from test_app_fixtures import create_local_app

JSX = "export default function A(){ return null }"


def _create(client, auth, **extra):
  return create_local_app(
    client, auth, name="App", jsx_source=JSX, **extra,
  )


def test_create_defaults_offline_capable_false(client, auth):
  assert _create(client, auth)["offline_capable"] is False


def test_create_can_set_offline_capable(client, auth):
  assert _create(client, auth, offline_capable=True)["offline_capable"] is True


def test_manifest_apply_toggles_offline_capable_and_persists(client, auth):
  app = _create(client, auth)
  source_dir = Path(app["source_dir"])
  manifest_path = source_dir / "mobius.json"
  manifest = json.loads(manifest_path.read_text())
  manifest["offline_capable"] = True
  manifest_path.write_text(json.dumps(manifest))
  r = client.post(
    "/api/apps/apply", headers=auth, json={"source_dir": str(source_dir)},
  )
  assert r.status_code == 200, r.text
  assert r.json()["app"]["offline_capable"] is True
  # A metadata patch cannot accidentally alter the manifest-owned field.
  r2 = client.patch(f"/api/apps/{app['id']}", headers=auth, json={"name": "X"})
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
