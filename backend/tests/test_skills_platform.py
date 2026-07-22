"""Skills platform layer: enumeration + frontmatter + generated index
(`app.skills`) and the /api/skills routes (list, install-from-online,
uninstall, manage_skills gating).

Network is never touched: the install fetch path is exercised by
monkeypatching `install._http_get` / `_github_contents` with canned bytes,
the same spirit as test_apps_install's mocked AsyncClient.
"""

import json
from pathlib import Path

import pytest

from app import skills as skills_mod
from app.config import get_settings


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def skills_dir():
  """A clean shared/skills tree under the suite's DATA_DIR."""
  import shutil

  root = Path(get_settings().data_dir) / "shared" / "skills"
  shutil.rmtree(root, ignore_errors=True)
  root.mkdir(parents=True)
  yield root
  shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def seed_names(monkeypatch, tmp_path):
  """Points the seed-name resolver at a controlled seed tree."""
  seed = tmp_path / "seed-skills"
  seed.mkdir()
  (seed / "cron.md").write_text("# cron seed\n")
  monkeypatch.setattr(skills_mod, "_SEED_CANDIDATES", (seed,))
  return {"cron"}


def _app_token(db, *, manage_skills):
  """A registered app + an app-scoped JWT with/without manage_skills."""
  from app import models
  from app.auth import create_access_token

  app_row = models.App(
    name="Skills",
    description="skills app",
    jsx_source="export default () => null",
    compiled_path="",
    manage_skills=manage_skills,
  )
  db.add(app_row)
  db.commit()
  db.refresh(app_row)
  token = create_access_token(
    {"sub": "test", "scope": "app", "app_id": app_row.id},
  )
  return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------- enumeration + frontmatter


def test_dir_skill_frontmatter_parsed(skills_dir):
  d = skills_dir / "pdf-tools"
  d.mkdir()
  (d / "SKILL.md").write_text(
    "---\nname: PDF Tools\ndescription: Extract and merge PDFs.\n"
    "license: MIT\n---\n# body\n",
  )
  (found,) = skills_mod.enumerate_skills(skills_dir)
  assert found.name == "PDF Tools"
  assert found.description == "Extract and merge PDFs."
  assert found.is_dir is True
  assert found.metadata.get("license") == "MIT"
  assert found.read_path == d / "SKILL.md"


def test_flat_skill_falls_back_to_stem_and_first_paragraph(skills_dir):
  (skills_dir / "cron.md").write_text(
    "# Scheduled tasks\n\nHow to create recurring\njobs that survive.\n\n"
    "A second paragraph that must not leak in.\n",
  )
  (found,) = skills_mod.enumerate_skills(skills_dir)
  assert found.name == "cron"
  # Wrapped lines of the first paragraph join; later paragraphs don't leak.
  assert found.description == "How to create recurring jobs that survive."
  assert found.is_dir is False


def test_unclosed_frontmatter_reads_as_body(skills_dir):
  (skills_dir / "odd.md").write_text("---\nname: never closed\nbody text\n")
  (found,) = skills_mod.enumerate_skills(skills_dir)
  # No closing fence: the whole file is body, so the name falls back to the
  # stem and no frontmatter scalars leak into metadata.
  assert found.name == "odd"
  assert "name" not in found.metadata


def test_dir_without_skill_md_is_not_a_skill(skills_dir):
  (skills_dir / "not-a-skill").mkdir()
  (skills_dir / "not-a-skill" / "notes.md").write_text("x")
  assert skills_mod.enumerate_skills(skills_dir) == []


