"""Focused route tests for terminal contribution staging disposal."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from app import fs_locks
from app.config import get_settings
from app.storage_io import atomic_write
from app.routes import github as github_routes
from test_app_fixtures import create_local_app


def _write_terminal_record(app_id: int, record_id: str, record: dict) -> None:
  root = Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
  root.mkdir(parents=True, exist_ok=True)
  atomic_write(root / f"{record_id}.json", json.dumps(record))


def test_cleanup_comment_without_checkout_is_an_idempotent_noop(client, auth):
  app = create_local_app(client, auth, name="Cleanup comment")
  record_id = "cleanup-comment-without-checkout"
  _write_terminal_record(app["id"], record_id, {
    "id": record_id,
    "type": "issue_comment",
    "status": "abandoned",
    "plan": {},
  })

  response = client.post(
    f"/api/github/contributions/{app['id']}/{record_id}/cleanup-staging",
    headers=auth,
  )

  assert response.status_code == 200, response.text
  assert response.json() == {"cleaned": False}


def test_cleanup_uses_linked_owner_when_equivalence_source_is_missing(
  client, auth, monkeypatch,
):
  app = create_local_app(client, auth, name="Cleanup route owner")
  source = Path(app["source_dir"])
  checkout = (
    Path(get_settings().data_dir)
    / "contrib"
    / "cleanup-route-missing-source"
    / "worktree"
  )
  checkout.parent.mkdir(parents=True)
  subprocess.run(
    [
      "git", "-C", str(source), "worktree", "add", "-qb",
      "test/cleanup-route-missing-source", str(checkout),
    ],
    check=True,
  )

  record_id = "cleanup-route-missing-source"
  _write_terminal_record(app["id"], record_id, {
    "id": record_id,
    "status": "closed",
    "plan": {
      "repo_path": str(checkout),
      "source_repo_path": str(
        Path(get_settings().data_dir) / "apps" / "missing-source"
      ),
    },
  })

  locked = []
  real_source_dir_lock = fs_locks.source_dir_lock

  def tracking_source_dir_lock(path):
    locked.append(Path(path).resolve())
    return real_source_dir_lock(path)

  monkeypatch.setattr(fs_locks, "source_dir_lock", tracking_source_dir_lock)
  response = client.post(
    f"/api/github/contributions/{app['id']}/{record_id}/cleanup-staging",
    headers=auth,
  )

  assert response.status_code == 200, response.text
  assert response.json() == {"cleaned": True}
  assert locked == [source.resolve()]
  assert not checkout.exists()
  worktrees = subprocess.run(
    ["git", "-C", str(source), "worktree", "list", "--porcelain"],
    check=True,
    capture_output=True,
    text=True,
  ).stdout
  assert str(checkout) not in worktrees


def test_cleanup_survives_merged_upstream_lookup_failure(
  client, auth, monkeypatch,
):
  app = create_local_app(client, auth, name="Cleanup route merged")
  checkout = (
    Path(get_settings().data_dir)
    / "contrib"
    / "cleanup-route-upstream-failure"
    / "repo"
  )
  (checkout / ".git").mkdir(parents=True)

  record_id = "cleanup-route-upstream-failure"
  _write_terminal_record(app["id"], record_id, {
    "id": record_id,
    "status": "merged",
    "plan": {"repo_path": str(checkout)},
  })

  def fail_upstream_lookup(*_args):
    raise RuntimeError("upstream unavailable")

  monkeypatch.setattr(
    github_routes, "_merged_upstream_sha", fail_upstream_lookup,
  )
  monkeypatch.setattr(
    github_routes,
    "_settle_equivalence",
    lambda *_args: pytest.fail(
      "a merged witness must remain pending without an authoritative commit"
    ),
  )
  response = client.post(
    f"/api/github/contributions/{app['id']}/{record_id}/cleanup-staging",
    headers=auth,
  )

  assert response.status_code == 200, response.text
  assert response.json() == {"cleaned": True}
  assert not checkout.exists()


def test_cleanup_rejects_an_unsigned_bot_merge_commit(
  client, auth,
):
  app = create_local_app(client, auth, name="Cleanup forged bot merge")
  checkout = (
    Path(get_settings().data_dir)
    / "contrib"
    / "cleanup-route-forged-bot-merge"
    / "repo"
  )
  (checkout / ".git").mkdir(parents=True)
  record_id = "cleanup-route-forged-bot-merge"
  _write_terminal_record(app["id"], record_id, {
    "id": record_id,
    "status": "merged",
    "submission_mode": "mobius-bot",
    "merge_commit_sha": "a" * 40,
    "plan": {"repo_path": str(checkout)},
  })

  response = client.post(
    f"/api/github/contributions/{app['id']}/{record_id}/cleanup-staging",
    headers=auth,
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"].startswith(
    "The saved Möbius relay merge result is invalid."
  )
  assert checkout.exists()


def test_cleanup_rejects_unsigned_alternate_bot_merge_fields(
  client, auth, monkeypatch,
):
  app = create_local_app(client, auth, name="Cleanup bot merge fallback")
  checkout = (
    Path(get_settings().data_dir)
    / "contrib"
    / "cleanup-route-bot-merge-fallback"
    / "repo"
  )
  (checkout / ".git").mkdir(parents=True)
  record_id = "cleanup-route-bot-merge-fallback"
  _write_terminal_record(app["id"], record_id, {
    "id": record_id,
    "status": "merged",
    "submission_mode": "mobius-bot",
    "relay_contribution_id": "ctr_1234567890abcdef1234567890abcdef",
    "last_land_head_sha": "b" * 40,
    "checks": {"merge_commit_sha": "c" * 40},
    "plan": {"repo_path": str(checkout)},
  })
  monkeypatch.setattr(
    github_routes,
    "_personal_merge_commit",
    lambda *_args: pytest.fail("relay records must not use personal lookup"),
  )
  monkeypatch.setattr(
    github_routes,
    "_settle_equivalence",
    lambda *_args: pytest.fail(
      "unsigned alternate SHA fields must not promote pending equivalence"
    ),
  )

  response = client.post(
    f"/api/github/contributions/{app['id']}/{record_id}/cleanup-staging",
    headers=auth,
  )

  assert response.status_code == 409, response.text
  assert response.json()["detail"] == (
    "The Möbius relay has not confirmed a terminal contribution state."
  )
  assert checkout.exists()


def test_cleanup_uses_signed_relay_terminal_state_over_mutable_status(
  client, auth, monkeypatch,
):
  app = create_local_app(client, auth, name="Cleanup signed relay terminal")
  checkout = (
    Path(get_settings().data_dir)
    / "contrib"
    / "cleanup-route-signed-relay-terminal"
    / "repo"
  )
  (checkout / ".git").mkdir(parents=True)
  record_id = "cleanup-route-signed-relay-terminal"
  merge_sha = "d" * 40
  _write_terminal_record(app["id"], record_id, {
    "id": record_id,
    "status": "closed",
    "relay_status": "closed",
    "submission_mode": "mobius-bot",
    "plan": {"repo_path": str(checkout)},
  })
  monkeypatch.setattr(
    github_routes,
    "_relay_merge_authority",
    lambda *_args: (True, "merged", merge_sha),
  )
  settled = []
  monkeypatch.setattr(
    github_routes,
    "_settle_equivalence",
    lambda record, upstream: settled.append((record["status"], upstream)),
  )

  response = client.post(
    f"/api/github/contributions/{app['id']}/{record_id}/cleanup-staging",
    headers=auth,
  )

  assert response.status_code == 200, response.text
  assert settled == [("merged", merge_sha)]
  assert not checkout.exists()


def test_cancelled_cleanup_holds_the_source_lock_through_its_settlement(
  client, auth, db, monkeypatch,
):
  """The settlement's thread ends under the lock; the cancel then lands."""
  import asyncio

  from tests.cancel_helpers import BlockedWorker, cancel_while_worker_blocked

  app = create_local_app(client, auth, name="Cleanup cancel")
  checkout = Path(get_settings().data_dir) / "contrib" / "cleanup-cancel" / "repo"
  (checkout / ".git").mkdir(parents=True)
  record_id = "cleanup-cancel"
  _write_terminal_record(app["id"], record_id, {
    "id": record_id, "status": "closed", "plan": {"repo_path": str(checkout)},
  })
  monkeypatch.setattr(github_routes, "_validate_submit_app", lambda *_args: None)
  monkeypatch.setattr(github_routes, "_recheck_submit_app", lambda *_args: None)
  monkeypatch.setattr(github_routes, "_equivalence_source_repo", lambda _record: None)
  worker = BlockedWorker()
  monkeypatch.setattr(github_routes, "_settle_equivalence", worker)

  outcome = asyncio.run(cancel_while_worker_blocked(
    fs_locks.source_dir_lock(str(checkout)),
    lambda: github_routes.cleanup_contribution_staging(
      None, app["id"], record_id, db, None,
    ),
    worker,
    while_blocked=checkout.exists,
  ))

  assert isinstance(outcome, asyncio.CancelledError), repr(outcome)
  assert checkout.exists()  # No cleanup step starts after the cancel.


