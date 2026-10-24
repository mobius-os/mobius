"""Server-owned, narrowly scoped checkouts for repairing a selected PR.

The original review target is never rewritten.  A repair is a successor with
its own checkout and review; callers must persist that successor and obtain a
fresh all-clear before asking the ordinary merge gate to act on it.

Checkouts are disposable: the confirmed push is the durable result. Receipt-
aware garbage collection keeps active editors and uncertain public effects,
deletes terminal receipts, and gives crash orphans and dormant edits grace.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from fastapi import HTTPException

from app import app_git
from app.config import get_settings
from app.contribution_errors import ContributionSubmitError
from app.contribution_review_effects import DispatchResult, NotDispatched
from app.github_contribution_contract import COAUTHOR_TRAILER, coauthor_trailer_required
from app.github_contribution_git import _git_env, _run_cmd, _validate_branch, _validate_repo_slug


def _repair_git_env(repo: Path) -> dict[str, str]:
  """One sanitized environment for every server proof in a repair checkout."""
  env = _git_env(repo)
  for key in tuple(env):
    if key.startswith("GIT_CONFIG_") or key in {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR",
        "GIT_ATTR_NOSYSTEM", "GIT_EXEC_PATH", "GIT_SSH_COMMAND", "GIT_SSH",
        "GIT_ASKPASS", "GIT_TEMPLATE_DIR", "GIT_REPLACE_REF_BASE"}:
      env.pop(key, None)
  env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL="/dev/null",
             GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0",
             GIT_ASKPASS="/bin/false", SSH_ASKPASS="/bin/false")
  return env


def _git(repo: Path, *args: str, check: bool = True):
  """Run server Git without inherited config, object redirects or prompts."""
  env = _repair_git_env(repo)
  # Global config is ignored, including the owner's configured credential
  # helper. Supply the one fixed GitHub helper for network operations only.
  auth = ["-c", "credential.helper=!gh auth git-credential"] if args[:1] in {("fetch",), ("push",)} else []
  return _run_cmd(["git", *auth, "-C", str(repo), *args], cwd=repo, check=check, env=env)


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


# Orphans can be half-built checkouts between mkdtemp and receipt save. A
# stopped/terminal receipt is explicit evidence; unreferenced paths need grace.
ORPHAN_GRACE_SECONDS = 3600
STALE_EDIT_SECONDS = 7 * 24 * 3600
_ACTIVITY_MARKERS = ("", ".git", ".git/index", ".git/HEAD", ".git/FETCH_HEAD", ".git/logs/HEAD")


def _repair_dir(raw_path: object) -> Path | None:
  """The exact server repair directory named by a receipt, or None."""
  if not isinstance(raw_path, (str, Path)):
    return None
  path = Path(raw_path)
  try:
    resolved = path.resolve()
  except (OSError, RuntimeError):
    return None
  if (not path.is_absolute() or path != resolved or path.parent != _checkout_root()
      or not path.name.startswith("repair-") or path.is_symlink() or not path.is_dir()):
    return None
  return path


def checkout_present(raw_path: object) -> bool:
  """Whether a recorded checkout still exists and can be reused."""
  return _repair_dir(raw_path) is not None


def discard_checkout(raw_path: object) -> None:
  """Remove a finished checkout. A later repair always prepares a new one."""
  path = _repair_dir(raw_path)
  if path is not None:
    try:
      shutil.rmtree(path)
    except FileNotFoundError:
      pass


def _last_activity(path: Path) -> float:
  times = []
  for marker in _ACTIVITY_MARKERS:
    try:
      times.append((path / marker).lstat().st_mtime)
    except OSError:
      pass
  return max(times, default=0.0)


def sweep_stale_checkouts(*, protected_paths: set[str], terminal_paths: set[str],
                          dormant_paths: set[str], now: float | None = None) -> None:
  """Filesystem test helper; production uses the writer-fenced quarantine."""
  delete_quarantined_checkouts(quarantine_stale_checkouts(
    protected_paths=protected_paths, terminal_paths=terminal_paths,
    dormant_paths=dormant_paths, now=now))


def quarantine_stale_checkouts(*, protected_paths: set[str], terminal_paths: set[str],
                               dormant_paths: set[str], now: float | None = None) -> list[Path]:
  """Rename eligible paths atomically while the caller holds a DB writer fence.

  The DB owner supplies a complete protected set, including every unresolved
  public effect and active editor. Only same-filesystem renames occur under the
  fence; recursive deletion runs after it is released.
  """
  root = _checkout_root()
  protected = {str(path) for path in protected_paths}
  terminal = {str(path) for path in terminal_paths}
  dormant = {str(path) for path in dormant_paths}
  current = time.time() if now is None else now
  quarantined = []
  for candidate in root.iterdir():
    if candidate.name.startswith("repair-gc-"):
      continue
    entry = _repair_dir(candidate)
    if entry is None or str(entry) in protected:
      continue
    age = current - _last_activity(entry)
    if (str(entry) in terminal or
        (str(entry) in dormant and age >= STALE_EDIT_SECONDS) or
        (str(entry) not in dormant and age >= ORPHAN_GRACE_SECONDS)):
      destination = root / f"repair-gc-{uuid.uuid4().hex}"
      try:
        entry.rename(destination)
      except FileNotFoundError:
        continue
      quarantined.append(destination)
  return quarantined


def delete_quarantined_checkouts(paths: list[Path] | None = None) -> None:
  """Delete renamed checkouts, including a prior process's crash leftovers."""
  root = _checkout_root()
  candidates = list(root.iterdir()) if paths is None else paths
  for candidate in candidates:
    if (candidate.parent != root or not candidate.name.startswith("repair-gc-")
        or candidate.is_symlink() or not candidate.is_dir()):
      continue
    try:
      shutil.rmtree(candidate)
    except FileNotFoundError:
      pass


