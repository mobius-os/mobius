"""FastAPI application factory.

In production the single container serves both the API and the frontend
static files.  API routes are registered first; the frontend SPA is
mounted last as a catch-all so that client-side routing works.
"""

import asyncio
import logging
import mimetypes
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import timezone
from email.utils import formatdate, parsedate_to_datetime
from pathlib import Path

# Do this before importing FastAPI, SQLAlchemy, or any app module that may
# create worker threads. See allocator.limit_glibc_arenas for the observed
# per-thread 64 MiB arena failure mode on the production host.
from app.allocator import limit_glibc_arenas

limit_glibc_arenas()

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy.exc import OperationalError

from app.config import get_settings
from app.database import (
  Base,
  SessionLocal,
  engine,
  reset_database_request_label,
  run_migrations,
  set_database_request_label,
)
from app.http_caching import strip_range
from app import activity, models
# providers and push are on the agent's write surface; deferred into
# lifespan with try/except so a SyntaxError in either doesn't prevent
# uvicorn boot (and thereby kill the recovery surface). See the
# wrapped imports in lifespan() below.
from app.routes import (
  admin_router, apps_router, auth_router,
  chat_embed_router, chat_logs_router, chat_router, chats_router, chats_stream_router,
  debug_router, fs_router, github_router, media_router,
  local_services_router, notifications_router, notify_router, proxy_router, push_router,
  secrets_router, self_reminders_router, settings_router,
  client_error_router, client_signal_router, standalone_router, storage_router,
  theme_router, uploads_router, platform_router,
  published_router,
)

_BOOT_ID = f"{os.getpid()}-{time.time_ns()}"


def _init_db():
  """Run migrations and create tables, retrying on transient failures."""
  for attempt in range(10):
    try:
      run_migrations(engine)
      Base.metadata.create_all(bind=engine)
      return
    except OperationalError as e:
      if attempt < 9:
        delay = min(2 ** attempt, 10)
        print(f"DB init retry {attempt + 1}/10 in {delay}s: {e}")
        time.sleep(delay)
      else:
        raise


def _assert_provider_defaults(provider_names) -> None:
  """Validate SQLAlchemy provider defaults against the registry.

  `provider_names` is passed in instead of imported at module scope
  so a broken providers.py doesn't crash main.py at import time.
  """
  owner_default = models.Owner.provider.default.arg
  chat_default = models.Chat.provider.default.arg
  assert owner_default in provider_names, (
    "models.Owner.provider default must be in providers.PROVIDER_NAMES"
  )
  assert chat_default in provider_names, (
    "models.Chat.provider default must be in providers.PROVIDER_NAMES"
  )


# A wake warns once only after more than two full periods of lateness.
_LOOP_PERIOD_SECS = 60.0
_LOOP_LATE_PERIODS = 2.0


def loop_lateness_warning(
  period: float, observed_gap: float, *, late_periods: float = _LOOP_LATE_PERIODS,
) -> str | None:
  """Return a WARNING string when a periodic loop woke far later than scheduled.

  Compares the actual wake gap to the scheduled period: a lateness (observed
  minus scheduled) beyond `late_periods` periods means the loop was blocked for
  multiple cycles — event-loop starvation from a long synchronous call, GC
  pause, or disk stall — not scheduler jitter. Returns None for a healthy or
  merely-jittery wake. Pure so the watchdog decision is unit-testable without
  driving a real loop; the log is necessarily retrospective (a loop that never
  recovers cannot warn — only an external HTTP monitor can).
  """
  lateness = observed_gap - period
  if lateness <= period * late_periods:
    return None
  return (
    "periodic loop woke %.1fs after a %.0fs sleep (%.1fs late, >%.0f periods)"
    " — the event loop may be starved" % (
      observed_gap, period, lateness, late_periods,
    )
  )


async def _sleep_with_lag_warning(
  sleep,
  monotonic,
  logger,
  *,
  period: float = _LOOP_PERIOD_SECS,
) -> str | None:
  """Sleep once and report a multi-period event-loop stall."""
  started_at = monotonic()
  await sleep(period)
  warning = loop_lateness_warning(period, monotonic() - started_at)
  if warning is not None:
    logger.warning("%s", warning)
  return warning


