"""Reviewer regressions for repair receipts, checkout ownership and admission."""
import asyncio
import subprocess
import threading
import logging
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app import agent_work_claims, contribution_review_repair_actions as actions
from app import contribution_review_repairs as repairs, contribution_review_runs as domain, models
from app import chat_queue
from app.chat_writer import create_chat
from app.contribution_review_effects import DispatchResult
from app.deps import Principal
from app.routes import contribution_reviews as routes
from app.runtime_supervisors import RuntimeSupervisors
from app.database import SessionLocal
from tests.test_contribution_review_runs import setup, ITEM
from tests.test_contribution_review_workflows import (BASE, NEW, SHA, checkout_body,
  configure_repair, publish_body, takeover)


@pytest.mark.parametrize("result_state,expected_release", [("not_dispatched", True), ("unknown", False)])
def test_push_result_releases_only_definite_non_effect(setup, monkeypatch, result_state, expected_release):
  db, row, principal = configure_repair(setup, monkeypatch)
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw:
    DispatchResult(result_state, reason="Exact refusal or uncertainty"))
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  claim = db.query(models.AgentWorkClaim).one()
  assert (claim.released_at is not None) is expected_release
  attempt = row.outcomes_json[domain.key(ITEM)].get("repair_attempts", [])
  assert (not attempt) if expected_release else attempt[-1]["state"] == "push_unknown"
  db.add(create_chat(id="fresh-repair-chat", title="Fresh approval"))
  db.add(models.ChatRun(id="fresh-repair-run", chat_id="fresh-repair-chat", status="running"))
  db.flush()
  peer = models.ContributionReviewRun(id="fresh-repair-grant", app_id=row.app_id,
    owner_id=row.owner_id, request_id="fresh-repair-request", mode=row.mode,
    targets_json=row.targets_json, outcomes_json={}, chat_id="fresh-repair-chat",
    github_actor_id=row.github_actor_id, app_nonce=row.app_nonce, options_json=row.options_json)
  db.add(peer)
  db.commit()
  fresh = Principal(owner=principal.owner, app_id=None, chat_id="fresh-repair-chat", run_id="fresh-repair-run")
  target = peer.targets_json[0]
  reservation = domain.arm_repair(db, peer, target, {}, {"id": "fresh", "state": "pushing",
    "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}, fresh)
  if expected_release:
    assert reservation is None
    assert peer.outcomes_json[domain.key(target)]["repair_attempts"][-1]["id"] == "fresh"
  else:
    assert isinstance(reservation, str) and "owning" in reservation
    assert not peer.outcomes_json[domain.key(target)].get("repair_attempts")


def test_remote_rejection_keeps_audit_but_allows_fresh_proved_repair(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  row.options_json = {**row.options_json, "max_rounds": 1}
  db.commit()
  calls = []
  def push(*args, **kwargs):
    calls.append(1)
    if len(calls) == 1:
      return DispatchResult("not_dispatched",
        receipt={"kind": "remote_ref_rejection", "head_sha": NEW},
        reason="GitHub rejected the exact repair ref; no branch update was applied.")
    return DispatchResult.confirmed({"head_sha": NEW})
  monkeypatch.setattr(repairs, "push_repair", push)
  first = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))["run"]["items"][0]
  assert first["repair_attempts"][-1]["state"] == "rejected"
  assert first["repair_attempts"][-1]["head_sha"] == NEW
  assert db.query(models.AgentWorkClaim).one().released_at is not None
  second = asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))["run"]["items"][0]
  assert calls == [1, 1]
  assert [a["state"] for a in second["repair_attempts"]] == ["rejected", "pushed"]
  assert second["successor"]["head_sha"] == NEW
  assert domain.effective_target(row, row.targets_json[0])["head_sha"] == NEW


def test_confirmed_publish_discards_checkout_after_durable_fact(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  discarded = []
  def discard(path):
    db.refresh(row)
    outcome = row.outcomes_json[domain.key(ITEM)]
    assert outcome["repair_attempts"][-1]["state"] == "pushed"
    assert outcome["successor"]["head_sha"] == NEW
    discarded.append(path)
  monkeypatch.setattr(repairs, "discard_checkout", discard)
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: DispatchResult.confirmed({"head_sha": NEW}))
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert discarded == ["/fake/server-repair"]


