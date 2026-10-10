#!/usr/bin/env python3
"""Root-owned controller for one fixed Möbius container rebuild."""

from __future__ import annotations

import base64
import fcntl
import hashlib
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
WORKER_REVISION = 10
# The frozen launcher runs the worker selected here; see offer_worker().
WORKERS = STATE_DIR / "workers"
WORKER_INDEX = STATE_DIR / "workers.json"
WORKER_IN_IMAGE = "/app/platform-baked/scripts/mobius-rebuild-host.py"
MAX_WORKER_BYTES = 1024 * 1024
MAX_REQUEST_BYTES = 4096
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


def inspect_image(image: str, template: str) -> str:
    result = docker_command(
        ["docker", "image", "inspect", "--format", template, image],
        timeout=30,
    )
    return result.stdout.strip()


def app_container(config_value: dict) -> tuple[str, str]:
    result = compose(config_value, "ps", "-q", "app")
    cid = result.stdout.strip()
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
        # Recovery must work even with no runnable container. A timed-out
        # `docker run` can leave a helper mutating the ledger after its caller
        # has moved on; rearm synchronously on the host under replace.lock.
        return rearm_rollback(config_value, operation)
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


def rearm_rollback(config_value: dict, operation: str, *,
                   trusted_uid: int = 0, trusted_gid: int = 0,
                   witness_only: bool = False) -> bool | str:
    """Rearm v1 only before the journaled rollback container's first start.

    This also refreshes an unconsumed acceptance: the old frozen helper left
    accepted_at unchanged, so a slow replacement could expire before rollback.
    The original root receipt still expires after one hour. Missing evidence
    never grants continuation and cannot be repaired by inventing an ack.
    """
    directory = None
    temporary = f".accepted-{uuid.uuid4().hex}.tmp"
    try:
        directory = os.open(config_value["data_dir"] / ".restart-ledger",
                            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(directory)
        if info.st_uid != trusted_uid or info.st_gid != trusted_gid or info.st_mode & 0o022:
            return False

        def read(name: str):
            try:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                return None
            try:
                item = os.fstat(fd)
                if (not stat.S_ISREG(item.st_mode) or item.st_size > 65536
                        or item.st_uid != trusted_uid or item.st_gid != trusted_gid
                        or item.st_mode & 0o022):
                    raise ValueError("untrusted cutover evidence")
                raw = os.read(fd, 65537)
                if len(raw) > 65536:
                    raise ValueError("oversized cutover evidence")
                return raw.decode().strip() if name == "boot-id" else json.loads(raw)
            finally:
                os.close(fd)

        receipt, boot = read("cutover-receipt.json"), read("boot-id")
        token = re.compile(r"[A-Za-z0-9._:-]{8,160}")
        valid = lambda value: isinstance(value, str) and bool(token.fullmatch(value))
        current = time.time()
        if (not isinstance(receipt, dict) or receipt.get("version") != 1
                or receipt.get("action") != "external_cutover"
                or receipt.get("cutover_id") != operation
                or not valid(receipt.get("nonce")) or not valid(receipt.get("source_boot_id"))
                or not valid(boot)
                or not 0 <= current - float(receipt.get("accepted_at", 0)) + 5 <= 3605):
            return False
        accepted, ack = read("accepted.json"), read("ack.json")

        def matches(value):
            return (isinstance(value, dict) and value.get("version") == 1
                    and value.get("action") == "external_cutover"
                    and value.get("cutover_id") == operation
                    and value.get("nonce") == receipt["nonce"])

        if accepted is not None:
            if not matches(accepted) or accepted.get("source_boot_id") != boot:
                return False
        elif not matches(ack) or ack.get("target_boot_id") != boot:
            return False
        if witness_only:
            # Docker can lose its started metadata on power failure after a
            # process booted. Its 'created' state alone is NOT proof that the
            # one-shot ledger authorization remains unconsumed.
            after = (config_value["data_dir"] / ".restart-ledger").lstat()
            if accepted is None or (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                return False
            return hashlib.sha256(json.dumps([boot, accepted], sort_keys=True).encode()).hexdigest()
        payload = json.dumps({**receipt, "source_boot_id": boot,
                              "accepted_at": current}, separators=(",", ":")).encode()
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=directory)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, "accepted.json", src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
        after = (config_value["data_dir"] / ".restart-ledger").lstat()
        return (after.st_dev, after.st_ino) == (info.st_dev, info.st_ino)
    except (OSError, ValueError, TypeError, UnicodeError):
        return False
    finally:
        if directory is not None:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)


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


