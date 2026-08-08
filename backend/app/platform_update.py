"""Platform self-update — clone-native ``git fetch`` + merge reconcile.

``/data/platform`` is a real ``git clone`` of the canonical repo; uvicorn serves
its backend directly (``cd /data/platform/backend && uvicorn app.main:app``).
Local ``main`` carries the agent's edits; the ``upstream`` branch records the
commit the clone was last reconciled to (set to HEAD at clone time). A normal
deploy advances ``origin/main``. A managed-channel deploy fetches its configured
branch but selects only the exact commit baked into that image. This module makes
either deploy actually REACH a running instance by merging the selected target
with local edits — on boot (before uvicorn imports the code, so
the update goes live automatically) and on owner-triggered Apply. Owner Apply
pins the exact target returned by the review plan even if its fetch observes a
newer remote head; backend changes then need a restart to load.

The reconcile is built to be non-destructive above all else:

1. ``/data/platform`` holds the SERVED backend, so a reconcile must never leave a
   half-applied tree. A merge conflict is aborted back to the pre-reconcile
   commit (the old, working code keeps serving) and surfaced as a conflict; a
   crash mid-merge is detected on the next boot and aborted before anything
   else runs. Legacy interrupted rebases are still cleaned up too.

2. Local edits are NEVER lost. Uncommitted working-tree edits are committed onto
   ``main`` before any fast-forward/merge, so either operation starts from a
   durable local tip. A conflict or an
   import-broken result rolls the served tree back to exactly those local edits.

3. A text-clean merge can still produce a tree that fails to import (e.g.
   upstream deleted a module a local edit still imports). A post-merge import
   probe catches that and rolls back to the previous served commit rather than
   serving a broken tree.

Availability is an EXACT ancestry check, not a sha-string compare: an update is
available iff the configured target is NOT already an ancestor of local ``main`` — the
same ``git merge-base --is-ancestor`` model ``app_git`` uses for an app. This
module reuses ``app_git``'s isolated git env and ``commit_local`` engine; it does
NOT carry forward the old baked-floor machinery (recording a baked tree onto
``upstream``), which fought the clone model — a real ``git fetch origin`` plus a
merge against real ancestry replaces it entirely. Diverged histories merge as
one net change instead of replaying every local commit separately, so all
conflicts surface together and local commit identities remain intact.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Literal, TypedDict

from sqlalchemy.orm import Session

from app import app_git, platform_activation, release_channel
from app.platform_activation import PlatformActivationImpact


log = logging.getLogger(__name__)

PLATFORM_REPO = Path("/data/platform")
# The served backend — the import probe's cwd, so ``import app.main`` resolves
# from the clone exactly as the uvicorn exec does.
PLATFORM_BACKEND = PLATFORM_REPO / "backend"

# Runtime marker files. Each is a transient signal, never user data (they are
# gitignored out of the outer ``/data`` repo in entrypoint.sh).
UPGRADE_FLAG = Path("/data/.platform-upgrade-available")
# Durable activation remainder.  The historical filename is retained so old
# images keep ignoring it and a rolling upgrade never invents another marker.
# Legacy content was a bare restart SHA; current content is a JSON target+paths
# record that can survive a server restart when host/image work is still due.
RESTART_NEEDED_FLAG = Path("/data/.platform-restart-needed")
# Written by entrypoint.sh before uvicorn starts. These identify the backend
# tree the current Python process actually imported, which can differ from the
# on-disk clone after an agent edits /data/platform.
SERVING_SOURCE_FILE = Path("/tmp/serving-source")
SERVING_SHA_FILE = Path("/tmp/serving-sha")
# Persist a conflict so Settings keeps showing it across reloads (the merge is
# aborted, so no git state alone can signal it). Records the target sha + paths.
CONFLICT_FLAG = Path("/data/.platform-conflict")
# Persist that the last reconcile could not refresh origin. Deploy verification
# treats this as an explicit exemption from the freshness assertion; the next
# successful fetch clears it.
OFFLINE_FLAG = Path("/data/.platform-offline")
# A text-clean merge whose result failed the import probe was rolled back to the
# previous served commit. Records the target sha + the import error so Settings
# can show "rolled back — needs repair" rather than silently staying "up to
# date".
ROLLED_BACK_FLAG = Path("/data/.platform-rolled-back")
# Transient crash-safety marker written immediately before reconcile mutates the
# served tree. If the boot subprocess is SIGKILLed mid-merge/probe/rollback, the
# post-timeout boot guard uses this sha to restore the last committed served tip
# before uvicorn imports anything.
RECONCILE_PRE_FLAG = Path("/data/.platform-reconcile-pre")
# A filesystem lock shared by the boot reconcile subprocess and the running
# uvicorn's Apply path. It MUST be a real flock (not an asyncio.Lock): the boot
# reconcile runs in a throwaway ``python3 -c`` process, so an in-process lock
# could not serialise it against uvicorn.
RECONCILE_LOCK = Path("/data/.platform-reconcile.lock")
# Durable, browser-safe phase record. Unlike the old process-only dictionary,
# this is visible when status/progress requests land on another worker.
UPDATE_PROGRESS_PATH = Path("/data/.platform-update-progress.json")

UPSTREAM_BRANCH = "upstream"
LOCAL_BRANCH = "main"
# Preserve the historical public constant for callers/tests. Runtime entry
# points resolve through release_channel so managed images can fetch a release
# branch while targeting only their exact baked commit.
DEFAULT_TARGET_REF = release_channel.DEFAULT_TARGET_REF
OWNER_UPDATE_FETCH_REFSPEC = (
  "+refs/heads/main:refs/remotes/origin/main"
)
# Keeps an off-tree semantic merge-base tree reachable while an owner may leave
# a platform conflict unresolved across Git maintenance or server restarts.
_CONFLICT_MERGE_BASE_REF = "refs/mobius/platform-conflict-base"

# The platform tree is larger than an app but still small; a git op slower than
# this is wedged, not busy. Fetch gets its own (network-bound) budget.
_GIT_TIMEOUT = 120
_FETCH_TIMEOUT = 120
# Distinct sentinel returned by ``_merge_target`` when the merge wedged and was
# aborted (as opposed to a positive git returncode from a content conflict).
# After the abort no unmerged paths survive, so the caller must treat this as an
# error/serve-old outcome rather than fabricating a zero-path content conflict.
_MERGE_TIMEOUT = -1
# The post-merge import probe. A module-level infinite loop or a blocking call
# in agent-edited code would otherwise wedge boot forever; a timeout-kill counts
# as probe-fail -> roll back.
_PROBE_TIMEOUT = 60
# Hook installation only copies a handful of local files and updates one
# repo-local config value. A long run is a wedged filesystem/process, not work.
_HOOK_INSTALL_TIMEOUT = 15
_HOOK_MAX_BYTES = 1_000_000
_HOOK_SOURCES = (
  ("scripts/pre-commit.sh", "pre-commit"),
  ("scripts/githooks/pre-push", "pre-push"),
)

# Update-preview payload bounds. A whole-platform deploy can carry a huge diff;
# the review sheet renders the file summary (always small) by default and the raw
# diff only on demand, so cap the diff bytes on the wire and flag truncation. The
# commit list is capped too — a normal deploy is a handful, and the sheet lists
# them, not paginates.
MAX_PREVIEW_DIFF_CHARS = 200_000
_PREVIEW_COMMIT_LIMIT = 100

# Serialise Apply in-process (uvicorn is single-worker; belt-and-braces against a
# double-click racing two reconciles). The cross-process guard is RECONCILE_LOCK.
_APPLY_LOCK = asyncio.Lock()
_PROGRESS_LOCK = threading.Lock()


class PlatformUpdatePhase(str, Enum):
  """Observable phases of the one active owner-triggered update operation."""

  IDLE = "idle"
  PREPARING = "preparing"
  FETCHING = "fetching"
  RECONCILING = "reconciling"
  VALIDATING = "validating"
  BUILDING = "building"
  FINALIZING = "finalizing"
  COMPLETE = "complete"
  BLOCKED = "blocked"
  FAILED = "failed"


class PlatformUpdateProgress(TypedDict):
  """Response shape for ``GET /api/platform/update-progress``.

  This in-process record makes today's synchronous Apply request observable.
  A future supervisor-owned generation updater should persist the same shape
  beside the staged generation so it survives a worker restart.
  """

  plan_id: str | None
  target_sha: str | None
  phase: str
  active: bool
  error: str | None
  updated_at: float


_UPDATE_PROGRESS = PlatformUpdateProgress(
  plan_id=None,
  target_sha=None,
  phase=PlatformUpdatePhase.IDLE.value,
  active=False,
  error=None,
  updated_at=0.0,
)


class PlatformUpdateError(RuntimeError):
  """A platform update could not proceed (carries a short machine code)."""


def _runtime_release_channel() -> release_channel.PlatformReleaseChannel:
  """Resolve the deployment contract as a platform-update error."""
  try:
    return release_channel.platform_release_channel()
  except release_channel.ReleaseChannelError as exc:
    raise PlatformUpdateError(str(exc)) from exc


class PlatformUpdateState(str, Enum):
  """User-visible state for the platform updater."""

  UP_TO_DATE = "up_to_date"
  AVAILABLE = "available"
  CONFLICT = "conflict"
  RESTART_NEEDED = "restart_needed"
  ACTIVATION_NEEDED = "activation_needed"
  # A text-clean merge failed the import probe and was rolled back to the
  # previous served commit; the update needs a repair pass before it can land.
  ROLLED_BACK = "rolled_back"


class PlatformStatus(TypedDict):
  """Response shape for ``GET /api/platform/status``."""

  state: str
  available: bool
  needs_restart: bool
  activation: PlatformActivationImpact
  current_build_sha: str | None
  recorded_upstream_sha: str | None
  # Latest fetched origin/main commit that is already contained in local main.
  # Unlike recorded_upstream_sha, this remains correct after a manual/agent
  # merge that did not run the updater's marker-maintenance path.
  contained_upstream_sha: str | None
  seed_required: bool
  conflict_paths: list[str]
  # The resolver chat opened for an in-progress conflict, so Settings can link
  # the owner straight to it. None unless ``state == "conflict"`` AND the id was
  # recorded.
  conflict_chat_id: str | None


class PlatformApplyResult(TypedDict):
  """Response shape for ``POST /api/platform/apply``."""

  state: str
  needs_restart: bool
  activation: PlatformActivationImpact
  upstream_commit: str | None
  merge_commit: str | None
  conflict_paths: list[str]
  chat_id: str | None
  phase: str
  reconciliation: dict[str, list[str]]


class _ActivationMarker(TypedDict):
  """Validated durable activation remainder."""

  target_sha: str
  paths: list[str]


class PlatformRestartResponse(TypedDict):
  """Response shape for ``POST /api/platform/restart``."""

  status: Literal["restarting"]


class PlatformConflictResolverChatOut(TypedDict):
  """Response shape for ``POST /api/platform/conflict-resolver-chat``."""

  chat_id: str
  created: bool
  started: bool


class PlatformCommitSummary(TypedDict):
  """One incoming commit in an update preview: short sha + subject line."""

  sha: str
  subject: str


class PlatformFileChange(TypedDict):
  """One file the incoming update touches. ``insertions``/``deletions`` are None
  for a binary file (git reports ``-`` in numstat)."""

  path: str
  status: str
  insertions: int | None
  deletions: int | None


class PlatformUpdatePreview(TypedDict):
  """Response shape for ``GET /api/platform/update-preview``.

  The upstream-side changes ``origin/main`` brings relative to the served clone,
  so the owner can review what a clean Apply would pull BEFORE applying. ``diff``
  is capped at :data:`MAX_PREVIEW_DIFF_CHARS`; ``files``/``commits`` stay small
  and are the compact default the review sheet renders."""

  state: str
  available: bool
  current_sha: str | None
  target_sha: str | None
  # Stable identity for this exact current->target review. Apply recomputes it
  # and rejects a changed local tip or substituted target instead of silently
  # installing bytes other than the ones represented by this preview.
  plan_id: str | None
  activation: PlatformActivationImpact
  total_commits: int
  commits_truncated: bool
  commits: list[PlatformCommitSummary]
  files: list[PlatformFileChange]
  diff: str | None
  diff_truncated: bool
  conflict_paths: list[str]


@dataclass(frozen=True)
class ReconcileResult:
  """Outcome of a single :func:`reconcile_clone` pass.

  ``status`` is one of ``up_to_date`` (origin already integrated), ``updated``
  (fast-forward or merge applied and the import probe passed), ``conflict``
  (merge conflicted, aborted, serving the pre sha), ``rolled_back`` (text-clean
  merge failed the import probe, reset to the pre sha), ``offline`` (fetch
  failed — kept serving unchanged), ``skipped`` (not a reconcilable clone), or
  ``error`` (an unexpected git failure was caught and the served tree reset to
  the pre sha).
  ``pre_sha`` is the served commit before the pass; ``new_sha`` the served commit
  after (== ``pre_sha`` unless ``updated``); ``target_sha`` the resolved
  ``origin/main``.
  """

  status: str
  pre_sha: str | None
  new_sha: str | None
  target_sha: str | None
  conflict_paths: list[str] = field(default_factory=list)
  error: str | None = None
  # Exact reviewed release/upstream commit captured while RECONCILE_LOCK is
  # still held. Hook refresh reads every allowlisted blob from this immutable
  # generation rather than trusting a locally merged HEAD or a moving ref.
  hook_source_sha: str | None = None
  reconciliation: app_git.ReconciliationReceipt = field(
    default_factory=app_git.ReconciliationReceipt,
  )


def platform_update_progress() -> PlatformUpdateProgress:
  """Return a snapshot of the current/recent owner-triggered update operation."""
  with _PROGRESS_LOCK:
    try:
      payload = json.loads(UPDATE_PROGRESS_PATH.read_text())
      if not isinstance(payload, dict):
        raise ValueError("progress is not an object")
      return PlatformUpdateProgress(
        plan_id=(
          payload.get("plan_id")
          if isinstance(payload.get("plan_id"), str)
          else None
        ),
        target_sha=(
          payload.get("target_sha")
          if isinstance(payload.get("target_sha"), str)
          else None
        ),
        phase=(
          payload.get("phase")
          if payload.get("phase") in {phase.value for phase in PlatformUpdatePhase}
          else PlatformUpdatePhase.IDLE.value
        ),
        active=bool(payload.get("active", False)),
        error=(
          payload.get("error")
          if isinstance(payload.get("error"), str)
          else None
        ),
        updated_at=float(payload.get("updated_at", 0.0)),
      )
    except (OSError, ValueError, TypeError):
      return PlatformUpdateProgress(**_UPDATE_PROGRESS)


def _set_update_progress(
  phase: PlatformUpdatePhase,
  *,
  plan_id: str | None,
  target_sha: str | None,
  active: bool,
  error: str | None = None,
) -> None:
  """Publish one phase transition from either the event loop or worker thread."""
  with _PROGRESS_LOCK:
    _UPDATE_PROGRESS.update(
      plan_id=plan_id,
      target_sha=target_sha,
      phase=phase.value,
      active=active,
      error=error,
      updated_at=time.time(),
    )
    _atomic_write_text(
      UPDATE_PROGRESS_PATH,
      json.dumps(_UPDATE_PROGRESS, sort_keys=True),
    )


def _update_plan_id(current_sha: str, target_sha: str) -> str:
  """Deterministic identity for the exact local tip + reviewed release pair."""
  material = f"mobius-platform-update-v1\0{current_sha}\0{target_sha}".encode()
  return hashlib.sha256(material).hexdigest()


def _validate_update_plan(
  repo: Path,
  *,
  plan_id: str,
  current_sha: str,
  target_sha: str,
) -> None:
  """Reject a stale or substituted preview before reconcile mutates the tree."""
  if not re.fullmatch(r"[0-9a-f]{40,64}", current_sha or ""):
    raise PlatformUpdateError("update_plan_invalid")
  if not re.fullmatch(r"[0-9a-f]{40,64}", target_sha or ""):
    raise PlatformUpdateError("update_plan_invalid")
  if plan_id != _update_plan_id(current_sha, target_sha):
    raise PlatformUpdateError("update_plan_invalid")

  local = _local_branch(repo)
  if _rev(repo, local) != current_sha:
    raise PlatformUpdateError("update_plan_stale")
  # The reviewed target must still resolve to that exact commit object. Passing
  # the full oid onward (rather than origin/main) pins Apply even if its fetch
  # observes a newer remote head.
  resolved = _rev(repo, target_sha)
  if resolved != target_sha:
    raise PlatformUpdateError("update_plan_target_missing")


def _scrubbed_git_env(repo: Path) -> dict:
  """The isolated git env every op here runs under.

  Reuses ``app_git._git_env`` so inherited ``GIT_DIR`` / ``GIT_WORK_TREE`` /
  ``GIT_INDEX_FILE`` pointers are SCRUBBED (an inherited ``GIT_DIR`` would
  silently retarget every op at the wrong repo) and ``GIT_CEILING_DIRECTORIES``
  is pinned to the repo's parent so git can never walk up into the enclosing
  ``/data`` repo. Identical isolation to the ``app_git`` engine's own ``_run``.
  """
  return app_git._git_env(repo)


def _git(
  *args: str,
  repo: Path = PLATFORM_REPO,
  check: bool = True,
  timeout: int = _GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
  """Run ``git -C repo <args>`` in text mode under the scrubbed, ceiling-pinned
  env. ``check=False`` lets callers read a non-zero return (a merge-base miss, a
  merge conflict) instead of raising."""
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    capture_output=True, text=True, timeout=timeout, check=check,
    env=_scrubbed_git_env(repo),
  )


def _rev(repo: Path, ref: str) -> str:
  """The commit sha ``ref`` resolves to, or ``""`` if it does not resolve."""
  proc = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}",
              repo=repo, check=False)
  return proc.stdout.strip()


def _has_branch(name: str, repo: Path = PLATFORM_REPO) -> bool:
  return _git(
    "rev-parse", "--verify", "--quiet", f"refs/heads/{name}",
    repo=repo, check=False,
  ).returncode == 0


def _local_branch(repo: Path = PLATFORM_REPO) -> str:
  """The repo's actual working branch. A clone of ``origin/main`` checks out
  ``main``, but detect it rather than assume so a differently-defaulted clone
  (some git versions, a ``master`` default) still reconciles. A detached HEAD
  falls back to ``main``."""
  name = _git(
    "rev-parse", "--abbrev-ref", "HEAD", repo=repo, check=False,
  ).stdout.strip()
  return name if name and name != "HEAD" else LOCAL_BRANCH


def _head_detached(repo: Path = PLATFORM_REPO) -> bool:
  name = _git(
    "rev-parse", "--abbrev-ref", "HEAD", repo=repo, check=False,
  ).stdout.strip()
  return name == "HEAD"


def _reattach_detached_head(repo: Path, local: str) -> None:
  """Move the working branch to the current detached HEAD, preserving the
  worktree. This makes the subsequent ``commit_local`` land on the branch the
  reconcile will actually fast-forward/merge."""
  if _head_detached(repo):
    _git("checkout", "-B", local, "HEAD", repo=repo)


def _has_origin(repo: Path = PLATFORM_REPO) -> bool:
  return _git("remote", "get-url", "origin", repo=repo, check=False).returncode == 0


def _is_shallow(repo: Path = PLATFORM_REPO) -> bool:
  return _git(
    "rev-parse", "--is-shallow-repository", repo=repo, check=False,
  ).stdout.strip() == "true"


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
  """Whether ``ancestor`` is an ancestor of (or equal to) ``descendant``."""
  return _git(
    "merge-base", "--is-ancestor", ancestor, descendant, repo=repo, check=False,
  ).returncode == 0


def _unmerged_paths(repo: Path = PLATFORM_REPO) -> list[str]:
  out = _git("diff", "--name-only", "--diff-filter=U", repo=repo, check=False)
  return [p.strip() for p in out.stdout.splitlines() if p.strip()]


def _rebase_in_progress(repo: Path = PLATFORM_REPO) -> bool:
  """Legacy sequencer state left by an updater or resolver from an older build."""
  git_dir = repo / ".git"
  return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()


def _merge_in_progress(repo: Path = PLATFORM_REPO) -> bool:
  return bool(_rev(repo, "MERGE_HEAD"))


def _reconcile_in_progress(repo: Path = PLATFORM_REPO) -> bool:
  return _rebase_in_progress(repo) or _merge_in_progress(repo)


def _abort_interrupted(repo: Path = PLATFORM_REPO) -> None:
  """Abort a current merge or a legacy rebase left half-finished by a crash."""
  if _rebase_in_progress(repo):
    _git("rebase", "--abort", repo=repo, check=False)
  if _merge_in_progress(repo):
    _git("merge", "--abort", repo=repo, check=False)


def _write_reconcile_pre(sha: str) -> None:
  _atomic_write_text(RECONCILE_PRE_FLAG, sha + "\n")


def _clear_reconcile_pre() -> None:
  RECONCILE_PRE_FLAG.unlink(missing_ok=True)


def _read_reconcile_pre() -> str | None:
  if not RECONCILE_PRE_FLAG.exists():
    return None
  sha = RECONCILE_PRE_FLAG.read_text().strip()
  return sha or None


def boot_guard_clean_served_tree(repo: Path = PLATFORM_REPO) -> str:
  """Post-timeout boot guard: never let uvicorn import a half-applied tree.

  The normal reconcile path cleans up after itself. This guard is for the harder
  case where the outer shell timeout SIGKILLed that process before Python could
  abort/reset. If the transient pre-mutation marker remains, restore that exact
  committed tip. Otherwise still abort any sequencer state and hard-reset the
  working branch to its current committed tip so conflict markers cannot be
  served.
  """
  if not (repo / ".git").exists():
    return "boot_guard[skipped] no_git"
  local = _local_branch(repo)
  pre = _read_reconcile_pre()
  interrupted = _reconcile_in_progress(repo)
  _abort_interrupted(repo)
  if pre and _rev(repo, pre):
    _reset_hard_to(repo, local, pre)
    _clear_reconcile_pre()
    return f"boot_guard[reset] pre={_short(pre)}"
  if interrupted:
    _git("checkout", "-q", local, repo=repo, check=False)
    _git("reset", "--hard", local, repo=repo, check=False)
  _clear_reconcile_pre()
  return "boot_guard[clean]"


def _fetch(
  repo: Path = PLATFORM_REPO, *, refspec: str | None = None,
) -> bool:
  """Bounded ``git fetch`` of main or one explicit release refspec. Returns True on
  success, False when the fetch fails (offline / unreachable origin) — a
  non-fatal condition: the caller keeps serving the current clone and retries on
  the next boot. A hung fetch (timeout) is treated as failure, not a wedge."""
  try:
    args = ["fetch", "--no-tags", "origin"]
    if refspec is not None:
      args.append(refspec)
    proc = _git(*args, repo=repo, check=False, timeout=_FETCH_TIMEOUT)
    return proc.returncode == 0
  except (subprocess.TimeoutExpired, OSError):
    return False


def _fetch_unshallow(
  repo: Path = PLATFORM_REPO, *, refspec: str | None = None,
) -> None:
  """Deepen a shallow clone so a merge can find a real merge base. Best-effort:
  an offline/timeout failure leaves the clone shallow and the caller's merge
  either still succeeds (the base was inside the shallow window) or reports a
  conflict, which fails closed to serve-old — never a hard reset."""
  try:
    args = ["fetch", "--unshallow", "--no-tags", "origin"]
    if refspec is not None:
      args.append(refspec)
    _git(*args, repo=repo, check=False, timeout=_FETCH_TIMEOUT)
  except (subprocess.TimeoutExpired, OSError):
    pass


def _merge_target(repo: Path, target: str) -> int:
  """Merge one reviewed upstream target into the checked-out local branch.

  A single merge compares the net local and upstream trees from their shared
  base. Unlike a rebase, it neither rewrites every local commit nor makes the
  resolver discover conflicts one historical commit at a time. ``--no-ff`` is
  deliberate: this helper is called only for diverged histories, and the merge
  commit preserves both the reviewed upstream target and the complete local
  history as explicit parents. Returns 0 on a clean committed merge and a
  positive returncode on a content conflict/error; returns the distinct
  sentinel :data:`_MERGE_TIMEOUT` when the merge wedged and was aborted (no
  unmerged paths survive the abort, so the caller must classify it as an error/
  serve-old outcome rather than a content conflict). The caller owns abort +
  serve-old recovery.
  """
  env = _scrubbed_git_env(repo)
  try:
    proc = subprocess.run(
      [
        "git",
        "-c", f"user.name={app_git._GIT_NAME}",
        "-c", f"user.email={app_git._GIT_EMAIL}",
        "-C", str(repo), "merge", "--no-ff", "-m",
        f"platform: merge upstream {target[:12]}", target,
      ],
      capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=False, env=env,
    )
    return proc.returncode
  except subprocess.TimeoutExpired:
    # A wedged merge must not leave a half-merged tree: abort so the caller's
    # serve-old path is honoured. Return a distinct sentinel so the caller does
    # not read the (now empty) unmerged paths and fabricate a zero-path conflict.
    _git("merge", "--abort", repo=repo, check=False)
    return _MERGE_TIMEOUT


def _commit_equivalent_merge_tree(
  repo: Path,
  *,
  local: str,
  pre_sha: str,
  target: str,
  tree_oid: str,
) -> str:
  """Commit a proven semantic merge while preserving both real histories.

  ``merge_with_equivalent_changes`` computes the tree entirely off-worktree.
  The compare-and-swap update refuses if another writer moved the local branch
  after ``pre_sha``; only after that succeeds do we reset the checked-out tree
  to the new two-parent merge commit.  The target remains an explicit parent,
  so every later update falls back to ordinary ancestry with no side metadata.
  """
  sha = app_git._run(
    repo,
    "commit-tree", tree_oid,
    "-p", pre_sha,
    "-p", target,
    "-m", f"platform: merge equivalent upstream {target[:12]}",
  ).stdout.strip()
  _git(
    "update-ref", f"refs/heads/{local}", sha, pre_sha,
    repo=repo,
  )
  _git("reset", "--hard", sha, repo=repo)
  return sha


def _reset_hard_to(repo: Path, local: str, sha: str) -> None:
  """Return the working branch to ``sha`` (the pre-reconcile served commit),
  updating the working tree. Used to serve OLD after a conflict/rollback."""
  _git("checkout", "-q", local, repo=repo, check=False)
  _git("reset", "--hard", sha, repo=repo, check=False)


def _set_upstream(repo: Path, target: str) -> None:
  """Point the ``upstream`` marker branch at ``target`` (the last reconciled
  origin commit). ``branch -f`` creates it if absent (it never is on a real
  clone). ``upstream`` is never checked out, so force-moving it is safe."""
  _git("branch", "-f", UPSTREAM_BRANCH, target, repo=repo, check=False)


def _clear_upstream(repo: Path) -> None:
  """Remove the marker when a failed Apply started without one."""
  _git(
    "update-ref", "-d", f"refs/heads/{UPSTREAM_BRANCH}",
    repo=repo, check=False,
  )


def _import_probe(repo: Path = PLATFORM_REPO, timeout: int = _PROBE_TIMEOUT):
  """Run ``import app.main`` as a fresh subprocess with cwd the served backend.

  Single-source probe for both boot and post-merge: it MUST be a subprocess (not
  an in-process import) so the reconcile process — which already imported the OLD
  ``app.platform_update`` — validates the NEW on-disk tree without corrupting its
  own interpreter, and so cwd/env exactly mirror the uvicorn exec. The env scrubs
  ``PYTHONPATH`` (no stray path may shadow ``app``) and the ``GIT_*`` pointers,
  and keeps ``SECRET_KEY`` / ``DATABASE_URL`` / ``DATA_DIR`` so settings resolve
  as the served process does. Returns ``(ok, error)``.
  """
  backend = repo / "backend"
  env = dict(os.environ)
  for var in (
    "PYTHONPATH", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_COMMON_DIR", "GIT_NAMESPACE",
  ):
    env.pop(var, None)
  try:
    proc = subprocess.run(
      [sys.executable or "python3", "-c", "import app.main"],
      cwd=str(backend), capture_output=True, text=True, timeout=timeout, env=env,
    )
  except subprocess.TimeoutExpired:
    return False, f"import probe timed out after {timeout}s"
  except OSError as exc:
    return False, f"import probe could not run: {exc!r}"
  if proc.returncode == 0:
    return True, ""
  # Keep the tail of stderr (the traceback's final lines carry the real cause).
  return False, (proc.stderr or proc.stdout or "").strip()[-2000:]


@contextlib.contextmanager
def _reconcile_flock():
  """Hold the cross-process reconcile lock (see :data:`RECONCILE_LOCK`). Released
  on context exit AND on process death (the fd closes), so a killed boot
  reconcile never leaves the lock held."""
  RECONCILE_LOCK.parent.mkdir(parents=True, exist_ok=True)
  fd = os.open(str(RECONCILE_LOCK), os.O_CREAT | os.O_RDWR, 0o644)
  try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    yield
  finally:
    try:
      fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
      os.close(fd)


def _atomic_write_text(path: Path, content: str) -> None:
  """Atomically publish one internal marker with owner-only permissions."""
  path.parent.mkdir(parents=True, exist_ok=True)
  fd, temporary = tempfile.mkstemp(
    dir=path.parent,
    prefix=f".{path.name}.",
    suffix=".tmp",
  )
  try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      handle.write(content)
      handle.flush()
      os.fsync(handle.fileno())
    os.replace(temporary, path)
  except BaseException:
    try:
      os.unlink(temporary)
    except OSError:
      pass
    raise


def _write_conflict_flag(
  target: str | None,
  paths: list[str],
  chat_id: str | None = None,
  merge_base: str | None = None,
) -> None:
  """Persist a conflict so Settings keeps surfacing it across reloads.

  Line 0 is the target (``origin/main``) sha; optional ``chat:<id>`` and
  ``base:<tree>`` lines record the resolver chat and a proven semantic merge
  base; the remaining lines are conflicting paths. Prefixes keep the format
  backward compatible with conflict flags written before either field existed.
  """
  body = [target or ""]
  if chat_id:
    body.append(f"chat:{chat_id}")
  if merge_base:
    body.append(f"base:{merge_base}")
  body.extend(paths)
  _atomic_write_text(CONFLICT_FLAG, "\n".join(body))


def _read_conflict_flag() -> dict | None:
  """Parse the conflict target, chat, semantic base, and paths, or return None.

  ``upstream`` is the target sha (named for backward compatibility with the
  status field, not the ``upstream`` branch)."""
  if not CONFLICT_FLAG.exists():
    return None
  lines = CONFLICT_FLAG.read_text().splitlines()
  target = lines[0].strip() if lines else ""
  chat_id: str | None = None
  merge_base: str | None = None
  paths: list[str] = []
  for line in lines[1:]:
    stripped = line.strip()
    if not stripped:
      continue
    if stripped.startswith("chat:"):
      chat_id = stripped[len("chat:"):] or None
    elif stripped.startswith("base:"):
      merge_base = stripped[len("base:"):] or None
    else:
      paths.append(stripped)
  return {
    "upstream": target or None,
    "chat_id": chat_id,
    "merge_base": merge_base,
    "paths": paths,
  }


def _write_offline_flag(error: str) -> None:
  _atomic_write_text(OFFLINE_FLAG, error or "offline")


def _write_rolled_back_flag(target: str | None, error: str | None) -> None:
  """Persist a rollback so Settings can show "needs repair". Line 0 is the target
  sha; the rest is the import error (truncated) for the log/UI."""
  body = (target or "") + "\n" + (error or "")
  _atomic_write_text(ROLLED_BACK_FLAG, body)


def _read_rolled_back_flag() -> dict | None:
  if not ROLLED_BACK_FLAG.exists():
    return None
  text = ROLLED_BACK_FLAG.read_text()
  target, _, error = text.partition("\n")
  return {"target": target.strip() or None, "error": error.strip() or None}


def current_build_sha() -> str | None:
  """The current image's build SHA: the ``BUILD_SHA`` baked into the image,
  falling back to the env var."""
  try:
    from app.config import settings
    cand = (getattr(settings, "build_sha", "") or "").strip()
    if cand and cand != "unknown":
      return cand
  except Exception:
    pass
  env = (os.environ.get("BUILD_SHA") or "").strip()
  if env and env != "unknown":
    return env
  return None


def recorded_upstream_sha(repo: Path = PLATFORM_REPO) -> str | None:
  """The commit the clone was last reconciled to — the ``upstream`` branch tip.
  Set to HEAD at clone time and advanced to ``origin/main`` on each successful
  reconcile."""
  return _rev(repo, UPSTREAM_BRANCH) or None


def _read_activation_marker() -> _ActivationMarker | None:
  """Read the current target+paths marker, including the legacy bare SHA."""
  try:
    raw = RESTART_NEEDED_FLAG.read_text(encoding="utf-8").strip()
  except (FileNotFoundError, OSError):
    return None
  if not raw:
    return None
  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError:
    # Before activation impacts existed this file held only a target SHA.  Its
    # only meaning was a backend restart, so preserve exactly that remainder.
    return {"target_sha": raw, "paths": ["backend/app"]}
  if not isinstance(parsed, dict):
    return None
  target = str(parsed.get("target_sha") or "").strip()
  paths = parsed.get("paths")
  if not isinstance(paths, list):
    return None
  clean_paths = sorted({str(path).strip() for path in paths if str(path).strip()})
  return {"target_sha": target, "paths": clean_paths}


def _write_activation_marker(target_sha: str, paths: list[str]) -> None:
  clean_paths = sorted({path.strip() for path in paths if path.strip()})
  if not clean_paths:
    RESTART_NEEDED_FLAG.unlink(missing_ok=True)
    return
  _atomic_write_text(RESTART_NEEDED_FLAG, json.dumps({
    "target_sha": target_sha or "",
    "paths": clean_paths,
  }, separators=(",", ":")))


def mark_activation_needed(target_sha: str, paths: list[str]) -> None:
  """Persist activation work, preserving any earlier host/image remainder."""
  existing = _read_activation_marker()
  carried = existing["paths"] if existing else []
  _write_activation_marker(target_sha, [*carried, *paths])


def _served_platform_sha() -> str | None:
  """Commit the running uvicorn imported from /data/platform, or None.

  ``/api/version`` already reports these sentinels. The updater reads the same
  files so Settings can notice the common agent-edit case: the platform checkout
  advanced after boot, but the live Python process is still running old modules.
  """
  try:
    if SERVING_SOURCE_FILE.read_text().strip() != "platform":
      return None
    sha = SERVING_SHA_FILE.read_text().strip()
  except Exception:
    return None
  return sha or None


def _activation_paths_between(
  repo: Path, before: str | None, after: str | None,
) -> list[str]:
  """Changed paths, failing closed to backend runtime when unreadable."""
  if before == after:
    return []
  if not before or not after:
    return ["backend/app"]
  paths = _changed_paths(repo, before, after)
  return ["backend/app"] if paths is None else paths


def _tree_change_needs_import_probe(
  repo: Path, before: str | None, after: str | None
) -> bool:
  """Whether a reconciled tree needs the defensive throwaway backend boot."""
  return platform_activation.backend_import_probe_required(
    _activation_paths_between(repo, before, after)
  )


def _pending_activation_paths(repo: Path = PLATFORM_REPO) -> list[str]:
  marker = _read_activation_marker()
  paths = list(marker["paths"]) if marker else []
  served = _served_platform_sha()
  if served:
    try:
      head = _rev(repo, _local_branch(repo))
    except Exception:
      head = None
    paths.extend(_activation_paths_between(repo, served, head))
  return sorted({str(path) for path in paths if str(path)})


def _platform_activation_impact(
  repo: Path = PLATFORM_REPO,
) -> PlatformActivationImpact:
  return platform_activation.classify_activation(_pending_activation_paths(repo))


def _complete_boot_activation(repo: Path) -> None:
  """Retire activation work this boot can prove complete.

  A fresh server always satisfies ``server_restart``.  A new image identity
  that contains the applied target also proves image/recreate work complete.
  Proxy reload and host maintenance remain explicit because the container
  cannot observe or control those external planes.
  """
  marker = _read_activation_marker()
  if not marker:
    RESTART_NEEDED_FLAG.unlink(missing_ok=True)
    return
  paths = marker["paths"]
  target = marker["target_sha"]
  build = current_build_sha()
  build_contains_target = bool(
    target and build and (_rev(repo, build) or "")
    and _is_ancestor(repo, target, build)
  )
  completed_by_image = {
    platform_activation.ActivationLevel.CONTAINER_RECREATE.value,
    platform_activation.ActivationLevel.IMAGE_REBUILD.value,
  }
  remaining: list[str] = []
  for path in paths:
    level = platform_activation.classify_activation([path])["level"]
    if level in {
      platform_activation.ActivationLevel.LIVE.value,
      platform_activation.ActivationLevel.SERVER_RESTART.value,
      # Owner Apply installed the locked deps in place before writing this
      # marker, so a fresh boot that loads the target already has them — retire
      # like a restart. A rebuild that bakes them instead retires via
      # build_contains_target below.
      platform_activation.ActivationLevel.DEPENDENCY_SYNC.value,
    }:
      continue
    if build_contains_target and level in completed_by_image:
      continue
    remaining.append(str(path))
  _write_activation_marker(target, remaining)


def _changed_paths(
  repo: Path, before: str | None, after: str | None
) -> list[str] | None:
  """Repo-relative paths changed between two commits, or None if the diff failed.

  ``--no-renames`` so a file moved OUT of a runtime dir (``git mv backend/app/x
  docs/x``) shows BOTH the deleted source and the added destination — otherwise
  rename detection reports only the destination and the classifier would miss
  that the served backend lost a module. None (diff failed) is distinct from []
  (a real, empty diff) so callers can fail closed on the former.
  """
  if not before or not after or before == after:
    return []
  proc = _git(
    "diff", "--name-only", "--no-renames", before, after, repo=repo, check=False,
  )
  if proc.returncode != 0:
    return None
  return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _touched_frontend(repo: Path, before: str | None, after: str | None) -> bool:
  return any(
    path == "frontend" or path.startswith("frontend/")
    for path in (_changed_paths(repo, before, after) or [])
  )


# pip can be slow on a small self-hosted box fetching wheels; keep the bound
# generous but finite so a wedged install still fails closed.
_DEP_SYNC_TIMEOUT = 900


def _requirements_changed(
  repo: Path, before: str | None, after: str | None
) -> bool:
  """Whether the locked Python dependency inputs changed between two commits."""
  return any(
    path in ("backend/requirements.lock", "backend/requirements.txt")
    for path in (_changed_paths(repo, before, after) or [])
  )


def _sync_python_dependencies(repo: Path) -> tuple[bool, str]:
  """Install the locked Python deps in place — the SAME command the image build
  runs — so an owner Apply lands a dependency bump without a container rebuild.

  Returns ``(ok, error_tail)`` and never raises for an operational failure.
  """
  lock = repo / "backend" / "requirements.lock"
  if not lock.is_file():
    return True, ""
  try:
    proc = subprocess.run(
      [
        sys.executable, "-m", "pip", "install", "--no-cache-dir",
        "--require-hashes", "-r", "requirements.lock",
      ],
      cwd=str(repo / "backend"),
      capture_output=True,
      text=True,
      timeout=_DEP_SYNC_TIMEOUT,
    )
  except (subprocess.TimeoutExpired, OSError) as exc:
    return False, repr(exc)[-500:]
  if proc.returncode != 0:
    detail = (proc.stderr or proc.stdout or "pip install failed").strip()
    return False, detail[-500:]
  return True, ""


def _hook_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
  """Run one bounded, non-interactive Git plumbing command for hook refresh."""
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    cwd=str(repo),
    env=_scrubbed_git_env(repo),
    capture_output=True,
    timeout=_HOOK_INSTALL_TIMEOUT,
    check=False,
  )


def _hook_command_error(proc: subprocess.CompletedProcess) -> str:
  raw = proc.stderr or proc.stdout or f"exit {proc.returncode}".encode()
  return os.fsdecode(raw).strip()[-500:]


def _stage_hook_file(hooks_dir: Path, data: bytes) -> Path:
  fd, raw_path = tempfile.mkstemp(prefix=".mobius-hook-", dir=str(hooks_dir))
  path = Path(raw_path)
  try:
    with os.fdopen(fd, "wb") as handle:
      handle.write(data)
      handle.flush()
      os.fchmod(handle.fileno(), 0o755)
      os.fsync(handle.fileno())
    return path
  except Exception:
    path.unlink(missing_ok=True)
    raise


def _read_hook_destination(path: Path) -> tuple[str, object] | None:
  """Snapshot one destination without ever following a hook symlink."""
  try:
    info = path.lstat()
  except FileNotFoundError:
    return None
  if stat.S_ISLNK(info.st_mode):
    return ("symlink", os.readlink(path))
  if not stat.S_ISREG(info.st_mode):
    raise OSError(f"hook destination is not a regular file: {path.name}")
  if info.st_size > _HOOK_MAX_BYTES:
    raise OSError(f"existing hook is unexpectedly large: {path.name}")
  return ("file", (path.read_bytes(), stat.S_IMODE(info.st_mode)))


def _restore_hook_destination(path: Path, previous: tuple[str, object] | None) -> None:
  if previous is None:
    path.unlink(missing_ok=True)
    return
  kind, value = previous
  if kind == "file":
    data, mode = value
    staged = _stage_hook_file(path.parent, data)
    os.chmod(staged, mode)
  else:
    staged = path.parent / f".mobius-hook-link-{os.getpid()}-{path.name}"
    staged.unlink(missing_ok=True)
    os.symlink(value, staged)
  os.replace(staged, path)


def _refresh_git_hooks_impl(repo: Path, source_oid: str) -> str | None:
  """Install allowlisted hooks from one pinned reviewed oid, without executing it."""
  # Preserve the rollout contract of older trees: no committed installer means
  # this checkout predates managed hooks and boot should simply skip refresh.
  enabled = _hook_git(
    repo, "cat-file", "-e", f"{source_oid}:scripts/install-hooks.sh",
  )
  if enabled.returncode != 0:
    return None

  sources: list[tuple[str, bytes]] = []
  for source, destination in _HOOK_SOURCES:
    blob = f"{source_oid}:{source}"
    size_proc = _hook_git(repo, "cat-file", "-s", blob)
    if size_proc.returncode != 0:
      raise OSError(_hook_command_error(size_proc))
    try:
      size = int(size_proc.stdout.strip())
    except (TypeError, ValueError) as exc:
      raise OSError(f"could not size committed hook {source}") from exc
    if size <= 0 or size > _HOOK_MAX_BYTES:
      raise OSError(f"committed hook has invalid size: {source}")
    # `cat-file blob` returns the committed bytes without textconv/filter
    # execution. `git show` is presentation porcelain and may consult local
    # diff-driver configuration, which is not a trusted boot-time code path.
    show = _hook_git(repo, "cat-file", "blob", blob)
    if show.returncode != 0:
      raise OSError(_hook_command_error(show))
    if len(show.stdout) != size or not show.stdout.startswith(b"#!"):
      raise OSError(f"committed hook failed verification: {source}")
    sources.append((destination, show.stdout))

  common = _hook_git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir")
  if common.returncode != 0:
    raise OSError(_hook_command_error(common))
  common_dir = Path(os.fsdecode(common.stdout.strip())).resolve(strict=True)
  hooks_dir = common_dir / "hooks"
  if hooks_dir.is_symlink():
    raise OSError("refusing symlinked git hooks directory")
  hooks_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
  if not hooks_dir.is_dir():
    raise OSError("git hooks path is not a directory")

  lock_path = hooks_dir / ".mobius-refresh.lock"
  with lock_path.open("a+b") as lock_handle:
    try:
      fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      return ""  # The concurrent refresher owns the same complete operation.

    previous = {
      name: _read_hook_destination(hooks_dir / name)
      for name, _data in sources
    }
    staged: dict[str, Path] = {}
    try:
      for name, data in sources:
        staged[name] = _stage_hook_file(hooks_dir, data)
    except Exception:
      for path in staged.values():
        path.unlink(missing_ok=True)
      raise
    replaced: list[str] = []
    try:
      try:
        for name, _data in sources:
          os.replace(staged[name], hooks_dir / name)
          replaced.append(name)
      except Exception:
        for name in reversed(replaced):
          _restore_hook_destination(hooks_dir / name, previous[name])
        raise
      # A repository-local hooksPath takes effect only after the complete set
      # exists. On refresh each destination changes by atomic inode swap, so a
      # concurrent Git process sees either the previous hook or the new hook,
      # never an absent path.
      configured = _hook_git(
        repo, "config", "--local", "core.hooksPath", str(hooks_dir),
      )
      if configured.returncode != 0:
        raise OSError(_hook_command_error(configured))
    finally:
      for path in staged.values():
        path.unlink(missing_ok=True)
  return ""


def _refresh_git_hooks(repo: Path, source_oid: str | None) -> str | None:
  """Best-effort hook refresh that is total at boot and after committed Apply."""
  if not source_oid:
    return None
  try:
    return _refresh_git_hooks_impl(repo, source_oid)
  except Exception as exc:
    return repr(exc)[:500]


def _rebuild_frontend_after_update_if_needed(
  repo: Path, res: ReconcileResult,
) -> None:
  """Rebuild served frontend assets after a clean update that changed them.

  The live edit watcher sees ordinary file saves, but git checkout/merge during
  the Settings update flow can move frontend files without a reliable watcher
  event. Without this explicit rebuild, ``/data/platform/frontend/src`` advances
  while ``dist`` keeps serving the old bundle.
  """
  if not _touched_frontend(repo, res.pre_sha, res.new_sha):
    return
  try:
    from app.frontend_watcher import rebuild_frontend_now
  except Exception as exc:
    raise RuntimeError("frontend rebuild is unavailable") from exc
  rebuild_frontend_now(
    f"platform update {_short(res.pre_sha)}->{_short(res.new_sha)}",
  )


def _roll_back_failed_frontend_build(
  repo: Path,
  res: ReconcileResult,
  previous_upstream_sha: str | None,
  error: Exception,
) -> ReconcileResult:
  """Restore the pre-Apply source generation when its frontend cannot build.

  ``reconcile`` and this rollback run under the same cross-process flock, so no
  boot update/check can observe or mutate the intermediate source tree. The
  frontend publisher already keeps the previously served ``dist`` on a failed
  candidate build; resetting the source closes the other half of that invariant.
  """
  _abort_interrupted(repo)
  if res.pre_sha:
    _reset_hard_to(repo, _local_branch(repo), res.pre_sha)
  if previous_upstream_sha:
    _set_upstream(repo, previous_upstream_sha)
  else:
    _clear_upstream(repo)
  # Apply does not create/replace the restart marker until preparation succeeds.
  # A marker already present before this attempt belongs to earlier on-disk
  # backend changes and must survive this failed frontend candidate.
  CONFLICT_FLAG.unlink(missing_ok=True)
  message = f"frontend_build_failed: {error!r}"[:2000]
  _write_rolled_back_flag(res.target_sha, message)
  _clear_reconcile_pre()
  return replace(
    res,
    status="rolled_back",
    new_sha=res.pre_sha,
    error=message,
    hook_source_sha=previous_upstream_sha,
  )


def reconcile_clone(
  repo: Path = PLATFORM_REPO,
  *,
  target_ref: str = DEFAULT_TARGET_REF,
  fetch_refspec: str | None = None,
  release_tip_ref: str | None = None,
  at_boot: bool = False,
  fetch_remote: bool = True,
  progress: Callable[[PlatformUpdatePhase], None] | None = None,
) -> ReconcileResult:
  """Reconcile the served clone onto ``target_ref``, safely.

  The one entry point for both boot and owner Apply. At boot (``at_boot=True``)
  ``fetch_remote`` refreshes the moving release ref before the fresh uvicorn
  imports it. A managed image passes an explicit ``fetch_refspec`` but targets
  its baked oid; ``release_tip_ref`` proves that oid still belongs to the fetched
  channel before any working-tree mutation. Owner Apply passes a full reviewed
  oid with ``fetch_remote=False`` so it neither repeats network work nor changes
  the selected release. A success
  that changes backend code still needs a restart. Never raises for an
  operational failure (offline, conflict, import-broken) — it returns a
  :class:`ReconcileResult` describing the outcome and always leaves
  ``/data/platform`` in a clean, served state (either the update, or the pre-
  reconcile code).
  """
  if not (repo / ".git").exists():
    return ReconcileResult("skipped", None, None, None, error="no_git")

  local = _local_branch(repo)
  # Crash-safety FIRST: a mid-reconcile crash must be aborted before anything reads
  # the tree, so we reconcile from the committed pre-crash tip.
  _abort_interrupted(repo)
  pre = _rev(repo, local)

  # A boot satisfies source-restart work, but must not erase image/proxy/host
  # requirements the process cannot perform or even observe.
  if at_boot:
    _complete_boot_activation(repo)

  if not _has_origin(repo):
    return ReconcileResult("skipped", pre, pre, None, error="no_origin")

  if fetch_remote:
    if progress:
      progress(PlatformUpdatePhase.FETCHING)
    fetched = (
      _fetch(repo)
      if fetch_refspec is None
      else _fetch(repo, refspec=fetch_refspec)
    )
    if not fetched:
      # Offline is non-fatal: keep serving the current clone, retry next boot.
      _write_offline_flag("fetch_failed")
      return ReconcileResult("offline", pre, pre, None, error="fetch_failed")
    OFFLINE_FLAG.unlink(missing_ok=True)

  target = _rev(repo, target_ref)
  if release_tip_ref is not None:
    # An explicit channel fetch may leave an old image's baked commit outside a
    # shallow boundary. Deepen only that same channel, then require both the
    # exact object and ancestry from it to the freshly fetched channel tip. A
    # force-moved/misconfigured branch therefore serves the old clean tree; it
    # never falls back to main or silently applies the moving tip.
    release_tip = _rev(repo, release_tip_ref)
    membership_proven = bool(
      target
      and release_tip
      and _is_ancestor(repo, target, release_tip)
    )
    if not membership_proven and _is_shallow(repo):
      _fetch_unshallow(repo, refspec=fetch_refspec)
      target = _rev(repo, target_ref)
      release_tip = _rev(repo, release_tip_ref)
      membership_proven = bool(
        target
        and release_tip
        and _is_ancestor(repo, target, release_tip)
      )
    if not target:
      _write_offline_flag("release_target_missing")
      return ReconcileResult(
        "error", pre, pre, None, error="release_target_missing",
      )
    if not membership_proven:
      _write_offline_flag("release_target_outside_ref")
      return ReconcileResult(
        "error", pre, pre, target, error="release_target_outside_ref",
      )
  if not target:
    _write_offline_flag("no_target_ref")
    return ReconcileResult("offline", pre, pre, None, error="no_target_ref")

  reconciliation = app_git.ReconciliationReceipt()

  # Already integrated: local main contains origin/main. Nothing to apply. Sync
  # the upstream marker and clear any stale conflict/rollback flag (this target
  # is fully in main, so a prior conflict/rollback for it is moot). The working
  # tree is untouched — any uncommitted local edits stay on disk.
  if _is_ancestor(repo, target, local):
    try:
      app_git.retire_landed_equivalent_changes(repo, target)
    except Exception:
      log.warning(
        "platform: could not retire contribution provenance",
        exc_info=True,
      )
    _set_upstream(repo, target)
    CONFLICT_FLAG.unlink(missing_ok=True)
    ROLLED_BACK_FLAG.unlink(missing_ok=True)
    return ReconcileResult("up_to_date", pre, pre, target, error=None)

  if progress:
    progress(PlatformUpdatePhase.RECONCILING)
  # A deploy advanced origin beyond committed main. Commit any uncommitted edits
  # FIRST so neither the fast-forward reset nor the merge can discard them.
  _reattach_detached_head(repo, local)
  app_git.commit_local(repo, "platform: local edits before reconcile")
  pre = _rev(repo, local)  # now includes the just-committed edits
  if pre:
    _write_reconcile_pre(pre)

  # From here on the working tree is mutated. The served tree MUST end at either
  # the update or exactly PRE — never a half-applied state — so any UNEXPECTED
  # git failure fails closed: abort anything in progress and hard-reset to PRE.
  # (The conflict/rollback branches below return normally; only a real error
  # reaches the except.)
  try:
    # Do not unshallow a clean instance merely because it is many releases
    # behind. The normal fetch has already transferred the new first-parent
    # chain, so Git can prove the overwhelmingly common fast-forward directly.
    # Only a shallow clone whose ancestry is still ambiguous needs the expensive
    # full-history fallback before we choose between reset and merge.
    fast_forward = bool(pre) and _is_ancestor(repo, pre, target)
    if _is_shallow(repo) and not fast_forward:
      if progress:
        progress(PlatformUpdatePhase.FETCHING)
      if fetch_refspec is None:
        _fetch_unshallow(repo)
      else:
        _fetch_unshallow(repo, refspec=fetch_refspec)
      if progress:
        progress(PlatformUpdatePhase.RECONCILING)
      fast_forward = bool(pre) and _is_ancestor(repo, pre, target)
    _git("checkout", "-q", local, repo=repo, check=False)
    if fast_forward:
      # main is fully contained in target (every commit on main is in target), so
      # a fast-forward is PROVABLY loss-free. This is decided by ANCESTRY, never by
      # an `upstream` marker that could drift and let `reset --hard` silently
      # discard committed local edits.
      _git("reset", "--hard", target, repo=repo)
      reconciliation = app_git.describe_reconciliation(
        repo, pre, target, local=pre,
      )
    else:
      ordinary_base = _git(
        "merge-base", pre, target, repo=repo, check=False,
      ).stdout.strip()
      if ordinary_base:
        reconciliation = app_git.describe_reconciliation(
          repo, ordinary_base, target, local=pre,
        )
      # Main and target diverged: merge the reviewed upstream tree ONCE. This
      # preserves local commit identities and makes one resolver pass see every
      # net conflict instead of stopping once per historical local commit.
      rc = _merge_target(repo, target)
      if rc != 0:
        # NEVER leave a half-merged tree. Read the unmerged paths BEFORE the
        # abort clears them.
        paths = _unmerged_paths(repo)
        _git("merge", "--abort", repo=repo, check=False)
        _reset_hard_to(repo, local, pre)  # belt-and-braces: ensure main == PRE
        # A wedged merge (timeout sentinel) — or, defensively, ANY nonzero
        # result that produced no unmerged paths — is NOT a reviewable content
        # conflict: ``_merge_target`` already aborted it, so there is nothing for
        # a resolver chat to reconcile. Classify it as an error/serve-old outcome
        # (reset to PRE, no conflict flag, no resolver chat) rather than
        # fabricating a zero-path conflict.
        if rc == _MERGE_TIMEOUT or not paths:
          CONFLICT_FLAG.unlink(missing_ok=True)
          ROLLED_BACK_FLAG.unlink(missing_ok=True)
          _clear_reconcile_pre()
          err = "merge_timeout" if rc == _MERGE_TIMEOUT else "merge_failed"
          return ReconcileResult("error", pre, pre, target, error=err)
        # Before asking the owner to resolve a content conflict, let the shared
        # app/platform provenance engine replace Git's historical base with a
        # semantic base made ONLY from reviewed changes proven to come from this
        # local history and to have landed in this target history.  This is the
        # squash/batch case: ordinary Git sees two edits to the same lines, while
        # the causal record says one side is the already-contributed predecessor
        # of the other.  The engine is off-tree and fail-closed; no proof (or a
        # genuine later conflict) returns the reduced conflict set plus its
        # semantic base so the resolver preserves every proven elimination.
        equivalent = app_git.merge_with_equivalent_changes(repo, pre, target)
        if equivalent is not None:
          reconciliation = equivalent.reconciliation
        if equivalent is not None and equivalent.merged_tree_oid:
          _commit_equivalent_merge_tree(
            repo,
            local=local,
            pre_sha=pre,
            target=target,
            tree_oid=equivalent.merged_tree_oid,
          )
        else:
          # Genuine/unproven content conflict: record it, clear any stale
          # rollback flag, and let the caller open a resolver chat. A partially
          # proven semantic base narrows both the path list and the real merge
          # the resolver will materialize; no proof keeps the ordinary result.
          conflict_paths = (
            equivalent.conflict_paths
            if equivalent is not None and equivalent.conflict_paths
            else paths
          )
          merge_base = (
            equivalent.merge_base_oid if equivalent is not None else None
          )
          if merge_base:
            _git(
              "update-ref", _CONFLICT_MERGE_BASE_REF, merge_base,
              repo=repo,
            )
          _write_conflict_flag(
            target, conflict_paths, merge_base=merge_base,
          )
          ROLLED_BACK_FLAG.unlink(missing_ok=True)
          _clear_reconcile_pre()
          return ReconcileResult(
            "conflict", pre, pre, target,
            conflict_paths=conflict_paths,
            reconciliation=(
              equivalent.reconciliation
              if equivalent is not None
              else app_git.describe_reconciliation(
                repo,
                ordinary_base,
                target,
                local=pre,
                conflict_paths=conflict_paths,
              )
            ),
          )

    # Dependency sync (owner Apply only): install the newly locked Python deps
    # in place — the same command the image build runs — so the merged code can
    # import them and this update lands with no container rebuild. Boot never
    # does this: a fresh image already has them, boot must stay fast/offline, and
    # a boot that merged a dep bump instead fails the probe below and rolls back
    # (correct — the deps are not installed until an owner Apply). A pip failure
    # is fail-closed exactly like a failed probe: reset to PRE and serve the old
    # tree.
    if not at_boot and _requirements_changed(repo, pre, _rev(repo, local)):
      if progress:
        progress(PlatformUpdatePhase.BUILDING)
      deps_ok, deps_err = _sync_python_dependencies(repo)
      if not deps_ok:
        _reset_hard_to(repo, local, pre)
        _write_rolled_back_flag(target, f"dependency_install_failed: {deps_err}")
        CONFLICT_FLAG.unlink(missing_ok=True)
        _clear_reconcile_pre()
        return ReconcileResult("rolled_back", pre, pre, target, error=deps_err)

    # Post-reconcile import probe: a text-clean ff/merge can still produce a
    # tree that fails to import (upstream dropped a module a local edit imports;
    # a bad deploy). Roll back to the previous served commit rather than serve it
    # broken. Skip the ~60s throwaway boot when the reconcile touched NO served
    # backend code (frontend/tests/docs/scripts only): the backend tree is then
    # byte-identical, so the probe would only re-prove an unchanged import. This
    # Constitution-only changes need a real server restart to refresh the
    # process cache, but cannot break Python imports, so they skip this probe.
    if _tree_change_needs_import_probe(repo, pre, _rev(repo, local)):
      if progress:
        progress(PlatformUpdatePhase.VALIDATING)
      ok, err = _import_probe(repo)
      if not ok:
        _reset_hard_to(repo, local, pre)
        _write_rolled_back_flag(target, err)
        CONFLICT_FLAG.unlink(missing_ok=True)
        _clear_reconcile_pre()
        return ReconcileResult("rolled_back", pre, pre, target, error=err)
  except Exception as exc:  # unexpected git failure — never serve a half-tree
    _abort_interrupted(repo)
    _reset_hard_to(repo, local, pre)
    _clear_reconcile_pre()
    return ReconcileResult("error", pre, pre, target, error=repr(exc))

  # Success: main now carries the update plus all local edits. Advance
  # the upstream marker and clear conflict/rollback flags. At boot the fresh
  # uvicorn imports this directly (clear the restart flag — the boot IS the
  # restart the flag would ask for); an owner Apply marks a restart via the
  # caller.
  new_sha = _rev(repo, local)
  try:
    app_git.retire_landed_equivalent_changes(repo, target)
  except Exception:
    # The target is already committed and validated.  A stale provenance ref is
    # harmless and can be retired by the next update; never turn housekeeping
    # into a false failed-update report after source has moved.
    log.warning("platform: could not retire contribution provenance", exc_info=True)
  _set_upstream(repo, target)
  CONFLICT_FLAG.unlink(missing_ok=True)
  ROLLED_BACK_FLAG.unlink(missing_ok=True)
  _clear_reconcile_pre()
  return ReconcileResult(
    "updated", pre, new_sha, target, error=None,
    reconciliation=reconciliation,
  )


def _reconcile_under_lock(
  repo: Path,
  at_boot: bool,
  *,
  target_ref: str = DEFAULT_TARGET_REF,
  fetch_refspec: str | None = None,
  release_tip_ref: str | None = None,
  plan_id: str | None = None,
  current_sha: str | None = None,
  progress: Callable[[PlatformUpdatePhase], None] | None = None,
  prepare_frontend: bool = False,
) -> ReconcileResult:
  """Hold :data:`RECONCILE_LOCK` around one reconcile so the boot subprocess and
  the running uvicorn's Apply can never run two reconciles on the same repo.

  Owner Apply additionally validates its immutable review plan and prepares the
  frontend while the same lock is held. This is still a live-tree updater; the
  future supervisor/generation implementation should move this transaction out
  of uvicorn while preserving the exact-target + phase contract.
  """
  with _reconcile_flock():
    if plan_id is not None:
      if current_sha is None:
        raise PlatformUpdateError("update_plan_invalid")
      _validate_update_plan(
        repo,
        plan_id=plan_id,
        current_sha=current_sha,
        target_sha=target_ref,
      )
    previous_upstream_sha = _rev(repo, UPSTREAM_BRANCH) or None
    reconcile_kwargs = {
      "target_ref": target_ref,
      "at_boot": at_boot,
      # A reviewed Apply already proved the immutable object exists. Fetching a
      # moving remote again adds latency and was the original TOCTOU bug; boot
      # keeps the normal refresh path. A shallow merge may still deepen below.
      "fetch_remote": plan_id is None,
      "progress": progress,
    }
    # Keep the default/main call shape unchanged for old monkeypatch consumers
    # and, more importantly, keep its git fetch behavior byte-for-byte normal.
    if fetch_refspec is not None:
      reconcile_kwargs["fetch_refspec"] = fetch_refspec
    if release_tip_ref is not None:
      reconcile_kwargs["release_tip_ref"] = release_tip_ref
    result = reconcile_clone(repo, **reconcile_kwargs)
    if (
      result.status == "updated"
      and prepare_frontend
      and _touched_frontend(repo, result.pre_sha, result.new_sha)
    ):
      if progress:
        progress(PlatformUpdatePhase.BUILDING)
      try:
        _rebuild_frontend_after_update_if_needed(repo, result)
      except Exception as exc:
        log.warning(
          "frontend build rejected platform update %s: %r",
          _short(result.target_sha),
          exc,
        )
        result = _roll_back_failed_frontend_build(
          repo, result, previous_upstream_sha, exc,
        )
    # `upstream` is moved only by a successful/contained reconcile to the
    # fetched release target. Capture its immutable oid before releasing the
    # cross-process lock; local commits on main are intentionally not a
    # hook trust transition.
    return replace(
      result,
      hook_source_sha=_rev(repo, UPSTREAM_BRANCH) or None,
    )


def _short(sha: str | None) -> str:
  return sha[:8] if sha else "-"


def reconcile_clone_sync() -> str:
  """Boot entry point (called from a throwaway ``python3 -c`` as mobius, cwd the
  served backend normally or the image-baked backend on a managed channel).
  Runs one locked reconcile and returns a one-line summary for
  the entrypoint log. Never raises — a reconcile failure must not brick boot; the
  worst case leaves the pre-reconcile code serving and a flag set."""
  try:
    channel = _runtime_release_channel()
    if channel.configured:
      res = _reconcile_under_lock(
        PLATFORM_REPO,
        at_boot=True,
        target_ref=channel.target_ref,
        fetch_refspec=channel.fetch_refspec,
        release_tip_ref=channel.tracking_ref,
      )
    else:
      res = _reconcile_under_lock(PLATFORM_REPO, at_boot=True)
    # Even an offline/conflict pass leaves a complete served tree on disk. Hook
    # refresh is local-only, so do it on every boot rather than waiting for a
    # successful fetch that may be unrelated to the stale installed copy.
    hook_refresh = _refresh_git_hooks(PLATFORM_REPO, res.hook_source_sha)
    summary = (
      f"reconcile[{res.status}] pre={_short(res.pre_sha)} "
      f"new={_short(res.new_sha)} target={_short(res.target_sha)}"
    )
    if hook_refresh == "":
      summary += " hooks=refreshed"
    elif hook_refresh:
      summary += f" hooks=error:{hook_refresh}"
    if res.conflict_paths:
      summary += f" conflicts={len(res.conflict_paths)}"
    if res.error:
      summary += f" err={res.error}"
    return summary
  except Exception as exc:  # never propagate to the boot shell
    return f"reconcile[error] {exc!r}"


def managed_release_ready_sync() -> str:
  """Prove the persistent checkout contains this image's exact release.

  Normal main-channel reconcile remains best-effort. A managed-channel image
  cannot safely serve an older persistent checkout, because that checkout may
  predate the release-channel updater entirely. This strict, read-only proof is
  imported from the immutable baked backend after reconcile; every failure is
  raised so the entrypoint can serve the exact baked floor instead.
  """
  channel = _runtime_release_channel()
  if not channel.configured:
    raise PlatformUpdateError("platform_release_channel_not_configured")
  if not (PLATFORM_REPO / ".git").exists():
    raise PlatformUpdateError("platform_repo_missing")
  target = _rev(PLATFORM_REPO, channel.target_ref)
  if not target:
    raise PlatformUpdateError("managed_release_target_missing")
  head = _rev(PLATFORM_REPO, "HEAD")
  if not head or not _is_ancestor(PLATFORM_REPO, target, head):
    raise PlatformUpdateError("managed_release_target_not_integrated")
  return (
    f"managed_release[ready] head={_short(head)} "
    f"target={_short(target)}"
  )


def _state_for_activation(
  impact: PlatformActivationImpact,
) -> PlatformUpdateState:
  level = impact["level"]
  if level in {
    platform_activation.ActivationLevel.SERVER_RESTART.value,
    # Deps were installed in place during Apply; only the restart remains.
    platform_activation.ActivationLevel.DEPENDENCY_SYNC.value,
  }:
    return PlatformUpdateState.RESTART_NEEDED
  if level == platform_activation.ActivationLevel.LIVE.value:
    return PlatformUpdateState.UP_TO_DATE
  return PlatformUpdateState.ACTIVATION_NEEDED


def boot_guard_sync() -> str:
  """Shell entry point run after reconcile and before uvicorn.

  Unlike the best-effort reconcile, this deliberately propagates failures: the
  guard is the final proof that the served tree is clean. Booting after a guard
  error would silently bypass the safety boundary it exists to enforce.
  """
  with _reconcile_flock():
    return boot_guard_clean_served_tree(PLATFORM_REPO)


def platform_status(repo: Path = PLATFORM_REPO) -> PlatformStatus:
  """Compute update availability on demand (no daemon, no polling, no fetch).

  Availability is an EXACT ancestry check against ``origin/main``. Deployment
  release channels constrain boot to the image's baked commit, but never change
  the release the owner checks, reviews, or applies here. Conflict and rolled-
  back states take precedence over a bare "available".
  """
  image_sha = current_build_sha()
  upstream_sha = recorded_upstream_sha(repo)
  conflict = CONFLICT_FLAG.exists() or _reconcile_in_progress(repo)
  rolled_back = ROLLED_BACK_FLAG.exists()
  activation = _platform_activation_impact(repo)
  activation_state = _state_for_activation(activation)
  restart_needed = activation["level"] in {
    platform_activation.ActivationLevel.SERVER_RESTART.value,
    platform_activation.ActivationLevel.DEPENDENCY_SYNC.value,
  }
  local = _local_branch(repo)
  target = _rev(repo, DEFAULT_TARGET_REF)
  target_contained = bool(target) and _is_ancestor(repo, target, local)
  contained_upstream_sha = target if target_contained else upstream_sha

  if conflict:
    flag = _read_conflict_flag() or {}
    paths = flag.get("paths") or _unmerged_paths(repo)
    return PlatformStatus(
      state=PlatformUpdateState.CONFLICT.value, available=False,
      needs_restart=restart_needed, activation=activation,
      current_build_sha=image_sha,
      recorded_upstream_sha=upstream_sha,
      contained_upstream_sha=contained_upstream_sha,
      seed_required=False,
      conflict_paths=paths, conflict_chat_id=flag.get("chat_id"),
    )

  available = bool(target) and not target_contained

  if rolled_back:
    # An update is available but its last apply failed the import probe.
    state = PlatformUpdateState.ROLLED_BACK
    available = True
  elif activation_state is not PlatformUpdateState.UP_TO_DATE:
    state = activation_state
  elif available:
    state = PlatformUpdateState.AVAILABLE
  else:
    state = PlatformUpdateState.UP_TO_DATE

  return PlatformStatus(
    state=state.value, available=available, needs_restart=restart_needed,
    activation=activation,
    current_build_sha=image_sha, recorded_upstream_sha=upstream_sha,
    contained_upstream_sha=contained_upstream_sha,
    seed_required=False, conflict_paths=[], conflict_chat_id=None,
  )


def check_for_updates(repo: Path = PLATFORM_REPO) -> PlatformStatus:
  """Owner-triggered "Check for updates": fetch origin, THEN report availability.

  :func:`platform_status` is deliberately fetch-free — it reads
  ``origin/main`` left by the last boot/owner fetch — so this is
  the one on-demand path that refreshes that ref without waiting for a reboot.
  A missing clone/origin or failed fetch is an explicit error: returning status
  from a stale remote-tracking ref would tell the owner "No updates found" when
  the service never actually reached upstream. The fetch runs under
  :data:`RECONCILE_LOCK` so it can never fetch mid-reconcile. The working tree
  and ``main`` are untouched — a fetch only advances remote-tracking refs, so
  this is safe to run anytime and never mutates the served code.
  """
  if not (repo / ".git").exists():
    raise PlatformUpdateError("platform_repo_missing")
  if not _has_origin(repo):
    raise PlatformUpdateError("platform_origin_missing")
  with _reconcile_flock():
    # Do not rely on remote.origin.fetch: the external-recovery transition
    # deliberately produced single-branch stack checkouts whose configured
    # refspec cannot advance origin/main.
    if not _fetch(repo, refspec=OWNER_UPDATE_FETCH_REFSPEC):
      raise PlatformUpdateError("platform_fetch_failed")
    target = _rev(repo, DEFAULT_TARGET_REF)
    local = _local_branch(repo)
    if target and _is_ancestor(repo, target, local):
      _set_upstream(repo, target)
  return platform_status(repo)


def empty_platform_update_preview(
  *, current_sha: str | None = None, target_sha: str | None = None,
) -> PlatformUpdatePreview:
  """A preview carrying no incoming changes — the up-to-date / unreadable case.
  The review sheet reads ``available``/``files`` and shows "nothing to review"
  rather than an empty diff panel."""
  return PlatformUpdatePreview(
    state=PlatformUpdateState.UP_TO_DATE.value, available=False,
    current_sha=current_sha, target_sha=target_sha, plan_id=None,
    activation=platform_activation.classify_activation([]),
    total_commits=0, commits_truncated=False,
    commits=[], files=[], diff=None, diff_truncated=False, conflict_paths=[],
  )


def _preview_commits(
  repo: Path, base: str, target: str,
) -> list[PlatformCommitSummary]:
  """The commits ``target`` adds beyond ``base`` (newest first), capped."""
  proc = _git(
    "log", f"--max-count={_PREVIEW_COMMIT_LIMIT}", "--format=%h%x1f%s",
    f"{base}..{target}", repo=repo, check=False,
  )
  if proc.returncode != 0:
    return []
  commits: list[PlatformCommitSummary] = []
  for line in proc.stdout.splitlines():
    if "\x1f" not in line:
      continue
    sha, subject = line.split("\x1f", 1)
    commits.append(PlatformCommitSummary(sha=sha.strip(), subject=subject.strip()))
  return commits


def _preview_commit_count(repo: Path, base: str, target: str) -> int:
  """Exact incoming commit count, independent of the rendered-list cap."""
  proc = _git(
    "rev-list", "--count", f"{base}..{target}", repo=repo, check=False,
  )
  if proc.returncode != 0:
    return 0
  try:
    return max(0, int(proc.stdout.strip()))
  except ValueError:
    return 0


def _preview_files(repo: Path, base: str, target: str) -> list[PlatformFileChange]:
  """Per-file change summary for ``base..target``.

  ``--name-status`` is authoritative for the path list + status letter (A/M/D/R);
  ``--numstat`` counts are merged in best-effort, keyed on the same path. A rename
  spells its numstat path differently, so its counts stay None — a display nicety,
  not load-bearing (the status letter still reads ``R``)."""
  by_path: dict[str, PlatformFileChange] = {}
  order: list[str] = []
  name_status = _git(
    "diff", "--name-status", f"{base}..{target}", repo=repo, check=False,
  )
  if name_status.returncode == 0:
    for line in name_status.stdout.splitlines():
      parts = line.split("\t")
      if len(parts) < 2:
        continue
      status = (parts[0].strip() or "M")[:1]
      path = parts[-1].strip()  # rename: last field is the new path
      if not path or path in by_path:
        continue
      by_path[path] = PlatformFileChange(
        path=path, status=status, insertions=None, deletions=None,
      )
      order.append(path)
  numstat = _git(
    "diff", "--numstat", f"{base}..{target}", repo=repo, check=False,
  )
  if numstat.returncode == 0:
    for line in numstat.stdout.splitlines():
      parts = line.split("\t")
      if len(parts) < 3:
        continue
      record = by_path.get(parts[-1].strip())
      if record is None:
        continue
      ins, dele = parts[0], parts[1]
      record["insertions"] = None if ins == "-" else (int(ins) if ins.isdigit() else None)
      record["deletions"] = None if dele == "-" else (int(dele) if dele.isdigit() else None)
  return [by_path[path] for path in order]


def _preview_diff(repo: Path, base: str, target: str) -> tuple[str | None, bool]:
  """The unified diff for ``base..target``, capped at :data:`MAX_PREVIEW_DIFF_CHARS`.
  Returns ``(diff, truncated)``; ``(None, False)`` when git could not produce it."""
  proc = _git(
    "diff", "--no-ext-diff", f"{base}..{target}", repo=repo, check=False,
  )
  if proc.returncode != 0:
    return None, False
  text = proc.stdout
  if len(text) > MAX_PREVIEW_DIFF_CHARS:
    return text[:MAX_PREVIEW_DIFF_CHARS], True
  return (text or None), False


def platform_update_preview(repo: Path = PLATFORM_REPO) -> PlatformUpdatePreview:
  """Read-only preview of the incoming platform update, for the Settings review
  step before Apply (fetch-free, never mutates the tree).

  Shows the upstream-side changes ``origin/main`` brings since the shared merge
  base — local edits are excluded, so the owner reviews exactly what a clean Apply
  would pull. Availability is the same ancestry check :func:`platform_status`
  uses; an up-to-date instance returns an empty preview. Degrades to an empty
  preview (never raises) when the clone or ancestry can't be read, so it can never
  break Settings."""
  # A missing/non-clone tree has no source snapshot to protect. Return before
  # touching the durable /data lock so read-only diagnostics and recovery
  # surfaces still degrade cleanly when DATA_DIR itself is unavailable.
  # Actual clones take the lock below; the unlocked builder repeats this check
  # after acquisition, which closes a removal/reseed race.
  if not (repo / ".git").exists():
    return empty_platform_update_preview()
  with _reconcile_flock():
    return _platform_update_preview_unlocked(repo)


def _platform_update_preview_unlocked(repo: Path) -> PlatformUpdatePreview:
  """Build one preview while the reconcile lock holds its source snapshot."""
  if not (repo / ".git").exists() or not _has_origin(repo):
    return empty_platform_update_preview()
  local = _local_branch(repo)
  local_sha = _rev(repo, local) or None
  target = _rev(repo, DEFAULT_TARGET_REF) or None
  available = bool(target) and not _is_ancestor(repo, target, local)
  if not target or not available:
    return empty_platform_update_preview(
      current_sha=local_sha, target_sha=target,
    )
  base = _git(
    "merge-base", local, target, repo=repo, check=False,
  ).stdout.strip() or local_sha
  if not base:
    # No shared base and no local tip to diff against — surface availability
    # without a diff rather than raising.
    return PlatformUpdatePreview(
      state=PlatformUpdateState.AVAILABLE.value, available=True,
      current_sha=local_sha, target_sha=target,
      plan_id=_update_plan_id(local_sha, target) if local_sha else None,
      activation=platform_activation.classify_activation(["backend/app"]),
      total_commits=0, commits_truncated=False, commits=[], files=[],
      diff=None, diff_truncated=False, conflict_paths=[],
    )
  diff, truncated = _preview_diff(repo, base, target)
  commits = _preview_commits(repo, base, target)
  total_commits = _preview_commit_count(repo, base, target)
  conflict = _read_conflict_flag() or {}
  activation_paths = _activation_paths_between(repo, base, target)
  return PlatformUpdatePreview(
    state=PlatformUpdateState.AVAILABLE.value, available=True,
    current_sha=local_sha, target_sha=target,
    plan_id=_update_plan_id(local_sha, target) if local_sha else None,
    activation=platform_activation.classify_activation(activation_paths),
    total_commits=total_commits,
    commits_truncated=total_commits > len(commits),
    commits=commits,
    files=_preview_files(repo, base, target),
    diff=diff, diff_truncated=truncated,
    conflict_paths=conflict.get("paths") or [],
  )


async def apply_platform_update(
  db: Session,
  *,
  plan_id: str,
  current_sha: str,
  target_sha: str,
  repo: Path = PLATFORM_REPO,
) -> PlatformApplyResult:
  """Owner-triggered reconcile with an explicit activation remainder.

  Clean source can be live, restartable, or require an external deployment
  action.  Conflict/rollback leave the previous runtime and its remainder
  untouched.  The backend never invokes Docker, Caddy, or a provider control
  plane.
  """
  async with _APPLY_LOCK:
    _set_update_progress(
      PlatformUpdatePhase.PREPARING,
      plan_id=plan_id,
      target_sha=target_sha,
      active=True,
    )

    def publish_progress(phase: PlatformUpdatePhase) -> None:
      _set_update_progress(
        phase,
        plan_id=plan_id,
        target_sha=target_sha,
        active=True,
      )

    try:
      existing_conflict = await asyncio.to_thread(_read_conflict_flag) or {}
      res = await asyncio.to_thread(
        _reconcile_under_lock,
        repo,
        False,
        target_ref=target_sha,
        plan_id=plan_id,
        current_sha=current_sha,
        progress=publish_progress,
        prepare_frontend=True,
      )
      chat_id: str | None = None

      def record_current_activation(head: str | None) -> PlatformActivationImpact:
        served = _served_platform_sha()
        changed_paths = _activation_paths_between(repo, served, head)
        incoming_impact = platform_activation.classify_activation(changed_paths)
        if incoming_impact["level"] != platform_activation.ActivationLevel.LIVE.value:
          mark_activation_needed(head or "", changed_paths)
        return _platform_activation_impact(repo)

      if res.status == "updated":
        publish_progress(PlatformUpdatePhase.FINALIZING)
        hook_refresh = await asyncio.to_thread(
          _refresh_git_hooks, repo, res.hook_source_sha,
        )
        if hook_refresh:
          log.warning("git hook refresh failed after platform update: %s", hook_refresh)
        # Compare what this process imported to the new head, not only the
        # incoming target: local commits made while uvicorn ran are part of the
        # same activation remainder.
        activation = record_current_activation(res.new_sha)
        state = _state_for_activation(activation)
      elif res.status == "conflict":
        # Keep the resolver gated behind the owner's next click. A conflict pass
        # rewrites the flag with target + paths, so preserve a previously opened
        # chat only when it belongs to this same target.
        target = res.target_sha or existing_conflict.get("upstream")
        existing_chat_id = (
          existing_conflict.get("chat_id")
          if target and existing_conflict.get("upstream") == target
          else None
        )
        chat_id = existing_chat_id
        _write_conflict_flag(
          target,
          res.conflict_paths or existing_conflict.get("paths") or [],
          existing_chat_id,
        )
        state = PlatformUpdateState.CONFLICT
      elif res.status == "rolled_back":
        state = PlatformUpdateState.ROLLED_BACK
      elif res.status == "up_to_date":
        head = _rev(repo, _local_branch(repo)) or res.pre_sha
        activation = record_current_activation(head)
        state = _state_for_activation(activation)
      else:  # offline / skipped — nothing changed; tell the UI plainly.
        raise PlatformUpdateError(res.error or res.status)

      if res.status in {"conflict", "rolled_back"}:
        activation = _platform_activation_impact(repo)

      final_phase = (
        PlatformUpdatePhase.BLOCKED
        if state in {PlatformUpdateState.CONFLICT, PlatformUpdateState.ROLLED_BACK}
        else PlatformUpdatePhase.COMPLETE
      )
      _set_update_progress(
        final_phase,
        plan_id=plan_id,
        target_sha=target_sha,
        active=False,
        error=res.error if state is PlatformUpdateState.ROLLED_BACK else None,
      )
      return PlatformApplyResult(
        state=state.value,
        needs_restart=(
          activation["level"]
          == platform_activation.ActivationLevel.SERVER_RESTART.value
        ),
        activation=activation,
        upstream_commit=res.target_sha,
        merge_commit=res.new_sha if res.status == "updated" else None,
        conflict_paths=res.conflict_paths,
        chat_id=chat_id,
        phase=final_phase.value,
        reconciliation=res.reconciliation.as_dict(),
      )
    except Exception as exc:
      _set_update_progress(
        PlatformUpdatePhase.FAILED,
        plan_id=plan_id,
        target_sha=target_sha,
        active=False,
        error=str(exc)[:500] or exc.__class__.__name__,
      )
      raise


async def create_platform_conflict_resolver_chat(
  db: Session, repo: Path = PLATFORM_REPO,
) -> PlatformConflictResolverChatOut:
  """Create or return the owner-clicked resolver chat for a platform conflict."""
  from app import models

  flag = _read_conflict_flag() or {}
  if not (CONFLICT_FLAG.exists() or _reconcile_in_progress(repo)):
    raise PlatformUpdateError("No unresolved platform update conflict.")

  existing_chat_id = flag.get("chat_id")
  if existing_chat_id:
    existing = (
      db.query(models.Chat)
      .filter(models.Chat.id == existing_chat_id)
      .filter(models.Chat.deleted_at.is_(None))
      .filter(models.Chat.created_by_app_id.is_(None))
      .first()
    )
    if existing is not None:
      return PlatformConflictResolverChatOut(
        chat_id=existing.id, created=False, started=False,
      )

  conflict_paths = flag.get("paths") or _unmerged_paths(repo)
  target_sha = flag.get("upstream") or _rev(repo, DEFAULT_TARGET_REF)
  merge_base = flag.get("merge_base")
  if not target_sha:
    raise PlatformUpdateError("Platform conflict target is unavailable.")
  result = await spawn_platform_conflict_chat(
    db, conflict_paths, target_sha, merge_base,
  )
  if result is None:
    raise PlatformUpdateError("Could not open resolver chat.")

  _write_conflict_flag(
    target_sha,
    conflict_paths,
    result["chat_id"],
    merge_base,
  )
  return result


def materialize_platform_conflict(
  target_sha: str,
  merge_base: str,
  repo: Path = PLATFORM_REPO,
) -> list[str]:
  """Start the owner-approved platform conflict from its proven semantic base."""
  target = _rev(repo, target_sha)
  base = _git(
    "rev-parse", "--verify", "--quiet", f"{merge_base}^{{tree}}",
    repo=repo, check=False,
  ).stdout.strip()
  if target != target_sha or not base:
    raise PlatformUpdateError("Platform conflict merge proof is unavailable.")
  return app_git.start_conflict_merge(
    repo,
    merge_base=base,
    local_branch=_local_branch(repo),
    upstream_branch=target,
  )


def _platform_conflict_resolver_message(
  target_sha: str,
  conflict_paths: list[str],
  merge_base: str | None = None,
) -> str:
  """Instructions bound to the exact release the owner reviewed and applied."""
  files = ", ".join(conflict_paths) if conflict_paths else "some files"
  if merge_base:
    start_merge = (
      "Start the prepared merge from its proven reviewed-change base: "
      "`cd /data/platform/backend && python3 -c \"from "
      "app.platform_update import materialize_platform_conflict as m; "
      f"print('\\\\n'.join(m('{target_sha}', '{merge_base}')))\"`. "
      "This preserves the updater's already-landed-change analysis and writes "
      "markers only for the residual conflicts."
    )
  else:
    start_merge = (
      "Resolve it with ordinary git: `git -C /data/platform merge --no-ff "
      f"{target_sha}` compares the complete local and reviewed upstream trees "
      "once and stops with every conflicting file marked."
    )
  return (
    "A platform update is ready but conflicts with local edits — the new "
    "version and the local changes both touched the same lines, so they can't "
    "merge cleanly.\n\n"
    "The clone at `/data/platform` is a real git checkout of the platform repo. "
    f"The exact reviewed version is commit `{target_sha}`; local edits are on "
    "the checked-out working branch. "
    f"Reconcile these conflicting files by hand: {files}.\n\n"
    f"{start_merge} Combine the intent of the local version and upstream's, "
    "save each file, then `git add` it and "
    "`git commit --no-edit` (this finishes the merge non-interactively from the "
    "prepared merge message). When the merge finishes, the working branch "
    "carries both histories.\n\n"
    "When the reconcile is committed, clear the flag "
    "(`rm -f /data/.platform-conflict`) and tell the owner to **restart the "
    "server** from Settings to finish. To back out instead, `git -C "
    "/data/platform merge --abort`, `rm -f /data/.platform-conflict`, and tell "
    "the owner the update was skipped."
  )


async def spawn_platform_conflict_chat(
  db: Session,
  conflict_paths: list[str],
  target_sha: str,
  merge_base: str | None = None,
) -> PlatformConflictResolverChatOut | None:
  """Open a visible agent chat to reconcile the new platform version into
  the checked-out working branch — the platform analogue of a per-app
  update-conflict resolver chat. Dedupes on a running resolver."""
  import uuid

  from app import models, providers
  from app.chat_start import start_programmatic_chat_turn
  from app.config import get_settings
  from app.push import notify_owner
  from app.run_state import running_chat_ids

  title = "Resolve platform update conflict"
  candidate_ids = [
    row.id for row in (
      db.query(models.Chat.id)
      .filter(models.Chat.title == title)
      .filter(models.Chat.deleted_at.is_(None))
      .all()
    )
  ]
  running_ids = running_chat_ids(db, candidate_ids)
  running_id = next(
    (chat_id for chat_id in candidate_ids if chat_id in running_ids),
    None,
  )
  if running_id is not None:
    return PlatformConflictResolverChatOut(
      chat_id=running_id, created=False, started=False,
    )

  owner = db.query(models.Owner).first()
  if owner is None:
    return None
  provider = providers.resolve_default_provider(
    get_settings().data_dir, owner.provider,
  )

  content = _platform_conflict_resolver_message(
    target_sha, conflict_paths, merge_base,
  )

  chat_id = str(uuid.uuid4())
  chat = models.Chat(
    id=chat_id, title=title, messages=[], pending_messages=[],
    provider=provider, created_by_app_id=None,
  )
  db.add(chat)
  db.commit()

  try:
    started = await start_programmatic_chat_turn(
      chat_id=chat_id,
      title=title,
      content=content,
      provider=provider,
    )
  finally:
    try:
      notify_owner(
        db, owner.id, title="Platform update needs conflict resolution",
        body="The platform update conflicts with local edits. Opened a chat to resolve it.",
        source_type="platform_conflict", source_id=chat_id,
        target=f"/shell/?chat={chat_id}",
      )
    except Exception:
      pass

  return PlatformConflictResolverChatOut(
    chat_id=chat_id, created=True, started=started,
  )