def test_missing_recorded_checkout_is_reprepared(setup, monkeypatch):
  db, row, principal = takeover(setup, monkeypatch)
  old = {"checkout": "/fake/missing", "initial_head_sha": SHA, "allowed_files": ["owned.py"]}
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "repairing", "checkout": old})
  monkeypatch.setattr(repairs, "checkout_present", lambda path: False)
  monkeypatch.setattr(repairs, "fetch_target_base", lambda *a: pytest.fail("missing path reused"))
  calls = []
  monkeypatch.setattr(repairs, "prepare_checkout", lambda *a: calls.append(1) or {**old, "checkout": "/fake/new"})
  result = asyncio.run(routes.repair_checkout(1, row.id, checkout_body(), db, principal))
  assert calls == [1] and result["checkout"]["checkout"] == "/fake/new"


def test_gc_protects_active_and_uncertain_but_collects_stopped_and_orphans(setup, monkeypatch, tmp_path):
  db, row, _ = setup
  row.mode = "review_fix_merge"
  db.commit()
  monkeypatch.setattr(repairs, "_checkout_root", lambda: tmp_path)
  paths = {name: tmp_path / f"repair-{name}" for name in ("active", "unknown", "terminal", "orphan")}
  for path in paths.values():
    path.mkdir()
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "repairing",
    "checkout": {"checkout": str(paths["active"])}, "repair_attempts": [{
      "state": "push_unknown", "checkout": str(paths["unknown"])}]})
  db.add(create_chat(id="completed-editor", title="Completed repair editor"))
  other = models.ContributionReviewRun(id="old-gc", app_id=row.app_id, owner_id=row.owner_id,
    request_id="old-gc", mode=row.mode, targets_json=row.targets_json,
    outcomes_json={"_review_stop": {"state": "requested"}, domain.key(ITEM): {
      "state": "needs_you", "checkout": {"checkout": str(paths["terminal"])}}},
    chat_id="completed-editor", github_actor_id=row.github_actor_id, app_nonce=row.app_nonce)
  db.add(other)
  db.commit()
  monkeypatch.setattr(repairs, "_last_activity", lambda path: 0)
  repairs.sweep_stale_checkouts(**actions.checkout_gc_scope(db), now=10**12)
  assert paths["active"].exists() and paths["unknown"].exists()
  assert not paths["terminal"].exists() and not paths["orphan"].exists()


@pytest.mark.parametrize("state,summary,base_advance", [
  ("needs_you", "Commit or discard working changes before validating.", None),
  ("needs_you", "The target base moved before repair dispatch.", None),
  ("reviewing", "Target base advanced; review the combined result.", {
    "head_sha": SHA, "from_base_sha": BASE, "base_sha": "d" * 40}),
])
def test_idle_live_grant_keeps_unpushed_files_until_dormant_grace(setup, monkeypatch, tmp_path,
                                                                    state, summary, base_advance):
  db, row, principal = setup
  row.mode = "review_fix_merge"
  db.get(models.ChatRun, principal.run_id).status = "completed"
  path = tmp_path / "repair-idle"
  path.mkdir()
  (path / "unpushed.py").write_text("important unpushed edit\n")
  monkeypatch.setattr(repairs, "_checkout_root", lambda: tmp_path)
  outcome = {"state": state, "summary": summary,
    "checkout": {"checkout": str(path), "initial_head_sha": SHA}}
  if base_advance:
    outcome["base_advance"] = base_advance
  domain.save_outcome(db, row, domain.key(ITEM), outcome)
  monkeypatch.setattr(repairs, "_last_activity", lambda candidate: 0)
  scope = actions.checkout_gc_scope(db)
  assert str(path) in scope["dormant_paths"] and str(path) not in scope["terminal_paths"]
  repairs.sweep_stale_checkouts(**scope, now=repairs.STALE_EDIT_SECONDS - 1)
  assert (path / "unpushed.py").read_text() == "important unpushed edit\n"
  repairs.sweep_stale_checkouts(**scope, now=repairs.STALE_EDIT_SECONDS)
  assert not path.exists()


