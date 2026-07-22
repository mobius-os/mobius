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
import json
import logging
import os
import re
import subprocess
from collections.abc import Iterator
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserverVFS

from app import app_git, fs_locks, models, source_dirs
from app.compiler import recompile_app_bundle
from app.config import get_settings
from app.database import SessionLocal

log = logging.getLogger(__name__)

_DEBOUNCE_SECS = 1.0
_INDEX_JSX = "index.jsx"
_SOURCE_SUFFIXES = {".js", ".jsx", ".ts", ".tsx"}
_DECLARATION_FILES = {"mobius.json"}
_IGNORED_SOURCE_PARTS = {
  ".build",
  ".git",
  "dist",
  "node_modules",
  "static",
}
_MAX_FAILURE_SUMMARY = 160


def _source_tree_scandir(
  apps_root: Path,
  path: str | None,
) -> Iterator[os.DirEntry[str]]:
  """Yield only paths the app-source handler can act on.

  ``PollingObserver`` otherwise snapshots every descendant once per second.
  On a production volume that includes numeric app-data dirs, per-app git
  object databases, and dependency trees, almost all of those stats are
  guaranteed to be discarded by ``_source_dir_for_changed_path`` later. Keep
  the reliable polling behavior while pruning those trees at traversal time.
  """
  scan_path = path or "."
  try:
    at_root = Path(scan_path).resolve() == apps_root
    entries = os.scandir(scan_path)
  except OSError:
    return
  with entries:
    for entry in entries:
      name = entry.name
      # The handler ignores hidden and generated subtrees. Excluding them here
      # also handles symlinked dependency trees such as node_modules.
      if name.startswith(".") or name in _IGNORED_SOURCE_PARTS:
        continue
      if at_root and name.isdigit():
        try:
          if entry.is_dir(follow_symlinks=True):
            continue
        except OSError:
          continue
      yield entry


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
  *, app_id: int, app_name: str, summary: str,
) -> None:
  """Emit an app build failure to the system bus ONLY.

  The system broadcast reaches Shell.handleSystemEvent in every view — which
  is exactly the posture for a build failure, since "the previous version is
  still running" describes an owner who navigated to look at the app. It is
  catch-up-unsafe (a chat reconnect must not replay a stale failure toast), so
  it rides the system bus alone; SystemBroadcast has no replay, so one
  delivery per client and no frontend dedup.
  """
  from app.broadcast import get_system_broadcast
  get_system_broadcast().publish({
    "type": "app_build_failed",
    "appId": str(app_id),
    "appName": app_name,
    "summary": summary,
  })


def _is_pending_update_changed(error: object) -> bool:
  """Whether a deferred update's exact reviewed candidate went stale."""
  detail = getattr(error, "detail", None)
  return (
    getattr(error, "status_code", None) == 409
    and isinstance(detail, dict)
    and detail.get("code") == "pending_update_changed"
  )


def _publish_app_update_stale(*, app_id: int, app_name: str) -> None:
  """Tell the owner to review a changed pending update, without replay."""
  from app.broadcast import get_system_broadcast
  get_system_broadcast().publish({
    "type": "app_update_stale",
    "appId": str(app_id),
    "appName": app_name,
  })


def _source_roots() -> list[Path]:
  data_dir = Path(get_settings().data_dir)
  return [source_dirs.apps_root(data_dir)]


