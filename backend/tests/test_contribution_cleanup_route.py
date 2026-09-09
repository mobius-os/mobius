"""Focused route tests for terminal contribution staging disposal."""

import json
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
