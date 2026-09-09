"""Byte-exact source proof and one-shot admission for Restart cards.

These tests never invoke a real process signal or supervisor handshake.  They
exercise the immutable Git/boot evidence used by a card and the shared
in-process admission latch used by card, Settings, and platform restart calls.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import subprocess
from types import SimpleNamespace

import pytest

from app import chat as chat_mod
from app import chat_writer
from app import models
from app import platform_update
from app import restart_ledger
import app.platform_restart as platform_restart
import app.restart_util as restart_util
from app.database import SessionLocal
from app.timeutil import now_naive_utc


def _git(repo, *args: str) -> str:
  result = subprocess.run(
    ["git", "-C", str(repo), *args],
    capture_output=True,
    text=True,
    check=True,
  )
  return result.stdout.strip()


def _commit(repo, message: str) -> str:
  _git(repo, "add", "-A")
  _git(repo, "commit", "-m", message)
  return _git(repo, "rev-parse", "HEAD")


def _restart_repo(monkeypatch, tmp_path, *, path="backend/app/example.py"):
  repo = tmp_path / "platform"
  repo.mkdir()
  _git(repo, "init")
  _git(repo, "config", "user.email", "tests@example.invalid")
  _git(repo, "config", "user.name", "Restart Tests")
  source = repo / path
  source.parent.mkdir(parents=True, exist_ok=True)
  source.write_text("VALUE = 'served'\n", encoding="utf-8")
  base = _commit(repo, "served source")

  serving_kind = tmp_path / "serving-source"
  serving_sha = tmp_path / "serving-sha"
  serving_kind.write_text("platform\n", encoding="utf-8")
  serving_sha.write_text(f"{base}\n", encoding="utf-8")
  monkeypatch.setattr(platform_update, "PLATFORM_REPO", repo)
  monkeypatch.setattr(platform_update, "SERVING_SOURCE_FILE", serving_kind)
  monkeypatch.setattr(platform_update, "SERVING_SHA_FILE", serving_sha)
  monkeypatch.setattr(platform_update, "_read_activation_marker", lambda: None)
  monkeypatch.setattr(platform_update, "_protected_runtime_status", lambda _repo: None)
  monkeypatch.setattr(
    platform_update.runtime_provenance, "activation_paths", lambda _status: [],
  )
  monkeypatch.setattr(platform_update, "image_input_drift", lambda _repo: [])
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-served")
  return repo, source, base


def test_requirement_rejects_modified_source_after_card_is_derived(
  monkeypatch, tmp_path,
):
  repo, source, _base = _restart_repo(monkeypatch, tmp_path)
  source.write_text("VALUE = 'approved'\n", encoding="utf-8")
  approved_sha = _commit(repo, "approved server change")

  requirement = platform_restart.build_restart_requirement(repo)

  assert requirement["target_sha"] == approved_sha
  assert list(requirement["files"]) == ["backend/app/example.py"]
  assert requirement["files"]["backend/app/example.py"]["state"] == "file"
  assert platform_restart.requirement_matches_current_source(requirement) is True

  # A saved action cannot authorize different working-tree bytes, even before
  # those bytes receive a new commit identity.
  source.write_text("VALUE = 'unreviewed working tree'\n", encoding="utf-8")
  with pytest.raises(
    platform_restart.RestartRequirementError,
    match="restart_source_must_be_committed",
  ):
    platform_restart.build_restart_requirement(repo)
  assert platform_restart.requirement_matches_current_source(requirement) is False


def test_descendant_that_reverts_approved_bytes_does_not_prove_activation(
  monkeypatch, tmp_path,
):
  repo, source, _base = _restart_repo(monkeypatch, tmp_path)
  source.write_text("VALUE = 'approved'\n", encoding="utf-8")
  approved_sha = _commit(repo, "approved server change")
  requirement = platform_restart.build_restart_requirement(repo)

  source.write_text("VALUE = 'served'\n", encoding="utf-8")
  reverted_sha = _commit(repo, "revert approved server change")
  assert subprocess.run(
    ["git", "-C", str(repo), "merge-base", "--is-ancestor", approved_sha, reverted_sha],
    check=False,
  ).returncode == 0

  reverted_snapshot = SimpleNamespace(
    boot_id="boot-reverted",
    source_kind="platform",
    service_ready=True,
    loaded_files_json=platform_restart.committed_manifest(
      repo, reverted_sha, ["backend/app/example.py"],
    ),
  )
  # Git ancestry is deliberately irrelevant: the expected committed bytes
  # were removed by the descendant.
  assert platform_restart.requirement_matches_snapshot(
    requirement, reverted_snapshot,
  ) is False
  assert platform_restart.requirement_matches_current_source(requirement) is False


def test_deleted_path_uses_explicit_absence_proof(monkeypatch, tmp_path):
  repo, source, base = _restart_repo(
    monkeypatch, tmp_path, path="backend/app/deleted_module.py",
  )
  source.unlink()
  deleted_sha = _commit(repo, "delete server module")

  requirement = platform_restart.build_restart_requirement(repo)
  expected = {"backend/app/deleted_module.py": {"state": "absent"}}
  assert requirement["files"] == expected

  still_present = SimpleNamespace(
    boot_id="boot-still-present",
    source_kind="platform",
    service_ready=True,
    loaded_files_json=platform_restart.committed_manifest(
      repo, base, ["backend/app/deleted_module.py"],
    ),
  )
  loaded_deletion = SimpleNamespace(
    boot_id="boot-loaded-deletion",
    source_kind="platform",
    service_ready=True,
    loaded_files_json=platform_restart.committed_manifest(
      repo, deleted_sha, ["backend/app/deleted_module.py"],
    ),
  )
  assert platform_restart.requirement_matches_snapshot(
    requirement, still_present,
  ) is False
  assert platform_restart.requirement_matches_snapshot(
    requirement, loaded_deletion,
  ) is True


def test_baked_and_not_ready_boots_never_satisfy_requirement():
  requirement = {
    "version": platform_restart.REQUIREMENT_VERSION,
    "action_id": "platform-restart:pending-proof",
    "source_boot_id": "boot-served",
    "files": {"backend/app/example.py": {"state": "absent"}},
  }
  loaded = {"backend/app/example.py": {"state": "absent"}}

  for snapshot in (
    SimpleNamespace(
      boot_id="boot-baked",
      source_kind="baked",
      service_ready=True,
      loaded_files_json=loaded,
    ),
    SimpleNamespace(
      boot_id="boot-not-ready",
      source_kind="platform",
      service_ready=False,
      loaded_files_json=loaded,
    ),
    SimpleNamespace(
      boot_id="boot-served",
      source_kind="platform",
      service_ready=True,
      loaded_files_json=loaded,
    ),
  ):
    assert platform_restart.requirement_matches_snapshot(
      requirement, snapshot,
    ) is False


def test_wait_verdict_leaves_unrelated_not_ready_and_baked_boots_pending():
  created_at = now_naive_utc()
  requirement = {
    "version": platform_restart.REQUIREMENT_VERSION,
    "action_id": "platform-restart:pending-wait",
    "source_boot_id": "boot-served",
    "files": {"backend/app/example.py": {"state": "absent"}},
  }
  row = SimpleNamespace(condition_json=requirement, created_at=created_at)

  with SessionLocal() as db:
    db.add(models.PlatformBootSnapshot(
      boot_id="boot-not-ready",
      source_kind="platform",
      loaded_files_json=requirement["files"],
      service_ready=False,
      captured_at=created_at + timedelta(seconds=1),
    ))
    db.commit()
    assert platform_restart.activation_wait_verdict(db, row) == ("pending", "")

    db.add(models.PlatformBootSnapshot(
      boot_id="boot-baked",
      source_kind="baked",
      loaded_files_json=requirement["files"],
      service_ready=True,
      captured_at=created_at + timedelta(seconds=2),
    ))
    db.commit()
    assert platform_restart.activation_wait_verdict(db, row) == ("pending", "")


def test_ready_boot_capture_hashes_wait_paths_and_requires_writer_readiness(
  monkeypatch, tmp_path,
):
  repo, source, _base = _restart_repo(monkeypatch, tmp_path)
  source.write_text("VALUE = 'approved'\n", encoding="utf-8")
  target_sha = _commit(repo, "approved server change")
  requirement = platform_restart.build_restart_requirement(repo)
  platform_update.SERVING_SHA_FILE.write_text(f"{target_sha}\n", encoding="utf-8")
  # A new boot captures committed inputs before imports; changing a sentinel
  # alone is deliberately no longer evidence about the running source.
  from app import boot_source
  monkeypatch.setattr(boot_source, "BOOT_SOURCE_INPUTS", boot_source.capture_boot_source_inputs(
    repo, source_kind="platform", source_sha=target_sha,
  ))

  now = now_naive_utc()
  with SessionLocal() as db:
    db.add(models.Chat(id="chat-proof", title="Proof", messages=[]))
    db.add(models.ChatWait(
      id="wait-proof",
      chat_id="chat-proof",
      description="Load approved source",
      condition_owner="Möbius platform",
      kind=platform_restart.ACTIVATION_WAIT_KIND,
      condition_json=requirement,
      interval_secs=30,
      deadline_at=now + timedelta(days=1),
      next_check_at=now,
      created_at=now,
    ))
    db.commit()

    monkeypatch.setattr(chat_writer, "writer_readiness", lambda: (False, "starting"))
    not_ready = platform_restart.capture_ready_boot_snapshot(
      db, boot_id="boot-starting", repo=repo,
    )
    assert not_ready.loaded_files_json == requirement["files"]
    assert not_ready.service_ready is False
    assert platform_restart.requirement_matches_snapshot(
      requirement, not_ready,
    ) is False

    monkeypatch.setattr(chat_writer, "writer_readiness", lambda: (True, None))
    ready = platform_restart.capture_ready_boot_snapshot(
      db, boot_id="boot-ready", repo=repo,
    )
    assert ready.service_ready is True
    assert ready.source_kind == "platform"
    assert ready.source_sha == target_sha
    assert ready.loaded_files_json == requirement["files"]
    assert platform_restart.requirement_matches_snapshot(requirement, ready) is True


def test_later_boot_reconciles_claim_without_replaying_side_effect(
  monkeypatch, tmp_path,
):
  repo, source, _base = _restart_repo(monkeypatch, tmp_path)
  source.write_text("VALUE = 'approved'\n", encoding="utf-8")
  target_sha = _commit(repo, "approved server change")
  requirement = platform_restart.build_restart_requirement(repo)
  platform_update.SERVING_SHA_FILE.write_text(f"{target_sha}\n", encoding="utf-8")
  # A new boot captures committed inputs before imports; changing a sentinel
  # alone is deliberately no longer evidence about the running source.
  from app import boot_source
  monkeypatch.setattr(boot_source, "BOOT_SOURCE_INPUTS", boot_source.capture_boot_source_inputs(
    repo, source_kind="platform", source_sha=target_sha,
  ))
  monkeypatch.setattr(chat_writer, "writer_readiness", lambda: (True, None))
  now = now_naive_utc()
  with SessionLocal() as db:
    db.add(models.Chat(id="chat-claim", title="Claim", messages=[]))
    db.add(models.ChatWait(
      id="wait-claim", chat_id="chat-claim", description="Load source",
      condition_owner="Möbius startup", kind=platform_restart.ACTIVATION_WAIT_KIND,
      condition_json=requirement, interval_secs=60,
      deadline_at=now + timedelta(days=1), next_check_at=now,
    ))
    db.add(models.PlatformRestartExecution(
      action_id=requirement["action_id"], question_id="question-claim",
      chat_id="chat-claim", wait_id="wait-claim",
      source_boot_id="boot-served", requirement_json=requirement,
      status="claimed", claimed_at=now,
    ))
    db.commit()

    snapshot = platform_restart.capture_ready_boot_snapshot(
      db, boot_id="boot-after-ambiguous-dispatch", repo=repo,
    )
    execution = db.get(models.PlatformRestartExecution, requirement["action_id"])
    assert snapshot.service_ready is True
    assert execution.status == "activated"
    assert execution.activated_boot_id == snapshot.boot_id
    # Re-reading the immutable boot proof is reconciliation only; there is no
    # callable restart seam here to replay.
    assert platform_restart.capture_ready_boot_snapshot(
      db, boot_id=snapshot.boot_id, repo=repo,
    ).boot_id == snapshot.boot_id


class _FakeTimer:
  instances = []

  def __init__(self, interval, callback):
    self.interval = interval
    self.callback = callback
    self.daemon = False
    self.started = False
    self.__class__.instances.append(self)

  def start(self):
    self.started = True


def test_concurrent_restart_callers_share_one_dispatch(monkeypatch):
  """Card/Settings/platform callers converge on one nonce, drain, and timer."""
  _FakeTimer.instances = []
  restart_util._RESTART_ADMITTED = False
  chat_mod.draining = False
  prepared = 0
  drained = 0
  requests = []

  monkeypatch.setattr(restart_util.threading, "Timer", _FakeTimer)
  monkeypatch.setattr(restart_util.os, "kill", lambda *_args: None)
  monkeypatch.setattr(restart_ledger, "current_boot_id", lambda: "boot-current")
  monkeypatch.setattr(restart_ledger, "new_nonce", lambda: "nonce-single")
  monkeypatch.setattr(
    restart_ledger, "request_restart", lambda **kwargs: requests.append(kwargs),
  )

  async def _prepare(nonce):
    nonlocal prepared
    assert nonce == "nonce-single"
    prepared += 1
    # Yield with the drain gate already raised so all duplicate callers race
    # the actual admission latch rather than merely running sequentially.
    await asyncio.sleep(0.02)
    return []

  async def _drain(timeout=0, *, restart_nonce="", prepared_runs=None):
    nonlocal drained
    del timeout, prepared_runs
    assert restart_nonce == "nonce-single"
    drained += 1
    return []

  monkeypatch.setattr(chat_mod, "prepare_restart_intents", _prepare)
  monkeypatch.setattr(chat_mod, "drain_all_for_restart", _drain)

  async def _race():
    await asyncio.gather(*(
      restart_util.restart_this_worker() for _ in range(8)
    ))

  try:
    asyncio.run(_race())
  finally:
    chat_mod.draining = False
    restart_util._RESTART_ADMITTED = False

  assert prepared == 1
  assert drained == 1
  assert len(_FakeTimer.instances) == 1
  assert requests == [{
    "boot_id": "boot-current",
    "nonce": "nonce-single",
    "runs": [],
  }]
