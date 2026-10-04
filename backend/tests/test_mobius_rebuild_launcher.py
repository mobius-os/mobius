"""The frozen host launcher and the worker's self-adoption from official images."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
WORKER_SCRIPT = ROOT / "scripts" / "mobius-rebuild-host.py"


def _load(name: str, path: Path):
  spec = importlib.util.spec_from_file_location(name, path)
  assert spec and spec.loader
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


launcher = _load("mobius_rebuild_launcher", ROOT / "scripts" / "mobius-rebuild-launcher.py")
host = _load("mobius_rebuild_host_for_launcher", WORKER_SCRIPT)
IMAGE_ID = "sha256:" + "b" * 64


def worker(revision: int | None, body: str = "print('worker')") -> bytes:
  line = f"WORKER_REVISION = {revision}\n" if revision is not None else ""
  return f"#!/usr/bin/env python3\n{line}{body}\n".encode()


@pytest.fixture
def state(tmp_path, monkeypatch):
  """Private worker state owned by the test user instead of root."""
  uid = os.geteuid()
  workers, index, status = tmp_path / "workers", tmp_path / "workers.json", tmp_path / "status.json"
  for module in (launcher, host):
    monkeypatch.setattr(module, "WORKERS", workers)
    monkeypatch.setattr(module, "STATE_DIR", tmp_path)
  monkeypatch.setattr(launcher, "INDEX", index)
  monkeypatch.setattr(launcher, "STATUS", status)
  monkeypatch.setattr(launcher, "LOCK", tmp_path / "replace.lock")
  monkeypatch.setattr(host, "WORKER_INDEX", index)
  monkeypatch.setenv("MOBIUS_REBUILD_LAUNCHER", "1")

  def private(path, *, directory):
    try:
      info = path.lstat()
    except OSError:
      return False
    kind = 0o040000 if directory else 0o100000
    return (info.st_mode & 0o170000 == kind and info.st_uid == uid
            and info.st_mode & 0o077 == 0)

  monkeypatch.setattr(launcher, "_root_private", private)
  return tmp_path


def _index(state) -> dict:
  return json.loads((state / "workers.json").read_text())


# --- The shipped worker's revision is tied to its bytes ----------------------

def test_the_shipped_worker_declares_its_revision():
  """CI requires a higher revision whenever the worker's bytes change."""
  assert host.worker_revision(WORKER_SCRIPT.read_bytes()) == host.WORKER_REVISION >= 1


def test_revision_is_read_as_text():
  assert host.worker_revision(worker(3)) == 3
  assert host.worker_revision(worker(None)) == 0
  assert host.worker_revision(b"WORKER_REVISION = 2\nWORKER_REVISION = 9\n") is None
  assert host.worker_revision(b"WORKER_REVISION = 2 + 7\n") is None
  assert host.worker_revision(b"WORKER_REVISION=4\n") is None


# --- Seeding and offers -----------------------------------------------------

def test_seed_installs_the_checkout_worker_and_never_downgrades(state):
  assert host.seed_worker(worker(1)) == "installed: revision 1"
  assert host.seed_worker(worker(1)) == "current: revision 1"
  assert host.offer_worker(worker(3), IMAGE_ID).startswith("offered: revision 3")
  # Reinstalling from an older checkout keeps the newer offered worker.
  assert host.seed_worker(worker(2)).startswith("kept")
  assert _index(state)["candidate"]["revision"] == 3
  assert host.seed_worker(worker(4, "def broken(:")).startswith("rejected")


def test_offers_only_strictly_newer_compiling_workers(state):
  host.seed_worker(worker(1))
  assert host.offer_worker(worker(1), IMAGE_ID).startswith("not adopted")
  assert host.offer_worker(worker(3), IMAGE_ID).startswith("offered")
  # Neither the same revision (other bytes included) nor an older official
  # image (possibly one a compromised app asked for) displaces it.
  for other in (worker(3), worker(3, "print(1)"), worker(2), worker(None)):
    assert host.offer_worker(other, IMAGE_ID).startswith("not adopted")
  assert host.offer_worker(worker(4, "def broken(:"), IMAGE_ID).startswith(
    "rejected: worker does not compile",
  )
  index = launcher.load_index()
  assert (index["active"]["revision"], index["candidate"]["revision"]) == (1, 3)
  assert index["candidate"]["path"].read_bytes() == worker(3)
  assert oct(index["candidate"]["path"].stat().st_mode & 0o777) == "0o700"


