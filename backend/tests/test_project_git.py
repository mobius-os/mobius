"""Project Git projections stay confined, bounded, and useful for diffs."""

import os
import subprocess
from pathlib import Path

from app import models
from app import project_git


def _git(cwd: Path, *args: str) -> str:
  result = subprocess.run(
    ["git", "-C", str(cwd), *args],
    check=True,
    capture_output=True,
    text=True,
    env={
      **os.environ,
      "GIT_AUTHOR_NAME": "Test",
      "GIT_AUTHOR_EMAIL": "test@example.com",
      "GIT_COMMITTER_NAME": "Test",
      "GIT_COMMITTER_EMAIL": "test@example.com",
    },
  )
  return result.stdout.strip()


def _project_root(db, project: dict) -> Path:
  row = db.get(models.Project, project["id"])
  return Path(os.environ["DATA_DIR"]) / row.root_path


def _write_file(
  client, auth: dict, project_id: str, path: str, content: str,
  expected_revision: str | None = None,
) -> str:
  response = client.put(
    f"/api/projects/{project_id}/file?path={path}", headers=auth,
    json={"content": content, "expected_revision": expected_revision},
  )
  assert response.status_code == 200, response.text
  return response.json()["revision"]


def test_project_without_git_reports_ordinary_unavailability(client, auth):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "No repository", "template_id": "blank"},
  ).json()
  response = client.get(
    f"/api/projects/{project['id']}/git/status", headers=auth,
  )
  assert response.status_code == 200
  assert response.json() == {
    "available": False,
    "branch": None,
    "head": None,
    "repository_scope": None,
    "changes": [],
    "counts": {},
    "truncated": False,
  }


def test_shared_repository_is_scoped_to_project_and_exposes_changed_lines(
  client, auth, db,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Git project", "template_id": "blank"},
  ).json()
  root = _project_root(db, project)
  data_root = Path(os.environ["DATA_DIR"])
  main_revision = _write_file(
    client, auth, project["id"], "main.py", "one\ntwo\nthree\n",
  )
  _git(data_root, "init", "-b", "main")
  _git(data_root, "add", "--", root.relative_to(data_root).as_posix())
  _git(data_root, "commit", "-m", "Baseline")
  (data_root / "outside.txt").write_text("not this project")

  _write_file(
    client, auth, project["id"], "main.py", "one\nTWO\nthree\nfour\n",
    main_revision,
  )
  _write_file(client, auth, project["id"], "notes.txt", "new\nnotes\n")
  generated = root / "artifacts" / "demo" / "output.txt"
  generated.parent.mkdir(parents=True)
  generated.write_text("generated")

  status = client.get(
    f"/api/projects/{project['id']}/git/status", headers=auth,
  )
  assert status.status_code == 200, status.text
  body = status.json()
  assert body["available"] is True
  assert body["branch"] == "main"
  assert body["repository_scope"] == "shared"
  assert body["counts"] == {"modified": 1, "untracked": 1}
  assert body["changes"] == [
    {"path": "main.py", "status": "modified", "staged": False},
    {"path": "notes.txt", "status": "untracked", "staged": False},
  ]

  diff = client.get(
    f"/api/projects/{project['id']}/git/diff?path=main.py", headers=auth,
  )
  assert diff.status_code == 200, diff.text
  assert diff.json()["status"] == "modified"
  assert diff.json()["additions"] == 2
  assert diff.json()["deletions"] == 1
  assert diff.json()["changed_lines"] == [2, 4]

  untracked = client.get(
    f"/api/projects/{project['id']}/git/diff?path=notes.txt", headers=auth,
  ).json()
  assert untracked["status"] == "untracked"
  assert untracked["additions"] == 2
  assert untracked["changed_lines"] == [1, 2]


def test_project_owned_repository_takes_precedence(client, auth, db):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Owned repository", "template_id": "blank"},
  ).json()
  root = _project_root(db, project)
  main_revision = _write_file(
    client, auth, project["id"], "main.py", "original\n",
  )
  _git(root, "init", "-b", "project-main")
  _git(root, "add", "--", "main.py")
  _git(root, "commit", "-m", "Baseline")
  _write_file(
    client, auth, project["id"], "main.py", "changed\n", main_revision,
  )

  body = client.get(
    f"/api/projects/{project['id']}/git/status", headers=auth,
  ).json()
  assert body["repository_scope"] == "project"
  assert body["branch"] == "project-main"
  assert body["changes"] == [
    {"path": "main.py", "status": "modified", "staged": False},
  ]


