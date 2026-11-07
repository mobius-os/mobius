"""Scoped takeover may merge only the verified live target tip into a PR.

The merge is the in-grant way to bring a published branch up to date: no
rebase, no force-push, no private or unrelated history. Commits it imports are
already public on the target branch; everything it authors stays inside the
frozen PR file scope, and its net result differs from that base only there.
"""
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import contribution_review_repairs as repairs
from app.github_contribution_contract import COAUTHOR_TRAILER


def git(repo: Path, *args: str) -> str:
  return subprocess.run(["git", "-C", str(repo), *args], check=True,
                        text=True, capture_output=True).stdout.strip()


def commit(repo: Path, message: str, files: dict[str, str | None], *, trailer=True) -> str:
  for name, content in files.items():
    path = repo / name
    if content is None:
      git(repo, "rm", "-q", name)
      continue
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    git(repo, "add", name)
  git(repo, "commit", "-qm", message, *(("-m", COAUTHOR_TRAILER) if trailer else ()))
  return git(repo, "rev-parse", "HEAD")


def merge(repo: Path, *others: str, trailer=True, resolve: dict[str, str] | None = None) -> str:
  message = ["-m", "Merge target branch", *(("-m", COAUTHOR_TRAILER) if trailer else ())]
  result = subprocess.run(["git", "-C", str(repo), "merge", "--no-ff", "-q", *message, *others],
                          text=True, capture_output=True)
  if result.returncode != 0:
    for name, content in (resolve or {}).items():
      (repo / name).write_text(content)
      git(repo, "add", name)
    git(repo, "commit", "-q", "--no-edit")
  return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def pr(tmp_path, monkeypatch):
  """A PR touching owned.py, and a target branch that moved on other files."""
  repo = tmp_path / "repair-merge"
  repo.mkdir()
  git(repo, "init", "-q", "-b", "main")
  git(repo, "config", "user.name", "Test")
  git(repo, "config", "user.email", "test@example.com")
  root = commit(repo, "initial", {"owned.py": "one\ntwo\n", "shared.py": "upstream\n"}, trailer=False)
  git(repo, "checkout", "-q", "-b", "topic")
  head = commit(repo, "the pull request", {"owned.py": "one\nPR\n"}, trailer=False)
  git(repo, "checkout", "-q", "main")
  base = commit(repo, "target moved", {"shared.py": "upstream v2\n", "README.md": "public\n"}, trailer=False)
  git(repo, "checkout", "-q", "topic")
  target = {"repo": "owner/base", "number": 4, "repo_id": 1, "pr_id": "PR_4",
            "head_sha": head, "base_ref": "main"}
  pull = {"node_id": "PR_4", "state": "open", "merged": False,
          "head": {"sha": head, "ref": "topic", "repo": {"id": 2, "full_name": "owner/fork"}},
          "base": {"ref": "main"}}
  monkeypatch.setattr(repairs, "_checkout_root", lambda: tmp_path)
  monkeypatch.setattr(repairs, "_live_head", lambda *a: ({"id": 1}, pull))
  monkeypatch.setattr(repairs, "_head_destination", lambda *a: ("owner/fork", 2, "topic"))
  monkeypatch.setattr(repairs, "_changed_files", lambda *a: {"owned.py"})
  snapshot = {"checkout": str(repo), "initial_head_sha": head, "head_repo": "owner/fork",
              "head_repo_id": 2, "head_ref": "topic", "allowed_files": ["owned.py"]}
  return repo, target, snapshot, {"root": root, "head": head, "base": base}


def validate(pr_fixture, base_sha):
  repo, target, snapshot, _ = pr_fixture
  return repairs.validate_repair(None, repo, None, target, snapshot, base_sha=base_sha)


def test_clean_merge_of_the_verified_live_tip_is_a_scoped_successor(pr):
  repo, _target, _snapshot, shas = pr
  head = merge(repo, shas["base"])
  result = validate(pr, shas["base"])
  assert result["head_sha"] == head and result["merged_base_sha"] == shas["base"]
  # Imported target commits are public; the authored scope is the PR's own file.
  assert result["files"] == ["owned.py"]


def test_conflict_resolution_inside_the_frozen_scope_is_accepted(pr):
  repo, _target, _snapshot, shas = pr
  git(repo, "checkout", "-q", "main")
  base = commit(repo, "target edits the same file", {"owned.py": "one\nupstream\n"}, trailer=False)
  git(repo, "checkout", "-q", "topic")
  merge(repo, base, resolve={"owned.py": "one\nPR and upstream\n"})
  follow_up = commit(repo, "address review after update", {"owned.py": "one\nPR and upstream, reviewed\n"})
  result = validate(pr, base)
  assert result["head_sha"] == follow_up and result["files"] == ["owned.py"]


