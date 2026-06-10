"""P1-D: offline contract manifest block — validation + migration + AppOut.

Four test groups:

  validate_manifest_offline  — the pure validator function
  install_offline_contract   — manifest install stores + returns the block
  migration                  — ALTER TABLE adds offline_contract on upgrade
  appout_schema              — AppOut includes the new field
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, inspect, text

from app.install import _validate_manifest_offline
from app.database import run_migrations


# ──────────────────────────────────────────────────────────────────────────────
# Helpers shared by the install group (mirrors test_apps_install.py pattern)
# ──────────────────────────────────────────────────────────────────────────────

JSX = "export default function App() { return <div>ok</div> }"
PROMPT = "# default prompt\nDo the work.\n"

MANIFEST_BASE = {
  "id": "test-offline-contract",
  "name": "Test Offline Contract",
  "version": "1.0.0",
  "description": "Contract test app",
  "entry": "index.jsx",
  "icon": "icon.png",
  "permissions": {"cross_app_access": "none", "share_with_apps": "none"},
  "runtime": {"imports": ["react"], "esm_deps": []},
}


def _make_response(status: int, body: bytes, headers: dict | None = None):
  r = MagicMock()
  r.status_code = status
  r.content = body
  r.text = body.decode("utf-8", errors="replace")
  r.headers = headers or {}
  r.json = lambda: json.loads(body.decode("utf-8"))
  return r


class _StreamCtx:
  def __init__(self, status, body, headers=None):
    self._resp = _make_response(status, body, headers)
    self._body = body

  async def __aenter__(self):
    return self

  async def __aexit__(self, *exc):
    return False

  def __getattr__(self, name):
    return getattr(self._resp, name)

  async def aiter_bytes(self):
    yield self._body


def _fake_async_client(responses: dict):
  class _FakeClient:
    async def __aenter__(self):
      return self

    async def __aexit__(self, *exc):
      return False

    def stream(self, method, url, **kwargs):
      if url not in responses:
        return _StreamCtx(404, b"")
      tup = responses[url]
      status, body = tup[0], tup[1]
      hdrs = tup[2] if len(tup) > 2 else None
      return _StreamCtx(status, body, headers=hdrs)

  return lambda *a, **kw: _FakeClient()


def _png_bytes() -> bytes:
  from PIL import Image
  import io
  buf = io.BytesIO()
  Image.new("RGB", (16, 16), (80, 150, 220)).save(buf, format="PNG")
  return buf.getvalue()


@pytest.fixture(autouse=True)
def _bypass_cron_scaffold():
  with patch("app.install.CRON_SCAFFOLD", Path("/nonexistent/scaffold.sh")):
    yield


@pytest.fixture(autouse=True)
def _stub_resolver_run_chat():
  async def _noop(*args, **kwargs):
    return None
  with patch("app.chat.run_chat", new=_noop):
    yield


@pytest.fixture
def bypass_url_validation():
  with patch("app.install._validate_url_safe",
             lambda url: (url, urlparse(url).netloc, urlparse(url).hostname)):
    yield


# ──────────────────────────────────────────────────────────────────────────────
# Group 1: pure validator
# ──────────────────────────────────────────────────────────────────────────────

class TestValidateManifestOffline:
  """Unit tests for _validate_manifest_offline — no DB, no HTTP."""

  def test_none_is_accepted(self):
    """Absent offline block is always valid (most manifests omit it)."""
    _validate_manifest_offline(None)  # must not raise

  def test_full_valid_block_accepted(self):
    _validate_manifest_offline({
      "reads": True,
      "writes": "queued",
      "execution": "full",
      "precache": [],
    })

  def test_minimal_empty_dict_accepted(self):
    _validate_manifest_offline({})

  def test_not_a_dict_rejected(self):
    with pytest.raises(HTTPException) as exc:
      _validate_manifest_offline("full")
    assert exc.value.status_code == 400

  def test_reads_must_be_bool(self):
    with pytest.raises(HTTPException) as exc:
      _validate_manifest_offline({"reads": "yes"})
    assert exc.value.status_code == 400
    assert "reads" in str(exc.value.detail)

  def test_writes_invalid_value_rejected(self):
    with pytest.raises(HTTPException) as exc:
      _validate_manifest_offline({"writes": "async"})
    assert exc.value.status_code == 400
    assert "writes" in str(exc.value.detail)

  def test_writes_valid_values_accepted(self):
    for v in ("queued", "none"):
      _validate_manifest_offline({"writes": v})  # must not raise

  def test_execution_invalid_value_rejected(self):
    with pytest.raises(HTTPException) as exc:
      _validate_manifest_offline({"execution": "maybe"})
    assert exc.value.status_code == 400
    assert "execution" in str(exc.value.detail)

  def test_execution_valid_values_accepted(self):
    for v in ("full", "partial", "none"):
      _validate_manifest_offline({"execution": v})

  def test_precache_must_be_list(self):
    with pytest.raises(HTTPException) as exc:
      _validate_manifest_offline({"precache": "index.html"})
    assert exc.value.status_code == 400
    assert "precache" in str(exc.value.detail)

  def test_precache_items_must_be_relative_paths(self):
    # Absolute path should be rejected by _validate_repo_relative_path.
    with pytest.raises(HTTPException):
      _validate_manifest_offline({"precache": ["/etc/passwd"]})

  def test_precache_traversal_rejected(self):
    with pytest.raises(HTTPException):
      _validate_manifest_offline({"precache": ["../sibling/secret.txt"]})

  def test_precache_valid_paths_accepted(self):
    _validate_manifest_offline({"precache": ["assets/logo.png", "index.html"]})

  def test_reads_false_accepted(self):
    _validate_manifest_offline({"reads": False})

  def test_extra_keys_tolerated(self):
    """Manifest may contain forward-compatible fields we don't recognise yet."""
    _validate_manifest_offline({"reads": True, "future_key": "anything"})


