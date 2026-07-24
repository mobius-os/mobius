"""Demand-built frontend watcher + atomic generation publisher.

A lightweight polling observer watches frontend source. After edits settle, a
one-shot Vite build writes only to ``.dist-staging`` and exits, releasing its
Rollup graph. This module then copies staging to ``.dist-next``, validates the
complete Vite shape, and swaps ``.dist-next`` into ``dist`` through the existing
attic hook. Builds are serialized; an edit during a build requests one rerun.

Failure handling:
- Build failures are logged and leave the previous generation served.
- Broken staging never touches ``dist``; a failed publish leaves the previous
  generation served.
- Container shutdown calls ``close()``, which SIGTERMs the Vite process group.
"""

from __future__ import annotations

import asyncio
import fcntl
import filecmp
import hashlib
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserverVFS

log = logging.getLogger(__name__)

_DEBOUNCE_SECS = 1.75
_INCOMPLETE_GRACE_SECS = 30.0
_WATCH_RESTART_BACKOFF_MAX = 30.0
_WATCH_LEASE_RETRY_INITIAL = 1.0
_ROOT_SOURCE_FILES = {
  "index.html", "package-lock.json", "package.json", "vite.config.js",
}
_SOURCE_DIRS = {"public", "src"}
_CONFLICT_SCAN_SUFFIXES = {
  ".cjs", ".css", ".html", ".js", ".json", ".jsx", ".md", ".mjs",
  ".svg", ".ts", ".tsx", ".txt", ".webmanifest", ".xml", ".yaml", ".yml",
}
_CONFLICT_MARKER_PREFIXES = (b"<<<<<<<", b">>>>>>>")
_FRONTEND_DIR = Path(os.environ.get(
  "MOBIUS_FRONTEND_DIR", "/data/platform/frontend",
))
_DIST_DIR = _FRONTEND_DIR / "dist"
_STAGING_DIST_DIR = _FRONTEND_DIR / ".dist-staging"
_REBUILD_DIST_DIR = _FRONTEND_DIR / ".dist-rebuild"
_NEXT_DIST_DIR = _FRONTEND_DIR / ".dist-next"
_OLD_DIST_DIR = _FRONTEND_DIR / ".dist-old"
_ATTIC_DIR = _FRONTEND_DIR / ".assets-attic"
# A running tab deliberately defers shell reloads while its owner is typing,
# steering, or reading a live reply. Agent edits can publish many generations
# during that one foreground session, and lazy chunks (Settings is the common
# case) are fetched only when first opened. Three generations lasted less than
# two minutes during a real multi-file refactor. Sixty-four keeps roughly an
# hour of that unusually rapid edit cadence while remaining a hard, predictable
# disk bound (today's complete hashed asset set is about 1.3 MiB/generation).
_ATTIC_KEEP = 64
_BUILT_GLOBAL_CHECK = (
  _FRONTEND_DIR / "scripts" / "check-built-globals.mjs"
)
_CACHE_DIR = _FRONTEND_DIR / ".vite-cache"
_TMP_DIR = _FRONTEND_DIR / ".vite-tmp"
# The explicit full-rebuild path gets its own cache/temp dirs: rebuild_shell.sh
# runs it in a SEPARATE process while the warm watch may be building, and
# vite's transform cache is not designed for concurrent writers.
_REBUILD_CACHE_DIR = _FRONTEND_DIR / ".vite-cache-rebuild"
_REBUILD_TMP_DIR = _FRONTEND_DIR / ".vite-tmp-rebuild"
# In-process serialization only — rebuild_shell.sh publishes from its own
# process, so _publish_built_dir additionally takes an OS-level flock (derived
# from _FRONTEND_DIR at call time so tests that repoint the dir stay isolated).
_PUBLISH_LOCK = threading.RLock()
_ACTIVE_LOCK = threading.Lock()
_START_LOCK = threading.Lock()
_ACTIVE_WATCHER: "_FrontendHandler | None" = None
_ACTIVE_SUPERVISOR: "_FrontendSupervisor | None" = None


def _source_tree_scandir(
  frontend_root: Path,
  path: str | None,
) -> Iterator[os.DirEntry[str]]:
  """Expose only build inputs to watchdog's once-per-second snapshot.

  A recursive observer rooted at the editable frontend would otherwise stat
  node_modules, build generations, caches, and git metadata even though none of
  those paths can trigger a source build. At the root, admit only the explicit
  config inputs and source directories; beneath source, prune hidden trees.
  """
  scan_path = path or "."
  try:
    at_root = Path(scan_path).resolve() == frontend_root.resolve()
    entries = os.scandir(scan_path)
  except OSError:
    return
  with entries:
    for entry in entries:
      if at_root:
        if entry.name not in _ROOT_SOURCE_FILES | _SOURCE_DIRS:
          continue
      elif entry.name.startswith(".") or entry.name == "node_modules":
        continue
      yield entry


def _is_frontend_source_path(path: str | Path) -> bool:
  try:
    rel = Path(path).resolve().relative_to(_FRONTEND_DIR.resolve())
  except (OSError, RuntimeError, ValueError):
    return False
  if len(rel.parts) == 1:
    return rel.name in _ROOT_SOURCE_FILES
  return bool(rel.parts and rel.parts[0] in _SOURCE_DIRS)


