"""Expose Möbius's shared skills through Codex's project-local skills dir.

Claude gets first-class skill auto-loading via the SDK ``skills="all"`` option
when the owner turns skills on. Codex has no such SDK switch, but its app-server
auto-discovers skills from ``<cwd>/.codex/skills/<name>/SKILL.md`` and injects
each skill's name + description into the model-visible prompt, loading the body
only when the skill activates — the same on-demand shape Claude gets. Möbius runs
Codex turns with ``cwd = data_dir``, so mirroring the shared skills into
``<data_dir>/.codex/skills`` gives Codex the same skill parity, gated on the same
``skills_enabled`` flag.

Every project-local entry is a disposable, complete copy. A legacy flat skill
gets routing frontmatter plus its full source body; a directory skill keeps its
whole package so scripts and references remain beside ``SKILL.md``. No cache
path links back to the authoritative shared source. That last property matters
during rollback: even an older pointer writer can only overwrite the disposable
cache, never the partner's real skill files.

A manifest records the entry names this module owns, while a marker inside each
new cache entry lets a later release recover ownership if an older release
drops the manifest name. Unmanaged project-local skills and Codex's built-in
``.system`` skills are never replaced or pruned.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path

import fcntl

from app.skills import enumerate_skills

_MANIFEST = ".mobius-managed.json"
_ENTRY_MARKER = ".mobius-skill-cache.json"
_SYNC_LOCK = ".mobius-skills.lock"
_CACHE_VERSION = 1
_RENAME_EXCHANGE = 2
_UNSAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def _safe_dir_name(name: str) -> str | None:
  """A filesystem-safe skill directory name, or None if nothing usable remains."""
  cleaned = _UNSAFE.sub("-", str(name or "").strip()).strip("-.")
  if not cleaned or cleaned in (".", ".."):
    return None
  return cleaned


def _render_flat_entry(name: str, description: str, body: str) -> str:
  """A complete native Codex entry for a legacy flat shared skill."""
  if body.startswith("---\n") and "\n---\n" in body[4:]:
    return body
  desc = " ".join(str(description or "").split()) or f"The {name} skill."
  return (
    "---\n"
    f"name: {json.dumps(str(name), ensure_ascii=True)}\n"
    f"description: {json.dumps(desc, ensure_ascii=True)}\n"
    "---\n\n"
    f"{body}"
  )


def _read_manifest(target: Path) -> list[str]:
  try:
    data = json.loads((target / _MANIFEST).read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return []
  names = data.get("names") if isinstance(data, dict) else None
  return [n for n in names if isinstance(n, str)] if isinstance(names, list) else []


def _write_manifest(target: Path, names: list[str]) -> None:
  target.mkdir(parents=True, exist_ok=True)
  (target / _MANIFEST).write_text(
    json.dumps({"names": sorted(names)}, ensure_ascii=True), encoding="utf-8"
  )


def _read_entry_marker(entry: Path) -> dict | None:
  if entry.is_symlink():
    return None
  marker = entry / _ENTRY_MARKER
  try:
    if marker.is_symlink():
      return None
    data = json.loads(marker.read_text(encoding="utf-8"))
  except (OSError, ValueError):
    return None
  if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
    return None
  if data.get("kind") not in ("flat", "directory"):
    return None
  if not isinstance(data.get("digest"), str):
    return None
  return data


def _marked_entries(target: Path) -> set[str]:
  """Recover managed names from valid per-entry markers."""
  try:
    entries = list(target.iterdir())
  except OSError:
    return set()
  return {
    entry.name
    for entry in entries
    if entry.is_dir()
    and not entry.is_symlink()
    and _read_entry_marker(entry) is not None
  }


def _write_entry_marker(entry: Path, kind: str, digest: str) -> None:
  (entry / _ENTRY_MARKER).write_text(
    json.dumps(
      {"version": _CACHE_VERSION, "kind": kind, "digest": digest},
      sort_keys=True,
    ),
    encoding="utf-8",
  )


def _tree_digest(root: Path, *, ignore_marker: bool = False) -> str:
  """Hash a real, self-contained package and reject every symlink."""
  digest = hashlib.sha256()
  if not root.is_dir() or root.is_symlink():
    raise OSError("skill package must be a real directory")

  for current, dirs, files in os.walk(root, followlinks=False):
    dirs.sort()
    files.sort()
    current_path = Path(current)

    # A copied entry must never retain a path back into the authoritative tree.
    # Installed skill packages already reject symlinks; apply the same boundary
    # to owner-authored directory skills that bypass the installer.
    for name in dirs:
      path = current_path / name
      rel = path.relative_to(root).as_posix()
      if path.is_symlink():
        raise OSError(f"symlinked skill package entry: {rel}")

    for name in files:
      if ignore_marker and current_path == root and name == _ENTRY_MARKER:
        continue
      path = current_path / name
      rel = path.relative_to(root).as_posix()
      if path.is_symlink():
        raise OSError(f"symlinked skill package entry: {rel}")
      info = path.stat()
      if not stat.S_ISREG(info.st_mode):
        raise OSError(f"unsupported skill package entry: {rel}")
      executable = stat.S_IMODE(info.st_mode) & 0o111
      digest.update(f"F\0{rel}\0{executable:o}\0{info.st_size}\0".encode())
      with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
          digest.update(chunk)
      digest.update(b"\0")
  return digest.hexdigest()


def _flat_digest(content: str) -> str:
  digest = hashlib.sha256()
  payload = content.encode("utf-8")
  digest.update(f"F\0SKILL.md\0{0:o}\0{len(payload)}\0".encode())
  digest.update(payload)
  digest.update(b"\0")
  return digest.hexdigest()


def _remove_managed_entry(target: Path, name: str) -> bool:
  """Remove one cache entry without ever following a link to shared source."""
  entry = target / name
  if entry.is_symlink():
    try:
      entry.unlink()
    except OSError:
      return False
    return True
  if not entry.exists():
    return True

  if _read_entry_marker(entry) is not None:
    try:
      shutil.rmtree(entry)
    except OSError:
      return False
    return True

  # Legacy pointer/flat caches were real directories containing only SKILL.md.
  # Remove that exact old shape; unexpected files prove it is not safe to own.
  try:
    children = list(entry.iterdir())
  except OSError:
    return False
  if len(children) != 1 or children[0].name != "SKILL.md":
    return False
  try:
    children[0].unlink()
    entry.rmdir()
  except OSError:
    return False
  return True


def _prune(target: Path, names: list[str]) -> None:
  """Remove only entries recorded as managed by the prior sync."""
  for name in names:
    _remove_managed_entry(target, name)


def _entry_matches(entry: Path, kind: str, digest: str) -> bool:
  """Whether a cache entry is complete and byte-for-byte current."""
  marker = _read_entry_marker(entry)
  if marker != {"version": _CACHE_VERSION, "kind": kind, "digest": digest}:
    return False
  try:
    return _tree_digest(entry, ignore_marker=True) == digest
  except OSError:
    return False


def _atomic_exchange(left: Path, right: Path) -> None:
  """Atomically exchange two paths on the Linux deployment filesystem."""
  libc = ctypes.CDLL(None, use_errno=True)
  renameat2 = getattr(libc, "renameat2", None)
  if renameat2 is None:
    raise OSError("renameat2 is unavailable")
  renameat2.argtypes = [
    ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p,
    ctypes.c_uint,
  ]
  renameat2.restype = ctypes.c_int
  result = renameat2(
    -100, os.fsencode(left), -100, os.fsencode(right), _RENAME_EXCHANGE,
  )
  if result != 0:
    error = ctypes.get_errno()
    raise OSError(error, os.strerror(error))


def _discard_path(path: Path) -> None:
  if path.is_symlink():
    path.unlink(missing_ok=True)
  elif path.exists():
    shutil.rmtree(path)


def _materialize_entry(
  target: Path,
  entry: Path,
  source: Path,
  kind: str,
  digest: str,
  flat_content: str | None,
) -> None:
  """Publish one verified, rollback-safe project-local cache entry."""
  temp = Path(tempfile.mkdtemp(prefix=f".{entry.name}-", dir=target))
  try:
    if kind == "directory":
      shutil.copytree(source.parent, temp, dirs_exist_ok=True, symlinks=True)
    else:
      if flat_content is None:
        raise OSError("flat skill content unavailable")
      (temp / "SKILL.md").write_text(flat_content, encoding="utf-8")
    _write_entry_marker(temp, kind, digest)
    if _tree_digest(temp, ignore_marker=True) != digest:
      raise OSError("skill source changed while its cache entry was copied")
    if entry.exists() or entry.is_symlink():
      # After the exchange `entry` is the complete new cache and `temp` is the
      # old one. Readers see one valid directory before and after the syscall,
      # and cleanup failure can only leave a hidden stale sibling.
      _atomic_exchange(temp, entry)
      _discard_path(temp)
    else:
      temp.replace(entry)
  except Exception:
    try:
      _discard_path(temp)
    except OSError:
      pass
    raise


@contextmanager
def _sync_lock(data_dir: str | Path):
  """Serialize the shared cache across concurrent chat starts."""
  codex_dir = Path(data_dir) / ".codex"
  codex_dir.mkdir(parents=True, exist_ok=True)
  with (codex_dir / _SYNC_LOCK).open("a+b") as handle:
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    try:
      yield
    finally:
      fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _sync_codex_skills(data_dir: str | Path, enabled: bool) -> list[str]:
  """Idempotently copy skills while the caller owns ``_sync_lock``.

  Returns the sorted skill names currently materialized (empty when disabled).
  Copies change only when source or cache bytes differ. Any I/O error on one
  skill is skipped rather than aborting the turn; discovery is advisory.
  """
  target = Path(data_dir) / ".codex" / "skills"
  previously_managed = sorted(
    set(_read_manifest(target)) | _marked_entries(target),
  )

  if not enabled:
    _prune(target, previously_managed)
    if target.exists():
      _write_manifest(target, [])
    return []

  try:
    skills = enumerate_skills(Path(data_dir) / "shared" / "skills")
  except Exception:
    return []

  wanted = {}
  for skill in skills:
    safe = _safe_dir_name(skill.name)
    if not safe or safe in wanted:
      continue
    wanted[safe] = skill

  target.mkdir(parents=True, exist_ok=True)
  previously_owned = set(previously_managed)
  materialized: list[str] = []
  for safe, skill in wanted.items():
    entry = target / safe
    try:
      source = skill.read_path.resolve(strict=True)
      if skill.is_dir:
        kind = "directory"
        flat_content = None
        source_digest = _tree_digest(source.parent)
      else:
        kind = "flat"
        flat_content = _render_flat_entry(
          skill.name,
          skill.description,
          source.read_text(encoding="utf-8"),
        )
        source_digest = _flat_digest(flat_content)
    except OSError:
      continue

    if _entry_matches(entry, kind, source_digest):
      materialized.append(safe)
      continue

    # A valid cache marker remains authoritative even if an older release
    # stripped the manifest name while trying to process this newer cache.
    owned = safe in previously_owned or _read_entry_marker(entry) is not None
    if not owned and (entry.exists() or entry.is_symlink()):
      continue

    try:
      _materialize_entry(
        target, entry, source, kind, source_digest, flat_content,
      )
    except OSError:
      continue
    if _entry_matches(entry, kind, source_digest):
      materialized.append(safe)

  _prune(target, [n for n in previously_managed if n not in wanted])
  names = sorted(materialized)
  _write_manifest(target, names)
  return names


def sync_codex_skills(data_dir: str | Path, enabled: bool) -> list[str]:
  """Serialize and materialize the project-local Codex skill inventory."""
  try:
    with _sync_lock(data_dir):
      return _sync_codex_skills(data_dir, enabled)
  except OSError:
    return []


def sync_codex_skills_for_prompt(data_dir: str | Path, enabled: bool) -> bool:
  """Sync once and report whether native discovery covers every shared skill.

  A partial cache is still useful to Codex, but it cannot replace Möbius's
  bounded fallback inventory without making the skipped skills undiscoverable.
  """
  try:
    expected = len(enumerate_skills(Path(data_dir) / "shared" / "skills"))
  except Exception:
    return False
  materialized = sync_codex_skills(data_dir, enabled)
  return enabled and len(materialized) == expected


async def codex_native_skills_ready(data_dir: str | Path, enabled: bool) -> bool:
  """Sync the locked filesystem cache without blocking the server event loop."""
  return await asyncio.to_thread(sync_codex_skills_for_prompt, data_dir, enabled)