def test_provenance_labels(skills_dir, seed_names):
  # seed: matches the controlled seed tree by stem.
  (skills_dir / "cron.md").write_text("# cron\n\nseeded copy\n")
  # agent: present on disk, in no sidecar and not a seed name.
  (skills_dir / "mine.md").write_text("# mine\n\nhand written\n")
  # app-owned flat skill, keyed `<name>.md` with slug.
  (skills_dir / "contributing.md").write_text("# contrib\n\nfrom app\n")
  (skills_dir / skills_mod.APP_SKILLS_SIDECAR).write_text(json.dumps({
    "contributing.md": {"app_id": 7, "slug": "contribute", "active": True},
  }))
  # installed dir skill, keyed bare `<name>` with source.
  d = skills_dir / "pdf"
  d.mkdir()
  (d / "SKILL.md").write_text("---\nname: pdf\ndescription: d\n---\n")
  (skills_dir / skills_mod.INSTALLED_SKILLS_SIDECAR).write_text(json.dumps({
    "pdf": {"source": "anthropics/skills"},
  }))

  by_name = {
    s.read_path.parent.name if s.is_dir else s.read_path.stem: s.provenance
    for s in skills_mod.enumerate_skills(skills_dir)
  }
  assert by_name == {
    "cron": "seed",
    "mine": "agent",
    "contributing": "app:contribute",
    "pdf": "installed:anthropics/skills",
  }


def test_inactive_app_skill_is_not_listed(skills_dir):
  (skills_dir / "gone.md").write_text("# gone\n")
  (skills_dir / skills_mod.APP_SKILLS_SIDECAR).write_text(json.dumps({
    "gone.md": {"app_id": 7, "slug": "x", "active": False},
  }))
  (found,) = skills_mod.enumerate_skills(skills_dir)
  # The file is still on disk so it enumerates, but an inactive sidecar
  # record must not claim app provenance for it.
  assert found.provenance in ("agent", "seed")


# ------------------------------------------------------------------- index


def test_write_index_lists_all_and_never_lists_itself(skills_dir):
  (skills_dir / "a.md").write_text("# a\n\nAlpha skill.\n")
  d = skills_dir / "b"
  d.mkdir()
  (d / "SKILL.md").write_text("---\nname: b\ndescription: Beta skill.\n---\n")
  path = skills_mod.write_index(skills_dir)
  text = path.read_text()
  assert "shared/skills/a.md" in text
  assert "shared/skills/b/SKILL.md" in text
  assert skills_mod.INDEX_FILENAME not in [
    (s.read_path.parent.name if s.is_dir else s.read_path.name)
    for s in skills_mod.enumerate_skills(skills_dir)
  ]
  # Deterministic: a second write with unchanged inputs is byte-identical.
  assert path.read_text() == skills_mod.write_index(skills_dir).read_text()


def test_write_index_without_dir_is_noop(tmp_path):
  assert skills_mod.write_index(tmp_path / "missing") is None


# ------------------------------------------------------------------ routes


def _fake_fetch(files):
  """A fake install._http_get serving canned bytes by exact URL."""

  async def fake(client, url, max_bytes, _hops=0):
    if url in files:
      return files[url]
    raise AssertionError(f"unexpected fetch: {url}")

  return fake


def test_list_skills_route(client, auth, skills_dir):
  (skills_dir / "cron.md").write_text("# cron\n\nRecurring jobs.\n")
  r = client.get("/api/skills", headers=auth)
  assert r.status_code == 200
  (row,) = r.json()["skills"]
  assert row["id"] == "cron"
  assert row["description"] == "Recurring jobs."
  assert "uses_30d" in row


def test_install_from_raw_url(client, auth, skills_dir, monkeypatch):
  from app.routes import skills as rs

  url = "https://raw.githubusercontent.com/o/r/main/writing-tips.md"
  monkeypatch.setattr(
    rs.install, "_http_get",
    _fake_fetch({url: b"---\nname: writing-tips\ndescription: Tips.\n---\nBody"}),
  )
  r = client.post("/api/skills/install", headers=auth, json={"url": url})
  assert r.status_code == 201, r.text
  assert r.json()["name"] == "writing-tips"
  target = skills_dir / "writing-tips" / "SKILL.md"
  assert target.is_file()
  sidecar = json.loads(
    (skills_dir / skills_mod.INSTALLED_SKILLS_SIDECAR).read_text(),
  )
  assert sidecar["writing-tips"]["url"] == url
  # The generated index refreshed as part of the install.
  assert "writing-tips" in (skills_dir / skills_mod.INDEX_FILENAME).read_text()


