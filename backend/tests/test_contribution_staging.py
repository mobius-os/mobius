"""Staging derives the reviewed PR candidate from Git and keeps live source on it.

Every scenario uses real repositories: a live source checkout under the data
dir and a linked review worktree under ``contrib/``, the same shape Contribute
prepares. Only GitHub reads (upstream default branch, an open PR) are faked.
"""

import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from app.config import get_settings
from app.routes import github as github_routes
from app.routes.github import _limiter as _github_limiter
from tests.test_app_git import bump_ctime_without_changing_bytes
from tests.test_github_routes import _agent_run_headers, _app_token

_github_limiter.enabled = False

TRAILER = "Co-authored-by: Möbius Agent <mobius-agent@users.noreply.github.com>"


def _git(repo: Path, *args: str) -> str:
  return subprocess.run(
    ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
  ).stdout.strip()


def _commit(repo: Path, files: dict[str, str], message: str, *, trailer=True) -> str:
  for name, text in files.items():
    (repo / name).write_text(text)
    _git(repo, "add", name)
  args = ["commit", "-q", "-m", message]
  if trailer:
    args += ["-m", TRAILER]
  _git(repo, *args)
  return _git(repo, "rev-parse", "HEAD")


def _identity(repo: Path) -> None:
  _git(repo, "config", "user.name", "octocat")
  _git(repo, "config", "user.email", "42+octocat@users.noreply.github.com")


DRAFT = "def greet():\n  return 'hi'\n"
REVISED = "def greet():\n  return 'hello'\n"


