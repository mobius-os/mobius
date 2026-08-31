"""Confined rename/move for project files and directories."""

import os
from pathlib import Path

from app import models


def _make_project(client, auth, name="Movable"):
  created = client.post(
    "/api/projects", headers=auth, json={"name": name, "template_id": "blank"},
  )
  assert created.status_code == 200, created.text
  return created.json()


def _write_file(client, auth, project, path, content):
  saved = client.put(
    f"/api/projects/{project['id']}/file?path={path}",
    headers=auth, json={"content": content, "expected_revision": None},
  )
  assert saved.status_code == 200, saved.text


def test_move_renames_a_file(client, auth):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "a.txt", "hello")
  moved = client.post(
    f"/api/projects/{project['id']}/move", headers=auth,
    json={"from_path": "a.txt", "to_path": "b.txt"},
  )
  assert moved.status_code == 200, moved.text
  assert moved.json()["to"] == "b.txt"
  assert client.get(
    f"/api/projects/{project['id']}/file?path=b.txt", headers=auth,
  ).json()["content"] == "hello"
  assert client.get(
    f"/api/projects/{project['id']}/file?path=a.txt", headers=auth,
  ).status_code == 404


def test_move_into_a_subdirectory_creates_parents(client, auth):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "a.txt", "hi")
  moved = client.post(
    f"/api/projects/{project['id']}/move", headers=auth,
    json={"from_path": "a.txt", "to_path": "nested/deep/a.txt"},
  )
  assert moved.status_code == 200, moved.text
  assert client.get(
    f"/api/projects/{project['id']}/file?path=nested/deep/a.txt", headers=auth,
  ).json()["content"] == "hi"


def test_save_after_remote_move_conflicts_instead_of_recreating_source(client, auth):
  project = _make_project(client, auth)
  created = client.put(
    f"/api/projects/{project['id']}/file?path=a.txt",
    headers=auth,
    json={"content": "opened", "expected_revision": None},
  )
  revision = created.json()["revision"]
  moved = client.post(
    f"/api/projects/{project['id']}/move", headers=auth,
    json={"from_path": "a.txt", "to_path": "b.txt"},
  )
  assert moved.status_code == 200, moved.text

  stale_save = client.put(
    f"/api/projects/{project['id']}/file?path=a.txt", headers=auth,
    json={"content": "draft", "expected_revision": revision},
  )
  assert stale_save.status_code == 409
  assert stale_save.json()["detail"]["code"] == "file_revision_conflict"
  assert client.get(
    f"/api/projects/{project['id']}/file?path=a.txt", headers=auth,
  ).status_code == 404


def test_move_missing_source_is_404(client, auth):
  project = _make_project(client, auth)
  response = client.post(
    f"/api/projects/{project['id']}/move", headers=auth,
    json={"from_path": "ghost.txt", "to_path": "b.txt"},
  )
  assert response.status_code == 404


def test_move_onto_existing_destination_is_409(client, auth):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "a.txt", "one")
  _write_file(client, auth, project, "b.txt", "two")
  response = client.post(
    f"/api/projects/{project['id']}/move", headers=auth,
    json={"from_path": "a.txt", "to_path": "b.txt"},
  )
  assert response.status_code == 409
  # Neither file was disturbed.
  assert client.get(
    f"/api/projects/{project['id']}/file?path=a.txt", headers=auth,
  ).json()["content"] == "one"
  assert client.get(
    f"/api/projects/{project['id']}/file?path=b.txt", headers=auth,
  ).json()["content"] == "two"


def test_move_a_folder_into_its_own_descendant_is_rejected(client, auth):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "d/x.txt", "content")
  response = client.post(
    f"/api/projects/{project['id']}/move", headers=auth,
    json={"from_path": "d", "to_path": "d/sub"},
  )
  assert response.status_code == 400
  assert client.get(
    f"/api/projects/{project['id']}/file?path=d/x.txt", headers=auth,
  ).status_code == 200


def test_move_touching_reserved_artifacts_area_is_rejected(client, auth, db):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "a.txt", "hi")
  into_artifacts = client.post(
    f"/api/projects/{project['id']}/move", headers=auth,
    json={"from_path": "a.txt", "to_path": "artifacts/a.txt"},
  )
  assert into_artifacts.status_code == 409
  # A move OUT of the artifacts area is refused too.
  row = db.get(models.Project, project["id"])
  generated = (
    Path(os.environ["DATA_DIR"]) / row.root_path / "artifacts" / "keep.txt"
  )
  generated.parent.mkdir(parents=True)
  generated.write_text("managed")
  out_of_artifacts = client.post(
    f"/api/projects/{project['id']}/move", headers=auth,
    json={"from_path": "artifacts/keep.txt", "to_path": "keep.txt"},
  )
  assert out_of_artifacts.status_code == 409


def test_move_through_a_symlink_cannot_escape_the_project(client, auth, db):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "inside.txt", "safe")
  row = db.get(models.Project, project["id"])
  root = Path(os.environ["DATA_DIR"]) / row.root_path
  outside = Path(os.environ["DATA_DIR"]) / "outside-move-target"
  outside.mkdir()
  (root / "escape").symlink_to(outside, target_is_directory=True)

  response = client.post(
    f"/api/projects/{project['id']}/move", headers=auth,
    json={"from_path": "inside.txt", "to_path": "escape/leaked.txt"},
  )
  assert response.status_code in (400, 403)
  assert not (outside / "leaked.txt").exists()
  assert (root / "inside.txt").read_text() == "safe"


def test_move_rejects_moving_the_project_root(client, auth):
  project = _make_project(client, auth)
  response = client.post(
    f"/api/projects/{project['id']}/move", headers=auth,
    json={"from_path": "/", "to_path": "elsewhere"},
  )
  assert response.status_code == 400