def prepare_checkout(gh, cwd: Path, row, target: dict, allowed_files: list[str]) -> dict:
  """Make a private, server-chosen checkout at the immutable selected head.

  ``allowed_files`` is frozen request intent, never authority: only its
  nonempty intersection with current PR files may be edited. The snapshot belongs in the outcome
  repair_attempts receipt, not in the immutable selection target.
  """
  _repo, pull = _live_head(gh, cwd, target)
  slug, repo_id, branch = _head_destination(gh, cwd, pull)
  current = _changed_files(gh, cwd, target)
  frozen = set(allowed_files) if isinstance(allowed_files, list) else set()
  if (not frozen or len(frozen) != len(allowed_files)
      or not all(isinstance(f, str) for f in allowed_files)):
    _fail("The frozen repair file scope is invalid.", 400)
  allowed = frozen & current
  if not allowed:
    _fail("No approved repair file remains in the current PR file scope.", 409)
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
            "allowed_files": sorted(allowed), "current_changed_files": sorted(current),
            "target_base_sha": fetch_target_base(gh, cwd, checkout, target)}
  except Exception:
    # A failed preparation has no durable receipt. Leave no half-built agent
    # checkout behind; callers never receive its path.
    shutil.rmtree(checkout, ignore_errors=True)
    raise


TARGET_BASE_REF = "refs/mobius/target-base"


def fetch_target_base(gh, cwd: Path, checkout: Path, target: dict) -> str:
  """Fetch the live target-branch tip into the checkout for an in-scope update.

  The ref is a convenience for ``git merge refs/mobius/target-base``, never
  evidence: validation and publication re-read the live tip from GitHub.
  """
  # The agent can write this checkout. Prove it is still the server's own,
  # with no planted transport, credential, hook or execution configuration,
  # before Git runs in it with the server's environment and network access.
  checkout = owned_checkout(checkout)
  ref = _read(gh, cwd, f"repos/{target['repo']}/git/ref/heads/{quote(target['base_ref'], safe='')}")
  live = _sha((ref.get("object") or {}).get("sha") if isinstance(ref, dict) else None)
  try:
    _git(checkout, "fetch", "--no-tags", f"https://github.com/{_validate_repo_slug(target['repo'])}.git",
         f"+refs/heads/{_validate_branch(target['base_ref'])}:{TARGET_BASE_REF}")
    fetched = _git(checkout, "rev-parse", f"{TARGET_BASE_REF}^{{commit}}").stdout.strip()
  except (ContributionSubmitError, OSError, subprocess.SubprocessError):
    _fail("GitHub did not provide the current target branch for this repair checkout.")
  if fetched != live:
    _fail("The target branch moved while it was fetched. Request the repair checkout again.")
  return live