def prepare_rollback(config_value: dict, transaction: dict) -> None:
    """Create without booting; persist the exact identity before authorizing it.

    The outer phase deliberately remains rollback_started so an older proven
    worker invoked by the launcher cannot replay an interrupted candidate's
    rollback. Only this worker understands the retryable preboot subphases.
    """
    previous = transaction["previous_image"]
    if transaction.get("rollback_stage") == "creating":
        existing, current, _health = container_health(config_value)
        if current and current not in {previous, transaction.get("target_image")}:
            raise RuntimeError("rollback cannot replace an unrecognized image")
        if current == previous and not never_started(existing, previous):
            raise RuntimeError("a previous-image boot already exists; creation cannot be replayed")
        compose(config_value, "up", "--no-start", "--no-build", "--no-deps",
                "--force-recreate", "app", image=previous,
                timeout=COMPOSE_MUTATION_SECONDS)
        cid, current, health = container_health(config_value)
        if current != previous or health != "created" or not never_started(cid, previous):
            raise RuntimeError("rollback creation did not produce one unstarted previous-image container")
        transaction.update(rollback_stage="prepared", rollback_container=cid)
        write_transaction(transaction)
    cid = transaction["rollback_container"]
    if not never_started(cid, previous):
        return  # a boot may have started; observation, never rearm, owns it
    first_start = False
    if transaction.get("rollback_stage") == "prepared":
        transaction["handoff_rearmed"] = restart_ledger(
            config_value, cid, "rearm-cutover", transaction["operation_id"], image=previous,
        )
        transaction["rollback_authorization"] = rearm_rollback(
            config_value, transaction["operation_id"], witness_only=True,
        )
        # Journal BEFORE starting; a lost response may mean the boot consumed
        # its authorization. Only a never-started container can retry start.
        transaction["rollback_stage"] = "starting"
        write_transaction(transaction)
        first_start = True
    witness = transaction.get("rollback_authorization")
    can_retry = witness and witness == rearm_rollback(
        config_value, transaction["operation_id"], witness_only=True,
    )
    if never_started(cid, previous) and (first_start or can_retry):
        docker_command(["docker", "start", cid], timeout=COMPOSE_MUTATION_SECONDS)


