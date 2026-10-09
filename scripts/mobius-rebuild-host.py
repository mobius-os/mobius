#!/usr/bin/env python3
"""Root-owned controller for one fixed Möbius container rebuild."""

from __future__ import annotations

import base64
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path("/var/lib/mobius-rebuild")
CONFIG = Path("/etc/mobius-rebuild/config.json")
COMPOSE = Path("/etc/mobius-rebuild/compose.yml")
OVERRIDE = Path("/etc/mobius-rebuild/image.override.yml")
STATUS = STATE_DIR / "status.json"
LOCK = STATE_DIR / "replace.lock"
IMAGES = STATE_DIR / "images.json"
# A replacement in progress, written before the running app is drained and
# removed only once its outcome is settled; reconcile() finishes or reverses
# whatever an interrupted worker left. Its schema is shared by every worker
# revision: keep it backward compatible.
TRANSACTION = STATE_DIR / "transaction.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OPERATION_RE = re.compile(r"^[0-9a-f]{32}$")
CONTAINER_RE = re.compile(r"^[0-9a-f]{12,64}$")
IMAGE = "ghcr.io/mobius-os/mobius"
IMAGE_SOURCE = "https://github.com/mobius-os/mobius"
ROLLBACK_TAG = f"{IMAGE}:mobius-rebuild-last-good"
# Helper-owned local tag pointed at the verified image ID right before Compose
# starts it, so a concurrent pull of the release tag cannot change what runs.
TARGET_TAG = f"{IMAGE}:mobius-rebuild-target"
ACTIVE_STATES = {"queued", "preparing", "replacing", "verifying"}
HANDOFF_VERSION = "external-cutover-v1"
# Version 2 requests carry the app's nonce, echoed as ``request_nonce`` so the
# app can tell its exact replacement's outcome from any earlier one.
REQUEST_VERSIONS = [1, 2]
# The frozen launcher (scripts/mobius-rebuild-launcher.py) adopts a worker from
# each requested official image only when this number is higher than every
# worker it has run. Increase it with every change to this file; never lower
# it. The launcher reads it as text, so keep it a plain literal on one line.
WORKER_REVISION = 12
# The frozen launcher runs the worker selected here; see offer_worker().
WORKERS = STATE_DIR / "workers"
WORKER_INDEX = STATE_DIR / "workers.json"
WORKER_IN_IMAGE = "/app/platform-baked/scripts/mobius-rebuild-host.py"
MAX_WORKER_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 4096
ADMISSION_CODE = Path("/usr/local/libexec/mobius-boot-admission.py")
ADMISSION_ROOT = STATE_DIR / "admission"
ADMISSION_CODE_TARGET = "/run/mobius-boot-admission.py"
ADMISSION_STATE_TARGET = "/run/mobius-admission"
COMPOSE_MUTATION_SECONDS = 300
TARGET_HEALTH_SECONDS = 600
ROLLBACK_HEALTH_SECONDS = 600
FAILED_TARGET_LOG = STATE_DIR / "failed-target.json"
FAILED_TARGET_LOG_BYTES = 32 * 1024
FAILED_TARGET_LOG_SECONDS = 5
_REVISION_LINE = re.compile(rb"^WORKER_REVISION\s*=.*$", re.MULTILINE)
_REVISION_EXACT = re.compile(rb"^WORKER_REVISION = ([1-9][0-9]{0,5})$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def acquire_lock(lock, timeout: float = 2.0) -> None:
    """Acquire the worker lock, allowing a boot reconciler to finish first."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def _atomic_json(path: Path, value: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def validate_config(value: dict, *, trusted_uid: int = 0) -> dict:
    if set(value) != {"version", "project", "data_dir"} or value["version"] != 3:
        raise ValueError("invalid fixed deployment configuration")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", str(value["project"])):
        raise ValueError("invalid Compose project")
    data_dir = Path(str(value["data_dir"]))
    control = data_dir / "mobius-rebuild"
    for path in (CONFIG.parent, CONFIG, COMPOSE, OVERRIDE, control):
        if path.is_symlink():
            raise ValueError("replacement configuration may not use symlinks")
        stat = path.stat()
        if stat.st_uid != trusted_uid or stat.st_mode & 0o022:
            raise ValueError("replacement configuration is not root-controlled")
    if not data_dir.is_absolute() or not data_dir.is_dir():
        raise ValueError("invalid persistent data directory")
    return {**value, "data_dir": data_dir, "control_dir": control}


def config() -> dict:
    return validate_config(read_json(CONFIG))












































def write_status(config_value: dict, **fields) -> dict:
    current = {
        "supported": True, "operation_id": None, "state": "idle",
        "expected_sha": None, "code": None, "message": None,
        "handoff": HANDOFF_VERSION,
    }
    try:
        current.update(read_json(STATUS))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    if "operation_id" in fields and fields["operation_id"] != current.get("operation_id"):
        current.update(failure_code=None, failure_detail=None, evidence_capture=None)
    current.update(fields)
    current["handoff"] = HANDOFF_VERSION
    current["request_versions"] = REQUEST_VERSIONS
    current["worker_revision"] = WORKER_REVISION
    launcher = os.environ.get("MOBIUS_REBUILD_LAUNCHER", "")
    current["launcher_revision"] = int(launcher) if launcher.isdigit() else None
    current.pop("runtime_overlay", None)
    current["updated_at"] = now()
    _atomic_json(STATUS, current)
    _atomic_json(config_value["control_dir"] / "status.json", current, 0o644)
    return current


def docker_command(args: list[str], *, timeout: float = 10, check: bool = True,
                   **kwargs) -> subprocess.CompletedProcess:
    """Bound Docker and its Compose plugin, including inherited output pipes."""
    if timeout <= 0:
        raise subprocess.TimeoutExpired(args, timeout)
    # Files, not pipes: a surviving descendant cannot keep communicate() open.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        with subprocess.Popen(args, stdout=out, stderr=err,
                              start_new_session=True, **kwargs) as process:
            try:
                process.wait(timeout=timeout)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                raise
            out.seek(0)
            err.seek(0)
            result = subprocess.CompletedProcess(
                args, process.returncode, out.read().decode(errors="replace"),
                err.read().decode(errors="replace"),
            )
    if check:
        result.check_returncode()
    return result


def _bounded_docker_logs(cid: str) -> tuple[bytes, bytes, bool, bool]:
    """Keep at most one small prefix of each Docker output stream in total.

    Pipes are drained only until the byte or wall-clock limit; the process
    group is then killed, including a Docker CLI descendant holding a pipe.
    A timed-out prefix is still useful evidence, but is always incomplete.
    """
    args = ["docker", "logs", "--timestamps", "--tail", "1000", cid]
    out, err = bytearray(), bytearray()
    deadline = time.monotonic() + FAILED_TARGET_LOG_SECONDS
    truncated = False
    timed_out = False
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               start_new_session=True)
    completed = False
    try:
        with selectors.DefaultSelector() as selector:
            for pipe, sink in ((process.stdout, out), (process.stderr, err)):
                selector.register(pipe, selectors.EVENT_READ, sink)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                ready = selector.select(remaining)
                if not ready:
                    timed_out = True
                    break
                for key, _ in ready:
                    budget = FAILED_TARGET_LOG_BYTES - len(out) - len(err)
                    if budget <= 0:
                        truncated = True
                        break
                    chunk = os.read(key.fd, min(4096, budget + 1))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        key.data.extend(chunk[:budget])
                        if len(chunk) > budget or len(out) + len(err) >= FAILED_TARGET_LOG_BYTES:
                            truncated = True
                            break
                if truncated:
                    break
        if timed_out or truncated or process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=1)
        completed = True
        if process.returncode and not (truncated or timed_out):
            raise subprocess.CalledProcessError(process.returncode, args)
        return bytes(out), bytes(err), truncated or timed_out, timed_out
    finally:
        if not completed:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=1)
        process.stdout.close()
        process.stderr.close()


def capture_failed_target(operation: str, cid: str, image: str) -> str:
    """Best-effort, root-private evidence for this journaled target only."""
    if not CONTAINER_RE.fullmatch(cid):
        return "failed"
    try:
        try:
            previous = (read_json(FAILED_TARGET_LOG)
                        if FAILED_TARGET_LOG.stat().st_size <= 128 * 1024 else {})
        except (OSError, ValueError):
            previous = {}
        if previous.get("operation_id") == operation and previous.get("container_id") == cid:
            return "saved"  # interrupted rollback replay must not erase evidence
        stdout, stderr, truncated, timed_out = _bounded_docker_logs(cid)
        _atomic_json(FAILED_TARGET_LOG, {
            "operation_id": operation, "container_id": cid, "target_image": image[:128],
            "captured_at": now(), "truncated": truncated, "timed_out": timed_out,
            "stdout_b64": base64.b64encode(stdout).decode("ascii"),
            "stderr_b64": base64.b64encode(stderr).decode("ascii"),
        })
        return "saved"
    except Exception:
        return "failed"


def compose(config_value: dict, *args: str, image: str | None = None,
            check: bool = True, timeout: float = 30) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["MOBIUS_IMAGE"] = image or IMAGE
    return docker_command(
        ["docker", "compose", "-p", config_value["project"],
         "-f", str(COMPOSE), "-f", str(OVERRIDE), *args],
        cwd=CONFIG.parent, env=env, check=check, timeout=timeout,
    )


def admission_store(config_value: dict, transaction: dict):
    """Load the installer-pinned gate, never code from the writable platform."""
    for path in (ADMISSION_CODE.parent, ADMISSION_CODE, ADMISSION_ROOT):
        item = path.lstat()
        if path.is_symlink() or item.st_uid != 0 or item.st_mode & 0o022:
            raise RuntimeError("the boot admission helper is not root-controlled; reinstall it")
    spec = importlib.util.spec_from_file_location("mobius_boot_admission", ADMISSION_CODE)
    if spec is None or spec.loader is None:
        raise RuntimeError("boot admission helper is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.AdmissionStore(
        ADMISSION_ROOT / transaction["operation_id"], config_value["data_dir"],
    )


def wrapped_configuration(config_value: dict, transaction: dict, role: str,
                          token: str, image: str) -> dict:
    """Preserve the resolved command and PID-1 chain behind a root boot gate."""
    image_config = json.loads(inspect_image(image, "{{json .Config}}"))
    resolved = transaction.get("target_topology" if role == "target" else "source_topology")
    if resolved is None:
        env = {**os.environ, "MOBIUS_IMAGE": image}
        result = docker_command(
            ["docker", "compose", "-p", config_value["project"], "-f", str(COMPOSE),
             "config", "--format", "json"], env=env, cwd=CONFIG.parent,
        )
        resolved = json.loads(result.stdout)
    service = resolved["services"]["app"]
    entrypoint = service.get("entrypoint")
    command = service.get("command")
    if command is None:
        # An explicit Compose entrypoint suppresses the image CMD. Preserve
        # that contract before replacing the entrypoint with admission.
        command = (image_config.get("Cmd") or []) if entrypoint is None else []
    if entrypoint is None:
        entrypoint = image_config.get("Entrypoint") or []
    if (not isinstance(entrypoint, list) or not isinstance(command, list)
            or not entrypoint + command
            or any(not isinstance(value, str) or "\0" in value for value in entrypoint + command)):
        raise RuntimeError("cannot preserve the image's original startup command")
    data_mounts = [v for v in service.get("volumes", []) if v.get("target") == "/data"]
    if len(data_mounts) != 1 or data_mounts[0].get("read_only", False):
        raise RuntimeError("replacement must preserve one writable /data mount")
    data_mount = data_mounts[0]
    if data_mount.get("type") == "bind":
        data_source = data_mount.get("source", "")
    elif data_mount.get("type") == "volume":
        volume = resolved.get("volumes", {}).get(data_mount.get("source"), {})
        name = volume.get("name")
        if not name:
            raise RuntimeError("persistent data volume has no immutable resource name")
        data_source = docker_command(["docker", "volume", "inspect", "--format", "{{.Mountpoint}}", name]).stdout.strip()
    else:
        raise RuntimeError("replacement cannot preserve this /data mount type")
    if os.path.realpath(data_source) != os.path.realpath(config_value["data_dir"]):
        raise RuntimeError("replacement /data mount differs from the trusted deployment")
    prefix = ["python3", "-I", "-S", ADMISSION_CODE_TARGET, "enter",
              "--state-dir", ADMISSION_STATE_TARGET, "--data-dir", "/data",
              "--token", token, "--"]
    app = {**service,
        "image": image,
        "container_name": "mobius-admission-" + token,
        "entrypoint": prefix + entrypoint,
        "command": command,
        "labels": {**service.get("labels", {}),
                   "io.mobius.admission.project": config_value["project"],
                   "io.mobius.admission.operation": transaction["operation_id"],
                   "io.mobius.admission.role": role, "io.mobius.admission.token": token},
        "volumes": [*service.get("volumes", []),
            {"type": "bind", "source": str(ADMISSION_CODE),
             "target": ADMISSION_CODE_TARGET, "read_only": True},
            {"type": "bind", "source": str(ADMISSION_ROOT / transaction["operation_id"]),
             "target": ADMISSION_STATE_TARGET, "read_only": False},
        ],
    }
    # Compose must never reconcile other attempts or own shared resources.
    # Its per-generation project contains one app and references the original
    # deployment's named resources externally. Delayed old Create calls cannot
    # make a later project's scale-down touch the admitted successor.
    for key in ("build", "depends_on", "links", "volumes_from"):
        if key in {"links", "volumes_from"} and app.get(key):
            raise RuntimeError("cross-service container references need an explicit topology migration")
        app.pop(key, None)
    for key in ("network_mode", "pid", "ipc"):
        if str(app.get(key, "")).startswith("service:"):
            raise RuntimeError("shared service namespaces need an explicit topology migration")
    networks = {}
    for key, value in resolved.get("networks", {}).items():
        if key in app.get("networks", {}):
            networks[key] = {"name": value["name"], "external": True}
    volumes = {}
    for volume in app.get("volumes", []):
        if volume.get("type") == "volume":
            name = volume.get("source")
            if not name or name not in resolved.get("volumes", {}):
                raise RuntimeError("anonymous app volumes cannot be preserved across recovery")
            volumes[name] = {"name": resolved["volumes"][name]["name"], "external": True}
    result = {"name": app["container_name"], "services": {"app": app},
              "networks": networks, "volumes": volumes}
    if app.get("configs") or app.get("secrets"):
        raise RuntimeError("app configs/secrets require an explicit preserved-resource topology")
    return result


def verify_wrapped_container(cid: str, expected: dict) -> dict:
    """Bind admission only to the exact created container and frozen wrapper."""
    result = docker_command(["docker", "container", "inspect", "--format", "{{json .}}", cid])
    item = json.loads(result.stdout)
    config_value = item.get("Config") or {}
    app = expected["services"]["app"]
    if (not re.fullmatch(r"[0-9a-f]{64}", cid) or item.get("Id") != cid
            or item.get("Image") != app["image"]
            or config_value.get("Entrypoint") != app["entrypoint"]
            or config_value.get("Cmd") != app["command"]
            or any((config_value.get("Labels") or {}).get(k) != v
                   for k, v in app["labels"].items())
            or item.get("Name") != "/" + app["container_name"]):
        raise RuntimeError("container identity or pre-entrypoint admission coverage is unconfirmed")
    mounts = {mount.get("Destination"): mount for mount in item.get("Mounts", [])}
    for volume in app["volumes"]:
        actual = mounts.get(volume["target"], {})
        if volume["type"] == "volume":
            name = expected["volumes"][volume["source"]]["name"]
            if actual.get("Type") != "volume" or actual.get("Name") != name:
                raise RuntimeError("container volume identity changed")
            continue
        if (actual.get("Type") != "bind" or actual.get("Source") != volume["source"]
                or actual.get("RW") != (not volume.get("read_only", False))):
            raise RuntimeError("container admission mount does not match the root-controlled gate")
    return item


def remove_exact_container(cid: str) -> None:
    """Fence one immutable Docker resource; transport errors never mean absent."""
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise RuntimeError("container removal requires an exact full ID")
    result = docker_command(["docker", "container", "rm", "--force", cid],
                            timeout=COMPOSE_MUTATION_SECONDS, check=False)
    if result.returncode == 0:
        return
    # Docker's exact-resource not-found response also fences a late start of
    # that ID. Never interpret generic inspection/list failures as this proof.
    message = result.stderr.strip().lower()
    if result.returncode == 1 and message in {
            f"error response from daemon: no such container: {cid}",
            f"error response from daemon: no such object: {cid}"}:
        return
    result.check_returncode()


def inspect_image(image: str, template: str) -> str:
    result = docker_command(
        ["docker", "image", "inspect", "--format", template, image],
        timeout=30,
    )
    return result.stdout.strip()


def current_attempt_name() -> str | None:
    try:
        value = read_json(OVERRIDE)
    except (OSError, ValueError):
        return None
    app = value.get("services", {}).get("app", {})
    token = app.get("labels", {}).get("io.mobius.admission.token")
    if token is None:
        return None
    name = app.get("container_name")
    if not re.fullmatch(r"[0-9a-f]{32}", str(token)) or name != "mobius-admission-" + token:
        raise ValueError("invalid current admission identity")
    return name


def app_container(config_value: dict) -> tuple[str, str]:
    name = current_attempt_name()
    if name:
        result = docker_command(["docker", "container", "inspect", "--format", "{{.Id}}", name])
    else:
        result = compose(config_value, "ps", "-q", "app")
    cid = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{64}", cid):
        raise RuntimeError("the app container identity is missing or ambiguous")
    if not cid:
        raise RuntimeError("the recorded Möbius app container is not running")
    inspected = docker_command(
        ["docker", "container", "inspect", "--format", "{{.Image}}", cid],
        timeout=30,
    )
    return cid, inspected.stdout.strip()




def container_health(config_value: dict, timeout: float = 10) -> tuple[str, str, str]:
    """Re-discover after a stale ID; errors never mean the container is absent.

    Compose can delete/rename containers between discovery and inspect. Retry
    the whole observation, within the same two-query budget, not the old ID.
    Multiple candidates are ambiguous until Compose finishes its mutation.
    """
    deadline = time.monotonic() + 2 * timeout
    for attempt in range(3):
        try:
            budget = min(timeout, max(0, deadline - time.monotonic()) / 2)
            if budget <= 0:
                raise subprocess.TimeoutExpired("Docker container observation", 2 * timeout)
            name = current_attempt_name()
            if name:
                result = docker_command(["docker", "container", "inspect", "--format", "{{.Id}}", name],
                                        timeout=budget, check=False)
                if result.returncode:
                    if result.returncode == 1 and result.stderr.strip().lower() in {
                            f"error response from daemon: no such container: {name}",
                            f"error response from daemon: no such object: {name}"}:
                        return "", "", "missing"
                    result.check_returncode()
            else:
                result = compose(config_value, "ps", "-a", "-q", "app", timeout=budget)
            ids = result.stdout.split()
            if not ids:
                return "", "", "missing"
            if len(ids) != 1:
                raise ValueError("Compose app container identity is ambiguous")
            cid = ids[0]
            probe = docker_command(
                ["docker", "container", "inspect", "--format",
                 "{{.Image}} {{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}", cid],
                check=True, timeout=min(timeout, max(0, deadline - time.monotonic())),
            )
            image, state, *health = probe.stdout.strip().split()
            return cid, image, (health[0] if state == "running" and health else state)
        except (OSError, ValueError, subprocess.SubprocessError):
            if attempt == 2 or time.monotonic() >= deadline:
                raise
            time.sleep(min(1, max(0, deadline - time.monotonic())))


def wait_healthy(config_value: dict, timeout: int = TARGET_HEALTH_SECONDS) -> bool:
    """False means not serviceable within budget, not proof of a crashed boot."""
    deadline = time.monotonic() + timeout
    probe_failed = False
    while time.monotonic() < deadline:
        try:
            _cid, _image, health = container_health(
                config_value, timeout=min(10, max(0, deadline - time.monotonic()) / 2),
            )
            probe_failed = False
            if health == "healthy":
                return True
        except (OSError, ValueError, subprocess.SubprocessError):
            # A transient daemon/Compose query error is not a failed boot.
            probe_failed = True
        time.sleep(min(3, max(0, deadline - time.monotonic())))
    if probe_failed:
        raise subprocess.TimeoutExpired("Docker health observation", timeout)
    return False


class ProvenanceRejected(RuntimeError):
    """A well-formed observation proves the served image is not acceptable."""


class ProvenanceUnconfirmed(RuntimeError):
    """The served image could not be proved either correct or incorrect."""


def verify_served_generation(cid: str, expected_sha: str) -> None:
    """Prove the running container serves the requested immutable image."""
    try:
        result = docker_command(
            ["docker", "exec", cid, "curl", "-fsS",
             "http://127.0.0.1:8000/api/version"],
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ProvenanceUnconfirmed("build provenance could not be observed") from exc
    if result.returncode != 0:
        raise ProvenanceUnconfirmed("build provenance could not be observed")
    try:
        version = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProvenanceUnconfirmed("the container returned invalid build provenance") from exc
    if (not isinstance(version, dict) or not isinstance(version.get("sha"), str)
            or not SHA_RE.fullmatch(version["sha"])):
        raise ProvenanceUnconfirmed("the container returned invalid build provenance")
    if version["sha"] != expected_sha:
        raise ProvenanceRejected("the container is not serving the requested image revision")
    try:
        mounts = docker_command(
            ["docker", "container", "inspect", "--format", "{{json .Mounts}}", cid],
            check=True,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise ProvenanceUnconfirmed("mount provenance could not be observed") from exc
    try:
        mounted = json.loads(mounts.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ProvenanceUnconfirmed("the container returned invalid mount provenance") from exc
    if not isinstance(mounted, list) or any(
            not isinstance(item, dict) or not isinstance(item.get("Destination"), str)
            for item in mounted):
        raise ProvenanceUnconfirmed("the container returned invalid mount provenance")
    if any(item.get("Destination") == "/app/runtime" for item in mounted):
        raise ProvenanceRejected("the container is not using the image's protected runtime")


def _docker_root() -> Path:
    result = docker_command(
        ["docker", "info", "--format", "{{.DockerRootDir}}"],
        timeout=30,
    )
    return Path(result.stdout.strip())


def require_pull_space(current_image: str) -> None:
    current_size = int(inspect_image(current_image, "{{.Size}}"))
    required = max(512 * 1024 * 1024, current_size)
    free = shutil.disk_usage(_docker_root()).free
    if free < required:
        raise RuntimeError(
            f"not enough Docker storage to pull safely (need {required // 1048576} MiB free)"
        )


def restart_ledger(config_value: dict, cid: str, command: str,
                   operation: str, *, image: str | None = None) -> bool:
    """Run one root ledger command, even when the app is crash-looping."""
    if command == "rearm-cutover":
        raise RuntimeError("only the boot admission gate may prepare recovery authorization")
    invocation = ["python3", "-P", "/app/runtime/restart_ledger.py",
                  command, operation]
    result = docker_command(
        ["docker", "exec", cid, *invocation],
        check=False,
    )
    if result.returncode == 0:
        return True
    if not image:
        return False
    # A failed replacement may not stay alive long enough for docker exec.
    # The prior verified image carries the same frozen helper; mount only the
    # persistent data root and run no entrypoint or application code.
    result = docker_command(
        ["docker", "run", "--rm", "--mount",
         f"type=bind,src={config_value['data_dir']},dst=/data",
         "--entrypoint", "python3", image, *invocation[1:]],
        check=False,
    )
    return result.returncode == 0


def request_drain(config_value: dict, operation: str, cid: str) -> None:
    if not restart_ledger(config_value, cid, "open-cutover", operation):
        raise RuntimeError("the running image does not support safe Host cutover")
    result = docker_command(
        ["docker", "exec", cid, "python3",
         "/data/platform/backend/scripts/prepare-container-cutover.py", operation],
        check=False, timeout=90,
    )
    if result.returncode != 0:
        raise RuntimeError("the running server could not complete a safe chat drain")
    if not restart_ledger(config_value, cid, "accept-cutover", operation):
        raise RuntimeError("the root supervisor did not accept the chat handoff")


def _image_state() -> dict:
    try:
        value = read_json(IMAGES)
        refs = value.get("sha_refs", [])
        rollback_id = value.get("rollback_image_id")
        return {
            "sha_refs": [
                ref for ref in refs
                if isinstance(ref, str) and ref.startswith(f"{IMAGE}:sha-")
            ],
            "rollback_image_id": (
                rollback_id
                if isinstance(rollback_id, str) and rollback_id.startswith("sha256:")
                else None
            ),
        }
    except (OSError, ValueError, json.JSONDecodeError):
        return {"sha_refs": [], "rollback_image_id": None}


def record_pulled_image(target_ref: str) -> None:
    state = _image_state()
    refs = sorted({*state["sha_refs"], target_ref})
    _atomic_json(IMAGES, {**state, "sha_refs": refs})


def discard_pulled_image(target_ref: str) -> None:
    state = _image_state()
    docker_command(["docker", "image", "rm", target_ref], check=False, timeout=30)
    _atomic_json(IMAGES, {
        **state,
        "sha_refs": [ref for ref in state["sha_refs"] if ref != target_ref],
    })


def retain_images(target_ref: str, rollback_image_id: str | None = None) -> None:
    state = _image_state()
    obsolete = [ref for ref in state["sha_refs"] if ref != target_ref]
    old_rollback = state["rollback_image_id"]
    if old_rollback and old_rollback != rollback_image_id:
        obsolete.append(old_rollback)
    if obsolete:
        # One bounded, non-force removal; references still used by containers
        # fail harmlessly. Never remove images outside our recorded references.
        docker_command(["docker", "image", "rm", *obsolete], check=False)
    _atomic_json(IMAGES, {
        "sha_refs": [target_ref],
        "rollback_tag": ROLLBACK_TAG,
        "rollback_image_id": rollback_image_id,
    })


def never_started(cid: str, image: str) -> bool:
    """A created container is retryable only before its first actual boot."""
    result = docker_command(["docker", "container", "inspect", "--format",
                             "{{json .}}", cid])
    item = json.loads(result.stdout)
    if not isinstance(item, dict) or not isinstance(item.get("State"), dict):
        raise ValueError("invalid Docker container state")
    state = item["State"]
    return (item.get("Id") == cid and item.get("Image") == image
            and state.get("Status") == "created"
            and state.get("StartedAt") == "0001-01-01T00:00:00Z"
            and item.get("RestartCount") == 0)


def discover_attempt(expected: dict) -> str:
    """A generation's unique Docker name makes no-start creation idempotent."""
    name = expected["services"]["app"]["container_name"]
    result = docker_command(["docker", "container", "inspect", "--format", "{{.Id}}", name],
                            check=False)
    if result.returncode:
        message = result.stderr.strip().lower()
        if result.returncode == 1 and message in {
                f"error response from daemon: no such container: {name}",
                f"error response from daemon: no such object: {name}"}:
            return ""
        result.check_returncode()
    cid = result.stdout.strip()
    verify_wrapped_container(cid, expected)
    return cid


def create_attempt(config_value: dict, expected: dict) -> str:
    directory = CONFIG.parent / "attempts"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if directory.is_symlink() or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise RuntimeError("untrusted attempt configuration directory")
    token = expected["services"]["app"]["labels"]["io.mobius.admission.token"]
    path = directory / (token + ".json")
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077 or info.st_nlink != 1 or read_json(path) != expected):
            raise RuntimeError("immutable attempt configuration changed")
    else:
        _durable_write(path, json.dumps(expected).encode(), 0o600)
    _durable_write(OVERRIDE, json.dumps(expected).encode(), 0o600)
    cid = discover_attempt(expected)
    if not cid:
        docker_command(["docker", "compose", "-p", expected["name"], "-f", str(path),
                        "up", "--no-start", "--no-recreate", "--no-build", "--no-deps", "app"],
                       cwd=CONFIG.parent, timeout=COMPOSE_MUTATION_SECONDS)
        cid = discover_attempt(expected)
    if not cid:
        raise RuntimeError("created container identity is not yet observable")
    return cid


