"""Platform self-update engine — the git plumbing that 3-way-merges a baked
image floor into the live ``/data/platform`` without ever writing the
root-owned recovery island.

These drive ``platform_update`` against throwaway repos in ``tmp_path`` with the
module's fixed ``/data`` / ``/app`` paths monkeypatched, so no real platform
tree is touched. The load-bearing cases: a clean disjoint merge writes the
non-protected files and marks restart-needed; a protected path is skipped (it
updates via the image, not the merge); and a same-line conflict records the new
upstream and reports the conflict (the async wrapper opens the resolver chat).
"""

import subprocess
from pathlib import Path

import pytest

from app import platform_update as pu


def _git(repo: Path, *args: str) -> str:
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    capture_output=True, text=True, check=True,
  ).stdout


def _init_platform(repo: Path, build_sha: str, files: dict[str, bytes]) -> None:
  repo.mkdir(parents=True, exist_ok=True)
  _git(repo, "init", "-q", "-b", "main")
  _git(repo, "config", "user.name", "t")
  _git(repo, "config", "user.email", "t@t")
  (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n")
  for rel, data in files.items():
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
  _git(repo, "add", "-A")
  _git(repo, "commit", "-q", "-m", "init")
  _git(repo, "tag", f"baked-{build_sha}", "HEAD")


def _write_baked(baked: Path, files: dict[str, bytes]) -> None:
  for rel, data in files.items():
    p = baked / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


@pytest.fixture
def platform_env(tmp_path, monkeypatch):
  """A throwaway platform repo + baked floor with all of platform_update's
  fixed ``/data`` / ``/app`` paths retargeted into tmp_path."""
  repo = tmp_path / "platform"
  baked = tmp_path / "baked"
  (baked / "app").mkdir(parents=True)
  (baked / "scripts").mkdir(parents=True)
  monkeypatch.setattr(pu, "PLATFORM_REPO", repo)
  monkeypatch.setattr(pu, "BAKED_APP", baked / "app")
  monkeypatch.setattr(pu, "BAKED_SCRIPTS", baked / "scripts")
  monkeypatch.setattr(pu, "PROTECTED_LIST", tmp_path / "protected.txt")
  monkeypatch.setattr(pu, "UPGRADE_FLAG", tmp_path / ".upgrade")
  monkeypatch.setattr(pu, "RESTART_NEEDED_FLAG", tmp_path / ".restart")
  monkeypatch.setattr(pu, "CONFLICT_FLAG", tmp_path / ".conflict")
  monkeypatch.setattr(pu, "APPLYING_FLAG", tmp_path / ".applying")
  (tmp_path / "protected.txt").write_text("")
  monkeypatch.setenv("BUILD_SHA", "")  # default; tests set the new sha
  return repo, baked, tmp_path


def test_clean_apply_writes_nonprotected_files_and_marks_restart(platform_env, monkeypatch):
  repo, baked, root = platform_env
  _init_platform(repo, "sha-old", {
    "app/server.py": b"line A\nline B\nline C\n",
    "app/util.py": b"helper\n",
    "scripts/run.sh": b"#!/bin/sh\necho old\n",
  })
  # Local edit on main (line A), disjoint from the upstream change (line C).
  (repo / "app/server.py").write_bytes(b"line A LOCAL\nline B\nline C\n")
  _git(repo, "commit", "-qam", "local edit")
  # New baked floor (the new image): edits line C + adds a new file.
  _write_baked(baked, {
    "app/server.py": b"line A\nline B\nline C UPSTREAM\n",
    "app/util.py": b"helper\n",
    "app/new_feature.py": b"new\n",
    "scripts/run.sh": b"#!/bin/sh\necho old\n",
  })
  monkeypatch.setenv("BUILD_SHA", "sha-new")

  out = pu._apply_sync(repo)

  assert out["state"] == "restart_needed"
  assert out["merge_commit"]
  # The 3-way merge combined both disjoint edits on disk.
  server = (repo / "app/server.py").read_bytes()
  assert b"line A LOCAL" in server and b"line C UPSTREAM" in server
  # The upstream-added file landed.
  assert (repo / "app/new_feature.py").read_bytes() == b"new\n"
  # Restart flag set; upstream advanced and tagged.
  assert pu.RESTART_NEEDED_FLAG.exists()
  assert "baked-sha-new" in _git(repo, "tag", "--list", "baked-*")


def test_protected_file_is_skipped_on_clean_apply(platform_env, monkeypatch):
  repo, baked, root = platform_env
  # Mark app/server.py protected (root-owned recovery-island analogue).
  (root / "protected.txt").write_text("/app/app/server.py\n")
  _init_platform(repo, "sha-old", {
    "app/server.py": b"PROTECTED OLD\n",
    "app/util.py": b"util old\n",
  })
  # New image changes BOTH the protected file and a normal file. Local clean.
  _write_baked(baked, {
    "app/server.py": b"PROTECTED NEW\n",
    "app/util.py": b"util new\n",
  })
  monkeypatch.setenv("BUILD_SHA", "sha-new")

  out = pu._apply_sync(repo)

  assert out["state"] == "restart_needed"
  # Normal file updated; protected file left untouched (image owns it).
  assert (repo / "app/util.py").read_bytes() == b"util new\n"
  assert (repo / "app/server.py").read_bytes() == b"PROTECTED OLD\n"


def test_conflict_records_upstream_and_reports_paths(platform_env, monkeypatch):
  repo, baked, root = platform_env
  _init_platform(repo, "sha-old", {"app/server.py": b"shared line\n"})
  # Local and upstream edit the SAME line differently -> conflict.
  (repo / "app/server.py").write_bytes(b"shared line LOCAL\n")
  _git(repo, "commit", "-qam", "local")
  _write_baked(baked, {"app/server.py": b"shared line UPSTREAM\n"})
  monkeypatch.setenv("BUILD_SHA", "sha-new")

  out = pu._apply_sync(repo)

  assert out["state"] == "conflict"
  assert out["upstream_commit"]  # upstream recorded so the agent can merge it
  assert any("server.py" in p for p in out["conflict_paths"])
  # The live worktree is NOT mutated on conflict (old local code keeps serving).
  assert (repo / "app/server.py").read_bytes() == b"shared line LOCAL\n"
  assert not pu.RESTART_NEEDED_FLAG.exists()
  # The conflict is persisted so Settings keeps surfacing it across reloads.
  assert pu.CONFLICT_FLAG.exists()
  status = pu.platform_status(repo)
  assert status["state"] == pu.PlatformUpdateState.CONFLICT.value


def test_conflict_flag_roundtrips_chat_id_and_reads_legacy(platform_env):
  pu._write_conflict_flag("up-sha", ["app/a.py", "app/b.py"], "chat-42")
  assert pu._read_conflict_flag() == {
    "upstream": "up-sha", "chat_id": "chat-42",
    "paths": ["app/a.py", "app/b.py"],
  }
  # A flag written before the chat id existed (no `chat:` line) must still
  # parse: chat_id None, paths intact — the format is backward compatible.
  pu.CONFLICT_FLAG.write_text("up-sha\napp/a.py\napp/b.py")
  legacy = pu._read_conflict_flag()
  assert legacy["chat_id"] is None
  assert legacy["paths"] == ["app/a.py", "app/b.py"]


def test_status_surfaces_conflict_chat_id(platform_env):
  repo, baked, root = platform_env
  _init_platform(repo, "sha-old", {"app/server.py": b"x\n"})
  # The recorded resolver chat must reach the owner from a Settings reload.
  pu._write_conflict_flag("up-sha", ["app/server.py"], "chat-99")
  status = pu.platform_status(repo)
  assert status["state"] == pu.PlatformUpdateState.CONFLICT.value
  assert status["conflict_chat_id"] == "chat-99"
  assert any("server.py" in p for p in status["conflict_paths"])


def test_status_non_conflict_has_null_chat_id(platform_env, monkeypatch):
  repo, baked, root = platform_env
  _init_platform(repo, "sha-old", {"app/server.py": b"x\n"})
  monkeypatch.setenv("BUILD_SHA", "sha-old")  # up to date, no conflict flag
  status = pu.platform_status(repo)
  assert status["conflict_chat_id"] is None


def test_restamp_chat_id_preserves_existing_conflict_data(platform_env):
  # A second apply that BAILS on a pre-existing flag-only conflict carries no
  # fresh upstream and an empty path list (the conflict isn't materialised in
  # git, so _unmerged_paths is []). Stamping the resolver chat id must fall back
  # to the recorded flag for both, never clobbering the good upstream/paths.
  pu._write_conflict_flag("good-upstream", ["app/a.py", "app/b.py"])
  existing = pu._read_conflict_flag()
  pu._write_conflict_flag(
    None or existing["upstream"],          # outcome.upstream_commit is None
    [] or existing["paths"] or [],          # outcome.conflict_paths is []
    "chat-77",
  )
  got = pu._read_conflict_flag()
  assert got["upstream"] == "good-upstream"
  assert got["paths"] == ["app/a.py", "app/b.py"]
  assert got["chat_id"] == "chat-77"


def test_status_reports_available_when_image_sha_advances(platform_env, monkeypatch):
  repo, baked, root = platform_env
  _init_platform(repo, "sha-old", {"app/server.py": b"x\n"})
  monkeypatch.setenv("BUILD_SHA", "sha-new")

  status = pu.platform_status(repo)

  assert status["available"] is True
  assert status["state"] == pu.PlatformUpdateState.AVAILABLE.value
  assert status["seed_required"] is True  # no upstream branch yet
  assert status["recorded_upstream_sha"] == "sha-old"
  assert status["current_build_sha"] == "sha-new"


def test_apply_works_on_default_branch_without_baked_tag(platform_env, monkeypatch):
  """Regression (caught in the container): entrypoint inits /data/platform with
  a bare `git init` (default branch often `master`, not `main`) and, when
  BUILD_SHA is unknown, writes NO `baked-*` tag. The engine must detect the real
  branch and fall back to the root commit as the seed base."""
  repo, baked, root = platform_env
  repo.mkdir(parents=True)
  subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)  # default branch
  subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
  subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
  (repo / ".gitignore").write_text("__pycache__/\n")
  (repo / "app").mkdir()
  (repo / "app/server.py").write_bytes(b"old\n")
  subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
  subprocess.run(
    ["git", "-C", str(repo), "commit", "-qm", "init: platform layer from baked image floor"],
    check=True,
  )
  # No baked-* tag at all; a local non-protected edit.
  (repo / "app/server.py").write_bytes(b"old LOCAL\n")
  subprocess.run(["git", "-C", str(repo), "commit", "-qam", "edit"], check=True)
  _write_baked(baked, {"app/server.py": b"old\n", "app/added.py": b"added\n"})
  monkeypatch.setenv("BUILD_SHA", "sha-new")

  out = pu._apply_sync(repo)

  assert out["state"] == "restart_needed"
  assert (repo / "app/added.py").read_bytes() == b"added\n"
  assert (repo / "app/server.py").read_bytes() == b"old LOCAL\n"  # local edit preserved
  assert pu._has_branch("upstream", repo)


def test_status_up_to_date_when_shas_match(platform_env, monkeypatch):
  repo, baked, root = platform_env
  _init_platform(repo, "sha-x", {"app/server.py": b"x\n"})
  monkeypatch.setenv("BUILD_SHA", "sha-x")

  status = pu.platform_status(repo)

  assert status["available"] is False
  assert status["state"] == pu.PlatformUpdateState.UP_TO_DATE.value
