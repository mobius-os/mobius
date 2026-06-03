"""File watcher that auto-recompiles mini-app JSX on edit.

Watches `/data/apps/*/index.jsx`.  When a JSX file changes, looks up
the app by its directory name in the DB, re-reads the source from
disk, recompiles via `compile_jsx`, and persists the new source +
`compiled_path`.  Publishes `app_updated` to the SystemBroadcast so
the Shell (which subscribes via /api/events/system) picks up the
change without a manual `register_app.py` roundtrip.

Debounced (1s) to coalesce rapid saves during multi-line edits.

Failure handling:
- File missing between event and read → skip (agent deleted it).
- DB row not found for the directory name → skip (app not yet
  registered; this happens during the gap between file-create and
  `register_app.py`, which is fine — the POST path will compile).
- `compile_jsx` raises (broken JSX, e.g. missing `export default`
  during mid-save) → log + skip; the old bundle stays in place
  until a valid save lands.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app import app_git, fs_locks, models
from app.compiler import recompile_app_bundle
from app.config import get_settings
from app.database import SessionLocal
from app.providers import per_app_git_enabled

log = logging.getLogger(__name__)

_DEBOUNCE_SECS = 1.0
_INDEX_JSX = "index.jsx"


class _JsxHandler(FileSystemEventHandler):
  """Watchdog event handler that schedules debounced recompiles."""

  def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
    self._loop = loop
    # path → TimerHandle from loop.call_later
    self._pending: dict[str, asyncio.TimerHandle] = {}

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
    # `mv` over the file shows up as a move; treat the dest like a write.
    if event.is_directory:
      return
    dest = getattr(event, "dest_path", None) or event.src_path
    self._schedule(dest)

  def _schedule(self, path: str) -> None:
    if not path.endswith(f"/{_INDEX_JSX}"):
      return
    # Hop back to the asyncio loop thread to touch _pending safely.
    # Loop may already be closing if the watchdog thread fires during
    # shutdown — guard so we don't crash the observer thread.
    if self._loop.is_closed():
      return
    try:
      asyncio.run_coroutine_threadsafe(self._reschedule(path), self._loop)
    except RuntimeError:
      # Loop stopped between the is_closed check and the call. Drop it.
      pass

  def close(self) -> None:
    """Cancels any pending debounce timers.

    Called from `lifespan` shutdown so a timer that hasn't fired yet
    doesn't post a `create_task` to a loop that's about to close.
    """
    for handle in self._pending.values():
      handle.cancel()
    self._pending.clear()

  async def _reschedule(self, path: str) -> None:
    handle = self._pending.pop(path, None)
    if handle is not None:
      handle.cancel()
    self._pending[path] = self._loop.call_later(
      _DEBOUNCE_SECS,
      lambda: asyncio.create_task(self._recompile(path)),
    )

  async def _recompile(self, path: str) -> None:
    self._pending.pop(path, None)
    p = Path(path)
    source_dir = str(p.parent)
    try:
      jsx_source = p.read_text(encoding="utf-8")
    except FileNotFoundError:
      return
    if not jsx_source.strip():
      # Empty or whitespace-only — likely a mid-save sentinel. Skip.
      return

    # Recompile under the same lifecycle -> app -> source locks that PATCH and
    # uninstall hold, so this awaited compile can't race a concurrent uninstall
    # + SQLite id reuse and overwrite a replacement app's bundle, and the bundle
    # stays transactional (compile out-of-place, swap only after commit). The
    # lifecycle lock blocks delete/install for the whole sequence; the app +
    # source locks serialize against a concurrent PATCH recompiling the same app.
    async with fs_locks.install_uninstall_lock():
      db = SessionLocal()
      try:
        # Resolve dir → app via the source_dir column set by
        # register_app.py.  Exact path match; no string normalization.
        # Apps with source_dir=NULL (legacy or created without going
        # through register_app.py) silently won't auto-recompile —
        # that's documented in the seed under "If your edit didn't
        # recompile" so the agent knows to re-register. We deliberately
        # don't slug-match the dir name to a candidate app here;
        # guessing is exactly the kind of code-policing the design
        # philosophy says belongs in the seed, not the server.
        app = (
          db.query(models.App)
          .filter(models.App.source_dir == source_dir)
          .first()
        )
        if app is None or app.jsx_source == jsx_source:
          return  # No such app, or already compiled — nothing to do.
        app_id = app.id
        async with (
          fs_locks.app_storage_lock(app_id),
          fs_locks.source_dir_lock(source_dir),
        ):
          # Re-read the row AND the source file fresh under the lock. The row
          # re-read re-verifies identity (the app still exists and still owns
          # this dir, so an id reused mid-recompile is caught). The file re-read
          # means we compile the CURRENT bytes, not the pre-lock snapshot — a
          # concurrent PATCH may have already superseded it, and compiling the
          # stale snapshot would overwrite the newer result.
          app = (
            db.query(models.App).populate_existing()
            .filter(models.App.id == app_id).first()
          )
          if app is None or app.source_dir != source_dir:
            return
          try:
            jsx_source = p.read_text(encoding="utf-8")
          except FileNotFoundError:
            return
          if not jsx_source.strip() or app.jsx_source == jsx_source:
            return
          try:
            await recompile_app_bundle(db, app, jsx_source)
            if (
              per_app_git_enabled(str(get_settings().data_dir))
              and app_git.is_repo(source_dir)
            ):
              try:
                await asyncio.to_thread(
                  app_git.commit_local, source_dir, "agent edit",
                )
              except subprocess.CalledProcessError as exc:
                log.info(
                  "auto-recompile: git commit skipped for %s: %s",
                  path, exc,
                )
          except RuntimeError as exc:
            log.warning(
              "auto-recompile: compile failed for %s: %s", path, exc,
            )
            db.rollback()
            return
        log.info(
          "auto-recompiled app id=%s name=%s", app.id, app.name,
        )
        # Publish to the SystemBroadcast only. That channel reaches
        # the Shell regardless of which view the user is on (chat /
        # canvas / settings) via the persistent
        # /api/events/system subscription installed by
        # frontend/src/hooks/useSystemEventStream.js. Shell.jsx wires
        # the same `app_updated` handler into both that hook and the
        # per-chat stream (frontend/src/components/ChatView/useStreamConnection.js
        # treats `app_updated` as a SYSTEM_EVENT and forwards to the
        # same callback) — so an extra fan-out to every active
        # ChatBroadcast (the v1 design preserved this as "intentional")
        # was redundant, not load-bearing. Ticket 033 removed it.
        from app.broadcast import get_broadcast, get_system_broadcast
        event = {"type": "app_updated", "appId": str(app.id)}
        get_system_broadcast().publish(event)
        # Chat-scoped CTA: if this edit landed during the building chat's
        # turn, fire `app_built` onto only that chat's stream so the
        # "Open app" CTA shows in the right chat (and nowhere else). The
        # global `app_updated` above stays list-refresh-only. No-op when
        # the app has no owning chat or that chat isn't streaming. See
        # routes/notify.publish_app_built_to_owning_chat for the rationale.
        if app.chat_id:
          bc = get_broadcast(str(app.chat_id))
          if bc is not None and bc.running:
            bc.publish({"type": "app_built", "appId": str(app.id)})
      except Exception:
        # Watcher must keep running across any single-event failure.
        log.exception("auto-recompile unexpected error for %s", path)
        try:
          db.rollback()
        except Exception:
          pass
      finally:
        db.close()


def start_watcher(
  loop: asyncio.AbstractEventLoop,
) -> tuple[Observer, _JsxHandler]:
  """Starts a watchdog Observer on the apps directory.

  Returns `(observer, handler)` so the caller can stop the observer
  AND drain the handler's pending debounce timers on shutdown.
  """
  apps_dir = Path(get_settings().data_dir) / "apps"
  apps_dir.mkdir(parents=True, exist_ok=True)
  handler = _JsxHandler(loop)
  observer = Observer()
  observer.schedule(handler, str(apps_dir), recursive=True)
  observer.start()
  log.info("app watcher started on %s", apps_dir)
  return observer, handler
