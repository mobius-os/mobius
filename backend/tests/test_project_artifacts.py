"""Project artifact registry, build lifecycle, and confined output serving.

Build lifecycle tests drive ``project_builders.run_build`` directly via
``asyncio.run`` rather than the POST endpoint's background task, so the build
runs deterministically to completion; the endpoint's scheduling is covered
separately with a stubbed task. tectonic is always stubbed — no binary, no
network in CI.
"""

import asyncio
import os
from pathlib import Path

import pytest
from sqlalchemy.orm.attributes import flag_modified

from app import models, project_builders


class _FakeTask:
  """Stand-in for a live build task in the registry (never finishes)."""

  def done(self) -> bool:
    return False


@pytest.fixture(autouse=True)
def _reset_live_builds():
  project_builders.reset_for_tests()
  yield
  project_builders.reset_for_tests()


def _make_project(client, auth, name="Site"):
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


def _artifact(client, auth, project_id, artifact_id):
  listed = client.get(
    f"/api/projects/{project_id}/artifacts", headers=auth,
  ).json()["artifacts"]
  return next(a for a in listed if a["id"] == artifact_id)


def test_provider_script_env_exposes_only_runtime_and_project_values(
  monkeypatch, tmp_path,
):
  monkeypatch.setenv("PATH", "/test/bin")
  monkeypatch.setenv("HOME", "/test/home")
  monkeypatch.setenv("AGENT_TOKEN", "must-not-leak")
  monkeypatch.setenv("GH_TOKEN", "must-not-leak-either")
  root = tmp_path / "project"
  output = root / "artifacts" / "deck" / "output"

  env = project_builders._provider_script_env(
    root=root, source="slides.deck", output_dir=output, artifact_id="deck",
  )

  assert env["PATH"] == "/test/bin"
  assert env["HOME"] == "/test/home"
  assert env["PROJECT_ROOT"] == str(root)
  assert env["PROJECT_SOURCE"] == "slides.deck"
  assert env["PROJECT_OUTPUT_DIR"] == str(output)
  assert env["PROJECT_ARTIFACT_ID"] == "deck"
  assert "AGENT_TOKEN" not in env
  assert "GH_TOKEN" not in env


def test_artifact_crud_validates_and_confines(client, auth):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "index.html", "<h1>Hi</h1>")

  created = client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Website", "builder": "website", "source": "index.html"},
  )
  assert created.status_code == 201, created.text
  art = created.json()
  assert art["id"] == "website"
  assert art["builder"] == "website"
  assert art["status"] == "idle"
  assert art["has_output"] is False
  assert art["source_missing"] is False
  assert art["output_rel"] == "artifacts/website/output/index.html"

  duplicate = client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Website", "builder": "website", "source": "index.html"},
  )
  assert duplicate.status_code == 409

  unknown_builder = client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Nope", "builder": "make", "source": "index.html"},
  )
  assert unknown_builder.status_code == 422

  missing_source = client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Ghost", "builder": "website", "source": "ghost.html"},
  )
  assert missing_source.status_code == 422

  listed = client.get(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
  ).json()["artifacts"]
  assert [a["id"] for a in listed] == ["website"]

  deleted = client.delete(
    f"/api/projects/{project['id']}/artifacts/website", headers=auth,
  )
  assert deleted.status_code == 204
  assert client.get(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
  ).json()["artifacts"] == []
  assert client.delete(
    f"/api/projects/{project['id']}/artifacts/website", headers=auth,
  ).status_code == 404


def test_creation_open_recency_is_navigation_state(client, auth, db):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "index.html", "<h1>Hi</h1>")
  client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Website", "builder": "website", "source": "index.html"},
  )
  row = db.get(models.Project, project["id"])
  original_updated_at = row.updated_at

  opened = client.post(
    f"/api/projects/{project['id']}/artifacts/website/opened", headers=auth,
  )

  assert opened.status_code == 204, opened.text
  artifact = _artifact(client, auth, project["id"], "website")
  assert artifact["last_opened_at"] is not None
  db.refresh(row)
  assert row.updated_at == original_updated_at
  assert client.post(
    f"/api/projects/{project['id']}/artifacts/missing/opened", headers=auth,
  ).status_code == 404


