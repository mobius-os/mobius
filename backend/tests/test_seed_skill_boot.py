"""Platform skill baselines replace hand-maintained predecessor lists."""

import hashlib
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from app import skills as skills_mod


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "init_skills.py"


def _load():
  spec = importlib.util.spec_from_file_location("seed_boot_under_test", SCRIPT)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _git(repo, *args):
  return subprocess.run(
    ["git", "-C", str(repo), *args], capture_output=True, check=True,
    env={**os.environ, "GIT_AUTHOR_NAME": "Seed Test", "GIT_AUTHOR_EMAIL": "seed@example.test",
         "GIT_COMMITTER_NAME": "Seed Test", "GIT_COMMITTER_EMAIL": "seed@example.test"},
  ).stdout.decode().strip()


def _commit(repo, text=None, *, remove=False):
  path = repo / "backend" / "scripts" / "seed-skills" / "sample.md"
  path.parent.mkdir(parents=True, exist_ok=True)
  if remove:
    path.unlink()
  else:
    path.write_text(text, encoding="utf-8")
  _git(repo, "add", "-A")
  _git(repo, "commit", "-m", "seed revision")
  return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def boot(tmp_path, monkeypatch):
  module = _load()
  seed = tmp_path / "image" / "seed-skills"
  seed.mkdir(parents=True)
  skills = tmp_path / "data" / "shared" / "skills"
  archive = tmp_path / "data" / "shared" / "retired-skills"
  repo = tmp_path / "platform"
  repo.mkdir()
  _git(repo, "init", "-b", "main")
  monkeypatch.setattr(module, "_SEED_CANDIDATES", (seed,))
  monkeypatch.setattr(module, "SKILLS", skills)
  monkeypatch.setattr(module, "RETIRED_SKILLS", archive)
  monkeypatch.setattr(module, "PLATFORM_REPO", repo)
  monkeypatch.setattr(module, "_writable", lambda _path: None)
  monkeypatch.setattr(module, "_write_index", lambda: None)
  return module, seed, skills, archive, repo


def _records(skills):
  return json.loads((skills / ".seed-skills.json").read_text())


def test_fresh_seed_records_baseline_and_updates_clean_copy(boot):
  module, seed, skills, _, repo = boot
  (seed / "sample.md").write_text("v1")
  module.BUILD_SHA = _commit(repo, "v1")
  module.init()
  assert (skills / "sample.md").read_text() == "v1"
  assert _records(skills)["sample.md"]["baseline_sha256"] == hashlib.sha256(b"v1").hexdigest()

  (seed / "sample.md").write_text("v2")
  module.BUILD_SHA = _commit(repo, "v2")
  module.init()
  assert (skills / "sample.md").read_text() == "v2"
  assert _records(skills)["sample.md"]["status"] == "current"


def test_recorded_owner_edit_survives_upstream_change(boot):
  module, seed, skills, _, repo = boot
  module.BUILD_SHA = _commit(repo, "v1")
  (seed / "sample.md").write_text("v1")
  module.init()
  (skills / "sample.md").write_text("owner improvement")

  module.BUILD_SHA = _commit(repo, "v2")
  (seed / "sample.md").write_text("v2")
  module.init()

  assert (skills / "sample.md").read_text() == "owner improvement"
  record = _records(skills)["sample.md"]
  assert record["baseline_sha256"] == hashlib.sha256(b"v1").hexdigest()
  assert record["upstream_sha256"] == hashlib.sha256(b"v2").hexdigest()
  assert record["status"] == "needs_review"


def test_legacy_shipped_copy_is_recognized_from_git_history(boot):
  module, seed, skills, _, repo = boot
  _commit(repo, "v1")
  module.BUILD_SHA = _commit(repo, "v2")
  (seed / "sample.md").write_text("v2")
  skills.mkdir(parents=True)
  (skills / "sample.md").write_text("v1")

  module.init()

  assert (skills / "sample.md").read_text() == "v2"
  assert _records(skills)["sample.md"]["status"] == "current"


