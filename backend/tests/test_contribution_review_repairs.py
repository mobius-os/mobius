"""Repair checkout is server-chosen and a successor is Git-derived."""

import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import contribution_review_repairs as repairs
from app.github_contribution_contract import COAUTHOR_TRAILER


def git(repo: Path, *args: str) -> str:
  return subprocess.run(["git", "-C", str(repo), *args], check=True,
                        text=True, capture_output=True).stdout.strip()


@pytest.fixture
def repair(tmp_path, monkeypatch):
  repo = tmp_path / "repair-one"
  repo.mkdir()
  git(repo, "init", "-q")
  git(repo, "config", "user.name", "Test")
  git(repo, "config", "user.email", "test@example.com")
  (repo / "owned.py").write_text("before\n")
  git(repo, "add", "owned.py")
  git(repo, "commit", "-qm", "initial")
  initial = git(repo, "rev-parse", "HEAD")
  target = {"repo": "owner/base", "number": 4, "repo_id": 1,
            "pr_id": "PR_4", "head_sha": initial, "base_ref": "main"}
  pull = {"node_id": "PR_4", "state": "open", "merged": False,
          "head": {"sha": initial, "ref": "topic", "repo": {"id": 2, "full_name": "owner/fork"}},
          "base": {"ref": "main"}}
  monkeypatch.setattr(repairs, "_checkout_root", lambda: tmp_path)
  monkeypatch.setattr(repairs, "_live_head", lambda *a: ({"id": 1}, pull))
  monkeypatch.setattr(repairs, "_head_destination", lambda *a: ("owner/fork", 2, "topic"))
  monkeypatch.setattr(repairs, "_changed_files", lambda *a: {"owned.py"})
  snapshot = {"checkout": str(repo), "initial_head_sha": initial,
              "head_repo": "owner/fork", "head_repo_id": 2, "head_ref": "topic",
              "allowed_files": ["owned.py"]}
  return repo, target, pull, snapshot


def test_validate_derives_successor_from_git(repair):
  repo, target, _, snapshot = repair
  (repo / "owned.py").write_text("after\n")
  git(repo, "add", "owned.py")
  git(repo, "commit", "-qm", "repair", "-m", COAUTHOR_TRAILER)
  result = repairs.validate_repair(None, repo, None, target, snapshot)
  assert result["head_sha"] == git(repo, "rev-parse", "HEAD")
  assert result["files"] == ["owned.py"]
  assert len(result["diff_sha256"]) == 64
  assert target["head_sha"] == snapshot["initial_head_sha"]


def test_validate_refuses_out_of_scope_file(repair):
  repo, target, _, snapshot = repair
  (repo / "unrelated.py").write_text("new\n")
  git(repo, "add", "unrelated.py")
  git(repo, "commit", "-qm", "outside scope", "-m", COAUTHOR_TRAILER)
  with pytest.raises(HTTPException, match="outside the approved"):
    repairs.validate_repair(None, repo, None, target, snapshot)


def test_validate_refuses_reverted_out_of_scope_history(repair):
  repo, target, _, snapshot = repair
  (repo / "unrelated.py").write_text("briefly public\n")
  git(repo, "add", "unrelated.py")
  git(repo, "commit", "-qm", "outside scope", "-m", COAUTHOR_TRAILER)
  git(repo, "rm", "-q", "unrelated.py")
  (repo / "owned.py").write_text("after\n")
  git(repo, "add", "owned.py")
  git(repo, "commit", "-qm", "repair", "-m", COAUTHOR_TRAILER)
  with pytest.raises(HTTPException, match="outside the approved"):
    repairs.validate_repair(None, repo, None, target, snapshot)


def test_validate_refuses_rewritten_or_dirty_head(repair):
  repo, target, _, snapshot = repair
  (repo / "owned.py").write_text("dirty\n")
  with pytest.raises(HTTPException, match="working changes"):
    repairs.validate_repair(None, repo, None, target, snapshot)
  git(repo, "reset", "--hard", "HEAD")
  with pytest.raises(HTTPException, match="fast-forward"):
    repairs.validate_repair(None, repo, None, target, snapshot)