def owned_checkout(raw_path: object) -> Path:
  """The agent-writable repair checkout, proven server-owned and inert.

  Every Git command the server runs in that checkout runs with the server's
  environment and network access, so each entry point repeats this whole
  check first: a reused checkout is never trusted from an earlier receipt.
  """
  if not isinstance(raw_path, (str, Path)):
    _fail("The repair checkout path is missing.")
  path = Path(raw_path)
  root = _checkout_root()
  if (not path.is_absolute() or path.is_symlink() or path != path.resolve()
      or path.parent != root or not path.name.startswith("repair-")
      or (path / ".git").is_symlink() or not (path / ".git").is_dir()):
    _fail("The repair checkout is not server-owned.")
  _assert_git_metadata(path)
  return path


def _assert_git_metadata(path: Path) -> None:
  """Reject local metadata that can forge Git evidence, redirect public I/O
  or run a program when the server's Git commands read this checkout.

  Fails closed: metadata Git cannot read is rejected, never treated as clean.
  """
  git_dir = path / ".git"
  forbidden = ("shallow", "info/grafts", "commondir", "config.worktree",
               "objects/info/alternates", "objects/info/http-alternates",
               "objects/info/commit-graph", "objects/info/commit-graphs", "info/attributes")
  if any((git_dir / name).exists() or (git_dir / name).is_symlink() for name in forbidden):
    _fail("The dedicated checkout has altered or incomplete Git ancestry metadata.")
  # Ref/object subtrees are agent-writable. A top-level check misses links in
  # objects/info or refs/replace; reject every redirect before any Git read.
  if any(item.is_symlink() for item in git_dir.rglob("*")):
    _fail("The dedicated checkout contains redirected Git metadata.")
  hooks = git_dir / "hooks"
  if hooks.exists() and (hooks.is_symlink() or any(not p.name.endswith(".sample") for p in hooks.iterdir())):
    _fail("The dedicated checkout contains unapproved Git hooks.")
  # --file and --no-includes parse only this local file. Inspect names, never
  # values, so a rejection cannot leak credentials planted in config.
  names = _git(path, "config", "--file", str(git_dir / "config"),
               "--no-includes", "--name-only", "--list", check=False)
  if names.returncode != 0:
    _fail("The dedicated checkout's Git configuration could not be verified.")
  allowed = {"core.repositoryformatversion", "core.filemode", "core.bare",
             "core.logallrefupdates", "core.ignorecase", "core.precomposeunicode",
             "user.name", "user.email"}
  if any(name.lower() not in allowed for name in names.stdout.splitlines()):
    _fail("The dedicated checkout contains unapproved Git configuration.")
  replaced = _git(path, "for-each-ref", "--format=%(refname)", "refs/replace", check=False)
  if replaced.returncode != 0:
    _fail("The dedicated checkout's Git references could not be verified.")
  if replaced.stdout.strip():
    _fail("Git replacement objects cannot authorize a public repair.")


