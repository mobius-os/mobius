"""Platform self-update — clone-native ``git fetch`` + linear overlay replay.

``/data/platform`` is a real ``git clone`` of the canonical repo; uvicorn serves
its backend directly (``cd /data/platform/backend && uvicorn app.main:app``).
Local ``main`` is the exact accepted upstream commit (recorded by the
``upstream`` marker branch) plus a LINEAR overlay of local commits, each tagged
with the unit it belongs to and why it is local (see ``app_git`` overlay
trailers). Only an explicit update replays that overlay onto a new target.
Owner Apply pins the exact target returned by the review plan; backend changes
then need a restart to load. Startup recovers interrupted work and runs the
installed source, without fetching or selecting another release.

Upstream is never merged INTO local history: that woven the same change in
twice whenever a contribution landed upstream under a squash identity, and
every later update re-merged an ever-larger local lineage. Instead each update
drops the overlay commits proven to have landed (equivalence anchors by
patch-id, or a cherry-pick that becomes empty), replays the rest in an
isolated worktree, and moves ``main`` to the candidate only once it is
complete. ``upstream..main`` therefore always reads as exactly what this
installation carries on top of upstream.

The reconcile is built to be non-destructive above all else:

1. ``/data/platform`` holds the SERVED backend, so a reconcile must never leave a
   half-applied tree. A replay conflict stays in the candidate worktree (the
   old, working code keeps serving) and is surfaced for a resolver; a crash
   mid-reconcile is detected on the next boot and reset before anything else
   runs. Legacy interrupted merges and rebases are still cleaned up too.

2. Local edits are NEVER lost. Uncommitted working-tree edits ride through the
   update as a transient overlay commit and return to the working tree
   afterwards, so ``git status`` reads the same before and after. A conflict
   or an import-broken result rolls the served tree back to exactly those
   local edits.

3. A clean replay can still produce a tree that fails to import (e.g. upstream
   deleted a module a local edit still imports). A post-replay import probe
   catches that and rolls back to the previous served commit rather than
   serving a broken tree.

A pre-overlay history (merge commits below ``main``) is folded into one
``legacy-overlay`` unit the first time this updater runs on it, using the same
off-tree net merge and reviewed-change provenance the merge model used, so an
existing installation converges on the linear shape without a manual rewrite.

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
# Container-local: survives a server restart but never claims a replacement
# image inherited packages installed in the previous container.
DEPENDENCY_RECEIPT_PATH = Path("/tmp/mobius-dependency-inputs.json")

UPSTREAM_BRANCH = "upstream"
LOCAL_BRANCH = "main"
DEFAULT_TARGET_REF = "origin/main"
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
# The candidate worktree an update replays the overlay into. It lives inside
# the clone's own git directory so neither the outer ``/data`` safety repo nor
# the platform tree ever sees it as content, and a conflicting replay can stay
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
  contained_upstream_committed_at: str | None
  # Timestamp of the most recent successful fetch represented by this status.
  # GET /status remains fetch-free; POST /check advances FETCH_HEAD first.
  upstream_checked_at: str | None
  seed_required: bool
  conflict_paths: list[str]
  # The resolver chat opened for an in-progress conflict, so Settings can link
  # the owner straight to it. None unless ``state == "conflict"`` AND the id was
  # recorded.
  conflict_chat_id: str | None
  # True only while ``state == "conflict"`` and origin/main has advanced past the
  # version this conflict is pinned to — i.e. more updates stacked up behind the
  # one being resolved. Lets Settings offer "review all & resolve together" so a
  # backlog is reviewed once and resolved once, instead of one resolve per
  # release. Fetch-free like the rest of status: reflects the last fetch.
  newer_updates_available: bool
  rollback_target_sha: str | None
  rollback_error: str | None
  # The overlay invariant: ``linear`` when the contained upstream commit is an
  # ancestor of local main with no merge between them; ``units`` lists what
  # this installation carries on top of upstream, in replay order.
  overlay: dict | None


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
  blockers: list[str]
  # Packages installed live in this container that the running image does not
  # carry (``pip``/``apt`` entries as ``name==version`` / ``name=version``).
  # A replacement drops them; the owner decides whether that matters.
  live_installs: dict[str, list[str]]


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
  # Proven semantic merge base for a ``conflict`` result: the equivalence
  # engine's tree that already eliminates changes proven to have landed
  # upstream. Callers that rewrite the conflict flag must carry it forward so
  # the resolver preserves every proven elimination.
  merge_base: str | None = None
  error: str | None = None
  # Exact reviewed release/upstream commit captured while RECONCILE_LOCK is
  # still held. Hook refresh reads every allowlisted blob from this immutable
  # generation rather than trusting a locally merged HEAD or a moving ref.
  hook_source_sha: str | None = None
  reconciliation: app_git.ReconciliationReceipt = field(
    default_factory=app_git.ReconciliationReceipt,
  )
  # How the overlay moved: replayed/dropped unit ids for an ``updated`` pass,
  # or the parked conflict (worktree, commit, unit, remaining) for ``conflict``.
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
    _restore_working_edits(repo, local)
    return f"boot_guard[reset] pre={_short(pre)}"
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
  """The parked replay that still belongs to the served tip, if any.

  A replay is parked against the committed tip the owner had (``served``);
  its candidate worktree must still exist for a resolver to finish it.
  """
  parked = (_read_conflict_flag() or {}).get("overlay")
  if not parked or not tip:
    return None
  worktree = Path(str(parked.get("worktree") or _overlay_candidate_path(repo)))
  if str(parked.get("served") or "") != tip or not (worktree / ".git").exists():
    return None
  return parked


def _activate_candidate(repo: Path, local: str, pre_sha: str, tip: str) -> None:
  """Move the served branch to a complete candidate, compare-and-swap.

  The update refuses if another writer moved the local branch after
  ``pre_sha``; only after that succeeds does the checked-out tree follow.
  """
  _git("update-ref", f"refs/heads/{local}", tip, pre_sha, repo=repo)
  _git("checkout", "-q", local, repo=repo, check=False)
  _git("reset", "--hard", tip, repo=repo)


def _commit_single_parent_tree(
  repo: Path, *, parent: str, tree_oid: str, message: str,
) -> str:
  """Record an off-tree merged tree as ONE overlay commit on ``parent``."""
  return app_git._run(
    repo, "commit-tree", tree_oid, "-p", parent, "-m", message,
  ).stdout.strip()


def _restore_working_edits(repo: Path, local: str) -> bool:
  """Return a transient working-tree overlay commit to uncommitted edits.

  Uncommitted edits are carried through a reconcile as a commit tagged with
  the ``working-tree`` unit so the replay can move them; once the served tree
  has settled (updated, rolled back, or conflicted) that commit is unwound so
  the owner's ``git status`` reads exactly as it did before the update.
  """
  head = _rev(repo, "HEAD")
  if not head or _reconcile_in_progress(repo):
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
  overlay: dict | None = None,
) -> None:
  """Persist a conflict so Settings keeps surfacing it across reloads.

  Line 0 is the target (``origin/main``) sha; optional ``chat:<id>``,
  ``base:<tree>`` and ``overlay:<json>`` lines record the resolver chat, a
  proven semantic merge base (legacy net-merge conflicts) and a parked overlay
  replay (worktree, conflicting commit, unit, remaining shas); the remaining
  lines are conflicting paths. Prefixes keep the format backward compatible
  with conflict flags written before any of these fields existed.
  """
  body = [target or ""]
  if chat_id:
    body.append(f"chat:{chat_id}")
  if merge_base:
    body.append(f"base:{merge_base}")
  if overlay:
    body.append("overlay:" + json.dumps(overlay, separators=(",", ":")))
  body.extend(paths)
  _atomic_write_text(CONFLICT_FLAG, "\n".join(body))


def _read_conflict_flag() -> dict | None:
  """Parse the conflict target, chat, semantic base, parked overlay replay,
  and paths, or return None.

  ``upstream`` is the target sha (named for backward compatibility with the
  status field, not the ``upstream`` branch)."""
  if not CONFLICT_FLAG.exists():
    return None
  lines = CONFLICT_FLAG.read_text().splitlines()
  target = lines[0].strip() if lines else ""
  chat_id: str | None = None
  merge_base: str | None = None
  overlay: dict | None = None
  paths: list[str] = []
  for line in lines[1:]:
    stripped = line.strip()
    if not stripped:
      continue
    if stripped.startswith("chat:"):
      chat_id = stripped[len("chat:"):] or None
    elif stripped.startswith("base:"):
      merge_base = stripped[len("base:"):] or None
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
    "merge_base": merge_base,
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
    for candidate in (_rev(repo, DEFAULT_TARGET_REF), recorded_upstream_sha(repo)):
      if candidate and _is_ancestor(repo, candidate, current):
        return candidate
    raise PlatformUpdateError("applied_release_unavailable")


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
    return {
      "version": 0,
      "target_sha": raw,
      "upstream_sha": None,
      "paths": ["backend/app"],
      "image_paths": [],
    }
  if not isinstance(parsed, dict):
    return None
  target = str(parsed.get("target_sha") or "").strip()
  paths = parsed.get("paths")
  if not isinstance(paths, list):
    return None
  clean_paths = sorted({str(path).strip() for path in paths if str(path).strip()})
  if parsed.get("version") == 2:
    upstream = str(parsed.get("upstream_sha") or "").strip() or None
    raw_image_paths = parsed.get("image_paths")
    if not isinstance(raw_image_paths, list):
      return None
    image_paths = sorted({
      str(path).strip() for path in raw_image_paths
      if str(path).strip() in clean_paths
    })
    return {
      "version": 2,
      "target_sha": target,
      "upstream_sha": upstream,
      "paths": clean_paths,
      "image_paths": image_paths,
    }
  # Schema 1 stored only target+paths, so it cannot prove an official image
  # contains a pending local runtime change. Fail closed until the next Apply
  # writes a schema-2 receipt with exact upstream coverage.
  return {
    "version": 1,
    "target_sha": target,
    "upstream_sha": None,
    "paths": clean_paths,
    "image_paths": [],
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

  Protected runtime (``backend/runtime``) is never a blocker here: the
  replacement controller's active-runtime overlay contract owns that case at
  the root boundary, carrying forward only bytes already executing from
  ``/app/runtime`` and never promoting newer editable source. Other image
  inputs have no equivalent active-generation receipt.
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

  image_pending: list[str] = []
  for path in sorted(set(pending)):
    if path == "backend/runtime" or path.startswith("backend/runtime/"):
      continue
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
    covered = exact_target_coverage | carried_marker_coverage
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
    paths = [*_pending_activation_paths(repo),
             *_activation_paths_between(repo, base, target_sha)]
    activation = platform_activation.classify_activation(paths)
    blockers = container_replacement_blockers(
      target_sha, repo, local_change_base=base,
    )
    return PlatformReviewedRebuild(
      target_sha=target_sha,
      image_digest=image_digest,
      local_base_sha=base,
      activation=activation,
      blockers=blockers,
      live_installs=live_install_drift(),
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
  paths.extend(served_to_head or [])
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
  # Successful in-place installs replace the image baseline only for the
  # exact dependency input bytes they installed. A later edit becomes pending
  # again, while restarting the server does not resurrect completed work.
  installed = _dependency_receipt()
  return sorted(
    path for path in set(baked) | set(current)
    if installed.get(path, baked.get(path)) != current.get(path)
  )


def _inventory_drift(
  baked_path: Path, current: list[str],
) -> list[str] | None:
  try:
    baked = set(
      line.strip() for line in baked_path.read_text(encoding="utf-8").splitlines()
    )
  except OSError:
    return None
  return sorted(
    line.strip() for line in current
    if line.strip() and line.strip() not in baked
  )


def live_install_drift(
  inventory_dir: Path | None = None,
  *,
  run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> dict[str, list[str]]:
  """Packages present in this container that its image did not install.

  Agents may ``pip``/``apt`` install into the running container; that is the
  cheap, restart-free path and it survives a process restart, but a container
  replacement rebuilds from the image and silently drops it. The image bakes
  its own ``pip freeze`` and ``dpkg-query`` inventories; anything the current
  container has beyond them is what a replacement would lose. An image
  without inventories reports nothing rather than guessing.
  """
  root = inventory_dir or Path(
    os.environ.get("MOBIUS_IMAGE_INVENTORY_DIR", "/app/image-inventory"),
  )
  commands = {
    "pip": [sys.executable or "python3", "-m", "pip", "freeze",
            "--disable-pip-version-check"],
    "apt": ["dpkg-query", "-W", "-f", "${Package}=${Version}\n"],
  }
  drift: dict[str, list[str]] = {}
  for kind, command in commands.items():
    baked_path = root / f"{kind}.txt"
    if not baked_path.is_file():
      continue
    try:
      proc = run(command, capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
      continue
    if proc.returncode != 0:
      continue
    extras = _inventory_drift(baked_path, proc.stdout.splitlines())
    if extras:
      drift[kind] = extras
  return drift


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
      platform_activation.ActivationLevel.DEPENDENCY_SYNC.value,
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


# pip can be slow on a small self-hosted box fetching wheels; keep the bound
# generous but finite so a wedged install still fails closed.
_DEP_SYNC_TIMEOUT = 900
_PYTHON_DEPENDENCY_INPUTS = ("backend/requirements.txt", "backend/requirements.lock")
_FRONTEND_DEPENDENCY_INPUTS = ("frontend/package.json", "frontend/package-lock.json")


def _dependency_receipt() -> dict[str, str]:
  try:
    receipt = json.loads(DEPENDENCY_RECEIPT_PATH.read_text())
  except (OSError, ValueError):
    return {}
  if not isinstance(receipt, dict):
    return {}
  allowed = {*_PYTHON_DEPENDENCY_INPUTS, *_FRONTEND_DEPENDENCY_INPUTS}
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


def _sync_python_dependencies(repo: Path) -> tuple[bool, str]:
  """Install the locked Python deps in place — the SAME command the image build
  runs — so an owner Apply lands a dependency bump without a container rebuild.

  Returns ``(ok, error_tail)`` and never raises for an operational failure.
  """
  lock = repo / "backend" / "requirements.lock"
  if not lock.is_file():
    return True, ""
  try:
    _record_dependency_inputs(repo, _PYTHON_DEPENDENCY_INPUTS, installed=False)
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
    return False, repr(exc)[-_ERROR_EXCERPT_CHARS:]
  if proc.returncode != 0:
    detail = (proc.stderr or proc.stdout or "pip install failed").strip()
    return False, detail[-_ERROR_EXCERPT_CHARS:]
  try:
    _record_dependency_inputs(repo, _PYTHON_DEPENDENCY_INPUTS, installed=True)
  except OSError as exc:
    return False, repr(exc)[-_ERROR_EXCERPT_CHARS:]
  return True, ""


def _sync_frontend_dependencies(repo: Path) -> tuple[bool, str]:
  """Install the locked frontend deps in place — the SAME command the image build
  runs (``npm ci --ignore-scripts``) — the frontend twin of
  :func:`_sync_python_dependencies`, run just before the frontend rebuild so
  the build sees the new ``node_modules``.

  Returns ``(ok, error_tail)`` and never raises for an operational failure.
  """
  frontend = repo / "frontend"
  if not (frontend / "package-lock.json").is_file():
    return True, ""
  try:
    _record_dependency_inputs(repo, _FRONTEND_DEPENDENCY_INPUTS, installed=False)
    proc = subprocess.run(
      ["npm", "ci", "--ignore-scripts"],
      cwd=str(frontend),
      capture_output=True,
      text=True,
      timeout=_DEP_SYNC_TIMEOUT,
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
  repo: Path, *, python_changed: bool, frontend_changed: bool,
) -> str:
  """Restore the previous source's declared versions after an in-place failure.

  This is not a container snapshot: pip may retain added packages. Never remove
  packages outside the old lock, since they may be the owner's live installs.
  Report a failed restoration durably rather than promising an intact runtime.
  """
  failures: list[str] = []
  for changed, lock, sync in (
    (python_changed, "backend/requirements.lock", _sync_python_dependencies),
    (frontend_changed, "frontend/package-lock.json", _sync_frontend_dependencies),
  ):
    if not changed:
      continue
    if not (repo / lock).is_file():
      failures.append(f"{lock}: previous lock unavailable")
      continue
    ok, error = sync(repo)
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
  python_changed: bool,
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
    _reset_hard_to(repo, _local_branch(repo), res.pre_sha)
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
    repo, python_changed=python_changed, frontend_changed=frontend_changed,
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

  # A replay parked for a resolver owns the candidate worktree until it is
  # finished or abandoned. A boot or a repeated Apply must not restart the
  # replay underneath the resolver's edits; Settings still shows that newer
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
  # as a transient overlay commit FIRST so neither the fast-forward reset nor
  # the replay can discard them; ``reconcile_clone`` unwinds it afterwards.
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
    # full-history fallback before we choose between reset and replay.
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
      # Main and target diverged: replay the local overlay onto the reviewed
      # upstream target so it stays exactly upstream + intentional local units.
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
    _reset_hard_to(repo, local, pre)
    # Nothing here is actionable by a resolver, and any earlier flag belonged
    # to an attempt this pass already superseded: leave the served tree at PRE
    # with no stale conflict/rollback state to mislead the next status read.
    app_git.remove_overlay_worktree(repo, _overlay_candidate_path(repo))
    CONFLICT_FLAG.unlink(missing_ok=True)
    ROLLED_BACK_FLAG.unlink(missing_ok=True)
    _clear_reconcile_pre()
    return ReconcileResult.unchanged("error", pre, target, error=repr(exc))


def _roll_back_update(
  repo: Path, local: str, pre: str, target: str, message: str, error: str,
  *, restore_python: bool = False,
) -> ReconcileResult:
  """Serve the previous source and restore dependencies changed by the attempt."""
  _reset_hard_to(repo, local, pre)
  restore_error = _restore_update_dependencies(
    repo, python_changed=restore_python, frontend_changed=False,
  )
  if restore_error:
    message += "\n" + restore_error
    error += "\n" + restore_error
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

  The one path that turns a candidate — a fast-forward target, a replayed
  overlay, a folded legacy history, or a resolver-finished replay — into the
  served tree: the declared dependency installs, the import probe, provenance
  bookkeeping, the upstream marker, flags, and the frontend rebuild. Every
  caller gets every gate, so a resolver-finished replay can never land with
  fewer checks than owner Apply. A rejected candidate rolls back to ``pre``
  (restoring the previous declared dependency versions) exactly like any
  other failed update.
  """
  _activate_candidate(repo, local, pre, tip)
  app_git.remove_overlay_worktree(repo, _overlay_candidate_path(repo))

  changed = _activation_paths_between(repo, pre, tip)
  python_changed = any(path in _PYTHON_DEPENDENCY_INPUTS for path in changed)
  frontend_changed = any(path in _FRONTEND_DEPENDENCY_INPUTS for path in changed)
  touched_frontend = any(path.startswith("frontend/") for path in changed)

  if python_changed:
    if progress:
      progress(PlatformUpdatePhase.BUILDING)
    deps_ok, deps_err = _sync_python_dependencies(repo)
    if not deps_ok:
      return _roll_back_update(
        repo, local, pre, target,
        f"dependency_install_failed: {deps_err}", deps_err,
        restore_python=True,
      )

  # Post-reconcile import probe: a text-clean replay can still produce a tree
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
        repo, local, pre, target, err, err, restore_python=python_changed,
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
      python_changed=python_changed, frontend_changed=frontend_changed,
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