def _standalone_checkout(name: str) -> Path:
  checkout = Path(get_settings().data_dir) / "contrib" / name / "repo"
  checkout.mkdir(parents=True)
  subprocess.run(["git", "init", "-q", str(checkout)], check=True)
  return checkout


def _cleanup(client, auth, app_id: int, record_id: str) -> dict:
  response = client.post(
    f"/api/github/contributions/{app_id}/{record_id}/cleanup-staging",
    headers=auth,
  )
  assert response.status_code == 200, response.text
  return response.json()


def test_a_checkout_shared_with_an_open_contribution_outlives_the_finished_one(
  client, auth,
):
  app = create_local_app(client, auth, name="Cleanup shared checkout")
  checkout = _standalone_checkout("cleanup-shared-checkout")
  _write_terminal_record(app["id"], "shared-first", {
    "id": "shared-first", "type": "pr", "status": "merged",
    "plan": {"repo_path": str(checkout)},
  })
  # A follow-up PR prepared in the same clone is still open.
  _write_terminal_record(app["id"], "shared-follow-up", {
    "id": "shared-follow-up", "type": "pr", "status": "draft",
    "plan": {"repo_path": str(checkout)},
  })

  assert _cleanup(client, auth, app["id"], "shared-first") == {
    "cleaned": False, "kept_for": "shared-follow-up"}
  assert checkout.is_dir()

  # Whichever finishes last removes the checkout.
  _write_terminal_record(app["id"], "shared-follow-up", {
    "id": "shared-follow-up", "type": "pr", "status": "merged",
    "plan": {"repo_path": str(checkout)},
  })
  assert _cleanup(client, auth, app["id"], "shared-follow-up") == {"cleaned": True}
  assert not checkout.exists()
  assert _cleanup(client, auth, app["id"], "shared-first") == {"cleaned": True}