def test_deleted_creation_does_not_transfer_recency_to_a_reused_id(
  client, auth, db,
):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "index.html", "<h1>Hi</h1>")
  artifact_url = f"/api/projects/{project['id']}/artifacts"
  body = {
    "name": "Website",
    "builder": "website",
    "source": "index.html",
  }
  assert client.post(artifact_url, headers=auth, json=body).status_code == 201
  assert client.post(
    f"{artifact_url}/website/opened", headers=auth,
  ).status_code == 204
  assert _artifact(client, auth, project["id"], "website")[
    "last_opened_at"
  ] is not None

  assert client.delete(
    f"{artifact_url}/website", headers=auth,
  ).status_code == 204
  assert db.get(
    models.ProjectArtifactDrawerState,
    {"project_id": project["id"], "artifact_id": "website"},
  ) is None

  assert client.post(artifact_url, headers=auth, json=body).status_code == 201
  recreated = _artifact(client, auth, project["id"], "website")
  assert recreated["last_opened_at"] is None


def test_website_build_copies_tree_and_serves_entry_with_csp(client, auth):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "index.html", "<h1>Hello site</h1>")
  _write_file(client, auth, project, "assets/app.css", "body{color:red}")
  client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Website", "builder": "website", "source": "index.html"},
  )

  asyncio.run(project_builders.run_build(project["id"], "website"))

  art = _artifact(client, auth, project["id"], "website")
  assert art["status"] == "ok"
  assert art["has_output"] is True
  assert art["duration_ms"] is not None

  entry = client.get(
    f"/api/projects/{project['id']}/artifacts/website/output/index.html",
    headers=auth,
  )
  assert entry.status_code == 200
  assert "Hello site" in entry.text
  # The website-entry CSP is exactly the spec's isolation policy — applied by
  # the authoritative security middleware for this output namespace, not the
  # shell CSP.
  csp = entry.headers.get("content-security-policy", "")
  assert csp == (
    "default-src 'self'; img-src 'self' data:; font-src 'self' data:; "
    "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
    "frame-ancestors 'self'"
  )

  asset = client.get(
    f"/api/projects/{project['id']}/artifacts/website/output/assets/app.css",
    headers=auth,
  )
  assert asset.status_code == 200
  assert "color:red" in asset.text
  # Sibling output files share the isolating namespace CSP, never the shell one.
  assert "frame-ancestors 'self'" in asset.headers.get(
    "content-security-policy", "",
  )

  log = client.get(
    f"/api/projects/{project['id']}/artifacts/website/log", headers=auth,
  ).json()
  assert "Copied" in log["log"]
  assert log["truncated"] is False


def test_website_build_never_dereferences_nested_symlinks(
  client, auth, db, tmp_path,
):
  project = _make_project(client, auth)
  row = db.query(models.Project).filter(models.Project.id == project["id"]).one()
  root = Path(os.environ["DATA_DIR"]) / row.root_path
  (root / "index.html").write_text("<h1>safe</h1>", encoding="utf-8")
  (root / "nested").mkdir()
  outside_file = tmp_path / "private.txt"
  outside_file.write_text("must-not-copy", encoding="utf-8")
  outside_dir = tmp_path / "private-dir"
  outside_dir.mkdir()
  (outside_dir / "secret.txt").write_text("also-private", encoding="utf-8")
  (root / "nested" / "file-link").symlink_to(outside_file)
  (root / "nested" / "dir-link").symlink_to(
    outside_dir, target_is_directory=True,
  )
  client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Website", "builder": "website", "source": "index.html"},
  )

  asyncio.run(project_builders.run_build(project["id"], "website"))

  output = root / "artifacts" / "website" / "output" / "nested"
  assert not (output / "file-link").exists()
  assert not (output / "file-link").is_symlink()
  assert not (output / "dir-link").exists()
  assert not (output / "dir-link").is_symlink()


