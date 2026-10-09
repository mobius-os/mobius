"""Disposable filesystem/process tests for the host's legacy boot wrapper.

Runs without application fixtures: pytest --noconftest this_file.py.
"""
import importlib.util
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATE_PATH = ROOT / "scripts/mobius-boot-admission.py"
LEGACY_PATH = ROOT / "backend/runtime/restart_ledger.py"
SOURCE = "1" * 64
TARGET = "2" * 64
ROLLBACK = "3" * 64
IMAGE = "sha256:" + "a" * 64
NEW_IMAGE = "sha256:" + "b" * 64
OP = "operation-12345678"
SOURCE_BOOT = "source-boot-12345678"
TOKEN = "rollback-token-12345678"
NOW = 1001.0


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def legacy_at(root):
    legacy = load(LEGACY_PATH, "admission_test_legacy")
    legacy.DATA_DIR = root
    legacy.LEDGER_DIR = root / ".restart-ledger"
    for name, filename in {
        "INTENT_PATH": ".restart-continuation-intent.json",
        "REQUEST_PATH": ".platform-restart-requested",
        "MANAGED_CUTOVER_REQUEST_PATH": ".managed-cutover-request.json",
    }.items():
        setattr(legacy, name, root / filename)
    for name, filename in {
        "ACCEPTED_PATH": "accepted.json", "ACK_PATH": "ack.json", "BOOT_PATH": "boot-id",
        "CUTOVER_CHALLENGE_PATH": "cutover-challenge.json", "CUTOVER_RECEIPT_PATH": "cutover-receipt.json",
    }.items():
        setattr(legacy, name, legacy.LEDGER_DIR / filename)
    legacy.SUPERVISOR_UID, legacy.SUPERVISOR_GID = os.getuid(), os.getgid()
    return legacy


