"""Codex project-local skill materialization (parity with Claude skills="all")."""

from app.codex_skills import (
  _MANIFEST,
  _safe_dir_name,
  render_shim,
  sync_codex_skills,
)


def _make_skill(root, name, description, body="Do the thing."):
  skills = root / "shared" / "skills"
  skills.mkdir(parents=True, exist_ok=True)
  (skills / f"{name}.md").write_text(
    f"---\nname: {name}\ndescription: {description}\n---\n{body}\n",
    encoding="utf-8",
  )


def test_render_shim_has_match_metadata_and_source_pointer():
  shim = render_shim("foo", "Do a foo thing.", "/data/shared/skills/foo.md")
  assert "name: foo" in shim
  assert "description: Do a foo thing." in shim
  # Points at the single source of truth rather than copying content.
  assert "/data/shared/skills/foo.md" in shim
  assert "Read that file now" in shim


def test_sync_materializes_shims_and_prunes_removed(tmp_path):
  _make_skill(tmp_path, "alpha", "Alpha skill.")
  _make_skill(tmp_path, "beta", "Beta skill.")

  names = sync_codex_skills(str(tmp_path), True)
  assert names == ["alpha", "beta"]
  shim = tmp_path / ".codex" / "skills" / "alpha" / "SKILL.md"
  assert shim.is_file() and "name: alpha" in shim.read_text(encoding="utf-8")
  assert (tmp_path / ".codex" / "skills" / _MANIFEST).is_file()

  # A skill removed from the source is pruned on the next sync.
  (tmp_path / "shared" / "skills" / "beta.md").unlink()
  assert sync_codex_skills(str(tmp_path), True) == ["alpha"]
  assert not (tmp_path / ".codex" / "skills" / "beta").exists()


def test_sync_disabled_prunes_only_managed_shims(tmp_path):
  _make_skill(tmp_path, "alpha", "Alpha skill.")
  sync_codex_skills(str(tmp_path), True)

  # A hand-placed, unmanaged skill must survive a disable/prune.
  hand = tmp_path / ".codex" / "skills" / "handmade"
  hand.mkdir(parents=True)
  (hand / "SKILL.md").write_text(
    "---\nname: handmade\ndescription: x\n---\n", encoding="utf-8"
  )

  assert sync_codex_skills(str(tmp_path), False) == []
  assert not (tmp_path / ".codex" / "skills" / "alpha").exists()
  assert (hand / "SKILL.md").is_file()


def test_safe_dir_name_sanitizes():
  assert _safe_dir_name("building-apps") == "building-apps"
  assert _safe_dir_name("a/b c") == "a-b-c"
  assert _safe_dir_name("..") is None
  assert _safe_dir_name("") is None