def prepare_attempt(config_value: dict, transaction: dict, role: str) -> str:
    """Allocate/create/bind/start once; never repeat an ambiguous start.

    Creation uses one immutable name and --no-recreate, without starting. An
    interrupted create can therefore be discovered or repeated for that same
    generation. Only a durably CLOSED and exact-ID-fenced attempt can be
    replaced. Admission winning that race permanently changes this to observe.
    """
    gate = admission_store(config_value, transaction)
    state = gate.observe()
    slot = state["slots"][role]
    if slot["consumed"]:
        return slot["attempts"][-1]["cid"]
    attempt = slot["attempts"][-1] if slot["attempts"] else None
    image = transaction["target_image" if role == "target" else "previous_image"]
    if attempt and (attempt["issued"] or attempt["state"] == "closed"):
        if not gate.close_pending(role, attempt["token"]):
            return attempt["cid"]  # the wrapper won admission; do not touch it
        if not attempt["cid"]:
            raise RuntimeError("closed creation has no confirmed container identity")
        remove_exact_container(attempt["cid"])
        gate.fenced(role, attempt["token"], attempt["cid"])
        attempt = None
    if attempt is None:
        attempt = gate.allocate(role, uuid.uuid4().hex, image)
    expected = wrapped_configuration(config_value, transaction, role, attempt["token"], image)
    _durable_write(OVERRIDE, json.dumps(expected).encode(), 0o600)
    cid = attempt["cid"]
    if cid is None:
        cid = create_attempt(config_value, expected)
        gate.bind(role, attempt["token"], cid)
    else:
        verify_wrapped_container(cid, expected)
    if not never_started(cid, image):
        # A wrapper invocation before OPEN was denied. Do not infer admission
        # or reuse that process; only explicit closed cancellation may retry.
        if gate.close_pending(role, attempt["token"]):
            remove_exact_container(cid)
            gate.fenced(role, attempt["token"], cid)
        raise RuntimeError("pre-admission container already ran; cancellation recorded")
    gate.open(role, attempt["token"])
    if not gate.issue_start(role, attempt["token"]):
        raise RuntimeError("start was already issued; reconcile its admission")
    docker_command(["docker", "start", cid], timeout=COMPOSE_MUTATION_SECONDS)
    return cid