def test_an_open_contribution_using_an_enclosing_folder_keeps_the_checkout(client, auth):
  app = create_local_app(client, auth, name="Cleanup enclosing checkout")
  checkout = _standalone_checkout("cleanup-enclosing-checkout")
  _write_terminal_record(app["id"], "nested-finished", {
    "id": "nested-finished", "type": "pr", "status": "closed",
    "plan": {"repo_path": str(checkout)},
  })
  _write_terminal_record(app["id"], "enclosing-open", {
    "id": "enclosing-open", "type": "pr", "status": "prepared",
    "plan": {"repo_path": str(checkout.parent)},
  })

  assert _cleanup(client, auth, app["id"], "nested-finished") == {
    "cleaned": False, "kept_for": "enclosing-open"}
  assert checkout.is_dir()


def test_an_open_worktree_of_a_finished_clone_keeps_the_clone(client, auth):
  app = create_local_app(client, auth, name="Cleanup worktree of clone")
  clone = _standalone_checkout("cleanup-worktree-clone")
  subprocess.run(
    ["git", "-C", str(clone), "commit", "-q", "--allow-empty", "-m", "base"],
    check=True,
    env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
  )
  worktree = Path(get_settings().data_dir) / "contrib" / "cleanup-worktree-open" / "repo"
  worktree.parent.mkdir(parents=True)
  subprocess.run(
    ["git", "-C", str(clone), "worktree", "add", "-q", "--detach", str(worktree)],
    check=True,
  )
  _write_terminal_record(app["id"], "clone-finished", {
    "id": "clone-finished", "type": "pr", "status": "merged",
    "plan": {"repo_path": str(clone)},
  })
  _write_terminal_record(app["id"], "worktree-open", {
    "id": "worktree-open", "type": "pr", "status": "open",
    "plan": {"repo_path": str(worktree)},
  })

  # The open worktree keeps its Git data inside the finished clone.
  assert _cleanup(client, auth, app["id"], "clone-finished") == {
    "cleaned": False, "kept_for": "worktree-open"}
  assert clone.is_dir()
  status = subprocess.run(["git", "-C", str(worktree), "status", "--short"], check=False)
  assert status.returncode == 0


def test_an_open_worktree_of_a_separate_git_dir_clone_keeps_its_git_data(client, auth):
  """A clone whose Git data sits in a sibling git folder, as the Möbius adapter
  stages apps without a Git origin: removal deletes that folder too."""
  app = create_local_app(client, auth, name="Cleanup separate git dir")
  base = Path(get_settings().data_dir) / "contrib" / "cleanup-separate-git"
  clone, git_dir = base / "worktree", base / "git"
  clone.mkdir(parents=True)
  env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
  subprocess.run(["git", "init", "-q", f"--separate-git-dir={git_dir}", str(clone)], check=True)
  subprocess.run(["git", "-C", str(clone), "commit", "-q", "--allow-empty", "-m", "base"],
                 check=True, env=env)
  follow_up = Path(get_settings().data_dir) / "contrib" / "cleanup-separate-follow-up" / "repo"
  follow_up.parent.mkdir(parents=True)
  subprocess.run(["git", "-C", str(clone), "worktree", "add", "-q", "--detach", str(follow_up)],
                 check=True)
  _write_terminal_record(app["id"], "separate-finished", {
    "id": "separate-finished", "type": "pr", "status": "merged",
    "plan": {"repo_path": str(clone)},
  })
  _write_terminal_record(app["id"], "separate-follow-up", {
    "id": "separate-follow-up", "type": "pr", "status": "draft",
    "plan": {"repo_path": str(follow_up)},
  })

  assert _cleanup(client, auth, app["id"], "separate-finished") == {
    "cleaned": False, "kept_for": "separate-follow-up"}
  assert git_dir.is_dir()
  assert subprocess.run(["git", "-C", str(follow_up), "status", "--short"],
                        check=False).returncode == 0
