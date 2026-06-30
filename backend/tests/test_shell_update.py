"""Shell self-update engine — the git plumbing that two-branch-merges a baked
image shell source into the live ``/data/shell`` without ever recording the
root-owned auth components.

These drive ``shell_update`` against throwaway repos in ``tmp_path`` with the
module's fixed ``/data`` / ``/app`` paths monkeypatched, so no real shell tree is
touched. The load-bearing cases: the baked-source reader excludes the auth
components + dist; a fresh seed reads "not available"; advancing the baked source
makes it available; a clean apply writes the merged source as a linear
single-parent commit and clears availability; and an auth-component change in the
baked source never generates a conflict because it's gitignored out of the model.
"""

import subprocess
from pathlib import Path

import pytest

from app import app_git
from app import shell_update as su


def _git(repo: Path, *args: str) -> str:
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    capture_output=True, text=True, check=True,
  ).stdout


def _write_baked(src: Path, files: dict[str, bytes]) -> None:
  for rel, data in files.items():
    p = src / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def _write_shell(repo: Path, files: dict[str, bytes]) -> None:
  for rel, data in files.items():
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


@pytest.fixture
def shell_env(tmp_path, monkeypatch):
  """A throwaway shell repo + baked source with shell_update's fixed ``/data`` /
  ``/app`` paths retargeted into tmp_path."""
  repo = tmp_path / "shell"
  src = tmp_path / "shell-src"
  src.mkdir(parents=True)
  monkeypatch.setattr(su, "SHELL_REPO", repo)
  monkeypatch.setattr(su, "SHELL_SRC", src)
  monkeypatch.setattr(su, "CONFLICT_FLAG", tmp_path / ".shell-conflict")
  monkeypatch.setattr(su, "REBUILD_FLAG", tmp_path / ".shell-rebuild-needed")
  # current_build_sha reads BUILD_SHA from the env; default to a known value.
  monkeypatch.setenv("BUILD_SHA", "sha-old")
  return repo, src, tmp_path


# The standard auth-component + dist files that must never enter git.
_AUTH_AND_DIST = {
  "src/components/LoginForm/LoginForm.jsx": b"login\n",
  "src/components/SetupWizard/SetupWizard.jsx": b"setup\n",
  "src/components/ProviderAuth/ProviderAuth.jsx": b"provider\n",
  "dist/index.html": b"<html></html>\n",
  "node_modules/dep/index.js": b"vendored\n",
}


def test_baked_shell_files_excludes_auth_components_and_dist(shell_env):
  _, src, _ = shell_env
  _write_baked(src, {
    "src/App.jsx": b"app\n",
    "src/main.jsx": b"main\n",
    **_AUTH_AND_DIST,
  })
  files = su._baked_shell_files(src)
  # Real source is recorded.
  assert files["src/App.jsx"] == b"app\n"
  assert files["src/main.jsx"] == b"main\n"
  # The gitignored set is excluded.
  assert not any(k.startswith("src/components/LoginForm/") for k in files)
  assert not any(k.startswith("src/components/SetupWizard/") for k in files)
  assert not any(k.startswith("src/components/ProviderAuth/") for k in files)
  assert not any(k.startswith("dist/") for k in files)
  assert not any(k.startswith("node_modules/") for k in files)


def test_record_shell_upstream_records_no_auth_component_paths(shell_env):
  repo, src, _ = shell_env
  _write_baked(src, {"src/App.jsx": b"app\n", **_AUTH_AND_DIST})
  # Seed so the repo + branches exist, then inspect the upstream tree.
  su.seed_shell_repo(repo)
  tree = _git(repo, "ls-tree", "-r", "--name-only", app_git.UPSTREAM_BRANCH)
  paths = set(tree.split())
  assert "src/App.jsx" in paths
  assert ".gitignore" in paths
  assert not any(p.startswith("src/components/LoginForm/") for p in paths)
  assert not any(p.startswith("src/components/SetupWizard/") for p in paths)
  assert not any(p.startswith("src/components/ProviderAuth/") for p in paths)
  assert not any(p.startswith("dist/") for p in paths)
  assert not any(p.startswith("node_modules/") for p in paths)