def _dir_install_mocks(monkeypatch, rs, tree, raw_files):
  """Wire the dir-install flow: contents says "directory", trees lists it.

  `tree` entries are subtree-relative (the `<ref>:<path>` trees call); raw
  bytes are served from raw.githubusercontent.com URLs.
  """

  async def fake_contents(client_, repo, path, ref):
    return [{"type": "dir", "name": "marker"}]  # any list means "a directory"

  async def fake_tree(client_, repo, path, ref):
    return tree

  monkeypatch.setattr(rs, "_github_contents", fake_contents)
  monkeypatch.setattr(rs, "_github_tree", fake_tree)
  monkeypatch.setattr(rs.install, "_http_get", _fake_fetch(raw_files))


def test_install_repo_dir_fetches_whole_subtree_of_vetted_resources(
  client, auth, skills_dir, monkeypatch,
):
  from app.routes import skills as rs

  raw = "https://raw.githubusercontent.com/anthropics/skills/main/docs/pdf"
  tree = [
    {"type": "blob", "path": "SKILL.md", "size": 40},
    {"type": "blob", "path": "ref.md", "size": 9},
    {"type": "blob", "path": "scripts/helper.py", "size": 12},
    {"type": "tree", "path": "scripts"},
    {"type": "blob", "path": "logo.png", "size": 10},  # unvetted suffix
    {"type": "blob", "path": ".github/ci.yml", "size": 5},  # dot segment
    {"type": "blob", "path": "a/b/c/d/deep.md", "size": 5},  # over depth cap
  ]
  _dir_install_mocks(monkeypatch, rs, tree, {
    f"{raw}/SKILL.md": b"---\nname: pdf\ndescription: PDFs.\n---\n",
    f"{raw}/ref.md": b"reference",
    f"{raw}/scripts/helper.py": b"print('hi')\n",
  })
  r = client.post(
    "/api/skills/install", headers=auth,
    json={"repo": "anthropics/skills", "path": "docs/pdf", "ref": "main"},
  )
  assert r.status_code == 201, r.text
  assert sorted(r.json()["files"]) == ["SKILL.md", "ref.md", "scripts/helper.py"]
  # Subdirectory structure is preserved on disk.
  assert (skills_dir / "pdf" / "scripts" / "helper.py").read_text() == "print('hi')\n"
  assert (skills_dir / "pdf" / "ref.md").read_text() == "reference"
  # Unvetted suffix, dot segments, and over-depth paths were never fetched
  # (_fake_fetch would have raised) and never materialized.
  assert not (skills_dir / "pdf" / "logo.png").exists()
  assert not (skills_dir / "pdf" / ".github").exists()
  assert not (skills_dir / "pdf" / "a").exists()


def test_install_repo_dir_rejects_traversal_paths_in_tree(
  client, auth, skills_dir, monkeypatch,
):
  from app.routes import skills as rs

  raw = "https://raw.githubusercontent.com/o/r/main/sk"
  tree = [
    {"type": "blob", "path": "SKILL.md", "size": 10},
    {"type": "blob", "path": "../escape.md", "size": 5},
    {"type": "blob", "path": "ok/../../escape2.md", "size": 5},
  ]
  _dir_install_mocks(monkeypatch, rs, tree, {
    f"{raw}/SKILL.md": b"# sk\n",
  })
  r = client.post(
    "/api/skills/install", headers=auth,
    json={"repo": "o/r", "path": "sk", "ref": "main"},
  )
  assert r.status_code == 201, r.text
  assert r.json()["files"] == ["SKILL.md"]
  assert not (skills_dir / "escape.md").exists()
  assert not (skills_dir / "escape2.md").exists()


def test_install_repo_dir_skips_over_budget_files_by_declared_size(
  client, auth, skills_dir, monkeypatch,
):
  from app.routes import skills as rs

  raw = "https://raw.githubusercontent.com/o/r/main/sk"
  tree = [
    {"type": "blob", "path": "SKILL.md", "size": 10},
    {"type": "blob", "path": "huge.md", "size": rs._RESOURCE_TOTAL_MAX + 1},
    {"type": "blob", "path": "small.md", "size": 5},
  ]
  _dir_install_mocks(monkeypatch, rs, tree, {
    f"{raw}/SKILL.md": b"# sk\n",
    f"{raw}/small.md": b"small",
  })
  r = client.post(
    "/api/skills/install", headers=auth,
    json={"repo": "o/r", "path": "sk", "ref": "main"},
  )
  assert r.status_code == 201, r.text
  # huge.md was skipped WITHOUT being fetched; the smaller file still landed.
  assert sorted(r.json()["files"]) == ["SKILL.md", "small.md"]


