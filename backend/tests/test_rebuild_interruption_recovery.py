"""Interrupted host recovery against the real admission gate and legacy ledger.

These tests intentionally share the stateful Docker/boot fixture with the fault
matrix, rather than emulating permission with an unwrapped Compose container.
"""
import json
import subprocess

import pytest

from tests.test_admission_host_recovery import incident, PowerLoss, prepare, recover


def status(c):
    return c.host.read_json(c.host.STATUS)


def test_legacy_issued_unwrapped_start_refuses_reconstruction(incident):
    c = incident
    tx = c.host.read_transaction()
    del tx["admission_version"]
    tx["phase"] = "rollback_started"
    c.host.write_transaction(tx)
    c.docker.containers[c.tx["source_container"]]["State"]["Status"] = "exited"
    accepted = c.legacy.ACCEPTED_PATH.read_bytes()
    for _ in range(2):
        recover(c)
    assert status(c)["state"] == "needs_recovery"
    assert status(c)["code"] == "legacy_admission_unconfirmed"
    assert c.host.TRANSACTION.exists()
    assert not c.docker.creates and not c.docker.starts and not c.docker.removes
    assert c.legacy.ACCEPTED_PATH.read_bytes() == accepted


@pytest.mark.parametrize("boundary", ["outcome-1-before", "outcome-1-after",
                                      "finalize-before", "finalize-after",
                                      "outcome-2-before", "outcome-2-after",
                                      "status-before", "status-after",
                                      "clear-before", "clear-after"])
def test_settlement_power_loss_never_restarts_consumed_generation(incident, monkeypatch, boundary):
    c = incident
    cid = prepare(c)
    assert c.docker.complete_start(cid)
    write, finish, publish, clear = (c.host.write_transaction, c.host.restart_ledger,
                                     c.host.write_status, c.host.clear_transaction)
    outcomes = [0]
    def inject(name, callback, *args, **kwargs):
        c.docker.hit(name + "-before")
        result = callback(*args, **kwargs)
        c.docker.hit(name + "-after")
        return result
    def write_guard(value):
        if value.get("outcome"):
            outcomes[0] += 1
            return inject(f"outcome-{outcomes[0]}", write, value)
        return write(value)
    def finish_guard(*args, **kwargs):
        if args[2] == "finalize-cutover":
            return inject("finalize", finish, *args, **kwargs)
        return finish(*args, **kwargs)
    def status_guard(*args, **kwargs):
        if kwargs.get("state") == "rolled_back":
            return inject("status", publish, *args, **kwargs)
        return publish(*args, **kwargs)
    monkeypatch.setattr(c.host, "write_transaction", write_guard)
    monkeypatch.setattr(c.host, "restart_ledger", finish_guard)
    monkeypatch.setattr(c.host, "write_status", status_guard)
    monkeypatch.setattr(c.host, "clear_transaction", lambda: inject("clear", clear))
    c.docker.fault = boundary
    with pytest.raises(PowerLoss):
        recover(c)
    assert c.docker.starts == [cid]
    for _ in range(2):
        c.host.reconcile()
    assert c.docker.starts == [cid] and cid not in c.docker.removes
    assert not c.host.TRANSACTION.exists()
    assert status(c)["state"] == "rolled_back"
    assert not c.legacy.ACCEPTED_PATH.exists()
    if boundary in {"outcome-1-after", "finalize-before", "finalize-after", "outcome-2-before"}:
        assert status(c)["code"] == "handoff_finalize_unconfirmed"


def test_transient_daemon_observation_retries_without_new_start(incident):
    c = incident
    cid = prepare(c)
    assert c.docker.complete_start(cid)
    c.docker.inspect_error = "Cannot connect to the Docker daemon"
    recover(c)
    assert status(c)["state"] == "needs_recovery"
    assert c.host.TRANSACTION.exists()
    c.docker.inspect_error = None
    c.host.reconcile()
    assert status(c)["state"] == "rolled_back"
    assert not c.host.TRANSACTION.exists()
    assert c.docker.starts == [cid] and cid not in c.docker.removes


def test_power_loss_loses_docker_started_metadata_but_not_consumed_admission(incident):
    c = incident
    cid = prepare(c)
    assert c.docker.complete_start(cid)
    ack = c.legacy.ACK_PATH.read_bytes()
    c.docker.containers[cid]["State"] = {"Status": "created", "StartedAt": "0001-01-01T00:00:00Z"}
    c.host.reconcile()
    assert c.docker.starts == [cid] and cid not in c.docker.removes
    assert c.legacy.ACK_PATH.read_bytes() == ack
    assert c.host.TRANSACTION.exists()
    assert status(c)["code"] == "rollback_failed"


