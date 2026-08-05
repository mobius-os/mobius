"""Local runtime capability acceptance is explicit, confined, and atomic."""

from copy import deepcopy
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from app import app_capability_acceptance as acceptance
from app import models
from app.app_capabilities import contract_and_digest
from app.database import SessionLocal
from app.manifest_contract import MANIFEST_MAX_BYTES
from scripts import accept_local_runtime_capabilities as command
from test_app_fixtures import create_local_app


def _store_app(client, auth, db):
  created = create_local_app(
    client,
    auth,
    name="Capability Manager",
    capabilities={"device.asset-cache": {"version": 1}},
  )
  app = db.query(models.App).filter(models.App.id == created["id"]).one()
  manifest = json.loads((Path(app.source_dir) / "mobius.json").read_text())
  app.capability_contract = contract_and_digest(manifest)[0]
  app.manifest_url = "https://store.example/capability-manager/mobius.json"
  db.commit()
  db.refresh(app)
  manifest["capabilities"] = {
    "media.speech": {
      "version": 1,
      "reason": "Read reports with the selected local voice.",
    },
  }
  (Path(app.source_dir) / "mobius.json").write_text(json.dumps(manifest))
  return app


def _review(client, auth, app_id):
  response = client.get(
    f"/api/apps/{app_id}/runtime-capabilities",
    headers=auth,
  )
  assert response.status_code == 200, response.text
  return response.json()


def _accept(client, auth, app_id, digest):
  return client.post(
    f"/api/apps/{app_id}/runtime-capabilities/accept",
    headers=auth,
    json={"accept_digest": digest},
  )


def test_server_review_reports_normalized_runtime_declaration(
  client, auth, db,
):
  app = _store_app(client, auth, db)

  report = _review(client, auth, app.id)

  assert report["status"] == "review"
  assert report["candidate_runtime"]["media.speech"]["limits"] == {
    "max_text_chars": 50_000,
  }
  assert len(report["accept_digest"]) == 64


def test_server_acceptance_preserves_contract_and_publishes_after_commit(
  client, auth, db,
):
  app = _store_app(client, auth, db)
  before = deepcopy(app.capability_contract)
  report = _review(client, auth, app.id)

  published = []

  class ObservingBroadcast:
    def publish(self, event):
      verifier = SessionLocal()
      try:
        stored = verifier.get(models.App, app.id)
        published.append((event, stored.capability_contract["runtime"]))
      finally:
        verifier.close()

  with patch.object(
    acceptance,
    "get_system_broadcast",
    return_value=ObservingBroadcast(),
  ):
    response = _accept(client, auth, app.id, report["accept_digest"])

  assert response.status_code == 200, response.text
  accepted = response.json()
  assert accepted["status"] == "accepted"
  assert published == [({
    "type": "app_updated",
    "appId": str(app.id),
  }, report["candidate_runtime"])]
  db.expire_all()
  stored = db.get(models.App, app.id).capability_contract
  assert stored["runtime"] == report["candidate_runtime"]
  assert {key: value for key, value in stored.items() if key != "runtime"} == {
    key: value for key, value in before.items() if key != "runtime"
  }


def test_cosmetic_manifest_edits_keep_a_reviewed_digest_valid(
  client, auth, db,
):
  app = _store_app(client, auth, db)
  report = _review(client, auth, app.id)

  manifest_path = Path(app.source_dir) / "mobius.json"
  manifest = json.loads(manifest_path.read_text())
  manifest["description"] = "An unrelated local description edit."
  manifest_path.write_text(json.dumps(manifest, indent=2))

  response = _accept(client, auth, app.id, report["accept_digest"])

  assert response.status_code == 200, response.text
  assert response.json()["status"] == "accepted"
  db.expire_all()
  stored = db.get(models.App, app.id).capability_contract
  assert stored["runtime"] == report["candidate_runtime"]


