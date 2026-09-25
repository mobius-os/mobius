"""Platform self-update — fetch one reviewed release and merge final source trees.

``/data/platform`` is a real ``git clone`` of the canonical repo; uvicorn serves
its backend directly (``cd /data/platform/backend && uvicorn app.main:app``).
Local ``main`` contains the exact accepted upstream commit (recorded by the
``upstream`` marker branch) plus local changes. An explicit update compares
the final local and reviewed upstream trees once, then records their resolved
net local delta as one linear commit on the reviewed target.
Owner Apply pins the exact target returned by the review plan; backend changes
then need a restart to load. Startup recovers interrupted work and runs the
installed source, without fetching or selecting another release.

Historical local commits are not replayed: many may be obsolete copies of
changes already merged upstream. Reviewed equivalence anchors can improve the
single merge base; any residual conflict is resolved once in an isolated
worktree. The old local tip stays reachable under a pre-update ref for undo.

The reconcile is built to be non-destructive above all else:

1. ``/data/platform`` holds the SERVED backend, so a reconcile must never leave a
   half-applied tree. A merge conflict stays in the candidate worktree (the
   old, working code keeps serving) and is surfaced for a resolver; a crash
   mid-reconcile is detected on the next boot and reset before anything else
   runs. Legacy interrupted merges and rebases are still cleaned up too.

2. Local edits are NEVER lost. Uncommitted working-tree edits ride through the
   update as a transient commit, merge onto the result separately, and return
   to the working tree afterwards. A resolver's answer is just one more input
   to the same final-tree merge: live edits made while it was parked merge
   with it, and any overlap parks again. A conflict or an import-broken result
   rolls the served tree back to exactly the owner's edits.

3. A clean merge can still produce a tree that fails to import (e.g. upstream
   deleted a module a local edit still imports). A post-merge import probe
   catches that and rolls back to the previous served commit rather than
   serving a broken tree.

Availability is an EXACT ancestry check, not a sha-string compare: an update is
available iff the configured target is NOT already an ancestor of local ``main`` — the
same ``git merge-base --is-ancestor`` model ``app_git`` uses for an app. This
module reuses ``app_git``'s isolated git env and ``commit_local`` engine; it does
NOT carry forward the old baked-floor machinery (recording a baked tree onto
``upstream``), which fought the clone model.
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
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
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable, Literal, TypedDict

from sqlalchemy.orm import Session

from app import app_git, platform_activation, runtime_provenance
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
ACTIVATION_V2_CUTOVER_RECEIPT = Path("/data/.platform-activation-v2")
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
# A filesystem lock shared by the startup recovery subprocess and the running
# uvicorn's Apply path. It MUST be a real flock (not an asyncio.Lock): the boot
# reconcile runs in a throwaway ``python3 -c`` process, so an in-process lock
# could not serialise it against uvicorn.
RECONCILE_LOCK = Path("/data/.platform-reconcile.lock")
# Durable, browser-safe phase record. Unlike the old process-only dictionary,
# this is visible when status/progress requests land on another worker.
UPDATE_PROGRESS_PATH = Path("/data/.platform-update-progress.json")
# One update prepared on a frozen copy and not yet fully in place. The live
# checkout keeps serving its current source until the next shutdown swaps the
# checked update in; boot then merges edits made on the old source meanwhile
# back on top. See ``prepare``/``swap_in_prepared_update``/``complete_platform_swap``.
PREPARED_UPDATE_PATH = Path("/data/.platform-prepared-update.json")
_PREPARED_REF = "refs/mobius/update-prepared"
# The live state saved at the swap: the previous version plus late edits. The
# boot script returns to it if the swapped-in version fails its startup check.
_LATE_REF = "refs/mobius/update-late"
# Container-local: survives a server restart but never claims a replacement
# image inherited packages installed in the previous container.
DEPENDENCY_RECEIPT_PATH = Path("/tmp/mobius-dependency-inputs.json")
# Fresh images link the editable clone to the baked dependency tree so ordinary
# frontend builds do not copy hundreds of megabytes into /data.  npm ci treats
# a node_modules symlink as the tree to clean, though, and the baked target is
# intentionally root-owned.  Dependency-changing updates therefore detach
# this one known bootstrap link lazily, just before the first mutable install.
BAKED_FRONTEND_NODE_MODULES = Path("/app/shell-src/node_modules")

UPSTREAM_BRANCH = "upstream"
LOCAL_BRANCH = "main"
DEFAULT_TARGET_REF = "origin/main"
OWNER_UPDATE_FETCH_REFSPEC = (
  "+refs/heads/main:refs/remotes/origin/main"
)
# The side a parked conflict merges in may be a synthetic commit (a resolver's
# earlier answer or a clean committed candidate). Keep the one parked side
# reachable while it waits; the next park overwrites it.
_CONFLICT_RIGHT_REF = "refs/mobius/platform-conflict-right"
# The local commit chain most recently replaced by a net-tree update.
_PRE_UPDATE_REF = "refs/mobius/platform-pre-update"

# The platform tree is larger than an app but still small; a git op slower than
# this is wedged, not busy. Fetch gets its own (network-bound) budget.
_GIT_TIMEOUT = 120
_FETCH_TIMEOUT = 120
# The candidate worktree an update reconciles the final trees in. It lives inside
# the clone's own git directory so neither the outer ``/data`` safety repo nor
# the platform tree ever sees it as content, and a conflicting merge can stay
# parked there for a resolver without touching the served checkout.
_OVERLAY_CANDIDATE_DIRNAME = "mobius-overlay-candidate"
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
  ("scripts/frontend-deps.sh", "frontend-deps.sh"),
  ("scripts/check-frontend-deps.mjs", "check-frontend-deps.mjs"),
)

# Update-preview payload bounds. A whole-platform deploy can carry a huge diff;
# the review sheet renders the file summary (always small) by default and the raw
# diff only on demand, so cap the diff bytes on the wire and flag truncation. The
# commit list is capped too — a normal deploy is a handful, and the sheet lists
# them, not paginates.
MAX_PREVIEW_DIFF_CHARS = 200_000
_PREVIEW_COMMIT_LIMIT = 100
# Bound on any error excerpt persisted to a flag or published to Settings. Tails
# keep a traceback's or pip/npm's final lines, where the cause is.
_ERROR_EXCERPT_CHARS = 2000

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

  The durable record makes Apply observable across workers and browser reloads;
  its terminal result remains available after a server restart.
  """

  plan_id: str | None
  target_sha: str | None
  # The reviewed image for ``target_sha`` on a managed deployment, so Finish
  # can still name it after a newer release is published.
  image_digest: str | None
  phase: str
  active: bool
  error: str | None
  updated_at: float


class UnfinishedUpdate(TypedDict):
  """The one update that must finish before another starts.

  ``resolve``: it is parked in the isolated candidate for a resolver.
  ``finish``: it installed, and its container replacement is still owed, so
  its source would otherwise keep running on the old image. A pending
  restart alone does not count: several updates may share one restart.
  """

  target_sha: str
  stage: Literal["resolve", "finish"]
  # What finishing takes: a container replacement, or just a restart.
  action: Literal["replace", "restart"]
  # Prepared but not yet in place: dropping it changes nothing live.
  cancellable: bool


_UPDATE_PROGRESS = PlatformUpdateProgress(
  plan_id=None,
  target_sha=None,
  image_digest=None,
  phase=PlatformUpdatePhase.IDLE.value,
  active=False,
  error=None,
  updated_at=0.0,
)


class PlatformUpdateError(RuntimeError):
  """A platform update could not proceed (carries a short machine code)."""


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
  # Exact release this response compared with the served source. For a
  # self-hosted check this is the freshly fetched origin/main; for a managed
  # deployment it is the verified image release selected by the controller.
  checked_target_sha: str | None
  # Plain-language alias for the historical ``recorded_upstream_sha`` field.
  # This is the last successfully reconciled release, not the latest fetch.
  installed_release_sha: str | None
  recorded_upstream_sha: str | None
  # Latest fetched origin/main commit that is already contained in local main.
  # Unlike recorded_upstream_sha, this remains correct after a manual/agent
  # merge that did not run the updater's marker-maintenance path.
  contained_upstream_sha: str | None
  contained_upstream_committed_at: str | None
  # Commit timestamp (%cI) of the running image's build commit, so Settings can
  # render "Current system" in the same local zone as "Installed update" rather
  # than the image's bare UTC build date. None when that commit is not in this
  # clone's object store.
  current_build_committed_at: str | None
  # Timestamp of the most recent successful fetch represented by this status.
  # GET /status remains fetch-free; POST /check advances FETCH_HEAD first.
  upstream_checked_at: str | None
  seed_required: bool
  conflict_paths: list[str]
  # The resolver chat opened for an in-progress conflict, so Settings can link
  # the owner straight to it. None unless ``state == "conflict"`` AND the id was
  # recorded.
  conflict_chat_id: str | None
  # Conflict-only backlog signal. This remains false outside ``state ==
  # "conflict"`` even when ``available`` is true. While resolving a conflict it
  # becomes true when origin/main advances past the pinned release, letting
  # Settings offer one combined review+resolve instead of one per release.
  # Fetch-free like the rest of status: reflects the last fetch.
  newer_updates_available: bool
  rollback_target_sha: str | None
  rollback_error: str | None
  # While set, Settings offers only Finish update for this exact release and
  # no newer release is offered or accepted.
  unfinished_update: UnfinishedUpdate | None


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
  error: str | None


class PlatformReviewedRebuild(TypedDict):
  """Validated exact target for a reviewed container replacement."""

  target_sha: str
  image_digest: str | None
  local_base_sha: str
  activation: PlatformActivationImpact
  incoming_activation: PlatformActivationImpact
  blockers: list[str]


class _ActivationMarker(TypedDict):
  """Validated durable activation remainder."""

  version: int
  target_sha: str
  upstream_sha: str | None
  paths: list[str]
  # Exact subset whose desired content matches ``upstream_sha`` and can
  # therefore be satisfied by the official image for that release. Local-only
  # image inputs stay outside this set and remain pending after a rebuild.
  image_paths: list[str]


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
  and are the compact default the review sheet renders. ``available`` means new
  source; ``actionable`` also includes finishing an already-applied update.
  Both operations bind the same immutable current/target/image review."""

  state: str
  available: bool
  actionable: bool
  operation: Literal["update", "finish", "none"]
  current_sha: str | None
  target_sha: str | None
  # Stable identity for this exact current->target review. Apply recomputes it
  # and rejects a changed local tip or substituted target instead of silently
  # installing bytes other than the ones represented by this preview.
  plan_id: str | None
  # Present for a Railway image-owned release. The review binds both the
  # source revision and the immutable GHCR manifest, so a moving `main` tag can
  # never substitute different bytes after the owner reviews the diff.
  image_digest: str | None
  # Activation introduced by this reviewed target alone. ``activation`` may
  # also include unfinished work from an already-applied release.
  incoming_activation: PlatformActivationImpact
  activation: PlatformActivationImpact
  total_commits: int
  commits_truncated: bool
  commits: list[PlatformCommitSummary]
  files: list[PlatformFileChange]
  diff: str | None
  diff_truncated: bool
  conflict_paths: list[str]
  # The same preservation check used immediately before replacement. A preview
  # explains these blockers early; it never replaces the mutation's recheck.
  blocking_paths: list[str]
  # Exact local image-owned behavior an official image would replace.
  blocking_diff: str | None
  blocking_diff_truncated: bool


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
  # The net local tree carried by an updated release, or the parked conflict
  # worktree and its unresolved paths.
  overlay: dict | None = None

  @classmethod
  def unchanged(
    cls, status: str, pre: str | None, target: str | None = None, **fields,
  ) -> "ReconcileResult":
    """A pass that left the served tree at ``pre``."""
    return cls(status, pre, pre, target, **fields)


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
        image_digest=(
          payload.get("image_digest")
          if isinstance(payload.get("image_digest"), str)
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
  image_digest: str | None = None,
) -> None:
  """Publish one phase transition from either the event loop or worker thread."""
  with _PROGRESS_LOCK:
    _UPDATE_PROGRESS.update(
      plan_id=plan_id,
      target_sha=target_sha,
      image_digest=image_digest,
      phase=phase.value,
      active=active,
      error=error,
      updated_at=time.time(),
    )
    _atomic_write_text(
      UPDATE_PROGRESS_PATH,
      json.dumps(_UPDATE_PROGRESS, sort_keys=True),
    )


def _finish_interrupted_update_progress() -> None:
  """Retire an active record after its process died.

  The caller has acquired the cross-process reconcile lock, proving that no
  earlier reconciler still owns the transaction. The in-process Apply lock is
  the one exception: its worker has just acquired this lock and is still live.
  """
  if _APPLY_LOCK.locked():
    return
  if not UPDATE_PROGRESS_PATH.exists():
    return
  progress = platform_update_progress()
  if not progress["active"]:
    return
  _set_update_progress(
    PlatformUpdatePhase.FAILED,
    plan_id=progress["plan_id"],
    target_sha=progress["target_sha"],
    image_digest=progress["image_digest"],
    active=False,
    error=(
      "Möbius restarted before this update finished. Review the update again "
      "before retrying."
    ),
  )


def _update_plan_id(
  current_sha: str,
  target_sha: str,
  image_digest: str | None = None,
) -> str:
  """Deterministic identity for the exact local tip + reviewed release pair."""
  if image_digest:
    material = (
      f"mobius-platform-update-v2\0{current_sha}\0{target_sha}\0{image_digest}"
    ).encode()
  else:
    material = f"mobius-platform-update-v1\0{current_sha}\0{target_sha}".encode()
  return hashlib.sha256(material).hexdigest()


def _validate_update_plan(
  repo: Path,
  *,
  plan_id: str,
  current_sha: str,
  target_sha: str,
  image_digest: str | None = None,
) -> None:
  """Reject a stale or substituted preview before reconcile mutates the tree."""
  if not re.fullmatch(r"[0-9a-f]{40,64}", current_sha or ""):
    raise PlatformUpdateError("update_plan_invalid")
  if not re.fullmatch(r"[0-9a-f]{40,64}", target_sha or ""):
    raise PlatformUpdateError("update_plan_invalid")
  if image_digest is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", image_digest):
    raise PlatformUpdateError("update_plan_invalid")
  if plan_id != _update_plan_id(current_sha, target_sha, image_digest):
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
  pending = unfinished_update(repo)
  if pending and pending["target_sha"] != target_sha:
    raise PlatformUpdateError("finish_update_first")


def _scrubbed_git_env(repo: Path) -> dict:
  """``app_git``'s scrubbed, ceiling-pinned git env — the same isolation as
  its own ``_run``."""
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


def _write_reconcile_pre(pre: str, tip: str | None = None) -> None:
  """Record the only branch transition boot recovery may reverse."""
  _atomic_write_text(
    RECONCILE_PRE_FLAG,
    json.dumps({"pre": pre, "tip": tip}, separators=(",", ":")) + "\n",
  )


def _clear_reconcile_pre() -> None:
  RECONCILE_PRE_FLAG.unlink(missing_ok=True)


def _read_reconcile_pre() -> tuple[str | None, str | None]:
  if not RECONCILE_PRE_FLAG.exists():
    return None, None
  raw = RECONCILE_PRE_FLAG.read_text().strip()
  try:
    value = json.loads(raw)
  except json.JSONDecodeError:
    # Legacy markers recorded only PRE. They can clean a tree still at PRE,
    # but cannot prove ownership of any later branch tip.
    return raw or None, None
  if not isinstance(value, dict):
    return None, None
  return value.get("pre") or None, value.get("tip") or None


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
  pre, tip = _read_reconcile_pre()
  interrupted = _reconcile_in_progress(repo)
  _abort_interrupted(repo)
  if pre and _rev(repo, pre):
    current = _rev(repo, local)
    restored = False
    if current == tip:
      restored = _restore_candidate(repo, local, tip, pre)
    elif current == pre:
      _reset_hard_to(repo, local, pre)
      restored = True
    _clear_reconcile_pre()
    _restore_working_edits(repo, local)
    state = "reset" if restored else "preserved"
    return f"boot_guard[{state}] pre={_short(pre)}"
  if interrupted:
    _git("checkout", "-q", local, repo=repo, check=False)
    _git("reset", "--hard", local, repo=repo, check=False)
  _clear_reconcile_pre()
  _restore_working_edits(repo, local)
  return "boot_guard[clean]"


def _fetch(
  repo: Path = PLATFORM_REPO, *, refspec: str | None = None,
) -> bool:
  """Bounded ``git fetch`` of main; False (offline, unreachable, or hung) is
  non-fatal — the caller keeps serving the current clone. An explicit refspec
  refreshes main even on a clone with stale local fetch settings."""
  try:
    args = ["fetch", "--no-tags", "origin"]
    if refspec is not None:
      args.append(refspec)
    proc = _git(*args, repo=repo, check=False, timeout=_FETCH_TIMEOUT)
    return proc.returncode == 0
  except (subprocess.TimeoutExpired, OSError):
    return False


def _fetch_unshallow(repo: Path = PLATFORM_REPO) -> None:
  """Deepen a shallow clone so a merge can find a real merge base. Best-effort:
  an offline/timeout failure leaves the clone shallow and the caller's merge
  either still succeeds (the base was inside the shallow window) or reports a
  conflict, which fails closed to serve-old — never a hard reset."""
  try:
    _git(
      "fetch", "--unshallow", "--no-tags", "origin",
      repo=repo, check=False, timeout=_FETCH_TIMEOUT,
    )
  except (subprocess.TimeoutExpired, OSError):
    pass


def _overlay_candidate_path(repo: Path) -> Path:
  return repo / ".git" / _OVERLAY_CANDIDATE_DIRNAME


def _parked_overlay_for(repo: Path, tip: str) -> dict | None:
  """The parked resolution that still belongs to the served tip, if any.

  A merge is parked against the committed tip the owner had (``served``);
  its candidate worktree must still exist for a resolver to finish it. Later
  commits on top of that tip do not orphan it: continuing the resolution
  merges them with the resolver's answer.
  """
  parked = (_read_conflict_flag() or {}).get("overlay")
  if not parked or not tip:
    return None
  worktree = Path(str(parked.get("worktree") or _overlay_candidate_path(repo)))
  served = str(parked.get("served") or "")
  if not served or not (worktree / ".git").exists():
    return None
  if served != tip and not _is_ancestor(repo, served, tip):
    return None
  return parked


def _commit_tree_oid(repo: Path, commit: str) -> str | None:
  """Resolve one commit's tree without accepting another object type."""
  if not commit:
    return None
  oid = _git(
    "rev-parse", "--verify", "--quiet", f"{commit}^{{tree}}",
    repo=repo, check=False,
  ).stdout.strip().lower()
  return oid if re.fullmatch(r"[0-9a-f]{40}", oid) else None