def _overlay_summary(
  commits: list[app_git.OverlayCommit],
  replay: app_git.OverlayReplayResult,
) -> dict:
  unit_of = {commit.sha: commit.unit for commit in commits}
  replayed_units = list(dict.fromkeys(
    unit_of[old] for old, _new in replay.replayed
  ))
  return {
    "replayed_units": replayed_units,
    "dropped_units": list(dict.fromkeys(
      unit_of[old] for old in replay.dropped
      if unit_of[old] not in replayed_units
    )),
    "dropped_commits": list(replay.dropped),
  }


def _park_replay(
  repo: Path,
  carried: _Carried,
  target: str,
  ordinary_base: str,
  reconciliation: app_git.ReconciliationReceipt,
  worktree: Path,
  replay: app_git.OverlayReplayResult,
  skip: set[str],
) -> ReconcileResult:
  """Leave a conflicting replay in its worktree for the resolver.

  The parked cherry-pick is the actionable conflict; the complete net conflict
  set tells the resolver what else lies behind it. The transient working-tree
  commit is never listed as remaining work: the owner's edits go back to the
  working tree now and are carried afresh when the replay continues.
  """
  pre = carried.pre
  try:
    net_paths = set(app_git.merge_refs(repo, pre, target).conflict_paths)
  except (OSError, subprocess.SubprocessError, RuntimeError):
    net_paths = set()
  paths = sorted(net_paths | set(replay.conflict["paths"]))
  parked = {
    **replay.conflict,
    "remaining": [
      sha for sha in replay.conflict.get("remaining", [])
      if sha != carried.working
    ],
    "worktree": str(worktree),
    "served": carried.served,
    "pre": pre,
    "working": carried.working,
    "target": target,
    "candidate": replay.tip,
    "skip": sorted(skip),
  }
  _write_conflict_flag(target, paths, overlay=parked)
  ROLLED_BACK_FLAG.unlink(missing_ok=True)
  _clear_reconcile_pre()
  return ReconcileResult.unchanged(
    "conflict", pre, target,
    conflict_paths=paths,
    overlay=parked,
    reconciliation=app_git.describe_reconciliation(
      repo, ordinary_base, target, local=pre, conflict_paths=paths,
    ),
  )