def test_website_build_excludes_git_directories_and_gitfiles(client, auth, db):
  project = _make_project(client, auth)
  row = db.query(models.Project).filter(models.Project.id == project["id"]).one()
  root = Path(os.environ["DATA_DIR"]) / row.root_path
  (root / "index.html").write_text("<h1>safe</h1>", encoding="utf-8")
  (root / ".git" / "hooks").mkdir(parents=True)
  (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")
  (root / "packages" / "child").mkdir(parents=True)
  (root / "packages" / "child" / ".git").write_text(
    "gitdir: ../../../.git/modules/child\n", encoding="utf-8",
  )
  (root / "packages" / "child" / "page.html").write_text(
    "<p>copied</p>", encoding="utf-8",
  )
  (root / "packages" / "vendor" / ".git").mkdir(parents=True)
  (root / "packages" / "vendor" / ".git" / "config").write_text(
    "[core]\n", encoding="utf-8",
  )
  client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Website", "builder": "website", "source": "index.html"},
  )

  asyncio.run(project_builders.run_build(project["id"], "website"))

  output = root / "artifacts" / "website" / "output"
  assert not (output / ".git").exists()
  assert not (output / "packages" / "child" / ".git").exists()
  assert not (output / "packages" / "vendor" / ".git").exists()
  assert (output / "packages" / "child" / "page.html").read_text(
    encoding="utf-8",
  ) == "<p>copied</p>"


def test_output_serving_is_confined_and_header_authed(client, auth, owner_token):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "index.html", "<h1>ok</h1>")
  client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Website", "builder": "website", "source": "index.html"},
  )
  asyncio.run(project_builders.run_build(project["id"], "website"))

  # The output route authenticates via the Authorization header only. The shell
  # fetches these bytes with the Bearer header (pdfjs for latex; the website is
  # fetched and inlined into a sandboxed srcDoc), so the owner token is never on
  # the URL where a sandboxed artifact's JS could read it.
  by_header = client.get(
    f"/api/projects/{project['id']}/artifacts/website/output/index.html",
    headers=auth,
  )
  assert by_header.status_code == 200
  assert "ok" in by_header.text

  # A ?token= query param must NOT authenticate — that is the URL-leak vector we
  # deliberately closed.
  by_query = client.get(
    f"/api/projects/{project['id']}/artifacts/website/output/index.html"
    f"?token={owner_token}",
  )
  assert by_query.status_code == 401

  unauth = client.get(
    f"/api/projects/{project['id']}/artifacts/website/output/index.html",
  )
  assert unauth.status_code == 401

  traversal = client.get(
    f"/api/projects/{project['id']}/artifacts/website/output/../../../secret",
    headers=auth,
  )
  assert traversal.status_code in (400, 404)

  missing = client.get(
    f"/api/projects/{project['id']}/artifacts/website/output/nope.html",
    headers=auth,
  )
  assert missing.status_code == 404


def test_latex_build_success_with_stubbed_tectonic(
  client, auth, monkeypatch, tmp_path,
):
  project = _make_project(client, auth, "Paper")
  _write_file(client, auth, project, "main.tex", "\\documentclass{article}")
  client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "PDF", "builder": "latex", "source": "main.tex"},
  )
  cache = tmp_path / "tectonic-cache"
  monkeypatch.setattr(project_builders, "TECTONIC_CACHE_DIR", str(cache))

  async def fake_tectonic(*, source, output_dir, cwd, env, log_path):
    assert source == "main.tex"
    # The cache dir is pinned into the environment before the subprocess runs.
    assert env["TECTONIC_CACHE_DIR"] == str(cache)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    (Path(output_dir) / "main.pdf").write_bytes(b"%PDF-1.5 fake pdf")
    Path(log_path).write_text("tectonic finished\n", encoding="utf-8")
    return 0

  monkeypatch.setattr(project_builders, "_run_tectonic", fake_tectonic)
  asyncio.run(project_builders.run_build(project["id"], "pdf"))

  art = _artifact(client, auth, project["id"], "pdf")
  assert art["status"] == "ok"
  assert art["has_output"] is True
  assert art["output_rel"] == "artifacts/pdf/output/main.pdf"
  assert cache.is_dir()

  pdf = client.get(
    f"/api/projects/{project['id']}/artifacts/pdf/output/main.pdf", headers=auth,
  )
  assert pdf.status_code == 200
  assert pdf.content.startswith(b"%PDF")
  # pdfjs fetches the PDF via this route; the namespace CSP rides along and is
  # harmless to a fetch() consumed by the shell.
  assert "frame-ancestors 'self'" in pdf.headers.get(
    "content-security-policy", "",
  )


