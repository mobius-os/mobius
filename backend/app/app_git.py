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
around every entry point here, so explicit apply cannot race the installer's
merge on the same repo. This module does not take the
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
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

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
_GITIGNORE = "\n".join([
  "# Generated build output and vendored deps are not hand-written source.",
  "dist/",
  ".build/",
  "node_modules/",
  "# Python app jobs compile beside source; bytecode is runtime cache, not source.",
  "__pycache__/",
  "*.py[cod]",
  ".pytest_cache/",
  ".mypy_cache/",
  ".ruff_cache/",
  ".coverage",
  "htmlcov/",
  "# Local diagnostics, editor residue, and machine-specific secrets are not source.",
  "*.log",
  ".DS_Store",
  "*.swp",
  "*.swo",
  "*~",
  ".env",
  ".env.local",
  ".env.*.local",
  ".build-*/",
  "# Manifest static_assets are install-managed (re-fetched from the manifest,",
  "# tracked in .mobius-static-assets.json), not edited source — keep runtime",
  "# bundles/binaries out of history while admitting Store listing media through",
  "# the one author-owned subtree reserved by the manifest contract.",
  "static/*",
  "!static/store/",
  "!static/store/**",
  ".mobius-static-assets.json",
  "# Install/rollback snapshots are not source.",
  "*.bak",
  "*.mobius-bak",
  "*.mobius-drop-bak",
  "# init-cron.sh is install-managed (re-generated by the scaffold on every",
  "# update that declares a schedule, dropped by _drop_app_cron when one is",
  "# removed). It must NOT be tracked: tracking it lets a later merge-abort or",
  "# conflict hard-reset restore a dropped script, which the entrypoint boot",
  "# replay then re-arms as an orphan cron (card 099).",
  "init-cron.sh",
  "init-cron.sh.tombstoned",
  ".cron-pending.json",
  "# Runtime workspaces staged by scheduled/background apps are not source.",
  "inputs/",
  "runs/",
  "settings.json",
  "last-run.json",
  "reflection-brief-template.html",
  "fork-chat.sh",
  "fork-session.sh",
  "# Defensive: the integer-id storage tree is a sibling dir, but if a",
  "# numeric data dir ever lands here it is runtime data, not source.",
  "[0-9]*/",
  "",
])

_MANAGED_RUNTIME_PATHS = (
  ".mobius-static-assets.json",
  "*.bak",
  "*.mobius-bak",
  "*.mobius-drop-bak",
  ":(glob)**/*.bak",
  ":(glob)**/*.mobius-bak",
  ":(glob)**/*.mobius-drop-bak",
  "init-cron.sh",
  "init-cron.sh.tombstoned",
  ".cron-pending.json",
  "inputs",
  "runs",
  "settings.json",
  "last-run.json",
  "reflection-brief-template.html",
  "fork-chat.sh",
  "fork-session.sh",
  ":(glob)**/__pycache__/**",
  ":(glob)**/*.py[cod]",
  ":(glob)**/.pytest_cache/**",
  ":(glob)**/.mypy_cache/**",
  ":(glob)**/.ruff_cache/**",
  ":(glob)**/.coverage",
  ":(glob)**/htmlcov/**",
  ":(glob)**/*.log",
  ":(glob)**/.DS_Store",
  ":(glob)**/*.swp",
  ":(glob)**/*.swo",
  ":(glob)**/*~",
  ":(glob)**/.env",
  ":(glob)**/.env.local",
  ":(glob)**/.env.*.local",
  ":(glob)**/.build-*/**",
)

_EXCLUDE_BEGIN = "# BEGIN MOBIUS MANAGED IGNORE RULES"
_EXCLUDE_END = "# END MOBIUS MANAGED IGNORE RULES"

# A fixed identity so commits don't depend on the container's global git
# config (which the mobius user may not have set). The installer commits
# under the upstream identity; local commits keep the same identity since
# the agent is the de-facto author of every local edit.
_GIT_NAME = "Mobius"
_GIT_EMAIL = "mobius@localhost"

# Subprocess timeout. App repos are tiny (one source file plus a couple
# of scripts), so any git op that runs longer than this is wedged, not
# slow.
_GIT_TIMEOUT = 30