def _apply_overlay(
  repo: Path,
  carried: _Carried,
  target: str,
  ordinary_base: str,
  reconciliation: app_git.ReconciliationReceipt,
) -> ReconcileResult | _Candidate:
  """Build the candidate that carries the local overlay onto ``target``.

  A linear overlay is replayed commit by commit in the candidate worktree,
  dropping every commit already proven upstream. No ref of the served checkout
  moves here; :func:`_finalize_update` activates a complete candidate. A
  conflict parks the worktree and returns the conflict result; a merge-shaped
  history is folded into one legacy unit.
  """
  pre = carried.pre
  try:
    commits = app_git.overlay_commits(repo, target, pre)
  except app_git.OverlayNotLinear:
    return _fold_legacy_overlay(
      repo, carried, target, ordinary_base, reconciliation,
    )
  equivalent = app_git.merge_with_equivalent_changes(repo, pre, target)
  if equivalent is not None:
    reconciliation = equivalent.reconciliation
  skip = app_git.landed_overlay_commits(repo, commits, target)
  worktree = _overlay_candidate_path(repo)
  replay = app_git.replay_overlay(
    repo, commits=commits, onto=target, worktree=worktree, skip=skip,
  )
  if replay.status == "conflict":
    return _park_replay(
      repo, carried, target, ordinary_base, reconciliation, worktree,
      replay, skip,
    )
  return _Candidate(replay.tip, reconciliation, _overlay_summary(commits, replay))


