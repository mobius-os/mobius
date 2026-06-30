"""Shell self-update — a thin ``/data/shell`` caller of ``app.app_git``.

The shell repo at ``/data/shell`` updates with the SAME two-branch engine the
apps and the platform use: ``app_git`` records the baked image source on an
``upstream`` branch, merges it into the local working branch with
``git merge-tree --write-tree`` (a clean-vs-conflict verdict computed off the
live worktree, so a conflict never leaves the served shell half-merged), and
finalises a clean apply as a single-parent linear replay. This module owns only
the shell-specific lifecycle: which baked floor to record, the gitignore that
keeps the auth components and build artifacts out of git entirely, the rebuild
trigger, and the resolver chat.

The shell is the SIMPLEST of the three callers:

1. A shell rebuild is HOT — ``vite build`` recompiles ``/data/shell/dist`` in
   place and the next page load picks it up — so there is no "restart needed"
   flag the way the platform has. A clean apply just trips a rebuild.

2. The auth components (``LoginForm``, ``SetupWizard``, ``ProviderAuth``) are
   root-owned ``chmod 444`` in the live tree (``protected-files.txt``). The
   ``mobius`` user that runs this engine CANNOT and MUST NOT overwrite them.
   Rather than special-case them in the merge path, they leave the git model
   entirely: ``.gitignore`` lists them, ``_baked_shell_files`` never records
   them on ``upstream``, so they simply never appear in any merged tree. They
   update only via the image (deploy / entrypoint), never in-product — exactly
   the same gitignore-respecting shape the platform uses for its recovery files.

Availability is an EXACT ancestry check, not a sha-string compare: an update is
available iff ``upstream`` is NOT already an ancestor of the working branch (the
same ``git merge-base --is-ancestor`` model ``app_git`` uses).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TypedDict

from sqlalchemy.orm import Session

from app import app_git, platform_update


SHELL_REPO = Path("/data/shell")
SHELL_SRC = Path("/app/shell-src")
# Persist a conflict so Settings keeps surfacing it across reloads (the merge is
# NOT materialised on disk, so MERGE_HEAD alone can't signal it). Records the
# conflicting upstream sha + paths + resolver chat id, same format the platform
# updater uses.
CONFLICT_FLAG = Path("/data/.shell-conflict")
# Set by a clean apply; the entrypoint (or a sibling) compiles the new source
# into /data/shell/dist when it sees this flag. A rebuild is hot — no restart.
REBUILD_FLAG = Path("/data/.shell-rebuild-needed")

# Paths gitignored out of the shell repo. The three auth-component dirs are
# root-owned chmod 444 (protected-files.txt), so they leave the git model
# entirely and the image owns them; dist/node_modules/.vite are build output and
# vendored deps, never hand-written source.
_GITIGNORE_DIRS = (
  "src/components/LoginForm/",
  "src/components/SetupWizard/",
  "src/components/ProviderAuth/",
  "dist/",
  "node_modules/",
  ".vite/",
)

# Serialise applies in-process (uvicorn is single-worker; this is belt-and-braces
# against a double-click racing two merges on the same repo).
_APPLY_LOCK = asyncio.Lock()


class ShellStatus(TypedDict):
  """Response shape for ``GET /api/shell/status``."""

  available: bool
  current_build_sha: str | None
  seed_required: bool
  conflict: bool
  conflict_paths: list[str]
  conflict_chat_id: str | None


class ShellApplyResult(TypedDict):
  """Response shape for ``POST /api/shell/apply``."""

  state: str
  upstream_commit: str | None
  merge_commit: str | None
  conflict_paths: list[str]
  chat_id: str | None


def _shell_gitignore_body() -> str:
  """The ``.gitignore`` text for the shell repo.

  Anchored entries (leading slash) so each matches only the repo-root path, the
  auth components leave the git model entirely (image-managed, root-owned 444),
  and the build artifacts (``dist``, ``node_modules``, ``.vite``) are generated
  output, not hand-written source.
  """
  lines = [
    "# Auth components are image-managed (root-owned chmod 444); they leave",
    "# the shell git model entirely. See protected-files.txt.",
  ]
  lines.extend(f"/{d}" for d in _GITIGNORE_DIRS)
  return "\n".join(lines) + "\n"


def _is_ignored(rel: str) -> bool:
  """Whether a repo-relative path falls under a gitignored dir."""
  return any(rel == d.rstrip("/") or rel.startswith(d) for d in _GITIGNORE_DIRS)


def _baked_shell_files(src: Path | None = None) -> dict[str, bytes]:
  """Read the baked shell source into ``{repo_relative_path: bytes}``.

  Walks ``/app/shell-src`` and returns every source file EXCEPT the gitignored
  set (the auth components + dist + node_modules + .vite) and ``.git`` — so the
  auth components and the build output are never recorded on ``upstream``, just
  like the platform's recovery files. Paths are repo-relative POSIX.

  ``src`` defaults to the module's :data:`SHELL_SRC` read at CALL time (not bound
  as a default-arg value), so tests that monkeypatch the global retarget the
  baked source.
  """
  src = src or SHELL_SRC
  files: dict[str, bytes] = {}
  for path in src.rglob("*"):
    if not path.is_file():
      continue
    rel = path.relative_to(src).as_posix()
    if rel == ".git" or rel.startswith(".git/"):
      continue
    if _is_ignored(rel):
      continue
    files[rel] = path.read_bytes()
  return files


def record_shell_upstream(repo: Path = SHELL_REPO) -> str:
  """Commit the baked ``/app/shell-src`` source onto ``upstream``.

  Records the gitignore-respecting baked tree (no auth components, no build
  output) as the canonical "this is what the image shipped" snapshot, WITHOUT
  disturbing the checked-out ``main`` working tree. Returns the new upstream
  commit sha (the merge base a later update diverges from). Stamps the version
  with the current image build sha so history reads cleanly.
  """
  version = platform_update.current_build_sha() or "baked"
  return app_git.record_upstream(
    repo, _baked_shell_files(), "image:shell-src", version,
  )


def seed_shell_repo(repo: Path = SHELL_REPO) -> bool:
  """Seed the two-branch shell git repo, preserving existing agent edits.

  Idempotent: a no-op if ``<repo>/.git`` already exists. Otherwise it builds the
  same ``upstream`` (pristine image source) + ``main`` (local working branch)
  shape the apps and platform use:

    1. ``git init`` on the ``upstream`` branch and write the SHELL ``.gitignore``
       (auth components + dist + node_modules + .vite) so every later ``git add``
       honours it — the auth components never enter git, the image owns them.
    2. ``record_shell_upstream`` records the BAKED ``/app/shell-src`` source onto
       ``upstream`` (the pristine floor).
    3. Commit the CURRENT on-disk ``/data/shell`` working tree onto ``main`` as a
       SINGLE-parent replay on the ``upstream`` tip — whatever is there, including
       agent edits — so existing edits are preserved as the local delta on top of
       the baked floor AND ``upstream`` is a direct ancestor of ``main``.

  Two correctness properties:

  - This NEVER resets the working tree to the baked source, so an existing
    instance whose ``/data/shell/src`` already carries agent edits keeps them; a
    fresh boot (where the working tree equals the baked source) records an empty
    local delta.
  - ``main`` descends linearly from ``upstream`` (via ``commit_replay``), so the
    ``git merge-base --is-ancestor`` availability check reads "not available"
    right after a seed, exactly like an app at install. A later image bump
    advances ``upstream`` past ``main`` and the check then reads "available".

  Returns True if it seeded.
  """
  if app_git.is_repo(repo):
    return False
  repo.mkdir(parents=True, exist_ok=True)
  app_git._run(repo, "init", "-q", "-b", app_git.UPSTREAM_BRANCH)
  # Write the shell .gitignore BEFORE anything is staged so the auth components
  # and build output are filtered out of every branch from the first commit.
  (repo / ".gitignore").write_text(_shell_gitignore_body(), encoding="utf-8")
  app_git._run(repo, "add", ".gitignore")
  app_git._run(
    repo, "commit", "-q", "-m", "Initialize shell repo", "--allow-empty",
  )
  # Branch `main` from the empty root and check it out FIRST, so `main` exists
  # before `record_shell_upstream` runs — `record_upstream` restores the index
  # to `main` at the end, which would fail if `main` were absent. The working
  # tree on disk (the agent's edits) is untouched by these ref ops.
  app_git._run(repo, "branch", app_git.LOCAL_BRANCH, app_git.UPSTREAM_BRANCH)
  app_git._run(repo, "checkout", "-q", app_git.LOCAL_BRANCH)
  # Record the baked image source onto `upstream`. `record_upstream` preserves
  # the .gitignore already on the upstream tip (the shell one we just committed),
  # so the auth components are never recorded.
  upstream_tip = record_shell_upstream(repo)
  # Replay the CURRENT working tree (the agent's on-disk edits) onto `main` as a
  # single-parent commit on the upstream tip: agent edits preserved AND `main`
  # descends linearly from `upstream` so availability is an exact ancestry check.
  app_git.commit_replay(repo, upstream_tip, "shell: seed local working tree")
  return True


def shell_status(repo: Path = SHELL_REPO) -> ShellStatus:
  """Compute update availability on demand (no daemon, no polling).

  Availability is an EXACT ancestry check: when ``upstream`` exists, an update is
  available iff ``upstream`` is NOT an ancestor of the working branch (the baked
  source carries commits the working branch has not replayed). When no
  ``upstream`` branch exists yet, the repo predates the two-branch model and an
  update is pending a one-time seed. A persisted conflict short-circuits to
  "not available" so Settings keeps surfacing the resolver chat.
  """
  build_sha = platform_update.current_build_sha()
  conflict = CONFLICT_FLAG.exists() or (repo / ".git" / "MERGE_HEAD").exists()
  if conflict:
    flag = _read_conflict_flag() or {}
    return ShellStatus(
      available=False, current_build_sha=build_sha, seed_required=False,
      conflict=True, conflict_paths=flag.get("paths") or [],
      conflict_chat_id=flag.get("chat_id"),
    )

  has_upstream = _has_branch(app_git.UPSTREAM_BRANCH, repo)
  if has_upstream:
    anc = app_git._run(
      repo, "merge-base", "--is-ancestor",
      app_git.UPSTREAM_BRANCH, app_git.LOCAL_BRANCH, check=False,
    )
    available = anc.returncode != 0
    seed_required = False
  else:
    # No upstream branch yet (pre-feature / unseeded repo). An update is pending
    # the one-time seed the entrypoint or the first apply performs.
    available = app_git.is_repo(repo)
    seed_required = available

  return ShellStatus(
    available=available, current_build_sha=build_sha,
    seed_required=seed_required, conflict=False, conflict_paths=[],
    conflict_chat_id=None,
  )


def _has_branch(name: str, repo: Path = SHELL_REPO) -> bool:
  """Whether ``refs/heads/<name>`` exists in the repo."""
  if not app_git.is_repo(repo):
    return False
  return app_git._run(
    repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}",
    check=False,
  ).returncode == 0


def _write_conflict_flag(
  upstream: str | None, paths: list[str], chat_id: str | None = None
) -> None:
  """Persist a conflict so Settings keeps surfacing it across reloads.

  Line 0 is the conflicting upstream sha; an optional ``chat:<id>`` line records
  the resolver chat; the remaining lines are the conflicting paths.
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