@pytest.fixture
def staging(client, owner_token, db, monkeypatch):
  """A live source holding a draft, and its review worktree with the draft."""
  data = Path(get_settings().data_dir)
  suffix = uuid.uuid4().hex[:8]
  source = data / "worktrees" / f"project-{suffix}"
  source.mkdir(parents=True)
  _git(source, "init", "-q", "-b", "main")
  _identity(source)
  base = _commit(source, {"app.py": "x = 1\n", "other.py": "y = 1\n"}, "base")
  _commit(source, {"greet.py": DRAFT}, "local draft", trailer=False)

  worktree = data / "contrib" / f"greet-{suffix}" / "worktree"
  _git(source, "worktree", "add", "-q", "-b", "fix/greet", str(worktree), base)
  _identity(worktree)
  _commit(worktree, {"greet.py": DRAFT}, "Add greeting")

  app_id, _ = _app_token(client, owner_token, github_access=True)
  chat = client.post(
    "/api/chats", json={"title": "Staging"},
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert chat.status_code == 200, chat.text
  headers = _agent_run_headers(db, chat.json()["id"], "run-staging")
  upstream = {"sha": base}
  monkeypatch.setattr(github_routes, "_upstream_default_branch", lambda *_: "main")
  monkeypatch.setattr(
    github_routes, "fetch_upstream_head", lambda *_: upstream["sha"],
  )

  def stage(record_id="greet", **body):
    return client.post(
      f"/api/github/contributions/{app_id}/{record_id}/stage",
      json=body, headers=headers,
    )

  def review(record, state="all_clear"):
    return client.post(
      f"/api/github/contributions/{app_id}/{record['id']}/review",
      json={
        "head_sha": record["plan"]["head_sha"],
        "diff_sha256": record["plan"]["diff_sha256"],
        "state": state,
        "summary": "Complete head reviewed.",
      },
      headers=headers,
    )

  def new_record(**overrides):
    body = {
      "repo_path": str(worktree), "repo": "octo/project",
      "title": "Add a greeting", "body_draft": "Adds a greeting.",
      "summary": "People get greeted.", **overrides,
    }
    return stage(**body)

  yield {
    "app_id": app_id, "source": source, "worktree": worktree, "base": base,
    "upstream": upstream, "stage": stage, "review": review,
    "new_record": new_record, "chat_id": chat.json()["id"],
  }
  shutil.rmtree(worktree.parent, ignore_errors=True)
  shutil.rmtree(source, ignore_errors=True)


def _stored_diff(app_id: int, record_id: str) -> bytes:
  return (
    Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
    / f"{record_id}.diff"
  ).read_bytes()


def test_stage_derives_the_canonical_candidate_from_git(staging):
  response = staging["new_record"]()
  assert response.status_code == 200, response.text
  record = response.json()["record"]
  plan = record["plan"]
  worktree = staging["worktree"]
  expected = subprocess.run(
    ["git", "-c", "core.quotePath=false", "diff", "--no-ext-diff", "--no-color",
     "--binary", "--full-index", "--src-prefix=a/", "--dst-prefix=b/",
     f"{staging['base']}..HEAD"],
    cwd=worktree, check=True, capture_output=True,
  ).stdout
  assert plan["base_sha"] == staging["base"]
  assert plan["head_sha"] == _git(worktree, "rev-parse", "HEAD")
  assert plan["diff_sha256"] == hashlib.sha256(expected).hexdigest()
  assert _stored_diff(staging["app_id"], "greet") == expected
  assert plan["files"] == ["greet.py"]
  assert plan["action"] == "pr" and record["status"] == "prepared"
  assert plan["source_repo_path"] == str(staging["source"].resolve())
  assert record["chat_ids"] == [staging["chat_id"]]
  # Staging never reviews: the card must ask for a verdict on this head.
  assert "quality_review" not in record
  assert response.json()["source_sync"]["state"] == "in_source"


def test_review_verdict_binds_to_the_exact_staged_head(staging):
  record = staging["new_record"]().json()["record"]
  moved = {**record, "plan": {**record["plan"], "head_sha": "0" * 40}}
  assert staging["review"](moved).status_code == 409

  response = staging["review"](record)
  assert response.status_code == 200, response.text
  verdict = response.json()["record"]["quality_review"]
  assert verdict["state"] == "all_clear"
  assert verdict["reviewed_head_sha"] == record["plan"]["head_sha"]
  assert verdict["reviewed_diff_sha256"] == record["plan"]["diff_sha256"]
  assert verdict["chat_id"] == staging["chat_id"]


def test_all_clear_is_refused_when_the_checkout_moved_after_staging(staging):
  record = staging["new_record"]().json()["record"]
  _commit(staging["worktree"], {"greet.py": REVISED}, "Unstaged revision")
  response = staging["review"](record)
  assert response.status_code == 409
  assert response.json()["detail"]["code"] == "branch_moved"


def test_restage_with_identical_change_carries_the_verdict(staging):
  record = staging["new_record"]().json()["record"]
  staging["review"](record)
  worktree = staging["worktree"]
  # Upstream moves on an unrelated file; the branch rebases onto it.
  _git(worktree, "checkout", "-q", "--detach", staging["base"])
  upstream = _commit(worktree, {"other.py": "y = 2\n"}, "upstream work", trailer=False)
  _git(worktree, "checkout", "-q", "fix/greet")
  _git(worktree, "rebase", "-q", upstream)
  staging["upstream"]["sha"] = upstream

  restaged = staging["stage"]().json()["record"]
  assert restaged["plan"]["base_sha"] == upstream
  assert restaged["plan"]["head_sha"] != record["plan"]["head_sha"]
  assert restaged["plan"]["diff_sha256"] == record["plan"]["diff_sha256"]
  verdict = restaged["quality_review"]
  assert verdict["state"] == "all_clear"
  assert verdict["reviewed_head_sha"] == restaged["plan"]["head_sha"]
  assert verdict["carried_from_head_sha"] == record["plan"]["head_sha"]


def test_restage_with_a_revision_drops_the_verdict_and_adopts_it_live(staging):
  record = staging["new_record"]().json()["record"]
  staging["review"](record)
  source = staging["source"]
  (source / "notes.txt").write_text("unrelated uncommitted work\n")
  live_before = _git(source, "rev-parse", "HEAD")
  _commit(staging["worktree"], {"greet.py": REVISED}, "Say hello")

  response = staging["stage"]()
  assert response.status_code == 200, response.text
  restaged = response.json()["record"]
  assert "quality_review" not in restaged
  sync = response.json()["source_sync"]
  assert sync["state"] == "adopted"
  assert (source / "greet.py").read_text() == REVISED
  assert _git(source, "rev-parse", "HEAD^") == live_before
  assert sync["source_sha"] == _git(source, "rev-parse", "HEAD")
  assert restaged["plan"]["source_sha"] == sync["source_sha"]
  assert (source / "notes.txt").read_text() == "unrelated uncommitted work\n"
  assert "draft" not in sync
  assert _git(source, "for-each-ref", "refs/mobius/contribution-drafts") == ""
  assert _git(source, "status", "--porcelain") == "?? notes.txt"


def test_adoption_brings_only_the_revision_across_a_rebase(staging):
  staging["new_record"]()
  worktree = staging["worktree"]
  _git(worktree, "checkout", "-q", "--detach", staging["base"])
  upstream = _commit(worktree, {"other.py": "y = 2\n"}, "upstream work", trailer=False)
  _git(worktree, "checkout", "-q", "fix/greet")
  _git(worktree, "rebase", "-q", upstream)
  _commit(worktree, {"greet.py": REVISED}, "Say hello")
  staging["upstream"]["sha"] = upstream

  response = staging["stage"]()
  assert response.json()["source_sync"]["state"] == "adopted"
  source = staging["source"]
  assert (source / "greet.py").read_text() == REVISED
  # Upstream movement is the updater's job, never smuggled in by a review.
  assert (source / "other.py").read_text() == "y = 1\n"


def test_adoption_ignores_a_metadata_only_change_to_live_files(staging):
  """A no-op ownership repair changes ctime, not bytes: it is not a live edit
  and must not block bringing a review revision across."""
  staging["new_record"]()
  source = staging["source"]
  bump_ctime_without_changing_bytes(*(source / name for name in ("greet.py", "app.py", "other.py")))
  dirty = subprocess.run(["git", "diff-files", "--quiet"], cwd=source, check=False)
  assert dirty.returncode == 1  # the cached stat data no longer matches
  _commit(staging["worktree"], {"greet.py": REVISED}, "Say hello")

  response = staging["stage"]()
  assert response.status_code == 200, response.text
  assert response.json()["source_sync"]["state"] == "adopted"
  assert (source / "greet.py").read_text() == REVISED


def test_live_edits_on_revised_files_are_left_untouched(staging):
  draft_record = staging["new_record"]().json()["record"]
  source = staging["source"]
  (source / "greet.py").write_text(DRAFT + "# in progress\n")
  live_before = _git(source, "rev-parse", "HEAD")
  _commit(staging["worktree"], {"greet.py": REVISED}, "Say hello")

  response = staging["stage"]()
  assert response.status_code == 200, response.text
  sync = response.json()["source_sync"]
  assert sync["state"] == "diverged"
  assert _git(source, "rev-parse", "HEAD") == live_before
  assert (source / "greet.py").read_text() == DRAFT + "# in progress\n"
  # The draft the live source still holds is named exactly and pinned.
  draft = sync["draft"]
  assert draft["base_sha"] == draft_record["plan"]["base_sha"]
  assert draft["head_sha"] == draft_record["plan"]["head_sha"]
  assert _git(source, "rev-parse", draft["ref"]) == draft["head_sha"]

  # A further revision keeps naming the same live draft, not the new head.
  _commit(staging["worktree"], {"greet.py": "def greet():\n  return 'hey'\n"}, "Say hey")
  again = staging["stage"]().json()["source_sync"]
  assert again["state"] == "diverged"
  assert again["draft"] == draft


def test_installed_app_source_is_never_committed_by_staging(staging, monkeypatch):
  monkeypatch.setattr(
    github_routes.contribution_staging, "adopts_reviewed_revisions",
    lambda _source: False,
  )
  staging["new_record"]()
  live_before = _git(staging["source"], "rev-parse", "HEAD")
  _commit(staging["worktree"], {"greet.py": REVISED}, "Say hello")
  sync = staging["stage"]().json()["source_sync"]
  assert sync["state"] == "diverged"
  assert _git(staging["source"], "rev-parse", "HEAD") == live_before


def test_stage_refuses_a_commit_without_the_coauthor_trailer(staging):
  _commit(staging["worktree"], {"greet.py": REVISED}, "No trailer", trailer=False)
  response = staging["new_record"]()
  assert response.status_code == 409
  assert response.json()["detail"]["code"] == "missing_coauthor"


def test_stage_refuses_uncommitted_review_work(staging):
  (staging["worktree"] / "greet.py").write_text(REVISED)
  response = staging["new_record"]()
  assert response.status_code == 409
  assert response.json()["detail"]["code"] == "working_changes"


def test_open_pr_restages_as_an_update_with_its_live_text(staging, monkeypatch):
  record = staging["new_record"]().json()["record"]
  published = record["plan"]["head_sha"]
  path = (
    Path(get_settings().data_dir) / "apps" / str(staging["app_id"])
    / "contributions" / "greet.json"
  )
  path.write_text(json.dumps({
    **record, "status": "open", "number": 7,
    "url": "https://github.com/octo/project/pull/7",
    "head_repository": "octo/project",
  }))
  live = {
    "error": None, "head_sha": published, "base_branch": "main",
    "base_sha": staging["base"], "title": "Maintainer title",
    "body": "Maintainer body",
  }
  monkeypatch.setattr(github_routes, "_autopilot_live_target", lambda *_: live)

  refused = staging["stage"](title="New title")
  assert refused.status_code == 422
  assert refused.json()["detail"]["code"] == "stage_public_text"

  _commit(staging["worktree"], {"greet.py": REVISED}, "Say hello")
  response = staging["stage"]()
  assert response.status_code == 200, response.text
  updated = response.json()["record"]
  assert updated["status"] == "prepared"
  assert updated["plan"]["action"] == "pr_update"
  assert updated["plan"]["title"] == "Maintainer title"
  assert updated["plan"]["body_draft"] == "Maintainer body"
  assert updated["plan"]["pr_metadata"] == {
    "old_title": "Maintainer title", "old_body": "Maintainer body",
  }
  assert updated["number"] == 7

  # A rewritten branch that drops the published head can never update the PR.
  worktree = staging["worktree"]
  _git(worktree, "reset", "-q", "--hard", staging["base"])
  _commit(worktree, {"greet.py": REVISED}, "Rewritten")
  rewritten = staging["stage"]()
  assert rewritten.status_code == 409
  assert rewritten.json()["detail"]["code"] == "review_refresh_needed"


def test_helper_stages_on_behalf_of_its_source_chat(staging):
  response = staging["new_record"](chat_id="source-chat-1")
  assert response.status_code == 200, response.text
  record = response.json()["record"]
  assert record["chat_id"] == "source-chat-1"
  assert record["chat_ids"] == ["source-chat-1"]


def test_send_freshness_accepts_what_staging_hashed_for_crlf_source(staging):
  """Staging, review status and Send share one byte-exact diff definition."""
  from app.contribution_records import read_record, record_paths
  from app.github_contribution_git import _assert_fresh

  worktree = staging["worktree"]
  (worktree / "win.txt").write_bytes(b"line one\r\nline two\r\n")
  _git(worktree, "add", "win.txt")
  _git(worktree, "commit", "-q", "-m", "Windows file", "-m", TRAILER)
  record = staging["new_record"]().json()["record"]
  assert staging["review"](record).status_code == 200
  record_path, diff_path = record_paths(staging["app_id"], "greet")
  _assert_fresh(read_record(record_path), diff_path, worktree, "fix/greet")


def test_an_oversized_record_is_refused_before_the_live_source_changes(staging, monkeypatch):
  staging["new_record"]()
  live_before = _git(staging["source"], "rev-parse", "HEAD")
  _commit(staging["worktree"], {"greet.py": REVISED}, "Say hello")
  monkeypatch.setattr(github_routes, "MAX_RECORD_BYTES", 2048)
  response = staging["stage"](body_draft="x" * 2048)
  assert response.status_code == 422
  assert response.json()["detail"]["code"] == "record_too_large"
  assert _git(staging["source"], "rev-parse", "HEAD") == live_before


def test_send_refuses_a_verdict_for_a_different_diff_of_the_same_head(staging):
  from fastapi import HTTPException

  from app.github_contributions import _require_all_clear_review

  record = staging["review"](staging["new_record"]().json()["record"]).json()["record"]
  _require_all_clear_review(record)
  swapped = {**record, "plan": {**record["plan"], "diff_sha256": "f" * 64}}
  with pytest.raises(HTTPException):
    _require_all_clear_review(swapped)


def test_a_carried_draft_is_dropped_once_live_no_longer_holds_it(staging):
  staging["new_record"]()
  source = staging["source"]
  (source / "greet.py").write_text(DRAFT + "# in progress\n")
  _commit(staging["worktree"], {"greet.py": REVISED}, "Say hello")
  assert staging["stage"]().json()["source_sync"]["draft"]

  # The owner commits their own rewrite, so live no longer holds the draft.
  _git(source, "add", "greet.py")
  _git(source, "commit", "-q", "-m", "owner rewrite")
  (source / "greet.py").write_text("def greet():\n  return 'yo'\n")
  _git(source, "commit", "-qam", "owner rewrite again")
  _commit(staging["worktree"], {"greet.py": "def greet():\n  return 'hey'\n"}, "Say hey")
  sync = staging["stage"]().json()["source_sync"]
  assert sync["state"] == "diverged"
  assert "draft" not in sync


def test_closing_an_unmerged_contribution_releases_its_draft_pin(staging):
  from app.github_contributions import _settle_equivalence

  staging["new_record"]()
  source = staging["source"]
  (source / "greet.py").write_text(DRAFT + "# in progress\n")
  _commit(staging["worktree"], {"greet.py": REVISED}, "Say hello")
  response = staging["stage"]().json()
  ref = response["source_sync"]["draft"]["ref"]
  assert _git(source, "for-each-ref", ref)

  _settle_equivalence({**response["record"], "status": "closed"})
  assert _git(source, "for-each-ref", ref) == ""


def test_autopilot_round_restages_its_open_pr_in_place(staging, monkeypatch):
  from app import contribution_autopilot
  from app.github_contributions import _assert_reviewed_existing_pr_metadata

  record = staging["new_record"]().json()["record"]
  path = (
    Path(get_settings().data_dir) / "apps" / str(staging["app_id"])
    / "contributions" / "greet.json"
  )
  path.write_text(json.dumps({
    **record, "status": "open", "number": 7,
    "url": "https://github.com/octo/project/pull/7",
    "head_repository": "octo/project",
  }))
  live = {
    "error": None, "head_sha": record["plan"]["head_sha"], "base_branch": "main",
    "base_sha": staging["base"], "title": "Add a greeting",
    "body": "Adds a greeting.",
  }
  monkeypatch.setattr(github_routes, "_autopilot_live_target", lambda *_: live)
  monkeypatch.setattr(contribution_autopilot, "get_row", lambda *_: object())
  monkeypatch.setattr(
    contribution_autopilot, "verify_claim",
    lambda _row, run_id: run_id == "round-1",
  )
  _commit(staging["worktree"], {"greet.py": REVISED}, "Answer review")

  assert staging["stage"](autopilot_run_id="stale-round").status_code == 409
  response = staging["stage"](autopilot_run_id="round-1")
  assert response.status_code == 200, response.text
  updated = response.json()["record"]
  assert updated["status"] == "open"
  # A PR first sent as "pr" is updated as "pr_update", carrying the exact
  # live text /autopilot/update requires before it pushes.
  assert updated["plan"]["action"] == "pr_update"
  _assert_reviewed_existing_pr_metadata(
    updated, live_title=live["title"], live_body=live["body"],
  )
  assert updated["plan"]["head_sha"] == _git(staging["worktree"], "rev-parse", "HEAD")
  # The published revision reaches the live copy, as for any restage.
  assert response.json()["source_sync"]["state"] == "adopted"
  assert (staging["source"] / "greet.py").read_text() == REVISED