def test_slow_rollback_observes_entire_203_second_health_window(incident, monkeypatch):
    c = incident
    cid = prepare(c)
    assert c.docker.complete_start(cid)
    c.docker.containers[cid]["State"]["Health"]["Status"] = "starting"
    monkeypatch.setattr(c.host, "ROLLBACK_HEALTH_SECONDS", 240)
    start = c.clock[0]
    original_sleep = c.docker.sleep
    def sleep(delay):
        original_sleep(delay)
        if c.clock[0] - start >= 203:
            c.docker.containers[cid]["State"]["Health"]["Status"] = "healthy"
    monkeypatch.setattr(c.host.time, "sleep", sleep)
    recover(c)
    assert c.clock[0] - start >= 203
    assert c.docker.starts == [cid]
    assert status(c)["state"] == "rolled_back"
    assert not c.host.TRANSACTION.exists()


def test_legacy_authorization_corruption_does_not_become_new_permission(incident):
    c = incident
    path = c.legacy.ACCEPTED_PATH
    value = json.loads(path.read_text())
    value["nonce"] = "wrong-nonce-1234"
    path.chmod(0o644)
    path.write_text(json.dumps(value))
    cid = prepare(c)
    assert not c.docker.complete_start(cid)
    assert c.docker.entries[-1] == (cid, "denied", False)
    recover(c)
    assert c.docker.starts[0] == cid
    assert all(not continuation for _cid, _result, continuation in c.docker.entries)
    assert all(entry[1] != "continuation" for entry in c.docker.entries)
    assert not c.legacy.ACK_PATH.exists()
    assert c.host.TRANSACTION.exists()
    assert status(c)["state"] == "needs_recovery"


@pytest.mark.parametrize("initial", ["running", "restarting"])
def test_slow_target_gets_more_than_180_seconds_before_rollback(incident, monkeypatch, initial):
    c = incident
    c.host.prepare_admission(c.config, c.tx)
    cid = c.host.prepare_attempt(c.config, c.tx, "target")
    assert c.docker.complete_start(cid)
    state = c.docker.containers[cid]["State"]
    state["Status"] = initial
    state["Health"]["Status"] = "starting"
    c.tx["phase"] = "replacement_started"
    c.host.write_transaction(c.tx)
    monkeypatch.setattr(c.host, "TARGET_HEALTH_SECONDS", 240)
    start = c.clock[0]
    original_sleep = c.docker.sleep
    def sleep(delay):
        original_sleep(delay)
        if c.clock[0] - start >= 203:
            state["Status"] = "running"
            state["Health"]["Status"] = "healthy"
    monkeypatch.setattr(c.host.time, "sleep", sleep)
    recover(c)
    assert c.clock[0] - start >= 203
    assert c.docker.starts == [cid]
    assert status(c)["state"] == "succeeded"
    assert not c.host.TRANSACTION.exists()


def test_consumed_target_failure_authorizes_one_separate_fenced_rollback(incident):
    c = incident
    c.host.prepare_admission(c.config, c.tx)
    target = c.host.prepare_attempt(c.config, c.tx, "target")
    assert c.docker.complete_start(target)
    target_boot = c.gate.observe()["slots"]["target"]["consumed"]["boot_id"]
    c.docker.containers[target]["State"]["Status"] = "exited"
    c.tx["phase"] = "replacement_started"
    c.host.write_transaction(c.tx)
    recover(c)
    assert status(c)["state"] == "rolled_back"
    assert len(c.docker.starts) == 2 and c.docker.starts[0] == target
    assert target in c.docker.removes
    assert len([entry for entry in c.docker.entries if entry[1] == "continuation"]) == 2
    assert json.loads(c.legacy.ACK_PATH.read_text())["source_boot_id"] == target_boot
    c.host.reconcile()
    assert len(c.docker.starts) == 2


# Delayed-consumer regressions: Start acceptance and wrapper entry are distinct.
def test_delayed_consumer_after_timeout_can_only_use_original_permission(incident):
    c = incident
    c.docker.start_timeout = True
    with pytest.raises(subprocess.TimeoutExpired):
        prepare(c)
    old = c.docker.starts[0]
    c.docker.start_timeout = False
    assert c.docker.complete_start(old)
    c.host.reconcile()
    assert c.docker.starts == [old]
    assert not c.host.TRANSACTION.exists()
    assert c.docker.entries == [(old, "continuation", True)]


def test_delayed_consumer_after_explicit_close_is_denied_before_successor(incident):
    c = incident
    old = prepare(c)
    seen = []
    c.docker.before_remove = lambda cid: seen.append(c.docker.complete_start(cid)) if cid == old else None
    new = prepare(c)
    assert old != new and seen == [False]
    assert c.docker.complete_start(new)
    assert c.docker.entries == [(old, "denied", False), (new, "continuation", True)]
    c.host.reconcile()
    assert c.docker.starts == [old, new]
    assert not c.host.TRANSACTION.exists()
