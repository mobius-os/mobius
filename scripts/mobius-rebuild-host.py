#!/usr/bin/env python3
"""Root-owned controller for one fixed Möbius container replacement."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

STATE_DIR = Path("/var/lib/mobius-rebuild")
CONFIG = Path("/etc/mobius-rebuild/config.json")
COMPOSE = Path("/etc/mobius-rebuild/compose.yml")
OVERRIDE = Path("/etc/mobius-rebuild/image.override.yml")
STATUS = STATE_DIR / "status.json"
LOCK = STATE_DIR / "replace.lock"
IMAGES = STATE_DIR / "images.json"
RUNTIME_GENERATIONS = STATE_DIR / "runtime-generations"
RUNTIME_STATE = STATE_DIR / "runtime.json"
RUNTIME_RESOLUTIONS = STATE_DIR / "runtime-resolutions"
RUNTIME_RESOLUTION = STATE_DIR / "runtime-resolution.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
OPERATION_RE = re.compile(r"^[0-9a-f]{32}$")
CONTAINER_RE = re.compile(r"^[0-9a-f]{12,64}$")
GENERATION_RE = re.compile(r"^runtime-[0-9a-f]{16}-[0-9a-f]{8}$")
RESOLUTION_RE = re.compile(r"^resolution-[0-9a-f]{32}$")
IMAGE = "ghcr.io/mobius-os/mobius"
IMAGE_SOURCE = "https://github.com/mobius-os/mobius"
ROLLBACK_TAG = f"{IMAGE}:mobius-rebuild-last-good"
ACTIVE_STATES = {"queued", "preparing", "replacing", "verifying"}
HANDOFF_VERSION = "external-cutover-v1"
RUNTIME_OVERLAY_VERSION = "active-runtime-v1"
MAX_RUNTIME_ARCHIVE_BYTES = 32 * 1024 * 1024


class RuntimeGeneration(NamedTuple):
    """One immutable root-owned protected-runtime generation."""

    name: str
    path: Path
    digest: str


class PreparedRuntime(NamedTuple):
    """The exact rollback and candidate generations for one replacement."""

    previous: RuntimeGeneration
    candidate: RuntimeGeneration
    carried_paths: tuple[str, ...]
    prior_active: RuntimeGeneration
    prior_rollback: RuntimeGeneration | None


class RuntimeResolution(NamedTuple):
    """One root-approved conflict resolution bound to an active tree + target."""

    expected_sha: str
    active_digest: str
    source_commit: str
    paths: tuple[str, ...]
    digest: str
    directory: Path


class RuntimeOverlayError(RuntimeError):
    code = "runtime_overlay_failed"


class RuntimeOverlayConflict(RuntimeOverlayError):
    code = "runtime_overlay_conflict"

    def __init__(self, paths: list[str]) -> None:
        self.paths = tuple(paths)
        visible = ", ".join(paths[:5])
        if len(paths) > 5:
            visible += f" and {len(paths) - 5} more"
        super().__init__(
            "The active local runtime conflicts with the official update: "
            + visible
        )


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


def _runtime_snapshot(root: Path) -> tuple[str, dict[str, str]]:
    """Hash one regular-file runtime tree with the app's provenance contract."""
    if not root.is_dir() or root.is_symlink():
        raise RuntimeOverlayError("the protected runtime tree is unavailable")
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or relative.suffix in {".pyc", ".pyo"}:
            continue
        name = relative.as_posix()
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise RuntimeOverlayError(
                f"the protected runtime contains an unsafe path: {name}"
            )
        if path.is_file():
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    tree = hashlib.sha256()
    for name, digest in sorted(files.items()):
        tree.update(name.encode("utf-8"))
        tree.update(b"\0")
        tree.update(digest.encode("ascii"))
        tree.update(b"\n")
    return tree.hexdigest(), files


def _normalize_runtime_tree(root: Path) -> str:
    """Remove inert bytecode and freeze a copied tree as root-owned read-only."""
    if not root.is_dir() or root.is_symlink():
        raise RuntimeOverlayError("the protected runtime tree is unavailable")
    for path in sorted(root.rglob("*"), reverse=True):
        relative = path.relative_to(root)
        if path.is_symlink():
            raise RuntimeOverlayError(
                f"the protected runtime contains an unsafe path: {relative.as_posix()}"
            )
        if path.is_dir() and path.name == "__pycache__":
            shutil.rmtree(path)
            continue
        if path.is_file() and path.suffix in {".pyc", ".pyo"}:
            path.unlink()
            continue
        if not path.is_dir() and not path.is_file():
            raise RuntimeOverlayError(
                f"the protected runtime contains an unsafe path: {relative.as_posix()}"
            )
    digest, _files = _runtime_snapshot(root)
    for path in sorted(root.rglob("*")):
        if path.is_dir():
            os.chmod(path, 0o755)
        else:
            executable = bool(path.stat().st_mode & 0o111)
            os.chmod(path, 0o555 if executable else 0o444)
        os.chown(path, 0, 0)
    os.chmod(root, 0o755)
    os.chown(root, 0, 0)
    return digest