def _write_merged_source(merged: dict[str, bytes], repo: Path) -> None:
  """Write a CLEAN merge result back to ``/data/shell`` file-by-file.

  Each file is written via a temp + fsync + ``os.replace`` so a crash never
  leaves a truncated source file, then tracked files that vanished from the
  merged tree are deleted. The auth components are gitignored out of the repo, so
  they are never in the merged tree and are never touched here — the image owns
  them. Skips ``.git``.
  """
  written: set[str] = set()
  for rel, data in merged.items():
    if rel == ".git" or rel.startswith(".git/"):
      continue
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    with open(tmp, "wb") as fh:
      fh.write(data)
      fh.flush()
      os.fsync(fh.fileno())
    os.replace(tmp, target)
    written.add(rel)
  tracked = app_git._run(repo, "ls-files").stdout.splitlines()
  for rel in tracked:
    rel = rel.strip()
    if not rel or rel in written:
      continue
    try:
      (repo / rel).unlink()
    except OSError:
      pass


def _apply_sync(repo: Path) -> dict:
  """The blocking git work of an apply (run under ``asyncio.to_thread``).

  A thin sequence over the ``app_git`` engine:

    1. ``commit_local`` — stash any uncommitted agent edits so the merge has a
       committed base to diverge from.
    2. ``record_shell_upstream`` — put the new baked source on ``upstream``.
    3. ``merge_upstream`` — the clean-vs-conflict verdict off the live worktree.
       Clean → write the merged source + single-parent ``commit_replay`` + trip
       the rebuild flag. Conflict → record the flag; the async wrapper spawns the
       resolver chat.

  Returns a dict describing the outcome; the async wrapper does the (async)
  conflict-chat spawn.
  """
  if CONFLICT_FLAG.exists() or (repo / ".git" / "MERGE_HEAD").exists():
    return {"state": "conflict", "conflict_paths": [],
            "upstream_commit": None, "merge_commit": None}

  app_git.commit_local(repo, "shell: local edits before update")
  new_upstream = record_shell_upstream(repo)
  result = app_git.merge_upstream(repo)

  if result.status == "conflict":
    # Do NOT materialise markers here: the new source is on `upstream`; the agent
    # reconciles the named files into `main` and the rebuild follows.
    _write_conflict_flag(new_upstream, result.conflict_paths)
    return {"state": "conflict", "conflict_paths": result.conflict_paths,
            "upstream_commit": new_upstream, "merge_commit": None}

  merged = app_git.read_merged_tree(repo, result.merged_tree_oid)
  _write_merged_source(merged, repo)
  # Finalise as a SINGLE-parent linear replay on the new upstream tip, so `main`
  # is a straight-line descendant of `upstream` and the next update's
  # `--is-ancestor` availability check is exact.
  merge_commit = app_git.commit_replay(repo, new_upstream, "shell: self-update")
  CONFLICT_FLAG.unlink(missing_ok=True)
  # A shell rebuild is hot — trip the flag and the next boot (or a sibling)
  # recompiles dist; nothing restarts.
  REBUILD_FLAG.write_text(platform_update.current_build_sha() or "baked")
  return {"state": "updated", "conflict_paths": [],
          "upstream_commit": new_upstream, "merge_commit": merge_commit}


