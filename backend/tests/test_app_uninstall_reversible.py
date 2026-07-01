"""Reversible app uninstall (feature 110).

Uninstall must soft-delete (tombstone) a mini-app, preserving its source +
runtime data, so a reinstall reattaches to the SAME numeric id + data instead
of minting a fresh empty app. Recovery is agent-driven and consistent with
chats: POST /api/apps/{id}/recover, plus reinstall-reattach for store apps.
"""

import io
import json
from datetime import datetime, timedelta, UTC
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.parse import urlparse

import pytest

from app import models
from app.config import get_settings

JSX = "export default function App() { return <div>ok</div> }"


@pytest.fixture(autouse=True)
def _bypass_cron_scaffold():
  with patch("app.install.CRON_SCAFFOLD", Path("/nonexistent/scaffold.sh")):
    yield


@pytest.fixture
def bypass_url_validation():
  with patch("app.install._validate_url_safe",
             lambda url: (url, urlparse(url).netloc, urlparse(url).hostname)):
    yield


def _png_bytes() -> bytes:
  from PIL import Image
  buf = io.BytesIO()
  Image.new("RGB", (16, 16), (139, 108, 247)).save(buf, format="PNG")
  return buf.getvalue()


def _make_response(status, body, headers=None):
  r = MagicMock()
  r.status_code = status
  r.content = body
  r.text = body.decode("utf-8", errors="replace")
  r.headers = headers or {}
  return r


class _StreamCtx:
  def __init__(self, status, body, headers=None):
    self._resp = _make_response(status, body, headers)
    self._chunks = [body]

  async def __aenter__(self):
    return self

  async def __aexit__(self, *exc):
    return False

  def __getattr__(self, name):
    return getattr(self._resp, name)

  async def aiter_bytes(self):
    for chunk in self._chunks:
      yield chunk


def _fake_async_client(responses):
  class _FakeClient:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *exc):
      return False

    def stream(self, method, url, **kwargs):
      if url not in responses:
        return _StreamCtx(404, b"")
      return _StreamCtx(*responses[url])

  return lambda *a, **kw: _FakeClient()


MANIFEST = {
  "id": "revtest",
  "name": "Rev Test",
  "version": "1.0.0",
  "description": "reversible-uninstall fixture",
  "entry": "index.jsx",
  "icon": "icon.png",
  "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  "runtime": {"imports": ["react"], "esm_deps": []},
}
BASE = "https://raw.githubusercontent.com/x/app-revtest/main/"


def _install(client, auth, manifest=MANIFEST, base=BASE):
  responses = {
    base + "mobius.json": (200, json.dumps(manifest).encode()),
    base + "index.jsx": (200, JSX.encode()),
    base + "icon.png": (200, _png_bytes()),
  }
  with patch("app.install.httpx.AsyncClient",
             side_effect=_fake_async_client(responses)):
    r = client.post("/api/apps/install", headers=auth,
                    json={"manifest_url": base + "mobius.json"})
  assert r.status_code == 201, r.text
  return r.json()


def _seed_data(app_id, name="entries.json", body="[1,2,3]"):
  """Write a runtime data file to the id-keyed storage tree."""
  d = Path(get_settings().data_dir) / "apps" / str(app_id)
  d.mkdir(parents=True, exist_ok=True)
  (d / name).write_text(body)
  return d / name


def test_uninstall_soft_deletes_and_preserves_data(
  client, auth, db, bypass_url_validation,
):
  app = _install(client, auth)
  app_id = app["id"]
  data_file = _seed_data(app_id)

  r = client.delete(f"/api/apps/{app_id}", headers=auth)
  assert r.status_code == 204, r.text

  # Row tombstoned, not gone; data tree preserved.
  row = db.query(models.App).filter(models.App.id == app_id).first()
  assert row is not None
  assert row.deleted_at is not None
  assert data_file.exists()
  assert data_file.read_text() == "[1,2,3]"

  # Hidden from the drawer list and a direct fetch.
  listed = client.get("/api/apps/", headers=auth).json()
  assert app_id not in [a["id"] for a in listed]
  assert client.get(f"/api/apps/{app_id}", headers=auth).status_code == 404


def test_reinstall_reattaches_same_id_and_data(
  client, auth, db, bypass_url_validation,
):
  app = _install(client, auth)
  app_id = app["id"]
  data_file = _seed_data(app_id, body="kept")

  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204

  again = _install(client, auth)  # same manifest_url
  assert again["mode"] == "update"       # revived, not a fresh install
  assert again["id"] == app_id           # SAME numeric id
  assert again["slug"] == app["slug"]    # no slug flip

  row = db.query(models.App).filter(models.App.id == app_id).first()
  assert row.deleted_at is None
  assert data_file.exists() and data_file.read_text() == "kept"
  assert app_id in [a["id"] for a in client.get("/api/apps/", headers=auth).json()]


