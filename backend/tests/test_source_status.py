"""Focused tests for Contribute's fetch-free aggregate source metadata."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from app import app_git, source_status
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
    "published_manifest_url": None,
    "source_dir": str(repo),
  }


def test_platform_source_status_ignores_unrelated_environment(
  monkeypatch, tmp_path,
):
  monkeypatch.setenv(
    "MOBIUS_UNUSED_SETTING",
    "ignored",
  )

  result = source_status._project_status(
    repo=tmp_path / "missing-platform",
    kind="platform",
    key="platform",
    name="Möbius",
    slug=None,
    version=None,
    manifest_url=None,
  )

  assert result["base_ref"] == "origin/main"
  assert "release_ref" not in result


def test_local_published_app_uses_distribution_manifest_as_repository_identity():
  repo = _repo("published-demo")
  app = _app(repo)
  app["manifest_url"] = None
  app["published_manifest_url"] = (
    "https://raw.githubusercontent.com/example/published-demo/main/mobius.json"
  )

  result = source_status.build_app_status(app)

  assert result is not None
  assert result["canonical_repo"] == "example/published-demo"


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
  assert ahead["reconciliation"]["local_only_count"] == 0
  assert ahead["reconciliation"]["new_upstream_count"] == 0


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


def test_project_diff_combines_committed_working_and_untracked_source():
  repo = _repo("diff-preview")
  (repo / "index.jsx").write_text("export default 2\n", encoding="utf-8")
  _git(repo, "add", "index.jsx")
  _commit(repo, "accepted local edit")
  (repo / "index.jsx").write_text("export default 3\n", encoding="utf-8")
  (repo / "new-file.js").write_text("export const newFile = true\n", encoding="utf-8")
  (repo / ".gitignore").write_text("ignored.log\n", encoding="utf-8")
  (repo / "ignored.log").write_text("runtime noise\n", encoding="utf-8")

  project = source_status.build_app_status(_app(repo))
  assert project is not None
  preview = source_status.build_project_diff(
    repo, project, expected_head=project["head_sha"],
    expected_comparison=project["comparison_sha"],
  )

  assert preview["project"] == project["key"]
  assert preview["head_sha"] == project["head_sha"]
  assert preview["diff_truncated"] is False
  assert "diff --git a/index.jsx b/index.jsx" in preview["diff"]
  assert "+export default 3" in preview["diff"]
  assert "diff --git a/new-file.js b/new-file.js" in preview["diff"]
  assert "+export const newFile = true" in preview["diff"]
  assert "diff --git a/.gitignore b/.gitignore" not in preview["diff"]
  assert "diff --git a/ignored.log b/ignored.log" not in preview["diff"]


def test_project_diff_refuses_a_stale_source_map_head():
  repo = _repo("stale-diff-preview")
  project = source_status.build_app_status(_app(repo))
  assert project is not None

  with pytest.raises(RuntimeError, match="source_snapshot_changed"):
    source_status.build_project_diff(
      repo, project, expected_head="0" * 40,
      expected_comparison=project["comparison_sha"],
    )
  with pytest.raises(RuntimeError, match="source_snapshot_changed"):
    source_status.build_project_diff(
      repo, project, expected_head=project["head_sha"],
      expected_comparison="0" * 40,
    )


def test_project_diff_caps_large_source_without_buffering_it(monkeypatch):
  repo = _repo("bounded-diff-preview")
  (repo / "large.js").write_text("x" * 4096, encoding="utf-8")
  project = source_status.build_app_status(_app(repo))
  assert project is not None
  monkeypatch.setattr(source_status, "_DIFF_PREVIEW_BYTES", 256)

  preview = source_status.build_project_diff(
    repo, project, expected_head=project["head_sha"],
    expected_comparison=project["comparison_sha"],
  )

  assert preview["diff_truncated"] is True
  assert len(preview["diff"].encode()) <= 256


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

  preview = source_status.build_project_diff(
    repo, customized, expected_head=customized["head_sha"],
    expected_comparison=customized["comparison_sha"],
  )
  assert "diff --git a/index.jsx b/index.jsx" in preview["diff"]
  assert "diff --git a/runner.sh b/runner.sh" not in preview["diff"]
  assert "diff --git a/.gitignore b/.gitignore" not in preview["diff"]


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
  assert result["reconciliation"]["local_only_paths"] == ["local.js"]
  assert result["reconciliation"]["new_upstream_paths"] == ["remote.js"]
  assert result["reconciliation"]["unresolved_conflict_paths"] == []
  assert result["canonical_repo"] == "example/private-demo"
  payload = repr(result)
  assert str(repo) not in payload
  assert "git@github.com" not in payload


def test_endpoint_equal_sibling_histories_are_semantically_aligned():
  repo = _repo("same-endpoint")
  base = _git(repo, "rev-parse", "HEAD")
  (repo / "index.jsx").write_text("export default 2\n", encoding="utf-8")
  _git(repo, "add", "index.jsx")
  _commit(repo, "local spelling")

  _git(repo, "checkout", "-q", "upstream")
  (repo / "index.jsx").write_text("export default 2\n", encoding="utf-8")
  _git(repo, "add", "index.jsx")
  _commit(repo, "shared spelling")
  _git(repo, "checkout", "-q", "main")

  result = source_status.build_app_status(_app(repo))

  assert result is not None
  assert result["ahead"] == 1
  assert result["behind"] == 1
  assert result["tree"]["files"] == 0
  assert result["reconciliation"]["local_only_count"] == 0
  assert result["reconciliation"]["new_upstream_count"] == 0
  assert result["state"] == "aligned"
  assert _git(repo, "merge-base", "main", "upstream") == base


def test_exact_orphan_origin_tree_suppresses_a_stale_installer_baseline():
  repo = _repo("origin-tree-witness")
  base = _git(repo, "rev-parse", "HEAD")
  _git(repo, "remote", "add", "origin", "https://github.com/example/demo.git")

  (repo / "index.jsx").write_text("export default 2\n", encoding="utf-8")
  _git(repo, "add", "index.jsx")
  _commit(repo, "local projection")

  _git(repo, "checkout", "-q", "--orphan", "shared")
  _git(repo, "rm", "-q", "-r", "--cached", ".")
  (repo / "index.jsx").write_text("export default 2\n", encoding="utf-8")
  _git(repo, "add", "index.jsx")
  shared = _commit(repo, "landed source")
  _git(repo, "update-ref", "refs/remotes/origin/main", shared)
  _git(repo, "checkout", "-q", "main")
  _git(repo, "branch", "-D", "shared")

  result = source_status.build_app_status(_app(repo))

  assert result is not None
  assert result["base_ref"] == "upstream"
  assert result["base_sha"] == base
  assert result["origin"]["head_tree_matches_origin"] is True
  assert result["comparison_ref"] == "origin/main"
  assert result["comparison_sha"] == shared
  assert result["tree"]["files"] == 0
  assert result["reconciliation"]["local_only_count"] == 0
  assert result["reconciliation"]["new_upstream_count"] == 0
  assert result["state"] == "aligned"
  merge_base = subprocess.run(
    ["git", "-C", str(repo), "merge-base", "main", "origin/main"],
    capture_output=True,
    check=False,
  )
  assert merge_base.returncode != 0


def test_exact_origin_commit_never_hides_live_working_files():
  repo = _repo("origin-tree-with-working")
  head = _git(repo, "rev-parse", "HEAD")
  _git(repo, "remote", "add", "origin", "https://github.com/example/demo.git")
  _git(repo, "update-ref", "refs/remotes/origin/main", head)
  (repo / "owner-draft.js").write_text("draft\n", encoding="utf-8")

  result = source_status.build_app_status(_app(repo))

  assert result is not None
  assert result["origin"]["head_tree_matches_origin"] is True
  assert result["comparison_ref"] == "origin/main"
  assert result["state"] == "working"
  assert result["working"]["untracked"] == 1


def test_nonmatching_package_tree_keeps_installer_projection_comparison():
  repo = _repo("origin-package-files")
  base = _git(repo, "rev-parse", "HEAD")
  _git(repo, "remote", "add", "origin", "https://github.com/example/demo.git")
  _git(repo, "checkout", "-q", "-b", "full-source", base)
  (repo / "package-only.md").write_text("not installed\n", encoding="utf-8")
  _git(repo, "add", "package-only.md")
  full_source = _commit(repo, "package source")
  _git(repo, "update-ref", "refs/remotes/origin/main", full_source)
  _git(repo, "checkout", "-q", "main")
  (repo / "owner.js").write_text("owner\n", encoding="utf-8")
  _git(repo, "add", "owner.js")
  _commit(repo, "owner source")

  result = source_status.build_app_status(_app(repo))

  assert result is not None
  assert result["origin"]["head_tree_matches_origin"] is False
  assert result["comparison_ref"] == "upstream"
  assert result["tree"]["paths"][0]["path"] == "owner.js"
  assert "package-only.md" not in repr(result["tree"])


def test_disjoint_same_file_edits_are_classified_as_compatible():
  repo = _repo("compatible-overlap")
  (repo / "index.jsx").write_text(
    "first = 1\nkeep_a = 2\nkeep_b = 3\nlast = 4\n",
    encoding="utf-8",
  )
  _git(repo, "add", "index.jsx")
  _commit(repo, "shared multiline base")
  _git(repo, "branch", "-f", "upstream", "HEAD")

  (repo / "index.jsx").write_text(
    "first = 10\nkeep_a = 2\nkeep_b = 3\nlast = 4\n",
    encoding="utf-8",
  )
  _git(repo, "add", "index.jsx")
  _commit(repo, "local first line")

  _git(repo, "checkout", "-q", "upstream")
  (repo / "index.jsx").write_text(
    "first = 1\nkeep_a = 2\nkeep_b = 3\nlast = 40\n",
    encoding="utf-8",
  )
  _git(repo, "add", "index.jsx")
  _commit(repo, "upstream second line")
  _git(repo, "checkout", "-q", "main")

  result = source_status.build_app_status(_app(repo))

  assert result is not None
  receipt = result["reconciliation"]
  assert receipt["local_only_paths"] == []
  assert receipt["new_upstream_paths"] == []
  assert receipt["compatible_paths"] == ["index.jsx"]
  assert receipt["compatible_count"] == 1
  assert receipt["unresolved_conflict_paths"] == []
  assert result["state"] == "diverged"


def test_managed_classification_does_not_depend_on_the_path_preview(monkeypatch):
  repo = _repo("managed-beyond-preview")
  (repo / "authored.js").write_text("owner\n", encoding="utf-8")
  _git(repo, "add", "authored.js")
  _commit(repo, "owner change")
  (repo / "managed.js").write_text("installer\n", encoding="utf-8")
  _git(repo, "add", "managed.js")
  _commit(repo, "install: adapt package")
  monkeypatch.setattr(source_status, "_PATH_PREVIEW", 1)

  result = source_status.build_app_status(_app(repo))

  assert result is not None
  assert result["tree"]["truncated"] is True
  assert result["tree"]["managed_files"] == 1
  assert result["reconciliation"]["local_only_paths"] == ["authored.js"]
  assert result["reconciliation"]["local_only_count"] == 1


def test_reconciliation_degrades_only_for_expected_runtime_failures(
  monkeypatch,
):
  repo = _repo("reconciliation-failures")

  def expected_failure(*_args, **_kwargs):
    raise RuntimeError("git comparison failed")

  monkeypatch.setattr(app_git, "preview_reconciliation", expected_failure)
  assert source_status._reconciliation_summary(
    repo, "main", "upstream",
  )["available"] is False

  def programming_error(*_args, **_kwargs):
    raise AssertionError("classifier invariant broke")

  monkeypatch.setattr(app_git, "preview_reconciliation", programming_error)
  with pytest.raises(AssertionError, match="classifier invariant broke"):
    source_status._reconciliation_summary(repo, "main", "upstream")


def test_reviewed_shared_change_is_removed_from_remaining_source_inventory():
  repo = _repo("semantic-source-map")
  base = _git(repo, "rev-parse", "upstream")

  (repo / "index.jsx").write_text("export default 'shared'\n", encoding="utf-8")
  _git(repo, "add", "index.jsx")
  reviewed = _commit(repo, "reviewed contribution")
  reviewed_diff = app_git._canonical_diff(repo, base, reviewed)
  assert reviewed_diff is not None
  digest = hashlib.sha256(reviewed_diff).hexdigest()
  assert app_git.record_pending_equivalent_change(
    repo,
    base_sha=base,
    head_sha=reviewed,
    source_sha=reviewed,
    diff_sha256=digest,
    contribution_id="source-map-shared",
  )

  (repo / "index.jsx").write_text(
    "export default 'local followup'\n", encoding="utf-8",
  )
  _git(repo, "add", "index.jsx")
  _commit(repo, "later local work")

  _git(repo, "checkout", "-q", "upstream")
  (repo / "index.jsx").write_text("export default 'shared'\n", encoding="utf-8")
  (repo / "incoming.js").write_text(
    "export default 'incoming'\n", encoding="utf-8",
  )
  _git(repo, "add", ".")
  upstream = _commit(repo, "squash plus independent upstream")
  _git(repo, "checkout", "-q", "main")
  landed = app_git.mark_equivalent_change_landed(
    repo, digest, upstream_sha=upstream,
  )
  assert landed

  result = source_status.build_app_status(_app(repo))

  assert result is not None
  receipt = result["reconciliation"]
  assert receipt["proven_present"] == ["source-map-shared"]
  assert receipt["local_only_paths"] == ["index.jsx"]
  assert receipt["new_upstream_paths"] == ["incoming.js"]
  assert receipt["unresolved_conflict_paths"] == []
  assert result["state"] == "diverged"


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
