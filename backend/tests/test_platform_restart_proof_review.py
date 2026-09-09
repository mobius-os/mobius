"""Failure boundaries of platform-owned restart admission and source proof."""

import asyncio
from datetime import timedelta

import pytest

from app import boot_source, chat, chat_writer, models, platform_restart, restart_ledger
from app import main, restart_util
from app.database import SessionLocal
from app.timeutil import now_naive_utc
from tests.test_platform_restart_source_proof import _restart_repo, _commit, _FakeTimer


def test_admission_exception_releases_only_unstarted_latch(monkeypatch):
  def fail(_action_id):
    raise RuntimeError("database unavailable")
  monkeypatch.setattr(platform_restart, "admit_execution_if_current", fail)
  _FakeTimer.instances = []
  monkeypatch.setattr(restart_util.threading, "Timer", _FakeTimer)
  with pytest.raises(RuntimeError, match="database unavailable"):
    asyncio.run(restart_util.restart_this_worker(action_id="approved"))
  assert not restart_util._RESTART_ADMITTED
  assert not chat.draining
  assert not _FakeTimer.instances

  requests = []
  async def drain():
    chat.begin_drain()
    return "boot", "nonce", []
  monkeypatch.setattr(restart_util, "_drain_exact_restart", drain)
  monkeypatch.setattr(restart_ledger, "request_restart", lambda **kw: requests.append(kw))
  monkeypatch.setattr(restart_util.os, "kill", lambda *_: pytest.fail("real signal"))
  asyncio.run(restart_util.restart_this_worker())
  assert len(requests) == 1
  assert len(_FakeTimer.instances) == 1


def _proof_wait(db, requirement):
  now = now_naive_utc()
  db.add(models.Chat(id="proof-review", title="Proof", messages=[]))
  wait = models.ChatWait(
    id="proof-review-wait", chat_id="proof-review", description="Activate",
    condition_owner="startup", kind=platform_restart.ACTIVATION_WAIT_KIND,
    condition_json=requirement, interval_secs=30, created_at=now,
    deadline_at=now + timedelta(days=1), next_check_at=now,
  )
  db.add(wait)
  db.commit()
  return wait


def _approved_boot(monkeypatch, tmp_path):
  repo, source, base = _restart_repo(monkeypatch, tmp_path)
  source.write_text("VALUE = 'approved'\n")
  target = _commit(repo, "approved")
  requirement = platform_restart.build_restart_requirement(repo)
  from app import platform_update
  platform_update.SERVING_SHA_FILE.write_text(target)
  monkeypatch.setattr(boot_source, "BOOT_SOURCE_INPUTS", boot_source.capture_boot_source_inputs(
    repo, source_kind="platform", source_sha=target,
  ))
  monkeypatch.setattr(chat_writer, "writer_readiness", lambda: (True, None))
  return repo, source, requirement


def test_router_degraded_boot_cannot_satisfy_activation(monkeypatch, tmp_path):
  repo, _source, requirement = _approved_boot(monkeypatch, tmp_path)
  monkeypatch.setattr(main, "router_import_failures", lambda: ["chats_stream"])
  assert main.service_readiness() == {
    "ready": False, "reason": "router_import_failure", "failed_routers": ["chats_stream"],
  }
  with SessionLocal() as db:
    _proof_wait(db, requirement)
    snapshot = platform_restart.capture_ready_boot_snapshot(db, boot_id="degraded", repo=repo)
    assert snapshot.loaded_files_json == requirement["files"]
    assert snapshot.service_ready is False
    assert not platform_restart.requirement_matches_snapshot(requirement, snapshot)


def test_source_changed_after_boot_boundary_is_not_loaded_proof(monkeypatch, tmp_path):
  repo, source, requirement = _approved_boot(monkeypatch, tmp_path)
  source.write_text("VALUE = 'changed during imports'\n")
  with SessionLocal() as db:
    _proof_wait(db, requirement)
    snapshot = platform_restart.capture_ready_boot_snapshot(db, boot_id="drifted", repo=repo)
    assert not snapshot.service_ready
    assert snapshot.loaded_files_json == {}


def test_approved_bytes_copied_after_import_boundary_do_not_prove_activation(monkeypatch, tmp_path):
  repo, source, base = _restart_repo(monkeypatch, tmp_path)
  old_inputs = boot_source.capture_boot_source_inputs(repo, source_kind="platform", source_sha=base)
  # Python imports the old source in this boot; an edit later writes the very
  # bytes the waiter wants. A post-startup-only filesystem hash would pass.
  source.write_text("VALUE = 'approved'\n")
  target = _commit(repo, "approved after import")
  requirement = platform_restart.build_restart_requirement(repo)
  monkeypatch.setattr(boot_source, "BOOT_SOURCE_INPUTS", old_inputs)
  monkeypatch.setattr(chat_writer, "writer_readiness", lambda: (True, None))
  with SessionLocal() as db:
    _proof_wait(db, requirement)
    snapshot = platform_restart.capture_ready_boot_snapshot(db, boot_id="old-imports", repo=repo)
    assert not snapshot.service_ready
    assert not platform_restart.requirement_matches_snapshot(requirement, snapshot)