def test_latex_build_failure_records_error(client, auth, monkeypatch, tmp_path):
  project = _make_project(client, auth, "Broken paper")
  _write_file(client, auth, project, "main.tex", "\\documentclass{article}")
  client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "PDF", "builder": "latex", "source": "main.tex"},
  )
  monkeypatch.setattr(
    project_builders, "TECTONIC_CACHE_DIR", str(tmp_path / "cache"),
  )

  async def failing_tectonic(*, source, output_dir, cwd, env, log_path):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    Path(log_path).write_text("! LaTeX Error: something\n", encoding="utf-8")
    return 1

  monkeypatch.setattr(project_builders, "_run_tectonic", failing_tectonic)
  asyncio.run(project_builders.run_build(project["id"], "pdf"))

  art = _artifact(client, auth, project["id"], "pdf")
  assert art["status"] == "error"
  assert art["has_output"] is False
  log = client.get(
    f"/api/projects/{project['id']}/artifacts/pdf/log", headers=auth,
  ).json()
  assert "LaTeX Error" in log["log"]


def test_build_missing_source_records_error_not_500(client, auth, db):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "index.html", "<h1>x</h1>")
  client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Website", "builder": "website", "source": "index.html"},
  )
  # The agent deletes the source out from under the artifact.
  client.delete(
    f"/api/projects/{project['id']}/file?path=index.html", headers=auth,
  )
  asyncio.run(project_builders.run_build(project["id"], "website"))
  art = _artifact(client, auth, project["id"], "website")
  assert art["status"] == "error"
  assert art["source_missing"] is True


def test_stale_building_reads_error_and_allows_rebuild(
  client, auth, db, monkeypatch,
):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "index.html", "<h1>x</h1>")
  client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Website", "builder": "website", "source": "index.html"},
  )
  # Simulate a crash mid-build: DB says building, but no live task exists.
  row = db.get(models.Project, project["id"])
  entries = list(row.artifacts_json)
  entries[0]["status"] = "building"
  row.artifacts_json = entries
  flag_modified(row, "artifacts_json")
  db.commit()

  reconciled = _artifact(client, auth, project["id"], "website")
  assert reconciled["status"] == "error"

  # A rebuild is allowed (never 409) and reports building once scheduled.
  def fake_start(project_id, artifact_id):
    project_builders._LIVE[(project_id, artifact_id)] = _FakeTask()

  monkeypatch.setattr(project_builders, "start_build", fake_start)
  rebuild = client.post(
    f"/api/projects/{project['id']}/artifacts/website/build", headers=auth,
  )
  assert rebuild.status_code == 200
  assert rebuild.json()["status"] == "building"


def test_build_and_delete_conflict_with_a_live_task(client, auth):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "index.html", "<h1>x</h1>")
  client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Website", "builder": "website", "source": "index.html"},
  )
  project_builders._LIVE[(project["id"], "website")] = _FakeTask()

  assert client.post(
    f"/api/projects/{project['id']}/artifacts/website/build", headers=auth,
  ).status_code == 409
  assert client.delete(
    f"/api/projects/{project['id']}/artifacts/website", headers=auth,
  ).status_code == 409


def test_template_previews_auto_register_as_artifacts(client, auth, db):
  source = Path(os.environ["DATA_DIR"]) / "apps" / "latex"
  (source / "templates").mkdir(parents=True)
  (source / "templates" / "main.tex").write_text("\\documentclass{article}")
  app = models.App(
    name="LaTeX", description="Documents", jsx_source="",
    slug="latex", source_dir=str(source), version="3.0.0",
    project_templates_json=[{
      "id": "latex",
      "name": "LaTeX document",
      "previews": [{
        "id": "pdf", "name": "PDF", "kind": "pdf", "path": "main.pdf",
      }],
      "files": {"main.tex": "templates/main.tex"},
    }],
  )
  db.add(app)
  db.commit()

  created = client.post(
    "/api/projects", headers=auth,
    json={"name": "Paper", "template_id": "latex:latex"},
  )
  assert created.status_code == 200, created.text
  project = created.json()
  artifacts = project["artifacts"]
  assert len(artifacts) == 1
  assert artifacts[0]["id"] == "pdf"
  assert artifacts[0]["builder"] == "latex"
  assert artifacts[0]["source"] == "main.tex"
  assert artifacts[0]["status"] == "idle"
  listed = client.get(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
  ).json()["artifacts"]
  assert [a["id"] for a in listed] == ["pdf"]