def _source_paths() -> list[Path]:
  """Return the files that can affect a frontend build."""
  paths = [
    _FRONTEND_DIR / name for name in sorted(_ROOT_SOURCE_FILES)
    if (_FRONTEND_DIR / name).is_file()
  ]
  for dirname in sorted(_SOURCE_DIRS):
    root = _FRONTEND_DIR / dirname
    if not root.is_dir():
      continue
    paths.extend(
      path for path in root.rglob("*")
      if path.is_file()
      and not any(part.startswith(".") or part == "node_modules"
                  for part in path.relative_to(root).parts)
    )
  return sorted(paths)


def _source_snapshot() -> tuple[str, int]:
  """Return a cheap source identity and newest input mtime."""
  digest = hashlib.sha256()
  newest_ns = 0
  for path in _source_paths():
    try:
      stat = path.stat()
      rel = path.relative_to(_FRONTEND_DIR).as_posix()
    except (OSError, ValueError):
      continue
    newest_ns = max(newest_ns, stat.st_mtime_ns)
    digest.update(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
  return digest.hexdigest(), newest_ns


def _source_conflict_markers() -> tuple[str, ...]:
  """Return build inputs containing a Git conflict boundary at line start.

  Restrict the scan to text-like inputs so a transient merge conflict is much
  cheaper to reject than a Vite process, without reading large binary assets
  under ``public``. Git writes conflict boundaries at column zero; requiring
  an opener or closer there avoids matching marker text inside source strings.
  """
  conflicts: list[str] = []
  for path in _source_paths():
    if path.suffix.lower() not in _CONFLICT_SCAN_SUFFIXES:
      continue
    try:
      with path.open("rb") as source:
        if any(
          line.startswith(_CONFLICT_MARKER_PREFIXES) for line in source
        ):
          conflicts.append(path.relative_to(_FRONTEND_DIR).as_posix())
    except (OSError, ValueError):
      # The before/after snapshots below turn ordinary edit races into a cheap
      # retry. A genuinely unreadable stable input is still reported by Vite.
      continue
  return tuple(conflicts)


def _stable_source_preflight() -> tuple[str, tuple[str, ...]] | None:
  """Return a stable source signature and its conflict-marker inputs.

  Files can change while the marker scan is reading them. Do not launch Vite
  for that indeterminate snapshot; the demand-build loop will debounce and
  retry it just like any other racing edit.
  """
  before, _ = _source_snapshot()
  conflicts = _source_conflict_markers()
  after, _ = _source_snapshot()
  if before != after:
    return None
  return after, conflicts


def _source_stamp_path() -> Path:
  return _FRONTEND_DIR / ".source-build-signature"


def _write_source_stamp(signature: str) -> None:
  _source_stamp_path().write_text(signature + "\n", encoding="utf-8")


def _startup_build_needed() -> bool:
  """Avoid a full Vite build when served output already matches source.

  Demand builds leave an exact cheap signature. Existing image builds predate
  that marker, so seed it only when a complete dist is newer than every source
  input. A missing/stale dist or any newer source edit still gets the normal
  boot-time recovery build.
  """
  signature, newest_source_ns = _source_snapshot()
  dist_complete = _complete_build(_DIST_DIR)
  if dist_complete:
    try:
      if _source_stamp_path().read_text(encoding="utf-8").strip() == signature:
        return False
    except OSError:
      pass
  if dist_complete:
    try:
      oldest_completion_ns = min(
        (_DIST_DIR / "index.html").stat().st_mtime_ns,
        (_DIST_DIR / "sw.js").stat().st_mtime_ns,
      )
    except OSError:
      oldest_completion_ns = 0
    if newest_source_ns <= oldest_completion_ns:
      try:
        _write_source_stamp(signature)
      except OSError:
        log.warning("could not seed frontend source signature")
      return False
  return True


class _StagingChangedDuringPublish(RuntimeError):
  """Raised when Vite mutates staging while publication is copying it."""


class _IncompleteBuild(RuntimeError):
  """Raised when the watched staging tree is not a full Vite generation yet."""


class _BuiltGlobalValidationError(RuntimeError):
  """Raised when a built shell still references an undeclared identifier."""


def _acquire_watch_lock():
  """Lease the editable frontend to one warm watcher process.

  The in-process ``_ACTIVE_WATCHER`` guard cannot see a test/rehearsal backend
  launched beside production in the same container. Both would otherwise keep
  a full Rollup graph and write the same staging tree. An OS lease follows the
  process lifetime, so crashes release it without cleanup while deliberate
  one-shot rebuilds remain independent.
  """
  lock_path = _FRONTEND_DIR / ".watch.lock"
  lock_fh = open(lock_path, "a+")
  try:
    fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except BlockingIOError as exc:
    lock_fh.close()
    raise RuntimeError(
      f"frontend warm watcher already active for {_FRONTEND_DIR}"
    ) from exc
  lock_fh.seek(0)
  lock_fh.truncate()
  lock_fh.write(f"{os.getpid()}\n")
  lock_fh.flush()
  return lock_fh


def _publish_system_event(event: dict) -> None:
  """Publish a shell-rebuild lifecycle event to the system broadcast ONLY.

  These events (shell_rebuilding/rebuilt/rebuild_failed) are catch-up-unsafe:
  a chat reconnect that replayed an old `shell_rebuilt` from its event log
  would tell the Shell a fresh build just landed and trigger a spurious apply.
  The system broadcast has no replay, so single-bus delivery is exactly one
  hit per client and needs no frontend dedup. Do NOT fan out to per-chat
  broadcasts.
  """
  try:
    from app.broadcast import get_system_broadcast
    get_system_broadcast().publish(event)
  except Exception:
    log.exception("frontend rebuild notify failed: %s", event.get("type"))


def _complete_build(d: Path) -> bool:
  return (
    d.is_dir()
    and (d / "assets").is_dir()
    and (d / "index.html").is_file()
    and (d / "sw.js").is_file()
    and (d / "manifest.webmanifest").is_file()
  )


def _validate_built_globals(d: Path) -> None:
  """Reject a complete-looking bundle with undeclared runtime identifiers.

  Vite/Rollup intentionally permits free identifiers because a web page may
  supply globals at runtime. That also means a half-finished refactor such as
  ``clearTimeout(ioBounceTimer)`` compiles successfully, publishes, and crashes
  only when the affected callback runs. The companion Node script parses every
  emitted JS module with scope analysis and allows only the explicit
  browser/worker/toolchain globals the shell actually relies on.

  Run this after copying to ``.dist-next`` and before the atomic swap so a
  rejected generation never displaces the last working ``dist``.
  """
  if not _BUILT_GLOBAL_CHECK.is_file():
    raise _BuiltGlobalValidationError(
      f"frontend global checker is missing: {_BUILT_GLOBAL_CHECK}"
    )
  try:
    result = subprocess.run(
      ["node", str(_BUILT_GLOBAL_CHECK), str(d)],
      cwd=str(_FRONTEND_DIR),
      stdout=subprocess.PIPE,
      stderr=subprocess.STDOUT,
      text=True,
      timeout=45,
    )
  except (OSError, subprocess.TimeoutExpired) as exc:
    raise _BuiltGlobalValidationError(
      f"frontend global checker could not run: {exc}"
    ) from exc
  if result.returncode != 0:
    detail = _tail(result.stdout) or (
      f"frontend global checker exited {result.returncode}"
    )
    raise _BuiltGlobalValidationError(detail)


def _tail(text: str, limit: int = 4000) -> str:
  text = text.strip()
  if len(text) <= limit:
    return text
  return "..." + text[-limit:]


def _attic_gen_num(p: Path) -> int:
  """Generation number encoded in an attic subdir name (``gen-<n>``)."""
  try:
    return int(p.name.split("-", 1)[1])
  except (IndexError, ValueError):
    return -1


def _hardlink(src: Path, dest: Path) -> None:
  """Hardlink ``src`` -> ``dest``, falling back to a cross-device copy."""
  try:
    os.link(src, dest)
  except FileExistsError:
    pass
  except OSError:
    shutil.copy2(src, dest)


def _prune_attic() -> None:
  """Keep only the newest ``_ATTIC_KEEP`` attic generations."""
  gens = sorted(
    (p for p in _ATTIC_DIR.glob("gen-*") if p.is_dir()),
    key=_attic_gen_num,
  )
  stale = gens[:-_ATTIC_KEEP] if _ATTIC_KEEP > 0 else gens
  for old in stale:
    shutil.rmtree(old, ignore_errors=True)


def _attic_generation(gen_dir: Path) -> None:
  """Hardlink an outgoing generation's assets + index into the attic.

  Invariant: after a swap ``dist`` holds only the NEW generation's content-
  hashed chunks, but an unreloaded tab can still request OLD chunks. Retaining
  the outgoing generation lets request-time ``/assets`` resolution answer those
  on a ``dist`` miss. Best-effort: publication has already happened, so attic
  failure must not fail the swap.
  """
  assets_src = gen_dir / "assets"
  if not assets_src.is_dir():
    return
  _ATTIC_DIR.mkdir(parents=True, exist_ok=True)
  existing = [p for p in _ATTIC_DIR.glob("gen-*") if p.is_dir()]
  next_n = 1 + max((_attic_gen_num(p) for p in existing), default=0)
  dest = _ATTIC_DIR / f"gen-{next_n}"
  dest_assets = dest / "assets"
  dest_assets.mkdir(parents=True, exist_ok=True)
  for src in assets_src.rglob("*"):
    if src.is_file():
      link = dest_assets / src.relative_to(assets_src)
      link.parent.mkdir(parents=True, exist_ok=True)
      _hardlink(src, link)
  index_src = gen_dir / "index.html"
  if index_src.is_file():
    _hardlink(index_src, dest / "index.html")
  _prune_attic()


def _replace_dist() -> None:
  """Swap the validated ``.dist-next`` into the served ``dist`` path."""
  if _OLD_DIST_DIR.exists():
    shutil.rmtree(_OLD_DIST_DIR)
  old_moved = False
  if _DIST_DIR.exists():
    _DIST_DIR.rename(_OLD_DIST_DIR)
    old_moved = True
  try:
    _NEXT_DIST_DIR.rename(_DIST_DIR)
  except Exception:
    if old_moved and not _DIST_DIR.exists() and _OLD_DIST_DIR.exists():
      _OLD_DIST_DIR.rename(_DIST_DIR)
    raise
  if old_moved and _OLD_DIST_DIR.is_dir():
    try:
      _attic_generation(_OLD_DIST_DIR)
    except Exception:
      log.exception("attic hardlink of outgoing generation failed")
  if _OLD_DIST_DIR.exists():
    try:
      shutil.rmtree(_OLD_DIST_DIR)
    except Exception:
      # The new dist is already the committed served generation. Cleanup must
      # not report the publish as failed: the platform updater would otherwise
      # roll source back while clients keep receiving the new frontend.
      log.exception("outgoing frontend generation cleanup failed")


def _tree_signature(root: Path) -> tuple[tuple[str, int, int], ...] | None:
  """Return a cheap content-shape signature for a build tree.

  It is intentionally mtime+size, not a content hash: this is a race detector
  around Vite's staging writes, not a cache key.
  """
  if not root.is_dir():
    return None
  entries: list[tuple[str, int, int]] = []
  try:
    for path in sorted(root.rglob("*")):
      if not path.is_file():
        continue
      st = path.stat()
      rel = path.relative_to(root).as_posix()
      entries.append((rel, st.st_size, st.st_mtime_ns))
  except OSError:
    return None
  return tuple(entries)


def _ensure_node_modules() -> None:
  """Link the editable clone to baked dependencies when needed."""
  dest = _FRONTEND_DIR / "node_modules"
  if dest.exists() or dest.is_symlink():
    return
  source = Path("/app/shell-src/node_modules")
  try:
    dest.symlink_to(source, target_is_directory=True)
  except OSError:
    log.warning("could not link frontend node_modules to %s", source)


def _vite_env(
  cache_dir: Path | None = None, tmp_dir: Path | None = None,
) -> dict[str, str]:
  """Return the one-shot Vite build environment with bounded memory."""
  cache_dir = cache_dir if cache_dir is not None else _CACHE_DIR
  tmp_dir = tmp_dir if tmp_dir is not None else _TMP_DIR
  cache_dir.mkdir(parents=True, exist_ok=True)
  tmp_dir.mkdir(parents=True, exist_ok=True)
  env = os.environ.copy()
  env["MOBIUS_VITE_CACHE"] = str(cache_dir)
  env["TMPDIR"] = str(tmp_dir)
  # Bound peak build memory while preserving an explicit operator override.
  # A 320 MiB ceiling can finish the main bundle but OOM while finalizing the
  # service worker; consecutive isolated builds pass at 384 MiB.
  node_options = env.get("NODE_OPTIONS", "")
  if "--max-old-space-size" not in node_options and "--max_old_space_size" not in node_options:
    env["NODE_OPTIONS"] = f"{node_options} --max-old-space-size=384".strip()
  return env


def _vite_build_cmd(out_dir: Path) -> list[str]:
  # Launch Vite's checked-in executable directly. An ``npx`` wrapper retains
  # an extra npm process and shell while the build runs, and makes health point
  # at the wrapper instead of the actual builder.
  cmd = [
    str(_FRONTEND_DIR / "node_modules" / ".bin" / "vite"),
    "build",
    "--configLoader",
    "runner",
    "--outDir",
    out_dir.name,
    "--emptyOutDir",
  ]
  return cmd


def _copy_vendor(dest: Path) -> None:
  vendor = Path("/app/static/vendor")
  if not vendor.is_dir():
    return
  vendor_dest = dest / "vendor"
  if vendor_dest.exists():
    shutil.rmtree(vendor_dest)
  shutil.copytree(vendor, vendor_dest)


def _prepare_next_from(source_dir: Path) -> None:
  """Copy a complete build tree into ``.dist-next`` for validation/swap."""
  if _NEXT_DIST_DIR.exists():
    shutil.rmtree(_NEXT_DIST_DIR)
  before = _tree_signature(source_dir)
  if before is None:
    raise RuntimeError(f"{source_dir} does not exist")
  try:
    shutil.copytree(source_dir, _NEXT_DIST_DIR, symlinks=True)
  except (FileNotFoundError, shutil.Error) as exc:
    shutil.rmtree(_NEXT_DIST_DIR, ignore_errors=True)
    raise _StagingChangedDuringPublish(str(exc)) from exc
  after = _tree_signature(source_dir)
  if before != after:
    shutil.rmtree(_NEXT_DIST_DIR, ignore_errors=True)
    raise _StagingChangedDuringPublish(
      "staging changed while it was being published",
    )
  _copy_vendor(_NEXT_DIST_DIR)
  if not _complete_build(_NEXT_DIST_DIR):
    shutil.rmtree(_NEXT_DIST_DIR, ignore_errors=True)
    raise _IncompleteBuild(
      "vite build did not produce index.html, assets/, sw.js, and "
      "manifest.webmanifest"
    )


def _content_identical(a: Path, b: Path) -> bool:
  """True when two build trees hold the same files with the same bytes.

  Guards against no-op publishes: ``vite build --watch`` performs an initial
  build on every (re)start, which rewrites staging with fresh mtimes even when
  the output is byte-identical to the served ``dist``. Publishing that would
  fire a spurious ``shell_rebuilt`` (an idle-client reload per container
  restart) and burn an attic slot per boot — repeated no-op restarts would
  needlessly consume the bounded window an unreloaded tab needs. Byte
  comparison, not mtimes: the question here is "would clients see anything
  new", not "did files move".
  """
  if not (a.is_dir() and b.is_dir()):
    return False
  files_a = sorted(
    p.relative_to(a).as_posix() for p in a.rglob("*") if p.is_file()
  )
  files_b = sorted(
    p.relative_to(b).as_posix() for p in b.rglob("*") if p.is_file()
  )
  if files_a != files_b:
    return False
  try:
    return all(
      filecmp.cmp(a / rel, b / rel, shallow=False) for rel in files_a
    )
  except OSError:
    return False


def _publish_built_dir(source_dir: Path, reason: str) -> bool:
  """Publish ``source_dir`` through the single atomic dist+attic path.

  Returns True when a new generation was actually swapped in, False when the
  built tree is byte-identical to the served ``dist`` (nothing published, no
  event owed, no attic rotation). Serialized twice: the threading lock covers
  in-process callers, and an OS-level flock covers rebuild_shell.sh publishing
  from its own process concurrently with the warm watcher (a per-process lock
  alone let the two interleave .dist-next/dist renames mid-deploy).
  """
  log.info("frontend publish requested: %s", reason)
  with _PUBLISH_LOCK:
    lock_path = _FRONTEND_DIR / ".publish.lock"
    with open(lock_path, "w") as lock_fh:
      fcntl.flock(lock_fh, fcntl.LOCK_EX)
      try:
        _prepare_next_from(source_dir)
        if _content_identical(_NEXT_DIST_DIR, _DIST_DIR):
          shutil.rmtree(_NEXT_DIST_DIR, ignore_errors=True)
          log.info(
            "frontend publish skipped (%s): built tree is identical to the "
            "served dist", reason,
          )
          return False
        try:
          _validate_built_globals(_NEXT_DIST_DIR)
        except Exception:
          # A rejected candidate must not linger as a complete-looking next
          # generation that a later publisher could mistake for its output.
          shutil.rmtree(_NEXT_DIST_DIR, ignore_errors=True)
          raise
        _replace_dist()
        return True
      finally:
        fcntl.flock(lock_fh, fcntl.LOCK_UN)


def _run_vite_build_once(out_dir: Path) -> str:
  """Run one explicit full Vite build into ``out_dir``."""
  _ensure_node_modules()
  if out_dir.exists():
    shutil.rmtree(out_dir)
  result = subprocess.run(
    _vite_build_cmd(out_dir),
    cwd=str(_FRONTEND_DIR),
    env=_vite_env(_REBUILD_CACHE_DIR, _REBUILD_TMP_DIR),
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    timeout=180,
  )
  if result.returncode != 0:
    if out_dir.exists():
      shutil.rmtree(out_dir)
    raise RuntimeError(_tail(result.stdout) or "vite build failed")
  return result.stdout


def rebuild_frontend_now(
  reason: str = "manual", *, emit_events: bool = True,
) -> str:
  """Run an explicit full rebuild, then publish via the generation path.

  Used by the platform update flow and ``backend/scripts/rebuild_shell.sh``.
  Warm watcher publications do NOT call this; they publish existing staging so
  settle/apply-now latency stays at file-copy + atomic swap, not a cold build.
  """
  log.info("frontend full rebuild requested: %s", reason)
  if emit_events:
    _publish_system_event({"type": "shell_rebuilding"})
  try:
    output = _run_vite_build_once(_REBUILD_DIST_DIR)
    published = _publish_built_dir(_REBUILD_DIST_DIR, reason)
  except Exception as exc:
    if emit_events:
      _publish_system_event({
        "type": "shell_rebuild_failed",
        "error": str(exc),
      })
    raise
  # An identical build published nothing, so clients owe no reload — emitting
  # shell_rebuilt anyway would bounce every idle tab for a byte-identical
  # bundle.
  if emit_events and published:
    _publish_system_event({"type": "shell_rebuilt"})
  return output


def _active_watcher() -> "_FrontendHandler | None":
  with _ACTIVE_LOCK:
    return _ACTIVE_WATCHER


def publish_now(reason: str = "shell_apply_now") -> bool:
  """Publish dirty warm staging immediately, if the watcher is running."""
  watcher = _active_watcher()
  if watcher is None:
    log.info("frontend publish-now ignored; watcher is not running")
    return False
  return watcher.publish_now(reason)


class _FrontendHandler(FileSystemEventHandler):
  """Detect source edits and serialize short-lived Vite builds."""

  def __init__(
    self,
    loop: asyncio.AbstractEventLoop,
    *,
    start_threads: bool = True,
  ) -> None:
    self._loop = loop
    self._loop_thread_id = threading.get_ident()
    self._pending: asyncio.TimerHandle | None = None
    self._closed = threading.Event()
    self._state_lock = threading.Lock()
    self._proc_lock = threading.Lock()
    self._watch_proc: subprocess.Popen | None = None
    self._build_requested = threading.Event()
    self._last_source_change = 0.0
    self._last_build_reason = "startup"
    self._blocked_conflict_signature: str | None = None
    self._staging_dirty = False
    self._incomplete_since: float | None = None
    self._incomplete_notified = False
    self._last_staging_signature = _tree_signature(_STAGING_DIST_DIR)
    self._watch_thread: threading.Thread | None = None
    self._source_observer: PollingObserverVFS | None = None
    self._watch_lock_fh = _acquire_watch_lock() if start_threads else None
    if start_threads:
      try:
        self._source_observer = PollingObserverVFS(
          stat=os.stat,
          listdir=lambda path: _source_tree_scandir(_FRONTEND_DIR, path),
          polling_interval=1.0,
        )
        self._source_observer.schedule(
          self, str(_FRONTEND_DIR), recursive=True,
        )
        self._watch_thread = threading.Thread(
          target=self._build_loop,
          name="frontend-vite-build",
          daemon=True,
        )
        self._source_observer.start()
        self._watch_thread.start()
        # Recover an edit saved before a prior shutdown, but do not spend a
        # full Vite heap on every ordinary container boot when dist is fresh.
        if _startup_build_needed():
          self._request_build("startup")
      except Exception:
        # One thread may already be running when the second .start() fails.
        # close() terminates and joins that partial startup before releasing
        # the lease, so a retry cannot inherit an orphan poll/watch thread.
        self.close()
        raise

  def close(self) -> None:
    """Cancel pending work and stop the observer/current Vite build."""
    self._closed.set()
    self._build_requested.set()
    self._cancel_pending_publish()
    self._terminate_watch_process(signal.SIGTERM)
    if self._source_observer is not None:
      try:
        self._source_observer.stop()
        if self._source_observer.ident is not None:
          self._source_observer.join(timeout=2)
      except RuntimeError:
        pass
      self._source_observer = None
    if self._watch_thread is not None and self._watch_thread.ident is not None:
      self._watch_thread.join(timeout=5)
    if self._watch_process_running():
      self._terminate_watch_process(signal.SIGKILL)
      if self._watch_thread is not None:
        self._watch_thread.join(timeout=2)
    if self._watch_lock_fh is not None:
      fcntl.flock(self._watch_lock_fh, fcntl.LOCK_UN)
      self._watch_lock_fh.close()
      self._watch_lock_fh = None
    global _ACTIVE_WATCHER
    with _ACTIVE_LOCK:
      if _ACTIVE_WATCHER is self:
        _ACTIVE_WATCHER = None

  def publish_now(self, reason: str = "shell_apply_now") -> bool:
    """Publish dirty staging immediately from a request or worker thread."""
    self._refresh_staging_signature()
    self._cancel_pending_publish()
    return self._publish_dirty_sync(reason)

  def health(self) -> dict:
    with self._proc_lock:
      proc = self._watch_proc
      pid = proc.pid if proc is not None and proc.poll() is None else None
    rss_bytes = None
    if pid is not None:
      try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
          if line.startswith("VmRSS:"):
            rss_bytes = int(line.split()[1]) * 1024
            break
      except (OSError, ValueError, IndexError):
        pass
    with self._state_lock:
      staging_dirty = self._staging_dirty
    return {
      "running": not self._closed.is_set(),
      "building": pid is not None,
      "pid": pid,
      "rss_bytes": rss_bytes,
      "staging_dirty": staging_dirty,
      "lease_path": str(_FRONTEND_DIR / ".watch.lock"),
    }

  # Watchdog calls these on its own thread.
  def on_modified(self, event) -> None:  # noqa: ANN001
    if not event.is_directory:
      self._request_build(event.src_path)

  def on_created(self, event) -> None:  # noqa: ANN001
    if not event.is_directory:
      self._request_build(event.src_path)

  def on_moved(self, event) -> None:  # noqa: ANN001
    if not event.is_directory:
      self._request_build(getattr(event, "dest_path", None) or event.src_path)

  def on_deleted(self, event) -> None:  # noqa: ANN001
    if not event.is_directory:
      self._request_build(event.src_path)

  def _request_build(self, reason: str) -> None:
    if reason != "startup" and not _is_frontend_source_path(reason):
      return
    self._queue_build(reason)

  def _queue_build(self, reason: str) -> None:
    """Queue a debounced build without applying the filesystem-path filter."""
    if self._closed.is_set():
      return
    with self._state_lock:
      self._last_source_change = time.monotonic()
      self._last_build_reason = reason
    self._build_requested.set()

  def _on_loop_thread(self) -> bool:
    return threading.get_ident() == self._loop_thread_id

  def _cancel_pending_publish(self) -> None:
    def cancel() -> None:
      if self._pending is not None:
        self._pending.cancel()
        self._pending = None

    if self._on_loop_thread() or not self._loop.is_running():
      cancel()
      return
    try:
      self._loop.call_soon_threadsafe(cancel)
    except RuntimeError:
      pass

  def _watch_process_running(self) -> bool:
    with self._proc_lock:
      return self._watch_proc is not None and self._watch_proc.poll() is None

  def _terminate_watch_process(self, sig: signal.Signals) -> None:
    with self._proc_lock:
      proc = self._watch_proc
    if proc is None or proc.poll() is not None:
      return
    try:
      os.killpg(proc.pid, sig)
    except ProcessLookupError:
      return
    except OSError:
      try:
        proc.send_signal(sig)
      except OSError:
        return

  def _build_loop(self) -> None:
    while not self._closed.is_set():
      self._build_requested.wait()
      if self._closed.is_set():
        break
      self._build_requested.clear()

      # Wait until the source tree has been quiet for the normal debounce.
      # New edits update the shared timestamp, so one loop coalesces a burst.
      while not self._closed.is_set():
        with self._state_lock:
          quiet_for = time.monotonic() - self._last_source_change
          reason = self._last_build_reason
        remaining = _DEBOUNCE_SECS - quiet_for
        if remaining <= 0:
          # Acknowledge every edit already covered by this build. A change
          # racing after this clear sets the event again and earns one rerun.
          self._build_requested.clear()
          with self._state_lock:
            if time.monotonic() - self._last_source_change < _DEBOUNCE_SECS:
              continue
            reason = self._last_build_reason
          break
        if self._closed.wait(remaining):
          return

      try:
        self._run_demand_build(reason)
      except Exception as exc:
        if self._closed.is_set():
          break
        log.warning("frontend build failed after %s: %s", reason, exc)
        _publish_system_event({
          "type": "shell_rebuild_failed",
          "error": str(exc),
        })

  def _run_demand_build(self, reason: str) -> None:
    """Run one isolated build and publish it before releasing its heap."""
    with self._state_lock:
      blocked_signature = self._blocked_conflict_signature
    if blocked_signature is not None:
      current_signature, _ = _source_snapshot()
      if current_signature == blocked_signature:
        # The same conflicted snapshot is still present. Stat it cheaply, but
        # do not reread every text input on each backstop retry.
        self._queue_build("conflict-marker preflight retry")
        return
    preflight = _stable_source_preflight()
    if preflight is None:
      self._queue_build("source changed during conflict-marker preflight")
      return
    source_signature, conflicts = preflight
    if conflicts:
      with self._state_lock:
        newly_blocked = self._blocked_conflict_signature != source_signature
        self._blocked_conflict_signature = source_signature
      if newly_blocked:
        log.warning(
          "frontend demand build deferred; conflict markers remain in %s",
          ", ".join(conflicts),
        )
      # Poll the cheap preflight after the normal debounce as a backstop for a
      # missed/coalesced observer event. Do not make a transient editing state
      # sticky, and do not emit shell_rebuild_failed while the last good dist
      # remains served.
      self._queue_build("conflict-marker preflight retry")
      return
    with self._state_lock:
      conflict_cleared = self._blocked_conflict_signature is not None
      self._blocked_conflict_signature = None
    if conflict_cleared:
      log.info("frontend conflict-marker preflight cleared; resuming build")
    _ensure_node_modules()
    if _STAGING_DIST_DIR.exists():
      shutil.rmtree(_STAGING_DIST_DIR)
    cmd = _vite_build_cmd(_STAGING_DIST_DIR)
    proc: subprocess.Popen | None = None
    rc: int | None = None
    output = ""
    try:
      proc = subprocess.Popen(
        cmd,
        cwd=str(_FRONTEND_DIR),
        env=_vite_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
      )
      with self._proc_lock:
        self._watch_proc = proc
      log.info("frontend demand build started after %s", reason)
      try:
        output, _ = proc.communicate(timeout=180)
      except subprocess.TimeoutExpired as exc:
        self._terminate_watch_process(signal.SIGTERM)
        try:
          output, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
          self._terminate_watch_process(signal.SIGKILL)
          output, _ = proc.communicate()
        raise RuntimeError("vite build timed out after 180 seconds") from exc
      rc = proc.returncode
    finally:
      with self._proc_lock:
        if self._watch_proc is proc:
          self._watch_proc = None
    if self._closed.is_set():
      return
    if rc != 0:
      shutil.rmtree(_STAGING_DIST_DIR, ignore_errors=True)
      detail = _tail(output) or f"vite build exited {rc}"
      raise RuntimeError(detail)
    if output.strip():
      log.info("frontend demand build complete: %s", _tail(output, 1000))
    current_source_signature, _ = _source_snapshot()
    if current_source_signature != source_signature:
      shutil.rmtree(_STAGING_DIST_DIR, ignore_errors=True)
      log.info(
        "frontend source changed during demand build; discarding staging",
      )
      self._queue_build("source changed during demand build")
      return
    if not self._refresh_staging_signature():
      raise RuntimeError("vite build completed without a staging generation")
    self._publish_dirty_sync(f"build:{reason}")
    with self._state_lock:
      publish_pending = self._staging_dirty
    if not publish_pending:
      try:
        _write_source_stamp(source_signature)
      except OSError:
        log.warning("could not persist frontend source signature")

  def _refresh_staging_signature(self) -> bool:
    sig = _tree_signature(_STAGING_DIST_DIR)
    if sig is None:
      return False
    with self._state_lock:
      if sig == self._last_staging_signature:
        return False
      self._last_staging_signature = sig
      self._staging_dirty = True
    return True

  def _schedule_publish_from_thread(self, reason: str) -> None:
    if self._loop.is_closed() or self._closed.is_set():
      return
    try:
      asyncio.run_coroutine_threadsafe(self._reschedule_publish(reason),
                                       self._loop)
    except RuntimeError:
      pass

  async def _reschedule_publish(self, reason: str) -> None:
    if self._closed.is_set():
      return
    if self._pending is not None:
      self._pending.cancel()
    self._pending = self._loop.call_later(
      _DEBOUNCE_SECS,
      lambda: asyncio.create_task(self._trigger_settled_publish(reason)),
    )

  async def _trigger_settled_publish(self, reason: str) -> None:
    self._pending = None
    if self._closed.is_set():
      return
    await asyncio.to_thread(self._publish_dirty_sync, f"settle:{reason}")

  def _publish_dirty_sync(self, reason: str) -> bool:
    with self._state_lock:
      if not self._staging_dirty or self._closed.is_set():
        return False
      self._staging_dirty = False
    try:
      published = _publish_built_dir(_STAGING_DIST_DIR, reason)
    except _StagingChangedDuringPublish:
      log.info("frontend staging changed during publish; waiting to settle")
      with self._state_lock:
        self._staging_dirty = True
      self._schedule_publish_from_thread("staging changed during publish")
      return False
    except _IncompleteBuild as exc:
      # The staging poller can observe a quiet gap while a slow Vite build is
      # still transforming modules, before index.html/PWA output is complete.
      # That is normal build progress, not a broken shell. Keep serving dist,
      # retry the same dirty generation, and only notify if it remains
      # incomplete long enough to represent a genuine stuck/failed build.
      now = time.monotonic()
      if self._incomplete_since is None:
        self._incomplete_since = now
      with self._state_lock:
        self._staging_dirty = True
      if (
        not self._incomplete_notified
        and now - self._incomplete_since >= _INCOMPLETE_GRACE_SECS
      ):
        self._incomplete_notified = True
        log.warning(
          "frontend staging remained incomplete for %.1fs: %s",
          now - self._incomplete_since,
          exc,
        )
        _publish_system_event({
          "type": "shell_rebuild_failed",
          "error": str(exc),
        })
      self._schedule_publish_from_thread("incomplete staging")
      return False
    except Exception as exc:
      log.warning("frontend publish failed: %s", exc)
      # Restore the dirty flag: it was cleared above, and the staging
      # signature already matches the poll loop's snapshot, so without this a
      # transient publish failure would strand the edit forever — the poll
      # never re-marks dirty and even shell_apply_now's publish_now would
      # no-op on the clean flag. Dirty must mean "differs from what dist
      # serves", not "differs from the last publish attempt".
      with self._state_lock:
        self._staging_dirty = True
      _publish_system_event({
        "type": "shell_rebuild_failed",
        "error": str(exc),
      })
      self._incomplete_since = None
      self._incomplete_notified = False
      return False
    self._incomplete_since = None
    self._incomplete_notified = False
    if not published:
      # Byte-identical to the served dist (e.g. the watch process's initial
      # build after a restart): nothing changed for clients, so no event.
      # The dirty flag stays cleared — staging and dist agree.
      return False
    _publish_system_event({"type": "shell_rebuilt"})
    return True


def start_watcher(
  loop: asyncio.AbstractEventLoop,
) -> tuple[None, _FrontendHandler]:
  """Start demand-build supervision for whole-repo frontend edits."""
  src_dir = _FRONTEND_DIR / "src"
  if not src_dir.is_dir():
    raise FileNotFoundError(f"{src_dir} does not exist")

  # Close a same-process predecessor before acquiring the cross-process lease.
  # Serialize the whole handoff so two concurrent lifespan starts cannot both
  # observe an empty in-process slot.
  global _ACTIVE_WATCHER
  with _START_LOCK:
    with _ACTIVE_LOCK:
      old = _ACTIVE_WATCHER
      _ACTIVE_WATCHER = None
    if old is not None:
      old.close()
    handler = _FrontendHandler(loop)
    with _ACTIVE_LOCK:
      _ACTIVE_WATCHER = handler
  log.info("frontend demand-build watcher started on %s", _FRONTEND_DIR)
  return None, handler


class _FrontendSupervisor:
  """Retries lease acquisition and exposes watcher health to operators."""

  def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
    self._loop = loop
    self._handler: _FrontendHandler | None = None
    self._task: asyncio.Task | None = None
    self._closed = asyncio.Event()
    self._status = "starting"
    self._last_error: str | None = None
    self._retry_in_seconds: float | None = None

  async def start(self) -> None:
    if self._attempt_start():
      return
    self._task = self._loop.create_task(self._retry_loop())

  def _attempt_start(self) -> bool:
    try:
      _, handler = start_watcher(self._loop)
    except Exception as exc:
      self._status = "waiting_for_lease"
      self._last_error = str(exc)
      log.warning("frontend watcher unavailable; will retry: %s", exc)
      return False
    self._handler = handler
    self._status = "running"
    self._last_error = None
    self._retry_in_seconds = None
    return True

  async def _retry_loop(self) -> None:
    backoff = _WATCH_LEASE_RETRY_INITIAL
    while not self._closed.is_set():
      self._retry_in_seconds = backoff
      try:
        await asyncio.wait_for(self._closed.wait(), timeout=backoff)
        return
      except asyncio.TimeoutError:
        pass
      if self._attempt_start():
        return
      backoff = min(backoff * 2, _WATCH_RESTART_BACKOFF_MAX)

  def close(self) -> None:
    self._status = "stopped"
    self._closed.set()
    if self._task is not None:
      self._task.cancel()
      self._task = None
    if self._handler is not None:
      self._handler.close()
      self._handler = None
    global _ACTIVE_SUPERVISOR
    if _ACTIVE_SUPERVISOR is self:
      _ACTIVE_SUPERVISOR = None

  def health(self) -> dict:
    detail = self._handler.health() if self._handler is not None else {
      "running": False,
      "building": False,
      "pid": None,
      "rss_bytes": None,
      "staging_dirty": False,
      "lease_path": str(_FRONTEND_DIR / ".watch.lock"),
    }
    return {
      "status": self._status,
      "last_error": self._last_error,
      "retry_in_seconds": self._retry_in_seconds,
      **detail,
    }


async def start_supervised_watcher(
  loop: asyncio.AbstractEventLoop,
) -> tuple[None, _FrontendSupervisor]:
  global _ACTIVE_SUPERVISOR
  supervisor = _FrontendSupervisor(loop)
  _ACTIVE_SUPERVISOR = supervisor
  await supervisor.start()
  return None, supervisor


def watcher_health() -> dict:
  supervisor = _ACTIVE_SUPERVISOR
  if supervisor is not None:
    return supervisor.health()
  watcher = _active_watcher()
  if watcher is not None:
    return {"status": "running", "last_error": None,
            "retry_in_seconds": None, **watcher.health()}
  return {
    "status": "stopped",
    "running": False,
    "building": False,
    "pid": None,
    "rss_bytes": None,
    "staging_dirty": False,
    "lease_path": str(_FRONTEND_DIR / ".watch.lock"),
    "last_error": None,
    "retry_in_seconds": None,
  }


def _cli() -> int:
  try:
    rebuild_frontend_now("rebuild_shell.sh", emit_events=False)
  except Exception as exc:
    print(str(exc), flush=True)
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(_cli())