def test_rejected_push_keeps_idle_checkout_through_dormant_grace(setup, monkeypatch, tmp_path):
  db, row, principal = setup
  row.mode = "review_fix_merge"
  db.get(models.ChatRun, principal.run_id).status = "completed"
  path = tmp_path / "repair-rejected"
  path.mkdir()
  (path / "unpushed.py").write_text("preserve rejected commit\n")
  monkeypatch.setattr(repairs, "_checkout_root", lambda: tmp_path)
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "needs_you",
    "checkout": {"checkout": str(path), "initial_head_sha": SHA},
    "repair_attempts": [{"state": "rejected", "from_sha": SHA,
      "head_sha": NEW, "checkout": str(path)}]})
  monkeypatch.setattr(repairs, "_last_activity", lambda candidate: 0)
  repairs.sweep_stale_checkouts(**actions.checkout_gc_scope(db),
    now=repairs.STALE_EDIT_SECONDS - 1)
  assert (path / "unpushed.py").read_text() == "preserve rejected commit\n"


def test_unreceipted_checkout_gets_one_hour_orphan_grace(monkeypatch, tmp_path):
  monkeypatch.setattr(repairs, "_checkout_root", lambda: tmp_path)
  path = tmp_path / "repair-unreceipted"
  path.mkdir()
  monkeypatch.setattr(repairs, "_last_activity", lambda candidate: 0)
  scope = {"protected_paths": set(), "terminal_paths": set(), "dormant_paths": set()}
  repairs.sweep_stale_checkouts(**scope, now=repairs.ORPHAN_GRACE_SECONDS - 1)
  assert path.exists()
  repairs.sweep_stale_checkouts(**scope, now=repairs.ORPHAN_GRACE_SECONDS)
  assert not path.exists()


@pytest.mark.parametrize("settlement", ["merged", "superseded"])
def test_idle_settled_or_superseded_checkout_is_terminal(setup, monkeypatch, tmp_path, settlement):
  db, row, principal = setup
  row.mode = "review_fix_merge"
  db.get(models.ChatRun, principal.run_id).status = "completed"
  path = tmp_path / "repair-settled"
  path.mkdir()
  monkeypatch.setattr(repairs, "_checkout_root", lambda: tmp_path)
  outcome = {"state": "merged" if settlement == "merged" else "reviewing",
    "checkout": {"checkout": str(path), "initial_head_sha": SHA}}
  if settlement == "superseded":
    outcome.update(successor={"head_sha": NEW, "base_sha": BASE},
      repair_attempts=[{"state": "pushed", "from_sha": SHA, "head_sha": NEW, "base_sha": BASE}])
  domain.save_outcome(db, row, domain.key(ITEM), outcome)
  scope = actions.checkout_gc_scope(db)
  assert str(path) in scope["terminal_paths"]


def test_cancelled_but_still_running_child_protects_parent_checkout(setup, monkeypatch, tmp_path):
  db, row, principal = setup
  row.mode = "review_fix_merge"
  db.commit()
  parent = tmp_path / "repair-parent"
  parent.mkdir()
  monkeypatch.setattr(repairs, "_checkout_root", lambda: tmp_path)
  db.get(models.ChatRun, principal.run_id).status = "stopped"
  db.add(create_chat(id="cancelled-child", title="Unsettled child"))
  db.add(models.ChatRun(id="cancelled-child-run", chat_id="cancelled-child", status="running"))
  db.add(models.Delegation(id="cancelled-child-delegation", parent_chat_id=row.chat_id,
    parent_root_run_id=principal.run_id, task_key="repair-check", child_chat_id="cancelled-child",
    provider="codex", model="test", effort="low", scope="write", cwd="/tmp",
    prompt_sha256="a" * 64, cancelled_at=datetime.now(timezone.utc)))
  db.commit()
  domain.save_outcome(db, row, domain.key(ITEM), {"state": "repairing",
    "checkout": {"checkout": str(parent)}})
  monkeypatch.setattr(repairs, "_last_activity", lambda path: 0)
  scope = actions.checkout_gc_scope(db)
  assert str(parent) in scope["protected_paths"]
  repairs.sweep_stale_checkouts(**scope, now=10**12)
  assert parent.exists()