def test_exact_unsafe_legacy_copy_is_preserved_before_replacement(boot, monkeypatch):
  module, seed, skills, archive, repo = boot
  module.BUILD_SHA = _commit(repo, "safe guidance")
  (seed / "sample.md").write_text("safe guidance")
  skills.mkdir(parents=True)
  (skills / "sample.md").write_text("unsafe curated guidance")
  digest = hashlib.sha256(b"unsafe curated guidance").hexdigest()
  monkeypatch.setattr(module, "_UNSAFE_LEGACY_COPIES", {"sample.md": digest})

  module.init()

  assert (skills / "sample.md").read_text() == "safe guidance"
  assert (archive / f"sample-{digest}.md").read_text() == "unsafe curated guidance"
  assert _records(skills)["sample.md"]["status"] == "current"


def test_unsafe_legacy_copy_is_not_replaced_if_archive_fails(boot, monkeypatch):
  module, seed, skills, _, repo = boot
  module.BUILD_SHA = _commit(repo, "safe guidance")
  (seed / "sample.md").write_text("safe guidance")
  skills.mkdir(parents=True)
  (skills / "sample.md").write_text("unsafe curated guidance")
  digest = hashlib.sha256(b"unsafe curated guidance").hexdigest()
  monkeypatch.setattr(module, "_UNSAFE_LEGACY_COPIES", {"sample.md": digest})
  monkeypatch.setattr(module, "_archive", lambda _name, _content: False)

  module.init()

  assert (skills / "sample.md").read_text() == "unsafe curated guidance"
  assert not (skills / ".seed-skills.json").exists()


def test_exact_unsafe_copy_is_repaired_even_with_existing_baseline(boot, monkeypatch):
  module, seed, skills, archive, repo = boot
  module.BUILD_SHA = _commit(repo, "safe guidance")
  (seed / "sample.md").write_text("safe guidance")
  module.init()
  (skills / "sample.md").write_text("unsafe curated guidance")
  digest = hashlib.sha256(b"unsafe curated guidance").hexdigest()
  monkeypatch.setattr(module, "_UNSAFE_LEGACY_COPIES", {"sample.md": digest})

  module.init()

  assert (skills / "sample.md").read_text() == "safe guidance"
  assert (archive / f"sample-{digest}.md").read_text() == "unsafe curated guidance"


def test_historical_identity_is_per_name_even_for_identical_blobs(boot):
  module, seed, skills, _, repo = boot
  other = repo / "backend" / "scripts" / "seed-skills" / "other.md"
  other.parent.mkdir(parents=True, exist_ok=True)
  other.write_text("shared old text")
  _commit(repo, "shared old text")
  other.write_text("other new text")
  module.BUILD_SHA = _commit(repo, "sample new text")
  (seed / "sample.md").write_text("sample new text")
  (seed / "other.md").write_text("other new text")
  skills.mkdir(parents=True)
  (skills / "sample.md").write_text("shared old text")
  (skills / "other.md").write_text("shared old text")

  module.init()

  assert (skills / "sample.md").read_text() == "sample new text"
  assert (skills / "other.md").read_text() == "other new text"


def test_unknown_legacy_and_later_local_edits_remain_visible(boot):
  module, seed, skills, _, repo = boot
  module.BUILD_SHA = _commit(repo, "v2")
  (seed / "sample.md").write_text("v2")
  skills.mkdir(parents=True)
  (skills / "sample.md").write_text("my version")

  module.init()
  assert (skills / "sample.md").read_text() == "my version"
  assert _records(skills)["sample.md"]["status"] == "needs_review"
  assert _records(skills)["sample.md"]["baseline_sha256"] is None
  found = skills_mod.enumerate_skills(skills)
  assert found[0].provenance == "seed"
  assert found[0].seed_status == "needs_review"
  assert "seed · needs review" in skills_mod._index_body(found)
  from app.routes.skills import _skill_row
  assert _skill_row(found[0], {}, {}, is_owner=True)["seed_status"] == "needs_review"

  (seed / "sample.md").write_text("v3")
  module.BUILD_SHA = _commit(repo, "v3")
  module.init()
  assert (skills / "sample.md").read_text() == "my version"
  assert _records(skills)["sample.md"]["upstream_sha256"] == hashlib.sha256(b"v3").hexdigest()