def _activate_candidate(repo: Path, local: str, pre_sha: str, tip: str) -> None:
  """Move the served branch to a complete candidate, compare-and-swap.

  The update refuses if another writer moved the local branch after
  ``pre_sha``; only after that succeeds does the checked-out tree follow.
  """
  _write_reconcile_pre(pre_sha, tip)
  try:
    _git("update-ref", f"refs/heads/{local}", tip, pre_sha, repo=repo)
  except Exception:
    _clear_reconcile_pre()  # nothing moved; the newer writer keeps its tree
    raise
  try:
    _git("checkout", "-q", local, repo=repo)
    _git("reset", "--hard", tip, repo=repo)
  except Exception:
    # Keep the marker unless PRE's bytes are provably back: boot recovery
    # resets to PRE, and PRE may hold carried working edits.
    if _restore_candidate(repo, local, tip, pre_sha) and _git(
      "diff", "--quiet", pre_sha, "--", repo=repo, check=False,
    ).returncode == 0:
      _clear_reconcile_pre()
    raise


def _restore_working_edits(repo: Path, local: str) -> bool:
  """Return a transient working-tree overlay commit to uncommitted edits.

  Uncommitted edits are carried through a reconcile as a commit tagged with
  the ``working-tree`` unit so the candidate can move them; once the served tree
  has settled (updated, rolled back, or conflicted) that commit is unwound so
  the owner's ``git status`` reads exactly as it did before the update.
  """
  head = _rev(repo, "HEAD")
  # An unsettled activation still needs its carried commit: the boot guard
  # restores PRE's bytes first, then unwinds it.
  if not head or _reconcile_in_progress(repo) or RECONCILE_PRE_FLAG.exists():
    return False
  trailers = _git(
    "show", "-s",
    f"--format=%(trailers:key={app_git.OVERLAY_UNIT_TRAILER},valueonly=true)"
    f"%x00%(trailers:key={app_git.OVERLAY_DISPOSITION_TRAILER},valueonly=true)",
    head, repo=repo, check=False,
  ).stdout
  unit, _, disposition = trailers.partition("\x00")
  if (
    unit.strip() != app_git.OVERLAY_WORKING_UNIT
    or disposition.strip() != "wip"
    or not _rev(repo, "HEAD~1")
  ):
    return False
  _git("checkout", "-q", local, repo=repo, check=False)
  _git("reset", "-q", "--mixed", "HEAD~1", repo=repo, check=False)
  return True


def _reset_hard_to(repo: Path, local: str, sha: str) -> None:
  """Return the working branch to ``sha`` (the pre-reconcile served commit),
  updating the working tree. Used to serve OLD after a conflict/rollback."""
  _git("checkout", "-q", local, repo=repo, check=False)
  _git("reset", "--hard", sha, repo=repo, check=False)


def _restore_candidate(repo: Path, local: str, tip: str, pre: str) -> bool:
  """Roll back only the exact candidate this update published.

  A failed compare-and-swap means another writer owns the branch now. Never
  reset that writer's commit or working tree.
  """
  moved = _git(
    "update-ref", f"refs/heads/{local}", pre, tip,
    repo=repo, check=False,
  )
  if moved.returncode != 0:
    return False
  _git("checkout", "-q", local, repo=repo, check=False)
  if _rev(repo, local) == pre:
    _git("reset", "--hard", pre, repo=repo, check=False)
  return True


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
  return False, (proc.stderr or proc.stdout or "").strip()[-_ERROR_EXCERPT_CHARS:]


@contextlib.contextmanager
def _reconcile_flock(*, blocking: bool = True):
  """Hold the cross-process reconcile lock (see :data:`RECONCILE_LOCK`). Released
  on context exit AND on process death (the fd closes), so a killed boot
  reconcile never leaves the lock held."""
  RECONCILE_LOCK.parent.mkdir(parents=True, exist_ok=True)
  fd = os.open(str(RECONCILE_LOCK), os.O_CREAT | os.O_RDWR, 0o644)
  try:
    flags = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
    try:
      fcntl.flock(fd, flags)
    except BlockingIOError as exc:
      raise PlatformUpdateError("platform_update_in_progress") from exc
    _finish_interrupted_update_progress()
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
  overlay: dict | None = None,
) -> None:
  """Persist a conflict so Settings keeps surfacing it across reloads.

  Line 0 is the target (``origin/main``) sha; optional ``chat:<id>`` and
  ``overlay:<json>`` lines record the resolver chat and the parked merge
  worktree; the remaining lines are conflicting paths.
  """
  body = [target or ""]
  if chat_id:
    body.append(f"chat:{chat_id}")
  if overlay:
    body.append("overlay:" + json.dumps(overlay, separators=(",", ":")))
  body.extend(paths)
  _atomic_write_text(CONFLICT_FLAG, "\n".join(body))