def test_boot_and_periodic_checkout_gc_uses_durable_receipts(setup, monkeypatch):
  db, row, _ = setup
  steps = []
  def quarantine(**scope):
    assert "protected_paths" in scope
    with SessionLocal() as contender:
      contender.connection().exec_driver_sql("PRAGMA busy_timeout=1")
      with pytest.raises(OperationalError):
        contender.execute(text("UPDATE chat_runs SET status=status WHERE id=:run_id"),
          {"run_id": "physical-run"})
    steps.append("renamed-under-fence")
  def delete():
    with SessionLocal() as contender:
      contender.execute(text("UPDATE chat_runs SET status=status WHERE id=:run_id"),
        {"run_id": "physical-run"})
      contender.commit()
    steps.append("deleted-after-commit")
  monkeypatch.setattr(repairs, "quarantine_stale_checkouts", quarantine)
  monkeypatch.setattr(repairs, "delete_quarantined_checkouts", delete)
  actions.sweep_checkout_receipts_once()
  assert steps == ["renamed-under-fence", "deleted-after-commit"]
  visited = threading.Event()
  monkeypatch.setattr(actions, "sweep_checkout_receipts_once", visited.set)
  supervisors = RuntimeSupervisors(settings=SimpleNamespace(data_dir="/tmp"),
    logger=logging.getLogger("test.repair-gc"), restart_authorization=None,
    restart_fallback_chats=[])
  async def exercise():
    task = asyncio.create_task(supervisors._repair_checkout_gc_loop())
    try:
      assert await asyncio.to_thread(visited.wait, 3)
    finally:
      task.cancel()
      await asyncio.gather(task, return_exceptions=True)
  asyncio.run(exercise())


def test_crashed_quarantine_is_reaped_without_touching_live_receipts(monkeypatch, tmp_path):
  monkeypatch.setattr(repairs, "_checkout_root", lambda: tmp_path)
  retired = tmp_path / "repair-retired"
  retired.mkdir()
  (retired / "data").write_text("finished")
  live = tmp_path / "repair-live"
  live.mkdir()
  renamed = repairs.quarantine_stale_checkouts(protected_paths={str(live)},
    terminal_paths={str(retired)}, dormant_paths=set(), now=10**12)
  assert len(renamed) == 1 and not retired.exists() and renamed[0].exists()
  # Simulate a crash after the DB fence commits but before recursive deletion.
  repairs.delete_quarantined_checkouts()
  assert not renamed[0].exists() and live.exists()


def test_periodic_gc_cancellation_waits_for_quarantine_and_delete(monkeypatch):
  entered = threading.Event()
  release = threading.Event()
  def slow_gc():
    entered.set()
    assert release.wait(3)
  monkeypatch.setattr(actions, "sweep_checkout_receipts_once", slow_gc)
  supervisors = RuntimeSupervisors(settings=SimpleNamespace(data_dir="/tmp"),
    logger=logging.getLogger("test.repair-gc-cancel"), restart_authorization=None,
    restart_fallback_chats=[])
  async def exercise():
    task = asyncio.create_task(supervisors._repair_checkout_gc_loop())
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0.01)
    assert not task.done()
    release.set()
    result = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result[0], asyncio.CancelledError)
  asyncio.run(exercise())