def _source_dir_for_changed_path(path: str | Path) -> Path | None:
  """Returns the immediate source-tree owner for a source edit."""
  p = Path(path)
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
    source_dir = root / rel.parts[0]
    # Ordinary rebuilds only care about importable source. During a real
    # update merge, however, the conflict may be in fetch.sh, mobius.json, or
    # any other tracked file. Let every safe in-tree file wake the resolver
    # finalizer while MERGE_HEAD exists; the unresolved-marker gate below still
    # prevents a premature bundle swap.
    if (
      p.suffix not in _SOURCE_SUFFIXES
      and p.name not in _DECLARATION_FILES
      and not (source_dir / ".git" / "MERGE_HEAD").exists()
    ):
      return None
    return source_dir
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
    has_pending_update = (
      source_dir_path / ".git" / "mobius-pending-update" / "receipt.json"
    ).is_file()
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
    # stays transactional (compile, immutable publish, then row commit). The
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
        has_pending_update = (
          source_dir_path / ".git" / "mobius-pending-update" / "receipt.json"
        ).is_file()
        if skip_unchanged_entry and app.jsx_source == jsx_source and not has_pending_update:
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
          has_pending_update = (
            source_dir_path / ".git" / "mobius-pending-update" / "receipt.json"
          ).is_file()
          try:
            jsx_source = index_path.read_text(encoding="utf-8")
          except FileNotFoundError:
            return
          if not jsx_source.strip():
            return
          if skip_unchanged_entry and app.jsx_source == jsx_source and not has_pending_update:
            return
          # Hold the PRIOR version entirely while a conflicting update is
          # unresolved. `index.jsx` may well compile (the conflict can be in
          # a NON-entry file like a job script), so without this gate the
          # bundle would swap to "updated" while `commit_local` is forced to
          # commit a tree still full of `<<<<<<<` markers — the invariant is
          # that an update finalizes (recompile/swap AND commit) only when no
          # tracked file has unresolved conflicts. commit_local refuses the
          # commit on its own; bailing here also blocks the bundle swap.
          merge_in_progress = (
            source_dir_path / ".git" / "MERGE_HEAD"
          ).exists()
          if app_git.is_repo(source_dir):
            if merge_in_progress:
              # Marker-free text is a positive resolution signal; commit_local
              # stages it and performs the final all-tracked marker scan. Binary
              # conflicts have no marker proof and remain gated until the agent
              # explicitly stages them.
              if await asyncio.to_thread(
                app_git.has_conflict_markers, source_dir,
              ) or await asyncio.to_thread(
                app_git.has_unresolved_binary_conflicts, source_dir,
              ):
                return
            elif await asyncio.to_thread(
              app_git.has_unresolved_conflicts, source_dir,
            ):
              return
          pending_receipt = None
          if app_git.is_repo(source_dir) and (merge_in_progress or has_pending_update):
            # A resolver is finishing a previously blocked Store update. It may
            # commit the resolved source, but it must not publish a new bundle,
            # static tree, or DB metadata piecemeal. The canonical installer
            # below promotes all of those together after verifying the receipt.
            from app import install
            pending_receipt = install.read_pending_conflict_update_receipt(
              source_dir,
              app_id=app.id,
              upstream_commit=app.upstream_commit,
            )
            if pending_receipt is None:
              summary = "Pending update receipt is missing or no longer matches."
              db.rollback()
              _publish_app_build_failed(
                app_id=app.id, app_name=app.name, summary=summary,
              )
              return
            if merge_in_progress:
              try:
                committed = await asyncio.to_thread(
                  app_git.commit_local, source_dir, "resolve app update",
                )
              except Exception as exc:
                log.warning(
                  "auto-recompile: resolved update commit failed for %s: %s",
                  changed_path, exc,
                )
                _publish_app_build_failed(
                  app_id=app.id,
                  app_name=app.name,
                  summary=_summarize_app_build_failure(exc),
                )
                return
              if committed is None or await asyncio.to_thread(
                app_git.merge_in_progress, source_dir,
              ):
                _publish_app_build_failed(
                  app_id=app.id,
                  app_name=app.name,
                  summary="Resolved update could not finalize its source merge.",
                )
                return
          if pending_receipt is None:
            try:
              # A local app's adjacent manifest is the sole declaration source
              # for host-mediated capabilities. Synchronize it under the same
              # DB/bundle transaction as the rebuild so code and authority can
              # never land in different revisions. Store-installed apps retain
              # the last explicitly reviewed manifest contract.
              if app.manifest_url is None:
                manifest_path = source_dir_path / "mobius.json"
                if manifest_path.is_file():
                  try:
                    manifest = json.loads(
                      manifest_path.read_text(encoding="utf-8")
                    )
                  except (OSError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"Invalid mobius.json: {exc}") from exc
                  if not isinstance(manifest, dict):
                    raise RuntimeError("Invalid mobius.json: expected an object")
                  capabilities = manifest.get("capabilities") or {}
                else:
                  capabilities = {}
                from app.app_capabilities import contract_from_app_state
                try:
                  app.capability_contract = contract_from_app_state(
                    app, capabilities=capabilities,
                  )
                except ValueError as exc:
                  raise RuntimeError(f"Invalid mobius.json: {exc}") from exc
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
                "summary": summary,
              }
              log.warning(
                "auto-recompile: compile failed for %s: %s", changed_path, exc,
              )
              db.rollback()
              _publish_app_build_failed(**failure)
              return
          replay_app_id = app.id
          replay_app_name = app.name
          replay_upstream_commit = app.upstream_commit
        if pending_receipt is not None:
          # Re-enter the canonical installer while the lifecycle lock is still
          # held but the inner app/source locks are released. The old DB source,
          # bundle, static files, icon, and capabilities remain live until this
          # exact-candidate replay reaches its normal commit/promotion boundary.
          from app import install
          try:
            reapplied, reapplied_mode, reapply_warnings, *_ = (
              await install.install_from_manifest(
                db,
                manifest_url=None,
                manifest=pending_receipt["manifest"],
                raw_base=pending_receipt["raw_base"],
                source="store",
                reviewed_capability_digest=pending_receipt["capability_digest"],
                expected_app_id=replay_app_id,
                expected_upstream_commit=replay_upstream_commit,
                expected_candidate_digest=pending_receipt["candidate_digest"],
              )
            )
          except Exception as exc:
            db.rollback()
            if _is_pending_update_changed(exc):
              # The receipt deliberately remains in place. It keeps watcher
              # edits behind the atomic update gate until the owner reviews
              # the new upstream candidate and starts the update again.
              log.info(
                "resolved update candidate changed for app %s; review required",
                replay_app_id,
              )
              _publish_app_update_stale(
                app_id=replay_app_id,
                app_name=replay_app_name,
              )
            else:
              log.warning(
                "resolved update replay failed for app %s: %s",
                replay_app_id, exc,
              )
              _publish_app_build_failed(
                app_id=replay_app_id,
                app_name=replay_app_name,
                summary=_summarize_app_build_failure(exc),
              )
            return
          if reapplied.id != replay_app_id:
            # install_from_manifest's expected_app_id guard (install.py) rejects
            # any candidate whose row id differs, so a mismatch here is a broken
            # invariant, not a runtime condition — fail loudly.
            raise RuntimeError(
              "resolved update replay promoted app id=%s, expected id=%s"
              % (reapplied.id, replay_app_id)
            )
          if reapplied_mode != "update":
            # The installer re-detected an unresolved conflict (its mode fell
            # back to "conflict") because this watcher event observed a
            # MULTI-FILE resolution mid-flight: the entry is marker-free but a
            # sibling is not yet reconciled, so the installer's fresh three-way
            # merge still conflicts. This is a legitimate "resolution not yet
            # complete" wait-state, NOT a failure — install_from_manifest already
            # committed the conflict provenance and re-staged the pending-update
            # receipt, leaving the prior bundle live. The next complete-resolution
            # watcher event replays and promotes cleanly (the designed self-heal).
            # Do not roll back as an error, raise, or toast a build failure.
            log.info(
              "resolved update replay for app %s not yet complete (mode=%s); "
              "awaiting full conflict resolution",
              replay_app_id, reapplied_mode,
            )
            return
          for warning in reapply_warnings:
            log.warning("resolved update %s: %s", replay_app_id, warning)
          app = reapplied
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
        from app.broadcast import get_system_broadcast
        event = {"type": "app_updated", "appId": str(app.id)}
        get_system_broadcast().publish(event)
        # The built-app "Open app" CTA is now DERIVED on the frontend from the
        # apps query's chat_id + updated_at (Shell.builtAppState), so a bumped
        # updated_at + this app_updated refetch surface the CTA in the owning
        # chat with no separate app_built event to publish here.
      except Exception:
        # Watcher must keep running across any single-event failure.
        log.exception("auto-recompile unexpected error for %s", changed_path)
        try:
          db.rollback()
        except Exception:
          pass
      finally:
        db.close()


