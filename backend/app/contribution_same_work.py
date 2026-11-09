"""Is a PR's own change the same at two versions of its branch?

An approval was given to one commit. When the branch only merged its base in
(to fix a conflict, say) the head is a new commit but not new work, and the
approval still stands. The PR machine (contribution_pr_machine) never reads
this: it is a fact (``sameWork`` on an approval) that the fact readers fill,
here and in the app (same-work.js), and both are held to the same cases
(tests/fixtures/contribution_same_work_cases.json, byte-identical with the
app's test/same-work.cases.json).

"Own change" is what GitHub's compare shows of a version against the base's
current tip: the added and removed lines of each file, in order. Hunk positions
are ignored, because they shift whenever the base moves; context lines are
ignored, because they are the base's. That is the line this draws: a line moved
elsewhere in its file still compares as the same. A file with no lines to
compare (a binary one) is compared by its content id instead. Only a full,
untruncated answer can say the work is the same.
"""
from __future__ import annotations

import json
import logging
import subprocess

from app import contribution_review_runs as reviews
from app.contribution_errors import ContributionSubmitError

_log = logging.getLogger(__name__)

# GitHub lists at most this many files of a comparison; a list that long may have been cut off.
COMPARE_FILE_LIMIT = 300
# A PR with more approved commits than this proves only the first few: the merge path waits on these reads.
MAX_APPROVED_COMMITS = 4
# A compare that cannot be read is "cannot tell", never a failure of the merge or ask that wanted it.
_READ_FAILURES = (ContributionSubmitError, subprocess.SubprocessError, OSError, ValueError)


def _count(value) -> int:
  return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _has_changes(file: dict) -> bool:
  return _count(file.get("additions")) + _count(file.get("deletions")) > 0


def _content(file: dict) -> list[str]:
  """A patch's lines say what changed; without one only the file's content id (`sha`) can.

  The id is ignored when there is a patch: a file's blob changes whenever the
  base changes other lines of it, so it would call moved-base work different.
  """
  patch = file.get("patch")
  if isinstance(patch, str):
    return [line for line in patch.split("\n") if line[:1] in ("+", "-")]
  sha = file.get("sha")
  return [f"blob {'' if sha is None else sha}"]


def _signature(file: dict) -> str:
  """One JSON array of the separate fields, so no file name or line can run into the next field.

  A name that holds ``|added|+`` must not read as a status and a line.
  """
  return json.dumps([file.get("previous_filename") or "", file.get("filename"), file.get("status"), _content(file)])


def _files(compare) -> list | None:
  """The file list of one complete comparison, or None when it cannot be trusted to be complete."""
  files = compare.get("files") if isinstance(compare, dict) else None
  if not isinstance(files, list) or len(files) >= COMPARE_FILE_LIMIT:
    return None
  return files if all(isinstance(file, dict) for file in files) else None


def same_work_of(approved: dict | None, head: dict | None) -> bool | None:
  """True or False when both comparisons are complete; None when either cannot say.

  None covers a missing comparison, a list that may have been cut off, and a
  changed file GitHub gave no lines for (such as a very large one).
  """
  lists = [_files(compare) for compare in (approved, head)]
  if any(files is None for files in lists):
    return None
  if any(_has_changes(file) and not isinstance(file.get("patch"), str) for files in lists for file in files):
    return None
  before, after = ([_signature(file) for file in files] for files in lists)
  return sorted(before) == sorted(after)


def _same_login(a, b) -> bool:
  return str(a or "").lower() == str(b or "").lower()


def carried_candidates(review_list: list[dict], head: str, author: str) -> list[dict]:
  """The approvals worth comparing: another person's approval of an older commit.

  Only while no such person has approved the head itself, so a PR that is
  already approved here costs no read. The author's and a bot's approvals never
  count (the machine ignores them), so they neither start a comparison nor end one.
  """
  counted = [r for r in review_list if r.get("state") == "APPROVED" and not r.get("bot")
             and not _same_login(r.get("by"), author)]
  if not head or any(r.get("commit") == head for r in counted):
    return []
  return [r for r in counted if r.get("commit") and r["commit"] != head]


def _compare(gh, cwd, repo: str, base_sha: str, commit: str) -> dict | None:
  try:
    answer = reviews.read(gh, cwd, f"repos/{repo}/compare/{base_sha}...{commit}")
  except _READ_FAILURES as exc:
    _log.info("Same-work compare read failed for %s (%s)", repo, type(exc).__name__)
    return None
  return answer if isinstance(answer, dict) else None


def mark_carried_approvals(gh, cwd, repo: str, base_sha: str, head: str, review_list: list[dict],
                           *, author: str = "") -> None:
  """Set ``sameWork`` (True, False or None) on each approval of an older commit that could carry over.

  ``base_sha`` is the base branch's current tip, which both versions are
  compared against; the head's comparison is read once for all of them. A
  failed read marks None. The facts' review dicts are changed in place.
  """
  candidates = carried_candidates(review_list, head, author)
  if not (candidates and repo and base_sha):
    return
  head_compare = _compare(gh, cwd, repo, base_sha, head)
  answers: dict[str, dict | None] = {}
  for review in candidates:
    commit = review["commit"]
    if commit not in answers and len(answers) < MAX_APPROVED_COMMITS:
      answers[commit] = _compare(gh, cwd, repo, base_sha, commit)
    if commit in answers:
      review["sameWork"] = same_work_of(answers[commit], head_compare)