def test_a_fixed_helper_never_writes_a_launcher_record(state, monkeypatch):
  monkeypatch.delenv("MOBIUS_REBUILD_LAUNCHER")
  assert host.offer_worker(worker(3), IMAGE_ID).startswith("not adopted: this helper predates")
  assert not (state / "workers.json").exists()


def test_a_tampered_worker_or_index_is_never_run(state):
  host.seed_worker(worker(2))
  active = launcher.load_index()["active"]["path"]
  active.write_bytes(worker(2, "print('changed')"))
  active.chmod(0o700)
  assert launcher.load_index() is None
  active.write_bytes(worker(2))
  active.chmod(0o755)
  assert launcher.load_index() is None
  active.chmod(0o700)
  index = _index(state)
  index["active"]["file"] = "../elsewhere.py"
  (state / "workers.json").write_text(json.dumps(index))
  assert launcher.load_index() is None


# --- Launcher: candidates prove themselves ----------------------------------

def _run_launcher(monkeypatch, behaviour: dict, command: str = "run") -> tuple[int, list]:
  """``behaviour`` maps a worker revision to (exit code, status written or
  None, callback run inside the worker)."""
  calls = []

  def execute(entry, cmd):
    calls.append((entry["revision"], cmd))
    code, status, inside = behaviour[entry["revision"]]
    if inside:
      inside()
    if status is not None:
      launcher.STATUS.write_text(json.dumps({"by": entry["revision"], **status}))
    return code

  monkeypatch.setattr(launcher, "execute", execute)
  monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
  return launcher.main(["launcher", command]), calls


def _with_candidate(state):
  host.seed_worker(worker(1))
  host.offer_worker(worker(2), IMAGE_ID)


def test_a_candidate_that_succeeds_becomes_active(state, monkeypatch):
  _with_candidate(state)
  result, calls = _run_launcher(monkeypatch, {2: (0, {"state": "succeeded"}, None)})
  assert (result, calls) == (0, [(2, "run")])
  index = _index(state)
  assert index["active"]["revision"] == 2 and index["candidate"] is None


@pytest.mark.parametrize("status", [
  {"state": "failed", "code": "replacement_failed"},
  {"state": "rolled_back", "code": "health_check_failed"},
  {"state": "replacing"},  # crashed mid-operation
  None,  # crashed before writing anything
])
def test_any_other_candidate_outcome_drops_it_for_good(state, monkeypatch, status):
  _with_candidate(state)
  assert _run_launcher(monkeypatch, {2: (1, status, None)})[0] == 1
  index = _index(state)
  assert index["active"]["revision"] == 1 and index["candidate"] is None
  # The retry runs the proven worker, and revision 2 is never offered again.
  assert _run_launcher(monkeypatch, {1: (0, {"state": "succeeded"}, None)})[1] == [(1, "run")]
  assert host.offer_worker(worker(2), IMAGE_ID).startswith("not adopted")
  assert host.offer_worker(worker(3), IMAGE_ID).startswith("offered")


def test_a_trial_the_host_interrupts_is_never_repeated(state, monkeypatch):
  _with_candidate(state)

  def killed():
    raise KeyboardInterrupt  # systemd stopped the service mid-trial

  with pytest.raises(KeyboardInterrupt):
    _run_launcher(monkeypatch, {2: (1, None, killed)})
  index = _index(state)
  assert index["active"]["revision"] == 1 and index["candidate"] is None


def test_a_candidate_is_kept_when_nothing_was_queued(state, monkeypatch):
  _with_candidate(state)
  assert _run_launcher(monkeypatch, {2: (0, None, None)})[1] == [(2, "run")]
  assert _index(state)["candidate"]["revision"] == 2


