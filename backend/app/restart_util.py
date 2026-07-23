"""Reliable, drain-gated in-process worker restart for the owner-facing paths.

Shared by ``/api/admin/restart`` (the Settings "Restart" button) and
``/api/platform/restart`` (the platform-update "Restart to finish" button) so
the two can never drift apart — a restart that works in one place but hangs in
the other is exactly the bug this consolidates away.

Every restart routes through one DRAIN-GATED path (design §2.2): live turns are
never simply killed. The worker first sets the ``draining`` gate (new sends
queue), interrupts each live turn so it finalizes its partials + a "paused for a
platform update" note WITHOUT touching the pending queue, then asks the frozen
entrypoint supervisor to acknowledge the exact restart intent and cycle pid 1.
A SIGKILL backstop still guarantees recovery if the handshake or shutdown
wedges. Boot reconcile handles fallback markers and unacknowledged parks
manually.
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
       platform update" note → mark that exact run due now using the existing
       continuation row; preserve the pending queue). Bounded by
       ``DRAIN_TIMEOUT``; best-effort — a turn that won't drain, or whose exact
       transition cannot commit, leaves its generic marker for manual boot
       reconciliation.
    4. Publish the exact intent + restart sentinel. The frozen root-owned
       poller acknowledges it in the boot ledger, then SIGTERMs pid 1. If that
       path wedges, the backstop force-exits the worker without an
       acknowledgement, so the next boot recovers manually.

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

  from app import restart_ledger

  boot_id = restart_ledger.current_boot_id()
  restart_nonce = restart_ledger.new_nonce()
  parked_runs: list[dict[str, str]] = []
  try:
    parked_runs = await asyncio.wait_for(
      chat.drain_all_for_restart(
        timeout=chat.DRAIN_TIMEOUT,
        restart_nonce=restart_nonce,
      ),
      timeout=chat.DRAIN_TIMEOUT,
    )
  except Exception:
    # Never let a drain failure block the restart — the backstop timer and the
    # SIGTERM below still reboots the worker. Exact runs already transitioned
    # remain due; any marker left set falls back to manual boot reconciliation.
    log.warning("drain-for-restart failed; restarting anyway", exc_info=True)

  try:
    if not boot_id:
      raise RuntimeError("entrypoint boot id is unavailable")
    # Publishing the sentinel is the only normal shutdown request. The frozen
    # root-owned entrypoint poller validates the matching intent, records its
    # exact runs in the one-shot boot ledger, and then terminates pid 1.
    restart_ledger.request_restart(
      boot_id=boot_id,
      nonce=restart_nonce,
      runs=parked_runs,
    )
  except Exception:
    # Restart reliability and continuation authorization are independent.
    # If the external handshake cannot be published, restart directly; the
    # next boot has no root-owned acknowledgement and resolves every parked run
    # to manual recovery.
    log.warning(
      "planned-restart handshake failed; restarting without automatic "
      "continuation",
      exc_info=True,
    )
    os.kill(pid, signal.SIGTERM)