def test_fresh_seed_is_not_available(shell_env):
  repo, src, _ = shell_env
  _write_baked(src, {"src/App.jsx": b"app\n", **_AUTH_AND_DIST})
  # The on-disk shell equals the baked source on a fresh boot.
  _write_shell(repo, {"src/App.jsx": b"app\n", **_AUTH_AND_DIST})

  assert su.seed_shell_repo(repo) is True
  assert su.seed_shell_repo(repo) is False  # idempotent

  status = su.shell_status(repo)
  assert status["available"] is False
  assert status["seed_required"] is False
  assert status["conflict"] is False
  # `main` descends linearly from `upstream` after the seed.
  assert app_git._run(
    repo, "merge-base", "--is-ancestor",
    app_git.UPSTREAM_BRANCH, app_git.LOCAL_BRANCH, check=False,
  ).returncode == 0


def test_seed_preserves_existing_agent_edits(shell_env):
  repo, src, _ = shell_env
  _write_baked(src, {"src/App.jsx": b"baked\n"})
  # An existing instance: the on-disk shell carries an agent edit, no .git yet.
  _write_shell(repo, {"src/App.jsx": b"AGENT EDIT\n"})

  su.seed_shell_repo(repo)

  # The agent's edit is preserved on disk and committed on `main`.
  assert (repo / "src/App.jsx").read_bytes() == b"AGENT EDIT\n"
  main_app = _git(repo, "show", f"{app_git.LOCAL_BRANCH}:src/App.jsx")
  assert main_app == "AGENT EDIT\n"
  # The pristine baked source is on `upstream`.
  up_app = _git(repo, "show", f"{app_git.UPSTREAM_BRANCH}:src/App.jsx")
  assert up_app == "baked\n"


def test_status_available_when_baked_source_advances(shell_env, monkeypatch):
  repo, src, _ = shell_env
  _write_baked(src, {"src/App.jsx": b"v1\n"})
  _write_shell(repo, {"src/App.jsx": b"v1\n"})
  su.seed_shell_repo(repo)
  assert su.shell_status(repo)["available"] is False

  # A new image bakes a newer App.jsx; record it onto `upstream`.
  _write_baked(src, {"src/App.jsx": b"v2\n"})
  monkeypatch.setenv("BUILD_SHA", "sha-new")
  su.record_shell_upstream(repo)

  assert su.shell_status(repo)["available"] is True


def test_apply_clean_writes_merged_source_and_linear_commit(shell_env, monkeypatch):
  repo, src, _ = shell_env
  _write_baked(src, {
    "src/App.jsx": b"line A\nline B\nline C\n",
    "src/util.jsx": b"helper\n",
  })
  _write_shell(repo, {
    "src/App.jsx": b"line A\nline B\nline C\n",
    "src/util.jsx": b"helper\n",
  })
  su.seed_shell_repo(repo)
  # Local edit on `main` (line A), disjoint from the upstream change (line C).
  (repo / "src/App.jsx").write_bytes(b"line A LOCAL\nline B\nline C\n")
  app_git.commit_local(repo, "local edit")
  # New baked source: edits line C + adds a new file.
  _write_baked(src, {
    "src/App.jsx": b"line A\nline B\nline C UPSTREAM\n",
    "src/util.jsx": b"helper\n",
    "src/new.jsx": b"new\n",
  })
  monkeypatch.setenv("BUILD_SHA", "sha-new")

  out = su._apply_sync(repo)

  assert out["state"] == "updated"
  assert out["merge_commit"]
  # The two disjoint edits combined on disk.
  app = (repo / "src/App.jsx").read_bytes()
  assert b"line A LOCAL" in app and b"line C UPSTREAM" in app
  # The upstream-added file landed.
  assert (repo / "src/new.jsx").read_bytes() == b"new\n"
  # The rebuild flag was tripped (hot rebuild, no restart).
  assert su.REBUILD_FLAG.exists()
  # The replay is a SINGLE-parent linear commit on the upstream tip.
  up_tip = _git(repo, "rev-parse", app_git.UPSTREAM_BRANCH).strip()
  parents = _git(repo, "rev-list", "--parents", "-n", "1", app_git.LOCAL_BRANCH).split()
  assert parents[1:] == [up_tip]  # exactly one parent == upstream
  # After apply, `upstream` is an ancestor of `main` → not available.
  assert su.shell_status(repo)["available"] is False