def test_a_newer_offer_made_by_a_succeeding_candidate_survives(state, monkeypatch):
  _with_candidate(state)
  offer = lambda: host.offer_worker(worker(3), IMAGE_ID)  # noqa: E731
  _run_launcher(monkeypatch, {2: (0, {"state": "succeeded"}, offer)})
  index = _index(state)
  assert (index["active"]["revision"], index["candidate"]["revision"]) == (2, 3)
  loaded = launcher.load_index()
  assert loaded and loaded["active"]["path"].read_bytes() == worker(2)


def test_a_worker_installed_while_a_candidate_ran_is_not_superseded(state, monkeypatch):
  _with_candidate(state)
  reinstall = lambda: host.seed_worker(worker(4))  # noqa: E731
  _run_launcher(monkeypatch, {2: (0, {"state": "succeeded"}, reinstall)})
  index = _index(state)
  assert index["active"]["revision"] == 4 and index["candidate"] is None


def test_reconcile_always_runs_the_proven_worker(state, monkeypatch):
  _with_candidate(state)
  assert _run_launcher(monkeypatch, {1: (0, None, None)}, "reconcile")[1] == [(1, "reconcile")]
  assert _index(state)["candidate"]["revision"] == 2


def test_launcher_refuses_without_a_verified_worker(state, monkeypatch):
  monkeypatch.setattr(launcher.os, "geteuid", lambda: 0)
  assert launcher.main(["launcher", "run"]) == 1
  assert launcher.main(["launcher", "anything"]) == 2


def test_launcher_runs_workers_isolated(state, monkeypatch):
  host.seed_worker(worker(1))
  seen = {}

  def fake_run(args, **kwargs):
    seen.update(args=args, **kwargs)
    return subprocess.CompletedProcess(args, 0)

  monkeypatch.setattr(launcher.subprocess, "run", fake_run)
  monkeypatch.setenv("DOCKER_HOST", "tcp://attacker:2375")
  monkeypatch.setenv("PYTHONPATH", "/tmp/evil")
  assert launcher.execute(launcher.load_index()["active"], "run") == 0
  assert seen["args"][:3] == ["/usr/bin/python3", "-I", "-S"]
  assert seen["cwd"] == "/"
  assert set(seen["env"]) == {"PATH", "HOME", "LANG", "MOBIUS_REBUILD_LAUNCHER"}


# --- Extraction from the exact verified image -------------------------------

def _archive(name: str, data: bytes, *, kind=tarfile.REGTYPE, extra=False) -> bytes:
  buffer = io.BytesIO()
  with tarfile.open(fileobj=buffer, mode="w") as tar:
    info = tarfile.TarInfo(name)
    info.type = kind
    info.size = len(data) if kind == tarfile.REGTYPE else 0
    if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
      info.linkname = "/etc/shadow"
    tar.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    if extra:
      other = tarfile.TarInfo("other.py")
      other.size = 1
      tar.addfile(other, io.BytesIO(b"x"))
  return buffer.getvalue()


def _fake_docker(monkeypatch, archive: bytes) -> list:
  calls = []

  def run(args, **kwargs):
    calls.append(args)
    return subprocess.CompletedProcess(args, 0, "c" * 64 if args[1] == "create" else "", "")

  monkeypatch.setattr(host.subprocess, "run", run)
  monkeypatch.setattr(host, "_bounded_output", lambda args, limit: (
    calls.append(args) or (archive if len(archive) <= limit else
                           (_ for _ in ()).throw(RuntimeError("too large")))
  ))
  return calls


def test_extracts_the_worker_from_the_exact_image_without_starting_it(monkeypatch):
  calls = _fake_docker(monkeypatch, _archive("mobius-rebuild-host.py", worker(5)))
  assert host.worker_from_image(IMAGE_ID) == worker(5)
  create = next(call for call in calls if call[1] == "create")
  assert create[-1] == IMAGE_ID and "--network" in create
  assert ["docker", "rm", "-f", "-v", "c" * 64] in calls
  assert not any(call[1] in {"run", "start", "exec"} for call in calls)


