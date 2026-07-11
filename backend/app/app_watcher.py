"""File watcher that auto-recompiles mini-app source on edit.

Watches source-like files under installable app source dirs
(`/data/apps/<slug>/`).
When `index.jsx` or one of its sibling modules changes, looks up the app by
its exact `source_dir` in the DB, re-reads `index.jsx`, recompiles from that
real entry path so relative imports resolve, and persists the current entry
source + `compiled_path`. Publishes `app_updated` to the SystemBroadcast so the
Shell (which subscribes via /api/events/system) picks up the change without a
manual `register_app.py` roundtrip.

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
import re
import subprocess
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from app import app_git, fs_locks, models, source_dirs
from app.compiler import recompile_app_bundle
from app.config import get_settings
from app.database import SessionLocal

log = logging.getLogger(__name__)

_DEBOUNCE_SECS = 1.0
_INDEX_JSX = "index.jsx"
_SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}
_IGNORED_SOURCE_PARTS = {
  ".build",
  ".git",
  "dist",
  "node_modules",
  "static",
}
_MAX_FAILURE_SUMMARY = 160


def _compact_failure_text(text: str) -> str:
  """Normalize compiler output into one display-safe line."""
  return re.sub(r"\s+", " ", text or "").strip()


def _truncate_failure_text(text: str, limit: int = _MAX_FAILURE_SUMMARY) -> str:
  """Return ``text`` trimmed to the owner-visible summary budget."""
  if len(text) <= limit:
    return text
  return text[:max(0, limit - 1)].rstrip() + "…"


def _is_build_noise(line: str) -> bool:
  """Return True for esbuild framing lines that hide the real error."""
  return bool(re.search(
    r"^(vite v|transforming|rendering chunks|computing gzip size|"
    r"[✓✗]|\d+\s+error|warning:|files generated$)",
    line,
    flags=re.IGNORECASE,
  ))


def _summarize_app_build_failure(error: object) -> str:
  """Extract the first meaningful mini-app compiler error line."""
  raw = getattr(error, "stderr", None) or str(error or "")
  lines = [
    _compact_failure_text(line)
    for line in str(raw).splitlines()
  ]
  lines = [line for line in lines if line]
  if not lines:
    return ""

  high_signal = next((
    line for line in lines
    if (
      re.search(r"\bERROR\b|Failed to resolve|Cannot find module", line, re.I)
      or "Unexpected" in line
      or "Expected" in line
    )
  ), None)
  summary = high_signal or next((
    line for line in lines if not _is_build_noise(line)
  ), lines[0])
  summary = re.sub(r"^.*?\[ERROR\]\s*", "", summary, flags=re.IGNORECASE)
  summary = re.sub(r"^.*?\bERROR:\s*", "", summary, flags=re.IGNORECASE)
  return _truncate_failure_text(summary)


def _publish_app_build_failed(
  *, app_id: int, app_name: str, chat_id: str | None, summary: str,
) -> None:
  """Emit an app build failure to the system bus and the building chat."""
  from app.broadcast import get_broadcast, get_system_broadcast
  event = {
    "type": "app_build_failed",
    "appId": str(app_id),
    "appName": app_name,
    "summary": summary,
  }
  # The system broadcast reaches Shell.handleSystemEvent in every view;
  # without it the failure toast is dropped the moment the owner
  # navigates away from the building chat — the most likely posture,
  # since "the previous version is still running" describes an owner
  # who went to look at the app. SystemBroadcast subscribers get no
  # backlog replay, and the Shell's per-app dedup window collapses the
  # double delivery when the chat stream also forwards the copy below.
  get_system_broadcast().publish(event)
  if not chat_id:
    return
  bc = get_broadcast(str(chat_id))
  if bc is None or not bc.running:
    return
  bc.publish(event)


def _source_roots() -> list[Path]:
  data_dir = Path(get_settings().data_dir)
  return [source_dirs.apps_root(data_dir)]


def _source_dir_for_changed_path(path: str | Path) -> Path | None:
  """Returns the immediate source-tree owner for a source edit."""
  p = Path(path)
  if p.suffix not in _SOURCE_SUFFIXES:
    return None
  try:
    resolved = p.resolve()
  except (OSError, RuntimeError):
    return None
  for root in _source_roots():
    try:
      rel = resolved.relative_to(root)
    except ValueError:
      continue
    if len(rel.parts) < 2:
      return None
    ignored_parts = rel.parts[1:-1]
    if any(
      part in _IGNORED_SOURCE_PARTS or part.startswith(".")
      for part in ignored_parts
    ):
      return None
    if rel.name.startswith("."):
      return None
    return root / rel.parts[0]
  return None


class _JsxHandler(FileSystemEventHandler):
  """Watchdog event handler that schedules debounced recompiles."""

  def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
    self._loop = loop
    # source_dir → (TimerHandle from loop.call_later, force_rebuild)
    self._pending: dict[str, tuple[asyncio.TimerHandle, bool]] = {}

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

  def on_deleted(self, event) -> None:  # noqa: ANN001
    if event.is_directory:
      return
    self._schedule(event.src_path)

  def _schedule(self, path: str) -> None:
    source_dir = _source_dir_for_changed_path(path)
    if source_dir is None:
      return
    force_rebuild = Path(path).resolve() != (source_dir / _INDEX_JSX).resolve()
    # Hop back to the asyncio loop thread to touch _pending safely.
    # Loop may already be closing if the watchdog thread fires during
    # shutdown — guard so we don't crash the observer thread.
    if self._loop.is_closed():
      return
    try:
      asyncio.run_coroutine_threadsafe(
        self._reschedule(str(source_dir), path, force_rebuild), self._loop,
      )
    except RuntimeError:
      # Loop stopped between the is_closed check and the call. Drop it.
      pass

  def close(self) -> None:
    """Cancels any pending debounce timers.

    Called from `lifespan` shutdown so a timer that hasn't fired yet
    doesn't post a `create_task` to a loop that's about to close.
    """
    for handle, _ in self._pending.values():
      handle.cancel()
    self._pending.clear()

  async def _reschedule(
    self, source_dir: str, changed_path: str, force_rebuild: bool,
  ) -> None:
    pending = self._pending.pop(source_dir, None)
    if pending is not None:
      handle, pending_force_rebuild = pending
      handle.cancel()
      force_rebuild = force_rebuild or pending_force_rebuild
    handle = self._loop.call_later(
      _DEBOUNCE_SECS,
      lambda: asyncio.create_task(
        self._recompile(changed_path, force_rebuild=force_rebuild),
      ),
    )
    self._pending[source_dir] = (handle, force_rebuild)

  async def _recompile(
    self, changed_path: str, *, force_rebuild: bool = False,
  ) -> None:
    p = Path(changed_path)
    source_dir_path = _source_dir_for_changed_path(p) or (
      p if p.name != _INDEX_JSX else p.parent
    )
    source_dir = str(source_dir_path)
    self._pending.pop(source_dir, None)
    index_path = source_dir_path / _INDEX_JSX
    changed_is_entry = p.resolve() == index_path.resolve()
    skip_unchanged_entry = changed_is_entry and not force_rebuild
    try:
      jsx_source = index_path.read_text(encoding="utf-8")
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
        if app is None or app.deleted_at is not None:
          return  # No such app, or it's tombstoned — don't recompile/revive
          # a soft-deleted app's source touched during the recovery window.
        if skip_unchanged_entry and app.jsx_source == jsx_source:
          return  # Already compiled — nothing to do.
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
          if app is None or app.deleted_at is not None or app.source_dir != source_dir:
            return
          try:
            jsx_source = index_path.read_text(encoding="utf-8")
          except FileNotFoundError:
            return
          if not jsx_source.strip():
            return
          if skip_unchanged_entry and app.jsx_source == jsx_source:
            return
          # Hold the PRIOR version entirely while a conflicting update is
          # unresolved. `index.jsx` may well compile (the conflict can be in
          # a NON-entry file like a job script), so without this gate the
          # bundle would swap to "updated" while `commit_local` is forced to
          # commit a tree still full of `<<<<<<<` markers — the invariant is
          # that an update finalizes (recompile/swap AND commit) only when no
          # tracked file has unresolved conflicts. commit_local refuses the
          # commit on its own; bailing here also blocks the bundle swap.
          if (
            app_git.is_repo(source_dir)
            and await asyncio.to_thread(
              app_git.has_unresolved_conflicts, source_dir,
            )
          ):
            return
          try:
            await recompile_app_bundle(db, app, jsx_source)
            if app_git.is_repo(source_dir):
              try:
                await asyncio.to_thread(
                  app_git.commit_local, source_dir, "agent edit",
                )
              except subprocess.CalledProcessError as exc:
                log.info(
                  "auto-recompile: git commit skipped for %s: %s",
                  changed_path, exc,
                )
          except RuntimeError as exc:
            summary = _summarize_app_build_failure(exc)
            failure = {
              "app_id": app.id,
              "app_name": app.name,
              "chat_id": app.chat_id,
              "summary": summary,
            }
            log.warning(
              "auto-recompile: compile failed for %s: %s", changed_path, exc,
            )
            db.rollback()
            _publish_app_build_failed(**failure)
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
) -> tuple[PollingObserver, _JsxHandler]:
  """Starts a watchdog Observer on app source directories.

  Returns `(observer, handler)` so the caller can stop the observer
  AND drain the handler's pending debounce timers on shutdown.
  """
  apps_dir = Path(get_settings().data_dir) / "apps"
  apps_dir.mkdir(parents=True, exist_ok=True)
  handler = _JsxHandler(loop)
  # PollingObserver, not the default inotify Observer: inotify events are
  # unreliable on the Docker volume backing /data (overlay/bind mounts can
  # silently drop IN_MODIFY/IN_CLOSE_WRITE), which left an agent's edit during
  # an app-update merge un-recompiled and the merge unfinalized (.pm/124).
  # Polling stats the small source trees on an interval — reliable everywhere,
  # negligible cost for these watch roots.
  observer = PollingObserver()
  watched: list[str] = []
  for root in _source_roots():
    if root.is_dir():
      observer.schedule(handler, str(root), recursive=True)
      watched.append(str(root))
  observer.start()
  log.info("app watcher started on %s", ", ".join(watched))
  return observer, handler
