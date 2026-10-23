"""Per-app git repo: pristine `upstream` history + a local working branch.

Each installed app gets its own git repo at `/data/apps/<slug>/.git` with
two branches:

  - `upstream` holds the exact bytes of each installed manifest version.
    Only the installer commits here, via `record_upstream`. It is the
    merge base that lets an update tell "did the user diverge from what
    upstream shipped" without guessing from a sha.
  - `main` is the local working branch. Explicit app apply and Store update
    resolution commit accepted edits here; it is the branch the working tree
    actually checks out.

Update is `record_upstream` (commit the new upstream source TREE) then
`merge_upstream`, which computes a clean-vs-conflict verdict with
`git merge-tree --write-tree` WITHOUT touching the live working tree —
so a conflict never leaves the app served in a half-merged state. The
caller (install.py) applies a clean merge by reading the whole merged tree
(`read_merged_tree`), writing it back, and recompiling; on conflict it
surfaces the conflicting paths and leaves the local edits intact for an
agent to resolve later. One and many files share one tree path — the entry
`index.jsx` is just one key in the tree.

A finalized update is a SINGLE-parent replay (linear): both the clean
apply (`commit_replay`) and a resolved conflict (`commit_local` with
MERGE_HEAD set) commit the applied tree with `upstream` as its sole
parent, squashing the local delta into one replay commit on top of
upstream. So `main` is a straight-line descendant of `upstream`
(`A -> B -> X`) and `git merge-base --is-ancestor upstream main` is
exact — never the 2-parent merge a `git merge` would leave.

Only SOURCE is tracked — `index.jsx`, job scripts (`*.sh`), prompts,
seed templates. The compiled bundle
(`/data/compiled/app-<id>-<sha256>.js`) is a gitignored build artifact, and the
integer-id storage tree
(`/data/apps/<id>/`) is a SEPARATE directory the mini-app writes at
runtime, not under this source dir at all. A committed `.gitignore`
keeps both out even if a future caller drops them here.

Why shell out to the container's `git` rather than pygit2: `git` 2.x is
already in the image, the operations are coarse-grained (one subprocess
per install/update, not a hot loop), and `merge-tree --write-tree` — the
primitive that makes the no-clobber verdict possible — is a porcelain we
get for free without a libgit2 binding to pin and maintain.

CONCURRENCY: callers MUST hold `fs_locks.source_dir_lock(<source_dir>)`
around every entry point that reads or writes an app repository, so explicit
apply cannot race the installer's merge on the same repo. The exceptions are
the staging functions (`stage_fetch`, `stage_clone`), which write only a
private staging repository and must run with no app lock held; their local
counterparts (`import_staged`, `adopt_clone`) need the lock. This module does
not take the
lock itself — the lock is keyed on the source dir, which only the caller
knows, and nesting lock acquisition inside would hide the ordering the
rest of install.py reasons about.

install.py drives this module for every app that has a source directory; an
app with no source_dir has no `.git`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable, Iterable, Iterator

from app import managed_paths

log = logging.getLogger(__name__)

# Branch names. `upstream` is installer-only pristine history; `main` is
# the local working branch explicit apply commits to and the working
# tree checks out.
UPSTREAM_BRANCH = "upstream"
LOCAL_BRANCH = "main"

# Files we never want in app source history regardless of who drops them
# in the source dir. The live compiled bundle path is stored on the App row
# (outside this tree), so it never appears here. We must NOT blanket-ignore
# `*.js`: building-apps.md tells the agent to split larger apps into sibling
# `.js`/`.jsx`/`.ts`/`.tsx` modules (e.g. `cards.js`) imported from index.jsx,
# and those ARE hand-written source — a blanket `*.js` silently dropped them
# from per-app history, breaking the merge/conflict-resolution model for any
# modular app. So the ignore is scoped to genuine generated/vendored output
# (mirrors compiler generated-output exclusions) plus install artifacts and the
# integer-id storage tree.
_GITIGNORE = managed_paths.GITIGNORE

_MANAGED_RUNTIME_PATHS = managed_paths.MANAGED_RUNTIME_PATHS

_EXCLUDE_BEGIN = "# BEGIN MOBIUS MANAGED IGNORE RULES"
_EXCLUDE_END = "# END MOBIUS MANAGED IGNORE RULES"

# A fixed identity so commits don't depend on the container's global git
# config (which the mobius user may not have set). The installer commits
# under the upstream identity; local commits keep the same identity since
# the agent is the de-facto author of every local edit.
_GIT_NAME = "Mobius"
_GIT_EMAIL = "mobius@localhost"

# The ceiling for local Git commands, which may run under app locks.
_GIT_TIMEOUT = 30
# Git reaches the network only in ``_transfer``, which never runs under an app
# lock or in a live app repository. A healthy slow download may therefore take
# minutes; a stalled one still fails once it stays below the low-speed limit.
_GIT_TRANSFER_TIMEOUT = 600
_GIT_LOW_SPEED_LIMIT = 1024  # bytes per second
_GIT_LOW_SPEED_TIME = 60  # seconds
# The last transferred candidate. Keeping it on a private ref protects it from
# garbage collection between preview and install; no managed branch moves.
FETCHED_REF = "refs/mobius/fetched"


class GitTransferFailed(RuntimeError):
  """A Git network transfer failed: the host, the network or the repository.

  ``transient`` says whether the same transfer may later succeed unchanged: a
  timeout or a dropped connection may, a missing ref or repository, refused
  credentials, a name that does not exist or a full disk will not.
  """

  def __init__(self, message: str, *, transient: bool = True) -> None:
    super().__init__(message)
    self.transient = transient


class GitTransferTimeout(GitTransferFailed):
  """A Git transfer exceeded its wall-clock ceiling."""


class HistoryIncomplete(RuntimeError):
  """A shallow app repository hides the merge base an update needs."""


# Contribute records a reviewed change as a Git object in the repository that
# owns the live source.  The pending ref proves the reviewed diff came from a
# specific local commit; terminal contribution cleanup promotes it to landed
# only after GitHub reports the PR merged.  Neither ref touches the working
# tree, and both survive branch rewrites because they are ordinary Git refs.
_EQUIVALENCE_PENDING_PREFIX = "refs/mobius/equivalences/pending"
_EQUIVALENCE_LANDED_PREFIX = "refs/mobius/equivalences/landed"
_REVIEWED_SOURCE_PREFIX = "refs/mobius/equivalences/reviewed-source"
# A contribution revised in review can merge in a different form than the draft
# the live source still holds. When GitHub settles it merged, Contribute records
# that draft here; the platform updater sets it aside before merging the release
# that carries the merged version, so the two never meet as a conflict.
_SUPERSEDED_DRAFT_PREFIX = "refs/mobius/equivalences/superseded"
_CONTRIBUTION_DRAFT_PREFIX = "refs/mobius/contribution-drafts/"
_EQUIVALENCE_VERSION = 1
_REVIEWED_SOURCE_VERSION = 1
_HEX_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIFF_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_APP_MANIFEST_PUBLICATION_ADAPTER = "github_app_manifest_identity_v1"
_APP_MANIFEST_PUBLICATION_PATH = "mobius.json"
_APP_MANIFEST_PUBLICATION_FIELDS = ("author", "homepage")


@dataclass
class ReconciliationReceipt:
  """Explain one update decision without exposing merge implementation details."""

  proven_present: tuple[str, ...] = ()
  local_only_paths: tuple[str, ...] = ()
  new_upstream_paths: tuple[str, ...] = ()
  compatible_paths: tuple[str, ...] = ()
  unresolved_conflict_paths: tuple[str, ...] = ()
  provenance_refs_used: tuple[str, ...] = ()
  # Ancillary conflicts kept locally, dropping the incoming edit.
  kept_local_paths: tuple[str, ...] = ()
  # Local copies of merged pull requests the release replaced (see
  # ``without_retired_units``). The Review preview API returns them as
  # ``retired_paths`` before the owner applies, and the Settings update review
  # lists them.
  retired_paths: tuple["RetiredPath", ...] = ()

  def as_dict(self) -> dict[str, list]:
    return {
      "retired_paths": [item.as_dict() for item in self.retired_paths],
      "proven_present": list(self.proven_present),
      "local_only_paths": list(self.local_only_paths),
      "new_upstream_paths": list(self.new_upstream_paths),
      "compatible_paths": list(self.compatible_paths),
      "unresolved_conflict_paths": list(self.unresolved_conflict_paths),
      "provenance_refs_used": list(self.provenance_refs_used),
      "kept_local_paths": list(self.kept_local_paths),
    }


@dataclass
class MergeResult:
  """Verdict of merging `upstream` into the local `main` branch.

  `status` is 'clean' when the three-way merge produced no conflicts and
  `merged_tree_oid` is the oid of the full merged tree; the caller passes it to
  `read_merged_tree` to materialise EVERY merged file and writes the whole tree
  back. `status` is 'conflict' when at least one tracked file conflicted;
  `conflict_paths` names them and `merged_tree_oid` is None — the caller must NOT
  write anything, leaving the local edits intact for an agent to resolve.
  `merge_base_oid` is normally None. When reviewed contribution provenance
  proves a newer semantic base but a later genuine conflict remains, it names
  that synthetic base tree so the owner-gated resolver can materialize only the
  residual conflicts instead of repeating Git's older historical conflict set.
  `unrelated_histories` records the other explicit merge mode so the resolver
  can ask Git to materialize the same verdict.

  There is one path for one and many files: a single-file app is just a tree
  with one source key (`index.jsx`), so the caller always reads the merged tree
  rather than a single-entry shortcut.
  """
  status: str
  conflict_paths: list[str] = field(default_factory=list)
  merged_tree_oid: str | None = None
  # Conflict-marker tree is evidence only, never an applicable merge result.
  conflict_tree_oid: str | None = None
  merge_base_oid: str | None = None
  unrelated_histories: bool = False
  equivalent_change_refs: tuple[str, ...] = ()
  reconciliation: ReconciliationReceipt = field(
    default_factory=ReconciliationReceipt,
  )
  # The local commit an update actually merged: the requested one, or a
  # commit on it setting merged work aside. A resolver must start from it, or
  # the retired conflicts would return. The platform updater's one merge
  # (``_final_tree_merge``) always sets it; bare merges leave it None.
  local_oid: str | None = None


@dataclass(frozen=True)
class FetchUpstreamResult:
  """Fetched origin tip plus the narrow proof needed for legacy adoption.

  ``allow_unrelated_histories`` is true only when the caller identified the
  configured origin as trusted and Git proved that its complete tree equals
  local ``main``.  Keeping the proof beside the fetched SHA prevents later
  merge callers from widening unrelated-history handling by accident.
  """

  sha: str
  allow_unrelated_histories: bool = False


@dataclass(frozen=True)
class EquivalentChange:
  """One reviewed local change that Contribute observed landing upstream.

  ``anchor_sha`` is a synthetic commit whose parent is ``base_sha`` and whose
  tree is the exact reviewed head tree.  ``source_sha`` is the local commit in
  which Contribute proved that reviewed delta was already present.  The updater
  may use the anchor only while ``source_sha`` remains an ancestor of local.
  ``upstream_sha`` is GitHub's merge commit when it was available; it must both
  contain the reviewed delta and be an ancestor of the target. Otherwise a
  conservative tree-subsumption proof is required against the update target.
  ``reviewed_source_resolution`` is explicit agent-reviewed evidence, not an
  exact-tree claim: its reviewed tree and conflict-resolution digest bind the
  accepted local adaptation, retained as a local overlay by the semantic base.
  """

  ref: str
  anchor_sha: str
  base_sha: str
  source_sha: str
  upstream_sha: str | None
  diff_sha256: str
  contribution_id: str
  proof_mode: str
  source_projection: PublicationSourceProjection | None = None
  source_resolution_sha256: str | None = None
  source_resolution_captured_sha: str | None = None


@dataclass(frozen=True)
class ReviewedSourceContinuity:
  """One agent-reviewed bridge from a prepared change to newer local source.

  Normally the reviewed contribution is machine-proven in ``source_sha``. An
  explicit ``source_resolution_sha256`` instead binds the exact local conflict
  resolution at ``reviewed_through_sha``. An active source-chat agent reviewed
  the conflict evidence and every local commit through
  ``reviewed_through_sha`` after those histories began overlapping.  The
  witness can authorize publication only while that commit remains in the live
  ancestry and no later commit touches a reviewed path.
  """

  ref: str
  base_sha: str
  head_sha: str
  source_sha: str
  reviewed_through_sha: str
  diff_sha256: str
  contribution_id: str
  review_identity_sha256: str
  source_projection: PublicationSourceProjection | None = None
  source_resolution_sha256: str | None = None


@dataclass(frozen=True)
class SourceResolution:
  """Exact, reviewable local resolution of a publication/source merge conflict."""

  conflict_paths: tuple[str, ...]
  diff: bytes
  diff_sha256: str


@dataclass(frozen=True)
class PublicationSourceProjection:
  """One narrowly defined, review-visible source-to-publication projection.

  Installed apps may personalize their local manifest identity while a public
  app package must name its canonical GitHub owner and repository.  This value
  is not a generic ignore list: its adapter, root path, two fields, and exact
  reviewed replacement values are all fixed and serialized into provenance
  witnesses.  No source worktree or ref is rewritten while proving it.
  """

  adapter: str
  path: str
  fields: tuple[tuple[str, str], ...]

  def as_dict(self) -> dict:
    return {
      "adapter": self.adapter,
      "path": self.path,
      "fields": dict(self.fields),
    }


def publication_source_projection(
  raw: object,
  *,
  repo_slug: str | None = None,
) -> PublicationSourceProjection | None:
  """Parse the sole supported publication projection, failing closed.

  ``None`` means the ordinary exact-tree contract.  Any supplied projection
  must have the complete canonical shape.  When the reviewed repository is
  known, the replacements must be its GitHub organization and canonical URL;
  an app-writable plan therefore cannot nominate arbitrary manifest values.
  """
  if raw is None:
    return None
  if not isinstance(raw, dict) or set(raw) != {"adapter", "path", "fields"}:
    raise ValueError("invalid publication source projection")
  fields = raw.get("fields")
  if (
    raw.get("adapter") != _APP_MANIFEST_PUBLICATION_ADAPTER
    or raw.get("path") != _APP_MANIFEST_PUBLICATION_PATH
    or not isinstance(fields, dict)
    or set(fields) != set(_APP_MANIFEST_PUBLICATION_FIELDS)
    or any(
      not isinstance(fields.get(field), str) or not fields[field]
      for field in _APP_MANIFEST_PUBLICATION_FIELDS
    )
  ):
    raise ValueError("invalid publication source projection")
  if repo_slug is not None:
    parts = str(repo_slug).split("/")
    if len(parts) != 2 or any(not part for part in parts):
      raise ValueError("invalid publication repository")
    expected = {
      "author": parts[0],
      "homepage": f"https://github.com/{repo_slug}",
    }
    if fields != expected:
      raise ValueError("publication projection does not match repository")
  return PublicationSourceProjection(
    adapter=_APP_MANIFEST_PUBLICATION_ADAPTER,
    path=_APP_MANIFEST_PUBLICATION_PATH,
    fields=tuple(
      (field, fields[field]) for field in _APP_MANIFEST_PUBLICATION_FIELDS
    ),
  )


@dataclass(frozen=True)
class WorktreeTree:
  """Immutable candidate captured from an app working tree.

  ``tree_oid`` names the exact bytes and modes staged through the app repo's
  canonical ignore rules. ``parent_sha`` is the local branch tip against which
  a later commit must compare-and-swap.
  """

  tree_oid: str
  parent_sha: str


class SourceTreeChanged(RuntimeError):
  """The app source or accepted branch moved during an explicit apply."""


def _git_env(
  repo: Path | str, *, read_only: bool = False,
) -> dict:
  """Isolated env for a per-app git op so it can never bleed into an
  enclosing repo. `/data` is itself a git repo (the agent's pm-commit
  history), so a source_dir with no dedicated `.git` would otherwise let
  git walk up to `/data` — making every per-app op operate on the wrong
  repo and report spurious conflicts. Pin GIT_CEILING_DIRECTORIES to the
  app-dir's parent (git won't search above it → it finds the app's OWN
  repo or none, never /data) and scrub inherited GIT_* pointers that would
  override `-C`. Used by EVERY git subprocess here (`_run` + the few direct
  bytes-on-stdin calls). Mirrors the test conftest's `_isolate_git_env`.
  """
  env = dict(os.environ)
  for var in (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_OPTIONAL_LOCKS",
  ):
    env.pop(var, None)
  env["GIT_CEILING_DIRECTORIES"] = str(Path(repo).resolve().parent)
  # App installs, explicit applies, updates, and cron work are unattended. A
  # missing/private origin must fail within the subprocess timeout and fall back
  # through the caller's normal error path; it must never wait on an inherited
  # terminal or desktop credential prompt. Configured credential helpers and
  # SSH agents remain usable without interaction.
  env["GIT_TERMINAL_PROMPT"] = "0"
  env["GCM_INTERACTIVE"] = "Never"
  env["GIT_ASKPASS"] = "/bin/false"
  env["SSH_ASKPASS"] = "/bin/false"
  quarantine = _OBJECT_QUARANTINE.get()
  if quarantine and quarantine[0] == str(Path(repo).resolve()):
    env["GIT_OBJECT_DIRECTORY"], env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = quarantine[1:]
  if read_only:
    # Read-only status/provenance checks must not refresh the durable index or
    # leave an optional lock behind. Mandatory locks for ordinary write paths
    # remain enabled because their callers keep the default environment.
    env["GIT_OPTIONAL_LOCKS"] = "0"
  return env


# (repository, temporary object store, the repository's own object store)
# while ``quarantined_objects`` is active in this context.
_OBJECT_QUARANTINE: ContextVar[tuple[str, str, str] | None] = ContextVar(
  "app_git_object_quarantine", default=None,
)


@contextmanager
def quarantined_objects(source_dir: str | Path) -> Iterator[None]:
  """Let a read-only caller run planners that write trees and commits.

  Git calls on ``source_dir`` inside the block still read every object of the
  repository, but the objects they create go to a temporary store deleted on
  exit, so nothing is added to the repository. Refs must not move inside it.
  """
  repo = Path(source_dir).resolve()
  common = _run(repo, "rev-parse", "--git-common-dir").stdout.strip()
  objects = (repo / common / "objects").resolve()
  with tempfile.TemporaryDirectory(prefix="mobius-quarantine-") as tmp:
    token = _OBJECT_QUARANTINE.set((str(repo), tmp, str(objects)))
    try:
      yield
    finally:
      _OBJECT_QUARANTINE.reset(token)


def ref_file_size(source_dir: str | Path, ref: str, path: str) -> int | None:
  """One file's size at ``ref`` without reading it, or None when it has none."""
  proc = _run(Path(source_dir), "cat-file", "-s", f"{ref}:{path}", check=False, read_only=True)
  return int(proc.stdout.strip()) if proc.returncode == 0 else None


def ref_file(source_dir: str | Path, ref: str, path: str) -> bytes | None:
  """One file's bytes at ``ref``, or None when ``ref`` has no file there."""
  return _ref_blob(Path(source_dir), ref, path, read_only=True)


