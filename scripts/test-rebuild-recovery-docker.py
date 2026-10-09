#!/usr/bin/env python3
"""Disposable hosted proof: real Compose, Docker, boot gate, and frozen ledger.

Only source-only provenance/adoption work is stubbed. This runs no Docker on a
production Host and makes no registry writes. The proxy forwards real daemon
requests and withholds one exact response; it does not emulate Docker state.
"""
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import select
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def command(*args, timeout=60, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=timeout, **kwargs)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def eventually(predicate, seconds=20):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.2)
    raise AssertionError("disposable Docker condition did not become true")


class ResponseBarrier:
    """Withhold the successful response to one exact Docker API operation."""

    def __init__(self, path, method, cid, action=""):
        self.path = path
        self.pattern = (method.encode() + rb" /(?:v[0-9.]+/)?containers/" +
                        re.escape(cid.encode()) +
                        (rb"/" + action.encode() if action else b"") + rb"(?:[? /]|\r|\n)")
        self.accepted = threading.Event()
        self.release = threading.Event()
        self.stop = threading.Event()
        self.listener = None
        self.threads = []
        self.connections = []

    def __enter__(self):
        if not Path("/var/run/docker.sock").is_socket():
            raise RuntimeError("disposable runner lacks the local Docker Unix socket")
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen(32)
        self.listener.settimeout(.2)
        thread = threading.Thread(target=self._accept, name="docker-response-barrier")
        self.threads.append(thread)
        thread.start()
        return self

    def _accept(self):
        while not self.stop.is_set():
            try:
                client, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            self.connections.append(client)
            thread = threading.Thread(target=self._forward, args=(client,),
                                      name="docker-response-forward")
            self.threads.append(thread)
            thread.start()

    def _forward(self, client):
        daemon = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.connections.append(daemon)
        try:
            daemon.connect("/var/run/docker.sock")
            watched, scan, held = False, b"", b""
            until = time.monotonic() + 45
            while not self.stop.is_set() and time.monotonic() < until:
                if held and self.release.is_set():
                    client.sendall(held)
                    held = b""
                readable, _, _ = select.select([client, daemon], [], [], .2)
                if client in readable:
                    data = client.recv(65536)
                    if not data:
                        break
                    scan = (scan + data)[-8192:]
                    if re.search(self.pattern, scan):
                        watched = True
                    daemon.sendall(data)
                if daemon in readable:
                    data = daemon.recv(65536)
                    if not data:
                        break
                    if watched and not self.accepted.is_set():
                        held += data
                        if len(held) > 65536:
                            raise AssertionError("Docker response unexpectedly large")
                        if b"\r\n\r\n" in held:
                            status = held.split(b"\r\n", 1)[0]
                            if not re.match(rb"HTTP/1\.[01] 2[0-9][0-9]", status):
                                raise AssertionError(f"Docker operation failed: {status!r}")
                            self.accepted.set()
                    elif held:
                        held += data
                    else:
                        client.sendall(data)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # bounded Docker caller killed its CLI after the held reply
        finally:
            daemon.close()
            client.close()

    def __exit__(self, *_):
        self.release.set()
        self.stop.set()
        self.listener.close()
        self.threads[0].join(timeout=2)
        for connection in self.connections:
            connection.close()
        for thread in self.threads[1:]:
            thread.join(timeout=2)
        self.path.unlink(missing_ok=True)
        assert all(not thread.is_alive() for thread in self.threads), "proxy thread leaked"


