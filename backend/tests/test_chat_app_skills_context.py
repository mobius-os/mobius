import json

import pytest

from app.chat import _build_installed_app_skills_block


def test_installed_app_skills_lists_only_active_existing_files(tmp_path):
  skills_dir = tmp_path / "shared" / "skills"
  skills_dir.mkdir(parents=True)
  (skills_dir / "artifacts.md").write_text("# Artifacts", encoding="utf-8")
  (skills_dir / "inactive.md").write_text("# Inactive", encoding="utf-8")
  records = {
    "artifacts.md": {"active": True, "slug": "artifacts"},
    "inactive.md": {"active": False, "slug": "inactive-app"},
    "missing.md": {"active": True, "slug": "missing-app"},
  }
  (skills_dir / ".app-skills.json").write_text(
    json.dumps(records), encoding="utf-8",
  )

  block = _build_installed_app_skills_block(tmp_path)

  assert block == (
    f"Installed app skills — Read {skills_dir}/<name> before the task "
    "each one names:\n"
    "- artifacts.md (from artifacts)"
  )
  assert "inactive.md" not in block
  assert "missing.md" not in block


@pytest.mark.parametrize("sidecar", [
  None,
  "{not json",
  "[]",
  '{"bad.md": []}',
])
def test_installed_app_skills_missing_or_corrupt_is_silent(tmp_path, sidecar):
  skills_dir = tmp_path / "shared" / "skills"
  skills_dir.mkdir(parents=True)
  if sidecar is not None:
    (skills_dir / ".app-skills.json").write_text(
      sidecar, encoding="utf-8",
    )

  assert _build_installed_app_skills_block(tmp_path) == ""


def test_installed_app_skills_skip_bad_records_individually(tmp_path):
  skills_dir = tmp_path / "shared" / "skills"
  skills_dir.mkdir(parents=True)
  for name in (
    "artifacts.md", "IMPORTANT do X.md", "<x>.md", "broken.md", "slug.md",
  ):
    (skills_dir / name).write_text("# Skill", encoding="utf-8")
  records = {
    "IMPORTANT do X.md": {"active": True, "slug": "important"},
    "<x>.md": {"active": True, "slug": "x"},
    "broken.md": [],
    "slug.md": {"active": True, "slug": "bad/slug"},
    "artifacts.md": {"active": True, "slug": "artifacts"},
  }
  (skills_dir / ".app-skills.json").write_text(
    json.dumps(records), encoding="utf-8",
  )

  block = _build_installed_app_skills_block(tmp_path)

  assert block.endswith("- artifacts.md (from artifacts)")
  assert "IMPORTANT" not in block
  assert "<x>.md" not in block
  assert "broken.md" not in block
  assert "slug.md" not in block