def test_resource_rel_ok_contract():
  from app.routes.skills import _resource_rel_ok

  assert _resource_rel_ok("ref.md")
  assert _resource_rel_ok("scripts/run.py")
  assert _resource_rel_ok("a/b/c/deep.md")  # depth 4 = at the cap
  assert not _resource_rel_ok("a/b/c/d/deep.md")  # depth 5
  assert not _resource_rel_ok("../up.md")
  assert not _resource_rel_ok(".hidden.md")
  assert not _resource_rel_ok("dir/.hidden.md")
  assert not _resource_rel_ok("dir//double.md")
  assert not _resource_rel_ok("win\\path.md")
  assert not _resource_rel_ok("binary.png")
  assert not _resource_rel_ok("")


def test_install_collision_is_409_with_provenance(
  client, auth, skills_dir, seed_names, monkeypatch,
):
  from app.routes import skills as rs

  (skills_dir / "cron.md").write_text("# cron\n")
  monkeypatch.setattr(
    rs.install, "_http_get", _fake_fetch({"https://x/cron.md": b"# other"}),
  )
  r = client.post(
    "/api/skills/install", headers=auth, json={"url": "https://x/cron.md"},
  )
  assert r.status_code == 409, r.text
  assert "seed" in r.json()["detail"]
  # Nothing was written.
  assert not (skills_dir / "cron").exists()


def test_install_underivable_name_is_400(client, auth, skills_dir, monkeypatch):
  from app.routes import skills as rs

  monkeypatch.setattr(
    rs.install, "_http_get", _fake_fetch({"https://x/___.md": b"# x"}),
  )
  r = client.post(
    "/api/skills/install", headers=auth, json={"url": "https://x/___.md"},
  )
  assert r.status_code == 400
  assert "name" in r.json()["detail"].lower()


def test_install_requires_manage_skills_permission(
  client, db, owner_token, skills_dir, monkeypatch,
):
  from app.routes import skills as rs

  monkeypatch.setattr(
    rs.install, "_http_get", _fake_fetch({"https://x/tips.md": b"# tips"}),
  )
  denied = _app_token(db, manage_skills=False)
  r = client.post(
    "/api/skills/install", headers=denied, json={"url": "https://x/tips.md"},
  )
  assert r.status_code == 403
  assert "manage_skills" in r.json()["detail"]

  granted = _app_token(db, manage_skills=True)
  r = client.post(
    "/api/skills/install", headers=granted, json={"url": "https://x/tips.md"},
  )
  assert r.status_code == 201, r.text


def test_uninstall_only_removes_installed_provenance(client, auth, skills_dir):
  (skills_dir / "cron.md").write_text("# cron\n")
  r = client.delete("/api/skills/cron", headers=auth)
  assert r.status_code == 409
  assert (skills_dir / "cron.md").exists()


def test_uninstall_removes_dir_and_sidecar_record(client, auth, skills_dir):
  d = skills_dir / "tips"
  d.mkdir()
  (d / "SKILL.md").write_text("---\nname: tips\ndescription: t\n---\n")
  (skills_dir / skills_mod.INSTALLED_SKILLS_SIDECAR).write_text(
    json.dumps({"tips": {"source": "o/r"}}),
  )
  r = client.delete("/api/skills/tips", headers=auth)
  assert r.status_code == 200, r.text
  assert not d.exists()
  sidecar = json.loads(
    (skills_dir / skills_mod.INSTALLED_SKILLS_SIDECAR).read_text(),
  )
  assert "tips" not in sidecar


def test_uninstall_rejects_traversal_name(client, auth, skills_dir):
  r = client.delete("/api/skills/..%2Fetc", headers=auth)
  assert r.status_code in (400, 404, 409)