@pytest.mark.parametrize("archive", [
  _archive("mobius-rebuild-host.py", b"", kind=tarfile.SYMTYPE),
  _archive("mobius-rebuild-host.py", b"", kind=tarfile.LNKTYPE),
  _archive("mobius-rebuild-host.py", worker(5), extra=True),
  _archive("../mobius-rebuild-host.py", worker(5)),
  _archive("mobius-rebuild-host.py", b"x" * (host.MAX_WORKER_BYTES + 1)),
  _archive("mobius-rebuild-host.py", worker(5))[:700],
  b"not a tar",
])
def test_refuses_anything_but_one_regular_worker_file(monkeypatch, archive):
  _fake_docker(monkeypatch, archive)
  with pytest.raises(RuntimeError):
    host.worker_from_image(IMAGE_ID)


def test_adoption_never_fails_a_finished_replacement(state, monkeypatch):
  def broken(_image_id):
    raise RuntimeError("docker cp failed")

  monkeypatch.setattr(host, "worker_from_image", broken)
  assert host.adopt_from_image(IMAGE_ID) == "not adopted: docker cp failed"


# --- Request reads ----------------------------------------------------------

def test_request_reads_are_bounded_and_never_follow_links(tmp_path):
  request = tmp_path / "request.json"
  target = tmp_path / "secret.json"
  target.write_text(json.dumps({"version": 1, "expected_sha": "a" * 40}))
  request.symlink_to(target)
  payload, identity = host.read_request(request)
  assert payload is None and identity is not None and identity[2] == b""
  request.unlink()
  os.mkfifo(request)
  assert host.read_request(request)[0] is None  # never blocks on a pipe
  request.unlink()
  request.write_bytes(b"{" + b" " * host.MAX_REQUEST_BYTES + b"}")
  assert host.read_request(request)[0] is None
  request.write_text(json.dumps({"version": 1, "expected_sha": "a" * 40}))
  assert host.read_request(request)[0] == {"version": 1, "expected_sha": "a" * 40}


def test_a_linked_request_is_claimed_and_refused_not_followed(tmp_path):
  request = tmp_path / "request.json"
  claimed = tmp_path / ".request-x.json"
  (tmp_path / "secret").write_text("{}")
  request.symlink_to(tmp_path / "secret")
  _payload, identity = host.read_request(request)
  assert host.claim_request(request, claimed, identity) is True
  assert claimed.is_symlink() and not request.exists()


# --- Interrupted replacements are settled from the journal -------------------

TXN = {
  "version": 1, "operation_id": "1" * 32, "expected_sha": "a" * 40,
  "request_nonce": "2" * 32, "target_image": IMAGE_ID,
  "previous_image": "sha256:" + "c" * 64,
}


@pytest.fixture
def journal(tmp_path, monkeypatch):
  monkeypatch.setattr(host, "TRANSACTION", tmp_path / "transaction.json")
  host.write_transaction(TXN)
  writes, rollbacks = [], []
  monkeypatch.setattr(host, "write_status", lambda _c, **fields: writes.append(fields))
  monkeypatch.setattr(host, "rollback", lambda *args: rollbacks.append(args) or 1)
  monkeypatch.setattr(host, "retain_images", lambda *a: None)
  monkeypatch.setattr(host, "restart_ledger", lambda *a, **k: True)
  monkeypatch.setattr(host, "verify_served_generation", lambda cid, sha: None)
  return writes, rollbacks


@pytest.mark.parametrize("healthy,running", [
  (True, TXN["previous_image"]),  # stopped before the new container started
  (True, IMAGE_ID),  # the new container was running when the worker stopped
  (False, IMAGE_ID),  # the new container never became healthy
])
def test_recovery_restores_the_previous_container(journal, monkeypatch, healthy, running):
  _writes, rollbacks = journal
  monkeypatch.setattr(host.subprocess, "run", lambda args, **_k: None)  # docker tag
  monkeypatch.setattr(host, "wait_healthy", lambda *a: healthy)
  monkeypatch.setattr(host, "app_container", lambda _c: ("cid", running))
  host.recover({}, host.read_transaction())
  assert rollbacks and rollbacks[0][3] == "worker_interrupted"
  assert rollbacks[0][5] == TXN["previous_image"]