def validate_repair(gh, cwd: Path, row, target: dict, checkout: dict,
                    allowed_files: list[str] | None = None, *,
                    base_sha: str | None = None) -> dict:
  """Derive the proposed successor from Git, not from agent-supplied metadata.

  ``base_sha`` is the live target-branch tip the caller just read from GitHub.
  With it, the repair may contain ONE merge commit whose second parent is
  exactly that tip: the only way scoped takeover brings a PR up to date
  without rewriting published history. Commits reachable from that tip are
  already public on the target, so only commits this repair authored are
  scope-checked; the merge itself is checked against the base it imports, so
  everything outside the frozen file scope must equal the public base.
  """
  _repo, pull = _live_head(gh, cwd, target)
  slug, repo_id, branch = _head_destination(gh, cwd, pull)
  if (slug != checkout.get("head_repo") or repo_id != checkout.get("head_repo_id")
      or branch != checkout.get("head_ref")
      or checkout.get("initial_head_sha") != target.get("head_sha")):
    _fail("The repair checkout no longer matches its selected PR head.")
  raw_path = checkout.get("checkout")
  if not isinstance(raw_path, str):
    _fail("The repair checkout path is missing.")
  path = owned_checkout(raw_path)
  snapshot_allowed = checkout.get("allowed_files")
  if (not isinstance(snapshot_allowed, list) or not snapshot_allowed or
      not all(isinstance(name, str) for name in snapshot_allowed) or
      len(set(snapshot_allowed)) != len(snapshot_allowed) or
      allowed_files is not None and not set(snapshot_allowed) <= set(allowed_files)):
    _fail("The repair's approved file scope changed.")
  allowed = set(snapshot_allowed) & _changed_files(gh, cwd, target)
  if not allowed:
    _fail("No approved repair file remains in the current PR file scope.")
  if _git(path, "status", "--porcelain", "--untracked-files=all").stdout.strip():
    _fail("Commit or discard working changes before validating the repair.")
  initial = _sha(checkout["initial_head_sha"])
  head = _git(path, "rev-parse", "HEAD^{commit}").stdout.strip()
  _sha(head)
  if head == initial or _git(path, "merge-base", "--is-ancestor", initial, head,
                             check=False).returncode != 0:
    _fail("The repair must be a new fast-forward descendant of the original PR head.")
  path_diff = _git(path, "diff", "--name-only", "--no-renames", "-z",
                   f"{initial}..{head}", check=False)
  paths = {name for name in path_diff.stdout.split("\x00") if name} if path_diff.returncode == 0 else None
  diff = app_git._canonical_diff(path, initial, head, read_only=True,
                                 env=_repair_git_env(path))
  if paths is None or diff is None or not diff:
    _fail("Git could not derive a nonempty repair diff.")
  base = _present_commit(path, base_sha) if base_sha is not None else None
  # Commits this repair introduced. Ancestors of the verified live base are
  # already public on the target branch, so importing them discloses nothing.
  authored = _git(path, "rev-list", "--reverse", f"{initial}..{head}",
                  *(("--not", base) if base else ())).stdout.splitlines()
  if len(authored) > 100:
    _fail("The repair has too many commits for a bounded review.")
  # Final-tree scope is insufficient: an intermediate commit with an unrelated
  # file remains in the published history even if a later commit reverts it.
  # This per-commit set also rejects private paths in every authored commit,
  # including ones a later commit reverted. (A pathspec rev-list cannot do
  # that across a merge: history simplification follows the base side and
  # silently skips the PR side.)
  historical_paths: set[str] = set()
  merges = []
  for commit in authored:
    parents = _git(path, "show", "-s", "--format=%P", commit).stdout.split()
    if len(parents) == 2 and base and parents[1] == base and (
        parents[0] == initial or parents[0] in authored):
      merges.append(commit)
      compared = base
    elif len(parents) == 1:
      compared = parents[0]
    else:
      _fail("A repair may merge only the exact current target branch tip, as the "
            "second parent of one merge commit; other merges are not accepted.")
    if (coauthor_trailer_required({"plan": target})
        and COAUTHOR_TRAILER not in _git(path, "show", "-s", "--format=%B", commit).stdout):
      _fail("The repair commit is missing the agent co-author trailer.")
    historical_paths.update(_tree_changes(path, compared, commit))
  if len(merges) > 1:
    _fail("A repair may merge the current target branch tip only once.")
  if merges:
    # The net result against the imported base must stay inside the frozen
    # PR scope: everything else equals already-public target content.
    net = _tree_changes(path, base, head)
    _assert_path_scope(net, allowed, target_repo=target["repo"])
    historical_paths.update(net)
  else:
    historical_paths.update(paths)
  _assert_path_scope(historical_paths, allowed, target_repo=target["repo"])
  return {"checkout": str(path), "initial_head_sha": initial,
          "head_sha": head, "head_repo": slug, "head_repo_id": repo_id,
          "head_ref": branch, "files": sorted(historical_paths if merges else paths),
          "allowed_files": sorted(allowed),
          **({"merged_base_sha": base} if merges else {}),
          "diff_sha256": hashlib.sha256(diff).hexdigest()}


def _present_commit(path: Path, sha: str) -> str | None:
  """The verified base commit when this checkout holds that exact object."""
  sha = _sha(sha)
  found = _git(path, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}", check=False)
  return sha if found.returncode == 0 and found.stdout.strip() == sha else None


def _tree_changes(path: Path, older: str, newer: str) -> set[str]:
  names = _git(path, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z",
               older, newer).stdout
  return {name for name in names.split("\x00") if name}


