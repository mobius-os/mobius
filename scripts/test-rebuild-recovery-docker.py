#!/usr/bin/env python3
"""Real Docker/Compose and frozen ledger recovery proof, hosted runner only.

No production unit, image, registry write, or daemon is touched. Docker and Compose
are real. This exercises reconcile/rollback rather than the download/drain
half of run(); served-generation probing and worker adoption are omitted.
The timeout happens on a real Compose call after a separate real no-start
creation. It does not prove delayed daemon API acceptance of that same call.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def command(*args, timeout=90, **kwargs):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=timeout, **kwargs)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def eventually(predicate, seconds=30):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return
        time.sleep(.25)
    raise AssertionError("condition did not become true")


def main():
    if (os.environ.get("MOBIUS_DOCKER_RECOVERY_PROOF") != "1"
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or os.geteuid() != 0):
        raise SystemExit("refusing outside explicitly opted-in disposable GitHub-hosted runner")
    command("docker", "info", timeout=15)
    import pytest
    from app import restart_ledger as platform_ledger
    from tests.test_restart_ledger import _bind, _load_supervisor

    host = load(ROOT / "scripts/mobius-rebuild-host.py", "recovery_docker_host")
    token = uuid.uuid4().hex[:10]
    image_base = f"mobius-recovery-proof-{token}"
    projects = []
    patch = pytest.MonkeyPatch()
    original_command = host.docker_command
    def cleanup():
        for project in reversed(projects):
            found = subprocess.run(["docker", "ps", "-aq", "--filter",
                                    f"label=com.docker.compose.project={project}"],
                                   capture_output=True, text=True, timeout=15).stdout.split()
            if found:
                subprocess.run(["docker", "rm", "-f", "-v", *found],
                               capture_output=True, timeout=30)
            subprocess.run(["docker", "network", "rm", f"{project}_default"],
                           capture_output=True, timeout=15)
        roles = ("source", "target", *(f"rollback-{name}" for name in
                 ("created-timeout", "slow-ready", "expired", "duplicates",
                  "missing", "journal", "stale-discovery", "restarting")),
                 *(f"target-{name}" for name in
                   ("created-timeout", "slow-ready", "expired", "duplicates",
                    "missing", "journal", "stale-discovery", "restarting")))
        for role in roles:
            subprocess.run(["docker", "image", "rm", "-f", f"{image_base}:{role}"],
                           capture_output=True, timeout=15)
    try:
        with tempfile.TemporaryDirectory(prefix=f"recovery-docker-{token}-") as temporary:
            root = Path(temporary)
            dockerfile = root / "Dockerfile"
            dockerfile.write_text('''FROM python:3.12-alpine
ARG ROLE
LABEL recovery-proof-role=${ROLE}
COPY restart_ledger.py /app/runtime/restart_ledger.py
ENTRYPOINT ["/bin/sh", "-c", "[ ${RESTART_BEFORE_BOOT:-0} != 1 ] || exit 2; python3 /app/runtime/restart_ledger.py begin-boot boot-$(cat /proc/sys/kernel/random/uuid) && exec sleep 3600"]
HEALTHCHECK --interval=1s --timeout=1s --retries=2 CMD test -f /data/ready
''')
            (root / "restart_ledger.py").write_bytes((ROOT / "backend/runtime/restart_ledger.py").read_bytes())
            for role in ("source", "target"):
                command("docker", "build", "-q", "--build-arg", f"ROLE={role}",
                        "-t", f"{image_base}:{role}", str(root), timeout=300)
            source = command("docker", "image", "inspect", "-f", "{{.Id}}", f"{image_base}:source").stdout.strip()
            target = command("docker", "image", "inspect", "-f", "{{.Id}}", f"{image_base}:target").stdout.strip()
            assert source != target

            def scenario(name, *, fault=False, readiness_delay=0, expired=False,
                         duplicates=False, missing=False, interrupt=False,
                         stale=False, restarting=False):
                project = f"recovery-{token}-{name}"
                projects.append(project)
                base = root / name
                base.mkdir()
                data = base / "data"
                data.mkdir()
                state = base / "state"
                state.mkdir()
                control = data / "mobius-rebuild"
                (control / "inbox").mkdir(parents=True)
                compose_file = base / "compose.yml"
                compose_file.write_text('''services:
  app:
    image: ${MOBIUS_IMAGE}
    restart: "${RESTART_POLICY:-no}"
    environment:
      RESTART_BEFORE_BOOT: "${RESTART_BEFORE_BOOT:-0}"
    volumes:
      - ./data:/data
''')
                override = base / "override.yml"
                override.write_text('services: {}\n')
                patch.setattr(host, "COMPOSE", compose_file)
                patch.setattr(host, "OVERRIDE", override)
                patch.setattr(host, "CONFIG", base / "config.json")
                patch.setattr(host, "STATE_DIR", state)
                patch.setattr(host, "LOCK", state / "replace.lock")
                patch.setattr(host, "STATUS", state / "status.json")
                patch.setattr(host, "TRANSACTION", state / "transaction.json")
                patch.setattr(host, "FAILED_TARGET_LOG", state / "failed-target.json")
                patch.setattr(host, "ROLLBACK_TAG", f"{image_base}:rollback-{name}")
                patch.setattr(host, "TARGET_TAG", f"{image_base}:target-{name}")
                config = {"project": project, "data_dir": data, "control_dir": control}
                patch.setattr(host, "config", lambda: config)
                ledger = _load_supervisor()
                _bind(ledger, data, patch)
                patch.setattr(host, "ROLLBACK_HEALTH_SECONDS", 12)
                patch.setattr(host, "TARGET_HEALTH_SECONDS", 12)
                patch.setattr(host, "COMPOSE_MUTATION_SECONDS", 12)
                patch.setattr(host, "verify_served_generation", lambda *_: None)
                patch.setattr(host, "retain_images", lambda *_: None)
                patch.setattr(host, "adopt_from_image", lambda *_: "not part of proof")
                patch.setattr(host, "docker_command", original_command)
                host.compose(config, "up", "-d", "--no-build", "app", image=source, timeout=30)
                cid, observed, _ = host.container_health(config)
                assert observed == source
                eventually(lambda: ledger.BOOT_PATH.exists())
                source_boot = ledger.BOOT_PATH.read_text().strip()
                operation, expected, nonce = uuid.uuid4().hex, "a" * 40, uuid.uuid4().hex
                now = time.time()
                assert ledger.open_cutover(operation, now=now)
                platform_ledger.publish_cutover_intent(boot_id=source_boot, nonce=nonce,
                    cutover_id=operation, runs=[], now=now)
                assert ledger.accept_cutover(operation, now=now)
                if expired:
                    for path in (ledger.CUTOVER_RECEIPT_PATH, ledger.ACCEPTED_PATH):
                        receipt = json.loads(path.read_text())
                        receipt["accepted_at"] = now - 3601
                        path.write_text(json.dumps(receipt))
                if interrupt:
                    # An aged but unexpired original acceptance must be
                    # refreshed before the journaled rollback boot starts.
                    for path in (ledger.CUTOVER_RECEIPT_PATH, ledger.ACCEPTED_PATH):
                        accepted = json.loads(path.read_text())
                        accepted["accepted_at"] = now - 601
                        path.write_text(json.dumps(accepted))
                transaction = host.transaction_record(operation, expected, nonce, source, target)
                transaction["phase"] = "replacement_started"
                host.write_transaction(transaction)
                if fault:
                    # First let real Compose delete the source and create the target.
                    # Then issue a *real* Compose mutation with a tiny caller deadline.
                    # This deterministically tests recovery from a timed-out command
                    # after the destructive boundary, not daemon API acceptance timing.
                    host.compose(config, "up", "--no-start", "--no-build", "--no-deps",
                                 "--force-recreate", "app", image=target, timeout=30)
                    _, observed, health = host.container_health(config)
                    assert observed == target and health == "created"
                    try:
                        host.compose(config, "up", "--no-start", "--no-build", "--no-deps",
                                     "app", image=target, timeout=.000001)
                    except subprocess.TimeoutExpired:
                        pass
                    else:
                        raise AssertionError("real Compose call did not time out")
                else:
                    if restarting:
                        with patch.context() as envpatch:
                            envpatch.setenv("RESTART_POLICY", "always")
                            envpatch.setenv("RESTART_BEFORE_BOOT", "1")
                            host.compose(config, "up", "--no-start", "--no-build", "--no-deps",
                                         "--force-recreate", "app", image=target, timeout=30)
                    else:
                        host.compose(config, "up", "--no-start", "--no-build", "--no-deps",
                                     "--force-recreate", "app", image=target, timeout=30)
                if restarting:
                    target_cid, _, _ = host.container_health(config)
                    command("docker", "start", target_cid)
                    eventually(lambda: host.container_health(config)[2] == "restarting")
                    assert ledger.ACCEPTED_PATH.exists(), "crash-before-boot consumed handoff"
                if duplicates:
                    # A second Compose-labelled app is an actual daemon object,
                    # not a fabricated `ps` response. Never choose arbitrarily.
                    target_cid, _, _ = host.container_health(config)
                    labels = json.loads(command("docker", "inspect", "-f",
                                                "{{json .Config.Labels}}", target_cid).stdout)
                    copied = [part for key, value in labels.items()
                              if key.startswith("com.docker.compose.")
                              for part in ("--label", f"{key}={value}")]
                    command("docker", "create", "--name", f"{project}-duplicate",
                            *copied, f"{image_base}:target")
                    ids = host.compose(config, "ps", "-a", "-q", "app").stdout.split()
                    assert len(ids) > 1, "Compose did not expose real duplicate labels"
                    before = ledger.ACCEPTED_PATH.read_bytes()
                    host.reconcile()
                    assert host.TRANSACTION.exists() and ledger.ACCEPTED_PATH.read_bytes() == before
                    command("docker", "rm", "-f", f"{project}-duplicate")
                if missing:
                    target_cid, _, _ = host.container_health(config)
                    command("docker", "rm", "-f", target_cid)
                # Compose really deleted the source. The separate stale
                # scenario below also races the worker's own ps/inspect pair.
                old = cid
                assert subprocess.run(["docker", "inspect", old], capture_output=True).returncode != 0
                if readiness_delay:
                    import threading
                    timer = threading.Timer(readiness_delay, lambda: (data / "ready").touch())
                    timer.start()
                else:
                    timer = None
                    (data / "ready").touch()
                try:
                    raced = {"removed": 0, "stale_inspect": 0, "ps": 0}
                    if stale:
                        def race(args, **kwargs):
                            if args[1] == "compose" and "ps" in args and "app" in args:
                                result = original_command(args, **kwargs)
                                raced["ps"] += 1
                                if not raced["removed"]:
                                    stale_id = result.stdout.strip()
                                    assert stale_id and "\n" not in stale_id
                                    command("docker", "rm", "-f", stale_id)
                                    create = ["docker", "compose", "-p", project,
                                              "-f", str(compose_file), "-f", str(override),
                                              "up", "--no-start", "--no-build", "--no-deps",
                                              "--force-recreate", "app"]
                                    original_command(create, cwd=base,
                                                     env={**os.environ, "MOBIUS_IMAGE": target},
                                                     timeout=30)
                                    fresh = original_command(args, **kwargs).stdout.strip()
                                    assert fresh and fresh != stale_id
                                    raced["removed"] = 1
                                return result
                            if args[1:3] == ["container", "inspect"]:
                                try:
                                    return original_command(args, **kwargs)
                                except subprocess.CalledProcessError:
                                    raced["stale_inspect"] += 1
                                    raise
                            return original_command(args, **kwargs)
                        patch.setattr(host, "docker_command", race)
                    if interrupt:
                        persist = host.write_transaction
                        crashed = [False]
                        def interrupted_write(value):
                            persist(value)
                            if value.get("rollback_stage") == "starting" and not crashed[0]:
                                crashed[0] = True
                                raise KeyboardInterrupt("journal persisted before Docker start")
                        patch.setattr(host, "write_transaction", interrupted_write)
                        wall_time = time.time
                        try:
                            # Persist start intent at the earlier wall clock, then
                            # resume 601 seconds later without sleeping ten minutes.
                            # Docker has not started the rollback at this boundary;
                            # its next real boot uses the restored real wall clock.
                            with patch.context() as clock_patch:
                                clock_patch.setattr(host.time, "time", lambda: wall_time() - 601)
                                host.reconcile()
                        except KeyboardInterrupt:
                            assert crashed[0] and host.TRANSACTION.exists()
                            receipt = json.loads(ledger.CUTOVER_RECEIPT_PATH.read_text())
                            accepted = json.loads(ledger.ACCEPTED_PATH.read_text())
                            assert wall_time() - receipt["accepted_at"] >= 600
                            assert wall_time() - accepted["accepted_at"] >= 600
                            assert host.read_transaction()["rollback_authorization"] == host.rearm_rollback(
                                config, operation, witness_only=True)
                        else:
                            raise AssertionError("journal interruption boundary was not reached")
                        patch.setattr(host, "write_transaction", persist)
                    host.reconcile()
                    if stale:
                        assert raced["removed"] == raced["stale_inspect"] == 1
                        assert raced["ps"] >= 2, "worker did not rediscover after stale inspect"
                        patch.setattr(host, "docker_command", original_command)
                    status = host.read_json(host.STATUS)
                    assert status["state"] in {"rolled_back", "needs_recovery"}, status
                    if expired:
                        assert status["state"] == "needs_recovery"
                        assert ledger.CUTOVER_RECEIPT_PATH.exists()
                        assert not ledger.ACK_PATH.exists()
                    else:
                        assert status["state"] == "rolled_back", status
                        assert ledger.ACK_PATH.exists() and not ledger.ACCEPTED_PATH.exists()
                        assert not host.TRANSACTION.exists()
                    boot = ledger.BOOT_PATH.read_bytes()
                    host.reconcile()
                    assert ledger.BOOT_PATH.read_bytes() == boot, "reconcile repeated a boot"
                finally:
                    if timer:
                        timer.cancel()
                        timer.join(timeout=2)

            try:
                scenario("created-timeout", fault=True)
                scenario("slow-ready", readiness_delay=4)
                scenario("expired", expired=True)
                scenario("duplicates", duplicates=True)
                scenario("missing", missing=True)
                scenario("journal", interrupt=True)
                scenario("stale-discovery", stale=True)
                scenario("restarting", restarting=True)
                print("real Docker/Compose and real ledger recovery proof passed")
            finally:
                cleanup()  # stop containers before deleting bind-mounted data
    finally:
        patch.undo()
        cleanup()  # also covers image-build failure before scenario setup


if __name__ == "__main__":
    main()