def test_runtime_database_services_spawn_repair_gc(monkeypatch):
  from app import chat
  from app.chat import ContinuationSweepResult
  async def no_parks(*args, **kwargs):
    return ContinuationSweepResult()
  monkeypatch.setattr(chat, "sweep_reset_parks", no_parks)
  supervisors = RuntimeSupervisors(settings=SimpleNamespace(data_dir="/tmp"),
    logger=logging.getLogger("test.repair-gc-spawn"), restart_authorization=None,
    restart_fallback_chats=[])
  names = []
  def capture(name, coroutine):
    names.append(name)
    coroutine.close()
  monkeypatch.setattr(supervisors, "_spawn", capture)
  asyncio.run(supervisors._start_chat_supervisors())
  assert "repair-checkout-gc" in names


def test_stranded_pushing_observation_becomes_visible_unknown(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  outcome = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**outcome, "state": "pushing",
    "repair_attempts": [{"id": "armed", "state": "pushing", "from_sha": SHA,
      "head_sha": NEW, "base_sha": BASE, "checkout": "/fake/server-repair"}]})
  monkeypatch.setattr(actions, "reconcile_push", lambda *a, **kw: asyncio.sleep(0, result=False))
  asyncio.run(routes.observe_review(1, row.id, db, Principal(owner=principal.owner, app_id=None)))
  assert row.outcomes_json[domain.key(ITEM)]["repair_attempts"][-1]["state"] == "push_unknown"


def test_timeout_observation_is_unknown_and_never_replays_push(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  outcome = row.outcomes_json[domain.key(ITEM)]
  domain.save_outcome(db, row, domain.key(ITEM), {**outcome, "state": "pushing",
    "repair_attempts": [{"id": "timeout", "state": "pushing", "from_sha": SHA,
      "head_sha": NEW, "base_sha": BASE, "checkout": "/fake/server-repair"}]})
  monkeypatch.setattr(domain, "current_pull", lambda *a: (_ for _ in ()).throw(
    subprocess.TimeoutExpired(["gh", "api"], 10)))
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: pytest.fail("push replayed"))
  owner = Principal(owner=principal.owner, app_id=None)
  for _ in range(2):
    asyncio.run(routes.observe_review(1, row.id, db, owner))
  assert row.outcomes_json[domain.key(ITEM)]["repair_attempts"][-1]["state"] == "push_unknown"


def test_stop_runs_receipt_aware_sweep(setup, monkeypatch):
  db, row, principal = setup
  row.mode = "review_fix_merge"
  db.commit()
  calls = []
  monkeypatch.setattr(actions, "sweep_checkout_receipts_once", lambda: calls.append(1))
  asyncio.run(routes.stop_review(1, row.id, db, Principal(owner=principal.owner, app_id=None)))
  assert calls == [1]


def test_checkout_preparation_runs_writer_fenced_gc(setup, monkeypatch):
  calls = []
  monkeypatch.setattr(actions, "sweep_checkout_receipts_once", lambda: calls.append(1))
  configure_repair(setup, monkeypatch)
  assert calls == [1]


def test_checkout_gc_failure_is_logged_but_does_not_block_preparation(setup, monkeypatch, caplog):
  def broken_gc():
    raise OSError("temporary cleanup failure")
  monkeypatch.setattr(actions, "sweep_checkout_receipts_once", broken_gc)
  db, row, _principal = configure_repair(setup, monkeypatch)
  assert row.outcomes_json[domain.key(ITEM)]["checkout"]["checkout"] == "/fake/server-repair"
  assert "repair checkout GC failed before checkout preparation" in caplog.text


def test_stop_admission_lock_blocks_repair_arm(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: pytest.fail("push after Stop"))
  async def race():
    async with chat_queue.get_transition_lock(f"review-admission:{row.id}"):
      pending = asyncio.create_task(routes.publish_repair(1, row.id, publish_body(), db, principal))
      await asyncio.sleep(0.05)
      assert not pending.done()
      domain.save_outcome(db, row, routes.REVIEW_STOP_KEY, {"state": "requested", "actor": "owner"})
    try:
      await pending
    except HTTPException as exc:
      assert exc.status_code == 409
    else:
      pytest.fail("repair admitted after Stop")
  asyncio.run(race())
  assert not row.outcomes_json[domain.key(ITEM)].get("repair_attempts")


