"""First-class Project persistence, confinement, and legacy compatibility."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from app import models
from app.agent_coordination import build_coordination_context
from app.chat_context import _build_app_context
from app.chat_retention import purge_expired_chat_tombstones
from app.chat_waits import declare_wait
from app.project_retention import purge_expired_project_tombstones
from app.timeutil import SOFT_DELETE_TTL, now_naive_utc


def _create_project_chat(client, auth, project, title="New chat", request_id=None):
  response = client.post(
    f"/api/projects/{project['id']}/chats",
    headers=auth,
    json={
      "title": title,
      "recovery_request_id": request_id or f"{project['id']}:{title}",
    },
  )
  assert response.status_code == 201, response.text
  return response.json()


def test_blank_project_starts_without_chat_and_has_confined_files(
  client, auth, db,
):
  created = client.post(
    "/api/projects",
    headers=auth,
    json={"name": "Research", "template_id": "blank"},
  )
  assert created.status_code == 200, created.text
  project = created.json()
  assert project["name"] == "Research"
  assert project["chat_id"] is None
  assert client.get(
    f"/api/projects/{project['id']}/chats", headers=auth,
  ).json() == []

  first_chat = _create_project_chat(
    client, auth, project, "Research notes", "notes-chat",
  )
  second_chat = _create_project_chat(
    client, auth, project, "Implementation", "implementation-chat",
  )
  listed_chats = client.get(
    f"/api/projects/{project['id']}/chats", headers=auth,
  ).json()
  assert {row["id"] for row in listed_chats} == {
    first_chat["id"], second_chat["id"],
  }
  assert all(db.get(models.Chat, row["id"]).project_id == project["id"]
             for row in listed_chats)
  # Project chats now appear in Recents too, each carrying its project so the
  # drawer can render a clickable project chip.
  chat_list = client.get("/api/chats", headers=auth)
  assert chat_list.status_code == 200
  rows_by_id = {row["id"]: row for row in chat_list.json()}
  assert {first_chat["id"], second_chat["id"]} <= set(rows_by_id)
  for chat_id in (first_chat["id"], second_chat["id"]):
    project_ref = rows_by_id[chat_id]["project"]
    assert project_ref["id"] == project["id"]
    assert project_ref["name"] == "Research"
    # The composer's @-mention turns a picked file into an ordinary path under
    # the data dir, so the ref carries the project's logical root locator.
    assert project_ref["root_path"].startswith("projects/")
  # The chat detail response carries the same ref for the composer.
  detail = client.get(f"/api/chats/{first_chat['id']}", headers=auth)
  assert detail.status_code == 200
  assert detail.json()["project"]["id"] == project["id"]

  saved = client.put(
    f"/api/projects/{project['id']}/file?path=notes/idea.md",
    headers=auth,
    json={"content": "A durable idea.", "expected_revision": None},
  )
  assert saved.status_code == 200, saved.text
  listing = client.get(
    f"/api/projects/{project['id']}/files?path=notes", headers=auth,
  )
  assert listing.status_code == 200
  assert listing.json()["entries"][0]["path"] == "notes/idea.md"
  opened = client.get(
    f"/api/projects/{project['id']}/file?path=notes/idea.md", headers=auth,
  )
  assert opened.json()["content"] == "A durable idea."

  traversal = client.get(
    f"/api/projects/{project['id']}/file?path=../../etc/passwd", headers=auth,
  )
  assert traversal.status_code in (400, 404)

  folder = client.post(
    f"/api/projects/{project['id']}/folder",
    headers=auth,
    json={"path": "assets/images"},
  )
  assert folder.status_code == 200, folder.text
  assert folder.json()["path"] == "assets/images"
  root_listing = client.get(
    f"/api/projects/{project['id']}/files", headers=auth,
  ).json()
  assert any(row["path"] == "assets" and row["type"] == "directory"
             for row in root_listing["entries"])

  # The recursive form (the composer's @-mention file index) flattens every
  # file under the root with the same exclusions: files only, no `artifacts/`.
  client.put(
    f"/api/projects/{project['id']}/file?path=artifacts/x/output/built.html",
    headers=auth, json={"content": "<p>built</p>", "expected_revision": None},
  )
  recursive = client.get(
    f"/api/projects/{project['id']}/files?recursive=true", headers=auth,
  )
  assert recursive.status_code == 200
  recursive_paths = [row["path"] for row in recursive.json()["entries"]]
  assert "notes/idea.md" in recursive_paths
  assert all(row["type"] == "file" for row in recursive.json()["entries"])
  assert not any(p.startswith("artifacts/") for p in recursive_paths)


def test_project_files_reserve_git_metadata_from_browse_and_mutation(
  client, auth, db,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Repository workspace", "template_id": "blank"},
  ).json()
  row = db.get(models.Project, project["id"])
  root = Path(os.environ["DATA_DIR"]) / row.root_path
  (root / ".git").mkdir()
  (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
  (root / "packages" / "child").mkdir(parents=True)
  (root / "packages" / "child" / ".git").write_text(
    "gitdir: ../../../.git/modules/child\n", encoding="utf-8",
  )
  (root / "notes.md").write_text("safe", encoding="utf-8")
  base = f"/api/projects/{project['id']}"

  root_listing = client.get(f"{base}/files", headers=auth)
  assert root_listing.status_code == 200
  assert ".git" not in {entry["name"] for entry in root_listing.json()["entries"]}
  recursive = client.get(f"{base}/files?recursive=true", headers=auth)
  assert recursive.status_code == 200
  assert all(
    ".git" not in Path(entry["path"]).parts
    for entry in recursive.json()["entries"]
  )

  blocked = (
    client.get(f"{base}/files?path=.git", headers=auth),
    client.get(f"{base}/file?path=.git/config", headers=auth),
    client.get(f"{base}/file?path=packages/child/.git", headers=auth),
    client.post(
      f"{base}/folder", headers=auth, json={"path": ".git/hooks"},
    ),
    client.put(
      f"{base}/file?path=.git/config", headers=auth,
      json={"content": "unsafe", "expected_revision": None},
    ),
    client.put(
      f"{base}/file-bytes?path=packages/child/.git",
      headers={**auth, "If-Match": "0" * 64}, content=b"unsafe",
    ),
    client.delete(f"{base}/file?path=.git/config", headers=auth),
    client.delete(f"{base}/file?path=packages/child/.git", headers=auth),
    client.post(
      f"{base}/move", headers=auth,
      json={"from_path": ".git/config", "to_path": "config-copy"},
    ),
    client.post(
      f"{base}/move", headers=auth,
      json={"from_path": "notes.md", "to_path": ".git/notes.md"},
    ),
  )
  for response in blocked:
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Path is not available in Projects."

  assert (root / ".git" / "config").read_text(encoding="utf-8") == "[core]\n"
  assert (root / "packages" / "child" / ".git").is_file()
  assert (root / "notes.md").read_text(encoding="utf-8") == "safe"

def test_project_file_revisions_reject_stale_edits_and_changes_reconnect(
  client, auth,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Live edits", "template_id": "blank"},
  ).json()
  baseline = client.get(
    f"/api/projects/{project['id']}/changes", headers=auth,
  ).json()
  assert baseline == {"cursor": 0, "changes": [], "truncated": False}

  created = client.put(
    f"/api/projects/{project['id']}/file?path=notes.md", headers=auth,
    json={"content": "first", "expected_revision": None},
  )
  assert created.status_code == 200, created.text
  first_revision = created.json()["revision"]
  opened = client.get(
    f"/api/projects/{project['id']}/file?path=notes.md", headers=auth,
  ).json()
  assert opened["revision"] == first_revision

  missing_precondition = client.put(
    f"/api/projects/{project['id']}/file?path=notes.md", headers=auth,
    json={"content": "unsafe"},
  )
  assert missing_precondition.status_code == 428
  assert missing_precondition.json()["detail"]["code"] == "file_revision_required"

  saved = client.put(
    f"/api/projects/{project['id']}/file?path=notes.md", headers=auth,
    json={"content": "second", "expected_revision": first_revision},
  )
  assert saved.status_code == 200, saved.text
  second_revision = saved.json()["revision"]
  assert second_revision != first_revision

  stale = client.put(
    f"/api/projects/{project['id']}/file?path=notes.md", headers=auth,
    json={"content": "silent overwrite", "expected_revision": first_revision},
  )
  assert stale.status_code == 409
  assert stale.json()["detail"]["code"] == "file_revision_conflict"
  assert client.get(
    f"/api/projects/{project['id']}/file?path=notes.md", headers=auth,
  ).json()["content"] == "second"

  changes = client.get(
    f"/api/projects/{project['id']}/changes", headers=auth,
    params={"after": baseline["cursor"]},
  ).json()
  assert [row["kind"] for row in changes["changes"]] == [
    "file_saved", "file_saved",
  ]
  assert changes["changes"][-1]["revision"] == second_revision


def test_project_change_tail_is_bounded_and_reports_a_pruned_cursor(
  client, auth, monkeypatch,
):
  from app import project_activity

  monkeypatch.setattr(project_activity, "PROJECT_CHANGE_LIMIT", 3)
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Bounded changes", "template_id": "blank"},
  ).json()
  for number in range(5):
    saved = client.put(
      f"/api/projects/{project['id']}/file?path={number}.txt",
      headers=auth,
      json={"content": str(number), "expected_revision": None},
    )
    assert saved.status_code == 200, saved.text

  changes = client.get(
    f"/api/projects/{project['id']}/changes", headers=auth,
    params={"after": 0},
  ).json()
  assert changes["truncated"] is True
  assert [row["path"] for row in changes["changes"]] == [
    "2.txt", "3.txt", "4.txt",
  ]


def test_nested_symlink_parent_cannot_escape_project_root(client, auth, db):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Confined", "template_id": "blank"},
  ).json()
  row = db.get(models.Project, project["id"])
  root = Path(os.environ["DATA_DIR"]) / row.root_path
  outside = Path(os.environ["DATA_DIR"]) / "outside-project"
  outside.mkdir()
  (outside / "secret.txt").write_text("secret")
  (root / "nested").symlink_to(outside, target_is_directory=True)

  for method, endpoint, kwargs in (
    (client.get, "file?path=nested/secret.txt", {}),
    (client.put, "file?path=nested/new.txt", {"json": {"content": "escape"}}),
    (client.delete, "file?path=nested/secret.txt", {}),
  ):
    response = method(
      f"/api/projects/{project['id']}/{endpoint}", headers=auth, **kwargs,
    )
    assert response.status_code in (400, 403), response.text
  assert (outside / "secret.txt").read_text() == "secret"
  assert not (outside / "new.txt").exists()


def test_project_creation_retry_is_idempotent(client, auth, db):
  body = {
    "name": "Retry-safe",
    "template_id": "blank",
    "recovery_request_id": "browser-request-1",
  }
  first = client.post("/api/projects", headers=auth, json=body)
  second = client.post("/api/projects", headers=auth, json=body)
  assert first.status_code == second.status_code == 200
  assert first.json()["id"] == second.json()["id"]
  assert first.json()["chat_id"] == second.json()["chat_id"]
  assert db.query(models.Project).count() == 1
  assert db.query(models.Chat).count() == 0


def test_project_color_can_be_set_and_cleared_without_affecting_other_fields(
  client, auth,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Colored", "template_id": "blank"},
  ).json()
  assert project["color"] is None

  colored = client.patch(
    f"/api/projects/{project['id']}", headers=auth,
    json={"color": "#3B82F6"},
  )
  assert colored.status_code == 200, colored.text
  assert colored.json()["color"] == "#3b82f6"
  assert colored.json()["name"] == "Colored"

  invalid = client.patch(
    f"/api/projects/{project['id']}", headers=auth,
    json={"color": "blue"},
  )
  assert invalid.status_code == 422
  assert client.get(
    f"/api/projects/{project['id']}", headers=auth,
  ).json()["color"] == "#3b82f6"

  cleared = client.patch(
    f"/api/projects/{project['id']}", headers=auth,
    json={"color": None},
  )
  assert cleared.status_code == 200, cleared.text
  assert cleared.json()["color"] is None


def test_project_open_recency_and_pin_are_navigation_state(client, auth, db):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Recent project", "template_id": "blank"},
  ).json()
  assert project["last_opened_at"] is None
  assert project["pinned_at"] is None

  row = db.get(models.Project, project["id"])
  original_updated_at = row.updated_at
  opened = client.post(
    f"/api/projects/{project['id']}/opened", headers=auth,
  )
  assert opened.status_code == 204, opened.text

  listed = client.get("/api/projects", headers=auth).json()
  recent = next(item for item in listed if item["id"] == project["id"])
  assert recent["last_opened_at"] is not None
  assert recent["pinned_at"] is None
  db.refresh(row)
  assert row.updated_at == original_updated_at

  pinned = client.patch(
    f"/api/projects/{project['id']}", headers=auth, json={"pinned": True},
  )
  assert pinned.status_code == 200, pinned.text
  assert pinned.json()["pinned_at"] is not None
  db.refresh(row)
  assert row.updated_at == original_updated_at

  unpinned = client.patch(
    f"/api/projects/{project['id']}", headers=auth, json={"pinned": False},
  )
  assert unpinned.status_code == 200, unpinned.text
  assert unpinned.json()["pinned_at"] is None
  assert unpinned.json()["last_opened_at"] == recent["last_opened_at"]


def test_concurrent_project_creation_retry_has_one_row_and_root(client, auth, db):
  body = {
    "name": "Concurrent",
    "template_id": "blank",
    "recovery_request_id": "same-concurrent-request",
  }
  with ThreadPoolExecutor(max_workers=2) as pool:
    responses = list(pool.map(
      lambda _: client.post("/api/projects", headers=auth, json=body),
      range(2),
    ))
  assert [response.status_code for response in responses] == [200, 200]
  assert len({response.json()["id"] for response in responses}) == 1
  assert db.query(models.Project).count() == 1
  assert db.query(models.Chat).count() == 0
  row = db.query(models.Project).one()
  assert (Path(os.environ["DATA_DIR"]) / row.root_path).is_dir()


def test_each_project_chat_gets_context_and_can_be_deleted_independently(
  client, auth, db,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Site", "template_id": "blank"},
  ).json()
  chat = _create_project_chat(client, auth, project, "Site plan")

  block, env = _build_app_context(db, chat["id"], os.environ["DATA_DIR"])
  assert "<project_context>" in block
  assert env["PROJECT_ID"] == project["id"]
  assert Path(env["PROJECT_ROOT"]).is_dir()
  deleted = client.delete(f"/api/chats/{chat['id']}", headers=auth)
  assert deleted.status_code == 204
  assert client.get(
    f"/api/projects/{project['id']}/chats", headers=auth,
  ).json() == []


def test_individually_deleted_project_chat_and_coordination_rows_expire(
  client, auth, db,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Long-lived project", "template_id": "blank"},
  ).json()
  expired = _create_project_chat(client, auth, project, "Finished agent")
  survivor = _create_project_chat(client, auth, project, "Remaining agent")
  message = models.ProjectAgentMessage(
    id="message-from-expired-project-chat",
    project_id=project["id"],
    from_chat_id=expired["id"],
    to_chat_id=survivor["id"],
    body="handoff",
  )
  claim = models.ProjectWorkClaim(
    id="claim-from-expired-project-chat",
    project_id=project["id"],
    actor_key=f"agent:{expired['id']}",
    actor_kind="agent",
    display_name="Finished agent",
    chat_id=expired["id"],
    path="index.html",
    summary="done",
    expires_at=now_naive_utc() + timedelta(days=30),
  )
  db.add_all([message, claim])
  db.commit()
  message_id = message.id
  claim_id = claim.id

  assert client.delete(
    f"/api/chats/{expired['id']}", headers=auth,
  ).status_code == 204
  db.get(models.Chat, expired["id"]).deleted_at = (
    now_naive_utc() - SOFT_DELETE_TTL - timedelta(seconds=1)
  )
  db.commit()

  assert expired["id"] in purge_expired_chat_tombstones(db)
  assert db.get(models.Chat, expired["id"]) is None
  assert db.get(models.Chat, survivor["id"]) is not None
  assert db.get(models.Project, project["id"]) is not None
  assert db.get(models.ProjectAgentMessage, message_id) is None
  assert db.get(models.ProjectWorkClaim, claim_id) is None


def test_manifest_template_scaffolds_files_and_snapshots_metadata(
  client, auth, db,
):
  source = Path(os.environ["DATA_DIR"]) / "apps" / "latex"
  (source / "templates").mkdir(parents=True)
  (source / "templates" / "main.tex").write_text("\\documentclass{article}")
  app = models.App(
    name="LaTeX", description="Documents", jsx_source="",
    slug="latex", source_dir=str(source), version="3.0.0",
    project_templates_json=[{
      "id": "latex",
      "name": "LaTeX document",
      "description": "Typeset a document.",
      "guidance": "Use tectonic for builds.",
      "skills": ["latex"],
      "dependencies": ["tectonic"],
      "previews": [{
        "id": "pdf", "name": "PDF", "kind": "pdf", "path": "main.pdf",
      }],
      "actions": [{
        "id": "build", "name": "Build PDF", "prompt": "Compile main.tex.",
      }],
      "artifact_types": [{
        "id": "latex", "name": "PDF", "extensions": ["tex"],
        "preview": "pdf", "script": "project-builder.sh",
        "output": "{stem}.pdf",
      }],
      "files": {"main.tex": "templates/main.tex"},
    }],
  )
  db.add(app)
  db.commit()

  templates = client.get("/api/projects/templates", headers=auth).json()
  assert [row["key"] for row in templates] == ["blank", "app", "latex:latex"]
  created = client.post(
    "/api/projects", headers=auth,
    json={"name": "Paper", "template_id": "latex:latex"},
  )
  assert created.status_code == 200, created.text
  project = created.json()
  assert project["source_app_id"] == app.id
  assert project["template"]["dependencies"] == ["tectonic"]
  assert project["template"]["previews"][0]["path"] == "main.pdf"
  assert project["template"]["actions"][0]["prompt"] == "Compile main.tex."
  assert project["template"]["artifact_types"][0]["script"] == "project-builder.sh"
  opened = client.get(
    f"/api/projects/{project['id']}/file?path=main.tex", headers=auth,
  )
  assert opened.status_code == 200, opened.text
  assert opened.json()["content"] == "\\documentclass{article}"

  binary = client.put(
    f"/api/projects/{project['id']}/file-bytes?path=figure.png",
    headers={
      **auth,
      "Content-Type": "application/octet-stream",
      "If-None-Match": "*",
    },
    content=b"\x89PNG\r\n\x1a\n",
  )
  assert binary.status_code == 200
  downloaded = client.get(
    f"/api/projects/{project['id']}/file?path=figure.png&download=true",
    headers=auth,
  )
  assert downloaded.content == b"\x89PNG\r\n\x1a\n"

  # A later app update cannot reinterpret an existing project.
  app.project_templates_json[0]["dependencies"] = ["different"]
  db.add(app)
  db.commit()
  stable = client.get(f"/api/projects/{project['id']}", headers=auth).json()
  assert stable["template"]["dependencies"] == ["tectonic"]


def test_ordinary_page_is_not_misidentified_as_builder_output(
  client, auth, db,
):
  data_root = Path(os.environ["DATA_DIR"])
  source = data_root / "apps" / "artifacts-source"
  source.mkdir(parents=True)
  (source / "index.jsx").write_text("export default function App() {}")
  catalog_app = models.App(
    name="Artifacts", description="Standalone work", jsx_source="",
    slug="pages", source_dir=str(source), system_app=True,
  )
  db.add(catalog_app)
  db.commit()
  db.refresh(catalog_app)
  storage = data_root / "apps" / str(catalog_app.id)
  (storage / "artifacts").mkdir(parents=True)
  (storage / "versions" / "artifact-one").mkdir(parents=True)
  original = "<!doctype html><title>Standalone</title><h1>Original</h1>"
  (storage / "versions" / "artifact-one" / "v1.html").write_text(
    original, encoding="utf-8",
  )
  (storage / "artifacts" / "artifact-one.json").write_text(json.dumps({
    "id": "artifact-one", "title": "Standalone", "description": "A demo",
    "current_version": 1, "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-02T00:00:00Z",
  }), encoding="utf-8")

  sources = client.get("/api/projects/import-sources", headers=auth)
  assert sources.status_code == 200, sources.text
  assert sources.json()["artifacts"] == []
  imported = client.post(
    "/api/projects/import", headers=auth,
    json={"kind": "artifact", "source_id": "artifact-one"},
  )
  assert imported.status_code == 409, imported.text
  assert db.query(models.Project).count() == 0
  assert (storage / "versions" / "artifact-one" / "v1.html").read_text() == original


def test_local_app_import_manages_existing_source_once_without_runtime_data(
  client, auth, db,
):
  data_root = Path(os.environ["DATA_DIR"])
  source = data_root / "apps" / "my-local-app"
  source.mkdir(parents=True)
  (source / "index.jsx").write_text("export default function App() { return null }")
  (source / "styles.css").write_text("body { color: black; }")
  (source / "mobius.json").write_text(json.dumps({
    "name": "My local app", "entry": "index.jsx",
    "source_files": ["styles.css"],
  }))
  app = models.App(
    name="My local app", description="Made in chat", jsx_source="",
    slug="my-local-app", source_dir=str(source), system_app=False,
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  runtime = data_root / "apps" / str(app.id)
  runtime.mkdir(parents=True)
  (runtime / "private-state.json").write_text('{"secret":"not source"}')

  sources = client.get("/api/projects/import-sources", headers=auth)
  assert sources.status_code == 200, sources.text
  assert [(row["id"], row["name"]) for row in sources.json()["apps"]] == [
    (str(app.id), "My local app"),
  ]
  imported = client.post(
    "/api/projects/import", headers=auth,
    json={"kind": "app", "source_id": str(app.id)},
  )
  assert imported.status_code == 200, imported.text
  project = imported.json()
  assert project["template"]["imported_from"] == {
    "kind": "app", "id": str(app.id), "slug": "my-local-app",
    "name": "My local app", "management": "linked",
  }
  row = db.get(models.Project, project["id"])
  project_root = data_root / row.root_path
  assert (project_root / "index.jsx").is_file()
  assert (project_root / "styles.css").is_file()
  assert (project_root / "mobius.json").is_file()
  assert not (project_root / "private-state.json").exists()
  assert project_root == source
  assert sources.json()["management"] == "linked"
  opened = client.get(f"/api/projects/{project['id']}/file?path=index.jsx", headers=auth).json()
  changed = client.put(
    f"/api/projects/{project['id']}/file?path=index.jsx", headers=auth,
    json={"content": "// shared draft", "expected_revision": opened["revision"]},
  )
  assert changed.status_code == 200, changed.text
  assert (source / "index.jsx").read_text() == "// shared draft"
  repeated = client.post("/api/projects/import", headers=auth,
    json={"kind": "app", "source_id": str(app.id), "recovery_request_id": "another-click"})
  assert repeated.json()["id"] == project["id"]
  assert db.query(models.Project).count() == 1
  assert client.get("/api/projects/import-sources", headers=auth).json()["apps"] == []
  assert client.delete(f"/api/projects/{project['id']}", headers=auth).status_code == 204
  assert (source / "index.jsx").read_text() == "// shared draft"
  assert client.post("/api/projects/import", headers=auth,
    json={"kind": "app", "source_id": str(app.id)}).status_code == 409



@pytest.mark.parametrize("source_state", ["valid", "missing", "remapped", "symlink", "builder_uninstalled"])
def test_latex_artifact_import_manages_declared_sources_in_place(
  client, auth, db, source_state,
):
  data_root = Path(os.environ["DATA_DIR"])
  artifacts_source = data_root / "apps" / "artifacts-package"
  latex_source = data_root / "apps" / "latex-package"
  artifacts_source.mkdir(parents=True)
  latex_source.mkdir(parents=True)
  (artifacts_source / "index.jsx").write_text("export default function App() {}")
  (latex_source / "index.jsx").write_text("export default function App() {}")
  catalog_app = models.App(
    name="Artifacts", description="Standalone work", jsx_source="",
    slug="pages", source_dir=str(artifacts_source), system_app=True,
  )
  latex_app = models.App(
    name="LaTeX", description="Documents", jsx_source="",
    slug="latex", source_dir=str(latex_source), project_templates_json=[{
      "id": "document", "name": "LaTeX document", "files": {},
      "skills": ["latex-project.md"], "dependencies": ["tectonic"],
      "previews": [{
        "id": "document", "name": "Document", "kind": "pdf",
        "path": "main.pdf",
      }],
      "artifact_types": [{
        "id": "latex", "name": "PDF", "extensions": ["tex"],
        "preview": "pdf", "script": "project-builder.sh",
        "output": "{stem}.pdf",
      }],
    }],
  )
  db.add_all([catalog_app, latex_app])
  db.commit()
  db.refresh(catalog_app)
  storage = data_root / "apps" / str(catalog_app.id)
  (storage / "artifacts").mkdir(parents=True)
  (storage / "versions" / "paper-one").mkdir(parents=True)
  (storage / "sources" / "paper-one").mkdir(parents=True)
  (storage / "versions" / "paper-one" / "v1.html").write_text(
    "<embed src='data:application/pdf;base64,AA=='>", encoding="utf-8",
  )
  tex = "\\documentclass{article}\\begin{document}Hello\\end{document}"
  (storage / "sources" / "paper-one" / "main.tex").write_text(tex)
  (storage / "artifacts" / "paper-one.json").write_text(json.dumps({
    "id": "paper-one", "title": "Paper", "current_version": 1,
    "created_at": "2026-01-01T00:00:00Z",
    "updated_at": "2026-01-01T00:00:00Z",
    "project_import": {
      "template_id": "latex:document",
      "files": [{
        "storage_path": "sources/paper-one/main.tex", "path": "main.tex",
      }],
    },
  }))

  record_path = storage / "artifacts" / "paper-one.json"
  if source_state == "missing":
    (storage / "sources" / "paper-one" / "main.tex").unlink()
  elif source_state == "remapped":
    record = json.loads(record_path.read_text())
    record["project_import"]["files"][0]["path"] = "renamed.tex"
    record_path.write_text(json.dumps(record))
  elif source_state == "symlink":
    target = storage / "sources" / "paper-one" / "main.tex"
    target.unlink()
    outside = data_root / "outside.tex"
    outside.write_text("private")
    target.symlink_to(outside)
  elif source_state == "builder_uninstalled":
    latex_app.deleted_at = now_naive_utc()
    db.commit()
  listed = client.get("/api/projects/import-sources", headers=auth).json()["artifacts"]
  if source_state != "valid":
    assert listed == []
    response = client.post("/api/projects/import", headers=auth,
      json={"kind": "artifact", "source_id": "paper-one"})
    assert response.status_code in (409, 422), response.text
    assert db.query(models.Project).count() == 0
    assert record_path.is_file()
    return
  assert listed[0]["catalog_app_id"] == catalog_app.id
  assert listed[0]["project_type"] == "latex:document"

  imported = client.post(
    "/api/projects/import", headers=auth,
    json={"kind": "artifact", "source_id": "paper-one"},
  )
  assert imported.status_code == 200, imported.text
  project = imported.json()
  assert project["project_type"] == "latex:document"
  assert project["artifacts"][0]["source"] == "main.tex"
  opened = client.get(
    f"/api/projects/{project['id']}/file?path=main.tex", headers=auth,
  )
  assert opened.json()["content"] == tex
  row = db.get(models.Project, project["id"])
  assert not (data_root / row.root_path / "index.html").exists()
  assert data_root / row.root_path == storage / "sources" / "paper-one"
  assert project["template"]["imported_from"]["management"] == "linked"
  assert client.get("/api/projects/import-sources", headers=auth).json()["artifacts"] == []
  updated = client.put(f"/api/projects/{project['id']}/file?path=main.tex", headers=auth,
    json={"content": tex + "% shared draft", "expected_revision": opened.json()["revision"]})
  assert updated.status_code == 200, updated.text
  assert (storage / "sources" / "paper-one" / "main.tex").read_text() == tex + "% shared draft"
  assert (storage / "versions" / "paper-one" / "v1.html").read_text() == "<embed src='data:application/pdf;base64,AA=='>"



def test_file_bytes_rejects_malformed_content_length(client, auth):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Headers", "template_id": "blank"},
  ).json()
  malformed = client.put(
    f"/api/projects/{project['id']}/file-bytes?path=asset.bin",
    headers={
      **auth,
      "Content-Type": "application/octet-stream",
      "Content-Length": "not-a-number",
    },
    content=b"asset",
  )
  assert malformed.status_code == 400, malformed.text
  assert "Content-Length" in malformed.json()["detail"]


def test_concurrent_file_writes_and_delete_are_atomic(client, auth):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "File races", "template_id": "blank"},
  ).json()
  project_id = project["id"]
  path = "shared/state.txt"
  payloads = ("A" * 8192, "B" * 8192)

  def write(payload):
    return client.put(
      f"/api/projects/{project_id}/file?path={path}&force=true",
      headers=auth, json={"content": payload},
    )

  def delete():
    return client.delete(
      f"/api/projects/{project_id}/file?path={path}", headers=auth,
    )

  # Seed once so a concurrent delete has a valid target; unique temp names plus
  # os.replace guarantee the surviving file is one complete writer payload.
  assert write("seed").status_code == 200
  # Production has one event loop. A contextless TestClient per worker thread
  # creates separate loops and cannot exercise shared asyncio source locks.
  import asyncio
  import httpx
  from app.main import app

  async def race():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as ac:
      responses = await asyncio.gather(
        ac.put(f"/api/projects/{project_id}/file?path={path}&force=true", headers=auth, json={"content": payloads[0]}),
        ac.delete(f"/api/projects/{project_id}/file?path={path}", headers=auth),
        ac.put(f"/api/projects/{project_id}/file?path={path}&force=true", headers=auth, json={"content": payloads[1]}),
      )
      return [response.status_code for response in responses]
  statuses = asyncio.run(race())
  assert all(status in (200, 404) for status in statuses), statuses
  final = client.get(
    f"/api/projects/{project_id}/file?path={path}", headers=auth,
  )
  if final.status_code == 200:
    assert final.json()["content"] in payloads
  else:
    assert final.status_code == 404


def test_legacy_import_reuses_project_chat_without_moving_files(
  client, auth, db,
):
  storage = Path(os.environ["DATA_DIR"]) / "apps"
  app_source = storage / "webstudio-source"
  app_source.mkdir(parents=True)
  app = models.App(
    name="Web Studio", description="Sites", jsx_source="",
    slug="webstudio", source_dir=str(app_source),
    project_templates_json=[{
      "id": "web-app", "name": "Web app", "files": {},
      "skills": ["web"], "dependencies": [],
      "previews": [{
        "id": "site", "name": "Website", "kind": "html",
        "path": "index.html",
      }],
      "artifact_types": [{
        "id": "website", "name": "Website", "extensions": ["html"],
        "preview": "html", "script": "project-builder.sh",
        "output": "{source}",
      }],
    }],
  )
  db.add(app)
  db.commit()
  db.refresh(app)
  legacy = storage / str(app.id) / "projects" / "portfolio"
  (legacy / "files").mkdir(parents=True)
  (legacy / "files" / "index.html").write_text("<h1>Mine</h1>")
  chat = models.Chat(id="legacy-project-chat", title="Portfolio", messages=[])
  db.add(chat)
  db.commit()
  (legacy / "chat_id.json").write_text(json.dumps({"id": chat.id}))
  (storage / str(app.id) / "projects.json").write_text(json.dumps([
    {"id": "portfolio", "name": "Portfolio"},
  ]))

  candidates = client.get("/api/projects/legacy", headers=auth).json()
  assert candidates == [{
    "legacy_project_id": "portfolio",
    "name": "Portfolio",
    "app_id": app.id,
    "app_name": "Web Studio",
    "imported": False,
  }]
  imported = client.post(
    "/api/projects/import-legacy", headers=auth,
    json={"app_id": app.id, "legacy_project_id": "portfolio"},
  )
  assert imported.status_code == 200, imported.text
  project = imported.json()
  assert project["chat_id"] is None
  assert [row["id"] for row in project["artifacts"]] == ["site"]
  assert project["artifacts"][0]["builder"] == "website"
  db.refresh(chat)
  assert chat.project_id == project["id"]
  assert (legacy / "files" / "index.html").is_file()
  opened = client.get(
    f"/api/projects/{project['id']}/file?path=index.html", headers=auth,
  )
  assert opened.json()["content"] == "<h1>Mine</h1>"

  uninstall = client.delete(f"/api/apps/{app.id}", headers=auth)
  assert uninstall.status_code == 409
  assert uninstall.json()["detail"]["code"] == "app_has_imported_project"


def test_github_import_creates_a_private_project_owned_repository(
  client, auth, db, monkeypatch,
):
  import app.routes.projects as projects_route

  real_run = projects_route.subprocess.run

  def fake_clone(command, **kwargs):
    assert command[:4] == ["gh", "repo", "clone", "octo/example"]
    assert kwargs["env"]["GH_TOKEN"] == "secret-test-token"
    root = Path(command[4])
    root.mkdir(parents=True)
    (root / "index.html").write_text("<h1>Hello</h1>", encoding="utf-8")
    for git_args in (
      ["git", "-C", str(root), "init", "-b", "main"],
      ["git", "-C", str(root), "add", "index.html"],
      [
        "git", "-C", str(root), "-c", "user.name=Test", "-c",
        "user.email=test@example.com", "commit", "-m", "Initial",
      ],
    ):
      real_run(git_args, check=True, capture_output=True)
    return projects_route.subprocess.CompletedProcess(command, 0, b"", b"")

  monkeypatch.setattr(projects_route.github_auth, "get_token", lambda: "secret-test-token")
  monkeypatch.setattr(projects_route.subprocess, "run", fake_clone)
  response = client.post(
    "/api/projects/import-github", headers=auth,
    json={
      "repository": "https://github.com/octo/example.git",
      "recovery_request_id": "github-import-1",
    },
  )
  assert response.status_code == 200, response.text
  project = response.json()
  assert project["name"] == "example"
  assert project["project_type"] == "github:repository"
  assert project["template"]["repository"] == {
    "slug": "octo/example", "url": "https://github.com/octo/example",
  }
  assert project["artifacts"][0]["source"] == "index.html"
  project_row = db.get(models.Project, project["id"])
  root = Path(os.environ["DATA_DIR"]) / project_row.root_path
  assert (root / ".git").is_dir()


def test_project_agents_have_a_confined_roster_mailbox_and_next_turn_context(
  client, auth, db,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Coordinated work", "template_id": "blank"},
  ).json()
  planner = _create_project_chat(client, auth, project, "Planner")
  builder = _create_project_chat(client, auth, project, "Builder")
  reviewer = _create_project_chat(client, auth, project, "Reviewer")
  db.add(models.ChatRun(
    id="planner-run", root_run_id="planner-run", chat_id=planner["id"],
    status="running", provider="codex", goal_objective="Map the file changes",
  ))
  db.commit()

  roster = client.get(f"/api/projects/{project['id']}/agents", headers=auth)
  assert roster.status_code == 200, roster.text
  planner_row = next(row for row in roster.json() if row["id"] == planner["id"])
  assert planner_row["run"]["status"] == "running"
  assert planner_row["run"]["goal"] == "Map the file changes"

  sent = client.post(
    f"/api/projects/{project['id']}/agent-messages", headers=auth,
    json={
      "sender_chat_id": planner["id"],
      "recipients": [builder["id"], reviewer["id"]],
      "body": "I mapped the source; edit index.html first.",
    },
  )
  assert sent.status_code == 200, sent.text
  assert len(sent.json()) == 2
  mailbox = client.get(
    f"/api/projects/{project['id']}/agent-messages",
    headers=auth, params={"chat_id": builder["id"]},
  )
  assert [row["body"] for row in mailbox.json()] == [
    "I mapped the source; edit index.html first.",
  ]
  room = client.get(
    f"/api/agent-coordination/projects/{project['id']}", headers=auth,
  )
  assert room.status_code == 200, room.text
  assert {row["id"] for row in room.json()["peers"]} == {planner["id"]}
  assert [row["body"] for row in room.json()["messages"]] == [
    "I mapped the source; edit index.html first.",
    "I mapped the source; edit index.html first.",
  ]
  claimed = client.put(
    f"/api/projects/{project['id']}/work-claim", headers=auth,
    json={
      "chat_id": planner["id"], "path": "plan.md",
      "summary": "Mapping the release plan",
    },
  )
  assert claimed.status_code == 200, claimed.text

  context, env = _build_app_context(
    db, builder["id"], os.environ["DATA_DIR"],
  )
  context += build_coordination_context(db, builder["id"], None)
  assert env["PROJECT_ID"] == project["id"]
  assert "<agent_coordination>" in context
  assert "Map the file changes" in context
  assert "edit index.html first" in context
  assert "Mapping the release plan" in context
  assert "work_claims" in context


def test_project_agent_direct_notes_broadcasts_and_disconnect_state_stay_confined(
  client, auth, db,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Agent room", "template_id": "blank"},
  ).json()
  lead = _create_project_chat(client, auth, project, "Lead")
  builder = _create_project_chat(client, auth, project, "Builder")
  observer = _create_project_chat(client, auth, project, "Observer")
  outside_project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Outside", "template_id": "blank"},
  ).json()
  outsider = _create_project_chat(client, auth, outside_project, "Outsider")
  ended_at = now_naive_utc()
  db.add_all([
    models.ChatRun(
      id="lead-live", root_run_id="lead-live", chat_id=lead["id"],
      status="running", provider="codex", goal_objective="Coordinate the release",
    ),
    models.ChatRun(
      id="builder-done", root_run_id="builder-done", chat_id=builder["id"],
      status="completed", provider="claude", ended_at=ended_at,
    ),
  ])
  db.commit()

  direct = client.post(
    f"/api/projects/{project['id']}/agent-messages", headers=auth,
    json={
      "sender_chat_id": lead["id"], "recipients": [builder["id"]],
      "body": "Builder owns the CSV editor.",
    },
  )
  assert direct.status_code == 200, direct.text
  broadcast = client.post(
    f"/api/projects/{project['id']}/agent-messages", headers=auth,
    json={
      "sender_chat_id": lead["id"], "broadcast": True,
      "body": "All agents: keep generated artifacts out of commits.",
    },
  )
  assert broadcast.status_code == 200, broadcast.text
  outside = client.post(
    f"/api/projects/{project['id']}/agent-messages", headers=auth,
    json={
      "sender_chat_id": lead["id"], "recipients": [outsider["id"]],
      "body": "This must not cross projects.",
    },
  )
  assert outside.status_code == 404

  builder_mail = client.get(
    f"/api/projects/{project['id']}/agent-messages", headers=auth,
    params={"chat_id": builder["id"]},
  ).json()
  assert [row["body"] for row in builder_mail] == [
    "Builder owns the CSV editor.",
    "All agents: keep generated artifacts out of commits.",
  ]
  observer_mail = client.get(
    f"/api/projects/{project['id']}/agent-messages", headers=auth,
    params={"chat_id": observer["id"]},
  ).json()
  assert [row["body"] for row in observer_mail] == [
    "All agents: keep generated artifacts out of commits.",
  ]

  roster = client.get(f"/api/projects/{project['id']}/agents", headers=auth).json()
  by_id = {row["id"]: row for row in roster}
  assert by_id[lead["id"]]["run"]["summary"] == "Coordinate the release"
  assert by_id[builder["id"]]["run"]["status"] == "completed"
  assert by_id[builder["id"]]["run"]["ended_at"] is not None

  observer_context, _ = _build_app_context(
    db, observer["id"], os.environ["DATA_DIR"],
  )
  observer_context += build_coordination_context(db, observer["id"], None)
  assert "keep generated artifacts out of commits" in observer_context
  assert "Builder owns the CSV editor" not in observer_context
  assert '"status":"completed"' in observer_context
  assert '"ended_at":' in observer_context


def test_project_delete_and_recover_are_atomic_with_its_live_chats(
  client, auth, db,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Recover me", "template_id": "blank"},
  ).json()
  first = _create_project_chat(client, auth, project, "Plan")
  second = _create_project_chat(client, auth, project, "Build")
  first_wait = declare_wait(
    db, chat_id=first["id"], description="plan gate",
    condition_owner="project workflow", kind="command", command="false",
    deadline_secs=3600,
  )
  second_wait = declare_wait(
    db, chat_id=second["id"], description="build gate",
    condition_owner="project workflow", kind="command", command="false",
    deadline_secs=3600,
  )
  row = db.get(models.Project, project["id"])
  root = Path(os.environ["DATA_DIR"]) / row.root_path
  deleted = client.delete(f"/api/projects/{project['id']}", headers=auth)
  assert deleted.status_code == 204
  assert root.is_dir()
  db.expire_all()
  assert db.get(models.Project, project["id"]).deleted_at is not None
  assert db.get(models.Chat, first["id"]).deleted_at is not None
  assert db.get(models.Chat, second["id"]).deleted_at is not None
  assert db.get(models.ChatWait, first_wait.id).status == "cancelled"
  assert db.get(models.ChatWait, second_wait.id).status == "cancelled"

  direct_chat_recovery = client.post(
    f"/api/chats/{first['id']}/recover", headers=auth,
  )
  assert direct_chat_recovery.status_code == 409
  assert direct_chat_recovery.json()["detail"]["code"] == "project_deleted"

  recovered = client.post(
    f"/api/projects/{project['id']}/recover", headers=auth,
  )
  assert recovered.status_code == 200
  db.expire_all()
  assert db.get(models.Project, project["id"]).deleted_at is None
  assert db.get(models.Chat, first["id"]).deleted_at is None
  assert db.get(models.Chat, second["id"]).deleted_at is None
  assert db.get(models.ChatWait, first_wait.id).status == "cancelled"
  assert db.get(models.ChatWait, second_wait.id).status == "cancelled"


def _expire_project_pair(db, project_id: str, chat_id: str) -> None:
  expired_at = now_naive_utc() - SOFT_DELETE_TTL - timedelta(seconds=1)
  db.get(models.Project, project_id).deleted_at = expired_at
  db.get(models.Chat, chat_id).deleted_at = expired_at
  db.commit()


def test_project_retention_removes_native_root_and_chat_but_preserves_legacy_root(
  client, auth, db,
):
  native = client.post(
    "/api/projects", headers=auth,
    json={"name": "Native", "template_id": "blank"},
  ).json()
  native_row = db.get(models.Project, native["id"])
  native_root = Path(os.environ["DATA_DIR"]) / native_row.root_path
  native_chat = _create_project_chat(client, auth, native, "Native chat")

  legacy_root = Path(os.environ["DATA_DIR"]) / "apps" / "legacy" / "files"
  legacy_root.mkdir(parents=True)
  (legacy_root / "keep.txt").write_text("legacy")
  legacy_chat = models.Chat(id="expired-legacy-chat", title="Legacy", messages=[])
  legacy_project = models.Project(
    id="75e94b57-fe5d-4bd7-a6e0-e74130566f37",
    name="Imported legacy",
    project_type="webstudio:web-app",
    root_path="apps/legacy/files",
    chat_id=None,
    template_snapshot_json={},
    legacy_source_json={"app_id": 1, "project_id": "default"},
  )
  legacy_project_id = legacy_project.id
  legacy_chat_id = legacy_chat.id
  legacy_chat.project_id = legacy_project_id
  db.add_all([legacy_chat, legacy_project])
  db.commit()
  _expire_project_pair(db, native["id"], native_chat["id"])
  _expire_project_pair(db, legacy_project_id, legacy_chat_id)

  purged_chats = purge_expired_chat_tombstones(db)

  assert db.get(models.Project, native["id"]) is None
  assert db.get(models.Project, legacy_project_id) is None
  assert db.get(models.Chat, native_chat["id"]) is None
  assert db.get(models.Chat, legacy_chat_id) is None
  assert native_chat["id"] in purged_chats
  assert legacy_chat_id in purged_chats
  assert not native_root.exists()
  assert (legacy_root / "keep.txt").read_text() == "legacy"


def test_project_retention_does_not_touch_root_when_database_commit_fails(
  client, auth, db, monkeypatch,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Still recoverable", "template_id": "blank"},
  ).json()
  row = db.get(models.Project, project["id"])
  root = Path(os.environ["DATA_DIR"]) / row.root_path
  chat = _create_project_chat(client, auth, project, "Still recoverable chat")
  _expire_project_pair(db, project["id"], chat["id"])

  real_commit = db.commit
  monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("commit failed")))
  with pytest.raises(RuntimeError, match="commit failed"):
    purge_expired_project_tombstones(db)
  assert root.is_dir()
  db.rollback()
  monkeypatch.setattr(db, "commit", real_commit)
  db.expire_all()
  assert db.get(models.Project, project["id"]) is not None
  assert db.get(models.Chat, chat["id"]) is not None


def test_project_retention_retries_native_orphan_after_filesystem_failure(
  client, auth, db, monkeypatch,
):
  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Retry cleanup", "template_id": "blank"},
  ).json()
  row = db.get(models.Project, project["id"])
  root = Path(os.environ["DATA_DIR"]) / row.root_path
  chat = _create_project_chat(client, auth, project, "Retry cleanup chat")
  _expire_project_pair(db, project["id"], chat["id"])

  import app.project_retention as retention
  real_remove = retention._remove_owned_root
  monkeypatch.setattr(
    retention, "_remove_owned_root",
    lambda _root: (_ for _ in ()).throw(OSError("filesystem busy")),
  )
  assert purge_expired_project_tombstones(db) == [project["id"]]
  assert db.get(models.Project, project["id"]) is None
  assert root.is_dir()

  monkeypatch.setattr(retention, "_remove_owned_root", real_remove)
  assert purge_expired_project_tombstones(db) == []
  assert not root.exists()


@pytest.fixture
def standalone_native_app(db):
  root = Path(os.environ["DATA_DIR"]) / "apps" / "linked-native"
  root.mkdir(parents=True)
  (root / "index.jsx").write_text("export default function App(){return null}")
  (root / "mobius.json").write_text(json.dumps({"entry": "index.jsx", "name": "Native"}))
  app = models.App(name="Native", description="", jsx_source="", slug="linked-native", source_dir=str(root), system_app=False)
  db.add(app)
  db.commit()
  db.refresh(app)
  return app, root


def test_repeated_concurrent_import_has_one_source_owner(client, auth, db, standalone_native_app):
  import asyncio
  import httpx
  from app.main import app as application
  source_app, root = standalone_native_app

  async def race():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application), base_url="http://test") as ac:
      return await asyncio.gather(*[
        ac.post("/api/projects/import", headers=auth, json={
          "kind": "app", "source_id": str(source_app.id), "recovery_request_id": f"click-{i}",
        }) for i in range(3)
      ])
  responses = asyncio.run(race())
  assert all(r.status_code == 200 for r in responses)
  assert len({r.json()["id"] for r in responses}) == 1
  assert db.query(models.Project).count() == 1
  assert root.is_dir()


def test_failed_link_commit_never_removes_existing_source(client, auth, monkeypatch, standalone_native_app):
  from sqlalchemy.orm import Session
  source_app, root = standalone_native_app
  original = (root / "index.jsx").read_bytes()
  commit = Session.commit

  def fail_project_commit(session):
    if any(isinstance(row, models.Project) for row in session.new):
      raise RuntimeError("simulated commit failure")
    return commit(session)
  monkeypatch.setattr(Session, "commit", fail_project_commit)
  with pytest.raises(RuntimeError, match="simulated commit failure"):
    client.post("/api/projects/import", headers=auth, json={"kind": "app", "source_id": str(source_app.id)})
  assert (root / "index.jsx").read_bytes() == original
  assert (root / "mobius.json").is_file()


def test_unavailable_page_catalog_does_not_hide_native_apps(client, auth, db, standalone_native_app):
  source_app, root = standalone_native_app
  catalog = models.App(name="Pages", description="", jsx_source="", slug="pages", source_dir=str(root.parent / "pages"), system_app=True)
  db.add(catalog)
  db.commit()
  listed = client.get("/api/projects/import-sources", headers=auth)
  assert listed.status_code == 200
  assert listed.json()["artifacts"] == []
  assert [row["id"] for row in listed.json()["apps"]] == [str(source_app.id)]
