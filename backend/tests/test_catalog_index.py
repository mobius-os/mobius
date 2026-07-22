"""The agent-facing catalog index (`app.catalog_index`) and its refresh route.

Network is never touched: `install._http_get` is monkeypatched with canned
bytes, matching test_skills_platform's approach.
"""

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from app import catalog_index as ci
from app.config import get_settings


@pytest.fixture
def skills_dir():
  import shutil

  root = Path(get_settings().data_dir) / "shared" / "skills"
  shutil.rmtree(root, ignore_errors=True)
  root.mkdir(parents=True)
  yield root
  shutil.rmtree(root, ignore_errors=True)


def _fake_fetch(files):
  async def fake(client, url, max_bytes, _hops=0):
    if url in files:
      return files[url]
    raise AssertionError(f"unexpected fetch: {url}")

  return fake


# ------------------------------------------------------------- pure helpers


def test_describe_skill_md_prefers_frontmatter_description():
  raw = "---\nname: pdf\ndescription: Fill PDFs.\n---\n\nBody paragraph.\n"
  assert ci.describe_skill_md(raw, "pdf") == "Fill PDFs."


def test_describe_skill_md_block_scalar_falls_back_to_body():
  raw = "---\nname: pdf\ndescription: >\n  folded\n---\n\nFirst paragraph wins.\n"
  assert ci.describe_skill_md(raw, "pdf") == "First paragraph wins."


def test_describe_skill_md_empty_body_yields_placeholder():
  assert "(no description)" in ci.describe_skill_md("", "mystery")


def test_describe_skill_md_squashes_and_truncates_to_one_line():
  raw = "---\ndescription: " + "very long words " * 30 + "\n---\nBody.\n"
  line = ci.describe_skill_md(raw, "x")
  assert "\n" not in line
  assert len(line) <= ci._DESC_MAX_CHARS


def test_build_index_groups_sources_and_carries_coordinates():
  src = {"label": "Anthropic Skills", "repo": "anthropics/skills", "path": "skills", "ref": "main"}
  bad = {"label": "Broken", "repo": "o/broken", "path": "", "ref": "main"}
  text = ci.build_index(
    [
      {"source": src, "skills": [
        {"name": "pdf", "dir": "skills/pdf", "description": "Fill PDFs."},
      ], "error": None},
      {"source": bad, "skills": [], "error": "boom"},
    ],
    "2026-07-22T00:00:00Z",
  )
  assert "## Anthropic Skills (anthropics/skills/skills)" in text
  assert "- pdf — Fill PDFs. (anthropics/skills skills/pdf @main)" in text
  assert "_Scan failed: boom_" in text
  assert "Grep this file first" in text


def test_is_fresh_gate(tmp_path):
  missing = tmp_path / "nope.md"
  assert ci.is_fresh(missing) is False
  f = tmp_path / "catalog-index.md"
  f.write_text("x")
  assert ci.is_fresh(f) is True
  old = time.time() - ci.FRESH_SECONDS - 60
  os.utime(f, (old, old))
  assert ci.is_fresh(f) is False


# ------------------------------------------------------------------ refresh


_SRC = {"label": "Test Source", "repo": "o/r", "path": "skills", "ref": "main"}


def _canned(monkeypatch):
  tree = {"tree": [
    {"path": "pdf/SKILL.md", "type": "blob"},
    {"path": "pdf/scripts/fill.py", "type": "blob"},
    {"path": "notes/SKILL.md", "type": "blob"},
  ]}
  monkeypatch.setattr(ci.install, "_http_get", _fake_fetch({
    "https://api.github.com/repos/o/r/git/trees/main%3Askills?recursive=1":
      json.dumps(tree).encode(),
    "https://raw.githubusercontent.com/o/r/main/skills/pdf/SKILL.md":
      b"---\ndescription: Fill PDFs.\n---\nBody.\n",
    "https://raw.githubusercontent.com/o/r/main/skills/notes/SKILL.md":
      b"# Notes\n\nTake better notes.\n",
  }))