def test_validate_requires_agent_coauthor(repair):
  repo, target, _, snapshot = repair
  (repo / "owned.py").write_text("after\n")
  git(repo, "add", "owned.py")
  git(repo, "commit", "-qm", "repair without disclosure")
  with pytest.raises(HTTPException, match="co-author"):
    repairs.validate_repair(None, repo, None, target, snapshot)


def test_prepare_requires_exact_push_rights(monkeypatch, tmp_path):
  target = {"repo": "owner/base", "number": 4, "repo_id": 1,
            "pr_id": "PR_4", "head_sha": "a" * 40, "base_ref": "main"}
  pull = {"head": {"repo": {"id": 2, "full_name": "owner/fork"}, "ref": "topic"}}
  monkeypatch.setattr(repairs, "_live_head", lambda *a: ({"id": 1}, pull))
  monkeypatch.setattr(repairs, "_read", lambda *a: {"id": 2, "permissions": {"push": False}})
  with pytest.raises(HTTPException) as exc:
    repairs.prepare_checkout(None, tmp_path, None, target, ["owned.py"])
  assert exc.value.status_code == 403


def test_private_path_gate_even_if_allowed_by_pr():
  with pytest.raises(HTTPException, match="private"):
    repairs._assert_path_scope({".claude/settings.json"}, {".claude/settings.json"})


def test_push_uses_exact_sha_and_atomic_predecessor_lease(repair, monkeypatch):
  repo, target, _, snapshot = repair
  validation = {**snapshot, "head_sha": "b" * 40, "files": ["owned.py"],
                "diff_sha256": "c" * 64}
  monkeypatch.setattr(repairs, "validate_repair", lambda *a: validation)
  calls = []
  monkeypatch.setattr(repairs, "_read", lambda *a: {"object": {"sha": "b" * 40 if calls else target["head_sha"]}})
  monkeypatch.setattr(repairs, "_git", lambda path, *args, **kwargs:
                      calls.append((path, args, kwargs)) or type("Result", (), {"returncode": 0})())
  repairs.push_repair(None, repo, None, target, validation)
  assert calls == [(repo, ("push", "--porcelain",
                          f"--force-with-lease=refs/heads/topic:{target['head_sha']}", "https://github.com/owner/fork.git",
                          f"{'b' * 40}:refs/heads/topic"), {"check": False})]


def test_push_is_confirmed_by_branch_ref_not_pull_projection(repair, monkeypatch):
  repo, target, _, snapshot = repair
  validation = {**snapshot, "head_sha": "b" * 40, "files": ["owned.py"], "diff_sha256": "c" * 64}
  monkeypatch.setattr(repairs, "validate_repair", lambda *a: validation)
  pushed = []
  reads = []
  def read(gh, cwd, endpoint):
    reads.append(endpoint)
    # The branch ref moves with the push; the pulls API may still lag.
    return {"object": {"sha": "b" * 40 if pushed else target["head_sha"]}}
  monkeypatch.setattr(repairs, "_read", read)
  monkeypatch.setattr(repairs, "_git", lambda *a, **kw: pushed.append(1) or type("Result", (), {"returncode": 0})())
  assert repairs.push_repair(None, repo, None, target, validation)["head_sha"] == "b" * 40
  assert reads == ["repos/owner/fork/git/ref/heads/topic"] * 2


