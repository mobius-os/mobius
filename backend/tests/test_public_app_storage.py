"""The general anonymous-write primitive: bounded public app storage.

A published app may opt in to let anonymous visitors READ its `public/` folder
and WRITE within one declared sub-prefix, with CAS, scope, and quota bounds.
"""

import pytest

from app.app_capabilities import normalize_public_access
from app.routes import public_apps, public_storage
from test_app_fixtures import create_local_app, public_host_config


STORAGE_ACCESS = {
  "storage": {"read": True, "write_prefix": "public/submissions/"},
}


def _create(client, auth, name="Booking test", public_access=STORAGE_ACCESS, **kw):
  return create_local_app(
    client, auth, name=name,
    manifest_extra={"public_access": public_access}, **kw,
  )


def _publish(client, headers, app_id):
  return client.put(f"/api/apps/{app_id}/hosted-publication", headers=headers)


def _public_token(client, slug):
  return public_host_config(client.get(f"/{slug}").text)["token"]


# ── Manifest normalization ──────────────────────────────────────────────────

def test_normalize_public_storage_accepts_and_defaults_closed():
  assert normalize_public_access({})["storage"] == {
    "read": False, "write_prefix": None,
  }
  norm = normalize_public_access({"public_access": STORAGE_ACCESS})
  assert norm["storage"] == {"read": True, "write_prefix": "public/submissions/"}
  # A trailing slash is normalized on.
  norm2 = normalize_public_access(
    {"public_access": {"storage": {"read": True, "write_prefix": "public/box"}}}
  )
  assert norm2["storage"]["write_prefix"] == "public/box/"


@pytest.mark.parametrize("bad_prefix", [
  "submissions/",        # not under public/
  "public/",             # the whole public/ root is not a valid write area
  "../public/x/",        # traversal
  "public/x y/",         # illegal character
  "/public/x/",          # absolute
  "public/" + "a" * 250, # longer than the 256-byte bound
])
def test_normalize_public_storage_rejects_unsafe_write_prefix(bad_prefix):
  with pytest.raises(ValueError):
    normalize_public_access(
      {"public_access": {"storage": {"read": True, "write_prefix": bad_prefix}}}
    )


def test_normalize_public_storage_rejects_unknown_keys():
  with pytest.raises(ValueError):
    normalize_public_access(
      {"public_access": {"storage": {"read": True, "extra": 1}}}
    )


# ── End-to-end HTTP behavior ────────────────────────────────────────────────

