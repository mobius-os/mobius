"""One-shot planned-restart cause authentication across process boots."""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import restart_ledger as platform_ledger


def _load_supervisor():
  path = (
    Path(__file__).resolve().parents[1]
    / "runtime"
    / "restart_ledger.py"
  )
  spec = importlib.util.spec_from_file_location("frozen_restart_ledger", path)
  assert spec and spec.loader
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


def _bind(supervisor, root: Path, monkeypatch) -> None:
  monkeypatch.setattr(supervisor, "DATA_DIR", root)
  monkeypatch.setattr(
    supervisor, "INTENT_PATH",
    root / ".restart-continuation-intent.json",
  )
  monkeypatch.setattr(
    supervisor, "REQUEST_PATH", root / ".platform-restart-requested",
  )
  monkeypatch.setattr(supervisor, "LEDGER_DIR", root / ".restart-ledger")
  monkeypatch.setattr(
    supervisor, "ACCEPTED_PATH",
    root / ".restart-ledger" / "accepted.json",
  )
  monkeypatch.setattr(
    supervisor, "ACK_PATH", root / ".restart-ledger" / "ack.json",
  )
  monkeypatch.setattr(
    supervisor, "BOOT_PATH", root / ".restart-ledger" / "boot-id",
  )
  monkeypatch.setattr(
    supervisor, "CUTOVER_CHALLENGE_PATH",
    root / ".restart-ledger" / "cutover-challenge.json",
  )
  monkeypatch.setattr(
    supervisor, "CUTOVER_RECEIPT_PATH",
    root / ".restart-ledger" / "cutover-receipt.json",
  )
  monkeypatch.setattr(
    supervisor, "MANAGED_CUTOVER_REQUEST_PATH",
    root / ".managed-cutover-request.json",
  )
  monkeypatch.setattr(supervisor, "SUPERVISOR_UID", os.getuid())
  monkeypatch.setattr(supervisor, "SUPERVISOR_GID", os.getgid())
  monkeypatch.setattr(
    platform_ledger,
    "get_settings",
    lambda: SimpleNamespace(data_dir=str(root)),
  )


def _request(*, boot: str, nonce: str, now: float) -> None:
  platform_ledger.request_restart(
    boot_id=boot,
    nonce=nonce,
    runs=[{"chat_id": "chat-12345678", "run_token": "run-12345678"}],
    now=now,
  )


def _authorized(root: Path, boot: str):
  del root
  return platform_ledger.authorized_restart_nonce(
    boot,
    trusted_uid=os.getuid(),
    trusted_gid=os.getgid(),
  )


def test_exact_accepted_restart_is_bound_to_immediately_following_boot(
  tmp_path, monkeypatch,
):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"
  target_boot = "boot-target-1234"
  nonce = "nonce-12345678"

  assert supervisor.begin_boot(source_boot, now=now) is False
  _request(boot=source_boot, nonce=nonce, now=now)
  assert supervisor.accept(source_boot, now=now + 1) is True
  assert supervisor.begin_boot(target_boot, now=now + 2) is True

  assert _authorized(tmp_path, target_boot) == nonce
  assert _authorized(tmp_path, source_boot) is None


def test_root_opened_cutover_binds_replacement_boot_without_self_restart(
  tmp_path, monkeypatch,
):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"
  target_boot = "boot-target-1234"
  cutover_id = "cutover-12345678"
  nonce = "nonce-12345678"

  supervisor.begin_boot(source_boot, now=now)
  assert supervisor.open_cutover(cutover_id, now=now + 1)
  assert platform_ledger.authorized_cutover_challenge(
    cutover_id,
    boot_id=source_boot,
    now=now + 1,
    trusted_uid=os.getuid(),
    trusted_gid=os.getgid(),
  )
  platform_ledger.publish_cutover_intent(
    boot_id=source_boot,
    nonce=nonce,
    cutover_id=cutover_id,
    runs=[{"chat_id": "chat-12345678", "run_token": "run-12345678"}],
    now=now + 1,
  )

  assert supervisor.accept_cutover(cutover_id, now=now + 2)
  assert not supervisor.REQUEST_PATH.exists()
  assert supervisor.begin_boot(target_boot, now=now + 3)
  assert _authorized(tmp_path, target_boot) == nonce