def _fold_legacy_overlay(
  repo: Path,
  carried: _Carried,
  target: str,
  ordinary_base: str,
  reconciliation: app_git.ReconciliationReceipt,
) -> ReconcileResult | _Candidate:
  """Fold a merge-shaped local history into one linear overlay unit.

  The net COMMITTED local tree is merged with the target off-tree ONCE,
  exactly as the merge model did, but the result is recorded as a single
  commit on the target instead of a two-parent merge, so the next update sees
  a linear overlay. Uncommitted edits stay their own transient unit, replayed
  on top of the fold, so they return to the working tree afterwards instead
  of being frozen into the legacy commit. Reviewed-change provenance still
  turns a squash-landed contribution into a clean result; a genuine conflict
  goes to the existing net-merge resolver whose finalize already writes a
  single-parent replay.
  """
  served = carried.served
  message = app_git.overlay_message(
    f"platform: local overlay carried onto {target[:12]}",
    unit=app_git.OVERLAY_LEGACY_UNIT,
    disposition="wip",
    body=(
      "Folded a merge-shaped local history into one linear overlay unit so "
      "later updates replay it instead of merging upstream into it."
    ),
  )
  merged = app_git.merge_refs(repo, served, target)
  tree_oid = merged.merged_tree_oid if merged.status == "clean" else None
  equivalent = None
  if tree_oid is None:
    # Before asking the owner to resolve a content conflict, let the shared
    # app/platform provenance engine replace Git's historical base with a
    # semantic base made ONLY from reviewed changes proven to come from this
    # local history and to have landed in this target history (the squash case).
    equivalent = app_git.merge_with_equivalent_changes(repo, served, target)
    if equivalent is not None:
      reconciliation = equivalent.reconciliation
      tree_oid = equivalent.merged_tree_oid
  if tree_oid:
    folded = _commit_single_parent_tree(
      repo, parent=target, tree_oid=tree_oid, message=message,
    )
    summary = {
      "legacy": True,
      "replayed_units": [app_git.OVERLAY_LEGACY_UNIT],
      "dropped_units": [],
      "dropped_commits": [],
    }
    if not carried.working:
      return _Candidate(folded, reconciliation, summary)
    worktree = _overlay_candidate_path(repo)
    working = app_git.overlay_commits(repo, served, carried.pre)
    replay = app_git.replay_overlay(
      repo, commits=working, onto=folded, worktree=worktree,
    )
    if replay.status == "conflict":
      return _park_replay(
        repo, carried, target, ordinary_base, reconciliation, worktree,
        replay, set(),
      )
    return _Candidate(replay.tip, reconciliation, summary)
  conflict_paths = (
    equivalent.conflict_paths
    if equivalent is not None and equivalent.conflict_paths
    else merged.conflict_paths
  )
  merge_base = equivalent.merge_base_oid if equivalent is not None else None
  if merge_base:
    _git("update-ref", _CONFLICT_MERGE_BASE_REF, merge_base, repo=repo)
  _write_conflict_flag(target, conflict_paths, merge_base=merge_base)
  ROLLED_BACK_FLAG.unlink(missing_ok=True)
  _clear_reconcile_pre()
  pre = carried.pre
  return ReconcileResult.unchanged(
    "conflict", pre, target,
    conflict_paths=conflict_paths,
    merge_base=merge_base,
    reconciliation=(
      equivalent.reconciliation
      if equivalent is not None
      else app_git.describe_reconciliation(
        repo, ordinary_base, target, local=pre,
        conflict_paths=conflict_paths,
      )
    ),
  )


