"""File watcher that auto-recompiles mini-app JSX on edit.

Watches `/data/apps/*/index.jsx`.  When a JSX file changes, looks up
the app by its directory name in the DB, re-reads the source from
disk, recompiles via `compile_jsx`, and persists the new source +
`compiled_path`.  Publishes `app_updated` to active broadcasts so a
running chat picks up the change without a manual `register_app.py`
roundtrip.

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
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from app import models
from app.broadcast import get_all_active_broadcasts
from app.compiler import compile_jsx
from app.config import get_settings
from app.database import SessionLocal

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
    app_dir_name = p.parent.name
    try:
      jsx_source = p.read_text(encoding="utf-8")
    except FileNotFoundError:
      return
    if not jsx_source.strip():
      # Empty or whitespace-only — likely a mid-save sentinel. Skip.
      return

    db = SessionLocal()
    try:
      # Resolve dir → app via the source_dir column set by
      # register_app.py.  Exact path match; no string normalization.
      app = (
        db.query(models.App)
        .filter(models.App.source_dir == str(p.parent))
        .first()
      )
      if app is None:
        return
      if app.jsx_source == jsx_source:
        return  # Already compiled; nothing to do.
      try:
        compiled = await compile_jsx(app.id, jsx_source)
      except RuntimeError as exc:
        log.warning(
          "auto-recompile: compile failed for %s: %s", path, exc,
        )
        return
      app.jsx_source = jsx_source
      app.compiled_path = compiled
      db.commit()
      log.info(
        "auto-recompiled app id=%s name=%s", app.id, app.name,
      )
      # Best-effort broadcast notification so any running chat reloads
      # the iframe.  No-op if no chat is active.
      for bc in get_all_active_broadcasts():
        bc.publish({"type": "app_updated", "appId": str(app.id)})
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
