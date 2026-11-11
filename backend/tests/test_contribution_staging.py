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
from tests.test_github_routes import _agent_run_headers, _app_token, _write_token

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


def test_stage_rejects_recycled_checkout_before_reading_another_reviews_branch(staging):
  worktree = staging["worktree"]
  marker = worktree / ".git"
  admin = Path(marker.read_text().split(":", 1)[1].strip())
  before = marker.read_bytes()
  # Model the retained-folder failure without operating on a live repository.
  pointer = admin / "gitdir"
  original = pointer.read_bytes()
  pointer.write_text(str(worktree.parent / "another-review" / ".git"))
  try:
    response = staging["new_record"]()
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "checkout_ownership_mismatch"
    assert marker.read_bytes() == before
    assert pointer.read_text().endswith("another-review/.git")
  finally:
    pointer.write_bytes(original)


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


# --- Review/Send share one commit-identity and publication preflight --------


@pytest.fixture(autouse=True)
def _disconnect_after():
  from app import github_auth
  yield
  shutil.rmtree(github_auth.GH_AUTH_DIR, ignore_errors=True)


def _push_permission(monkeypatch, answer):
  """Fake the one GitHub read review may make, recording each call."""
  calls = []

  def permission(_repo, upstream_repo):
    calls.append(upstream_repo)
    return answer
  monkeypatch.setattr(github_routes, "upstream_push_permission", permission)
  return calls


def _review_status(client, staging, owner_token):
  response = client.get(
    f"/api/github/contributions/{staging['app_id']}/review-status",
    headers={"Authorization": f"Bearer {owner_token}"},
  )
  assert response.status_code == 200, response.text
  return response.json()["records"][0]


def test_a_direct_standalone_commit_with_another_identity_is_never_all_clear(
  staging, client, owner_token, monkeypatch,
):
  # The commit is octocat's; the connected account can push to the target.
  _write_token(login="hubot", user_id=7)
  calls = _push_permission(monkeypatch, True)
  staged = staging["new_record"]()
  assert staged.status_code == 200, staged.text
  # Staging tells the preparing agent at once, without refusing private work.
  assert staged.json()["review_status"]["code"] == "commit_identity_mismatch"
  record = staged.json()["record"]
  assert "stack" not in record["plan"]

  refused = staging["review"](record)
  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "commit_identity_mismatch"
  assert "stack" not in refused.json()["detail"]["message"].lower()
  assert staging["review"](record, "changes_needed").status_code == 200
  status = _review_status(client, staging, owner_token)
  assert (status["state"], status["code"]) == (
    "needs_refresh", "commit_identity_mismatch",
  )
  assert set(calls) == {"octo/project"}


def test_an_owned_target_needs_the_identity_without_asking_github(
  staging, monkeypatch,
):
  _write_token(login="octo", user_id=9)
  monkeypatch.setattr(
    github_routes, "upstream_push_permission",
    lambda *_: pytest.fail("the target owner always publishes directly"),
  )
  record = staging["new_record"]().json()["record"]
  refused = staging["review"](record)
  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "commit_identity_mismatch"


def test_a_fork_publication_keeps_normalizing_another_identity(
  staging, client, owner_token, monkeypatch,
):
  _write_token(login="hubot", user_id=7)
  _push_permission(monkeypatch, False)
  staged = staging["new_record"]()
  assert staged.json()["review_status"]["state"] == "ready"
  assert staging["review"](staged.json()["record"]).status_code == 200
  assert _review_status(client, staging, owner_token)["state"] == "ready"


def test_a_matching_identity_needs_no_destination_read(staging, monkeypatch):
  _write_token(login="octocat", user_id=42)
  monkeypatch.setattr(
    github_routes, "upstream_push_permission",
    lambda *_: pytest.fail("a matching commit needs no permission read"),
  )
  record = staging["new_record"]().json()["record"]
  assert staging["review"](record).status_code == 200


def test_an_unreadable_destination_does_not_become_all_clear(
  staging, client, owner_token, monkeypatch,
):
  _write_token(login="hubot", user_id=7)
  _push_permission(monkeypatch, None)
  record = staging["new_record"]().json()["record"]
  refused = staging["review"](record)
  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "publication_destination_unavailable"
  status = _review_status(client, staging, owner_token)
  assert (status["state"], status["code"]) == (
    "unavailable", "publication_destination_unavailable",
  )


def test_review_preflight_reads_github_outside_every_lock_and_never_publishes(
  staging, client, owner_token, monkeypatch,
):
  from app import fs_locks
  from app import github_contribution_git as git_ops
  from app import github_contributions

  _write_token(login="hubot", user_id=7)
  gh_calls = []

  def gh(_repo, *args, **_kwargs):
    assert not fs_locks.app_storage_lock(staging["app_id"]).locked()
    assert not fs_locks.source_dir_lock(str(staging["worktree"])).locked()
    assert not fs_locks.source_dir_lock(str(staging["source"])).locked()
    gh_calls.append(args)
    return subprocess.CompletedProcess(args, 0, "true\n", "")

  monkeypatch.setattr(git_ops, "_gh", gh)
  for name in ("_ensure_owner_fork_remote", "_push_branch", "_push_topic_branch"):
    monkeypatch.setattr(
      github_contributions, name,
      lambda *_a, **_k: pytest.fail("review preflight must not publish"),
    )
  monkeypatch.setattr(
    github_contributions.contribution_screenshots, "publish",
    lambda *_a, **_k: pytest.fail("review preflight must not upload"),
  )
  record = staging["new_record"]().json()["record"]
  assert staging["review"](record).status_code == 409
  _review_status(client, staging, owner_token)
  assert gh_calls and set(gh_calls) == {
    ("api", "repos/octo/project", "--jq", ".permissions.push"),
  }