def push_repair(gh, cwd: Path, row, target: dict, validation: dict, *, before_push=None) -> DispatchResult[dict]:
  """Dispatch one ordinary fast-forward push with explicit effect certainty.

  A local/preflight refusal is not dispatched. Once Git push starts, even a
  failed process is unknown until the exact branch ref confirms the successor.
  This is a cooperative-agent guard, not a hostile-agent filesystem sandbox.
  """
  snapshot = {k: validation[k] for k in
              ("checkout", "initial_head_sha", "head_repo", "head_repo_id", "head_ref", "allowed_files")}
  try:
    base_sha = _sha(validation.get("base_sha"))
    fresh = validate_repair(gh, cwd, row, target, snapshot, base_sha=base_sha)
    if (fresh["head_sha"] != validation.get("head_sha")
        or fresh["diff_sha256"] != validation.get("diff_sha256")):
      _fail("The repair checkout changed after validation.")
    slug, branch = fresh["head_repo"], fresh["head_ref"]
    if _branch_head(gh, cwd, slug, branch) != target["head_sha"]:
      _fail("The remote PR head moved before the repair push.")
    if _target_base_head(gh, cwd, target) != base_sha:
      _fail("The target base moved before repair dispatch. Test the new combined result first.")
    if before_push is not None:
      before_push()
    # Repeat the complete checkout proof after the callback and immediately
    # before public I/O. Do not trust the earlier diff-only validation.
    push_from = owned_checkout(fresh["checkout"])
    repeated = validate_repair(gh, cwd, row, target, snapshot, base_sha=base_sha)
    if repeated["head_sha"] != fresh["head_sha"] or repeated["diff_sha256"] != fresh["diff_sha256"]:
      _fail("The repair checkout changed before dispatch.")
    # The repeated proof may involve GitHub reads. Neither its old head check
    # nor its earlier Stop check is a final dispatch guard.
    if _branch_head(gh, cwd, slug, branch) != target["head_sha"]:
      _fail("The remote PR head moved before the repair push.")
    if _target_base_head(gh, cwd, target) != base_sha:
      _fail("The target base moved before repair dispatch. Test the new combined result first.")
    if before_push is not None:
      # This also re-proves queue-removal authority in newer callers. Remote
      # state cannot be sampled atomically with a push, but Stop observed
      # during the repeated proof must never be ignored.
      before_push()
  except Exception:
    # Every exception above occurs before the push subprocess is invoked.
    # In particular an execution callback refusal is never push_unknown.
    return DispatchResult.not_dispatched("The repair failed a final pre-dispatch guard.")
  try:
    result = _git(push_from, "push", "--porcelain", f"https://github.com/{slug}.git",
                  f"{fresh['head_sha']}:refs/heads/{branch}", check=False)
  except Exception:
    return DispatchResult.unknown("The repair push result is unknown; reconcile the exact remote head.")
  try:
    remote_head = _branch_head(gh, cwd, slug, branch)
    if remote_head == fresh["head_sha"]:
      return DispatchResult.confirmed(fresh)
    if (result.returncode > 0 and remote_head == target["head_sha"]
        and _exact_ref_rejected(getattr(result, "stdout", ""), fresh["head_sha"], branch)):
      # Git completed and named only this ref as rejected; the exact old tip
      # still exists. This proves no branch update, unlike timeout or EOF.
      return DispatchResult("not_dispatched",
        receipt={"kind": "remote_ref_rejection", "head_sha": fresh["head_sha"]},
        reason="GitHub rejected the exact repair ref; no branch update was applied.")
  except Exception:
    pass
  return DispatchResult.unknown("The repair push was not confirmed; reconcile the exact remote head.")


def _exact_ref_rejected(porcelain: str, head: str, branch: str) -> bool:
  """Accept only Git's complete rejection record for our sole pushed ref."""
  if not isinstance(porcelain, str):
    return False
  exact = f"{head}:refs/heads/{branch}"
  records = []
  for line in porcelain.splitlines():
    if line.startswith("To ") or line == "Done":
      continue
    fields = line.split("\t")
    if len(fields) != 3 or fields[0] not in {"!", "=", "*", "+", "-", " "}:
      return False
    records.append(fields)
  return (len(records) == 1 and len(records[0]) == 3
          and records[0][0] == "!" and records[0][1] == exact
          and (records[0][2].startswith("[rejected]")
               or records[0][2].startswith("[remote rejected]")))


def _target_base_head(gh, cwd: Path, target: dict) -> str:
  ref = _read(gh, cwd, f"repos/{target['repo']}/git/ref/heads/{quote(target['base_ref'], safe='')}")
  return _sha((ref.get("object") or {}).get("sha") if isinstance(ref, dict) else None)


def _branch_head(gh, cwd: Path, slug: str, branch: str) -> str | None:
  ref = _read(gh, cwd, f"repos/{slug}/git/ref/heads/{quote(branch, safe='')}")
  return (ref.get("object") or {}).get("sha") if isinstance(ref, dict) else None