def test_managed_cutover_poller_opens_then_accepts_exact_handoff(
  tmp_path, monkeypatch,
):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"
  target_boot = "boot-target-1234"
  cutover_id = "replace_12345678"
  nonce = "nonce-12345678"

  supervisor.begin_boot(source_boot, now=now)
  platform_ledger.request_managed_cutover(
    boot_id=source_boot, cutover_id=cutover_id, now=now + 1,
  )
  assert supervisor.service_managed_cutover(source_boot, now=now + 1)
  assert platform_ledger.authorized_cutover_challenge(
    cutover_id, boot_id=source_boot, now=now + 1,
    trusted_uid=os.getuid(), trusted_gid=os.getgid(),
  )
  platform_ledger.publish_cutover_intent(
    boot_id=source_boot, nonce=nonce, cutover_id=cutover_id, runs=[], now=now + 2,
  )
  assert supervisor.service_managed_cutover(source_boot, now=now + 2)
  assert platform_ledger.accepted_cutover_receipt(
    cutover_id, boot_id=source_boot, now=now + 2,
    trusted_uid=os.getuid(), trusted_gid=os.getgid(),
  )
  assert supervisor.begin_boot(target_boot, now=now + 3)
  assert _authorized(tmp_path, target_boot) == nonce


def test_cutover_requires_matching_root_challenge(tmp_path, monkeypatch):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"
  supervisor.begin_boot(source_boot, now=now)
  platform_ledger.publish_cutover_intent(
    boot_id=source_boot,
    nonce="nonce-12345678",
    cutover_id="cutover-12345678",
    runs=[],
    now=now,
  )

  assert not supervisor.accept_cutover("cutover-12345678", now=now + 1)
  assert not supervisor.ACCEPTED_PATH.exists()


def test_failed_cutover_can_rearm_exact_rollback_then_finalize(
  tmp_path, monkeypatch,
):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"
  failed_boot = "boot-failed-1234"
  rollback_boot = "boot-rollback-1234"
  cutover_id = "cutover-12345678"
  nonce = "nonce-12345678"

  supervisor.begin_boot(source_boot, now=now)
  supervisor.open_cutover(cutover_id, now=now + 1)
  platform_ledger.publish_cutover_intent(
    boot_id=source_boot,
    nonce=nonce,
    cutover_id=cutover_id,
    runs=[],
    now=now + 1,
  )
  assert supervisor.accept_cutover(cutover_id, now=now + 2)
  assert supervisor.begin_boot(failed_boot, now=now + 3)
  assert supervisor.rearm_cutover(cutover_id, now=now + 4)
  assert supervisor.begin_boot(rollback_boot, now=now + 5)
  assert _authorized(tmp_path, rollback_boot) == nonce
  assert supervisor.finalize_cutover(cutover_id, now=now + 6)
  assert not supervisor.rearm_cutover(cutover_id, now=now + 7)


def test_restart_intent_keeps_exact_runs_for_older_frozen_supervisors(
  tmp_path, monkeypatch,
):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"

  assert supervisor.begin_boot(source_boot, now=now) is False
  _request(boot=source_boot, nonce="nonce-12345678", now=now)

  intent = json.loads(supervisor.INTENT_PATH.read_text(encoding="utf-8"))
  assert intent["runs"] == [{
    "chat_id": "chat-12345678",
    "run_token": "run-12345678",
  }]
  # The current nonce-only supervisor accepts the same payload, proving the
  # compatibility field does not alter the simplified authorization model.
  assert supervisor.accept(source_boot, now=now + 1) is True


def test_crash_after_intent_before_supervisor_acceptance_is_not_authorized(
  tmp_path, monkeypatch,
):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"

  supervisor.begin_boot(source_boot, now=now)
  _request(boot=source_boot, nonce="nonce-12345678", now=now)
  # Simulate OOM/SIGKILL before the frozen poller accepts the request.
  assert supervisor.begin_boot("boot-after-oom-1", now=now + 1) is False
  assert _authorized(tmp_path, "boot-after-oom-1") is None
  assert not supervisor.INTENT_PATH.exists()
  assert not supervisor.REQUEST_PATH.exists()


