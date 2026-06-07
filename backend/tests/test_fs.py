"""Tests for the owner filesystem + git oversight API (routes/fs.py)."""

import subprocess
from pathlib import Path

import pytest

from app.config import get_settings


@pytest.fixture
def fsroot():
  """The dir the /api/fs viewer is rooted at (= data_dir in tests). Cleans up
  the test files (incl. root-level secret stand-ins) afterwards."""
  root = Path(get_settings().fs_view_root or get_settings().data_dir).resolve()
  work = root / "fstest"
  work.mkdir(parents=True, exist_ok=True)
  made: list[Path] = []
  yield root, work, made
  import shutil
  shutil.rmtree(work, ignore_errors=True)
  for p in made:
    if p.is_dir():
      shutil.rmtree(p, ignore_errors=True)
    else:
      p.unlink(missing_ok=True)


def test_tree_lists_dirs_first_and_redacts_secrets(client, auth, fsroot):
  root, work, made = fsroot
  (work / "b.txt").write_text("b")
  (work / "a.txt").write_text("a")
  (work / "sub").mkdir(exist_ok=True)
  # root-level secrets must be omitted + reported
  secret = root / ".secret-key"
  secret.write_text("shh"); made.append(secret)
  cli = root / "cli-auth"
  cli.mkdir(exist_ok=True); made.append(cli)

  r = client.get("/api/fs/tree", params={"path": "fstest"}, headers=auth)
  assert r.status_code == 200
  names = [e["name"] for e in r.json()["entries"]]
  assert names == ["sub", "a.txt", "b.txt"]  # dir first, then files alpha

  root_tree = client.get("/api/fs/tree", headers=auth).json()
  root_names = [e["name"] for e in root_tree["entries"]]
  assert ".secret-key" not in root_names and "cli-auth" not in root_names
  assert ".secret-key" in root_tree["redacted"]
  assert "cli-auth" in root_tree["redacted"]


def test_read_text_and_meta(client, auth, fsroot):
  _, work, _ = fsroot
  (work / "doc.md").write_text("# Hello\n")
  r = client.get("/api/fs/read", params={"path": "fstest/doc.md"}, headers=auth)
  assert r.status_code == 200 and r.text == "# Hello\n"
  m = client.get("/api/fs/read",
                 params={"path": "fstest/doc.md", "meta": 1}, headers=auth).json()
  assert m["name"] == "doc.md" and m["is_binary"] is False and m["writable"] is True


def test_read_denied_secret_403(client, auth, fsroot):
  root, _, made = fsroot
  secret = root / "service-token.txt"
  secret.write_text("jwt"); made.append(secret)
  r = client.get("/api/fs/read", params={"path": "service-token.txt"}, headers=auth)
  assert r.status_code == 403


def test_write_create_overwrite_and_roundtrip(client, auth, fsroot):
  _, work, _ = fsroot
  rel = "fstest/notes/new.md"
  r = client.put("/api/fs/write", params={"path": rel},
                 content="first", headers={**auth, "Content-Type": "text/plain"})
  assert r.status_code == 200
  assert client.get("/api/fs/read", params={"path": rel}, headers=auth).text == "first"
  client.put("/api/fs/write", params={"path": rel},
             content="second", headers={**auth, "Content-Type": "text/plain"})
  assert client.get("/api/fs/read", params={"path": rel}, headers=auth).text == "second"


def test_write_denied_and_traversal(client, auth, fsroot):
  r = client.put("/api/fs/write", params={"path": ".secret-key"},
                 content="x", headers={**auth, "Content-Type": "text/plain"})
  assert r.status_code == 403
  t = client.put("/api/fs/write", params={"path": "../escape.txt"},
                 content="x", headers={**auth, "Content-Type": "text/plain"})
  assert t.status_code == 400


def test_tree_traversal_rejected(client, auth, fsroot):
  r = client.get("/api/fs/tree", params={"path": "../../etc"}, headers=auth)
  assert r.status_code == 400


def test_git_status(client, auth, fsroot):
  _, work, _ = fsroot
  repo = work / "repo"
  repo.mkdir(exist_ok=True)
  env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
  import os
  e = {**os.environ, **env}
  run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], env=e,
                                  capture_output=True, text=True)
  run("init", "-q")
  run("-c", "user.name=t", "-c", "user.email=t@t", "commit",
      "--allow-empty", "-q", "-m", "init")
  (repo / "tracked.txt").write_text("v1")
  run("add", "tracked.txt")
  (repo / "loose.txt").write_text("untracked")

  r = client.get("/api/fs/git", params={"path": "fstest/repo"}, headers=auth)
  assert r.status_code == 200
  body = r.json()
  assert body["repo_root"] == "fstest/repo"
  assert body["counts"]["staged"] == 1
  assert body["counts"]["untracked"] == 1
  assert any(f["path"] == "tracked.txt" for f in body["staged"])

  # No repo above a plain dir -> 404.
  assert client.get("/api/fs/git", params={"path": "fstest"},
                    headers=auth).status_code == 404


def test_owner_required(client, fsroot):
  assert client.get("/api/fs/tree").status_code == 401
  assert client.get("/api/fs/read", params={"path": "fstest/x"}).status_code == 401
  assert client.put("/api/fs/write", params={"path": "fstest/x"},
                    content="x").status_code == 401
