"""Project Git projections stay confined, bounded, and useful for diffs."""

import os
import subprocess
from pathlib import Path

import pytest

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
  _write_file(
    client, auth, project["id"], "artifacts/demo/output.txt", "generated",
  )

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
    {"path": "main.py", "status": "modified", "staged": False, "additions": 2, "deletions": 1, "binary": False},
    {"path": "notes.txt", "status": "untracked", "staged": False, "additions": 2, "deletions": 0, "binary": False},
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
    {"path": "main.py", "status": "modified", "staged": False, "additions": 1, "deletions": 1, "binary": False},
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


def test_cancelled_init_holds_the_source_lock_until_git_finishes(
  client, auth, db, monkeypatch,
):
  import asyncio

  from app import fs_locks
  from app.deps import ProjectPrincipal
  from app.routes import projects as routes
  from tests.cancel_helpers import BlockedWorker, cancel_while_worker_blocked

  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Cancelled init", "template_id": "blank"},
  ).json()
  root = _project_root(db, project)
  worker = BlockedWorker(wraps=project_git.initialize_project)
  monkeypatch.setattr(project_git, "initialize_project", worker)
  principal = ProjectPrincipal(owner=db.query(models.Owner).one(), role="owner")

  outcome = asyncio.run(cancel_while_worker_blocked(
    fs_locks.source_dir_lock(str(root)),
    lambda: routes.initialize_project_git(project["id"], principal, db),
    worker,
  ))

  assert isinstance(outcome, asyncio.CancelledError), repr(outcome)
  assert (root / ".git").is_dir()


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


def test_project_push_uses_the_branch_reviewed_in_remote_status(tmp_path, monkeypatch):
  _git(tmp_path, "init", "-b", "main")
  _git(tmp_path, "commit", "--allow-empty", "-m", "Remote base")
  _git(tmp_path, "commit", "--allow-empty", "-m", "Reviewed change")
  _git(tmp_path, "remote", "add", "origin", "https://github.com/example/project.git")
  _git(tmp_path, "update-ref", "refs/remotes/origin/release", "HEAD^")
  _git(tmp_path, "branch", "--set-upstream-to=origin/release", "main")
  commands = []
  monkeypatch.setattr(project_git, "_run_git_write", lambda root, *args, **kw: commands.append(args))
  status = project_git.project_remote_status(tmp_path)
  assert status["upstream"] == "origin/release"
  assert status["push_branch"] == "release"
  assert status["ahead"] == 1

  project_git.push_project(tmp_path, status["head"], status["push_branch"])

  head = _git(tmp_path, "rev-parse", "HEAD")
  pushes = [command for command in commands if command[0] == "push"]
  assert pushes == [("push", "origin", f"{head}:refs/heads/release")]


def test_project_push_refuses_a_local_upstream_that_looks_like_origin(tmp_path, monkeypatch):
  _git(tmp_path, "init", "-b", "main")
  _git(tmp_path, "commit", "--allow-empty", "-m", "Base")
  _git(tmp_path, "branch", "origin/release")
  _git(tmp_path, "branch", "--set-upstream-to=origin/release", "main")
  _git(tmp_path, "remote", "add", "origin", "https://github.com/example/project.git")
  writes = []
  monkeypatch.setattr(project_git, "_run_git_write", lambda root, *args, **kw: writes.append(args))
  assert project_git.project_remote_status(tmp_path)["push_branch"] is None

  with pytest.raises(project_git.GitProjectError, match="Track a branch on the connected"):
    project_git.push_project(tmp_path)

  assert writes == []