def rollback(config_value: dict, operation: str, expected: str,
             code: str, detail: str, previous_image: str | None = None) -> int:
    """Restore the previous container; a settled outcome retires the
    transaction, while needs_recovery keeps it for the next reconcile.

    Only this operation's journal can authorize a restore. The healthy
    container must run its recorded previous image, never a mutable tag."""
    transaction = read_transaction()
    if (transaction is None or transaction["operation_id"] != operation
            or transaction["expected_sha"] != expected
            or (previous_image is not None and previous_image != transaction["previous_image"])):
        raise RuntimeError("rollback does not own the replacement transaction")
    if transaction.get("outcome"):
        # A failed status write/unlink after settlement is not a failed boot.
        write_status(config_value, **transaction["outcome"])
        clear_transaction()
        return 0 if transaction["outcome"]["state"] == "succeeded" else 1
    previous_image = transaction["previous_image"]
    code = transaction.get("failure_code", code)
    detail = transaction.get("failure_detail", detail)
    transaction.update(failure_code=code, failure_detail=detail)
    write_transaction(transaction)
    write_status(config_value, operation_id=operation, state="verifying",
                 expected_sha=expected, request_nonce=transaction.get("request_nonce"),
                 code=code,
                 message="Replacement failed; restoring the previous container.")
    cid, current, health = container_health(config_value)
    observing = current == previous_image and health in {"healthy", "starting", "unhealthy", "running", "restarting"}
    if observing and transaction.get("phase") == "replacement_started":
        # A timed-out daemon request may still be stopping this source. It is
        # not a rollback boot: marking rollback_started here would strand the
        # target if the delayed mutation removes the source after this probe.
        write_status(config_value, state="needs_recovery", code="source_still_running",
                     message="The source container is still running; waiting for the interrupted replacement to settle.")
        return 1
    if not observing:
        if current and current not in {previous_image, transaction.get("target_image")}:
            write_status(config_value, state="needs_recovery", code="rollback_wrong_image",
                         message=f"An unrecognized image is running. Original failure: {detail}"[:300])
            return 1
        if transaction.get("phase") == "rollback_started":
            if transaction.get("rollback_stage") in {"creating", "prepared", "starting"}:
                if transaction.get("rollback_stage") != "creating" and cid != transaction.get("rollback_container"):
                    raise RuntimeError("the journaled rollback container is missing or changed")
                prepare_rollback(config_value, transaction)
            elif current == previous_image and health == "created" and never_started(cid, previous_image):
                # Legacy Compose timed out after creation but before start.
                # Its rearm may already have happened; never repeat it here.
                witness = rearm_rollback(config_value, operation, witness_only=True)
                transaction.update(rollback_container=cid, rollback_stage="starting",
                                   rollback_authorization=witness)
                write_transaction(transaction)
                prepare_rollback(config_value, transaction)
            else:
                # The rollback may already have consumed its exact receipt.
                write_status(config_value, state="needs_recovery", code="rollback_failed",
                             message=f"Rollback is not serviceable: {detail}"[:300])
                return 1
        else:
            # Capture the exact failed target before no-start creation removes it.
            if current == transaction.get("target_image") and cid:
                evidence = capture_failed_target(operation, cid, current)
                try:
                    write_status(config_value, evidence_capture=evidence)
                except OSError:
                    pass
            docker_command(["docker", "tag", previous_image, ROLLBACK_TAG])
            transaction.update(phase="rollback_started", rollback_stage="creating")
            write_transaction(transaction)
            prepare_rollback(config_value, transaction)
    else:
        # Also adopts an old worker's already-running rollback without
        # replaying rearm or replacing a container that is making progress.
        transaction["phase"] = "rollback_started"
        write_transaction(transaction)
    if wait_healthy(config_value, ROLLBACK_HEALTH_SECONDS):
        cid, current, health = container_health(config_value)
        if previous_image and current != previous_image:
            write_status(
                config_value, operation_id=operation, state="needs_recovery",
                expected_sha=expected, code="rollback_wrong_image",
                message=("The restored container is not the recorded previous "
                         f"image. Original failure: {detail}")[:300],
            )
            return 1
        if health != "healthy":
            write_status(config_value, state="needs_recovery", code="rollback_failed",
                         message=f"Rollback health changed. Original failure: {detail}"[:300])
            return 1
        finish_verified(config_value, transaction, cid, previous_image,
                        state="rolled_back", code=code,
                        message=f"The previous container was restored: {detail}")
        return 1
    write_status(config_value, operation_id=operation, state="needs_recovery",
                 expected_sha=expected, code="rollback_failed",
                 message=f"Rollback is not yet healthy. Original failure: {detail}"[:300])
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
    replacement_started = False
    replacement_verified = False
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
            # From here an interruption can leave chats drained or the app
            # removed; reconcile() settles it from this record.
            write_transaction(transaction)
            request_drain(config_value, operation, cid)
            write_status(config_value, operation_id=operation, state="replacing",
                         expected_sha=expected, code=None,
                         message="Rebuilding the container.")
            replacement_started = True
            transaction["phase"] = "replacement_started"
            write_transaction(transaction)
            docker_command(["docker", "tag", digest, TARGET_TAG], timeout=30)
            compose(config_value, "up", "-d", "--no-build", "--no-deps",
                    "--force-recreate", "app", image=TARGET_TAG,
                    timeout=COMPOSE_MUTATION_SECONDS)
            write_status(config_value, operation_id=operation, state="verifying",
                         expected_sha=expected, message="Checking the new container.")
            if not wait_healthy(config_value):
                result = rollback(
                    config_value, operation, expected,
                    "readiness_budget_exhausted",
                    "the new container was not serviceable within the readiness budget",
                    previous,
                )
                try:
                    discard_pulled_image(image_ref)
                except Exception:
                    pass  # image cleanup cannot overturn a settled service outcome
                return result
            cid, current = app_container(config_value)
            if current != digest:
                raise RuntimeError("the new container does not run the verified image")
            verify_served_generation(cid, expected)
            replacement_verified = True
            settled = finish_verified(config_value, transaction, cid, digest,
                                      state="succeeded", code=None,
                                      message="Container rebuilt successfully.")
            return 0 if settled else 1
        except ProvenanceUnconfirmed:
            write_status(config_value, operation_id=operation,
                         state="needs_recovery" if TRANSACTION.exists() else "failed",
                         expected_sha=expected, code="observation_unconfirmed",
                         message="Target provenance could not be observed; no outcome was inferred.")
            return 1
        except subprocess.TimeoutExpired:
            if replacement_started:
                write_status(config_value, state="needs_recovery", code="observation_timed_out",
                             message="Docker did not finish in time; the replacement remains recoverable.")
                return 1
            write_status(config_value, state="needs_recovery" if TRANSACTION.exists() else "failed",
                         code="observation_timed_out", message="Docker did not finish in time.")
            return 1
        except Exception as exc:
            detail = str(exc)[:300]
            if replacement_verified:
                write_status(config_value, state="needs_recovery", code="outcome_record_failed",
                             message="The replacement was verified; its outcome could not be published.")
                return 1
            if replacement_started and previous and expected:
                try:
                    result = rollback(config_value, operation, expected,
                                      "replacement_failed", detail, previous)
                    if image_ref and pulled_recorded:
                        try:
                            discard_pulled_image(image_ref)
                        except Exception:
                            pass  # never retry rollback for optional image cleanup
                    return result
                except Exception as rollback_exc:
                    detail = f"{detail}; rollback failed: {str(rollback_exc)[:160]}"
                    write_status(config_value, operation_id=operation,
                                 state="needs_recovery", expected_sha=expected,
                                 code="rollback_failed", message=detail[:300])
                    return 1
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
                          trusted_uid: int = 0, trusted_gid: int = 0) -> bool:
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
    if not cutover_boot_consumed(config_value, transaction["operation_id"]):
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


def settle_transaction(config_value: dict, transaction: dict, **outcome) -> None:
    transaction["outcome"] = {
        **outcome, "operation_id": transaction["operation_id"],
        "expected_sha": transaction["expected_sha"],
        "request_nonce": transaction.get("request_nonce"),
    }
    write_transaction(transaction)
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
        write_status(config_value, **outcome)
        clear_transaction()
        return
    previous = transaction["previous_image"]
    try:
        cid, current, health = container_health(config_value)
        if transaction.get("phase") != "rollback_started" and current == transaction.get("target_image"):
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
