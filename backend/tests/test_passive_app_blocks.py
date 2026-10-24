"""Passive execution requires owner acceptance and an opted-in applied revision."""

from copy import deepcopy
import json
import shutil
from pathlib import Path
from unittest.mock import patch

from app import models
from app.app_capabilities import (
  RUNTIME_CAPABILITY_DEFINITIONS, _applied_passive_digest, contract_and_digest, diff_contracts,
  passive_block_module_digest,
)
from app.applied_app_runtime import runtime_parent
from app.compiler import app_bundle_digest
from test_app_fixtures import create_local_app


CAPABILITY = "chat.blocks.passive"


def test_passive_opt_in_is_a_reviewable_widening_with_no_legacy_default():
  before, _ = contract_and_digest({})
  after, _ = contract_and_digest({"capabilities": {CAPABILITY: {"version": 1}}})
  assert CAPABILITY not in before["runtime"]
  assert diff_contracts(before, after)["widens"] is True
  assert diff_contracts(after, before)["widens"] is False


def test_consent_text_says_confirm_can_make_the_app_act():
  definition = RUNTIME_CAPABILITY_DEFINITIONS[CAPABILITY]
  assert definition["risk"] == "execution"
  assert "Confirm" in definition["description"]
  assert "publish" in definition["description"]


def test_opted_in_applied_revision_admits_its_exact_compiled_digest(client, auth, db):
  created = create_local_app(client, auth, capabilities={CAPABILITY: {"version": 1}})
  app = db.get(models.App, created["id"])
  digest = app_bundle_digest(app.id, app.compiled_path)
  assert digest and created["passive_block_module_digest"] == digest
  assert passive_block_module_digest(app) == digest
  response = client.get("/api/apps/", headers=auth)
  assert response.status_code == 200, response.text
  row = next(item for item in response.json() if item["id"] == app.id)
  assert row["passive_block_module_digest"] == digest
  assert "runtime_revision" not in row and "source_commit" not in row
  frame = client.get(f"/api/apps/{app.id}/frame")
  assert f'var _FRAME_PASSIVE_BLOCK_DIGEST = "{digest}"' in frame.text
  assert f"p{digest[:16]}" in frame.headers["etag"]
  # Dirty source declarations neither revoke accepted code nor admit other code.
  manifest_path = Path(app.source_dir) / "mobius.json"
  manifest = json.loads(manifest_path.read_text())
  manifest["capabilities"] = {}
  manifest_path.write_text(json.dumps(manifest))
  assert passive_block_module_digest(app) == digest
  # Revoking the owner's grant takes effect immediately, despite the cache.
  app.capability_contract = deepcopy(app.capability_contract)
  app.capability_contract["runtime"].pop(CAPABILITY)
  db.commit()
  assert passive_block_module_digest(app) is None
  frame = client.get(f"/api/apps/{app.id}/frame")
  assert "var _FRAME_PASSIVE_BLOCK_DIGEST = null" in frame.text


def test_new_local_acceptance_cannot_admit_a_legacy_served_revision(client, auth, db):
  created = create_local_app(client, auth)
  app = db.get(models.App, created["id"])
  assert created["passive_block_module_digest"] is None
  manifest_path = Path(app.source_dir) / "mobius.json"
  manifest = json.loads(manifest_path.read_text())
  manifest["capabilities"] = {CAPABILITY: {"version": 1}}
  manifest_path.write_text(json.dumps(manifest))
  assert passive_block_module_digest(app) is None
  # Even explicit acceptance of the edited declaration is not source Apply.
  app.capability_contract = contract_and_digest(manifest)[0]
  db.commit()
  assert passive_block_module_digest(app) is None
  assert "var _FRAME_PASSIVE_BLOCK_DIGEST = null" in client.get(f"/api/apps/{app.id}/frame").text


def test_serialization_reads_the_applied_manifest_once_per_immutable_revision(client, auth, db):
  created = create_local_app(client, auth, name="Cached passive", capabilities={CAPABILITY: {"version": 1}})
  app = db.get(models.App, created["id"])
  _applied_passive_digest.cache_clear()
  real_open = Path.open
  opened = []

  def counting_open(path, *args, **kwargs):
    if path.name == "mobius.json":
      opened.append(path)
    return real_open(path, *args, **kwargs)

  with patch.object(Path, "open", counting_open):
    for _ in range(3):
      assert client.get("/api/apps/", headers=auth).status_code == 200
  assert len(opened) == 1


def test_missing_runtime_cache_is_unadmitted_without_rebuilding_or_caching(client, auth, db):
  created = create_local_app(client, auth, name="Evicted passive", capabilities={CAPABILITY: {"version": 1}})
  app = db.get(models.App, created["id"])
  digest = created["passive_block_module_digest"]
  _applied_passive_digest.cache_clear()
  root = runtime_parent(app.id) / app.runtime_revision
  moved = root.with_name(root.name + ".moved")
  root.rename(moved)
  try:
    with patch("app.applied_app_runtime.prepare_runtime") as rebuild:
      assert passive_block_module_digest(app) is None
      row = next(item for item in client.get("/api/apps/", headers=auth).json() if item["id"] == app.id)
      assert row["passive_block_module_digest"] is None
      rebuild.assert_not_called()
    assert not root.exists()
  finally:
    if root.exists():
      shutil.rmtree(moved)
    else:
      moved.rename(root)
  # The unavailable answer was not cached: the restored tree admits again.
  assert passive_block_module_digest(app) == digest


def test_in_repo_browser_fixture_applies_as_an_admitted_session_app(client, auth, db):
  """tests/app-blocks.spec.mjs installs this exact fixture; keep it compiling."""
  fixture = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "app-block-session.jsx"
  created = create_local_app(client, auth, name="Block fixture", jsx_source=fixture.read_text(),
                             capabilities={CAPABILITY: {"version": 1}})
  app = db.get(models.App, created["id"])
  assert created["passive_block_module_digest"] == app_bundle_digest(app.id, app.compiled_path)
  assert "appBlockSessions" in Path(app.compiled_path).read_text()