def test_store_update_between_review_and_acceptance_rejects_the_digest(
  client, auth, db,
):
  """A changed baseline must send the owner back to a fresh review.

  The reviewed diff is meaningful only against the contract it was computed
  from.  If a Store update replaces that contract first, accepting the older
  review would apply a transition the owner never saw.
  """
  app = _store_app(client, auth, db)
  runtime_before = deepcopy(app.capability_contract["runtime"])
  report = _review(client, auth, app.id)
  assert report["candidate_runtime"] != runtime_before

  writer = SessionLocal()
  try:
    current = writer.get(models.App, app.id)
    revised_contract = deepcopy(current.capability_contract)
    revised_contract["agent"]["skills"] = ["newly-reviewed-skill.md"]
    current.capability_contract = revised_contract
    writer.commit()
  finally:
    writer.close()

  with patch.object(acceptance, "get_system_broadcast") as get_broadcast:
    response = _accept(client, auth, app.id, report["accept_digest"])

  assert response.status_code == 409
  get_broadcast.assert_not_called()
  db.expire_all()
  stored = db.get(models.App, app.id).capability_contract
  assert stored["agent"]["skills"] == ["newly-reviewed-skill.md"]
  assert stored["runtime"] == runtime_before


def test_semantic_runtime_change_rejects_prior_digest_without_write_or_event(
  client, auth, db,
):
  app = _store_app(client, auth, db)
  before = deepcopy(app.capability_contract)
  report = _review(client, auth, app.id)
  manifest_path = Path(app.source_dir) / "mobius.json"
  manifest = json.loads(manifest_path.read_text())
  manifest["capabilities"]["media.speech"]["reason"] = "A different use."
  manifest_path.write_text(json.dumps(manifest))

  with patch.object(acceptance, "get_system_broadcast") as get_broadcast:
    response = _accept(client, auth, app.id, report["accept_digest"])

  assert response.status_code == 409
  assert "runtime capability declaration" in response.json()["detail"]
  get_broadcast.assert_not_called()
  db.expire_all()
  assert db.get(models.App, app.id).capability_contract == before


def test_review_returns_invalid_runtime_declarations_as_owner_errors(
  client, auth, db,
):
  app = _store_app(client, auth, db)
  manifest_path = Path(app.source_dir) / "mobius.json"
  manifest = json.loads(manifest_path.read_text())
  manifest["capabilities"] = {"unknown.capability": {"version": 1}}
  manifest_path.write_text(json.dumps(manifest))

  response = client.get(
    f"/api/apps/{app.id}/runtime-capabilities", headers=auth,
  )

  assert response.status_code == 422
  assert response.json()["detail"] == "Unknown capability `unknown.capability`."


@pytest.mark.parametrize(
  ("manifest_bytes", "detail"),
  [
    (b"{\xff}", "valid mobius.json"),
    (b'{"value":' + b"9" * 5000 + b"}", "valid mobius.json"),
    (b" " * (MANIFEST_MAX_BYTES + 1), "exceeds"),
  ],
)
def test_review_rejects_unreadable_manifests_with_bounded_owner_errors(
  client, auth, db, manifest_bytes, detail,
):
  app = _store_app(client, auth, db)
  (Path(app.source_dir) / "mobius.json").write_bytes(manifest_bytes)

  response = client.get(
    f"/api/apps/{app.id}/runtime-capabilities", headers=auth,
  )

  assert response.status_code == 422
  assert detail in response.json()["detail"]


def test_review_rejects_a_manifest_fifo_without_blocking(client, auth, db):
  app = _store_app(client, auth, db)
  manifest_path = Path(app.source_dir) / "mobius.json"
  manifest_path.unlink()
  os.mkfifo(manifest_path)

  response = client.get(
    f"/api/apps/{app.id}/runtime-capabilities", headers=auth,
  )

  assert response.status_code == 422
  assert "regular file" in response.json()["detail"]


def test_command_rejects_non_store_apps_and_source_paths_outside_apps_root(
  client, auth, db, tmp_path,
):
  app = _store_app(client, auth, db)
  app.manifest_url = None
  db.commit()
  response = client.get(
    f"/api/apps/{app.id}/runtime-capabilities", headers=auth,
  )
  assert response.status_code == 422
  assert "only for Store-installed apps" in response.json()["detail"]

  app.manifest_url = "https://store.example/app/mobius.json"
  app.source_dir = str(tmp_path)
  db.commit()
  response = client.get(
    f"/api/apps/{app.id}/runtime-capabilities", headers=auth,
  )
  assert response.status_code == 422
  assert "outside the reviewed apps root" in response.json()["detail"]


