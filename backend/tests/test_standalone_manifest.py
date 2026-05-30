"""The per-app PWA manifest must reflect a rename immediately and never
be served stale.

The install flow saves the home-screen name (PATCH /api/apps/{id}) and
then navigates straight to the install surface, so the manifest fetched
there has to carry the just-saved name. Two guarantees back that:
`Cache-Control: no-cache` (the browser can't pin an old manifest) and a
microsecond-resolution icon `?v=` (a name PATCH + icon PUT in the same
second still bust the icon cache).
"""

import re


def _create_app(client, owner_token, name):
  r = client.post(
    "/api/apps/",
    json={
      "name": name,
      "description": "x",
      "jsx_source": "export default function App() { return <div>hi</div> }",
    },
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert r.status_code == 201, r.text
  return r.json()


def test_manifest_has_no_cache_header(client, owner_token):
  app = _create_app(client, owner_token, "News App")
  r = client.get(f"/apps/{app['slug']}/manifest.json")
  assert r.status_code == 200
  assert "no-cache" in r.headers.get("cache-control", "").lower()


def test_manifest_name_reflects_rename_immediately(client, owner_token, auth):
  app = _create_app(client, owner_token, "News App")
  slug, app_id = app["slug"], app["id"]

  before = client.get(f"/apps/{slug}/manifest.json").json()
  assert before["name"] == "News App"
  assert before["short_name"] == "News App"[:12]

  r = client.patch(f"/api/apps/{app_id}", json={"name": "News"}, headers=auth)
  assert r.status_code == 200, r.text

  after = client.get(f"/apps/{slug}/manifest.json").json()
  assert after["name"] == "News"
  assert after["short_name"] == "News"


def test_icon_version_is_microsecond_resolution(client, owner_token):
  app = _create_app(client, owner_token, "News App")
  body = client.get(f"/apps/{app['slug']}/manifest.json").json()
  src = body["icons"][0]["src"]
  m = re.search(r"[?&]v=(\d+)", src)
  assert m, src
  # Microseconds since the epoch are ~1.7e15 in 2026; int-seconds would
  # be ~1.7e9. This locks in the resolution bump that prevents
  # same-second collisions between a name PATCH and an icon PUT.
  assert int(m.group(1)) > 10**15, src