def _copy_container_runtime(cid: str, destination: Path) -> str:
    destination.mkdir(mode=0o700, parents=True)
    subprocess.run(
        ["docker", "cp", f"{cid}:/app/runtime/.", str(destination)],
        text=True, capture_output=True, check=True,
    )
    return _normalize_runtime_tree(destination)


def _copy_image_runtime(image: str, destination: Path) -> tuple[str, dict]:
    """Copy the raw image tree, bypassing any Compose runtime mount."""
    created = subprocess.run(
        ["docker", "create", image], text=True, capture_output=True, check=True,
    ).stdout.strip()
    if not CONTAINER_RE.fullmatch(created):
        raise RuntimeOverlayError("Docker returned an invalid image container id")
    build_info: dict = {}
    try:
        digest = _copy_container_runtime(created, destination)
        info_path = destination.parent / f".{destination.name}-build-info.json"
        copied = subprocess.run(
            ["docker", "cp", f"{created}:/app/build-info.json", str(info_path)],
            text=True, capture_output=True,
        )
        if copied.returncode == 0:
            try:
                value = read_json(info_path)
                build_info = value
            finally:
                info_path.unlink(missing_ok=True)
        return digest, build_info
    finally:
        subprocess.run(
            ["docker", "rm", "-f", created], text=True, capture_output=True,
        )


def _extract_git_runtime(repo: Path, revision: str, destination: Path) -> str:
    """Extract one content-addressed runtime tree without trusting a branch."""
    if repo.is_symlink() or not (repo / ".git").is_dir() or not SHA_RE.fullmatch(
        revision
    ):
        raise RuntimeOverlayError("the platform runtime provenance is unavailable")
    with tempfile.TemporaryFile() as payload:
        result = subprocess.run(
            ["git", "-c", f"safe.directory={repo}",
             "-c", "core.hooksPath=/dev/null", "-C", str(repo), "archive",
             "--format=tar", revision, "backend/runtime"],
            stdout=payload, stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            raise RuntimeOverlayError(
                "the platform checkout does not contain the required runtime provenance"
            )
        if payload.tell() > MAX_RUNTIME_ARCHIVE_BYTES:
            raise RuntimeOverlayError("the protected runtime archive is unexpectedly large")
        payload.seek(0)
        destination.mkdir(mode=0o700, parents=True)
        prefix = "backend/runtime/"
        with tarfile.open(fileobj=payload, mode="r:") as archive:
            for member in archive.getmembers():
                if member.name in {"backend", "backend/runtime"}:
                    continue
                if not member.name.startswith(prefix):
                    raise RuntimeOverlayError("the runtime base archive is invalid")
                relative = member.name[len(prefix):]
                if (
                    not relative
                    or relative.startswith("/")
                    or ".." in Path(relative).parts
                ):
                    raise RuntimeOverlayError("the runtime base archive is invalid")
                target = destination / relative
                if member.isdir():
                    target.mkdir(mode=0o700, parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise RuntimeOverlayError("the runtime base archive is invalid")
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise RuntimeOverlayError("the runtime base archive is invalid")
                target.write_bytes(source.read())
                os.chmod(target, member.mode & 0o777)
    return _normalize_runtime_tree(destination)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for name in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
        "GIT_COMMON_DIR", "GIT_NAMESPACE",
    ):
        env.pop(name, None)
    return subprocess.run(
        ["git", "-c", f"safe.directory={repo}",
         "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args],
        text=True, capture_output=True, check=check, env=env,
    )


def _replace_checkout(repo: Path, source: Path) -> None:
    for child in repo.iterdir():
        if child.name == ".git":
            continue
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for child in source.iterdir():
        target = repo / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    for path in repo.rglob("*"):
        if ".git" not in path.parts:
            os.chmod(path, path.stat().st_mode | 0o200)


def _merge_runtime_trees(
    base: Path, local: Path, incoming: Path, result: Path,
    resolution: RuntimeResolution | None = None,
) -> tuple[str, tuple[str, ...]]:
    """Three-way merge an already-active runtime onto an official runtime."""
    repo = result.parent / "merge-repo"
    repo.mkdir(mode=0o700)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Möbius Runtime Controller")
    _git(repo, "config", "user.email", "runtime-controller@localhost")
    _replace_checkout(repo, base)
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "runtime base")
    base_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    _git(repo, "checkout", "-q", "-b", "active")
    _replace_checkout(repo, local)
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "active local runtime")
    active_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    carried = tuple(
        line for line in _git(
            repo, "diff", "--name-only", base_sha, active_sha,
        ).stdout.splitlines() if line
    )

    _git(repo, "checkout", "-q", "-b", "incoming", base_sha)
    _replace_checkout(repo, incoming)
    _git(repo, "add", "-A", "-f")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "incoming official runtime")
    merged = _git(
        repo, "merge", "--no-commit", "--no-ff", "active", check=False,
    )
    resolution_applied = False
    if merged.returncode:
        conflicts = sorted(
            line for line in _git(
                repo, "diff", "--name-only", "--diff-filter=U", check=False,
            ).stdout.splitlines() if line
        )
        if conflicts:
            if resolution is None or tuple(conflicts) != resolution.paths:
                raise RuntimeOverlayConflict(conflicts)
            for name in resolution.paths:
                source = resolution.directory / name
                target = repo / name
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                elif target.exists() or target.is_symlink():
                    target.unlink()
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copy2(source, target)
            _git(repo, "add", "-A", "-f")
            resolution_applied = True
            unresolved = [
                line for line in _git(
                    repo, "diff", "--name-only", "--diff-filter=U", check=False,
                ).stdout.splitlines() if line
            ]
            if unresolved:
                raise RuntimeOverlayConflict(unresolved)
            carried = tuple(sorted({*carried, *resolution.paths}))
        else:
            raise RuntimeOverlayError(
                (merged.stderr or merged.stdout or "runtime merge failed").strip()[:300]
            )
    if resolution is not None and not resolution_applied:
        for name in resolution.paths:
            source = resolution.directory / name
            target = repo / name
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(source, target)
        _git(repo, "add", "-A", "-f")
        carried = tuple(sorted({*carried, *resolution.paths}))
    result.mkdir(mode=0o700)
    for child in repo.iterdir():
        if child.name == ".git":
            continue
        target = result / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            shutil.copy2(child, target)
    return _normalize_runtime_tree(result), carried