@pytest.fixture
def setup(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    legacy = legacy_at(data)
    legacy.begin_boot(SOURCE_BOOT, now=1000)
    assert legacy.open_cutover(OP, now=1000)
    legacy.INTENT_PATH.write_text(json.dumps({
        "version": 1, "action": "external_cutover", "cutover_id": OP,
        "nonce": "nonce-original-12345678", "source_boot_id": SOURCE_BOOT, "created_at": 1000,
    }))
    assert legacy.accept_cutover(OP, now=NOW)
    receipt = json.loads(legacy.CUTOVER_RECEIPT_PATH.read_text())
    module = load(GATE_PATH, "admission_test_gate")
    store = module.AdmissionStore(tmp_path / "gate", data, os.getuid(), os.getgid())
    store.prepare(OP, receipt, SOURCE, IMAGE, SOURCE_BOOT)
    return module, store, legacy, receipt


def rollback_open(store):
    store.quiesce(SOURCE)
    store.allocate("rollback", TOKEN, IMAGE)
    store.bind("rollback", TOKEN, ROLLBACK)
    store.open("rollback", TOKEN)
    assert store.issue_start("rollback", TOKEN)


def test_first_claim_preserves_exact_legacy_handoff(setup):
    _, store, legacy, receipt = setup
    rollback_open(store)
    assert store.enter(TOKEN, "rollback-boot-12345678", now=NOW) == "continuation"
    assert legacy.begin_boot("rollback-boot-12345678", now=NOW)
    ack = json.loads(legacy.ACK_PATH.read_text())
    assert ack["nonce"] == receipt["nonce"]
    assert ack["source_boot_id"] == SOURCE_BOOT
    assert not legacy.ACCEPTED_PATH.exists()
    assert not store.close_pending("rollback", TOKEN)
    assert store.observe()["slots"]["rollback"]["consumed"] == {
        "generation": 1, "boot_id": "rollback-boot-12345678",
    }


@pytest.mark.parametrize("age,eligible", [(601, True), (3590, True), (3600, True), (3601, False)])
def test_original_receipt_age_not_short_acceptance_controls_first_claim(setup, age, eligible):
    _, store, legacy, receipt = setup
    rollback_open(store)
    receipt_bytes = legacy.CUTOVER_RECEIPT_PATH.read_bytes()
    result = store.enter(TOKEN, "rollback-boot-12345678", now=NOW + age)
    assert result == ("continuation" if eligible else "service_only")
    assert legacy.begin_boot("rollback-boot-12345678", now=NOW + age) is eligible
    assert legacy.ACK_PATH.exists() is eligible
    assert legacy.CUTOVER_RECEIPT_PATH.read_bytes() == receipt_bytes
    assert store.observe()["receipt"] == receipt


@pytest.mark.parametrize("legacy_consumed", [False, True])
def test_postclaim_death_and_autonomous_restart_never_reissue(setup, legacy_consumed):
    _, store, legacy, _ = setup
    rollback_open(store)
    store.enter(TOKEN, "first-boot-12345678", now=NOW)
    if legacy_consumed:
        assert legacy.begin_boot("first-boot-12345678", now=NOW)
    assert store.enter(TOKEN, "autonomous-boot-12345678", now=NOW + 1) == "service_only"
    assert not legacy.ACCEPTED_PATH.exists()
    assert not legacy.ACK_PATH.exists()
    assert not legacy.begin_boot("autonomous-boot-12345678", now=NOW + 1)
    assert store.observe()["slots"]["rollback"]["consumed"]["boot_id"] == "first-boot-12345678"


def test_unrelated_fresh_ordinary_restart_survives_cleanup(setup):
    _, store, legacy, _ = setup
    rollback_open(store)
    store.enter(TOKEN, "first-boot-12345678", now=NOW)
    assert legacy.begin_boot("first-boot-12345678", now=NOW)
    request = {"version": 1, "nonce": "new-ordinary-nonce-12345678",
               "source_boot_id": "first-boot-12345678", "created_at": NOW + 1}
    legacy.INTENT_PATH.write_text(json.dumps(request))
    legacy.REQUEST_PATH.write_text(json.dumps(request))
    assert legacy.accept("first-boot-12345678", now=NOW + 1)
    before = legacy.ACCEPTED_PATH.read_bytes()
    assert store.enter(TOKEN, "planned-later-boot-12345678", now=NOW + 2) == "service_only"
    assert legacy.ACCEPTED_PATH.read_bytes() == before
    assert legacy.begin_boot("planned-later-boot-12345678", now=NOW + 2)
    assert json.loads(legacy.ACK_PATH.read_text())["nonce"] == request["nonce"]


def test_closed_unknown_and_quiesced_tokens_do_not_touch_legacy_ledger(setup):
    module, store, legacy, _ = setup
    rollback_open(store)
    assert store.close_pending("rollback", TOKEN)
    before = {p.name: p.read_bytes() for p in legacy.LEDGER_DIR.iterdir()}
    for token in (TOKEN, "unknown-token-12345678"):
        with pytest.raises(module.AdmissionError):
            store.enter(token, "denied-boot-12345678", now=NOW)
    assert before == {p.name: p.read_bytes() for p in legacy.LEDGER_DIR.iterdir()}


def test_cancellation_requires_exact_fence_and_never_reuses_cid(setup):
    module, store, _, _ = setup
    rollback_open(store)
    assert not store.issue_start("rollback", TOKEN)
    with pytest.raises(module.AdmissionError):
        store.allocate("rollback", "second-token-12345678", IMAGE)
    assert store.close_pending("rollback", TOKEN)
    with pytest.raises(module.AdmissionError):
        store.allocate("rollback", "second-token-12345678", IMAGE)
    with pytest.raises(module.AdmissionError):
        store.fenced("rollback", TOKEN, "9" * 64)
    store.fenced("rollback", TOKEN, ROLLBACK)
    attempt = store.allocate("rollback", "second-token-12345678", IMAGE)
    assert attempt["generation"] == 2
    with pytest.raises(module.AdmissionError):
        store.bind("rollback", "second-token-12345678", ROLLBACK)
    store.bind("rollback", "second-token-12345678", "4" * 64)
    store.open("rollback", "second-token-12345678")
    store.issue_start("rollback", "second-token-12345678")
    store.enter("second-token-12345678", "second-boot-12345678", now=NOW)
    with pytest.raises(module.AdmissionError):
        store.allocate("rollback", "third-token-12345678", IMAGE)
    assert not store.close_pending("rollback", "second-token-12345678")


def test_unbound_create_can_close_then_bind_discovered_cid_for_fencing(setup):
    module, store, _, _ = setup
    store.allocate("target", "target-token-12345678", NEW_IMAGE)
    assert store.close_pending("target", "target-token-12345678")
    with pytest.raises(module.AdmissionError):
        store.fenced("target", "target-token-12345678", TARGET)
    store.bind("target", "target-token-12345678", TARGET)
    store.fenced("target", "target-token-12345678", TARGET)
    assert store.allocate("target", "target-next-token-12345678", NEW_IMAGE)["generation"] == 2


def test_source_and_target_quiescence_precede_rollback(setup):
    module, store, legacy, _ = setup
    store.allocate("target", "target-token-12345678", NEW_IMAGE)
    store.bind("target", "target-token-12345678", TARGET)
    with pytest.raises(module.AdmissionError):
        store.open("target", "target-token-12345678")
    store.quiesce(SOURCE)
    store.open("target", "target-token-12345678")
    store.issue_start("target", "target-token-12345678")
    store.enter("target-token-12345678", "target-boot-12345678", now=NOW)
    assert legacy.begin_boot("target-boot-12345678", now=NOW)
    with pytest.raises(module.AdmissionError):
        store.allocate("rollback", TOKEN, IMAGE)
    store.quiesce(TARGET)
    with pytest.raises(module.AdmissionError):
        store.enter("target-token-12345678", "stale-target-boot-12345678", now=NOW)
    store.allocate("rollback", TOKEN, IMAGE)
    store.bind("rollback", TOKEN, ROLLBACK)
    store.open("rollback", TOKEN)
    store.issue_start("rollback", TOKEN)
    store.enter(TOKEN, "rollback-boot-12345678", now=NOW)
    assert legacy.begin_boot("rollback-boot-12345678", now=NOW)
    assert json.loads(legacy.ACK_PATH.read_text())["source_boot_id"] == "target-boot-12345678"


def test_rollback_ack_never_allocates_second_rollback(setup):
    module, store, legacy, _ = setup
    rollback_open(store)
    store.enter(TOKEN, "rollback-boot-12345678", now=NOW)
    legacy.begin_boot("rollback-boot-12345678", now=NOW)
    with pytest.raises(module.AdmissionError):
        store.allocate("rollback", "retry-token-12345678", IMAGE)
    assert store.enter(TOKEN, "later-boot-12345678", now=NOW + 601) == "service_only"
    assert not legacy.begin_boot("later-boot-12345678", now=NOW + 601)


@pytest.mark.parametrize("damage", ["symlink", "mode", "json", "nan", "duplicate", "directory", "hardlink"])
def test_malformed_trusted_acceptance_fails_before_claim(setup, damage):
    module, store, legacy, _ = setup
    rollback_open(store)
    path = legacy.ACCEPTED_PATH
    if damage == "symlink":
        path.unlink()
        path.symlink_to(legacy.CUTOVER_RECEIPT_PATH)
    elif damage == "mode":
        path.chmod(0o666)
    elif damage == "directory":
        path.unlink()
        path.mkdir()
    elif damage == "hardlink":
        os.link(path, path.parent / "alias")
    else:
        path.write_text({"json": "[", "nan": '{"value":NaN}', "duplicate": '{"x":1,"x":2}'}[damage])
    with pytest.raises((module.AdmissionError, OSError)):
        store.enter(TOKEN, "denied-boot-12345678", now=NOW)
    assert store.observe()["slots"]["rollback"]["consumed"] is None


def test_untrusted_gate_and_malformed_state_do_not_mutate_acceptance(setup):
    module, store, legacy, _ = setup
    rollback_open(store)
    accepted = legacy.ACCEPTED_PATH.read_bytes()
    state_path = store.state_dir / "state.json"
    original = state_path.read_bytes()
    state_path.write_text('{"version":1,"source":[]}')
    with pytest.raises(module.AdmissionError):
        store.enter(TOKEN, "denied-boot-12345678", now=NOW)
    state_path.write_bytes(original)
    store.state_dir.chmod(0o777)
    with pytest.raises(module.AdmissionError):
        store.enter(TOKEN, "denied-boot-12345678", now=NOW)
    store.state_dir.chmod(0o700)
    assert legacy.ACCEPTED_PATH.read_bytes() == accepted


def test_prepare_verifies_root_receipt_and_is_idempotent(setup):
    module, store, _, receipt = setup
    assert store.prepare(OP, receipt, SOURCE, IMAGE, SOURCE_BOOT) == store.observe()
    with pytest.raises(module.AdmissionError):
        store.prepare(OP, {**receipt, "nonce": "different-nonce-12345678"}, SOURCE, IMAGE, SOURCE_BOOT)
    another = module.AdmissionStore(store.state_dir.parent / "other", store.data_dir, os.getuid(), os.getgid())
    with pytest.raises(module.AdmissionError):
        another.prepare(OP, {**receipt, "nonce": "different-nonce-12345678"}, SOURCE, IMAGE, SOURCE_BOOT)


class PowerLoss(BaseException):
    pass


@pytest.mark.parametrize("boundary", ["accepted-before", "accepted-after", "state-before", "state-after"])
def test_interruption_at_acceptance_and_claim_publication(setup, monkeypatch, boundary):
    _, store, legacy, _ = setup
    rollback_open(store)
    write = store._write
    def interrupted(directory, name, value, mode=0o600):
        stage = "accepted" if name == "accepted.json" else "state"
        if boundary == stage + "-before":
            raise PowerLoss()
        write(directory, name, value, mode)
        if boundary == stage + "-after":
            raise PowerLoss()
    monkeypatch.setattr(store, "_write", interrupted)
    with pytest.raises(PowerLoss):
        store.enter(TOKEN, "interrupted-boot-12345678", now=NOW + 601)
    monkeypatch.setattr(store, "_write", write)
    consumed = store.observe()["slots"]["rollback"]["consumed"] is not None
    result = store.enter(TOKEN, "recovery-boot-12345678", now=NOW + 602)
    assert result == ("service_only" if consumed else "continuation")
    assert legacy.begin_boot("recovery-boot-12345678", now=NOW + 602) is not consumed


@pytest.mark.parametrize("failure_number", range(1, 6))
def test_each_enter_fsync_error_propagates_without_reusing_consumption(setup, monkeypatch, failure_number):
    module, store, legacy, _ = setup
    rollback_open(store)
    fsync, count = module.os.fsync, [0]
    def fail(fd):
        count[0] += 1
        if count[0] == failure_number:
            raise OSError("simulated persistence failure")
        return fsync(fd)
    monkeypatch.setattr(module.os, "fsync", fail)
    with pytest.raises(OSError):
        store.enter(TOKEN, "failed-boot-12345678", now=NOW)
    monkeypatch.setattr(module.os, "fsync", fsync)
    consumed = store.observe()["slots"]["rollback"]["consumed"] is not None
    result = store.enter(TOKEN, "recovery-boot-12345678", now=NOW)
    assert result == ("service_only" if consumed else "continuation")
    assert legacy.begin_boot("recovery-boot-12345678", now=NOW) is not consumed


def _consume_and_begin(state_dir, data_dir, output):
    module = load(GATE_PATH, "child_gate")
    store = module.AdmissionStore(state_dir, data_dir, os.getuid(), os.getgid())
    result = store.enter(TOKEN, "child-boot-12345678", now=NOW)
    authorized = legacy_at(Path(data_dir)).begin_boot("child-boot-12345678", now=NOW)
    output.put((result, authorized))


def test_consumption_after_host_observation_cannot_reissue(setup):
    _, store, legacy, _ = setup
    rollback_open(store)
    assert store.observe()["slots"]["rollback"]["consumed"] is None
    ctx = multiprocessing.get_context("spawn")
    output = ctx.Queue()
    process = ctx.Process(target=_consume_and_begin, args=(store.state_dir, store.data_dir, output))
    process.start()
    process.join(5)
    assert process.exitcode == 0
    assert output.get(timeout=1) == ("continuation", True)
    assert not store.close_pending("rollback", TOKEN)
    assert not legacy.ACCEPTED_PATH.exists()
    assert not legacy.begin_boot("unrelated-boot-12345678", now=NOW)


def _enter_paused_before_replace(state_dir, data_dir, paused, resume, output):
    module = load(GATE_PATH, "child_gate")
    replace = module.os.replace
    def paused_replace(source, target, **kwargs):
        if target == "accepted.json":
            paused.set()
            assert resume.wait(5)
        return replace(source, target, **kwargs)
    module.os.replace = paused_replace
    store = module.AdmissionStore(state_dir, data_dir, os.getuid(), os.getgid())
    output.put(store.enter(TOKEN, "child-boot-12345678", now=NOW))
    assert legacy_at(Path(data_dir)).begin_boot("child-boot-12345678", now=NOW)


def _close_attempt(state_dir, data_dir, started, done, output):
    module = load(GATE_PATH, "close_gate")
    store = module.AdmissionStore(state_dir, data_dir, os.getuid(), os.getgid())
    started.set()
    output.put(store.close_pending("rollback", TOKEN))
    done.set()


def test_acceptance_replace_and_close_are_serialized_in_real_processes(setup):
    _, store, legacy, _ = setup
    rollback_open(store)
    ctx = multiprocessing.get_context("spawn")
    paused, resume, started, done = (ctx.Event() for _ in range(4))
    enter_output, close_output = ctx.Queue(), ctx.Queue()
    consumer = ctx.Process(target=_enter_paused_before_replace,
                           args=(store.state_dir, store.data_dir, paused, resume, enter_output))
    closer = ctx.Process(target=_close_attempt,
                         args=(store.state_dir, store.data_dir, started, done, close_output))
    consumer.start()
    try:
        assert paused.wait(5)
        closer.start()
        assert started.wait(5)
        assert not done.wait(0.1)
        resume.set()
        consumer.join(5)
        closer.join(5)
        assert consumer.exitcode == closer.exitcode == 0
        assert enter_output.get(timeout=1) == "continuation"
        assert close_output.get(timeout=1) is False
        assert not legacy.ACCEPTED_PATH.exists()
    finally:
        resume.set()
        for process in (consumer, closer):
            if process.pid and process.is_alive():
                process.terminate()
                process.join(5)


def test_wrapper_cli_denial_never_executes_original_argv(setup):
    _, store, _, _ = setup
    rollback_open(store)
    store.close_pending("rollback", TOKEN)
    marker = store.state_dir / "executed"
    result = subprocess.run([sys.executable, str(GATE_PATH), "enter", "--state-dir", str(store.state_dir),
                             "--data-dir", str(store.data_dir), "--token", TOKEN, "--", sys.executable,
                             "-c", f"open({str(marker)!r},'w').write('bad')"], capture_output=True, text=True)
    assert result.returncode == 75
    assert not marker.exists()


def test_boot_before_host_issue_is_denied_without_legacy_mutation(setup):
    module, store, legacy, _ = setup
    store.quiesce(SOURCE)
    store.allocate("rollback", TOKEN, IMAGE)
    store.bind("rollback", TOKEN, ROLLBACK)
    store.open("rollback", TOKEN)
    before = legacy.ACCEPTED_PATH.read_bytes()
    with pytest.raises(module.AdmissionError):
        store.enter(TOKEN, "premature-boot-12345678", now=NOW)
    assert legacy.ACCEPTED_PATH.read_bytes() == before
    assert store.observe()["slots"]["rollback"]["consumed"] is None


@pytest.mark.parametrize("boundary", ["accepted-before", "accepted-after", "ack-before", "ack-after"])
def test_consumed_cleanup_is_retryable_after_each_unlink_boundary(setup, monkeypatch, boundary):
    module, store, legacy, _ = setup
    rollback_open(store)
    store.enter(TOKEN, "first-boot-12345678", now=NOW)
    # Represent both the pre-begin leftover and a matching old ACK. Cleanup
    # must be safe for either file independently and for partial progress.
    legacy._write_json(legacy.ACK_PATH, {
        **json.loads(legacy.ACCEPTED_PATH.read_text()), "target_boot_id": "first-boot-12345678",
    }, 0o444)
    unlink = module.os.unlink
    def interrupted(name, **kwargs):
        stage = "accepted" if name == "accepted.json" else "ack"
        if boundary == stage + "-before":
            raise PowerLoss()
        unlink(name, **kwargs)
        if boundary == stage + "-after":
            raise PowerLoss()
    monkeypatch.setattr(module.os, "unlink", interrupted)
    with pytest.raises(PowerLoss):
        store.enter(TOKEN, "interrupted-boot-12345678", now=NOW)
    monkeypatch.setattr(module.os, "unlink", unlink)
    assert store.enter(TOKEN, "recovery-boot-12345678", now=NOW) == "service_only"
    assert not legacy.begin_boot("recovery-boot-12345678", now=NOW)
    assert store.observe()["slots"]["rollback"]["consumed"]["boot_id"] == "first-boot-12345678"


@pytest.mark.parametrize("step", ["allocate", "bind", "open", "issue", "close", "fence", "quiesce"])
@pytest.mark.parametrize("when", ["before", "after"])
def test_host_transition_publication_replays_without_authority_regression(setup, monkeypatch, step, when):
    _, store, _, _ = setup
    store.quiesce(SOURCE)
    if step not in {"allocate", "quiesce"}:
        store.allocate("rollback", TOKEN, IMAGE)
    if step not in {"allocate", "bind", "quiesce"}:
        store.bind("rollback", TOKEN, ROLLBACK)
    if step not in {"allocate", "bind", "open", "quiesce"}:
        store.open("rollback", TOKEN)
    if step in {"close", "fence"}:
        store.issue_start("rollback", TOKEN)
    if step == "fence":
        store.close_pending("rollback", TOKEN)
    if step == "quiesce":
        # A new target identity provides a non-idempotent quiescence update.
        store.allocate("target", "target-token-12345678", NEW_IMAGE)
        store.bind("target", "target-token-12345678", TARGET)
        store.close_pending("target", "target-token-12345678")
    action = {
        "allocate": lambda: store.allocate("rollback", TOKEN, IMAGE),
        "bind": lambda: store.bind("rollback", TOKEN, ROLLBACK),
        "open": lambda: store.open("rollback", TOKEN),
        "issue": lambda: store.issue_start("rollback", TOKEN),
        "close": lambda: store.close_pending("rollback", TOKEN),
        "fence": lambda: store.fenced("rollback", TOKEN, ROLLBACK),
        "quiesce": lambda: store.quiesce(TARGET),
    }[step]
    save = store._save
    def interrupted(directory, value):
        if when == "before":
            raise PowerLoss()
        save(directory, value)
        raise PowerLoss()
    monkeypatch.setattr(store, "_save", interrupted)
    with pytest.raises(PowerLoss):
        action()
    monkeypatch.setattr(store, "_save", save)
    result = action()
    if step == "issue":
        assert result is (when == "before")
    assert store.observe()["slots"]["rollback"]["consumed"] is None


def test_wrapper_main_preserves_argv_and_overrides_persisted_boot_id(setup, monkeypatch):
    module, store, legacy, _ = setup
    rollback_open(store)
    # main's wall clock must be within the receipt, and its default trust
    # identity is replaced only for an unprivileged test runner.
    monkeypatch.setattr(module, "AdmissionStore", lambda *_args: store)
    monkeypatch.setattr(module.time, "time", lambda: NOW)
    monkeypatch.setenv("MOBIUS_BOOT_ID", "persisted-boot-must-not-recur")
    seen = []
    def execute(program, argv):
        seen.append((program, argv, os.environ["MOBIUS_BOOT_ID"]))
        raise PowerLoss()
    monkeypatch.setattr(module.os, "execvp", execute)
    argv = ["/sbin/tini", "--", "/original/entrypoint", "arg with spaces", "--literal"]
    with pytest.raises(PowerLoss):
        module.main(["enter", "--state-dir", str(store.state_dir), "--data-dir", str(store.data_dir),
                     "--token", TOKEN, "--", *argv])
    assert seen[0][:2] == (argv[0], argv)
    assert seen[0][2] != "persisted-boot-must-not-recur"
    assert store.observe()["slots"]["rollback"]["consumed"]["boot_id"] == seen[0][2]
    assert legacy.begin_boot(seen[0][2], now=NOW)


@pytest.mark.parametrize("field,value", [("accepted_at", "invalid"), ("created_at", -1)])
def test_malformed_matching_acceptance_is_not_repaired_by_refresh(setup, field, value):
    module, store, legacy, _ = setup
    rollback_open(store)
    acceptance = json.loads(legacy.ACCEPTED_PATH.read_text())
    acceptance[field] = value
    legacy.ACCEPTED_PATH.write_text(json.dumps(acceptance))
    before = legacy.ACCEPTED_PATH.read_bytes()
    with pytest.raises(module.AdmissionError):
        store.enter(TOKEN, "denied-boot-12345678", now=NOW)
    assert legacy.ACCEPTED_PATH.read_bytes() == before
    assert store.observe()["slots"]["rollback"]["consumed"] is None


def test_closed_old_process_cannot_mutate_successor_acceptance(setup):
    module, store, legacy, _ = setup
    rollback_open(store)
    store.close_pending("rollback", TOKEN)
    store.fenced("rollback", TOKEN, ROLLBACK)
    next_token = "next-token-12345678"
    store.allocate("rollback", next_token, IMAGE)
    store.bind("rollback", next_token, "4" * 64)
    store.open("rollback", next_token)
    store.issue_start("rollback", next_token)
    store.enter(next_token, "next-boot-12345678", now=NOW)
    before = legacy.ACCEPTED_PATH.read_bytes()
    with pytest.raises(module.AdmissionError):
        store.enter(TOKEN, "late-old-boot-12345678", now=NOW)
    assert legacy.ACCEPTED_PATH.read_bytes() == before
    assert legacy.begin_boot("next-boot-12345678", now=NOW)


def test_old_source_restart_cannot_steal_later_external_cutover(setup):
    _, store, legacy, _ = setup
    rollback_open(store)
    store.enter(TOKEN, "first-boot-12345678", now=NOW)
    assert legacy.begin_boot("first-boot-12345678", now=NOW)
    next_op = "later-operation-12345678"
    assert legacy.open_cutover(next_op, now=NOW + 1)
    legacy.INTENT_PATH.write_text(json.dumps({
        "version": 1, "action": "external_cutover", "cutover_id": next_op,
        "nonce": "later-cutover-nonce-12345678", "source_boot_id": "first-boot-12345678",
        "created_at": NOW + 1,
    }))
    assert legacy.accept_cutover(next_op, now=NOW + 1)
    receipt = legacy.CUTOVER_RECEIPT_PATH.read_bytes()
    unrelated_ack = {**json.loads(receipt), "target_boot_id": "other-evidence-12345678"}
    legacy._write_json(legacy.ACK_PATH, unrelated_ack, 0o444)
    ack = legacy.ACK_PATH.read_bytes()
    assert store.enter(TOKEN, "source-autonomous-12345678", now=NOW + 2) == "service_only"
    assert not legacy.ACCEPTED_PATH.exists()
    assert legacy.ACK_PATH.read_bytes() == ack  # wrapper never destroys another op's ACK
    assert legacy.CUTOVER_RECEIPT_PATH.read_bytes() == receipt
    assert not legacy.begin_boot("source-autonomous-12345678", now=NOW + 2)


@pytest.mark.parametrize("timing", ["before-prepare", "before-quiesce"])
@pytest.mark.parametrize("boundary", ["consumed", "after-unlink", "after-ack"])
def test_unwrapped_source_consumption_freezes_service_only(setup, timing, boundary):
    module, prepared, legacy, receipt = setup
    store = (module.AdmissionStore(prepared.state_dir.parent / "not-yet-prepared", prepared.data_dir,
                                   os.getuid(), os.getgid()) if timing == "before-prepare" else prepared)
    if boundary == "consumed":
        assert legacy.begin_boot("unexpected-source-boot-12345678", now=NOW)
    else:
        legacy.ACCEPTED_PATH.unlink()
        if boundary == "after-ack":
            legacy._write_json(legacy.ACK_PATH, {**receipt, "target_boot_id": "unexpected-source-boot-12345678"}, 0o444)
    if timing == "before-prepare":
        store.prepare(OP, receipt, SOURCE, IMAGE, SOURCE_BOOT)
    rollback_open(store)
    assert store.observe()["source_handoff_valid"] is False
    assert store.enter(TOKEN, "restored-service-12345678", now=NOW) == "service_only"
    assert not legacy.begin_boot("restored-service-12345678", now=NOW)
    assert not legacy.ACK_PATH.exists()


def test_source_invalidity_cannot_be_reconstructed_from_later_acceptance(setup):
    _, store, legacy, receipt = setup
    legacy.ACCEPTED_PATH.unlink()
    store.quiesce(SOURCE)
    assert store.observe()["source_handoff_valid"] is False
    legacy._write_json(legacy.ACCEPTED_PATH, receipt, 0o600)
    store.quiesce(SOURCE)
    assert store.observe()["source_handoff_valid"] is False
    store.allocate("target", "target-token-12345678", NEW_IMAGE)
    store.bind("target", "target-token-12345678", TARGET)
    store.open("target", "target-token-12345678")
    store.issue_start("target", "target-token-12345678")
    assert store.enter("target-token-12345678", "target-boot-12345678", now=NOW) == "service_only"
    assert not legacy.begin_boot("target-boot-12345678", now=NOW)
    store.quiesce(TARGET)
    store.allocate("rollback", TOKEN, IMAGE)
    store.bind("rollback", TOKEN, ROLLBACK)
    store.open("rollback", TOKEN)
    store.issue_start("rollback", TOKEN)
    assert store.enter(TOKEN, "rollback-boot-12345678", now=NOW) == "service_only"
    assert not legacy.begin_boot("rollback-boot-12345678", now=NOW)


@pytest.mark.parametrize("boundary", ["before-unlink", "after-unlink", "after-ack", "after-boot"])
def test_target_partial_consumption_requires_exact_complete_proof(setup, boundary):
    _, store, legacy, receipt = setup
    store.quiesce(SOURCE)
    store.allocate("target", "target-token-12345678", NEW_IMAGE)
    store.bind("target", "target-token-12345678", TARGET)
    store.open("target", "target-token-12345678")
    store.issue_start("target", "target-token-12345678")
    assert store.enter("target-token-12345678", "target-boot-12345678", now=NOW) == "continuation"
    if boundary == "after-boot":
        assert legacy.begin_boot("target-boot-12345678", now=NOW)
    elif boundary in {"after-unlink", "after-ack"}:
        legacy.ACCEPTED_PATH.unlink()
        if boundary == "after-ack":
            legacy._write_json(legacy.ACK_PATH, {**receipt, "target_boot_id": "target-boot-12345678"}, 0o444)
    store.quiesce(TARGET)
    store.allocate("rollback", TOKEN, IMAGE)
    store.bind("rollback", TOKEN, ROLLBACK)
    store.open("rollback", TOKEN)
    store.issue_start("rollback", TOKEN)
    eligible = boundary in {"before-unlink", "after-boot"}
    assert store.enter(TOKEN, "rollback-boot-12345678", now=NOW) == ("continuation" if eligible else "service_only")
    assert legacy.begin_boot("rollback-boot-12345678", now=NOW) is eligible


def test_unknown_boot_ack_never_authorizes_rollback(setup):
    _, store, legacy, receipt = setup
    store.quiesce(SOURCE)
    legacy.ACCEPTED_PATH.unlink()
    legacy._write_json(legacy.ACK_PATH, {**receipt, "target_boot_id": "unknown-boot-12345678"}, 0o444)
    legacy._atomic_write(legacy.BOOT_PATH, b"unknown-boot-12345678\n", 0o444)
    store.allocate("rollback", TOKEN, IMAGE)
    store.bind("rollback", TOKEN, ROLLBACK)
    store.open("rollback", TOKEN)
    store.issue_start("rollback", TOKEN)
    assert store.enter(TOKEN, "rollback-boot-12345678", now=NOW) == "service_only"
    assert not legacy.begin_boot("rollback-boot-12345678", now=NOW)


def test_prepare_never_reinitializes_existing_null_state(setup):
    module, store, _, receipt = setup
    state = store.state_dir / "state.json"
    state.write_text("null")
    with pytest.raises(module.AdmissionError):
        store.prepare(OP, receipt, SOURCE, IMAGE, SOURCE_BOOT)
    assert state.read_text() == "null"
