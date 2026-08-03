"""Lifecycle owner for long-running process supervisors.

Startup reconciliation is owned by ``startup.py``. This module owns only work
that remains alive after readiness: watcher processes, periodic chat recovery,
durable continuation wakeups, writer health, background compression, and
browser-profile quota enforcement. ``RuntimeSupervisors.start`` and ``stop``
are the complete lifecycle boundary so lifespan never needs individual task
handles or cancellation ordering knowledge.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Protocol

from app.database import SessionLocal


RESTART_BACKLOG_DRAIN_INTERVAL_SECS = 2.0


class RuntimeSettings(Protocol):
  data_dir: str


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

  def _spawn(self, name: str, coroutine) -> None:
    self._tasks[name] = asyncio.create_task(
      coroutine, name=f"mobius:{name}",
    )

  async def start(self) -> None:
    """Start every supervisor; individual wiring failures fail open."""
    await self._start_frontend_watcher()
    try:
      await self._start_chat_supervisors()
    except Exception as exc:
      self.log.error(
        "chat supervisor wiring failed: %s", exc, exc_info=True,
      )

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
                  if event and event.get("type") == "chat_run_finished":
                    break
            except asyncio.TimeoutError:
              pass
          last_sweep = await sweep_reset_parks_once()
      finally:
        system_broadcast.unsubscribe(events)

    async def compress_legacy_tool_outputs():
      from app.tool_output_storage import compress_legacy_tool_output_batch
      total_rows = raw_chars = stored_chars = 0
      after_chat_id = after_tool_use_id = None
      try:
        while True:
          report = await asyncio.to_thread(
            compress_legacy_tool_output_batch,
            SessionLocal,
            batch_size=16,
            after_chat_id=after_chat_id,
            after_tool_use_id=after_tool_use_id,
          )
          if not int(report["scanned"]):
            break
          total_rows += int(report["compressed"])
          raw_chars += int(report["raw_chars"])
          stored_chars += int(report["stored_chars"])
          after_chat_id = report["last_chat_id"]
          after_tool_use_id = report["last_tool_use_id"]
          await asyncio.sleep(0.01)
      except asyncio.CancelledError:
        raise
      except Exception as exc:
        self.log.error(
          "legacy tool-output compression failed: %s", exc, exc_info=True,
        )
        return
      if total_rows:
        self.log.info(
          "compressed %d legacy tool output(s): %d -> %d stored characters",
          total_rows, raw_chars, stored_chars,
        )

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
      # Scratch cleanup is retention work, not part of starting a chat. Keeping
      # it here avoids making every concurrent turn rescan the same directory.
      await asyncio.sleep(300)
      while True:
        try:
          from app.agent_scratch import sweep_idle_scratch

          def sweep_once():
            with SessionLocal() as db:
              return sweep_idle_scratch(db)

          result = await asyncio.to_thread(sweep_once)
          if result["bytes"]:
            self.log.info(
              "agent scratch retention reclaimed %d bytes",
              result["bytes"],
            )
        except asyncio.CancelledError:
          raise
        except Exception as exc:
          self.log.error(
            "agent scratch retention failed: %s", exc, exc_info=True,
          )
        await asyncio.sleep(60 * 60)

    self._spawn("wedged-marker-sweep", wedged_marker_loop())
    self._spawn("reset-park-sweep", reset_park_loop())
    self._spawn("writer-supervisor", writer_supervisor_loop())
    self._spawn("browser-profile-quota", browser_profile_loop())
    self._spawn("agent-scratch-retention", agent_scratch_loop())
    self._spawn("legacy-tool-output-compression", compress_legacy_tool_outputs())

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
