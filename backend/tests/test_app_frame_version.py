"""The shell reloads an app frame only when what the frame executes changes.

`frame_version` is the shell's reload key. Every App row write advances
`updated_at`, so keying frames on it reloaded running apps (and lost their
place) on a pin, rename, or permission change.
"""

import asyncio
from pathlib import Path

from app import compiler, models
from app.app_compile_contract import app_frame_version
from test_app_fixtures import create_local_app


BUNDLE = "/data/compiled/app-7-" + "a" * 64 + ".js"
CONTRACT = {
  "runtime": {"camera": {"version": 1, "reason": "Scan"}},
  "permissions": {"cross_app_access": "none"},
}


def frame_app(**overrides):
  return models.App(**{
    "compiled_path": BUNDLE,
    "capability_contract": CONTRACT,
    "token_nonce": "nonce-1",
    "runtime_revision": "revision-1",
    **overrides,
  })


def test_frame_version_tracks_bundle_runtime_and_storage_generation():
  base = app_frame_version(frame_app())

  assert base == app_frame_version(frame_app(capability_contract=dict(CONTRACT)))
  assert len(base) == 20 and "nonce-1" not in base
  assert app_frame_version(frame_app(
    compiled_path=BUNDLE.replace("a" * 64, "b" * 64),
  )) != base
  assert app_frame_version(frame_app(
    capability_contract={**CONTRACT, "runtime": {}},
  )) != base
  assert app_frame_version(frame_app(token_nonce="nonce-2")) != base


def test_frame_version_ignores_server_permissions_and_bundle_directory():
  base = app_frame_version(frame_app())
  granted = {**CONTRACT, "permissions": {"cross_app_access": "read"}}

  assert app_frame_version(frame_app(capability_contract=granted)) == base
  assert app_frame_version(frame_app(
    compiled_path="/elsewhere/" + Path(BUNDLE).name,
  )) == base


def test_settings_writes_keep_frame_version(client, auth):
  app = create_local_app(client, auth, name="Reader", description="test")
  before = client.get(f"/api/apps/{app['id']}", headers=auth).json()

  pinned = client.patch(
    f"/api/apps/{app['id']}", json={"pinned": True}, headers=auth,
  )
  renamed = client.patch(
    f"/api/apps/{app['id']}",
    json={"name": "Reader 2", "share_with_apps": "read"},
    headers=auth,
  )

  assert pinned.status_code == 200, pinned.text
  assert renamed.status_code == 200, renamed.text
  after = renamed.json()
  assert after["updated_at"] != before["updated_at"]
  assert after["frame_version"] == before["frame_version"]
  assert after["storage_generation"] == before["storage_generation"]
  assert "token_nonce" not in after
  assert "runtime_revision" not in after


def test_code_change_and_data_wipe_rotate_frame_version(
  client, auth, db, monkeypatch,
):
  app = create_local_app(client, auth, name="Notes", description="test")
  initial = client.get(f"/api/apps/{app['id']}", headers=auth).json()

  async def fake_compile(jsx, *, out_path=None, source_path=None):
    Path(out_path).write_text("// a different bundle\n", encoding="utf-8")

  monkeypatch.setattr(compiler, "compile_jsx", fake_compile)
  row = db.query(models.App).filter(models.App.id == app["id"]).one()
  asyncio.run(compiler.recompile_app_bundle(db, row, row.jsx_source))
  rebuilt = client.get(f"/api/apps/{app['id']}", headers=auth).json()
  assert rebuilt["frame_version"] != initial["frame_version"]
  assert rebuilt["storage_generation"] == initial["storage_generation"]

  wiped = client.delete(f"/api/apps/{app['id']}/data", headers=auth)
  assert wiped.status_code in (200, 204), wiped.text
  after_wipe = client.get(f"/api/apps/{app['id']}", headers=auth).json()
  assert after_wipe["frame_version"] != rebuilt["frame_version"]
  assert after_wipe["storage_generation"] != rebuilt["storage_generation"]


def test_standalone_boot_uses_the_same_frame_version(client, auth, db):
  from app.routes.standalone import _standalone_boot_payload

  app = create_local_app(client, auth, name="Solo", description="test")
  listed = client.get(f"/api/apps/{app['id']}", headers=auth).json()
  row = db.query(models.App).filter(models.App.id == app["id"]).one()

  boot = _standalone_boot_payload(row)
  assert boot["frame_version"] == listed["frame_version"]
  assert boot["storage_generation"] == listed["storage_generation"]
  assert row.token_nonce not in str(boot)


def test_assets_only_update_rotates_frame_version_but_settings_do_not(
  client, auth, db,
):
  from app.routes.standalone import _standalone_boot_payload

  app = create_local_app(client, auth, name="Static reader", description="test")
  row = db.query(models.App).filter(models.App.id == app["id"]).one()
  row.runtime_revision = "a" * 64
  db.commit()
  before = client.get(f"/api/apps/{app['id']}", headers=auth).json()

  # Static-app packaging keeps the wrapper bundle and declarations unchanged.
  row.runtime_revision = "b" * 64
  db.commit()
  updated = client.get(f"/api/apps/{app['id']}", headers=auth).json()
  assert updated["compiled_path"] == before["compiled_path"]
  assert updated["capability_contract"] == before["capability_contract"]
  assert updated["frame_version"] != before["frame_version"]
  assert updated["storage_generation"] == before["storage_generation"]
  assert "runtime_revision" not in updated
  assert _standalone_boot_payload(row)["frame_version"] == updated["frame_version"]

  pinned = client.patch(
    f"/api/apps/{app['id']}", json={"pinned": True}, headers=auth,
  )
  assert pinned.status_code == 200, pinned.text
  assert pinned.json()["frame_version"] == updated["frame_version"]
  db.refresh(row)
  assert row.runtime_revision == "b" * 64
