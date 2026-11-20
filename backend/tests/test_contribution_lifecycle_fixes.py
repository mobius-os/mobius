"""The fixes that are always on, whichever way the app's `lifecycle_writer` is set.

Each one is a place where a route used to lose or keep the wrong thing; every test here fails on
the code before the lifecycle work and passes after it.
"""
import json
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest

from app import contribution_autopilot as autopilot
from app import contribution_autopilot_recovery as recovery
from app import github_contributions as gc
from app.config import get_settings
from app.database import SessionLocal
from app.routes import github as github_routes
from app.storage_io import atomic_write
from app.timeutil import now_naive_utc
from test_app_fixtures import create_local_app
from tests.test_contribution_follow import (  # noqa: F401  (fixtures and helpers)
  _claim, _grant, _record_path, _sent_record, _toggle, _write, desk, live_pull,
)
from tests.test_contribution_staging import REVISED, _commit, staging  # noqa: F401

PR_URL = "https://github.com/octo/project/pull/7"


# Re-preparing a public PR answers what GitHub raised about the version it replaces


def _public_record(staging, monkeypatch, **fields) -> Path:
  """An open PR whose record carries what GitHub raised, and a new commit ready to restage."""
  record = staging["new_record"]().json()["record"]
  path = Path(get_settings().data_dir) / "apps" / str(staging["app_id"]) / "contributions" / "greet.json"
  path.write_text(json.dumps({
    **record, "status": "open", "number": 7, "url": PR_URL, "head_repository": "octo/project", **fields,
  }))
  live = {
    "error": None, "head_sha": record["plan"]["head_sha"], "base_branch": "main",
    "base_sha": staging["base"], "title": "Add a greeting", "body": "Adds a greeting.",
  }
  monkeypatch.setattr(github_routes, "_autopilot_live_target", lambda *_: live)
  _commit(staging["worktree"], {"greet.py": REVISED}, "Answer review")
  return path


RAISED = {
  "needs_attention": True,
  "attention": {"type": "checks_failed", "key": "checks_failed:old"},
  "waiting_attention": [
    {"type": "merge_conflict", "key": "merge_conflict:old"},
    {"type": "human_required", "key": "human:1"},
  ],
  "outside_activity": {"type": "outside_activity", "key": "outside_activity:1"},
  "reconciliation_hint": {"kind": "already_landed", "main_sha": "f" * 40},
  "reconciliation_probe": {"main_sha": "f" * 40, "checked_at": "2026-11-17T00:00:00Z"},
}


def test_restaging_a_public_pr_clears_what_github_raised_about_the_old_version(staging, monkeypatch):
  _public_record(staging, monkeypatch, **RAISED)
  response = staging["stage"]()
  assert response.status_code == 200, response.text
  record = response.json()["record"]
  assert record["status"] == "prepared" and record["plan"]["action"] == "pr_update"
  assert record["needs_attention"] is False and record["attention"] is None
  assert record["waiting_attention"] == [{"type": "human_required", "key": "human:1"}]  # an escalation stays
  assert "outside_activity" not in record  # what GitHub raised about the old version
  assert "reconciliation_hint" not in record
  assert "reconciliation_probe" not in record  # the app looks again at the new version


def test_restaging_keeps_an_escalation_that_is_on_show(staging, monkeypatch):
  escalation = {"type": "human_required", "key": "human:1"}
  _public_record(staging, monkeypatch, **{**RAISED, "attention": escalation})
  record = staging["stage"]().json()["record"]
  assert record["attention"] == escalation and record["needs_attention"] is True
  assert [item["type"] for item in record["waiting_attention"]] == ["human_required"]


def test_an_autopilot_round_restaging_in_place_keeps_its_own_attention_until_it_completes(staging, monkeypatch):
  _public_record(staging, monkeypatch, **RAISED)
  monkeypatch.setattr(
    autopilot, "get_row", lambda *_: github_routes.models.ContributionAutopilot(push_mode="auto"),
  )
  monkeypatch.setattr(autopilot, "verify_claim", lambda _row, run_id: run_id == "round-1")
  record = staging["stage"](autopilot_run_id="round-1").json()["record"]
  assert record["status"] == "open"
  assert record["attention"] == RAISED["attention"] and record["needs_attention"] is True
  assert record["waiting_attention"] == RAISED["waiting_attention"]