def test_owner_can_initialize_and_commit_only_a_project_owned_repository(
  client, auth, db,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Versioned project", "template_id": "blank"},
  ).json()
  root = _project_root(db, project)
  main_revision = _write_file(
    client, auth, project["id"], "main.py", "first\n",
  )

  initialized = client.post(
    f"/api/projects/{project['id']}/git/init", headers=auth,
  )
  assert initialized.status_code == 200, initialized.text
  initial = initialized.json()
  assert initial["repository_scope"] == "project"
  assert initial["branch"] == "main"
  assert initial["head"]
  assert initial["changes"] == []

  _write_file(
    client, auth, project["id"], "main.py", "second\n", main_revision,
  )
  committed = client.post(
    f"/api/projects/{project['id']}/git/commit", headers=auth,
    json={"message": "Update the greeting", "expected_head": initial["head"]},
  )
  assert committed.status_code == 200, committed.text
  assert committed.json()["changes"] == []
  assert committed.json()["head"] != initial["head"]
  assert _git(root, "log", "-1", "--pretty=%s") == "Update the greeting"


def test_commit_route_refuses_to_stage_the_shared_data_repository(client, auth, db):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Nested project", "template_id": "blank"},
  ).json()
  root = _project_root(db, project)
  data_root = Path(os.environ["DATA_DIR"])
  main_revision = _write_file(
    client, auth, project["id"], "main.py", "first\n",
  )
  _git(data_root, "init", "-b", "main")
  _git(data_root, "add", "--", root.relative_to(data_root).as_posix())
  _git(data_root, "commit", "-m", "Baseline")
  _write_file(
    client, auth, project["id"], "main.py", "second\n", main_revision,
  )

  response = client.post(
    f"/api/projects/{project['id']}/git/commit", headers=auth,
    json={"message": "Must not escape"},
  )
  assert response.status_code == 409
  assert "shared repository" in response.json()["detail"]


def test_github_remote_flow_reviews_pushes_and_fast_forward_pulls(
  client, auth, db, tmp_path,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Remote project", "template_id": "blank"},
  ).json()
  root = _project_root(db, project)
  _write_file(client, auth, project["id"], "README.md", "local\n")
  client.post(f"/api/projects/{project['id']}/git/init", headers=auth)

  bare = tmp_path / "remote.git"
  _git(tmp_path, "init", "--bare", str(bare))
  # Keep the stored origin truthful while transparently routing this isolated
  # test to a local bare repository.
  _git(
    root, "config", f"url.file://{bare.as_posix()}.insteadOf",
    "https://github.com/example/shared-project.git",
  )
  connected = project_git.connect_github_remote(root, "example/shared-project")
  assert connected["connected"] is True
  assert connected["repository"] == "example/shared-project"
  assert connected["ahead"] == 1
  assert connected["commits"][0]["subject"] == "Start project"

  pushed = project_git.push_project(root, connected["head"])
  assert pushed["ahead"] == 0
  assert pushed["upstream"] == "origin/main"
  _git(bare, "symbolic-ref", "HEAD", "refs/heads/main")

  peer = tmp_path / "peer"
  _git(tmp_path, "clone", str(bare), str(peer))
  _git(peer, "config", "user.name", "Peer")
  _git(peer, "config", "user.email", "peer@example.com")
  (peer / "README.md").write_text("from GitHub\n")
  _git(peer, "add", "README.md")
  _git(peer, "commit", "-m", "Update remotely")
  _git(peer, "push", "origin", "main")

  fetched = project_git.fetch_project(root)
  assert fetched["behind"] == 1
  pulled = project_git.pull_project(root, pushed["head"])
  assert pulled["behind"] == 0
  assert (root / "README.md").read_text() == "from GitHub\n"


def test_project_remote_routes_keep_network_actions_owner_confirmed(
  client, auth, db,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Publish review", "template_id": "blank"},
  ).json()
  _write_file(client, auth, project["id"], "README.md", "review me\n")
  client.post(f"/api/projects/{project['id']}/git/init", headers=auth)
  connect = client.post(
    f"/api/projects/{project['id']}/git/remote", headers=auth,
    json={"repository": "https://github.com/example/review.git"},
  )
  assert connect.status_code == 200, connect.text
  assert connect.json()["repository"] == "example/review"

  status = client.get(
    f"/api/projects/{project['id']}/git/remote", headers=auth,
  )
  assert status.status_code == 200
  assert status.json()["github_connected"] is False
  assert client.post(
    f"/api/projects/{project['id']}/git/push", headers=auth,
    json={"confirmed": False, "expected_head": status.json()["head"]},
  ).status_code == 400
  unavailable = client.post(
    f"/api/projects/{project['id']}/git/fetch", headers=auth,
  )
  assert unavailable.status_code == 409
  assert "Connect GitHub" in unavailable.json()["detail"]
