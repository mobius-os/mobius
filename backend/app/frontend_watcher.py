"""File watcher that auto-builds the whole-repo frontend on edit.

Watches the editable frontend clone under ``/data/platform/frontend`` and
coalesces rapid saves into one Vite build.  Builds are serialized: if a save
lands while Vite is running, the watcher performs one additional build after
the current one completes.

Failure handling:
- Build failures log + publish ``shell_rebuild_failed``.
- The previous ``dist`` stays in place because Vite writes to ``.dist-next``
  and only swaps it into ``dist`` after a complete successful build.
- Watcher errors never escape into FastAPI lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

log = logging.getLogger(__name__)

_DEBOUNCE_SECS = 1.75
_FRONTEND_DIR = Path("/data/platform/frontend")
_DIST_DIR = _FRONTEND_DIR / "dist"
_NEXT_DIST_DIR = _FRONTEND_DIR / ".dist-next"
_OLD_DIST_DIR = _FRONTEND_DIR / ".dist-old"
_ATTIC_DIR = _FRONTEND_DIR / ".assets-attic"
_ATTIC_KEEP = 3
_CACHE_DIR = _FRONTEND_DIR / ".vite-cache"
_TMP_DIR = _FRONTEND_DIR / ".vite-tmp"
_ROOT_FILES = {"index.html", "vite.config.js"}
_WATCH_DIRS = ("src", "public")
_BUILD_LOCK = threading.Lock()
# Attic + swap scratch dirs must never re-trigger a build. `.`-prefixed dirs
# are already ignored by _is_frontend_source_path, but listing them here is
# the explicit, greppable contract.
_IGNORED_PARTS = {
  ".assets-attic",
  ".dist-next",
  ".dist-old",
  ".git",
  ".vite-cache",
  ".vite-tmp",
  "dist",
  "node_modules",
}


def _is_frontend_source_path(path: str | Path) -> bool:
  """Return True when ``path`` is a frontend source/build-input edit."""
  try:
    rel = Path(path).resolve().relative_to(_FRONTEND_DIR.resolve())
  except ValueError:
    return False
  if not rel.parts:
    return False
  if any(part in _IGNORED_PARTS or part.startswith(".")
         for part in rel.parts[:-1]):
    return False
  if len(rel.parts) == 1:
    return rel.name in _ROOT_FILES
  return rel.parts[0] in _WATCH_DIRS


def _publish_system_event(event: dict) -> None:
  """Publish the same system rebuild events that ``/api/notify`` emits."""
  try:
    from app.broadcast import (
      get_active_broadcast,
      get_all_active_broadcasts,
      get_system_broadcast,
    )
    get_system_broadcast().publish(event)
    targets = get_all_active_broadcasts()
    if not targets:
      active = get_active_broadcast()
      targets = [active] if active is not None else []
    for bc in targets:
      bc.publish(event)
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
  """Hardlink ``src`` -> ``dest``, falling back to a copy across filesystems.

  Same-fs hardlinks are the near-free default (the attic is a sibling of
  ``dist``); the copy fallback keeps the attic correct on the rare cross-device
  layout rather than leaving a gap an unreloaded tab would 404 on.
  """
  try:
    os.link(src, dest)
  except FileExistsError:
    pass
  except OSError:
    shutil.copy2(src, dest)


def _prune_attic() -> None:
  """Keep only the newest ``_ATTIC_KEEP`` attic generations, drop older ones.

  Per-generation subdirs make membership explicit, so pruning is a numeric
  sort + rmtree. Dropping a generation only unlinks ITS hardlinks — the
  underlying inodes survive as long as a newer generation (or the live
  ``dist``) still links the same content-hashed bytes.
  """
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
  hashed chunks, but a tab that has not reloaded still requests the OLD
  generation's chunks. Retaining the outgoing generation here lets request-time
  ``/assets`` resolution (see main.py) answer those on a ``dist`` miss, so a
  mid-build tab never 404s its module graph — that is why we attic on every
  swap. Per-generation subdirs keep membership explicit for pruning; the index
  is retained too because the generation boundary covers every entry file.
  Content-hashed names never collide, so the flat serve union stays safe.
  Best-effort: the published ``dist`` is already live, so the caller must not
  let an attic failure fail the swap.
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
  """Swap the freshly built temp directory into the served ``dist`` path.

  Two renames (``dist``→``.dist-old``, then ``.dist-next``→``dist``) leave a
  window of a few microseconds where ``dist`` does not exist; a request served
  in that window gets a 404 on an asset or a 503 from the SPA fallback (which
  guards its ``index.html`` read for exactly this). The window is accepted, not
  eliminated: ``rename`` cannot atomically replace a non-empty directory
  (ENOTEMPTY), so a truly windowless swap would require serving ``dist`` as a
  symlink flipped with ``os.replace`` — but a symlinked ``dist`` is not matched
  by ``dist/`` in ``.gitignore`` (trailing slash = directories only), so it
  would surface as untracked and break the clean-diff PR property. Given the
  swap is owner-edit-triggered, single-owner, and recoverable on reload, the
  documented microsecond window is the right trade over that ripple.
  """
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
    # Attic the OUTGOING generation before deleting it so an unreloaded tab can
    # still fetch its content-hashed chunks after the swap (request-time
    # /assets resolution serves the attic on a dist miss). Best-effort: the new
    # dist is already published and live, so an attic failure must not fail the
    # swap — the only cost is that a mid-build tab may 404 a lazy chunk.
    try:
      _attic_generation(_OLD_DIST_DIR)
    except Exception:
      log.exception("attic hardlink of outgoing generation failed")
  if _OLD_DIST_DIR.exists():
    shutil.rmtree(_OLD_DIST_DIR)


def _run_vite_build() -> str:
  """Run Vite with writable cache/temp dirs and refresh ``dist`` on success."""
  if _CACHE_DIR.exists():
    shutil.rmtree(_CACHE_DIR)
  _CACHE_DIR.mkdir(parents=True, exist_ok=True)
  _TMP_DIR.mkdir(parents=True, exist_ok=True)
  if _NEXT_DIST_DIR.exists():
    shutil.rmtree(_NEXT_DIST_DIR)

  env = os.environ.copy()
  env["MOBIUS_VITE_CACHE"] = str(_CACHE_DIR)
  env["TMPDIR"] = str(_TMP_DIR)
  cmd = [
    "npx",
    "vite",
    "build",
    "--configLoader",
    "runner",
    "--outDir",
    ".dist-next",
    "--emptyOutDir",
  ]
  result = subprocess.run(
    cmd,
    cwd=str(_FRONTEND_DIR),
    env=env,
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    timeout=180,
  )
  if result.returncode != 0:
    if _NEXT_DIST_DIR.exists():
      shutil.rmtree(_NEXT_DIST_DIR)
    raise RuntimeError(_tail(result.stdout) or "vite build failed")

  vendor = Path("/app/static/vendor")
  if vendor.is_dir():
    vendor_dest = _NEXT_DIST_DIR / "vendor"
    if vendor_dest.exists():
      shutil.rmtree(vendor_dest)
    shutil.copytree(vendor, vendor_dest)

  if not _complete_build(_NEXT_DIST_DIR):
    if _NEXT_DIST_DIR.exists():
      shutil.rmtree(_NEXT_DIST_DIR)
    raise RuntimeError(
      "vite build did not produce index.html, assets/, sw.js, and "
      "manifest.webmanifest"
    )

  _replace_dist()
  return result.stdout


def rebuild_frontend_now(reason: str = "manual") -> str:
  """Run an immediate serialized frontend rebuild and publish shell events.

  The filesystem watcher calls this after ordinary owner edits. The platform
  self-updater calls the same path after a git reconcile because git checkout /
  rebase updates can bypass watchdog polling and otherwise leave ``dist`` stale.
  """
  log.info("frontend rebuild requested: %s", reason)
  with _BUILD_LOCK:
    _publish_system_event({"type": "shell_rebuilding"})
    try:
      output = _run_vite_build()
    except Exception as exc:
      _publish_system_event({
        "type": "shell_rebuild_failed",
        "error": str(exc),
      })
      raise
    _publish_system_event({"type": "shell_rebuilt"})
    return output


class _FrontendHandler(FileSystemEventHandler):
  """Watchdog event handler that schedules debounced frontend rebuilds."""

  def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
    self._loop = loop
    self._pending: asyncio.TimerHandle | None = None
    self._building = False
    self._rerun_requested = False
    self._closed = False
    self._last_path = ""

  # Watchdog calls these on its own thread.
  def on_modified(self, event) -> None:  # noqa: ANN001
    if event.is_directory:
      return
    self._schedule(event.src_path)

  def on_created(self, event) -> None:  # noqa: ANN001
    if event.is_directory:
      return
    self._schedule(event.src_path)

  def on_moved(self, event) -> None:  # noqa: ANN001
    if event.is_directory:
      return
    dest = getattr(event, "dest_path", None) or event.src_path
    self._schedule(dest)

  def on_deleted(self, event) -> None:  # noqa: ANN001
    if event.is_directory:
      return
    self._schedule(event.src_path)

  def _schedule(self, path: str) -> None:
    if not _is_frontend_source_path(path):
      return
    if self._loop.is_closed():
      return
    try:
      asyncio.run_coroutine_threadsafe(self._reschedule(path), self._loop)
    except RuntimeError:
      pass

  def close(self) -> None:
    """Cancel pending debounce timers during lifespan shutdown."""
    self._closed = True
    if self._pending is not None:
      self._pending.cancel()
      self._pending = None

  async def _reschedule(self, changed_path: str) -> None:
    if self._closed:
      return
    if self._pending is not None:
      self._pending.cancel()
    self._last_path = changed_path
    self._pending = self._loop.call_later(
      _DEBOUNCE_SECS,
      lambda: asyncio.create_task(self._trigger()),
    )

  async def _trigger(self) -> None:
    self._pending = None
    if self._closed:
      return
    self._rerun_requested = True
    if self._building:
      return

    self._building = True
    try:
      while self._rerun_requested and not self._closed:
        self._rerun_requested = False
        await self._rebuild(self._last_path)
    finally:
      self._building = False

  async def _rebuild(self, changed_path: str) -> None:
    log.info("frontend rebuild scheduled after %s", changed_path)
    try:
      await asyncio.to_thread(rebuild_frontend_now, f"watch:{changed_path}")
    except asyncio.CancelledError:
      # Cancelled mid-build (lifespan shutdown): resolve the rebuild indicator
      # so a client doesn't hang on shell_rebuilding, then propagate — never
      # swallow CancelledError. The worker thread's vite child is reaped with
      # the process group on exit.
      _publish_system_event({
        "type": "shell_rebuild_failed",
        "error": "cancelled",
      })
      raise
    except Exception as exc:
      log.warning(
        "frontend rebuild failed after %s: %s", changed_path, exc,
      )
      return
    log.info("frontend rebuilt from %s", _FRONTEND_DIR)


def start_watcher(
  loop: asyncio.AbstractEventLoop,
) -> tuple[PollingObserver, _FrontendHandler]:
  """Starts a polling watchdog Observer for whole-repo frontend edits."""
  src_dir = _FRONTEND_DIR / "src"
  if not src_dir.is_dir():
    raise FileNotFoundError(f"{src_dir} does not exist")

  handler = _FrontendHandler(loop)
  observer = PollingObserver()
  observer.schedule(handler, str(_FRONTEND_DIR), recursive=False)
  for dirname in _WATCH_DIRS:
    watch_dir = _FRONTEND_DIR / dirname
    if watch_dir.is_dir():
      observer.schedule(handler, str(watch_dir), recursive=True)
  observer.start()
  log.info("frontend watcher started on %s", _FRONTEND_DIR)
  return observer, handler