def _run(
  repo: Path,
  *args: str,
  check: bool = True,
  read_only: bool = False,
  timeout: int = _GIT_TIMEOUT,
  env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
  """Runs `git -C <repo> <args>` with the fixed Mobius identity.

  The identity is injected per-invocation via `-c` rather than written
  into the repo config so the repo carries no state the next reader has
  to know about. `check=False` lets callers inspect a non-zero return
  (e.g. merge-tree signalling a conflict) instead of raising. Runs under
  the isolated `_git_env` so it can never bleed into the enclosing /data
  repo.
  """
  cmd = [
    "git",
    "-c", f"user.name={_GIT_NAME}",
    "-c", f"user.email={_GIT_EMAIL}",
    "-C", str(repo),
    *args,
  ]
  with subprocess.Popen(
    cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    start_new_session=True,
    env={**_git_env(repo, read_only=read_only), **(env or {})},
  ) as process:
    try:
      stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
      # Git removes its locks on SIGTERM; stop transport children with it.
      try:
        os.killpg(process.pid, signal.SIGTERM)
        try:
          process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
          os.killpg(process.pid, signal.SIGKILL)
          process.communicate()
      except ProcessLookupError:
        pass  # The whole group exited on its own in the meantime.
      raise
    if check and process.returncode:
      raise subprocess.CalledProcessError(process.returncode, cmd, stdout, stderr)
    return subprocess.CompletedProcess(cmd, process.returncode, stdout, stderr)


def _run_with_index(
  repo: Path,
  index_file: Path,
  *args: str,
  check: bool = True,
  read_only: bool = False,
  input: str | None = None,
) -> subprocess.CompletedProcess:
  """Run Git against a temporary index while sharing this repo's object DB."""
  env = _git_env(repo, read_only=read_only)
  env["GIT_INDEX_FILE"] = str(index_file)
  cmd = [
    "git",
    "-c", f"user.name={_GIT_NAME}",
    "-c", f"user.email={_GIT_EMAIL}",
    "-C", str(repo),
    *args,
  ]
  return subprocess.run(
    cmd, capture_output=True, text=True, timeout=_GIT_TIMEOUT,
    check=check, env=env, input=input,
  )


def merge_trees_into_worktree(
  repo: Path, *trees: str, dry_run: bool = False, check: bool = True,
  timeout: int = _GIT_TIMEOUT,
) -> subprocess.CompletedProcess:
  """Git's native ``read-tree -m -u`` on the real index and worktree.

  Two trees are Git's branch switch (carry unrelated uncommitted work, refuse
  to overwrite an edited path); three are a merge from an explicit base. Every
  merging ``read-tree -m -u`` goes through here, because it is plumbing:
  unlike checkout, merge or status it never refreshes the index, and it
  rejects any entry whose cached stat data differs as "not uptodate" even when
  the bytes are unchanged. A metadata-only change such as an ownership or mode
  repair (which updates ctime) would otherwise refuse a transition with no
  local edit to protect, and repeat that refusal on every attempt.

  The refresh re-hashes only stat-dirty entries: unchanged bytes become clean,
  real edits stay dirty, so read-tree's own overwrite checks still refuse
  genuine local work. ``--unmerged`` leaves an unmerged index for read-tree
  itself to refuse. A dry run (``-n``) refreshes too; that changes no content.
  With ``check=False`` the first failing step's result is returned.
  """
  refreshed = _run(
    repo, "update-index", "-q", "--unmerged", "--refresh",
    check=check, timeout=timeout,
  )
  if refreshed.returncode:
    return refreshed
  return _run(
    repo, "read-tree", *(["-n"] if dry_run else []), "-m", "-u", *trees,
    check=check, timeout=timeout,
  )


def _require_local_branch(repo: Path) -> None:
  """Fail before an accepted-source operation can touch the wrong index.

  App history has one canonical writable branch. Updating ``main`` while a
  deployment or review branch is checked out would move one ref and refresh a
  different branch's index, making ignored runtime files appear staged. Keep
  that invalid repository shape explicit instead of trying to reconcile two
  source owners inside the publisher.
  """
  current = _run(
    repo, "symbolic-ref", "--quiet", "--short", "HEAD", check=False,
  )
  if current.returncode != 0 or current.stdout.strip() != LOCAL_BRANCH:
    raise SourceTreeChanged(
      f"App source must be checked out on {LOCAL_BRANCH!r} before applying it."
    )


def _tracked_source(source_dir: Path) -> list[str]:
  """The source files to stage: everything in the dir minus what
  `.gitignore` excludes. We add by directory (`git add -A`) and let the
  committed `.gitignore` do the filtering, so a new job script the agent
  drops in is picked up without this module enumerating extensions. The
  explicit backup exclusions are load-bearing for repos whose committed
  `.gitignore` predates the drop-backup pattern."""
  return [
    "-A", ".",
    ":(exclude)*.mobius-drop-bak",
    ":(exclude,glob)**/*.mobius-drop-bak",
  ]


def _managed_exclude_block() -> str:
  return f"{_EXCLUDE_BEGIN}\n{_GITIGNORE}{_EXCLUDE_END}\n"


def _write_managed_exclude(exclude: Path) -> None:
  """Write Mobius rules into .git/info/exclude without owning the file."""
  block = _managed_exclude_block()
  current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
  start = current.find(_EXCLUDE_BEGIN)
  end = current.find(_EXCLUDE_END, start + len(_EXCLUDE_BEGIN)) if start >= 0 else -1
  if start >= 0 and end >= 0:
    end += len(_EXCLUDE_END)
    if current[end:end + 1] == "\n":
      end += 1
    updated = current[:start] + block + current[end:]
  elif current in (_GITIGNORE, _GITIGNORE + "\n"):
    updated = block
  else:
    prefix = current
    if prefix and not prefix.endswith("\n"):
      prefix += "\n"
    if prefix and not prefix.endswith("\n\n"):
      prefix += "\n"
    updated = prefix + block
  if updated != current:
    exclude.write_text(updated, encoding="utf-8")


def _ref_has_path(repo: Path, ref: str, rel: str) -> bool:
  proc = _run(repo, "cat-file", "-e", f"{ref}:{rel}", check=False)
  return proc.returncode == 0


def _looks_like_managed_gitignore(text: str) -> bool:
  if text in (_GITIGNORE, _GITIGNORE + "\n"):
    return True
  return (
    "Generated build output and vendored deps are not hand-written source" in text
    and "Manifest static_assets are install-managed" in text
    and "init-cron.sh" in text
    and "[0-9]*/" in text
  )


def _drop_stale_managed_gitignore_from_origin_repo(
  repo: Path,
  *,
  index_file: Path | None = None,
) -> None:
  """Remove old synthetic Mobius .gitignore files from real-origin repos.

  Real-origin apps own their committed `.gitignore`; Mobius rules belong in
  `.git/info/exclude`. During migration from the older synthetic repo model, a
  generated `.gitignore` can be left in the worktree. If the catalog upstream
  does not track `.gitignore`, that stale file would otherwise be committed as
  a local app edit on the next install/update.
  """
  gitignore = repo / ".gitignore"
  if not gitignore.exists() or _ref_has_path(repo, UPSTREAM_BRANCH, ".gitignore"):
    return
  try:
    text = gitignore.read_text(encoding="utf-8")
  except UnicodeDecodeError:
    return
  if not _looks_like_managed_gitignore(text):
    return
  runner = (
    (lambda *args, **kwargs: _run_with_index(
      repo, index_file, *args, **kwargs,
    ))
    if index_file is not None
    else (lambda *args, **kwargs: _run(repo, *args, **kwargs))
  )
  runner("rm", "--cached", "--ignore-unmatch", "--", ".gitignore", check=False)
  gitignore.unlink(missing_ok=True)


def _refresh_ignore_rules(
  source_dir: str | Path,
  *,
  index_file: Path | None = None,
) -> None:
  """Refresh Mobius-managed ignore rules for a per-app repo.

  Synthetic installer repos own their committed `.gitignore`, so old repos are
  upgraded in place before staging. Real-origin cloned repos own their committed
  `.gitignore`; Mobius rules live in `.git/info/exclude` there. Both repo kinds
  can have old commits that already tracked runtime files, so this also removes
  those paths from the index while keeping the files on disk.
  """
  repo = Path(source_dir)
  if has_origin(repo):
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    _write_managed_exclude(exclude)
    _drop_stale_managed_gitignore_from_origin_repo(
      repo, index_file=index_file,
    )
  else:
    gitignore = repo / ".gitignore"
    if not gitignore.exists() or gitignore.read_text(encoding="utf-8") != _GITIGNORE:
      gitignore.write_text(_GITIGNORE, encoding="utf-8")
  if index_file is None:
    _run(
      repo, "rm", "-r", "--cached", "--ignore-unmatch", "--",
      *_MANAGED_RUNTIME_PATHS, check=False,
    )
  else:
    _run_with_index(
      repo, index_file,
      "rm", "-r", "--cached", "--ignore-unmatch", "--",
      *_MANAGED_RUNTIME_PATHS, check=False,
    )


def ignore_migration_paths(
  source_dir: str | Path, accepted: str, tree: str | None = None,
) -> set[str]:
  """Paths that differ from ``accepted`` only through managed-ignore migration.

  The read-only mirror of ``_refresh_ignore_rules``, which apply runs before
  every snapshot: a synthetic repo's ``.gitignore`` rewritten to the managed
  rules, a stale managed ``.gitignore`` dropped from a real-origin repo, and
  managed runtime paths untracked. ``tree`` is the candidate to compare, or
  ``None`` for the worktree, where untracking leaves files in place, so any
  change to a managed runtime path there is part of the migration too.
  """
  repo = Path(source_dir)
  before = _ref_blob(repo, accepted, ".gitignore", read_only=True)
  if tree is None:
    gitignore = repo / ".gitignore"
    after = gitignore.read_bytes() if gitignore.is_file() else None
  else:
    after = _ref_blob(repo, tree, ".gitignore", read_only=True)
  paths: set[str] = set()
  if before != after and (
    after == _GITIGNORE.encode("utf-8")
    or (
      after is None and before is not None
      and _looks_like_managed_gitignore(before.decode("utf-8", "replace"))
    )
  ):
    paths.add(".gitignore")
  if tree is not None:
    untracked = [_run(
      repo, "diff", "--name-only", "--no-renames", "-z", "--diff-filter=D",
      accepted, tree, "--", *_MANAGED_RUNTIME_PATHS, check=False, read_only=True,
    )]
  else:
    # Apply untracks these runtime paths, so whatever they hold in the
    # worktree (changed, or new under older ignore rules) never reaches it.
    untracked = [
      _run(
        repo, "diff", "--name-only", "--no-renames", "-z", accepted,
        "--", *_MANAGED_RUNTIME_PATHS, check=False, read_only=True,
      ),
      _run(
        repo, "ls-files", "--others", "--exclude-standard", "-z",
        "--", *_MANAGED_RUNTIME_PATHS, check=False, read_only=True,
      ),
    ]
  for proc in untracked:
    if proc.returncode == 0:
      paths.update(path for path in proc.stdout.split("\0") if path)
  return paths


def worktree_diff_paths(source_dir: str | Path, ref: str) -> set[str] | None:
  """Paths whose worktree differs from ``ref``, untracked included, read-only.

  Ignored files are left out, as apply never commits them. ``None`` when Git
  cannot answer. Unlike a snapshot, this never touches ignore rules or any
  index, so read-only views can use it.
  """
  repo = Path(source_dir)
  diff = _run(
    repo, "diff", "--name-only", "--no-renames", "-z", ref, "--",
    check=False, read_only=True,
  )
  untracked = _run(
    repo, "ls-files", "--others", "--exclude-standard", "-z",
    check=False, read_only=True,
  )
  if diff.returncode or untracked.returncode:
    return None
  return {path for path in (diff.stdout + untracked.stdout).split("\0") if path}


def is_repo(source_dir: str | Path) -> bool:
  """Whether `source_dir` already holds a git repo."""
  return (Path(source_dir) / ".git").exists()


def worktree_dirty(
  source_dir: str | Path, paths: Iterable[str] = (),
) -> bool:
  """Whether tracked/untracked accepted-source paths differ from ``HEAD``.

  ``paths`` narrows the check to those literal paths, so a proof about one
  change is not blocked by unrelated in-progress edits. No paths means the
  whole tree.
  """
  if not is_repo(source_dir):
    return False
  pathspec = [f":(literal){path}" for path in sorted(set(paths))]
  return bool(_run(
    Path(source_dir), "status", "--porcelain",
    *(("--untracked-files=all", "--", *pathspec) if pathspec else ()),
    read_only=True,
  ).stdout.strip())


def head_sha(source_dir: str | Path, branch: str) -> str:
  """The commit sha at the tip of `branch` (e.g. the merge base an
  update will diverge from). Assumes the repo + branch exist."""
  return _run(Path(source_dir), "rev-parse", branch).stdout.strip()


def ref_is_ancestor(
  source_dir: str | Path, ancestor: str, descendant: str,
) -> bool | None:
  """Whether ``ancestor`` is reachable from ``descendant``.

  Returns ``True`` for Git's proven-ancestor exit 0, ``False`` for its normal
  not-an-ancestor exit 1, and ``None`` when Git cannot answer (missing/corrupt
  refs, timeout, or another execution error). Callers must not turn an unknown
  repository state into a positive conflict/resolution claim.
  """
  if not is_repo(source_dir):
    return None
  try:
    proc = _run(
      Path(source_dir), "merge-base", "--is-ancestor",
      ancestor, descendant, check=False,
    )
  except (OSError, subprocess.SubprocessError):
    return None
  if proc.returncode == 0:
    return True
  if proc.returncode == 1:
    return False
  return None


def ref_exists(source_dir: str | Path, ref: str) -> bool:
  """Whether `ref` resolves in this repo. Read-only; never raises.

  Lets a caller tell "this app has a recorded upstream branch" apart from "the
  branch was never recorded" before reading its tree — `read_ref_tree` would
  raise on a missing ref, so the guard belongs here. `--verify --quiet` resolves
  the ref without printing, exiting non-zero when it doesn't exist. A repo with
  no `.git` is trivially false rather than a git error."""
  if not is_repo(source_dir):
    return False
  try:
    proc = _run(
      Path(source_dir), "rev-parse", "--verify", "--quiet", ref, check=False,
      read_only=True,
    )
  except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
    return False
  return proc.returncode == 0


def preview_merge_refs(
  source_dir: str | Path, left: str, right: str,
) -> MergeResult | None:
  """Read-only merge verdict whose generated objects are disposable.

  ``merge-tree --write-tree`` always writes objects.  Review/status callers
  must therefore run it in a temporary primary object database and expose the
  durable checkout only through an alternate.
  """
  repo = Path(source_dir)
  try:
    left_oid = _resolve_commit(repo, left, read_only=True)
    right_oid = _resolve_commit(repo, right, read_only=True)
    common = _run(
      repo, "rev-parse", "--path-format=absolute", "--git-common-dir",
      check=False, read_only=True,
    )
    if left_oid is None or right_oid is None or common.returncode != 0:
      return None
    object_dir = (Path(common.stdout.strip()) / "objects").resolve()
    if not object_dir.is_dir() or "\n" in str(object_dir):
      return None
    with tempfile.TemporaryDirectory(prefix="mobius-merge-preview-") as tmp:
      proof_repo = Path(tmp) / "repo"
      _run(Path(tmp), "init", "-q", str(proof_repo), read_only=True)
      alternates = proof_repo / ".git" / "objects" / "info" / "alternates"
      alternates.parent.mkdir(parents=True, exist_ok=True)
      alternates.write_text(f"{object_dir}\n", encoding="utf-8")
      return merge_refs(
        proof_repo, left_oid, right_oid, read_only=True,
      )
  except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
    return None


def _resolve_commit(
  repo: Path, ref: str, *, read_only: bool = False,
) -> str | None:
  """Resolve ``ref`` to a full commit oid, failing closed on bad metadata."""
  proc = _run(
    repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
    f"{ref}^{{commit}}",
    check=False, read_only=read_only,
  )
  oid = proc.stdout.strip().lower()
  return oid if proc.returncode == 0 and _HEX_OID.fullmatch(oid) else None


def _tree_oid(
  repo: Path, ref: str, *, read_only: bool = False,
) -> str | None:
  proc = _run(
    repo, "rev-parse", "--verify", "--quiet", "--end-of-options",
    f"{ref}^{{tree}}", read_only=read_only,
    check=False,
  )
  oid = proc.stdout.strip().lower()
  return oid if proc.returncode == 0 and _HEX_OID.fullmatch(oid) else None


def _canonical_diff(
  repo: Path,
  base_sha: str,
  head_sha: str,
  *,
  read_only: bool = False,
) -> bytes | None:
  """The byte-exact diff shape Contribute hashes before a PR can be sent."""
  try:
    proc = subprocess.run(
      [
        "git", "-c", "core.quotePath=false", "-C", str(repo),
        "diff", "--no-ext-diff", "--no-color", "--binary", "--full-index",
        "--src-prefix=a/", "--dst-prefix=b/", f"{base_sha}..{head_sha}",
      ],
      capture_output=True, timeout=_GIT_TIMEOUT, check=False,
      env=_git_env(repo, read_only=read_only),
    )
  except (OSError, subprocess.SubprocessError):
    return None
  return proc.stdout if proc.returncode == 0 else None


def canonical_diff(
  source_dir: str | Path, base_ref: str, candidate_tree: str,
) -> bytes | None:
  """Return the stable full-tree diff used to bind accepted source reviews."""
  return _canonical_diff(Path(source_dir), base_ref, candidate_tree)


def merge_refs(
  source_dir: str | Path,
  left: str,
  right: str,
  *,
  merge_base: str | None = None,
  allow_unrelated_histories: bool = False,
  read_only: bool = False,
) -> MergeResult:
  """Off-tree three-way merge of two refs, optionally with an explicit base.

  This is the one parser for ``git merge-tree --write-tree`` used by ordinary
  app updates, provenance proofs, and the platform updater's conflict fallback.
  It never moves a ref, index, or working tree.
  """
  repo = Path(source_dir)
  args = ["merge-tree", "--write-tree", "--name-only", "-z"]
  if merge_base is not None:
    args.extend(("--merge-base", merge_base))
  if allow_unrelated_histories:
    args.append("--allow-unrelated-histories")
  args.extend((left, right))
  proc = _run(
    repo, *args, check=False, read_only=read_only,
  )
  if proc.returncode > 1:
    detail = proc.stderr.strip() or proc.stdout.strip()
    raise RuntimeError(
      f"git merge-tree failed (rc={proc.returncode}): {detail}"
    )
  lines = proc.stdout.split("\0")
  tree_oid = lines[0].strip() if lines else ""
  if not tree_oid:
    raise RuntimeError("git merge-tree returned no merged tree")
  if proc.returncode == 1:
    paths: list[str] = []
    for line in lines[1:]:
      if not line:
        break
      paths.append(line)
    return MergeResult(
      status="conflict", conflict_paths=paths, conflict_tree_oid=tree_oid,
    )
  return MergeResult(status="clean", merged_tree_oid=tree_oid)


def _change_is_subsumed(
  repo: Path,
  base_sha: str,
  anchor_sha: str,
  descendant: str,
  *,
  read_only: bool = False,
) -> bool:
  """Prove ``base..anchor`` is already contained in ``descendant``'s tree.

  A clean three-way result equal to the descendant tree means adding the
  reviewed delta contributes no bytes.  This proof is intentionally stricter
  than patch-id: later edits to the same lines make it fail rather than guess.
  Causal provenance (the source/upstream witness SHAs) covers that later-edit
  case once the pending/landed refs have been recorded.
  """
  try:
    merged = merge_refs(
      repo, anchor_sha, descendant, merge_base=base_sha,
      read_only=read_only,
    )
  except (OSError, subprocess.SubprocessError, RuntimeError):
    return False
  descendant_tree = _tree_oid(
    repo, descendant, read_only=read_only,
  )
  return bool(
    merged.status == "clean"
    and descendant_tree
    and merged.merged_tree_oid == descendant_tree
  )


def endpoint_diff_paths(
  source_dir: str | Path,
  left: str,
  right: str,
  *,
  read_only: bool = False,
) -> set[str] | None:
  """Complete endpoint path differences, or ``None`` when Git cannot answer."""
  repo = Path(source_dir)
  proc = _run(
    repo, "diff", "--name-only", "--no-renames", "-z", f"{left}..{right}",
    check=False, read_only=read_only,
  )
  if proc.returncode != 0:
    return None
  return {path for path in proc.stdout.split("\0") if path}


def commit_range_touches_paths(
  source_dir: str | Path,
  ancestor: str,
  descendant: str,
  paths: Iterable[str],
  *,
  read_only: bool = False,
) -> bool | None:
  """Whether any commit edge in ``ancestor..descendant`` touches ``paths``.

  Endpoint diffs cannot answer this safely: a revert followed by a reapply has
  no final path delta, and a merge can hide a reviewed-path change when only
  its first-parent result is inspected. ``--diff-merges=separate`` emits every
  merge-parent edge, while the unrestricted revision walk also includes the
  commits introduced through those parents. The NUL-delimited path stream
  keeps unusual but valid source names unambiguous.

  ``None`` means Git could not prove the complete descendant range. Callers
  that guard publication must treat that as a rejection, never as no overlap.
  """
  repo = Path(source_dir)
  guarded = {str(path) for path in paths if str(path)}
  if not guarded:
    return None
  if ref_is_ancestor(repo, ancestor, descendant) is not True:
    return None
  proc = _run(
    repo,
    "log",
    "--format=",
    "--name-only",
    "--no-renames",
    "--diff-merges=separate",
    "-z",
    f"{ancestor}..{descendant}",
    "--",
    check=False,
    read_only=read_only,
  )
  if proc.returncode != 0:
    return None
  return any(path in guarded for path in proc.stdout.split("\0") if path)


def ref_trees_equal(
  source_dir: str | Path, left: str, right: str,
) -> bool:
  """Whether two refs name the same complete tree, regardless of history."""
  repo = Path(source_dir)
  try:
    left_tree = _tree_oid(repo, left)
    right_tree = _tree_oid(repo, right)
  except (OSError, subprocess.SubprocessError):
    return False
  return bool(left_tree and left_tree == right_tree)


def describe_reconciliation(
  source_dir: str | Path,
  shared_base: str,
  upstream: str,
  *,
  local: str | None = None,
  conflict_paths: Iterable[str] = (),
  proven_changes: Iterable[EquivalentChange] = (),
) -> ReconciliationReceipt:
  """Return the common platform/app receipt for one semantic merge base.

  ``shared_base`` already contains every reviewed change proven present on
  both sides. Its diffs to ``local`` and ``upstream`` are therefore the
  genuinely local and genuinely incoming path sets, while ``conflict_paths``
  is the residual owner decision. Callers do not need to reconstruct any fact
  from warnings or Git topology.
  """
  repo = Path(source_dir)
  proven = tuple(proven_changes)
  local_paths = (
    endpoint_diff_paths(repo, shared_base, local) if local else set()
  ) or set()
  new_paths = endpoint_diff_paths(repo, shared_base, upstream) or set()
  conflicts = set(conflict_paths)
  compatible = local_paths & new_paths - conflicts
  return ReconciliationReceipt(
    proven_present=tuple(change.contribution_id for change in proven),
    local_only_paths=tuple(sorted(local_paths - new_paths)),
    new_upstream_paths=tuple(sorted(new_paths - local_paths)),
    compatible_paths=tuple(sorted(compatible)),
    unresolved_conflict_paths=tuple(sorted(conflicts)),
    provenance_refs_used=tuple(change.ref for change in proven),
  )


def primary_worktree_path(source_dir: str | Path) -> Path | None:
  """Primary live checkout path when ``source_dir`` is a linked worktree.

  Contribute stages reviews in linked worktrees.  Their common Git directory is
  the live platform/app checkout's ``.git`` directory. A standalone/separate
  staging clone deliberately returns ``None`` because it cannot prove a
  relationship to any installed source tree.
  """
  repo = Path(source_dir)
  proc = _run(
    repo, "rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir",
    check=False,
  )
  if proc.returncode != 0:
    return None
  paths = [Path(line).resolve() for line in proc.stdout.splitlines() if line]
  if len(paths) != 2:
    return None
  git_dir, common = paths
  if git_dir == common:
    return None
  if common.name != ".git":
    return None
  primary = common.parent
  if not (primary / ".git").is_dir():
    return None
  return primary


def _equivalence_ref(prefix: str, diff_sha256: str) -> str:
  return f"{prefix}/{diff_sha256}"


def _write_equivalence_anchor(
  repo: Path,
  *,
  prefix: str,
  base_sha: str,
  head_sha: str,
  source_sha: str,
  diff_sha256: str,
  contribution_id: str,
  upstream_sha: str | None,
  proof_mode: str,
  source_projection: PublicationSourceProjection | None = None,
  source_resolution_sha256: str | None = None,
  source_resolution_captured_sha: str | None = None,
) -> str:
  tree = _tree_oid(repo, head_sha)
  if tree is None:
    raise RuntimeError("reviewed contribution tree is unavailable")
  metadata = {
    "version": _EQUIVALENCE_VERSION,
    "diff_sha256": diff_sha256,
    "source_sha": source_sha,
    "upstream_sha": upstream_sha,
    "contribution_id": contribution_id[:128],
    "proof_mode": proof_mode,
  }
  if source_projection is not None:
    metadata["source_projection"] = source_projection.as_dict()
  if source_resolution_sha256 is not None:
    metadata["source_resolution_sha256"] = source_resolution_sha256
    metadata["source_resolution_captured_sha"] = source_resolution_captured_sha
  anchor = _run(
    repo, "commit-tree", tree, "-p", base_sha,
    "-m", json.dumps(metadata, sort_keys=True, separators=(",", ":")),
  ).stdout.strip()
  ref = _equivalence_ref(prefix, diff_sha256)
  _run(repo, "update-ref", ref, anchor)
  return ref


def _pending_equivalence_proof_mode(
  repo: Path,
  *,
  base_sha: str,
  head_sha: str,
  source_sha: str,
  source_projection: PublicationSourceProjection | None = None,
  read_only: bool = False,
) -> str | None:
  """Return the exact proof that can back one pending equivalence witness."""
  if _change_is_subsumed(
    repo, base_sha, head_sha, source_sha, read_only=read_only,
  ):
    return "exact_tree"
  if source_projection is None:
    return None
  if _publication_projection_is_subsumed(
    repo,
    base_sha=base_sha,
    head_sha=head_sha,
    source_sha=source_sha,
    projection=source_projection,
    read_only=read_only,
  ):
    return source_projection.adapter
  return None


def _ref_blob(
  repo: Path,
  ref: str,
  path: str,
  *,
  read_only: bool = False,
) -> bytes | None:
  """Read one fixed-path blob without passing through a shell or worktree."""
  try:
    proc = subprocess.run(
      [
        "git", "-C", str(repo), "show", "--no-textconv", "--end-of-options",
        f"{ref}:{path}",
      ],
      capture_output=True,
      timeout=_GIT_TIMEOUT,
      check=False,
      env=_git_env(repo, read_only=read_only),
    )
  except (OSError, subprocess.SubprocessError):
    return None
  return proc.stdout if proc.returncode == 0 else None


def _project_publication_source(
  repo: Path,
  *,
  head_sha: str,
  source_sha: str,
  projection: PublicationSourceProjection,
) -> str | None:
  """Return an ephemeral commit with only the reviewed manifest projection.

  The public manifest blob replaces the installed blob only in unreachable Git
  objects used by the proof. Before doing that, parsed JSON must be identical
  after substituting the two adapter-owned fields. Thus formatting can be
  normalized by the reviewed public package, but an unrelated manifest field,
  source path, mode, or byte outside the manifest still fails the later
  exact-tree proof. No worktree, index, or ref is changed.
  """
  if (
    projection.adapter != _APP_MANIFEST_PUBLICATION_ADAPTER
    or projection.path != _APP_MANIFEST_PUBLICATION_PATH
    or tuple(field for field, _value in projection.fields)
       != _APP_MANIFEST_PUBLICATION_FIELDS
  ):
    return None
  reviewed_blob = _ref_blob(
    repo, head_sha, projection.path,
  )
  source_blob = _ref_blob(
    repo, source_sha, projection.path,
  )
  if reviewed_blob is None or source_blob is None:
    return None
  try:
    reviewed_manifest = json.loads(reviewed_blob.decode("utf-8"))
    source_manifest = json.loads(source_blob.decode("utf-8"))
  except (UnicodeDecodeError, json.JSONDecodeError):
    return None
  if not isinstance(reviewed_manifest, dict) or not isinstance(source_manifest, dict):
    return None
  projected_manifest = dict(source_manifest)
  for field, reviewed_value in projection.fields:
    if (
      reviewed_manifest.get(field) != reviewed_value
      or not isinstance(source_manifest.get(field), str)
      or not source_manifest[field]
    ):
      return None
    projected_manifest[field] = reviewed_value
  if projected_manifest != reviewed_manifest:
    return None

  try:
    tree_proc = subprocess.run(
      ["git", "-C", str(repo), "ls-tree", "-z", source_sha],
      capture_output=True,
      timeout=_GIT_TIMEOUT,
      check=False,
      env=_git_env(repo),
    )
    if tree_proc.returncode != 0:
      return None
    blob_proc = subprocess.run(
      ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
      input=reviewed_blob,
      capture_output=True,
      timeout=_GIT_TIMEOUT,
      check=False,
      env=_git_env(repo),
    )
    blob_oid = blob_proc.stdout.strip().decode("ascii")
    if blob_proc.returncode != 0 or not _HEX_OID.fullmatch(blob_oid):
      return None
    records = [record for record in tree_proc.stdout.split(b"\0") if record]
    matched = 0
    projected_records: list[bytes] = []
    target = projection.path.encode("utf-8")
    for record in records:
      header, separator, path = record.partition(b"\t")
      if not separator:
        return None
      if path == target:
        parts = header.split(b" ")
        if len(parts) != 3 or parts[0] != b"100644" or parts[1] != b"blob":
          return None
        header = b" ".join((parts[0], parts[1], blob_oid.encode("ascii")))
        matched += 1
      projected_records.append(header + b"\t" + path + b"\0")
    if matched != 1:
      return None
    projected_tree = subprocess.run(
      ["git", "-C", str(repo), "mktree", "-z"],
      input=b"".join(projected_records),
      capture_output=True,
      timeout=_GIT_TIMEOUT,
      check=False,
      env=_git_env(repo),
    )
    tree_oid = projected_tree.stdout.strip().decode("ascii")
    if projected_tree.returncode != 0 or not _HEX_OID.fullmatch(tree_oid):
      return None
    projected_commit = _run(
      repo,
      "commit-tree", tree_oid, "-p", source_sha,
      "-m", "Möbius publication source projection",
    ).stdout.strip().lower()
  except (OSError, subprocess.SubprocessError, UnicodeError):
    return None
  return projected_commit if _HEX_OID.fullmatch(projected_commit) else None


def _publication_projection_is_subsumed(
  repo: Path,
  *,
  base_sha: str,
  head_sha: str,
  source_sha: str,
  projection: PublicationSourceProjection,
  read_only: bool,
) -> bool:
  """Prove one projection without writing during a read-only check."""

  proof_repo = repo
  temporary: tempfile.TemporaryDirectory[str] | None = None
  try:
    if read_only:
      common_dir = _run(
        repo,
        "rev-parse", "--path-format=absolute", "--git-common-dir",
        check=False, read_only=True,
      )
      if common_dir.returncode != 0 or not common_dir.stdout.strip():
        return False
      object_dir = (Path(common_dir.stdout.strip()) / "objects").resolve()
      if not object_dir.is_dir() or "\n" in str(object_dir):
        return False
      temporary = tempfile.TemporaryDirectory(
        prefix="mobius-publication-projection-",
      )
      proof_repo = Path(temporary.name) / "repo"
      _run(Path(temporary.name), "init", "-q", str(proof_repo))
      alternates = proof_repo / ".git" / "objects" / "info" / "alternates"
      alternates.parent.mkdir(parents=True, exist_ok=True)
      alternates.write_text(f"{object_dir}\n", encoding="utf-8")
      if not all(
        _resolve_commit(proof_repo, oid) is not None
        for oid in (base_sha, head_sha, source_sha)
      ):
        return False

    projected_source = _project_publication_source(
      proof_repo,
      head_sha=head_sha,
      source_sha=source_sha,
      projection=projection,
    )
    return bool(
      projected_source is not None
      and _change_is_subsumed(
        proof_repo, base_sha, head_sha, projected_source,
      )
    )
  except (OSError, subprocess.SubprocessError, RuntimeError, UnicodeError):
    return False
  finally:
    if temporary is not None:
      temporary.cleanup()


def preview_pending_equivalent_change(
  source_dir: str | Path,
  *,
  base_sha: str,
  head_sha: str,
  source_sha: str,
  diff_sha256: str,
  review_source_dir: str | Path | None = None,
  source_projection: PublicationSourceProjection | None = None,
) -> str | None:
  """Prove a pending witness can be created without changing either repo.

  The proof always runs in a disposable repository. Even linked reviews share
  the installed source's object database, and ``git merge-tree --write-tree``
  writes tree objects there despite leaving refs, indexes, and worktrees alone.
  Read-only alternates expose the immutable source/review objects to that
  disposable repository without fetching or copying either history. Git still
  writes proof artifacts only to the disposable primary object database, so a
  review-status read cannot grow or otherwise mutate either durable checkout.
  """
  repo = Path(source_dir)
  review_repo = Path(review_source_dir) if review_source_dir else repo
  digest = str(diff_sha256 or "").lower()
  review_base = _resolve_commit(
    review_repo, base_sha, read_only=True,
  )
  review_head = _resolve_commit(
    review_repo, head_sha, read_only=True,
  )
  source = _resolve_commit(repo, source_sha, read_only=True)
  if (
    not _DIFF_SHA256.fullmatch(digest)
    or review_base is None
    or review_head is None
    or source is None
  ):
    return None
  reviewed_diff = _canonical_diff(
    review_repo, review_base, review_head, read_only=True,
  )
  if reviewed_diff is None or hashlib.sha256(reviewed_diff).hexdigest() != digest:
    return None

  try:
    with _disposable_proof_repository(repo, review_repo) as proof_repo:
      return _pending_equivalence_proof_mode(
        proof_repo,
        base_sha=review_base,
        head_sha=review_head,
        source_sha=source,
        source_projection=source_projection,
        read_only=True,
      )
  except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
    return None


@contextmanager
def _disposable_proof_repository(*source_repos: Path):
  """Expose immutable source objects while confining generated trees to tmp."""
  object_dirs: list[Path] = []
  for source_repo in source_repos:
    common = _run(
      source_repo, "rev-parse", "--path-format=absolute", "--git-common-dir",
      check=False, read_only=True,
    )
    if common.returncode != 0 or not common.stdout.strip():
      raise ValueError("source object database is unavailable")
    object_dir = (Path(common.stdout.strip()) / "objects").resolve()
    if not object_dir.is_dir() or "\n" in str(object_dir):
      raise ValueError("invalid source object database")
    if object_dir not in object_dirs:
      object_dirs.append(object_dir)
  with tempfile.TemporaryDirectory(prefix="mobius-equivalence-preview-") as tmp:
    proof_repo = Path(tmp) / "repo"
    _run(Path(tmp), "init", "-q", str(proof_repo), read_only=True)
    alternates = proof_repo / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(
      "".join(f"{object_dir}\n" for object_dir in object_dirs), encoding="utf-8",
    )
    yield proof_repo


def preview_source_resolution(
  source_dir: str | Path,
  *,
  base_sha: str,
  head_sha: str,
  source_sha: str,
  diff_sha256: str,
  review_source_dir: str | Path | None = None,
) -> SourceResolution | None:
  """Show an explicit conflict resolution, never infer source equivalence.

  Every nonconflicting reviewed change must already be in the local source.
  Only Git's reported conflict paths may differ from its conflict-marker tree.
  The returned full binary diff is evidence for an active source-chat review,
  not permission to publish. Generated merge objects never enter either source
  repository. Manifest projection and conflict resolution are not combined.
  """
  repo = Path(source_dir)
  review_repo = Path(review_source_dir) if review_source_dir else repo
  digest = str(diff_sha256 or "").lower()
  try:
    base = _resolve_commit(review_repo, base_sha, read_only=True)
    head = _resolve_commit(review_repo, head_sha, read_only=True)
    source = _resolve_commit(repo, source_sha, read_only=True)
    if not _DIFF_SHA256.fullmatch(digest) or None in (base, head, source):
      return None
    reviewed_diff = _canonical_diff(review_repo, base, head, read_only=True)
    if reviewed_diff is None or hashlib.sha256(reviewed_diff).hexdigest() != digest:
      return None
    with _disposable_proof_repository(repo, review_repo) as proof_repo:
      # Tree identities make conflict labels independent of the review commit
      # lifetime: a durable publication anchor can reproduce this evidence.
      merged = merge_refs(
        proof_repo, _tree_oid(proof_repo, head, read_only=True),
        _tree_oid(proof_repo, source, read_only=True),
        merge_base=base, read_only=True,
      )
      if (
        merged.status != "conflict" or not merged.conflict_paths
        or not merged.conflict_tree_oid
      ):
        return None
      paths = endpoint_diff_paths(
        proof_repo, merged.conflict_tree_oid, source, read_only=True,
      )
      if paths is None or not paths.issubset(merged.conflict_paths):
        return None
      resolution_diff = _canonical_diff(
        proof_repo, merged.conflict_tree_oid, source, read_only=True,
      )
      if resolution_diff is None:
        return None
      return SourceResolution(
        conflict_paths=tuple(sorted(set(merged.conflict_paths))),
        diff=resolution_diff,
        diff_sha256=hashlib.sha256(resolution_diff).hexdigest(),
      )
  except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
    return None


def _verified_review_source_commits(
  repo: Path,
  review_repo: Path,
  *,
  base_sha: str,
  head_sha: str,
  source_sha: str,
  diff_sha256: str,
  source_projection: PublicationSourceProjection | None = None,
  source_resolution_sha256: str | None = None,
) -> tuple[str, str, str, str] | None:
  """Import and verify the three immutable commits behind one review proof."""
  digest = str(diff_sha256 or "").lower()
  review_base = _resolve_commit(review_repo, base_sha)
  review_head = _resolve_commit(review_repo, head_sha)
  source = _resolve_commit(repo, source_sha)
  if (
    not _DIFF_SHA256.fullmatch(digest)
    or review_base is None
    or review_head is None
    or source is None
  ):
    return None
  reviewed_diff = _canonical_diff(review_repo, review_base, review_head)
  if reviewed_diff is None or hashlib.sha256(reviewed_diff).hexdigest() != digest:
    return None
  base = _resolve_commit(repo, review_base)
  head = _resolve_commit(repo, review_head)
  if base is None or head is None:
    try:
      # Import raw immutable objects only. No remote, branch, FETCH_HEAD, index,
      # or worktree is created or moved.
      _run(
        repo,
        "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
        str(review_repo), review_base, review_head,
      )
    except (OSError, subprocess.SubprocessError, RuntimeError):
      return None
    base = _resolve_commit(repo, review_base)
    head = _resolve_commit(repo, review_head)
  if base is None or head is None:
    return None
  if source_resolution_sha256 is not None:
    if source_projection is not None:
      return None
    resolution = preview_source_resolution(
      repo, base_sha=base, head_sha=head, source_sha=source, diff_sha256=digest,
    )
    if resolution is None or resolution.diff_sha256 != source_resolution_sha256:
      return None
    return base, head, source, "reviewed_source_resolution"
  proof_mode = _pending_equivalence_proof_mode(
    repo,
    base_sha=base,
    head_sha=head,
    source_sha=source,
    source_projection=source_projection,
  )
  if proof_mode is None:
    return None
  return base, head, source, proof_mode


def record_pending_equivalent_change(
  source_dir: str | Path,
  *,
  base_sha: str,
  head_sha: str,
  source_sha: str,
  diff_sha256: str,
  contribution_id: str,
  review_source_dir: str | Path | None = None,
  source_projection: PublicationSourceProjection | None = None,
) -> str | None:
  """Record a reviewed contribution only after proving its local provenance.

  The caller invokes this after the owner sends the reviewed PR.  The canonical
  diff hash must still match, and ``base..head`` must be fully present in the
  supplied local ``source_sha``.  A later local edit may overlap the contributed
  lines: the immutable source witness remains the causal proof as long as it is
  still an ancestor of the local update branch. ``review_source_dir`` is the
  durable review checkout when it does not share Git objects with the installed
  source (the standalone-clone app workflow). Only the two verified reviewed
  commits are imported into the installed repository; no branch or worktree is
  moved.
  """
  repo = Path(source_dir)
  review_repo = Path(review_source_dir) if review_source_dir else repo
  digest = str(diff_sha256 or "").lower()
  commits = _verified_review_source_commits(
    repo, review_repo,
    base_sha=base_sha,
    head_sha=head_sha,
    source_sha=source_sha,
    diff_sha256=digest,
    source_projection=source_projection,
  )
  if commits is None:
    return None
  base, head, source, proof_mode = commits
  return _write_equivalence_anchor(
    repo,
    prefix=_EQUIVALENCE_PENDING_PREFIX,
    base_sha=base,
    head_sha=head,
    source_sha=source,
    diff_sha256=digest,
    contribution_id=str(contribution_id or ""),
    upstream_sha=None,
    proof_mode=proof_mode,
    source_projection=(
      source_projection if proof_mode != "exact_tree" else None
    ),
  )


def _reviewed_source_ref(
  diff_sha256: str,
  contribution_id: str,
  reviewed_through_sha: str,
  review_identity_sha256: str | None = None,
) -> str:
  record_key = hashlib.sha256(contribution_id.encode("utf-8")).hexdigest()
  # A new review envelope needs its own immutable witness even when the source
  # has not moved. A sibling name avoids file/directory collisions with legacy
  # refs keyed only by reviewed_through_sha, including packed legacy refs.
  identity_suffix = f"-{review_identity_sha256}" if review_identity_sha256 else ""
  return (
    f"{_REVIEWED_SOURCE_PREFIX}/{record_key}/"
    f"{diff_sha256}/{reviewed_through_sha}{identity_suffix}"
  )


def _prune_reviewed_source_refs(
  repo: Path, contribution_id: str, *, keep: str,
) -> None:
  record_key = hashlib.sha256(contribution_id.encode("utf-8")).hexdigest()
  refs = _run(
    repo, "for-each-ref", "--format=%(refname)",
    f"{_REVIEWED_SOURCE_PREFIX}/{record_key}/", check=False,
  )
  if refs.returncode != 0:
    return
  for ref in (line.strip() for line in refs.stdout.splitlines() if line.strip()):
    if ref != keep:
      _run(repo, "update-ref", "-d", ref, check=False)


def record_prepublication_source_continuity(
  source_dir: str | Path,
  *,
  base_sha: str,
  head_sha: str,
  source_sha: str,
  reviewed_through_sha: str,
  diff_sha256: str,
  contribution_id: str,
  review_identity_sha256: str,
  review_source_dir: str | Path | None = None,
  source_projection: PublicationSourceProjection | None = None,
  source_resolution_sha256: str | None = None,
) -> str | None:
  """Persist an active-agent review of an overlapping local source advance.

  The original ``source_sha`` still has to contain the reviewed change by the
  existing exact-tree proof. This witness does not ask Git to infer semantics
  from a later conflict resolution. Instead, it records that the source-chat
  agent reviewed the exact committed advance through ``reviewed_through_sha``.
  Publication may use it only until another reviewed path changes. An explicit
  ``source_resolution_sha256`` instead binds a reviewed conflict resolution at
  the current source: it never relaxes the automatic exact-tree proof.
  """
  repo = Path(source_dir)
  review_repo = Path(review_source_dir) if review_source_dir else repo
  digest = str(diff_sha256 or "").lower()
  contribution = str(contribution_id or "")
  review_identity = str(review_identity_sha256 or "").lower()
  if (
    not contribution
    or len(contribution) > 128
    or not _DIFF_SHA256.fullmatch(digest)
    or not _DIFF_SHA256.fullmatch(review_identity)
  ):
    return None
  commits = _verified_review_source_commits(
    repo, review_repo,
    base_sha=base_sha,
    head_sha=head_sha,
    source_sha=(
      reviewed_through_sha if source_resolution_sha256 is not None else source_sha
    ),
    diff_sha256=digest,
    source_projection=source_projection,
    source_resolution_sha256=source_resolution_sha256,
  )
  if commits is None:
    return None
  base, head, _verified_source, _proof_mode = commits
  source = _resolve_commit(repo, source_sha)
  reviewed_through = _resolve_commit(repo, reviewed_through_sha)
  if source is None or reviewed_through is None or ref_is_ancestor(
    repo, source, reviewed_through,
  ) is not True:
    return None
  advance_diff = _canonical_diff(repo, source, reviewed_through)
  reviewed_paths = endpoint_diff_paths(repo, base, head)
  overlap = commit_range_touches_paths(
    repo, source, reviewed_through, reviewed_paths or (),
  )
  if (
    advance_diff is None or not reviewed_paths
    or (source_resolution_sha256 is None and overlap is not True)
  ):
    return None
  metadata = {
    "version": _REVIEWED_SOURCE_VERSION,
    "kind": "reviewed_source_continuity",
    "base_sha": base,
    "head_sha": head,
    "source_sha": source,
    "reviewed_through_sha": reviewed_through,
    "diff_sha256": digest,
    "contribution_id": contribution,
    "review_identity_sha256": review_identity,
    "source_advance_diff_sha256": hashlib.sha256(advance_diff).hexdigest(),
  }
  if source_projection is not None:
    metadata["source_projection"] = source_projection.as_dict()
  if source_resolution_sha256 is not None:
    metadata["source_resolution_sha256"] = source_resolution_sha256
  ref = _reviewed_source_ref(
    digest, contribution, reviewed_through, review_identity,
  )
  existing = _read_prepublication_source_continuity(repo, ref)
  if existing is not None and (
    existing.base_sha == base
    and existing.head_sha == head
    and existing.source_sha == source
    and existing.reviewed_through_sha == reviewed_through
    and existing.diff_sha256 == digest
    and existing.contribution_id == contribution
    and existing.review_identity_sha256 == review_identity
    and existing.source_projection == source_projection
    and existing.source_resolution_sha256 == source_resolution_sha256
  ):
    _prune_reviewed_source_refs(repo, contribution, keep=ref)
    return ref
  if _resolve_commit(repo, ref) is not None:
    return None
  tree = _tree_oid(repo, head)
  if tree is None:
    return None
  try:
    anchor = _run(
      repo, "commit-tree", tree, "-p", base,
      "-m", json.dumps(metadata, sort_keys=True, separators=(",", ":")),
    ).stdout.strip()
    if not _HEX_OID.fullmatch(anchor):
      return None
    created = _run(
      repo, "update-ref", ref, anchor, "0" * len(anchor), check=False,
    )
  except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
    return None
  if created.returncode == 0:
    _prune_reviewed_source_refs(repo, contribution, keep=ref)
    return ref
  # A restarted or concurrent exact retry may have lost the create-only ref
  # race after another host request wrote the same immutable anchor. Accept
  # only that byte-identical winner; arbitrary pre-existing refs still fail.
  winner = _read_prepublication_source_continuity(repo, ref)
  if winner is not None and (
    winner.base_sha == base
    and winner.head_sha == head
    and winner.source_sha == source
    and winner.reviewed_through_sha == reviewed_through
    and winner.diff_sha256 == digest
    and winner.contribution_id == contribution
    and winner.review_identity_sha256 == review_identity
    and winner.source_projection == source_projection
    and winner.source_resolution_sha256 == source_resolution_sha256
  ):
    _prune_reviewed_source_refs(repo, contribution, keep=ref)
    return ref
  return None


def _read_prepublication_source_continuity(
  repo: Path,
  ref: str,
) -> ReviewedSourceContinuity | None:
  anchor = _resolve_commit(repo, ref, read_only=True)
  if anchor is None:
    return None
  parents_result = _run(
    repo, "rev-list", "--parents", "-n", "1", anchor,
    check=False, read_only=True,
  )
  parents = parents_result.stdout.split()
  if parents_result.returncode != 0 or len(parents) != 2:
    return None
  try:
    message = _run(
      repo, "show", "-s", "--format=%B", anchor, read_only=True,
      check=False,
    )
    if message.returncode != 0:
      return None
    metadata = json.loads(message.stdout.strip())
  except (TypeError, ValueError):
    return None
  if (
    not isinstance(metadata, dict)
    or metadata.get("version") != _REVIEWED_SOURCE_VERSION
    or metadata.get("kind") != "reviewed_source_continuity"
  ):
    return None
  base = str(metadata.get("base_sha") or "").lower()
  head = str(metadata.get("head_sha") or "").lower()
  source = str(metadata.get("source_sha") or "").lower()
  reviewed_through = str(
    metadata.get("reviewed_through_sha") or ""
  ).lower()
  digest = str(metadata.get("diff_sha256") or "").lower()
  contribution = str(metadata.get("contribution_id") or "")
  review_identity = str(
    metadata.get("review_identity_sha256") or ""
  ).lower()
  advance_digest = str(
    metadata.get("source_advance_diff_sha256") or ""
  ).lower()
  resolution_digest = metadata.get("source_resolution_sha256")
  raw_projection = metadata.get("source_projection")
  try:
    source_projection = publication_source_projection(raw_projection)
  except ValueError:
    return None
  if (
    not all(_HEX_OID.fullmatch(oid) for oid in (
      base, head, source, reviewed_through,
    ))
    or not _DIFF_SHA256.fullmatch(digest)
    or not _DIFF_SHA256.fullmatch(review_identity)
    or not _DIFF_SHA256.fullmatch(advance_digest)
    or (resolution_digest is not None and (
      not isinstance(resolution_digest, str)
      or not _DIFF_SHA256.fullmatch(resolution_digest)
      or source_projection is not None
    ))
    or parents[1].lower() != base
    or ref not in {
      _reviewed_source_ref(digest, contribution, reviewed_through, review_identity),
      # Previously stored witnesses still bind identity in their checked payload.
      _reviewed_source_ref(digest, contribution, reviewed_through),
    }
    or _resolve_commit(repo, head, read_only=True) != head
    or _tree_oid(repo, anchor, read_only=True)
       != _tree_oid(repo, head, read_only=True)
    or _resolve_commit(repo, source, read_only=True) != source
    or _resolve_commit(repo, reviewed_through, read_only=True) != reviewed_through
  ):
    return None
  reviewed_diff = _canonical_diff(repo, base, head, read_only=True)
  advance_diff = _canonical_diff(repo, source, reviewed_through, read_only=True)
  if (
    reviewed_diff is None
    or hashlib.sha256(reviewed_diff).hexdigest() != digest
    or advance_diff is None
    or hashlib.sha256(advance_diff).hexdigest() != advance_digest
    or ref_is_ancestor(repo, source, reviewed_through) is not True
  ):
    return None
  if resolution_digest is not None:
    resolution = preview_source_resolution(
      repo, base_sha=base, head_sha=head, source_sha=reviewed_through,
      diff_sha256=digest,
    )
    if resolution is None or resolution.diff_sha256 != resolution_digest:
      return None
  elif (
    preview_pending_equivalent_change(
      repo, base_sha=base, head_sha=head, source_sha=source,
      diff_sha256=digest, source_projection=source_projection,
    ) is None
    or commit_range_touches_paths(
      repo, source, reviewed_through,
      endpoint_diff_paths(repo, base, head, read_only=True) or (), read_only=True,
    ) is not True
  ):
    return None
  return ReviewedSourceContinuity(
    ref=ref,
    base_sha=base,
    head_sha=head,
    source_sha=source,
    reviewed_through_sha=reviewed_through,
    diff_sha256=digest,
    contribution_id=contribution,
    review_identity_sha256=review_identity,
    source_projection=source_projection,
    source_resolution_sha256=resolution_digest,
  )


def prepublication_source_continuity(
  source_dir: str | Path,
  *,
  base_sha: str,
  head_sha: str,
  source_sha: str,
  current_source_sha: str,
  diff_sha256: str,
  contribution_id: str,
  review_identity_sha256: str,
  source_projection: PublicationSourceProjection | None = None,
) -> ReviewedSourceContinuity | None:
  """Return a matching continuity witness for the current installed source.

  The witness is useful only for an exact reviewed identity. A source rewind
  loses ancestry; a later edit on any reviewed path—including an explicit or
  manual revert—invalidates it until an active agent reviews the new advance.
  """
  repo = Path(source_dir)
  digest = str(diff_sha256 or "").lower()
  contribution = str(contribution_id or "")
  review_identity = str(review_identity_sha256 or "").lower()
  current = _resolve_commit(repo, current_source_sha, read_only=True)
  if (
    current is None
    or not contribution
    or len(contribution) > 128
    or not _DIFF_SHA256.fullmatch(digest)
    or not _DIFF_SHA256.fullmatch(review_identity)
  ):
    return None
  record_key = hashlib.sha256(contribution.encode("utf-8")).hexdigest()
  prefix = f"{_REVIEWED_SOURCE_PREFIX}/{record_key}/{digest}/"
  refs = _run(
    repo, "for-each-ref", "--format=%(refname)", prefix,
    check=False, read_only=True,
  )
  if refs.returncode != 0:
    return None
  reviewed_paths = endpoint_diff_paths(
    repo, base_sha, head_sha, read_only=True,
  )
  if not reviewed_paths:
    return None
  for ref in sorted(
    (line.strip() for line in refs.stdout.splitlines() if line.strip()),
    reverse=True,
  ):
    witness = _read_prepublication_source_continuity(repo, ref)
    if witness is None or (
      witness.base_sha != base_sha
      or witness.head_sha != head_sha
      or witness.source_sha != source_sha
      or witness.diff_sha256 != digest
      or witness.contribution_id != contribution
      or witness.review_identity_sha256 != review_identity
      or witness.source_projection != source_projection
      or ref_is_ancestor(repo, witness.reviewed_through_sha, current) is not True
    ):
      continue
    later_touches_reviewed = commit_range_touches_paths(
      repo,
      witness.reviewed_through_sha,
      current,
      reviewed_paths,
      read_only=True,
    )
    if later_touches_reviewed is False:
      return witness
  return None


def record_reviewed_source_equivalence(
  source_dir: str | Path, *, witness: ReviewedSourceContinuity,
) -> str | None:
  """Publish an exact, still-current explicit source-resolution witness.

  The pending anchor retains the review tree and resolution digest so landed
  provenance remains independently verifiable after review cleanup. Its
  local source is the reviewed resolution, never the older unproven snapshot.
  """
  repo = Path(source_dir)
  current = prepublication_source_continuity(
    repo, base_sha=witness.base_sha, head_sha=witness.head_sha,
    source_sha=witness.source_sha, current_source_sha="HEAD",
    diff_sha256=witness.diff_sha256, contribution_id=witness.contribution_id,
    review_identity_sha256=witness.review_identity_sha256,
    source_projection=witness.source_projection,
  )
  if (
    current != witness or witness.source_resolution_sha256 is None
    or witness.source_projection is not None
  ):
    return None
  return _write_equivalence_anchor(
    repo, prefix=_EQUIVALENCE_PENDING_PREFIX,
    base_sha=witness.base_sha, head_sha=witness.head_sha,
    source_sha=witness.reviewed_through_sha, diff_sha256=witness.diff_sha256,
    contribution_id=witness.contribution_id, upstream_sha=None,
    proof_mode="reviewed_source_resolution",
    source_resolution_sha256=witness.source_resolution_sha256,
    source_resolution_captured_sha=witness.source_sha,
  )


def discard_prepublication_source_continuity(
  source_dir: str | Path,
  contribution_id: str,
) -> None:
  """Retire one record's private continuity witnesses after publication."""
  repo = Path(source_dir)
  record_key = hashlib.sha256(str(contribution_id or "").encode("utf-8")).hexdigest()
  refs = _run(
    repo, "for-each-ref", "--format=%(refname)",
    f"{_REVIEWED_SOURCE_PREFIX}/{record_key}/", check=False,
  )
  if refs.returncode != 0:
    return
  for ref in (line.strip() for line in refs.stdout.splitlines() if line.strip()):
    _run(repo, "update-ref", "-d", ref, check=False)


def _read_equivalent_change(repo: Path, ref: str) -> EquivalentChange | None:
  anchor = _resolve_commit(repo, ref)
  if anchor is None:
    return None
  parent_line = _run(
    repo, "rev-list", "--parents", "-n", "1", anchor, check=False,
  ).stdout.split()
  if len(parent_line) != 2:
    return None
  try:
    metadata = json.loads(
      _run(repo, "show", "-s", "--format=%B", anchor).stdout.strip()
    )
  except (ValueError, TypeError):
    return None
  if not isinstance(metadata, dict) or metadata.get("version") != _EQUIVALENCE_VERSION:
    return None
  digest = str(metadata.get("diff_sha256") or "").lower()
  source = str(metadata.get("source_sha") or "").lower()
  upstream_raw = metadata.get("upstream_sha")
  upstream = str(upstream_raw).lower() if upstream_raw else None
  proof_mode = str(metadata.get("proof_mode") or "exact_tree")
  resolution_digest = metadata.get("source_resolution_sha256")
  resolution_captured = metadata.get("source_resolution_captured_sha")
  raw_projection = metadata.get("source_projection")
  try:
    source_projection = publication_source_projection(raw_projection)
  except ValueError:
    return None
  if (
    not _DIFF_SHA256.fullmatch(digest)
    or not ref.endswith(f"/{digest}")
    or not _HEX_OID.fullmatch(source)
    or (upstream is not None and not _HEX_OID.fullmatch(upstream))
    or (
      proof_mode == "exact_tree" and source_projection is not None
    )
    or (
      proof_mode not in {"exact_tree", "reviewed_source_resolution"}
      and (
        source_projection is None
        or proof_mode != source_projection.adapter
      )
    )
  ):
    return None
  if proof_mode == "reviewed_source_resolution":
    if (
      source_projection is not None
      or not isinstance(resolution_digest, str)
      or not _DIFF_SHA256.fullmatch(resolution_digest)
      or not isinstance(resolution_captured, str)
      or not _HEX_OID.fullmatch(resolution_captured)
      or ref_is_ancestor(repo, resolution_captured, source) is not True
    ):
      return None
    resolution = preview_source_resolution(
      repo, base_sha=parent_line[1].lower(), head_sha=anchor,
      source_sha=source, diff_sha256=digest,
    )
    if resolution is None or resolution.diff_sha256 != resolution_digest:
      return None
  elif resolution_digest is not None or resolution_captured is not None:
    return None
  return EquivalentChange(
    ref=ref,
    anchor_sha=anchor,
    base_sha=parent_line[1].lower(),
    source_sha=source,
    upstream_sha=upstream,
    diff_sha256=digest,
    contribution_id=str(metadata.get("contribution_id") or "")[:128],
    proof_mode=proof_mode,
    source_projection=source_projection,
    source_resolution_sha256=resolution_digest,
    source_resolution_captured_sha=resolution_captured,
  )


def mark_equivalent_change_landed(
  source_dir: str | Path,
  diff_sha256: str,
  *,
  upstream_sha: str | None = None,
) -> str | None:
  """Promote one owner-sent reviewed change after its PR is confirmed merged."""
  repo = Path(source_dir)
  digest = str(diff_sha256 or "").lower()
  if not _DIFF_SHA256.fullmatch(digest):
    return None
  pending_ref = _equivalence_ref(_EQUIVALENCE_PENDING_PREFIX, digest)
  pending = _read_equivalent_change(repo, pending_ref)
  if pending is None:
    return None
  upstream = _resolve_commit(repo, upstream_sha) if upstream_sha else None
  # The merge commit may not have been fetched into this checkout yet.  Keep a
  # validated hex oid as provenance; the updater verifies ancestry only after
  # it fetches the target that should contain it.
  if upstream is None and upstream_sha:
    candidate = str(upstream_sha).lower()
    upstream = candidate if _HEX_OID.fullmatch(candidate) else None
  ref = _write_equivalence_anchor(
    repo,
    prefix=_EQUIVALENCE_LANDED_PREFIX,
    base_sha=pending.base_sha,
    head_sha=pending.anchor_sha,
    source_sha=pending.source_sha,
    diff_sha256=pending.diff_sha256,
    contribution_id=pending.contribution_id,
    upstream_sha=upstream,
    proof_mode=pending.proof_mode,
    source_projection=pending.source_projection,
    source_resolution_sha256=pending.source_resolution_sha256,
    source_resolution_captured_sha=pending.source_resolution_captured_sha,
  )
  _run(repo, "update-ref", "-d", pending_ref, check=False)
  return ref


def discard_pending_equivalent_change(
  source_dir: str | Path, diff_sha256: str,
) -> None:
  digest = str(diff_sha256 or "").lower()
  if _DIFF_SHA256.fullmatch(digest):
    _run(
      Path(source_dir), "update-ref", "-d",
      _equivalence_ref(_EQUIVALENCE_PENDING_PREFIX, digest), check=False,
    )


def _equivalent_changes(repo: Path, prefix: str) -> list[EquivalentChange]:
  proc = _run(
    repo, "for-each-ref", "--format=%(refname)",
    f"{prefix}/", check=False,
  )
  if proc.returncode != 0:
    return []
  changes = []
  for ref in sorted(line.strip() for line in proc.stdout.splitlines() if line.strip()):
    change = _read_equivalent_change(repo, ref)
    if change is not None:
      changes.append(change)
  return changes


def _landed_equivalent_changes(repo: Path) -> list[EquivalentChange]:
  return _equivalent_changes(repo, _EQUIVALENCE_LANDED_PREFIX)


def verify_landed_equivalent_change(
  source_dir: str | Path,
  *,
  diff_sha256: str,
  contribution_id: str,
  base_sha: str,
  head_sha: str,
  source_sha: str,
  upstream_sha: str,
  local: str = LOCAL_BRANCH,
) -> bool:
  """Prove one exact reviewed contribution exists locally and upstream.

  The landed equivalence ref is the immutable witness written after the owner
  sent a reviewed PR.  A publication handoff may reuse it only when every
  record-bound identity still matches, its local source witness remains in the
  installed app's history, and GitHub's actual merge commit contains the exact
  reviewed delta.  This is deliberately stronger than checking a PR state or
  trusting mutable contribution JSON on its own.
  """
  repo = Path(source_dir)
  digest = str(diff_sha256 or "").lower()
  if not _DIFF_SHA256.fullmatch(digest):
    return False
  ref = _equivalence_ref(_EQUIVALENCE_LANDED_PREFIX, digest)
  change = _read_equivalent_change(repo, ref)
  if change is None:
    return False

  expected_base = _resolve_commit(repo, base_sha)
  expected_head = _resolve_commit(repo, head_sha)
  expected_source = _resolve_commit(repo, source_sha)
  expected_upstream = _resolve_commit(repo, upstream_sha)
  local_tip = _resolve_commit(repo, local)
  if None in (
    expected_base, expected_head, expected_source, expected_upstream, local_tip,
  ):
    return False
  if (
    change.contribution_id != str(contribution_id or "")[:128]
    or change.base_sha != expected_base
    or (
      change.source_resolution_captured_sha
      if change.proof_mode == "reviewed_source_resolution" else change.source_sha
    ) != expected_source
    or change.upstream_sha != expected_upstream
    or _tree_oid(repo, change.anchor_sha) != _tree_oid(repo, expected_head)
    or ref_is_ancestor(repo, change.source_sha, local_tip) is not True
  ):
    return False
  return _change_landed_in_target(repo, change, expected_upstream)


def carry_equivalent_change_sources(
  source_dir: str | Path, old_local: str, new_local: str,
) -> int:
  """Carry local witnesses across the app model's intentional replay rewrite.

  ``commit_replay`` and resolved-conflict finalization preserve the accepted
  source tree but replace its ancestry with the new upstream tip. Any pending
  or not-yet-integrated landed contribution whose source witness was reachable
  from the old local tip therefore needs the new replay commit as its witness
  only if the exact reviewed delta is still present there. This rejects the
  explicit take-upstream path when it drops those edits. Anchors unrelated to
  ``old_local`` remain untouched.
  """
  repo = Path(source_dir)
  old = _resolve_commit(repo, old_local)
  new = _resolve_commit(repo, new_local)
  if old is None or new is None:
    return 0
  carried = 0
  for prefix in (_EQUIVALENCE_PENDING_PREFIX, _EQUIVALENCE_LANDED_PREFIX):
    for change in _equivalent_changes(repo, prefix):
      if change.proof_mode == "reviewed_source_resolution":
        # A source-history rewrite requires a fresh explicit review, not an
        # automatic re-attestation of the old conflict resolution.
        continue
      if ref_is_ancestor(repo, change.source_sha, old) is not True:
        continue
      reviewed_delta_preserved = _pending_equivalence_proof_mode(
        repo,
        base_sha=change.base_sha,
        head_sha=change.anchor_sha,
        source_sha=new,
        source_projection=change.source_projection,
      ) is not None
      if not reviewed_delta_preserved:
        continue
      _write_equivalence_anchor(
        repo,
        prefix=prefix,
        base_sha=change.base_sha,
        head_sha=change.anchor_sha,
        source_sha=new,
        diff_sha256=change.diff_sha256,
        contribution_id=change.contribution_id,
        upstream_sha=change.upstream_sha,
        proof_mode=change.proof_mode,
        source_projection=change.source_projection,
      )
      carried += 1
  return carried


def _change_landed_in_target(
  repo: Path, change: EquivalentChange, target: str,
) -> bool:
  if change.upstream_sha:
    return bool(
      ref_is_ancestor(repo, change.upstream_sha, target) is True
      and _change_is_subsumed(
        repo, change.base_sha, change.anchor_sha, change.upstream_sha,
      )
    )
  return _change_is_subsumed(
    repo, change.base_sha, change.anchor_sha, target,
  )


def merge_with_equivalent_changes(
  source_dir: str | Path, local: str, upstream: str,
) -> MergeResult | None:
  """Resolve a duplicate-contribution conflict from proven shared changes.

  Start at Git's real merge base.  Each applicable reviewed anchor contributes
  only its ``reviewed-base..reviewed-head`` delta to a synthetic shared TREE.
  The anchor is applicable only when its immutable local source witness is an
  ancestor of ``local`` and its GitHub merge witness is an ancestor of
  ``upstream`` *and still contains the exact reviewed delta* (or strict
  tree-subsumption proves the latter directly against the target when GitHub
  could not provide a merge oid). A final off-tree merge with that semantic
  base returns either a clean tree or the strictly reduced residual conflict
  set plus the semantic base needed to materialize it. No ref or working tree
  moves here; callers keep their existing rollback transaction.

  Anchors are applied in deterministic passes.  A dependent anchor that cannot
  merge onto the current shared tree is deferred until another anchor supplies
  its prerequisite.  Any anchor still unprovable is simply omitted; the final
  merge then remains conflicted and the owner-gated agent fallback owns only
  those residual paths.
  """
  repo = Path(source_dir)
  real_base = _run(repo, "merge-base", local, upstream, check=False)
  if real_base.returncode != 0 or not real_base.stdout.strip():
    return None
  shared_tree = _tree_oid(repo, real_base.stdout.strip())
  if shared_tree is None:
    return None

  pending = [
    change for change in _landed_equivalent_changes(repo)
    if ref_is_ancestor(repo, change.source_sha, local) is True
    and _change_landed_in_target(repo, change, upstream)
  ]
  if not pending:
    return None

  applied: list[EquivalentChange] = []
  while pending:
    deferred: list[EquivalentChange] = []
    progressed = False
    for change in pending:
      try:
        combined = merge_refs(
          repo,
          shared_tree,
          change.anchor_sha,
          merge_base=change.base_sha,
        )
      except (OSError, subprocess.SubprocessError, RuntimeError):
        deferred.append(change)
        continue
      if combined.status != "clean" or not combined.merged_tree_oid:
        deferred.append(change)
        continue
      shared_tree = combined.merged_tree_oid
      applied.append(change)
      progressed = True
    if not progressed:
      break
    pending = deferred

  if not applied:
    return None
  try:
    merged = merge_refs(repo, local, upstream, merge_base=shared_tree)
  except (OSError, subprocess.SubprocessError, RuntimeError):
    return None
  if merged.status not in {"clean", "conflict"}:
    return None
  if merged.status == "clean" and not merged.merged_tree_oid:
    return None
  merged.merge_base_oid = shared_tree
  merged.equivalent_change_refs = tuple(change.ref for change in applied)
  merged.reconciliation = describe_reconciliation(
    repo,
    shared_tree,
    upstream,
    local=local,
    conflict_paths=merged.conflict_paths,
    proven_changes=applied,
  )
  return merged


def preview_reconciliation(
  source_dir: str | Path,
  local: str,
  upstream: str,
) -> ReconciliationReceipt:
  """Classify two refs with the same semantic proof used by real updates.

  This is the read-only inventory seam for both platform and app source maps.
  It never fetches, moves a ref, or touches the worktree. Proven contribution
  anchors replace Git's historical base when available; otherwise the ordinary
  merge base and conflict verdict remain authoritative.
  """
  repo = Path(source_dir)
  equivalent = merge_with_equivalent_changes(repo, local, upstream)
  if equivalent is not None:
    return equivalent.reconciliation
  base = _run(repo, "merge-base", local, upstream, check=False).stdout.strip()
  if not base:
    try:
      unrelated = merge_refs(
        repo, local, upstream, allow_unrelated_histories=True,
      )
    except (OSError, subprocess.SubprocessError, RuntimeError):
      return ReconciliationReceipt()
    return ReconciliationReceipt(
      unresolved_conflict_paths=tuple(sorted(unrelated.conflict_paths)),
    )
  try:
    ordinary = merge_refs(repo, local, upstream)
    conflicts = ordinary.conflict_paths
  except (OSError, subprocess.SubprocessError, RuntimeError):
    conflicts = ()
  return describe_reconciliation(
    repo,
    base,
    upstream,
    local=local,
    conflict_paths=conflicts,
  )


@dataclass(frozen=True)
class SupersededDraft:
  ref: str
  anchor_sha: str  # tree = the draft; its one parent is the draft's base
  base_sha: str
  upstream_sha: str  # the merge commit that carries the reviewed version
  draft_ref: str | None


def mark_draft_superseded(
  source_dir: str | Path,
  *,
  contribution_id: str,
  base_sha: str,
  head_sha: str,
  upstream_sha: str,
  draft_ref: str | None = None,
) -> str | None:
  """Record that a merged contribution supersedes the draft live still holds.

  ``base..head`` is the draft Contribute proved the live source contained
  before review revised it. ``upstream_sha`` is GitHub's merge commit; it need
  not be fetched yet, because the updater only acts once its release contains
  it. Returns the ref, or None when the draft commits are unavailable.
  """
  repo = Path(source_dir)
  base = _resolve_commit(repo, base_sha)
  head = _resolve_commit(repo, head_sha)
  upstream = str(upstream_sha or "").strip().lower()
  tree = _tree_oid(repo, head) if head else None
  if (
    base is None or tree is None or not _HEX_OID.fullmatch(upstream)
    or ref_is_ancestor(repo, base, head) is not True
  ):
    return None
  pin = draft_ref if (
    isinstance(draft_ref, str) and draft_ref.startswith(_CONTRIBUTION_DRAFT_PREFIX)
  ) else None
  metadata = {
    "version": _EQUIVALENCE_VERSION,
    "contribution_id": str(contribution_id)[:128],
    "upstream_sha": upstream,
    "draft_ref": pin,
  }
  anchor = _run(
    repo, "commit-tree", tree, "-p", base,
    "-m", json.dumps(metadata, sort_keys=True, separators=(",", ":")),
  ).stdout.strip()
  key = hashlib.sha256(str(contribution_id).encode("utf-8")).hexdigest()
  ref = f"{_SUPERSEDED_DRAFT_PREFIX}/{key}"
  _run(repo, "update-ref", ref, anchor)
  return ref


def _superseded_drafts(repo: Path) -> list[SupersededDraft]:
  listing = _run(
    repo, "for-each-ref", "--format=%(refname) %(objectname)",
    _SUPERSEDED_DRAFT_PREFIX, check=False,
  )
  drafts: list[SupersededDraft] = []
  for line in listing.stdout.splitlines():
    ref, _, anchor = line.partition(" ")
    parents = _run(
      repo, "rev-list", "--parents", "-n", "1", anchor, check=False,
    ).stdout.split()
    message = _run(repo, "log", "-1", "--format=%B", anchor, check=False).stdout
    try:
      metadata = json.loads(message)
    except ValueError:
      continue
    upstream = str(metadata.get("upstream_sha") or "") if isinstance(metadata, dict) else ""
    if len(parents) != 2 or not _HEX_OID.fullmatch(upstream):
      continue
    drafts.append(SupersededDraft(
      ref=ref, anchor_sha=anchor, base_sha=parents[1], upstream_sha=upstream,
      draft_ref=metadata.get("draft_ref") or None,
    ))
  return drafts


def without_superseded_drafts(
  source_dir: str | Path, local: str, upstream: str,
  *, protected: Iterable[str] = (),
) -> str:
  """Return ``local``, or a commit on it with merged contributions' drafts set aside.

  A draft is set aside only when all hold: this update newly brings the merge
  that superseded it (``upstream`` contains it and the shared base does not;
  once integrated, removing a draft a resolver kept would revert the merged
  version); the local tree provably contains the exact draft; and removing it
  merges cleanly (local work built on the draft stays with the resolver, as
  before); removing it changes none of the ``protected`` paths, which the
  owner chose to keep local (an ``owner-choice:local`` unit touched them);
  and ``upstream`` still differs from the shared base on every restored path
  the merging commit changed (a merge this update also reverts would
  otherwise just drop the local draft). A restored path the merge left alone
  is a file reviewers dropped from the pull request; it returns to the base
  as before.
  Upstream's version then arrives from the release side of the merge.
  The result's parent is ``local``, so merge bases and other witnesses hold.
  """
  repo = Path(source_dir)
  tip = _resolve_commit(repo, local)
  tree = _tree_oid(repo, tip) if tip else None
  shared = _run(repo, "merge-base", local, upstream, check=False).stdout.strip()
  if tip is None or tree is None or not shared:
    return local
  protected = set(protected)
  set_aside = 0
  for draft in _superseded_drafts(repo):
    if (
      ref_is_ancestor(repo, draft.upstream_sha, upstream) is not True
      or ref_is_ancestor(repo, draft.upstream_sha, shared) is True
      or not _change_is_subsumed(repo, draft.base_sha, draft.anchor_sha, tree)
    ):
      continue
    try:
      removed = merge_refs(
        repo, tree, draft.base_sha, merge_base=draft.anchor_sha,
      )
    except (OSError, subprocess.SubprocessError, RuntimeError):
      continue
    if removed.status != "clean" or not removed.merged_tree_oid:
      continue
    changed = _tree_changes(repo, tree, removed.merged_tree_oid)
    if protected and not protected.isdisjoint(changed):
      continue
    if _release_reverts_merge(repo, draft.upstream_sha, shared, upstream, changed):
      continue
    tree = removed.merged_tree_oid
    set_aside += 1
  if not set_aside:
    return local
  return _run(
    repo, "commit-tree", tree, "-p", tip,
    "-m", "Set aside local drafts of merged contributions",
  ).stdout.strip()


def _release_reverts_merge(
  repo: Path, merge: str, shared: str, upstream: str, paths: Iterable[str],
) -> bool:
  """Whether ``upstream`` leaves a path of ``paths`` that ``merge`` itself
  changed exactly as ``shared`` had it: the merge was reverted or undone.

  Judged per path, and only on the merging commit's own paths
  (``merge^..merge``), as ``without_retired_units`` does.
  """
  merged = (endpoint_diff_paths(repo, f"{merge}^", merge) or set()) & set(paths)
  if not merged:
    return False
  released = _path_entries(repo, upstream, merged)
  based = _path_entries(repo, shared, merged)
  return any(released.get(path) == based.get(path) for path in merged)


def retire_landed_equivalent_changes(
  source_dir: str | Path, integrated_upstream: str,
) -> int:
  """Drop landed anchors made obsolete by a successfully integrated target.

  A superseded draft retires with its pin once the integrated release carries
  its merge: from then on the merge is in every shared base, so the draft can
  never be set aside again (a draft a resolver kept is ordinary local work).
  """
  repo = Path(source_dir)
  retired = 0
  for change in _landed_equivalent_changes(repo):
    if _change_landed_in_target(repo, change, integrated_upstream):
      proc = _run(repo, "update-ref", "-d", change.ref, check=False)
      retired += int(proc.returncode == 0)
  for draft in _superseded_drafts(repo):
    if ref_is_ancestor(repo, draft.upstream_sha, integrated_upstream) is True:
      for ref in (draft.ref, draft.draft_ref):
        if ref:
          _run(repo, "update-ref", "-d", ref, check=False)
      retired += 1
  # Likewise a merged-contribution witness: once its merge is integrated it is
  # in every shared base and can never retire anything again; nor can one for
  # another repository.
  for contribution in _merged_contributions(repo):
    if not contribution.for_platform_release or ref_is_ancestor(
      repo, contribution.merge_sha, integrated_upstream,
    ) is True:
      proc = _run(repo, "update-ref", "-d", contribution.ref, check=False)
      retired += int(proc.returncode == 0)
  return retired


def restore_upstream_ref(source_dir: str | Path, expected_sha: str | None) -> bool:
  """Put installer-owned ``upstream`` back on the DB-recorded commit.

  ``install_from_manifest`` stores the pristine upstream commit in the App row
  inside the SQL transaction, while the git ref lives outside that transaction.
  If an update attempt advances the ref and then rolls the DB transaction back,
  a retry must trust the DB row and restore the ref before doing another merge.

  Returns True when the ref moved, False when it was already correct or the
  expected commit is unavailable in the repo.
  """
  if not expected_sha or not is_repo(source_dir):
    return False
  repo = Path(source_dir)
  exists = _run(repo, "cat-file", "-e", f"{expected_sha}^{{commit}}",
                check=False)
  if exists.returncode != 0:
    return False
  current = _run(
    repo, "rev-parse", "--verify", UPSTREAM_BRANCH, check=False,
  )
  if current.returncode == 0 and current.stdout.strip() == expected_sha:
    return False
  _run(repo, "update-ref", f"refs/heads/{UPSTREAM_BRANCH}", expected_sha)
  return True


def replace_upstream_ref(
  source_dir: str | Path,
  commit_sha: str,
  *,
  expected_sha: str | None = None,
) -> None:
  """Move the installer-owned upstream ref to one verified commit.

  ``expected_sha`` makes legacy-history adoption a compare-and-swap: another
  updater cannot replace the recorded baseline between its proof and this ref
  move. The commit must already exist in the app repository.
  """
  repo = Path(source_dir)
  commit = _resolve_commit(repo, commit_sha)
  if commit is None:
    raise ValueError("invalid upstream commit")
  args = ["update-ref", f"refs/heads/{UPSTREAM_BRANCH}", commit]
  if expected_sha is not None:
    args.append(expected_sha)
  moved = _run(repo, *args, check=False)
  if moved.returncode != 0:
    raise SourceTreeChanged("recorded upstream changed during update")


def origin_url(source_dir: str | Path) -> str | None:
  """Return this app repo's configured ``origin`` URL, if available."""
  if not is_repo(source_dir):
    return None
  proc = _run(
    Path(source_dir), "remote", "get-url", "origin", check=False,
  )
  value = proc.stdout.strip()
  return value if proc.returncode == 0 and value else None


def set_origin_url(source_dir: str | Path, url: str) -> None:
  """Replace one existing app checkout's origin with a reviewed successor."""
  repo = Path(source_dir)
  if not is_repo(repo) or origin_url(repo) is None:
    raise RuntimeError("source repository has no origin")
  _run(repo, "remote", "set-url", "origin", url)


def has_origin(source_dir: str | Path) -> bool:
  """Whether this app repo has a real `origin` remote.

  A cloned catalog app has `origin`; a synthetic-upstream app created via
  `record_upstream` does not. Treat any git failure as false so callers can
  fall back to the synthetic path unchanged.
  """
  return origin_url(source_dir) is not None


@dataclass(frozen=True)
class Staged:
  """A private repository holding one commit transferred from ``url``."""

  path: Path
  sha: str
  url: str
  # The origin branch the commit came from, when the ref named a branch.
  branch: str | None = None


# Neither the transfer nor the import may start background maintenance that
# outlives the staging directory or the app's source lock.
_NO_AUTO_GC = ("-c", "gc.auto=0", "-c", "maintenance.auto=false")


def same_origin(left: str | None, right: str | None) -> bool:
  """Whether two Git remote URLs name the same repository."""
  normalize = lambda value: value.rstrip("/").removesuffix(".git").lower()
  return bool(left and right) and normalize(left) == normalize(right)


def _is_shallow(repo: Path) -> bool:
  return _run(
    repo, "rev-parse", "--is-shallow-repository", read_only=True,
  ).stdout.strip() == "true"


# Failures, in Git's untranslated text, that a later identical fetch repeats.
_PERMANENT_FETCH_ERROR = re.compile(
  r"couldn't find remote ref|not our ref|repository\b.*\bnot found"
  r"|does not appear to be a git repository|authentication failed"
  r"|could not read (?:username|password)|returned error: 40[134]\b"
  r"|no space left on device|disk quota exceeded",
  re.IGNORECASE,
)
_DNS_RECHECK_TIMEOUT = 5  # seconds
_UNRESOLVED_HOST = re.compile(r"could not resolve host: ([^\s']+)", re.IGNORECASE)


def _transient_fetch_error(stderr: str) -> bool:
  """Whether a failed fetch may succeed unchanged later; anything unknown may.

  Git reports every resolver failure alike, so the host is looked up again:
  as for connectors, only a resolver that could not answer (``EAI_AGAIN``) is
  temporary, while a name that does not exist stays missing.
  """
  if _PERMANENT_FETCH_ERROR.search(stderr):
    return False
  unresolved = _UNRESOLVED_HOST.search(stderr)
  if unresolved is None:
    return True
  answers: list[bool] = []

  def lookup() -> None:
    try:
      socket.getaddrinfo(unresolved.group(1), None)
    except socket.gaierror as exc:
      answers.append(exc.errno == socket.EAI_AGAIN)
    except (OSError, UnicodeError):
      pass

  # A resolver too slow to answer is itself a temporary failure.
  lookup_thread = threading.Thread(target=lookup, daemon=True)
  lookup_thread.start()
  lookup_thread.join(_DNS_RECHECK_TIMEOUT)
  return answers[0] if answers else True


def _transfer(
  stage: Path, url: str, ref: str, *, unshallow: bool,
) -> tuple[str, str | None]:
  """Fetch ``ref`` from ``url`` into ``stage``; Git's only network command.

  ``stage`` is always a private staging repository, never a live app
  repository, and no app lock is held: a slow transfer delays only its own
  request. A full commit id is fetched exactly; a name prefers the branch and
  falls back to the tag, as ``git clone --branch`` did. Returns the commit and
  the branch it came from, if any.
  """
  requested = str(ref or "")
  immutable = requested.lower() if _HEX_OID.fullmatch(requested.lower()) else None
  if immutable is None and (
    not requested
    or requested.startswith("-")
    or any(char in requested for char in "\\\n\r")
    or _run(
      stage, "check-ref-format", "--branch", requested, check=False,
    ).returncode != 0
  ):
    raise ValueError("invalid origin ref")
  sources = (
    [immutable] if immutable
    else [f"refs/heads/{requested}", f"refs/tags/{requested}"]
  )
  for source in sources:
    try:
      _run(
        stage, *_NO_AUTO_GC,
        "-c", f"http.lowSpeedLimit={_GIT_LOW_SPEED_LIMIT}",
        "-c", f"http.lowSpeedTime={_GIT_LOW_SPEED_TIME}",
        "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
        *(["--unshallow"] if unshallow else []),
        "--end-of-options", url, f"+{source}:{FETCHED_REF}",
        # The tag fallback and the failure's classification read Git's
        # untranslated error text.
        timeout=_GIT_TRANSFER_TIMEOUT, env={"LC_ALL": "C"},
      )
      break
    except subprocess.TimeoutExpired as exc:
      raise GitTransferTimeout(
        f"it ran longer than {_GIT_TRANSFER_TIMEOUT} seconds"
      ) from exc
    except subprocess.CalledProcessError as exc:
      if source == sources[-1] or "couldn't find remote ref" not in exc.stderr:
        lines = (exc.stderr or "").strip().splitlines()
        raise GitTransferFailed(
          lines[-1] if lines else f"git exited with status {exc.returncode}",
          transient=_transient_fetch_error(exc.stderr or ""),
        ) from exc
  fetched = _resolve_commit(stage, FETCHED_REF)
  if fetched is None:
    raise RuntimeError("origin ref did not resolve to a commit")
  if immutable is not None and fetched != immutable:
    raise RuntimeError("origin returned a different commit")
  return fetched, requested if source == f"refs/heads/{requested}" else None


def _record_origin_branch(repo: Path, staged: Staged) -> None:
  """Keep ``origin/<branch>`` where ``git fetch origin`` would have left it.

  Source status and Contribute compare an app with its last-fetched origin.
  """
  if staged.branch:
    _run(repo, "update-ref", f"refs/remotes/origin/{staged.branch}", staged.sha)


def _staging_dir(root: Path, prefix: str) -> Path:
  root.mkdir(parents=True, exist_ok=True)
  return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def stage_fetch(source_dir: str | Path, url: str, ref: str, root: Path) -> Staged:
  """Transfer ``ref`` for an existing app repo without touching that repo.

  The bare staging repository borrows the live objects through alternates, so
  Git negotiates with everything the app already has and downloads only new
  objects. A legacy shallow repo's boundary is copied and completed here, so
  the later local import gives it full history.
  """
  repo = Path(source_dir)
  git_dir = Path(_run(
    repo, "rev-parse", "--path-format=absolute", "--git-common-dir",
    read_only=True,
  ).stdout.strip())
  object_format = _run(
    repo, "rev-parse", "--show-object-format", read_only=True,
  ).stdout.strip()
  stage = _staging_dir(root, "fetch-")
  try:
    _run(stage, "init", "-q", "--bare", f"--object-format={object_format}")
    (stage / "objects" / "info" / "alternates").write_text(
      f"{git_dir / 'objects'}\n", encoding="utf-8",
    )
    unshallow = (git_dir / "shallow").is_file()
    if unshallow:
      shutil.copyfile(git_dir / "shallow", stage / "shallow")
    sha, branch = _transfer(stage, url, ref, unshallow=unshallow)
    return Staged(stage, sha, url, branch)
  except BaseException:
    shutil.rmtree(stage, ignore_errors=True)
    raise


def import_staged(source_dir: str | Path, staged: Staged) -> str:
  """Copy one staged commit into the app repo; local only, under its lock.

  The repository must still have the origin the transfer came from. Only
  objects, ``FETCHED_REF`` and the shallow marker change. The import is
  proportional to the download, so it shares the transfer ceiling.
  """
  repo = Path(source_dir)
  if not same_origin(origin_url(repo), staged.url):
    raise SourceTreeChanged(
      "the app's Git origin is not the repository this update came from"
    )
  try:
    _run(
      repo, *_NO_AUTO_GC, "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
      *(["--unshallow"] if _is_shallow(repo) else []),
      str(staged.path), f"+{FETCHED_REF}:{FETCHED_REF}",
      timeout=_GIT_TRANSFER_TIMEOUT,
    )
  except subprocess.TimeoutExpired as exc:
    raise GitTransferTimeout(
      f"it ran longer than {_GIT_TRANSFER_TIMEOUT} seconds"
    ) from exc
  if _resolve_commit(repo, staged.sha) != staged.sha:
    raise RuntimeError("staged commit did not import")
  _record_origin_branch(repo, staged)
  return staged.sha


def present_commit(source_dir: str | Path, ref: str) -> str | None:
  """``ref`` when it is a full commit id already present with full history."""
  repo = Path(source_dir)
  commit = str(ref or "").lower()
  if not _HEX_OID.fullmatch(commit) or _is_shallow(repo):
    return None
  return commit if _resolve_commit(repo, commit, read_only=True) == commit else None


def stage_clone(url: str, ref: str, root: Path) -> Staged:
  """Build a complete real-origin app checkout privately, with no lock held.

  ``main`` and ``upstream`` start at the resolved catalog commit, so the
  installed baseline is a real commit an update can fetch from and a local
  fix can be pushed against. A 64-character commit id implies a SHA-256
  repository. ``adopt_clone`` moves the result into place under the locks.
  """
  stage = _staging_dir(root, "clone-")
  try:
    sha256 = len(ref) == 64 and _HEX_OID.fullmatch(ref.lower())
    _run(stage, "init", "-q", *(["--object-format=sha256"] if sha256 else []))
    _run(stage, "remote", "add", "origin", url)
    # Catalog repos are untrusted content. Check out any tracked symlink as a
    # plain file holding the link text, never a real filesystem symlink that
    # could point outside the app directory. Set before the first checkout.
    _run(stage, "config", "core.symlinks", "false")
    sha, branch = _transfer(stage, url, ref, unshallow=False)
    staged = Staged(stage, sha, url, branch)
    _record_origin_branch(stage, staged)
    if branch:
      # Source status reads the remote's default branch through origin/HEAD.
      _run(
        stage, "symbolic-ref", "refs/remotes/origin/HEAD",
        f"refs/remotes/origin/{branch}",
      )
    _run(stage, "branch", "-f", UPSTREAM_BRANCH, sha)
    # Writing a large working tree is proportional to the download.
    _run(
      stage, "checkout", "-q", "-B", LOCAL_BRANCH, sha,
      timeout=_GIT_TRANSFER_TIMEOUT,
    )
    # Keep the app's OWN .gitignore committed (it travels upstream), but layer
    # Möbius's managed-artifact ignores locally via .git/info/exclude so a
    # later `git add -A` never tracks generated per-install files.
    _refresh_ignore_rules(stage)
    return staged
  except BaseException:
    shutil.rmtree(stage, ignore_errors=True)
    raise


def adopt_clone(staged: Staged, source_dir: str | Path) -> Callable[[], None]:
  """Move a staged clone into an empty ``source_dir``; local, under the locks.

  Returns the undo, which moves the checkout back and resets it to the staged
  commit, so a rolled-back install pass can adopt it again without another
  transfer.
  """
  repo = Path(source_dir).resolve()
  if repo.exists() and not repo.is_dir():
    raise RuntimeError(f"source_dir exists and is not a directory: {repo}")
  repo.mkdir(parents=True, exist_ok=True)
  if any(repo.iterdir()):
    raise RuntimeError(f"source_dir is not empty: {repo}")
  moved: list[str] = []

  def undo() -> None:
    for name in reversed(moved):
      shutil.move(str(repo / name), str(staged.path / name))
    moved.clear()
    _run(
      staged.path, "checkout", "-q", "-f", "-B", LOCAL_BRANCH, staged.sha,
      timeout=_GIT_TRANSFER_TIMEOUT,
    )
    _run(staged.path, "clean", "-q", "-ffdx", timeout=_GIT_TRANSFER_TIMEOUT)

  try:
    for child in list(staged.path.iterdir()):
      shutil.move(str(child), str(repo / child.name))
      moved.append(child.name)
  except BaseException:
    undo()
    raise
  return undo


def _release_continues(repo: Path, previous: str, fetched: str) -> bool:
  """Whether ``fetched`` moves the recorded upstream forward, never back.

  Store releases are pinned to content-free proof commits: each one has a
  single parent on the publisher's history and that parent's exact tree, on
  its own side branch. The next release descends from the old proof's parent,
  never from the proof itself. Such a leaf carries no bytes of its own, so its
  parent stands in for it; a rollback or unrelated history still fails
  because the parent is not an ancestor of the new release.
  """
  if ref_is_ancestor(repo, previous, fetched) is True:
    return True
  shape = _run(
    repo, "rev-list", "--parents", "-n", "1", previous, check=False,
  ).stdout.split()
  if len(shape) != 2 or not ref_trees_equal(repo, previous, shape[1]):
    return False
  return ref_is_ancestor(repo, shape[1], fetched) is True


def promote_upstream(
  source_dir: str | Path,
  commit: str,
  *,
  trusted_origin_adoption: bool = False,
) -> FetchUpstreamResult:
  """Advance managed upstream to one already-fetched immutable commit."""
  repo = Path(source_dir)
  fetched_ref = _resolve_commit(repo, str(commit).lower())
  if fetched_ref is None or fetched_ref != str(commit).lower():
    raise RuntimeError("reviewed upstream commit is unavailable")
  previous = _run(
    repo, "rev-parse", "--verify", UPSTREAM_BRANCH, check=False,
  )
  previous_sha = previous.stdout.strip() if previous.returncode == 0 else ""
  require_merge_history(repo, LOCAL_BRANCH, fetched_ref)
  equal_local_adoption = (
    trusted_origin_adoption
    and ref_trees_equal(repo, LOCAL_BRANCH, fetched_ref)
  )
  if previous_sha and previous_sha != fetched_ref:
    if (
      not _release_continues(repo, previous_sha, fetched_ref)
      and not equal_local_adoption
    ):
      raise RuntimeError(
        f"reviewed commit is unrelated to recorded upstream {previous_sha}"
      )
  _run(repo, "branch", "-f", UPSTREAM_BRANCH, fetched_ref)
  return FetchUpstreamResult(
    sha=fetched_ref,
    allow_unrelated_histories=equal_local_adoption,
  )


def ensure_repo(source_dir: str | Path) -> None:
  """Initializes the per-app repo if absent; a no-op once it exists.

  Creates the repo, writes the `.gitignore`, and makes an empty root
  commit on `upstream`. `main` is branched from that same root and
  checked out as the working branch.

  The empty root is deliberate: a meaningful merge needs `main` to
  descend from the SAME `upstream` commit that recorded the version it
  was installed at, so that a later update's merge base is the
  previously-installed version (not an unrelated root). The install path
  establishes that by `record_upstream` + `align_local_to_upstream`; the
  empty root is just the shared seed both branches grow from. Idempotent:
  re-running on an existing repo does nothing.
  """
  repo = Path(source_dir)
  if is_repo(repo):
    return
  repo.mkdir(parents=True, exist_ok=True)
  _run(repo, "init", "-q", "-b", UPSTREAM_BRANCH)
  (repo / ".gitignore").write_text(_GITIGNORE, encoding="utf-8")
  _run(repo, "add", ".gitignore")
  _run(repo, "commit", "-q", "-m", "Initialize app repo", "--allow-empty")
  _run(repo, "branch", LOCAL_BRANCH, UPSTREAM_BRANCH)
  _run(repo, "checkout", "-q", LOCAL_BRANCH)


def snapshot_worktree(source_dir: str | Path) -> WorktreeTree:
  """Capture accepted app-source candidates without mutating the real index.

  Git is the canonical source inventory: the temporary index starts at
  ``main``, applies the same managed ignore migration and pathspec as ordinary
  commits, then writes one immutable tree. Repeating this function is the
  caller's optimistic stability check around validation/compilation.
  """
  repo = Path(source_dir)
  ensure_repo(repo)
  _require_local_branch(repo)
  parent = head_sha(repo, LOCAL_BRANCH)
  with tempfile.TemporaryDirectory(prefix="mobius-app-index-") as tmp:
    index_file = Path(tmp) / "index"
    _run_with_index(repo, index_file, "read-tree", parent)
    _refresh_ignore_rules(repo, index_file=index_file)
    _run_with_index(repo, index_file, "add", *_tracked_source(repo))
    tree = _run_with_index(repo, index_file, "write-tree").stdout.strip()
  return WorktreeTree(tree_oid=tree, parent_sha=parent)


def commit_worktree_tree(
  source_dir: str | Path,
  snapshot: WorktreeTree,
  msg: str,
) -> str | None:
  """Commit exactly ``snapshot.tree_oid`` if ``main`` still has its parent.

  The candidate was compiled from this same immutable tree. Updating the ref
  with Git's expected-old-value argument is the compare-and-swap that prevents
  another accepted revision from being overwritten. The real index is updated
  only after a successful ref move; failed validation/compile paths never use
  it as scratch state.
  """
  repo = Path(source_dir)
  _require_local_branch(repo)
  current = head_sha(repo, LOCAL_BRANCH)
  if current != snapshot.parent_sha:
    raise SourceTreeChanged(
      "App source history changed while the revision was being applied."
    )
  current_tree = _run(
    repo, "rev-parse", f"{LOCAL_BRANCH}^{{tree}}",
  ).stdout.strip()
  if current_tree == snapshot.tree_oid:
    return None
  sha = _run(
    repo, "commit-tree", snapshot.tree_oid,
    "-p", snapshot.parent_sha,
    "-m", msg,
  ).stdout.strip()
  moved = _run(
    repo, "update-ref", f"refs/heads/{LOCAL_BRANCH}",
    sha, snapshot.parent_sha, check=False,
  )
  if moved.returncode != 0:
    raise SourceTreeChanged(
      "App source history changed while the revision was being applied."
    )
  # The checked-out branch moved without checkout. Make the ordinary index
  # describe the accepted commit while preserving any later worktree edits as
  # an unapplied draft.
  _run(repo, "read-tree", sha)
  return sha


def align_local_to_upstream(source_dir: str | Path) -> None:
  """Resets the local `main` branch to the current `upstream` tip.

  Called at INSTALL so the working branch starts at exactly the
  installed version: `main` then descends from that upstream commit, and
  the next update's three-way merge has it as the shared base. Must NOT
  be called on update — that would discard the local edits this whole
  model exists to preserve.

  Updates the branch ref and checks the working tree out to it so the
  on-disk `index.jsx` matches what was just installed.
  """
  repo = Path(source_dir)
  up = head_sha(repo, UPSTREAM_BRANCH)
  _run(repo, "checkout", "-q", LOCAL_BRANCH)
  _refresh_ignore_rules(repo)
  _run(repo, "reset", "-q", "--hard", up)
  _refresh_ignore_rules(repo)


def record_upstream(
  source_dir: str | Path,
  files: dict[str, bytes],
  manifest_url: str,
  version: str,
  *,
  exec_paths: frozenset[str] = frozenset(),
) -> str:
  """Commits the pristine installed SOURCE TREE onto `upstream`.

  Records the COMPLETE shipped tree `files` (repo-relative path -> bytes) onto
  the upstream branch as the canonical "this is what version <version> shipped"
  snapshot, WITHOUT disturbing the checked-out `main` working tree. Returns the
  new upstream commit sha (the merge base a later update diverges from).

  `files` is the whole tree: `index.jsx` is just one key, sibling modules
  (`cards.js`, …) are more keys, and the schedule job script is a key listed in
  `exec_paths`. There is no entry/sibling distinction — recording the whole tree
  is what lets a multi-file app update cleanly (a sibling that only ever lived on
  `main` would have no pristine `upstream` version to three-way-merge against,
  so every update would keep it stale or report a spurious add/add conflict).
  The managed `.gitignore` is always carried in regardless of `files`.

  Paths in `exec_paths` are staged executable (100755), everything else 100644;
  the caller names the exec files explicitly (no `*.sh` inference). The job
  script's mode must match what `main` records or the merge reports a phantom
  add/add conflict on a pure 644-vs-755 skew. The tree is built from an EMPTY
  index, not by patching the previous upstream tip, so a file DROPPED from the
  new version is correctly removed from `upstream` too. Staged into a temp view
  of the shared index and restored to `main` afterwards, so the live working
  tree an explicit apply may be snapshotting stays put.
  """
  repo = Path(source_dir)
  ensure_repo(repo)
  _refresh_ignore_rules(repo)
  parent = _run(repo, "rev-parse", UPSTREAM_BRANCH).stdout.strip()
  if has_origin(repo):
    gi = subprocess.run(
      ["git", "-C", str(repo), "cat-file", "-p", f"{UPSTREAM_BRANCH}:.gitignore"],
      capture_output=True, timeout=_GIT_TIMEOUT, check=False, env=_git_env(repo),
    )
    gitignore_bytes = gi.stdout if gi.returncode == 0 else _GITIGNORE.encode()
  else:
    gitignore_bytes = (repo / ".gitignore").read_bytes()
  # The full pristine source set: the managed .gitignore plus every file in the
  # shipped tree. Files in `exec_paths` are 100755 so `upstream` and `main` agree
  # on the mode (a 644/755 skew alone makes merge report a phantom add/add).
  staged: list[tuple[str, bytes, str]] = [
    (".gitignore", gitignore_bytes, "100644"),
  ]
  for rel, data in files.items():
    if rel == ".gitignore":
      continue  # the managed .gitignore always wins
    staged.append((rel, data, "100755" if rel in exec_paths else "100644"))
  # Build the upstream tree from an EMPTY index so files dropped from the new
  # version vanish. Each blob needs bytes on stdin (which text-mode `_run` can't
  # carry), so hash-object + update-index run as direct subprocess calls under
  # the isolated git env.
  _run(repo, "read-tree", "--empty")
  for rel, data, mode in staged:
    blob = subprocess.run(
      ["git", "-C", str(repo), "hash-object", "-w", "--stdin", "--path", rel],
      input=data, capture_output=True, timeout=_GIT_TIMEOUT, check=True,
      env=_git_env(repo),
    ).stdout.decode().strip()
    subprocess.run(
      ["git", "-C", str(repo), "update-index", "--add", "--cacheinfo",
       f"{mode},{blob},{rel}"],
      capture_output=True, text=True, timeout=_GIT_TIMEOUT, check=True,
      env=_git_env(repo),
    )
  tree = _run(repo, "write-tree").stdout.strip()
  msg = f"install v{version} from {manifest_url}"
  sha = _run(
    repo, "commit-tree", tree, "-p", parent, "-m", msg,
  ).stdout.strip()
  _run(repo, "update-ref", f"refs/heads/{UPSTREAM_BRANCH}", sha)
  # Restore the index to match the checked-out working branch so a later
  # `git status`/commit on `main` isn't confused by the tree we just staged.
  _run(repo, "read-tree", LOCAL_BRANCH)
  return sha


def has_unresolved_conflicts(source_dir: str | Path) -> bool:
  """Whether an in-progress merge still has ANY tracked file unresolved.

  The invariant this guards: an update must NEVER finalize while conflict
  markers remain in any tracked file. Two signals, BOTH required, because
  they cover disjoint moments of the resolve flow:

  - `git ls-files -u` lists unmerged index entries. It is non-empty right
    after `start_conflict_merge` materializes the conflict, BUT it clears
    the instant the agent `git add`s a file — even one that still carries
    `<<<<<<<` markers (adding marks the path "resolved" in the index
    regardless of content).
  - A `git grep` for the LABELED boundary markers (`<<<<<<< <ref>` and
    `>>>>>>> <ref>`) still fires on a marker-bearing file the agent has
    already staged. We match only those two boundaries — NOT `git diff
    --check`, which flags any 7-char marker line including a bare `=======`.
    A legitimate `=======` separator (a heredoc divider, a setext rule) in
    resolved app content would otherwise read as an unresolved conflict
    forever and deadlock the update. The labeled boundaries that a real `git
    merge` always writes essentially never start a line of real source.

  Only meaningful while a merge is in progress: outside a merge there is
  nothing to finalize, so the caller is expected to check MERGE_HEAD first
  and this returns False (no unmerged entries, `--check` not run).
  """
  repo = Path(source_dir)
  if not merge_in_progress(repo):
    return False
  unmerged = _run(repo, "ls-files", "-u").stdout.strip()
  if unmerged:
    return True
  return has_conflict_markers(repo)


def _git_control_path(repo: Path, name: str) -> Path:
  """Resolve per-worktree Git state without assuming `.git` is a directory."""
  return Path(_run(
    repo, "rev-parse", "--path-format=absolute", "--git-path", name,
  ).stdout.strip())


def merge_in_progress(source_dir: str | Path) -> bool:
  """Whether Git records an in-progress merge for this repository."""
  repo = Path(source_dir)
  return is_repo(repo) and _git_control_path(repo, "MERGE_HEAD").is_file()


def has_conflict_markers(source_dir: str | Path) -> bool:
  """Whether an in-progress merge's working tree still has text markers."""
  repo = Path(source_dir)
  if not merge_in_progress(repo):
    return False
  # Scan tracked content for the labeled conflict boundaries. `git grep`
  # exits 0 when a line matches, 1 when none do. We match only `<<<<<<< ` and
  # `>>>>>>> ` (7 chars + the space before the ref that `git merge` writes),
  # never the bare `=======` separator that `git diff --check` would flag on
  # legitimate content — see the docstring.
  markers = _run(repo, "grep", "-lE", r"^(<<<<<<< |>>>>>>> )", check=False)
  return markers.returncode == 0


def commit_local(source_dir: str | Path, msg: str) -> str | None:
  """Commits the current working-tree source onto `main`.

  Stages the source files (filtered by `.gitignore`) and commits them as
  a local edit. Returns the new commit sha, or None when there was
  nothing to commit (the tree already matches `main`'s tip) — a no-op
  rather than an empty commit so the history stays meaningful.

  HARD GATE on the merge-finalize path: if a merge is in progress
  (MERGE_HEAD present) and `has_unresolved_conflicts` is True, this REFUSES
  to commit and returns None, leaving the merge in progress. The invariant
  is that an update is never finalized while ANY tracked file still holds
  conflict markers — without this gate a conflict in a NON-entry file (a
  job script like `fetch.sh`) would sail through, because the only other
  resolution signal is "does index.jsx compile", which stays true. So no
  caller can commit a marker-bearing tree as source.

  If a merge is in progress and FULLY resolved (no unmerged entries, no
  markers), this finalizes it as a SINGLE-parent replay commit parented on
  the upstream tip read from `.git/MERGE_HEAD`, so the upstream tip becomes
  a direct parent of `main` and the merge base advances for free while
  history stays linear — the same `A -> B -> X` shape `commit_replay` makes
  for a clean apply. The pre-merge local `main` tip becomes unreachable
  (its content lives on in the replay's tree); the squash is intentional.
  An ordinary local edit (no MERGE_HEAD) still commits as a plain
  single-parent commit on the old `main` tip.

  HARD GATE on an in-progress rebase/cherry-pick: if one is mid-flight (no
  MERGE_HEAD, but a rebase/cherry-pick state dir/head is present OR the index
  still carries unmerged entries), this REFUSES before staging and returns
  None. Staging there would mark conflicted paths resolved in the index — a
  later `git add`/commit could then bake conflict markers into tracked source,
  or a `git commit` mid-rebase would write a stray commit onto the detached
  sequencer HEAD. The MERGE_HEAD finalize path below is the ONE case that must
  legitimately stage-then-scan, so it is exempt from this refusal.
  """
  repo = Path(source_dir)
  ensure_repo(repo)
  _require_local_branch(repo)
  merge_head_path = _git_control_path(repo, "MERGE_HEAD")
  # Refuse to touch the index while a rebase/cherry-pick/am is in progress
  # (see the docstring's HARD GATE). Scoped to "no MERGE_HEAD" so the merge
  # finalize path below still stages-then-scans. `ls-files -u` covers a
  # conflict left in the index with no state dir (e.g. a bare `read-tree -m`).
  if not merge_head_path.exists():
    in_progress = (
      _git_control_path(repo, "rebase-merge").exists()
      or _git_control_path(repo, "rebase-apply").exists()
      or _git_control_path(repo, "CHERRY_PICK_HEAD").exists()
      or bool(_run(repo, "ls-files", "-u").stdout.strip())
    )
    if in_progress:
      return None
  _refresh_ignore_rules(repo)
  # Stage BEFORE the gate so `has_unresolved_conflicts` reads git's resolved
  # state: staging marks unmerged paths resolved in the index (clearing
  # `ls-files -u`) whether or not their content is clean, so the gate then
  # rests on the marker scan, which fires only when markers actually remain.
  # If we gated before staging, a conflict the agent resolved on disk but
  # never `git add`ed would still show as an unmerged index entry and we'd
  # wrongly refuse to finalize a clean resolution.
  _run(repo, "add", *_tracked_source(repo))
  if merge_head_path.exists():
    if has_unresolved_conflicts(repo):
      return None
    # Finalize the resolved merge as a single-parent replay on the upstream
    # tip so the line stays linear. A plain `git commit` here would parent on
    # BOTH the old main tip and MERGE_HEAD (git makes a merge commit whenever
    # MERGE_HEAD is set), fanning history into a 2-parent merge; we want the
    # squashed `A -> B -> X` shape instead, identical to commit_replay.
    merge_head = merge_head_path.read_text(encoding="utf-8").strip()
    old_local = head_sha(repo, LOCAL_BRANCH)
    tree = _run(repo, "write-tree").stdout.strip()
    sha = _run(
      repo, "commit-tree", tree, "-p", merge_head, "-m", msg,
    ).stdout.strip()
    _run(repo, "update-ref", f"refs/heads/{LOCAL_BRANCH}", sha)
    try:
      carry_equivalent_change_sources(repo, old_local, sha)
    except Exception:
      # The accepted replay is already committed. A stale witness only means a
      # later update may ask the agent; it must not make this resolution appear
      # failed after the source branch has moved.
      log.warning("could not carry contribution provenance across replay",
                  exc_info=True)
    for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE"):
      _git_control_path(repo, name).unlink(missing_ok=True)
    return sha
  status = _run(repo, "status", "--porcelain").stdout.strip()
  if not status:
    return None
  _run(repo, "commit", "-q", "-m", msg)
  return _run(repo, "rev-parse", LOCAL_BRANCH).stdout.strip()


def commit_replay(
  source_dir: str | Path, upstream_tip: str, msg: str,
) -> str | None:
  """Records the working-tree source onto `main` as a single-parent replay.

  Commits the current working tree (the just-applied merge result) with a
  SINGLE parent: `upstream_tip` (the upstream commit whose changes were
  folded in). The local delta is squashed into this one replay commit on
  top of upstream, so history stays linear (`A -> B -> X`): `main` is a
  straight-line descendant of `upstream`, after which `upstream_tip` is a
  direct parent of `main` and `git merge-base --is-ancestor upstream main`
  is exact. That ancestry is what advances the base — the NEXT update's
  three-way merge diffs only the genuinely-new upstream delta against local
  edits instead of re-litigating already-merged history against a stale base
  (the bug that turned every repeated update into a spurious conflict).

  The previous local `main` tip becomes unreachable after the replay, which
  is intentional: its content lives on in the replay commit's tree, and the
  squash is what keeps the line linear rather than fanning into a merge.

  A plain `commit_local` cannot do this: it parents on the old `main` tip,
  which leaves `upstream` unreachable from `main`, so `git merge-base` keeps
  returning the original install point. Returns the new commit sha, or None
  when there is nothing to record (working tree already matches `main` and
  `upstream_tip` is already an ancestor — e.g. a re-install of the same
  version with no local edits).
  """
  repo = Path(source_dir)
  ensure_repo(repo)
  old_local = head_sha(repo, LOCAL_BRANCH)
  _refresh_ignore_rules(repo)
  _run(repo, "add", *_tracked_source(repo))
  tree = _run(repo, "write-tree").stdout.strip()
  main_tree = _run(repo, "rev-parse", f"{LOCAL_BRANCH}^{{tree}}").stdout.strip()
  already_merged = _run(
    repo, "merge-base", "--is-ancestor", upstream_tip, LOCAL_BRANCH,
    check=False,
  ).returncode == 0
  if tree == main_tree and already_merged:
    return None
  sha = _run(
    repo, "commit-tree", tree, "-p", upstream_tip, "-m", msg,
  ).stdout.strip()
  _run(repo, "update-ref", f"refs/heads/{LOCAL_BRANCH}", sha)
  try:
    carry_equivalent_change_sources(repo, old_local, sha)
  except Exception:
    log.warning("could not carry contribution provenance across replay",
                exc_info=True)
  return sha


# ── Overlay commit trailers ──────────────────────────────────────────────────
#
# Platform updates record local source as commits on top of the reviewed
# upstream. These trailers name the unit a commit carries, so the updater can
# recognise and unwind its own transient working-edits commit.

OVERLAY_UNIT_TRAILER = "Mobius-Overlay-Unit"
OVERLAY_DISPOSITION_TRAILER = "Mobius-Disposition"
# The original local commits a re-emitted unit carries; a record, not a proof.
OVERLAY_SOURCE_TRAILER = "Mobius-Overlay-Source"
# Another unit that also shaped every path of this commit, as
# ``<slug>=<disposition>``. A re-emitted chain gives a path only to its owner's
# commit, so this keeps the other contributors' claims for retirement.
OVERLAY_ALSO_TRAILER = "Mobius-Overlay-Also"
# Uncommitted working edits ride through an update as this transient unit and
# return to the working tree afterwards.
OVERLAY_WORKING_UNIT = "working-tree"


def overlay_message(
  subject: str, *, unit: str, disposition: str, body: str = "",
  sources: Iterable[str] = (), also: Iterable[tuple[str, str]] = (),
) -> str:
  """A commit message carrying the overlay unit and disposition trailers.

  ``also`` lists ``(slug, disposition)`` of other units that shaped the paths.
  """
  parts = [subject.strip()]
  if body.strip():
    parts.append(body.strip())
  trailers = [
    f"{OVERLAY_UNIT_TRAILER}: {unit}",
    f"{OVERLAY_DISPOSITION_TRAILER}: {disposition}",
  ]
  sources = tuple(sources)
  if sources:
    trailers.append(f"{OVERLAY_SOURCE_TRAILER}: {' '.join(sources)}")
  trailers.extend(
    f"{OVERLAY_ALSO_TRAILER}: {slug}={disposition}" for slug, disposition in also
  )
  parts.append("\n".join(trailers))
  return "\n\n".join(parts) + "\n"


# ── Overlay units ────────────────────────────────────────────────────────────
#
# A unit is one intent of local platform work: the commits sharing a
# ``Mobius-Overlay-Unit`` slug, a labelled integration merge, or one untagged
# commit. Each changed path belongs to the last unit that touched it, so units
# partition the local delta; every unit also records all the paths it touched.
# An update re-emits that delta as one commit per unit on the release
# (``unit_chain``), so the next update still knows which change each path
# carries and can retire a unit whose pull request merged upstream
# (``set_aside_merged_work``) instead of re-merging a stale local copy.
# No contributor marked ``owner-choice:local`` is ever retired; the chain names
# the other contributors on the owner's commit (``Mobius-Overlay-Also``) so
# that guard survives later updates.

# Agents change a commit's disposition without rewriting history: the newest
# ``Mobius-Disposition:`` note in this ref wins over the commit's trailer.
OVERLAY_NOTES_REF = "refs/notes/mobius-overlay"
# Contribute's offline witness of the exact bytes a merged platform pull
# request carried; see ``record_merged_contribution``.
MERGED_CONTRIBUTION_PREFIX = "refs/mobius/merged-contributions"
# The repository platform releases come from. A witness for a pull request in
# any other repository (a test fork) never retires anything: its number says
# nothing about this release.
PLATFORM_RELEASE_REPO = "mobius-os/mobius"
# Updaters before unit chains squashed the whole delta into this one unit and
# named the local tip it replaced, which ``overlay_units`` follows back.
LEGACY_OVERLAY_UNIT = "platform-local-tree"
_LEGACY_UNATTRIBUTED_UNIT = "legacy-unattributed"
_LEGACY_SOURCE = re.compile(
  r"^Previous local source: ([0-9a-f]{40}|[0-9a-f]{64})[ \t]*$", re.M,
)
_LEGACY_EXPANSION_DEPTH = 8
_OVERLAY_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_OVERLAY_DISPOSITION = re.compile(
  r"^(?:local-only|owner-choice:local|wip|pending-pr:#([1-9][0-9]{0,8}))$",
)
# Upstream squash-merges end their subject with the pull request number.
_PR_SUBJECT_SUFFIX = re.compile(r"\(#([1-9][0-9]{0,8})\)\s*$")
_MAX_OVERLAY_SOURCES = 16
_OVERLAY_TRAILER_LINE = re.compile(
  rf"^(?:{OVERLAY_UNIT_TRAILER}|{OVERLAY_DISPOSITION_TRAILER}|"
  rf"{OVERLAY_SOURCE_TRAILER}|{OVERLAY_ALSO_TRAILER}|Previous local source):.*$\n?",
  re.M,
)


def _last_trailer(lines: str, key: str) -> str:
  """``key``'s last value in ``lines``: a commit's trailer block, or a note.

  Callers pass only a commit's parsed trailer block (``%(trailers)``), never
  its whole message, so a body line quoting a trailer sets nothing. A
  disposition note holds nothing but the trailer line, which Git's own
  parser would read as a title, so notes are read line by line.
  """
  values = re.findall(rf"^{re.escape(key)}:[ \t]*(\S.*?)[ \t]*$", lines, re.M)
  return values[-1] if values else ""


def _valid_disposition(value: str) -> str:
  return value if _OVERLAY_DISPOSITION.fullmatch(value) else ""


@dataclass(frozen=True)
class _OverlayCommit:
  sha: str
  message: str
  note: str
  paths: tuple[str, ...]
  # The message's trailer block as Git parses it; the only place labels live.
  trailers: str = ""

  @property
  def subject(self) -> str:
    return self.message.partition("\n")[0].strip()

  @property
  def slug(self) -> str:
    tagged = _last_trailer(self.trailers, OVERLAY_UNIT_TRAILER)
    if _OVERLAY_SLUG.fullmatch(tagged):
      return tagged
    words = re.sub(r"[^a-z0-9]+", "-", self.subject.lower())[:40].strip("-")
    return f"c-{self.sha[:8]}" + (f"-{words}" if words else "")

  @property
  def noted_disposition(self) -> str:
    return _valid_disposition(_last_trailer(self.note, OVERLAY_DISPOSITION_TRAILER))

  @property
  def tagged_disposition(self) -> str:
    return _valid_disposition(_last_trailer(self.trailers, OVERLAY_DISPOSITION_TRAILER))

  @property
  def sources(self) -> tuple[str, ...]:
    carried = _last_trailer(self.trailers, OVERLAY_SOURCE_TRAILER).split()
    return tuple(s for s in carried if _HEX_OID.fullmatch(s)) or (self.sha,)

  @property
  def also(self) -> tuple[tuple[str, str], ...]:
    """The other units this commit records as shaping its paths."""
    found = []
    for value in re.findall(
      rf"^{OVERLAY_ALSO_TRAILER}:[ \t]*(\S+)[ \t]*$", self.trailers, re.M,
    ):
      slug, _, disposition = value.partition("=")
      if (
        _OVERLAY_SLUG.fullmatch(slug) and slug != OVERLAY_WORKING_UNIT
        and disposition != "wip" and _valid_disposition(disposition)
      ):
        found.append((slug, disposition))
    return tuple(found)


@dataclass(frozen=True)
class OverlayUnit:
  """One intent of local work and the changed paths it owns."""

  slug: str
  disposition: str  # local-only | pending-pr:#N | owner-choice:local
  pr: int | None
  subject: str
  body: str
  commits: tuple[str, ...]  # walked commits, oldest first
  paths: tuple[str, ...]  # changed paths this unit was the last to touch
  sources: tuple[str, ...] = ()  # original local commits, for the record
  # Every changed path this unit contributed to, owned or not. A path several
  # units shaped is retired only when all of them may retire.
  touched: tuple[str, ...] = ()

  def message(self, also: Iterable[tuple[str, str]] = ()) -> str:
    return overlay_message(
      self.subject, unit=self.slug, disposition=self.disposition,
      body=self.body, sources=self.sources, also=also,
    )


def _overlay_commits(
  repo: Path, tip: str, exclude: Iterable[str],
) -> list[_OverlayCommit]:
  """The local commits of ``tip`` outside ``exclude``, oldest first.

  History is walked along first parents. A merge carrying an overlay trailer
  or disposition note (an integration merge bringing in a PR branch) is one
  commit whose paths are its first-parent diff; the commits it imported are
  not listed separately. An unlabelled merge is not itself listed: the
  commits it imported through its other parents are, walked the same way.
  """
  exclude = list(exclude)
  proc = _run(
    repo, "log", "--reverse", "--first-parent", "--diff-merges=first-parent",
    "--no-renames", f"--notes={OVERLAY_NOTES_REF}",
    "--format=%x1e%H%x1f%P%x1f%(trailers:only,unfold)%x1f%B%x1f%N%x1f",
    "--name-only", "-z", tip, "--not", *exclude, "--",
  )
  commits = []
  for record in proc.stdout.split("\x1e")[1:]:
    sha, parents, trailers, message, note, names = (
      record.split("\x1f", 5) + [""] * 5
    )[:6]
    first, *imported = parents.split() or [""]
    labelled = _OverlayCommit(sha.strip(), message, note, (), trailers)
    if imported and not (
      _OVERLAY_SLUG.fullmatch(_last_trailer(trailers, OVERLAY_UNIT_TRAILER))
      or labelled.tagged_disposition or labelled.noted_disposition
    ):
      seen = [*exclude, first]
      for parent in imported:
        commits.extend(_overlay_commits(repo, parent, seen))
        seen.append(parent)
      continue
    paths = tuple(dict.fromkeys(
      name.lstrip("\n") for name in names.split("\0") if name.strip("\n")
    ))
    commits.append(replace(labelled, paths=paths))
  return commits


# ``(commit, slug, paths, owns)``: ``commit`` (None for unattributed legacy
# paths) contributed to ``paths`` as unit ``slug``, and owns them unless it
# only guards them.
_Claim = tuple[_OverlayCommit | None, str, tuple[str, ...], bool]


def _legacy_guard_slug(squash: _OverlayCommit) -> str:
  """The unit of one noted legacy squash's guard.

  Every legacy squash carries the slug ``platform-local-tree``, so nested
  squashes would otherwise share one unit and one disposition, letting an
  older inner note override an outer one. Each noted squash is its own
  intent over its own paths.
  """
  return f"{LEGACY_OVERLAY_UNIT}-{squash.sha[:12]}"


def _commit_claims(
  repo: Path, commit: _OverlayCommit, paths: tuple[str, ...],
  exclude: list[str], depth: int = 1,
) -> list[_Claim]:
  """The claims one walked commit makes, expanding a legacy squash."""
  if commit.slug != LEGACY_OVERLAY_UNIT:
    return [(commit, commit.slug, paths, True)]
  claims: list[_Claim] = []
  # The squash's own trailer is always the default ``local-only``; only a
  # disposition someone noted on it is an explicit choice, and it keeps
  # guarding every path the squash carried after expansion hands ownership
  # back to the commits it replaced.
  noted = commit.noted_disposition
  if noted and noted != "wip":
    claims.append((commit, _legacy_guard_slug(commit), paths, False))
  return claims + _expand_legacy_squash(repo, commit, paths, exclude, depth)


def _expand_legacy_squash(
  repo: Path, squash: _OverlayCommit, paths: tuple[str, ...],
  exclude: list[str], depth: int,
) -> list[_Claim]:
  """Attribute a legacy squash's paths to the local commits it replaced.

  Each path is claimed, in order, by every commit that touched it in the
  squash's ``Previous local source``, following earlier squashes the same
  way, so the last one owns it and the earlier ones still count as its
  contributors. Paths no commit explains stay together under
  ``legacy-unattributed``. This only labels history: the chain built from it
  is proved to equal the merged tree.
  """
  found = _LEGACY_SOURCE.search(squash.message)
  previous = _resolve_commit(repo, found.group(1)) if found else None
  if previous is None or depth > _LEGACY_EXPANSION_DEPTH:
    return [(None, _LEGACY_UNATTRIBUTED_UNIT, paths, True)]
  walked = _overlay_commits(repo, previous, exclude)
  wanted = set(paths)
  explained = {path for commit in walked for path in commit.paths}
  loose = tuple(path for path in paths if path not in explained)
  claims: list[_Claim] = (
    [(None, _LEGACY_UNATTRIBUTED_UNIT, loose, True)] if loose else []
  )
  for commit in walked:
    owned = tuple(path for path in commit.paths if path in wanted)
    if owned:
      claims.extend(_commit_claims(repo, commit, owned, exclude, depth + 1))
  return claims


def overlay_units(
  source_dir: str | Path, local: str, upstream: str,
  *, official: str = "refs/remotes/origin/main",
) -> list[OverlayUnit]:
  """Partition the local commits ``upstream`` lacks into units.

  ``paths`` partition the delta by last touch; ``touched`` lists every path a
  unit contributed to. A unit whose paths later units all took over is still
  returned for its ``touched`` paths. The transient ``working-tree`` unit is
  excluded: its paths belong to no unit. A legacy ``platform-local-tree``
  squash is expanded back into the commits it replaced, so an overlay
  squashed by an older updater (including late edits squashed at boot by an
  older image) regains its unit identity; a disposition noted on one squash
  itself stays a contributor over that squash's paths, as its own
  ``platform-local-tree-<sha>`` unit.

  A unit's disposition is the newest one noted on its commits; failing that,
  ``owner-choice:local`` if any of its commits or a later commit's
  ``Mobius-Overlay-Also`` records it (a default ``local-only`` trailer on a
  newer commit never drops an owner's choice); else the newest recorded.
  """
  repo = Path(source_dir)
  exclude = [upstream, *([official] if _resolve_commit(repo, official) else [])]
  claims = [
    claim for commit in _overlay_commits(repo, local, exclude)
    for claim in _commit_claims(repo, commit, commit.paths, exclude)
  ]
  owner: dict[str, str] = {}
  groups: dict[str, list[_OverlayCommit]] = {}
  touched: dict[str, set[str]] = {}
  # Dispositions other commits recorded for a unit that shaped their paths.
  recorded: dict[str, list[str]] = {}
  for commit, slug, paths, owns in claims:
    if slug == OVERLAY_WORKING_UNIT or (commit and commit.tagged_disposition == "wip"):
      for path in paths:
        owner.pop(path, None)
      continue
    members = groups.setdefault(slug, [])
    if commit is not None:
      members.append(commit)
    touched.setdefault(slug, set()).update(paths)
    for path in paths if owns else ():
      owner[path] = slug
    for other, disposition in commit.also if commit else ():
      groups.setdefault(other, [])
      touched.setdefault(other, set()).update(paths)
      recorded.setdefault(other, []).append(disposition)
  units = []
  for slug, members in groups.items():
    paths = tuple(sorted(path for path, unit in owner.items() if unit == slug))
    if not touched.get(slug):
      continue
    labelled = [
      *reversed([c.tagged_disposition for c in members]),
      *reversed(recorded.get(slug, [])),
    ]
    disposition = next((
      d for d in [
        *reversed([c.noted_disposition for c in members]),
        *(["owner-choice:local"] if "owner-choice:local" in labelled else []),
        *labelled,
      ] if d
    ), "local-only")
    pr = _OVERLAY_DISPOSITION.fullmatch(disposition).group(1)
    first = members[0] if members else None
    units.append(OverlayUnit(
      slug=slug, disposition=disposition, pr=int(pr) if pr else None,
      subject=(
        first.subject if first
        else "Local platform changes from before overlay units"
      ),
      body=(
        _OVERLAY_TRAILER_LINE.sub("", first.message.partition("\n")[2]).strip()
        if first else ""
      ),
      commits=tuple(c.sha for c in members),
      paths=paths,
      sources=tuple(dict.fromkeys(
        s for c in members for s in c.sources
      ))[-_MAX_OVERLAY_SOURCES:],
      touched=tuple(sorted(touched[slug])),
    ))
  return units


def _tree_changes(repo: Path, left: str, right: str) -> dict[str, tuple[str, str]]:
  """``{path: (mode, oid)}`` turning ``left`` into ``right``; mode ``0`` deletes."""
  proc = _run(repo, "diff-tree", "-r", "-z", "--no-renames", left, right)
  fields = proc.stdout.split("\0")
  changes = {}
  for header, path in zip(fields[0::2], fields[1::2]):
    _old_mode, mode, _old_oid, oid, _status = header.lstrip(":").split(" ", 4)
    changes[path] = ("0", "0" * len(oid)) if int(mode, 8) == 0 else (mode, oid)
  return changes


def _index_info(changes: dict[str, tuple[str, str]], paths: Iterable[str]) -> str:
  return "".join(f"{changes[p][0]} {changes[p][1]}\t{p}\0" for p in paths)


def unit_chain(
  source_dir: str | Path, target: str, tree: str, units: Iterable[OverlayUnit],
  *, resolution: OverlayUnit,
) -> str:
  """Commit ``tree`` on ``target`` as one labelled commit per unit.

  Each unit's commit sets the paths it owns to ``tree``'s bytes; a unit with
  nothing left to change (retired, or already upstream) emits no commit.
  Changed paths no unit owns, such as a resolver's additions or a path a
  stray working-edits commit took from its owner, go into ``resolution``. A
  path other units also touched is committed with those units named in
  ``Mobius-Overlay-Also`` trailers, one commit per distinct set, resolution
  included, so their dispositions still guard it after the original commits
  are gone.
  Raises ``RuntimeError`` unless the chain's tip tree is exactly ``tree``;
  callers then fall back to one commit.
  """
  repo = Path(source_dir)
  units = list(units)
  remaining = _tree_changes(repo, target, tree)
  changes = dict(remaining)
  shaped_by: dict[str, set[tuple[str, str]]] = {}
  for unit in units:
    for path in unit.touched:
      shaped_by.setdefault(path, set()).add((unit.slug, unit.disposition))
  plan = []
  for unit in [*units, resolution]:
    by_also: dict[tuple[tuple[str, str], ...], list[str]] = {}
    owned = unit.paths if unit is not resolution else sorted(remaining)
    for path in owned:
      if remaining.pop(path, None):
        also = tuple(sorted(
          c for c in shaped_by.get(path, ()) if c[0] != unit.slug
        ))
        by_also.setdefault(also, []).append(path)
    plan.extend((unit.message(also), paths) for also, paths in by_also.items())
  tip = target
  with tempfile.TemporaryDirectory(prefix="mobius-unit-chain-") as tmp:
    index = Path(tmp) / "index"
    _run_with_index(repo, index, "read-tree", target)
    for message, paths in plan:
      _run_with_index(
        repo, index, "update-index", "-z", "--index-info",
        input=_index_info(changes, paths),
      )
      written = _run_with_index(repo, index, "write-tree").stdout.strip()
      tip = _run(
        repo, "commit-tree", written, "-p", tip, "-m", message,
      ).stdout.strip()
  if _tree_oid(repo, tip) != _tree_oid(repo, tree):
    raise RuntimeError("the unit chain does not reproduce the merged tree")
  return tip


def overlay_evidence_fingerprint(source_dir: str | Path) -> str:
  """Identity of the refs that change what an update sets aside, or "".

  Disposition notes, merged-contribution witnesses and superseded drafts move
  without moving any branch, so the reviewed update plan and the status cache
  key on this too. Empty when none exist.
  """
  proc = _run(
    Path(source_dir), "for-each-ref", "--format=%(refname) %(objectname)",
    OVERLAY_NOTES_REF, MERGED_CONTRIBUTION_PREFIX, _SUPERSEDED_DRAFT_PREFIX,
    check=False,
  )
  listing = proc.stdout.strip()
  return hashlib.sha256(listing.encode("utf-8")).hexdigest() if listing else ""


def mark_overlay_disposition(
  source_dir: str | Path, commit: str, disposition: str,
) -> str:
  """Record a disposition for one local commit's unit without rewriting it."""
  repo = Path(source_dir)
  sha = _resolve_commit(repo, commit)
  if sha is None:
    raise ValueError(f"unknown commit: {commit}")
  if disposition == "wip" or not _valid_disposition(disposition):
    raise ValueError(
      "disposition must be local-only, owner-choice:local or pending-pr:#<N>",
    )
  _run(
    repo, "notes", f"--ref={OVERLAY_NOTES_REF}", "add", "-f",
    "-m", f"{OVERLAY_DISPOSITION_TRAILER}: {disposition}", sha,
  )
  return sha


@dataclass(frozen=True)
class RetiredPath:
  """A local copy set aside because the pull request carrying it merged.

  ``proof`` is ``merged-contribution`` when the local bytes are exactly what
  the reviewed head of the PR carried (Contribute's witness), or
  ``pending-pr`` when they are still the ones the unit marked for that PR
  last wrote.
  """

  path: str
  pr: int
  unit: str | None
  proof: str

  def as_dict(self) -> dict:
    return {"path": self.path, "pr": self.pr, "unit": self.unit, "proof": self.proof}


# Witness metadata version. Only this version is read; a ref under
# ``MERGED_CONTRIBUTION_PREFIX`` in any other form is not a witness, so it
# retires nothing and nothing here deletes it. (Pre-release builds wrote a
# version 1 whose tree was the staging source, not the reviewed head;
# ``backfill_merged_contribution_refs`` migrates those.)
_MERGED_CONTRIBUTION_FORMAT = 2


@dataclass(frozen=True)
class _MergedContribution:
  ref: str
  number: int
  repo: str  # GitHub ``owner/name``; "" for an unreadable witness
  merge_sha: str | None
  paths: tuple[str, ...]
  metadata: dict = field(default_factory=dict, compare=False)

  @property
  def for_platform_release(self) -> bool:
    return bool(self.merge_sha) and self.repo.lower() == PLATFORM_RELEASE_REPO


_GITHUB_REPO_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def record_merged_contribution(
  source_dir: str | Path,
  *,
  number: int,
  repo_slug: str,
  head_sha: str,
  merge_sha: str,
  paths: Iterable[str],
  contribution_id: str = "",
) -> str | None:
  """Record that PR ``number`` of ``repo_slug`` merged as ``merge_sha``.

  ``head_sha`` is the reviewed head the PR carried; the witness commit takes
  its tree (and keeps it reachable as its parent), so the updater can prove
  offline that a local file still holds exactly the bytes the PR carried.
  ``paths`` are the reviewed files. ``merge_sha`` is GitHub's merge commit;
  it need not be fetched yet, because the updater acts only once a release
  contains it. Returns the ref, or None when the reviewed head is not in this
  repository or an input is invalid. Recording the same witness again leaves
  the ref as it is, so a re-run backfill does not move the overlay evidence
  a reviewed update plan is bound to; anything else at the ref is replaced.
  """
  repo = Path(source_dir)
  head = _resolve_commit(repo, head_sha) if head_sha else None
  tree = _tree_oid(repo, head) if head else None
  merge = str(merge_sha or "").strip().lower()
  files = sorted({
    p for p in paths if isinstance(p, str) and p and "\n" not in p and "\0" not in p
  })
  if (
    not isinstance(number, int) or number <= 0 or tree is None or not files
    or not _HEX_OID.fullmatch(merge) or not _GITHUB_REPO_SLUG.fullmatch(repo_slug)
  ):
    return None
  metadata = {
    "version": _MERGED_CONTRIBUTION_FORMAT,
    "number": number,
    "repo": repo_slug,
    "merge_sha": merge,
    "head_sha": head,
    "paths": files,
    "contribution_id": str(contribution_id)[:128],
  }
  ref = f"{MERGED_CONTRIBUTION_PREFIX}/{number}"
  if any(
    existing.ref == ref and existing.metadata == metadata
    for existing in _merged_contributions(repo)
  ):
    return ref
  anchor = _run(
    repo, "commit-tree", tree, "-p", head,
    "-m", json.dumps(metadata, sort_keys=True, separators=(",", ":")),
  ).stdout.strip()
  _run(repo, "update-ref", ref, anchor)
  return ref


def _merged_contributions(repo: Path) -> list[_MergedContribution]:
  listing = _run(
    repo, "for-each-ref", "--format=%(refname)%00%(contents)%1e",
    MERGED_CONTRIBUTION_PREFIX, check=False,
  )
  found = []
  for record in listing.stdout.split("\x1e"):
    ref, _, message = record.strip("\n").partition("\0")
    try:
      metadata = json.loads(message)
    except ValueError:
      continue
    if (
      not isinstance(metadata, dict)
      or metadata.get("version") != _MERGED_CONTRIBUTION_FORMAT
      or not metadata.get("head_sha")
    ):
      continue
    number = metadata.get("number")
    merge = str(metadata.get("merge_sha") or "")
    paths = metadata.get("paths")
    slug = metadata.get("repo")
    if not isinstance(number, int) or number <= 0 or not isinstance(paths, list):
      continue
    found.append(_MergedContribution(
      ref=ref, number=number, repo=slug if isinstance(slug, str) else "",
      merge_sha=merge if _HEX_OID.fullmatch(merge) else None,
      paths=tuple(p for p in paths if isinstance(p, str) and p),
      metadata=metadata,
    ))
  return found


def _pr_merge_commits(repo: Path, revisions: str) -> dict[int, str]:
  """``{N: commit}`` for first-parent commits whose subject ends in ``(#N)``."""
  proc = _run(
    repo, "log", "--first-parent", "-z", "--format=%H %s", revisions, "--",
    check=False,
  )
  merged: dict[int, str] = {}
  for entry in proc.stdout.split("\0"):
    sha, _, subject = entry.strip("\n").partition(" ")
    found = _PR_SUBJECT_SUFFIX.search(subject)
    if found:
      merged.setdefault(int(found.group(1)), sha)
  return merged


def _path_entries(repo: Path, treeish: str, paths: Iterable[str]) -> dict[str, str]:
  """``{path: "mode oid"}`` for the listed files present in ``treeish``."""
  wanted = sorted(set(paths))
  entries = {}
  for start in range(0, len(wanted), 500):
    chunk = wanted[start:start + 500]
    proc = _run(
      repo, "ls-tree", "-r", "-z", "--full-tree", treeish, "--", *chunk,
      env={"GIT_LITERAL_PATHSPECS": "1"},
    )
    listed = set(chunk)
    for line in proc.stdout.split("\0"):
      meta, _, path = line.partition("\t")
      if path in listed:
        mode, _type, oid = meta.split(" ")
        entries[path] = f"{mode} {oid}"
  return entries


def set_aside_merged_work(
  source_dir: str | Path, local: str, upstream: str,
) -> tuple[str, tuple[RetiredPath, ...]]:
  """Return ``local``, or a commit on it with merged pull requests' local work set aside.

  The one retirement planner: Review, Apply, the resolver handoff and the
  status summary all call it, so what status counts and Review returns is
  what Apply does. Superseded drafts are set aside first
  (``without_superseded_drafts``), never changing a path an
  ``owner-choice:local`` unit touched; then unchanged copies of merged pull
  requests retire (``without_retired_units``). The result descends from
  ``local``, so everything set aside stays recoverable and merge bases hold.

  Unit labels only add retirement and owner-choice protection on top of the
  superseded-draft step that predates them. If labelling fails, no owner's
  choice is known and no unit can retire, but superseded drafts are still set
  aside exactly as before units existed; if retirement fails, the drafts set
  aside so far are kept and every local copy stays for the resolver.
  """
  try:
    units = overlay_units(source_dir, local, upstream)
  except Exception:
    log.warning("could not label local overlay units", exc_info=True)
    return without_superseded_drafts(source_dir, local, upstream), ()
  drafted = without_superseded_drafts(source_dir, local, upstream, protected={
    path for unit in units if unit.disposition == "owner-choice:local"
    for path in (*unit.touched, *unit.paths)
  })
  try:
    return without_retired_units(source_dir, drafted, upstream, units)
  except Exception:
    log.warning("could not retire merged local copies", exc_info=True)
    return drafted, ()


def without_retired_units(
  source_dir: str | Path, local: str, upstream: str, units: Iterable[OverlayUnit],
) -> tuple[str, tuple[RetiredPath, ...]]:
  """Return ``local``, or a commit on it with merged pull requests' copies retired.

  A path is retired, taking ``upstream``'s bytes, only when one claim on it
  holds all of:
  - the PR's merge is newly brought by this update (an ancestor of
    ``upstream`` but not of the shared base, as for superseded drafts, so a
    later resolver's choice to keep local is never reverted). For a
    merged-contribution witness that is its recorded merge commit in
    ``PLATFORM_RELEASE_REPO``; for a ``pending-pr:#N`` unit, the first-parent
    release commit whose subject ends in ``(#N)``;
  - that merge commit changed the path, so a wrong claim retires nothing else,
    and ``upstream`` still differs there from the shared base (a merge this
    update also reverts leaves the local change to the ordinary merge);
  - the local bytes are exactly the ones handed to the PR: what its reviewed
    head carried (merged-contribution), or what the ``pending-pr:#N`` unit
    last wrote (pending-pr);
  - no unit that contributed to the path is ``owner-choice:local``, and under
    a pending-pr proof every contributor is a ``pending-pr:#M`` unit this
    update merges, since that unit's bytes may include another unit's earlier
    edit. The reviewed head's bytes are the PR's own, so whatever other units
    shaped them was handed over with it.
  The result's parent is ``local``, so the retired bytes stay recoverable.
  """
  repo = Path(source_dir)
  tip = _resolve_commit(repo, local)
  shared = _run(repo, "merge-base", local, upstream, check=False).stdout.strip()
  if tip is None or not shared:
    return local, ()
  units = list(units)
  owners = {path: unit for unit in units for path in unit.paths}
  contributors: dict[str, list[OverlayUnit]] = {}
  for unit in units:
    for path in unit.touched or unit.paths:
      contributors.setdefault(path, []).append(unit)
  merged_prs = _pr_merge_commits(repo, f"{shared}..{upstream}")
  # path -> claims, tried in order: (PR, merge commit, proof, tree-ish holding
  # the handed-over bytes).
  claims: dict[str, list[tuple[int, str, str, str]]] = {}
  for contribution in _merged_contributions(repo):
    merge = contribution.merge_sha
    if not (
      contribution.for_platform_release
      and ref_is_ancestor(repo, merge, upstream) is True
      and ref_is_ancestor(repo, merge, shared) is False
    ):
      continue
    for path in contribution.paths:
      claims.setdefault(path, []).append(
        (contribution.number, merge, "merged-contribution", contribution.ref),
      )
  # The owning unit last wrote the local bytes, so its claim is tried first.
  for unit, paths in [
    *((u, u.paths) for u in units), *((u, u.touched) for u in units),
  ]:
    if unit.pr in merged_prs and unit.commits:
      claim = (unit.pr, merged_prs[unit.pr], "pending-pr", unit.commits[-1])
      for path in paths:
        if claim not in claims.setdefault(path, []):
          claims[path].append(claim)

  def may_retire(unit: OverlayUnit, proof: str) -> bool:
    if unit.disposition == "owner-choice:local":
      return False
    return proof == "merged-contribution" or unit.pr in merged_prs

  if not claims:
    return local, ()
  local_entries = _path_entries(repo, tip, claims)
  upstream_entries = _path_entries(repo, upstream, claims)
  shared_entries = _path_entries(repo, shared, claims)
  held: dict[str, dict[str, str]] = {}
  changed_by: dict[str, set[str]] = {}
  retired = []
  for path, options in sorted(claims.items()):
    if local_entries.get(path) == upstream_entries.get(path):
      continue
    # The release leaves the path as the shared base had it (the PR merged
    # and was reverted, or a later change undid it): taking its version
    # would only drop the local change, which the ordinary merge keeps.
    if upstream_entries.get(path) == shared_entries.get(path):
      continue
    for pr, merge, proof, holder in options:
      if not all(may_retire(unit, proof) for unit in contributors.get(path, ())):
        continue
      if merge not in changed_by:
        changed_by[merge] = endpoint_diff_paths(repo, f"{merge}^", merge) or set()
      if holder not in held:
        held[holder] = _path_entries(repo, holder, [
          p for p, listed in claims.items() if any(c[3] == holder for c in listed)
        ])
      if path in changed_by[merge] and local_entries.get(path) == held[holder].get(path):
        unit = owners.get(path)
        retired.append(RetiredPath(path, pr, unit.slug if unit else None, proof))
        break
  if not retired:
    return local, ()
  changes = {
    item.path: tuple(upstream_entries[item.path].split(" "))
    if item.path in upstream_entries else ("0", "0" * len(tip))
    for item in retired
  }
  with tempfile.TemporaryDirectory(prefix="mobius-retire-") as tmp:
    index = Path(tmp) / "index"
    _run_with_index(repo, index, "read-tree", tip)
    _run_with_index(
      repo, index, "update-index", "-z", "--index-info",
      input=_index_info(changes, changes),
    )
    tree = _run_with_index(repo, index, "write-tree").stdout.strip()
  prs = ", ".join(f"#{n}" for n in sorted({item.pr for item in retired}))
  commit = _run(
    repo, "commit-tree", tree, "-p", tip,
    "-m", f"Retire local copies of merged pull requests {prs}",
  ).stdout.strip()
  return commit, tuple(retired)


def remove_overlay_worktree(source_dir: str | Path, worktree: str | Path) -> None:
  repo = Path(source_dir)
  path = Path(worktree)
  if path.exists():
    _run(repo, "worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)
  _run(repo, "worktree", "prune", check=False)


def local_diverged_from(source_dir: str | Path, base_commit: str) -> bool:
  """Whether local `main` differs from `base_commit`.

  The caller uses the previous upstream commit as `base_commit`, before
  recording a new upstream version. Exit 0 means no local tree delta;
  non-zero means local edits exist or git could not prove equality.
  """
  proc = _run(
    Path(source_dir), "diff", "--quiet", base_commit, LOCAL_BRANCH,
    check=False,
  )
  return proc.returncode != 0


def require_merge_history(
  source_dir: str | Path,
  left: str = LOCAL_BRANCH,
  right: str = UPSTREAM_BRANCH,
) -> None:
  """Refuse a merge whose base a legacy shallow boundary may hide.

  Local only: callers complete history before taking locks (see
  ``install.fetch_app_commit``). Without a shallow boundary, a missing merge
  base is a genuine fact that the merge seam handles itself.
  """
  repo = Path(source_dir)
  if (
    not _is_shallow(repo)
    or not ref_exists(repo, left)
    or not ref_exists(repo, right)
    or _run(repo, "merge-base", left, right, check=False).stdout.strip()
  ):
    return
  raise HistoryIncomplete(
    f"the app's Git history is incomplete (no merge base between {left} and "
    f"{right}); retry when the app's source is reachable"
  )


def origin_branch(source_dir: str | Path) -> str | None:
  """The last-fetched origin branch, preferring ``main`` and ``master``."""
  branches = [
    name for name in _run(
      Path(source_dir), "for-each-ref", "--format=%(refname:lstrip=3)",
      "refs/remotes/origin/", read_only=True,
    ).stdout.split()
    if name != "HEAD"
  ]
  for preferred in ("main", "master"):
    if preferred in branches:
      return preferred
  return branches[0] if branches else None


def merge_upstream(
  source_dir: str | Path,
  *,
  allow_unrelated_histories: bool = False,
) -> MergeResult:
  """Merges `upstream` into `main` and returns the verdict.

  Uses `git merge-tree --write-tree` to perform the three-way merge in
  memory: it neither moves `main` nor touches the working tree, so a
  conflict cannot leave the app served in a half-merged state. On a clean
  merge `merged_tree_oid` is the resulting tree; the caller materialises the
  WHOLE tree via `read_merged_tree` (there is no single-file shortcut — a
  single-file app is just a tree with one source key) and writes it back. On
  conflict the conflicting paths are returned and `merged_tree_oid` is None —
  the caller leaves the local edits untouched.

  The caller is responsible for advancing `main` to the merged tree through
  the explicit resolution transaction. We deliberately
  do NOT fast-forward `main` here: the merged source has to be recompiled and
  committed as one transactional unit on the caller's side, and moving the
  branch before that would briefly point `main` at a tree the compiled bundle
  doesn't match.
  """
  repo = Path(source_dir)
  ensure_repo(repo)
  require_merge_history(repo)
  ordinary_base = _run(
    repo, "merge-base", LOCAL_BRANCH, UPSTREAM_BRANCH, check=False,
  ).stdout.strip()
  if ordinary_base:
    ordinary = merge_refs(repo, LOCAL_BRANCH, UPSTREAM_BRANCH)
  else:
    if not allow_unrelated_histories:
      raise RuntimeError(
        "local main and recorded upstream have unrelated histories"
      )
    # A rewritten legacy checkout and its explicitly proven catalog origin can
    # have no common commit. The empty tree is the only honest shared state.
    ordinary = merge_refs(
      repo, LOCAL_BRANCH, UPSTREAM_BRANCH, allow_unrelated_histories=True,
    )
    ordinary.unrelated_histories = True
  if ordinary_base:
    ordinary.reconciliation = describe_reconciliation(
      repo,
      ordinary_base,
      UPSTREAM_BRANCH,
      local=LOCAL_BRANCH,
      conflict_paths=ordinary.conflict_paths,
    )
  elif ordinary.conflict_paths:
    ordinary.reconciliation = ReconciliationReceipt(
      unresolved_conflict_paths=tuple(sorted(ordinary.conflict_paths)),
    )
  if ordinary.status == "clean":
    return ordinary
  # A normal three-way merge can conflict when both sides carry the same
  # contributed change under different commit identities and local work later
  # evolved those lines.  Only replace the base when Contribute recorded both
  # causal witnesses; otherwise preserve the ordinary conflict verbatim for the
  # owner-gated agent resolver.
  equivalent = merge_with_equivalent_changes(
    repo, LOCAL_BRANCH, UPSTREAM_BRANCH,
  )
  return equivalent or ordinary


@dataclass(frozen=True)
class GitTreeBlob:
  """A binary range owned by an open Git-tree spool, without retained bytes.

  The spool must outlive consumption. Digest callers traverse in bounded
  chunks; materialization callers explicitly request the complete bytes.
  """

  _stream: BinaryIO
  _offset: int
  size: int

  def read_bytes(self) -> bytes:
    self._stream.seek(self._offset)
    value = self._stream.read(self.size)
    if len(value) != self.size:
      raise RuntimeError("Git tree spool is truncated")
    return value

  def chunks(self) -> Iterable[bytes]:
    position = 0
    while position < self.size:
      self._stream.seek(self._offset + position)
      value = self._stream.read(min(64 * 1024, self.size - position))
      if not value:
        raise RuntimeError("Git tree spool is truncated")
      position += len(value)
      yield value


@contextmanager
def _spooled_git_tree(
  source_dir: str | Path, tree_oid: str,
) -> Iterator[dict[str, GitTreeBlob | None]]:
  """One binary-faithful batch reader for full trees and bounded comparisons.

  Names are NUL-separated; gitlinks are skipped. Missing blobs remain None
  so comparisons distinguish absence from unavailable declared content.
  The disk spool avoids a pipe producer/consumer deadlock on large trees and
  closes on success or failure. Its index holds paths/ranges, not blob bodies.
  """
  repo = Path(source_dir)
  listing = subprocess.run(
    ["git", "-C", str(repo), "ls-tree", "-r", "-z", tree_oid],
    capture_output=True, timeout=_GIT_TIMEOUT, check=True, env=_git_env(repo),
  )
  entries: list[tuple[str, str]] = []
  for record in listing.stdout.split(b"\0"):
    if not record:
      continue
    meta, rel = record.split(b"\t", 1)
    _mode, kind, oid = meta.split()
    if kind == b"blob":
      entries.append((rel.decode(), oid.decode()))
  if not entries:
    yield {}
    return
  with tempfile.TemporaryFile() as out:
    subprocess.run(
      ["git", "-C", str(repo), "cat-file", "--batch"],
      input="".join(f"{oid}\n" for _rel, oid in entries).encode(),
      stdout=out, stderr=subprocess.PIPE,
      timeout=_GIT_TIMEOUT, check=True, env=_git_env(repo),
    )
    out.seek(0)
    blobs: dict[str, GitTreeBlob | None] = {}
    for rel, oid in entries:
      header = out.readline().split()
      if header[:1] != [oid.encode()]:
        raise RuntimeError(f"git cat-file --batch lost its place at {rel}")
      if header[1:] == [b"missing"]:
        blobs[rel] = None
        continue
      if len(header) != 3 or header[1] != b"blob":
        raise RuntimeError(f"Invalid Git blob header at {rel}")
      size = int(header[2])
      if size < 0:
        raise RuntimeError(f"Invalid Git blob size at {rel}")
      blobs[rel] = GitTreeBlob(out, out.tell(), size)
      out.seek(size, os.SEEK_CUR)
      if out.read(1) != b"\n":
        raise RuntimeError(f"Truncated Git blob at {rel}")
    yield blobs


@contextmanager
def open_ref_tree(
  source_dir: str | Path, ref: str,
) -> Iterator[dict[str, GitTreeBlob | None]]:
  """Open immutable ref contents for bounded binary consumption in this scope."""
  repo = Path(source_dir)
  tree_oid = _run(repo, "rev-parse", f"{ref}^{{tree}}").stdout.strip()
  with _spooled_git_tree(repo, tree_oid) as tree:
    yield tree


def read_merged_tree(source_dir: str | Path, tree_oid: str) -> dict[str, bytes]:
  """Materialize EVERY file, preserving the install/merge full-tree contract.

  The same batch/spool owns binary transport as bounded comparisons, but a
  merge deliberately needs the complete bytes rather than a package digest.
  """
  with _spooled_git_tree(source_dir, tree_oid) as tree:
    return {
      rel: blob.read_bytes() for rel, blob in tree.items() if blob is not None
    }


def read_ref_tree(source_dir: str | Path, ref: str) -> dict[str, bytes]:
  """Read EVERY file at `ref` into {repo_relative_path: bytes}.

  `ref` may be a branch, tag, or commit. It is resolved to its tree first so
  callers share the same binary-faithful full-tree reader as clean merges.
  """
  repo = Path(source_dir)
  tree_oid = _run(repo, "rev-parse", f"{ref}^{{tree}}").stdout.strip()
  return read_merged_tree(repo, tree_oid)


def read_ref_subtree(source_dir: str | Path, ref: str, path: str) -> dict[str, bytes]:
  """Read every file under directory `path` at `ref`, keyed repo-relative."""
  repo = Path(source_dir)
  tree_oid = _run(repo, "rev-parse", f"{ref}:{path}").stdout.strip()
  return {f"{path}/{rel}": data for rel, data in read_merged_tree(repo, tree_oid).items()}


def materialize_tree(
  source_dir: str | Path,
  tree_oid: str,
  target_dir: str | Path,
) -> None:
  """Write an immutable Git tree into an empty compile directory.

  Catalog repositories are untrusted input. Paths are confined to
  ``target_dir`` and Git symlinks are written as plain files containing their
  link text, matching ``stage_clone``'s ``core.symlinks=false`` policy.
  Submodules and other non-blob entries are rejected.
  """
  repo = Path(source_dir)
  target = Path(target_dir)
  target.mkdir(parents=True, exist_ok=True)
  listing = subprocess.run(
    ["git", "-C", str(repo), "ls-tree", "-r", "-z", tree_oid],
    capture_output=True, timeout=_GIT_TIMEOUT, check=True, env=_git_env(repo),
  ).stdout
  for raw_entry in listing.split(b"\0"):
    if not raw_entry:
      continue
    raw_meta, separator, raw_path = raw_entry.partition(b"\t")
    if not separator:
      raise RuntimeError("Invalid Git tree entry.")
    try:
      mode, object_type, object_oid = raw_meta.decode("ascii").split(" ", 2)
      rel = raw_path.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
      raise RuntimeError("Invalid Git tree entry.") from exc
    pure = PurePosixPath(rel)
    if (
      pure.is_absolute()
      or not pure.parts
      or any(part in ("", ".", "..") for part in pure.parts)
      or pure.parts[0] == ".git"
    ):
      raise RuntimeError(f"Unsafe Git tree path: {rel!r}")
    if object_type != "blob" or mode not in ("100644", "100755", "120000"):
      raise RuntimeError(
        f"Unsupported Git tree entry {rel!r} ({mode} {object_type})."
      )
    output = target.joinpath(*pure.parts)
    output.parent.mkdir(parents=True, exist_ok=True)
    blob = subprocess.run(
      ["git", "-C", str(repo), "cat-file", "blob", object_oid],
      capture_output=True, timeout=_GIT_TIMEOUT, check=True,
      env=_git_env(repo),
    ).stdout
    output.write_bytes(blob)
    output.chmod(0o755 if mode == "100755" else 0o644)


def read_tree_exec_paths(
  source_dir: str | Path, tree_ish: str
) -> frozenset[str]:
  """Repo-relative paths that are executable (mode 100755) in `tree_ish`.

  `read_ref_tree`/`read_merged_tree` return bytes only, so a cloned-update write
  that re-materialises the tree from those bytes lands every file 0644 and drops
  the executable bit git tracks — a spurious 644-vs-755 mode diff against origin
  that breaks the clean-diff PR property and marks the app diverged. The
  installer restores exec bits for these paths after writing. `tree_ish` may be
  a ref, tag, commit, or a merged tree oid.
  """
  repo = Path(source_dir)
  listing = subprocess.run(
    ["git", "-C", str(repo), "ls-tree", "-r", "-z", tree_ish],
    capture_output=True, timeout=_GIT_TIMEOUT, check=True, env=_git_env(repo),
  )
  out: set[str] = set()
  for entry in listing.stdout.decode().split("\0"):
    if not entry:
      continue
    meta, _, path = entry.partition("\t")
    if path and meta.split(" ", 1)[0] == "100755":
      out.add(path)
  return frozenset(out)


def start_conflict_merge(
  source_dir: str | Path,
  *,
  merge_base: str | None = None,
  allow_unrelated_histories: bool = False,
  local_branch: str = LOCAL_BRANCH,
  upstream_branch: str = UPSTREAM_BRANCH,
) -> list[str]:
  """Starts a REAL merge of `upstream` into the working tree on `main`.

  Unlike `merge_upstream` (an in-memory verdict that never touches the tree),
  this MUTATES the working tree: `git merge --no-commit --no-ff upstream`
  writes conflict markers into the conflicting files and records `MERGE_HEAD`,
  leaving exactly the state a human's `git pull` would. The caller must NOT
  publish afterward: explicit update resolution keeps the prior good bundle
  live until every marker is reconciled, then ``commit_local`` finalizes a
  single-parent replay (linear) on the upstream tip and the installer promotes
  the complete update.

  To back out, the agent runs `git merge --abort`, restoring `main` to its
  pre-merge (committed local) state. Call only after `merge_upstream` verdicted
  a conflict, and under `source_dir_lock`.

  When ``merge_base`` is supplied, it is the proven semantic base returned by
  :func:`merge_with_equivalent_changes`. Git's ordinary merge command cannot
  accept an explicit base, so this path performs the same three-tree merge in
  the real index with ``read-tree -m -u``, writes conflict markers, and records
  the normal MERGE_HEAD/ORIG_HEAD state. Abort/finalize therefore remain the
  same as an ordinary merge while already-landed contribution conflicts stay
  eliminated for both platform and app resolvers.

  When ``allow_unrelated_histories`` is true, Git owns the corresponding
  empty-base merge directly. This is reserved for legacy source whose catalog
  identity is proven even though an earlier apply rewrote its commit history.

  Returns the conflicting repo-relative paths. In the pathological case where
  the materialized merge resolves clean, it resets to the committed local state
  and returns an empty list so the caller never strands a half-merge.
  """
  repo = Path(source_dir)
  ensure_repo(repo)
  if merge_base is None:
    require_merge_history(repo, local_branch, upstream_branch)
  local_sha = head_sha(repo, local_branch)
  upstream_sha = head_sha(repo, upstream_branch)
  if _run(repo, "status", "--porcelain").stdout.strip():
    raise RuntimeError("cannot start conflict merge with local source edits")
  if merge_base is None:
    merge_args = ["merge", "--no-commit", "--no-ff"]
    if allow_unrelated_histories:
      merge_args.append("--allow-unrelated-histories")
    merge_args.append(upstream_branch)
    merged = _run(
      repo, *merge_args, check=False,
    )
  else:
    verdict = merge_refs(
      repo, local_branch, upstream_branch, merge_base=merge_base,
    )
    if verdict.status != "conflict" or not verdict.conflict_paths:
      return []
    try:
      # read-tree resolves every clean path directly and leaves only residual
      # conflicts as staged 1/2/3 entries. The follow-up checkout writes the
      # familiar markers without collapsing the unmerged index, so binary
      # conflicts stay unresolved until someone explicitly stages a side.
      merge_trees_into_worktree(repo, merge_base, local_branch, upstream_branch)
      # read-tree prepares the exact three-stage index but deliberately does
      # only trivial whole-blob resolution. Run Git's standard content driver
      # so disjoint hunks inside one file merge cleanly; a nonzero result is
      # expected while any genuine conflict remains and is classified below.
      _run(
        repo, "merge-index", "-o", "git-merge-one-file", "-a", check=False,
      )
      stages_by_path: dict[str, set[int]] = {}
      for row in _run(repo, "ls-files", "-u", "-z").stdout.split("\0"):
        if "\t" not in row:
          continue
        meta, path = row.split("\t", 1)
        try:
          stage = int(meta.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
          continue
        stages_by_path.setdefault(path, set()).add(stage)
      # git-merge-one-file predates Git's modern merge engine and leaves a
      # stage-1-only entry when both sides deleted the same path (it can even
      # misdescribe this as a delete/mode conflict). The off-tree verdict
      # correctly treats that as clean, so finish that unambiguous deletion
      # before exposing the real residual set.
      for path, stages in stages_by_path.items():
        if stages == {1}:
          _run(repo, "update-index", "--force-remove", "--", path)
      unresolved = sorted(
        path for path, stages in stages_by_path.items() if stages != {1}
      )
      if unresolved:
        # Limit marker checkout to paths that remain unmerged. A blanket "."
        # also consults Git's resolve-undo cache and can resurrect conflicts
        # that merge-index just resolved cleanly inside the same file. Render
        # each path independently because delete/modify and binary conflicts
        # legitimately have no pair of text blobs from which Git can write
        # markers. Their nonzero checkout is expected: the three-stage index
        # remains the authoritative unresolved state while other text paths
        # still receive familiar markers for the resolver.
        for path in unresolved:
          _run(
            repo, "checkout", "--conflict=merge", "--", path, check=False,
          )
      _run(repo, "update-ref", "ORIG_HEAD", local_sha)
      _git_control_path(repo, "MERGE_HEAD").write_text(upstream_sha + "\n")
      _git_control_path(repo, "MERGE_MSG").write_text(
        f"Merge {upstream_branch} with reviewed contribution base\n"
      )
      merged = subprocess.CompletedProcess(
        args=("git", "read-tree"), returncode=1, stdout="", stderr="",
      )
    except Exception:
      _run(repo, "reset", "--hard", local_sha, check=False)
      for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE"):
        _git_control_path(repo, name).unlink(missing_ok=True)
      raise
  # Unmerged paths show in `git status --porcelain` with a U in either status
  # column (UU/AU/UA/UD/DU) or the AA/DD both-added/both-deleted codes.
  status = _run(repo, "status", "--porcelain").stdout
  conflict_paths: list[str] = []
  for line in status.splitlines():
    xy = line[:2]
    if "U" in xy or xy in ("AA", "DD"):
      conflict_paths.append(line[3:].strip())
  if merged.returncode != 0 and not conflict_paths:
    detail = merged.stderr.strip() or merged.stdout.strip()
    raise RuntimeError(f"git merge failed (rc={merged.returncode}): {detail}")
  if not conflict_paths and merge_in_progress(repo):
    # Real/explicit-base materialization resolved clean despite the earlier
    # verdict. Don't strand a dangling merge; restore the committed local state.
    _run(repo, "reset", "--hard", local_sha, check=False)
    for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE"):
      _git_control_path(repo, name).unlink(missing_ok=True)
  return conflict_paths


def abort_in_progress_merge(source_dir: str | Path) -> bool:
  """Abort a half-finished merge left by a prior unresolved conflict.

  When a `start_conflict_merge` was never resolved (MERGE_HEAD still set,
  markers on disk) and a NEW update arrives, the caller must abort the stale
  merge before committing local edits — otherwise the markers get committed as
  source. `git merge --abort` restores `main`'s working tree to its pre-merge
  (committed local) state. Returns True if a merge was aborted, else False.
  """
  repo = Path(source_dir)
  if not merge_in_progress(repo):
    return False
  _run(repo, "merge", "--abort", check=False)
  return True


# A version identifier is never a semantic merge: when both the local edits and
# the new release bump it, take-upstream is always correct. We recognise the two
# forms real apps carry the label in — a JSON manifest's top-level `version` key
# and an in-code `APP_VERSION` const — but the RECOGNITION alone never resolves:
# the value is swapped to upstream's and the merge is re-proven, so a real edit
# that shares the version's file (or line) can never be silently dropped.
_JSON_MANIFESTS = frozenset({"mobius.json", "package.json"})

# A top-level `const APP_VERSION = "x"` (optionally `export`ed / TS-annotated).
# Anchored at column 0 so an indented `VERSION` inside a function, template
# literal, or comment never matches; captures the value substring alone so only
# the value — not the whole line — is swapped.
_APP_VERSION_RE = re.compile(
  rb'^((?:export\s+)?const\s+APP_VERSION\b[^=]*=\s*)(["\'])([^"\']*)(["\'])',
)


def read_blob(source_dir: str | Path, ref: str, rel: str) -> bytes | None:
  """Raw blob bytes of `rel` at `ref`, or None for absent/non-file paths."""
  repo = Path(source_dir)
  proc = subprocess.run(
    ["git", "-C", str(repo), "cat-file", "blob", f"{ref}:{rel}"],
    capture_output=True, timeout=_GIT_TIMEOUT, check=False, env=_git_env(repo),
  )
  return proc.stdout if proc.returncode == 0 else None


_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_REGULAR_FILE_MODES = frozenset({b"100644", b"100755"})


def read_committed_file(
  source_dir: str | Path,
  commit: str,
  rel: str,
  *,
  max_bytes: int,
  timeout: float = 5,
) -> bytes | None:
  """Bytes of one regular file in an exact accepted commit, or None.

  Reads only Git's object database, so uncommitted worktree edits, symlinks,
  and other non-regular tree entries can never reach the caller. ``commit``
  must be a full object id (an accepted ``source_commit``), not a moving ref.
  """
  if not isinstance(commit, str) or not _COMMIT_SHA_RE.fullmatch(commit):
    return None
  repo = Path(source_dir)
  env = _git_env(repo, read_only=True)
  try:
    listing = subprocess.run(
      ["git", "--literal-pathspecs", "-C", str(repo),
       "ls-tree", "-l", "-z", commit, "--", rel],
      capture_output=True, timeout=timeout, check=False, env=env,
    )
    if listing.returncode != 0:
      return None
    meta, tab, name = listing.stdout.removesuffix(b"\0").partition(b"\t")
    fields = meta.split()
    if (
      not tab
      or name != rel.encode()
      or len(fields) != 4
      or fields[0] not in _REGULAR_FILE_MODES
      or fields[1] != b"blob"
      or not fields[3].isdigit()
      or int(fields[3]) > max_bytes
    ):
      return None
    blob = subprocess.run(
      ["git", "-C", str(repo), "cat-file", "blob", fields[2].decode("ascii")],
      capture_output=True, timeout=timeout, check=False, env=env,
    )
  except (OSError, subprocess.SubprocessError, UnicodeError):
    return None
  if blob.returncode != 0 or len(blob.stdout) > max_bytes:
    return None
  return blob.stdout


# Sentinels for the structural JSON merge: a key that is absent on a side, and
# an irreconcilable overlap. Both are `object()` so they compare only by
# identity and can never collide with real JSON values (including `null`).
_JSON_MISSING = object()
_JSON_CONFLICT = object()


def _json_values_equal(left, right) -> bool:
  """Compare JSON structures without Python's boolean/number equivalence."""
  if isinstance(left, dict) and isinstance(right, dict):
    return left.keys() == right.keys() and all(
      _json_values_equal(left[key], right[key]) for key in left
    )
  if isinstance(left, list) and isinstance(right, list):
    return len(left) == len(right) and all(
      _json_values_equal(a, b) for a, b in zip(left, right)
    )
  if isinstance(left, bool) != isinstance(right, bool):
    return False
  return left == right


def _merge_json_value(base, ours, theirs):
  """Recursive three-way merge of one JSON value.

  Returns the merged value, `_JSON_MISSING` when the key should be absent, or
  `_JSON_CONFLICT` when ours and theirs changed the same value incompatibly.
  Objects merge per key; arrays and scalars are atomic — a value either matches
  both sides, matches base on one side (the other side's change wins), or is a
  genuine clash. A missing side is modelled with `_JSON_MISSING`, so an add/add
  of different values and a delete-vs-edit both surface as conflicts. This never
  silently drops a real edit: any true overlap returns `_JSON_CONFLICT` and the
  caller falls back to the owner resolver.
  """
  if _json_values_equal(ours, theirs):
    return ours
  if _json_values_equal(base, ours):
    return theirs
  if _json_values_equal(base, theirs):
    return ours
  # Both sides diverged from base and from each other. Only a per-key object
  # merge can still reconcile disjoint edits; any other shape is a real clash.
  if not (
    isinstance(base, dict) and isinstance(ours, dict) and isinstance(theirs, dict)
  ):
    return _JSON_CONFLICT
  merged: dict = {}
  for key in dict.fromkeys([*base, *ours, *theirs]):
    sub = _merge_json_value(
      base.get(key, _JSON_MISSING),
      ours.get(key, _JSON_MISSING),
      theirs.get(key, _JSON_MISSING),
    )
    if sub is _JSON_CONFLICT:
      return _JSON_CONFLICT
    if sub is not _JSON_MISSING:
      merged[key] = sub
  return merged


def _resolve_json_manifest(base: bytes, ours: bytes, theirs: bytes) -> bytes | None:
  """Structurally three-way merge a JSON manifest (mobius.json / package.json).

  A manifest's meaning is its PARSED structure, not its exact bytes, so
  serialization drift — unicode escaping (`\\u00f6` vs `ö`), whitespace, or key
  order — is not a real conflict, and a per-key three-way merge also reconciles
  disjoint semantic edits (an upstream `version` bump alongside a local additive
  entry, say). Returns the merged manifest bytes, or None when ours and theirs
  genuinely overlap, in which case the caller routes to the owner resolver.
  Fail-safe: any irreconcilable value returns None, so a real local edit is
  never dropped.

  This subsumes the old version-only special case: a pure version bump is just
  the shape where only `theirs` changed one scalar. Byte reuse minimises churn —
  when the merged structure equals one side's parse we return that side's
  original bytes (preferring upstream, which heals accumulated drift); only a
  genuinely combined result is re-serialised, with one deterministic policy.
  """
  try:
    b = json.loads(base)
    o = json.loads(ours)
    t = json.loads(theirs)
  except (ValueError, TypeError):
    return None
  if not (isinstance(b, dict) and isinstance(o, dict) and isinstance(t, dict)):
    return None
  # Upstream owns the top-level release version, including its presence. Keep
  # it outside the ordinary merge so a local deletion cannot remove it and an
  # upstream deletion cannot conflict with a local bump. Nested versions merge
  # normally; originals remain intact for byte reuse.
  merged = _merge_json_value(*(
    {key: value for key, value in side.items() if key != "version"}
    for side in (b, o, t)
  ))
  if merged is _JSON_CONFLICT or not isinstance(merged, dict):
    return None
  if "version" in t:
    merged["version"] = t["version"]
  # Byte reuse minimises churn and heals drift: reuse a side's ORIGINAL bytes
  # only when the merged structure matches that side's ORIGINAL parse (so the
  # reused bytes can't carry a stale version). Prefer upstream. Otherwise the
  # result genuinely combines both sides — serialise it deterministically.
  if _json_values_equal(merged, t):
    return theirs
  if _json_values_equal(merged, o):
    return ours
  return (json.dumps(merged, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _resolve_source_version(base: bytes, ours: bytes, theirs: bytes) -> bytes | None:
  """Resolve a source-file conflict IFF it is confined to the `APP_VERSION`
  value. Swaps ONLY the value substring (never the whole line) into ours, then
  re-runs the three-way merge: if a real edit shares the version's line or file,
  that re-merge conflicts and returns None instead of dropping the edit.
  """
  ours_lines = ours.splitlines(keepends=True)
  theirs_lines = theirs.splitlines(keepends=True)
  om = [
    (i, m) for i, ln in enumerate(ours_lines)
    if (m := _APP_VERSION_RE.match(ln)) and m.group(2) == m.group(4)
  ]
  tm = [
    (i, m) for i, ln in enumerate(theirs_lines)
    if (m := _APP_VERSION_RE.match(ln)) and m.group(2) == m.group(4)
  ]
  if len(om) != 1 or len(tm) != 1:
    return None
  oi, o_match = om[0]
  _, t_match = tm[0]
  if o_match.group(3) == t_match.group(3):
    return None
  line = ours_lines[oi]
  ours_lines[oi] = line[:o_match.start(3)] + t_match.group(3) + line[o_match.end(3):]
  return _three_way_merge_file(base, b"".join(ours_lines), theirs)


def _resolve_benign_conflict_file(
  rel: str, base: bytes | None, ours: bytes, theirs: bytes
) -> bytes | None:
  """One conflicting file auto-resolved, or None when it needs the owner.

  JSON manifests get a full structural three-way merge (serialization drift and
  disjoint edits reconcile; true overlap does not). A manifest both sides added
  merges from the empty object, so only agreeing or disjoint keys combine. Other
  source files keep the narrow APP_VERSION-only line resolution and need a
  base."""
  if rel.rsplit("/", 1)[-1] in _JSON_MANIFESTS:
    return _resolve_json_manifest(b"{}" if base is None else base, ours, theirs)
  if base is None:
    return None
  return _resolve_source_version(base, ours, theirs)


def _three_way_merge_file(
  base: bytes, ours: bytes, theirs: bytes
) -> bytes | None:
  """`git merge-file` on plain bytes: merged content if clean, else None.

  merge-file returns 0 on a clean merge, the conflict count (>0) when markers
  remain, and a negative code on error — every non-zero case is a real
  (non-version) clash we refuse to auto-resolve.
  """
  with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    (d / "ours").write_bytes(ours)
    (d / "base").write_bytes(base)
    (d / "theirs").write_bytes(theirs)
    proc = subprocess.run(
      ["git", "merge-file", "-p", str(d / "ours"), str(d / "base"),
       str(d / "theirs")],
      capture_output=True, timeout=_GIT_TIMEOUT, check=False,
    )
    return proc.stdout if proc.returncode == 0 else None


@dataclass
class BenignResolution:
  """Result of auto-resolving a conflict that carries no genuine overlap.

  `tree` is the full merged source tree (repo-relative path -> bytes) with each
  benign conflict resolved (a JSON manifest structurally merged, a source file's
  APP_VERSION taken from upstream); `tree_oid` is the merge-tree oid it was built
  from, so the caller can read exec bits off the same tree the clean-merge path
  uses (`read_tree_exec_paths`) rather than approximating from a branch.
  `kept_local` names conflicting paths outside the package that keep the
  local version as is.
  """
  tree: dict[str, bytes]
  tree_oid: str
  kept_local: tuple[str, ...] = ()


def resolve_benign_conflict(
  source_dir: str | Path, conflict_paths: list[str],
  *, merge_base: str | None = None,
  package_paths: set[str] | None = None,
  incoming: str = UPSTREAM_BRANCH,
) -> BenignResolution | None:
  """Reconcile package conflicts or keep local ancillary paths, else None.

  Call after a merge with ``incoming`` reports a conflict. Try each narrow
  per-file merge first (structural JSON or APP_VERSION-only line resolution).
  Residual conflicts outside ``package_paths`` retain local bytes/deletions;
  package conflicts still need the owner-resolver flow. The returned tree
  includes all clean merges, and ``kept_local`` reports dropped upstream edits.
  The caller must establish source completeness before accepting kept paths.

  Pass ``merge_base`` when the caller's merge verdict used an explicit base
  (a recorded previous release unrelated to the installed history), so this
  proof reasons from the same base.

  An entry ending in ``/`` protects every path below that directory.
  If the per-file merge fails, a path outside ``package_paths`` can keep its
  local version, including a local deletion of a base file. Synthetic conflict
  paths absent from both local and base trees need the resolver. None protects
  every path.
  """
  repo = Path(source_dir)
  if not conflict_paths:
    return None
  # merge-tree still writes a tree on conflict: non-conflict files are already
  # cleanly three-way merged in it; only the conflict paths carry markers, which
  # we overwrite below. rc must be 1 (conflict); 0 (clean) means the caller
  # shouldn't be here and >1 is a real git error — bail either way. We parse the
  # conflict paths from THIS run (not the caller's list) so the tree oid and the
  # paths we overwrite are guaranteed to come from the same merge — a marker can
  # never leak through a path mismatch.
  args = ["merge-tree", "--write-tree", "--name-only"]
  if merge_base is not None:
    args.extend(("--merge-base", merge_base))
  proc = _run(
    repo, *args, LOCAL_BRANCH, incoming, check=False,
  )
  if proc.returncode != 1:
    return None
  lines = proc.stdout.splitlines()
  tree_oid = lines[0].strip() if lines else ""
  if not tree_oid:
    return None
  merge_conflicts: list[str] = []
  for ln in lines[1:]:
    if not ln.strip():
      break  # blank line ends the path section; messages follow
    merge_conflicts.append(ln)  # verbatim — a path may legitimately hold spaces
  if not merge_conflicts:
    return None
  base_ref = merge_base
  if base_ref is None:
    base_proc = _run(
      repo, "merge-base", LOCAL_BRANCH, incoming, check=False,
    )
    base_ref = base_proc.stdout.strip() if base_proc.returncode == 0 else ""
  if not base_ref:
    return None
  resolved: dict[str, bytes | None] = {}
  kept_local: list[str] = []
  for rel in merge_conflicts:
    ours = read_blob(repo, LOCAL_BRANCH, rel)
    theirs = read_blob(repo, incoming, rel)
    base = read_blob(repo, base_ref, rel)
    merged = (
      _resolve_benign_conflict_file(rel, base, ours, theirs)
      if ours is not None and theirs is not None else None
    )
    if merged is not None:
      resolved[rel] = merged
    elif (
      package_paths is not None
      and not any(
        rel == path or (path.endswith("/") and rel.startswith(path))
        for path in package_paths
      )
      and (ours is not None or base is not None)
    ):
      resolved[rel] = ours
      kept_local.append(rel)
    else:
      return None
  full = read_merged_tree(repo, tree_oid)
  for rel, data in resolved.items():
    if data is None:
      full.pop(rel, None)
    else:
      full[rel] = data
  return BenignResolution(
    tree=full, tree_oid=tree_oid, kept_local=tuple(kept_local),
  )


# Smells
# - record_upstream stages the new blob into the SHARED index (read-tree
#   upstream -> update-index -> write-tree) then read-tree's it back to
#   `main`. That borrows the working index for a write-tree, which is safe
#   only because the caller holds source_dir_lock so no other writer can commit
#   on `main` concurrently. The lock contract is documented at
#   the module top; flagged here because the index-borrow is the spot that
#   relies on it. A conflict-free alternative is a bare temp index
#   (GIT_INDEX_FILE), deferred until a non-locked caller needs it.
# - The whole source tree flows through record_upstream + merge_upstream as one
#   set: record_upstream commits every file in `files` onto the `upstream` tree
#   (the schedule job script is just a key listed in `exec_paths`), and a clean
#   merge_upstream hands back the merged tree oid the caller reads in full via
#   read_merged_tree. A locally edited fetch.sh/build.sh survives a clean update
#   like any other file — there is no entry/sibling/job special-casing.