def test_a_new_request_waits_for_an_interrupted_replacement(tmp_path, monkeypatch):
  control = tmp_path / "control"
  (control / "inbox").mkdir(parents=True)
  request = control / "inbox" / "request.json"
  request.write_text(json.dumps({"version": 1, "expected_sha": "a" * 40}))
  monkeypatch.setattr(host, "config", lambda: {"control_dir": control})
  monkeypatch.setattr(host, "LOCK", tmp_path / "replace.lock")
  monkeypatch.setattr(host, "TRANSACTION", tmp_path / "transaction.json")
  host.write_transaction(TXN)
  assert host.run() == 0
  assert request.exists()


def test_a_claimed_directory_is_left_not_deleted(tmp_path):
  claimed = tmp_path / ".request-x.json"
  claimed.mkdir()
  (claimed / "keep").write_text("x")
  host._discard_claim(claimed)
  assert (claimed / "keep").exists()


def test_extraction_is_bounded_while_reading():
  import time
  assert host._bounded_output(["python3", "-c", "print('ok')"], 10) == b"ok\n"
  started = time.monotonic()
  # An endless stream is cut off at the size bound, not after it ends.
  with pytest.raises(RuntimeError, match="too large"):
    host._bounded_output(
      ["python3", "-c", "import sys\nwhile True: sys.stdout.write('x' * 65536)"], 1024,
    )
  # A stalled reader is killed at the deadline.
  with pytest.raises(RuntimeError, match="timed out"):
    host._bounded_output(["python3", "-c", "import time; time.sleep(30)"], 1024, timeout=0.5)
  assert time.monotonic() - started < 10


def test_recovery_restores_the_journaled_previous_image(journal, monkeypatch):
  tagged = []
  monkeypatch.setattr(host.subprocess, "run", lambda args, **_k: tagged.append(args))
  monkeypatch.setattr(host, "wait_healthy", lambda *a: False)
  host.recover({}, host.read_transaction())
  assert ["docker", "tag", TXN["previous_image"], host.ROLLBACK_TAG] in tagged


def test_recovery_never_restores_an_unconfirmed_previous_image(journal, monkeypatch):
  writes, rollbacks = journal

  def failing_tag(args, **_kwargs):
    raise subprocess.CalledProcessError(1, args)

  monkeypatch.setattr(host.subprocess, "run", failing_tag)
  monkeypatch.setattr(host, "wait_healthy", lambda *a: False)
  host.recover({}, host.read_transaction())
  assert not rollbacks
  assert writes[-1]["state"] == "needs_recovery"
  assert host.read_transaction() is not None


def test_a_healthy_rollback_to_the_wrong_image_is_not_settled(tmp_path, monkeypatch):
  monkeypatch.setattr(host, "TRANSACTION", tmp_path / "transaction.json")
  host.write_transaction(TXN)
  writes = []
  monkeypatch.setattr(host, "write_status", lambda _c, **fields: writes.append(fields))
  monkeypatch.setattr(host, "restart_ledger", lambda *a, **k: True)
  monkeypatch.setattr(host, "compose", lambda *a, **k: None)
  # Keep the real floor comparison; isolate only its Docker probe seams.
  monkeypatch.setattr(host, "fence_app", lambda *a: True)
  monkeypatch.setattr(host, "rollback_image_level", lambda *a: 1)
  monkeypatch.setattr(host, "read_database_floor", lambda *a: 0)
  monkeypatch.setattr(host, "wait_ready", lambda *a: ("ready", None))
  monkeypatch.setattr(host, "app_container", lambda _c: ("cid", "sha256:" + "e" * 64))
  assert host.rollback({}, TXN["operation_id"], TXN["expected_sha"], "x", "y",
                       TXN["previous_image"]) == 1
  assert writes[-1]["state"] == "needs_recovery"
  assert writes[-1]["code"] == "rollback_wrong_image"
  assert host.read_transaction() is not None
  monkeypatch.setattr(host, "app_container", lambda _c: ("cid", TXN["previous_image"]))
  host.rollback({}, TXN["operation_id"], TXN["expected_sha"], "x", "y",
                TXN["previous_image"])
  assert writes[-1]["state"] == "rolled_back" and host.read_transaction() is None


