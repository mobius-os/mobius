"""Best-effort eviction of file pages left behind by short-lived tools.

Railway accounts a container's page cache in ``memory.current``.  Large
compiler, browser, and provider executables can therefore remain billable
after their processes have exited.  This module uses POSIX_FADV_DONTNEED on
known tool files; it never deletes or rewrites a file. Source-tree sweeps skip
mapped files; exact tool executables may advise unused pages even when shared.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
import time
from collections.abc import Iterable
from pathlib import Path


log = logging.getLogger(__name__)


def _cached_file_bytes() -> int | None:
  """Cheap observation, not attribution of concurrent activity to cleanup."""
  try:
    for line in Path('/sys/fs/cgroup/memory.stat').read_text().splitlines():
      name, value = line.split()
      if name == 'file':
        return int(value)
  except (OSError, ValueError):
    pass
  return None


def _mapped_file_identities(proc_root: Path = Path("/proc")) -> set[tuple[int, int, int]]:
  mapped: set[tuple[int, int, int]] = set()
  try:
    processes = list(proc_root.iterdir())
  except OSError:
    return mapped
  for process in processes:
    if not process.name.isdigit():
      continue
    try:
      lines = (process / "maps").read_text(errors="replace").splitlines()
    except OSError:
      continue
    for line in lines:
      fields = line.split(maxsplit=5)
      if len(fields) < 5 or fields[4] == "0":
        continue
      try:
        major, minor = (int(part, 16) for part in fields[3].split(":", 1))
        mapped.add((major, minor, int(fields[4])))
      except (TypeError, ValueError):
        continue
  return mapped


def _files(paths: Iterable[Path]):
  for root in paths:
    try:
      if root.is_symlink():
        continue
      if root.is_file():
        yield root
        continue
      if not root.is_dir():
        continue
    except OSError:
      continue
    for base, _dirs, names in os.walk(root):
      for name in names:
        yield Path(base) / name


def reclaim_file_cache(
  paths: Iterable[str | Path],
  *,
  skip_mapped: bool = True,
) -> dict:
  """Advise Linux to evict clean, dormant pages beneath ``paths``.

  General sweeps skip mapped files to avoid needlessly refaulting a live
  process's code. Provider-exit cleanup may opt out: parallel agents share one
  large executable, and Linux safely retains any mapped pages it cannot evict
  while releasing unused portions left hot by the agent that just exited.
  """
  started = time.monotonic()
  before = _cached_file_bytes()
  if not hasattr(os, "posix_fadvise") or not hasattr(os, "POSIX_FADV_DONTNEED"):
    return {"files": 0, "advised_file_bytes": 0, "skipped_mapped": 0, "errors": 0,
            "supported": False}
  mapped = _mapped_file_identities() if skip_mapped else set()
  files = bytes_ = skipped = errors = 0
  seen = set()
  for path in _files(Path(value) for value in paths):
    try:
      st = path.stat(follow_symlinks=False)
      if not stat.S_ISREG(st.st_mode) or not st.st_size:
        continue
      identity = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
      if identity in seen:
        continue
      seen.add(identity)
      if identity in mapped:
        skipped += 1
        continue
      fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
      try:
        # A replacement between stat and open must not inherit the old file's
        # mapping verdict. No writes and no retries on a changing source tree.
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
          continue
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
      finally:
        os.close(fd)
      files += 1
      bytes_ += st.st_size
    except OSError:
      errors += 1
  result = {
    "files": files,
    "advised_file_bytes": bytes_,
    "skipped_mapped": skipped,
    "errors": errors,
    "supported": True,
    "duration_ms": round((time.monotonic() - started) * 1000, 2),
    "file_cache_before_bytes": before,
    "file_cache_after_bytes": _cached_file_bytes(),
  }
  log.debug('file cache advice: %s', result)
  return result


def frontend_tool_paths(frontend_dir: str | Path) -> tuple[Path, ...]:
  roots = [(Path(frontend_dir) / "node_modules").resolve()]
  node = shutil.which("node")
  if node:
    roots.append(Path(node).resolve())
  return tuple(roots)


def provider_tool_paths(provider: str) -> tuple[Path, ...]:
  if provider == "claude":
    cli = shutil.which("claude")
    return (Path(cli).resolve(),) if cli else ()
  if provider == "codex":
    cli = shutil.which("codex")
    if not cli:
      return ()
    package = Path(cli).resolve().parent.parent
    roots = list(package.glob("node_modules/@openai/codex-*/vendor/*/bin/codex*"))
    node = shutil.which("node")
    if node:
      roots.append(Path(node).resolve())
    return tuple(roots)
  return ()


def browser_tool_paths() -> tuple[Path, ...]:
  roots = [Path("/opt/agent-browser/browsers")]
  cli = shutil.which("agent-browser")
  if cli:
    roots.append(Path(cli).resolve().parent)
  for pattern in (
    "/usr/lib/*/libLLVM.so.*",
    "/usr/lib/*/libgallium-*.so",
  ):
    roots.extend(Path("/").glob(pattern.lstrip("/")))
  return tuple(roots)


def settled_turn_paths(data_dir: str | Path, chat_id: str) -> tuple[Path, ...]:
  """Return tool/source caches safe to evict once a chat turn is settled.

  Deliberately exclude databases, app data, shared files, and ``cli-auth``.
  The latter is both credential-bearing and outside the agent write surface.
  """
  data = Path(data_dir)
  roots = [
    data / "platform" / ".git",
    data / "platform" / "backend",
    # Dependency pages belong to the existing build-exit cleanup, not every
    # unrelated chat. Keep only source/publication pages at this boundary.
    data / "platform" / "frontend" / "src",
    data / "platform" / "frontend" / "dist",
    data / "platform" / "frontend" / ".dist-staging",
    data / "platform" / "frontend" / ".assets-attic",
    data / "agent-browser-profiles" / f"chat-{chat_id}",
  ]
  for pattern in ('contrib/*/worktree', 'contrib/*', 'worktrees/*', 'apps/*'):
    for checkout in data.glob(pattern):
      if checkout.is_symlink() or checkout.parent.is_symlink():
        continue
      git = checkout / '.git'
      if git.exists():
        roots.append(git)
        if checkout.parent != data / 'apps':
          roots.extend(checkout / path for path in (
            'backend', 'frontend/src', 'frontend/dist',
          ))
  return tuple(roots)


def settled_tool_paths() -> tuple[Path, ...]:
  """Exact reusable tools, rather than walking all of /usr and /opt."""
  paths = [*provider_tool_paths('claude'), *provider_tool_paths('codex'),
           *browser_tool_paths()]
  for name in ('git', 'gh', 'rg'):
    executable = shutil.which(name)
    if executable:
      paths.append(Path(executable).resolve())
  return tuple(paths)


async def reclaim_provider_cache(provider: str) -> None:
  """Optional post-exit work must not block the event loop or mask a result."""
  try:
    await asyncio.to_thread(
      reclaim_file_cache, provider_tool_paths(provider), skip_mapped=False,
    )
  except Exception:
    log.debug('provider file cache advice failed', exc_info=True)


def reclaim_settled_cache(data_dir: str | Path, chat_id: str) -> None:
  """Called by the existing settled-turn worker; no new scheduler or timer."""
  reclaim_file_cache(settled_tool_paths(), skip_mapped=False)
  reclaim_file_cache(settled_turn_paths(data_dir, chat_id))
