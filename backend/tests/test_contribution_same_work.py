"""Whether a PR's own change is the same at two versions: the shared cases, the limit, and the reader."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import contribution_same_work as subject
from app.contribution_errors import ContributionSubmitError

CASES = json.loads((Path(__file__).parent / "fixtures" / "contribution_same_work_cases.json").read_text("utf-8"))["cases"]
REPO, BASE, HEAD, OLD = "o/r", "b" * 40, "h" * 40, "a" * 40
PATCH = {"filename": "a.js", "status": "modified", "additions": 1, "deletions": 0, "patch": "@@ -1 +1 @@\n+x"}


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_every_shared_case_says_whether_the_prs_own_work_is_the_same_or_that_it_cannot_tell(case):
  assert subject.same_work_of(case["approved"], case["head"]) is case["expect"]


def test_a_comparison_that_may_have_been_cut_off_at_githubs_file_limit_cannot_say_the_work_is_the_same():
  def files(count):
    return {"files": [{**PATCH, "filename": f"f{index}.js"} for index in range(count)]}
  assert subject.COMPARE_FILE_LIMIT == 300
  assert subject.same_work_of(files(300), files(300)) is None
  assert subject.same_work_of(files(299), files(299)) is True


def test_a_binary_file_is_the_same_work_only_with_the_same_content_id():
  def binary(sha):
    return {"files": [{"filename": "logo.png", "status": "modified", "additions": 0, "deletions": 0, "sha": sha}]}
  assert subject.same_work_of(binary("r1"), binary("r1")) is True
  assert subject.same_work_of(binary("r1"), binary("r2")) is False
  assert subject.same_work_of(binary(None), binary("r2")) is False


def test_a_file_entry_that_is_not_an_object_cannot_be_compared():
  assert subject.same_work_of({"files": ["a.js"]}, {"files": ["a.js"]}) is None


def review(login, state, commit, **extra):
  return {"by": login, "state": state, "commit": commit, "bot": False, **extra}


QUIET = [
  [review("sam", "APPROVED", HEAD)],
  [review("sam", "APPROVED", OLD), review("kim", "APPROVED", HEAD)],
  [review("alex", "APPROVED", OLD)],
  [{**review("qa-bot", "APPROVED", OLD), "bot": True}],
  [review("sam", "CHANGES_REQUESTED", OLD)],
  [review("sam", "COMMENTED", OLD)],
  [review("sam", "APPROVED", "")],
]


@pytest.mark.parametrize("review_list", QUIET)
def test_nothing_is_a_candidate_unless_someone_elses_approval_is_of_an_older_commit_and_none_is_of_the_head(review_list):
  assert subject.carried_candidates(review_list, HEAD, "Alex") == []


def test_only_an_approval_the_machine_counts_at_the_head_ends_the_comparison():
  older = review("sam", "APPROVED", OLD)
  bot_here = {**review("qa-bot", "APPROVED", HEAD), "bot": True}
  assert subject.carried_candidates([older, bot_here], HEAD, "alex") == [older]
  assert subject.carried_candidates([older, review("Alex", "APPROVED", HEAD)], HEAD, "alex") == [older]
  assert subject.carried_candidates([older, review("kim", "COMMENTED", HEAD)], HEAD, "alex") == [older]
  assert subject.carried_candidates([older, review("kim", "APPROVED", HEAD)], HEAD, "alex") == []


def test_every_approval_of_an_older_commit_by_someone_else_is_a_candidate():
  older = review("sam", "APPROVED", OLD)
  assert subject.carried_candidates([older, review("sam", "CHANGES_REQUESTED", OLD)], HEAD, "alex") == [older]
  assert subject.carried_candidates([older], "", "alex") == []


class CompareGitHub:
  """A stand-in for `gh` that answers only compare reads and records each one."""

  def __init__(self, answers):
    self.answers = answers  # commit -> a comparison, or an exception
    self.reads = []

  def __call__(self, cwd, *args, **_ignored):
    assert args[0] == "api" and len(args) == 2
    endpoint = args[1]
    self.reads.append(endpoint)
    assert endpoint.startswith(f"repos/{REPO}/compare/{BASE}...")
    answer = self.answers.get(endpoint.split("...")[1], ContributionSubmitError("gh: Not Found (HTTP 404)"))
    if isinstance(answer, BaseException):
      raise answer
    return SimpleNamespace(stdout=answer if isinstance(answer, str) else json.dumps(answer))


def mark(github, review_list, author="alex"):
  subject.mark_carried_approvals(github, None, REPO, BASE, HEAD, review_list, author=author)
  return [r.get("sameWork", "unasked") for r in review_list]


def test_an_approval_of_an_older_commit_is_marked_with_whether_the_work_is_the_same_reading_the_head_once():
  github = CompareGitHub({OLD: {"files": [PATCH]}, HEAD: {"files": [PATCH]}, "e" * 40: {"files": [{**PATCH, "patch": "+y"}]}})
  review_list = [review("sam", "APPROVED", OLD), review("kim", "APPROVED", OLD), review("lee", "APPROVED", "e" * 40)]
  assert mark(github, review_list) == [True, True, False]
  assert github.reads == [f"repos/{REPO}/compare/{BASE}...{commit}" for commit in (HEAD, OLD, "e" * 40)]


def test_a_binary_file_swapped_between_the_versions_is_marked_different_and_an_unchanged_one_the_same():
  def binary(sha):
    return {"files": [PATCH, {"filename": "logo.png", "status": "modified", "additions": 0, "deletions": 0, "sha": sha}]}
  swapped = CompareGitHub({OLD: binary("r1"), HEAD: binary("r2")})
  assert mark(swapped, [review("sam", "APPROVED", OLD)]) == [False]
  unchanged = CompareGitHub({OLD: binary("r1"), HEAD: binary("r1")})
  assert mark(unchanged, [review("sam", "APPROVED", OLD)]) == [True]


def test_a_failed_or_unusable_read_marks_none_and_never_raises():
  assert mark(CompareGitHub({HEAD: {"files": [PATCH]}}), [review("sam", "APPROVED", OLD)]) == [None]
  assert mark(CompareGitHub({OLD: {"files": [PATCH]}}), [review("sam", "APPROVED", OLD)]) == [None]
  assert mark(CompareGitHub({OLD: "not json", HEAD: "[]"}), [review("sam", "APPROVED", OLD)]) == [None]
  broken = CompareGitHub({OLD: OSError("no network"), HEAD: {"files": [PATCH]}})
  assert mark(broken, [review("sam", "APPROVED", OLD)]) == [None]


def test_no_read_is_made_unless_an_approval_could_carry_over():
  github = CompareGitHub({})
  for review_list in QUIET:
    assert mark(github, review_list) == ["unasked"] * len(review_list)
  assert github.reads == []


def test_nothing_is_read_without_a_base_to_compare_against():
  github = CompareGitHub({})
  review_list = [review("sam", "APPROVED", OLD)]
  subject.mark_carried_approvals(github, None, REPO, "", HEAD, review_list, author="alex")
  assert github.reads == [] and "sameWork" not in review_list[0]