def quiesce_target(config_value: dict, transaction: dict) -> None:
    """Fence every known target before granting the separate rollback slot."""
    gate = admission_store(config_value, transaction)
    state = gate.observe()
    for attempt in state["slots"]["target"]["attempts"]:
        if attempt["cid"] in state["quiesced"]:
            continue
        if attempt["state"] != "admitted":
            # Persist revocation before any removal; a crash releases locks,
            # but cannot let a delayed target wrapper enter the legacy ledger.
            if not gate.close_pending("target", attempt["token"]):
                attempt = gate.observe()["slots"]["target"]["attempts"][-1]
        cid = attempt["cid"]
        if cid is None:
            expected = wrapped_configuration(config_value, transaction, "target",
                                             attempt["token"], attempt["image"])
            # Complete/reobserve the same named no-start creation while its
            # gate is CLOSED. Even a delayed old daemon Create stays denied.
            cid = create_attempt(config_value, expected)
            gate.bind("target", attempt["token"], cid)
        evidence = capture_failed_target(transaction["operation_id"], cid, attempt["image"])
        try:
            write_status(config_value, evidence_capture=evidence)
        except OSError:
            pass  # diagnostics cannot hold service recovery hostage
        remove_exact_container(cid)
        if attempt["state"] == "admitted":
            gate.quiesce(cid)
        else:
            gate.fenced("target", attempt["token"], cid)