def test_app_contributed_builder_runs_reviewed_script_and_serves_output(
  client, auth, db,
):
  source = Path(os.environ["DATA_DIR"]) / "apps" / "presenter"
  (source / "templates").mkdir(parents=True)
  (source / "templates" / "main.deck").write_text("<h1>Project-owned</h1>")
  script = source / "project-builder.sh"
  script.write_text(
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    "mkdir -p \"$PROJECT_OUTPUT_DIR\"\n"
    "cp \"$PROJECT_ROOT/$PROJECT_SOURCE\" "
    "\"$PROJECT_OUTPUT_DIR/preview.html\"\n"
  )
  app = models.App(
    name="Presenter", description="Decks", jsx_source="",
    slug="presenter", source_dir=str(source), version="1.0.0",
    project_templates_json=[{
      "id": "deck", "name": "Deck", "files": {
        "main.deck": "templates/main.deck",
      },
      "artifact_types": [{
        "id": "presentation", "name": "Presentation",
        "extensions": ["deck"], "preview": "html",
        "script": "project-builder.sh", "output": "preview.html",
      }],
    }],
  )
  db.add(app)
  db.commit()

  project = client.post(
    "/api/projects", headers=auth,
    json={"name": "Launch", "template_id": "presenter:deck"},
  ).json()
  created = client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={
      "id": "deck", "name": "Launch deck", "builder": "presentation",
      "source": "main.deck",
    },
  )
  assert created.status_code == 201, created.text
  assert created.json()["type_name"] == "Presentation"
  assert created.json()["preview"] == "html"
  assert created.json()["output_rel"] == (
    "artifacts/deck/output/preview.html"
  )

  asyncio.run(project_builders.run_build(project["id"], "deck"))

  artifact = _artifact(client, auth, project["id"], "deck")
  assert artifact["status"] == "ok"
  assert artifact["has_output"] is True
  output = client.get(
    f"/api/projects/{project['id']}/artifacts/deck/output/preview.html",
    headers=auth,
  )
  assert output.status_code == 200, output.text
  assert output.text == "<h1>Project-owned</h1>"


def test_malformed_artifacts_json_never_500s(client, auth, db):
  project = _make_project(client, auth)
  row = db.get(models.Project, project["id"])
  # A non-list top-level value reads as "no artifacts".
  row.artifacts_json = {"not": "a list"}
  flag_modified(row, "artifacts_json")
  db.commit()
  empty = client.get(f"/api/projects/{project['id']}/artifacts", headers=auth)
  assert empty.status_code == 200
  assert empty.json()["artifacts"] == []

  # A list with junk entries keeps only entries carrying a valid id.
  row = db.get(models.Project, project["id"])
  row.artifacts_json = [
    "junk",
    {"no_id": True},
    {"id": "ok-one", "builder": "website", "source": "index.html"},
    {"id": "bad id with spaces", "builder": "website", "source": "index.html"},
  ]
  flag_modified(row, "artifacts_json")
  db.commit()
  artifacts = client.get(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
  ).json()["artifacts"]
  assert [a["id"] for a in artifacts] == ["ok-one"]
  assert artifacts[0]["source_missing"] is True


def test_artifacts_dir_is_hidden_from_the_root_finder_listing(client, auth):
  project = _make_project(client, auth)
  _write_file(client, auth, project, "index.html", "<h1>ok</h1>")
  client.post(
    f"/api/projects/{project['id']}/artifacts", headers=auth,
    json={"name": "Website", "builder": "website", "source": "index.html"},
  )
  asyncio.run(project_builders.run_build(project["id"], "website"))

  # The build created artifacts/ on disk; it must NOT show in the root finder
  # listing (it is surfaced in the Artifacts zone instead), while real source
  # files stay listed.
  root = client.get(f"/api/projects/{project['id']}/files", headers=auth).json()
  names = {e["name"] for e in root["entries"]}
  assert "artifacts" not in names
  assert "index.html" in names

  # It stays reachable on disk (listing inside it still works).
  inside = client.get(
    f"/api/projects/{project['id']}/files?path=artifacts", headers=auth,
  ).json()
  assert inside["entries"], "artifacts/ should still be browsable when navigated into"