def _runtime_generation(value: dict) -> RuntimeGeneration:
    if not isinstance(value, dict) or set(value) != {"name", "digest"}:
        raise RuntimeOverlayError("the active runtime receipt is invalid")
    name = str(value["name"])
    digest = str(value["digest"])
    if not GENERATION_RE.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise RuntimeOverlayError("the active runtime receipt is invalid")
    path = RUNTIME_GENERATIONS / name
    actual, _files = _runtime_snapshot(path)
    if actual != digest:
        raise RuntimeOverlayError("the active runtime generation was modified")
    return RuntimeGeneration(name=name, path=path, digest=digest)


def _runtime_record(generation: RuntimeGeneration | None) -> dict | None:
    if generation is None:
        return None
    return {"name": generation.name, "digest": generation.digest}


def _read_runtime_state() -> tuple[RuntimeGeneration, RuntimeGeneration | None]:
    try:
        value = read_json(RUNTIME_STATE)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeOverlayError("the active runtime receipt is unavailable") from exc
    if value.get("version") != 1 or set(value) != {"version", "active", "rollback"}:
        raise RuntimeOverlayError("the active runtime receipt is invalid")
    active = _runtime_generation(value["active"])
    rollback_value = value["rollback"]
    rollback = _runtime_generation(rollback_value) if rollback_value else None
    return active, rollback


def active_runtime_generation() -> RuntimeGeneration:
    return _read_runtime_state()[0]


def _safe_runtime_path(value: object) -> str:
    path = Path(str(value))
    if (
        path.is_absolute()
        or not path.parts
        or ".." in path.parts
        or any(part in {"", ".git"} for part in path.parts)
    ):
        raise RuntimeOverlayError("the staged runtime resolution is invalid")
    return path.as_posix()