def test_a_first_prepare_has_no_attention_to_clear_and_loses_nothing(staging):
  record = staging["new_record"]().json()["record"]
  assert record["status"] == "prepared" and record["plan"]["action"] == "pr"
  assert not record.get("needs_attention") and "reconciliation_hint" not in record


# Resume and a finished round clear only what is theirs


def _resume(desk, db, **fields) -> dict:
  record = {**_sent_record(), **fields}
  _write(desk.app_id, record)
  _grant(db, desk.app_id, record)
  assert _toggle(desk, {"enabled": True}).status_code == 200
  return json.loads(_record_path(desk.app_id).read_text())


def test_resume_leaves_a_failing_check_alone(desk, db):
  shown = {"type": "checks_failed", "key": "checks_failed:aaa"}
  saved = _resume(desk, db, needs_attention=True, attention=shown)
  assert saved["attention"] == shown and saved["needs_attention"] is True


def test_resume_clears_the_escalation_and_leaves_what_waits_behind_it(desk, db):
  waiting = [{"type": "checks_failed", "key": "checks_failed:aaa"}]
  saved = _resume(
    desk, db, needs_attention=True, attention={"type": "human_required", "key": "human:1"},
    waiting_attention=waiting, outside_activity={"type": "outside_activity", "key": "o:1"},
  )
  assert saved["attention"] is None and saved["needs_attention"] is False
  assert saved["waiting_attention"] == waiting
  assert saved["outside_activity"] == {"type": "outside_activity", "key": "o:1"}


def test_resume_never_touches_the_flags_waiting_behind_the_one_on_show(desk, db):
  """Main's Resume only ever wrote the attention on show; an escalation or an untyped note that waits stays."""
  waiting = [{"type": "human_required", "key": "human:2"}, {"key": "untyped:1"}]
  saved = _resume(
    desk, db, needs_attention=True, attention={"type": "human_required", "key": "human:1"},
    waiting_attention=waiting,
  )
  assert saved["attention"] is None and saved["needs_attention"] is False
  assert saved["waiting_attention"] == waiting


def _complete(desk, db, outcome="handled", **fields) -> dict:
  record = {**_sent_record(), **fields}
  _write(desk.app_id, record)
  _grant(db, desk.app_id, record)
  run_id = _claim(db, desk.app_id)
  response = desk.client.post(
    f"/api/github/contributions/{desk.app_id}/rec1/complete",
    json={"run_id": run_id, "outcome": outcome, "summary": "Answered."}, headers=desk.owner,
  )
  assert response.status_code == 200, response.text
  return json.loads(_record_path(desk.app_id).read_text())


def test_a_finished_round_clears_the_github_attention_it_answered_and_nothing_else(desk, db):
  waiting = [{"type": "merge_conflict", "key": "merge_conflict:aaa"}]
  parked = {"type": "outside_activity", "key": "outside_activity:1"}
  saved = _complete(
    desk, db, needs_attention=True, attention={"type": "changes_requested", "key": "review:1"},
    waiting_attention=waiting, outside_activity=parked,
  )
  assert saved["attention"] is None and saved["needs_attention"] is False
  assert saved["waiting_attention"] == waiting and saved["outside_activity"] == parked


def test_a_finished_round_never_clears_an_escalation(desk, db):
  escalation = {"type": "human_required", "key": "human:1"}
  saved = _complete(desk, db, needs_attention=True, attention=escalation)
  assert saved["attention"] == escalation and saved["needs_attention"] is True


# A lease that expires is mirrored onto the record, as every other change is


@pytest.mark.asyncio
async def test_an_expired_lease_is_mirrored_onto_the_record_by_the_recovery_pass(fresh_db, monkeypatch):
  monkeypatch.setattr(autopilot, "_reclaimed", set())
  db = SessionLocal()
  path = Path(get_settings().data_dir) / "apps" / "1" / "contributions" / "wedged.json"
  path.parent.mkdir(parents=True, exist_ok=True)
  atomic_write(path, json.dumps({"id": "wedged", "type": "pr", "status": "open", "number": 7, "url": PR_URL}))
  try:
    autopilot.stamp_grant(db, 1, "wedged", head_sha="a" * 40)
    autopilot.claim_for_round(db, 1, "wedged", attention_key="k1", event_at="2026-07-01T00:00:00Z")
    await autopilot.mirror_to_ledger(1, "wedged")
    assert json.loads(path.read_text())["autopilot"]["state"] == "responding"
    autopilot.get_row(db, 1, "wedged").lease_expires_at = now_naive_utc() - timedelta(minutes=1)
    db.commit()

    assert autopilot.sweep_expired_leases(db) == 1
    assert json.loads(path.read_text())["autopilot"]["state"] == "responding"  # the row moved, the mirror lags
    await recovery.recover_resolved_blocks()
  finally:
    db.close()
  mirror = json.loads(path.read_text())["autopilot"]
  assert mirror["state"] == "idle" and mirror["last_round"]["outcome"] == "stale"


