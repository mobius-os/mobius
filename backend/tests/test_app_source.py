"""Installed app source is inspectable and safely editable in one workspace."""

from __future__ import annotations

from pathlib import Path

from app import models
from app.config import get_settings
from app.timeutil import now_naive_utc
from test_app_fixtures import create_local_app


def _source_app(client, auth, name: str = "Source view") -> tuple[dict, Path]:
  root = Path(get_settings().data_dir) / "apps" / "source-view"
  app = create_local_app(
    client,
    auth,
    name=name,
    source_dir=root,
    jsx_source="export default function App() {\n  return <main>Before</main>\n}\n",
  )
  (root / "styles").mkdir()
  (root / "styles" / "main.css").write_text("main { color: blue; }\n")
  return app, root


def test_app_source_lists_and_reads_without_exposing_generated_or_linked_paths(
  client, auth, tmp_path,
):
  app, root = _source_app(client, auth)
  (root / "node_modules" / "package").mkdir(parents=True)
  (root / "node_modules" / "package" / "index.js").write_text("hidden\n")
  (root / "dist").mkdir()
  (root / "dist" / "bundle.js").write_text("hidden\n")
  outside = tmp_path / "outside.txt"
  outside.write_text("secret\n")
  (root / "linked.txt").symlink_to(outside)

  listing = client.get(
    f"/api/apps/{app['id']}/source/files?recursive=true", headers=auth,
  )
  assert listing.status_code == 200, listing.text
  paths = [entry["path"] for entry in listing.json()["entries"]]
  assert "index.jsx" in paths
  assert "styles/main.css" in paths
  assert not any(path.startswith((".git/", "dist/", "node_modules/")) for path in paths)
  assert "linked.txt" not in paths

  source = client.get(
    f"/api/apps/{app['id']}/source/file?path=index.jsx", headers=auth,
  )
  assert source.status_code == 200, source.text
  assert "Before" in source.json()["content"]

  for hidden in (".git/config", "node_modules/package/index.js", "linked.txt"):
    response = client.get(
      f"/api/apps/{app['id']}/source/file?path={hidden}", headers=auth,
    )
    assert response.status_code == 403

  traversal = client.get(
    f"/api/apps/{app['id']}/source/file?path=../outside.txt", headers=auth,
  )
  assert traversal.status_code == 400
  blocked_write = client.put(
    f"/api/apps/{app['id']}/source/file?path=index.jsx",
    json={"content": "no", "expected_revision": source.json()["revision"]},
  )
  assert blocked_write.status_code == 401
  assert "Before" in (root / "index.jsx").read_text()


def test_app_source_mutations_use_revision_guards_and_confined_paths(client, auth):
  app, root = _source_app(client, auth)
  source_url = f"/api/apps/{app['id']}/source/file?path=index.jsx"
  opened = client.get(source_url, headers=auth).json()

  missing_revision = client.put(
    source_url, headers=auth, json={"content": "missing guard\n"},
  )
  assert missing_revision.status_code == 428

  saved = client.put(source_url, headers=auth, json={
    "content": "export default function App() { return <main>After</main> }\n",
    "expected_revision": opened["revision"],
  })
  assert saved.status_code == 200, saved.text
  assert saved.json()["revision"]
  assert "After" in (root / "index.jsx").read_text()

  stale = client.put(source_url, headers=auth, json={
    "content": "stale overwrite\n",
    "expected_revision": opened["revision"],
  })
  assert stale.status_code == 409
  assert stale.json()["detail"]["code"] == "file_revision_conflict"
  assert "After" in (root / "index.jsx").read_text()

  created = client.put(
    f"/api/apps/{app['id']}/source/file?path=notes.txt",
    headers=auth,
    json={"content": "draft\n", "expected_revision": None},
  )
  assert created.status_code == 200, created.text

  folder = client.post(
    f"/api/apps/{app['id']}/source/folder",
    headers=auth,
    json={"path": "assets/icons"},
  )
  assert folder.status_code == 200, folder.text
  uploaded = client.put(
    f"/api/apps/{app['id']}/source/file-bytes?path=assets/icons/mark.bin",
    headers={**auth, "If-None-Match": "*", "Content-Type": "application/octet-stream"},
    content=b"\x00\x01mark",
  )
  assert uploaded.status_code == 200, uploaded.text

  moved = client.post(
    f"/api/apps/{app['id']}/source/move",
    headers=auth,
    json={"from_path": "notes.txt", "to_path": "assets/notes.txt"},
  )
  assert moved.status_code == 200, moved.text
  assert (root / "assets" / "notes.txt").is_file()

  deleted = client.delete(
    f"/api/apps/{app['id']}/source/file?path=assets/icons",
    headers=auth,
  )
  assert deleted.status_code == 200, deleted.text
  assert not (root / "assets" / "icons").exists()

  traversal = client.post(
    f"/api/apps/{app['id']}/source/move",
    headers=auth,
    json={"from_path": "assets/notes.txt", "to_path": "../escaped.txt"},
  )
  assert traversal.status_code == 400


def test_app_source_reports_its_own_git_changes_and_diff(client, auth):
  app, root = _source_app(client, auth)
  (root / "index.jsx").write_text(
    "export default function App() {\n  return <main>After</main>\n}\n",
  )
  (root / "notes.md").write_text("New note\n")

  response = client.get(
    f"/api/apps/{app['id']}/source/git/status", headers=auth,
  )
  assert response.status_code == 200, response.text
  status = response.json()
  assert status["available"] is True
  assert status["repository_scope"] == "project"
  assert {row["path"]: row["status"] for row in status["changes"]} == {
    "index.jsx": "modified",
    "notes.md": "untracked",
    "styles/main.css": "untracked",
  }

  diff = client.get(
    f"/api/apps/{app['id']}/source/git/diff?path=index.jsx", headers=auth,
  )
  assert diff.status_code == 200, diff.text
  assert diff.json()["status"] == "modified"
  assert diff.json()["changed_lines"] == [2]


def test_deleted_or_non_app_source_is_not_inspectable(client, auth, db, tmp_path):
  app, _root = _source_app(client, auth)
  row = db.get(models.App, app["id"])
  row.deleted_at = now_naive_utc()
  db.commit()
  assert client.get(
    f"/api/apps/{app['id']}/source/files", headers=auth,
  ).status_code == 404

  outside = tmp_path / "not-an-app-source"
  outside.mkdir()
  row = models.App(
    name="Outside",
    description="",
    jsx_source="",
    compiled_path="",
    slug="outside",
    source_dir=str(outside),
  )
  db.add(row)
  db.commit()
  assert client.get(
    f"/api/apps/{row.id}/source/files", headers=auth,
  ).status_code == 404