async def apply_shell_update(
  db: Session, repo: Path = SHELL_REPO,
) -> ShellApplyResult:
  """Apply the current image's baked shell source to local ``main``. Clean →
  written + rebuild tripped. Conflict → ``upstream`` recorded + an agent resolver
  chat opened. Never restarts (a shell rebuild is hot)."""
  async with _APPLY_LOCK:
    outcome = await asyncio.to_thread(_apply_sync, repo)
    chat_id: str | None = None
    if outcome["state"] == "conflict":
      chat_id = await spawn_shell_conflict_chat(db, outcome["conflict_paths"])
      # Stamp the resolver chat into the persisted flag so a Settings reload can
      # still link straight to it, falling back to the recorded flag for every
      # field when this apply bailed on a pre-existing conflict (no fresh
      # upstream/paths and a deduped None chat id).
      existing = _read_conflict_flag() or {}
      _write_conflict_flag(
        outcome["upstream_commit"] or existing.get("upstream"),
        outcome["conflict_paths"] or existing.get("paths") or [],
        chat_id or existing.get("chat_id"),
      )
    return ShellApplyResult(
      state=outcome["state"],
      upstream_commit=outcome["upstream_commit"],
      merge_commit=outcome["merge_commit"],
      conflict_paths=outcome["conflict_paths"],
      chat_id=chat_id,
    )