def test_owner_deleted_seed_is_not_recreated(boot):
  module, seed, skills, _, repo = boot
  module.BUILD_SHA = _commit(repo, "v1")
  (seed / "sample.md").write_text("v1")
  module.init()
  (skills / "sample.md").unlink()
  module.init()
  assert not (skills / "sample.md").exists()
  assert _records(skills)["sample.md"]["status"] == "missing_local"

  module.BUILD_SHA = _commit(repo, "v2")
  (seed / "sample.md").write_text("v2")
  module.init()
  assert not (skills / "sample.md").exists()
  assert _records(skills)["sample.md"]["upstream_sha256"] == hashlib.sha256(b"v2").hexdigest()


def test_retired_seed_is_archived_once_even_when_modified(boot):
  module, seed, skills, archive, repo = boot
  _commit(repo, "v1")
  module.BUILD_SHA = _commit(repo, remove=True)
  skills.mkdir(parents=True)
  (skills / "sample.md").write_text("owner notes")

  module.init()
  module.init()

  assert not (skills / "sample.md").exists()
  assert list(archive.glob("*.md")) == [archive / f"sample-{hashlib.sha256(b'owner notes').hexdigest()}.md"]
  assert list(archive.glob("*.md"))[0].read_text() == "owner notes"
  assert _records(skills)["sample.md"]["status"] == "retired"

  # A deliberate later owner recreation is not retired again on every boot.
  (skills / "sample.md").write_text("new personal skill")
  module.init()
  assert (skills / "sample.md").read_text() == "new personal skill"
  assert skills_mod.enumerate_skills(skills)[0].provenance == "agent"


def test_interrupted_retirement_replays_without_losing_archive(boot, monkeypatch):
  module, _, skills, archive, repo = boot
  _commit(repo, "v1")
  module.BUILD_SHA = _commit(repo, remove=True)
  skills.mkdir(parents=True)
  (skills / "sample.md").write_text("owner notes")
  original = module._write_records
  monkeypatch.setattr(module, "_write_records", lambda _records: (_ for _ in ()).throw(OSError("crash")))
  with pytest.raises(OSError, match="crash"):
    module.init()
  monkeypatch.setattr(module, "_write_records", original)

  module.init()

  assert not (skills / "sample.md").exists()
  assert len(list(archive.glob("*.md"))) == 1
  assert list(archive.glob("*.md"))[0].read_text() == "owner notes"


def test_app_owned_historical_name_is_never_retired(boot):
  module, seed, skills, archive, repo = boot
  _commit(repo, "v1")
  module.BUILD_SHA = _commit(repo, remove=True)
  skills.mkdir(parents=True)
  (skills / "sample.md").write_text("app copy")
  (skills / ".app-skills.json").write_text(json.dumps({"sample.md": {"app_id": 42}}))

  module.init()

  assert (skills / "sample.md").read_text() == "app copy"
  assert not archive.exists()


def test_missing_history_never_guesses_legacy_ownership(boot):
  module, seed, skills, archive, repo = boot
  _commit(repo, "v1")
  module.BUILD_SHA = "unavailable"
  (seed / "sample.md").write_text("v2")
  skills.mkdir(parents=True)
  (skills / "sample.md").write_text("v1")
  (skills / "retired.md").write_text("local retirement candidate")

  module.init()

  assert (skills / "sample.md").read_text() == "v1"
  assert (skills / "retired.md").exists()
  assert not archive.exists()
  assert _records(skills)["sample.md"]["status"] == "needs_review"


