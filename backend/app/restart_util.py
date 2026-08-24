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
from pathlib import Path

log = logging.getLogger("mobius.restart")

# Grace after SIGTERM before the hard kill — the crash floor. uvicorn's graceful
# shutdown blocks on the never-closing chat SSE stream, so without a hard-kill
# fallback a plain SIGTERM hangs the worker in shutdown limbo: it stops serving
# but never exits, so tini (PID 1) never exits and the container never restarts.
_FORCE_KILL_AFTER_SECONDS = 5.0
_CUTOVER_FAILSAFE_SECONDS = 90.0


async def _drain_exact_restart() -> tuple[str, str, list[dict[str, str]]]:
  """Gate admission and bind every live run to one fresh restart nonce."""
  from app import chat
  from app.broadcast import get_system_broadcast

  get_system_broadcast().publish({"type": "server_restarting"})
  chat.begin_drain()

  from app import restart_ledger

  boot_id = restart_ledger.current_boot_id()
  restart_nonce = restart_ledger.new_nonce()
  restart_runs: list[dict[str, str]] = []
  try:
    restart_runs = await asyncio.wait_for(
      chat.prepare_restart_intents(restart_nonce),
      timeout=min(10.0, chat.DRAIN_TIMEOUT),
    )
  except Exception:
    log.warning(
      "restart-intent preparation failed; fallbacks will remain manual",
      exc_info=True,
    )
  try:
    drained_runs = await asyncio.wait_for(
      chat.drain_all_for_restart(
        timeout=chat.DRAIN_TIMEOUT,
        restart_nonce=restart_nonce,
        prepared_runs=restart_runs,
      ),
      timeout=chat.DRAIN_TIMEOUT,
    )
    known = {
      (item["chat_id"], item["run_token"]) for item in restart_runs
    }
    restart_runs.extend(
      item for item in drained_runs
      if (item["chat_id"], item["run_token"]) not in known
    )
  except Exception:
    log.warning("drain-for-restart failed; restarting anyway", exc_info=True)
  return boot_id, restart_nonce, restart_runs


async def restart_this_worker(ready_path: Path | None = None) -> None:
  """Drain live turns, then restart this uvicorn worker with the current code.

  Runs as an async BackgroundTask (after the response is flushed), so the drain
  executes on the event loop where the runner handles + writer acks live. The
  sequence:

    1. Set the ``draining`` gate so sends arriving during the restart queue
       rather than start, and both liveness sweeps stand down.
    2. Arm an ABSOLUTE SIGKILL backstop at ``DRAIN_TIMEOUT + grace`` — the
       worker dies no matter what, so a wedged drain or a hung graceful shutdown
       can never leave the container "Up" with a dead worker.
    3. Before interruption, bind the one-shot restart nonce to every exact live
       run in a transcript-independent writer transaction. Then drain every
       turn (interrupt → finalize partials + a "paused for a platform update"
       note → mark clean stops due now; preserve the pending queue). Bounded by
       ``DRAIN_TIMEOUT``; a slow stop or failed terminal save retains its exact
       nonce-stamped running row, which authenticated startup converts to the
       same due continuation state.
    4. Publish the exact intent + restart sentinel. The frozen root-owned
       poller acknowledges it in the boot ledger, then SIGTERMs pid 1. If that
       path wedges, the backstop force-exits the worker without an
       acknowledgement, so the next boot recovers manually.

  Data is safe: the chat writer commits before any response returns, and the
  drain flushes each paused note before SIGTERM, so a hard kill loses nothing a
  graceful drain would have saved.
  """
  from app import chat

  pid = os.getpid()

  def _force_exit() -> None:
    os.kill(pid, signal.SIGKILL)

  timer = threading.Timer(
    chat.DRAIN_TIMEOUT + _FORCE_KILL_AFTER_SECONDS, _force_exit
  )
  timer.daemon = True
  timer.start()

  from app import restart_ledger

  boot_id, restart_nonce, restart_runs = await _drain_exact_restart()

  try:
    if not boot_id:
      raise RuntimeError("entrypoint boot id is unavailable")
    # Publishing the sentinel is the only normal shutdown request. The frozen
    # root-owned entrypoint poller validates the matching intent, records its
    # exact runs in the one-shot boot ledger, and then terminates pid 1.
    restart_ledger.request_restart(
      boot_id=boot_id,
      nonce=restart_nonce,
      runs=restart_runs,
    )
    if ready_path is not None:
      ready_path.write_text("ready\n", encoding="utf-8")
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


async def prepare_container_cutover(cutover_id: str) -> dict[str, object]:
  """Drain once for a Host-owned replacement without self-terminating.

  The root supervisor must first open the exact cutover challenge.  This
  process then parks and nonce-binds active turns, but publishes no shutdown
  sentinel: Docker/Compose owns the stop, so the accepted authorization binds
  to the replacement boot rather than an accidental intermediate restart.
  """
  from app import restart_ledger

  if not restart_ledger.authorized_cutover_challenge(cutover_id):
    raise RuntimeError("the Host did not authorize this cutover")

  boot_id, restart_nonce, restart_runs = await _drain_exact_restart()
  try:
    if not boot_id:
      raise RuntimeError("entrypoint boot id is unavailable")
    restart_ledger.publish_cutover_intent(
      boot_id=boot_id,
      nonce=restart_nonce,
      cutover_id=cutover_id,
      runs=restart_runs,
    )
  except Exception:
    # A failed external handoff must not strand a live-but-drained worker.
    # Convert it into the ordinary supervised restart using the same exact run
    # bindings.  If even that cannot publish, fail closed to manual recovery.
    log.warning(
      "container-cutover handoff failed; falling back to a normal restart",
      exc_info=True,
    )
    try:
      restart_ledger.request_restart(
        boot_id=boot_id,
        nonce=restart_nonce,
        runs=restart_runs,
      )
    except Exception:
      os.kill(os.getpid(), signal.SIGTERM)
    raise

  pid = os.getpid()

  def _recover_abandoned_cutover() -> None:
    try:
      restart_ledger.request_restart(
        boot_id=boot_id,
        nonce=restart_nonce,
        runs=restart_runs,
      )
    except Exception:
      log.warning(
        "abandoned container cutover could not self-recover",
        exc_info=True,
      )
      os.kill(pid, signal.SIGTERM)

  watchdog = threading.Timer(_CUTOVER_FAILSAFE_SECONDS, _recover_abandoned_cutover)
  watchdog.daemon = True
  watchdog.start()
  return {
    "status": "prepared",
    "cutover_id": cutover_id,
    "boot_id": boot_id,
    "run_count": len(restart_runs),
  }