def test_a_destination_resolved_for_another_target_is_not_reused(
  staging, monkeypatch,
):
  _write_token(login="hubot", user_id=7)
  _push_permission(monkeypatch, False)
  record = staging["new_record"]().json()["record"]
  path = (
    Path(get_settings().data_dir) / "apps" / str(staging["app_id"])
    / "contributions" / f"{record['id']}.json"
  )
  resolve = github_routes._resolve_review_destinations

  def retarget_while_resolving(records, state):
    resolved = resolve(records, state)
    # An app-writable edit lands between the unlocked read and the verdict.
    stored = json.loads(path.read_text())
    stored["plan"]["repo"] = stored["repo"] = "octo/other"
    path.write_text(json.dumps(stored))
    return resolved

  monkeypatch.setattr(
    github_routes, "_resolve_review_destinations", retarget_while_resolving,
  )
  refused = staging["review"](record)
  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "publication_destination_unavailable"


def test_all_clear_rechecks_text_the_app_can_edit_after_staging(staging):
  record = staging["new_record"]().json()["record"]
  path = (
    Path(get_settings().data_dir) / "apps" / str(staging["app_id"])
    / "contributions" / f"{record['id']}.json"
  )
  stored = json.loads(path.read_text())
  stored["plan"]["title"] = " Add a greeting"
  path.write_text(json.dumps(stored))
  refused = staging["review"](record)
  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "invalid_pr_text"


def test_all_clear_rechecks_the_coauthor_trailer_and_a_moved_branch(staging):
  record = staging["new_record"]().json()["record"]
  worktree = staging["worktree"]
  _git(worktree, "commit", "-q", "--amend", "-m", "Add greeting")
  refused = staging["review"](record)
  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "branch_moved"
  restaged = staging["stage"](record["id"])
  assert restaged.status_code != 200
  assert "co-author" in restaged.text


def _failing_gh(failure):
  def gh(_repo, *args, **_kwargs):
    if failure == "timeout":
      raise subprocess.TimeoutExpired(["gh", *args], 90)
    if failure == "missing_gh":
      raise FileNotFoundError("gh")
    if failure == "error_exit":
      return subprocess.CompletedProcess(args, 1, "", "HTTP 502")
    return subprocess.CompletedProcess(args, 0, "maybe\n", "")
  return gh


@pytest.mark.parametrize(
  "failure", ["timeout", "missing_gh", "error_exit", "malformed"],
)
def test_every_failed_destination_read_is_unavailable_and_staging_still_succeeds(
  staging, client, owner_token, monkeypatch, failure,
):
  from app import github_contribution_git as git_ops

  _write_token(login="hubot", user_id=7)
  monkeypatch.setattr(git_ops, "_gh", _failing_gh(failure))
  staged = staging["new_record"]()
  assert staged.status_code == 200, staged.text
  assert staged.json()["review_status"]["code"] == (
    "publication_destination_unavailable"
  )
  record = staged.json()["record"]
  stored = json.loads((
    Path(get_settings().data_dir) / "apps" / str(staging["app_id"])
    / "contributions" / f"{record['id']}.json"
  ).read_text())
  assert stored["plan"]["head_sha"] == record["plan"]["head_sha"]
  refused = staging["review"](record)
  assert refused.status_code == 409
  assert refused.json()["detail"]["code"] == "publication_destination_unavailable"
  status = _review_status(client, staging, owner_token)
  assert (status["state"], status["code"]) == (
    "unavailable", "publication_destination_unavailable",
  )


def test_missing_pr_scopes_skip_the_destination_read(staging, monkeypatch):
  _write_token(login="hubot", user_id=7, scopes=("public_repo",))
  monkeypatch.setattr(
    github_routes, "upstream_push_permission",
    lambda *_: pytest.fail("reconnecting comes before the destination"),
  )
  staged = staging["new_record"]()
  assert staged.json()["review_status"]["code"] == "github_reconnect"


def test_an_account_only_verdict_still_stops_send_before_any_public_step(
  staging, monkeypatch,
):
  """An all_clear recorded while reconnect is pending grants Send nothing."""
  from app import github_contributions
  from app.contribution_errors import ContributionSubmitError

  _write_token(login="octocat", user_id="not-a-number")
  record = staging["new_record"]().json()["record"]
  assert staging["review"](record).status_code == 200
  for name in (
    "_ensure_owner_fork_remote", "_push_branch", "_push_topic_branch",
    "_authoritative_public_reconciliation",
  ):
    monkeypatch.setattr(
      github_contributions, name,
      lambda *_a, **_k: pytest.fail("nothing may be published"),
    )
  monkeypatch.setattr(
    github_contributions._git_ops, "_gh",
    lambda *_a, **_k: pytest.fail("no GitHub read before the account check"),
  )
  _, diff_path = github_routes._record_paths(staging["app_id"], record["id"])
  with pytest.raises(ContributionSubmitError) as single:
    github_contributions._submit_prepared_pr(record, diff_path)
  assert single.value.status_code == 401
  layer = {**record, "status": "submitting", "plan": {
    **record["plan"],
    "stack": {"id": "s", "position": 1, "total": 1, "base_branch": "main"},
  }}
  with pytest.raises(ContributionSubmitError) as stack:
    github_contributions._preflight_prepared_stack(
      [{"record": layer, "diff_path": diff_path, "stack": layer["plan"]["stack"]}],
    )
  assert stack.value.status_code == 401