def test_auth_component_change_in_baked_does_not_conflict(shell_env, monkeypatch):
  repo, src, _ = shell_env
  _write_baked(src, {
    "src/App.jsx": b"app\n",
    "src/components/LoginForm/LoginForm.jsx": b"LOGIN OLD\n",
  })
  _write_shell(repo, {
    "src/App.jsx": b"app\n",
    "src/components/LoginForm/LoginForm.jsx": b"LOGIN OLD\n",
  })
  su.seed_shell_repo(repo)
  # The local shell's auth component diverges (root-owned in prod; here just a
  # different on-disk file). It is gitignored, so git never sees it.
  (repo / "src/components/LoginForm/LoginForm.jsx").write_bytes(b"LOGIN LOCAL\n")
  # The new baked image changes BOTH App.jsx (clean) and the auth component.
  _write_baked(src, {
    "src/App.jsx": b"app v2\n",
    "src/components/LoginForm/LoginForm.jsx": b"LOGIN NEW\n",
  })
  monkeypatch.setenv("BUILD_SHA", "sha-new")

  out = su._apply_sync(repo)

  # No conflict — the auth component is out of the git model entirely.
  assert out["state"] == "updated"
  assert not su.CONFLICT_FLAG.exists()
  # The normal file updated; the auth component on disk is left exactly as it
  # was (the image owns it; the merge never touched it).
  assert (repo / "src/App.jsx").read_bytes() == b"app v2\n"
  assert (
    repo / "src/components/LoginForm/LoginForm.jsx"
  ).read_bytes() == b"LOGIN LOCAL\n"


def test_apply_conflict_records_upstream_and_persists_flag(shell_env, monkeypatch):
  repo, src, _ = shell_env
  _write_baked(src, {"src/App.jsx": b"shared line\n"})
  _write_shell(repo, {"src/App.jsx": b"shared line\n"})
  su.seed_shell_repo(repo)
  # Local and upstream edit the SAME line differently -> conflict.
  (repo / "src/App.jsx").write_bytes(b"shared line LOCAL\n")
  app_git.commit_local(repo, "local")
  _write_baked(src, {"src/App.jsx": b"shared line UPSTREAM\n"})
  monkeypatch.setenv("BUILD_SHA", "sha-new")

  out = su._apply_sync(repo)

  assert out["state"] == "conflict"
  assert out["upstream_commit"]  # upstream recorded so the agent can merge it
  assert any("App.jsx" in p for p in out["conflict_paths"])
  # The live worktree is NOT mutated on conflict (old local source keeps serving).
  assert (repo / "src/App.jsx").read_bytes() == b"shared line LOCAL\n"
  assert not su.REBUILD_FLAG.exists()
  # The conflict is persisted so Settings keeps surfacing it across reloads.
  assert su.CONFLICT_FLAG.exists()
  status = su.shell_status(repo)
  assert status["conflict"] is True
  assert status["available"] is False


def test_conflict_flag_roundtrips_chat_id_and_reads_legacy(shell_env):
  su._write_conflict_flag("up-sha", ["src/a.jsx", "src/b.jsx"], "chat-42")
  assert su._read_conflict_flag() == {
    "upstream": "up-sha", "chat_id": "chat-42",
    "paths": ["src/a.jsx", "src/b.jsx"],
  }
  # A flag written before the chat id existed must still parse.
  su.CONFLICT_FLAG.write_text("up-sha\nsrc/a.jsx\nsrc/b.jsx")
  legacy = su._read_conflict_flag()
  assert legacy["chat_id"] is None
  assert legacy["paths"] == ["src/a.jsx", "src/b.jsx"]