@pytest.mark.asyncio
async def test_a_lease_is_mirrored_once_and_a_record_that_was_dropped_is_no_trouble(fresh_db, monkeypatch):
  monkeypatch.setattr(autopilot, "_reclaimed", set())
  autopilot._note_reclaimed(1, "dropped-record")
  assert await autopilot.mirror_reclaimed_leases() == 1  # nothing to write, nothing raised
  assert await autopilot.mirror_reclaimed_leases() == 0


# Discarding an update leaves its public PR followed and its checkout in place, once the app runs the
# engine (`lifecycle_writer: "engine"`). In shadow mode the app still ends the PR's life with the discard, so
# cleanup treats the record as finished exactly as it always did.


def _engine_mode(app_id: int) -> None:
  settings = Path(get_settings().data_dir) / "apps" / str(app_id) / "settings.json"
  settings.parent.mkdir(parents=True, exist_ok=True)
  settings.write_text(json.dumps({"lifecycle_writer": "engine"}))


def _abandoned_update(app_id: int, record_id: str, checkout: Path, **fields) -> dict:
  root = Path(get_settings().data_dir) / "apps" / str(app_id) / "contributions"
  root.mkdir(parents=True, exist_ok=True)
  record = {
    "id": record_id, "type": "pr", "status": "abandoned", "repo": "octo/project",
    "plan": {"action": "pr_update", "repo_path": str(checkout)}, **fields,
  }
  atomic_write(root / f"{record_id}.json", json.dumps(record))
  return record


def _checkout(name: str) -> Path:
  checkout = Path(get_settings().data_dir) / "contrib" / name / "repo"
  (checkout / ".git").mkdir(parents=True)
  return checkout


def _cleanup(client, auth, app_id: int, record_id: str):
  response = client.post(f"/api/github/contributions/{app_id}/{record_id}/cleanup-staging", headers=auth)
  assert response.status_code == 200, response.text
  return response.json()


def _grant_enabled(app_id: int, record_id: str) -> bool:
  with SessionLocal() as db:
    return bool(autopilot.get_row(db, app_id, record_id).enabled)


def test_cleanup_of_a_discarded_update_keeps_its_checkout_and_its_follow_up_in_engine_mode(client, auth):
  app = create_local_app(client, auth, name="Discarded update")
  _engine_mode(app["id"])
  checkout = _checkout("discarded-update")
  _abandoned_update(app["id"], "upd", checkout, url=PR_URL, number=7)
  with SessionLocal() as db:
    autopilot.stamp_grant(db, app["id"], "upd", head_sha="a" * 40)
  assert _cleanup(client, auth, app["id"], "upd") == {"cleaned": False}
  assert checkout.exists() and _grant_enabled(app["id"], "upd")


def test_cleanup_of_a_discarded_update_is_exactly_what_it_was_in_shadow_mode(client, auth):
  """No setting, or `shadow`: the word `abandoned` ends the record, so its checkout goes and its grant closes."""
  app = create_local_app(client, auth, name="Discarded update, shadow")
  checkout = _checkout("discarded-update-shadow")
  _abandoned_update(app["id"], "upd", checkout, url=PR_URL, number=7)
  with SessionLocal() as db:
    autopilot.stamp_grant(db, app["id"], "upd", head_sha="a" * 40)
  assert _cleanup(client, auth, app["id"], "upd") == {"cleaned": True}
  assert not checkout.exists() and not _grant_enabled(app["id"], "upd")