def _read_runtime_resolution(
    expected_sha: str, active_digest: str,
) -> RuntimeResolution | None:
    try:
        value = read_json(RUNTIME_RESOLUTION)
    except FileNotFoundError:
        return None
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeOverlayError("the staged runtime resolution is invalid") from exc
    required = {
        "version", "expected_sha", "active_digest", "source_commit",
        "paths", "digest", "directory",
    }
    if value.get("version") != 1 or set(value) != required:
        raise RuntimeOverlayError("the staged runtime resolution is invalid")
    target = str(value["expected_sha"])
    active = str(value["active_digest"])
    source_commit = str(value["source_commit"])
    resolution_digest = str(value["digest"])
    directory_name = str(value["directory"])
    raw_paths = value["paths"]
    if (
        not SHA_RE.fullmatch(target)
        or not re.fullmatch(r"[0-9a-f]{64}", active)
        or not SHA_RE.fullmatch(source_commit)
        or not re.fullmatch(r"[0-9a-f]{64}", resolution_digest)
        or not RESOLUTION_RE.fullmatch(directory_name)
        or not isinstance(raw_paths, list)
        or not raw_paths
    ):
        raise RuntimeOverlayError("the staged runtime resolution is invalid")
    paths = tuple(sorted({_safe_runtime_path(item) for item in raw_paths}))
    if len(paths) != len(raw_paths):
        raise RuntimeOverlayError("the staged runtime resolution is invalid")
    directory = RUNTIME_RESOLUTIONS / directory_name
    trusted_uid = os.geteuid()
    for path in (RUNTIME_RESOLUTION, directory, *directory.rglob("*")):
        if path.is_symlink():
            raise RuntimeOverlayError("the staged runtime resolution is unsafe")
        stat = path.stat()
        if stat.st_uid != trusted_uid or stat.st_mode & 0o022:
            raise RuntimeOverlayError("the staged runtime resolution is unsafe")
    actual_digest, files = _runtime_snapshot(directory)
    if actual_digest != resolution_digest or set(files) != set(paths):
        raise RuntimeOverlayError("the staged runtime resolution is invalid")
    if target != expected_sha or active != active_digest:
        return None
    return RuntimeResolution(
        target, active, source_commit, paths, resolution_digest, directory,
    )


def _consume_runtime_resolution(expected_sha: str, active_digest: str) -> None:
    resolution = _read_runtime_resolution(expected_sha, active_digest)
    if resolution is None:
        return
    RUNTIME_RESOLUTION.unlink(missing_ok=True)
    shutil.rmtree(resolution.directory, ignore_errors=True)


def _write_runtime_state(
    active: RuntimeGeneration, rollback: RuntimeGeneration | None,
) -> None:
    _atomic_json(RUNTIME_STATE, {
        "version": 1,
        "active": _runtime_record(active),
        "rollback": _runtime_record(rollback),
    })


def _store_runtime_generation(source: Path) -> RuntimeGeneration:
    digest, _files = _runtime_snapshot(source)
    RUNTIME_GENERATIONS.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(RUNTIME_GENERATIONS, 0o700)
    name = f"runtime-{digest[:16]}-{uuid.uuid4().hex[:8]}"
    path = RUNTIME_GENERATIONS / name
    shutil.copytree(source, path)
    stored_digest = _normalize_runtime_tree(path)
    if stored_digest != digest:
        shutil.rmtree(path, ignore_errors=True)
        raise RuntimeOverlayError("the runtime generation changed while being stored")
    return RuntimeGeneration(name=name, path=path, digest=digest)


def _remove_runtime_generation(generation: RuntimeGeneration | None) -> None:
    if generation is None:
        return
    if generation.path.parent != RUNTIME_GENERATIONS or not GENERATION_RE.fullmatch(
        generation.name
    ):
        raise RuntimeOverlayError("refusing to remove an invalid runtime generation")
    shutil.rmtree(generation.path, ignore_errors=True)


def bootstrap_runtime(cid: str) -> RuntimeGeneration:
    """Adopt the exact runtime already executing when the helper is installed."""
    if not CONTAINER_RE.fullmatch(cid):
        raise RuntimeOverlayError("invalid running container id")
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=STATE_DIR, prefix="runtime-bootstrap-") as raw:
        tree = Path(raw) / "active"
        _copy_container_runtime(cid, tree)
        current = _store_runtime_generation(tree)
    previous: RuntimeGeneration | None = None
    old_rollback: RuntimeGeneration | None = None
    try:
        previous, old_rollback = _read_runtime_state()
    except RuntimeOverlayError:
        pass
    if previous and previous.digest == current.digest:
        _remove_runtime_generation(current)
        return previous
    _write_runtime_state(current, previous)
    if old_rollback and (not previous or old_rollback.name != previous.name):
        _remove_runtime_generation(old_rollback)
    return current


