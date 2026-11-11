"""A retained review folder must never borrow a replacement review's Git state."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import contribution_staging, git_checkout, github_contributions
from app.config import get_settings
from app.contribution_errors import ContributionSubmitError


def git(repo, *args):
  return subprocess.run(
    ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
     "-C", str(repo), *args],
    capture_output=True, text=True, check=True,
  ).stdout.strip()


@pytest.fixture
def recycled(tmp_path):
  source = tmp_path / "source"
  source.mkdir()
  git(source, "init", "-q")
  git(source, "commit", "-q", "--allow-empty", "-m", "base")
  old = tmp_path / "old" / "worktree"
  git(source, "worktree", "add", "-q", "-b", "review-a", str(old))
  retained = old.with_name("retained")
  old.rename(retained)
  git(source, "worktree", "prune", "--expire", "now")
  current = tmp_path / "current" / "worktree"
  git(source, "worktree", "add", "-q", "-b", "review-b", str(current))
  assert (retained / ".git").read_bytes() == (current / ".git").read_bytes()
  return source, retained, current


def snapshot(root):
  return {str(p.relative_to(root)): p.read_bytes()
          for p in root.rglob("*") if p.is_file()}


def test_recycled_slot_is_rejected_without_changing_either_checkout(recycled):
  source, retained, current = recycled
  before = snapshot(source.parent)
  assert git_checkout.checkout_ownership_error(retained)
  assert git_checkout.checkout_ownership_error(current) is None
  assert git_checkout.checkout_ownership_error(source) is None
  for read in (
    lambda: contribution_staging.checkout_branch(retained),
    lambda: contribution_staging.derive_candidate(retained, "review-b", "HEAD"),
  ):
    with pytest.raises(ContributionSubmitError) as caught:
      read()
    assert caught.value.code == "checkout_ownership_mismatch"
  assert snapshot(source.parent) == before


def test_publication_path_refuses_recycled_state_but_cleanup_can_locate_it(
  recycled, monkeypatch,
):
  source, retained, current = recycled
  monkeypatch.setattr(get_settings(), "data_dir", str(source.parent))
  # The real durable-path allowlist is part of this boundary, not mocked out.
  contrib = source.parent / "contrib"
  contrib.mkdir()
  allowed = contrib / "retained"
  retained.rename(allowed)
  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._safe_repo_path(str(allowed))
  assert caught.value.code == "checkout_ownership_mismatch"
  assert github_contributions._durable_repo_path(str(allowed)) == allowed
  assert git_checkout.checkout_ownership_error(current) is None


def test_valid_relative_linked_pointers_and_separate_git_directory(recycled, tmp_path):
  source, _, current = recycled
  marker = current / ".git"
  admin = Path(marker.read_text().split(":", 1)[1].strip())
  marker.write_text(f"gitdir: {os.path.relpath(admin, current)}\n")
  (admin / "gitdir").write_text(os.path.relpath(marker, admin) + "\n")
  assert git_checkout.checkout_ownership_error(current) is None
  assert contribution_staging.checkout_branch(current) == "review-b"

  separate = tmp_path / "separate" / "worktree"
  separate.parent.mkdir()
  git(source, "clone", "-q", "--separate-git-dir",
      str(separate.parent / "git"), str(source), str(separate))
  assert git_checkout.checkout_ownership_error(separate) is None
  assert contribution_staging.checkout_branch(separate)


def test_one_pointer_parser_names_the_git_directory_for_both_callers(recycled, tmp_path):
  source, _, current = recycled
  admin = Path((current / ".git").read_text().split(":", 1)[1].strip()).resolve()
  assert git_checkout.linked_git_dir(current) == admin
  (current / ".git").write_text(f"gitdir: {os.path.relpath(admin, current)}\n")
  assert git_checkout.linked_git_dir(current) == admin
  assert git_checkout.linked_git_dir(source) is None
  assert git_checkout.linked_git_dir(tmp_path / "absent") is None
  alias = tmp_path / "alias"
  alias.mkdir()
  (alias / ".git").symlink_to(current / ".git")
  assert git_checkout.linked_git_dir(alias) is None
  for pointer in ("not-a-pointer", "gitdir: "):
    (alias / ".git").unlink()
    (alias / ".git").write_text(pointer)
    assert git_checkout.linked_git_dir(alias) is None


@pytest.mark.parametrize("damage", ["missing", "empty", "other", "symlink"])
def test_linked_checkout_requires_its_reciprocal_pointer(recycled, damage):
  _, _, current = recycled
  admin = Path((current / ".git").read_text().split(":", 1)[1].strip())
  pointer = admin / "gitdir"
  if damage == "missing":
    pointer.unlink()
  elif damage == "empty":
    pointer.write_text("")
  elif damage == "other":
    pointer.write_text(str(current.parent / "not-this-folder" / ".git"))
  else:
    pointer.unlink()
    pointer.symlink_to(current / ".git")
  assert git_checkout.checkout_ownership_error(current)


def test_reuse_preflight_cli_is_read_only_and_fails_for_stale_folder(recycled):
  source, retained, current = recycled
  script = Path(__file__).parents[1] / "scripts" / "check-review-checkout.py"
  before = snapshot(source.parent)
  for repo, status in ((retained, 1), (current, 0), (source.parent / "missing", 1)):
    result = subprocess.run(
      [sys.executable, str(script), str(repo)], capture_output=True, text=True,
    )
    assert result.returncode == status, result.stderr
    assert "Traceback" not in result.stderr
  assert snapshot(source.parent) == before


@pytest.mark.parametrize("pointer", ["gitdir: /missing/git", "not-a-pointer", "gitdir: "])
def test_missing_or_invalid_administrative_state_is_refused(tmp_path, pointer):
  (tmp_path / ".git").write_text(pointer)
  assert git_checkout.checkout_ownership_error(tmp_path)


def test_missing_checkout_keeps_existing_callers_missing_repo_handling(tmp_path):
  assert git_checkout.checkout_ownership_error(tmp_path / "absent") is None


@pytest.mark.parametrize("shape", ["linked", "ordinary"])
def test_symbolic_git_marker_cannot_borrow_another_checkouts_identity(recycled, shape):
  source, _, current = recycled
  alias = source.parent / "alias"
  alias.mkdir()
  target = current if shape == "linked" else source
  (alias / ".git").symlink_to(target / ".git", target_is_directory=shape == "ordinary")
  before = snapshot(source.parent)
  assert git_checkout.checkout_ownership_error(alias)
  assert git_checkout.checkout_ownership_error(target) is None
  assert snapshot(source.parent) == before


def test_cli_does_not_create_bytecode_in_the_checkout_it_checks(tmp_path):
  backend = Path(__file__).parents[1]
  package = tmp_path / "backend" / "app"
  scripts = tmp_path / "backend" / "scripts"
  package.mkdir(parents=True)
  scripts.mkdir()
  (package / "__init__.py").write_text("")
  shutil.copyfile(backend / "app" / "git_checkout.py", package / "git_checkout.py")
  script = scripts / "check-review-checkout.py"
  shutil.copyfile(backend / "scripts" / script.name, script)
  (tmp_path / ".git").mkdir()
  before = snapshot(tmp_path)
  env = {k: v for k, v in os.environ.items()
         if k not in {"PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX"}}
  result = subprocess.run(
    [sys.executable, str(script), str(tmp_path)],
    capture_output=True, text=True, env=env,
  )
  assert result.returncode == 0, result.stderr
  assert snapshot(tmp_path) == before
