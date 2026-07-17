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
    "Installed app skills — Read /data/shared/skills/<name> before the task "
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