def prepare_runtime_overlay(
    cid: str, current_image: str, incoming_image: str,
    repo: Path, expected_sha: str,
) -> PreparedRuntime:
    """Prepare a clean merge without reading editable /data platform source."""
    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=STATE_DIR, prefix="runtime-prepare-") as raw:
        workspace = Path(raw)
        local = workspace / "active"
        current_raw = workspace / "current-image"
        current_source = workspace / "current-source"
        incoming = workspace / "incoming-image"
        incoming_source = workspace / "incoming-source"
        base = workspace / "base"
        merged = workspace / "merged"

        prior_active, prior_rollback = _read_runtime_state()
        active_digest = _copy_container_runtime(cid, local)
        previous = _store_runtime_generation(local)
        try:
            current_digest, build_info = _copy_image_runtime(
                current_image, current_raw,
            )
            incoming_digest, _incoming_info = _copy_image_runtime(
                incoming_image, incoming,
            )
            current_sha = str(build_info.get("sha") or "").strip().lower()
            if not SHA_RE.fullmatch(current_sha) or not SHA_RE.fullmatch(expected_sha):
                raise RuntimeOverlayError(
                    "the active image does not expose an exact runtime revision"
                )
            if _extract_git_runtime(repo, current_sha, current_source) != current_digest:
                raise RuntimeOverlayError(
                    "the active image runtime does not match its recorded revision"
                )
            if _extract_git_runtime(
                repo, expected_sha, incoming_source,
            ) != incoming_digest:
                raise RuntimeOverlayError(
                    "the official image runtime does not match its requested revision"
                )
            merge_base = _git(
                repo, "merge-base", current_sha, expected_sha, check=False,
            ).stdout.strip()
            if not SHA_RE.fullmatch(merge_base):
                raise RuntimeOverlayError(
                    "the active and official runtimes have no verifiable merge base"
                )
            _extract_git_runtime(repo, merge_base, base)

            resolution = _read_runtime_resolution(expected_sha, active_digest)
            _merged_digest, carried = _merge_runtime_trees(
                base, local, incoming, merged, resolution,
            )
            candidate = _store_runtime_generation(merged)
        except Exception:
            _remove_runtime_generation(previous)
            raise
    return PreparedRuntime(
        previous=previous,
        candidate=candidate,
        carried_paths=carried,
        prior_active=prior_active,
        prior_rollback=prior_rollback,
    )


def activate_runtime_overlay(prepared: PreparedRuntime) -> None:
    active, rollback = _read_runtime_state()
    if (
        active.name != prepared.prior_active.name
        or (rollback.name if rollback else None)
        != (prepared.prior_rollback.name if prepared.prior_rollback else None)
    ):
        raise RuntimeOverlayError("the active runtime receipt changed during replacement")
    _write_runtime_state(prepared.candidate, prepared.previous)
    for retired in (prepared.prior_active, prepared.prior_rollback):
        if retired and retired.name not in {
            prepared.previous.name, prepared.candidate.name,
        }:
            _remove_runtime_generation(retired)


def write_status(config_value: dict, **fields) -> dict:
    current = {
        "supported": True, "operation_id": None, "state": "idle",
        "expected_sha": None, "code": None, "message": None,
        "handoff": HANDOFF_VERSION,
        "runtime_overlay": RUNTIME_OVERLAY_VERSION,
    }
    try:
        current.update(read_json(STATUS))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    current.update(fields)
    current["handoff"] = HANDOFF_VERSION
    current["runtime_overlay"] = RUNTIME_OVERLAY_VERSION
    current["updated_at"] = now()
    _atomic_json(STATUS, current)
    _atomic_json(config_value["control_dir"] / "status.json", current, 0o644)
    return current


