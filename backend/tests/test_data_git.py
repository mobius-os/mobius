"""Focused contract tests for snapshots in the shared /data repository."""

from __future__ import annotations

import subprocess

from app import data_git


def _git(repo, *args: str) -> subprocess.CompletedProcess:
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    capture_output=True,
    text=True,
    check=True,
  )


def _init_repo(repo) -> None:
  _git(repo, "init", "-q", "-b", "main")
  _git(
    repo,
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.invalid",
    "commit",
    "-q",
    "--allow-empty",
    "-m",
    "init",
  )


def test_snapshot_commits_only_target_and_preserves_unrelated_staging(
  tmp_path,
  monkeypatch,
):
  """A safety snapshot neither captures nor unstages another pending change."""
  _init_repo(tmp_path)
  target = tmp_path / "shared" / "skills" / "demo.md"
  target.parent.mkdir(parents=True)
  target.write_text("owner edit\n")
  unrelated = tmp_path / "unrelated.txt"
  unrelated.write_text("keep staged\n")
  _git(tmp_path, "add", "unrelated.txt")

  # Inherited hook variables override `git -C`; the helper must scrub them.
  monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong-repository"))
  monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "wrong-worktree"))
  monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "wrong-index"))

  result = data_git.snapshot_path(
    tmp_path,
    "shared/skills/demo.md",
    "preserve demo before mutation",
  )
  for variable in data_git._REDIRECTING_GIT_ENV:
    monkeypatch.delenv(variable)

  assert result == (True, "committed")
  assert _git(tmp_path, "show", "HEAD:shared/skills/demo.md").stdout == (
    "owner edit\n"
  )
  assert _git(tmp_path, "show", "--pretty=", "--name-only", "HEAD").stdout == (
    "shared/skills/demo.md\n"
  )
  assert _git(tmp_path, "diff", "--cached", "--name-only").stdout == (
    "unrelated.txt\n"
  )


def test_snapshot_reuses_clean_history_without_creating_commit(tmp_path):
  """Already-recorded bytes are durable without manufacturing history."""
  _init_repo(tmp_path)
  target = tmp_path / "shared" / "skills" / "demo.md"
  target.parent.mkdir(parents=True)
  target.write_text("recorded\n")
  _git(tmp_path, "add", "shared/skills/demo.md")
  _git(
    tmp_path,
    "-c",
    "user.name=Test",
    "-c",
    "user.email=test@example.invalid",
    "commit",
    "-q",
    "-m",
    "record demo",
  )
  before = _git(tmp_path, "rev-parse", "HEAD").stdout

  result = data_git.snapshot_path(
    tmp_path,
    "shared/skills/demo.md",
    "should not be created",
  )

  assert result == (True, "already committed")
  assert _git(tmp_path, "rev-parse", "HEAD").stdout == before
