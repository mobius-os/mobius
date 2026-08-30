"""Shared app instances pin builds and keep membership separate from source."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy.orm.attributes import flag_modified

from app import models, shared_app_state
from app.config import get_settings
from app.shared_app_retention import purge_expired_shared_apps
from app.timeutil import SOFT_DELETE_TTL, now_naive_utc


def _built_project(client, auth, db):
  response = client.post(
    "/api/projects", headers=auth,
    json={"name": "Together Board", "template_id": "blank"},
  )
  assert response.status_code == 200, response.text
  project_id = response.json()["id"]
  project = db.get(models.Project, project_id)
  root = Path(get_settings().data_dir) / project.root_path
  output = root / "artifacts" / "website" / "output"
  output.mkdir(parents=True)
  (output / "index.html").write_text("<h1>Pinned board</h1>")
  project.artifacts_json = [{
    "id": "website", "name": "Website", "builder": "website",
    "source": "index.html", "output_rel": "artifacts/website/output/index.html",
    "status": "ok",
  }]
  flag_modified(project, "artifacts_json")
  db.commit()
  return project, output


def _create_instance(client, auth, project_id):
  response = client.post(
    "/api/shared-apps", headers=auth,
    json={"project_id": project_id, "artifact_id": "website", "name": "Together Board"},
  )
  assert response.status_code == 201, response.text
  return response.json()


def test_shared_app_pins_build_and_confines_member_to_runtime(client, auth, db):
  project, output = _built_project(client, auth, db)
  instance = _create_instance(client, auth, project.id)
  (output / "index.html").write_text("<h1>Changed source build</h1>")

  invite = client.post(
    f"/api/shared-apps/{instance['id']}/invites", headers=auth,
    json={"invitee_name": "Morgan", "role": "editor"},
  )
  assert invite.status_code == 200, invite.text
  secret = urlsplit(invite.json()["join_url"]).fragment
  redeemed = client.post(
    "/api/shared-apps/invites/redeem",
    json={"invite": secret, "display_name": "Morgan"},
  )
  assert redeemed.status_code == 200, redeemed.text
  guest = {"Authorization": f"Bearer {redeemed.json()['access_token']}"}

  pinned = client.get(
    f"/api/shared-apps/{instance['id']}/output/index.html", headers=guest,
  )
  assert pinned.status_code == 200
  assert pinned.text == "<h1>Pinned board</h1>"
  assert client.get(f"/api/projects/{project.id}", headers=guest).status_code == 403

  other_project, _other_output = _built_project(client, auth, db)
  other = _create_instance(client, auth, other_project.id)
  assert client.get(f"/api/shared-apps/{other['id']}", headers=guest).status_code == 404
  assert client.post(
    "/api/shared-apps/invites/redeem",
    json={"invite": secret, "display_name": "Replay"},
  ).status_code == 410


def test_shared_state_is_path_version_checked_and_visible_to_members(client, auth, db):
  project, _output = _built_project(client, auth, db)
  instance = _create_instance(client, auth, project.id)
  invite = client.post(
    f"/api/shared-apps/{instance['id']}/invites", headers=auth,
    json={"role": "editor"},
  ).json()
  redeemed = client.post(
    "/api/shared-apps/invites/redeem",
    json={"invite": urlsplit(invite["join_url"]).fragment, "display_name": "Morgan"},
  ).json()
  guest = {"Authorization": f"Bearer {redeemed['access_token']}"}

  first = client.put(
    f"/api/shared-apps/{instance['id']}/state/board.json", headers=auth,
    json={"expected_version": None, "value": [{"title": "Owner card"}]},
  )
  assert first.status_code == 200, first.text
  first_version = first.json()["version"]
  member_state = client.get(
    f"/api/shared-apps/{instance['id']}/state", headers=guest,
  ).json()
  assert member_state["values"]["board.json"][0]["title"] == "Owner card"
  assert member_state["versions"]["board.json"] == first_version

  second = client.put(
    f"/api/shared-apps/{instance['id']}/state/board.json", headers=guest,
    json={"expected_version": first_version, "value": [{"title": "Together"}]},
  )
  assert second.status_code == 200, second.text
  stale = client.put(
    f"/api/shared-apps/{instance['id']}/state/board.json", headers=auth,
    json={"expected_version": first_version, "value": []},
  )
  assert stale.status_code == 409
  assert stale.json()["detail"]["version"] == second.json()["version"]

  changes = client.get(
    f"/api/shared-apps/{instance['id']}/changes?after=0", headers=guest,
  ).json()
  assert changes["truncated"] is False
  assert [(item["kind"], item["path"]) for item in changes["changes"]] == [
    ("set", "board.json"), ("set", "board.json"),
  ]
  assert changes["cursor"] == second.json()["change_id"]


def test_shared_state_snapshot_reads_files_and_cursor_under_one_lock(
  client, auth, db, monkeypatch,
):
  project, _output = _built_project(client, auth, db)
  instance = _create_instance(client, auth, project.id)
  row = db.get(models.SharedAppInstance, instance["id"])
  original = shared_app_state.list_changes

  class ReentrantSpy:
    depth = 0

    def __enter__(self):
      self.depth += 1
      return self

    def __exit__(self, *_args):
      self.depth -= 1

  lock = ReentrantSpy()
  monkeypatch.setattr(shared_app_state, "state_lock", lambda _instance_id: lock)

  def assert_locked(*args, **kwargs):
    assert lock.depth == 1
    return original(*args, **kwargs)

  monkeypatch.setattr(shared_app_state, "list_changes", assert_locked)

  snapshot = shared_app_state.read_state_snapshot(db, row)

  assert snapshot == {"values": {}, "versions": {}, "cursor": 0}


def test_shared_state_accepts_only_one_writer_for_one_path_version(client, auth, db):
  project, _output = _built_project(client, auth, db)
  instance = _create_instance(client, auth, project.id)
  barrier = Barrier(2)

  def write(title):
    barrier.wait()
    return client.put(
      f"/api/shared-apps/{instance['id']}/state/board.json", headers=auth,
      json={"expected_version": None, "value": [{"title": title}]},
    )

  with ThreadPoolExecutor(max_workers=2) as pool:
    responses = list(pool.map(write, ["First", "Second"]))

  assert sorted(response.status_code for response in responses) == [200, 409]
  state = client.get(f"/api/shared-apps/{instance['id']}/state", headers=auth).json()
  assert state["values"]["board.json"][0]["title"] in {"First", "Second"}


def test_shared_state_allows_independent_paths_without_global_conflicts(client, auth, db):
  project, _output = _built_project(client, auth, db)
  instance = _create_instance(client, auth, project.id)

  first = client.put(
    f"/api/shared-apps/{instance['id']}/state/board.json", headers=auth,
    json={"expected_version": None, "value": ["card"]},
  )
  second = client.put(
    f"/api/shared-apps/{instance['id']}/state/settings.json", headers=auth,
    json={"expected_version": None, "value": {"compact": True}},
  )

  assert first.status_code == second.status_code == 200
  state = client.get(f"/api/shared-apps/{instance['id']}/state", headers=auth).json()
  assert state["values"] == {
    "board.json": ["card"], "settings.json": {"compact": True},
  }
  assert set(state["versions"]) == {"board.json", "settings.json"}


def test_shared_state_ignores_only_atomic_write_crash_artifacts(client, auth, db):
  project, _output = _built_project(client, auth, db)
  instance = _create_instance(client, auth, project.id)
  row = db.get(models.SharedAppInstance, instance["id"])
  root = shared_app_state.state_root(row)
  root.mkdir(parents=True, exist_ok=True)
  (root / ".board.json.deadbeef.tmp").write_bytes(b"{partial")
  (root / ".settings.json.abc12345.tmp").write_text(
    '{"phantom":true}', encoding="utf-8",
  )
  # Similar ordinary owner paths remain visible: only mkstemp's exact
  # eight-character atomic-write namespace is reserved.
  (root / ".board.json.owner.tmp").write_text(
    '{"kept":true}', encoding="utf-8",
  )

  state = client.get(
    f"/api/shared-apps/{instance['id']}/state", headers=auth,
  )

  assert state.status_code == 200, state.text
  assert state.json()["values"] == {
    ".board.json.owner.tmp": {"kept": True},
  }
  reserved_write = client.put(
    f"/api/shared-apps/{instance['id']}/state/.board.json.deadbeef.tmp",
    headers=auth,
    json={"expected_version": None, "value": {"not": "owner state"}},
  )
  assert reserved_write.status_code == 400
  assert reserved_write.json()["detail"] == "Invalid shared app data path."


def test_shared_app_reuses_identity_and_publishes_a_new_pinned_release(client, auth, db):
  project, output = _built_project(client, auth, db)
  instance = _create_instance(client, auth, project.id)
  repeated = client.post(
    "/api/shared-apps", headers=auth,
    json={"project_id": project.id, "artifact_id": "website"},
  )
  assert repeated.status_code == 200
  assert repeated.json()["id"] == instance["id"]

  client.put(
    f"/api/shared-apps/{instance['id']}/state/board.json", headers=auth,
    json={"expected_version": None, "value": ["kept"]},
  )
  (output / "index.html").write_text("<h1>Published again</h1>")
  published = client.put(f"/api/shared-apps/{instance['id']}/release", headers=auth)
  assert published.status_code == 200, published.text
  assert client.get(
    f"/api/shared-apps/{instance['id']}/output/index.html", headers=auth,
  ).text == "<h1>Published again</h1>"
  assert client.get(
    f"/api/shared-apps/{instance['id']}/state", headers=auth,
  ).json()["values"]["board.json"] == ["kept"]


def test_shared_app_owner_controls_access_and_can_recover_removal(client, auth, db):
  project, _output = _built_project(client, auth, db)
  instance = _create_instance(client, auth, project.id)
  invitation = client.post(
    f"/api/shared-apps/{instance['id']}/invites", headers=auth,
    json={"invitee_name": "Morgan", "role": "editor"},
  ).json()
  redeemed = client.post(
    "/api/shared-apps/invites/redeem",
    json={"invite": urlsplit(invitation["join_url"]).fragment, "display_name": "Morgan"},
  ).json()
  guest = {"Authorization": f"Bearer {redeemed['access_token']}"}
  member_id = redeemed["instance"]["member_id"]

  assert client.patch(
    f"/api/shared-apps/{instance['id']}/members/{member_id}", headers=auth,
    json={"role": "viewer"},
  ).status_code == 200
  assert client.put(
    f"/api/shared-apps/{instance['id']}/state/board.json", headers=guest,
    json={"expected_version": None, "value": []},
  ).status_code == 403
  assert client.delete(
    f"/api/shared-apps/{instance['id']}/members/{member_id}", headers=auth,
  ).status_code == 204
  assert client.get(f"/api/shared-apps/{instance['id']}", headers=guest).status_code == 401

  assert client.delete(f"/api/shared-apps/{instance['id']}", headers=auth).status_code == 204
  assert client.get(f"/api/shared-apps/{instance['id']}", headers=auth).status_code == 404
  recovered = client.post(f"/api/shared-apps/{instance['id']}/recover", headers=auth)
  assert recovered.status_code == 200
  assert recovered.json()["id"] == instance["id"]


def test_shared_app_recovery_does_not_duplicate_a_newer_instance(client, auth, db):
  project, _output = _built_project(client, auth, db)
  original = _create_instance(client, auth, project.id)
  assert client.delete(
    f"/api/shared-apps/{original['id']}", headers=auth,
  ).status_code == 204

  replacement = _create_instance(client, auth, project.id)
  assert replacement["id"] != original["id"]
  blocked = client.post(
    f"/api/shared-apps/{original['id']}/recover", headers=auth,
  )
  assert blocked.status_code == 409
  assert client.get(
    f"/api/shared-apps/{replacement['id']}", headers=auth,
  ).status_code == 200


def test_shared_app_output_rejects_a_snapshot_outside_its_owned_root(
  client, auth, db, tmp_path,
):
  project, _output = _built_project(client, auth, db)
  instance = _create_instance(client, auth, project.id)
  outside = tmp_path / "outside"
  outside.mkdir()
  (outside / "index.html").write_text("not shared", encoding="utf-8")
  row = db.get(models.SharedAppInstance, instance["id"])
  row.snapshot_path = str(outside)
  db.commit()

  response = client.get(
    f"/api/shared-apps/{instance['id']}/output/index.html", headers=auth,
  )
  assert response.status_code == 500
  assert response.json()["detail"] == "Shared app storage is misconfigured."


def test_expired_shared_app_removal_deletes_its_owned_snapshot(client, auth, db):
  project, _output = _built_project(client, auth, db)
  instance = _create_instance(client, auth, project.id)
  row = db.get(models.SharedAppInstance, instance["id"])
  snapshot_root = Path(get_settings().data_dir) / row.snapshot_path
  row.deleted_at = now_naive_utc() - (SOFT_DELETE_TTL * 2)
  db.commit()

  assert purge_expired_shared_apps(db) == [instance["id"]]
  assert db.get(models.SharedAppInstance, instance["id"]) is None
  assert not snapshot_root.parent.exists()


def test_project_removal_recovers_its_shared_app_but_not_old_guest_access(client, auth, db):
  project, _output = _built_project(client, auth, db)
  instance = _create_instance(client, auth, project.id)
  invitation = client.post(
    f"/api/shared-apps/{instance['id']}/invites", headers=auth,
    json={"role": "editor"},
  ).json()
  redeemed = client.post(
    "/api/shared-apps/invites/redeem",
    json={"invite": urlsplit(invitation["join_url"]).fragment, "display_name": "Morgan"},
  ).json()
  guest = {"Authorization": f"Bearer {redeemed['access_token']}"}

  assert client.delete(f"/api/projects/{project.id}", headers=auth).status_code == 204
  assert client.get(f"/api/shared-apps/{instance['id']}", headers=auth).status_code == 404
  assert client.get(f"/api/shared-apps/{instance['id']}", headers=guest).status_code == 401
  assert client.post(f"/api/projects/{project.id}/recover", headers=auth).status_code == 200
  assert client.get(f"/api/shared-apps/{instance['id']}", headers=auth).status_code == 200
  assert client.get(f"/api/shared-apps/{instance['id']}", headers=guest).status_code == 401


def test_expired_project_removal_purges_its_shared_app_snapshot(client, auth, db):
  from app.project_retention import purge_expired_project_tombstones

  project, _output = _built_project(client, auth, db)
  project_id = str(project.id)
  instance = _create_instance(client, auth, project.id)
  shared = db.get(models.SharedAppInstance, instance["id"])
  snapshot_root = Path(get_settings().data_dir) / shared.snapshot_path
  assert client.delete(f"/api/projects/{project.id}", headers=auth).status_code == 204
  expired_at = now_naive_utc() - (SOFT_DELETE_TTL * 2)
  db.get(models.Project, project.id).deleted_at = expired_at
  db.get(models.SharedAppInstance, instance["id"]).deleted_at = expired_at
  db.commit()

  assert purge_expired_project_tombstones(db) == [project_id]
  assert db.get(models.SharedAppInstance, instance["id"]) is None
  assert not snapshot_root.parent.exists()