# ──────────────────────────────────────────────────────────────────────────────
# Group 2: install stores and returns the offline block
# ──────────────────────────────────────────────────────────────────────────────

class TestInstallOfflineContract:
  """Install/update via /api/apps/install persists and returns offline_contract."""

  def _do_install(self, client, auth, offline_block=None, bypass_url_validation=None):
    """Install test-offline-contract; inject offline block when given."""
    base = "https://raw.githubusercontent.com/test/app-oc/main/"
    manifest = dict(MANIFEST_BASE)
    if offline_block is not None:
      manifest["offline"] = offline_block

    responses = {
      base + "mobius.json": (200, json.dumps(manifest).encode()),
      base + "index.jsx": (200, JSX.encode()),
      base + "icon.png": (200, _png_bytes()),
    }
    with patch(
      "app.install.httpx.AsyncClient",
      side_effect=_fake_async_client(responses),
    ):
      r = client.post("/api/apps/install", headers=auth, json={
        "manifest_url": base + "mobius.json",
      })
    return r

  def test_install_without_offline_block_returns_none(
    self, client, auth, bypass_url_validation,
  ):
    r = self._do_install(client, auth)
    assert r.status_code == 201, r.text
    assert r.json()["offline_contract"] is None

  def test_install_with_offline_block_returns_block(
    self, client, auth, bypass_url_validation,
  ):
    block = {"reads": True, "writes": "queued", "execution": "full", "precache": []}
    r = self._do_install(client, auth, offline_block=block)
    assert r.status_code == 201, r.text
    assert r.json()["offline_contract"] == block

  def test_install_offline_block_appears_in_list(
    self, client, auth, bypass_url_validation,
  ):
    block = {"reads": False, "execution": "partial"}
    r = self._do_install(client, auth, offline_block=block)
    app_id = r.json()["id"]
    apps = client.get("/api/apps/", headers=auth).json()
    row = next(a for a in apps if a["id"] == app_id)
    assert row["offline_contract"] == block

  def test_install_invalid_offline_block_rejected(
    self, client, auth, bypass_url_validation,
  ):
    base = "https://raw.githubusercontent.com/test/app-oc-bad/main/"
    manifest = dict(MANIFEST_BASE)
    manifest["id"] = "test-oc-bad"
    manifest["offline"] = {"writes": "instantly"}  # invalid enum value

    responses = {
      base + "mobius.json": (200, json.dumps(manifest).encode()),
      base + "index.jsx": (200, JSX.encode()),
      base + "icon.png": (200, _png_bytes()),
    }
    with patch(
      "app.install.httpx.AsyncClient",
      side_effect=_fake_async_client(responses),
    ):
      r = client.post("/api/apps/install", headers=auth, json={
        "manifest_url": base + "mobius.json",
      })
    assert r.status_code == 400, r.text

  def test_update_overwrites_offline_block(
    self, client, auth, bypass_url_validation,
  ):
    """Re-installing (update path) replaces the stored offline_contract."""
    # First install — with a block.
    block_v1 = {"reads": True, "execution": "full"}
    r1 = self._do_install(client, auth, offline_block=block_v1)
    assert r1.status_code == 201, r1.text
    app_id = r1.json()["id"]

    # Update — different block.
    base = "https://raw.githubusercontent.com/test/app-oc/main/"
    manifest_v2 = dict(MANIFEST_BASE)
    manifest_v2["version"] = "2.0.0"
    manifest_v2["offline"] = {"reads": False, "execution": "none"}
    responses = {
      base + "mobius.json": (200, json.dumps(manifest_v2).encode()),
      base + "index.jsx": (200, JSX.encode()),
      base + "icon.png": (200, _png_bytes()),
    }
    with patch(
      "app.install.httpx.AsyncClient",
      side_effect=_fake_async_client(responses),
    ):
      r2 = client.post("/api/apps/install", headers=auth, json={
        "manifest_url": base + "mobius.json",
      })
    # The install endpoint always returns 201 regardless of install vs update mode.
    assert r2.status_code == 201, r2.text
    payload2 = r2.json()
    assert payload2["mode"] == "update"
    assert payload2["offline_contract"] == {"reads": False, "execution": "none"}

    listed = client.get("/api/apps/", headers=auth).json()
    row = next(a for a in listed if a["id"] == app_id)
    assert row["offline_contract"] == {"reads": False, "execution": "none"}


