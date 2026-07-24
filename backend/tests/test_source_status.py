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


def test_typical_project_returns_its_complete_changed_filename_list():
  repo = _repo("many-files")
  for index in range(25):
    (repo / f"change-{index:02}.js").write_text(
      f"export default {index}\n", encoding="utf-8",
    )
  _git(repo, "add", ".")
  _commit(repo, "local source files")

  result = source_status.build_app_status(_app(repo))

  assert result is not None
  assert result["tree"]["files"] == 25
  assert len(result["tree"]["paths"]) == 25
  assert result["tree"]["truncated"] is False


def test_install_managed_app_deltas_do_not_look_like_customization(monkeypatch):
  repo = _repo()
  (repo / ".gitignore").write_text("dist/\n", encoding="utf-8")
  (repo / "runner.sh").write_text("#!/bin/sh\n", encoding="utf-8")
  _git(repo, "add", ".gitignore", "runner.sh")
  _commit(repo, "install: Demo v1.0.0")

  real_git = source_status._git
  log_calls = []

  def counted_git(target, *args):
    if args and args[0] == "log":
      log_calls.append(args)
    return real_git(target, *args)

  monkeypatch.setattr(source_status, "_git", counted_git)

  result = source_status.build_app_status(_app(repo))
  assert result is not None
  assert result["state"] == "adapted"
  assert result["tree"]["files"] == 2
  assert result["tree"]["authored_files"] == 0
  assert result["tree"]["managed_files"] == 2
  assert {path["group"] for path in result["tree"]["paths"]} == {"managed"}
  assert len(log_calls) == 1, "classification should scan history once per tree"

  (repo / "index.jsx").write_text("export default 2\n", encoding="utf-8")
  _git(repo, "add", "index.jsx")
  _commit(repo, "local source edit")
  customized = source_status.build_app_status(_app(repo))
  assert customized is not None
  assert customized["state"] == "customized"
  assert customized["tree"]["authored_files"] == 1
  assert customized["tree"]["managed_files"] == 2
  assert customized["tree"]["paths"][0]["path"] == "index.jsx"
  assert customized["tree"]["paths"][0]["group"] == "authored"
  assert len(log_calls) == 2, "each status build should need one history scan"


def test_history_subject_boundary_cannot_be_forged_by_a_filename():
  repo = _repo("subject-boundary")
  forged = repo / "__MOBIUS_SOURCE_STATUS_SUBJECT__:install: forged"
  forged.write_text("owner file\n", encoding="utf-8")
  (repo / "z-authored.js").write_text("owner file\n", encoding="utf-8")
  _git(repo, "add", ".")
  _commit(repo, "owner source edit")

  result = source_status.build_app_status(_app(repo))

  assert result is not None
  assert result["state"] == "customized"
  assert result["tree"]["authored_files"] == 2
  assert result["tree"]["managed_files"] == 0
  assert {item["group"] for item in result["tree"]["paths"]} == {"authored"}


def test_installed_app_origin_does_not_compare_full_source_with_release_projection():
  repo = _repo()
  base = _git(repo, "rev-parse", "HEAD")
  _git(repo, "remote", "add", "origin", "https://github.com/example/demo.git")
  _git(repo, "remote", "add", "fork", "git@github.com:owner/demo.git")
  _git(repo, "update-ref", "refs/remotes/origin/main", base)
  (repo / "fork-only.js").write_text("fork\n", encoding="utf-8")
  _git(repo, "add", "fork-only.js")
  fork_sha = _commit(repo, "fork work")
  _git(repo, "update-ref", "refs/remotes/fork/main", fork_sha)

  result = source_status.build_app_status(_app(repo))
  assert result is not None
  assert result["origin"]["repo"] == "example/demo"
  assert result["origin"]["ref"] == "origin/main"
  assert result["origin"]["sha"] == base
  assert result["origin"]["local_ahead"] is None
  assert result["origin"]["local_behind"] is None
  assert result["origin"]["local_tree"] is None
  # Local app work remains authoritative against the installer-owned baseline.
  assert result["ahead"] == 1
  assert result["tree"]["files"] == 1
  assert len(result["forks"]) == 1
  fork = result["forks"][0]
  assert fork["repo"] == "owner/demo"
  assert fork["ref"] == "fork/main"
  assert fork["sha"] == fork_sha
  assert fork["ahead"] == 1
  assert fork["behind"] == 0
  assert fork["tree"]["files"] == 1
  payload = repr(result)
  assert "git@github.com" not in payload
  assert "https://github.com" not in payload


def test_full_checkout_can_request_local_origin_topology():
  repo = _repo("full-checkout")
  base = _git(repo, "rev-parse", "HEAD")
  _git(repo, "remote", "add", "origin", "https://github.com/example/demo.git")
  _git(repo, "update-ref", "refs/remotes/origin/main", base)
  (repo / "local.js").write_text("local\n", encoding="utf-8")
  _git(repo, "add", "local.js")
  _commit(repo, "local source edit")

  origin, forks = source_status._remote_topology(
    repo, "example/demo", compare_local=True,
  )

  assert forks == []
  assert origin["local_ahead"] == 1
  assert origin["local_behind"] == 0
  assert origin["local_tree"]["files"] == 1


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