def test_recover_endpoint_restores_app(client, auth, db, bypass_url_validation):
  app = _install(client, auth)
  app_id = app["id"]
  data_file = _seed_data(app_id, body="recover-me")
  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204

  r = client.post(f"/api/apps/{app_id}/recover", headers=auth)
  assert r.status_code == 200, r.text
  assert r.json()["ok"] is True

  db.expire_all()
  row = db.query(models.App).filter(models.App.id == app_id).first()
  assert row.deleted_at is None
  assert data_file.read_text() == "recover-me"
  assert app_id in [a["id"] for a in client.get("/api/apps/", headers=auth).json()]


def test_recover_expired_returns_410(client, auth, db, bypass_url_validation):
  app = _install(client, auth)
  app_id = app["id"]
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()

  r = client.post(f"/api/apps/{app_id}/recover", headers=auth)
  assert r.status_code == 410


def test_recover_active_returns_404(client, auth, bypass_url_validation):
  app = _install(client, auth)
  r = client.post(f"/api/apps/{app['id']}/recover", headers=auth)
  assert r.status_code == 404


def test_purge_after_ttl_hard_deletes(client, auth, db, bypass_url_validation):
  app = _install(client, auth)
  app_id = app["id"]
  data_file = _seed_data(app_id)
  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204

  # Age the tombstone past the TTL, then a list call sweeps it.
  row = db.query(models.App).filter(models.App.id == app_id).first()
  row.deleted_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=8)
  db.commit()

  client.get("/api/apps/", headers=auth)

  db.expire_all()
  assert db.query(models.App).filter(models.App.id == app_id).first() is None
  assert not data_file.exists()


def test_tombstoned_app_module_and_frame_404(
  client, auth, owner_token, bypass_url_validation,
):
  app = _install(client, auth)
  app_id = app["id"]
  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  # A cached deep-link must not render a deleted app. The module endpoint takes
  # the token as a query param (dynamic import() can't set headers); the frame
  # is public.
  assert client.get(
    f"/api/apps/{app_id}/module?token={owner_token}"
  ).status_code == 404
  assert client.get(f"/api/apps/{app_id}/frame").status_code == 404


def test_tombstoned_app_standalone_route_404(
  client, auth, bypass_url_validation,
):
  app = _install(client, auth)
  app_id, slug = app["id"], app["slug"]
  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  # A home-screen PWA deep-link must not render an uninstalled app.
  assert client.get(f"/apps/{slug}/manifest.json").status_code == 404


def test_tombstoned_app_cannot_mint_token(
  client, auth, owner_token, bypass_url_validation,
):
  """No fresh app authority for an uninstalled app — the mint endpoint 404s,
  which also keeps its cron/run-job endpoints unreachable. Revive makes it
  mintable again (security contract, feature 110 Q12)."""
  app = _install(client, auth)
  app_id = app["id"]
  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  r = client.post("/api/auth/app-token", json={"app_id": app_id},
                  headers={"Authorization": f"Bearer {owner_token}"})
  assert r.status_code == 404


def test_tombstoned_app_no_slug_redirect_or_static(
  client, auth, bypass_url_validation,
):
  """The /<slug> alias redirect and /app-assets static resolver both treat a
  tombstoned app as absent (feature 110)."""
  from app.main import (
    _top_level_app_slug_alias, _app_source_dir_for_static_asset,
  )
  app = _install(client, auth)
  app_id, slug = app["id"], app["slug"]
  assert _top_level_app_slug_alias(slug) == slug
  assert _app_source_dir_for_static_asset(slug=slug) is not None

  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  assert _top_level_app_slug_alias(slug) is None
  assert _app_source_dir_for_static_asset(slug=slug) is None
  assert _app_source_dir_for_static_asset(app_id=app_id) is None


# --- delete app data (wipe storage, keep app installed) ---


def test_delete_app_data_wipes_storage_and_keeps_app(
  client, auth, db, bypass_url_validation,
):
  """DELETE /api/apps/{id}/data must wipe the id-keyed storage tree while
  leaving the app fully installed: the DB row stays live (deleted_at IS NULL),
  the app is still listed, and only the runtime data is gone."""
  app = _install(client, auth)
  app_id = app["id"]
  data_file = _seed_data(app_id, body="wipe-me")
  storage_dir = Path(get_settings().data_dir) / "apps" / str(app_id)
  assert data_file.exists()

  r = client.delete(f"/api/apps/{app_id}/data", headers=auth)
  assert r.status_code == 204, r.text

  # Storage tree gone.
  assert not data_file.exists()
  assert not storage_dir.exists()

  # App row still LIVE — not tombstoned, not hard-deleted.
  db.expire_all()
  row = db.query(models.App).filter(models.App.id == app_id).first()
  assert row is not None
  assert row.deleted_at is None

  # Still listed and still reachable (unlike an uninstall, the app stays).
  listed = client.get("/api/apps/", headers=auth).json()
  assert app_id in [a["id"] for a in listed]
  assert client.get(f"/api/apps/{app_id}", headers=auth).status_code == 200


def test_delete_app_data_404s_tombstoned(client, auth, db, bypass_url_validation):
  """A wipe on an uninstalled (tombstoned) app 404s — it resolves through the
  live-app filter, so a data wipe can't touch a soft-deleted app's preserved
  tree (which the recovery window still owns)."""
  app = _install(client, auth)
  app_id = app["id"]
  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  assert client.delete(f"/api/apps/{app_id}/data", headers=auth).status_code == 404


