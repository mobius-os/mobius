"""Tests for the owner filesystem + git oversight API (routes/fs.py)."""

import subprocess
from pathlib import Path

import pytest

from app import auth as token_auth, models
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


def _app_auth(db, *, filesystem_access: bool) -> tuple[dict[str, str], models.App]:
  owner = db.query(models.Owner).filter(models.Owner.username == "test").one()
  app = models.App(
    name="Filesystem test app",
    description="",
    jsx_source="export default function App() {}",
    compiled_path="/tmp/filesystem-test-app.js",
    slug="filesystem-test-app",
    filesystem_access=filesystem_access,
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  token = token_auth.create_app_token(
    app.id, owner.username, owner.token_epoch, app.token_nonce,
  )
  return {"Authorization": f"Bearer {token}"}, app


def test_app_token_requires_explicit_filesystem_capability(
  client, owner_token, db, fsroot,
):
  denied_auth, _ = _app_auth(db, filesystem_access=False)
  response = client.get("/api/fs/tree", headers=denied_auth)
  assert response.status_code == 403
  assert "permissions.filesystem_access=true" in response.json()["detail"]


def test_app_filesystem_capability_is_live_and_revocable(
  client, owner_token, db, fsroot,
):
  app_auth, app = _app_auth(db, filesystem_access=True)
  assert client.get("/api/fs/tree", headers=app_auth).status_code == 200

  # The authority comes from the live row, not a long-lived JWT claim.
  app.filesystem_access = False
  db.commit()
  assert client.get("/api/fs/tree", headers=app_auth).status_code == 403


def test_tree_pagination_offset_cursor(client, auth, fsroot):
  _, work, _ = fsroot
  for i in range(7):
    (work / f"f{i}.txt").write_text("x")
  seen, cursor = [], None
  for _ in range(10):  # guard against a runaway cursor
    params = {"path": "fstest", "limit": 3}
    if cursor:
      params["cursor"] = cursor
    d = client.get("/api/fs/tree", params=params, headers=auth).json()
    seen += [e["name"] for e in d["entries"]]
    cursor = d["next_cursor"]
    if not cursor:
      break
  assert sorted(seen) == [f"f{i}.txt" for i in range(7)]
  assert len(seen) == len(set(seen))  # every entry returned exactly once


def test_write_rejects_cross_site(client, auth, fsroot):
  # The reject_cross_site CSRF guard on PUT /write rejects a cross-site fetch.
  r = client.put("/api/fs/write", params={"path": "fstest/x.txt"}, content="x",
                 headers={**auth, "Content-Type": "text/plain",
                          "Sec-Fetch-Site": "cross-site"})
  assert r.status_code == 403


def test_tree_pagination_mixed_case_and_dirs(client, auth, fsroot):
  # The offset cursor must page a dir whose names mix case and whose dirs sort
  # among the files, without skipping or duplicating (the keyset-cursor bug).
  _, work, _ = fsroot
  files = ["Banana.txt", "apple.txt", "Zebra.txt", "mango.txt", "Cherry.txt"]
  for n in files:
    (work / n).write_text("x")
  (work / "Zdir").mkdir(exist_ok=True)
  (work / "adir").mkdir(exist_ok=True)
  seen, cursor = [], None
  for _ in range(20):
    params = {"path": "fstest", "limit": 2}
    if cursor:
      params["cursor"] = cursor
    d = client.get("/api/fs/tree", params=params, headers=auth).json()
    seen += [e["name"] for e in d["entries"]]
    cursor = d["next_cursor"]
    if not cursor:
      break
  assert sorted(seen) == sorted(files + ["Zdir", "adir"])
  assert len(seen) == len(set(seen))


def test_db_sidecars_denied(client, auth, fsroot):
  # The DB runs in WAL mode; its -wal/-shm/-journal sidecars hold live DB
  # pages and must be denied just like db/ultimate.db itself.
  root, _, made = fsroot
  dbdir = root / "db"
  dbdir.mkdir(exist_ok=True)
  made.append(dbdir)
  for name in ("ultimate.db", "ultimate.db-wal", "ultimate.db-shm",
               "ultimate.db-journal"):
    (dbdir / name).write_text("secret pages")
  tree = client.get("/api/fs/tree", params={"path": "db"}, headers=auth).json()
  assert tree["entries"] == []
  assert {"ultimate.db", "ultimate.db-wal", "ultimate.db-shm",
          "ultimate.db-journal"} <= set(tree["redacted"])
  for name in ("ultimate.db-wal", "ultimate.db-shm", "ultimate.db-journal"):
    assert client.get("/api/fs/read", params={"path": f"db/{name}"},
                      headers=auth).status_code == 403
    assert client.put("/api/fs/write", params={"path": f"db/{name}"},
                      content="x",
                      headers={**auth, "Content-Type": "text/plain"}).status_code == 403