def test_refresh_writes_index_and_gates_on_freshness(skills_dir, monkeypatch):
  _canned(monkeypatch)
  out = asyncio.run(ci.refresh(sources=[_SRC]))
  assert out["refreshed"] is True
  assert out["skills"] == 2
  text = (skills_dir / ci.INDEX_FILENAME).read_text()
  assert "- pdf — Fill PDFs. (o/r skills/pdf @main)" in text
  assert "- notes — Take better notes. (o/r skills/notes @main)" in text

  # Fresh file → gate skips the scan entirely (fetch mock would raise on use).
  monkeypatch.setattr(ci.install, "_http_get", _fake_fetch({}))
  again = asyncio.run(ci.refresh(sources=[_SRC]))
  assert again["refreshed"] is False

  # force bypasses the gate.
  _canned(monkeypatch)
  forced = asyncio.run(ci.refresh(force=True, sources=[_SRC]))
  assert forced["refreshed"] is True


def test_refresh_survives_a_failing_source(skills_dir, monkeypatch):
  async def explode(client, url, max_bytes, _hops=0):
    raise RuntimeError("github down")

  monkeypatch.setattr(ci.install, "_http_get", explode)
  out = asyncio.run(ci.refresh(force=True, sources=[_SRC]))
  assert out["refreshed"] is True
  assert out["skills"] == 0
  assert "_Scan failed:" in (skills_dir / ci.INDEX_FILENAME).read_text()


def test_refresh_description_fetch_failure_degrades_to_placeholder(
  skills_dir, monkeypatch,
):
  tree = {"tree": [{"path": "pdf/SKILL.md", "type": "blob"}]}
  files = {
    "https://api.github.com/repos/o/r/git/trees/main%3Askills?recursive=1":
      json.dumps(tree).encode(),
  }

  async def fake(client, url, max_bytes, _hops=0):
    if url in files:
      return files[url]
    raise RuntimeError("raw fetch failed")

  monkeypatch.setattr(ci.install, "_http_get", fake)
  out = asyncio.run(ci.refresh(force=True, sources=[_SRC]))
  assert out["skills"] == 1
  assert "- pdf — (description unavailable)" in (
    skills_dir / ci.INDEX_FILENAME
  ).read_text()


# -------------------------------------------------------------------- route


def test_refresh_route_requires_auth(client):
  r = client.post("/api/skills/catalog-index/refresh")
  assert r.status_code in (401, 403)


def test_refresh_route_calls_refresh_with_override(
  client, auth, db, skills_dir, monkeypatch,
):
  from app import models
  from app.routes import skills as rs

  app_row = models.App(
    name="Skills", description="", jsx_source="x", compiled_path="", slug="skills",
  )
  db.add(app_row)
  db.commit()
  db.refresh(app_row)
  storage = Path(get_settings().data_dir) / "apps" / str(app_row.id)
  storage.mkdir(parents=True, exist_ok=True)
  (storage / "sources.json").write_text(json.dumps([_SRC]))

  seen = {}

  async def fake_refresh(force=False, sources=None):
    seen["force"] = force
    seen["sources"] = sources
    return {"refreshed": True, "skills": 0, "generated_at": "t", "path": "p"}

  monkeypatch.setattr(rs.catalog_index, "refresh", fake_refresh)
  r = client.post(
    "/api/skills/catalog-index/refresh", headers=auth, json={"force": True},
  )
  assert r.status_code == 200, r.text
  assert r.json()["refreshed"] is True
  assert seen["force"] is True
  assert seen["sources"] == [_SRC]


def test_sources_override_absent_or_malformed_is_none(db, monkeypatch):
  from app import models
  from app.routes import skills as rs

  # No skills app row at all.
  assert rs._catalog_sources_override(db) is None

  app_row = models.App(
    name="Skills", description="", jsx_source="x", compiled_path="", slug="skills",
  )
  db.add(app_row)
  db.commit()
  db.refresh(app_row)
  storage = Path(get_settings().data_dir) / "apps" / str(app_row.id)
  storage.mkdir(parents=True, exist_ok=True)
  (storage / "sources.json").write_text("{not json")
  assert rs._catalog_sources_override(db) is None