def test_cleanup_of_a_dropped_private_record_still_removes_its_checkout_and_closes_its_follow_up(client, auth):
  app = create_local_app(client, auth, name="Dropped record")
  checkout = _checkout("dropped-record")
  _abandoned_update(app["id"], "drop", checkout)
  with SessionLocal() as db:
    autopilot.stamp_grant(db, app["id"], "drop", head_sha="a" * 40)
  assert _cleanup(client, auth, app["id"], "drop") == {"cleaned": True}
  assert not checkout.exists() and not _grant_enabled(app["id"], "drop")


def test_cleanup_of_a_record_that_is_not_finished_never_closes_its_follow_up_in_engine_mode(client, auth):
  app = create_local_app(client, auth, name="Still open")
  _engine_mode(app["id"])
  checkout = _checkout("still-open")
  _abandoned_update(app["id"], "live", checkout, status="open", url=PR_URL, number=7)
  with SessionLocal() as db:
    autopilot.stamp_grant(db, app["id"], "live", head_sha="a" * 40)
  assert _cleanup(client, auth, app["id"], "live") == {"cleaned": False}
  assert checkout.exists() and _grant_enabled(app["id"], "live")


def test_cleanup_of_a_record_that_is_not_finished_closes_its_follow_up_in_shadow_mode_as_it_always_did(client, auth):
  """The route has no status guard on main: the grant is closed out whatever the word. Shadow mode keeps that."""
  app = create_local_app(client, auth, name="Still open, shadow")
  checkout = _checkout("still-open-shadow")
  _abandoned_update(app["id"], "live", checkout, status="open", url=PR_URL, number=7)
  with SessionLocal() as db:
    autopilot.stamp_grant(db, app["id"], "live", head_sha="a" * 40)
  assert _cleanup(client, auth, app["id"], "live") == {"cleaned": False}
  assert checkout.exists() and not _grant_enabled(app["id"], "live")


def _unpins(monkeypatch) -> list[str]:
  """The records whose pinned draft the cleanup route released (the equivalence settlement does it)."""
  source = Path(get_settings().data_dir) / "platform"
  unpinned: list[str] = []
  monkeypatch.setattr(gc, "_equivalence_source_repo", lambda record: (source, source))
  monkeypatch.setattr(gc.contribution_staging, "unpin_draft", lambda _repo, record_id: unpinned.append(record_id))
  return unpinned


def test_cleanup_of_a_discarded_update_keeps_its_pinned_draft_in_engine_mode(client, auth, monkeypatch):
  app = create_local_app(client, auth, name="Pinned draft")
  _engine_mode(app["id"])
  unpinned = _unpins(monkeypatch)
  _abandoned_update(app["id"], "upd", _checkout("pinned-draft"), url=PR_URL, number=7)
  assert _cleanup(client, auth, app["id"], "upd") == {"cleaned": False}
  assert unpinned == []  # its PR goes on, and the next update starts from that draft


def test_cleanup_of_an_abandoned_record_releases_its_pinned_draft_in_shadow_mode_as_it_always_did(client, auth, monkeypatch):
  app = create_local_app(client, auth, name="Pinned draft, shadow")
  unpinned = _unpins(monkeypatch)
  _abandoned_update(app["id"], "upd", _checkout("pinned-draft-shadow"), url=PR_URL, number=7)
  assert _cleanup(client, auth, app["id"], "upd") == {"cleaned": True}
  assert unpinned == ["upd"]


def test_cleanup_of_a_merged_record_closes_its_follow_up_as_it_always_did(client, auth, monkeypatch):
  monkeypatch.setattr(github_routes, "_personal_merge_commit", lambda *_: None)
  app = create_local_app(client, auth, name="Merged")
  checkout = _checkout("merged-record")
  _abandoned_update(app["id"], "done", checkout, status="merged", url=PR_URL, number=7)
  with SessionLocal() as db:
    autopilot.stamp_grant(db, app["id"], "done", head_sha="a" * 40)
  assert _cleanup(client, auth, app["id"], "done") == {"cleaned": True}
  assert not _grant_enabled(app["id"], "done")


def test_a_discarded_update_still_needs_the_checkout_it_shares_with_a_finished_record_in_engine_mode(client, auth):
  app = create_local_app(client, auth, name="Shared checkout")
  _engine_mode(app["id"])
  checkout = _checkout("shared-checkout")
  _abandoned_update(app["id"], "finished", checkout, status="closed")
  _abandoned_update(app["id"], "discarded", checkout, url=PR_URL, number=7)
  assert _cleanup(client, auth, app["id"], "finished") == {"cleaned": False, "kept_for": "discarded"}
  assert checkout.exists()