def _read_conflict_flag() -> dict | None:
  """Parse the conflict target, chat, parked tree merge and paths, or None.

  ``upstream`` is the target sha (named for backward compatibility with the
  status field, not the ``upstream`` branch)."""
  if not CONFLICT_FLAG.exists():
    return None
  lines = CONFLICT_FLAG.read_text().splitlines()
  target = lines[0].strip() if lines else ""
  chat_id: str | None = None
  overlay: dict | None = None
  paths: list[str] = []
  for line in lines[1:]:
    stripped = line.strip()
    if not stripped:
      continue
    if stripped.startswith("chat:"):
      chat_id = stripped[len("chat:"):] or None
    elif stripped.startswith("base:"):
      continue  # written by older updaters; never a path
    elif stripped.startswith("overlay:"):
      try:
        parsed = json.loads(stripped[len("overlay:"):])
      except ValueError:
        parsed = None
      overlay = parsed if isinstance(parsed, dict) else None
    else:
      paths.append(stripped)
  return {
    "upstream": target or None,
    "chat_id": chat_id,
    "overlay": overlay,
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


def _latest_known_release(repo: Path) -> str | None:
  """The newest release this clone knows: ``origin/main`` unless it lags.

  A reviewed Apply can install an exact target fetched outside the tracking
  ref, leaving ``origin/main`` behind the installed ``upstream`` marker until
  the next check. Treating that older ref as the release would make Finish
  target an image older than the served source and misreport upstream's own
  changes as local ones.
  """
  remote = _rev(repo, DEFAULT_TARGET_REF) or None
  installed = recorded_upstream_sha(repo)
  if remote and installed and _is_ancestor(repo, remote, installed):
    return installed
  # A missing tracking ref stays unavailable, never "up to date".
  return remote


def _update_source_tip(repo: Path) -> str:
  """Require readable source before reporting update availability or completion."""
  if not (repo / ".git").exists():
    raise PlatformUpdateError("platform_repo_missing")
  if not _has_origin(repo):
    raise PlatformUpdateError("platform_origin_missing")
  tip = _rev(repo, "HEAD")
  if not tip:
    raise PlatformUpdateError("platform_source_unavailable")
  return tip


def applied_release_sha(repo: Path = PLATFORM_REPO) -> str:
  """Prove Finish's official target is already present in the served history.

  A recorded upstream ref is a reconciliation marker, not proof of containment:
  an owner can reset the served branch independently. Never turn Finish into a
  source update merely because that marker survived the reset.
  """
  with _reconcile_flock():
    current = _update_source_tip(repo)
    for candidate in (_latest_known_release(repo), recorded_upstream_sha(repo)):
      if candidate and _is_ancestor(repo, candidate, current):
        return candidate
    raise PlatformUpdateError("applied_release_unavailable")


def _parse_legacy_activation_marker(raw: str) -> _ActivationMarker | None:
  """Translate a pre-v2 activation marker during the one-way boot cutover."""
  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError:
    if re.fullmatch(r"[0-9a-fA-F]{40}", raw) is None:
      return None
    return {
      "version": 2,
      "target_sha": raw.lower(),
      "upstream_sha": None,
      "paths": ["backend/app"],
      "image_paths": [],
    }
  if not isinstance(parsed, dict):
    return None
  if parsed.get("version") not in (None, 1):
    return None
  target = str(parsed.get("target_sha") or "").strip()
  paths = parsed.get("paths")
  if re.fullmatch(r"[0-9a-fA-F]{40}", target) is None or not isinstance(paths, list):
    return None
  clean_paths = sorted({str(path).strip() for path in paths if str(path).strip()})
  return {
    "version": 2,
    "target_sha": target.lower(),
    "upstream_sha": None,
    "paths": clean_paths,
    "image_paths": [],
  }


def _normalize_activation_marker() -> None:
  """Replace the old bare-SHA/v1 shapes before current runtime reads them."""
  # A selectively restored marker is newer evidence than this derived proof.
  # Rebuild the receipt only after the marker present on this boot passes the
  # current parser; never let a stale receipt bypass normalization.
  ACTIVATION_V2_CUTOVER_RECEIPT.unlink(missing_ok=True)
  try:
    raw = RESTART_NEEDED_FLAG.read_text(encoding="utf-8").strip()
  except FileNotFoundError:
    _atomic_write_text(ACTIVATION_V2_CUTOVER_RECEIPT, "v2")
    return
  if not raw:
    RESTART_NEEDED_FLAG.unlink(missing_ok=True)
    _atomic_write_text(ACTIVATION_V2_CUTOVER_RECEIPT, "v2")
    return
  if _read_activation_marker() is not None:
    _atomic_write_text(ACTIVATION_V2_CUTOVER_RECEIPT, "v2")
    return
  migrated = _parse_legacy_activation_marker(raw)
  if migrated is None:
    return
  _write_activation_marker(
    migrated["target_sha"], migrated["paths"],
    upstream_sha=migrated["upstream_sha"],
    image_paths=migrated["image_paths"],
  )
  if _read_activation_marker() is not None:
    _atomic_write_text(ACTIVATION_V2_CUTOVER_RECEIPT, "v2")


def _read_activation_marker() -> _ActivationMarker | None:
  """Read the current v2 activation marker; boot owns older normalization."""
  try:
    parsed = json.loads(RESTART_NEEDED_FLAG.read_text(encoding="utf-8"))
  except (FileNotFoundError, OSError, json.JSONDecodeError):
    return None
  if not isinstance(parsed, dict) or parsed.get("version") != 2:
    return None
  target = str(parsed.get("target_sha") or "").strip()
  paths = parsed.get("paths")
  raw_image_paths = parsed.get("image_paths")
  upstream = parsed.get("upstream_sha")
  clean_upstream = str(upstream or "").strip()
  if (
    re.fullmatch(r"[0-9a-fA-F]{40}", target) is None
    or not isinstance(paths, list)
    or not paths
    or not all(isinstance(path, str) and path.strip() for path in paths)
    or not isinstance(raw_image_paths, list)
    or not all(isinstance(path, str) and path.strip() for path in raw_image_paths)
    or (clean_upstream and re.fullmatch(
      r"[0-9a-fA-F]{40}", clean_upstream,
    ) is None)
  ):
    return None
  clean_paths = sorted({path.strip() for path in paths})
  image_paths = sorted({path.strip() for path in raw_image_paths})
  if any(path not in clean_paths for path in image_paths):
    return None
  return {
    "version": 2,
    "target_sha": target.lower(),
    "upstream_sha": clean_upstream.lower() or None,
    "paths": clean_paths,
    "image_paths": image_paths,
  }


def _protected_runtime_status(
  repo: Path = PLATFORM_REPO,
) -> runtime_provenance.RuntimeParity:
  return runtime_provenance.protected_runtime_status(
    repo / "backend" / "runtime",
  )


def container_replacement_blockers(
  expected_sha: str | None = None,
  repo: Path = PLATFORM_REPO,
  *,
  local_change_base: str | None = None,
) -> list[str]:
  """Return local image changes absent from the official target.

  An official image can retire only activation paths whose desired content is
  exactly the applied upstream revision. Replacing a working container while a
  local-only Dockerfile or bootstrap-script change remains would silently
  remove that runtime addition, so the owner action must fail closed before
  cutover.

  Protected runtime (``backend/runtime``) follows the same rule as every other
  image input. Privileged code comes only from the reviewed image; a local
  protected-runtime change must therefore be upstream in that exact target or
  block replacement before chat drain.
  """
  marker = _read_activation_marker()
  covered = set(marker["image_paths"]) if marker else set()
  pending = list(marker["paths"]) if marker else []

  # Activation markers record intended work, not the commits an agent made
  # directly.  When the caller supplies the official replacement target,
  # verify parity against the histories themselves: every path whose desired
  # local content differs from the official target (local Dockerfile,
  # dependency-lock, bootstrap-script, or seeded-skill commits never write a
  # marker, yet the official image would silently replace them).  This catches
  # lost/cleared/stale markers without blocking a replacement whose exact
  # official image will repair the drift.  A failed diff (e.g. an unfetched
  # target) adds nothing here; the replacement controller independently fails
  # closed on unverifiable provenance.
  head = _rev(repo, _local_branch(repo)) if expected_sha else None
  working_paths: set[str] = set()
  if head:
    # A reviewed target may be ahead of the applied tree. Diffing target to
    # head would mislabel the incoming official Dockerfile/runtime changes as
    # local drift. The reviewed path supplies the proven merge base so only
    # the local side is considered; ordinary rebuilds keep expected..head.
    drift = _git(
      "diff", "--name-only", "--no-renames",
      local_change_base or expected_sha, head, repo=repo, check=False,
    )
    if drift.returncode == 0:
      pending.extend(
        line.strip() for line in drift.stdout.splitlines() if line.strip()
      )
    working = _git(
      "diff", "--name-only", "--no-renames", "HEAD", "--",
      repo=repo, check=False,
    )
    if working.returncode == 0:
      working_paths.update(
        line.strip() for line in working.stdout.splitlines() if line.strip()
      )
    untracked = _git(
      "ls-files", "--others", "--exclude-standard",
      repo=repo, check=False,
    )
    if untracked.returncode == 0:
      working_paths.update(
        line.strip() for line in untracked.stdout.splitlines() if line.strip()
      )
    pending.extend(working_paths)

  image_pending: list[str] = []
  for path in sorted(set(pending)):
    impact = platform_activation.classify_activation(
      [path], deployment="self_hosted",
    )
    if (
      impact["level"]
      == platform_activation.ActivationLevel.IMAGE_REBUILD.value
    ):
      image_pending.append(path)

  # Actual content parity with the target is authoritative whenever both
  # revisions are known. A newer official image may legitimately advance a
  # marker-covered path, though, so preserve that coverage when the current
  # local bytes still match the marker's official upstream and the replacement
  # target descends from it. This distinguishes incoming official changes from
  # real local drift without letting stale marker coverage excuse an
  # unrelated release.
  if expected_sha and head:
    exact_target_coverage = set(_paths_matching_upstream(
      repo, head, expected_sha, image_pending,
    ))
    marker_upstream = marker["upstream_sha"] if marker else None
    carried_marker_coverage: set[str] = set()
    if marker_upstream and _is_ancestor(repo, marker_upstream, expected_sha):
      carried_marker_coverage.update(_paths_matching_upstream(
        repo,
        head,
        marker_upstream,
        [path for path in image_pending if path in covered],
      ))
    # A target/head match says nothing about uncommitted bytes. Preserve those
    # paths as blockers until the owner commits, reverts, or reviews them.
    covered = (exact_target_coverage | carried_marker_coverage) - working_paths
  return sorted(path for path in image_pending if path not in covered)


def reviewed_container_rebuild_plan(
  *,
  plan_id: str,
  current_sha: str,
  target_sha: str,
  image_digest: str | None,
  repo: Path = PLATFORM_REPO,
) -> PlatformReviewedRebuild:
  """Validate an immutable image review without mutating the served checkout."""
  with _reconcile_flock():
    _validate_update_plan(
      repo,
      plan_id=plan_id,
      current_sha=current_sha,
      target_sha=target_sha,
      image_digest=image_digest,
    )
    base = _git(
      "merge-base", current_sha, target_sha, repo=repo, check=False,
    ).stdout.strip() or current_sha
    incoming_activation = _incoming_activation_impact(repo, base, target_sha)
    activation = platform_activation.classify_activation([
      *_pending_activation_paths(repo),
      *_activation_paths_between(repo, base, target_sha),
    ])
    blockers = container_replacement_blockers(
      target_sha, repo, local_change_base=base,
    )
    return PlatformReviewedRebuild(
      target_sha=target_sha,
      image_digest=image_digest,
      local_base_sha=base,
      activation=activation,
      incoming_activation=incoming_activation,
      blockers=blockers,
    )


def _write_activation_marker(
  target_sha: str,
  paths: list[str],
  *,
  upstream_sha: str | None = None,
  image_paths: list[str] | None = None,
) -> None:
  clean_paths = sorted({path.strip() for path in paths if path.strip()})
  if not clean_paths:
    RESTART_NEEDED_FLAG.unlink(missing_ok=True)
    return
  clean_image_paths = sorted({
    path.strip() for path in (image_paths or [])
    if path.strip() in clean_paths
  })
  _atomic_write_text(RESTART_NEEDED_FLAG, json.dumps({
    "version": 2,
    "target_sha": target_sha or "",
    "upstream_sha": upstream_sha or "",
    "paths": clean_paths,
    "image_paths": clean_image_paths,
  }, separators=(",", ":")))


def _paths_matching_upstream(
  repo: Path,
  target_sha: str,
  upstream_sha: str | None,
  paths: list[str],
) -> list[str]:
  """Paths whose desired local content is exactly the upstream tree content."""
  if not target_sha or not upstream_sha:
    return []
  matching: list[str] = []
  for path in paths:
    result = _git(
      "diff", "--quiet", target_sha, upstream_sha, "--", path,
      repo=repo, check=False,
    )
    if result.returncode == 0:
      matching.append(path)
  return matching


def mark_activation_needed(
  target_sha: str,
  paths: list[str],
  *,
  upstream_sha: str | None = None,
  repo: Path = PLATFORM_REPO,
) -> None:
  """Persist activation work, preserving any earlier host/image remainder."""
  existing = _read_activation_marker()
  carried = existing["paths"] if existing else []
  combined = sorted({*[str(path) for path in carried], *[str(path) for path in paths]})
  covered = _paths_matching_upstream(
    repo, target_sha, upstream_sha, combined,
  )
  _write_activation_marker(
    target_sha,
    combined,
    upstream_sha=upstream_sha,
    image_paths=covered,
  )


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
  """Repo-relative paths changed between two commits, failing closed to the
  backend runtime (``backend/app``) when either side is unknown or the diff
  cannot be read.

  ``--no-renames`` so a file moved OUT of a runtime dir (``git mv backend/app/x
  docs/x``) shows BOTH the deleted source and the added destination — otherwise
  rename detection reports only the destination and the classifier would miss
  that the served backend lost a module.
  """
  if before == after:
    return []
  if not before or not after:
    return ["backend/app"]
  proc = _git(
    "diff", "--name-only", "--no-renames", before, after, repo=repo, check=False,
  )
  if proc.returncode != 0:
    return ["backend/app"]
  return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


_PYTHON_DEPENDENCY_INPUTS = (
  "backend/requirements.txt",
  "backend/requirements.lock",
)


def target_python_inputs_baked_into_image(
  repo: Path,
  target_sha: str | None,
) -> bool:
  """Whether this process's image contains the target's Python inputs.

  The served checkout can intentionally lag the image during an image-first
  deployment. Compare immutable target objects with the hashes recorded by
  that image instead of comparing either side with mutable working-tree bytes.
  Missing provenance, target objects, or inputs fail closed.
  """
  if not target_sha or _rev(repo, target_sha) != target_sha:
    return False
  baked = _build_info().get("image_inputs")
  if not isinstance(baked, dict) or not baked:
    return False
  for path in _PYTHON_DEPENDENCY_INPUTS:
    expected = baked.get(path)
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
      return False
    try:
      blob = subprocess.run(
        ["git", "-C", str(repo), "show", f"{target_sha}:{path}"],
        capture_output=True,
        check=False,
        timeout=_GIT_TIMEOUT,
        env=_scrubbed_git_env(repo),
      )
    except (OSError, subprocess.SubprocessError):
      return False
    if blob.returncode != 0 or hashlib.sha256(blob.stdout).hexdigest() != expected:
      return False
  return True


def _incoming_activation_impact(
  repo: Path,
  before: str | None,
  target_sha: str | None,
) -> PlatformActivationImpact:
  """Classify target work that is not already proven active in this image."""
  paths = _activation_paths_between(repo, before, target_sha)
  if target_python_inputs_baked_into_image(repo, target_sha):
    paths = [path for path in paths if path not in _PYTHON_DEPENDENCY_INPUTS]
  return platform_activation.classify_activation(paths)


def activation_changes_python_dependencies(
  impact: PlatformActivationImpact,
) -> bool:
  """Whether one classified change set introduces Python package inputs."""
  return any(
    reason.get("code") == "python_dependencies"
    for reason in impact.get("reasons", [])
    if isinstance(reason, dict)
  )


def _changes_python_dependencies(
  repo: Path, before: str | None, after: str | None,
) -> bool:
  return activation_changes_python_dependencies(
    _incoming_activation_impact(repo, before, after),
  )


def _paths_already_active_in_image(
  repo: Path, paths: list[str],
) -> list[str]:
  """Exclude exact image-owned files whose desired bytes already run in image.

  ``SERVING_SHA_FILE`` identifies the Python checkout, not every image-owned
  bootstrap input. A local repair can restore a seed template to the running
  image's exact bytes while still differing from the served checkout's Git
  commit. That is not pending activation: the image already owns and runs
  those bytes. Keep the check exact and fail closed when a manifest or source
  hash is unavailable.
  """
  baked = _build_info().get("image_inputs")
  if not isinstance(baked, dict) or not baked:
    return paths
  try:
    current = platform_activation.image_input_hashes(repo)
  except OSError:
    return paths
  return [
    path for path in paths
    if not (
      platform_activation.path_is_image_owned(path)
      and isinstance(baked.get(path), str)
      and baked[path] == current.get(path)
    )
  ]


def _pending_activation_paths(
  repo: Path = PLATFORM_REPO,
  *,
  served_to_head: list[str] | None = None,
) -> list[str]:
  """Every path whose activation is still owed by the running process.

  ``served_to_head`` lets a caller that has just diffed the served revision
  against the local head (to write the activation marker) hand that result
  over instead of having the same pair diffed again.
  """
  marker = _read_activation_marker()
  paths = list(marker["paths"]) if marker else []
  paths.extend(runtime_provenance.activation_paths(
    _protected_runtime_status(repo),
  ))
  if served_to_head is None:
    served = _served_platform_sha()
    if served:
      try:
        head = _rev(repo, _local_branch(repo))
      except Exception:
        head = None
      served_to_head = _activation_paths_between(repo, served, head)
  paths.extend(_paths_already_active_in_image(repo, served_to_head or []))
  paths.extend(image_input_drift(repo) or [])
  return sorted({str(path) for path in paths if str(path)})


def _build_info() -> dict:
  path = Path(os.environ.get("MOBIUS_BUILD_INFO_PATH", "/app/build-info.json"))
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except Exception:
    return {}
  return data if isinstance(data, dict) else {}


def image_input_drift(repo: Path = PLATFORM_REPO) -> list[str] | None:
  """Image inputs whose served source no longer matches the running image.

  The image records the hash of every input it was built from
  (``build-info.json`` ``image_inputs``). Comparing those with the same paths
  in the served checkout, with successful container-local dependency installs
  accounted for, says whether this container still matches its source. The
  classifier then turns each differing path into its activation level.
  Returns None when the running image predates the record.
  """
  baked = _build_info().get("image_inputs")
  if not isinstance(baked, dict) or not baked:
    return None
  try:
    current = platform_activation.image_input_hashes(repo)
  except OSError:
    return None
  # The image build can leave generated output inside broad baked-runtime
  # prefixes (for example frontend assets and Python bytecode).  Those files
  # are not authored source and may differ from the live checkout even when
  # its Git state is clean.  Compare tracked files plus intentionally
  # unignored local additions; ignored build output must not manufacture a
  # permanent image-rebuild warning.  A Git failure keeps the older
  # fail-closed comparison rather than hiding possible source drift.
  authored = _git(
    "ls-files", "--cached", "--others", "--exclude-standard", "-z",
    repo=repo, check=False,
  )
  # A running image can record a path that the current classifier has since
  # moved to served source; that stale entry must not manufacture a permanent
  # warning. Keep every current input plus baked paths that the classifier still
  # owns, including a tracked image-owned file deleted from the served tree.
  candidates = set(current) | {
    path for path in baked if platform_activation.path_is_image_owned(path)
  }
  if authored.returncode == 0:
    candidates &= {
      path for path in authored.stdout.split("\0") if path
    }
  # Successful frontend installs replace the image baseline only for the exact
  # lock bytes they installed. Python dependencies remain image-owned.
  installed = _dependency_receipt()
  return sorted(
    path for path in candidates
    if installed.get(path, baked.get(path)) != current.get(path)
  )


def _platform_activation_impact(
  repo: Path = PLATFORM_REPO,
  *,
  served_to_head: list[str] | None = None,
) -> PlatformActivationImpact:
  return platform_activation.classify_activation(
    _pending_activation_paths(repo, served_to_head=served_to_head),
  )


def _complete_boot_activation(repo: Path) -> None:
  """Retire activation work this boot can prove complete.

  A fresh server always satisfies ``server_restart``.  A new image identity
  that contains the applied target proves only matching image work complete.
  Proxy reload and host maintenance remain explicit because the container
  cannot observe or control those external planes.
  """
  marker = _read_activation_marker()
  if not marker:
    RESTART_NEEDED_FLAG.unlink(missing_ok=True)
    return
  paths = marker["paths"]
  target = marker["upstream_sha"] or marker["target_sha"]
  build = current_build_sha()
  build_contains_target = bool(
    target and build and (_rev(repo, build) or "")
    and _is_ancestor(repo, target, build)
  )
  completed_by_image = {
    platform_activation.ActivationLevel.IMAGE_REBUILD.value,
  }
  remaining: list[str] = []
  for path in paths:
    level = platform_activation.classify_activation([path])["level"]
    if level in {
      platform_activation.ActivationLevel.LIVE.value,
      platform_activation.ActivationLevel.SERVER_RESTART.value,
    }:
      continue
    if (
      build_contains_target
      and path in marker["image_paths"]
      and level in completed_by_image
    ):
      continue
    remaining.append(str(path))
  _write_activation_marker(
    marker["target_sha"],
    remaining,
    upstream_sha=marker["upstream_sha"],
    image_paths=[
      path for path in marker["image_paths"] if path in remaining
    ],
  )


# npm can be slow on a small self-hosted box fetching packages; keep the bound
# generous but finite so a wedged frontend install still fails closed.
_FRONTEND_DEPENDENCY_TIMEOUT = 900
_FRONTEND_DEPENDENCY_INPUTS = ("frontend/package.json", "frontend/package-lock.json")


def _dependency_receipt() -> dict[str, str]:
  try:
    receipt = json.loads(DEPENDENCY_RECEIPT_PATH.read_text())
  except (OSError, ValueError):
    return {}
  if not isinstance(receipt, dict):
    return {}
  allowed = set(_FRONTEND_DEPENDENCY_INPUTS)
  return {
    path: digest for path, digest in receipt.items()
    if path in allowed and isinstance(digest, str)
  }


def _record_dependency_inputs(repo: Path, paths: tuple[str, ...], *, installed: bool) -> None:
  receipt = _dependency_receipt()
  for path in paths:
    source = repo / path
    # Empty is deliberately not the baked baseline: a partial install can
    # have changed packages even when source later returns to the image lock.
    receipt[path] = ""
    if installed:
      receipt.pop(path)
      if source.is_file():
        receipt[path] = hashlib.sha256(source.read_bytes()).hexdigest()
  _atomic_write_text(DEPENDENCY_RECEIPT_PATH, json.dumps(receipt, sort_keys=True))


def _sync_frontend_dependencies(repo: Path) -> tuple[bool, str]:
  """Install the locked frontend deps in place — the SAME command the image build
  runs (``npm ci --ignore-scripts``), just before the frontend rebuild so
  the build sees the new ``node_modules``.

  Returns ``(ok, error_tail)`` and never raises for an operational failure.
  """
  frontend = repo / "frontend"
  if not (frontend / "package-lock.json").is_file():
    return True, ""
  try:
    _record_dependency_inputs(repo, _FRONTEND_DEPENDENCY_INPUTS, installed=False)
    node_modules = frontend / "node_modules"
    if node_modules.is_symlink():
      target = node_modules.resolve(strict=False)
      if target != BAKED_FRONTEND_NODE_MODULES.resolve(strict=False):
        return False, (
          "frontend node_modules is an unexpected symlink; refusing to let "
          f"npm ci replace its target ({target})"
        )
      # Leave no missing-path window for the frontend watcher to relink the
      # baked tree before npm starts.  Once detached, candidate failure and
      # rollback both reuse this writable real directory.
      node_modules.unlink()
      node_modules.mkdir()
    proc = subprocess.run(
      ["npm", "ci", "--ignore-scripts"],
      cwd=str(frontend),
      capture_output=True,
      text=True,
      timeout=_FRONTEND_DEPENDENCY_TIMEOUT,
    )
  except (subprocess.TimeoutExpired, OSError) as exc:
    return False, repr(exc)[-_ERROR_EXCERPT_CHARS:]
  if proc.returncode != 0:
    detail = (proc.stderr or proc.stdout or "npm ci failed").strip()
    return False, detail[-_ERROR_EXCERPT_CHARS:]
  try:
    _record_dependency_inputs(repo, _FRONTEND_DEPENDENCY_INPUTS, installed=True)
  except OSError as exc:
    return False, repr(exc)[-_ERROR_EXCERPT_CHARS:]
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
  return os.fsdecode(raw).strip()[-_ERROR_EXCERPT_CHARS:]


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
    return repr(exc)[:_ERROR_EXCERPT_CHARS]


def _rebuild_frontend(repo: Path, res: ReconcileResult) -> None:
  """Rebuild served frontend assets after a clean update that changed them.

  The live edit watcher sees ordinary file saves, but git checkout/merge during
  the Settings update flow can move frontend files without a reliable watcher
  event. Without this explicit rebuild, ``/data/platform/frontend/src`` advances
  while ``dist`` keeps serving the old bundle.
  """
  try:
    from app.frontend_watcher import rebuild_frontend_now
  except Exception as exc:
    raise RuntimeError("frontend rebuild is unavailable") from exc
  rebuild_frontend_now(
    f"platform update {_short(res.pre_sha)}->{_short(res.new_sha)}",
  )


def _restore_update_dependencies(
  repo: Path, *, frontend_changed: bool,
) -> str:
  """Restore the previous frontend dependency tree after a failed build."""
  failures: list[str] = []
  if frontend_changed:
    lock = "frontend/package-lock.json"
    if not (repo / lock).is_file():
      failures.append(f"{lock}: previous lock unavailable")
    else:
      ok, error = _sync_frontend_dependencies(repo)
      if not ok:
        failures.append(f"{lock}: {error}")
  if not failures:
    return ""
  return (
    "dependency_restore_failed: Source was restored, but its dependencies "
    "could not be restored. Repair them before restarting. " + "; ".join(failures)
  )


def _roll_back_failed_frontend_build(
  repo: Path,
  res: ReconcileResult,
  previous_upstream_sha: str | None,
  error: Exception,
  *,
  frontend_changed: bool,
) -> ReconcileResult:
  """Restore the pre-Apply source generation when its frontend cannot build.

  ``reconcile`` and this rollback run under the same cross-process flock, so no
  startup recovery/check can observe or mutate the intermediate source tree. The
  frontend publisher already keeps the previously served ``dist`` on a failed
  candidate build; resetting the source closes the other half of that invariant.
  """
  _abort_interrupted(repo)
  if res.pre_sha:
    if not _restore_candidate(
      repo, _local_branch(repo), res.new_sha or "", res.pre_sha,
    ):
      return replace(
        res,
        status="error",
        error="rollback_ref_changed: a newer writer owns the served branch",
      )
  if previous_upstream_sha:
    _set_upstream(repo, previous_upstream_sha)
  else:
    _clear_upstream(repo)
  # Apply does not create/replace the restart marker until preparation succeeds.
  # A marker already present before this attempt belongs to earlier on-disk
  # backend changes and must survive this failed frontend candidate.
  CONFLICT_FLAG.unlink(missing_ok=True)
  message = f"frontend_build_failed: {error!r}"[:_ERROR_EXCERPT_CHARS]
  restore_error = _restore_update_dependencies(
    repo, frontend_changed=frontend_changed,
  )
  if restore_error:
    message += "\n" + restore_error
  _write_rolled_back_flag(res.target_sha, message)
  _clear_reconcile_pre()
  return replace(
    res,
    status="rolled_back",
    new_sha=res.pre_sha,
    error=message,
    hook_source_sha=previous_upstream_sha,
  )


@dataclass(frozen=True)
class _Carried:
  """Working edits carried through one update as a transient overlay commit.

  ``served`` is the committed tip the owner had (what ``git status`` read
  against); ``pre`` is the tip after carrying, equal to ``served`` when the
  tree was clean; ``working`` is the transient commit when there were edits.
  """

  served: str
  pre: str
  working: str | None


def _carry_working_edits(repo: Path, local: str) -> _Carried:
  served = _rev(repo, local)
  app_git.commit_local(repo, app_git.overlay_message(
    "platform: working edits carried across update",
    unit=app_git.OVERLAY_WORKING_UNIT, disposition="wip",
  ))
  pre = _rev(repo, local) or served
  return _Carried(served=served, pre=pre, working=pre if pre != served else None)


def _working_tree_oid(repo: Path, base: str) -> str:
  """Snapshot tracked and unignored files without changing the shared index."""
  with tempfile.TemporaryDirectory(prefix="mobius-platform-index-") as tmp:
    index = Path(tmp) / "index"
    app_git._run_with_index(repo, index, "read-tree", base)
    app_git._run_with_index(repo, index, "add", "-A", ".")
    return app_git._run_with_index(repo, index, "write-tree").stdout.strip()


def _reconcile_pass(
  repo: Path,
  *,
  target_ref: str,
  fetch_remote: bool,
  progress: Callable[[PlatformUpdatePhase], None] | None,
) -> ReconcileResult:
  """One reconcile pass; see :func:`reconcile_clone` for the contract."""
  if not (repo / ".git").exists():
    return ReconcileResult.unchanged("skipped", None, error="no_git")

  local = _local_branch(repo)
  # Crash-safety FIRST: a mid-reconcile crash must be aborted before anything reads
  # the tree, so we reconcile from the committed pre-crash tip.
  _abort_interrupted(repo)
  pre = _rev(repo, local)

  if not _has_origin(repo):
    return ReconcileResult.unchanged("skipped", pre, error="no_origin")

  if fetch_remote:
    if progress:
      progress(PlatformUpdatePhase.FETCHING)
    fetched = _fetch(repo)
    if not fetched:
      # Offline is non-fatal: keep serving the current clone until the next explicit update.
      _write_offline_flag("fetch_failed")
      return ReconcileResult.unchanged("offline", pre, error="fetch_failed")
    OFFLINE_FLAG.unlink(missing_ok=True)

  target = _rev(repo, target_ref)
  if not target:
    _write_offline_flag("no_target_ref")
    return ReconcileResult.unchanged("offline", pre, error="no_target_ref")

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
    return ReconcileResult.unchanged("up_to_date", pre, target)

  # A merge parked for a resolver owns the candidate worktree until it is
  # finished or abandoned. A boot or a repeated Apply must not replace it
  # underneath the resolver's edits; Settings still shows that newer
  # releases are stacked up behind the parked one.
  parked = _parked_overlay_for(repo, pre)
  if parked is not None:
    flag = _read_conflict_flag() or {}
    return ReconcileResult.unchanged(
      "conflict", pre, str(parked.get("target") or target),
      conflict_paths=list(flag.get("paths") or []),
      overlay=parked,
    )

  if progress:
    progress(PlatformUpdatePhase.RECONCILING)
  # A deploy advanced origin beyond committed main. Carry any uncommitted edits
  # as a transient overlay commit FIRST so neither activation nor the merge
  # can discard them; ``reconcile_clone`` unwinds it afterwards.
  _reattach_detached_head(repo, local)
  carried = _carry_working_edits(repo, local)
  pre = carried.pre
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
    # full-history fallback before we choose between fast-forward and merge.
    fast_forward = bool(pre) and _is_ancestor(repo, pre, target)
    if _is_shallow(repo) and not fast_forward:
      if progress:
        progress(PlatformUpdatePhase.FETCHING)
      _fetch_unshallow(repo)
      if progress:
        progress(PlatformUpdatePhase.RECONCILING)
      fast_forward = bool(pre) and _is_ancestor(repo, pre, target)
    _git("checkout", "-q", local, repo=repo, check=False)
    overlay_summary: dict | None = None
    if fast_forward:
      # main is fully contained in target (every commit on main is in target), so
      # a fast-forward is PROVABLY loss-free. This is decided by ANCESTRY, never by
      # an `upstream` marker that could drift and let `reset --hard` silently
      # discard committed local edits.
      candidate = target
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
      # Main and target diverged: merge their final trees once, then place the
      # resulting local delta on the reviewed upstream target.
      outcome = _apply_overlay(
        repo, carried, target, ordinary_base, reconciliation,
      )
      if isinstance(outcome, ReconcileResult):
        return outcome
      reconciliation = outcome.reconciliation
      overlay_summary = outcome.overlay
      candidate = outcome.tip
    return _finalize_update(
      repo, local, pre=pre, tip=candidate, target=target,
      progress=progress, reconciliation=reconciliation,
      overlay=overlay_summary,
    )
  except Exception as exc:  # unexpected git failure — never serve a half-tree
    _abort_interrupted(repo)
    if _rev(repo, local) == pre:
      _reset_hard_to(repo, local, pre)
    # Nothing here is actionable by a resolver, and any earlier flag belonged
    # to an attempt this pass already superseded: leave the served tree at PRE
    # with no stale conflict/rollback state to mislead the next status read.
    app_git.remove_overlay_worktree(repo, _overlay_candidate_path(repo))
    CONFLICT_FLAG.unlink(missing_ok=True)
    ROLLED_BACK_FLAG.unlink(missing_ok=True)
    if _git("diff", "--quiet", pre, "--", repo=repo, check=False).returncode == 0:
      _clear_reconcile_pre()
    return ReconcileResult.unchanged("error", pre, target, error=repr(exc))


def _roll_back_update(
  repo: Path, local: str, pre: str, tip: str, target: str,
  message: str, error: str,
) -> ReconcileResult:
  """Serve the previous source after a rejected candidate."""
  if not _restore_candidate(repo, local, tip, pre):
    return ReconcileResult.unchanged(
      "error", pre, target,
      error="rollback_ref_changed: a newer writer owns the served branch",
    )
  _write_rolled_back_flag(target, message)
  CONFLICT_FLAG.unlink(missing_ok=True)
  _clear_reconcile_pre()
  return ReconcileResult.unchanged("rolled_back", pre, target, error=error)


def _finalize_update(
  repo: Path,
  local: str,
  *,
  pre: str,
  tip: str,
  target: str,
  progress: Callable[[PlatformUpdatePhase], None] | None,
  reconciliation: app_git.ReconciliationReceipt,
  overlay: dict | None,
) -> ReconcileResult:
  """Move the served branch to a complete candidate and run every gate.

  The one path that turns a candidate — a fast-forward target, a net-tree
  merge, or a resolver-finished merge — into the
  served tree: the declared dependency installs, the import probe, provenance
  bookkeeping, the upstream marker, flags, and the frontend rebuild. Every
  caller gets every gate, so a resolver-finished merge can never land with
  fewer checks than owner Apply. A rejected candidate rolls back to ``pre``
  (restoring the previous declared dependency versions) exactly like any
  other failed update.
  """
  changed = _activation_paths_between(repo, pre, tip)
  frontend_changed = any(path in _FRONTEND_DEPENDENCY_INPUTS for path in changed)
  touched_frontend = any(path.startswith("frontend/") for path in changed)

  if (overlay or {}).get("mode") == "net" and tip != pre:
    # The new linear commit replaces the old local commit chain. Keep the
    # latest replaced chain reachable for undo; main's reflog has older ones.
    _git("update-ref", _PRE_UPDATE_REF, pre, repo=repo)
  _activate_candidate(repo, local, pre, tip)
  app_git.remove_overlay_worktree(repo, _overlay_candidate_path(repo))

  # Post-reconcile import probe: a text-clean merge can still produce a tree
  # that fails to import (upstream dropped a module a local edit imports; a bad
  # deploy). Roll back to the previous served commit rather than serve it
  # broken. Skip the ~60s throwaway boot when the reconcile touched NO served
  # backend code (frontend/tests/docs/scripts only): the backend tree is then
  # byte-identical, so the probe would only re-prove an unchanged import.
  if platform_activation.backend_import_probe_required(changed):
    if progress:
      progress(PlatformUpdatePhase.VALIDATING)
    ok, err = _import_probe(repo)
    if not ok:
      return _roll_back_update(
        repo, local, pre, tip, target, err, err,
      )

  # Success: main now carries the update plus all local edits. Advance the
  # upstream marker and clear conflict/rollback flags. Owner Apply records
  # the remaining activation through its caller.
  try:
    app_git.carry_equivalent_change_sources(repo, pre, tip)
    app_git.retire_landed_equivalent_changes(repo, target)
  except Exception:
    # The target is already committed and validated.  A stale provenance ref is
    # harmless and can be retired by the next update; never turn housekeeping
    # into a false failed-update report after source has moved.
    log.warning("platform: could not update contribution provenance", exc_info=True)
  previous_upstream_sha = _rev(repo, UPSTREAM_BRANCH) or None
  _set_upstream(repo, target)
  CONFLICT_FLAG.unlink(missing_ok=True)
  ROLLED_BACK_FLAG.unlink(missing_ok=True)
  _clear_reconcile_pre()
  result = ReconcileResult(
    "updated", pre, tip, target, error=None,
    reconciliation=reconciliation,
    overlay=overlay,
  )
  if not touched_frontend:
    return result
  # Source moved without a watcher event. Dropping the build stamp makes the
  # watcher's startup check (and /api/version's freshness fact) see the
  # served bundle as behind the source until a rebuild actually publishes.
  _invalidate_frontend_build_stamp(repo)
  if progress:
    progress(PlatformUpdatePhase.BUILDING)
  try:
    if frontend_changed:
      deps_ok, deps_err = _sync_frontend_dependencies(repo)
      if not deps_ok:
        raise RuntimeError(f"frontend dependency install failed: {deps_err}")
    _rebuild_frontend(repo, result)
  except Exception as exc:
    log.warning(
      "frontend build rejected platform update %s: %r", _short(target), exc,
    )
    return _roll_back_failed_frontend_build(
      repo, result, previous_upstream_sha, exc,
      frontend_changed=frontend_changed,
    )
  return result


def _invalidate_frontend_build_stamp(repo: Path) -> None:
  (repo / "frontend" / ".source-build-signature").unlink(missing_ok=True)


def reconcile_clone(
  repo: Path = PLATFORM_REPO,
  *,
  target_ref: str = DEFAULT_TARGET_REF,
  fetch_remote: bool = True,
  progress: Callable[[PlatformUpdatePhase], None] | None = None,
) -> ReconcileResult:
  """Reconcile the served clone onto ``target_ref``, safely.

  Explicit updates only; startup never calls this source-changing path.
  Owner Apply passes a full reviewed oid with ``fetch_remote=False`` so it does
  not repeat network work or change the selected release. A success that
  changes backend code still needs a restart. Never raises for an operational
  failure (offline, conflict, import-broken) — it returns a
  :class:`ReconcileResult` describing the outcome and always leaves
  ``/data/platform`` in a clean, served state (either the update, or the pre-
  reconcile code) with the owner's uncommitted edits back in the working tree.
  """
  result = _reconcile_pass(
    repo, target_ref=target_ref, fetch_remote=fetch_remote,
    progress=progress,
  )
  if result.status == "skipped":
    return result
  local = _local_branch(repo)
  if _restore_working_edits(repo, local) and result.status == "updated":
    result = replace(result, new_sha=_rev(repo, local) or result.new_sha)
  return result


@dataclass(frozen=True)
class _Candidate:
  """A complete candidate tip for :func:`_finalize_update` to activate."""

  tip: str
  reconciliation: app_git.ReconciliationReceipt
  overlay: dict


def _apply_overlay(
  repo: Path,
  carried: _Carried,
  target: str,
  ordinary_base: str,
  reconciliation: app_git.ReconciliationReceipt,
) -> ReconcileResult | _Candidate:
  """Reconcile the final local and upstream trees once, off the served tree.

  Historical local commits may contain older versions of changes that have
  since landed upstream. Replaying each commit makes the owner resolve those
  obsolete intermediate states. A single three-way merge compares only the
  final trees, using reviewed contribution provenance when available.
  """
  source = carried.served
  equivalent = app_git.merge_with_equivalent_changes(repo, source, target)
  if equivalent is not None:
    reconciliation = equivalent.reconciliation
  merged = equivalent or app_git.merge_refs(repo, source, target)
  return _merged_candidate(
    repo, carried, target, merged,
    right=target, base=merged.merge_base_oid or ordinary_base,
    reconciliation=reconciliation,
  )


def _merged_candidate(
  repo: Path,
  carried: _Carried,
  target: str,
  merged: app_git.MergeResult,
  *,
  right: str,
  base: str,
  reconciliation: app_git.ReconciliationReceipt,
  resolved_working: tuple[str, str] | None = None,
) -> ReconcileResult | _Candidate:
  """Turn one final-tree merge into a candidate on ``target``, or park it.

  ``merged`` combines the committed local source with ``right`` (the reviewed
  target, or a resolver's answer on it) from ``base``; its result becomes the
  one local commit on ``target``. The transient working-tree commit is NOT
  part of that durable delta: uncommitted edits merge onto it separately so
  they return to the owner's working tree after activation.
  ``resolved_working`` is a resolver's answer to an earlier working-edit
  conflict as ``(answer commit, the working commit it was resolved from)``.
  If live edits changed under that answer, they are merged afresh instead.
  """
  if merged.status == "conflict":
    return _park_net_conflict(
      repo, carried, target, source=carried.served, right=right, base=base,
      stage="committed", reconciliation=reconciliation,
    )
  if not merged.merged_tree_oid:
    raise PlatformUpdateError("The local and upstream trees could not be merged.")
  tip = _net_overlay_commit(repo, target, merged.merged_tree_oid, carried.served)
  dirty = None
  if resolved_working is not None:
    answer, answered_from = resolved_working
    dirty = app_git.merge_refs(repo, carried.pre, answer, merge_base=answered_from)
    if dirty.status == "conflict":
      dirty = None
  if dirty is None and carried.working:
    dirty = app_git.merge_refs(repo, carried.pre, tip, merge_base=carried.served)
    if dirty.status == "conflict":
      return _park_net_conflict(
        repo, carried, target, source=carried.pre, right=tip,
        base=carried.served, stage="working", reconciliation=reconciliation,
      )
  if dirty is not None:
    if not dirty.merged_tree_oid:
      raise PlatformUpdateError("The working-tree merge returned no tree.")
    tip = _working_overlay_commit(repo, tip, dirty.merged_tree_oid)
  return _Candidate(tip, reconciliation, {"mode": "net", "source": carried.served})


def _park_net_conflict(
  repo: Path, carried: _Carried, target: str,
  *, source: str, right: str, base: str, stage: str,
  reconciliation: app_git.ReconciliationReceipt,
) -> ReconcileResult:
  """Park all residual conflicts from one tree merge in an isolated worktree.

  ``stage`` is ``committed`` when the committed source conflicts, or
  ``working`` when only the uncommitted edits conflict with a clean committed
  candidate (``right``); a resolved working answer stays uncommitted.
  """
  worktree = _overlay_candidate_path(repo)
  app_git.remove_overlay_worktree(repo, worktree)
  _git("worktree", "add", "--detach", "-q", str(worktree), source, repo=repo)
  paths = app_git.start_conflict_merge(
    worktree, merge_base=base, local_branch="HEAD", upstream_branch=right,
  )
  if not paths:
    raise PlatformUpdateError(
      "The net merge verdict changed while preparing its conflict worktree."
    )
  _git("update-ref", _CONFLICT_RIGHT_REF, right, repo=repo)
  parked = {
    "mode": "net", "worktree": str(worktree), "served": carried.served,
    "pre": carried.pre, "target": target, "paths": paths,
    "stage": stage, "base": base, "right": right,
  }
  _write_conflict_flag(target, paths, overlay=parked)
  ROLLED_BACK_FLAG.unlink(missing_ok=True)
  _clear_reconcile_pre()
  return ReconcileResult.unchanged(
    "conflict", carried.pre, target, conflict_paths=paths, overlay=parked,
    reconciliation=app_git.describe_reconciliation(
      repo, base, right, local=source, conflict_paths=paths,
    ),
  )


def _park_for_repair(
  repo: Path, target: str, blockers: list[str], image_digest: str | None,
) -> None:
  """Open the reviewed release in the isolated candidate for an agent.

  The agent works on this frozen copy of the merged update instead of the
  live checkout. Finishing goes through ``continue_platform_overlay_update``,
  which merges any live edits made meanwhile in one short locked step just
  before the switch.
  """
  source = _rev(repo, _local_branch(repo))
  carried = _Carried(served=source, pre=source, working=None)
  merged = (
    app_git.merge_with_equivalent_changes(repo, source, target)
    or app_git.merge_refs(repo, source, target)
  )
  base = merged.merge_base_oid or _git(
    "merge-base", source, target, repo=repo, check=False,
  ).stdout.strip()
  if merged.status == "conflict":
    _park_net_conflict(
      repo, carried, target, source=source, right=target, base=base,
      stage="committed", reconciliation=app_git.ReconciliationReceipt(),
    )
    flag = _read_conflict_flag() or {}
    parked = {
      **(flag.get("overlay") or {}), "blockers": blockers,
      "image_digest": image_digest,
    }
    _write_conflict_flag(target, list(flag.get("paths") or []), overlay=parked)
    return
  worktree = _overlay_candidate_path(repo)
  app_git.remove_overlay_worktree(repo, worktree)
  _git("worktree", "add", "--detach", "-q", str(worktree), source, repo=repo)
  # Leave the clean merge open so the agent's staged answer is picked up by
  # the same continue step a conflict resolution uses.
  app_git._run(worktree, "merge", "--no-commit", "--no-ff", "-q", target, check=False)
  _git("update-ref", _CONFLICT_RIGHT_REF, target, repo=repo)
  paths = _unmerged_paths(worktree)
  parked = {
    "mode": "net", "worktree": str(worktree), "served": source, "pre": source,
    "target": target, "paths": paths, "stage": "committed", "base": base,
    "right": target, "blockers": blockers, "image_digest": image_digest,
  }
  _write_conflict_flag(target, paths, overlay=parked)


class PreparedUpdate(TypedDict):
  """A checked update waiting to be swapped in, or swapped in and settling."""

  state: Literal["prepared", "swapped", "reverted"]
  snapshot: str  # the live source the update was prepared from
  prepared: str  # the checked commit that boots
  target: str  # the reviewed release it contains
  image_digest: str | None
  requires_image: bool
  late: str | None  # live state saved at the swap, in-progress edits on top
  late_committed: str | None  # its committed part


def read_prepared_update() -> PreparedUpdate | None:
  try:
    record = json.loads(PREPARED_UPDATE_PATH.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return None
  if not isinstance(record, dict) or record.get("state") not in {
    "prepared", "swapped", "reverted",
  }:
    return None
  return PreparedUpdate(
    state=record["state"],
    snapshot=str(record.get("snapshot") or ""),
    prepared=str(record.get("prepared") or ""),
    target=str(record.get("target") or ""),
    image_digest=record.get("image_digest") or None,
    requires_image=bool(record.get("requires_image")),
    late=record.get("late") or None,
    late_committed=record.get("late_committed") or None,
  )


def _write_prepared_update(record: PreparedUpdate) -> None:
  _atomic_write_text(PREPARED_UPDATE_PATH, json.dumps(record, sort_keys=True))


def _clear_prepared_update(repo: Path) -> None:
  PREPARED_UPDATE_PATH.unlink(missing_ok=True)
  for ref in (_PREPARED_REF, _LATE_REF):
    _git("update-ref", "-d", ref, repo=repo, check=False)


def _prepare(
  repo: Path, *, snapshot: str, prepared: str, target: str,
  image_digest: str | None, checkout: Path,
) -> PreparedUpdate:
  """Check a finished update on its frozen copy and record it for the swap.

  ``checkout`` holds exactly ``prepared``. Nothing here touches the live
  checkout: it keeps serving ``snapshot`` plus any later edits until the next
  shutdown swaps the update in.
  """
  from app.restart_util import RestartSourceInvalid, validate_restart_source

  if _changes_python_dependencies(repo, snapshot, prepared):
    # The running image cannot check source that imports new packages.
    raise PlatformUpdateError("image_rebuild_required")
  impact = _incoming_activation_impact(repo, snapshot, prepared)
  requires_image = (
    platform_activation.ActivationLevel.IMAGE_REBUILD.value
    in impact["required_actions"]
  )
  if requires_image:
    differing = _git(
      "diff", "--name-only", "--no-renames", target, prepared,
      repo=repo, check=False,
    ).stdout.split()
    kept = [path for path in differing if platform_activation.path_is_image_owned(path)]
    if kept:
      raise PlatformUpdateError(
        "The official image would drop these local image-owned changes: "
        + ", ".join(kept)
      )
  try:
    validate_restart_source(checkout)
  except RestartSourceInvalid as exc:
    raise PlatformUpdateError(str(exc)) from exc
  _git("update-ref", _PREPARED_REF, prepared, repo=repo)
  record = PreparedUpdate(
    state="prepared", snapshot=snapshot, prepared=prepared, target=target,
    image_digest=image_digest, requires_image=requires_image, late=None,
    late_committed=None,
  )
  _write_prepared_update(record)
  _set_update_progress(
    PlatformUpdatePhase.COMPLETE, plan_id=None, target_sha=target,
    image_digest=image_digest, active=False,
  )
  return record


def prepare_reviewed_update(
  *,
  plan_id: str,
  current_sha: str,
  target_sha: str,
  image_digest: str | None = None,
  repo: Path = PLATFORM_REPO,
) -> PreparedUpdate | ReconcileResult:
  """Prepare a reviewed update from a snapshot of the live source.

  The merge runs against the committed snapshot only; edits made afterwards
  are late edits that boot merges back. A conflict parks for the resolver.
  """
  with _reconcile_flock():
    _validate_update_plan(
      repo, plan_id=plan_id, current_sha=current_sha,
      target_sha=target_sha, image_digest=image_digest,
    )
    carried = _Carried(served=current_sha, pre=current_sha, working=None)
    equivalent = app_git.merge_with_equivalent_changes(repo, current_sha, target_sha)
    merged = equivalent or app_git.merge_refs(repo, current_sha, target_sha)
    base = merged.merge_base_oid or _git(
      "merge-base", current_sha, target_sha, repo=repo, check=False,
    ).stdout.strip()
    outcome = _merged_candidate(
      repo, carried, target_sha, merged, right=target_sha, base=base,
      reconciliation=(
        equivalent.reconciliation if equivalent else app_git.ReconciliationReceipt()
      ),
    )
    if isinstance(outcome, ReconcileResult):
      flag = _read_conflict_flag() or {}
      overlay = {**(flag.get("overlay") or {}), "image_digest": image_digest}
      _write_conflict_flag(target_sha, list(flag.get("paths") or []), overlay=overlay)
      return outcome
    with tempfile.TemporaryDirectory(prefix="mobius-prepared-") as tmp:
      checkout = Path(tmp) / "platform"
      _git("worktree", "add", "--detach", "-q", str(checkout), outcome.tip, repo=repo)
      try:
        return _prepare(
          repo, snapshot=current_sha, prepared=outcome.tip, target=target_sha,
          image_digest=image_digest, checkout=checkout,
        )
      finally:
        app_git.remove_overlay_worktree(repo, checkout)


async def prepare_platform_update(
  *,
  plan_id: str,
  current_sha: str,
  target_sha: str,
  image_digest: str | None = None,
) -> PreparedUpdate | PlatformApplyResult:
  """Prepare a reviewed update off the event loop.

  Returns the prepared record, or the conflict the review sheet renders (the
  merge is parked for its resolver and nothing was prepared).
  """
  outcome = await asyncio.to_thread(
    prepare_reviewed_update, plan_id=plan_id, current_sha=current_sha,
    target_sha=target_sha, image_digest=image_digest,
  )
  if not isinstance(outcome, ReconcileResult):
    return outcome
  return PlatformApplyResult(
    state=PlatformUpdateState.CONFLICT.value, needs_restart=False,
    activation=platform_activation.classify_activation([]),
    upstream_commit=target_sha, merge_commit=None,
    conflict_paths=outcome.conflict_paths, chat_id=None,
    phase=PlatformUpdatePhase.BLOCKED.value,
    reconciliation=outcome.reconciliation.as_dict(), error=None,
  )


def swap_in_prepared_update(
  *, cutover: bool, repo: Path = PLATFORM_REPO,
) -> bool:
  """At shutdown, after every chat is paused, point the live checkout at the
  checked update. Everything live (commits and in-progress edits) is saved as
  one commit under ``_LATE_REF`` first. An update that needs a new image
  swaps only at its container cutover, never at an unrelated restart.
  Returns whether the swap happened; failures leave the live source intact.
  """
  record = read_prepared_update()
  if record is None or record["state"] != "prepared":
    return False
  if record["requires_image"] and not cutover:
    return False
  with _reconcile_flock():
    local = _local_branch(repo)
    _reattach_detached_head(repo, local)
    carried = _carry_working_edits(repo, local)
    late = carried.pre
    try:
      _git("update-ref", _LATE_REF, late, repo=repo)
      # Record the swap first: if the process dies before the checkout moves,
      # or boot's guard restores ``late``, boot sees an unswapped checkout and
      # returns the update to prepared instead of losing the late edits.
      _write_prepared_update({
        **record, "state": "swapped", "late": late,
        "late_committed": carried.served,
      })
      _activate_candidate(repo, local, late, record["prepared"])
    except Exception:
      log.exception("platform: could not swap in the prepared update")
      _write_prepared_update(record)
      _restore_working_edits(repo, local)
      return False
    _clear_reconcile_pre()
    try:
      app_git.carry_equivalent_change_sources(repo, carried.served, record["prepared"])
      app_git.retire_landed_equivalent_changes(repo, record["target"])
    except Exception:
      log.warning("platform: could not update contribution provenance", exc_info=True)
    _set_upstream(repo, record["target"])
    ROLLED_BACK_FLAG.unlink(missing_ok=True)
    _record_update_activation(repo, record["prepared"], record["target"])
    return True


def cancel_prepared_update(repo: Path = PLATFORM_REPO) -> None:
  """Forget a prepared update that has not been swapped in.

  Nothing has touched the live checkout yet, so this is always safe; once
  swapped, the update boots and its late edits are merged back instead.
  """
  with _reconcile_flock():
    record = read_prepared_update()
    if record is None:
      return
    if record["state"] != "prepared":
      raise PlatformUpdateError("prepared_update_swapped")
    _clear_prepared_update(repo)


def late_edits_pending() -> bool:
  """Whether edits made on the previous source still await their merge back.

  Automatic chat resumes wait for this, so no agent resumes on a checkout
  that is missing its own recent edits.
  """
  record = read_prepared_update()
  return record is not None and record["state"] == "swapped"


def complete_platform_swap(repo: Path = PLATFORM_REPO) -> str | None:
  """At boot, finish a swap: merge late edits back or park them.

  Returns ``replayed``, ``conflict`` (parked for the resolver), ``reverted``
  (the boot script returned to the previous version), or None.
  """
  record = read_prepared_update()
  if record is None:
    return None
  local = _local_branch(repo)
  unswapped = PreparedUpdate(
    **{**record, "state": "prepared", "late": None, "late_committed": None},
  )
  if record["state"] == "reverted":
    # The previous version is back with its recent edits. The update stays
    # prepared, so the owner can retry Finish (often an interrupted cutover)
    # or cancel it.
    _write_rolled_back_flag(
      record["target"],
      "The updated version failed its startup check, so Möbius returned to "
      "the previous version with your recent edits.",
    )
    _restore_working_edits(repo, local)
    _write_prepared_update(unswapped)
    return "reverted"
  if record["state"] != "swapped" or not record["late"]:
    return None
  with _reconcile_flock():
    head = _rev(repo, local)
    if head != record["prepared"]:
      # The checkout never moved (the swap failed or boot restored it), so
      # there is nothing to merge back: the update is still just prepared.
      _restore_working_edits(repo, local)
      _write_prepared_update(unswapped)
      return "not_swapped"
    # The source moved while the server was down: install a changed
    # dependency lock now; the shell is rebuilt once late edits are back.
    moved = _activation_paths_between(repo, record["late"], head)
    if any(path in _FRONTEND_DEPENDENCY_INPUTS for path in moved):
      installed, error = _sync_frontend_dependencies(repo)
      if not installed:
        log.error("platform: frontend dependencies for the update failed: %s", error)
    # Late edits are local work and the booted update is the incoming
    # release: the same merge Apply uses, so in-progress edits come back
    # uncommitted and a conflict parks for a resolver on a frozen copy.
    late_committed = record["late_committed"] or record["late"]
    carried = _Carried(
      served=late_committed, pre=record["late"],
      working=record["late"] if record["late"] != late_committed else None,
    )
    outcome = _merged_candidate(
      repo, carried, head,
      app_git.merge_refs(repo, late_committed, head, merge_base=record["snapshot"]),
      right=head, base=record["snapshot"],
      reconciliation=app_git.ReconciliationReceipt(),
    )
    if isinstance(outcome, ReconcileResult):
      flag = _read_conflict_flag() or {}
      _write_conflict_flag(
        head, list(flag.get("paths") or []),
        overlay={**(flag.get("overlay") or {}), "replay": True, "live": head},
      )
      _invalidate_frontend_build_stamp(repo)
      return "conflict"
    if outcome.tip != head:
      _activate_candidate(repo, local, head, outcome.tip)
      _clear_reconcile_pre()
      _restore_working_edits(repo, local)
    # Dropping the stamp makes the watcher's startup check rebuild the shell.
    _invalidate_frontend_build_stamp(repo)
    _clear_prepared_update(repo)
    return "replayed"


def _net_overlay_commit(repo: Path, target: str, tree: str, source: str) -> str:
  """Make the one local commit after upstream; never rewrite the live branch."""
  target_tree = _commit_tree_oid(repo, target)
  if not target_tree:
    raise PlatformUpdateError("The reviewed upstream tree is unavailable.")
  if tree == target_tree:
    return target
  message = app_git.overlay_message(
    "Reconcile local platform source with reviewed upstream",
    unit="platform-local-tree", disposition="local-only",
    body=f"Previous local source: {source}",
  )
  return app_git._run(
    repo, "commit-tree", tree, "-p", target, "-m", message,
  ).stdout.strip()


def _working_overlay_commit(repo: Path, parent: str, tree: str) -> str:
  """Carry uncommitted edits temporarily so they can be unwound after Apply."""
  if tree == _commit_tree_oid(repo, parent):
    return parent
  return app_git._run(
    repo, "commit-tree", tree, "-p", parent, "-m",
    app_git.overlay_message(
      "platform: working edits carried across update",
      unit=app_git.OVERLAY_WORKING_UNIT, disposition="wip",
    ),
  ).stdout.strip()


def _record_update_activation(
  repo: Path, head: str | None, target: str | None,
) -> PlatformActivationImpact:
  """Record what the running process still owes for the release at ``head``.

  Compare what this process imported to the new head, not only the incoming
  target: local commits made while uvicorn ran are part of the same remainder.
  """
  served = _served_platform_sha()
  changed_paths = _activation_paths_between(repo, served, head)
  changed_paths = _paths_already_active_in_image(repo, changed_paths)
  incoming_impact = platform_activation.classify_activation(changed_paths)
  if incoming_impact["level"] != platform_activation.ActivationLevel.LIVE.value:
    mark_activation_needed(
      head or "", changed_paths, upstream_sha=target, repo=repo,
    )
  return _platform_activation_impact(repo, served_to_head=changed_paths)


def continue_platform_overlay_update(repo: Path = PLATFORM_REPO) -> str:
  """Finish one parked resolution.

  An update's answer becomes a prepared update: committed on the reviewed
  release and checked on the frozen copy, then swapped in at the next shutdown
  (``prepared``). The live checkout is untouched until then. Late edits parked
  after an update's boot merge into the live checkout directly (``updated``,
  ``conflict`` or ``rolled_back``).
  """
  with _reconcile_flock():
    flag = _read_conflict_flag() or {}
    parked = flag.get("overlay") or {}
    if parked.get("mode") != "net":
      raise PlatformUpdateError("No parked net-tree platform resolution.")
    local = _local_branch(repo)
    target = str(parked.get("target") or "")
    source = str(parked.get("served") or "")
    worktree = Path(str(parked.get("worktree") or _overlay_candidate_path(repo)))
    if not target or not source or not (worktree / ".git").exists():
      raise PlatformUpdateError("The parked net merge is incomplete.")
    answer = _resolved_tree(parked, worktree, source)
    if parked.get("stage") == "working":
      committed = str(parked.get("right") or "")
      resolved_working = (
        _working_overlay_commit(repo, committed, answer), str(parked.get("pre")),
      )
    else:
      committed = _net_overlay_commit(repo, target, answer, source)
      resolved_working = None
    # A committed update answer is prepared and swapped in at shutdown. A
    # conflict with the owner's own uncommitted edits, from an ordinary Apply,
    # and late edits after a swap finish on the live checkout under the lock.
    if not parked.get("replay") and parked.get("stage") != "working":
      _prepare(
        repo, snapshot=source,
        prepared=resolved_working[0] if resolved_working else committed,
        target=target, image_digest=parked.get("image_digest"), checkout=worktree,
      )
      app_git.remove_overlay_worktree(repo, worktree)
      CONFLICT_FLAG.unlink(missing_ok=True)
      return "prepared"
    # Late edits were parked against the booted update; live edits made since
    # that boot merge with the answer from there, and are unwound again below.
    live = str(parked.get("live") or source)
    _reattach_detached_head(repo, local)
    carried = _carry_working_edits(repo, local)
    if parked.get("replay") and resolved_working is not None:
      # A late in-progress edit was resolved against the booted update; it
      # returns uncommitted on top of whatever is live now.
      resolved_working = (resolved_working[0], carried.pre)
    try:
      outcome = _merged_candidate(
        repo, carried, target,
        app_git.merge_refs(repo, carried.served, committed, merge_base=live),
        right=committed, base=live,
        reconciliation=app_git.ReconciliationReceipt(),
        resolved_working=resolved_working,
      )
      if isinstance(outcome, ReconcileResult):
        _write_conflict_flag(
          target, outcome.conflict_paths, flag.get("chat_id"),
          overlay={**(outcome.overlay or {}), "replay": True, "live": carried.served},
        )
        return outcome.status
      result = _finalize_update(
        repo, local, pre=carried.pre, tip=outcome.tip, target=target,
        progress=None, reconciliation=outcome.reconciliation,
        overlay=outcome.overlay,
      )
      if result.status == "updated":
        if parked.get("replay"):
          _clear_prepared_update(repo)
        else:
          _record_update_activation(repo, result.new_sha, target)
      return result.status
    finally:
      _restore_working_edits(repo, local)


def _resolved_tree(parked: dict, worktree: Path, source: str) -> str:
  """The resolver's staged answer, only while its parked merge is intact."""
  left = str(parked.get("pre") if parked.get("stage") == "working" else source)
  right = str(parked.get("right") or "")
  merging = app_git.merge_in_progress(worktree)
  active_merge = (
    merging and _rev(worktree, "HEAD") == left
    and _rev(worktree, "MERGE_HEAD") == right
  )
  committed_merge = (
    not merging and _rev(worktree, "HEAD^1") == left
    and _rev(worktree, "HEAD^2") == right
  )
  if not (active_merge or committed_merge):
    raise PlatformUpdateError(
      "The parked merge is no longer in progress against its reviewed source. "
      "Abandon this resolution and review the update again."
    )
  if app_git.has_unresolved_conflicts(worktree):
    raise PlatformUpdateError("Unresolved files remain in the candidate worktree.")
  if committed_merge:
    # Git removes MERGE_HEAD after commit, so the ordinary unresolved check
    # cannot see markers that were mistakenly committed as the resolution.
    markers = _git(
      "grep", "-lE", r"^(<<<<<<< |>>>>>>> )", repo=worktree, check=False,
    )
    if markers.returncode != 1:
      raise PlatformUpdateError(
        "Conflict markers remain in the committed candidate resolution."
      )
  # write-tree reads the index, but a resolver can fix staged markers or add
  # files without staging the final bytes. Never take a stale index or
  # silently publish an untracked scratch file: require an explicit git add.
  tree = _git("write-tree", repo=worktree).stdout.strip()
  if _working_tree_oid(worktree, _rev(worktree, "HEAD")) != tree:
    raise PlatformUpdateError(
      "Stage every intended resolution before continuing the update."
    )
  return tree


def abandon_platform_overlay_update(repo: Path = PLATFORM_REPO) -> str:
  """Drop a parked tree merge and keep serving the pre-update tree."""
  with _reconcile_flock():
    flag = _read_conflict_flag() or {}
    parked = flag.get("overlay") or {}
    worktree = Path(str(parked.get("worktree") or _overlay_candidate_path(repo)))
    app_git.remove_overlay_worktree(repo, worktree)
    CONFLICT_FLAG.unlink(missing_ok=True)
    _restore_working_edits(repo, _local_branch(repo))
    if parked.get("replay"):
      # The late edits stay reachable under their ref; stop holding resumes.
      PREPARED_UPDATE_PATH.unlink(missing_ok=True)
    return "abandoned"


def _reconcile_under_lock(
  repo: Path,
  *,
  target_ref: str = DEFAULT_TARGET_REF,
  plan_id: str | None = None,
  current_sha: str | None = None,
  image_digest: str | None = None,
  allow_image_activation: bool = False,
  progress: Callable[[PlatformUpdatePhase], None] | None = None,
  lock_already_held: bool = False,
) -> ReconcileResult:
  """Serialize source updates with startup recovery using RECONCILE_LOCK.

  Owner Apply additionally validates its immutable review plan under the same
  lock. The lock covers source, dependency installation, the frontend build
  and rollback; browser progress reads use the durable phase record.
  """
  guard = contextlib.nullcontext() if lock_already_held else _reconcile_flock()
  with guard:
    if plan_id is not None:
      if current_sha is None:
        raise PlatformUpdateError("update_plan_invalid")
      _validate_update_plan(
        repo,
        plan_id=plan_id,
        current_sha=current_sha,
        target_sha=target_ref,
        image_digest=image_digest,
      )
      if not allow_image_activation:
        base = _git(
          "merge-base", current_sha, target_ref, repo=repo, check=False,
        ).stdout.strip() or current_sha
        # Existing local image drift is an independent remainder. It must not
        # turn an unrelated source-only update into an agent-only dead end.
        # Refuse only when the reviewed incoming release itself needs an image.
        impact = _incoming_activation_impact(repo, base, target_ref)
        if platform_activation.ActivationLevel.IMAGE_REBUILD.value in (
          impact["required_actions"]
        ):
          raise PlatformUpdateError("image_rebuild_required")
    result = reconcile_clone(
      repo,
      target_ref=target_ref,
      # A reviewed Apply already proved the immutable object exists. Fetching a
      # moving remote again adds latency and changes no reviewed decision.
      # Unreviewed operator calls refresh first; a shallow replay may deepen.
      fetch_remote=plan_id is None,
      progress=progress,
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


def _commit_timestamp(repo: Path, sha: str | None) -> str | None:
  """Return one commit's ISO timestamp without trusting display metadata."""
  if not sha:
    return None
  value = _git(
    "show", "-s", "--format=%cI", sha, repo=repo, check=False,
  ).stdout.strip()
  try:
    datetime.fromisoformat(value)
    return value
  except ValueError:
    return None


def _last_fetch_timestamp(repo: Path) -> str | None:
  """Return when Git last completed a fetch for this checkout."""
  raw = _git(
    "rev-parse", "--git-path", "FETCH_HEAD", repo=repo, check=False,
  ).stdout.strip()
  if not raw:
    return None
  path = Path(raw)
  if not path.is_absolute():
    path = repo / path
  try:
    current = path.stat()
  except OSError:
    return None
  if not stat.S_ISREG(current.st_mode):
    return None
  return datetime.fromtimestamp(current.st_mtime, timezone.utc).isoformat()


def reconcile_clone_sync() -> str:
  """Prepare installed source locally before uvicorn imports it.

  The name is the entrypoint contract baked into deployed images. It no longer
  reconciles a remote release: restarting must never become an update. Recovery
  restores interrupted work before completing activation or selecting trusted
  hooks. The shell's separate boot guard remains fail-closed if preparation
  fails or is interrupted.
  """
  try:
    with _reconcile_flock():
      recovery = boot_guard_clean_served_tree(PLATFORM_REPO)
      _normalize_activation_marker()
      _complete_boot_activation(PLATFORM_REPO)
      source = recorded_upstream_sha(PLATFORM_REPO)
      if source and not _is_ancestor(PLATFORM_REPO, source, "HEAD"):
        source = None  # Recovery may leave provenance unprovable; do not guess hook authority.
      hook_refresh = _refresh_git_hooks(PLATFORM_REPO, source)
      summary = f"startup[installed] head={_short(_rev(PLATFORM_REPO, 'HEAD'))} {recovery}"
      if hook_refresh == "":
        summary += " hooks=refreshed"
      elif hook_refresh:
        summary += f" hooks=error:{hook_refresh}"
      return summary
  except Exception as exc:
    return f"startup[error] {exc!r}"


def _state_for_activation(
  impact: PlatformActivationImpact,
) -> PlatformUpdateState:
  level = impact["level"]
  if level == platform_activation.ActivationLevel.SERVER_RESTART.value:
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


def unfinished_update(repo: Path = PLATFORM_REPO) -> UnfinishedUpdate | None:
  """The update that must finish before another starts, if any.

  Derived from records the updater already keeps: the parked merge and the
  activation remainder an update recorded for its release.
  """
  prepared = read_prepared_update()
  flag = _read_conflict_flag() if CONFLICT_FLAG.exists() else None
  if flag and flag.get("upstream"):
    return UnfinishedUpdate(
      target_sha=prepared["target"] if prepared else str(flag["upstream"]),
      stage="resolve", action="replace", cancellable=False,
    )
  if prepared and prepared["state"] in {"prepared", "swapped"}:
    return UnfinishedUpdate(
      target_sha=prepared["target"], stage="finish",
      action="replace" if prepared["requires_image"] else "restart",
      cancellable=prepared["state"] == "prepared",
    )
  marker = _read_activation_marker()
  if marker and marker["upstream_sha"] and any(
    platform_activation.classify_activation([path])["level"]
    == platform_activation.ActivationLevel.IMAGE_REBUILD.value
    for path in marker["image_paths"]
  ):
    return UnfinishedUpdate(
      target_sha=marker["upstream_sha"], stage="finish", action="replace",
      cancellable=False,
    )
  return None


def park_update_for_agent(
  *,
  plan_id: str,
  current_sha: str,
  target_sha: str,
  image_digest: str | None = None,
  repo: Path = PLATFORM_REPO,
) -> UnfinishedUpdate:
  """Hand a reviewed, blocked update to an agent on a frozen copy.

  The update is parked in the isolated candidate (see ``_park_for_repair``)
  so the agent never edits the live checkout, and it stays the one update to
  finish until the resolver continues it or abandons it.
  """
  with _reconcile_flock():
    _validate_update_plan(
      repo, plan_id=plan_id, current_sha=current_sha,
      target_sha=target_sha, image_digest=image_digest,
    )
    if not unfinished_update(repo):
      base = _git(
        "merge-base", current_sha, target_sha, repo=repo, check=False,
      ).stdout.strip() or current_sha
      _park_for_repair(repo, target_sha, container_replacement_blockers(
        target_sha, repo, local_change_base=base,
      ), image_digest)
    pending = unfinished_update(repo)
    if not pending or pending["target_sha"] != target_sha:
      raise PlatformUpdateError("update_plan_stale")
    return pending


def platform_status(
  repo: Path = PLATFORM_REPO,
  *,
  target_sha: str | None = None,
) -> PlatformStatus:
  """Compute update availability on demand (no daemon, no polling, no fetch).

  Availability is an EXACT ancestry check against the selected release. Generic
  self-hosted callers use ``origin/main``; managed deployments pass the exact
  GHCR release SHA so Settings never advertises a Git commit that has no
  deployable image. Conflict and rolled-back states take precedence over a bare
  "available".
  """
  local = _update_source_tip(repo)
  image_sha = current_build_sha()
  upstream_sha = recorded_upstream_sha(repo)
  conflict = CONFLICT_FLAG.exists() or _reconcile_in_progress(repo)
  rolled_back = ROLLED_BACK_FLAG.exists()
  rollback = _read_rolled_back_flag() if rolled_back else None
  # A rollback flag is stale once its recorded target is already contained in
  # local main: the failed release (or an equivalent) has since landed, so there
  # is nothing left to repair. Mirror _reconcile_pass and stop projecting "needs
  # repair" — otherwise Settings shows a permanent ghost that no read/check path
  # clears. This projection is read-only; check_for_updates removes the file
  # under the reconcile lock.
  if rollback and rollback.get("target") and _is_ancestor(repo, rollback["target"], local):
    rolled_back = False
    rollback = None
  activation = _platform_activation_impact(repo)
  activation_state = _state_for_activation(activation)
  restart_needed = (
    activation["level"]
    == platform_activation.ActivationLevel.SERVER_RESTART.value
  )
  target = _rev(repo, target_sha) if target_sha else _latest_known_release(repo)
  if not target and not target_sha:
    raise PlatformUpdateError("platform_target_unavailable")
  # Managed deployments may select a verified image release whose Git object
  # is not present in the persistent checkout yet. Preserve that exact selected
  # identity; self-hosted refs use the locally resolved commit.
  checked_target_sha = target_sha or target
  target_contained = bool(target) and _is_ancestor(repo, target, local)
  contained_upstream_sha = target if target_contained else (
    upstream_sha if upstream_sha and _is_ancestor(repo, upstream_sha, local) else None
  )
  contained_upstream_committed_at = _commit_timestamp(
    repo, contained_upstream_sha,
  )
  current_build_committed_at = _commit_timestamp(repo, image_sha)
  upstream_checked_at = _last_fetch_timestamp(repo)

  if conflict:
    flag = _read_conflict_flag() or {}
    paths = flag.get("paths") or _unmerged_paths(repo)
    # `target` is the last-fetched origin/main. If it strictly descends the
    # version this conflict is pinned to, newer releases stacked up behind it.
    # They wait until this one finishes (see ``unfinished_update``).
    conflict_target = flag.get("upstream")
    newer_available = bool(
      target and conflict_target and target != conflict_target
      and _is_ancestor(repo, conflict_target, target)
    )
    return PlatformStatus(
      state=PlatformUpdateState.CONFLICT.value, available=False,
      needs_restart=restart_needed, activation=activation,
      current_build_sha=image_sha,
      checked_target_sha=checked_target_sha,
      installed_release_sha=upstream_sha,
      recorded_upstream_sha=upstream_sha,
      contained_upstream_sha=contained_upstream_sha,
      contained_upstream_committed_at=contained_upstream_committed_at,
      current_build_committed_at=current_build_committed_at,
      upstream_checked_at=upstream_checked_at,
      seed_required=False,
      conflict_paths=paths, conflict_chat_id=flag.get("chat_id"),
      newer_updates_available=newer_available,
      rollback_target_sha=None, rollback_error=None,
      unfinished_update=unfinished_update(repo),
      )

  # A freshly published GHCR revision may not yet be in this clone's object
  # store. Its immutable SHA is still authoritative evidence that a different
  # release exists; the preview/check path fetches and proves the source object
  # before it creates an actionable plan.
  available = bool(target_sha or target) and not target_contained

  if rolled_back:
    # A not-yet-landed target whose last apply failed (import probe or build).
    # Stale rollbacks whose target already landed were cleared above.
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
    current_build_sha=image_sha,
    checked_target_sha=checked_target_sha,
    installed_release_sha=upstream_sha,
    recorded_upstream_sha=upstream_sha,
    contained_upstream_sha=contained_upstream_sha,
    contained_upstream_committed_at=contained_upstream_committed_at,
    current_build_committed_at=current_build_committed_at,
    upstream_checked_at=upstream_checked_at,
    seed_required=False, conflict_paths=[], conflict_chat_id=None,
    newer_updates_available=False,
    rollback_target_sha=(rollback or {}).get("target"),
    rollback_error=(rollback or {}).get("error"),
    unfinished_update=unfinished_update(repo),
  )


def check_for_updates(
  repo: Path = PLATFORM_REPO,
  *,
  target_sha: str | None = None,
) -> PlatformStatus:
  """Owner-triggered "Check for updates": fetch origin, THEN report availability.

  :func:`platform_status` is deliberately fetch-free — it reads
  ``origin/main`` left by the last explicit check/update — so this is
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
    # Do not rely on remote.origin.fetch: older single-branch checkouts may
    # have a configured refspec that cannot advance origin/main.
    if not _fetch(repo, refspec=OWNER_UPDATE_FETCH_REFSPEC):
      raise PlatformUpdateError("platform_fetch_failed")
    if target_sha and _rev(repo, target_sha) != target_sha:
      raise PlatformUpdateError("image_release_source_unavailable")
    target = _latest_known_release(repo)
    local = _local_branch(repo)
    if target and _is_ancestor(repo, target, local):
      _set_upstream(repo, target)
    # Remove a stale rollback flag whose target already landed, so the owner's
    # explicit check clears the "needs repair" ghost for good (safe under the
    # reconcile lock; a live rollback is always for a not-yet-contained target).
    rollback = _read_rolled_back_flag()
    if rollback and rollback.get("target") and _is_ancestor(repo, rollback["target"], local):
      ROLLED_BACK_FLAG.unlink(missing_ok=True)
  return platform_status(repo, target_sha=target_sha)


def empty_platform_update_preview(
  *, current_sha: str | None = None, target_sha: str | None = None,
  image_digest: str | None = None,
) -> PlatformUpdatePreview:
  """A verified preview carrying no incoming changes or activation work."""
  incoming_activation = platform_activation.classify_activation([])
  return PlatformUpdatePreview(
    state=PlatformUpdateState.UP_TO_DATE.value, available=False,
    actionable=False, operation="none",
    current_sha=current_sha, target_sha=target_sha, plan_id=None,
    image_digest=image_digest,
    activation=incoming_activation,
    incoming_activation=incoming_activation,
    total_commits=0, commits_truncated=False,
    commits=[], files=[], diff=None, diff_truncated=False, conflict_paths=[],
    blocking_paths=[], blocking_diff=None, blocking_diff_truncated=False,
  )


def prepared_update_preview(
  repo: Path = PLATFORM_REPO,
) -> PlatformUpdatePreview | None:
  """Finish review for a prepared update: what its swap still needs."""
  record = read_prepared_update()
  if record is None or record["state"] != "prepared":
    return None
  current = _update_source_tip(repo)
  activation = _incoming_activation_impact(
    repo, record["snapshot"], record["prepared"],
  )
  preview = empty_platform_update_preview(
    current_sha=current, target_sha=record["target"],
    image_digest=record["image_digest"],
  )
  preview.update(
    state=PlatformUpdateState.ACTIVATION_NEEDED.value, actionable=True,
    operation="finish", activation=activation, incoming_activation=activation,
    plan_id=_update_plan_id(current, record["target"], record["image_digest"]),
  )
  return preview


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


def _preview_blocking_diff(
  repo: Path, target: str, paths: list[str],
) -> tuple[str | None, bool]:
  """Explain the local image-owned behavior a replacement would remove."""
  if not paths:
    return None, False
  chunks: list[str] = []
  for path in paths:
    reviewed = _git(
      "show", f"{target}:{path}", repo=repo, check=False,
    )
    reviewed_text = reviewed.stdout if reviewed.returncode == 0 else ""
    local_text = _read_worktree_path_without_links(repo, path)
    delta = "".join(difflib.unified_diff(
      reviewed_text.splitlines(keepends=True),
      local_text.splitlines(keepends=True),
      fromfile=f"reviewed/{path}",
      tofile=f"local/{path}",
    ))
    if delta and not delta.endswith("\n"):
      delta += "\n"
    chunks.append(delta)
  text = "".join(chunks)
  if len(text) > MAX_PREVIEW_DIFF_CHARS:
    return text[:MAX_PREVIEW_DIFF_CHARS], True
  return (text or None), False


def _read_worktree_path_without_links(repo: Path, relative: str) -> str:
  """Read one regular worktree file without following any path symlink.

  Blocker previews are owner-visible and can be copied into a repair chat, so
  a working-tree link must never turn this read into disclosure of a host or
  owner-data file outside the checkout. Directory descriptors pin every path
  component and ``O_NOFOLLOW`` closes the final-component swap race.
  """
  parts = Path(relative).parts
  if (
    not parts or relative.startswith("/")
    or any(part in {"", ".", ".."} for part in parts)
  ):
    return ""
  opened: list[int] = []
  try:
    current = os.open(repo, os.O_RDONLY | os.O_DIRECTORY)
    opened.append(current)
    for part in parts[:-1]:
      current = os.open(
        part,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=current,
      )
      opened.append(current)
    try:
      leaf = os.open(
        parts[-1],
        os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
        dir_fd=current,
      )
    except FileNotFoundError:
      return f"(local path is not present: {relative})\n"
    except OSError:
      try:
        # Git represents a symlink by its destination string. Reading that
        # string through the pinned parent descriptor remains no-follow.
        return os.readlink(parts[-1], dir_fd=current)
      except FileNotFoundError:
        return f"(local path is not present: {relative})\n"
      except OSError:
        return f"(local path was not read because it crosses a link: {relative})\n"
    opened.append(leaf)
    if not stat.S_ISREG(os.fstat(leaf).st_mode):
      return ""
    chunks: list[bytes] = []
    remaining = MAX_PREVIEW_DIFF_CHARS + 1
    while remaining > 0:
      chunk = os.read(leaf, min(65536, remaining))
      if not chunk:
        break
      chunks.append(chunk)
      remaining -= len(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")
  except FileNotFoundError:
    return f"(local path is not present: {relative})\n"
  except OSError:
    return f"(local path was not read because it crosses a link: {relative})\n"
  finally:
    for descriptor in reversed(opened):
      with contextlib.suppress(OSError):
        os.close(descriptor)


def platform_update_preview(
  repo: Path = PLATFORM_REPO,
  *,
  target_sha: str | None = None,
  image_digest: str | None = None,
) -> PlatformUpdatePreview:
  """Read-only preview of the incoming platform update, for the Settings review
  step before Apply. Generic previews remain fetch-free. When a managed
  deployment supplies an exact GHCR target, this function may refresh the
  canonical remote ref solely to obtain and verify that immutable source tree;
  it never mutates the served branch or working tree.

  Shows the upstream-side changes ``origin/main`` brings since the shared merge
  base; local edits are excluded from the public diff. Review never reconciles
  local history. Apply owns that work once and reports a real conflict if one
  exists, rather than making every review merge the local source.
  Availability is the same ancestry check :func:`platform_status` uses; an
  already-applied target can still have actionable activation work.
  Missing source or target provenance is an explicit error on both deployments;
  "unavailable" must never masquerade as "up to date."""
  # A missing clone has no snapshot to lock. Fail explicitly without requiring
  # a writable /data recovery surface; existing clones are checked under lock.
  if not (repo / ".git").exists():
    raise PlatformUpdateError("platform_repo_missing")
  with _reconcile_flock(blocking=False):
    preview = _platform_update_preview_unlocked(
      repo,
      target_sha=target_sha,
      image_digest=image_digest,
    )
    if "image_rebuild" in preview["activation"]["required_actions"]:
      current, target = preview["current_sha"], preview["target_sha"]
      base = _git(
        "merge-base", current, target, repo=repo, check=False,
      ).stdout.strip() or current
      preview["blocking_paths"] = container_replacement_blockers(
        target, repo, local_change_base=base,
      )
      preview["blocking_diff"], preview["blocking_diff_truncated"] = (
        _preview_blocking_diff(
          repo, target, preview["blocking_paths"],
        )
      )
    return preview


def _platform_update_preview_unlocked(
  repo: Path,
  *,
  target_sha: str | None = None,
  image_digest: str | None = None,
) -> PlatformUpdatePreview:
  """Build one preview while the reconcile lock holds its source snapshot."""
  local_sha = _update_source_tip(repo)
  if target_sha:
    if not re.fullmatch(r"[0-9a-f]{40}", target_sha):
      raise PlatformUpdateError("image_release_invalid")
    if _rev(repo, target_sha) != target_sha:
      if not _fetch(repo, refspec=OWNER_UPDATE_FETCH_REFSPEC):
        raise PlatformUpdateError("image_release_source_unavailable")
    if _rev(repo, target_sha) != target_sha:
      raise PlatformUpdateError("image_release_source_unavailable")
  local = local_sha
  target = (
    _rev(repo, target_sha) if target_sha else _latest_known_release(repo)
  ) or None
  if not target:
    raise PlatformUpdateError("platform_target_unavailable")
  available = not _is_ancestor(repo, target, local)
  if not available:
    preview = empty_platform_update_preview(
      current_sha=local_sha, target_sha=target,
      image_digest=image_digest,
    )
    activation = _platform_activation_impact(repo)
    if local_sha and target and activation["level"] != "live":
      preview.update(
        state=_state_for_activation(activation).value,
        actionable=True,
        operation="finish",
        plan_id=_update_plan_id(local_sha, target, image_digest),
        activation=activation,
      )
    return preview
  base = _git(
    "merge-base", local, target, repo=repo, check=False,
  ).stdout.strip() or local_sha
  if not base:
    # No shared base and no local tip to diff against — surface availability
    # without a diff rather than raising.
    incoming_activation = platform_activation.classify_activation(["backend/app"])
    return PlatformUpdatePreview(
      state=PlatformUpdateState.AVAILABLE.value, available=True,
      actionable=True, operation="update",
      current_sha=local_sha, target_sha=target,
      plan_id=(
        _update_plan_id(local_sha, target, image_digest) if local_sha else None
      ),
      image_digest=image_digest,
      activation=incoming_activation,
      incoming_activation=incoming_activation,
      total_commits=0, commits_truncated=False, commits=[], files=[],
      diff=None, diff_truncated=False, conflict_paths=[], blocking_paths=[],
      blocking_diff=None, blocking_diff_truncated=False,
    )
  diff, truncated = _preview_diff(repo, base, target)
  commits = _preview_commits(repo, base, target)
  total_commits = _preview_commit_count(repo, base, target)
  conflict = _read_conflict_flag() or {}
  # Review this incoming release on its own. Existing activation drift remains
  # visible in status after Apply, but must not turn an unrelated source update
  # into an image replacement or agent-only dead end.
  incoming_activation = _incoming_activation_impact(repo, base, target)
  return PlatformUpdatePreview(
    state=PlatformUpdateState.AVAILABLE.value, available=True,
    actionable=True, operation="update",
    current_sha=local_sha, target_sha=target,
    plan_id=(
      _update_plan_id(local_sha, target, image_digest) if local_sha else None
    ),
    image_digest=image_digest,
    activation=incoming_activation,
    incoming_activation=incoming_activation,
    total_commits=total_commits,
    commits_truncated=total_commits > len(commits),
    commits=commits,
    files=_preview_files(repo, base, target),
    diff=diff, diff_truncated=truncated,
    conflict_paths=sorted(set(conflict.get("paths") or [])),
    blocking_paths=[],
    blocking_diff=None,
    blocking_diff_truncated=False,
  )


async def apply_platform_update(
  db: Session,
  *,
  plan_id: str,
  current_sha: str,
  target_sha: str,
  image_digest: str | None = None,
  repo: Path = PLATFORM_REPO,
  allow_image_activation: bool = False,
) -> PlatformApplyResult:
  """Complete an admitted Apply transaction even if its HTTP client leaves.

  Cancellation before this request acquires the update lock cancels it without
  mutation. Once admitted, the reconcile and its durable terminal status are
  one operation: a disconnected client may stop waiting, but cannot leave
  source published with progress still marked active.
  """
  started = asyncio.Event()
  task = asyncio.create_task(_apply_platform_update_guarded(
    db,
    plan_id=plan_id,
    current_sha=current_sha,
    target_sha=target_sha,
    image_digest=image_digest,
    repo=repo,
    allow_image_activation=allow_image_activation,
    started=started,
  ))
  try:
    return await asyncio.shield(task)
  except asyncio.CancelledError:
    if not started.is_set():
      task.cancel()
    while not task.done():
      try:
        await asyncio.shield(task)
      except asyncio.CancelledError:
        continue
      except Exception:
        break
    if started.is_set():
      try:
        task.result()
      except Exception:
        log.exception("platform Apply failed after its client disconnected")
    raise


async def _apply_platform_update_guarded(
  db: Session,
  *,
  plan_id: str,
  current_sha: str,
  target_sha: str,
  image_digest: str | None = None,
  repo: Path = PLATFORM_REPO,
  allow_image_activation: bool = False,
  started: asyncio.Event,
) -> PlatformApplyResult:
  """Owner-triggered reconcile with an explicit activation remainder.

  Clean source can be live, restartable, or require an external deployment
  action.  Conflict/rollback leave the previous runtime and its remainder
  untouched.  The backend never invokes Docker, Caddy, or a provider control
  plane.
  """
  async with _APPLY_LOCK:
    started.set()
    return await asyncio.to_thread(
      _apply_platform_update_sync,
      plan_id=plan_id,
      current_sha=current_sha,
      target_sha=target_sha,
      image_digest=image_digest,
      repo=repo,
      allow_image_activation=allow_image_activation,
    )


def _apply_platform_update_sync(
  *,
  plan_id: str,
  current_sha: str,
  target_sha: str,
  image_digest: str | None,
  repo: Path,
  allow_image_activation: bool,
) -> PlatformApplyResult:
  """Own reconcile through terminal progress under one cross-process lock."""
  with _reconcile_flock():
    _set_update_progress(
      PlatformUpdatePhase.PREPARING,
      plan_id=plan_id,
      target_sha=target_sha,
      image_digest=image_digest,
      active=True,
    )

    def publish_progress(phase: PlatformUpdatePhase) -> None:
      _set_update_progress(
        phase,
        plan_id=plan_id,
        target_sha=target_sha,
        image_digest=image_digest,
        active=True,
      )

    try:
      existing_conflict = _read_conflict_flag() or {}
      res = _reconcile_under_lock(
        repo,
        target_ref=target_sha,
        plan_id=plan_id,
        current_sha=current_sha,
        image_digest=image_digest,
        allow_image_activation=allow_image_activation,
        progress=publish_progress,
        lock_already_held=True,
      )
      chat_id: str | None = None

      def record_current_activation(head: str | None) -> PlatformActivationImpact:
        return _record_update_activation(repo, head, res.target_sha)

      if res.status == "updated":
        publish_progress(PlatformUpdatePhase.FINALIZING)
        hook_refresh = _refresh_git_hooks(repo, res.hook_source_sha)
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
          overlay=res.overlay,
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
        image_digest=image_digest,
        active=False,
        error=res.error if state is PlatformUpdateState.ROLLED_BACK else None,
      )
      return PlatformApplyResult(
        state=state.value,
        needs_restart=(
          _state_for_activation(activation) is PlatformUpdateState.RESTART_NEEDED
        ),
        activation=activation,
        upstream_commit=res.target_sha,
        merge_commit=res.new_sha if res.status == "updated" else None,
        conflict_paths=res.conflict_paths,
        chat_id=chat_id,
        phase=final_phase.value,
        reconciliation=res.reconciliation.as_dict(),
        error=res.error if state is PlatformUpdateState.ROLLED_BACK else None,
      )
    except Exception as exc:
      phase = PlatformUpdatePhase.FAILED
      _set_update_progress(
        phase,
        plan_id=plan_id,
        target_sha=target_sha,
        image_digest=image_digest,
        active=False,
        error=str(exc)[:_ERROR_EXCERPT_CHARS] or exc.__class__.__name__,
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
  parked = flag.get("overlay")
  if not target_sha:
    raise PlatformUpdateError("Platform conflict target is unavailable.")
  result = await spawn_platform_conflict_chat(
    db, conflict_paths, target_sha, parked,
  )
  if result is None:
    raise PlatformUpdateError("Could not open resolver chat.")

  _write_conflict_flag(
    target_sha,
    conflict_paths,
    result["chat_id"],
    overlay=parked,
  )
  return result


# Shared by every agent the owner asks to unblock an update: the owner's request
# covers finishing that exact update, through the same controls Settings uses.
FINISH_UPDATE_INSTRUCTIONS = (
  "finish this same update yourself; the owner's request to resolve it "
  "covers that. Read `mapi '/api/platform/update-preview?intent=finish'`: it "
  "names the prepared release and never offers a newer one. If "
  "`activation.required_actions` includes `image_rebuild`, POST its "
  "`plan_id`, `current_sha`, `target_sha` and `image_digest` to "
  "`/api/platform/rebuild`: the prepared update is swapped in at that "
  "container cutover and Möbius restarts once. Otherwise request a restart "
  "with the restart card; the prepared update is swapped in at that restart. "
  "Either way this chat resumes after boot: confirm the release is running. "
  "A plain restart never swaps in an update that needs a new image. If it "
  "cannot finish, say exactly why: Settings keeps offering Finish update for "
  "this release until it does."
)


def _platform_conflict_resolver_message(
  target_sha: str,
  conflict_paths: list[str],
  overlay: dict | None = None,
) -> str:
  """Instructions bound to the exact release the owner reviewed and applied."""
  files = ", ".join(conflict_paths) if conflict_paths else "some files"
  if overlay and overlay.get("replay"):
    return (
      "Möbius just finished a platform update. Edits made on the previous "
      "version while it was being finished overlap it in: " + files + ". "
      "Paused chats resume once these edits are back.\n\n"
      f"Resolve the marked files in `{overlay.get('worktree')}`, keeping "
      "both the update and the intent of those edits, stage them, then run "
      "`cd /data/platform/backend && python3 -c \"from app.platform_update "
      "import continue_platform_overlay_update as c; print(c())\"`. It "
      "merges your answer into the live checkout with the normal build and "
      "import checks. If it prints `conflict`, newer live edits overlap your "
      "answer; resolve the fresh markers and run it again. Backend edits "
      "load at the next restart."
    )
  if overlay and overlay.get("mode") == "net":
    blockers = list(overlay.get("blockers") or [])
    opening = (
      "The platform update compared the final local source with the reviewed "
      "upstream source once. Both changed these paths: " + files + ".\n\n"
      if conflict_paths else
      "The owner asked you to finish a platform update that local changes "
      "block.\n\n"
    )
    if blockers:
      opening += (
        "These image-owned files differ from the reviewed release, so its "
        "official container image would drop what they do: "
        + ", ".join(blockers) + ". In the candidate below, keep any behavior "
        "that still matters through its proper owner (an upstream pull "
        "request, the installed skill, or an app), then make each file match "
        f"the release with `git checkout {target_sha} -- <path>` (or `git rm` "
        "it when the release does not have it).\n\n"
      )
    return opening + (
      "The running platform is untouched. Resolve all marked files together "
      f"in the isolated candidate at `{overlay.get('worktree')}`; preserve "
      "the intended local behavior and the incoming upstream behavior. Stage "
      "the resolved files there, then run `cd /data/platform/backend && "
      "python3 -c \"from app.platform_update import "
      "continue_platform_overlay_update as c; print(c())\"`. It commits "
      "your answer on the reviewed release and runs the same startup check "
      "the next boot will, without touching the live checkout. Edits other "
      "chats make meanwhile are not part of this update; they are merged "
      "back after it boots. When it prints `prepared`, "
      + FINISH_UPDATE_INSTRUCTIONS
    )
  return (
    "This platform update conflict was recorded by an older updater "
    f"(reviewed target `{target_sha}`; files: {files}). The running "
    "platform is untouched. Preserve any resolution you still need, then "
    "abandon it with `cd /data/platform/backend && python3 -c \"from "
    "app.platform_update import abandon_platform_overlay_update as a; "
    "print(a())\"` and ask the owner to review the update again, which "
    "compares the final local and upstream trees once."
  )


async def spawn_platform_conflict_chat(
  db: Session,
  conflict_paths: list[str],
  target_sha: str,
  overlay: dict | None = None,
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
  data_dir = get_settings().data_dir
  # Automatic app-agent work: resolve the provider from the owner's
  # background-agents list, walked to the first entry with usage quota, instead
  # of the interactive default. The owner can switch it in-chat afterwards.
  from app import background_agents
  _bg_choice = background_agents.resolve_background_chat_choice(data_dir, db)
  provider = _bg_choice["provider"]
  agent_settings = _bg_choice["agent_settings"]

  content = _platform_conflict_resolver_message(
    target_sha, conflict_paths, overlay,
  )

  chat_id = str(uuid.uuid4())
  chat = models.Chat(
    id=chat_id, title=title, messages=[], pending_messages=[],
    provider=provider, agent_settings_json=agent_settings,
    created_by_app_id=None,
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