def test_imported_public_paths_that_look_private_do_not_block_the_update(pr):
  repo, _target, _snapshot, shas = pr
  git(repo, "checkout", "-q", "main")
  base = commit(repo, "upstream keeps its own tool config", {".claude/settings.json": "{}\n"}, trailer=False)
  git(repo, "checkout", "-q", "topic")
  merge(repo, base)
  assert validate(pr, base)["merged_base_sha"] == base


def test_merge_is_refused_without_a_verified_live_base(pr):
  repo, _target, _snapshot, shas = pr
  merge(repo, shas["base"])
  # Without the verified tip every imported commit counts as authored work.
  with pytest.raises(HTTPException):
    validate(pr, None)


def test_merge_of_anything_but_the_exact_live_tip_is_refused(pr):
  repo, _target, _snapshot, shas = pr
  git(repo, "checkout", "-q", "main")
  newer = commit(repo, "target moved again", {"README.md": "public v2\n"}, trailer=False)
  git(repo, "checkout", "-q", "topic")
  merge(repo, shas["base"])  # an older public tip, not the live one
  with pytest.raises(HTTPException, match="other merges are not accepted"):
    validate(pr, newer)


def test_merge_of_an_unpublished_side_branch_is_refused(pr):
  repo, _target, _snapshot, shas = pr
  git(repo, "checkout", "-q", "-b", "private-side", shas["base"])
  side = commit(repo, "never published", {"secret_plan.py": "private\n"})
  git(repo, "checkout", "-q", "topic")
  merge(repo, side)
  with pytest.raises(HTTPException, match="other merges are not accepted"):
    validate(pr, shas["base"])


def test_octopus_third_parent_is_refused(pr):
  repo, _target, _snapshot, shas = pr
  git(repo, "checkout", "-q", "-b", "other", shas["root"])
  other = commit(repo, "another line", {"other.py": "x\n"})
  git(repo, "checkout", "-q", "topic")
  merge(repo, shas["base"], other)
  with pytest.raises(HTTPException, match="other merges are not accepted"):
    validate(pr, shas["base"])


def test_reversed_parent_order_is_refused(pr):
  repo, _target, snapshot, shas = pr
  git(repo, "checkout", "-q", "-b", "reversed", shas["base"])
  merge(repo, shas["head"])
  git(repo, "branch", "-f", "topic", "HEAD")
  git(repo, "checkout", "-q", "topic")
  with pytest.raises(HTTPException):
    validate(pr, shas["base"])


def test_off_scope_conflict_resolution_is_refused(pr):
  repo, _target, _snapshot, shas = pr
  git(repo, "checkout", "-q", "main")
  base = commit(repo, "target edits both", {"owned.py": "one\nupstream\n", "shared.py": "upstream v3\n"}, trailer=False)
  git(repo, "checkout", "-q", "topic")
  merge(repo, base, resolve={"owned.py": "one\nresolved\n"})
  # Amend the merge to also rewrite a file outside the frozen PR scope.
  (repo / "shared.py").write_text("quietly reverted upstream\n")
  git(repo, "add", "shared.py")
  git(repo, "commit", "-q", "--amend", "--no-edit")
  with pytest.raises(HTTPException, match="outside the approved"):
    validate(pr, base)


def test_dropping_an_upstream_file_in_the_merge_is_refused(pr):
  repo, _target, _snapshot, shas = pr
  merge(repo, shas["base"])
  git(repo, "rm", "-q", "README.md")
  git(repo, "commit", "-q", "--amend", "--no-edit")
  with pytest.raises(HTTPException, match="outside the approved"):
    validate(pr, shas["base"])


def test_follow_up_that_reverts_upstream_outside_scope_is_refused(pr):
  repo, _target, _snapshot, shas = pr
  merge(repo, shas["base"])
  commit(repo, "undo upstream", {"shared.py": "upstream\n"})
  with pytest.raises(HTTPException, match="outside the approved"):
    validate(pr, shas["base"])


@pytest.mark.parametrize("private", [".env", ".claude/private.md"])
def test_reverted_private_path_before_the_merge_still_blocks(pr, monkeypatch, private):
  repo, _target, snapshot, shas = pr
  snapshot["allowed_files"] = sorted({private, "owned.py"})
  monkeypatch.setattr(repairs, "_changed_files", lambda *a: set(snapshot["allowed_files"]))
  commit(repo, "introduce private", {private: "PRIVATE=1\n"})
  commit(repo, "remove it again", {private: None})
  merge(repo, shas["base"])
  with pytest.raises(HTTPException, match="private or unsafe"):
    validate(pr, shas["base"])