def test_anonymous_read_write_scoping_cas_and_quota(client, auth, monkeypatch):
  app = _create(client, auth)
  app_id = app["id"]
  # Owner seeds admin-authored config OUTSIDE the write area (public-readable).
  assert client.put(
    f"/api/storage/apps/{app_id}/public/config.json",
    json={"maxPerSlot": 2}, headers=auth,
  ).status_code in (200, 204)
  assert _publish(client, auth, app_id).status_code == 200
  token = _public_token(client, app["slug"])
  bearer = {"Authorization": f"Bearer {token}"}

  # Anonymous READ of the public/ folder works.
  read = client.get(
    f"/api/public-apps/{app_id}/storage/public/config.json", headers=bearer,
  )
  assert read.status_code == 200
  assert read.json() == {"maxPerSlot": 2}

  # Anonymous READ of anything outside public/ is refused.
  assert client.get(
    f"/api/public-apps/{app_id}/storage/private.json", headers=bearer,
  ).status_code == 403

  # Anonymous WRITE inside the declared area succeeds.
  wrote = client.put(
    f"/api/public-apps/{app_id}/storage/public/submissions/b1.json",
    json={"name": "Sam"}, headers=bearer,
  )
  assert wrote.status_code == 204
  assert wrote.headers.get("ETag")

  # The owner sees the anonymous submission through ordinary app storage.
  owner_view = client.get(
    f"/api/storage/apps/{app_id}/public/submissions/b1.json", headers=auth,
  )
  assert owner_view.status_code == 200 and owner_view.json() == {"name": "Sam"}

  # Anonymous WRITE outside the declared area is refused (admin data is safe).
  assert client.put(
    f"/api/public-apps/{app_id}/storage/public/config.json",
    json={"maxPerSlot": 999}, headers=bearer,
  ).status_code == 403

  # Create-only precondition (If-None-Match: *) enforces one booking per key.
  again = client.put(
    f"/api/public-apps/{app_id}/storage/public/submissions/b1.json",
    json={"name": "Imposter"}, headers={**bearer, "If-None-Match": "*"},
  )
  assert again.status_code == 412

  # Compare-and-swap: a stale If-Match is rejected (capacity stays consistent).
  stale = client.put(
    f"/api/public-apps/{app_id}/storage/public/submissions/b1.json",
    json={"name": "Race"}, headers={**bearer, "If-Match": '"deadbeef"'},
  )
  assert stale.status_code == 412

  # One anonymous value is capped, and the whole write area is capped.
  oversized = client.put(
    f"/api/public-apps/{app_id}/storage/public/submissions/big.txt",
    content=b"x" * (public_storage.PUBLIC_WRITE_MAX_VALUE_BYTES + 1),
    headers={**bearer, "Content-Type": "text/plain"},
  )
  assert oversized.status_code == 413
  monkeypatch.setattr(public_storage, "PUBLIC_WRITE_SUBTREE_MAX_BYTES", 64)
  full = client.put(
    f"/api/public-apps/{app_id}/storage/public/submissions/b2.json",
    json={"name": "y" * 100}, headers=bearer,
  )
  assert full.status_code == 413
  assert client.get(
    f"/api/public-apps/{app_id}/storage/public/submissions/b2.json",
    headers=bearer,
  ).status_code == 404
  monkeypatch.undo()

  # Anonymous listing enumerates the submissions.
  listing = client.get(
    f"/api/public-apps/{app_id}/storage-list/public/submissions",
    headers=bearer,
  )
  assert listing.status_code == 200
  names = {e["name"] for e in listing.json()["entries"]}
  assert "b1.json" in names

  # Anonymous delete inside the write area works; outside is refused.
  assert client.delete(
    f"/api/public-apps/{app_id}/storage/public/submissions/b1.json",
    headers=bearer,
  ).status_code == 204
  assert client.delete(
    f"/api/public-apps/{app_id}/storage/public/config.json", headers=bearer,
  ).status_code == 403


def test_storage_closed_by_default_and_write_needs_declared_prefix(client, auth):
  # An app that only opts into READ cannot be written anonymously.
  app = _create(client, auth, public_access={"storage": {"read": True}})
  app_id = app["id"]
  assert _publish(client, auth, app_id).status_code == 200
  token = _public_token(client, app["slug"])
  bearer = {"Authorization": f"Bearer {token}"}
  assert client.put(
    f"/api/public-apps/{app_id}/storage/public/submissions/x.json",
    json={"a": 1}, headers=bearer,
  ).status_code == 403

  # An app with no storage declaration exposes neither read nor write.
  plain = create_local_app(client, auth, name="No public storage")
  assert _publish(client, auth, plain["id"]).status_code == 200
  plain_token = _public_token(client, plain["slug"])
  plain_bearer = {"Authorization": f"Bearer {plain_token}"}
  assert client.get(
    f"/api/public-apps/{plain['id']}/storage/public/config.json",
    headers=plain_bearer,
  ).status_code == 403