def main():
    if (os.environ.get("MOBIUS_DOCKER_RECOVERY_PROOF") != "1"
            or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted"
            or os.geteuid() != 0):
        raise SystemExit("refusing outside explicitly opted-in root disposable hosted runner")
    command("docker", "info", timeout=15)
    import pytest
    from app import restart_ledger as platform_ledger
    from tests.test_restart_ledger import _bind, _load_supervisor

    host = load(ROOT / "scripts/mobius-rebuild-host.py", "recovery_docker_host")
    token = uuid.uuid4().hex[:10]
    image_base = f"mobius-recovery-proof-{token}"
    scenarios = []
    patch = pytest.MonkeyPatch()
    original_docker = host.docker_command
    original_wait = host.wait_healthy

    def cleanup():
        for project in reversed(scenarios):
            found = set()
            for label in (f"com.docker.compose.project={project}",
                          f"io.mobius.admission.project={project}"):
                found.update(subprocess.run(["docker", "ps", "-aq", "--filter",
                                             f"label={label}"], capture_output=True,
                                            text=True, timeout=15).stdout.split())
            if found:
                subprocess.run(["docker", "rm", "-f", "-v", *sorted(found)],
                               capture_output=True, timeout=30)
            subprocess.run(["docker", "network", "rm", f"{project}_default"],
                           capture_output=True, timeout=15)
        for role in ("source", "target"):
            subprocess.run(["docker", "image", "rm", "-f", f"{image_base}:{role}"],
                           capture_output=True, timeout=15)

    try:
        with tempfile.TemporaryDirectory(prefix=f"recovery-docker-{token}-") as temporary:
            root = Path(temporary)
            (root / "Dockerfile").write_text('''FROM python:3.12-alpine
ARG ROLE
LABEL recovery-proof-role=${ROLE}
COPY restart_ledger.py /app/runtime/restart_ledger.py
STOPSIGNAL SIGKILL
ENTRYPOINT ["/bin/sh", "-c", "[ ${RESTART_BEFORE_BOOT:-0} != 1 ] || exit 2; python3 /app/runtime/restart_ledger.py begin-boot ${MOBIUS_BOOT_ID:-boot-$(cat /proc/sys/kernel/random/uuid)} && exec sleep 3600"]
HEALTHCHECK --interval=1s --timeout=1s --retries=2 CMD test -f /data/ready
''')
            (root / "restart_ledger.py").write_bytes(
                (ROOT / "backend/runtime/restart_ledger.py").read_bytes())
            for role in ("source", "target"):
                command("docker", "build", "-q", "--build-arg", f"ROLE={role}",
                        "-t", f"{image_base}:{role}", str(root), timeout=300)
            source, target = (command("docker", "image", "inspect", "-f", "{{.Id}}",
                                      f"{image_base}:{role}").stdout.strip()
                              for role in ("source", "target"))
            assert source != target
            gate_code = root / "mobius-boot-admission.py"
            gate_code.write_bytes((ROOT / "scripts/mobius-boot-admission.py").read_bytes())
            gate_code.chmod(0o644)

            def scenario(name, kind):
                print(f"recovery Docker scenario: {name}", flush=True)
                project = f"recovery-{token}-{name}"
                scenarios.append(project)
                base = root / name
                base.mkdir()
                data, state = base / "data", base / "state"
                data.mkdir()
                state.mkdir()
                (data / "mobius-rebuild" / "inbox").mkdir(parents=True)
                admission_root = state / "admission"
                admission_root.mkdir(mode=0o700)
                compose_file, override = base / "compose.yml", base / "override.json"
                compose_file.write_text('''services:
  app:
    image: ${MOBIUS_IMAGE}
    volumes:
      - ./data:/data
''')
                override.write_text('{}\n')
                patch.setattr(host, "CONFIG", base / "config.json")
                patch.setattr(host, "COMPOSE", compose_file)
                patch.setattr(host, "OVERRIDE", override)
                patch.setattr(host, "STATE_DIR", state)
                patch.setattr(host, "STATUS", state / "status.json")
                patch.setattr(host, "LOCK", state / "replace.lock")
                patch.setattr(host, "TRANSACTION", state / "transaction.json")
                patch.setattr(host, "FAILED_TARGET_LOG", state / "failed-target.json")
                patch.setattr(host, "ADMISSION_CODE", gate_code)
                patch.setattr(host, "ADMISSION_ROOT", admission_root)
                patch.setattr(host, "COMPOSE_MUTATION_SECONDS", 4)
                patch.setattr(host, "ROLLBACK_HEALTH_SECONDS", 12)
                patch.setattr(host, "wait_healthy", lambda c, timeout=12: original_wait(c, timeout))
                patch.setattr(host, "verify_served_generation", lambda *_: None)
                patch.setattr(host, "retain_images", lambda *_: None)
                patch.setattr(host, "adopt_from_image", lambda *_: "not part of proof")
                patch.setattr(host, "docker_command", original_docker)
                config = {"project": project, "data_dir": data,
                          "control_dir": data / "mobius-rebuild"}
                patch.setattr(host, "config", lambda: config)
                host.compose(config, "up", "-d", "--no-build", "app", image=source, timeout=30)
                source_cid, source_image, _ = host.container_health(config)
                assert source_image == source
                ledger = _load_supervisor()
                _bind(ledger, data, patch)
                eventually(lambda: ledger.BOOT_PATH.exists())
                source_boot = ledger.BOOT_PATH.read_text().strip()
                operation, nonce = uuid.uuid4().hex, uuid.uuid4().hex
                now = time.time()
                assert ledger.open_cutover(operation, now=now)
                platform_ledger.publish_cutover_intent(boot_id=source_boot, nonce=nonce,
                    cutover_id=operation, runs=[], now=now)
                assert ledger.accept_cutover(operation, now=now)
                tx = host.transaction_record(operation, "a" * 40, nonce, source, target)
                tx.update(phase="replacement_started", source_container=source_cid)
                if kind != "legacy":
                    tx["admission_version"] = 1
                host.write_transaction(tx)
                accepted = ledger.ACCEPTED_PATH.read_bytes()
                (data / "ready").touch()

                if kind == "legacy":
                    # The old incident is still observable, but an unwrapped
                    # transaction now refuses automatic adoption/restart.
                    with ResponseBarrier(base / "proxy.sock", "DELETE", source_cid) as barrier:
                        with patch.context() as envpatch:
                            envpatch.setenv("DOCKER_HOST", f"unix://{barrier.path}")
                            try:
                                host.compose(config, "up", "-d", "--no-build", "--no-deps",
                                             "--force-recreate", "app", image=target, timeout=20)
                            except subprocess.TimeoutExpired:
                                assert barrier.accepted.is_set(), "DELETE was not accepted"
                            else:
                                raise AssertionError("same Compose call did not time out")
                    assert subprocess.run(["docker", "inspect", source_cid],
                                          capture_output=True, timeout=10).returncode != 0
                    _, image, health = host.container_health(config)
                    assert (image, health) == (target, "created")
                    host.reconcile()
                    assert host.read_json(host.STATUS)["code"] == "legacy_admission_unconfirmed"
                    assert host.TRANSACTION.exists()
                    assert ledger.ACCEPTED_PATH.read_bytes() == accepted
                    return

                if kind == "remove-timeout":
                    with ResponseBarrier(base / "proxy.sock", "DELETE", source_cid) as barrier:
                        def timeout_remove(args, **kwargs):
                            if args[1:3] == ["container", "rm"] and args[-1] == source_cid:
                                kwargs["env"] = {**os.environ, "DOCKER_HOST": f"unix://{barrier.path}"}
                            return original_docker(args, **kwargs)
                        patch.setattr(host, "docker_command", timeout_remove)
                        try:
                            host.prepare_admission(config, tx)
                        except subprocess.TimeoutExpired:
                            assert barrier.accepted.is_set(), "exact source remove not accepted"
                        else:
                            raise AssertionError("source remove response did not time out")
                        patch.setattr(host, "docker_command", original_docker)
                    assert subprocess.run(["docker", "inspect", source_cid],
                                          capture_output=True, timeout=10).returncode != 0
                    assert ledger.ACCEPTED_PATH.read_bytes() == accepted
                elif kind in {"start-timeout", "delayed-entry"}:
                    gate = host.prepare_admission(config, tx)
                    attempt = gate.allocate("target", uuid.uuid4().hex, target)
                    expected = host.wrapped_configuration(config, tx, "target",
                                                          attempt["token"], target)
                    target_cid = host.create_attempt(config, expected)
                    gate.bind("target", attempt["token"], target_cid)
                    lock = None
                    with ResponseBarrier(base / "proxy.sock", "POST", target_cid, "start") as barrier:
                        def timeout_start(args, **kwargs):
                            nonlocal lock
                            if args[1] == "start" and args[-1] == target_cid:
                                if kind == "delayed-entry":
                                    lock = (admission_root / operation / "lock").open("r+b")
                                    fcntl.flock(lock, fcntl.LOCK_EX)
                                kwargs["env"] = {**os.environ, "DOCKER_HOST": f"unix://{barrier.path}"}
                                try:
                                    return original_docker(args, **kwargs)
                                finally:
                                    if lock is not None:
                                        # Docker accepted Start while the wrapper
                                        # was blocked on its real gate lock. Fence
                                        # the exact old CID before it can enter.
                                        assert barrier.accepted.is_set()
                                        command("docker", "rm", "-f", target_cid, timeout=15)
                                        fcntl.flock(lock, fcntl.LOCK_UN)
                                        lock.close()
                                        lock = None
                            return original_docker(args, **kwargs)
                        patch.setattr(host, "docker_command", timeout_start)
                        try:
                            host.prepare_attempt(config, tx, "target")
                        except subprocess.TimeoutExpired:
                            assert barrier.accepted.is_set(), "Docker Start was not accepted"
                        else:
                            raise AssertionError("accepted Docker Start did not time out")
                        finally:
                            patch.setattr(host, "docker_command", original_docker)
                    if kind == "delayed-entry":
                        assert gate.observe()["slots"]["target"]["consumed"] is None
                        assert ledger.ACCEPTED_PATH.read_bytes() == accepted
                        host.prepare_attempt(config, tx, "target")
                        eventually(lambda: gate.observe()["slots"]["target"]["consumed"] is not None)
                        assert ledger.ACK_PATH.exists()
                    else:
                        eventually(lambda: gate.observe()["slots"]["target"]["consumed"] is not None)
                        assert ledger.ACK_PATH.exists()
                else:
                    raise AssertionError(kind)

                host.reconcile()
                status = host.read_json(host.STATUS)
                if kind in {"start-timeout", "delayed-entry"}:
                    assert status["state"] == "succeeded", status
                    assert ledger.ACK_PATH.exists()
                else:
                    assert status["state"] == "rolled_back", status
                    assert ledger.ACK_PATH.exists()
                assert not host.TRANSACTION.exists()
                first_boot = ledger.BOOT_PATH.read_bytes()
                host.reconcile()
                assert ledger.BOOT_PATH.read_bytes() == first_boot, "reconcile replayed a boot"
                if kind == "delayed-entry":
                    history = gate.observe()["slots"]["target"]["attempts"]
                    assert len(history) >= 2 and history[0]["fenced"]
                    assert history[0]["cid"] != history[-1]["cid"]
                    assert history[0]["token"] != history[-1]["token"]

            try:
                scenario("legacy-incident", "legacy")
                scenario("remove-ambiguity", "remove-timeout")
                scenario("accepted-start", "start-timeout")
                scenario("delayed-entry", "delayed-entry")
                print("real Docker/Compose, admission, and frozen ledger proof passed")
            finally:
                cleanup()  # stop mounted containers before deleting scratch data
    finally:
        patch.undo()
        cleanup()  # also covers interrupted image build


if __name__ == "__main__":
    main()