def compose(config_value: dict, *args: str, image: str | None = None,
            runtime: RuntimeGeneration | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["MOBIUS_IMAGE"] = image or IMAGE
    selected = runtime or active_runtime_generation()
    env["MOBIUS_RUNTIME_OVERLAY"] = str(selected.path)
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


def stage_runtime_resolution(
    config_value: dict, cid: str, expected_sha: str, source_commit: str,
) -> RuntimeResolution:
    """Stage reviewed conflict files without modifying the running runtime."""
    if not CONTAINER_RE.fullmatch(cid):
        raise RuntimeOverlayError("invalid running container id")
    if not SHA_RE.fullmatch(expected_sha) or not SHA_RE.fullmatch(source_commit):
        raise RuntimeOverlayError("invalid runtime resolution revision")
    current_cid, current_image = app_container(config_value)
    if cid != current_cid:
        raise RuntimeOverlayError("the runtime resolution targets another container")
    repo = Path(config_value["data_dir"]) / "platform"
    if _git(
        repo, "cat-file", "-e", f"{source_commit}^{{commit}}", check=False,
    ).returncode:
        raise RuntimeOverlayError("the reviewed runtime resolution is unavailable")
    if _git(
        repo, "merge-base", "--is-ancestor", expected_sha, source_commit,
        check=False,
    ).returncode:
        raise RuntimeOverlayError(
            "the reviewed runtime resolution does not contain the official target"
        )

    STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=STATE_DIR, prefix="runtime-resolution-prepare-",
    ) as raw:
        workspace = Path(raw)
        local = workspace / "active"
        current_raw = workspace / "current-image"
        current_source = workspace / "current-source"
        incoming = workspace / "incoming"
        base = workspace / "base"
        attempted = workspace / "attempted"
        reviewed = workspace / "reviewed"
        selected = workspace / "selected"

        active_digest = _copy_container_runtime(cid, local)
        current_digest, build_info = _copy_image_runtime(current_image, current_raw)
        current_sha = str(build_info.get("sha") or "").strip().lower()
        if not SHA_RE.fullmatch(current_sha):
            raise RuntimeOverlayError(
                "the active image does not expose an exact runtime revision"
            )
        if _extract_git_runtime(repo, current_sha, current_source) != current_digest:
            raise RuntimeOverlayError(
                "the active image runtime does not match its recorded revision"
            )
        _extract_git_runtime(repo, expected_sha, incoming)
        merge_base = _git(
            repo, "merge-base", current_sha, expected_sha, check=False,
        ).stdout.strip()
        if not SHA_RE.fullmatch(merge_base):
            raise RuntimeOverlayError(
                "the active and official runtimes have no verifiable merge base"
            )
        _extract_git_runtime(repo, merge_base, base)
        _extract_git_runtime(repo, source_commit, reviewed)
        try:
            _merge_runtime_trees(base, local, incoming, attempted)
        except RuntimeOverlayConflict as exc:
            selected_paths = exc.paths
        else:
            _attempted_digest, attempted_files = _runtime_snapshot(attempted)
            _reviewed_digest, reviewed_files = _runtime_snapshot(reviewed)
            selected_paths = tuple(sorted(
                name for name in {*attempted_files, *reviewed_files}
                if attempted_files.get(name) != reviewed_files.get(name)
            ))
            if not selected_paths:
                raise RuntimeOverlayError(
                    "the reviewed runtime matches the active official merge"
                )

        for name in selected_paths:
            source = reviewed / name
            if source.is_symlink() or not source.is_file():
                raise RuntimeOverlayError(
                    f"the reviewed resolution does not contain {name}"
                )
            target = selected / name
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copy2(source, target)
        resolution_digest = _normalize_runtime_tree(selected)

        RUNTIME_RESOLUTIONS.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(RUNTIME_RESOLUTIONS, 0o700)
        directory_name = f"resolution-{uuid.uuid4().hex}"
        directory = RUNTIME_RESOLUTIONS / directory_name
        shutil.copytree(selected, directory)
        _normalize_runtime_tree(directory)

    old_directory: Path | None = None
    try:
        old_value = read_json(RUNTIME_RESOLUTION)
        old_name = str(old_value.get("directory") or "")
        if RESOLUTION_RE.fullmatch(old_name):
            old_directory = RUNTIME_RESOLUTIONS / old_name
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    paths = tuple(sorted(selected_paths))
    _atomic_json(RUNTIME_RESOLUTION, {
        "version": 1,
        "expected_sha": expected_sha,
        "active_digest": active_digest,
        "source_commit": source_commit,
        "paths": list(paths),
        "digest": resolution_digest,
        "directory": directory_name,
    })
    if old_directory and old_directory != directory:
        shutil.rmtree(old_directory, ignore_errors=True)
    resolution = _read_runtime_resolution(expected_sha, active_digest)
    if resolution is None:
        raise RuntimeOverlayError("the staged runtime resolution could not be verified")
    write_status(
        config_value,
        operation_id=None,
        state="idle",
        expected_sha=expected_sha,
        code="runtime_overlay_resolved",
        message="A reviewed runtime conflict resolution is staged. Replace again when ready.",
    )
    return resolution


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


