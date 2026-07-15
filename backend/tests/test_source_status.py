"""Focused tests for Contribute's fetch-free aggregate source metadata."""

from __future__ import annotations

import subprocess
from pathlib import Path

from app import source_status
from app.config import get_settings


def _git(repo: Path, *args: str) -> str:
  proc = subprocess.run(
    ["git", "-C", str(repo), *args], capture_output=True, text=True,
    check=True,
  )
  return proc.stdout.strip()


def _commit(repo: Path, message: str, *, allow_empty: bool = False) -> str:
  args = [
    "-c", "user.name=Test", "-c", "user.email=test@example.com",
    "commit", "-q", "-m", message,
  ]
  if allow_empty:
    args.append("--allow-empty")
  _git(repo, *args)
  return _git(repo, "rev-parse", "HEAD")


def _repo(name: str = "demo") -> Path:
  root = Path(get_settings().data_dir) / "apps" / name
  root.mkdir(parents=True, exist_ok=True)
  _git(root, "init", "-q", "-b", "main")
  (root / "index.jsx").write_text("export default 1\n", encoding="utf-8")
  _git(root, "add", "index.jsx")
  _commit(root, "install")
  _git(root, "branch", "upstream")
  return root


def _app(repo: Path, *, app_id: int = 7) -> dict:
  return {
    "id": app_id,
    "name": "Demo",
    "slug": repo.name,
    "version": "1.0.0",
    "manifest_url": (
      "https://raw.githubusercontent.com/mobius-os/app-demo/main/mobius.json"
    ),
    "source_dir": str(repo),
  }


def test_aligned_and_history_only_ahead_keep_tree_magnitude_zero():
  repo = _repo()
  aligned = source_status.build_app_status(_app(repo))
  assert aligned is not None
  assert aligned["state"] == "aligned"
  assert aligned["ahead"] == 0
  assert aligned["behind"] == 0
  assert aligned["tree"]["files"] == 0

  _commit(repo, "watcher bookkeeping", allow_empty=True)
  ahead = source_status.build_app_status(_app(repo))
  assert ahead is not None
  assert ahead["ahead"] == 1
  assert ahead["tree"]["files"] == 0
  assert ahead["state"] == "aligned"


def test_committed_and_working_deltas_are_reported_separately():
  repo = _repo()
  (repo / "index.jsx").write_text("export default 2\n", encoding="utf-8")
  _git(repo, "add", "index.jsx")
  _commit(repo, "local source edit")
  (repo / "index.jsx").write_text("export default 3\n", encoding="utf-8")
  (repo / "staged.js").write_text("export const x = 1\n", encoding="utf-8")
  (repo / "untracked.js").write_text("export const y = 2\n", encoding="utf-8")
  _git(repo, "add", "staged.js")

  result = source_status.build_app_status(_app(repo))
  assert result is not None
  assert result["state"] == "working"
  assert result["tree"]["files"] == 1
  assert result["tree"]["insertions"] == 1
  assert result["tree"]["deletions"] == 1
  assert result["working"]["files"] == 3
  assert result["working"]["staged"] == 1
  assert result["working"]["unstaged"] == 1
  assert result["working"]["untracked"] == 1


def test_diverged_counts_and_sanitized_github_identity():
  repo = _repo()
  _git(repo, "checkout", "-q", "upstream")
  (repo / "remote.js").write_text("remote\n", encoding="utf-8")
  _git(repo, "add", "remote.js")
  _commit(repo, "incoming")
  _git(repo, "checkout", "-q", "main")
  (repo / "local.js").write_text("local\n", encoding="utf-8")
  _git(repo, "add", "local.js")
  _commit(repo, "local")
  _git(repo, "remote", "add", "origin", "git@github.com:example/private-demo.git")

  result = source_status.build_app_status(_app(repo))
  assert result is not None
  assert result["state"] == "diverged"
  assert result["behind"] == 1
  assert result["ahead"] == 1
  assert result["canonical_repo"] == "example/private-demo"
  payload = repr(result)
  assert str(repo) not in payload
  assert "git@github.com" not in payload


def test_local_only_and_invalid_source_paths_degrade_safely(tmp_path):
  repo = _repo("local-only")
  _git(repo, "branch", "-D", "upstream")
  result = source_status.build_app_status(_app(repo))
  assert result is not None
  assert result["state"] == "local_only"
  assert result["base_sha"] is None

  outside = tmp_path / "outside"
  outside.mkdir()
  assert source_status.build_app_status(_app(outside)) is None

  numeric = Path(get_settings().data_dir) / "apps" / "66"
  numeric.mkdir(parents=True, exist_ok=True)
  assert source_status.build_app_status(_app(numeric)) is None

  target = Path(get_settings().data_dir) / "apps" / "target"
  target.mkdir()
  link = Path(get_settings().data_dir) / "apps" / "linked"
  link.symlink_to(target, target_is_directory=True)
  assert source_status.build_app_status(_app(link)) is None


def test_git_output_with_non_utf8_path_is_safely_sanitized():
  repo = _repo("odd-path")
  raw_name = b"odd-\xff.js"
  raw_path = bytes(repo) + b"/" + raw_name
  with open(raw_path, "wb") as handle:
    handle.write(b"export default 1\n")

  result = source_status.build_app_status(_app(repo))

  assert result is not None
  assert result["working"]["files"] == 1
  assert result["working"]["untracked"] == 1
  assert result["working"]["paths"][0]["path"] == "odd-�.js"