def test_history_newer_than_image_cannot_authorize_overwrite(boot):
  module, seed, skills, _, repo = boot
  image_sha = _commit(repo, "v2")
  _commit(repo, "future local edit")
  module.BUILD_SHA = image_sha
  (seed / "sample.md").write_text("v2")
  skills.mkdir(parents=True)
  (skills / "sample.md").write_text("future local edit")

  module.init()

  assert (skills / "sample.md").read_text() == "future local edit"
  assert _records(skills)["sample.md"]["status"] == "needs_review"


def test_corrupt_owner_sidecar_blocks_all_mutation(boot):
  module, seed, skills, _, repo = boot
  module.BUILD_SHA = _commit(repo, "v1")
  (seed / "sample.md").write_text("v1")
  skills.mkdir(parents=True)
  (skills / ".app-skills.json").write_text("not json")

  module.init()

  assert not (skills / "sample.md").exists()
  assert not (skills / ".seed-skills.json").exists()


def test_interrupted_file_write_reconciles_on_next_boot(boot, monkeypatch):
  module, seed, skills, _, repo = boot
  module.BUILD_SHA = _commit(repo, "v1")
  (seed / "sample.md").write_text("v1")
  module.init()
  module.BUILD_SHA = _commit(repo, "v2")
  (seed / "sample.md").write_text("v2")
  original = module._write_records
  monkeypatch.setattr(module, "_write_records", lambda _records: (_ for _ in ()).throw(OSError("crash")))
  with pytest.raises(OSError, match="crash"):
    module.init()
  assert (skills / "sample.md").read_text() == "v2"
  monkeypatch.setattr(module, "_write_records", original)

  module.init()
  assert _records(skills)["sample.md"]["baseline_sha256"] == hashlib.sha256(b"v2").hexdigest()
  assert _records(skills)["sample.md"]["status"] == "current"


def test_keep_local_decision_holds_edits_across_future_seed_updates(boot):
  module, seed, skills, _, repo = boot
  module.BUILD_SHA = _commit(repo, "v1")
  (seed / "sample.md").write_text("v1")
  module.init()
  (skills / "sample.md").write_text("my version")
  module.resolve("sample.md", "keep-local", hashlib.sha256(b"my version").hexdigest())

  module.BUILD_SHA = _commit(repo, "v2")
  (seed / "sample.md").write_text("v2")
  module.init()

  assert (skills / "sample.md").read_text() == "my version"
  assert _records(skills)["sample.md"]["status"] == "held"
  assert skills_mod.enumerate_skills(skills)[0].seed_status == "held"


def test_take_upstream_preserves_local_bytes_before_replacement(boot):
  module, seed, skills, archive, repo = boot
  module.BUILD_SHA = _commit(repo, "v1")
  (seed / "sample.md").write_text("v1")
  module.init()
  (skills / "sample.md").write_text("my version")

  module.resolve("sample.md", "take-upstream", hashlib.sha256(b"my version").hexdigest())

  assert (skills / "sample.md").read_text() == "v1"
  assert (archive / f"sample-{hashlib.sha256(b'my version').hexdigest()}.md").read_text() == "my version"
  assert _records(skills)["sample.md"]["status"] == "current"


def test_review_decision_rejects_changed_skill(boot):
  module, seed, skills, archive, repo = boot
  module.BUILD_SHA = _commit(repo, "v1")
  (seed / "sample.md").write_text("v1")
  module.init()
  (skills / "sample.md").write_text("new edit")

  with pytest.raises(ValueError, match="changed since review"):
    module.resolve("sample.md", "take-upstream", hashlib.sha256(b"old edit").hexdigest())

  assert (skills / "sample.md").read_text() == "new edit"
  assert not archive.exists()