def test_manifest_symlink_cannot_escape_the_app_source(
  client, auth, db, tmp_path,
):
  app = _store_app(client, auth, db)
  manifest_path = Path(app.source_dir) / "mobius.json"
  outside = tmp_path / "mobius.json"
  outside.write_text(manifest_path.read_text())
  manifest_path.unlink()
  manifest_path.symlink_to(outside)

  response = client.get(
    f"/api/apps/{app.id}/runtime-capabilities", headers=auth,
  )
  assert response.status_code == 422
  assert "regular file inside the app source" in response.json()["detail"]


def test_source_directory_symlink_cannot_alias_a_sibling_app(
  client, auth, db,
):
  app = _store_app(client, auth, db)
  source = Path(app.source_dir)
  sibling = source.with_name(f"{source.name}-sibling")
  source.rename(sibling)
  source.symlink_to(sibling, target_is_directory=True)
  try:
    response = client.get(
      f"/api/apps/{app.id}/runtime-capabilities", headers=auth,
    )
    assert response.status_code == 422
    assert "direct, regular directory under the apps root" in (
      response.json()["detail"]
    )
  finally:
    source.unlink()
    sibling.rename(source)


def test_pinned_source_directory_cannot_escape_during_parent_swap(
  client, auth, db, tmp_path, monkeypatch,
):
  app = _store_app(client, auth, db)
  source = Path(app.source_dir)
  pinned = source.with_name(f"{source.name}-pinned")
  outside = tmp_path / "outside"
  outside.mkdir()
  outside_manifest = json.loads((source / "mobius.json").read_text())
  outside_manifest["capabilities"] = {
    "media.microphone.capture": {"version": 1, "reason": "must not escape"},
  }
  (outside / "mobius.json").write_text(json.dumps(outside_manifest))

  real_open = acceptance.os.open
  swapped = False

  def swap_before_manifest(path, flags, *args, **kwargs):
    nonlocal swapped
    if path == "mobius.json" and not swapped:
      swapped = True
      source.rename(pinned)
      source.symlink_to(outside, target_is_directory=True)
    return real_open(path, flags, *args, **kwargs)

  monkeypatch.setattr(acceptance.os, "open", swap_before_manifest)
  try:
    report = _review(client, auth, app.id)
    assert swapped is True
    assert "media.speech" in report["candidate_runtime"]
    assert "media.microphone.capture" not in report["candidate_runtime"]
  finally:
    if source.is_symlink():
      source.unlink()
    if pinned.exists():
      pinned.rename(source)


def test_acceptance_route_rejects_cross_site_requests(client, auth, db):
  app = _store_app(client, auth, db)
  response = client.post(
    f"/api/apps/{app.id}/runtime-capabilities/accept",
    headers={**auth, "Sec-Fetch-Site": "cross-site"},
    json={"accept_digest": "0" * 64},
  )
  assert response.status_code == 403


def test_command_forwards_review_and_acceptance_to_the_live_server(
  monkeypatch, capsys,
):
  requests = []

  class Response:
    def __enter__(self):
      return self

    def __exit__(self, *_):
      return False

    def read(self):
      return json.dumps({"status": "ok"}).encode()

  def urlopen(request, timeout):
    requests.append((request, timeout))
    return Response()

  monkeypatch.setenv("AGENT_TOKEN", "agent-token")
  monkeypatch.setenv("API_BASE_URL", "http://mobius.test/")
  monkeypatch.setattr(command.urllib.request, "urlopen", urlopen)

  command.main(["--app-id", "7"])
  assert json.loads(capsys.readouterr().out) == {"status": "ok"}
  command.main(["--app-id", "7", "--accept-digest", "a" * 64])
  assert json.loads(capsys.readouterr().out) == {"status": "ok"}

  review_request, _ = requests[0]
  accept_request, _ = requests[1]
  assert review_request.full_url == (
    "http://mobius.test/api/apps/7/runtime-capabilities"
  )
  assert review_request.method == "GET"
  assert review_request.get_header("Authorization") == "Bearer agent-token"
  assert accept_request.full_url == (
    "http://mobius.test/api/apps/7/runtime-capabilities/accept"
  )
  assert accept_request.method == "POST"
  assert json.loads(accept_request.data) == {"accept_digest": "a" * 64}