def start_watcher(
  loop: asyncio.AbstractEventLoop,
) -> tuple[PollingObserverVFS, _JsxHandler]:
  """Starts a watchdog Observer on app source directories.

  Returns `(observer, handler)` so the caller can stop the observer
  AND drain the handler's pending debounce timers on shutdown.
  """
  apps_dir = (Path(get_settings().data_dir) / "apps").resolve()
  apps_dir.mkdir(parents=True, exist_ok=True)
  handler = _JsxHandler(loop)
  # PollingObserver, not the default inotify Observer: inotify events are
  # unreliable on the Docker volume backing /data (overlay/bind mounts can
  # silently drop IN_MODIFY/IN_CLOSE_WRITE), which left an agent's edit during
  # an app-update merge un-recompiled and the merge unfinalized (.pm/124).
  # Polling stats the small source trees on an interval — reliable everywhere,
  # negligible cost for these watch roots.
  observer = PollingObserverVFS(
    stat=os.stat,
    listdir=lambda path: _source_tree_scandir(apps_dir, path),
  )
  watched: list[str] = []
  for root in _source_roots():
    if root.is_dir():
      observer.schedule(handler, str(root), recursive=True)
      watched.append(str(root))
  observer.start()
  # A process restart can happen after the agent saved a marker-free
  # resolution but before the debounce fired. Polling establishes its baseline
  # from the already-resolved bytes and would never emit that old change, so
  # explicitly revisit every in-progress merge once at startup. Unresolved
  # merges safely no-op at the hard gate; resolved ones finish and replay their
  # pending install receipt.
  for root in _source_roots():
    if not root.is_dir():
      continue
    for repo in root.iterdir():
      if (
        (repo / ".git" / "MERGE_HEAD").exists()
        or (repo / ".git" / "mobius-pending-update" / "receipt.json").is_file()
      ):
        handler._schedule(str(repo / _INDEX_JSX))
  log.info("app watcher started on %s", ", ".join(watched))
  return observer, handler
