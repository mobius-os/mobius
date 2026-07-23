"""One-shot planned-restart cause authentication across process boots."""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

from app import restart_ledger as platform_ledger


def _load_supervisor():
  path = (
    Path(__file__).resolve().parents[1]
    / "recovery"
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
  monkeypatch.setattr(supervisor, "SUPERVISOR_UID", os.getuid())
  monkeypatch.setattr(supervisor, "SUPERVISOR_GID", os.getgid())
  monkeypatch.setattr(
    platform_ledger,
    "get_settings",
    lambda: SimpleNamespace(data_dir=str(root)),
  )


def _request(root: Path, *, boot: str, nonce: str, now: float) -> None:
  platform_ledger.request_restart(
    boot_id=boot,
    nonce=nonce,
    runs=[{"chat_id": "chat-12345678", "run_token": "run-12345678"}],
    now=now,
  )


def _authorized(root: Path, boot: str):
  del root
  return platform_ledger.authorized_runs(
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
  _request(tmp_path, boot=source_boot, nonce=nonce, now=now)
  assert supervisor.accept(source_boot, now=now + 1) is True
  assert supervisor.begin_boot(target_boot, now=now + 2) is True

  assert _authorized(tmp_path, target_boot) == {
    "run-12345678": ("chat-12345678", nonce),
  }
  assert _authorized(tmp_path, source_boot) == {}


def test_crash_after_intent_before_supervisor_acceptance_is_not_authorized(
  tmp_path, monkeypatch,
):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"

  supervisor.begin_boot(source_boot, now=now)
  _request(tmp_path, boot=source_boot, nonce="nonce-12345678", now=now)
  # Simulate OOM/SIGKILL before the frozen poller accepts the request.
  assert supervisor.begin_boot("boot-after-oom-1", now=now + 1) is False
  assert _authorized(tmp_path, "boot-after-oom-1") == {}
  assert not supervisor.INTENT_PATH.exists()
  assert not supervisor.REQUEST_PATH.exists()


def test_second_boot_before_claim_retires_one_shot_ack(tmp_path, monkeypatch):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"

  supervisor.begin_boot(source_boot, now=now)
  _request(tmp_path, boot=source_boot, nonce="nonce-12345678", now=now)
  assert supervisor.accept(source_boot, now=now + 1)
  assert supervisor.begin_boot("boot-target-1234", now=now + 2)
  assert _authorized(tmp_path, "boot-target-1234")

  assert supervisor.begin_boot("boot-repeated-1234", now=now + 3) is False
  assert _authorized(tmp_path, "boot-target-1234") == {}
  assert _authorized(tmp_path, "boot-repeated-1234") == {}


def test_recovery_restart_without_chat_intent_never_authorizes(
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
  assert _authorized(tmp_path, "boot-recovery-1234") == {}


def test_mismatched_or_expired_request_is_consumed_without_ack(
  tmp_path, monkeypatch,
):
  supervisor = _load_supervisor()
  _bind(supervisor, tmp_path, monkeypatch)
  now = time.time()
  source_boot = "boot-source-1234"
  supervisor.begin_boot(source_boot, now=now)
  _request(
    tmp_path,
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
  _request(tmp_path, boot=source_boot, nonce="nonce-12345678", now=now)
  assert supervisor.accept(source_boot, now=now + 1)
  assert supervisor.begin_boot(target_boot, now=now + 2)

  supervisor.ACK_PATH.chmod(0o644)
  assert _authorized(tmp_path, target_boot) == {}
  supervisor.ACK_PATH.chmod(0o444)
  value = json.loads(supervisor.ACK_PATH.read_text(encoding="utf-8"))
  value["runs"][0]["run_token"] = "forged-run-1234"
  supervisor.ACK_PATH.chmod(0o644)
  supervisor.ACK_PATH.write_text(json.dumps(value), encoding="utf-8")
  supervisor.ACK_PATH.chmod(0o444)
  # Filesystem ownership proves who wrote the ledger in production. This test
  # process is the trusted uid, so content tampering is structurally valid; the
  # exact DB run/nonce comparison remains the second authorization boundary.
  assert _authorized(tmp_path, target_boot) == {
    "forged-run-1234": ("chat-12345678", "nonce-12345678"),
  }
