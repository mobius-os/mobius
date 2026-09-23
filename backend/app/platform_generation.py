"""Durable identity for one complete platform generation.

Git ``HEAD`` is not enough: Möbius deliberately permits an editable checkout,
and the running image, dependency inputs, and published frontend can move on
different schedules.  This module gives restart/update ownership one narrow
identity that covers every input a fresh process is expected to load.

The state file is coordination metadata, not owner data.  It is versioned,
atomically replaced, and guarded by a cross-process flock because the running
backend and the next container boot both participate.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator, TypedDict

from app import platform_activation


PLATFORM_REPO = Path(os.environ.get("MOBIUS_PLATFORM_DIR", "/data/platform"))
STATE_PATH = Path(os.environ.get("DATA_DIR", "/data")) / ".platform-generation.json"
LOCK_PATH = STATE_PATH.with_suffix(STATE_PATH.suffix + ".lock")
SCHEMA_VERSION = 1
_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")


class PlatformGeneration(TypedDict):
  """Immutable inputs one fresh platform process is expected to load."""

  version: int
  generation_id: str
  source_kind: str
  source_sha: str | None
  source_tree: str | None
  worktree_sha256: str | None
  worktree_dirty: bool
  backend_tree: str | None
  frontend_signature: str | None
  dependency_sha256: str
  image_sha: str | None
  required_actions: list[str]


class PlatformGenerationState(TypedDict):
  """One pending/active/last-ready generation and rollback ownership."""

  version: int
  pending: PlatformGeneration | None
  active: PlatformGeneration | None
  last_ready: PlatformGeneration | None
  activation: dict | None
  updated_at: str


class GenerationUnavailable(RuntimeError):
  """The exact source generation cannot be proven."""


class GenerationChanged(RuntimeError):
  """The checkout changed after the owner reviewed the generation."""


def _git(
  repo: Path, *args: str, timeout: int = 60,
) -> subprocess.CompletedProcess:
  env = os.environ.copy()
  for name in (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
  ):
    env.pop(name, None)
  return subprocess.run(
    ["git", "-C", str(repo), *args],
    capture_output=True, text=False, timeout=timeout, check=False, env=env,
  )


def _git_oid(repo: Path, expression: str) -> str | None:
  result = _git(repo, "rev-parse", "--verify", "--quiet", expression)
  value = result.stdout.decode("ascii", errors="ignore").strip().lower()
  return value if result.returncode == 0 and _SHA_RE.fullmatch(value) else None


def _hash_files(root: Path, paths: list[str]) -> str:
  digest = hashlib.sha256()
  for relative in sorted(set(paths)):
    path = root / relative
    digest.update(relative.encode("utf-8", errors="surrogateescape"))
    digest.update(b"\0")
    try:
      current = path.lstat()
    except OSError:
      digest.update(b"missing\0")
      continue
    digest.update(f"{stat.S_IFMT(current.st_mode):o}:{stat.S_IMODE(current.st_mode):o}".encode())
    digest.update(b"\0")
    try:
      if stat.S_ISLNK(current.st_mode):
        digest.update(os.readlink(path).encode("utf-8", errors="surrogateescape"))
      elif stat.S_ISREG(current.st_mode):
        with path.open("rb") as handle:
          while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
      else:
        digest.update(b"unsupported")
    except OSError as exc:
      raise GenerationUnavailable(
        f"Platform source changed while its generation was being read: {relative}"
      ) from exc
    digest.update(b"\0")
  return digest.hexdigest()


def _worktree_identity(repo: Path) -> tuple[str, bool]:
  def sample() -> tuple[str | None, bytes, bytes, str]:
    files = _git(
      repo, "ls-files", "--cached", "--others", "--exclude-standard", "-z",
    )
    status = _git(
      repo, "status", "--porcelain=v1", "-z", "--untracked-files=all",
    )
    if files.returncode != 0 or status.returncode != 0:
      raise GenerationUnavailable("The platform Git checkout could not be inspected.")
    paths = [
      raw.decode("utf-8", errors="surrogateescape")
      for raw in files.stdout.split(b"\0") if raw
    ]
    return _git_oid(repo, "HEAD"), files.stdout, status.stdout, _hash_files(repo, paths)

  first = sample()
  second = sample()
  if not first[0] or first != second:
    raise GenerationUnavailable(
      "Platform source changed while its generation was being prepared."
    )
  return first[3], bool(first[2])


def _dependency_fingerprint(repo: Path) -> str:
  return _hash_files(
    repo, platform_activation.dependency_fingerprint_paths(repo),
  )


def _frontend_signature(repo: Path) -> str | None:
  try:
    value = (repo / "frontend" / ".source-build-signature").read_text(
      encoding="utf-8",
    ).strip()
  except OSError:
    return None
  return value or None


def _image_sha() -> str | None:
  try:
    payload = json.loads(Path(
      os.environ.get("MOBIUS_BUILD_INFO_PATH", "/app/build-info.json"),
    ).read_text(encoding="utf-8"))
  except (OSError, ValueError, TypeError):
    return None
  value = str(payload.get("sha") or payload.get("build_sha") or "").lower()
  return value if _SHA_RE.fullmatch(value) else None


def checkout_generation(
  repo: Path = PLATFORM_REPO,
  *,
  required_actions: list[str] | None = None,
) -> PlatformGeneration:
  """Return a stable identity for the exact editable checkout bytes.

  The working-tree hash intentionally includes tracked and untracked authored
  files.  A later edit therefore changes the restart identity even when HEAD
  does not move; ignored build/runtime trees remain outside the contract.
  """
  source_sha = _git_oid(repo, "HEAD")
  source_tree = _git_oid(repo, "HEAD^{tree}")
  backend_tree = _git_oid(repo, "HEAD:backend")
  if not source_sha or not source_tree or not backend_tree:
    raise GenerationUnavailable("The platform checkout has no complete Git source identity.")
  worktree_sha256, worktree_dirty = _worktree_identity(repo)
  actions = sorted(set(required_actions or []))
  unsigned = {
    "version": SCHEMA_VERSION,
    "source_kind": "platform",
    "source_sha": source_sha,
    "source_tree": source_tree,
    "worktree_sha256": worktree_sha256,
    "worktree_dirty": worktree_dirty,
    "backend_tree": backend_tree,
    "frontend_signature": _frontend_signature(repo),
    "dependency_sha256": _dependency_fingerprint(repo),
    "image_sha": _image_sha(),
    "required_actions": actions,
  }
  generation_id = hashlib.sha256(json.dumps(
    unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
  ).encode("utf-8")).hexdigest()
  return PlatformGeneration(generation_id=generation_id, **unsigned)


def baked_generation(
  source_sha: str | None,
  *,
  required_actions: list[str] | None = None,
) -> PlatformGeneration:
  """Identity for a complete baked fallback when the editable tree is absent."""
  clean_sha = source_sha.lower() if source_sha and _SHA_RE.fullmatch(source_sha.lower()) else None
  actions = sorted(set(required_actions or []))
  unsigned = {
    "version": SCHEMA_VERSION,
    "source_kind": "baked",
    "source_sha": clean_sha,
    "source_tree": None,
    "worktree_sha256": None,
    "worktree_dirty": False,
    "backend_tree": None,
    "frontend_signature": None,
    "dependency_sha256": hashlib.sha256(b"baked").hexdigest(),
    "image_sha": _image_sha(),
    "required_actions": actions,
  }
  generation_id = hashlib.sha256(json.dumps(
    unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
  ).encode("utf-8")).hexdigest()
  return PlatformGeneration(generation_id=generation_id, **unsigned)


def _empty_state() -> PlatformGenerationState:
  return PlatformGenerationState(
    version=SCHEMA_VERSION,
    pending=None,
    active=None,
    last_ready=None,
    activation=None,
    updated_at=datetime.now(UTC).isoformat(),
  )


def _valid_generation(value: object) -> PlatformGeneration | None:
  if not isinstance(value, dict):
    return None
  keys = {
    "version", "generation_id", "source_kind", "source_sha", "source_tree",
    "worktree_sha256", "worktree_dirty", "backend_tree",
    "frontend_signature", "dependency_sha256", "image_sha",
    "required_actions",
  }
  if set(value) != keys or value.get("version") != SCHEMA_VERSION:
    return None
  if (
    not isinstance(value.get("generation_id"), str)
    or not re.fullmatch(r"[0-9a-f]{64}", value["generation_id"])
    or value.get("source_kind") not in {"platform", "baked"}
    or not isinstance(value.get("worktree_dirty"), bool)
    or not isinstance(value.get("dependency_sha256"), str)
    or not re.fullmatch(r"[0-9a-f]{64}", value["dependency_sha256"])
    or not isinstance(value.get("required_actions"), list)
    or not all(isinstance(item, str) for item in value["required_actions"])
  ):
    return None
  for key in (
    "source_sha", "source_tree", "worktree_sha256", "backend_tree",
    "frontend_signature", "image_sha",
  ):
    if value.get(key) is not None and not isinstance(value[key], str):
      return None
  normalized = {key: value[key] for key in keys}
  generation_id = normalized.pop("generation_id")
  expected = hashlib.sha256(json.dumps(
    normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
  ).encode("utf-8")).hexdigest()
  if generation_id != expected:
    return None
  return PlatformGeneration(generation_id=generation_id, **normalized)


def _read_unlocked(path: Path) -> PlatformGenerationState:
  try:
    payload = json.loads(path.read_text(encoding="utf-8"))
  except (OSError, ValueError, TypeError):
    return _empty_state()
  if not isinstance(payload, dict) or payload.get("version") != SCHEMA_VERSION:
    return _empty_state()
  return PlatformGenerationState(
    version=SCHEMA_VERSION,
    pending=_valid_generation(payload.get("pending")),
    active=_valid_generation(payload.get("active")),
    last_ready=_valid_generation(payload.get("last_ready")),
    activation=(payload.get("activation") if isinstance(payload.get("activation"), dict) else None),
    updated_at=str(payload.get("updated_at") or datetime.now(UTC).isoformat()),
  )


def _write_unlocked(path: Path, state: PlatformGenerationState) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  state["updated_at"] = datetime.now(UTC).isoformat()
  fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      json.dump(state, handle, sort_keys=True, separators=(",", ":"))
      handle.write("\n")
      handle.flush()
      os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
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


@contextmanager
def _state_lock(lock_path: Path = LOCK_PATH) -> Iterator[None]:
  lock_path.parent.mkdir(parents=True, exist_ok=True)
  with lock_path.open("a+", encoding="utf-8") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
      yield
    finally:
      fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def generation_state(path: Path = STATE_PATH) -> PlatformGenerationState:
  with _state_lock(path.with_suffix(path.suffix + ".lock")):
    return _read_unlocked(path)


def prepare_generation(
  generation: PlatformGeneration,
  *,
  operation_id: str | None = None,
  repo: Path = PLATFORM_REPO,
  path: Path = STATE_PATH,
) -> PlatformGenerationState:
  """Durably stage one generation without changing the active generation."""
  from app.platform_artifacts import DEFAULT_ROOT, materialize, prune

  artifacts_root = DEFAULT_ROOT if path == STATE_PATH else path.parent / "platform-generations"
  _artifact, manifest_sha256 = materialize(
    repo, generation, root=artifacts_root,
  )
  require_generation(
    generation["generation_id"],
    repo=repo,
    required_actions=generation["required_actions"],
  )
  with _state_lock(path.with_suffix(path.suffix + ".lock")):
    state = _read_unlocked(path)
    state["pending"] = generation
    state["activation"] = {
      "generation_id": generation["generation_id"],
      "operation_id": operation_id,
      "status": "pending",
      "rollback_attempted": False,
      "manifest_sha256": manifest_sha256,
    }
    _write_unlocked(path, state)
  prune(artifacts_root, state)
  return state


def require_generation(
  expected_generation_id: str,
  *,
  repo: Path = PLATFORM_REPO,
  required_actions: list[str] | None = None,
) -> PlatformGeneration:
  """Fail when current authored bytes differ from the reviewed generation."""
  current = checkout_generation(repo, required_actions=required_actions)
  if current["generation_id"] != expected_generation_id:
    raise GenerationChanged(
      "Restart stopped: platform source changed after this Restart card was "
      "prepared. Review the current changes and request a new restart."
    )
  return current


def record_ready_generation(
  generation: PlatformGeneration,
  *,
  boot_id: str,
  path: Path = STATE_PATH,
) -> PlatformGenerationState:
  """Promote one proven-ready generation and retire the prior pending claim.

  An exact match completed the requested activation. A different ready
  generation means the pending candidate was overtaken before boot (for
  example, another committed change landed during a legacy restart). It did
  not activate and must be recorded as superseded rather than left pending
  forever or misreported as successful.
  """
  with _state_lock(path.with_suffix(path.suffix + ".lock")):
    state = _read_unlocked(path)
    state["active"] = generation
    state["last_ready"] = generation
    pending = state.get("pending")
    if pending:
      state["pending"] = None
      activation = state.get("activation") or {}
      if pending["generation_id"] == generation["generation_id"]:
        activation.update(status="ready", boot_id=boot_id)
      elif (
        "image_rebuild" in pending.get("required_actions", [])
        and generation.get("source_kind") == "baked"
        and pending.get("source_sha") == generation.get("source_sha")
        and pending.get("image_sha") != generation.get("image_sha")
      ):
        activation.update(
          status="image_bootstrap_ready",
          boot_id=boot_id,
          observed_generation_id=generation["generation_id"],
        )
      else:
        activation.update(
          status="superseded_on_boot",
          boot_id=boot_id,
          observed_generation_id=generation["generation_id"],
        )
      state["activation"] = activation
    else:
      activation = state.get("activation") or {}
      if activation.get("status") == "rollback_booting":
        if (
          activation.get("rollback_target_generation_id")
          == generation["generation_id"]
        ):
          activation.update(status="rolled_back_ready", boot_id=boot_id)
        else:
          activation.update(
            status="rollback_failed",
            boot_id=boot_id,
            observed_generation_id=generation["generation_id"],
          )
        state["activation"] = activation
    _write_unlocked(path, state)
    return state


def spend_rollback(
  generation_id: str,
  *,
  owner: str,
  path: Path = STATE_PATH,
) -> PlatformGeneration | None:
  """Spend the one rollback attempt before its owner performs any mutation."""
  with _state_lock(path.with_suffix(path.suffix + ".lock")):
    state = _read_unlocked(path)
    activation = state.get("activation") or {}
    if activation.get("generation_id") != generation_id:
      return None
    if activation.get("rollback_attempted") is True:
      return None
    activation.update(
      rollback_attempted=True,
      rollback_owner=owner,
      status="rolling_back",
    )
    state["activation"] = activation
    _write_unlocked(path, state)
    return state.get("last_ready")
