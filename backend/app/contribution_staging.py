"""Derive a reviewed pull-request candidate from its review checkout.

Contribute's review gate is only as good as the candidate it names. Staging is
therefore platform-owned: an agent commits on a review branch and asks to stage
it; this module derives every reviewed input (base, head, canonical diff and
its hash, files, action, public text for an existing PR) from Git, invalidates
a verdict whose reviewed content changed, and keeps the owner's live source
tracking the reviewed version so a later update does not replay a stale draft.

Publication still re-verifies everything from scratch. Nothing here pushes,
comments, or reads credentials.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app import app_git
from app.config import get_settings
from app.contribution_errors import ContributionSubmitError
from app.contribution_records import now_iso

REVIEW_SCOPE = (
  "correctness", "maintainability", "simplicity", "tests",
  "security_privacy", "technical_debt",
)
# Reviewed public inputs besides the diff. A verdict survives a restage only
# while all of them, and the canonical diff bytes, are unchanged.
_REVIEWED_TEXT_FIELDS = ("title", "body_draft", "labels", "coauthor_trailer")


@dataclass(frozen=True)
class StagedCandidate:
  """The exact branch change a verdict and a publication both refer to."""

  base_sha: str
  head_sha: str
  diff: bytes
  diff_sha256: str
  diff_stat: str
  files: tuple[str, ...]


def _git(repo: Path, *args: str) -> str:
  proc = app_git._run(repo, *args, check=False, read_only=True)
  if proc.returncode != 0:
    raise ContributionSubmitError(
      "Git could not read the review checkout.",
      code="stage_git_failed",
      detail=(proc.stderr or proc.stdout or "").strip()[:300],
    )
  return proc.stdout.strip()


def checkout_branch(repo: Path) -> str:
  """The branch a review checkout has checked out; a detached HEAD is refused."""
  proc = app_git._run(
    repo, "symbolic-ref", "-q", "--short", "HEAD", check=False, read_only=True,
  )
  branch = proc.stdout.strip()
  if proc.returncode != 0 or not branch:
    raise ContributionSubmitError(
      "The review checkout is not on a branch. Check out the topic branch "
      "before staging it.",
      code="detached_review_checkout",
    )
  return branch


def derive_candidate(repo: Path, branch: str, base_ref: str) -> StagedCandidate:
  """Read the committed change on ``branch`` since ``base_ref``.

  The base is the accepted parent the change is reviewed against: normally the
  merge base with upstream, so a branch that merged upstream in (to clear a
  merge-queue conflict) still shows only its own change.
  """
  if app_git.worktree_dirty(repo):
    raise ContributionSubmitError(
      "The review checkout has uncommitted changes. Commit or discard them "
      "before staging.",
      code="working_changes",
    )
  head = app_git._resolve_commit(repo, branch, read_only=True)
  base = app_git._resolve_commit(repo, base_ref, read_only=True)
  if head is None or base is None:
    raise ContributionSubmitError(
      "The review branch or its base is not available locally.",
      code="review_unavailable",
    )
  if app_git.ref_is_ancestor(repo, base, head) is not True:
    raise ContributionSubmitError(
      "The review branch does not descend from its accepted base.",
      code="invalid_ancestry",
    )
  diff = app_git._canonical_diff(repo, base, head, read_only=True)
  files = app_git.endpoint_diff_paths(repo, base, head, read_only=True)
  if diff is None or files is None:
    raise ContributionSubmitError(
      "Git could not compute the review branch's change.",
      code="stage_git_failed",
    )
  if not diff:
    raise ContributionSubmitError(
      "The review branch has no change against its base.",
      code="empty_change",
    )
  return StagedCandidate(
    base_sha=base,
    head_sha=head,
    diff=diff,
    diff_sha256=hashlib.sha256(diff).hexdigest(),
    diff_stat=_git(repo, "diff", "--shortstat", f"{base}..{head}"),
    files=tuple(sorted(files)),
  )


def restaged_verdict(previous: dict | None, staged: dict) -> dict | None:
  """Carry an all-clear verdict only across a byte-identical reviewed change.

  A rebase or upstream merge that leaves the canonical diff and public text
  unchanged moves the head without changing what was reviewed; the verdict is
  re-pinned to the new head with a trace of the head it was given for. Any
  other change drops the verdict, so the card shows that review is needed.
  """
  if not isinstance(previous, dict):
    return None
  review = previous.get("quality_review")
  old = previous.get("plan") if isinstance(previous.get("plan"), dict) else {}
  new = staged["plan"]
  if (
    not isinstance(review, dict)
    or review.get("state") != "all_clear"
    or review.get("reviewed_head_sha") != old.get("head_sha")
    or old.get("diff_sha256") != new["diff_sha256"]
    or old.get("action") != new.get("action")
    or any(old.get(key) != new.get(key) for key in _REVIEWED_TEXT_FIELDS)
  ):
    return None
  if old.get("head_sha") == new["head_sha"]:
    return review
  return {
    **review,
    "reviewed_head_sha": new["head_sha"],
    "carried_from_head_sha": old.get("head_sha"),
  }


def review_verdict(
  record: dict, *, state: str, summary: str, scope: list[str] | None,
  chat_id: str,
) -> dict:
  """A verdict bound to the record's exact staged head and diff."""
  plan = record["plan"]
  previous = record.get("quality_review")
  iteration = (
    previous.get("iteration") if isinstance(previous, dict) else None
  )
  return {
    "state": state,
    "reviewed_head_sha": plan["head_sha"],
    "reviewed_diff_sha256": plan["diff_sha256"],
    "reviewed_at": now_iso(),
    "iteration": (iteration if isinstance(iteration, int) else 0) + 1,
    "chat_id": chat_id,
    "scope": list(scope or REVIEW_SCOPE),
    "summary": summary,
  }