def test_public_host_page_hands_its_static_script_the_exact_app_session(client, auth):
  app = _create(client, auth)
  published = _publish(client, auth, app["id"])
  assert published.status_code == 200
  page = client.get(f"/{app['slug']}")
  assert page.headers["Cache-Control"] == "no-store"
  html = page.text
  # The host logic is the checked-in bundle of frontend/src/publicHost; the
  # page carries only its configuration, and nothing else executes inline.
  assert f'<script type="module" src="/{public_apps.PUBLIC_HOST_SCRIPT}?v=' in html
  assert html.count("<script") == 2
  config = public_host_config(html)
  assert set(config) == {
    "appId", "token", "version", "appInstance", "capabilityContract",
  }
  assert config["appId"] == app["id"]
  assert config["version"].startswith(
    published.json()["hosted_publication"]["revision"],
  )
  assert config["appInstance"]
  # The public host is a reserved slug so no app can shadow its own script.
  assert not public_apps.public_slug_is_available(public_apps.PUBLIC_HOST_SCRIPT)


def test_public_host_exposes_only_the_reviewed_device_storage_capability(client, auth):
  app = create_local_app(
    client,
    auth,
    name="Remember bookings",
    manifest_extra={
      "public_access": STORAGE_ACCESS,
      "capabilities": {
        "device.storage": {
          "version": 1,
          # Owner-authored text lands in the page's JSON slot verbatim, so it
          # must not be able to end that slot early.
          "reason": "Remember bookings </script><script>alert(1)</script>",
          "limits": {"max_bytes": 32768},
        },
        "media.microphone.capture": {
          "version": 1,
          "reason": "Leave a voice note with the booking.",
          "limits": {"max_duration_ms": 8_000},
        },
      },
    },
  )
  assert _publish(client, auth, app["id"]).status_code == 200
  html = client.get(f"/{app['slug']}").text
  assert html.count("</script>") == 2
  config = public_host_config(html)
  installed = app["capability_contract"]["runtime"]
  assert installed["device.storage"]["limits"] == {"max_bytes": 32768}
  assert "media.microphone.capture" in installed
  # The reviewed declaration reaches the host verbatim; the microphone, which
  # this host cannot provide, is withheld entirely rather than stubbed.
  assert config["capabilityContract"] == {
    "runtime": {"device.storage": installed["device.storage"]},
  }

  plain = _create(client, auth, name="No device capabilities")
  assert _publish(client, auth, plain["id"]).status_code == 200
  plain_config = public_host_config(client.get(f"/{plain['slug']}").text)
  assert plain_config["capabilityContract"] == {"runtime": {}}


def test_canonical_public_storage_and_legacy_alias_share_one_namespace(client, auth):
  app = _create(client, auth)
  app_id = app["id"]
  assert _publish(client, auth, app_id).status_code == 200
  token = _public_token(client, app["slug"])
  bearer = {"Authorization": f"Bearer {token}"}

  wrote = client.put(
    "/api/public-storage/public/submissions/canonical.json",
    json={"source": "canonical"}, headers=bearer,
  )
  assert wrote.status_code == 204
  legacy = client.get(
    f"/api/public-apps/{app_id}/storage/public/submissions/canonical.json",
    headers=bearer,
  )
  assert legacy.status_code == 200
  assert legacy.json() == {"source": "canonical"}

  listing = client.get(
    "/api/public-storage",
    params={"prefix": "public/submissions"},
    headers=bearer,
  )
  assert listing.status_code == 200
  assert {item["name"] for item in listing.json()["entries"]} == {
    "canonical.json",
  }


def test_public_storage_is_exact_app_scoped(client, auth):
  app = _create(client, auth, name="First booking")
  other = _create(client, auth, name="Second booking")
  assert _publish(client, auth, app["id"]).status_code == 200
  assert _publish(client, auth, other["id"]).status_code == 200
  token = _public_token(client, app["slug"])
  bearer = {"Authorization": f"Bearer {token}"}
  # One app's public token cannot touch another app's public storage.
  assert client.get(
    f"/api/public-apps/{other['id']}/storage/public/config.json",
    headers=bearer,
  ).status_code == 401
  assert client.put(
    f"/api/public-apps/{other['id']}/storage/public/submissions/x.json",
    json={"a": 1}, headers=bearer,
  ).status_code == 401
