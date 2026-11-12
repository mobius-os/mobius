"""A retained review folder must never borrow a replacement review's Git state."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app import app_git, contribution_staging, git_checkout, github_contributions
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


@pytest.fixture
def stale_settlement(tmp_path, monkeypatch):
  """A finished review whose retained folder points at a recycled worktree slot."""
  data = tmp_path / "data"
  source = data / "worktrees" / "project"
  source.mkdir(parents=True)
  git(source, "init", "-q")
  git(source, "commit", "-q", "--allow-empty", "-m", "base")
  base = git(source, "rev-parse", "HEAD")
  old = data / "contrib" / "record-a" / "worktree"
  git(source, "worktree", "add", "-q", "-b", "review-a", str(old))
  git(old, "commit", "-q", "--allow-empty", "-m", "reviewed")
  head = git(old, "rev-parse", "HEAD")
  retained = old.with_name("retained")
  old.rename(retained)
  git(source, "worktree", "prune", "--expire", "now")
  git(source, "worktree", "add", "-q", "-b", "review-b",
      str(data / "contrib" / "record-b" / "worktree"))
  assert git_checkout.checkout_ownership_error(retained)
  monkeypatch.setattr(get_settings(), "data_dir", str(data))
  digest = "a" * 64
  record = {
    "id": "record-a", "type": "pr", "repo": "owner/project", "number": 7,
    "plan": {
      "repo": "owner/project", "repo_path": str(retained),
      "source_repo_path": str(source), "base_sha": base, "head_sha": head,
      "diff_sha256": digest,
    },
  }
  return source, retained, record


def test_settlement_finds_recorded_source_behind_a_stale_review_folder(
  stale_settlement,
):
  source, retained, record = stale_settlement
  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._safe_repo_path(str(retained))
  assert caught.value.code == "checkout_ownership_mismatch"
  assert github_contributions._equivalence_source_repo(record) == (source, retained)
  # Reading the reviewed commits uses the durable source, never the stale folder.
  spec = github_contributions._pending_equivalence_spec(record)
  assert spec is not None and spec.review_repo == source
  # Without a recorded source, the stale folder's Git state cannot name one.
  del record["plan"]["source_repo_path"]
  with pytest.raises(ContributionSubmitError) as caught:
    github_contributions._equivalence_source_repo(record)
  assert caught.value.code == "checkout_ownership_mismatch"


def test_closed_pr_settlement_cleans_source_refs_behind_a_stale_review_folder(
  stale_settlement,
):
  source, _, record = stale_settlement
  plan = record["plan"]
  pending = app_git._equivalence_ref(
    app_git._EQUIVALENCE_PENDING_PREFIX, plan["diff_sha256"],
  )
  draft = contribution_staging._draft_ref(record["id"])
  for ref in (pending, draft):
    git(source, "update-ref", ref, plan["head_sha"])
  github_contributions._settle_equivalence({**record, "status": "closed"})
  for ref in (pending, draft):
    assert not app_git.ref_exists(source, ref)


def test_merged_pr_settlement_records_landing_behind_a_stale_review_folder(
  stale_settlement, monkeypatch,
):
  source, _, record = stale_settlement
  calls = []
  for name in ("_mark_superseded_draft", "_record_merged_contribution"):
    monkeypatch.setattr(
      github_contributions, name,
      lambda rec, repo, sha, name=name: calls.append((name, repo, sha)),
    )
  monkeypatch.setattr(
    app_git, "mark_equivalent_change_landed",
    lambda repo, digest, upstream_sha=None: calls.append(
      ("landed", Path(repo), upstream_sha),
    ) or "landed",
  )
  upstream = "b" * 40
  assert github_contributions._settle_equivalence(
    {**record, "status": "merged"}, upstream,
  ) == "landed"
  assert calls == [
    ("_mark_superseded_draft", source, upstream),
    ("_record_merged_contribution", source, upstream),
    ("landed", source, upstream),
  ]