def test_an_idle_trial_never_brings_back_a_superseded_candidate(state, monkeypatch):
  _with_candidate(state)
  reinstall = lambda: host.seed_worker(worker(4))  # noqa: E731
  _run_launcher(monkeypatch, {2: (0, None, reinstall)})
  index = _index(state)
  assert index["active"]["revision"] == 4 and index["candidate"] is None


def test_a_candidate_taken_by_someone_else_is_not_run(state, monkeypatch):
  _with_candidate(state)
  loaded = launcher.load_index()
  host.offer_worker(worker(3), IMAGE_ID)  # the record changes before the trial
  calls = []
  monkeypatch.setattr(launcher, "execute", lambda entry, cmd: calls.append(entry) or 0)
  assert launcher.try_candidate(loaded["candidate"], loaded["active"]["sha256"]) == 1
  assert not calls and _index(state)["candidate"]["revision"] == 3


def test_a_revision_one_worker_can_still_recover_this_journal(tmp_path, monkeypatch):
  """The launcher's fallback may be an older worker: the journal written
  here must stay readable by revision 1 (mobius-os/mobius 238360c3e7)."""
  old = subprocess.run(
    ["git", "-C", str(ROOT), "show", "238360c3e7:scripts/mobius-rebuild-host.py"],
    capture_output=True, text=True,
  )
  if old.returncode != 0:
    pytest.skip("revision 1 is not in this checkout's history")
  path = tmp_path / "revision1.py"
  path.write_text(old.stdout)
  revision1 = _load("mobius_rebuild_host_revision1", path)
  assert revision1.WORKER_REVISION == 1
  monkeypatch.setattr(host, "TRANSACTION", tmp_path / "transaction.json")
  monkeypatch.setattr(revision1, "TRANSACTION", tmp_path / "transaction.json")
  host.write_transaction(host.transaction_record(
    "1" * 32, "a" * 40, "2" * 32, "sha256:" + "c" * 64, IMAGE_ID,
  ))
  assert revision1.read_transaction() is not None
  assert host.read_transaction() is not None


@pytest.fixture
def root_owned_test_state(state, monkeypatch):
  """Only test-owned temporary paths simulate root uid; bytes/modes stay real."""
  from types import SimpleNamespace
  original = Path.lstat
  def owned(path):
    info = original(path)
    if path == state or state in path.parents:
      return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
    return info
  monkeypatch.setattr(Path, "lstat", owned)
  return state


def test_active_receipt_does_not_confuse_trial_writer_with_recovery_owner(root_owned_test_state):
  host.seed_worker(worker(2))
  host.offer_worker(worker(4, "ROLLBACK_FLOOR_LEVEL = 1"), IMAGE_ID)
  receipt = host.active_worker_receipt()
  assert receipt["revision"] == 2
  assert receipt["rollback_floor_level"] == 0
  assert receipt["sha256"] == _index(root_owned_test_state)["active"]["sha256"]


def test_installing_new_reviewed_revision_seeds_active_without_relaxing_high_water(root_owned_test_state):
  state = root_owned_test_state
  host.seed_worker(worker(2))
  host.offer_worker(worker(3), IMAGE_ID)
  assert host.seed_worker(worker(3)).startswith("kept")
  assert host.active_worker_receipt()["revision"] == 2
  safe = worker(4, "ROLLBACK_FLOOR_LEVEL = 1")
  assert host.seed_worker(safe) == "installed: revision 4"
  assert host.active_worker_receipt() == {
    "revision": 4, "sha256": hashlib.sha256(safe).hexdigest(), "rollback_floor_level": 1,
  }
  assert _index(state)["candidate"] is None


