"""Warm frontend builder + atomic generation publisher.

Vite owns source watching: one long-lived ``vite build --watch`` keeps its
module graph warm and writes only to ``.dist-staging``.  This module owns the
separate publication step: after staging settles, or after an explicit
``shell_apply_now``, copy staging to ``.dist-next``, validate the complete Vite
shape, then swap ``.dist-next`` into ``dist`` through the existing attic hook.

Failure handling:
- Watch subprocess crashes are logged and restarted with backoff.
- Broken staging never touches ``dist``; a failed publish leaves the previous
  generation served.
- Container shutdown calls ``close()``, which SIGTERMs the Vite process group.
"""

from __future__ import annotations

import asyncio
import fcntl
import filecmp
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

_DEBOUNCE_SECS = 1.75
_STAGING_POLL_SECS = 0.5
_WATCH_RESTART_BACKOFF_MAX = 30.0
_FRONTEND_DIR = Path("/data/platform/frontend")
_DIST_DIR = _FRONTEND_DIR / "dist"
_STAGING_DIST_DIR = _FRONTEND_DIR / ".dist-staging"
_REBUILD_DIST_DIR = _FRONTEND_DIR / ".dist-rebuild"
_NEXT_DIST_DIR = _FRONTEND_DIR / ".dist-next"
_OLD_DIST_DIR = _FRONTEND_DIR / ".dist-old"
_ATTIC_DIR = _FRONTEND_DIR / ".assets-attic"
_ATTIC_KEEP = 3
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
_ACTIVE_WATCHER: "_FrontendHandler | None" = None


class _StagingChangedDuringPublish(RuntimeError):
  """Raised when Vite mutates staging while publication is copying it."""


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
    shutil.rmtree(_OLD_DIST_DIR)


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
  """Return Vite env with cache/temp dirs and polling watch enabled."""
  cache_dir = cache_dir if cache_dir is not None else _CACHE_DIR
  tmp_dir = tmp_dir if tmp_dir is not None else _TMP_DIR
  cache_dir.mkdir(parents=True, exist_ok=True)
  tmp_dir.mkdir(parents=True, exist_ok=True)
  env = os.environ.copy()
  env["MOBIUS_VITE_CACHE"] = str(cache_dir)
  env["TMPDIR"] = str(tmp_dir)
  # Docker volume events have been unreliable here. Vite/Rollup watch uses
  # chokidar underneath, so force polling rather than reintroducing a Python
  # source watcher beside Vite's own watch mode.
  env["CHOKIDAR_USEPOLLING"] = "1"
  env["CHOKIDAR_INTERVAL"] = env.get("CHOKIDAR_INTERVAL", "250")
  return env


def _vite_build_cmd(out_dir: Path, *, watch: bool) -> list[str]:
  cmd = [
    "npx",
    "vite",
    "build",
    "--configLoader",
    "runner",
    "--outDir",
    out_dir.name,
    "--emptyOutDir",
  ]
  if watch:
    cmd.append("--watch")
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
    raise RuntimeError(
      "vite build did not produce index.html, assets/, sw.js, and "
      "manifest.webmanifest"
    )


