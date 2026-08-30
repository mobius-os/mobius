"""Behavioral boot contract for the outer /data safety repository."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "init_data_repo.py"
SPEC = importlib.util.spec_from_file_location("init_data_repo", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
init_data_repo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(init_data_repo)


def _git(repo: Path, *args: str) -> str:
  env = {
    name: value
    for name, value in os.environ.items()
    if name not in init_data_repo._REDIRECTING_GIT_ENV
  }
  process = subprocess.run(
    ["git", "-C", str(repo), *args],
    capture_output=True,
    text=True,
    check=True,
    env=env,
  )
  return process.stdout


def _commit(repo: Path, message: str) -> None:
  _git(
    repo,
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.invalid",
    "commit",
    "-q",
    "-m",
    message,
  )


def _init_repo(repo: Path) -> None:
  _git(repo, "init", "-q", "-b", "main")
  (repo / "owner.txt").write_text("owner data\n", encoding="utf-8")
  _git(repo, "add", "owner.txt")
  _commit(repo, "init")


def _nested_repo(path: Path) -> None:
  path.mkdir(parents=True)
  _git(path, "init", "-q", "-b", "main")
  (path / "tracked.txt").write_text("nested history\n", encoding="utf-8")
  _git(path, "add", "tracked.txt")
  _commit(path, "nested")


def test_fresh_boot_tracks_owner_data_but_never_runtime_repositories(tmp_path):
  (tmp_path / "owner.txt").write_text("owner data\n", encoding="utf-8")
  owned_repositories = (
    tmp_path / "run" / "job",
    tmp_path / "agent-scratch" / "chat" / "candidate",
    tmp_path / "contrib" / "review" / "worktree",
    tmp_path / "contributions" / "legacy" / "worktree",
  )
  for repo in owned_repositories:
    _nested_repo(repo)
  owner_repository = tmp_path / "shared" / "projects" / "owner-repository"
  _nested_repo(owner_repository)

  init_data_repo.write_ignore(tmp_path)
  state, removed = init_data_repo.reconcile(tmp_path)

  assert (state, removed) == ("initialized", 0)
  tracked = set(_git(tmp_path, "ls-files").splitlines())
  assert {".gitignore", "owner.txt"}.issubset(tracked)
  assert not any(
    path.startswith(("run/", "agent-scratch/", "contrib/", "contributions/"))
    for path in tracked
  )
  for repo in owned_repositories:
    assert (repo / ".git").is_dir()
  assert (owner_repository / ".git").is_dir()
  assert "shared/projects/owner-repository" in tracked

  before = _git(tmp_path, "rev-parse", "HEAD")
  assert init_data_repo.reconcile(tmp_path) == ("reconciled", 0)
  assert _git(tmp_path, "rev-parse", "HEAD") == before


def test_upgrade_untracks_every_root_policy_match_without_deleting_files(
  tmp_path,
  monkeypatch,
):
  _init_repo(tmp_path)
  historical_repositories = (
    tmp_path / "run" / "stale",
    tmp_path / "agent-scratch" / "chat" / "candidate",
    tmp_path / "contrib" / "review" / "worktree",
    tmp_path / "contributions" / "legacy" / "worktree",
  )
  for repo in historical_repositories:
    _nested_repo(repo)

  runtime_launcher = tmp_path / ".pm-commit"
  runtime_launcher.write_text("runtime launcher\n", encoding="utf-8")
  _git(
    tmp_path,
    "add",
    ".pm-commit",
    "run",
    "agent-scratch",
    "contrib",
    "contributions",
  )
  _commit(tmp_path, "historical runtime entries")

  # A nested app ignore file must not expand the root ownership policy.
  app = tmp_path / "apps" / "demo"
  app.mkdir(parents=True)
  (app / ".gitignore").write_text("generated.txt\n", encoding="utf-8")
  (app / "generated.txt").write_text("keep outer history\n", encoding="utf-8")
  _git(tmp_path, "add", "-f", "apps/demo/.gitignore", "apps/demo/generated.txt")
  _commit(tmp_path, "tracked app fixture")

  unrelated = tmp_path / "unrelated.txt"
  unrelated.write_text("already staged\n", encoding="utf-8")
  _git(tmp_path, "add", "unrelated.txt")

  # Reproduce the original failure shape: a tracked gitlink whose checkout
  # remains but whose Git admin directory disappeared during a restart.
  broken = historical_repositories[0]
  shutil.rmtree(broken / ".git")
  (broken / ".git").write_text(
    "gitdir: /definitely/missing/worktree-admin\n",
    encoding="utf-8",
  )

  # Inherited Git pointers must not redirect a boot operation into another repo.
  monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-git-dir"))
  monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-worktree"))
  monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "wrong-index"))

  init_data_repo.write_ignore(tmp_path)
  state, removed = init_data_repo.reconcile(tmp_path)

  assert state == "reconciled"
  assert removed == len(historical_repositories) + 1
  tracked = set(_git(tmp_path, "ls-files").splitlines())
  assert ".pm-commit" not in tracked
  assert "apps/demo/generated.txt" in tracked
  assert "unrelated.txt" in tracked
  assert not any(
    path.startswith(("run/", "agent-scratch/", "contrib/", "contributions/"))
    for path in tracked
  )

  # Reconciliation is index-only, including for the deliberately broken repo.
  assert runtime_launcher.read_text(encoding="utf-8") == "runtime launcher\n"
  for repo in historical_repositories:
    assert (repo / "tracked.txt").read_text(encoding="utf-8") == (
      "nested history\n"
    )
  assert (broken / ".git").is_file()
  assert unrelated.read_text(encoding="utf-8") == "already staged\n"

  staged_before = _git(tmp_path, "diff", "--cached", "--binary")
  assert init_data_repo.reconcile(tmp_path) == ("reconciled", 0)
  assert _git(tmp_path, "diff", "--cached", "--binary") == staged_before


def test_boot_policy_replaces_a_symlink_instead_of_following_it(tmp_path):
  outside = tmp_path.parent / f"{tmp_path.name}-outside"
  outside.write_text("do not overwrite\n", encoding="utf-8")
  os.symlink(outside, tmp_path / ".gitignore")

  init_data_repo.write_ignore(tmp_path)

  assert outside.read_text(encoding="utf-8") == "do not overwrite\n"
  assert not (tmp_path / ".gitignore").is_symlink()
  assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == (
    init_data_repo.DATA_GITIGNORE
  )


def test_repository_work_has_no_fixed_boot_timeout(tmp_path, monkeypatch):
  """Large valid owner volumes must not become unable to boot after 60 seconds."""
  observed: dict[str, object] = {}

  def run(command, **kwargs):
    observed.update(kwargs)
    return subprocess.CompletedProcess(command, 0, b"", b"")

  monkeypatch.setattr(init_data_repo.subprocess, "run", run)

  init_data_repo._git(tmp_path, "status", "--porcelain")

  assert "timeout" not in observed