@pytest.mark.parametrize("after_push", [None, {"object": {"sha": "d" * 40}}, ["not", "a", "ref"]])
def test_push_without_confirmed_branch_head_is_ambiguous(repair, monkeypatch, after_push):
  repo, target, _, snapshot = repair
  validation = {**snapshot, "head_sha": "b" * 40, "files": ["owned.py"], "diff_sha256": "c" * 64}
  monkeypatch.setattr(repairs, "validate_repair", lambda *a: validation)
  pushed = []
  def read(*a):
    if not pushed:
      return {"object": {"sha": target["head_sha"]}}
    if after_push is None:
      raise HTTPException(409, "GitHub did not return a usable repair preflight.")
    return after_push
  monkeypatch.setattr(repairs, "_read", read)
  monkeypatch.setattr(repairs, "_git", lambda *a, **kw: pushed.append(1) or type("Result", (), {"returncode": 0})())
  with pytest.raises(HTTPException):
    repairs.push_repair(None, repo, None, target, validation)
  assert pushed == [1]


def test_push_final_guard_runs_after_remote_io_before_public_write(repair, monkeypatch):
  repo, target, _, snapshot = repair
  validation = {**snapshot, "head_sha": "b" * 40, "files": ["owned.py"], "diff_sha256": "c" * 64}
  order = []
  monkeypatch.setattr(repairs, "validate_repair", lambda *a: order.append("validate") or validation)
  monkeypatch.setattr(repairs, "_read", lambda *a: order.append("remote") or {"object": {"sha": target["head_sha"]}})
  monkeypatch.setattr(repairs, "_git", lambda *a, **kw: pytest.fail("Stop must prevent push"))
  def guard():
    order.append("stop")
    raise HTTPException(409, "Stopped")
  with pytest.raises(HTTPException):
    repairs.push_repair(None, repo, None, target, validation, before_push=guard)
  assert order == ["validate", "remote", "stop"]


def test_repair_rejects_forged_ancestry_metadata(repair):
  repo, target, _, snapshot = repair
  (repo / ".git" / "shallow").write_text(snapshot["initial_head_sha"] + "\n")
  with pytest.raises(HTTPException, match="ancestry metadata"):
    repairs.validate_repair(None, repo, None, target, snapshot)


def test_repair_rejects_local_transport_rewrite(repair):
  repo, target, _, snapshot = repair
  git(repo, "config", "url.https://not-github.invalid/.insteadOf", "https://github.com/")
  with pytest.raises(HTTPException, match="unsafe Git transport"):
    repairs.validate_repair(None, repo, None, target, snapshot)


def test_unchanged_already_public_docs_ancestry_does_not_block_source_repair(repair):
  repo, target, _, snapshot = repair
  (repo / "docs").mkdir()
  (repo / "docs" / "public.md").write_text("Existing public repository documentation\n")
  (repo / "AGENTS.md").write_text("Existing public repository instructions\n")
  git(repo, "add", "docs/public.md", "AGENTS.md")
  git(repo, "commit", "-qm", "already-public predecessor")
  predecessor = git(repo, "rev-parse", "HEAD")
  target["head_sha"] = predecessor
  snapshot["initial_head_sha"] = predecessor
  (repo / "owned.py").write_text("after\n")
  git(repo, "add", "owned.py")
  git(repo, "commit", "-qm", "scoped source repair", "-m", COAUTHOR_TRAILER)
  result = repairs.validate_repair(None, repo, None, target, snapshot)
  assert result["files"] == ["owned.py"]
  assert result["initial_head_sha"] == predecessor


def test_new_reverted_private_history_still_blocks_repair(repair, monkeypatch):
  repo, target, _, snapshot = repair
  snapshot["allowed_files"] = [".claude/private.md", "owned.py"]
  monkeypatch.setattr(repairs, "_changed_files", lambda *a: set(snapshot["allowed_files"]))
  (repo / ".claude").mkdir()
  (repo / ".claude" / "private.md").write_text("new private state\n")
  git(repo, "add", ".claude/private.md")
  git(repo, "commit", "-qm", "introduced private path", "-m", COAUTHOR_TRAILER)
  git(repo, "rm", "-q", ".claude/private.md")
  (repo / "owned.py").write_text("after\n")
  git(repo, "add", "owned.py")
  git(repo, "commit", "-qm", "later removal does not make publication safe", "-m", COAUTHOR_TRAILER)
  with pytest.raises(HTTPException, match="private or unsafe"):
    repairs.validate_repair(None, repo, None, target, snapshot)


