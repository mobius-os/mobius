#!/usr/bin/env python3
"""Root-owned, fixed-verb host controller for replacing one Möbius container.

Installed by install-rebuild-helper.sh.  The SSH-facing mode accepts only
``status`` and ``rebuild``.  Deployment paths and Compose arguments are read
from a root-owned configuration file; the container cannot provide them.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

STATE_DIR = Path("/var/lib/mobius-rebuild")
CONFIG = Path("/etc/mobius-rebuild/config.json")
STATUS = STATE_DIR / "status.json"
LOCK = STATE_DIR / "rebuild.lock"
REQUEST_LOCK = STATE_DIR / "request.lock"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_SOURCE = "https://github.com/mobius-os/mobius"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain an object")
    return value


def write_status(**fields) -> dict:
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
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=STATE_DIR, prefix="status.", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(current, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, STATUS)
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass
    return current


def status() -> dict:
    try:
        return read_json(STATUS)
    except (OSError, ValueError, json.JSONDecodeError):
        return write_status()


def _lock_is_held(path: Path) -> bool:
    """Whether another process currently owns the worker lock."""
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def _unit_is_running(operation_id: str | None) -> bool:
    if not operation_id or not re.fullmatch(r"[0-9a-f]{32}", operation_id):
        return False
    result = subprocess.run(
        ["systemctl", "show", f"mobius-rebuild-{operation_id}",
         "--property=ActiveState", "--value"],
        text=True, capture_output=True,
    )
    return result.returncode == 0 and result.stdout.strip() in {
        "activating", "active", "reloading", "deactivating",
    }


def rebuild_is_running(current: dict) -> bool:
    return _lock_is_held(LOCK) or _unit_is_running(current.get("operation_id"))


def compose(config: dict, *args: str, image: str | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
    command = ["docker", "compose", "-p", config["project"]]
    for filename in config["files"]:
        command.extend(["-f", filename])
    command.extend(args)
    env = os.environ.copy()
    env["MOBIUS_IMAGE"] = image or config["image"]
    return subprocess.run(command, cwd=config["directory"], env=env,
                          text=True, capture_output=True, check=check)


def inspect_value(image: str, template: str) -> str:
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", template, image],
        text=True, capture_output=True, check=True,
    )
    return result.stdout.strip()


def container_image(config: dict) -> str:
    result = compose(config, "ps", "-q", "app")
    cid = result.stdout.strip()
    if not cid:
        raise RuntimeError("the recorded Möbius app container is not running")
    run = subprocess.run(
        ["docker", "container", "inspect", "--format", "{{.Image}}", cid],
        text=True, capture_output=True, check=True,
    )
    return run.stdout.strip()


def wait_healthy(config: dict, timeout: int = 180) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = compose(config, "ps", "-q", "app", check=False)
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


def validate_config(
    value: dict, *, config_path: Path = CONFIG, trusted_uid: int = 0,
) -> dict:
    required = {"version", "directory", "project", "service", "files", "image",
                "source_commit"}
    if set(value) != required or value["version"] != 2 or value["service"] != "app":
        raise ValueError("invalid fixed deployment configuration")
    directory = Path(value["directory"])
    files = [Path(item) for item in value["files"]]
    expected_files = [config_path.parent / "compose.yml",
                      config_path.parent / "image.override.yml"]
    if directory != config_path.parent or files != expected_files:
        raise ValueError("invalid deployment directory")
    for path in [directory, *files]:
        if path.is_symlink():
            raise ValueError("deployment configuration may not use symlinks")
        stat = path.stat()
        if stat.st_uid != trusted_uid or stat.st_mode & 0o022:
            raise ValueError("deployment configuration is not root-controlled")
    if not directory.is_dir() or any(not path.is_file() for path in files):
        raise ValueError("invalid frozen Compose configuration")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", value["project"]):
        raise ValueError("invalid Compose project")
    if value["image"] != "ghcr.io/mobius-os/mobius":
        raise ValueError("invalid image source")
    if not SHA_RE.fullmatch(str(value["source_commit"])):
        raise ValueError("invalid installer source revision")
    return {**value, "files": [str(path) for path in files]}


def worker(request_path: Path) -> int:
    operation = None
    try:
        request = read_json(request_path)
        operation = str(request.get("operation_id") or "")
        expected = str(request.get("expected_sha") or "")
        if not operation or not SHA_RE.fullmatch(expected):
            raise ValueError("invalid rebuild request")
        config = validate_config(read_json(CONFIG))
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        with LOCK.open("a+") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                write_status(operation_id=operation, state="failed",
                             expected_sha=expected, code="already_running",
                             message="Another rebuild is already running.")
                return 1
            write_status(operation_id=operation, state="preparing",
                         expected_sha=expected, code=None,
                         message="Pulling the official Möbius image.")
            image = f"{config['image']}:sha-{expected}"
            subprocess.run(["docker", "pull", "--platform", "linux/amd64", image],
                           text=True, capture_output=True, check=True)
            revision = inspect_value(image, "{{index .Config.Labels \"org.opencontainers.image.revision\"}}")
            source = inspect_value(image, "{{index .Config.Labels \"org.opencontainers.image.source\"}}")
            architecture = inspect_value(image, "{{.Architecture}}")
            if revision != expected or source != IMAGE_SOURCE or architecture != "amd64":
                raise RuntimeError("the downloaded image did not match the requested official revision")
            digest = inspect_value(image, "{{.Id}}")
            previous = container_image(config)
            if previous == digest:
                write_status(operation_id=operation, state="no_change",
                             expected_sha=expected, code=None,
                             message="This container is already current.")
                return 0
            write_status(operation_id=operation, state="replacing",
                         expected_sha=expected, message="Replacing the container.")
            compose(config, "up", "-d", "--no-build", "--no-deps",
                    "--force-recreate", "app", image=digest)
            write_status(operation_id=operation, state="verifying",
                         expected_sha=expected, message="Checking the new container.")
            if wait_healthy(config):
                write_status(operation_id=operation, state="succeeded",
                             expected_sha=expected, code=None,
                             message="Container rebuilt successfully.")
                return 0
            write_status(operation_id=operation, state="verifying",
                         expected_sha=expected, message="Restoring the previous container.")
            compose(config, "up", "-d", "--no-build", "--no-deps",
                    "--force-recreate", "app", image=previous)
            if wait_healthy(config, 120):
                write_status(operation_id=operation, state="rolled_back",
                             expected_sha=expected, code="health_check_failed",
                             message="The new container was unhealthy, so the previous container was restored.")
                return 1
            write_status(operation_id=operation, state="needs_recovery",
                         expected_sha=expected, code="rollback_failed",
                         message="Neither the new nor previous container became healthy.")
            return 1
    except Exception as exc:
        write_status(operation_id=operation, state="failed", code="rebuild_failed",
                     message=str(exc)[:300])
        return 1
    finally:
        try: request_path.unlink()
        except OSError: pass


def dispatch(command: str) -> int:
    if command == "status":
        print(json.dumps(status(), separators=(",", ":")))
        return 0
    if command != "rebuild":
        print("unsupported command", file=sys.stderr)
        return 2
    try:
        payload = json.loads(sys.stdin.buffer.read(4097))
        if (not isinstance(payload, dict) or set(payload) != {"version", "request_id", "expected_sha"}
                or payload["version"] != 1 or not SHA_RE.fullmatch(str(payload["expected_sha"]))):
            raise ValueError
    except (ValueError, json.JSONDecodeError):
        print("invalid rebuild request", file=sys.stderr)
        return 2
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with REQUEST_LOCK.open("a+") as request_lock:
        fcntl.flock(request_lock, fcntl.LOCK_EX)
        current = status()
        if (
            current.get("state") in {
                "queued", "preparing", "waiting_for_work", "replacing", "verifying",
            }
            and rebuild_is_running(current)
        ):
            print("a rebuild is already running", file=sys.stderr)
            return 3
        operation = uuid.uuid4().hex
        request = STATE_DIR / f"request-{operation}.json"
        request.write_text(json.dumps({"operation_id": operation,
                                       "expected_sha": payload["expected_sha"]}), encoding="utf-8")
        os.chmod(request, 0o600)
        result = write_status(operation_id=operation, state="queued",
                              expected_sha=payload["expected_sha"], code=None,
                              message="Rebuild queued.")
        try:
            subprocess.run(["systemd-run", "--quiet", "--collect",
                            f"--unit=mobius-rebuild-{operation}",
                            "/usr/local/libexec/mobius-rebuild-host", "worker", str(request)],
                           check=True)
        except (OSError, subprocess.CalledProcessError):
            request.unlink(missing_ok=True)
            write_status(operation_id=operation, state="failed",
                         expected_sha=payload["expected_sha"],
                         code="runner_unavailable",
                         message="The host could not start the rebuild job.")
            print("the host could not start the rebuild job", file=sys.stderr)
            return 4
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "dispatch" and sys.argv[2] in {"status", "rebuild"}:
        raise SystemExit(dispatch(sys.argv[2]))
    if len(sys.argv) == 3 and sys.argv[1] == "worker" and os.geteuid() == 0:
        raise SystemExit(worker(Path(sys.argv[2])))
    print("invalid invocation", file=sys.stderr)
    raise SystemExit(2)