def _content_identical(a: Path, b: Path) -> bool:
  """True when two build trees hold the same files with the same bytes.

  Guards against no-op publishes: ``vite build --watch`` performs an initial
  build on every (re)start, which rewrites staging with fresh mtimes even when
  the output is byte-identical to the served ``dist``. Publishing that would
  fire a spurious ``shell_rebuilt`` (an idle-client reload per container
  restart) and burn an attic slot per boot — three restarts could evict a
  generation an unreloaded tab still needs. Byte comparison, not mtimes: the
  question here is "would clients see anything new", not "did files move".
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
    _vite_build_cmd(out_dir, watch=False),
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


class _FrontendHandler:
  """Supervises Vite watch and schedules staging publication."""

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
    self._staging_dirty = False
    self._last_staging_signature = _tree_signature(_STAGING_DIST_DIR)
    self._watch_thread: threading.Thread | None = None
    self._stage_thread: threading.Thread | None = None
    if start_threads:
      self._watch_thread = threading.Thread(
        target=self._watch_loop,
        name="frontend-vite-watch",
        daemon=True,
      )
      self._stage_thread = threading.Thread(
        target=self._stage_poll_loop,
        name="frontend-stage-poll",
        daemon=True,
      )
      self._watch_thread.start()
      self._stage_thread.start()

  def close(self) -> None:
    """Cancel publication timers and stop the Vite watch process."""
    self._closed.set()
    self._cancel_pending_publish()
    self._terminate_watch_process(signal.SIGTERM)
    for thread in (self._stage_thread, self._watch_thread):
      if thread is not None:
        thread.join(timeout=5)
    if self._watch_process_running():
      self._terminate_watch_process(signal.SIGKILL)
      if self._watch_thread is not None:
        self._watch_thread.join(timeout=2)
    global _ACTIVE_WATCHER
    with _ACTIVE_LOCK:
      if _ACTIVE_WATCHER is self:
        _ACTIVE_WATCHER = None

  def publish_now(self, reason: str = "shell_apply_now") -> bool:
    """Publish dirty staging immediately from a request or worker thread."""
    self._refresh_staging_signature()
    self._cancel_pending_publish()
    return self._publish_dirty_sync(reason)

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

  def _watch_loop(self) -> None:
    backoff = 1.0
    while not self._closed.is_set():
      proc: subprocess.Popen | None = None
      started_at = time.monotonic()
      try:
        _ensure_node_modules()
        env = _vite_env()
        cmd = _vite_build_cmd(_STAGING_DIST_DIR, watch=True)
        proc = subprocess.Popen(
          cmd,
          cwd=str(_FRONTEND_DIR),
          env=env,
          stdout=subprocess.PIPE,
          stderr=subprocess.STDOUT,
          text=True,
          start_new_session=True,
        )
        with self._proc_lock:
          self._watch_proc = proc
        log.info("frontend vite watch started: %s", " ".join(cmd))
        if proc.stdout is not None:
          for line in proc.stdout:
            if line.strip():
              log.info("vite watch: %s", line.rstrip())
            if self._closed.is_set():
              break
        rc = proc.wait()
      except Exception as exc:
        rc = None
        log.warning("frontend vite watch crashed before start: %s", exc)
      finally:
        with self._proc_lock:
          if self._watch_proc is proc:
            self._watch_proc = None
      if self._closed.is_set():
        break
      # A watch that survived a healthy stretch earns a fresh backoff — an
      # isolated crash after hours of stability should restart in 1s, not
      # inherit a 30s penalty from an unstable period long past.
      if time.monotonic() - started_at >= 60.0:
        backoff = 1.0
      log.warning(
        "frontend vite watch exited rc=%s; restarting in %.1fs", rc, backoff,
      )
      if self._closed.wait(backoff):
        break
      backoff = min(backoff * 2, _WATCH_RESTART_BACKOFF_MAX)

  def _stage_poll_loop(self) -> None:
    while not self._closed.wait(_STAGING_POLL_SECS):
      if self._refresh_staging_signature():
        self._schedule_publish_from_thread("staging changed")

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
      return False
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
  """Start Vite watch supervision for whole-repo frontend edits."""
  src_dir = _FRONTEND_DIR / "src"
  if not src_dir.is_dir():
    raise FileNotFoundError(f"{src_dir} does not exist")

  handler = _FrontendHandler(loop)
  global _ACTIVE_WATCHER
  with _ACTIVE_LOCK:
    old = _ACTIVE_WATCHER
    _ACTIVE_WATCHER = handler
  if old is not None:
    old.close()
  log.info("frontend warm watcher started on %s", _FRONTEND_DIR)
  return None, handler


def _cli() -> int:
  try:
    rebuild_frontend_now("rebuild_shell.sh", emit_events=False)
  except Exception as exc:
    print(str(exc), flush=True)
    return 1
  return 0


if __name__ == "__main__":
  raise SystemExit(_cli())