class AdoptionRefused(Exception):
  """The live source cannot take the reviewed revision without a person."""

  def __init__(self, detail: str) -> None:
    super().__init__(detail)
    self.detail = detail


def adopts_reviewed_revisions(source: Path) -> bool:
  """Whether staging may commit review revisions onto this live source.

  Installed apps are excluded: their source becomes live only through the
  app apply flow, which owns their commits.
  """
  apps = Path(get_settings().data_dir).resolve() / "apps"
  return not source.resolve().is_relative_to(apps)


def adopt_reviewed_revision(
  source: Path,
  *,
  previous: tuple[str, str],
  current: tuple[str, str],
  message: str,
) -> str:
  """Carry a review revision onto the live source that holds the earlier draft.

  ``previous`` is the ``(base, head)`` the live source provably contains and
  ``current`` the revised ``(base, head)``. The earlier draft is first replayed
  onto the new base so that only the review revision (never the upstream
  movement between the two bases) reaches the live source; that revision is
  then merged three-way into live ``HEAD``, preserving unrelated local work.
  Only a clean result is committed and checked out, and a dirty or moving
  checkout is left untouched. Returns the live commit that contains the
  revised change.
  """
  previous_base, previous_head = previous
  base, head = current
  if app_git._run(
    source, "symbolic-ref", "-q", "HEAD", check=False, read_only=True,
  ).returncode != 0:
    raise AdoptionRefused("The live source is not on a branch.")
  live = app_git._resolve_commit(source, "HEAD")
  if live is None:
    raise AdoptionRefused("The live source has no current commit.")
  anchor = previous_head
  if previous_base != base:
    replayed = app_git.merge_refs(
      source, base, previous_head, merge_base=previous_base,
    )
    if replayed.status != "clean":
      raise AdoptionRefused(
        "The earlier reviewed version no longer applies to the new base."
      )
    anchor = app_git._run(
      source, "commit-tree", replayed.merged_tree_oid, "-p", base,
      "-m", "Earlier reviewed version on the revised base",
    ).stdout.strip()
  merged = app_git.merge_refs(source, live, head, merge_base=anchor)
  if merged.status != "clean":
    raise AdoptionRefused(
      ("Live edits overlap the review revision in: "
       + ", ".join(merged.conflict_paths[:8]))[:400]
    )
  if merged.merged_tree_oid == app_git._tree_oid(source, live):
    return live
  adopted = app_git._run(
    source, "commit-tree", merged.merged_tree_oid, "-p", live, "-m", message,
  ).stdout.strip()
  # A two-tree read-tree is Git's branch switch: it refuses to overwrite a
  # path with local edits and carries unrelated uncommitted work forward.
  # Updating files before the branch is the recoverable order: a crash in
  # between leaves the revision staged on the unchanged branch, which the next
  # stage reports as diverged rather than silently losing it.
  switched = app_git.merge_trees_into_worktree(source, live, adopted, check=False)
  if switched.returncode != 0:
    raise AdoptionRefused(
      "The live source has uncommitted edits on files the revision changes."
    )
  moved = app_git._run(
    source, "update-ref", "-m", message, "HEAD", adopted, live, check=False,
  )
  if moved.returncode != 0:
    app_git.merge_trees_into_worktree(source, adopted, live, check=False)
    raise AdoptionRefused("The live source moved while the revision was adopted.")
  return adopted


def source_holds(source: Path, base_sha: str, head_sha: str) -> bool:
  """Whether the live source's committed ``HEAD`` contains ``base..head``.

  Uncommitted work is deliberately ignored: this names the draft history the
  source holds, while publication proofs separately refuse dirty reviewed
  paths.
  """
  live = app_git._resolve_commit(source, "HEAD", read_only=True)
  return bool(live) and app_git._change_is_subsumed(
    source, base_sha, head_sha, live, read_only=True,
  )


def _draft_ref(record_id: str) -> str:
  # Hashed: a valid record id is not always a valid Git ref component.
  digest = hashlib.sha256(record_id.encode("utf-8")).hexdigest()
  return f"refs/mobius/contribution-drafts/{digest}"


def pin_draft(source: Path, record_id: str, base_sha: str, head_sha: str) -> dict:
  """Name the draft delta the live source holds and keep its commits alive.

  ``base_sha..head_sha`` is the staged version the live source provably
  contained before review revised it. A private ref keeps those commits
  reachable after the review branch is rebased; whoever retires the draft
  (the updater, once the PR has merged) deletes that ref.
  """
  app_git._run(source, "update-ref", _draft_ref(record_id), head_sha, check=False)
  return {"base_sha": base_sha, "head_sha": head_sha, "ref": _draft_ref(record_id)}


def unpin_draft(source: Path, record_id: str) -> None:
  app_git._run(source, "update-ref", "-d", _draft_ref(record_id), check=False)


def source_sync(
  state: str, source_sha: str, detail: str = "", *, draft: dict | None = None,
) -> dict:
  """Operational record of how the live source relates to the staged head.

  ``in_source``: the live source provably contains the reviewed change.
  ``adopted``: staging just committed the review revision onto it.
  ``diverged``: it does not; ``draft`` (when known) is the exact earlier
  staged delta it still holds, so a later update can retire that draft.
  """
  value = {"state": state, "source_sha": source_sha, "checked_at": now_iso()}
  if detail:
    value["detail"] = detail
  if draft:
    value["draft"] = draft
  return value