def test_a_discarded_update_does_not_keep_the_checkout_of_a_finished_record_in_shadow_mode(client, auth):
  app = create_local_app(client, auth, name="Shared checkout, shadow")
  checkout = _checkout("shared-checkout-shadow")
  _abandoned_update(app["id"], "finished", checkout, status="closed")
  _abandoned_update(app["id"], "discarded", checkout, url=PR_URL, number=7)
  assert _cleanup(client, auth, app["id"], "finished") == {"cleaned": True}
  assert not checkout.exists()


def test_one_records_damaged_vector_never_fails_the_cleanup_of_another_in_engine_mode(client, auth):
  """The cleanup of any record reads every sibling's lifecycle. A sibling vector with a deadline on a
  state that has none (newer tables, version unmoved) is read as none, so the sweep goes on."""
  app = create_local_app(client, auth, name="Damaged sibling")
  _engine_mode(app["id"])
  checkout = _checkout("damaged-sibling")
  damaged = {"v": 1, "history": [], "attention": {"flags": []}, "regions": {
    "publication": {"state": "MERGED", "since": 1, "deadline": 5},
    "update": {"state": "NONE", "since": 1, "deadline": None},
    "followup": {"state": "OFF", "since": 1, "deadline": None}}}
  _abandoned_update(app["id"], "sibling", _checkout("damaged-other"), status="merged", url=PR_URL, number=8, lifecycle=damaged)
  _abandoned_update(app["id"], "finished", checkout, status="closed")
  assert _cleanup(client, auth, app["id"], "finished") == {"cleaned": True}
  assert not checkout.exists()


# Only a merge into the default branch lands the work


def _gh_reads(monkeypatch, pull: dict, default: str | None = "main"):
  calls = []

  def gh(_repo, *args, check=False):
    calls.append(args[:2])
    if args[:2] == ("pr", "view"):
      return subprocess.CompletedProcess(args, 0, json.dumps(pull), "")
    return subprocess.CompletedProcess(args, 0 if default else 1, (default or "") + "\n", "")

  monkeypatch.setattr(gc._git_ops, "_gh", gh)
  return calls


MERGED = {"state": "MERGED", "mergeCommit": {"oid": "a" * 40}}
RECORD = {"repo": "octo/project", "number": 7}


@pytest.mark.parametrize("base,default,landed", [
  ("main", "main", True),
  ("stack-parent", "main", False),
  ("main", "trunk", False),
  (None, "main", True),  # GitHub named no base: nothing is said against landing
  ("stack-parent", None, True),  # the default branch cannot be read: likewise
])
def test_a_merge_commit_is_the_landed_work_only_when_the_base_is_the_default_branch(monkeypatch, base, default, landed):
  _gh_reads(monkeypatch, {**MERGED, **({"baseRefName": base} if base else {})}, default)
  assert gc.github_merge_commit(RECORD, Path("/nowhere")) == ("a" * 40 if landed else None)


@pytest.mark.parametrize("failure", [OSError("gh is not installed"), subprocess.TimeoutExpired("gh", 30)])
def test_a_default_branch_read_that_cannot_run_says_nothing_against_landing(monkeypatch, failure):
  """Fail open, as before this check existed: gh missing or timing out must not turn a merge into 'not landed'."""
  calls = _gh_reads(monkeypatch, {**MERGED, "baseRefName": "stack-parent"})
  real = gc._git_ops._gh

  def gh(repo, *args, check=False):
    if args[:2] == ("repo", "view"):
      raise failure
    return real(repo, *args, check=check)

  monkeypatch.setattr(gc._git_ops, "_gh", gh)
  assert gc.github_merge_commit(RECORD, Path("/nowhere")) == "a" * 40
  assert ("pr", "view") in calls


def test_an_unmerged_pr_has_no_merge_commit_and_costs_no_default_branch_read(monkeypatch):
  calls = _gh_reads(monkeypatch, {"state": "OPEN", "baseRefName": "main", "mergeCommit": None})
  assert gc.github_merge_commit(RECORD, Path("/nowhere")) is None
  assert ("repo", "view") not in calls