async def spawn_shell_conflict_chat(
  db: Session, conflict_paths: list[str],
) -> str | None:
  """Open a visible agent chat to merge the new shell source into ``main`` and
  resolve conflicts — the shell analogue of
  ``platform_update.spawn_platform_conflict_chat``. Dedupes on a running
  resolver. The auth components are gitignored + image-managed, so they are never
  part of the merge — no protected-file caution is needed."""
  import time
  import uuid

  from app import models
  from app.broadcast import create_broadcast, get_system_broadcast
  from app.chat import (
    current_run_generation, discard_starting, mark_starting, run_chat,
  )
  from app.chat_writer import StartTurn, alloc_run_token, await_ack, get_writer
  from app.push import notify_owner

  title = "Resolve shell update conflict"
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

  files = ", ".join(conflict_paths) if conflict_paths else "some shell files"
  content = (
    "A shell update is ready but conflicts with local edits — the new "
    "version and the local changes both touched the same lines, so it can't "
    "apply cleanly.\n\n"
    "The new shell source is recorded on the `upstream` branch of the git "
    "repo at `/data/shell`. Reconcile these conflicting files into `main` "
    f"by hand: {files}.\n\n"
    "Resolve it with ordinary git: `git -C /data/shell merge upstream` "
    "writes conflict markers into the listed files; for each, combine the "
    "intent of the local version and upstream's, save it, then `git add` it. "
    "When every listed file is reconciled, `git commit` to finalise the merge "
    "on `main`. Then rebuild the shell (`bash /app/scripts/rebuild_shell.sh`) "
    "so the new bundle is served, and clear the flag "
    "(`rm -f /data/.shell-conflict`).\n\n"
    "To back out instead, `git -C /data/shell merge --abort`, "
    "`rm -f /data/.shell-conflict`, and tell the owner the update was skipped."
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
        db, owner.id, title="Shell update needs conflict resolution",
        body="The shell update conflicts with local edits. Opened a chat to resolve it.",
        source_type="shell_conflict", source_id=chat_id, target=f"chat:{chat_id}",
      )
    except Exception:
      pass

  return chat_id