def test_active_receipt_refuses_modified_or_public_worker(root_owned_test_state):
  state = root_owned_test_state
  host.seed_worker(worker(4, "ROLLBACK_FLOOR_LEVEL = 1"))
  path = host.WORKERS / _index(state)["active"]["file"]
  path.chmod(0o755)
  assert host.active_worker_receipt() is None
  path.chmod(0o700)
  path.write_bytes(worker(4, "ROLLBACK_FLOOR_LEVEL = 9"))
  assert host.active_worker_receipt() is None


def test_active_receipt_refuses_symlink_worker(root_owned_test_state):
  state = root_owned_test_state
  host.seed_worker(worker(4, "ROLLBACK_FLOOR_LEVEL = 1"))
  path = host.WORKERS / _index(state)["active"]["file"]
  target = state / "elsewhere.py"
  target.write_bytes(path.read_bytes())
  target.chmod(0o700)
  path.unlink()
  path.symlink_to(target)
  assert host.active_worker_receipt() is None


def test_helper_install_prerequisite_is_explicit_and_verifies_active_before_services():
  marker = (ROOT / "deployment/self-hosted-helper.required").read_text().strip()
  installer = (ROOT / "scripts/install-rebuild-helper.sh").read_text()
  assert marker == "2"
  assert "Helper protocol revision: 2" in installer
  assert installer.index("adopt-self") < installer.index("verify-active")
  assert installer.index("verify-active") < installer.index("ExecStart=")


def test_mounted_active_proof_rechecks_selection_and_actual_worker_bytes(root_owned_test_state):
  state = root_owned_test_state
  host.seed_worker(worker(4, "ROLLBACK_FLOOR_LEVEL = 1"))
  proof = host.active_worker_receipt(state)
  assert proof == host.active_worker_receipt()
  active = host.WORKERS / _index(state)["active"]["file"]
  active.unlink()
  assert host.active_worker_receipt(state) is None
  assert proof["rollback_floor_level"] == 1  # Old proof cannot validate new boot.
  host.WORKER_INDEX.unlink()
  assert host.active_worker_receipt(state) is None


def test_installer_pins_host_intent_and_readonly_state_outside_writable_data():
  installer = (ROOT / "scripts/install-rebuild-helper.sh").read_text()
  assert 'MOBIUS_HOST_RECOVERY_REQUIRED: "1"' in installer
  assert "source: /var/lib/mobius-rebuild" in installer
  assert "target: /run/mobius-rebuild-host" in installer
  assert "read_only: true" in installer
  assert "create_host_path: false" in installer
  entrypoint = (ROOT / "backend/scripts/entrypoint.sh").read_text()
  assert entrypoint.index("export MOBIUS_BOOT_ID") < entrypoint.index("verify-mounted-active")
  assert entrypoint.index("verify-mounted-active") < entrypoint.index("PHASE 1:")
  assert "rm -f /run/mobius-rebuild-active.json" in entrypoint
  assert "chmod 0644 /run/mobius-rebuild-active.json" in entrypoint


@pytest.mark.parametrize("version", [2, True, "1", None])
def test_boot_proof_and_frozen_launcher_reject_unknown_worker_index_version(root_owned_test_state, version):
  state = root_owned_test_state
  host.seed_worker(worker(4, "ROLLBACK_FLOOR_LEVEL = 1"))
  index = _index(state)
  index["version"] = version
  host._atomic_json(host.WORKER_INDEX, index)
  assert host.active_worker_receipt(state) is None


# --- A refused rollback settles forward --------------------------------------