def test_successor_invariant_refusal_never_dispatches_and_releases(setup, monkeypatch):
  db, row, principal = configure_repair(setup, monkeypatch)
  monkeypatch.setattr(domain, "confirmed_successor", lambda *a: (_ for _ in ()).throw(HTTPException(409, "bad history")))
  monkeypatch.setattr(repairs, "push_repair", lambda *a, **kw: pytest.fail("public push"))
  asyncio.run(routes.publish_repair(1, row.id, publish_body(), db, principal))
  assert db.query(models.AgentWorkClaim).one().released_at is not None


def test_confirmed_remote_push_survives_later_projection_failure(setup, monkeypatch):
  db, row, _ = setup
  pending = {"id": "receipt", "state": "push_unknown", "from_sha": SHA,
    "head_sha": NEW, "base_sha": BASE, "checkout": "/fake/server-repair"}
  previous = {"state": "needs_you", "repair_attempts": [pending]}
  domain.save_outcome(db, row, domain.key(ITEM), previous)
  monkeypatch.setattr(domain, "confirmed_successor", lambda *a: (_ for _ in ()).throw(HTTPException(409, "history changed")))
  deleted = []
  monkeypatch.setattr(repairs, "discard_checkout", deleted.append)
  assert asyncio.run(actions.reconcile_push(db, row, ITEM, pending, gh=None, cwd=None))
  actual = row.outcomes_json[domain.key(ITEM)]
  assert actual["repair_attempts"][-1]["state"] == "pushed"
  assert actual["state"] == "needs_you" and deleted == ["/fake/server-repair"]


def test_reservation_helper_enforces_fence_and_claim_cas(setup, monkeypatch):
  db, row, principal = setup
  called = []
  real_fence = domain.fence_public_transition
  def fence(*args):
    called.append(1)
    return real_fence(*args)
  monkeypatch.setattr(domain, "fence_public_transition", fence)
  domain.reserve_public_work(db, row, principal, "test:reservation", "Test", "Test")
  assert called == [1]
  # A stale claim revision cannot pass the exact CAS even when the row fence did.
  real_claim = agent_work_claims.claim_work
  monkeypatch.setattr(agent_work_claims, "claim_work", lambda *a, **kw: {
    **real_claim(*a, **kw), "revision": -1})
  with pytest.raises(HTTPException):
    domain.reserve_public_work(db, row, principal, "test:stale", "Test", "Test")


def test_canonical_diff_receives_repair_sanitized_environment(setup, monkeypatch):
  from app import app_git
  seen = []
  def canonical(*args, **kwargs):
    seen.append(kwargs.get("env"))
    raise RuntimeError("spy complete")
  monkeypatch.setattr(app_git, "_canonical_diff", canonical)
  monkeypatch.setattr(repairs, "_live_head", lambda *a: ({}, {}))
  monkeypatch.setattr(repairs, "_head_destination", lambda *a: ("fork/repo", 1, "topic"))
  monkeypatch.setattr(repairs, "owned_checkout", lambda *a: Path("/tmp/fake-checkout"))
  monkeypatch.setattr(repairs, "_changed_files", lambda *a: {"owned.py"})
  def git(_path, *args, **kwargs):
    text = {"status": "", "rev-parse": NEW, "diff": "owned.py\0"}.get(args[0], "")
    return type("Result", (), {"stdout": text, "returncode": 0})()
  monkeypatch.setattr(repairs, "_git", git)
  checkout = {"checkout": "/tmp/fake-checkout", "head_repo": "fork/repo", "head_repo_id": 1,
    "head_ref": "topic", "initial_head_sha": SHA, "allowed_files": ["owned.py"]}
  with pytest.raises(RuntimeError, match="spy complete"):
    repairs.validate_repair(None, Path("/tmp"), None,
      {"repo": "base/repo", "head_sha": SHA}, checkout)
  assert len(seen) == 1
  assert seen[0]["GIT_CONFIG_GLOBAL"] == "/dev/null"
  assert seen[0]["GIT_CONFIG_NOSYSTEM"] == "1"
  assert seen[0]["GIT_ASKPASS"] == "/bin/false"