def test_boot_capture_is_immutable_and_rejects_uncommitted_inputs(monkeypatch, tmp_path):
  repo, source, base = _restart_repo(monkeypatch, tmp_path)
  inputs = boot_source.capture_boot_source_inputs(repo, source_kind="platform", source_sha=base)
  assert inputs.valid
  with pytest.raises(TypeError):
    inputs.files["backend/app/example.py"]["sha256"] = "changed"
  source.write_text("VALUE = 'dirty'\n")
  assert not boot_source.capture_boot_source_inputs(
    repo, source_kind="platform", source_sha=base,
  ).valid


def test_boot_capture_before_any_wait_can_prove_deletion(monkeypatch, tmp_path):
  repo, source, base = _restart_repo(monkeypatch, tmp_path)
  source.unlink()
  target = _commit(repo, "delete")
  inputs = boot_source.capture_boot_source_inputs(repo, source_kind="platform", source_sha=target)
  assert inputs.unchanged_manifest(repo, ["backend/app/example.py"]) == {
    "backend/app/example.py": {"state": "absent"},
  }


@pytest.mark.parametrize("status,changed", [("claimed", True), ("admitted", False), ("activated", False)])
def test_response_end_settles_only_undispatched_execution(monkeypatch, status, changed):
  requirement = {
    "version": 1, "action_id": "platform-restart:handoff", "source_boot_id": "current-boot",
    "files": {"backend/app/example.py": {"state": "absent"}},
  }
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "current-boot")
  with SessionLocal() as db:
    wait = _proof_wait(db, requirement)
    db.add(models.PlatformRestartExecution(
      action_id=requirement["action_id"], chat_id=wait.chat_id, wait_id=wait.id,
      question_id="handoff-card", source_boot_id="current-boot", status=status,
      requirement_json=requirement, claimed_at=now_naive_utc(),
    ))
    db.commit()
  assert platform_restart.settle_undispatched_execution(requirement["action_id"]) == changed
  assert platform_restart.settle_undispatched_execution(requirement["action_id"]) is False
  with SessionLocal() as db:
    execution = db.get(models.PlatformRestartExecution, requirement["action_id"])
    assert execution.status == ("uncertain" if changed else status)
    if changed:
      verdict, detail = platform_restart.activation_wait_verdict(db, db.get(models.ChatWait, "proof-review-wait"))
      assert verdict == "failed"
      assert "No action was replayed" in detail


@pytest.mark.parametrize("elapsed,admitting,expected", [(0, False, "pending"), (121, False, "failed"), (121, True, "pending")])
def test_wait_checker_bounds_failed_response_finalizer_without_dispatch(
  monkeypatch, elapsed, admitting, expected,
):
  requirement = {
    "version": 1, "action_id": "platform-restart:deadline", "source_boot_id": "current-boot",
    "files": {"backend/app/example.py": {"state": "absent"}},
  }
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "current-boot")
  monkeypatch.setattr(restart_util, "restart_admission_in_progress", lambda: admitting)
  with SessionLocal() as db:
    wait = _proof_wait(db, requirement)
    db.add(models.PlatformRestartExecution(
      action_id=requirement["action_id"], chat_id=wait.chat_id, wait_id=wait.id,
      question_id="deadline-card", source_boot_id="current-boot", status="claimed",
      requirement_json=requirement, claimed_at=now_naive_utc() - timedelta(seconds=elapsed),
    ))
    db.commit()
    assert platform_restart.activation_wait_verdict(db, wait)[0] == expected
    execution = db.get(models.PlatformRestartExecution, requirement["action_id"])
    assert execution.status == ("uncertain" if expected == "failed" else "claimed")


def test_admission_cannot_resurrect_handoff_settled_during_source_check(monkeypatch):
  requirement = {
    "version": 1, "action_id": "platform-restart:race", "source_boot_id": "current-boot",
    "files": {"backend/app/example.py": {"state": "absent"}},
  }
  with SessionLocal() as db:
    wait = _proof_wait(db, requirement)
    db.add(models.PlatformRestartExecution(
      action_id=requirement["action_id"], chat_id=wait.chat_id, wait_id=wait.id,
      question_id="race-card", source_boot_id="current-boot", status="claimed",
      requirement_json=requirement, claimed_at=now_naive_utc(),
    ))
    db.commit()
  def check(_requirement):
    assert platform_restart.settle_undispatched_execution(requirement["action_id"])
    return True
  monkeypatch.setattr(platform_restart, "requirement_matches_current_source", check)
  assert not platform_restart.admit_execution_if_current(requirement["action_id"])
  with SessionLocal() as db:
    assert db.get(models.PlatformRestartExecution, requirement["action_id"]).status == "uncertain"