@pytest.fixture
def refused(tmp_path, monkeypatch):
  """The database floor refuses the previous image; Docker seams recorded."""
  monkeypatch.setattr(host, "TRANSACTION", tmp_path / "transaction.json")
  host.write_transaction(TXN)
  calls = {"status": [], "ledger": [], "compose": [], "docker": []}
  monkeypatch.setattr(host, "write_status", lambda _c, **f: calls["status"].append(f))
  monkeypatch.setattr(host, "restart_ledger",
                      lambda _c, cid, cmd, op, image=None: calls["ledger"].append((cmd, image)) or True)
  monkeypatch.setattr(host, "compose", lambda *a, **k: calls["compose"].append(a))
  monkeypatch.setattr(host.subprocess, "run", lambda args, **k: calls["docker"].append(args))
  monkeypatch.setattr(host, "fence_app", lambda *a: True)
  monkeypatch.setattr(host, "rollback_image_level", lambda *a: 0)
  monkeypatch.setattr(host, "read_database_floor", lambda *a: 1)
  monkeypatch.setattr(host, "verify_served_generation", lambda cid, sha: None)
  monkeypatch.setattr(host, "adopt_from_image", lambda image: "offered")
  monkeypatch.setattr(host, "inspect_container_image", lambda cid: TXN["target_image"])
  return calls


def _rollback():
  return host.rollback({}, TXN["operation_id"], TXN["expected_sha"],
                       "health_check_failed", "slow", TXN["previous_image"])


@pytest.mark.parametrize("running_before_fence", [True, False])
def test_refused_rollback_finishes_on_the_new_release_when_it_serves(
    refused, monkeypatch, running_before_fence):
  """Also when the new container had already stopped before the fence."""
  monkeypatch.setattr(host, "wait_ready", lambda *a: ("ready", {"ready": True}))
  served = iter([("new-cid", TXN["target_image"])] if running_before_fence else [])
  monkeypatch.setattr(host, "app_container", lambda _c: next(
    served, ("new-cid", TXN["target_image"])) if running_before_fence or refused["docker"]
    else (_ for _ in ()).throw(RuntimeError("no running app")))
  monkeypatch.setattr(host, "app_container_any", lambda _c: "new-cid")
  assert _rollback() == 0
  assert ["docker", "start", "new-cid"] in refused["docker"]
  assert refused["status"][-1]["state"] == "succeeded"
  assert ("rearm-cutover", TXN["target_image"]) in refused["ledger"]
  assert ("finalize-cutover", TXN["target_image"]) in refused["ledger"]
  assert not any("up" in args for args in refused["compose"])
  assert host.read_transaction() is None


@pytest.mark.parametrize("ready,image", [
  ("timeout", TXN["target_image"]),
  ("ready", "sha256:" + "e" * 64),
])
def test_refused_rollback_never_leaves_a_journal_that_refences_the_app(
    refused, monkeypatch, ready, image):
  """No earlier image may ever run again: the journal has nothing left to
  restore, and keeping it would fence the serving app on every later run."""
  monkeypatch.setattr(host, "wait_ready", lambda *a: (ready, None))
  monkeypatch.setattr(host, "app_container", lambda _c: ("new-cid", image))
  assert _rollback() == 1
  assert refused["status"][-1]["state"] == "needs_recovery"
  assert refused["status"][-1]["code"] == "new_version_not_ready"
  assert not any("up" in args for args in refused["compose"])
  assert host.read_transaction() is None


def test_a_refused_rollback_never_starts_a_container_that_is_not_the_new_release(
    refused, monkeypatch):
  monkeypatch.setattr(host, "app_container", lambda _c: ("cid", TXN["previous_image"]))
  monkeypatch.setattr(host, "inspect_container_image", lambda cid: TXN["previous_image"])
  monkeypatch.setattr(host, "wait_ready", lambda *a: (_ for _ in ()).throw(
    AssertionError("nothing may be started")))
  assert _rollback() == 1
  assert not any(args[:2] == ["docker", "start"] for args in refused["docker"])
  assert refused["status"][-1]["state"] == "needs_recovery"
  assert host.read_transaction() is None


def test_installed_service_allows_recovery_to_finish_after_an_interruption():
  installer = (ROOT / "scripts/install-rebuild-helper.sh").read_text()
  unit = installer[installer.index("mobius-rebuild.service <<'EOF'"):]
  unit = unit[:unit.index("EOF\n")]
  assert "ExecStopPost=/usr/local/libexec/mobius-rebuild-host reconcile" in unit
  assert "TimeoutStopSec=15min" in unit