# ──────────────────────────────────────────────────────────────────────────────
# Group 3: migration adds the column on an existing apps table
# ──────────────────────────────────────────────────────────────────────────────

class TestOfflineContractMigration:
  """run_migrations() adds offline_contract to pre-existing apps tables."""

  def test_column_added_to_legacy_table(self, tmp_path):
    """Simulates an existing DB that was created before P1-D shipped."""
    db_path = tmp_path / "legacy.db"
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.connect() as conn:
      conn.execute(text(
        "CREATE TABLE apps ("
        "id INTEGER PRIMARY KEY, "
        "name VARCHAR(255) NOT NULL"
        ")"
      ))
      conn.commit()

    run_migrations(eng)

    inspector = inspect(eng)
    cols = {c["name"] for c in inspector.get_columns("apps")}
    assert "offline_contract" in cols

  def test_migration_is_idempotent(self, tmp_path):
    """Running run_migrations twice must not raise (column already exists)."""
    db_path = tmp_path / "legacy.db"
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.connect() as conn:
      conn.execute(text(
        "CREATE TABLE apps ("
        "id INTEGER PRIMARY KEY, "
        "name VARCHAR(255) NOT NULL"
        ")"
      ))
      conn.commit()

    run_migrations(eng)
    run_migrations(eng)  # must not raise

  def test_column_is_nullable_json(self, tmp_path):
    """Existing rows survive the migration with NULL in the new column."""
    db_path = tmp_path / "legacy.db"
    eng = create_engine(f"sqlite:///{db_path}")
    with eng.connect() as conn:
      conn.execute(text(
        "CREATE TABLE apps ("
        "id INTEGER PRIMARY KEY, "
        "name VARCHAR(255) NOT NULL"
        ")"
      ))
      conn.execute(text("INSERT INTO apps (id, name) VALUES (1, 'OldApp')"))
      conn.commit()

    run_migrations(eng)

    with eng.connect() as conn:
      row = conn.execute(text("SELECT offline_contract FROM apps WHERE id=1")).fetchone()
    assert row is not None
    assert row[0] is None  # existing row untouched, new column is NULL