def prepare_admission(config_value: dict, transaction: dict):
    gate = admission_store(config_value, transaction)
    # A missing receipt after a failed drain must not authorize removal of a
    # still-serviceable source. A completed drain or exact evidence that the
    # source is already down permits service-only restoration, never handoff.
    service_only = transaction.get("drain_confirmed") is True
    if not service_only:
        cid = transaction["source_container"]
        try:
            result = docker_command(["docker", "container", "inspect", "--format", "{{json .}}", cid],
                                    check=False)
            if result.returncode == 0:
                item = json.loads(result.stdout)
                service_only = (item.get("Id") == cid and item.get("Image") == transaction["previous_image"]
                                and item.get("State", {}).get("Status") in {"created", "exited", "dead", "restarting"})
            else:
                service_only = result.returncode == 1 and result.stderr.strip().lower() in {
                    f"error response from daemon: no such container: {cid}",
                    f"error response from daemon: no such object: {cid}"}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass  # Unknown source state never grants service-only cutover.
    state = gate.prepare_for_recovery(transaction["operation_id"], transaction["source_container"],
                                      transaction["previous_image"], allow_service_only=service_only)
    if state["operation"] != transaction["operation_id"] or state["source"]["cid"] != transaction["source_container"]:
        raise RuntimeError("admission and replacement journals disagree")
    if state["source"]["cid"] not in state["quiesced"]:
        remove_exact_container(state["source"]["cid"])
        gate.quiesce(state["source"]["cid"])
    return gate


def prepare_rollback(config_value: dict, transaction: dict) -> str:
    if transaction.get("admission_version") != 1:
        raise RuntimeError("unwrapped legacy rollback requires explicit Host recovery")
    prepare_admission(config_value, transaction)
    quiesce_target(config_value, transaction)
    return prepare_attempt(config_value, transaction, "rollback")


