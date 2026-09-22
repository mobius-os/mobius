"""Best-effort eviction of file pages left behind by short-lived tools.

Railway accounts a container's page cache in ``memory.current``.  Large
compiler, browser, and provider executables can therefore remain billable
after their processes have exited.  This module uses POSIX_FADV_DONTNEED on
known tool files; it never deletes or rewrites a file, and it skips every file
currently mapped by any process in the container.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from pathlib import Path


def _file_identity(path: Path) -> tuple[int, int, int]:
  st = path.stat(follow_symlinks=False)
  return os.major(st.st_dev), os.minor(st.st_dev), st.st_ino


def _mapped_file_identities(proc_root: Path = Path("/proc")) -> set[tuple[int, int, int]]:
  mapped: set[tuple[int, int, int]] = set()
  try:
    processes = proc_root.iterdir()
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
) -> dict[str, int]:
  """Advise Linux to evict clean, dormant pages beneath ``paths``.

  General sweeps skip mapped files to avoid needlessly refaulting a live
  process's code. Provider-exit cleanup may opt out: parallel agents share one
  large executable, and Linux safely retains any mapped pages it cannot evict
  while releasing unused portions left hot by the agent that just exited.
  """
  if not hasattr(os, "posix_fadvise") or not hasattr(os, "POSIX_FADV_DONTNEED"):
    return {"files": 0, "bytes": 0, "skipped_mapped": 0, "errors": 0}
  mapped = _mapped_file_identities() if skip_mapped else set()
  files = bytes_ = skipped = errors = 0
  for path in _files(Path(value) for value in paths):
    try:
      identity = _file_identity(path)
      if identity in mapped:
        skipped += 1
        continue
      st = path.stat(follow_symlinks=False)
      if not st.st_size or not path.is_file():
        continue
      fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
      try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
      finally:
        os.close(fd)
      files += 1
      bytes_ += st.st_size
    except OSError:
      errors += 1
  return {
    "files": files,
    "bytes": bytes_,
    "skipped_mapped": skipped,
    "errors": errors,
  }


def frontend_tool_paths(frontend_dir: str | Path) -> tuple[Path, ...]:
  roots = [Path(frontend_dir) / "node_modules"]
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
    data / "platform" / "frontend" / "node_modules",
    data / "platform" / "frontend" / "dist",
    data / "platform" / "frontend" / ".dist-staging",
    data / "platform" / "frontend" / ".assets-attic",
    data / "agent-browser-profiles" / f"chat-{chat_id}",
  ]
  for parent in (data / "contrib", data / "apps"):
    try:
      children = parent.iterdir()
    except OSError:
      continue
    for child in children:
      git = child / ".git"
      if git.exists():
        roots.append(git)
  return tuple(roots)