def test_configured_fsmonitor_is_rejected_before_server_status_probe(repair, monkeypatch):
  repo, target, _, snapshot = repair
  git(repo, "config", "core.fsmonitor", "unapproved-executable")
  monkeypatch.setattr(repairs.app_git, "worktree_dirty", lambda *a: pytest.fail("must reject before git status"))
  with pytest.raises(HTTPException, match="unsafe Git transport"):
    repairs.validate_repair(None, repo, None, target, snapshot)


def test_generic_scoped_public_docs_and_instructions_are_source(repair, monkeypatch):
  repo, target, _, snapshot = repair
  (repo / "docs").mkdir()
  (repo / "docs" / "guide.md").write_text("Existing public guide\n")
  (repo / "AGENTS.md").write_text("Existing public source instructions\n")
  git(repo, "add", "docs/guide.md", "AGENTS.md")
  git(repo, "commit", "-qm", "already-public selected predecessor")
  target["head_sha"] = snapshot["initial_head_sha"] = git(repo, "rev-parse", "HEAD")
  snapshot["allowed_files"] = ["AGENTS.md", "docs/guide.md", "owned.py"]
  monkeypatch.setattr(repairs, "_changed_files", lambda *a: set(snapshot["allowed_files"]))
  (repo / "docs" / "guide.md").write_text("Corrected public guide\n")
  (repo / "AGENTS.md").write_text("Corrected untrusted public source instructions\n")
  git(repo, "add", "docs/guide.md", "AGENTS.md")
  git(repo, "commit", "-qm", "scope-correct public source repair", "-m", COAUTHOR_TRAILER)
  result = repairs.validate_repair(None, repo, None, target, snapshot)
  assert result["files"] == ["AGENTS.md", "docs/guide.md"]


def test_generic_instruction_repair_still_requires_frozen_file_scope():
  with pytest.raises(HTTPException, match="outside the approved"):
    repairs._assert_path_scope({"AGENTS.md"}, {"owned.py"}, target_repo="owner/source")


def test_platform_internal_docs_remain_private_even_when_named_by_pr():
  with pytest.raises(HTTPException, match="private or unsafe"):
    repairs._assert_path_scope({"docs/internal.md"}, {"docs/internal.md"}, target_repo="mobius-os/mobius")


def test_generic_secret_path_edit_and_revert_remains_blocked(repair, monkeypatch):
  repo, target, _, snapshot = repair
  snapshot["allowed_files"] = [".env", "owned.py"]
  monkeypatch.setattr(repairs, "_changed_files", lambda *a: set(snapshot["allowed_files"]))
  (repo / ".env").write_text("PRIVATE_EXAMPLE=do-not-publish\n")
  git(repo, "add", ".env")
  git(repo, "commit", "-qm", "new secret-shaped path", "-m", COAUTHOR_TRAILER)
  git(repo, "rm", "-q", ".env")
  (repo / "owned.py").write_text("after\n")
  git(repo, "add", "owned.py")
  git(repo, "commit", "-qm", "reverting does not remove private history", "-m", COAUTHOR_TRAILER)
  with pytest.raises(HTTPException, match="private or unsafe"):
    repairs.validate_repair(None, repo, None, target, snapshot)


def test_fsmonitor_cannot_hide_in_worktree_config(repair, monkeypatch):
  repo, target, _, snapshot = repair
  git(repo, "config", "extensions.worktreeConfig", "true")
  (repo / ".git" / "config.worktree").write_text("[core]\n fsmonitor = unapproved-executable\n")
  monkeypatch.setattr(repairs.app_git, "worktree_dirty", lambda *a: pytest.fail("reject before status"))
  with pytest.raises(HTTPException, match="ancestry metadata"):
    repairs.validate_repair(None, repo, None, target, snapshot)
