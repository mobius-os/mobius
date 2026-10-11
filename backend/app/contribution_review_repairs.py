"""Server-owned, narrowly scoped checkouts for repairing a selected PR.

The original review target is never rewritten.  A repair is a successor with
its own checkout and review; callers must persist that successor and obtain a
fresh all-clear before asking the ordinary merge gate to act on it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from app import app_git
from app.config import get_settings
from app.github_contribution_contract import COAUTHOR_TRAILER, coauthor_trailer_required
from app.github_contribution_git import _git, _validate_branch, _validate_repo_slug


_OID = re.compile(r"^[0-9a-f]{40}$")
# These names are private planning/dev state in the platform repository, not
# a universal classification of docs or instruction files in other projects.
# See platform .gitignore and the existing follow-up source allowlist.
_PLATFORM_PRIVATE_ROOTS = {"docs", "demo-logs", "AGENTS.md", "CLAUDE.md"}
_INSTANCE_PRIVATE_ROOTS = {".git", ".claude", ".pm", "contributions"}
_PRIVATE_ROOTS = _PLATFORM_PRIVATE_ROOTS | _INSTANCE_PRIVATE_ROOTS
_PRIVATE_NAMES = {".env", ".secret-key", ".recovery-secret", "service-token.txt",
                  ".credentials.json"}


def _fail(message: str, status: int = 409) -> None:
  raise HTTPException(status, message)


def _read(gh, cwd: Path, endpoint: str) -> dict | list:
  try:
    value = json.loads(gh(cwd, "api", endpoint).stdout)
  except (ValueError, AttributeError):
    _fail("GitHub did not return a usable repair preflight.")
  if not isinstance(value, (dict, list)):
    _fail("GitHub returned an invalid repair preflight.")
  return value


def _sha(value: object) -> str:
  if not isinstance(value, str) or not _OID.fullmatch(value):
    _fail("The selected pull request has no canonical commit SHA.")
  return value


def _changed_files(gh, cwd: Path, target: dict) -> set[str]:
  """Get all current PR paths, including both sides of a rename."""
  files: set[str] = set()
  for page in range(1, 31):
    batch = _read(gh, cwd, f"repos/{target['repo']}/pulls/{target['number']}/files?per_page=100&page={page}")
    if not isinstance(batch, list):
      _fail("GitHub did not confirm the pull request's changed files.")
    for item in batch:
      if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
        _fail("GitHub returned an invalid pull request file.")
      files.add(item["filename"])
      if item.get("status") == "renamed":
        if not isinstance(item.get("previous_filename"), str):
          _fail("GitHub did not confirm the renamed file's original path.")
        files.add(item["previous_filename"])
    if len(batch) < 100:
      return files
  _fail("This pull request is too large for a bounded repair checkout.")


def _private_roots_for(target_repo: str | None) -> set[str]:
  if target_repo is None or target_repo.lower() == "mobius-os/mobius":
    return _PRIVATE_ROOTS
  return _INSTANCE_PRIVATE_ROOTS


def _assert_path_scope(files: set[str], allowed: set[str], *, target_repo: str | None = None) -> None:
  if not files or not files <= allowed:
    _fail("The repair changed files outside the approved PR file scope.")
  for name in files:
    path = Path(name)
    if (path.is_absolute() or ".." in path.parts or not path.parts
        or path.parts[0] in _private_roots_for(target_repo)
        or path.parts[:2] == ("data", "shared")
        or any(part in _PRIVATE_NAMES for part in path.parts)):
      _fail("The repair contains a private or unsafe source path.")


def _live_head(gh, cwd: Path, target: dict) -> tuple[dict, dict]:
  repo = _read(gh, cwd, f"repos/{target['repo']}")
  pull = _read(gh, cwd, f"repos/{target['repo']}/pulls/{target['number']}")
  if not isinstance(repo, dict) or not isinstance(pull, dict):
    _fail("GitHub did not confirm the selected pull request.")
  head = pull.get("head") or {}
  base = pull.get("base") or {}
  if (repo.get("id") != target.get("repo_id")
      or pull.get("node_id") != target.get("pr_id")
      or pull.get("state") != "open" or pull.get("merged")
      or head.get("sha") != target.get("head_sha")
      or base.get("ref") != target.get("base_ref")
      or (target.get("head_repo_id") is not None and (
        (head.get("repo") or {}).get("id") != target["head_repo_id"]
        or (head.get("repo") or {}).get("full_name", "").lower() != target["head_repo"].lower()
        or head.get("ref") != target["head_ref"]))):
    _fail("The original pull request changed. Refresh and select it again.")
  return repo, pull


def _head_destination(gh, cwd: Path, pull: dict) -> tuple[str, int, str]:
  head = pull.get("head") or {}
  head_repo = head.get("repo") or {}
  slug = _validate_repo_slug(head_repo.get("full_name"))
  branch = _validate_branch(head.get("ref"))
  repo = _read(gh, cwd, f"repos/{slug}")
  if (not isinstance(repo, dict) or not isinstance(repo.get("id"), int)
      or repo["id"] != head_repo.get("id")
      or repo.get("archived") or repo.get("disabled")
      or (repo.get("permissions") or {}).get("push") is not True):
    _fail("The connected owner cannot push to this PR's exact head repository.", 403)
  return slug, repo["id"], branch


def _checkout_root() -> Path:
  root = Path(get_settings().data_dir).resolve() / ".contribution-runtime" / "review-repairs"
  from app.contribution_runtime import _prepare_private_directory
  _prepare_private_directory(root.parent)
  _prepare_private_directory(root)
  return root


def prepare_checkout(gh, cwd: Path, row, target: dict, allowed_files: list[str]) -> dict:
  """Make a private, server-chosen checkout at the immutable selected head.

  ``allowed_files`` is request intent, never authority: every member must be
  among the current PR files.  The returned snapshot belongs in the outcome
  repair_attempts receipt, not in the immutable selection target.
  """
  _repo, pull = _live_head(gh, cwd, target)
  slug, repo_id, branch = _head_destination(gh, cwd, pull)
  current = _changed_files(gh, cwd, target)
  allowed = set(allowed_files) if isinstance(allowed_files, list) else set()
  if (not allowed or len(allowed) != len(allowed_files)
      or not all(isinstance(f, str) for f in allowed_files)
      or not allowed <= current):
    _fail("Allowed repair files must be a nonempty subset of current PR files.", 400)
  _assert_path_scope(allowed, allowed, target_repo=target["repo"])
  initial = _sha(target.get("head_sha"))
  checkout = Path(tempfile.mkdtemp(prefix="repair-", dir=_checkout_root()))
  os.chmod(checkout, 0o700)
  try:
    _git(checkout, "init", "-q")
    _git(checkout, "fetch", "--no-tags", f"https://github.com/{slug}.git",
         f"refs/heads/{branch}")
    fetched = _git(checkout, "rev-parse", "FETCH_HEAD^{commit}").stdout.strip()
    if fetched != initial:
      _fail("The PR head moved during checkout preparation.")
    _git(checkout, "checkout", "-q", "-b", "mobius-repair", initial)
    return {"checkout": str(checkout), "initial_head_sha": initial,
            "head_repo": slug, "head_repo_id": repo_id, "head_ref": branch,
            "allowed_files": sorted(allowed), "current_changed_files": sorted(current)}
  except Exception:
    # A failed preparation has no durable receipt. Leave no half-built agent
    # checkout behind; callers never receive its path.
    import shutil
    shutil.rmtree(checkout, ignore_errors=True)
    raise


def _assert_git_metadata(path: Path) -> None:
  """Reject local metadata that can forge Git evidence or redirect public I/O."""
  git_dir = path / ".git"
  if any((git_dir / name).exists() for name in ("shallow", "info/grafts", "commondir", "config.worktree")):
    _fail("The dedicated checkout has altered or incomplete Git ancestry metadata.")
  if any((git_dir / name).is_symlink() for name in ("config", "packed-refs", "refs", "objects")):
    _fail("The dedicated checkout contains redirected Git metadata.")
  if _git(path, "for-each-ref", "--format=%(refname)", "refs/replace").stdout.strip():
    _fail("Git replacement objects cannot authorize a public repair.")
  # Read names only: do not disclose config values, which may contain secrets.
  changed = _git(path, "config", "--local", "--name-only", "--get-regexp",
    r"^(url\.|http\.|credential\.|include|extensions\.|filter\.|remote\.|protocol\.|uploadpack\.|core\.(hookspath|worktree|sshcommand|gitproxy|fsmonitor|alternaterefscommand|pager))",
    check=False)
  if changed.stdout.strip():
    _fail("The dedicated checkout contains unsafe Git transport or execution configuration.")
  hooks = git_dir / "hooks"
  if hooks.exists() and (hooks.is_symlink() or any(not p.name.endswith(".sample") for p in hooks.iterdir())):
    _fail("The dedicated checkout contains unapproved Git hooks.")


def validate_repair(gh, cwd: Path, row, target: dict, checkout: dict,
                    allowed_files: list[str] | None = None) -> dict:
  """Derive the proposed successor from Git, not from agent-supplied metadata."""
  _repo, pull = _live_head(gh, cwd, target)
  slug, repo_id, branch = _head_destination(gh, cwd, pull)
  if (slug != checkout.get("head_repo") or repo_id != checkout.get("head_repo_id")
      or branch != checkout.get("head_ref")
      or checkout.get("initial_head_sha") != target.get("head_sha")):
    _fail("The repair checkout no longer matches its selected PR head.")
  root = _checkout_root()
  raw_path = checkout.get("checkout")
  if not isinstance(raw_path, str):
    _fail("The repair checkout path is missing.")
  path = Path(raw_path)
  if (not path.is_absolute() or path.is_symlink() or path != path.resolve()
      or path.parent != root or not path.name.startswith("repair-")
      or (path / ".git").is_symlink() or not (path / ".git").is_dir()):
    _fail("The repair checkout is not server-owned.")
  _assert_git_metadata(path)
  snapshot_allowed = checkout.get("allowed_files")
  if (not isinstance(snapshot_allowed, list) or
      allowed_files is not None and sorted(allowed_files) != snapshot_allowed):
    _fail("The repair's approved file scope changed.")
  allowed = set(snapshot_allowed)
  if not allowed <= _changed_files(gh, cwd, target):
    _fail("The PR file scope changed since checkout preparation.")
  if app_git.worktree_dirty(path):
    _fail("Commit or discard working changes before validating the repair.")
  initial = _sha(checkout["initial_head_sha"])
  head = _git(path, "rev-parse", "HEAD^{commit}").stdout.strip()
  _sha(head)
  if head == initial or not app_git.ref_is_ancestor(path, initial, head):
    _fail("The repair must be a new fast-forward descendant of the original PR head.")
  paths = app_git.endpoint_diff_paths(path, initial, head, read_only=True)
  diff = app_git._canonical_diff(path, initial, head, read_only=True)
  if paths is None or diff is None or not diff:
    _fail("Git could not derive a nonempty repair diff.")
  # Final-tree scope is insufficient: an intermediate commit with an unrelated
  # file remains in the published history even if a later commit reverts it.
  commits = _git(path, "rev-list", "--reverse", f"{initial}..{head}").stdout.splitlines()
  if len(commits) > 100:
    _fail("The repair has too many commits for a bounded review.")
  historical_paths = set(paths)
  for commit in commits:
    parents = _git(path, "show", "-s", "--format=%P", commit).stdout.split()
    if len(parents) != 1:
      _fail("Merge commits are not accepted in a narrow repair checkout.")
    if (coauthor_trailer_required({"plan": target})
        and COAUTHOR_TRAILER not in _git(path, "show", "-s", "--format=%B", commit).stdout):
      _fail("The repair commit is missing the agent co-author trailer.")
    names = _git(path, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
                 parents[0], commit).stdout
    historical_paths.update(name for name in names.split("\x00") if name)
  _assert_path_scope(historical_paths, allowed, target_repo=target["repo"])
  # The exact predecessor is already present on this same GitHub head ref.
  # Its unchanged ancestry is not newly disclosed by a repair (many public
  # repositories legitimately contain docs/ or AGENTS.md). Inspect ALL new
  # commits, including reverted private paths, rather than rejecting public
  # predecessor history. Historical path-scope validation above remains strict.
  for private in _private_roots_for(target["repo"]) | _PRIVATE_NAMES:
    if _git(path, "rev-list", f"{initial}..{head}", "--", private).stdout.strip():
      _fail("The new repair history contains private workspace paths.")
  return {"checkout": str(path), "initial_head_sha": initial,
          "head_sha": head, "head_repo": slug, "head_repo_id": repo_id,
          "head_ref": branch, "files": sorted(paths), "allowed_files": sorted(allowed),
          "diff_sha256": hashlib.sha256(diff).hexdigest()}


def push_repair(gh, cwd: Path, row, target: dict, validation: dict, *, before_push=None) -> dict:
  """Push one validated descendant with an atomic exact-predecessor lease.

  The lease cannot authorize force-push: validate_repair proves fast-forward
  ancestry of the immutable SHA; a changed ref causes lease rejection.

  Routes must durably arm an unrepeatable push receipt before calling this.
  Any transport failure is ambiguous; reconcile GitHub read-only, do not retry.

  Success is confirmed from the head branch ref, which GitHub updates with the
  push itself. The PR's head projection is refreshed asynchronously and can
  still show the predecessor right after a successful push.
  """
  snapshot = {k: validation[k] for k in
              ("checkout", "initial_head_sha", "head_repo", "head_repo_id", "head_ref", "allowed_files")}
  fresh = validate_repair(gh, cwd, row, target, snapshot)
  if fresh["head_sha"] != validation.get("head_sha") or fresh["diff_sha256"] != validation.get("diff_sha256"):
    _fail("The repair checkout changed after validation.")
  slug, branch = fresh["head_repo"], fresh["head_ref"]
  if _branch_head(gh, cwd, slug, branch) != target["head_sha"]:
    _fail("The remote PR head moved before the repair push.")
  # Route callback crosses back to its owning event loop to recheck the live
  # run, Stop and app nonce after ALL remote preflight I/O, before public I/O.
  if before_push is not None:
    before_push()
  result = _git(Path(fresh["checkout"]), "push", "--porcelain",
                f"--force-with-lease=refs/heads/{branch}:{target['head_sha']}",
                f"https://github.com/{slug}.git",
                f"{fresh['head_sha']}:refs/heads/{branch}", check=False)
  if result.returncode != 0 or _branch_head(gh, cwd, slug, branch) != fresh["head_sha"]:
    _fail("The repair push was not confirmed. Reconcile the exact remote head before any retry.")
  return fresh


def _branch_head(gh, cwd: Path, slug: str, branch: str) -> str | None:
  ref = _read(gh, cwd, f"repos/{slug}/git/ref/heads/{quote(branch, safe='')}")
  return (ref.get("object") or {}).get("sha") if isinstance(ref, dict) else None