def _project_with_bare_origin(tmp_path: Path) -> tuple[Path, Path]:
  root = tmp_path / "project"
  bare = tmp_path / "remote.git"
  root.mkdir()
  _git(tmp_path, "init", "--bare", str(bare))
  _git(root, "init", "-b", "main")
  _git(root, "commit", "--allow-empty", "-m", "Remote base")
  _git(root, "remote", "add", "origin", "https://github.com/example/project.git")
  # Keep the stored origin a GitHub URL while routing to the local bare repo.
  _git(root, "config", f"url.file://{bare.as_posix()}.insteadOf",
       "https://github.com/example/project.git")
  _git(root, "push", "origin", "main:refs/heads/release")
  _git(root, "config", "branch.main.remote", "origin")
  _git(root, "config", "branch.main.merge", "refs/heads/release")
  _git(root, "commit", "--allow-empty", "-m", "Reviewed change")
  return root, bare


def _remote_branches(bare: Path) -> dict[str, str]:
  listing = _git(bare, "for-each-ref", "--format=%(refname:lstrip=2) %(objectname)", "refs/heads")
  return dict(line.split(" ") for line in listing.splitlines())


def test_project_push_refuses_a_destination_changed_after_the_owner_reviewed_it(tmp_path):
  root, bare = _project_with_bare_origin(tmp_path)
  status = project_git.project_remote_status(root)
  assert status["push_branch"] == "release"
  before = _remote_branches(bare)
  # Something retargets the branch between the confirmation and the click.
  _git(root, "config", "branch.main.merge", "refs/heads/prod")

  with pytest.raises(project_git.GitProjectError, match="changed since this view"):
    project_git.push_project(root, status["head"], status["push_branch"])

  assert _remote_branches(bare) == before


def test_project_push_destination_comes_from_config_before_the_first_fetch(tmp_path):
  root, bare = _project_with_bare_origin(tmp_path)
  # The upstream is configured but its tracking ref was never fetched.
  _git(root, "update-ref", "-d", "refs/remotes/origin/release")
  status = project_git.project_remote_status(root)
  assert status["upstream"] is None
  assert status["push_branch"] == "release"

  pushed = project_git.push_project(root, status["head"], status["push_branch"])

  assert _remote_branches(bare) == {"release": _git(root, "rev-parse", "HEAD")}
  assert pushed["upstream"] == "origin/release"
  assert pushed["ahead"] == 0


def test_project_push_publishes_the_verified_commit_not_a_later_one(tmp_path, monkeypatch):
  root, bare = _project_with_bare_origin(tmp_path)
  status = project_git.project_remote_status(root)
  reviewed = _git(root, "rev-parse", "HEAD")
  write = project_git._run_git_write

  def commit_just_before_push(cwd, *args, **kw):
    if args[0] == "push":
      _git(root, "commit", "--allow-empty", "-m", "Not reviewed")
    return write(cwd, *args, **kw)

  monkeypatch.setattr(project_git, "_run_git_write", commit_just_before_push)
  project_git.push_project(root, status["head"], status["push_branch"])

  assert _remote_branches(bare) == {"release": reviewed}
  assert _git(root, "rev-parse", "HEAD") != reviewed


def test_project_push_route_refuses_a_stale_reviewed_destination(
  client, auth, db, tmp_path, monkeypatch,
):
  from app.routes import projects as project_routes

  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Stale destination", "template_id": "blank"},
  ).json()
  _write_file(client, auth, project["id"], "README.md", "local\n")
  client.post(f"/api/projects/{project['id']}/git/init", headers=auth)
  root = _project_root(db, project)
  bare = tmp_path / "remote.git"
  _git(tmp_path, "init", "--bare", str(bare))
  _git(root, "config", f"url.file://{bare.as_posix()}.insteadOf",
       "https://github.com/example/stale.git")
  project_git.connect_github_remote(root, "example/stale")
  monkeypatch.setattr(project_routes, "_require_project_github_connection", lambda: None)
  status = client.get(f"/api/projects/{project['id']}/git/remote", headers=auth).json()
  assert status["push_branch"] == "main"

  response = client.post(
    f"/api/projects/{project['id']}/git/push", headers=auth,
    json={"confirmed": True, "expected_head": status["head"], "expected_push_branch": "release"},
  )

  assert response.status_code == 409, response.text
  assert "changed since this view" in response.json()["detail"]
  assert _git(bare, "for-each-ref", "refs/heads") == ""


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