def verify_served_generation(
    cid: str, expected_sha: str, runtime_digest: str,
) -> None:
    """Prove the image and mounted protected runtime are the requested bytes.

    Desired source under /data may legitimately be newer than the active
    root-owned overlay, so source parity is not a cutover invariant. The Host
    instead verifies the deployed digest against its immutable generation.
    """
    result = subprocess.run(
        ["docker", "exec", cid, "curl", "-fsS",
         "http://127.0.0.1:8000/api/version"],
        text=True, capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError("the container did not expose build provenance")
    try:
        version = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("the container returned invalid build provenance") from exc
    if not isinstance(version, dict) or version.get("sha") != expected_sha:
        raise RuntimeError("the container is not serving the requested image revision")
    protected = version.get("protected_runtime")
    deployed = protected.get("deployed_sha256") if isinstance(protected, dict) else None
    if deployed != runtime_digest:
        raise RuntimeError(
            "the container's protected runtime does not match the prepared generation"
        )


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


def restart_ledger(config_value: dict, cid: str, command: str,
                   operation: str, *, image: str | None = None,
                   runtime: RuntimeGeneration | None = None) -> bool:
    """Run one root ledger command, even when the app is crash-looping."""
    invocation = ["python3", "-P", "/app/runtime/restart_ledger.py",
                  command, operation]
    result = subprocess.run(
        ["docker", "exec", cid, *invocation],
        text=True, capture_output=True,
    )
    if result.returncode == 0:
        return True
    if not image:
        return False
    # A failed replacement may not stay alive long enough for docker exec.
    # The prior verified image carries the same frozen helper; mount only the
    # persistent data root and run no entrypoint or application code.
    selected = runtime or active_runtime_generation()
    result = subprocess.run(
        ["docker", "run", "--rm", "--mount",
         f"type=bind,src={config_value['data_dir']},dst=/data",
         "--mount",
         f"type=bind,src={selected.path},dst=/app/runtime,readonly",
         "--entrypoint", "python3", image, *invocation[1:]],
        text=True, capture_output=True,
    )
    return result.returncode == 0


def request_drain(config_value: dict, operation: str, cid: str) -> None:
    if not restart_ledger(config_value, cid, "open-cutover", operation):
        raise RuntimeError("the running image does not support safe Host cutover")
    result = subprocess.run(
        ["docker", "exec", cid, "python3",
         "/data/platform/backend/scripts/prepare-container-cutover.py", operation],
        text=True, capture_output=True,
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
             code: str, detail: str, *,
             runtime: RuntimeGeneration | None = None,
             failed_runtime: RuntimeGeneration | None = None,
             retired_runtimes: tuple[RuntimeGeneration | None, ...] = ()) -> int:
    write_status(config_value, operation_id=operation, state="verifying",
                 expected_sha=expected, code=code,
                 message="Replacement failed; restoring the previous container.")
    try:
        cid, _current = app_container(config_value)
    except Exception:
        cid = ""
    handoff_rearmed = restart_ledger(
        config_value, cid, "rearm-cutover", operation, image=ROLLBACK_TAG,
        runtime=runtime,
    )
    compose(config_value, "up", "-d", "--no-build", "--no-deps",
            "--force-recreate", "app", image=ROLLBACK_TAG, runtime=runtime)
    if wait_healthy(config_value, 120):
        cid, _current = app_container(config_value)
        handoff_finalized = restart_ledger(
            config_value, cid, "finalize-cutover", operation,
            image=ROLLBACK_TAG, runtime=runtime,
        )
        if handoff_finalized:
            status_code = code
            message = f"The previous container was restored: {detail}"
        elif not handoff_rearmed:
            status_code = "handoff_rearm_failed"
            message = (
                "The previous container was restored, but exact active-chat "
                "continuation could not be re-armed; affected chats may need "
                f"manual Resume. Original failure: {detail}"
            )
        else:
            status_code = "handoff_finalize_failed"
            message = (
                "The previous container was restored, but the Host could not "
                "verify and retire the exact chat handoff receipt. Check the "
                f"affected chats. Original failure: {detail}"
            )
        write_status(config_value, operation_id=operation, state="rolled_back",
                     expected_sha=expected, code=status_code,
                     message=message[:300])
        if runtime is not None:
            _write_runtime_state(runtime, None)
        _remove_runtime_generation(failed_runtime)
        for retired in retired_runtimes:
            if retired and (runtime is None or retired.name != runtime.name):
                _remove_runtime_generation(retired)
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
    prepared_runtime: PreparedRuntime | None = None
    runtime_activated = False
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
                try:
                    active_runtime = active_runtime_generation()
                    verify_served_generation(
                        cid, expected, active_runtime.digest,
                    )
                except RuntimeError as exc:
                    write_status(
                        config_value, operation_id=operation, state="failed",
                        expected_sha=expected, code="provenance_failed",
                        message=str(exc)[:300],
                    )
                    return 1
                if _read_runtime_resolution(expected, active_runtime.digest) is None:
                    retain_images(image_ref, _image_state()["rollback_image_id"])
                    write_status(
                        config_value, operation_id=operation, state="no_change",
                        expected_sha=expected, code=None,
                        message="This container already uses that official image.",
                    )
                    return 0
            write_status(
                config_value, operation_id=operation, state="preparing",
                expected_sha=expected, code=None,
                message="Merging the active local runtime with the official image.",
            )
            prepared_runtime = prepare_runtime_overlay(
                cid, previous, image_ref,
                Path(config_value["data_dir"]) / "platform", expected,
            )
            subprocess.run(["docker", "tag", previous, ROLLBACK_TAG], check=True,
                           text=True, capture_output=True)
            request_drain(config_value, operation, cid)
            write_status(config_value, operation_id=operation, state="replacing",
                         expected_sha=expected, code=None,
                         message="Replacing the container.")
            replacement_started = True
            compose(config_value, "up", "-d", "--no-build", "--no-deps",
                    "--force-recreate", "app", image=image_ref,
                    runtime=prepared_runtime.candidate)
            write_status(config_value, operation_id=operation, state="verifying",
                         expected_sha=expected, message="Checking the new container.")
            if not wait_healthy(config_value):
                result = rollback(
                    config_value, operation, expected,
                    "health_check_failed", "the new container was unhealthy",
                    runtime=prepared_runtime.previous,
                    failed_runtime=prepared_runtime.candidate,
                    retired_runtimes=(
                        prepared_runtime.prior_active,
                        prepared_runtime.prior_rollback,
                    ),
                )
                discard_pulled_image(image_ref)
                return result
            cid, _current = app_container(config_value)
            verify_served_generation(
                cid, expected, prepared_runtime.candidate.digest,
            )
            activate_runtime_overlay(prepared_runtime)
            _consume_runtime_resolution(expected, prepared_runtime.previous.digest)
            runtime_activated = True
            handoff_finalized = restart_ledger(
                config_value, cid, "finalize-cutover", operation,
                image=image_ref, runtime=prepared_runtime.candidate,
            )
            retain_images(image_ref, previous)
            if handoff_finalized:
                status_code = None
                if prepared_runtime.carried_paths:
                    message = (
                        "Container replaced successfully; the active local "
                        "runtime overlay was carried forward."
                    )
                else:
                    message = "Container replaced successfully."
            else:
                status_code = "handoff_finalize_failed"
                message = (
                    "Container replaced successfully, but the Host could not "
                    "verify and retire the exact chat handoff receipt. Check "
                    "the affected chats."
                )
            write_status(config_value, operation_id=operation, state="succeeded",
                         expected_sha=expected, code=status_code,
                         message=message)
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
                                  "replacement_failed", detail,
                                  runtime=(
                                      prepared_runtime.previous
                                      if prepared_runtime else None
                                  ),
                                  failed_runtime=(
                                      prepared_runtime.candidate
                                      if prepared_runtime else None
                                  ),
                                  retired_runtimes=(
                                      (
                                          prepared_runtime.prior_active,
                                          prepared_runtime.prior_rollback,
                                      )
                                      if prepared_runtime else ()
                                  ))
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
        if prepared_runtime and not runtime_activated:
            _remove_runtime_generation(prepared_runtime.candidate)
            _remove_runtime_generation(prepared_runtime.previous)
        code = exc.code if isinstance(exc, RuntimeOverlayError) else "replacement_failed"
        write_status(config_value, operation_id=operation, state="failed",
                     expected_sha=expected, code=code, message=detail)
        return 1
    finally:
        claimed.unlink(missing_ok=True)
        if not request_claimed:
            # A malformed path or other claim failure must not leave the path
            # unit continuously retriggering an unrecoverable request.
            request.unlink(missing_ok=True)


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
    if (
        len(sys.argv) == 3
        and sys.argv[1] == "bootstrap-runtime"
        and os.geteuid() == 0
    ):
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        with LOCK.open("a+") as lock:
            acquire_lock(lock)
            generation = bootstrap_runtime(sys.argv[2])
        print(generation.digest)
        raise SystemExit(0)
    if (
        len(sys.argv) == 5
        and sys.argv[1] == "stage-runtime-resolution"
        and os.geteuid() == 0
    ):
        config_value = config()
        STATE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        with LOCK.open("a+") as lock:
            acquire_lock(lock)
            resolution = stage_runtime_resolution(
                config_value, sys.argv[2], sys.argv[3], sys.argv[4],
            )
        print(json.dumps({
            "expected_sha": resolution.expected_sha,
            "active_digest": resolution.active_digest,
            "source_commit": resolution.source_commit,
            "paths": list(resolution.paths),
            "digest": resolution.digest,
        }, separators=(",", ":")))
        raise SystemExit(0)
    print("invalid invocation", file=sys.stderr)
    raise SystemExit(2)