def continue_platform_overlay_update(repo: Path = PLATFORM_REPO) -> str:
  """Finish a parked overlay replay after the resolver edited its worktree.

  Returns ``updated`` once the served checkout moved to the completed
  candidate, ``conflict`` when a later commit conflicted (the flag now names
  it), or ``rolled_back`` when the finished tree failed a post-replay gate.
  Runs the same finalize path as an ordinary owner Apply, including the
  dependency sync, import probe, and frontend build.
  """
  with _reconcile_flock():
    flag = _read_conflict_flag() or {}
    parked = flag.get("overlay")
    if not parked:
      raise PlatformUpdateError("No parked platform overlay replay.")
    local = _local_branch(repo)
    served = str(parked.get("served") or "")
    target = str(parked.get("target") or "")
    worktree = Path(str(parked.get("worktree") or _overlay_candidate_path(repo)))
    if not served or not target or _rev(repo, local) != served:
      raise PlatformUpdateError(
        "The served branch moved since the replay was parked; run the update "
        "again instead of continuing this one."
      )
    # The owner's current edits ride along exactly as they would on a fresh
    # update; they are unwound again below whatever the outcome.
    _reattach_detached_head(repo, local)
    carried = _carry_working_edits(repo, local)
    try:
      replay = app_git.continue_overlay_replay(
        repo,
        worktree=worktree,
        commit_sha=str(parked.get("sha") or ""),
        remaining=list(parked.get("remaining") or []),
        skip=list(parked.get("skip") or []),
      )
      if replay is None:
        raise PlatformUpdateError(
          "Unmerged files or conflict markers remain in the candidate worktree."
        )
      if replay.status == "clean" and carried.working:
        # Carry the current edits onto the candidate unless the resolved pick
        # WAS those edits and they have not changed since it was parked.
        consumed = (
          parked.get("sha") == parked.get("working")
          and bool(parked.get("working"))
          and app_git.ref_trees_equal(repo, carried.working, parked["working"])
        )
        if not consumed:
          replay = app_git.replay_more(
            repo,
            worktree=worktree,
            commits=app_git.overlay_commits(repo, carried.served, carried.pre),
            previous=replay,
          )
      if replay.status == "conflict":
        again = {
          **parked, **replay.conflict, "candidate": replay.tip,
          "pre": carried.pre, "working": carried.working,
          "remaining": [
            sha for sha in replay.conflict.get("remaining", [])
            if sha != carried.working
          ],
        }
        _write_conflict_flag(
          target,
          sorted(set(flag.get("paths") or []) | set(replay.conflict["paths"])),
          flag.get("chat_id"),
          overlay=again,
        )
        return "conflict"
      _write_reconcile_pre(carried.pre)
      result = _finalize_update(
        repo, local, pre=carried.pre, tip=replay.tip, target=target,
        progress=None,
        reconciliation=app_git.ReconciliationReceipt(),
        overlay={"continued": True, "dropped_commits": list(replay.dropped)},
      )
      return result.status
    finally:
      _restore_working_edits(repo, local)