def test_applied_source_status_distinguishes_clean_commits_from_deployment(tmp_path):
  root = tmp_path / 'app'
  root.mkdir()
  _git(root, 'init', '-b', 'main')
  (root / 'index.jsx').write_text('accepted')
  _git(root, 'add', '.')
  _git(root, 'commit', '-m', 'Accepted')
  accepted = _git(root, 'rev-parse', 'HEAD')
  def state():
    return project_git.applied_source_status(root, accepted, project_git.project_status(root))['state']
  assert state() == 'current'
  (root / 'index.jsx').write_text('draft')
  assert state() == 'pending'
  _git(root, 'add', '.')
  _git(root, 'commit', '-m', 'Saved locally, not applied')
  assert project_git.project_status(root)['changes'] == []
  assert state() == 'pending'
  (root / 'index.jsx').write_text('accepted')
  assert state() == 'current'
  (root / 'new.js').write_text('new input')
  assert state() == 'pending'
  assert project_git.applied_source_status(root, None, project_git.project_status(root)) == {'state': 'unknown'}
  assert project_git.applied_source_status(root, 'a' * 40, project_git.project_status(root)) == {'state': 'unknown'}


def test_status_line_counts_and_unified_patches_include_new_deleted_and_binary_files(tmp_path):
  root = tmp_path
  _git(root, 'init', '-b', 'main')
  (root / 'old.txt').write_text('old\n')
  (root / 'edit.txt').write_text('before\ncontext\n')
  _git(root, 'add', '.')
  _git(root, 'commit', '-m', 'Baseline')
  (root / 'old.txt').unlink()
  (root / 'edit.txt').write_text('+++literal\ncontext\n')
  (root / 'new.txt').write_text('one\ntwo\n')
  (root / 'image.bin').write_bytes(b'\0binary')
  status = project_git.project_status(root)
  assert status['line_stats'] == {'available': True, 'additions': 3, 'deletions': 2, 'truncated': False}
  assert next(row for row in status['changes'] if row['path'] == 'image.bin')['binary']
  deleted = project_git.project_file_diff(root, root / 'old.txt')
  assert deleted['deletions'] == 1 and '-old' in deleted['patch']
  changed = project_git.project_file_diff(root, root / 'edit.txt')
  assert changed['additions'] == 1 and changed['changed_lines'] == [1]
  assert ' context' in changed['patch']
  new = project_git.project_file_diff(root, root / 'new.txt')
  assert '+one\n+two\n' in new['patch']


def test_deleted_project_file_has_a_diff_but_unknown_paths_remain_404(client, auth, db):
  project = client.post('/api/projects', headers=auth, json={'name': 'Diffs', 'template_id': 'blank'}).json()
  root = _project_root(db, project)
  (root / 'old.txt').write_text('removed\n')
  _git(root, 'init', '-b', 'main')
  _git(root, 'add', '.')
  _git(root, 'commit', '-m', 'Baseline')
  (root / 'old.txt').unlink()
  response = client.get(f"/api/projects/{project['id']}/git/diff?path=old.txt", headers=auth)
  assert response.status_code == 200
  assert '-removed' in response.json()['patch']
  assert client.get(f"/api/projects/{project['id']}/git/diff?path=missing.txt", headers=auth).status_code == 404


def test_new_file_stat_reads_are_bounded_and_report_partial_counts(tmp_path, monkeypatch):
  _git(tmp_path, 'init', '-b', 'main')
  (tmp_path / 'base').write_text('base')
  _git(tmp_path, 'add', '.')
  _git(tmp_path, 'commit', '-m', 'Baseline')
  (tmp_path / 'large').write_text('a\n' * 100)
  monkeypatch.setattr(project_git, '_DIFF_OUTPUT_MAX', 10)
  assert project_git.project_status(tmp_path)['line_stats']['truncated'] is True
