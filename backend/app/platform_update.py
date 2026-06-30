"""Platform self-update — a thin ``/data/platform`` caller of ``app.app_git``.

The platform repo at ``/data/platform`` updates with the SAME engine every
installed app uses: ``app_git`` records each baked image floor on an
``upstream`` branch, merges it into the local working branch with
``git merge-tree --write-tree`` (a clean-vs-conflict verdict computed off the
live worktree, so a conflict never leaves the served backend half-merged), and
finalises a clean apply as a single-parent linear replay. This module owns only
the platform-specific lifecycle around that engine: which baked floor to record,
the gitignore migration that takes the recovery files out of git entirely, the
restart/conflict flags, and the resolver chat.

Two facts shape every operation here:

1. ``/data/platform`` holds the SERVED backend, so a half-applied merge must be
   impossible. The engine's ``merge_upstream`` computes the verdict off the live
   worktree; only a proven-clean merge writes files, and ``write_merged_tree_to_
   worktree`` writes them one-by-one via ``os.replace`` so a crash leaves whole
   files, never truncated ones.

2. The recovery island + core files (``protected-files.txt``: ``main.py``,
   ``auth.py``, the ``recover_*`` modules, ``entrypoint.sh`` …) are root-owned
   ``chmod 444`` in the live tree. The ``mobius`` user that runs this engine
   CANNOT and MUST NOT overwrite them. Rather than special-case them in the merge
   path, they leave the git model entirely: ``/data/platform/.gitignore`` lists
   them, a one-time migration untracks them, and ``record_baked_upstream`` never
   records them on ``upstream``. So they simply never appear in any merged tree —
   they update only via the image (deploy / root entrypoint), never in-product,
   and the merge code is the same gitignore-respecting engine an app uses.

A conflict (local edits and the new baked floor changed the same lines — only
possible in gitignored-out recovery files never conflict) does not get
materialised programmatically. The engine records the new ``upstream`` and the
async wrapper spawns an agent resolver chat to merge it, mirroring how a per-app
conflict is handed to the agent. Nothing restarts on its own: a clean apply
marks "restart needed" and the owner confirms.

Availability is an EXACT ancestry check, not a sha-string compare: an update is
available iff ``upstream`` is NOT already an ancestor of the working branch (the
same ``git merge-base --is-ancestor`` model ``app_git`` uses). A baked sha that
merely differs from the recorded one no longer falsely reads "available".
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, TypedDict

from sqlalchemy.orm import Session

from app import app_git


PLATFORM_REPO = Path("/data/platform")
PLATFORM_APP = PLATFORM_REPO / "app"
PLATFORM_SCRIPTS = PLATFORM_REPO / "scripts"
BAKED_APP = Path("/app/app-baked")
BAKED_SCRIPTS = Path("/app/scripts-baked")
# The recovery / core files (root-owned chmod 444 in the live tree). These are
# gitignored out of the platform repo, so the merge engine never has to special-
# case them; the image owns them.
PROTECTED_LIST = Path("/app/protected-files.txt")
UPGRADE_FLAG = Path("/data/.platform-upgrade-available")
RESTART_NEEDED_FLAG = Path("/data/.platform-restart-needed")
# Persist a conflict so Settings keeps showing it across reloads (the merge is
# NOT materialised on disk, so MERGE_HEAD alone can't signal it). Records the
# conflicting upstream sha + paths.
CONFLICT_FLAG = Path("/data/.platform-conflict")
# Set just before the clean write-back, cleared after the merge commit. If a
# later apply finds it, a previous apply crashed mid-write — reset to the last
# good `main` first. (A crash that leaves /data/platform non-bootable is also
# caught by the entrypoint crash-loop -> restore-from-baked backstop.)
APPLYING_FLAG = Path("/data/.platform-apply-in-progress")
UPSTREAM_BRANCH = "upstream"
LOCAL_BRANCH = "main"

# The platform tree is larger than an app, but still small; a git op slower
# than this is wedged, not busy.
_GIT_TIMEOUT = 120

# Serialise applies in-process (uvicorn is single-worker; this is belt-and-braces
# against a double-click racing two merges on the same repo).
_APPLY_LOCK = asyncio.Lock()


class PlatformUpdateError(RuntimeError):
  """A platform update could not proceed (carries a short machine code)."""


class PlatformUpdateState(str, Enum):
  """User-visible state for the platform updater."""

  UP_TO_DATE = "up_to_date"
  AVAILABLE = "available"
  CONFLICT = "conflict"
  RESTART_NEEDED = "restart_needed"


class PlatformStatus(TypedDict):
  """Response shape for ``GET /api/platform/status``."""

  state: str
  available: bool
  needs_restart: bool
  current_build_sha: str | None
  recorded_upstream_sha: str | None
  seed_required: bool
  conflict_paths: list[str]
  # The resolver chat opened for an in-progress conflict, so Settings can link
  # the owner straight to it (a conflict row that names a chat but can't reach
  # it is a dead end). None unless ``state == "conflict"`` AND the id was
  # recorded; a conflict flag written before this field existed reads back None.
  conflict_chat_id: str | None


class PlatformApplyResult(TypedDict):
  """Response shape for ``POST /api/platform/apply``."""

  state: str
  needs_restart: bool
  upstream_commit: str | None
  merge_commit: str | None
  conflict_paths: list[str]
  chat_id: str | None


class PlatformRestartResponse(TypedDict):
  """Response shape for ``POST /api/platform/restart``."""

  status: Literal["restarting"]


@dataclass(frozen=True)
class BakedFloor:
  """The baked platform tree available in the current image."""

  build_sha: str
  app_dir: Path = BAKED_APP
  scripts_dir: Path = BAKED_SCRIPTS


def _write_conflict_flag(
  upstream: str | None, paths: list[str], chat_id: str | None = None
) -> None:
  """Persist a conflict so Settings keeps surfacing it across reloads.

  Line 0 is the conflicting upstream sha; an optional ``chat:<id>`` line records
  the resolver chat; the remaining lines are the conflicting paths. The
  ``chat:`` prefix keeps the format backward compatible — a flag written before
  the chat id was recorded simply lacks that line and reads back as no chat id.
  """
  body = [upstream or ""]
  if chat_id:
    body.append(f"chat:{chat_id}")
  body.extend(paths)
  CONFLICT_FLAG.write_text("\n".join(body))


def _read_conflict_flag() -> dict | None:
  """Parse :data:`CONFLICT_FLAG` into ``{upstream, chat_id, paths}`` or None."""
  if not CONFLICT_FLAG.exists():
    return None
  lines = CONFLICT_FLAG.read_text().splitlines()
  upstream = lines[0].strip() if lines else ""
  chat_id: str | None = None
  paths: list[str] = []
  for line in lines[1:]:
    stripped = line.strip()
    if not stripped:
      continue
    if stripped.startswith("chat:"):
      chat_id = stripped[len("chat:"):] or None
    else:
      paths.append(stripped)
  return {"upstream": upstream or None, "chat_id": chat_id, "paths": paths}


def _git(*args: str, repo: Path = PLATFORM_REPO, check: bool = True):
  """Run a git command in the platform repo, text mode, with the search ceiling
  pinned to the repo's parent so it can never walk up into the enclosing
  ``/data`` repo and operate on the wrong tree."""
  env = {
    **os.environ,
    "GIT_CEILING_DIRECTORIES": str(Path(repo).resolve().parent),
  }
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=check, env=env,
  )


def _has_branch(name: str, repo: Path = PLATFORM_REPO) -> bool:
  return _git(
    "rev-parse", "--verify", "--quiet", f"refs/heads/{name}",
    repo=repo, check=False,
  ).returncode == 0


def _local_branch(repo: Path = PLATFORM_REPO) -> str:
  """The repo's actual working branch. ``git init`` defaults to ``master`` on
  some git versions and ``main`` on others, and ``entrypoint.sh`` inits
  ``/data/platform`` WITHOUT ``-b`` — so detect the branch rather than assume
  one (the live prod repo is on ``master``)."""
  name = _git(
    "rev-parse", "--abbrev-ref", "HEAD", repo=repo, check=False,
  ).stdout.strip()
  return name if name and name != "HEAD" else LOCAL_BRANCH


def _unmerged_paths(repo: Path = PLATFORM_REPO) -> list[str]:
  out = _git("diff", "--name-only", "--diff-filter=U", repo=repo, check=False)
  return [p.strip() for p in out.stdout.splitlines() if p.strip()]


def current_build_sha() -> str | None:
  """The current image's build SHA: the ``BUILD_SHA`` baked into the image,
  falling back to the env var, then to the SHA recorded in the upgrade flag."""
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
  if UPGRADE_FLAG.exists():
    parts = UPGRADE_FLAG.read_text().split()
    if len(parts) >= 2 and parts[0] == "upgrade-available":
      return parts[1].strip() or None
  return None


def _seed_baked_tag(repo: Path = PLATFORM_REPO) -> str | None:
  """The newest ``baked-<sha>`` tag that is an ANCESTOR of the local branch —
  the baked tree the live platform descends from, i.e. the correct 3-way merge
  base. On a once-upgraded instance ``.baked-sha`` already points at the new
  image, so the ancestor tag (not that file) is the reliable seed."""
  local = _local_branch(repo)
  out = _git("tag", "--list", "baked-*", repo=repo, check=False)
  best: tuple[int, str] | None = None
  for tag in (t.strip() for t in out.stdout.splitlines() if t.strip()):
    anc = _git(
      "merge-base", "--is-ancestor", tag, local, repo=repo, check=False,
    )
    if anc.returncode != 0:
      continue
    ts = _git("log", "-1", "--format=%ct", tag, repo=repo, check=False).stdout.strip()
    key = int(ts) if ts.isdigit() else 0
    if best is None or key > best[0]:
      best = (key, tag)
  return best[1] if best else None


def _seed_point(repo: Path = PLATFORM_REPO) -> str | None:
  """The commit to seed ``upstream`` at: the ancestor baked tag if one exists,
  else the repo's ROOT commit — which is the ``init: platform layer from baked
  image floor`` commit, i.e. the original baked tree, so it is a sound merge
  base even when no ``baked-<sha>`` tag was ever written (e.g. BUILD_SHA was
  unknown at init)."""
  tag = _seed_baked_tag(repo)
  if tag:
    return tag
  local = _local_branch(repo)
  roots = _git(
    "rev-list", "--max-parents=0", local, repo=repo, check=False,
  ).stdout.split()
  return roots[-1] if roots else None


def recorded_upstream_build_sha(repo: Path = PLATFORM_REPO) -> str | None:
  """The build SHA the platform code currently descends from. Prefer the
  ``baked-<sha>`` tag at the ``upstream`` tip; fall back to the ancestor seed
  tag, then ``/data/platform/.baked-sha``."""
  if _has_branch(UPSTREAM_BRANCH, repo):
    out = _git(
      "tag", "--points-at", UPSTREAM_BRANCH, "--list", "baked-*",
      repo=repo, check=False,
    )
    tags = [t.strip()[len("baked-"):] for t in out.stdout.splitlines() if t.strip()]
    if tags:
      return tags[-1]
  tag = _seed_baked_tag(repo)
  if tag:
    return tag[len("baked-"):]
  f = repo / ".baked-sha"
  if f.exists():
    return (f.read_text().strip() or None)
  return None


def platform_status(repo: Path = PLATFORM_REPO) -> PlatformStatus:
  """Compute update availability on demand (no daemon, no polling).

  Availability is an EXACT ancestry check: when ``upstream`` exists, an update
  is available iff ``upstream`` is NOT an ancestor of the working branch (the
  baked floor carries commits the working branch has not replayed). When no
  ``upstream`` branch exists yet, this instance predates the feature and a baked
  floor newer than what the code descends from is available pending a one-time
  seed. The old ``image_sha != upstream_sha`` string compare is gone — a baked
  sha that merely differs no longer falsely reads "available" (the phantom).
  """
  image_sha = current_build_sha()
  upstream_sha = recorded_upstream_build_sha(repo)
  conflict = CONFLICT_FLAG.exists() or (repo / ".git" / "MERGE_HEAD").exists()
  restart_needed = RESTART_NEEDED_FLAG.exists()
  has_upstream = _has_branch(UPSTREAM_BRANCH, repo)

  if conflict:
    flag = _read_conflict_flag() or {}
    paths = flag.get("paths") or _unmerged_paths(repo)
    return PlatformStatus(
      state=PlatformUpdateState.CONFLICT.value, available=False,
      needs_restart=restart_needed, current_build_sha=image_sha,
      recorded_upstream_sha=upstream_sha, seed_required=False,
      conflict_paths=paths, conflict_chat_id=flag.get("chat_id"),
    )

  if has_upstream:
    # Available iff `upstream` is NOT already an ancestor of the working branch.
    anc = _git(
      "merge-base", "--is-ancestor", UPSTREAM_BRANCH, _local_branch(repo),
      repo=repo, check=False,
    )
    available = anc.returncode != 0
    seed_required = False
  else:
    # No upstream branch yet (pre-feature instance). A baked floor newer than
    # what the code descends from is available, gated on the one-time seed.
    available = bool(image_sha and upstream_sha and image_sha != upstream_sha)
    seed_required = available

  if restart_needed:
    state = PlatformUpdateState.RESTART_NEEDED
  elif available:
    state = PlatformUpdateState.AVAILABLE
  else:
    state = PlatformUpdateState.UP_TO_DATE

  return PlatformStatus(
    state=state.value, available=available, needs_restart=restart_needed,
    current_build_sha=image_sha, recorded_upstream_sha=upstream_sha,
    seed_required=seed_required, conflict_paths=[],
    conflict_chat_id=None,
  )


def seed_upstream_if_missing(repo: Path = PLATFORM_REPO) -> bool:
  """Create the ``upstream`` branch from the ancestor ``baked-<sha>`` tag on
  instances that predate this feature (they have only ``main`` + that tag).
  Returns True if it created the branch. Fails closed when no ancestor baked
  tag exists — recovery / deploy still restore from the image floor."""
  if _has_branch(UPSTREAM_BRANCH, repo):
    return False
  point = _seed_point(repo)
  if not point:
    raise PlatformUpdateError("seed_point_unavailable")
  _git("branch", UPSTREAM_BRANCH, point, repo=repo)
  anc = _git(
    "merge-base", "--is-ancestor", UPSTREAM_BRANCH, _local_branch(repo),
    repo=repo, check=False,
  )
  if anc.returncode != 0:
    _git("branch", "-D", UPSTREAM_BRANCH, repo=repo, check=False)
    raise PlatformUpdateError("seed_not_ancestor")
  return True


def _normalize_working_branch(repo: Path = PLATFORM_REPO) -> bool:
  """Rename the platform working branch to ``main`` if it isn't already.

  ``entrypoint.sh`` inits ``/data/platform`` with a bare ``git init`` (no ``-b``),
  so the working branch is whatever the container's git defaults to — ``master``
  on prod. The ``app_git`` engine operates on the literal branch name ``main``
  (``LOCAL_BRANCH``), so the platform repo's branch must be ``main`` before any
  engine call. Returns True if it renamed. A detached HEAD (no branch) is left
  alone — there is nothing to rename, and the engine paths that follow create
  ``main`` from the seed point instead.
  """
  current = _git(
    "rev-parse", "--abbrev-ref", "HEAD", repo=repo, check=False,
  ).stdout.strip()
  if not current or current == "HEAD" or current == app_git.LOCAL_BRANCH:
    return False
  _git("branch", "-m", current, app_git.LOCAL_BRANCH, repo=repo)
  return True


def recovery_platform_paths() -> set[str]:
  """Repo-relative paths gitignored out of the platform repo entirely. From
  ``protected-files.txt``: ``/app/app/...`` → ``app/...``,
  ``/app/scripts/...`` → ``scripts/...`` (the ``/data/shell`` entries are not in
  this repo). These are the root-owned recovery / core files the mobius user
  CANNOT write; ignoring them keeps them out of every tree the merge engine
  touches, so the engine never special-cases them — the image owns them."""
  paths: set[str] = set()
  if not PROTECTED_LIST.exists():
    return paths
  for raw in PROTECTED_LIST.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or not line.startswith("/app/"):
      continue
    rel = line[len("/app/"):]
    if rel.startswith("app/") or rel.startswith("scripts/"):
      paths.add(rel)
  return paths


def _ensure_gitignore_has_recovery(repo: Path, paths: set[str]) -> bool:
  """Ensure ``/data/platform/.gitignore`` lists every recovery path under a
  managed block. Returns True if the file changed. Each path is anchored with a
  leading slash so it matches only the repo-root entry, never a same-named file
  deeper in the tree."""
  gi = repo / ".gitignore"
  existing = gi.read_text() if gi.exists() else ""
  lines = existing.splitlines()
  want = {f"/{p}" for p in paths}
  present = {ln.strip() for ln in lines}
  missing = sorted(want - present)
  if not missing:
    return False
  body = list(lines)
  if body and body[-1].strip():
    body.append("")
  body.append("# Recovery / core files are image-managed (root-owned chmod 444);")
  body.append("# they leave the platform git model entirely. See protected-files.txt.")
  body.extend(missing)
  gi.write_text("\n".join(body) + "\n")
  return True


def _untrack_recovery_files(repo: Path = PLATFORM_REPO) -> bool:
  """One-time idempotent migration: take the recovery files out of git.

  If any recovery path is still tracked (``git ls-files``), remove it from the
  INDEX ONLY (``git rm --cached`` — the root-owned file stays on disk), ensure
  ``.gitignore`` lists every recovery path, and commit both. Idempotent: a no-op
  once the files are already untracked and ignored. Returns True if it committed.

  ORDERING IS LOAD-BEARING: this must run BEFORE any ``record_baked_upstream`` /
  ``merge_upstream`` / seed in an apply, or the recovery files would still be in
  the merged tree and the mobius user would try (and fail) to write them.
  """
  paths = recovery_platform_paths()
  if not paths:
    return False
  tracked = {
    ln.strip()
    for ln in _git("ls-files", repo=repo).stdout.splitlines()
    if ln.strip()
  }
  to_untrack = sorted(p for p in paths if p in tracked)
  gitignore_changed = _ensure_gitignore_has_recovery(repo, paths)
  if not to_untrack and not gitignore_changed:
    return False
  if to_untrack:
    _git("rm", "--cached", "--ignore-unmatch", *to_untrack, repo=repo)
  _git("add", ".gitignore", repo=repo)
  _git(
    "-c", "user.name=Mobius", "-c", "user.email=agent@mobius",
    "commit", "-q", "-m",
    "migration: untrack recovery files from platform git", repo=repo,
  )
  return True


def collect_baked_floor(build_sha: str | None = None) -> BakedFloor:
  """Describe the baked floor that becomes the next ``upstream`` commit."""
  sha = build_sha or current_build_sha()
  if not sha:
    raise PlatformUpdateError("unknown_build_sha")
  if not BAKED_APP.is_dir() or not BAKED_SCRIPTS.is_dir():
    raise PlatformUpdateError("baked_floor_missing")
  # Pass the dirs explicitly (read at call time) so the resolved paths follow
  # the current module globals rather than the dataclass field defaults bound
  # at import — which is what lets tests retarget the baked floor.
  return BakedFloor(build_sha=sha, app_dir=BAKED_APP, scripts_dir=BAKED_SCRIPTS)


def record_baked_upstream(floor: BakedFloor, repo: Path = PLATFORM_REPO) -> str:
  """Commit the baked ``app`` + ``scripts`` floor onto ``upstream`` as a child
  of the previous upstream tip, WITHOUT checking the live worktree out to
  ``upstream``. Returns the new upstream commit SHA and (force-)tags it
  ``baked-<sha>`` so the next update's merge base is exact.

  Recovery files are EXCLUDED from the recorded tree: the repo's ``.gitignore``
  (which ``_untrack_recovery_files`` has populated with the recovery paths) is
  copied into the temp worktree, so ``git add`` honours it and stages neither
  the recovery files nor pycache. The recorded ``upstream`` tree therefore
  carries the ``.gitignore`` and NONE of the recovery files — exactly the same
  gitignore-respecting shape ``app_git.record_upstream`` produces for an app.
  """
  old_upstream = _git("rev-parse", UPSTREAM_BRANCH, repo=repo).stdout.strip()
  tmp = Path(tempfile.mkdtemp(prefix="platform-baked-"))
  index_path = Path(tempfile.mkstemp(prefix="platform-index-")[1])
  try:
    # Skip pycache entirely rather than copy-then-delete: the baked floor's
    # __pycache__ dirs/.pyc are root-owned read-only, and copytree preserves
    # their modes, so a later unlink would EPERM. (.gitignore would drop them
    # from the commit anyway; not copying them also keeps the temp tree clean.)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(floor.app_dir, tmp / "app", symlinks=False, ignore=ignore)
    shutil.copytree(floor.scripts_dir, tmp / "scripts", symlinks=False, ignore=ignore)
    # Carry the repo's managed .gitignore (recovery paths + pycache) into the
    # temp worktree so `git add -A` honours it and never stages a recovery file.
    # `_untrack_recovery_files` has already committed it to `main`; read it from
    # the work tree (it always exists by the time an apply records the floor).
    gitignore_bytes = (
      (repo / ".gitignore").read_bytes() if (repo / ".gitignore").exists()
      else b""
    )
    (tmp / ".gitignore").write_bytes(gitignore_bytes)
    # Carry the floor stamp over so the recorded upstream tree keeps it and a
    # later merge doesn't delete `.baked-sha` off `main`. It is a root file the
    # baked floor copy doesn't include, so copy the on-disk one verbatim.
    if (repo / ".baked-sha").exists():
      shutil.copyfile(repo / ".baked-sha", tmp / ".baked-sha")
    env = {
      **os.environ,
      "GIT_CEILING_DIRECTORIES": str(repo.resolve().parent),
      "GIT_INDEX_FILE": str(index_path),
      "GIT_WORK_TREE": str(tmp),
    }

    def g(*a: str, check: bool = True):
      return subprocess.run(
        ["git", "-C", str(repo), *a], capture_output=True, text=True,
        timeout=_GIT_TIMEOUT, check=check, env=env,
      )

    # Build the recorded tree from an EMPTY index, like `app_git.record_upstream`,
    # so nothing leaks in from a possibly-recovery-laden previous upstream tree —
    # a file dropped from the new baked floor (or an already-tracked recovery
    # file on the old upstream) vanishes. `git add -A` then stages only the temp
    # worktree, and the copied .gitignore filters recovery files + pycache out.
    g("read-tree", "--empty")
    g("add", "-A")
    tree = g("write-tree").stdout.strip()
    new_upstream = _git(
      "-c", "user.name=Mobius", "-c", "user.email=agent@mobius",
      "commit-tree", tree, "-p", old_upstream,
      "-m", f"upstream: baked platform {floor.build_sha}", repo=repo,
    ).stdout.strip()
    _git(
      "update-ref", f"refs/heads/{UPSTREAM_BRANCH}", new_upstream, old_upstream,
      repo=repo,
    )
    _git("tag", "-f", f"baked-{floor.build_sha}", new_upstream, repo=repo)
    return new_upstream
  finally:
    # The temp copy can contain read-only (copied-mode) files in read-only
    # dirs; make it writable so cleanup can't leak temp trees into /tmp.
    subprocess.run(
      ["chmod", "-R", "u+rwX", str(tmp)], capture_output=True, check=False,
    )
    shutil.rmtree(tmp, ignore_errors=True)
    index_path.unlink(missing_ok=True)


def _tree_modes(tree_oid: str, repo: Path = PLATFORM_REPO) -> dict[str, str]:
  out = _git("ls-tree", "-r", tree_oid, repo=repo).stdout
  modes: dict[str, str] = {}
  for line in out.splitlines():
    if "\t" not in line:
      continue
    meta, path = line.split("\t", 1)
    modes[path] = meta.split()[0]
  return modes


def write_merged_tree_to_worktree(
  merged_files: dict[str, bytes],
  *,
  repo: Path = PLATFORM_REPO,
  exec_paths: set[str] | None = None,
) -> list[str]:
  """Write a CLEAN merge result back to ``/data/platform`` file-by-file (temp +
  fsync + ``os.replace`` so a crash never leaves a truncated file), then delete
  tracked files that vanished from the merged tree. Skips ``.git`` and pycache.
  The recovery files are gitignored out of the repo, so they are never in the
  merged tree and need no skip here. Returns the paths written."""
  execs = exec_paths or set()
  written: list[str] = []
  for rel, data in merged_files.items():
    if rel == ".git" or rel.startswith(".git/"):
      continue
    if "/__pycache__/" in rel or rel.endswith(".pyc"):
      continue
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    with open(tmp, "wb") as fh:
      fh.write(data)
      fh.flush()
      os.fsync(fh.fileno())
    os.chmod(tmp, 0o755 if rel in execs else 0o644)
    os.replace(tmp, target)
    written.append(rel)
  merged_set = set(merged_files)
  for rel in _git("ls-files", repo=repo).stdout.splitlines():
    rel = rel.strip()
    if not rel or rel in merged_set:
      continue
    try:
      (repo / rel).unlink()
    except OSError:
      pass
  return written


def mark_restart_needed(build_sha: str) -> None:
  """Record that a restart is needed to finish the applied update, stamping the
  build sha the apply targeted. The clear-side reads this sha back to confirm a
  process actually booted on the applied floor before clearing the flag."""
  tmp = RESTART_NEEDED_FLAG.with_name(RESTART_NEEDED_FLAG.name + f".tmp-{os.getpid()}")
  tmp.write_text(build_sha)
  os.replace(tmp, RESTART_NEEDED_FLAG)


def clear_restart_needed_if_reconciled(repo: Path = PLATFORM_REPO) -> None:
  """Clear the restart flag only once a process actually booted on the applied
  sha and no merge is in progress.

  The flag records the build sha the apply TARGETED (``mark_restart_needed``).
  The bug this fixes: the old clear fired on ``image_sha == recorded_upstream_
  sha``, but an apply advances ``upstream`` to the new floor BEFORE the new image
  boots — so that equality could hold while the OLD process is still running,
  clearing "restart needed" before the restart happened. Comparing the RUNNING
  image's build sha against the sha stamped in the flag instead means the flag
  clears only when the booted process is the one the apply targeted.
  """
  if not RESTART_NEEDED_FLAG.exists():
    return
  if (repo / ".git" / "MERGE_HEAD").exists():
    return
  applied_sha = RESTART_NEEDED_FLAG.read_text().strip()
  image = current_build_sha()
  if applied_sha and image and image == applied_sha:
    RESTART_NEEDED_FLAG.unlink(missing_ok=True)


def _apply_sync(repo: Path) -> dict:
  """The blocking git work of an apply (run under ``asyncio.to_thread``).

  A thin sequence over the ``app_git`` engine, with the platform-specific
  ordering up front:

    1. ``_untrack_recovery_files`` — the gitignore migration. MUST run first so
       the recovery files are out of the index before anything records or merges
       a tree; otherwise they would surface in the merged tree and the mobius
       user would try (and fail) to write them.
    2. ``app_git.commit_local`` — stash any uncommitted owner/agent edits so the
       merge has a committed base to diverge from.
    3. seed + ``record_baked_upstream`` — put the new baked floor on ``upstream``.
    4. ``app_git.merge_upstream`` — the clean-vs-conflict verdict off the live
       worktree. Clean → write the merged tree + single-parent ``commit_replay``
       + mark restart. Conflict → record the flag; the async wrapper spawns the
       resolver chat.

  Returns a dict describing the outcome; the async wrapper does the (async)
  conflict-chat spawn.
  """
  if CONFLICT_FLAG.exists() or (repo / ".git" / "MERGE_HEAD").exists():
    return {"state": "conflict", "conflict_paths": _unmerged_paths(repo),
            "upstream_commit": None, "merge_commit": None}

  # The engine operates on the literal branch `main`; rename `master` first so
  # every app_git call resolves the working branch.
  _normalize_working_branch(repo)
  local = app_git.LOCAL_BRANCH

  # Recover from a previous apply that crashed mid-write: only gitignored-out
  # recovery files are never written, so resetting to the committed working
  # branch restores every file we touched. `upstream` stays advanced for retry.
  if APPLYING_FLAG.exists():
    _git("reset", "--hard", local, repo=repo, check=False)
    APPLYING_FLAG.unlink(missing_ok=True)

  _git("checkout", "-q", local, repo=repo, check=False)
  # ORDERING IS LOAD-BEARING: untrack the recovery files BEFORE recording or
  # merging any tree, so they never enter the merged tree.
  _untrack_recovery_files(repo)
  app_git.commit_local(repo, "platform: local edits before update")
  seed_upstream_if_missing(repo)
  floor = collect_baked_floor()
  new_upstream = record_baked_upstream(floor, repo)
  result = app_git.merge_upstream(repo)

  if result.status == "conflict":
    # Do NOT materialise markers here: the new code is on `upstream`; the agent
    # reconciles the named files into the working branch and restarts. Record the
    # conflict so Settings keeps surfacing it (the chat id is stamped in by the
    # async wrapper once the resolver chat is spawned).
    _write_conflict_flag(new_upstream, result.conflict_paths)
    return {"state": "conflict", "conflict_paths": result.conflict_paths,
            "upstream_commit": new_upstream, "merge_commit": None}

  merged = app_git.read_merged_tree(repo, result.merged_tree_oid)
  modes = _tree_modes(result.merged_tree_oid, repo)
  exec_paths = {p for p, m in modes.items() if m == "100755"}
  APPLYING_FLAG.write_text(floor.build_sha)
  try:
    write_merged_tree_to_worktree(merged, repo=repo, exec_paths=exec_paths)
    # Finalise as a SINGLE-parent linear replay on the new upstream tip, so the
    # working branch is a straight-line descendant of `upstream` and the next
    # update's `--is-ancestor` availability check is exact.
    merge_commit = app_git.commit_replay(
      repo, new_upstream, "merge: platform self-update",
    )
  except Exception:
    # Roll the worktree back to the last good local tip (rewrites only the files
    # we touched; the gitignored recovery files were never written).
    _git("reset", "--hard", local, repo=repo, check=False)
    raise
  finally:
    APPLYING_FLAG.unlink(missing_ok=True)
  CONFLICT_FLAG.unlink(missing_ok=True)
  mark_restart_needed(floor.build_sha)
  UPGRADE_FLAG.unlink(missing_ok=True)
  return {"state": "restart_needed", "conflict_paths": [],
          "upstream_commit": new_upstream, "merge_commit": merge_commit}


async def apply_platform_update(
  db: Session, repo: Path = PLATFORM_REPO,
) -> PlatformApplyResult:
  """Apply the current image's baked platform floor to local ``main``. Clean →
  written + ``restart_needed``. Conflict → ``upstream`` recorded + an agent
  resolver chat opened. Never restarts on its own."""
  async with _APPLY_LOCK:
    outcome = await asyncio.to_thread(_apply_sync, repo)
    chat_id: str | None = None
    if outcome["state"] == "conflict":
      chat_id = await spawn_platform_conflict_chat(db, outcome["conflict_paths"])
      # Stamp the resolver chat into the persisted flag so a Settings reload can
      # still link straight to it. When the apply BAILED on a pre-existing
      # conflict, the outcome carries no fresh upstream AND an empty path list
      # (a flag-only conflict isn't materialised in git, so _unmerged_paths is
      # []), AND spawn_platform_conflict_chat dedups to None because a resolver
      # is already running — so fall back to the recorded flag for ALL three
      # fields (upstream, paths, chat_id), never clobbering the good values
      # already on disk. Missing the chat_id fallback would drop the "Open chat"
      # link on every re-apply of an in-progress conflict.
      existing = _read_conflict_flag() or {}
      _write_conflict_flag(
        outcome["upstream_commit"] or existing.get("upstream"),
        outcome["conflict_paths"] or existing.get("paths") or [],
        chat_id or existing.get("chat_id"),
      )
    state = (
      PlatformUpdateState.RESTART_NEEDED
      if outcome["state"] == "restart_needed"
      else PlatformUpdateState.CONFLICT
    )
    return PlatformApplyResult(
      state=state.value,
      needs_restart=(state is PlatformUpdateState.RESTART_NEEDED),
      upstream_commit=outcome["upstream_commit"],
      merge_commit=outcome["merge_commit"],
      conflict_paths=outcome["conflict_paths"],
      chat_id=chat_id,
    )


async def spawn_platform_conflict_chat(
  db: Session, conflict_paths: list[str],
) -> str | None:
  """Open a visible agent chat to merge the new platform version into ``main``
  and resolve conflicts — the platform analogue of
  ``routes.apps._spawn_app_conflict_chat``. Dedupes on a running resolver."""
  import time
  import uuid

  from app import models
  from app.broadcast import create_broadcast, get_system_broadcast
  from app.chat import (
    current_run_generation, discard_starting, mark_starting, run_chat,
  )
  from app.chat_writer import StartTurn, alloc_run_token, await_ack, get_writer
  from app.push import notify_owner

  title = "Resolve platform update conflict"
  running = (
    db.query(models.Chat.id)
    .filter(models.Chat.title == title)
    .filter(models.Chat.run_status == "running")
    .filter(models.Chat.deleted_at.is_(None))
    .first()
  )
  if running is not None:
    return None

  owner = db.query(models.Owner).first()
  if owner is None:
    return None
  provider = owner.provider or "claude"

  files = ", ".join(conflict_paths) if conflict_paths else "some backend files"
  content = (
    "A platform update is ready but conflicts with local edits — the new "
    "version and the local changes both touched the same lines, so it can't "
    "apply cleanly.\n\n"
    "The new platform code is recorded on the `upstream` branch of the git "
    "repo at `/data/platform`. Reconcile these conflicting files into `main` "
    f"by hand: {files}.\n\n"
    "Resolve it with ordinary git: `git -C /data/platform merge upstream` "
    "writes conflict markers into the listed files; for each, combine the "
    "intent of the local version and upstream's, save it, then `git add` it. "
    "When every listed file is reconciled, `git commit` to finalise the merge "
    "on `main`. The recovery/core files (main.py, auth.py, the recover_* "
    "modules, entrypoint.sh …) are gitignored and image-managed, so they are "
    "never part of the merge — leave them be.\n\n"
    "When the reconcile is committed, clear the flag "
    "(`rm -f /data/.platform-conflict`) and tell the owner to **restart the "
    "server** from Settings to finish. To back out instead, `git -C "
    "/data/platform merge --abort`, `rm -f /data/.platform-conflict`, and tell "
    "the owner the update was skipped."
  )

  chat_id = str(uuid.uuid4())
  chat = models.Chat(
    id=chat_id, title=title, messages=[], pending_messages=[],
    provider=provider, created_by_app_id=None,
  )
  db.add(chat)
  db.commit()

  if not mark_starting(chat_id):
    return chat_id

  try:
    start_gen = current_run_generation(chat_id)
    run_token = alloc_run_token()
    user_msg = {"role": "user", "content": content, "ts": int(time.time() * 1000)}
    ack = get_writer().submit(StartTurn(
      chat_id=chat_id, run_token=run_token, user_msg=user_msg,
      title_source=title, default_provider=provider,
    ))
    result = await await_ack(ack)
    if current_run_generation(chat_id) == start_gen:
      create_broadcast(chat_id)
      get_system_broadcast().publish(
        {"type": "chat_run_started", "chatId": chat_id}
      )
      asyncio.create_task(run_chat(
        result["history"], chat_id=chat_id, session_id=result["session_id"],
        provider_id=result["provider"], run_gen=start_gen, run_token=run_token,
      ))
    else:
      discard_starting(chat_id)
  except Exception:
    discard_starting(chat_id)
    raise
  finally:
    try:
      notify_owner(
        db, owner.id, title="Platform update needs conflict resolution",
        body="The platform update conflicts with local edits. Opened a chat to resolve it.",
        source_type="platform_conflict", source_id=chat_id, target=f"chat:{chat_id}",
      )
    except Exception:
      pass

  return chat_id