# Contribute records a reviewed change as a Git object in the repository that
# owns the live source.  The pending ref proves the reviewed diff came from a
# specific local commit; terminal contribution cleanup promotes it to landed
# only after GitHub reports the PR merged.  Neither ref touches the working
# tree, and both survive branch rewrites because they are ordinary Git refs.
_EQUIVALENCE_PENDING_PREFIX = "refs/mobius/equivalences/pending"
_EQUIVALENCE_LANDED_PREFIX = "refs/mobius/equivalences/landed"
_REVIEWED_SOURCE_PREFIX = "refs/mobius/equivalences/reviewed-source"
_EQUIVALENCE_VERSION = 1
_REVIEWED_SOURCE_VERSION = 1
_HEX_OID = re.compile(r"^[0-9a-f]{40,64}$")
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

  def as_dict(self) -> dict[str, list[str]]:
    return {
      "proven_present": list(self.proven_present),
      "local_only_paths": list(self.local_only_paths),
      "new_upstream_paths": list(self.new_upstream_paths),
      "compatible_paths": list(self.compatible_paths),
      "unresolved_conflict_paths": list(self.unresolved_conflict_paths),
      "provenance_refs_used": list(self.provenance_refs_used),
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
  if read_only:
    # Read-only status/provenance checks must not refresh the durable index or
    # leave an optional lock behind. Mandatory locks for ordinary write paths
    # remain enabled because their callers keep the default environment.
    env["GIT_OPTIONAL_LOCKS"] = "0"
  return env


def _run(
  repo: Path,
  *args: str,
  check: bool = True,
  read_only: bool = False,
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
  return subprocess.run(
    cmd, capture_output=True, text=True, timeout=_GIT_TIMEOUT,
    check=check, env=_git_env(repo, read_only=read_only),
  )


def _run_with_index(
  repo: Path,
  index_file: Path,
  *args: str,
  check: bool = True,
  read_only: bool = False,
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
    check=check, env=env,
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


def is_repo(source_dir: str | Path) -> bool:
  """Whether `source_dir` already holds a git repo."""
  return (Path(source_dir) / ".git").exists()


def worktree_dirty(source_dir: str | Path) -> bool:
  """Whether tracked/untracked accepted-source paths differ from ``main``."""
  if not is_repo(source_dir):
    return False
  return bool(_run(
    Path(source_dir), "status", "--porcelain",
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


def primary_worktree_head(source_dir: str | Path) -> str | None:
  """HEAD of the primary live checkout linked to ``source_dir``, if any."""
  primary = primary_worktree_path(source_dir)
  return _resolve_commit(primary, "HEAD") if primary else None


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


def fetch_origin_commit(source_dir: str | Path, commit_sha: str) -> str:
  """Fetch one immutable origin commit without moving any local ref.

  Publication handoff verification needs GitHub's merge commit in the live app
  object database before it can prove that the reviewed change really landed.
  Importing the object without FETCH_HEAD or a branch update keeps that proof
  read-only with respect to the app's source and updater-owned refs.
  """
  repo = Path(source_dir)
  requested = str(commit_sha or "").lower()
  if not _HEX_OID.fullmatch(requested):
    raise ValueError("invalid origin commit")
  if origin_url(repo) is None:
    raise RuntimeError("source repository has no origin")
  _run(
    repo,
    "fetch", "--quiet", "--no-tags", "--no-write-fetch-head",
    "origin", requested,
  )
  fetched = _resolve_commit(repo, requested)
  if fetched != requested:
    raise RuntimeError("origin returned a different commit")
  return fetched


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


def retire_landed_equivalent_changes(
  source_dir: str | Path, integrated_upstream: str,
) -> int:
  """Drop landed anchors made obsolete by a successfully integrated target."""
  repo = Path(source_dir)
  retired = 0
  for change in _landed_equivalent_changes(repo):
    if _change_landed_in_target(repo, change, integrated_upstream):
      proc = _run(repo, "update-ref", "-d", change.ref, check=False)
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


def origin_url(source_dir: str | Path) -> str | None:
  """Return this app repo's configured ``origin`` URL, if available."""
  if not is_repo(source_dir):
    return None
  proc = _run(
    Path(source_dir), "remote", "get-url", "origin", check=False,
  )
  value = proc.stdout.strip()
  return value if proc.returncode == 0 and value else None


def has_origin(source_dir: str | Path) -> bool:
  """Whether this app repo has a real `origin` remote.

  A cloned catalog app has `origin`; a synthetic-upstream app created via
  `record_upstream` does not. Treat any git failure as false so callers can
  fall back to the synthetic path unchanged.
  """
  return origin_url(source_dir) is not None


def clone_upstream(
  source_dir: str | Path, repo_url: str, ref: str, *, depth: int = 1,
) -> str:
  """Clone a real upstream repo and check out local `main` at `origin/<ref>`.

  This is the REAL-origin install variant of `record_upstream`: instead of
  synthesizing an installer-owned `upstream` branch from fetched bytes, it keeps
  the app repository's own origin and makes `origin/<ref>` the pristine
  upstream. That remote-tracking commit is a real catalog commit an update can
  fetch from and a local fix can be pushed against as a PR. Local edits then
  commit onto `main`, so `git diff origin/<ref> main` is the user's delta.

  `source_dir` may already exist as an empty directory. The clone is built in a
  sibling temp directory first and moved into place after success, so a failed
  clone leaves the caller free to fall back to the synthetic `record_upstream`
  path without a half-created `.git`.

  Returns:
    The checked-out HEAD sha.
  """
  repo = Path(source_dir)
  if repo.exists() and not repo.is_dir():
    raise RuntimeError(f"source_dir exists and is not a directory: {repo}")
  repo.parent.mkdir(parents=True, exist_ok=True)
  clone_parent = repo.parent
  with tempfile.TemporaryDirectory(
    prefix=f".{repo.name}.clone-", dir=clone_parent,
  ) as tmp:
    clone_dir = Path(tmp) / "repo"
    cmd = [
      "git",
      "-c", f"user.name={_GIT_NAME}",
      "-c", f"user.email={_GIT_EMAIL}",
      # core.symlinks=false: check out any tracked symlink as a PLAIN FILE
      # (the link text as content), never a real filesystem symlink. Catalog
      # repos are untrusted content; a materialized symlink (e.g. `static` ->
      # outside the app dir, or `index.jsx` -> /data/service-token.txt) would
      # escape the containment the fetched-source path enforces via
      # _assert_within — that guard is skipped for the cloned tree, so the
      # non-symlink checkout is what keeps the clone inside its own dir.
      "-c", "core.symlinks=false",
      "clone", "-q",
      "--depth", str(depth),
      "--branch", ref,
      repo_url,
      str(clone_dir),
    ]
    subprocess.run(
      cmd, capture_output=True, text=True, timeout=_GIT_TIMEOUT,
      check=True, env=_git_env(repo),
    )
    remote_ref = f"origin/{ref}"
    _run(clone_dir, "rev-parse", "--verify", remote_ref)
    _run(clone_dir, "branch", "-f", UPSTREAM_BRANCH, remote_ref)
    _run(clone_dir, "checkout", "-q", "-B", LOCAL_BRANCH, remote_ref)
    # Keep the app's OWN .gitignore committed (it travels upstream), but layer
    # Möbius's managed-artifact ignores locally via .git/info/exclude so a
    # later `git add -A` never tracks generated per-install files (static/,
    # the static-asset manifest, init-cron.sh, *.bak, the compiled/id trees).
    # A committed catalog .gitignore rarely lists these; without this, the
    # first commit pollutes `main` and — since record_upstream preserves the
    # on-disk .gitignore — never self-heals (the card-099 orphan-cron class).
    _refresh_ignore_rules(clone_dir)
    sha = _run(clone_dir, "rev-parse", "HEAD").stdout.strip()
    repo.mkdir(parents=True, exist_ok=True)
    existing = list(repo.iterdir())
    if existing:
      raise RuntimeError(f"source_dir is not empty: {repo}")
    for child in clone_dir.iterdir():
      shutil.move(str(child), str(repo / child.name))
    return sha


def fetch_upstream(
  source_dir: str | Path,
  ref: str,
  *,
  adopt_equal_local_tree: bool = False,
) -> FetchUpstreamResult:
  """Fetch `origin/<ref>` and advance local `upstream` to that real commit.

  This is the cloned-app update analogue of `record_upstream`: the catalog's
  complete git tree is fetched from the real remote, then the installer-owned
  `upstream` branch is moved to the fetched remote-tracking commit.
  Depth-1 fetches can leave the previous upstream behind a shallow boundary;
  when that happens, unshallow so Git can prove ancestry and the existing merge
  path can compute a real merge base.

  ``adopt_equal_local_tree`` is the narrow legacy-adoption exception. The
  caller must first prove that this repo's configured origin is the trusted
  package identity. When the fetched origin commit has the exact same complete
  tree as local ``main``, moving the old synthetic ``upstream`` ref cannot
  overwrite a local byte; rebinding it to the real origin repairs provenance
  without asking the owner to resolve unrelated installer history.

  Returns:
    The fetched commit and any trusted equal-tree adoption proof.
  """
  repo = Path(source_dir)
  previous = _run(
    repo, "rev-parse", "--verify", UPSTREAM_BRANCH, check=False,
  )
  previous_sha = previous.stdout.strip() if previous.returncode == 0 else ""
  _run(repo, "fetch", "--depth", "1", "origin", ref)
  remote_ref = f"origin/{ref}"
  sha = _run(repo, "rev-parse", "--verify", remote_ref).stdout.strip()
  # A depth-one fetch can re-graft even an unchanged tip. Repair based on the
  # relationship the installer actually needs (local main ↔ fetched tip), not
  # only on whether the remote SHA string changed.
  _restore_shallow_history_if_needed(repo, LOCAL_BRANCH, remote_ref)
  equal_local_adoption = (
    adopt_equal_local_tree
    and ref_trees_equal(repo, LOCAL_BRANCH, remote_ref)
  )
  if previous_sha and previous_sha != sha:
    related = _run(
      repo, "merge-base", "--is-ancestor", previous_sha, sha, check=False,
    )
    if related.returncode != 0 and not equal_local_adoption:
      raise RuntimeError(
        f"origin/{ref} is unrelated to recorded upstream {previous_sha}; "
        "falling back to manifest-source update"
      )
  _run(repo, "branch", "-f", UPSTREAM_BRANCH, remote_ref)
  return FetchUpstreamResult(
    sha=sha,
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
  if not (repo / ".git" / "MERGE_HEAD").exists():
    return False
  unmerged = _run(repo, "ls-files", "-u").stdout.strip()
  if unmerged:
    return True
  return has_conflict_markers(repo)


def merge_in_progress(source_dir: str | Path) -> bool:
  """Whether Git records an in-progress merge for this repository."""
  repo = Path(source_dir)
  if not is_repo(repo):
    return False
  git_path = _run(repo, "rev-parse", "--git-path", "MERGE_HEAD").stdout.strip()
  path = Path(git_path)
  if not path.is_absolute():
    path = repo / path
  return path.is_file()


def has_conflict_markers(source_dir: str | Path) -> bool:
  """Whether an in-progress merge's working tree still has text markers."""
  repo = Path(source_dir)
  if not (repo / ".git" / "MERGE_HEAD").exists():
    return False
  # Scan tracked content for the labeled conflict boundaries. `git grep`
  # exits 0 when a line matches, 1 when none do. We match only `<<<<<<< ` and
  # `>>>>>>> ` (7 chars + the space before the ref that `git merge` writes),
  # never the bare `=======` separator that `git diff --check` would flag on
  # legitimate content — see the docstring.
  markers = _run(repo, "grep", "-lE", r"^(<<<<<<< |>>>>>>> )", check=False)
  return markers.returncode == 0


def has_unresolved_binary_conflicts(source_dir: str | Path) -> bool:
  """Keep binary conflicts gated until the resolver explicitly stages them.

  Text conflicts can be proven resolved by removal of Git's marker boundaries,
  after which ``commit_local`` safely stages them. Binary conflicts have no
  markers, so an unmerged binary path is never auto-accepted.
  """
  repo = Path(source_dir)
  if not (repo / ".git" / "MERGE_HEAD").exists():
    return False
  listing = _run(repo, "ls-files", "-u", "-z").stdout
  paths = {
    row.split("\t", 1)[1]
    for row in listing.split("\0")
    if "\t" in row
  }
  for rel in paths:
    worktree = repo / rel
    try:
      if b"\0" in worktree.read_bytes():
        return True
    except OSError:
      pass
    for stage in (1, 2, 3):
      blob = subprocess.run(
        ["git", "-C", str(repo), "show", f":{stage}:{rel}"],
        capture_output=True, timeout=_GIT_TIMEOUT, check=False,
        env=_git_env(repo),
      )
      if blob.returncode == 0 and b"\0" in blob.stdout:
        return True
  return False


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
  git_dir = repo / ".git"
  merge_head_path = git_dir / "MERGE_HEAD"
  # Refuse to touch the index while a rebase/cherry-pick/am is in progress
  # (see the docstring's HARD GATE). Scoped to "no MERGE_HEAD" so the merge
  # finalize path below still stages-then-scans. `ls-files -u` covers a
  # conflict left in the index with no state dir (e.g. a bare `read-tree -m`).
  if not merge_head_path.exists():
    in_progress = (
      (git_dir / "rebase-merge").exists()
      or (git_dir / "rebase-apply").exists()
      or (git_dir / "CHERRY_PICK_HEAD").exists()
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
      (repo / ".git" / name).unlink(missing_ok=True)
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


# ── Linear overlay ───────────────────────────────────────────────────────────
#
# A project's local history is an exact accepted upstream commit plus a LINEAR
# overlay of intentional commits. Each overlay commit declares which unit it
# belongs to and why it is local through commit trailers, so the disposition
# survives every replay (a rebase preserves messages) with no side metadata.
# The updater replays this overlay onto each new upstream base instead of
# merging upstream into local history, so upstream never becomes an interleaved
# side parent and a unit that has landed upstream simply disappears.

OVERLAY_UNIT_TRAILER = "Mobius-Overlay-Unit"
OVERLAY_DISPOSITION_TRAILER = "Mobius-Disposition"
OVERLAY_RECORD_TRAILER = "Mobius-Record"
# ``pending``: has a Contribute record on its way upstream. ``local-only``: an
# intentional overlay this installation keeps. ``private``: never leaves the
# instance. ``wip``: not yet organised.
OVERLAY_DISPOSITIONS = ("pending", "local-only", "private", "wip")
# Commits without a unit trailer are still overlay commits; they belong to the
# implicit unit Contribute organises later.
OVERLAY_UNSORTED_UNIT = "unsorted"
# Uncommitted working edits ride through an update as this transient unit and
# return to the working tree afterwards.
OVERLAY_WORKING_UNIT = "working-tree"
# A pre-overlay (merge-shaped) local history is folded into this one unit the
# first time the linear updater runs on it.
OVERLAY_LEGACY_UNIT = "legacy-overlay"
_OVERLAY_UNIT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_OVERLAY_LOG_FORMAT = (
  "%H%x00%s%x00"
  f"%(trailers:key={OVERLAY_UNIT_TRAILER},valueonly=true)%x00"
  f"%(trailers:key={OVERLAY_DISPOSITION_TRAILER},valueonly=true)%x00"
  f"%(trailers:key={OVERLAY_RECORD_TRAILER},valueonly=true)%x1e"
)
_CONFLICT_BOUNDARY_RE = r"^(<<<<<<< |>>>>>>> )"


class OverlayNotLinear(RuntimeError):
  """The local range contains merge commits; it is not a linear overlay."""


@dataclass(frozen=True)
class OverlayCommit:
  sha: str
  subject: str
  unit: str
  disposition: str
  record_id: str | None


@dataclass(frozen=True)
class OverlayUnit:
  id: str
  disposition: str
  record_id: str | None
  commits: tuple[str, ...]


@dataclass(frozen=True)
class OverlayReplayResult:
  """Outcome of replaying an overlay onto a new base inside a worktree.

  ``status`` is ``clean`` (``tip`` is the complete candidate) or ``conflict``
  (the worktree is left at the conflicting cherry-pick for a resolver;
  ``conflict`` names the commit, its unit, the unmerged paths and the shas
  still to replay). ``replayed`` maps old shas to their replayed shas and
  ``dropped`` lists old shas whose change was already present in the base.
  """

  status: str
  tip: str | None
  replayed: tuple[tuple[str, str], ...]
  dropped: tuple[str, ...]
  conflict: dict | None = None


def overlay_trailers(
  unit: str, disposition: str, record_id: str | None = None,
) -> str:
  if not _OVERLAY_UNIT_RE.match(unit):
    raise ValueError(f"invalid overlay unit {unit!r}")
  if disposition not in OVERLAY_DISPOSITIONS:
    raise ValueError(f"invalid overlay disposition {disposition!r}")
  # Provenance must never be ambiguous: a pending unit names the Contribute
  # record carrying it upstream, and only a pending unit may name one.
  if (disposition == "pending") != bool(record_id):
    raise ValueError(
      "a pending overlay unit needs its Contribute record id; other "
      "dispositions must not carry one"
    )
  lines = [
    f"{OVERLAY_UNIT_TRAILER}: {unit}",
    f"{OVERLAY_DISPOSITION_TRAILER}: {disposition}",
  ]
  if record_id:
    lines.append(f"{OVERLAY_RECORD_TRAILER}: {record_id}")
  return "\n".join(lines)


def overlay_message(
  subject: str,
  *,
  unit: str,
  disposition: str,
  record_id: str | None = None,
  body: str = "",
) -> str:
  """A commit message carrying the overlay trailers."""
  parts = [subject.strip()]
  if body.strip():
    parts.append(body.strip())
  parts.append(overlay_trailers(unit, disposition, record_id))
  return "\n\n".join(parts) + "\n"


def _first_line(value: str) -> str:
  return value.strip().splitlines()[0].strip() if value.strip() else ""


def _overlay_log(repo: Path, *rev_args: str) -> list[OverlayCommit]:
  proc = _run(
    repo, "log", f"--format={_OVERLAY_LOG_FORMAT}", *rev_args, read_only=True,
  )
  commits: list[OverlayCommit] = []
  for record in proc.stdout.split("\x1e"):
    if not record.strip():
      continue
    fields = record.strip("\n").split("\x00")
    fields.extend([""] * (5 - len(fields)))
    unit = _first_line(fields[2])
    disposition = _first_line(fields[3])
    record_id = _first_line(fields[4])
    if not _OVERLAY_UNIT_RE.match(unit):
      unit = OVERLAY_UNSORTED_UNIT
    if disposition not in OVERLAY_DISPOSITIONS:
      disposition = "wip"
    # Read leniently so a hand-written or legacy commit still replays, but
    # never report provenance the trailers do not actually establish.
    if disposition == "pending" and not record_id:
      disposition = "wip"
    if disposition != "pending":
      record_id = ""
    commits.append(OverlayCommit(
      sha=fields[0].strip(), subject=fields[1].strip(), unit=unit,
      disposition=disposition, record_id=record_id or None,
    ))
  return commits


def overlay_commits(
  source_dir: str | Path, base: str, tip: str,
) -> list[OverlayCommit]:
  """The overlay ``base..tip`` in replay order; raises when it is not linear."""
  repo = Path(source_dir)
  merges = _run(
    repo, "rev-list", "--merges", "--count", f"{base}..{tip}", read_only=True,
  ).stdout.strip()
  if merges and int(merges) > 0:
    raise OverlayNotLinear(
      f"{merges} merge commit(s) between {base[:12]} and {tip[:12]}"
    )
  return _overlay_log(repo, "--reverse", "--no-merges", f"{base}..{tip}")


def overlay_units(commits: Iterable[OverlayCommit]) -> list[OverlayUnit]:
  order: list[str] = []
  grouped: dict[str, dict] = {}
  for commit in commits:
    entry = grouped.get(commit.unit)
    if entry is None:
      entry = grouped[commit.unit] = {
        "disposition": commit.disposition,
        "record_id": commit.record_id,
        "commits": [],
      }
      order.append(commit.unit)
    entry["commits"].append(commit.sha)
    if commit.record_id and not entry["record_id"]:
      entry["record_id"] = commit.record_id
  return [
    OverlayUnit(
      id=unit,
      disposition=grouped[unit]["disposition"],
      record_id=grouped[unit]["record_id"],
      commits=tuple(grouped[unit]["commits"]),
    )
    for unit in order
  ]


def describe_overlay(source_dir: str | Path, base: str, tip: str) -> dict:
  """The overlay invariant as a status projection.

  ``linear`` is the invariant itself: the base is an ancestor of the tip and no
  merge commit sits between them. Units are listed in replay order with their
  disposition so Settings and Contribute can show exactly what this
  installation carries on top of upstream.
  """
  repo = Path(source_dir)
  base_sha = _resolve_commit(repo, base)
  tip_sha = _resolve_commit(repo, tip)
  out = {
    "base_sha": base_sha,
    "tip_sha": tip_sha,
    "base_is_ancestor": False,
    "linear": False,
    "commits": 0,
    "unsorted_commits": 0,
    "units": [],
  }
  if not base_sha or not tip_sha:
    return out
  out["base_is_ancestor"] = ref_is_ancestor(repo, base_sha, tip_sha) is True
  if not out["base_is_ancestor"]:
    return out
  try:
    commits = overlay_commits(repo, base_sha, tip_sha)
  except OverlayNotLinear:
    return out
  subjects = {commit.sha: commit.subject for commit in commits}
  out["linear"] = True
  out["commits"] = len(commits)
  out["unsorted_commits"] = sum(
    1 for commit in commits if commit.unit == OVERLAY_UNSORTED_UNIT
  )
  out["units"] = [
    {
      "id": unit.id,
      "disposition": unit.disposition,
      "record_id": unit.record_id,
      "commits": len(unit.commits),
      "subject": subjects.get(unit.commits[0], ""),
    }
    for unit in overlay_units(commits)
  ]
  return out


def _same_change(
  repo: Path, *, base: str, tip: str, change: EquivalentChange,
) -> bool:
  """Whether ``base..tip`` is exactly the reviewed delta of ``change``.

  Two strict three-way proofs, both byte-exact on trees: the reviewed delta
  adds nothing to ``tip`` (it is already there), and ``base..tip`` adds
  nothing to the reviewed head (it carries nothing beyond the review). Whitespace
  or indentation differences fail both, as they must for source where they
  matter.
  """
  return (
    _change_is_subsumed(repo, change.base_sha, change.anchor_sha, tip)
    and _change_is_subsumed(repo, base, tip, change.anchor_sha)
  )


def landed_overlay_commits(
  source_dir: str | Path, commits: Iterable[OverlayCommit], target: str,
) -> set[str]:
  """Overlay commits whose exact reviewed change already landed in ``target``.

  A squash or rebase merge gives a contribution a new identity upstream, so a
  replay of the local original would conflict instead of vanishing. Each
  landed equivalence anchor proves one reviewed delta reached the target. A
  contiguous run of same-unit commits whose combined delta is exactly that
  reviewed delta is the landed contribution and is dropped before replay;
  when a run is not the whole review, each commit is tried on its own. A
  commit carrying more than the reviewed delta keeps replaying, where its
  remainder either applies or conflicts honestly.
  """
  repo = Path(source_dir)
  anchors = [
    change for change in _landed_equivalent_changes(repo)
    if _change_landed_in_target(repo, change, target)
  ]
  ordered = list(commits)
  if not anchors or not ordered:
    return set()
  runs: list[list[OverlayCommit]] = []
  for commit in ordered:
    if runs and runs[-1][-1].unit == commit.unit:
      runs[-1].append(commit)
    else:
      runs.append([commit])
  dropped: set[str] = set()

  def _proven(span: list[OverlayCommit]) -> bool:
    base = _resolve_commit(repo, f"{span[0].sha}^")
    if base is None:
      return False
    return any(
      _same_change(repo, base=base, tip=span[-1].sha, change=change)
      for change in anchors
    )

  for run in runs:
    if _proven(run):
      dropped.update(commit.sha for commit in run)
      continue
    if len(run) > 1:
      dropped.update(
        commit.sha for commit in run if _proven([commit])
      )
  return dropped


def remove_overlay_worktree(source_dir: str | Path, worktree: str | Path) -> None:
  repo = Path(source_dir)
  path = Path(worktree)
  if path.exists():
    _run(repo, "worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)
  _run(repo, "worktree", "prune", check=False)


def _worktree_head(worktree: Path) -> str:
  return _run(worktree, "rev-parse", "HEAD", read_only=True).stdout.strip()


def _worktree_unmerged(worktree: Path) -> list[str]:
  out = _run(
    worktree, "diff", "--name-only", "--diff-filter=U", check=False,
    read_only=True,
  ).stdout
  return sorted(line.strip() for line in out.splitlines() if line.strip())


def _semantic_replay_prefix(
  repo: Path,
  commits: list[OverlayCommit],
  start: int,
  onto: str,
) -> tuple[int, MergeResult] | None:
  """Prove a conflicted overlay prefix through its reviewed source tip.

  A source-resolution witness can become complete in a later commit than the
  one where ordinary cherry-picking first conflicts. The later commits may be
  consumed by the semantic replay only when every path they touch belongs to
  the exact reviewed change. That keeps unrelated overlay units separate while
  allowing one reviewed adaptation to replay as the atomic unit it was.
  """
  for end in range(start, len(commits)):
    merged = merge_with_equivalent_changes(repo, commits[end].sha, onto)
    if (
      merged is None
      or merged.status != "clean"
      or not merged.merged_tree_oid
      or not merged.equivalent_change_refs
    ):
      continue
    reviewed_paths: set[str] = set()
    valid = True
    for ref in merged.equivalent_change_refs:
      change = _read_equivalent_change(repo, ref)
      paths = (
        endpoint_diff_paths(repo, change.base_sha, change.anchor_sha)
        if change is not None else None
      )
      if not paths:
        valid = False
        break
      reviewed_paths.update(paths)
    if not valid:
      continue
    consumed_paths: set[str] = set()
    for consumed in commits[start + 1:end + 1]:
      parent = _resolve_commit(repo, f"{consumed.sha}^")
      paths = (
        endpoint_diff_paths(repo, parent, consumed.sha)
        if parent is not None else None
      )
      if paths is None:
        valid = False
        break
      consumed_paths.update(paths)
    if valid and consumed_paths.issubset(reviewed_paths):
      return end, merged
  return None


def _replay_into(
  repo: Path,
  worktree: Path,
  commits: list[OverlayCommit],
  skip: set[str],
  replayed: list[tuple[str, str]],
  dropped: list[str],
) -> OverlayReplayResult:
  for index, commit in enumerate(commits):
    if commit.sha in skip:
      dropped.append(commit.sha)
      continue
    previous_tip = _worktree_head(worktree)
    pick = _run(worktree, "cherry-pick", "--no-commit", commit.sha, check=False)
    unmerged = _worktree_unmerged(worktree)
    if unmerged:
      # A source-resolution witness may bind a reviewed adaptation at a later
      # source tip even though the adapted bytes entered in this earlier
      # commit. Re-run the same proof against this commit and, when it is exact,
      # materialize the proven semantic merge tree while preserving the
      # original commit message and overlay trailers.
      semantic = (
        _semantic_replay_prefix(repo, commits, index, previous_tip)
        if not replayed else None
      )
      if semantic is not None:
        consumed_through, equivalent = semantic
        restored = _run(
          worktree, "reset", "-q", "--hard", previous_tip, check=False,
        )
        if restored.returncode == 0:
          _run(
            worktree, "read-tree", "--reset", "-u",
            equivalent.merged_tree_oid,
          )
          if _run(
            worktree, "diff", "--cached", "--quiet", check=False,
          ).returncode == 0:
            dropped.append(commit.sha)
          else:
            _run(worktree, "commit", "-q", "--no-verify", "-C", commit.sha)
            replayed.append((commit.sha, _worktree_head(worktree)))
          skip.update(
            later.sha for later in commits[index + 1:consumed_through + 1]
          )
          continue
      return OverlayReplayResult(
        status="conflict",
        tip=_worktree_head(worktree),
        replayed=tuple(replayed),
        dropped=tuple(dropped),
        conflict={
          "sha": commit.sha,
          "unit": commit.unit,
          "subject": commit.subject,
          "paths": unmerged,
          "remaining": [later.sha for later in commits[index + 1:]],
        },
      )
    nothing_staged = _run(
      worktree, "diff", "--cached", "--quiet", check=False,
    ).returncode == 0
    if nothing_staged:
      # The change is already in the base: a landed contribution whose
      # identity Git could not otherwise connect. Nothing to carry.
      _run(worktree, "cherry-pick", "--quit", check=False)
      _run(worktree, "reset", "-q", "--hard", check=False)
      dropped.append(commit.sha)
      continue
    if pick.returncode != 0:
      raise RuntimeError(
        f"cherry-pick {commit.sha[:12]} failed: "
        f"{(pick.stderr or pick.stdout).strip()[-500:]}"
      )
    _run(worktree, "commit", "-q", "--no-verify", "-C", commit.sha)
    replayed.append((commit.sha, _worktree_head(worktree)))
  return OverlayReplayResult(
    status="clean",
    tip=_worktree_head(worktree),
    replayed=tuple(replayed),
    dropped=tuple(dropped),
  )


def replay_overlay(
  source_dir: str | Path,
  *,
  commits: Iterable[OverlayCommit],
  onto: str,
  worktree: str | Path,
  skip: Iterable[str] = (),
) -> OverlayReplayResult:
  """Replay ``commits`` onto ``onto`` in a fresh detached worktree.

  Every commit keeps its author, message and trailers. A commit in ``skip``
  or one whose cherry-pick is empty is dropped as already landed. A conflict
  is retried only through an exact landed-change semantic proof; otherwise the
  first conflict stops the replay and leaves the worktree for a resolver. No
  ref of the live checkout moves here.
  """
  repo = Path(source_dir)
  path = Path(worktree)
  remove_overlay_worktree(repo, path)
  path.parent.mkdir(parents=True, exist_ok=True)
  _run(repo, "worktree", "add", "--detach", "-q", str(path), onto)
  return _replay_into(repo, path, list(commits), set(skip), [], [])


def replay_more(
  source_dir: str | Path,
  *,
  worktree: str | Path,
  commits: Iterable[OverlayCommit],
  previous: OverlayReplayResult,
) -> OverlayReplayResult:
  """Replay further commits onto an existing clean candidate worktree."""
  repo = Path(source_dir)
  path = Path(worktree)
  return _replay_into(
    repo, path, list(commits), set(),
    list(previous.replayed), list(previous.dropped),
  )


def continue_overlay_replay(
  source_dir: str | Path,
  *,
  worktree: str | Path,
  commit_sha: str,
  remaining: Iterable[str],
  skip: Iterable[str] = (),
) -> OverlayReplayResult | None:
  """Finish a conflicting replay after the resolver edited the worktree.

  Returns None while unmerged entries or labeled conflict boundaries remain.
  Otherwise commits the resolved pick under its original message, replays the
  remaining commits and returns the new outcome.
  """
  repo = Path(source_dir)
  path = Path(worktree)
  if not (path / ".git").exists():
    return None
  if _run(path, "ls-files", "-u", read_only=True).stdout.strip():
    return None
  markers = _run(
    path, "grep", "-lE", _CONFLICT_BOUNDARY_RE, check=False, read_only=True,
  )
  if markers.returncode == 0:
    return None
  _run(path, "add", "-A")
  replayed: list[tuple[str, str]] = []
  dropped: list[str] = []
  if _run(path, "diff", "--cached", "--quiet", check=False).returncode == 0:
    _run(path, "cherry-pick", "--quit", check=False)
    dropped.append(commit_sha)
  else:
    _run(path, "commit", "-q", "--no-verify", "-C", commit_sha)
    replayed.append((commit_sha, _worktree_head(path)))
  shas = [sha for sha in remaining if sha]
  commits = _overlay_log(repo, "--no-walk=unsorted", *shas) if shas else []
  return _replay_into(repo, path, commits, set(skip), replayed, dropped)


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


def _restore_shallow_history_if_needed(
  source_dir: str | Path,
  left: str = LOCAL_BRANCH,
  right: str = UPSTREAM_BRANCH,
) -> None:
  """Restore remote history when a shallow graft may hide a merge base.

  Depth-one app fetches can make ``main`` and ``upstream`` appear unrelated
  even though both came from the same origin. The update verdict and the
  resolver's real merge both require that base, so they self-heal at their
  entry points. A failed/offline fetch raises without moving refs or the
  working tree. If unshallowing proves the histories are genuinely unrelated,
  return normally: the merge seam handles that valid legacy-adoption shape
  with Git's empty-base semantics.
  """
  repo = Path(source_dir)
  shallow_raw = _run(repo, "rev-parse", "--git-path", "shallow").stdout.strip()
  shallow_path = Path(shallow_raw)
  if not shallow_path.is_absolute():
    shallow_path = repo / shallow_path
  if not shallow_path.is_file():
    return
  if not ref_exists(repo, left) or not ref_exists(repo, right):
    return
  base = _run(repo, "merge-base", left, right, check=False)
  if base.returncode == 0 and base.stdout.strip():
    return
  if not has_origin(repo):
    raise RuntimeError(
      f"no merge base between {left} and {right} in shallow repo without origin"
    )
  fetched = _run(
    repo, "fetch", "--unshallow", "--no-tags", "origin", check=False,
  )
  if fetched.returncode != 0:
    detail = fetched.stderr.strip() or fetched.stdout.strip()
    raise RuntimeError(f"failed to restore app git history: {detail}")


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
  _restore_shallow_history_if_needed(repo)
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


def read_merged_tree(source_dir: str | Path, tree_oid: str) -> dict[str, bytes]:
  """Read EVERY file of a merged tree oid into {repo_relative_path: bytes}.

  The single source-tree path: every clean-merge caller (app install + the
  platform layer) materialises the WHOLE merged tree from
  `MergeResult.merged_tree_oid` here and writes it back, so one and many files
  share one path. Paths are repo-relative POSIX; bytes are read binary-faithful
  (no text decode). `-z` keeps paths NUL-separated so names with spaces or
  newlines survive.
  """
  repo = Path(source_dir)
  listing = subprocess.run(
    ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--name-only", tree_oid],
    capture_output=True, timeout=_GIT_TIMEOUT, check=True, env=_git_env(repo),
  )
  files: dict[str, bytes] = {}
  for rel in listing.stdout.decode().split("\0"):
    if not rel:
      continue
    blob = subprocess.run(
      ["git", "-C", str(repo), "cat-file", "-p", f"{tree_oid}:{rel}"],
      capture_output=True, timeout=_GIT_TIMEOUT, check=False, env=_git_env(repo),
    )
    if blob.returncode == 0:
      files[rel] = blob.stdout
  return files


def read_ref_tree(source_dir: str | Path, ref: str) -> dict[str, bytes]:
  """Read EVERY file at `ref` into {repo_relative_path: bytes}.

  `ref` may be a branch, tag, or commit. It is resolved to its tree first so
  callers share the same binary-faithful full-tree reader as clean merges.
  """
  repo = Path(source_dir)
  tree_oid = _run(repo, "rev-parse", f"{ref}^{{tree}}").stdout.strip()
  return read_merged_tree(repo, tree_oid)


def materialize_tree(
  source_dir: str | Path,
  tree_oid: str,
  target_dir: str | Path,
) -> None:
  """Write an immutable Git tree into an empty compile directory.

  Catalog repositories are untrusted input. Paths are confined to
  ``target_dir`` and Git symlinks are written as plain files containing their
  link text, matching ``clone_upstream``'s ``core.symlinks=false`` policy.
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
  _restore_shallow_history_if_needed(repo, local_branch, upstream_branch)
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
      # conflicts remain gated by has_unresolved_binary_conflicts.
      _run(
        repo, "read-tree", "-m", "-u",
        merge_base, local_branch, upstream_branch,
      )
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
      git_dir = repo / ".git"
      (git_dir / "MERGE_HEAD").write_text(upstream_sha + "\n")
      (git_dir / "MERGE_MSG").write_text(
        f"Merge {upstream_branch} with reviewed contribution base\n"
      )
      merged = subprocess.CompletedProcess(
        args=("git", "read-tree"), returncode=1, stdout="", stderr="",
      )
    except Exception:
      _run(repo, "reset", "--hard", local_sha, check=False)
      for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE"):
        (repo / ".git" / name).unlink(missing_ok=True)
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
  if not conflict_paths and (repo / ".git" / "MERGE_HEAD").exists():
    # Real/explicit-base materialization resolved clean despite the earlier
    # verdict. Don't strand a dangling merge; restore the committed local state.
    _run(repo, "reset", "--hard", local_sha, check=False)
    for name in ("MERGE_HEAD", "MERGE_MSG", "MERGE_MODE"):
      (repo / ".git" / name).unlink(missing_ok=True)
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
  if not is_repo(repo) or not (repo / ".git" / "MERGE_HEAD").exists():
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
  """Raw bytes of `rel` at `ref`, or None if the path is absent there."""
  repo = Path(source_dir)
  proc = subprocess.run(
    ["git", "-C", str(repo), "cat-file", "-p", f"{ref}:{rel}"],
    capture_output=True, timeout=_GIT_TIMEOUT, check=False, env=_git_env(repo),
  )
  return proc.stdout if proc.returncode == 0 else None


def _resolve_json_version(base: bytes, ours: bytes, theirs: bytes) -> bytes | None:
  """Resolve a JSON-manifest conflict IFF the two sides differ ONLY in the
  top-level `version` key — then take upstream's whole file.

  Structured (not textual), so a nested `dependencies.version`, a non-version
  local edit, or a minified layout can never be misread: any of those makes the
  non-version content differ, which returns None → owner resolves it. `base` is
  unused because the invariant (ours == theirs except `version`) already proves
  no non-version local edit would be dropped.
  """
  try:
    o = json.loads(ours)
    t = json.loads(theirs)
  except (ValueError, TypeError):
    return None
  if not isinstance(o, dict) or not isinstance(t, dict):
    return None
  if "version" not in o or "version" not in t or o["version"] == t["version"]:
    return None
  o_rest = {k: v for k, v in o.items() if k != "version"}
  t_rest = {k: v for k, v in t.items() if k != "version"}
  if o_rest != t_rest:
    return None
  return theirs


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


def _resolve_version_only_file(
  rel: str, base: bytes, ours: bytes, theirs: bytes
) -> bytes | None:
  """One conflicting file resolved to upstream's version, or None when the
  conflict is not confined to the version identifier."""
  if rel.rsplit("/", 1)[-1] in _JSON_MANIFESTS:
    return _resolve_json_version(base, ours, theirs)
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
class VersionOnlyResolution:
  """Result of auto-resolving a version-only conflict.

  `tree` is the full merged source tree (repo-relative path -> bytes) with the
  version taken from upstream; `tree_oid` is the merge-tree oid it was built
  from, so the caller can read exec bits off the same tree the clean-merge path
  uses (`read_tree_exec_paths`) rather than approximating from a branch.
  """
  tree: dict[str, bytes]
  tree_oid: str


def resolve_version_only_conflict(
  source_dir: str | Path, conflict_paths: list[str]
) -> VersionOnlyResolution | None:
  """Full merged source tree with a VERSION-ONLY conflict resolved to upstream,
  or None when the conflict is not confined to the version line.

  Call only after `merge_upstream` verdicted a conflict. We PROVE the conflict
  is version-only rather than assume it: for every conflicting file we normalise
  ours's version line to upstream's and re-run the three-way FILE merge. If they
  all merge clean, the version label was the sole conflict and we return the
  whole merged tree (non-conflict files carry their clean three-way merge; the
  conflicting files carry the version-normalised merge). If any file still
  conflicts — a real code clash — we return None and the caller falls back to
  the owner-resolver flow. Fail-safe by construction: a genuine local edit is
  never silently dropped, because a residual conflict aborts the whole attempt.
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
  proc = _run(
    repo, "merge-tree", "--write-tree", "--name-only",
    LOCAL_BRANCH, UPSTREAM_BRANCH, check=False,
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
  base_proc = _run(
    repo, "merge-base", LOCAL_BRANCH, UPSTREAM_BRANCH, check=False,
  )
  base_ref = base_proc.stdout.strip()
  if base_proc.returncode != 0 or not base_ref:
    return None
  resolved: dict[str, bytes] = {}
  for rel in merge_conflicts:
    ours = read_blob(repo, LOCAL_BRANCH, rel)
    theirs = read_blob(repo, UPSTREAM_BRANCH, rel)
    base_blob = read_blob(repo, base_ref, rel)
    # An add/add or delete conflict (a side missing the file) is not the
    # version-bump shape; leave it to the owner.
    if ours is None or theirs is None or base_blob is None:
      return None
    merged = _resolve_version_only_file(rel, base_blob, ours, theirs)
    if merged is None:
      return None
    resolved[rel] = merged
  full = read_merged_tree(repo, tree_oid)
  full.update(resolved)
  return VersionOnlyResolution(tree=full, tree_oid=tree_oid)


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
