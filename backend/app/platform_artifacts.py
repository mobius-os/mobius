"""Immutable, self-verifying source/frontend artifacts for platform boots.

This module is deliberately stdlib-only.  The mutable backend uses it to
materialize a reviewed generation; the image-baked entrypoint executes the
same file with ``-P`` to verify and select that artifact without importing
mutable platform code.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Iterator


FORMAT_VERSION = 1
DEFAULT_ROOT = Path(os.environ.get("DATA_DIR", "/data")) / "platform-generations"
_GENERATION_NAME_LENGTH = 64
_RETAIN_UNREFERENCED = 2
_GENERATION_KEYS = {
  "version", "generation_id", "source_kind", "source_sha", "source_tree",
  "worktree_sha256", "worktree_dirty", "backend_tree",
  "frontend_signature", "dependency_sha256", "image_sha",
  "required_actions",
}


class ArtifactError(RuntimeError):
  pass


class PendingArtifactError(ArtifactError):
  """A published pending generation could not be selected safely."""


class ImageBootstrapRequired(ArtifactError):
  """A reviewed replacement image must reach readiness before its artifact."""


def artifact_dir(generation_id: str, root: Path = DEFAULT_ROOT) -> Path:
  if (
    len(generation_id) != _GENERATION_NAME_LENGTH
    or any(c not in "0123456789abcdef" for c in generation_id)
  ):
    raise ArtifactError("invalid platform generation identity")
  return root / generation_id


def _authored_paths(repo: Path) -> list[str]:
  result = subprocess.run(
    ["git", "-C", str(repo), "ls-files", "--cached", "--others",
     "--exclude-standard", "-z"],
    capture_output=True, check=False,
  )
  if result.returncode != 0:
    raise ArtifactError("could not enumerate platform source")
  return sorted({
    raw.decode("utf-8", errors="surrogateescape")
    for raw in result.stdout.split(b"\0") if raw
  })


def _safe_relative(value: str) -> Path:
  pure = PurePosixPath(value)
  if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
    raise ArtifactError(f"unsafe artifact path: {value!r}")
  return Path(*pure.parts)


def _copy_entry(source: Path, target: Path, source_root: Path) -> None:
  target.parent.mkdir(parents=True, exist_ok=True)
  info = source.lstat()
  if stat.S_ISLNK(info.st_mode):
    link_target = os.readlink(source)
    if Path(link_target).is_absolute():
      raise ArtifactError(f"artifact symlink is absolute: {source}")
    try:
      (source.parent / link_target).resolve(strict=False).relative_to(
        source_root.resolve(),
      )
    except (OSError, ValueError):
      raise ArtifactError(f"artifact symlink escapes its source tree: {source}")
    target.symlink_to(link_target)
  elif stat.S_ISREG(info.st_mode):
    shutil.copy2(source, target, follow_symlinks=False)
  else:
    raise ArtifactError(f"unsupported artifact source: {source}")


def _source_fingerprint(root: Path, paths: list[str]) -> str:
  """Mirror the generation worktree hash over copied authored paths."""
  digest = hashlib.sha256()
  for value in sorted(set(paths)):
    relative = _safe_relative(value)
    path = root / relative
    digest.update(value.encode("utf-8", errors="surrogateescape"))
    digest.update(b"\0")
    try:
      current = path.lstat()
    except OSError:
      digest.update(b"missing\0")
      continue
    digest.update(
      f"{stat.S_IFMT(current.st_mode):o}:{stat.S_IMODE(current.st_mode):o}".encode()
    )
    digest.update(b"\0")
    if stat.S_ISLNK(current.st_mode):
      digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
    elif stat.S_ISREG(current.st_mode):
      with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
          digest.update(chunk)
    else:
      digest.update(b"unsupported")
    digest.update(b"\0")
  return digest.hexdigest()


def _validate_symlinks(root: Path) -> None:
  resolved_root = root.resolve()
  for path in root.rglob("*"):
    if not path.is_symlink():
      continue
    target = Path(os.readlink(path))
    if target.is_absolute():
      raise ArtifactError(f"artifact symlink is absolute: {path.relative_to(root)}")
    try:
      (path.parent / target).resolve(strict=False).relative_to(resolved_root)
    except (OSError, ValueError):
      raise ArtifactError(f"artifact symlink escapes its root: {path.relative_to(root)}")


def _inventory(root: Path) -> list[dict]:
  records: list[dict] = []
  for path in sorted(root.rglob("*")):
    if path == root / "manifest.json" or (
      path.is_dir() and not path.is_symlink()
    ):
      continue
    relative = path.relative_to(root).as_posix()
    info = path.lstat()
    record = {"path": relative, "mode": stat.S_IMODE(info.st_mode)}
    if stat.S_ISLNK(info.st_mode):
      record.update(kind="symlink", target=os.readlink(path))
    elif stat.S_ISREG(info.st_mode):
      digest = hashlib.sha256()
      with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
          digest.update(chunk)
      record.update(kind="file", sha256=digest.hexdigest(), size=info.st_size)
    else:
      raise ArtifactError(f"unsupported artifact entry: {relative}")
    records.append(record)
  return records


def _manifest_digest(payload: dict) -> str:
  return hashlib.sha256(json.dumps(
    payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
  ).encode("utf-8")).hexdigest()


def _read_state(path: Path) -> dict:
  try:
    value = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError, TypeError) as exc:
    raise ArtifactError("generation state is unavailable") from exc
  if not isinstance(value, dict):
    raise ArtifactError("generation state is invalid")
  return value


@contextmanager
def _state_lock(path: Path) -> Iterator[None]:
  lock_path = path.with_suffix(path.suffix + ".lock")
  lock_path.parent.mkdir(parents=True, exist_ok=True)
  with lock_path.open("a+", encoding="utf-8") as handle:
    if os.geteuid() == 0:
      try:
        try:
          owner = path.stat()
          owner_uid, owner_gid = owner.st_uid, owner.st_gid
        except FileNotFoundError:
          try:
            runtime_user = pwd.getpwnam("mobius")
            owner_uid, owner_gid = runtime_user.pw_uid, runtime_user.pw_gid
          except KeyError:
            owner = path.parent.stat()
            owner_uid, owner_gid = owner.st_uid, owner.st_gid
        os.fchown(handle.fileno(), owner_uid, owner_gid)
        os.fchmod(handle.fileno(), 0o600)
      except OSError:
        pass
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
      yield
    finally:
      fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_state(path: Path, state: dict) -> None:
  state["updated_at"] = datetime.now(UTC).isoformat()
  try:
    prior = path.stat()
  except OSError:
    prior = None
  fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      json.dump(state, handle, sort_keys=True, separators=(",", ":"))
      handle.write("\n")
      handle.flush()
      os.fsync(handle.fileno())
      if prior is not None:
        os.fchmod(handle.fileno(), stat.S_IMODE(prior.st_mode))
        if hasattr(os, "fchown"):
          os.fchown(handle.fileno(), prior.st_uid, prior.st_gid)
      else:
        os.fchmod(handle.fileno(), 0o600)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
      os.fsync(directory_fd)
    finally:
      os.close(directory_fd)
  finally:
    try:
      os.unlink(temporary)
    except FileNotFoundError:
      pass


def _valid_generation(value: object) -> bool:
  if not isinstance(value, dict) or set(value) != _GENERATION_KEYS:
    return False
  generation_id = value.get("generation_id")
  if (
    not isinstance(generation_id, str)
    or len(generation_id) != _GENERATION_NAME_LENGTH
    or any(c not in "0123456789abcdef" for c in generation_id)
  ):
    return False
  unsigned = {key: value[key] for key in value if key != "generation_id"}
  return generation_id == _manifest_digest(unsigned)


def materialize(repo: Path, generation: dict, root: Path = DEFAULT_ROOT) -> tuple[Path, str]:
  """Copy one stable authored tree plus its complete frontend build."""
  destination = artifact_dir(generation["generation_id"], root)
  if destination.is_dir():
    try:
      manifest = verify(destination, generation["generation_id"])
      if manifest.get("generation") != generation:
        raise ArtifactError("generation manifest inputs do not match")
      return destination, manifest["manifest_sha256"]
    except ArtifactError:
      # Generated cache, never owner source: rebuild the exact generation when
      # a prior crash or disk fault left a corrupt directory.
      shutil.rmtree(destination)
  root.mkdir(parents=True, exist_ok=True)
  temporary = Path(tempfile.mkdtemp(prefix=".platform-generation-", dir=root))
  try:
    source_paths = _authored_paths(repo)
    for value in source_paths:
      relative = _safe_relative(value)
      source = repo / relative
      if source.exists() or source.is_symlink():
        _copy_entry(source, temporary / relative, repo)
    dist = repo / "frontend" / "dist"
    if not all((dist / name).exists() for name in (
      "assets", "index.html", "sw.js", "manifest.webmanifest",
    )):
      raise ArtifactError("frontend build is incomplete")
    target_dist = temporary / "frontend" / "dist"
    shutil.rmtree(target_dist, ignore_errors=True)
    shutil.copytree(dist, target_dist, symlinks=True)
    _validate_symlinks(temporary)
    if _source_fingerprint(temporary, source_paths) != generation.get("worktree_sha256"):
      raise ArtifactError("platform source changed while its artifact was copied")
    unsigned = {
      "version": FORMAT_VERSION,
      "generation": generation,
      "source_paths": source_paths,
      "files": _inventory(temporary),
    }
    manifest_sha = _manifest_digest(unsigned)
    manifest = {**unsigned, "manifest_sha256": manifest_sha}
    (temporary / "manifest.json").write_text(
      json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
      encoding="utf-8",
    )
    os.replace(temporary, destination)
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
      os.fsync(directory_fd)
    finally:
      os.close(directory_fd)
    return destination, manifest_sha
  except Exception:
    shutil.rmtree(temporary, ignore_errors=True)
    raise


def verify(directory: Path, generation_id: str | None = None) -> dict:
  try:
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
  except (OSError, ValueError, TypeError) as exc:
    raise ArtifactError("generation manifest is unreadable") from exc
  digest = manifest.pop("manifest_sha256", None)
  if not isinstance(digest, str) or digest != _manifest_digest(manifest):
    raise ArtifactError("generation manifest checksum failed")
  generation = manifest.get("generation")
  if not _valid_generation(generation) or (
    generation_id and generation.get("generation_id") != generation_id
  ):
    raise ArtifactError("generation manifest identity does not match")
  source_paths = manifest.get("source_paths")
  if (
    not isinstance(source_paths, list)
    or not all(isinstance(value, str) for value in source_paths)
    or _source_fingerprint(directory, source_paths)
      != generation.get("worktree_sha256")
  ):
    raise ArtifactError("generation source fingerprint does not match")
  _validate_symlinks(directory)
  if manifest.get("files") != _inventory(directory):
    raise ArtifactError("generation artifact content changed")
  return {**manifest, "manifest_sha256": digest}


def _select_state(
  state: dict,
  root: Path,
  image_sha: str | None,
  *,
  skip_pending: bool = False,
) -> dict:
  activation = state.get("activation")
  if (
    not skip_pending
    and isinstance(activation, dict)
    and activation.get("status") in {"rollback_failed", "rollback_exhausted"}
  ):
    raise PendingArtifactError("the one automatic rollback also failed")
  roles = ("active", "last_ready") if skip_pending else (
    "pending", "active", "last_ready",
  )
  for role in roles:
    generation = state.get(role)
    if role == "pending" and generation is not None and not _valid_generation(generation):
      raise PendingArtifactError("pending platform generation is invalid")
    if not _valid_generation(generation):
      continue
    if generation.get("source_kind") != "platform":
      if role == "pending":
        raise PendingArtifactError("pending platform generation has the wrong source")
      continue
    expected_image = generation.get("image_sha")
    if expected_image and image_sha and expected_image != image_sha:
      if role == "pending":
        raise PendingArtifactError("pending platform generation needs a different image")
      continue
    directory = artifact_dir(generation.get("generation_id", ""), root)
    try:
      manifest = verify(directory, generation["generation_id"])
    except ArtifactError as exc:
      if role == "pending":
        raise PendingArtifactError("pending platform artifact failed verification") from exc
      continue
    if role == "pending":
      activation = state.get("activation")
      if isinstance(activation, dict):
        expected_manifest = activation.get("manifest_sha256")
        if (
          activation.get("generation_id") == generation["generation_id"]
          and isinstance(expected_manifest, str)
          and expected_manifest != manifest["manifest_sha256"]
        ):
          raise PendingArtifactError(
            "pending platform artifact does not match its activation receipt"
          )
    return {
      "role": role,
      "generation_id": generation["generation_id"],
      "source_sha": generation.get("source_sha"),
      "backend": str(directory / "backend"),
      "frontend": str(directory / "frontend" / "dist"),
      "manifest_sha256": manifest["manifest_sha256"],
    }
  raise ArtifactError("no verified platform generation is selectable")


def select(
  state_path: Path,
  root: Path,
  image_sha: str | None,
  *,
  skip_pending: bool = False,
) -> dict:
  return _select_state(
    _read_state(state_path), root, image_sha, skip_pending=skip_pending,
  )


def _spend_failed_pending(
  state: dict,
  *,
  boot_id: str,
  stage: str,
  message: str,
) -> str | None:
  pending = state.get("pending")
  activation = state.get("activation")
  if not isinstance(activation, dict) or pending is None:
    return None
  generation_id = (
    pending.get("generation_id") if isinstance(pending, dict) else None
  ) or activation.get("generation_id")
  if activation.get("rollback_attempted") is True:
    status = "rollback_exhausted"
  else:
    activation.update(
      rollback_attempted=True,
      rollback_owner="frozen_bootstrap",
    )
    status = "rolling_back"
  target = state.get("last_ready")
  target_id = target.get("generation_id") if _valid_generation(target) else None
  activation.update(
    status=status,
    rollback_target_generation_id=target_id,
    last_failure={
      "boot_id": boot_id,
      "failed_generation_id": generation_id,
      "message": message[:500],
      "recorded_at": time.time(),
      "stage": stage,
    },
  )
  state["activation"] = activation
  state["pending"] = None
  return generation_id if isinstance(generation_id, str) else None


def select_for_boot(
  state_path: Path,
  root: Path,
  image_sha: str | None,
  boot_id: str,
) -> dict:
  """Select once, spending rollback before abandoning a failed pending boot."""
  if not re.fullmatch(r"[A-Za-z0-9._:-]{8,160}", boot_id):
    raise ArtifactError("boot identity is invalid")
  with _state_lock(state_path):
    state = _read_state(state_path)
    changed = False
    rollback_from: str | None = None
    activation = state.get("activation")
    pending = state.get("pending")
    if (
      _valid_generation(pending)
      and "image_rebuild" in pending.get("required_actions", [])
      and pending.get("image_sha") != image_sha
      and pending.get("source_sha") == image_sha
    ):
      raise ImageBootstrapRequired(
        "the reviewed replacement image must reach readiness before its "
        "platform artifact can be activated"
      )
    if (
      isinstance(activation, dict)
      and activation.get("status") == "rollback_booting"
      and activation.get("rollback_boot_id") != boot_id
    ):
      activation.update(
        status="rollback_failed",
        last_failure={
          "boot_id": activation.get("rollback_boot_id"),
          "failed_generation_id": activation.get("rollback_target_generation_id"),
          "message": "The rollback process ended before it reached readiness.",
          "recorded_at": time.time(),
          "stage": "rollback_early_boot_exit",
        },
      )
      state["activation"] = activation
      _write_state(state_path, state)
      raise PendingArtifactError("the one automatic rollback also failed")
    if (
      isinstance(activation, dict)
      and activation.get("status") in {"rollback_failed", "rollback_exhausted"}
    ):
      raise PendingArtifactError("the one automatic rollback also failed")
    if (
      isinstance(activation, dict)
      and pending is not None
      and activation.get("status") == "booting"
      and activation.get("boot_id") != boot_id
    ):
      rollback_from = _spend_failed_pending(
        state,
        boot_id=str(activation.get("boot_id") or "unknown"),
        stage="early_boot_exit",
        message="The candidate process ended before it reached readiness.",
      )
      changed = True
    try:
      selected = _select_state(state, root, image_sha)
    except PendingArtifactError as exc:
      rollback_from = _spend_failed_pending(
        state, boot_id=boot_id, stage="artifact_selection", message=str(exc),
      )
      changed = True
      try:
        selected = _select_state(state, root, image_sha, skip_pending=True)
      except ArtifactError as fallback_exc:
        _write_state(state_path, state)
        raise PendingArtifactError(
          "pending platform generation failed and no rollback artifact is usable"
        ) from fallback_exc
    activation = state.get("activation")
    if (
      isinstance(activation, dict)
      and activation.get("status") == "rolling_back"
    ):
      if (
        selected["generation_id"]
        != activation.get("rollback_target_generation_id")
      ):
        _write_state(state_path, state)
        raise PendingArtifactError("no exact last-ready rollback artifact is usable")
      activation.update(status="rollback_booting", rollback_boot_id=boot_id)
      state["activation"] = activation
      changed = True
    if rollback_from:
      selected["rollback_from_generation_id"] = rollback_from
    if selected["role"] == "pending":
      activation = state.get("activation")
      if not isinstance(activation, dict):
        raise PendingArtifactError("pending platform activation receipt is unavailable")
      activation.update(status="booting", boot_id=boot_id, boot_started_at=time.time())
      state["activation"] = activation
      changed = True
    if changed:
      _write_state(state_path, state)
    return selected


def fail_boot(
  state_path: Path,
  *,
  generation_id: str,
  boot_id: str,
  stage: str,
  message: str,
) -> bool:
  """Spend one rollback for the exact still-pending boot, before termination."""
  with _state_lock(state_path):
    state = _read_state(state_path)
    activation = state.get("activation")
    pending = state.get("pending")
    pending_boot = (
      isinstance(pending, dict)
      and pending.get("generation_id") == generation_id
      and activation.get("generation_id") == generation_id
      and activation.get("boot_id") == boot_id
      and activation.get("status") == "booting"
    ) if isinstance(activation, dict) else False
    rollback_boot = (
      isinstance(activation, dict)
      and activation.get("status") == "rollback_booting"
      and activation.get("rollback_target_generation_id") == generation_id
      and activation.get("rollback_boot_id") == boot_id
    )
    if not pending_boot and not rollback_boot:
      return False
    if rollback_boot:
      activation.update(
        status="rollback_failed",
        last_failure={
          "boot_id": boot_id,
          "failed_generation_id": generation_id,
          "message": message[:500],
          "recorded_at": time.time(),
          "stage": stage,
        },
      )
      state["activation"] = activation
    else:
      _spend_failed_pending(
        state, boot_id=boot_id, stage=stage, message=message,
      )
    _write_state(state_path, state)
    return True


def prune(root: Path, state: dict, retain_unreferenced: int = _RETAIN_UNREFERENCED) -> None:
  """Bound generated artifacts without deleting any recorded generation."""
  keep = {
    item.get("generation_id")
    for item in (state.get("pending"), state.get("active"), state.get("last_ready"))
    if isinstance(item, dict)
  }
  candidates: list[Path] = []
  try:
    entries = list(root.iterdir())
  except OSError:
    return
  for entry in entries:
    if (
      not entry.is_dir()
      or len(entry.name) != _GENERATION_NAME_LENGTH
      or any(c not in "0123456789abcdef" for c in entry.name)
      or entry.name in keep
    ):
      continue
    candidates.append(entry)
  def modified(path: Path) -> int:
    try:
      return path.stat().st_mtime_ns
    except OSError:
      return 0

  candidates.sort(key=modified, reverse=True)
  for stale in candidates[max(0, retain_unreferenced):]:
    shutil.rmtree(stale, ignore_errors=True)


def _main() -> int:
  parser = argparse.ArgumentParser()
  action = parser.add_mutually_exclusive_group(required=True)
  action.add_argument("--select", action="store_true")
  action.add_argument("--fail-boot", action="store_true")
  parser.add_argument("--state", type=Path, default=Path("/data/.platform-generation.json"))
  parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
  parser.add_argument("--image-sha")
  parser.add_argument("--skip-pending", action="store_true")
  parser.add_argument("--boot-id")
  parser.add_argument("--generation-id")
  parser.add_argument("--stage", default="early_boot")
  parser.add_argument("--message", default="The candidate did not reach readiness.")
  args = parser.parse_args()
  try:
    if args.fail_boot:
      if not args.generation_id or not args.boot_id:
        raise ArtifactError("fail-boot needs a generation and boot identity")
      return 0 if fail_boot(
        args.state,
        generation_id=args.generation_id,
        boot_id=args.boot_id,
        stage=args.stage,
        message=args.message,
      ) else 1
    result = (
      select_for_boot(args.state, args.root, args.image_sha, args.boot_id)
      if args.boot_id and not args.skip_pending
      else select(
        args.state, args.root, args.image_sha,
        skip_pending=args.skip_pending,
      )
    )
  except PendingArtifactError as exc:
    print(str(exc), file=os.sys.stderr)
    return 2
  except ImageBootstrapRequired as exc:
    print(str(exc), file=os.sys.stderr)
    return 3
  except ArtifactError as exc:
    print(str(exc), file=os.sys.stderr)
    return 1
  print(json.dumps(result, separators=(",", ":")))
  return 0


if __name__ == "__main__":
  raise SystemExit(_main())