def test_only_one_merge_of_the_target_tip_is_accepted(pr):
  repo, _target, _snapshot, shas = pr
  merge(repo, shas["base"])
  commit(repo, "in scope", {"owned.py": "one\nPR v2\n"})
  # Git itself refuses a redundant merge; build the second merge object directly.
  again = git(repo, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-p", shas["base"],
              "-m", "merge the same tip again", "-m", COAUTHOR_TRAILER)
  git(repo, "reset", "-q", "--hard", again)
  with pytest.raises(HTTPException, match="only once"):
    validate(pr, shas["base"])


def test_merge_commit_needs_the_agent_co_author_trailer(pr):
  repo, _target, _snapshot, shas = pr
  merge(repo, shas["base"], trailer=False)
  with pytest.raises(HTTPException, match="co-author"):
    validate(pr, shas["base"])


def test_base_object_missing_from_the_checkout_is_not_trusted(pr):
  repo, _target, _snapshot, shas = pr
  merge(repo, shas["base"])
  with pytest.raises(HTTPException):
    validate(pr, "9" * 40)


def test_replaced_ancestry_cannot_forge_the_public_base(pr):
  repo, _target, _snapshot, shas = pr
  merge(repo, shas["base"])
  git(repo, "replace", "--graft", shas["base"])
  with pytest.raises(HTTPException, match="replacement"):
    validate(pr, shas["base"])


def test_push_revalidates_the_merge_against_the_same_verified_base_without_force(pr, monkeypatch):
  repo, target, snapshot, shas = pr
  head = merge(repo, shas["base"])
  validation = {**validate(pr, shas["base"]), "base_sha": shas["base"]}
  pushes = []
  monkeypatch.setattr(repairs, "_branch_head", lambda *a: head if pushes else target["head_sha"])
  monkeypatch.setattr(repairs, "_target_base_head", lambda *a: shas["base"])
  real_git = repairs._git
  def record_push(path, *args, **kwargs):
    if args[:1] == ("push",):
      pushes.append(args)
      return type("Result", (), {"returncode": 0})()
    return real_git(path, *args, **kwargs)
  monkeypatch.setattr(repairs, "_git", record_push)
  assert repairs.push_repair(None, repo, None, target, validation).receipt["head_sha"] == head
  assert pushes == [("push", "--porcelain", "https://github.com/owner/fork.git",
                     f"{head}:refs/heads/topic")]


def test_push_without_the_verified_base_refuses_a_merge_before_public_io(pr, monkeypatch):
  repo, target, snapshot, shas = pr
  merge(repo, shas["base"])
  validation = {**validate(pr, shas["base"])}  # caller forgot the verified base
  monkeypatch.setattr(repairs, "_read", lambda *a: pytest.fail("no remote read before validation"))
  assert repairs.push_repair(None, repo, None, target, validation).state == "not_dispatched"


def test_non_fast_forward_rewrite_is_still_refused(pr):
  repo, _target, _snapshot, shas = pr
  git(repo, "checkout", "-q", "-B", "topic", shas["base"])
  commit(repo, "rebased PR", {"owned.py": "one\nPR\n"})
  with pytest.raises(HTTPException, match="fast-forward"):
    validate(pr, shas["base"])


def test_fetch_target_base_binds_the_ref_to_the_live_github_tip(tmp_path, monkeypatch):
  target = {"repo": "owner/base", "base_ref": "main"}
  calls = []
  live = "a" * 40
  checkout = tmp_path / "repair-fetch"
  checkout.mkdir()
  git(checkout, "init", "-q")
  monkeypatch.setattr(repairs, "_checkout_root", lambda: tmp_path)
  monkeypatch.setattr(repairs, "_read", lambda _gh, _cwd, endpoint: calls.append(endpoint) or {"object": {"sha": live}})
  fetched = {"sha": live}
  real_git = repairs._git
  def fake_git(path, *args, **kwargs):
    if args[0] in {"config", "for-each-ref"}:
      return real_git(path, *args, **kwargs)
    calls.append(args)
    return type("Result", (), {"stdout": fetched["sha"] + "\n", "returncode": 0})()
  monkeypatch.setattr(repairs, "_git", fake_git)
  assert repairs.fetch_target_base(None, tmp_path, checkout, target) == live
  assert ("fetch", "--no-tags", "--no-recurse-submodules", "https://github.com/owner/base.git",
          "+refs/heads/main:refs/mobius/target-base") in calls
  fetched["sha"] = "b" * 40
  with pytest.raises(HTTPException, match="moved while it was fetched"):
    repairs.fetch_target_base(None, tmp_path, checkout, target)
