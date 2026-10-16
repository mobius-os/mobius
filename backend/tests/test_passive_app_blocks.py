"""Passive execution requires owner acceptance and an opted-in served revision."""

from copy import deepcopy
import json
from pathlib import Path

from app import models
from app.app_capabilities import contract_and_digest, diff_contracts, passive_block_module_digest
from app.compiler import app_bundle_digest
from test_app_fixtures import create_local_app


CAPABILITY = "chat.blocks.passive"


def test_passive_opt_in_is_a_reviewable_widening_with_no_legacy_default():
  before, _ = contract_and_digest({})
  after, _ = contract_and_digest({"capabilities": {CAPABILITY: {"version": 1}}})
  assert CAPABILITY not in before["runtime"]
  assert diff_contracts(before, after)["widens"] is True
  assert diff_contracts(after, before)["widens"] is False


def test_opted_in_applied_revision_admits_its_exact_compiled_digest(client, auth, db):
  created = create_local_app(client, auth, capabilities={CAPABILITY: {"version": 1}})
  app = db.get(models.App, created["id"])
  digest = app_bundle_digest(app.id, app.compiled_path)
  assert digest and created["passive_block_module_digest"] == digest
  assert passive_block_module_digest(app) == digest
  response = client.get("/api/apps/", headers=auth)
  assert response.status_code == 200, response.text
  listed = response.json()
  row = next(item for item in listed if item["id"] == app.id)
  assert row["passive_block_module_digest"] == digest
  assert "runtime_revision" not in row and "source_commit" not in row
  html = client.get(f"/api/apps/{app.id}/frame").text
  assert f'var _FRAME_PASSIVE_BLOCK_DIGEST = "{digest}"' in html
  # Dirty source declarations neither revoke accepted code nor grant another app.
  manifest_path = Path(app.source_dir) / "mobius.json"
  manifest = json.loads(manifest_path.read_text())
  manifest["capabilities"] = {}
  manifest_path.write_text(json.dumps(manifest))
  assert passive_block_module_digest(app) == digest
  app.capability_contract = deepcopy(app.capability_contract)
  app.capability_contract["runtime"].pop(CAPABILITY)
  db.commit()
  assert passive_block_module_digest(app) is None


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
  html = client.get(f"/api/apps/{app.id}/frame").text
  assert 'var _FRAME_PASSIVE_BLOCK_DIGEST = null' in html