def abandon_platform_overlay_update(repo: Path = PLATFORM_REPO) -> str:
  """Drop a parked overlay replay and keep serving the pre-update tree."""
  with _reconcile_flock():
    flag = _read_conflict_flag() or {}
    parked = flag.get("overlay") or {}
    worktree = Path(str(parked.get("worktree") or _overlay_candidate_path(repo)))
    app_git.remove_overlay_worktree(repo, worktree)
    CONFLICT_FLAG.unlink(missing_ok=True)
    _restore_working_edits(repo, _local_branch(repo))
    return "abandoned"


def _reconcile_under_lock(
  repo: Path,
  *,
  target_ref: str = DEFAULT_TARGET_REF,
  plan_id: str | None = None,
  current_sha: str | None = None,
  image_digest: str | None = None,
  progress: Callable[[PlatformUpdatePhase], None] | None = None,
) -> ReconcileResult:
  """Serialize source updates with startup recovery using RECONCILE_LOCK.

  Owner Apply additionally validates its immutable review plan under the same
  lock. The lock covers source, dependency installation, the frontend build
  and rollback; browser progress reads use the durable phase record.
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
        image_digest=image_digest,
      )
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
  activation = _platform_activation_impact(repo)
  activation_state = _state_for_activation(activation)
  restart_needed = activation["level"] in {
    platform_activation.ActivationLevel.SERVER_RESTART.value,
    platform_activation.ActivationLevel.DEPENDENCY_SYNC.value,
  }
  target = _rev(repo, target_sha or DEFAULT_TARGET_REF)
  if not target and not target_sha:
    raise PlatformUpdateError("platform_target_unavailable")
  target_contained = bool(target) and _is_ancestor(repo, target, local)
  contained_upstream_sha = target if target_contained else (
    upstream_sha if upstream_sha and _is_ancestor(repo, upstream_sha, local) else None
  )
  contained_upstream_committed_at = _commit_timestamp(
    repo, contained_upstream_sha,
  )
  upstream_checked_at = _last_fetch_timestamp(repo)

  if conflict:
    flag = _read_conflict_flag() or {}
    paths = flag.get("paths") or _unmerged_paths(repo)
    # `target` is the last-fetched origin/main. If it strictly descends the
    # version this conflict is pinned to, newer releases stacked up behind the
    # one being resolved — Settings can then offer one combined review+resolve.
    conflict_target = flag.get("upstream")
    newer_available = bool(
      target and conflict_target and target != conflict_target
      and _is_ancestor(repo, conflict_target, target)
    )
    return PlatformStatus(
      state=PlatformUpdateState.CONFLICT.value, available=False,
      needs_restart=restart_needed, activation=activation,
      current_build_sha=image_sha,
      recorded_upstream_sha=upstream_sha,
      contained_upstream_sha=contained_upstream_sha,
      contained_upstream_committed_at=contained_upstream_committed_at,
      upstream_checked_at=upstream_checked_at,
      seed_required=False,
      conflict_paths=paths, conflict_chat_id=flag.get("chat_id"),
      newer_updates_available=newer_available,
      rollback_target_sha=None, rollback_error=None,
      overlay=_overlay_status(repo, local, contained_upstream_sha),
    )

  # A freshly published GHCR revision may not yet be in this clone's object
  # store. Its immutable SHA is still authoritative evidence that a different
  # release exists; the preview/check path fetches and proves the source object
  # before it creates an actionable plan.
  available = bool(target_sha or target) and not target_contained

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
    contained_upstream_committed_at=contained_upstream_committed_at,
    upstream_checked_at=upstream_checked_at,
    seed_required=False, conflict_paths=[], conflict_chat_id=None,
    newer_updates_available=False,
    rollback_target_sha=(rollback or {}).get("target"),
    rollback_error=(rollback or {}).get("error"),
    overlay=_overlay_status(repo, local, contained_upstream_sha),
  )


def _overlay_status(
  repo: Path, local: str, base: str | None,
) -> dict | None:
  """The overlay invariant for Settings; never raises."""
  if not base:
    return None
  try:
    return app_git.describe_overlay(repo, base, local)
  except Exception:
    log.warning("platform: could not describe the local overlay", exc_info=True)
    return None


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
    target = _rev(repo, DEFAULT_TARGET_REF)
    local = _local_branch(repo)
    if target and _is_ancestor(repo, target, local):
      _set_upstream(repo, target)
  return platform_status(repo, target_sha=target_sha)


def empty_platform_update_preview(
  *, current_sha: str | None = None, target_sha: str | None = None,
  image_digest: str | None = None,
) -> PlatformUpdatePreview:
  """A verified preview carrying no incoming changes or activation work."""
  return PlatformUpdatePreview(
    state=PlatformUpdateState.UP_TO_DATE.value, available=False,
    actionable=False, operation="none",
    current_sha=current_sha, target_sha=target_sha, plan_id=None,
    image_digest=image_digest,
    activation=platform_activation.classify_activation([]),
    total_commits=0, commits_truncated=False,
    commits=[], files=[], diff=None, diff_truncated=False, conflict_paths=[], blocking_paths=[],
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
  base — local edits are excluded, so the owner reviews exactly what a clean Apply
  would pull. Availability is the same ancestry check :func:`platform_status`
  uses; an already-applied target can still have actionable activation work.
  Missing source or target provenance is an explicit error on both deployments;
  "unavailable" must never masquerade as "up to date."""
  # A missing clone has no snapshot to lock. Fail explicitly without requiring
  # a writable /data recovery surface; existing clones are checked under lock.
  if not (repo / ".git").exists():
    raise PlatformUpdateError("platform_repo_missing")
  with _reconcile_flock():
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
  target = _rev(repo, target_sha or DEFAULT_TARGET_REF) or None
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
    return PlatformUpdatePreview(
      state=PlatformUpdateState.AVAILABLE.value, available=True,
      actionable=True, operation="update",
      current_sha=local_sha, target_sha=target,
      plan_id=(
        _update_plan_id(local_sha, target, image_digest) if local_sha else None
      ),
      image_digest=image_digest,
      activation=platform_activation.classify_activation(["backend/app"]),
      total_commits=0, commits_truncated=False, commits=[], files=[],
      diff=None, diff_truncated=False, conflict_paths=[], blocking_paths=[],
    )
  diff, truncated = _preview_diff(repo, base, target)
  commits = _preview_commits(repo, base, target)
  total_commits = _preview_commit_count(repo, base, target)
  conflict = _read_conflict_flag() or {}
  activation_paths = [*_pending_activation_paths(repo),
                      *_activation_paths_between(repo, base, target)]
  return PlatformUpdatePreview(
    state=PlatformUpdateState.AVAILABLE.value, available=True,
    actionable=True, operation="update",
    current_sha=local_sha, target_sha=target,
    plan_id=(
      _update_plan_id(local_sha, target, image_digest) if local_sha else None
    ),
    image_digest=image_digest,
    activation=platform_activation.classify_activation(activation_paths),
    total_commits=total_commits,
    commits_truncated=total_commits > len(commits),
    commits=commits,
    files=_preview_files(repo, base, target),
    diff=diff, diff_truncated=truncated,
    conflict_paths=conflict.get("paths") or [], blocking_paths=[],
  )