@asynccontextmanager
async def lifespan(app):
  import asyncio as _asyncio
  import logging as _logging
  _log = _logging.getLogger(__name__)
  # Wrapped: providers.py is on the agent's write surface. A broken
  # providers.py shouldn't take down the server — log and skip the
  # defaults check so the recovery surface stays reachable.
  try:
    from app.providers import PROVIDER_NAMES
    _assert_provider_defaults(PROVIDER_NAMES)
  except Exception as exc:
    _log.error("provider defaults check skipped: %s", exc, exc_info=True)
  # One-way compatibility migration: the preference is now chat-local, but a
  # legacy true value left in the shared JSON would become global again after
  # a rollback. Run before serving (not lazily on the first settings request)
  # so every successful boot removes that rollback hazard.
  try:
    from app.providers import remove_legacy_global_auto_resume_setting
    if not remove_legacy_global_auto_resume_setting(get_settings().data_dir):
      _log.warning("legacy global auto-resume setting cleanup did not persist")
  except Exception as exc:
    # providers.py and the shared file are recovery-surface dependencies:
    # report cleanup failure without making the whole service unbootable.
    _log.error("legacy global auto-resume cleanup failed: %s", exc, exc_info=True)
  _init_db()
  # First-boot claim gate (card 261). Publish/reconcile the one-time setup
  # claim now — after _init_db() so the owner state is readable, and before
  # `yield` so no request can reach POST /api/auth/setup before the gate
  # exists. Verification remains disabled until reconciliation succeeds this
  # boot, even if a filesystem error prevents deletion of an old claim. We
  # still keep the recovery surface reachable and log the failure for the
  # operator.
  try:
    from app import setup_claim
    setup_claim.begin_initialization()
    with SessionLocal() as _claim_db:
      _owner_exists = _claim_db.query(models.Owner).first() is not None
    _claim_code = setup_claim.ensure_claim(
      get_settings().data_dir, owner_exists=_owner_exists,
    )
    if _claim_code:
      _log.warning(
        "SETUP CLAIM (first-boot owner gate): POST /api/auth/setup requires "
        'claim="%s". It is single-use; preset it with MOBIUS_SETUP_CLAIM.',
        _claim_code,
      )
  except Exception as exc:
    _log.error("setup claim init failed: %s", exc, exc_info=True)
  # One-time semantic migration for the per-chat prompt boundary. Existing
  # chats were started before the snapshot column existed, so freeze their
  # currently effective base + system-app prompt now, before serving requests.
  # Empty drafts remain unsnapshotted and pick up the live app set when their
  # first turn actually starts.
  try:
    from app.chat import (
      _chat_settings_dict,
      _custom_system_prompt,
      _read_skill_text,
    )
    from app.system_prompts import backfill_started_chat_prompt_snapshots
    with SessionLocal() as db:
      count = backfill_started_chat_prompt_snapshots(
        db,
        lambda chat: (
          _custom_system_prompt(_chat_settings_dict(chat))
          or _read_skill_text()
        ),
      )
      db.commit()
    if count:
      _log.info("captured system prompt snapshots for %s existing chats", count)
  except Exception as exc:
    # Keep the recovery surface available. A chat still snapshots before its
    # next provider call, and the failure is visible for operator repair.
    _log.error("system prompt snapshot backfill failed: %s", exc, exc_info=True)

  # Fix old chat-image storage forward to the canonical media/ path before
  # serving requests. The migration is idempotent and removes the need for a
  # permanent compatibility route in the live API.
  app.state.media_migration_failed = False
  try:
    from app.chat_media import fix_forward_chat_media
    _media_db = SessionLocal()
    try:
      fix_forward_chat_media(_media_db, get_settings().data_dir)
    finally:
      _media_db.close()
  except Exception as exc:
    _log.error("chat media fix-forward failed: %s", exc, exc_info=True)
    app.state.media_migration_failed = True
  # Crash recovery: a process death (OOM / SIGKILL — a recurring
  # failure mode on this host) mid-turn leaves the chat's durable
  # run marker set but the in-memory registry empty. Reconcile those
  # stranded chats now, before the server accepts requests, so a
  # mid-turn crash resolves cleanly on reopen instead of spinning
  # "running" forever and stranding queued messages. Wrapped like the
  # other lifespan steps: a failure here must not brick the recovery
  # surface. Runs single-threaded pre-serving, so no queue-lock
  # contention — see reconcile_interrupted_chats for the argument.
  # Chats reconciled at boot (incl. any turn paused by a drain-gated restart),
  # carried to the post-`init_vapid` notify below — VAPID must be initialized
  # before a push can be delivered.
  _reconciled_chats: list[str] = []
  try:
    from app.chat import reconcile_interrupted_chats
    from app.database import SessionLocal as _ReconcileSession
    _rc_db = _ReconcileSession()
    try:
      _reconciled_chats = reconcile_interrupted_chats(_rc_db) or []
    finally:
      _rc_db.close()
  except Exception as exc:
    _log.error("startup chat reconciliation failed: %s", exc, exc_info=True)
    # Expose the failure through /api/debug/status so operators and
    # tests can detect it without tailing logs. The never-crash-boot
    # contract is preserved: we only set a flag, never raise.
    app.state.reconciliation_failed = True
  # Discard any `*.js.staging` bundle left by an interrupted compile. Staging
  # paths are never stored on App rows or served.
  try:
    from app.compiler import reap_staging_bundles
    reap_staging_bundles()
  except Exception as exc:
    _log.error("staging-bundle reap failed: %s", exc, exc_info=True)
  # Recompile any live App row whose compiled bundle is missing/empty. New
  # content-addressed writes cannot commit a missing path, but this still heals
  # legacy interrupted rows, manual deletion, and incomplete volume restores.
  # Runs after staging reap and before serving. Wrapped + per-app error-isolated
  # so neither bad source nor a compiler failure can brick recovery.
  try:
    from app.compiler import (
      reap_orphaned_bundles,
      reconcile_missing_bundles,
      reconcile_outdated_bundles,
    )
    from app.database import SessionLocal as _BundleSession
    _bn_db = _BundleSession()
    try:
      healed_bundle_ids = await reconcile_missing_bundles(_bn_db)
      # Compiler ABI migrations fix forward: every app frame receives one
      # dependency-complete module, so legacy external-import bundles must be
      # rebuilt before requests can expose them. The sweep is atomic per app
      # and resumable across interrupted boots.
      migrated_bundle_ids = await reconcile_outdated_bundles(_bn_db)
      # Both crash boundaries are now coherent but may leak one immutable file:
      # before commit it is the unpublished candidate, after commit it is the
      # superseded prior bundle. Reap only after reconciliation has established
      # every live row's final path; tombstoned rows remain referenced/recoverable.
      removed_bundle_paths = reap_orphaned_bundles(_bn_db)
      if healed_bundle_ids or migrated_bundle_ids or removed_bundle_paths:
        _log.info(
          "compiled-bundle reconciliation: healed=%d migrated=%d reaped=%d",
          len(healed_bundle_ids),
          len(migrated_bundle_ids),
          len(removed_bundle_paths),
        )
    finally:
      _bn_db.close()
  except Exception as exc:
    _log.error("compiled-bundle reconcile wiring failed: %s", exc, exc_info=True)
  # Start the single-writer chat-persistence actor AFTER db init and
  # crash reconciliation. Order is load-bearing: reconcile_interrupted_chats
  # must run BEFORE the actor exists — recovery has to work even when
  # persistence is degraded, so it never routes through the actor.
  # start_writer catches its own startup failure (marks the writer fatal
  # rather than raising), so a writer that can't start can't brick boot
  # or the recovery surface. The actor is LIVE: it is the chat-persistence
  # path the C2 write routes/runners submit every transcript write through.
  try:
    from app.chat_writer import start_writer
    start_writer()
  except Exception as exc:
    _log.error("chat writer start wiring failed: %s", exc, exc_info=True)
  # Wrapped: push.py is on the agent's write surface. VAPID init is
  # nice-to-have (no push notifications without it) but not boot-critical.
  try:
    from app.push import init_vapid
    init_vapid()
  except Exception as exc:
    _log.error("init_vapid failed: %s", exc, exc_info=True)
  # Boot resume notify (design §2.2 step 4). Runs AFTER init_vapid so the push
  # can actually deliver: one "tap to resume" notification for any turn left
  # paused by a drain-gated restart (or crash) that boot reconcile finalized.
  # Best-effort — the resumable note is already durable in the transcript, so a
  # notify failure never blocks boot.
  try:
    if _reconciled_chats:
      from app.chat import notify_after_reconcile
      from app.database import SessionLocal as _NotifySession
      _nt_db = _NotifySession()
      try:
        notify_after_reconcile(_nt_db, _reconciled_chats)
      finally:
        _nt_db.close()
  except Exception as exc:
    _log.error("paused-turn resume notify failed: %s", exc, exc_info=True)
  # First-boot auto-install of the App Store, Memory, and Reflection. The
  # bootstrap module is idempotent and isolates failures so a GitHub blip must
  # not crash lifespan or prevent another bootstrap app from installing.
  try:
    from app.bootstrap import ensure_bootstrap_apps_installed
    from app.database import SessionLocal as _BootstrapSession
    _bs_db = _BootstrapSession()
    try:
      await ensure_bootstrap_apps_installed(_bs_db)
    finally:
      _bs_db.close()
  except Exception as exc:
    _log.error("bootstrap app install wiring failed: %s", exc, exc_info=True)
  # Reconcile the DB-independent recovery credential seed with the current
  # owner. Backfills instances that completed setup before the seed
  # existed and keeps it current; idempotent (no write when unchanged) and
  # best-effort — recovery is a convenience layer, never boot-critical.
  try:
    from app import recovery_seed
    from app.database import SessionLocal as _SeedSession
    _seed_db = _SeedSession()
    try:
      recovery_seed.sync_owner_seed(_seed_db)
    finally:
      _seed_db.close()
  except Exception as exc:
    _log.error("recovery owner seed sync wiring failed: %s", exc, exc_info=True)
  # Backfill source_dir for legacy app rows. The file watcher resolves
  # /data/apps/<slug>/index.jsx → app.id via exact source_dir match;
  # rows with NULL (older builds, or apps imported without going
  # through register_app.py) would silently never auto-recompile.
  # Derive the same slug shape register_app.py uses and persist it.
  #
  # Wrapped: app/routes/apps.py is on the agent's write surface. The
  # routes/__init__.py _load() scaffold stubs apps_router on import
  # failure, but this direct import bypasses that — without the
  # try/except a SyntaxError in apps.py would crash lifespan and take
  # /recover/chat down with it (the exact failure mode the scaffold
  # was built to prevent).
  try:
    from pathlib import Path as _Path
    from app import models as _models
    _db = SessionLocal()
    try:
      legacy = _db.query(_models.App).filter(
        _models.App.source_dir.is_(None)
      ).all()
      changed = False
      for _a in legacy:
        # Derive from the UNIQUE slug (the migration assigns one) — NOT the raw
        # name, which would give two legacy rows named "News" the same
        # /data/apps/news tree. Skip a dir another app already claims so the
        # repair never creates a shared source tree.
        if not _a.slug:
          continue
        candidate = str(_Path(settings.data_dir) / "apps" / _a.slug)
        if _db.query(_models.App).filter(
          _models.App.id != _a.id, _models.App.source_dir == candidate
        ).first():
          continue
        _a.source_dir = candidate
        changed = True
      if changed:
        _db.commit()
    finally:
      _db.close()
  except Exception as exc:
    _log.error("source_dir backfill failed: %s", exc, exc_info=True)
  # Rewrite schedules created by older releases through the revocable common
  # job runner. The entrypoint does not start cron until lifespan completes,
  # so replayed legacy direct entries cannot fire in the migration window.
  # Best-effort and per-app isolated: one malformed legacy source must not
  # prevent the server (or unrelated schedules) from starting.
  try:
    from app.routes.apps import reconcile_app_cron_supervision
    from app.database import SessionLocal as _CronSession
    _cron_db = _CronSession()
    try:
      _cron_count, _cron_warnings = reconcile_app_cron_supervision(_cron_db)
    finally:
      _cron_db.close()
    if _cron_count:
      _log.info("supervised %d app cron schedule(s)", _cron_count)
    for _warning in _cron_warnings:
      _log.warning("app cron supervision skipped: %s", _warning)
    if not _cron_warnings:
      _cron_ready = (
        Path(settings.data_dir) / "run" / "app-cron-supervision-ready"
      )
      _cron_ready.parent.mkdir(parents=True, exist_ok=True)
      _cron_ready.write_text(f"{_BOOT_ID}\n", encoding="utf-8")
  except Exception as exc:
    _log.error("app cron supervision wiring failed: %s", exc, exc_info=True)
  # Route the app-watcher and provider model-registry diagnostics to the
  # durable rotating chat.log handler. Both loggers otherwise land only on
  # stdout (via lastResort), which evaporates on container recreation — that
  # is exactly what erased the forensic trail for the beat-machine app-update
  # incident. Keep stdout too: the handler is ADDED, propagation stays on. The
  # SAME shared handler instance is attached (never a second RotatingFileHandler
  # on the same path, which would race on rotation). app_watcher runs at INFO
  # so the merge-replay wait-states are captured; the model-registry child logs
  # only its warning.
  try:
    from app.chat import get_chat_log_handler
    _diag_handler = get_chat_log_handler()
    for _diag_name, _diag_level in (
      ("app.app_watcher", logging.INFO),
      ("app.providers.models", logging.WARNING),
    ):
      _diag_logger = logging.getLogger(_diag_name)
      if _diag_handler not in _diag_logger.handlers:
        _diag_logger.addHandler(_diag_handler)
      _diag_logger.setLevel(_diag_level)
  except Exception as exc:
    _log.error("chat.log diagnostic routing failed: %s", exc, exc_info=True)
  # Start the JSX file watcher so direct edits to /data/apps/*/index.jsx
  # auto-recompile and refresh the served bundle — agents don't need to
  # re-run register_app.py just to push a code change.
  # Wrapped: app/app_watcher.py is on the agent's write surface; a
  # failure must not crash lifespan.
  _observer = None
  _handler = None
  _frontend_observer = None
  _frontend_handler = None
  try:
    from app.app_watcher import start_watcher
    _observer, _handler = start_watcher(_asyncio.get_running_loop())
  except Exception as exc:
    _log.error("start_watcher failed: %s", exc, exc_info=True)
  # Start the whole-repo frontend watcher when /data/platform is active.
  # Wrapped separately from app_watcher: a broken frontend build path must not
  # disable mini-app recompiles or crash lifespan.
  try:
    from pathlib import Path as _Path
    _frontend_src = _Path("/data/platform/frontend/src")
    if _frontend_src.is_dir():
      from app.frontend_watcher import start_supervised_watcher
      _frontend_observer, _frontend_handler = await start_supervised_watcher(
        _asyncio.get_running_loop(),
      )
  except Exception as exc:
    _log.error("start_frontend_watcher failed: %s", exc, exc_info=True)
  # Runtime liveness watchdog. reconcile_interrupted_chats only runs at boot,
  # so a turn that leaves its run marker set without a process restart (a
  # FAILED_LEAVE_MARKER terminal, or the late-promote gap) would hold the chat
  # "running" forever and make the app look permanently busy. This periodic
  # sweep clears such orphaned markers between boots. Wrapped so a sweep failure
  # can't kill the loop; the loop is cancelled on shutdown.
  _wedged_sweep_task = None
  _stalled_live_task = None
  _reset_park_task = None
  _browser_profile_task = None
  _writer_supervisor_task = None
  try:
    from app.chat import (
      sweep_idle_pending_chats,
      sweep_reset_parks,
      sweep_stalled_live_runs,
      sweep_wedged_run_markers,
    )
    from app.database import SessionLocal as _SweepSession

    async def _wedged_marker_loop():
      while True:
        await _asyncio.sleep(60)
        try:
          _sw_db = _SweepSession()
          try:
            await sweep_wedged_run_markers(_sw_db)
            await sweep_idle_pending_chats(_sw_db)
          finally:
            _sw_db.close()
        except _asyncio.CancelledError:
          raise
        except Exception as _exc:
          _log.error("wedged-marker sweep failed: %s", _exc, exc_info=True)

    _wedged_sweep_task = _asyncio.create_task(_wedged_marker_loop())

    async def _stalled_live_loop():
      import time as _time
      while True:
        await _sleep_with_lag_warning(
          _asyncio.sleep,
          _time.monotonic,
          _log,
        )
        try:
          _sl_db = _SweepSession()
          try:
            await sweep_stalled_live_runs(_sl_db)
          finally:
            _sl_db.close()
        except _asyncio.CancelledError:
          raise
        except Exception as _exc:
          _log.error("stalled-live sweep failed: %s", _exc, exc_info=True)

    _stalled_live_task = _asyncio.create_task(_stalled_live_loop())

    # Provider-limit reset sweep (design §2.4): notifies once when a parked
    # turn's reset time arrives, and — when the owner opted in — starts the
    # strictly-serial auto-resume. Same shape as the two loops above.
    async def _reset_park_loop():
      while True:
        await _asyncio.sleep(60)
        try:
          _rp_db = _SweepSession()
          try:
            await sweep_reset_parks(_rp_db)
          finally:
            _rp_db.close()
        except _asyncio.CancelledError:
          raise
        except Exception as _exc:
          _log.error("reset-park sweep failed: %s", _exc, exc_info=True)

    _reset_park_task = _asyncio.create_task(_reset_park_loop())

    # The 60-second loop cadence is the writer respawn backoff.
    async def _writer_supervisor_loop():
      from app.chat_writer import supervise_writer
      while True:
        await _asyncio.sleep(60)
        try:
          supervise_writer()
        except _asyncio.CancelledError:
          raise
        except Exception as _exc:
          _log.error("writer supervisor tick failed: %s", _exc, exc_info=True)

    _writer_supervisor_task = _asyncio.create_task(_writer_supervisor_loop())

    async def _browser_profile_loop():
      # Keep boot latency predictable; the first quota scan is low-priority.
      await _asyncio.sleep(300)
      while True:
        _profile_sweep_seconds = 60 * 60
        try:
          from app.browser_profiles import (
            browser_profile_sweep_seconds,
            chat_activity_snapshot,
            enforce_browser_profile_quota,
          )
          _profile_sweep_seconds = browser_profile_sweep_seconds()
          from app.runner_registry import registry as _runner_registry
          _bp_db = _SweepSession()
          try:
            _chat_snapshot = chat_activity_snapshot(_bp_db)
          finally:
            _bp_db.close()
          _profile_result = await _asyncio.to_thread(
            enforce_browser_profile_quota,
            settings.data_dir,
            _chat_snapshot,
            _runner_registry.all_alive_chat_ids(),
          )
          if _profile_result["reclaimed_bytes"]:
            _log.info(
              "agent-browser profile quota reclaimed %d bytes",
              _profile_result["reclaimed_bytes"],
            )
        except _asyncio.CancelledError:
          raise
        except Exception as _exc:
          _log.error(
            "agent-browser profile quota failed: %s", _exc, exc_info=True,
          )
        await _asyncio.sleep(_profile_sweep_seconds)

    _browser_profile_task = _asyncio.create_task(_browser_profile_loop())
  except Exception as exc:
    _log.error("chat liveness sweep wiring failed: %s", exc, exc_info=True)
  try:
    yield
  finally:
    # Preserve the final partial request-error windows across graceful restarts.
    # This is one bounded batch append, not one write per response.
    activity.flush_request_errors()
    # Stop the liveness watchdog before the writer drains.
    if _wedged_sweep_task is not None:
      _wedged_sweep_task.cancel()
    if _stalled_live_task is not None:
      _stalled_live_task.cancel()
    if _reset_park_task is not None:
      _reset_park_task.cancel()
    if _browser_profile_task is not None:
      _browser_profile_task.cancel()
    if _writer_supervisor_task is not None:
      _writer_supervisor_task.cancel()
    # Drain + join the chat-writer actor so any in-flight persistence
    # completes before the process exits. Wrapped: a stop failure must
    # not mask the rest of shutdown.
    try:
      from app.chat_writer import stop_writer
      stop_writer()
    except Exception as exc:
      _log.error("chat writer stop failed: %s", exc, exc_info=True)
    # Drain pending debounce timers first so they can't post coroutines
    # to a loop that's about to close, then stop+join the observer.
    if _handler is not None:
      try:
        _handler.close()
      except Exception as exc:
        _log.error("watcher handler.close failed: %s", exc, exc_info=True)
    if _frontend_handler is not None:
      try:
        _frontend_handler.close()
      except Exception as exc:
        _log.error(
          "frontend watcher handler.close failed: %s", exc, exc_info=True,
        )
    if _observer is not None:
      # Split stop/join into independent try blocks so a stop()
      # failure doesn't skip join() — otherwise the watchdog thread
      # would never be reaped on shutdown. In practice both are very
      # unlikely to raise, but structurally a shared try would let
      # one fault swallow the other.
      try:
        _observer.stop()
      except Exception as exc:
        _log.error("watcher observer.stop failed: %s", exc, exc_info=True)
      try:
        _observer.join(timeout=2)
      except Exception as exc:
        _log.error("watcher observer.join failed: %s", exc, exc_info=True)
    if _frontend_observer is not None:
      try:
        _frontend_observer.stop()
      except Exception as exc:
        _log.error(
          "frontend watcher observer.stop failed: %s", exc, exc_info=True,
        )
      try:
        _frontend_observer.join(timeout=2)
      except Exception as exc:
        _log.error(
          "frontend watcher observer.join failed: %s", exc, exc_info=True,
        )