def test_delete_app_data_on_empty_tree_succeeds(
  client, auth, db, bypass_url_validation,
):
  """Wiping an app that has never written data is a no-op success — the
  rmtree is ignore_errors, so an absent tree is fine."""
  app = _install(client, auth)
  app_id = app["id"]
  r = client.delete(f"/api/apps/{app_id}/data", headers=auth)
  assert r.status_code == 204, r.text
  assert app_id in [a["id"] for a in client.get("/api/apps/", headers=auth).json()]


# --- feature 113: soft-delete tooling (live_app helper, now_naive_utc) ---


def test_validate_app_404s_tombstoned(client, auth, db, bypass_url_validation):
  """A tombstoned app must not validate.

  validate_app was the one per-app read path the feature-110 sweep AND two
  reviews all missed — it queried by id with no deleted_at filter. This is
  the regression guard for the 113 live_app_or_404 conversion.
  """
  app = _install(client, auth)
  app_id = app["id"]
  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  assert client.get(
    f"/api/apps/{app_id}/validate", headers=auth
  ).status_code == 404


def test_app_read_endpoints_hide_tombstoned(
  client, auth, db, bypass_url_validation,
):
  """Every owner-facing per-app GET resolves through the live-app filter, so a
  future read endpoint that forgets it fails CI (the scatter 113 centralizes)."""
  app = _install(client, auth)
  app_id = app["id"]
  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  for path in (
    f"/api/apps/{app_id}",
    f"/api/apps/{app_id}/validate",
    f"/api/apps/{app_id}/update-preview",
  ):
    assert client.get(path, headers=auth).status_code == 404, path


def test_live_app_helpers(client, auth, db, bypass_url_validation):
  """live_app returns None / live_app_or_404 raises 404 for a tombstoned or
  missing app; both return the row when live (the App analogue of
  get_active_chat_or_404)."""
  from fastapi import HTTPException
  from app.resource_access import live_app, live_app_or_404
  app = _install(client, auth)
  app_id = app["id"]
  assert live_app(db, app_id) is not None
  assert live_app_or_404(db, app_id).id == app_id

  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  db.expire_all()
  assert live_app(db, app_id) is None
  with pytest.raises(HTTPException) as ei:
    live_app_or_404(db, app_id)
  assert ei.value.status_code == 404

  assert live_app(db, 10_000_001) is None
  with pytest.raises(HTTPException):
    live_app_or_404(db, 10_000_001)


def test_now_naive_utc_returns_naive():
  """now_naive_utc centralizes the datetime.now(UTC).replace(tzinfo=None)
  boilerplate the soft-delete write/compare sites repeat (App wrote naive,
  Chat wrote aware — this aligns them)."""
  from app.timeutil import now_naive_utc
  v = now_naive_utc()
  assert v.tzinfo is None


# --- cron-tombstone replay robustness (entrypoint replays init-cron.sh) ---


def _write_init_cron(source_dir: Path) -> Path:
  """Drop a harmless init-cron.sh into an app's source tree.

  The real scaffold installs a crontab entry; this stub just exits 0 so
  _reenable_init_cron_replay's one-shot run touches nothing global but still
  exercises the rename + bash-run path.
  """
  script = source_dir / "init-cron.sh"
  script.write_text("#!/bin/bash\nexit 0\n")
  return script


def test_tombstone_disables_init_cron_replay(
  client, auth, db, bypass_url_validation,
):
  """Tombstoning a scheduled app moves init-cron.sh aside so entrypoint.sh's
  boot replay (which runs every /data/apps/*/init-cron.sh) can't resurrect the
  schedule the tombstone just dropped."""
  app = _install(client, auth)
  app_id = app["id"]
  source_dir = Path(
    db.query(models.App).filter(models.App.id == app_id).first().source_dir
  )
  script = _write_init_cron(source_dir)
  tombstoned = source_dir / "init-cron.sh.tombstoned"

  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204

  # init-cron.sh renamed outside the entrypoint's */init-cron.sh glob.
  assert not script.exists()
  assert tombstoned.exists()


def test_recover_reenables_init_cron_replay(
  client, auth, db, bypass_url_validation,
):
  """Recovery renames init-cron.sh.tombstoned back into the replay glob (and
  re-runs it) so the revived app's schedule is re-armed — recovery undoes every
  side-effect the delete tore down."""
  app = _install(client, auth)
  app_id = app["id"]
  source_dir = Path(
    db.query(models.App).filter(models.App.id == app_id).first().source_dir
  )
  script = _write_init_cron(source_dir)
  tombstoned = source_dir / "init-cron.sh.tombstoned"

  assert client.delete(f"/api/apps/{app_id}", headers=auth).status_code == 204
  assert tombstoned.exists() and not script.exists()

  assert client.post(
    f"/api/apps/{app_id}/recover", headers=auth
  ).status_code == 200

  # Renamed back into place; the tombstoned copy is gone.
  assert script.exists()
  assert not tombstoned.exists()
