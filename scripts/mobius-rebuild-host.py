#!/usr/bin/env python3
"""Root-owned controller for one fixed Möbius container replacement."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
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
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OPERATION_RE = re.compile(r"^[0-9a-f]{32}$")
IMAGE = "ghcr.io/mobius-os/mobius"
IMAGE_SOURCE = "https://github.com/mobius-os/mobius"
ROLLBACK_TAG = f"{IMAGE}:mobius-rebuild-last-good"
ACTIVE_STATES = {"queued", "preparing", "replacing", "verifying"}


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
    }
    try:
        current.update(read_json(STATUS))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    current.update(fields)
    current["updated_at"] = now()
    _atomic_json(STATUS, current)
    _atomic_json(config_value["control_dir"] / "status.json", current, 0o644)
    return current


def compose(config_value: dict, *args: str, image: str | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["MOBIUS_IMAGE"] = image or IMAGE
    return subprocess.run(
        ["docker", "compose", "-p", config_value["project"],
         "-f", str(COMPOSE), "-f", str(OVERRIDE), *args],
        cwd=CONFIG.parent, env=env, text=True, capture_output=True, check=check,
    )


def inspect_image(image: str, template: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", template, image],
        text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


def app_container(config_value: dict) -> tuple[str, str]:
    result = compose(config_value, "ps", "-q", "app")
    cid = result.stdout.strip()
    if not cid:
        raise RuntimeError("the recorded Möbius app container is not running")
    inspected = subprocess.run(
        ["docker", "container", "inspect", "--format", "{{.Image}}", cid],
        text=True, capture_output=True, check=True,
    )
    return cid, inspected.stdout.strip()


def wait_healthy(config_value: dict, timeout: int = 180) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = compose(config_value, "ps", "-q", "app", check=False)
        cid = result.stdout.strip()
        if cid:
            probe = subprocess.run(
                ["docker", "container", "inspect", "--format",
                 "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", cid],
                text=True, capture_output=True,
            )
            if probe.returncode == 0 and probe.stdout.strip() == "healthy":
                return True
        time.sleep(3)
    return False


def _docker_root() -> Path:
    result = subprocess.run(
        ["docker", "info", "--format", "{{.DockerRootDir}}"],
        text=True, capture_output=True, check=True,
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


def request_drain(config_value: dict, operation: str, cid: str) -> Path:
    ready = config_value["control_dir"] / "inbox" / f"ready-{operation}"
    ready.unlink(missing_ok=True)
    requested_at = time.time()
    result = subprocess.run(
        ["docker", "exec", cid, "python3",
         "/data/platform/backend/scripts/prepare-container-replacement.py", operation],
        text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError("the running server could not begin a safe chat drain")
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if ready.is_file():
            break
        time.sleep(0.25)
    else:
        raise RuntimeError("active chats did not drain before the cutover deadline")
    # The app writes `ready` after publishing its restart intent, but the
    # root-owned entrypoint poller must accept that nonce before Docker removes
    # the old container. Waiting for its fresh accepted receipt preserves the
    # same authenticated continuation contract as the ordinary Restart button.
    accepted = Path(config_value["data_dir"]) / ".restart-ledger" / "accepted.json"
    request = Path(config_value["data_dir"]) / ".platform-restart-requested"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            if accepted.stat().st_mtime >= requested_at and not request.exists():
                return ready
        except OSError:
            pass
        time.sleep(0.25)
    raise RuntimeError("the restart supervisor did not accept the chat continuation")


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
    subprocess.run(["docker", "image", "rm", target_ref], capture_output=True, text=True)
    _atomic_json(IMAGES, {
        **state,
        "sha_refs": [ref for ref in state["sha_refs"] if ref != target_ref],
    })


def retain_images(target_ref: str, rollback_image_id: str | None = None) -> None:
    state = _image_state()
    for ref in state["sha_refs"]:
        if ref != target_ref:
            subprocess.run(["docker", "image", "rm", ref], capture_output=True, text=True)
    old_rollback = state["rollback_image_id"]
    if old_rollback and old_rollback != rollback_image_id:
        # Non-force removal fails harmlessly if another tag or container still
        # owns the image; the helper never removes unrelated references.
        subprocess.run(
            ["docker", "image", "rm", old_rollback], capture_output=True, text=True,
        )
    _atomic_json(IMAGES, {
        "sha_refs": [target_ref],
        "rollback_tag": ROLLBACK_TAG,
        "rollback_image_id": rollback_image_id,
    })


def rollback(config_value: dict, operation: str, expected: str,
             code: str, detail: str) -> int:
    write_status(config_value, operation_id=operation, state="verifying",
                 expected_sha=expected, code=code,
                 message="Replacement failed; restoring the previous container.")
    compose(config_value, "up", "-d", "--no-build", "--no-deps",
            "--force-recreate", "app", image=ROLLBACK_TAG)
    if wait_healthy(config_value, 120):
        write_status(config_value, operation_id=operation, state="rolled_back",
                     expected_sha=expected, code=code,
                     message=f"The previous container was restored: {detail}"[:300])
        return 1
    write_status(config_value, operation_id=operation, state="needs_recovery",
                 expected_sha=expected, code="rollback_failed",
                 message=f"Replacement and rollback failed: {detail}"[:300])
    return 1


def run() -> int:
    config_value = config()
    request = config_value["control_dir"] / "inbox" / "request.json"
    if not request.is_file():
        return 0
    operation = uuid.uuid4().hex
    # Claim inside the root-owned control directory. The inbox and its parent
    # are guaranteed to share a filesystem, unlike /data and STATE_DIR, so the
    # atomic rename also works when operators place Docker data on a separate
    # mount. Moving out of the app-writable inbox prevents later replacement.
    claimed = config_value["control_dir"] / f".request-{operation}.json"
    request_claimed = False
    expected = None
    previous = None
    image_ref = None
    pulled_recorded = False
    replacement_started = False
    ready: Path | None = None
    try:
        with LOCK.open("a+") as lock:
            acquire_lock(lock)
            # Reconciliation uses this same lock when removing abandoned
            # claims. Claim only after ownership is established so a boot-time
            # reconcile can never mistake a live worker's request for debris.
            os.replace(request, claimed)
            request_claimed = True
            payload = read_json(claimed)
            if set(payload) != {"version", "expected_sha"} or payload["version"] != 1:
                raise ValueError("invalid replacement request")
            expected = str(payload["expected_sha"])
            if not SHA_RE.fullmatch(expected):
                raise ValueError("invalid replacement target")
            write_status(config_value, operation_id=operation, state="queued",
                         expected_sha=expected, code=None,
                         message="Container replacement queued.")
            cid, previous = app_container(config_value)
            image_ref = f"{IMAGE}:sha-{expected}"
            require_pull_space(previous)
            write_status(config_value, operation_id=operation, state="preparing",
                         expected_sha=expected, code=None,
                         message="Downloading and checking the official image.")
            subprocess.run(["docker", "pull", image_ref], check=True,
                           text=True, capture_output=True)
            record_pulled_image(image_ref)
            pulled_recorded = True
            revision = inspect_image(
                image_ref, '{{index .Config.Labels "org.opencontainers.image.revision"}}',
            )
            source = inspect_image(
                image_ref, '{{index .Config.Labels "org.opencontainers.image.source"}}',
            )
            architecture = inspect_image(image_ref, "{{.Architecture}}")
            if revision != expected or source != IMAGE_SOURCE or architecture != "amd64":
                raise RuntimeError("the downloaded image is not the requested official amd64 release")
            digest = inspect_image(image_ref, "{{.Id}}")
            if previous == digest:
                retain_images(image_ref, _image_state()["rollback_image_id"])
                write_status(config_value, operation_id=operation, state="no_change",
                             expected_sha=expected, code=None,
                             message="This container already uses that official image.")
                return 0
            subprocess.run(["docker", "tag", previous, ROLLBACK_TAG], check=True,
                           text=True, capture_output=True)
            ready = request_drain(config_value, operation, cid)
            write_status(config_value, operation_id=operation, state="replacing",
                         expected_sha=expected, code=None,
                         message="Replacing the container.")
            replacement_started = True
            compose(config_value, "up", "-d", "--no-build", "--no-deps",
                    "--force-recreate", "app", image=image_ref)
            write_status(config_value, operation_id=operation, state="verifying",
                         expected_sha=expected, message="Checking the new container.")
            if not wait_healthy(config_value):
                result = rollback(
                    config_value, operation, expected,
                    "health_check_failed", "the new container was unhealthy",
                )
                discard_pulled_image(image_ref)
                return result
            retain_images(image_ref, previous)
            write_status(config_value, operation_id=operation, state="succeeded",
                         expected_sha=expected, code=None,
                         message="Container replaced successfully.")
            return 0
    except BlockingIOError:
        write_status(config_value, operation_id=operation, state="failed",
                     expected_sha=expected, code="already_running",
                     message="Another replacement is already running.")
        return 1
    except Exception as exc:
        detail = str(exc)[:300]
        if replacement_started and previous and expected:
            try:
                result = rollback(config_value, operation, expected,
                                  "replacement_failed", detail)
                if image_ref and pulled_recorded:
                    discard_pulled_image(image_ref)
                return result
            except Exception as rollback_exc:
                detail = f"{detail}; rollback failed: {str(rollback_exc)[:160]}"
                write_status(config_value, operation_id=operation,
                             state="needs_recovery", expected_sha=expected,
                             code="rollback_failed", message=detail[:300])
                return 1
        if image_ref and pulled_recorded:
            discard_pulled_image(image_ref)
        write_status(config_value, operation_id=operation, state="failed",
                     expected_sha=expected, code="replacement_failed", message=detail)
        return 1
    finally:
        claimed.unlink(missing_ok=True)
        if not request_claimed:
            # A malformed path or other claim failure must not leave the path
            # unit continuously retriggering an unrecoverable request.
            request.unlink(missing_ok=True)
        if ready is not None:
            ready.unlink(missing_ok=True)


def reconcile() -> int:
    config_value = config()
    try:
        current = read_json(STATUS)
    except (OSError, ValueError, json.JSONDecodeError):
        current = None
    with LOCK.open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        # A hard power loss can strand the already-claimed request before or
        # after the first status write. Once no worker owns the lock, it is no
        # longer runnable and must not accumulate in the root-controlled area.
        for claimed in config_value["control_dir"].glob(".request-*.json"):
            if re.fullmatch(r"\.request-[0-9a-f]{32}\.json", claimed.name):
                claimed.unlink(missing_ok=True)
        if current is None:
            write_status(config_value)
        elif current.get("state") in ACTIVE_STATES:
            write_status(config_value, state="failed", code="worker_interrupted",
                         message="The host replacement worker stopped unexpectedly.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "run" and os.geteuid() == 0:
        raise SystemExit(run())
    if len(sys.argv) == 2 and sys.argv[1] == "reconcile" and os.geteuid() == 0:
        raise SystemExit(reconcile())
    print("invalid invocation", file=sys.stderr)
    raise SystemExit(2)
