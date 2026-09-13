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
def _reset_live_builds(monkeypatch):
  project_builders.reset_for_tests()
  # Exercise the generic registry/lifecycle with a synthetic declaration.
  # Production website/PDF declarations now come only from provider apps.
  monkeypatch.setitem(project_builders.BUILTIN_ARTIFACT_TYPES, "website", {
    "id": "website", "name": "Test output", "extensions": ["html"],
    "preview": "html", "script": None, "output": "{source}",
  })
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
  (source / "project-builder.sh").write_text("#!/bin/bash\n")
  app = models.App(
    name="LaTeX", description="Documents", jsx_source="",
    slug="latex", source_dir=str(source), version="3.0.0",
    project_templates_json=[{
      "id": "latex",
      "name": "LaTeX document",
      "previews": [{
        "id": "pdf", "name": "PDF", "source": "main.tex", "builder": "latex",
      }],
      "files": {"main.tex": "templates/main.tex"},
      "artifact_types": [{
        "id": "latex", "name": "PDF", "extensions": ["tex"],
        "preview": "pdf", "script": "project-builder.sh",
        "output": "{stem}.pdf",
      }],
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
