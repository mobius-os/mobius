"""A Store update parked on a conflict, until its resolution is installed.

When an update conflicts with the owner's edits, install records a receipt
under the app's ``.git`` instead of materializing it. The receipt binds the
exact candidate, and a private checkout beside it is where a resolver
reconciles the update. Install promotes the committed resolution later, and
clearing the receipt is what marks the update installed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from sqlalchemy.orm import Session

from app import app_git, models, settled
from app.storage_io import atomic_write


_PENDING_UPDATE_DIR = "mobius-pending-update"


def stage_pending_conflict_update(
  source_dir: str | Path,
  *,
  app_id: int,
  upstream_commit: str,
  manifest: dict,
  raw_base: str,
  capability_digest: str,
  candidate_digest: str,
  merge_base_override: str | None = None,
) -> None:
  """Persist everything explicit resolution needs to finish an update.

  A conflict deliberately returns before install materialization. The resolver
  may run minutes later (or after a restart), so its exact identity must
  outlive the request. The digest binds every fetched source/static/icon/seed
  byte; replay refetches and refuses to promote if any byte moved. The receipt
  lives under ``.git`` and is replaced atomically, so a restart sees either the
  previous complete candidate or the new complete candidate, never a gap. A
  newer conflicting release supersedes any resolution parked for an older one;
  the same release conflicting again keeps the resolver's checkout.
  """
  repo = Path(source_dir)
  git_dir = repo / ".git"
  target = git_dir / _PENDING_UPDATE_DIR
  if target.is_symlink():
    raise ValueError("pending update path must not be a symlink")
  target.mkdir(parents=True, exist_ok=True)
  try:
    previous = json.loads((target / "receipt.json").read_text(encoding="utf-8"))
  except (OSError, ValueError):
    previous = None
  if not isinstance(previous, dict) or (
    previous.get("upstream_commit") != upstream_commit
  ):
    _drop_pending_update_worktree(repo)
  atomic_write(target / "receipt.json", json.dumps({
    "schema": 4,
    "app_id": app_id,
    "upstream_commit": upstream_commit,
    "manifest": manifest,
    "raw_base": raw_base,
    "capability_digest": capability_digest,
    "candidate_digest": candidate_digest,
    "merge_base_override": merge_base_override,
  }, ensure_ascii=False, sort_keys=True) + "\n")


def pending_update_worktree(source_dir: str | Path) -> Path:
  """The private checkout where a resolver reconciles one pending update.

  It lives inside the app's own git directory, so the served source directory
  never holds a half-merged tree: ordinary edits and applies keep working
  while a resolver works, and finalization merges those late edits with the
  resolution through the same three-way merge every update uses.
  """
  return Path(source_dir) / ".git" / _PENDING_UPDATE_DIR / "worktree"


_CONFLICT_MARKER = r"^(<{7,} |>{7,} )"


def committed_conflict_marker_paths(
  repo: str | Path, ref: str, upstream_commit: str,
) -> list[str] | None:
  """Paths the resolution changed from upstream that commit a conflict
  boundary. Unchanged upstream content is never a conflict, even if it
  legitimately contains such a line. None when Git cannot tell."""
  repo = Path(repo)
  changed = app_git._run(
    repo, "diff", "--name-only", "-z", upstream_commit, ref, check=False,
  )
  if changed.returncode != 0:
    return None
  paths = [path for path in changed.stdout.split("\0") if path]
  if not paths:
    return []
  found = app_git._run(
    repo, "grep", "-z", "-lE", _CONFLICT_MARKER, ref, "--",
    *(f":(literal){path}" for path in paths),
    check=False,
  )
  if found.returncode > 1:
    return None
  return [p.removeprefix(f"{ref}:") for p in found.stdout.split("\0") if p]


def committed_pending_resolution(
  source_dir: str | Path, upstream_commit: str,
) -> str | None:
  """The resolver's committed answer for ``upstream_commit``, if it has one.

  The answer is the private checkout's commit, and only when it contains the
  pending upstream, nothing is left uncommitted or mid-merge, and it commits no
  conflict markers. Git has already refused to commit any unresolved path.
  """
  worktree = pending_update_worktree(source_dir)
  if (
    not (worktree / ".git").exists()
    or app_git.merge_in_progress(worktree)
    or app_git.worktree_dirty(worktree)
    or app_git.ref_is_ancestor(worktree, upstream_commit, "HEAD") is not True
    or committed_conflict_marker_paths(worktree, "HEAD", upstream_commit) != []
  ):
    return None
  return app_git._run(worktree, "rev-parse", "HEAD").stdout.strip()


def pending_update_resolved(source_dir: str | Path, upstream_commit: str) -> bool:
  """Whether nothing but installation is left for this pending update.

  Either the private checkout holds a committed answer, or ``main`` already
  contains the upstream: a resolver from an earlier release committed there,
  or an install moved ``main`` and was interrupted before completing. In both
  cases rerunning the installer promotes it; the receipt stays until then.
  """
  return (
    app_git.ref_is_ancestor(source_dir, upstream_commit, app_git.LOCAL_BRANCH)
    is True
    or committed_pending_resolution(source_dir, upstream_commit) is not None
  )


def _drop_pending_update_worktree(source_dir: str | Path) -> None:
  worktree = pending_update_worktree(source_dir)
  if os.path.lexists(worktree):
    app_git.remove_overlay_worktree(source_dir, worktree)


def pending_update_receipt_file(source_dir: str | Path) -> Path:
  """The durable "not installed yet" marker of a pending update."""
  return Path(source_dir) / ".git" / _PENDING_UPDATE_DIR / "receipt.json"


_HEX_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def pending_conflict_update_receipt_present(source_dir: str | Path) -> bool:
  """Whether any pending-receipt filesystem entry needs validation."""
  root = Path(source_dir) / ".git" / _PENDING_UPDATE_DIR
  receipt_path = root / "receipt.json"
  return os.path.lexists(root) or os.path.lexists(receipt_path)


class PendingReceiptUnreadable(Exception):
  """A pending-update receipt exists but cannot be read or is not valid, so
  whether its update is still pending is unknown."""


def read_pending_conflict_update_receipt(
  source_dir: str | Path, *, app_id: int, upstream_commit: str | None,
) -> dict | None:
  """Read validated pending metadata without loading potentially large assets.

  Receipts written by earlier releases (schemas 1-3) carried a resolution
  policy and a reviewed tree; both are ignored because the resolver's own
  commit is now the complete answer. A receipt that cannot be read or is
  invalid reads as none here; ``checked_pending_conflict_update_receipt``
  tells those apart from a missing receipt.
  """
  try:
    return checked_pending_conflict_update_receipt(
      source_dir, app_id=app_id, upstream_commit=upstream_commit,
    )
  except PendingReceiptUnreadable:
    return None


def checked_pending_conflict_update_receipt(
  source_dir: str | Path, *, app_id: int, upstream_commit: str | None,
) -> dict | None:
  """The validated receipt pending ``upstream_commit``, or None when proven
  absent: no receipt, or a well-formed receipt for a different update.

  A receipt that exists but cannot be read, does not parse, is a symlink, or
  names this update with invalid content raises ``PendingReceiptUnreadable``.
  """
  root = Path(source_dir) / ".git" / _PENDING_UPDATE_DIR
  receipt_path = root / "receipt.json"
  if root.is_symlink() or receipt_path.is_symlink():
    raise PendingReceiptUnreadable(f"symlinked pending receipt in {source_dir}")
  try:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
  except FileNotFoundError:
    # Only a genuinely absent receipt is "none". Anything else, including a
    # regular file where the receipt directory belongs, is unreadable.
    return None
  except (OSError, ValueError) as exc:
    raise PendingReceiptUnreadable(f"pending receipt in {source_dir}") from exc
  if not isinstance(receipt, dict):
    raise PendingReceiptUnreadable(f"pending receipt in {source_dir}")
  named = receipt.get("upstream_commit")
  if not upstream_commit or (
    isinstance(named, str) and _HEX_COMMIT.fullmatch(named)
    and named != upstream_commit
  ):
    # Proven absent for this commit: a later update's receipt does not
    # unseat an earlier one.
    return None
  schema = receipt.get("schema")
  merge_base_override = (
    receipt.get("merge_base_override") if schema in (3, 4) else None
  )
  candidate_digest = receipt.get("candidate_digest")
  if (
    schema not in (1, 2, 3, 4)
    or receipt.get("app_id") != app_id
    or named != upstream_commit
    or not isinstance(receipt.get("manifest"), dict)
    or not isinstance(receipt.get("raw_base"), str)
    or not isinstance(receipt.get("capability_digest"), str)
    or not isinstance(candidate_digest, str)
    or not re.fullmatch(r"[0-9a-f]{64}", candidate_digest)
    or (
      merge_base_override is not None
      and (
        not isinstance(merge_base_override, str)
        or not _HEX_COMMIT.fullmatch(merge_base_override)
      )
    )
  ):
    raise PendingReceiptUnreadable(f"invalid pending receipt in {source_dir}")
  receipt["merge_base_override"] = merge_base_override
  return receipt


async def clear_pending_conflict_update(db: Session, app: models.App) -> None:
  """Remove the receipt and resolver binding; caller holds the source lock."""
  source_dir = app.source_dir
  # The receipt is the durable "not installed yet" marker, so it goes first. A
  # crash afterwards leaves only a stray checkout, which the next conflict or
  # resolver replaces.
  pending_update_receipt_file(source_dir).unlink(missing_ok=True)
  app.conflict_resolver_chat_id = None
  app.conflict_resolver_upstream_commit = None
  db.commit()
  await settled.run_settled(_drop_pending_update_worktree, source_dir)
  shutil.rmtree(
    Path(source_dir) / ".git" / _PENDING_UPDATE_DIR,
    ignore_errors=True,
  )