def rollback(config_value: dict, operation: str, expected: str,
             code: str, detail: str, previous_image: str | None = None) -> int:
    transaction = read_transaction()
    if (transaction is None or transaction["operation_id"] != operation
            or transaction["expected_sha"] != expected
            or (previous_image is not None and previous_image != transaction["previous_image"])):
        raise RuntimeError("rollback does not own the replacement transaction")
    if transaction.get("outcome"):
        promote_topology(transaction)
        write_status(config_value, **transaction["outcome"])
        clear_transaction()
        return 0 if transaction["outcome"]["state"] == "succeeded" else 1
    # Preserve the original failure, except when later evidence resolves an
    # earlier unknown observation into a definite replacement failure.
    if not (transaction.get("failure_code") == "observation_unconfirmed"
            and code in {"replacement_failed", "readiness_budget_exhausted"}):
        code = transaction.get("failure_code", code)
        detail = transaction.get("failure_detail", detail)
    transaction.update(failure_code=code, failure_detail=detail)
    write_transaction(transaction)
    if transaction.get("admission_version") != 1:
        # Observe an old already-running recovery, but never retrofit a gate
        # to an issued unwrapped start or reconstruct its consumed authority.
        cid, image, health = container_health(config_value)
        if (transaction.get("phase") != "replacement_started"
                and image == transaction["previous_image"] and health == "healthy"):
            finish_verified(config_value, transaction, cid, image, state="rolled_back",
                            code=code, message=f"The previous container was restored: {detail}")
        else:
            status_code = ("rollback_wrong_image" if image and
                           (image not in {transaction["previous_image"], transaction.get("target_image")}
                            or (transaction.get("phase") == "rollback_started"
                                and image != transaction["previous_image"]))
                           else "legacy_admission_unconfirmed")
            write_status(config_value, state="needs_recovery", code=status_code,
                         message="An unwrapped interrupted replacement needs explicit Host recovery; "
                                 "its restart permission cannot be reconstructed.")
        return 1
    transaction["phase"] = "rollback_started"
    write_transaction(transaction)
    write_status(config_value, operation_id=operation, state="verifying", expected_sha=expected,
                 request_nonce=transaction.get("request_nonce"), code=code,
                 message="Replacement failed; restoring the previous container.")
    cid = prepare_rollback(config_value, transaction)
    if wait_healthy(config_value, ROLLBACK_HEALTH_SECONDS):
        current_cid, image, health = container_health(config_value)
        if current_cid != cid or image != transaction["previous_image"] or health != "healthy":
            raise RuntimeError("rollback identity or readiness changed")
        finish_verified(config_value, transaction, cid, image, state="rolled_back",
                        code=code, message=f"The previous container was restored: {detail}")
    else:
        write_status(config_value, state="needs_recovery", code="rollback_failed",
                     message="The admitted rollback is not yet healthy; recovery will observe it.")
    return 1


def worker_revision(source: bytes) -> int | None:
    """A worker's declared revision, read as text and never by running it:
    0 for a worker from before revisions, None when the declaration is
    malformed or repeated."""
    lines = _REVISION_LINE.findall(source)
    if not lines:
        return 0
    match = _REVISION_EXACT.fullmatch(lines[0].rstrip(b"\r"))
    return int(match[1]) if len(lines) == 1 and match else None


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _durable_write(path: Path, data: bytes, mode: int) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
        _fsync_dir(path.parent)
    finally:
        Path(name).unlink(missing_ok=True)


def _worker_entry(source: bytes, revision: int, origin: str) -> dict:
    """Store ``source`` durably and describe it for the selection record."""
    WORKERS.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(WORKERS, 0o700)
    digest = hashlib.sha256(source).hexdigest()
    name = f"{revision}-{digest[:16]}.py"
    _durable_write(WORKERS / name, source, 0o700)
    return {"revision": revision, "file": name, "sha256": digest,
            "origin": origin[:80]}


def _publish_index(index: dict) -> None:
    """Publish the selection record. Worker files are small and never
    deleted: a running candidate and the record may still name any of them."""
    _durable_write(
        WORKER_INDEX, json.dumps(index, separators=(",", ":")).encode(), 0o600,
    )


def _checked(source: bytes) -> tuple[int | None, str | None]:
    """The revision of a worker that may be adopted, or why it may not."""
    revision = worker_revision(source)
    if revision is None:
        return None, "rejected: malformed WORKER_REVISION"
    if revision == 0:
        return None, "not adopted: the image predates self-updating workers"
    if len(source) > MAX_WORKER_BYTES:
        return None, "rejected: worker too large"
    try:
        compile(source, "mobius-rebuild-host.py", "exec", dont_inherit=True)
    except (SyntaxError, ValueError) as exc:
        return None, f"rejected: worker does not compile ({exc.__class__.__name__})"
    return revision, None