async def apply_platform_update(
  db: Session,
  *,
  plan_id: str,
  current_sha: str,
  target_sha: str,
  image_digest: str | None = None,
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
        target_ref=target_sha,
        plan_id=plan_id,
        current_sha=current_sha,
        image_digest=image_digest,
        progress=publish_progress,
      )
      chat_id: str | None = None

      def record_current_activation(head: str | None) -> PlatformActivationImpact:
        served = _served_platform_sha()
        changed_paths = _activation_paths_between(repo, served, head)
        incoming_impact = platform_activation.classify_activation(changed_paths)
        if incoming_impact["level"] != platform_activation.ActivationLevel.LIVE.value:
          mark_activation_needed(
            head or "",
            changed_paths,
            upstream_sha=res.target_sha,
            repo=repo,
          )
        return _platform_activation_impact(repo, served_to_head=changed_paths)

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
        # chat only when it belongs to this same target — and carry the proven
        # semantic merge base forward so the resolver keeps every elimination
        # the equivalence engine already proved (falling back to a same-target
        # base recorded by an earlier pass).
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
          merge_base=res.merge_base or (
            existing_conflict.get("merge_base")
            if target and existing_conflict.get("upstream") == target
            else None
          ),
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
      _set_update_progress(
        PlatformUpdatePhase.FAILED,
        plan_id=plan_id,
        target_sha=target_sha,
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
  merge_base = flag.get("merge_base")
  parked = flag.get("overlay")
  if not target_sha:
    raise PlatformUpdateError("Platform conflict target is unavailable.")
  result = await spawn_platform_conflict_chat(
    db, conflict_paths, target_sha, merge_base, parked,
  )
  if result is None:
    raise PlatformUpdateError("Could not open resolver chat.")

  _write_conflict_flag(
    target_sha,
    conflict_paths,
    result["chat_id"],
    merge_base,
    overlay=parked,
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
  overlay: dict | None = None,
) -> str:
  """Instructions bound to the exact release the owner reviewed and applied."""
  files = ", ".join(conflict_paths) if conflict_paths else "some files"
  if overlay:
    parked_files = ", ".join(overlay.get("paths") or []) or files
    remaining = len(overlay.get("remaining") or [])
    return (
      "A platform update is ready but one of this installation's local "
      "changes conflicts with it — the new version and that local change "
      "both touched the same lines.\n\n"
      "The updater keeps local work as a linear overlay on top of the exact "
      f"upstream version. It replayed that overlay onto `{target_sha}` in a "
      f"separate candidate worktree at `{overlay.get('worktree')}` and stopped "
      f"at local commit `{overlay.get('sha')}` (\"{overlay.get('subject')}\", "
      f"unit `{overlay.get('unit')}`) with conflicts in: {parked_files}. "
      f"Every conflicting file behind it, if any, is: {files}.\n\n"
      "Resolve IN THAT WORKTREE, not in `/data/platform` (which keeps serving "
      "the old code untouched): edit each conflicting file to combine the "
      "intent of the local change and upstream's, remove the conflict "
      f"markers, and `git -C {overlay.get('worktree')} add` it. Then finish "
      "the update non-interactively: `cd /data/platform/backend && python3 -c "
      "\"from app.platform_update import continue_platform_overlay_update as "
      "c; print(c())\"`. That commits the resolved change under its original "
      f"message, replays the remaining {remaining} local commit(s), and moves "
      "the served checkout to the finished result; if a later commit "
      "conflicts it stops again and this flag names the new stop. When it "
      "prints `updated`, tell the owner to **restart the server** from "
      "Settings to finish. To skip this update instead, run `python3 -c "
      "\"from app.platform_update import abandon_platform_overlay_update as "
      "a; print(a())\"` from the same directory and tell the owner the "
      "update was skipped."
    )
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
  provider = providers.owner_default_provider(
    data_dir, owner.provider,
  )
  agent_settings = providers.snapshot_chat_agent_settings(
    data_dir,
    provider,
    fallback_model=providers.DEFAULT_MODELS.get(provider),
  )

  content = _platform_conflict_resolver_message(
    target_sha, conflict_paths, merge_base, overlay,
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