settings = get_settings()

def _real_peer_address(request: Request) -> str:
  """Rate-limit key: actual TCP peer address, never X-Forwarded-For.

  Port 8000 is only exposed inside the Docker network (not published to the
  host), so the only peer that can reach it is Caddy. Trusting
  X-Forwarded-For would let any client that injects that header bypass
  per-IP limits; the real peer address is simpler and correct.
  """
  return request.client.host if request.client else "127.0.0.1"


limiter = Limiter(
  key_func=_real_peer_address, default_limits=["120/minute"]
)

app = FastAPI(
  title="Möbius",
  description="Self-hosted AI agent platform.",
  version="0.1.0",
  lifespan=lifespan,
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# Global request-body backstop. Endpoints that read raw bodies stream-cap
# themselves (storage PUT 50 MB, icon 12 MB via storage_io.read_capped_body),
# but FastAPI buffers the WHOLE body for Pydantic-parsed endpoints (e.g. a
# create with a huge jsx_source) before validation — an unbounded body there
# could OOM the memory-tight host (Codex review round-9 #4, round-10 #5). The
# cap sits ABOVE every legitimate route limit (storage 50 MB, uploads 20 MB) so
# it only ever stops abuse.
_MAX_REQUEST_BODY_BYTES = 64 * 1024 * 1024


class _BodySizeLimitMiddleware:
  """ASGI middleware that bounds the request body — including chunked bodies
  with no Content-Length.

  A declared Content-Length over the cap is rejected with 413 before the app
  runs. Otherwise the body stream is wrapped with a running byte counter; once
  it crosses the cap we stop feeding the app and signal `http.disconnect`, so
  the app aborts (a Pydantic endpoint sees a truncated body and 422s) rather
  than buffering an unbounded body into memory. Pure ASGI (not
  BaseHTTPMiddleware) so it never itself buffers the body.
  """

  def __init__(self, app, max_bytes: int):
    self.app = app
    self.max_bytes = max_bytes

  async def __call__(self, scope, receive, send):
    if scope["type"] != "http":
      return await self.app(scope, receive, send)
    for name, value in scope.get("headers") or []:
      if name == b"content-length":
        try:
          if int(value) > self.max_bytes:
            return await self._reject(send)
        except ValueError:
          pass
        break
    received = 0
    disconnected = False

    async def limited_receive():
      nonlocal received, disconnected
      if disconnected:
        return {"type": "http.disconnect"}
      message = await receive()
      if message["type"] == "http.request":
        received += len(message.get("body", b""))
        if received > self.max_bytes:
          disconnected = True
          return {"type": "http.disconnect"}
      return message

    return await self.app(scope, limited_receive, send)

  async def _reject(self, send):
    await send({
      "type": "http.response.start",
      "status": 413,
      "headers": [(b"content-type", b"application/json")],
    })
    await send({
      "type": "http.response.body",
      "body": b'{"detail":"Request body too large."}',
    })


# Standard security headers. The bundled Caddy sets these, but production is
# fronted by an external Caddy whose vhost has no header block (and managed
# deployments often proxy the app directly), so prod serves NONE of them today.
# Setting them here means they hold regardless of what fronts the app. These are
# resource-load-agnostic — they protect against clickjacking, MIME-sniffing, TLS
# downgrade, and referrer leakage WITHOUT restricting what apps may load, so web
# images / external embeds keep working. There is deliberately no global
# Content-Security-Policy here yet: mini-apps intentionally support user-chosen
# external resources and a strict shell-wide policy would break that contract.
# App isolation instead comes from opaque-origin sandboxed frames plus scoped
# tokens. Clickjacking is covered by X-Frame-Options without a CSP. Narrow
# exceptions are the inert embedded-chat bootstrap, response-sandboxed packaged
# documents, and an explicitly configured shared service-gateway surface.
_SECURITY_HEADERS = [
  (b"x-content-type-options", b"nosniff"),
  (b"x-frame-options", b"SAMEORIGIN"),
  (b"referrer-policy", b"strict-origin-when-cross-origin"),
  (b"permissions-policy", b"camera=(), geolocation=()"),
  (b"strict-transport-security",
   b"max-age=31536000; includeSubDomains; preload"),
]
_SECURITY_HEADER_NAMES = frozenset(name for name, _ in _SECURITY_HEADERS)
_X_FRAME_OPTIONS = b"x-frame-options"
_CONTENT_SECURITY_POLICY = b"content-security-policy"
_OPAQUE_STATIC_EMBED_PREFIX = "/app-embeds/by-id/"
_PUBLISHED_SITE_PREFIX = "/sites/"

# This isolation boundary must always be enforced, never Report-Only: browsers
# ignore the CSP sandbox directive in a Report-Only policy.
_STATIC_EMBED_CSP = (
  "sandbox allow-scripts allow-forms allow-pointer-lock; default-src 'self'; "
  "script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
  "font-src 'self' data:; connect-src 'self'; img-src 'self' data: blob:; "
  "media-src 'self' blob:; worker-src 'self' blob:"
)

# Published sites (`/sites/<token>/`) are public snapshots of the owner's own
# agent-authored artifacts and Web Studio builds. The `sandbox` directive
# (WITHOUT allow-same-origin) forces the top-level document into an opaque
# origin so its JS cannot read the shell origin's localStorage/cookies/JWT —
# the credential boundary this closes. Unlike a packaged embed we do NOT lock
# resource loading to `'self'`: `/sites/` also serves multi-file Web Studio
# sites that may legitimately pull external assets, and the opaque-origin
# sandbox is the actual isolation, not resource confinement. We keep the
# sandbox capability set minimal (no modals/downloads/pointer-lock, never
# allow-popups-to-escape-sandbox) and add cheap defense-in-depth
# (object-src/base-uri/frame-ancestors). Residual accepted: a compromised
# external script a published page chose to include can read that page's own
# share token + public artifact data, but never the shell origin or the owner
# JWT. Must be enforcing, never Report-Only. X-Frame-Options SAMEORIGIN is
# KEPT (published pages open top-level; no cross-site framing need).
_PUBLISHED_SITE_CSP = (
  "sandbox allow-scripts allow-forms allow-popups; "
  "object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
)


def _frame_policy_exception(scope) -> bool:
  """Exact routes whose own isolation permits a non-SAMEORIGIN ancestor."""
  path = scope.get("path") or ""
  if path == "/shell/embed/chat":
    return True
  if path.startswith("/services/"):
    try:
      from app.routes.local_services import is_public_service_surface_request
      return is_public_service_surface_request(scope)
    except Exception:
      return False
  return False


class _SecurityHeadersMiddleware:
  """Authoritatively sets the platform security headers on every response. Pure
  ASGI so it never buffers a streaming body. It strips any same-named header a
  route may have set first and replaces it with the platform value, so no route
  can weaken the HSTS/MIME/etc. wall. Opaque static embeds get their enforced
  response sandbox here alongside their frame-policy exception. Other frame
  exceptions are exact routes whose inert response or gateway-origin adapter
  provides the replacement boundary; ordinary routes retain SAMEORIGIN."""

  def __init__(self, app):
    self.app = app

  async def __call__(self, scope, receive, send):
    if scope["type"] != "http":
      return await self.app(scope, receive, send)

    # Frameability and sandboxing are one policy for these namespaces; keep the
    # decisions adjacent so no response can receive only part of the boundary.
    path = scope.get("path") or ""
    opaque_static_embed = path.startswith(_OPAQUE_STATIC_EMBED_PREFIX)
    published_site = path.startswith(_PUBLISHED_SITE_PREFIX)
    # Both namespaces need an ENFORCED response CSP and the same protection on
    # the generic-500 path; only the embed also drops X-Frame-Options.
    response_sandboxed = opaque_static_embed or published_site
    response_headers = _SECURITY_HEADERS
    replaced_header_names = _SECURITY_HEADER_NAMES
    if opaque_static_embed or _frame_policy_exception(scope):
      response_headers = [
        (name, value) for name, value in _SECURITY_HEADERS
        if name != _X_FRAME_OPTIONS
      ]
    if response_sandboxed:
      csp = _STATIC_EMBED_CSP if opaque_static_embed else _PUBLISHED_SITE_CSP
      # Copy before appending so we never mutate the _SECURITY_HEADERS module
      # constant (the published-site branch leaves X-Frame-Options in place, so
      # response_headers is still that shared list here).
      response_headers = list(response_headers)
      response_headers.append((
        _CONTENT_SECURITY_POLICY,
        csp.encode("ascii"),
      ))
      replaced_header_names = replaced_header_names | {
        _CONTENT_SECURITY_POLICY
      }

    response_started = False

    async def _send(message):
      nonlocal response_started
      if message["type"] == "http.response.start":
        response_started = True
        headers = [
          (k, v) for k, v in message.get("headers", [])
          if k.lower() not in replaced_header_names
        ]
        headers.extend(response_headers)
        message["headers"] = headers
      await send(message)

    try:
      return await self.app(scope, receive, _send)
    except Exception:
      # Starlette's unhandled-error response is outside user middleware. Send
      # this namespace's generic 500 through our wrapper before re-raising so
      # the outer layer still logs it without bypassing the sandbox boundary.
      if response_sandboxed and not response_started:
        response = Response(
          "Internal Server Error",
          status_code=500,
          media_type="text/plain",
        )
        await response(scope, receive, _send)
      raise


class _DatabaseRequestContextMiddleware:
  """Attributes connection checkout time to the owning HTTP request."""

  def __init__(self, app):
    self.app = app

  async def __call__(self, scope, receive, send):
    if scope["type"] != "http":
      return await self.app(scope, receive, send)
    label = f'{scope.get("method", "?")} {scope.get("path", "/")}'
    token = set_database_request_label(label)
    try:
      return await self.app(scope, receive, send)
    finally:
      reset_database_request_label(token)


class _RequestErrorTelemetryMiddleware:
  """Aggregate failed responses by matched route without retaining raw URLs.

  Successful requests do no logging or aggregation. For failures, the activity
  module keeps bounded in-memory minute counters and writes compact summaries,
  so a retry loop remains observable without amplifying its CPU or disk cost.
  FastAPI leaves the matched route template and path params in the ASGI scope;
  those templates contain no user paths or query values.
  """

  def __init__(self, app):
    self.app = app

  async def __call__(self, scope, receive, send):
    if scope["type"] != "http":
      return await self.app(scope, receive, send)
    status = None

    async def _send(message):
      nonlocal status
      if message["type"] == "http.response.start":
        status = int(message.get("status", 500))
      await send(message)

    try:
      return await self.app(scope, receive, _send)
    except Exception:
      status = status or 500
      raise
    finally:
      if status is not None and status >= 400:
        matched = scope.get("route")
        route = getattr(matched, "path", None) or "<unmatched>"
        raw_app_id = (scope.get("path_params") or {}).get("app_id")
        try:
          app_id = int(raw_app_id) if raw_app_id is not None else None
        except (TypeError, ValueError):
          app_id = None
        activity.record_request_error(
          scope.get("method", "?"), route, status, app_id,
        )


class _ServiceSurfaceHostMiddleware:
  """Prevent the service gateway host from becoming another Möbius origin."""

  def __init__(self, app):
    self.app = app

  async def __call__(self, scope, receive, send):
    if scope["type"] == "http":
      from app.routes.local_services import service_surface_host_allows_path
      if not service_surface_host_allows_path(scope):
        await send({
          "type": "http.response.start",
          "status": 404,
          "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        })
        await send({"type": "http.response.body", "body": b"Not found"})
        return
    return await self.app(scope, receive, send)


app.add_middleware(_BodySizeLimitMiddleware, max_bytes=_MAX_REQUEST_BODY_BYTES)

app.add_middleware(
  CORSMiddleware,
  # "null" is the origin of sandboxed iframes (allow-same-origin absent).
  # All sensitive endpoints are independently protected by JWT.
  allow_origins=[settings.frontend_origin, "null"],
  allow_credentials=False,
  allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
  # The app runtime uses X-Mobius-Version to opt into ETag reads, then
  # If-Match / If-None-Match for conflict-safe writes. Sandboxed app frames
  # have the opaque `null` origin, so Chromium preflights these non-simple
  # headers; omitting them here makes the runtime's versioned request fail
  # before it ever reaches the authenticated storage route.
  allow_headers=[
    "Authorization",
    "Content-Type",
    "X-Mobius-Embed-Instance",
    "X-Mobius-Version",
    "If-Match",
    "If-None-Match",
  ],
  # ETag is not CORS-safelisted. Expose it so getWithVersion() can actually
  # return the version token that the storage route intentionally emits.
  expose_headers=["ETag"],
)

# Security remains outside CORS and request-size enforcement so its headers
# land on those generated responses. Request context is outermost so every
# request receives one diagnostic label before middleware can touch the DB.
# A managed deployment may expose the application directly on more than one
# hostname without the bundled Caddyfile. Reserve each configured service host
# before routing so it can never serve the shell, APIs, recovery, or another
# service prefix.
app.add_middleware(_ServiceSurfaceHostMiddleware)
app.add_middleware(_SecurityHeadersMiddleware)
app.add_middleware(_DatabaseRequestContextMiddleware)
app.add_middleware(_RequestErrorTelemetryMiddleware)

# -- API routes --------------------------------------------------------
app.include_router(auth_router)
app.include_router(apps_router)
app.include_router(storage_router)
app.include_router(fs_router)
app.include_router(chat_router)
app.include_router(chat_embed_router)
app.include_router(chats_router)
app.include_router(chats_stream_router)
app.include_router(chat_logs_router)
# App-attributed chat contract (design §1) — a SECOND router defined in
# routes/chats.py under /api/app-chats, so it's imported directly rather
# than via routes/__init__'s `_load` (which only returns `.router`).
# Guarded: a broken chats.py already degraded chats_router to a stub
# above, and shouldn't take the whole app down here either.
try:
  from app.routes.chats import app_chat_router  # noqa: E402
  app.include_router(app_chat_router)
except Exception as _exc:  # pragma: no cover - defensive boot guard
  logging.getLogger(__name__).error(
    "app_chat_router not mounted: %s", _exc, exc_info=True,
  )
app.include_router(notify_router)
app.include_router(proxy_router)
app.include_router(local_services_router)
app.include_router(client_error_router)
app.include_router(client_signal_router)
app.include_router(settings_router)
app.include_router(platform_router)
app.include_router(uploads_router)
app.include_router(media_router)
app.include_router(secrets_router)
app.include_router(github_router)
app.include_router(push_router)
app.include_router(notifications_router)
app.include_router(debug_router)
app.include_router(theme_router)
app.include_router(admin_router)
app.include_router(self_reminders_router)
# Standalone PWA surface at /apps/<slug>/{,manifest.json,icon-N.png}.
# Registered AFTER the API routers but BEFORE the SPA catch-all
# (which mounts conditionally below at /{path:path}) so its explicit
# routes win.
app.include_router(standalone_router)
app.include_router(published_router)  # /sites/<token>/ — before the SPA catch-all


@app.get("/api/health")
def health(response: Response):
  """Returns a simple health check response.

  `Cache-Control: no-store` so the client's reachability probe
  (`useOnlineStatus`) can never be answered from any HTTP cache or heuristic
  freshness — the probe must reflect a real network round-trip. The probe
  already sends `cache: 'no-store'`, but the response carrying the directive
  too is belt-and-suspenders against an intermediary or a stale-200 path
  (a suspected contributor to the Android offline-probe-returns-true anomaly).
  `boot_id` is a per-worker marker the Settings restart flow uses to avoid
  reloading while the old process is still briefly answering before SIGTERM.
  """
  response.headers["Cache-Control"] = "no-store"
  return {"status": "ok", "boot_id": _BOOT_ID}


@app.get("/api/ready")
def ready(response: Response):
  """Readiness probe: 200 only when chat persistence can actually serve.

  Distinct from `/api/health` (liveness — the process is up and answering
  HTTP). The single-writer chat-persistence actor can fail to start, go
  fatal, or be stopping while the process still answers `/api/health` 200;
  in that window every chat write fails. A deploy (and `deploy-prod.sh`'s
  health gate) must NOT green on a process that can't persist a chat, so
  this route returns 503 until the writer is genuinely ready.

  `is_writer_ready()` (via `writer_readiness`) owns the predicate: the
  writer singleton exists, its worker thread is alive, and the actor is
  neither fatal nor stopping. The route only maps the verdict to a status
  code and surfaces the reason. Startup ordering is fine — the lifespan
  runs `start_writer()` before uvicorn serves, so there is no cold-start
  window where this false-fails.
  """
  response.headers["Cache-Control"] = "no-store"
  from app.chat_writer import writer_readiness
  is_ready, reason = writer_readiness()
  if is_ready:
    return {"ready": True}
  response.status_code = 503
  return {"ready": False, "reason": reason}


def _served_platform_identity(data_dir: str) -> dict:
  """The ACTUALLY-SERVED backend identity, distinct from the image ``build_sha``.

  The served backend is normally ``/data/platform/app``, which persists across
  image deploys. On a broken platform tree, entrypoint falls back to the baked
  floor. The entrypoint writes ``/tmp/serving-source`` (``platform``|``baked``)
  and ``/tmp/serving-sha`` at boot so this route reports the tree actually
  selected for uvicorn. Never raises — every field degrades to
  ``unknown``/``None``.
  """
  import os
  import subprocess

  out = {"serving_source": "unknown", "served_sha": None, "platform_sha": None,
         "platform_dirty": None, "baked_sha": None}
  try:
    sentinel = Path("/tmp/serving-source").read_text(encoding="utf-8").strip()
    if sentinel:
      out["serving_source"] = sentinel
  except Exception:  # incl. UnicodeError, which is not an OSError — never raise
    pass
  try:
    served_sha = Path("/tmp/serving-sha").read_text(encoding="utf-8").strip()
    out["served_sha"] = served_sha or None
  except Exception:
    pass
  repo = Path(data_dir) / "platform"
  try:
    out["baked_sha"] = (repo / ".baked-sha").read_text(encoding="utf-8").strip() or None
  except Exception:
    pass
  if out["serving_source"] == "platform" and (repo / ".git").exists():
    out["platform_sha"] = out["served_sha"]
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(repo.parent)}

    def _git(*args):
      return subprocess.run(["git", "-C", str(repo), *args],
                            capture_output=True, text=True, timeout=5, env=env)

    try:
      if not out["platform_sha"]:
        head = _git("rev-parse", "HEAD")
        if head.returncode == 0:
          out["platform_sha"] = head.stdout.strip() or None
      # dirty filters .baked-sha churn + untracked dotfiles, mirroring step-3b.
      st = _git("-c", "core.fileMode=false", "status", "--porcelain")
      if st.returncode == 0:
        dirty = [ln for ln in st.stdout.splitlines()
                 if ln.strip() and not ln.rstrip().endswith(".baked-sha")
                 and not ln.startswith("?? .")]
        out["platform_dirty"] = bool(dirty)
    except Exception:
      pass
  return out


def _served_frontend_identity() -> dict:
  """Identity of the frontend bundle ACTUALLY being served.

  The whole-repo platform serves the per-request-resolved static dir —
  ``/data/platform/frontend/dist`` when it is a complete build, else the baked
  ``/app/static`` floor. Vite injects a content-hashed asset name into
  ``index.html``, so hashing the served ``index.html`` yields an identity that
  changes on every rebuild the watcher swaps in: the frontend analogue of
  ``served_sha``. Resolving per request (not a boot-time snapshot) means a dist
  that appears after boot is reflected here without a restart. ``frontend_source``
  says which tree is live. Never raises.
  """
  import hashlib

  static_dir = _resolve_static_dir()
  out = {"served_frontend": None,
         "frontend_source": "baked" if static_dir == _baked_dir else "platform"}
  try:
    html = (static_dir / "index.html").read_bytes()
    out["served_frontend"] = hashlib.sha256(html).hexdigest()[:16]
  except Exception:  # missing/unreadable dist — degrade, never raise
    pass
  return out


@app.get("/api/version")
def version():
  """Returns the build identity the running image was built from.

  - ``sha``: the git commit baked at `docker build` time via the `BUILD_SHA`
    build-arg (Dockerfile + deploy-prod.sh); "unknown" for a local
    `docker compose up` that didn't pass it. Lets a deploy verify the SERVED
    backend matches the intended commit — the backend analogue of the
    frontend bundle-hash check (bundle-info.sh / verify-fresh.sh).
  - ``served_frontend``: a content hash of the ``index.html`` in the frontend
    dir ACTUALLY being served (``frontend_source`` = ``platform`` or ``baked``).
    Changes whenever the watcher swaps a fresh ``vite build`` into the served
    ``dist`` — the frontend analogue of ``served_sha``. Poll it to confirm a
    frontend edit went live.

  A full GitHub-release check + one-click update is a follow-up; this exposes
  the local build identity cleanly so the image-pull path is self-verifying.
  """
  settings = get_settings()
  return {"sha": settings.build_sha,
          "build_date": settings.build_date,
          # Browser setup verifies this dedicated test-container marker before
          # any write. Localhost is not sufficient evidence because a preview
          # proxy can still forward to the live app.
          "test_runtime": os.environ.get("MOBIUS_TEST_RUNTIME") == "1",
          **_served_platform_identity(settings.data_dir),
          **_served_frontend_identity()}


@app.api_route(
  "/api/{path:path}",
  methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
  include_in_schema=False,
)
def unknown_api(path: str):
  """Return a real API 404 instead of letting deleted endpoints fall through.

  The SPA catch-all below intentionally serves index.html for client routes,
  but `/api/*` misses are not client routes. Keeping this explicit makes
  removed backend surfaces disappear cleanly for every HTTP method. The
  prime example is the old `/api/ai` provider proxy, dropped 2026-06-05
  once apps moved to reaching models through the agent (`window.mobius.chat`,
  or a bundled server-side script run via `/api/apps/{id}/run-job`) rather
  than a synchronous in-backend completion endpoint.
  """
  raise HTTPException(status_code=404, detail="Not found.")


@app.get("/", include_in_schema=False)
def root_redirect():
  """Redirects the bare domain to the Möbius shell at `/shell/`.

  The PWA manifest's `scope` is `/shell/` so per-app sub-PWAs at
  `/apps/<slug>/` aren't absorbed into Möbius's install identity
  (the platform suppresses install prompts for in-scope URLs).
  Redirecting `/` keeps bookmarks and the bare-domain entry point
  working — users land where the shell actually lives.
  """
  from fastapi.responses import RedirectResponse
  return RedirectResponse(url="/shell/", status_code=308)


# -- Frontend static files (single-container mode) ---------------------
# Prefer the agent-editable whole-repo build at /data/platform/frontend/dist/
# if it exists and is complete (Vite root files + assets/ must be present).
# Fall back to the baked-in build at /app/static/ on any error.
_live_dir = Path(settings.data_dir) / "platform" / "frontend" / "dist"
# The baked SPA is at the IMAGE path /app/static, NOT relative to __file__.
# Under the clone serve model __file__ is /data/platform/backend/app/main.py, so
# `__file__.parent.parent / "static"` would resolve to /data/platform/backend/
# static (nonexistent) and the baked-frontend recovery fallback would be dead
# whenever /data/platform/frontend/dist is incomplete. Resolve it absolutely
# (overridable via MOBIUS_BAKED_STATIC_DIR for non-standard image layouts).
_baked_dir = Path(os.environ.get("MOBIUS_BAKED_STATIC_DIR", "/app/static"))


def _is_complete_build(d: Path) -> bool:
  """Returns True only if the directory looks like a complete Vite build."""
  return (
    d.is_dir()
    and (d / "assets").is_dir()
    and (d / "index.html").is_file()
    and (d / "sw.js").is_file()
    and (d / "manifest.webmanifest").is_file()
  )


# The served frontend is resolved PER REQUEST, never frozen at module load: the
# live build when it is complete, else the baked image floor. A dist that
# appears or rebuilds after boot is then served with no restart, and a dist
# gone incomplete mid-swap transparently falls back to the floor. A ~1s memo
# keeps the stat cost off the hot asset path without pinning a stale choice
# across a rebuild (a swap settles well inside one TTL).
_STATIC_DIR_TTL_SECS = 1.0
_static_dir_memo: dict = {"dir": None, "at": 0.0}


def _resolve_static_dir() -> Path:
  """Return the frontend dir serving this request: live dist if complete, else
  the baked floor. Memoized for ``_STATIC_DIR_TTL_SECS``."""
  now = time.monotonic()
  memo = _static_dir_memo
  if memo["dir"] is not None and now - memo["at"] < _STATIC_DIR_TTL_SECS:
    return memo["dir"]
  resolved = _live_dir if _is_complete_build(_live_dir) else _baked_dir
  memo["dir"] = resolved
  memo["at"] = now
  return resolved


# The asset attic — frontend_watcher hardlinks each OUTGOING generation's
# content-hashed assets here on a dist swap. A sibling of ``dist`` so the
# hardlinks stay on one filesystem. Request-time /assets resolution serves a
# dist miss from these retained old generations so an unreloaded tab never 404s
# its chunk graph after a swap.
_ATTIC_DIR = _live_dir.parent / ".assets-attic"


def _resolve_asset_file(asset_path: str) -> Path | None:
  """Resolve ``/assets/<asset_path>`` to a file on disk, or None on a miss.

  Searches, in order, the served build's ``assets``, the attic's retained old
  generations, then the baked floor. Vite content-hashes every asset name, so
  those names never collide across generations — the union always yields the
  right bytes for whichever generation the client loaded, and the search order
  only affects which byte-identical copy answers first. A miss returns None so
  the caller emits a plain 404, never the SPA HTML: a JS module served as
  ``text/html`` is MIME-rejected by the browser and poisons the cache-first
  service worker (exactly the missing-``three.core.js`` failure). Each
  candidate is containment-checked against its root so ``..`` cannot escape.
  """
  roots = [_resolve_static_dir() / "assets"]
  try:
    roots.extend(p for p in _ATTIC_DIR.glob("gen-*/assets") if p.is_dir())
  except OSError:
    pass
  roots.append(_baked_dir / "assets")  # per-request corruption/boot floor
  for root in roots:
    try:
      root_r = root.resolve()
      target = (root_r / asset_path).resolve()
    except OSError:
      continue
    if target == root_r or root_r not in target.parents:
      continue  # the dir itself, or a `..` traversal escaping the root
    if target.is_file():
      return target
  return None


def _is_static_asset_path(path: str) -> bool:
  """True for paths that must 404 on a miss rather than fall through to
  the SPA HTML.

  A module/asset URL served as `200 text/html` (the SPA fallback) is
  rejected by the browser's strict module-MIME check AND poisons a
  cache-first service worker — this is exactly how a missing
  `three.core.js` surfaced as "failed to load dynamic module". The HTML
  fallback is only meaningful for app routes, which have no file
  extension. We keep the set narrow (code/style assets) so a missing
  image still degrades gracefully instead of 404-ing a real route.

  The extension check matches code/asset URLs ANYWHERE (not just under
  `vendor/`/`assets/`) on purpose: a module miss outside those namespaces
  must also 404 rather than poison the SW with text/html. SPA client
  routes are extensionless by convention here, so this never 404s a real
  route — but if a future client route needs a `.js`/`.json` suffix,
  drop that extension from the set.
  """
  if path == "index.html":
    return False
  return (
    # First path segment — catches both `vendor` and `vendor/<file>`
    # without over-matching a route like `vendorfoo`.
    path.split("/", 1)[0] in {"vendor", "assets"}
    or path == "sw.js"
    or path.rsplit(".", 1)[-1] in {
      "js", "mjs", "css", "html", "map", "wasm", "json",
    }
  )


_RESERVED_TOP_LEVEL_APP_ALIASES = {
  "api",
  "app",
  "app-assets",
  "apps",
  "assets",
  "chat",
  "recover",
  "shell",
  "sw.js",
  "vendor",
}


def _public_static_headers(path: str) -> dict[str, str]:
  """Headers required when public shell assets cross an opaque app origin.

  Sandboxed app frames intentionally have the effective origin ``null`` and
  import both ``/mobius-runtime.js`` and the public modules under ``/vendor``.
  The nested chat embed inherits that opaque origin and loads the Vite shell
  JavaScript and CSS under ``/assets``.  All three namespaces are also fetched
  and cached by the shell service worker without an Origin header.  CORS
  middleware can decorate a direct opaque-origin request, but it cannot repair
  that already-cached response when the worker later returns it to the frame.
  Make the public executable assets intrinsically cross-origin readable so both
  the HTTP cache and service-worker cache preserve the contract.
  """
  if (
    path == "mobius-runtime.js"
    or path.split("/", 1)[0] in {"assets", "vendor"}
  ):
    return {"Access-Control-Allow-Origin": "*"}
  return {}


def _top_level_app_slug_alias(path: str) -> str | None:
  """Return an app slug for legacy top-level app URLs like `/cuberun`.

  Standalone apps are canonical at `/apps/<slug>/`, but older install
  experiments and shortcuts used `/<slug>`. If the root-scoped shell SW does
  not intercept that navigation, FastAPI's SPA fallback would otherwise serve
  the Mobius shell at `/<slug>`, which looks like the app opened a copy of
  Mobius. Redirect exact single-segment app slugs to the canonical standalone
  URL before serving the SPA.
  """
  slug = path.strip("/")
  if not slug or "/" in slug:
    return None
  if not all(ch.isalnum() or ch in "-_" for ch in slug):
    return None
  if slug in _RESERVED_TOP_LEVEL_APP_ALIASES:
    return None
  db = SessionLocal()
  try:
    # Only LIVE apps redirect — a tombstoned (soft-deleted) app's `/<slug>`
    # shouldn't bounce to a now-404 standalone route (feature 110).
    exists = (
      db.query(models.App.id)
      .filter(models.App.slug == slug, models.App.deleted_at.is_(None))
      .first()
    )
    return slug if exists else None
  finally:
    db.close()


def _app_source_dir_for_static_asset(
  *, slug: str | None = None, app_id: int | None = None,
) -> str | None:
  db = SessionLocal()
  try:
    # Tombstoned apps don't serve their /app-assets/ static files either —
    # consistent with the frame/module/standalone routes (feature 110).
    query = db.query(models.App.source_dir).filter(
      models.App.deleted_at.is_(None)
    )
    if app_id is not None:
      row = query.filter(models.App.id == app_id).first()
    elif slug is not None:
      row = query.filter(models.App.slug == slug).first()
    else:
      row = None
    return row[0] if row else None
  finally:
    db.close()


# A content-hash segment in the filename (main.8f3a2b1c.js,
# commando.f3b9c2e1a4.ttf) marks the asset immutable: a re-install that
# changes the bytes ships a different name, so the URL itself is the
# validator. Mirrored by isImmutableAppAsset in frontend/src/
# sw-cache-policy.js — keep the two in sync.
#
# The lookahead requires at least one ALPHABETIC hex digit (a-f) so an
# all-DIGIT segment isn't mistaken for a content hash: a date-stamped name
# like IMG-20260612.png or report.20260101.html is replaced in place on a
# re-upload and MUST keep revalidate semantics — marking it immutable would
# pin a year-stale copy in every client's cache. A real esbuild/Vite hash
# always mixes in a-f (it's hex of a digest), so this never misfires on a
# genuine content hash.
_HASHED_ASSET_NAME = re.compile(
  r"[.-](?=[0-9a-f]*[a-f])[0-9a-f]{8,}\.", re.IGNORECASE
)


def _client_copy_is_fresh(request: Request, etag: str, mtime: float) -> bool:
  """True when conditional headers prove the client's copy is current.

  If-None-Match takes precedence over If-Modified-Since when both are
  present (RFC 7232 section 6); the date check is the fallback for
  clients that dropped the ETag.
  """
  if_none_match = request.headers.get("if-none-match")
  if if_none_match is not None:
    if if_none_match.strip() == "*":
      return True
    candidates = [
      tag.strip().removeprefix("W/") for tag in if_none_match.split(",")
    ]
    return etag in candidates
  if_modified_since = request.headers.get("if-modified-since")
  if if_modified_since is not None:
    try:
      since = parsedate_to_datetime(if_modified_since)
    except (TypeError, ValueError):
      return False
    if since.tzinfo is None:
      since = since.replace(tzinfo=timezone.utc)
    # HTTP dates have one-second resolution, so compare whole seconds.
    return int(mtime) <= since.timestamp()
  return False


def _serve_app_static_asset(
  source_dir: str | None, asset_path: str, request: Request,
):
  if not source_dir:
    raise HTTPException(status_code=404, detail="Not found.")

  root = (Path(source_dir) / "static").resolve()
  try:
    target = (root / (asset_path or "index.html")).resolve()
  except OSError:
    raise HTTPException(status_code=404, detail="Not found.")
  if target == root or target.is_dir():
    target = (target / "index.html").resolve()
  if root not in target.parents or not target.is_file():
    raise HTTPException(status_code=404, detail="Not found.")

  try:
    stat = target.stat()
  except OSError:
    raise HTTPException(status_code=404, detail="Not found.")

  # Asset files under a slug change only on app re-install, so
  # hashed-named files are cacheable forever (the new name busts the
  # cache) and everything else revalidates — but a revalidation is now
  # a bodiless 304 instead of a full re-download (CubeRun re-shipped
  # ~19MB of models/textures on every open before this).
  hashed = bool(_HASHED_ASSET_NAME.search(target.name))
  etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
  headers = {
    "Cache-Control": (
      "public, max-age=31536000, immutable"
      if hashed
      else "no-cache, must-revalidate"
    ),
    "ETag": etag,
    "Last-Modified": formatdate(stat.st_mtime, usegmt=True),
    "X-Content-Type-Options": "nosniff",
  }
  if _client_copy_is_fresh(request, etag, stat.st_mtime):
    return Response(status_code=304, headers=headers)
  media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
  if not hashed:
    # Revalidating (non-hashed) assets get the full body unconditionally,
    # ignoring any Range header (RFC 9110 lets a server do that). Serving a
    # 206 slice of a `no-cache` + ETag asset poisoned Chromium's HTTP cache:
    # the stored slice revalidated 304 and was then served as a status-200
    # full response — 1 byte long. CubeRun's `Range: bytes=0-0` probe turned
    # the game's index.html into the single character '<' for every later
    # open (the 2026-06-12 black-screen outage). Strip Range so FileResponse
    # streams the full body off disk (no whole-file read into memory) and
    # answers HEAD header-only with the true Content-Length.
    strip_range(request)
  # Hashed (immutable) files keep Range/206 support for media seeking —
  # safe because Chromium never revalidates an immutable entry, so the
  # partial-slice-as-200 trap above can't fire for them.
  return FileResponse(str(target), media_type=media_type, headers=headers)


# HEAD is registered alongside GET because client-side asset probes ("are
# the files installed?") want existence + headers without the body; a 405
# pushes well-meaning probes into `Range: bytes=0-0` fallbacks, which is
# exactly the poisoning trigger described in _serve_app_static_asset.
@app.api_route(
  "/app-assets/by-id/{app_id}/{asset_path:path}",
  methods=["GET", "HEAD"],
  include_in_schema=False,
)
async def app_owned_asset_by_id(app_id: int, asset_path: str, request: Request):
  """Serve durable static assets owned by an installed app.

  Imported apps like CubeRun can keep a built static site under
  /data/apps/<slug>/static instead of copying it into the platform frontend.
  This route is public like standalone app shells; it serves only files below
  the installed app's source_dir/static.
  """
  return _serve_app_static_asset(
    await asyncio.to_thread(_app_source_dir_for_static_asset, app_id=app_id),
    asset_path,
    request,
  )


@app.api_route(
  "/app-embeds/by-id/{app_id}/{asset_path:path}",
  methods=["GET", "HEAD"],
  include_in_schema=False,
)
async def app_owned_opaque_embed_by_id(
  app_id: int, asset_path: str, request: Request,
):
  """Serve a packaged static document under a permanently opaque origin.

  This namespace is intentionally frameable, including by an external site,
  so every response carries CSP sandbox without allow-same-origin. Relative
  assets stay below the same alias. Ordinary /app-assets remains protected by
  SAMEORIGIN and is never the document-navigation surface.
  """
  return _serve_app_static_asset(
    await asyncio.to_thread(_app_source_dir_for_static_asset, app_id=app_id),
    asset_path,
    request,
  )


@app.api_route(
  "/app-assets/{slug}/{asset_path:path}",
  methods=["GET", "HEAD"],
  include_in_schema=False,
)
async def app_owned_asset(slug: str, asset_path: str, request: Request):
  """Serve durable static assets owned by an installed app slug."""
  if not slug or not all(ch.isalnum() or ch in "-_" for ch in slug):
    raise HTTPException(status_code=404, detail="Not found.")
  return _serve_app_static_asset(
    await asyncio.to_thread(_app_source_dir_for_static_asset, slug=slug),
    asset_path,
    request,
  )


# Register the frontend serving routes whenever any static tree exists as a
# floor: the baked build is the guaranteed one inside the image, so this is
# effectively always-on in production; a bare local checkout has neither and
# skips the SPA fallback. WHICH tree serves each request — and where each
# /assets file comes from — is resolved per request (_resolve_static_dir /
# _resolve_asset_file), never frozen here at module load.
if _baked_dir.is_dir() or _live_dir.is_dir():
  from app.theme import get_bg_color, theme_data

  # /assets is a request-time handler, NOT a StaticFiles mount: it serves the
  # live build, then the attic's retained old generations, then a plain 404 —
  # never the SPA HTML. A missing chunk MUST be a 404, not a mystery text/html
  # payload (the browser MIME-rejects a module served as HTML and it poisons
  # the cache-first service worker). The mount had to bind one directory at
  # module load; this resolves per request, so a post-boot dist and a mid-swap
  # old generation both serve without a restart.
  @app.api_route(
    "/assets/{asset_path:path}", methods=["GET", "HEAD"], include_in_schema=False
  )
  async def serve_asset(request: Request, asset_path: str):
    target = await asyncio.to_thread(_resolve_asset_file, asset_path)
    if target is None:
      raise HTTPException(status_code=404, detail="Not found.")
    media_type = (
      mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    )
    # Vite content-hashes every /assets filename, so the URL itself is the
    # validator — a changed asset ships a new name. That makes the bytes
    # safely immutable: cache hard, skip the revalidation round-trip.
    return FileResponse(
      str(target),
      media_type=media_type,
      headers={
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=31536000, immutable",
        "X-Content-Type-Options": "nosniff",
      },
    )

  @app.get("/{path:path}")
  async def spa_fallback(request: Request, path: str):
    """Serves the SPA index.html for any non-API, non-asset path."""
    # Resolve which build serves THIS request (live dist if complete, else the
    # baked floor) once, up front — per request, never a module-load snapshot.
    static_dir = _resolve_static_dir()
    app_slug = await asyncio.to_thread(_top_level_app_slug_alias, path)
    if app_slug:
      from fastapi.responses import RedirectResponse
      return RedirectResponse(
        url=f"/apps/{app_slug}/",
        status_code=307,
        headers={"Cache-Control": "no-store"},
      )

    # Dynamically update manifest background to match theme.
    if path == "manifest.webmanifest":
      import json
      from fastapi.responses import JSONResponse
      try:
        manifest = json.loads(
          (static_dir / "manifest.webmanifest").read_text()
        )
      except OSError:
        # The dist swap has a microsecond two-rename window where the resolved
        # static dir can vanish; mirror the index.html guard below — a 503
        # asks the client to retry rather than 500ing a manifest fetch that
        # raced the publish.
        return Response(status_code=503, headers={"Retry-After": "1"})
      bg = get_bg_color(settings.data_dir)
      manifest["background_color"] = bg
      manifest["theme_color"] = bg
      return JSONResponse(
        manifest,
        media_type="application/manifest+json",
        # Revalidate on every fetch so an installed PWA picks up a new
        # theme_color after the owner changes the theme. On standalone
        # Android the OS derives the system/gesture-nav bar tint from the
        # manifest theme_color, so a browser-heuristic-cached manifest pins
        # the bar to the OLD --bg even though the page's own meta theme-color
        # (pre-paint + applyTheme) already followed the change — that lag was
        # the residual "gesture bar lighter than the app" report in card 164.
        # The manifest is NOT in the SW precache (vite.config.js globIgnores),
        # so the HTTP cache was the only stale layer left; no-cache keeps the
        # body cheap (304 when unchanged). Matches the per-app standalone
        # manifest (routes/standalone.py) and index.html/sw.js. This is the
        # delivery-path piece the reverted pre-paint-only #9 (2d882be) never
        # addressed; the meta theme-color sync it tried is already covered.
        headers={"Cache-Control": "no-cache, must-revalidate"},
      )

    file = static_dir / path
    if file.is_file() and path != "index.html":
      # The service worker MUST be served with `Cache-Control:
      # no-cache` so the browser revalidates it on every page load.
      # Without this header the browser caches sw.js by HTTP
      # heuristic (10% of last-modified age), which for a daily-
      # updated SW can be hours — old SW keeps serving the old
      # precached bundle even after deploys. Users reported the
      # PWA "not updating despite multiple refreshes" because of
      # this. `no-cache` (not `no-store`) still lets the browser
      # cache the response body but forces revalidation via
      # If-None-Match on every request, so a 304 keeps the
      # download cheap when nothing changed.
      headers = _public_static_headers(path)
      if path == "sw.js":
        headers["Cache-Control"] = "no-cache, must-revalidate"
      if path == "sw.js":
        # sw.js is a REVALIDATING response (no-cache + the mtime ETag
        # FileResponse sets), so it must never answer a 206. A
        # `Range: bytes=0-0` probe would otherwise let Chromium store the
        # 1-byte slice and later serve it as a status-200 full body — a
        # one-byte service worker. Stripping Range keeps the full-body 200
        # (same class as the /app-assets + /module fix; see http_caching).
        strip_range(request)
      return FileResponse(str(file), headers=headers or None)
    # When the live build is being served, a file that lives ONLY in the baked
    # build (/app/static) would otherwise fall through to the HTML response.
    # /vendor/pdfjs/* is the canonical example: the npm-install asset copy
    # lands in /app/static at image build time, but Vite doesn't emit it
    # into /data/platform/frontend/dist. Falling back to the baked dir for
    # files-not-in-live keeps app-authored asset URLs working without forcing the
    # rebuild to mirror the entire vendor tree.
    if static_dir != _baked_dir and path != "index.html":
      baked = _baked_dir / path
      if baked.is_file():
        return FileResponse(
          str(baked), headers=_public_static_headers(path) or None
        )
    # Static asset namespaces 404 on a miss — they must never receive the
    # SPA HTML below (a module URL served as text/html is MIME-rejected by
    # the browser and poisons the cache-first service worker). Only app
    # routes get the HTML fallback.
    if _is_static_asset_path(path):
      raise HTTPException(status_code=404, detail="Not found.")
    # Theme-as-data: serialize the effective theme into the page's
    # `__mobius-theme__` JSON slot so the client's pre-paint script can
    # paint it flash-free (src/lib/applyTheme.js). The server no longer
    # injects a <style> block — it hands the client DATA, not pre-rendered
    # HTML, so there is exactly one theme <style> (the client's).
    #
    # Slot-injection security: the payload is owner-controlled CSS embedded
    # inside `<script type="application/json">`. The HTML parser ends that
    # script element at the first literal `</`, so an embedded `</script>`
    # (or `</`-anything) in the theme CSS would break out of the slot.
    # Escaping `</` -> `<\/` defuses that (JSON treats `\/` as `/`, so the
    # parsed value is identical). U+2028/U+2029 are valid in JSON strings
    # but are JS line terminators inside a <script>, so they must be
    # `\u`-escaped too. This is the mandatory slot-XSS defense.
    import json
    from fastapi.responses import HTMLResponse
    try:
      html = (static_dir / "index.html").read_text(encoding="utf-8")
    except FileNotFoundError:
      # The served dist is absent only during the frontend watcher's dist swap
      # (a two-rename window of a few microseconds — see
      # frontend_watcher._replace_dist). Report transient-unavailable so the
      # client retries into the settled build rather than seeing a 500.
      raise HTTPException(
        status_code=503, detail="Frontend rebuilding, retry.",
        headers={"Retry-After": "1"},
      )
    payload = (
      json.dumps(theme_data(settings.data_dir))
      .replace("</", "<\\/")
      .replace("\u2028", "\\u2028")
      .replace("\u2029", "\\u2029")
    )
    html = html.replace(
      '<script type="application/json" id="__mobius-theme__"></script>',
      f'<script type="application/json" id="__mobius-theme__">{payload}</script>',
    )
    # index.html MUST be served with `Cache-Control: no-cache` so the
    # browser revalidates on every page load. Without it, the browser
    # heuristically caches HTML for hours and the user's PWA keeps
    # loading the OLD <script src="/assets/index-{old-hash}.js">
    # references — they reload, see old code, blame the deploy. The
    # asset bundles themselves are content-hashed and immutable, so
    # the cost of revalidating index.html is one round-trip; with the
    # ETag the body usually comes back as 304. Paired with the
    # equivalent header on /sw.js (above) so neither side of the
    # shell-entry can pin the user to a stale build.
    return HTMLResponse(
      html,
      headers={"Cache-Control": "no-cache, must-revalidate"},
    )