def offer_worker(source: bytes, origin: str) -> str:
    """Offer a verified image's worker to the launcher as its candidate.

    Only a revision above every revision offered or installed so far is
    offered, so a worker the launcher tried and dropped is never offered
    again, and no official image can bring back an older one. Returns a short
    outcome for status."""
    if not os.environ.get("MOBIUS_REBUILD_LAUNCHER"):
        return "not adopted: this helper predates the launcher"
    revision, refusal = _checked(source)
    if refusal:
        return refusal
    try:
        index = json.loads(WORKER_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "not adopted: the launcher's worker record is unreadable"
    high_water = int(index.get("high_water", 0))
    if revision <= high_water:
        return f"not adopted: revision {revision} is not newer than {high_water}"
    entry = _worker_entry(source, revision, origin)
    _publish_index({**index, "high_water": revision, "candidate": entry})
    return f"offered: revision {revision} runs the next replacement"


def seed_worker(source: bytes) -> str:
    """Activate trusted checkout bytes without downgrading or retrying a trial.

    A still-pending exact candidate has never been tried: the launcher removes
    it before a trial. Only that candidate may be activated at high_water.
    """
    revision, refusal = _checked(source)
    if refusal:
        return refusal
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        index = json.loads(WORKER_INDEX.read_text(encoding="utf-8"))
    except FileNotFoundError:
        index = {"version": 1, "high_water": 0, "active": None, "candidate": None}
    digest = hashlib.sha256(source).hexdigest()
    active = index.get("active") or {}
    candidate = index.get("candidate") or {}
    high_water = int(index.get("high_water", 0))
    if active.get("sha256") == digest:
        return f"current: revision {revision}"
    if int(active.get("revision", 0)) > revision:
        return f"kept: a newer worker (revision {active['revision']}) is installed"
    pending_exact = (
        revision == high_water
        and candidate.get("revision") == revision
        and candidate.get("sha256") == digest
    )
    if revision <= high_water and not pending_exact:
        return (f"rejected: revision {revision} was already tried or superseded; "
                "install a higher worker revision")
    entry = _worker_entry(source, revision, "checkout")
    _publish_index({**index, "high_water": max(high_water, revision), "active": entry,
                    "candidate": None})
    return f"installed: revision {revision}"


def adopt_self() -> int:
    """The installer's locked seed operation; success means a usable active worker."""
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        if os.environ.get("MOBIUS_REBUILD_LOCK_HELD") != "1":
            fcntl.flock(lock, fcntl.LOCK_EX)
        outcome = seed_worker(Path(__file__).read_bytes())
    print(outcome)
    return 0 if outcome.startswith(("installed:", "current:", "kept:")) else 1


def _bounded_output(args: list[str], limit: int, timeout: float = 300) -> bytes:
    """Run ``args`` and collect at most ``limit`` bytes of its output within
    ``timeout`` seconds; the child is killed as soon as either is exceeded."""
    import select

    deadline = time.monotonic() + timeout
    chunks: list[bytes] = []
    size = 0
    with subprocess.Popen(args, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        try:
            assert process.stdout is not None
            fd = process.stdout.fileno()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("reading the image's worker timed out")
                if not select.select([fd], [], [], remaining)[0]:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise RuntimeError("the image's worker is too large")
                chunks.append(chunk)
            if process.wait(timeout=max(deadline - time.monotonic(), 0.1)) != 0:
                raise RuntimeError("the image's worker could not be read")
        except BaseException:
            process.kill()
            raise
    return b"".join(chunks)


def worker_from_image(image_id: str) -> bytes:
    """The worker inside the exact verified image, without starting it."""
    import io
    import tarfile

    created = docker_command(
        ["docker", "create", "--network", "none", "--entrypoint", "/bin/false",
         "--label", "mobius-rebuild.worker-extract=1", image_id],
        check=True, timeout=30,
    )
    cid = created.stdout.strip()
    try:
        archive = _bounded_output(
            ["docker", "cp", f"{cid}:{WORKER_IN_IMAGE}", "-"],
            MAX_WORKER_BYTES + 64 * 1024, timeout=30,
        )
    finally:
        docker_command(["docker", "rm", "-f", "-v", cid], check=False, timeout=30)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            members = tar.getmembers()
            if (len(members) != 1 or members[0].type != tarfile.REGTYPE
                    or members[0].name != Path(WORKER_IN_IMAGE).name
                    or members[0].size > MAX_WORKER_BYTES):
                raise RuntimeError("the image's worker is not one regular file")
            handle = tar.extractfile(members[0])
            source = handle.read(MAX_WORKER_BYTES + 1) if handle else b""
    except tarfile.TarError as exc:
        raise RuntimeError("the image's worker archive is invalid") from exc
    if len(source) != members[0].size:
        raise RuntimeError("the image's worker archive is truncated")
    return source


def adopt_from_image(image_id: str) -> str:
    """After a verified replacement, adopt the worker that image carries."""
    try:
        return offer_worker(worker_from_image(image_id), image_id)
    except Exception as exc:  # adoption never fails a finished replacement
        return f"not adopted: {str(exc)[:160]}"


def parse_request(payload: dict) -> tuple[str, str | None]:
    """The requested target and, for a version 2 request, the app's nonce."""
    version = payload.get("version")
    keys = {"version", "expected_sha"} | ({"nonce"} if version == 2 else set())
    if version not in REQUEST_VERSIONS or set(payload) != keys:
        raise ValueError("invalid replacement request")
    expected = str(payload["expected_sha"])
    if not SHA_RE.fullmatch(expected):
        raise ValueError("invalid replacement target")
    nonce = str(payload["nonce"]) if version == 2 else None
    if nonce is not None and not OPERATION_RE.fullmatch(nonce):
        raise ValueError("invalid replacement nonce")
    return expected, nonce


def read_request(request: Path) -> tuple[dict | None, tuple[int, int, bytes] | None]:
    """The queued request's payload (None when unreadable) and the identity of
    the exact file read (device, inode, bytes), or ``(None, None)`` when
    nothing is queued."""
    try:
        info = os.lstat(request)
    except FileNotFoundError:
        return None, None
    raw = _read_regular(request, info)
    try:
        value = json.loads(raw) if raw is not None else None
    except (ValueError, UnicodeError):
        value = None
    payload = value if isinstance(value, dict) else None
    return payload, (info.st_dev, info.st_ino, raw or b"")


def _read_regular(path: Path, info: os.stat_result) -> bytes | None:
    """A bounded read of exactly the regular file ``info`` describes; None for
    a link, pipe, oversized or swapped file (still claimed, then refused)."""
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_REQUEST_BYTES:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return None
        raw = handle.read(MAX_REQUEST_BYTES + 1)
    return raw if len(raw) <= MAX_REQUEST_BYTES else None


def claim_request(
    request: Path, claimed: Path, identity: tuple[int, int, bytes],
) -> bool:
    """Take exactly the request this worker read out of the inbox.

    The app reuses the inbox path and may withdraw a request and queue a newer
    one at any moment, so a rename alone can take the wrong file. The claimed
    file counts as this request only when both its inode and its bytes match
    what was read. Returns False when the request read was withdrawn; any
    other file taken is put back, or kept beside it, never deleted."""
    try:
        os.replace(request, claimed)
    except FileNotFoundError:
        return False
    try:
        info = os.lstat(claimed)
        verified = (info.st_dev, info.st_ino) == identity[:2] and (
            (_read_regular(claimed, info) or b"") == identity[2]
        )
    except OSError:
        verified = False
    if verified:
        return True
    return_unverified_request(request, claimed)
    return False


def _discard_claim(claimed: Path) -> None:
    """Remove a claimed request file, link or pipe. A claimed directory is
    left in the root-owned control directory rather than deleted blindly."""
    try:
        claimed.unlink(missing_ok=True)
    except (IsADirectoryError, PermissionError):
        pass


def return_unverified_request(request: Path, claimed: Path) -> None:
    """Put a file this worker did not read back in the inbox without
    overwriting a newer one; failing that, keep it under a durable name."""
    try:
        os.link(claimed, request)
    except OSError:
        os.replace(claimed, claimed.with_name(f".unreturned-{claimed.name}"))
    else:
        claimed.unlink()


def run() -> int:
    launcher = os.environ.get("MOBIUS_REBUILD_LAUNCHER")
    if launcher is not None and (not launcher.isdigit() or int(launcher) < 2):
        # A legacy launcher cannot retain this trial's recovery owner. Leave
        # both status and request untouched: its unchanged-status success path
        # puts the candidate back instead of consuming the one-shot trial.
        print("Reinstall the reviewed host helper before another replacement; "
              "launcher revision 2 is required.", file=sys.stderr)
        return 0
    if not ADMISSION_CODE.is_file() or not ADMISSION_ROOT.is_dir():
        print("Reinstall the reviewed host helper: boot admission revision 5 is required.", file=sys.stderr)
        return 0
    config_value = config()
    request = config_value["control_dir"] / "inbox" / "request.json"
    if not os.path.lexists(request):
        return 0
    operation = uuid.uuid4().hex
    # Claim inside the root-owned control directory. The inbox and its parent
    # are guaranteed to share a filesystem, unlike /data and STATE_DIR, so the
    # atomic rename also works when operators place Docker data on a separate
    # mount. Moving out of the app-writable inbox prevents later replacement.
    claimed = config_value["control_dir"] / f".request-{operation}.json"
    claim_verified = False
    expected = None
    previous = None
    image_ref = None
    pulled_recorded = False
    lock = LOCK.open("a+")
    try:
        acquire_lock(lock)
    except BlockingIOError:
        lock.close()
        # The lock owner owns the shared status too. Leave both its operation
        # and this still-queued request untouched.
        return 1
    # Everything below, including rollback and transaction settlement after
    # a failure, runs under the lock the installer and reconcile also take.
    with lock:
        try:
            if TRANSACTION.exists():
                # An interrupted replacement is settled first: this unit's
                # ExecStopPost reconcile recovers it, and the request stays
                # queued for the next run.
                return 0
            payload, identity = read_request(request)
            if identity is None:
                return 0  # withdrawn before this worker looked
            try:
                expected, nonce = parse_request(payload or {})
            except ValueError:
                expected = nonce = None
            if expected:
                # Publish this operation's nonce before claiming: while the
                # request is in the inbox the app sees it queued, and once it
                # is gone the status already names it, so the app never
                # mistakes a claimed request for one that ended.
                write_status(config_value, operation_id=operation, state="queued",
                             expected_sha=expected, request_nonce=nonce, code=None,
                             message="Container rebuild queued.")
            # Reconciliation uses this same lock when removing abandoned
            # claims. Claim only after ownership is established so a boot-time
            # reconcile can never mistake a live worker's request for debris.
            if not claim_request(request, claimed, identity):
                # Whatever the rename took is back in the inbox or kept aside.
                if expected:
                    write_status(config_value, operation_id=operation, state="failed",
                                 expected_sha=expected, request_nonce=nonce,
                                 code="withdrawn",
                                 message="The request was withdrawn before it started.")
                return 1
            claim_verified = True
            if not expected:
                raise ValueError("invalid replacement request")
            cid, previous = app_container(config_value)
            image_ref = f"{IMAGE}:sha-{expected}"
            require_pull_space(previous)
            write_status(config_value, operation_id=operation, state="preparing",
                         expected_sha=expected, code=None,
                         message="Downloading and checking the official image.")
            docker_command(["docker", "pull", image_ref], timeout=3600)
            record_pulled_image(image_ref)
            pulled_recorded = True
            # Bind everything that follows to the exact image this pull
            # produced: labels, deployment and worker adoption.
            digest = inspect_image(image_ref, "{{.Id}}")
            revision = inspect_image(
                digest, '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            )
            source = inspect_image(
                digest, '{{index .Config.Labels "org.opencontainers.image.source"}}',
            )
            architecture = inspect_image(digest, "{{.Architecture}}")
            if revision != expected or source != IMAGE_SOURCE or architecture != "amd64":
                raise RuntimeError("the downloaded image is not the requested official amd64 release")
            if previous == digest:
                try:
                    verify_served_generation(cid, expected)
                except ProvenanceRejected as exc:
                    write_status(
                        config_value, operation_id=operation, state="failed",
                        expected_sha=expected, code="provenance_failed",
                        message=str(exc)[:300],
                    )
                    return 1
                retain_images(image_ref, _image_state()["rollback_image_id"])
                write_status(
                    config_value, operation_id=operation, state="no_change",
                    expected_sha=expected, code=None,
                    message="This container already uses that official image.",
                    worker_adoption=adopt_from_image(digest),
                )
                return 0
            docker_command(["docker", "tag", previous, ROLLBACK_TAG], timeout=30)
            transaction = transaction_record(operation, expected, nonce,
                                             previous, digest)
            transaction.update(admission_version=1, source_container=cid)
            return execute_replacement(config_value, transaction)
        except ProvenanceUnconfirmed:
            write_status(config_value, operation_id=operation,
                         state="needs_recovery" if TRANSACTION.exists() else "failed",
                         expected_sha=expected, code="observation_unconfirmed",
                         message="Target provenance could not be observed; no outcome was inferred.")
            return 1
        except subprocess.TimeoutExpired:
            write_status(config_value, state="needs_recovery" if TRANSACTION.exists() else "failed",
                         code="observation_timed_out", message="Docker did not finish in time.")
            return 1
        except Exception as exc:
            detail = str(exc)[:300]
            if TRANSACTION.exists():
                # Drain may have accepted a one-shot boot authorization even
                # when its response (or the following status write) failed.
                # The source still running does not prove cancellation; keep
                # recovery ownership without pretending replacement started.
                write_status(config_value, operation_id=operation,
                             state="needs_recovery", expected_sha=expected,
                             code="replacement_failed", message=detail)
                return 1
            if image_ref and pulled_recorded:
                discard_pulled_image(image_ref)
            write_status(config_value, operation_id=operation, state="failed",
                         expected_sha=expected, code="replacement_failed", message=detail)
            return 1
        finally:
            # Only the verified claim is ever removed; whatever is still in the
            # inbox may be a newer request and stays for the next run or withdrawal.
            if claim_verified:
                _discard_claim(claimed)


def execute_replacement(config_value: dict, transaction: dict) -> int:
    """The shared official/manual cutover owner; caller holds replace.lock.

    Every recoverable input, including manual topology, is durable before
    drain. Exceptions retain the journal: only reconciled evidence can settle
    an ambiguous Docker response. No shell caller may perform its own rollback.
    """
    if TRANSACTION.exists():
        raise RuntimeError("an unresolved replacement already owns the host")
    operation = transaction["operation_id"]
    expected = transaction["expected_sha"]
    previous, target = transaction["previous_image"], transaction["target_image"]
    cid = transaction["source_container"]
    admission_store(config_value, transaction)
    for role, image in (("target", target), ("rollback", previous)):
        wrapped_configuration(config_value, transaction, role, uuid.uuid4().hex, image)
    write_transaction(transaction)
    try:
        request_drain(config_value, operation, cid)
        transaction["drain_confirmed"] = True
        transaction["phase"] = "replacement_started"
        write_transaction(transaction)
        write_status(config_value, operation_id=operation, state="replacing", expected_sha=expected,
                     request_nonce=transaction.get("request_nonce"), code=None,
                     message="Replacing the container under boot admission control.")
        prepare_admission(config_value, transaction)
        prepare_attempt(config_value, transaction, "target")
        if not wait_healthy(config_value):
            return rollback(config_value, operation, expected, "readiness_budget_exhausted",
                            "the new container was not serviceable within the readiness budget", previous)
        cid, current = app_container(config_value)
        if current != target:
            raise RuntimeError("the new container does not run the verified image")
        verify_served_generation(cid, expected)
        return 0 if finish_verified(config_value, transaction, cid, target,
                                    state="succeeded", code=None,
                                    message="Container rebuilt successfully.") else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        code = "observation_unconfirmed"
        if isinstance(exc, ProvenanceRejected):
            code = "replacement_failed"
            transaction.update(failure_code=code, failure_detail=str(exc)[:300])
            write_transaction(transaction)
        write_status(config_value, operation_id=operation, state="needs_recovery", expected_sha=expected,
                     request_nonce=transaction.get("request_nonce"), code=code,
                     message=f"The replacement remains journaled for recovery: {str(exc)[:180]}")
        return 1


def transaction_record(operation: str, expected: str, nonce: str | None,
                       previous: str, target: str) -> dict:
    return {
        "version": 1, "operation_id": operation, "expected_sha": expected,
        "request_nonce": nonce, "previous_image": previous,
        # Unused here, but revision 1 workers (a launcher's fallback)
        # recognise a journal only with it.
        "target_image": target,
    }


def write_transaction(value: dict) -> None:
    _durable_write(
        TRANSACTION, json.dumps(value, separators=(",", ":")).encode(), 0o600,
    )


def cutover_boot_consumed(config_value: dict, operation: str, *,
                          trusted_uid: int = 0, trusted_gid: int = 0,
                          expected_boot_id: str | None = None) -> bool:
    """Read-only proof that this boot consumed the exact cutover authorization.

    A receipt alone is inert, but accepted.json can authorize a future boot.
    Never settle an ambiguous source boot merely because its image is healthy.
    """
    path = config_value["data_dir"] / ".restart-ledger"
    directory = None
    try:
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory)
        if (info.st_uid != trusted_uid or info.st_gid != trusted_gid
                or info.st_mode & 0o022):
            return False

        def read(name: str) -> bytes:
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                item = os.fstat(fd)
                if (not stat.S_ISREG(item.st_mode) or item.st_size > 65536
                        or item.st_uid != trusted_uid or item.st_gid != trusted_gid
                        or item.st_mode & 0o022):
                    raise ValueError("untrusted cutover evidence")
                raw = os.read(fd, 65537)
                if len(raw) > 65536:
                    raise ValueError("oversized cutover evidence")
                return raw
            finally:
                os.close(fd)

        def accepted_absent() -> bool:
            try:
                os.stat("accepted.json", dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                return True
            return False

        if not accepted_absent():
            return False
        boot = read("boot-id").decode().strip()
        ack = json.loads(read("ack.json"))
        token = r"[A-Za-z0-9._:-]{8,160}"
        if (not isinstance(ack, dict) or ack.get("version") != 1
                or ack.get("action") != "external_cutover"
                or ack.get("cutover_id") != operation
                or ack.get("target_boot_id") != boot
                or (expected_boot_id is not None and boot != expected_boot_id)
                or not re.fullmatch(token, boot)
                or not isinstance(ack.get("nonce"), str)
                or not re.fullmatch(token, ack["nonce"])
                or not isinstance(ack.get("source_boot_id"), str)
                or not re.fullmatch(token, ack["source_boot_id"])
                or ack.get("source_boot_id") == boot):
            return False
        receipt_absent = False
        try:
            receipt = json.loads(read("cutover-receipt.json"))
        except FileNotFoundError:
            receipt_absent = True  # a prior finalize may already have retired it
            receipt = None
        if not receipt_absent and (
                not isinstance(receipt, dict) or receipt.get("version") != 1
                or receipt.get("action") != "external_cutover"
                or receipt.get("cutover_id") != operation
                or receipt.get("nonce") != ack["nonce"]):
            return False
        after = path.lstat()
        return (accepted_absent() and read("boot-id").decode().strip() == boot
                and (after.st_dev, after.st_ino) == (info.st_dev, info.st_ino))
    except (OSError, ValueError, UnicodeError):
        return False
    finally:
        if directory is not None:
            os.close(directory)


def finish_verified(config_value: dict, transaction: dict, cid: str, image: str,
                    *, state: str, code: str | None, message: str) -> bool:
    """Commit service success before receipt retirement or optional housekeeping.

    Finalization and the journal cannot be atomic together. A crash in between
    therefore replays an honest degraded outcome, never authorizes another boot.
    """
    expected_boot_id = None
    if transaction.get("admission_version") == 1:
        gate_state = admission_store(config_value, transaction).observe()
        role = "target" if state == "succeeded" else "rollback"
        slot = gate_state["slots"][role]
        if not slot["consumed"] or slot["attempts"][-1]["cid"] != cid:
            raise RuntimeError("healthy container has no matching admission")
        expected_boot_id = slot["consumed"]["boot_id"]
    if not cutover_boot_consumed(config_value, transaction["operation_id"],
                                 expected_boot_id=expected_boot_id):
        write_status(config_value, state="needs_recovery", code="handoff_boot_unconfirmed",
                     message="The container is healthy, but this boot has not proven consumption of "
                             "the exact handoff. Keep the transaction for manual Host recovery.")
        return False
    transaction["outcome"] = {
        "state": state, "code": "handoff_finalize_unconfirmed",
        "message": ("Exact chat handoff finalization is unconfirmed; check affected chats; "
                    f"manual Resume may be needed. {message}")[:300],
        "operation_id": transaction["operation_id"],
        "expected_sha": transaction["expected_sha"],
        "request_nonce": transaction.get("request_nonce"),
        "failure_code": transaction.get("failure_code"),
        "failure_detail": transaction.get("failure_detail"),
    }
    write_transaction(transaction)
    try:
        finalized = restart_ledger(config_value, cid, "finalize-cutover",
                                   transaction["operation_id"], image=image)
    except Exception:
        finalized = False
    if finalized:
        transaction["outcome"].update(code=code, message=message[:300])
        write_transaction(transaction)
    if state == "succeeded":
        # These are best-effort after the service outcome is durable. Their
        # failure cannot turn an already verified replacement into a rollback.
        try:
            retain_images(f"{IMAGE}:sha-{transaction['expected_sha']}",
                          transaction["previous_image"])
            transaction["outcome"]["worker_adoption"] = adopt_from_image(image)
        except Exception:
            transaction["outcome"]["worker_adoption"] = "not adopted: image housekeeping failed"
    settle_transaction(config_value, transaction, **transaction["outcome"])
    return True


def promote_topology(transaction: dict) -> None:
    if transaction.get("outcome", {}).get("state") == "succeeded" and transaction.get("target_topology"):
        _durable_write(COMPOSE, json.dumps(transaction["target_topology"]).encode(), 0o600)


def settle_transaction(config_value: dict, transaction: dict, **outcome) -> None:
    transaction["outcome"] = {
        **outcome, "operation_id": transaction["operation_id"],
        "expected_sha": transaction["expected_sha"],
        "request_nonce": transaction.get("request_nonce"),
    }
    write_transaction(transaction)
    promote_topology(transaction)
    write_status(config_value, **transaction["outcome"])
    clear_transaction()


def clear_transaction() -> None:
    TRANSACTION.unlink(missing_ok=True)
    _fsync_dir(TRANSACTION.parent)


def read_transaction() -> dict | None:
    try:
        value = read_json(TRANSACTION)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    ok = (
        value.get("version") == 1
        and OPERATION_RE.fullmatch(str(value.get("operation_id") or ""))
        and SHA_RE.fullmatch(str(value.get("expected_sha") or ""))
        and str(value.get("previous_image") or "").startswith("sha256:")
    )
    return value if ok else None


def recover(config_value: dict, transaction: dict) -> None:
    """Settle this operation, observing an existing boot rather than replaying it."""
    operation = transaction["operation_id"]
    expected = transaction["expected_sha"]
    fields = {"request_nonce": transaction.get("request_nonce")}
    # Older workers kept the failure only in status. Adopt it only when it
    # belongs to this journal, never from a later queued request.
    if "failure_code" not in transaction:
        try:
            status = read_json(STATUS)
        except (OSError, ValueError):
            status = {}
        if status.get("operation_id") == operation and status.get("state") == "needs_recovery":
            transaction.update(failure_code=status.get("code") or "worker_interrupted",
                               failure_detail=status.get("message") or "the replacement worker stopped")
            write_transaction(transaction)
    write_status(config_value, operation_id=operation, state="verifying",
                 expected_sha=expected, code=transaction.get("failure_code"), **fields,
                 message="Recovering an interrupted replacement.")
    # A completed worker can be interrupted between publishing its outcome
    # and removing the journal. Reconciliation must not undo that success.
    outcome = transaction.get("outcome")
    if outcome:
        promote_topology(transaction)
        write_status(config_value, **outcome)
        clear_transaction()
        return
    previous = transaction["previous_image"]
    try:
        cid, current, health = container_health(config_value)
        # A config-only manual cutover can retain the same image. The old
        # source is never evidence of a completed target admission.
        if (transaction.get("phase") != "rollback_started"
                and cid != transaction.get("source_container")
                and current == transaction.get("target_image")):
            if health in {"starting", "unhealthy", "running", "restarting"}:
                if wait_healthy(config_value):
                    cid, current, health = container_health(config_value)
                else:
                    rollback(config_value, operation, expected,
                             "readiness_budget_exhausted",
                             "the interrupted replacement was not serviceable within the readiness budget",
                             previous)
                    return
            if health == "healthy" and current == transaction["target_image"]:
                try:
                    verify_served_generation(cid, expected)
                except ProvenanceRejected as exc:
                    rollback(config_value, operation, expected,
                             "replacement_failed", str(exc)[:300], previous)
                    return
                except (ProvenanceUnconfirmed, OSError, ValueError, subprocess.SubprocessError):
                    write_status(config_value, operation_id=operation,
                                 state="needs_recovery", expected_sha=expected,
                                 code="observation_unconfirmed", **fields,
                                 message="Target provenance could not be observed; recovery is still needed.")
                    return
                finish_verified(config_value, transaction, cid, current,
                                state="succeeded", code=None,
                                message="Container rebuilt successfully.")
                return
        rollback(config_value, operation, expected, "worker_interrupted",
                 "the replacement worker stopped before it finished", previous)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        write_status(config_value, operation_id=operation, state="needs_recovery",
                     expected_sha=expected, code="rollback_failed", **fields,
                     message=f"Recovery could not finish: {str(exc)[:200]}")


def publish_capabilities() -> int:
    """Installer publication only: no requests, Docker, drains or recovery."""
    config_value = config()
    with LOCK.open("a+") as lock:
        try:
            acquire_lock(lock)
        except BlockingIOError:
            return 1
        if TRANSACTION.exists():
            return 1
        try:
            current = read_json(STATUS)
        except FileNotFoundError:
            current = {}
        if current.get("state") in ACTIVE_STATES:
            return 1
        # write_status preserves the exact settled outcome while refreshing
        # only the capability fields. A racing operation owns the same lock.
        write_status(config_value)
    return 0


def reconcile() -> int:
    config_value = config()
    with LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        # Status and the journal belong to the lock owner. A previous owner
        # may have settled and cleared the journal before we acquired it.
        try:
            current = read_json(STATUS)
        except (OSError, ValueError, json.JSONDecodeError):
            current = None
        # A hard power loss can strand the already-claimed request before or
        # after the first status write. Once no worker owns the lock, it is no
        # longer runnable and must not accumulate in the root-controlled area.
        for claimed in config_value["control_dir"].glob(".request-*.json"):
            if re.fullmatch(r"\.request-[0-9a-f]{32}\.json", claimed.name):
                _discard_claim(claimed)
        # Extraction containers are never started; one left by a killed
        # worker only holds a reference to its image.
        try:
            leftovers = docker_command(
                ["docker", "ps", "-aq", "--filter",
                 "label=mobius-rebuild.worker-extract=1"],
                check=False, timeout=5,
            ).stdout.split()
            if leftovers:
                docker_command(["docker", "rm", "-f", "-v", *leftovers],
                               check=False, timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass  # best effort; reconcile's real work follows
        transaction = read_transaction()
        if transaction is not None:
            recover(config_value, transaction)
        elif current is None:
            write_status(config_value)
        elif current.get("state") in ACTIVE_STATES:
            write_status(config_value, state="failed", code="worker_interrupted",
                         message="The host replacement worker stopped unexpectedly.")
        else:
            # Installation and boot reconciliation also refresh the controller
            # capability receipt.  Otherwise an upgraded helper can remain
            # invisible behind an idle status written by the previous binary.
            write_status(config_value)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "publish-capabilities" and os.geteuid() == 0:
        raise SystemExit(publish_capabilities())
    if len(sys.argv) == 2 and sys.argv[1] == "run" and os.geteuid() == 0:
        raise SystemExit(run())
    if len(sys.argv) == 2 and sys.argv[1] == "reconcile" and os.geteuid() == 0:
        raise SystemExit(reconcile())
    if len(sys.argv) == 2 and sys.argv[1] == "adopt-self" and os.geteuid() == 0:
        # The installer holds the replacement lock for its whole installation;
        # a standalone invocation takes it inside adopt_self().
        raise SystemExit(adopt_self())
    print("invalid invocation", file=sys.stderr)
    raise SystemExit(2)
