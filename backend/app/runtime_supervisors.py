"""Lifecycle owner for long-running process supervisors.

Startup reconciliation is owned by ``startup.py``. This module owns only work
that remains alive after readiness: watcher processes, periodic chat recovery,
durable continuation wakeups, writer health, background compression, and
browser-profile quota enforcement. Process-only and database-backed services
have distinct start boundaries so a database-degraded boot can keep source
diagnostics alive without starting database work. ``stop`` remains the single
shutdown boundary.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

from app.database import SessionLocal


RESTART_BACKLOG_DRAIN_INTERVAL_SECS = 2.0
CHAT_WAIT_SWEEP_INTERVAL_SECS = 30.0
DELEGATION_STARTUP_RECOVERY_INTERVAL_SECS = 60.0
DELEGATION_WAKE_RECOVERY_INTERVAL_SECS = 60.0
AUTOPILOT_LEASE_RECOVERY_INTERVAL_SECS = 60.0
# Capacity monitor: sample /data headroom every tick; recompute the (more
# expensive) per-domain attribution walk only every Nth tick unless pressure is
# present, so the monitor never amplifies the disk pressure it watches.
CAPACITY_MONITOR_INTERVAL_SECS = 300.0
CAPACITY_MONITOR_DOMAIN_EVERY_N_TICKS = 6
PROVIDER_SESSION_RETENTION_INTERVAL_SECS = 6 * 60 * 60
# A Codex-only sweep skipped by an active Codex, or stopped by its store budget,
# retries this soon; it blocks nothing but Codex launches, and only briefly.
PROVIDER_SESSION_RETENTION_BACKLOG_INTERVAL_SECS = 5 * 60
# OOM watchdog: the counter read is a single tiny file, but the per-tick process
# sample walks the cgroup, so we sample fast only through the boot window (when a
# resume burst can OOM) and back off afterwards. The kernel's oom_kill counter is
# monotonic per container life, so a rise is unambiguous proof of a kill.
OOM_WATCHDOG_FAST_INTERVAL_SECS = 2.0
OOM_WATCHDOG_SLOW_INTERVAL_SECS = 20.0
OOM_WATCHDOG_FAST_WINDOW_SECS = 180.0
REQUIRED_DATABASE_SUPERVISORS = frozenset({
  "wedged-marker-sweep",
  "reset-park-sweep",
  "chat-wait-sweep",
  "writer-supervisor",
})


class RuntimeSettings(Protocol):
  data_dir: str


def provider_retention_delay_after(result: dict) -> float:
  """Seconds until the next Codex retention sweep.

  The sweep waits only for Codex (its lock plus open-file evidence), never for
  unrelated agents, so a skipped or budget-limited pass retries soon instead
  of leaving a busy installation's backlog for another six hours.
  """
  if result.get("status") == "skipped_active":
    return PROVIDER_SESSION_RETENTION_BACKLOG_INTERVAL_SECS
  if not (result.get("stores") or {}).get("complete", True):
    return PROVIDER_SESSION_RETENTION_BACKLOG_INTERVAL_SECS
  return PROVIDER_SESSION_RETENTION_INTERVAL_SECS


class RuntimeSupervisors:
  """Start and stop the fixed set of post-readiness background owners."""

  def __init__(
    self,
    *,
    settings: RuntimeSettings,
    logger: logging.Logger,
    restart_authorization: str | None,
    restart_fallback_chats: list[str],
  ) -> None:
    self.settings = settings
    self.log = logger
    self.restart_authorization = restart_authorization
    self.restart_fallback_chats = restart_fallback_chats
    self._tasks: dict[str, asyncio.Task] = {}
    self._frontend_observer = None
    self._frontend_handler = None
    self._database_services_started = False
    self._database_services_error: str | None = None

  def _spawn(self, name: str, coroutine) -> None:
    self._tasks[name] = asyncio.create_task(
      coroutine, name=f"mobius:{name}",
    )

  def reclaim_boot_file_cache(self) -> None:
    """Release file pages boot left cached, once the server is ready.

    Boot reconciles the platform checkout, may finish an update swap, and
    bootstraps apps before any turn exists, so no settled-turn cleanup follows
    that git and tool I/O. Startup maintenance also reads much of the main
    database file, so that one file is advised too. One pass at readiness,
    off the event loop.
    """
    async def reclaim():
      from app.file_cache import reclaim_background_work_cache
      try:
        await asyncio.to_thread(
          reclaim_background_work_cache, self.settings.data_dir,
        )
      except Exception:
        self.log.debug("boot file cache advice failed", exc_info=True)
      from app.database import reclaim_startup_database_file_cache
      try:
        result = await asyncio.to_thread(reclaim_startup_database_file_cache)
      except Exception:
        self.log.debug("startup database file cache advice failed", exc_info=True)
        return
      if result and result["files"]:
        # Advised, not reclaimed: concurrent activity moves the cgroup figures.
        self.log.info(
          "startup database file cache advice: advised_bytes=%s "
          "cgroup_file_before=%s cgroup_file_after=%s",
          result["advised_file_bytes"],
          result["file_cache_before_bytes"],
          result["file_cache_after_bytes"],
        )

    self._spawn("boot-file-cache-reclaim", reclaim())

  async def start_process_services(self) -> None:
    """Start services that are safe without a serviceable database."""
    await self._start_frontend_watcher()
    from app.connect_outbound import supervise_outbound_connects
    self._spawn("connect-outbound", supervise_outbound_connects())
    self._spawn("oom-watchdog", self._oom_watchdog_loop())

  async def _oom_watchdog_loop(self) -> None:
    """Record a durable diagnostic whenever the cgroup loses a process to OOM.

    Started with the process services — before chat supervisors trigger the
    restart resume burst — so a boot-time OOM is captured rather than lost. The
    previous tick's process sample is retained so a detected kill can name the
    PIDs that vanished across it (the likely victims), which kernel logs no
    longer reveal after the fact.
    """
    from app import oom_diagnostics

    data_dir = self.settings.data_dir
    boot_at = asyncio.get_running_loop().time()
    # Establish the baseline WITHOUT recording: a nonzero count here means a kill
    # happened earlier in this container's life (or before the watchdog started),
    # which we cannot attribute, so we only mark it and watch for the next rise.
    baseline = oom_diagnostics.cgroup_oom_kill_count()
    if baseline:
      self.log.warning(
        "oom watchdog started with oom_kill=%d already recorded this container",
        baseline,
      )
    prev_sample = None
    while True:
      try:
        sample = await asyncio.to_thread(
          oom_diagnostics.lightweight_process_sample,
        )
        count = oom_diagnostics.cgroup_oom_kill_count()
        if count is not None and baseline is not None and count > baseline:
          kills = count - baseline
          event = await asyncio.to_thread(
            oom_diagnostics.capture_oom_event,
            oom_kill_count=count,
            kills_since_last=kills,
            seconds_since_boot=asyncio.get_running_loop().time() - boot_at,
            pre_sample=prev_sample,
            post_sample=sample,
          )
          await asyncio.to_thread(
            oom_diagnostics.record_oom_event, data_dir, event,
          )
          victims = event.get("likely_victims") or []
          self.log.warning(
            "OOM kill recorded: oom_kill=%d (+%d) active_turns=%s "
            "likely_victims=%s",
            count, kills, event.get("active_turns"),
            [v.get("name") for v in victims],
          )
        if count is not None:
          baseline = count if baseline is None else max(baseline, count)
        prev_sample = sample
      except asyncio.CancelledError:
        raise
      except Exception as exc:
        self.log.error("oom watchdog tick failed: %s", exc, exc_info=True)
      elapsed = asyncio.get_running_loop().time() - boot_at
      interval = (
        OOM_WATCHDOG_FAST_INTERVAL_SECS
        if elapsed < OOM_WATCHDOG_FAST_WINDOW_SECS
        else OOM_WATCHDOG_SLOW_INTERVAL_SECS
      )
      await asyncio.sleep(interval)

  async def start_database_services(self) -> None:
    """Start required database owners and retain an explicit readiness verdict."""
    before = set(self._tasks)
    try:
      await self._start_chat_supervisors()
      await asyncio.sleep(0)
      started = set(self._tasks) - before
      missing = REQUIRED_DATABASE_SUPERVISORS - started
      if missing:
        raise RuntimeError(
          "required runtime supervisors were not started: "
          + ", ".join(sorted(missing))
        )
      self._database_services_started = True
      self._database_services_error = None
    except Exception as exc:
      self._database_services_error = str(exc)
      self.log.error(
        "chat supervisor wiring failed: %s", exc, exc_info=True,
      )

  def database_service_readiness(self) -> tuple[bool, str]:
    """Report whether every required database supervisor still has an owner."""
    if self._database_services_error:
      return False, "runtime_supervisor_start_failed"
    if not self._database_services_started:
      return False, "runtime_supervisors_not_started"
    stopped = sorted(
      name for name in REQUIRED_DATABASE_SUPERVISORS
      if name not in self._tasks or self._tasks[name].done()
    )
    if stopped:
      return False, "runtime_supervisor_stopped"
    return True, ""

  async def _start_frontend_watcher(self) -> None:
    try:
      if Path("/data/platform/frontend/src").is_dir():
        from app.frontend_watcher import start_supervised_watcher
        self._frontend_observer, self._frontend_handler = (
          await start_supervised_watcher(asyncio.get_running_loop())
        )
    except Exception as exc:
      self.log.error("start_frontend_watcher failed: %s", exc, exc_info=True)

  async def _start_chat_supervisors(self) -> None:
    from app.agent_scratch import sweep_idle_scratch
    from app.broadcast import get_system_broadcast
    from app.chat import (
      ContinuationSweepResult,
      sweep_idle_pending_chats,
      sweep_reset_parks,
      sweep_wedged_runs,
    )

    async def wedged_marker_loop():
      while True:
        await asyncio.sleep(60)
        try:
          from app.saved_secure_inputs import recover_interrupted
          await recover_interrupted()
          with SessionLocal() as db:
            await sweep_wedged_runs(db)
            await sweep_idle_pending_chats(db)
        except asyncio.CancelledError:
          raise
        except Exception as exc:
          self.log.error("wedged-marker sweep failed: %s", exc, exc_info=True)

    async def sweep_reset_parks_once():
      try:
        with SessionLocal() as db:
          return await sweep_reset_parks(
            db, restart_authorization=self.restart_authorization,
          )
      except asyncio.CancelledError:
        raise
      except Exception as exc:
        self.log.error("reset-park sweep failed: %s", exc, exc_info=True)
        return ContinuationSweepResult()

    startup_sweep = await sweep_reset_parks_once()
    if self.restart_authorization:
      self.log.info(
        "startup restart continuation pass authorized=%d fallback_recovered=%d "
        "started_or_resolved=%d",
        1,
        len(self.restart_fallback_chats),
        len(startup_sweep.resolved),
      )

    async def reset_park_loop():
      system_broadcast = get_system_broadcast()
      events = system_broadcast.subscribe()
      last_sweep = startup_sweep
      try:
        while True:
          fast_followup = bool(
            last_sweep.restart_deferred and last_sweep.resolved
          )
          if fast_followup:
            await asyncio.sleep(RESTART_BACKLOG_DRAIN_INTERVAL_SECS)
          else:
            try:
              async with asyncio.timeout(60):
                while True:
                  event = await events.get()
                  if event and event.get("type") in {
                    "chat_run_finished", "platform_boot_ready",
                  }:
                    break
            except asyncio.TimeoutError:
              pass
          last_sweep = await sweep_reset_parks_once()
      finally:
        system_broadcast.unsubscribe(events)

    async def writer_supervisor_loop():
      from app.chat_writer import supervise_writer
      while True:
        await asyncio.sleep(60)
        try:
          supervise_writer()
        except asyncio.CancelledError:
          raise
        except Exception as exc:
          self.log.error(
            "writer supervisor tick failed: %s", exc, exc_info=True,
          )

    async def chat_wait_loop():
      # Durable declared waits: run due checks and resume their chats. The
      # rows are the promise; this loop restarting with the server is what
      # makes a declared wait restart-immune.
      from app.chat_waits import sweep_due_waits
      system_broadcast = get_system_broadcast()
      events = system_broadcast.subscribe()
      try:
        while True:
          force_kind = None
          try:
            async with asyncio.timeout(CHAT_WAIT_SWEEP_INTERVAL_SECS):
              while True:
                event = await events.get()
                if event and event.get("type") == "platform_boot_ready":
                  force_kind = "platform_activation"
                  break
          except asyncio.TimeoutError:
            pass
          try:
            await sweep_due_waits(force_kind=force_kind)
          except asyncio.CancelledError:
            raise
          except Exception as exc:
            self.log.error("chat-wait sweep failed: %s", exc, exc_info=True)
      finally:
        system_broadcast.unsubscribe(events)

    async def delegation_startup_recovery_loop():
      # Startup admission and source-attached work have their own repair path;
      # neither can be delayed by a stalled parent wake or SQLite lease sweep.
      from app.delegations import (
        reconcile_unstarted_delegations,
      )
      from app.routes.github import reconcile_attached_contribution_work

      while True:
        await asyncio.sleep(DELEGATION_STARTUP_RECOVERY_INTERVAL_SECS)
        try:
          await reconcile_unstarted_delegations()
          await reconcile_attached_contribution_work()
        except asyncio.CancelledError:
          raise
        except Exception as exc:
          self.log.error(
            "delegation startup recovery failed: %s", exc, exc_info=True,
          )

    async def delegation_wake_recovery_loop():
      # Live completion hooks own the prompt path. This bounded cursor pass
      # repairs missed hooks without letting one parent monopolize recovery.
      from app.delegations import wake_parents_for_completed_delegations
      cursor = None
      while True:
        await asyncio.sleep(DELEGATION_WAKE_RECOVERY_INTERVAL_SECS)
        try:
          result = await wake_parents_for_completed_delegations(after=cursor)
          cursor = result.next_cursor
        except asyncio.CancelledError:
          raise
        except Exception as exc:
          self.log.error(
            "delegation wake recovery failed: %s", exc, exc_info=True,
          )

    async def autopilot_lease_recovery_loop():
      from app.contribution_autopilot import sweep_expired_leases
      from app.contribution_autopilot_recovery import recover_resolved_blocks

      def sweep_once() -> int:
        # Session creation belongs in the worker with every operation that uses
        # it; no synchronous SQLite wait may stall the server event loop.
        with SessionLocal() as db:
          return sweep_expired_leases(db)

      while True:
        await asyncio.sleep(AUTOPILOT_LEASE_RECOVERY_INTERVAL_SECS)
        try:
          await asyncio.to_thread(sweep_once)
          await recover_resolved_blocks()
        except asyncio.CancelledError:
          raise
        except Exception as exc:
          self.log.error(
            "autopilot lease sweep failed: %s", exc, exc_info=True,
          )

    async def browser_profile_loop():
      await asyncio.sleep(300)
      while True:
        sweep_seconds = 60 * 60
        try:
          from app.browser_profiles import (
            browser_profile_sweep_seconds,
            chat_activity_snapshot,
            enforce_browser_profile_quota,
          )
          from app.runner_registry import registry
          sweep_seconds = browser_profile_sweep_seconds()
          with SessionLocal() as db:
            chat_snapshot = chat_activity_snapshot(db)
          result = await asyncio.to_thread(
            enforce_browser_profile_quota,
            self.settings.data_dir,
            chat_snapshot,
            registry.all_alive_chat_ids(),
          )
          if result["reclaimed_bytes"]:
            self.log.info(
              "agent-browser profile quota reclaimed %d bytes",
              result["reclaimed_bytes"],
            )
        except asyncio.CancelledError:
          raise
        except Exception as exc:
          self.log.error(
            "agent-browser profile quota failed: %s", exc, exc_info=True,
          )
        await asyncio.sleep(sweep_seconds)

    async def agent_scratch_loop():
      # Scratch persists across a chat's turns; this hourly sweep is its only
      # retention, first run five minutes after startup.
      await asyncio.sleep(300)
      while True:
        try:
          result = await sweep_idle_scratch()
          if result["bytes"]:
            self.log.info(
              "agent scratch retention reclaimed %d bytes", result["bytes"],
            )
        except asyncio.CancelledError:
          raise
        except Exception as exc:
          self.log.error("agent scratch retention failed: %s", exc, exc_info=True)
        await asyncio.sleep(60 * 60)

    async def capacity_monitor_loop():
      # Outcome #7: see a full /data COMING, not just when admission refuses
      # turns. Each tick samples headroom, records the bounded history ring,
      # forecasts time-to-exhaustion, and pushes ONE owner alert per tier
      # escalation (storm-suppressed). The blocking snapshot/tick runs off the
      # event loop; only the async push runs here.
      from app import models, push
      from app.capacity_monitor import (
        record_alert_delivery,
        run_capacity_tick,
      )
      from app.config import get_settings

      data_dir = get_settings().data_dir
      tick = 0
      while True:
        try:
          include_domains = tick % CAPACITY_MONITOR_DOMAIN_EVERY_N_TICKS == 0
          result = await asyncio.to_thread(
            run_capacity_tick, data_dir,
            notify=None, include_domains=include_domains,
          )
          if result["alerted"] and result["notification_id"]:
            notification_id = result["notification_id"]
            with SessionLocal() as notification_db:
              owner_row = notification_db.query(models.Owner.id).first()
              if owner_row is not None:
                await push.notify_owner_async(
                  notification_db,
                  owner_row[0],
                  title="Storage capacity alert",
                  body=result["body"],
                  source_type="capacity",
                  notification_id=notification_id,
                )
            # notify_owner_async deliberately fails open. Suppress this tier
            # only after its durable bell row exists; otherwise retry next tick
            # with the same idempotent notification id.
            with SessionLocal() as verification_db:
              delivered = verification_db.query(models.Notification.id).filter(
                models.Notification.id == notification_id,
              ).first() is not None
            if delivered:
              await asyncio.to_thread(
                record_alert_delivery, data_dir, result["tier"],
              )
              self.log.warning(
                "capacity alert tier=%s free=%s", result["tier"],
                (result["snapshot"] or {}).get("data_free_bytes"),
              )
          # This tick observes the disk; a transcript conversion that paused
          # for disk resumes when this observation says pressure recovered.
          from app.chat_writer import rearm_transcript_conversion
          from app.resource_pressure import resource_status
          disk_state = resource_status(data_dir)["pressure"]["disk"].get("state")
          if rearm_transcript_conversion(str(disk_state)):
            self.log.info("transcript conversion resumed after disk pressure recovered")
        except asyncio.CancelledError:
          raise
        except Exception as exc:
          self.log.error("capacity monitor failed: %s", exc, exc_info=True)
        tick += 1
        await asyncio.sleep(CAPACITY_MONITOR_INTERVAL_SECS)

    async def provider_session_retention_loop():
      # Codex rollout JSONL is resumable context, not permanent owner data.
      # Startup owns the immediate, pre-DB sweep. Periodic sweeps need only
      # Codex to be idle: the exclusive Codex lock excludes every Möbius
      # launcher (a rollout cannot race thread_resume), and open-file evidence
      # excludes a Codex started outside them. Other agents keep running.
      from app.provider_session_retention import sweep_stale_provider_sessions
      # The startup sweep is budget-limited; follow up on any backlog soon.
      delay = PROVIDER_SESSION_RETENTION_BACKLOG_INTERVAL_SECS
      while True:
        await asyncio.sleep(delay)
        delay = PROVIDER_SESSION_RETENTION_INTERVAL_SECS
        try:
          result = await asyncio.to_thread(
            sweep_stale_provider_sessions, self.settings.data_dir,
          )
          delay = provider_retention_delay_after(result)
          if result["status"] == "skipped_active":
            self.log.info(
              "provider session retention skipped while Codex is active",
            )
            continue
          if result["reclaimed_bytes"]:
            self.log.info(
              "provider session retention reclaimed %d bytes from %d files",
              result["reclaimed_bytes"], result["removed_files"],
            )
          if result.get("store_reclaimed_bytes"):
            self.log.info(
              "Codex store compaction reclaimed %d bytes (complete=%s)",
              result["store_reclaimed_bytes"],
              result.get("stores", {}).get("complete"),
            )
          if result["errors"]:
            self.log.warning(
              "provider session retention skipped %d file(s)", result["errors"],
            )
        except asyncio.CancelledError:
          raise
        except Exception as exc:
          self.log.error(
            "provider session retention failed: %s", exc, exc_info=True,
          )

    self._spawn("wedged-marker-sweep", wedged_marker_loop())
    self._spawn("reset-park-sweep", reset_park_loop())
    self._spawn("chat-wait-sweep", chat_wait_loop())
    self._spawn(
      "delegation-startup-recovery", delegation_startup_recovery_loop(),
    )
    self._spawn(
      "delegation-wake-recovery", delegation_wake_recovery_loop(),
    )
    self._spawn(
      "autopilot-lease-recovery", autopilot_lease_recovery_loop(),
    )
    self._spawn("writer-supervisor", writer_supervisor_loop())
    self._spawn("browser-profile-quota", browser_profile_loop())
    self._spawn("agent-scratch-retention", agent_scratch_loop())
    self._spawn("provider-session-retention", provider_session_retention_loop())
    self._spawn("capacity-monitor", capacity_monitor_loop())

  async def stop(self) -> None:
    """Cancel and observe every task, then stop external watcher resources."""
    tasks = list(self._tasks.values())
    for task in tasks:
      task.cancel()
    if tasks:
      results = await asyncio.gather(*tasks, return_exceptions=True)
      for task, result in zip(tasks, results):
        if isinstance(result, BaseException) and not isinstance(
          result, asyncio.CancelledError,
        ):
          self.log.error(
            "runtime supervisor %s stopped with error: %s",
            task.get_name(), result,
            exc_info=(type(result), result, result.__traceback__),
          )
    self._tasks.clear()

    if self._frontend_handler is not None:
      try:
        self._frontend_handler.close()
      except Exception as exc:
        self.log.error(
          "frontend watcher handler.close failed: %s", exc, exc_info=True,
        )
    if self._frontend_observer is not None:
      try:
        self._frontend_observer.stop()
      except Exception as exc:
        self.log.error(
          "frontend watcher observer.stop failed: %s", exc, exc_info=True,
        )
      try:
        self._frontend_observer.join(timeout=2)
      except Exception as exc:
        self.log.error(
          "frontend watcher observer.join failed: %s", exc, exc_info=True,
        )