def test_second_boot_before_claim_retires_one_shot_ack(tmp_path, monkeypatch):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"

  supervisor.begin_boot(source_boot, now=now)
  _request(boot=source_boot, nonce="nonce-12345678", now=now)
  assert supervisor.accept(source_boot, now=now + 1)
  assert supervisor.begin_boot("boot-target-1234", now=now + 2)
  assert _authorized(tmp_path, "boot-target-1234")

  assert supervisor.begin_boot("boot-repeated-1234", now=now + 3) is False
  assert _authorized(tmp_path, "boot-target-1234") is None
  assert _authorized(tmp_path, "boot-repeated-1234") is None


def test_boot_does_not_ack_when_accepted_intent_cannot_be_consumed(
  tmp_path, monkeypatch,
):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"
  supervisor.begin_boot(source_boot, now=now)
  _request(boot=source_boot, nonce="nonce-12345678", now=now)
  assert supervisor.accept(source_boot, now=now + 1)
  real_remove = supervisor._remove

  def _refuse_accepted(path):
    if path == supervisor.ACCEPTED_PATH:
      return False
    return real_remove(path)

  monkeypatch.setattr(supervisor, "_remove", _refuse_accepted)
  with pytest.raises(OSError, match="consume"):
    supervisor.begin_boot("boot-target-1234", now=now + 2)
  assert not supervisor.ACK_PATH.exists()


def test_unpaired_restart_request_never_authorizes(
  tmp_path, monkeypatch,
):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"
  supervisor.begin_boot(source_boot, now=now)
  supervisor.REQUEST_PATH.write_text("", encoding="utf-8")

  assert supervisor.accept(source_boot, now=now + 1) is False
  assert supervisor.begin_boot("boot-recovery-1234", now=now + 2) is False
  assert _authorized(tmp_path, "boot-recovery-1234") is None


def test_supervisor_cli_rejects_unaccepted_request_and_failed_hardening(
  tmp_path, monkeypatch,
):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  boot = "boot-source-1234"

  assert supervisor._main(["restart_ledger.py", "begin-boot", boot]) == 0
  assert supervisor._main(["restart_ledger.py", "accept", boot]) == 1
  supervisor.BOOT_PATH.unlink()
  assert supervisor._main(["restart_ledger.py", "harden", boot]) == 1


def test_mismatched_or_expired_request_is_consumed_without_ack(
  tmp_path, monkeypatch,
):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"
  supervisor.begin_boot(source_boot, now=now)
  _request(
    boot=source_boot,
    nonce="nonce-12345678",
    now=now - supervisor.MAX_REQUEST_AGE_SECONDS - 1,
  )

  assert supervisor.accept(source_boot, now=now) is False
  assert not supervisor.ACCEPTED_PATH.exists()
  assert not supervisor.INTENT_PATH.exists()
  assert not supervisor.REQUEST_PATH.exists()


def test_platform_rejects_writable_or_tampered_ack(tmp_path, monkeypatch):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"
  target_boot = "boot-target-1234"
  supervisor.begin_boot(source_boot, now=now)
  _request(boot=source_boot, nonce="nonce-12345678", now=now)
  assert supervisor.accept(source_boot, now=now + 1)
  assert supervisor.begin_boot(target_boot, now=now + 2)

  supervisor.ACK_PATH.chmod(0o644)
  assert _authorized(tmp_path, target_boot) is None
  supervisor.ACK_PATH.chmod(0o444)
  value = json.loads(supervisor.ACK_PATH.read_text(encoding="utf-8"))
  value["nonce"] = "forged-nonce-1234"
  supervisor.ACK_PATH.chmod(0o644)
  supervisor.ACK_PATH.write_text(json.dumps(value), encoding="utf-8")
  supervisor.ACK_PATH.chmod(0o444)
  # This test process is the trusted uid, so structurally valid content written
  # by it is accepted. In production only the frozen supervisor owns this file.
  assert _authorized(tmp_path, target_boot) == "forged-nonce-1234"
