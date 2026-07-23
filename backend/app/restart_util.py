"""Reliable, drain-gated in-process worker restart for the owner-facing paths.

Shared by ``/api/admin/restart`` (the Settings "Restart" button) and
``/api/platform/restart`` (the platform-update "Restart to finish" button) so
the two can never drift apart — a restart that works in one place but hangs in
the other is exactly the bug this consolidates away.

Every restart routes through one DRAIN-GATED path (design §2.2): live turns are
never simply killed. The worker first sets the ``draining`` gate (new sends
queue), interrupts each live turn so it finalizes its partials + a "paused for a
platform update" note WITHOUT touching the pending queue, then restarts — SIGTERM
for a graceful exit, with a SIGKILL backstop so a hung shutdown still cycles the
container. Boot reconcile finalizes any marker left set and offers a one-tap
Resume.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading

log = logging.getLogger("mobius.restart")

# Grace after SIGTERM before the hard kill — the crash floor. uvicorn's graceful
# shutdown blocks on the never-closing chat SSE stream, so without a hard-kill
# fallback a plain SIGTERM hangs the worker in shutdown limbo: it stops serving
# but never exits, so tini (PID 1) never exits and the container never restarts.
_FORCE_KILL_AFTER_SECONDS = 5.0


async def restart_this_worker() -> None:
  """Drain live turns, then restart this uvicorn worker with the current code.

  Runs as an async BackgroundTask (after the response is flushed), so the drain
  executes on the event loop where the runner handles + writer acks live. The
  sequence:

    1. Set the ``draining`` gate so sends arriving during the restart queue
       rather than start, and both liveness sweeps stand down.
    2. Arm an ABSOLUTE SIGKILL backstop at ``DRAIN_TIMEOUT + grace`` — the
       worker dies no matter what, so a wedged drain or a hung graceful shutdown
       can never leave the container "Up" with a dead worker.
    3. Drain every live turn (interrupt → finalize partials + a "paused for a
       platform update" note → LEAVE the run marker + pending queue intact for
       boot reconcile + one-tap Resume). Bounded by ``DRAIN_TIMEOUT``;
       best-effort — a turn that won't drain in time keeps today's contract (the
       backstop kills the worker, boot reconcile finalizes the marker).
    4. SIGTERM uvicorn for a graceful exit. If it drains and exits within the
       remaining window the backstop never fires (SIGKILL never runs); if it
       hangs on the open SSE stream, the backstop force-exits and the container
       cycles, and a fresh worker boots.

  Data is safe: the chat writer commits before any response returns, and the
  drain flushes each paused note before SIGTERM, so a hard kill loses nothing a
  graceful drain would have saved.
  """
  from app import chat

  # Gate new sends ASAP so the whole restart window queues rather than starts.
  chat.begin_drain()

  pid = os.getpid()

  def _force_exit() -> None:
    os.kill(pid, signal.SIGKILL)

  timer = threading.Timer(
    chat.DRAIN_TIMEOUT + _FORCE_KILL_AFTER_SECONDS, _force_exit
  )
  timer.daemon = True
  timer.start()

  try:
    await asyncio.wait_for(
      chat.drain_all_for_restart(timeout=chat.DRAIN_TIMEOUT),
      timeout=chat.DRAIN_TIMEOUT,
    )
  except Exception:
    # Never let a drain failure block the restart — the backstop timer and the
    # SIGTERM below still reboot the worker, and boot reconcile finalizes any
    # turn whose marker was left set.
    log.warning("drain-for-restart failed; restarting anyway", exc_info=True)

  os.kill(pid, signal.SIGTERM)
