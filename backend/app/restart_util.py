"""Reliable, drain-gated in-process worker restart for the owner-facing paths.

Shared by ``/api/admin/restart`` (the Settings button),
``/api/platform/restart`` (the update button), and typed owner Restart cards,
so competing surfaces cannot create multiple nonces, drains, or supervisor
requests.

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
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import NamedTuple

from app.config import import_probe_env

log = logging.getLogger("mobius.restart")

# Grace after SIGTERM before the hard kill — the crash floor. uvicorn's graceful
# shutdown blocks on the never-closing chat SSE stream, so without a hard-kill
# fallback a plain SIGTERM hangs the worker in shutdown limbo: it stops serving
# but never exits, so tini (PID 1) never exits and the container never restarts.
_FORCE_KILL_AFTER_SECONDS = 5.0
_CUTOVER_FAILSAFE_SECONDS = 90.0
_RESTART_ADMISSION_LOCK = threading.Lock()
_RESTART_ADMITTED = False


class RestartSourceInvalid(RuntimeError):
  """The editable platform would not survive the next startup probe.

  ``detail`` carries a structured reason when one exists: ``{"reason":
  "image_packages_behind_source", "gap": PackageGap}``. The restart routes
  add it beside their stable ``platform_source_invalid`` code.
  """

  def __init__(self, message: str, detail: dict | None = None) -> None:
    super().__init__(message)
    self.detail = detail or {}


# The image's own checkout, whose boot transaction decides what may serve.
BAKED_BACKEND = Path(os.environ.get("MOBIUS_BAKED_BACKEND", "/app/platform-baked/backend"))


# A module-level infinite loop or blocking call in agent-edited source would
# otherwise wedge the gate; a timeout counts as a failed check.
STARTUP_CHECK_TIMEOUT_SECONDS = 60

_STARTUP_CHECK = (
  "import importlib.util, runpy\n"
  "if importlib.util.find_spec('app.startup_selftest') is None:\n"
  "    import app.main\n"
  "else:\n"
  "    runpy.run_module('app.startup_selftest', run_name='__main__')\n"
  "from app.routes import require_all_routers_loaded\n"
  "require_all_routers_loaded()\n"
)


def _probe_env() -> dict[str, str]:
  """The environment of a child that judges source the way the entrypoint's
  boot does: PYTHONPATH, every GIT_* variable and server-only credentials
  scrubbed, DATA_DIR / DATABASE_URL retained, bytecode writes disabled, and
  the withheld signing key replaced by an import-only placeholder."""
  env = {
    key: value for key, value in os.environ.items()
    if not key.startswith("GIT_") and key not in {
      "PYTHONPATH", "MOBIUS_SSO_CLIENT_SECRET", "MOBIUS_COMPUTE_INSTANCE_TOKEN",
      "MOBIUS_IDENTITY_BOOTSTRAP",
    }
  }
  env["PYTHONDONTWRITEBYTECODE"] = "1"
  import_probe_env(env)
  return env


def run_candidate_startup_check(
  backend: Path, *, timeout: int = STARTUP_CHECK_TIMEOUT_SECONDS,
) -> str | None:
  """Prove a backend tree would start; return None, or why it would not.

  A fresh interpreter, cwd ``backend``, runs the candidate's own
  ``python -m app.startup_selftest`` (which imports ``app.main`` and resolves
  local provider configuration offline), then the route registry's explicit
  ``require_all_routers_loaded`` verdict. A tree without that selftest module
  (an older candidate, or one that deleted it) gets only the import and
  router verdict; that silent downgrade is accepted. A present selftest that fails is a
  failure, never a fallback.

  Gates that run this check: platform update reconcile (``_import_probe``),
  restart admission (``validate_restart_source`` from Settings, the update
  button and Restart cards), and the prepared/overlay update
  (``validate_restart_source`` on the frozen checkout). The frozen root-owned
  entrypoint's ``_platform_import_probe`` is the remaining import-only gate:
  every boot runs import plus router verdict alone, because that file ships
  with the image and is not changed to delegate to the candidate.

  The child mirrors the entrypoint's uvicorn exec (``_probe_env``). It must
  stay offline: no authentication, provider process, network call or
  database write.
  """
  try:
    completed = subprocess.run(
      [sys.executable or "python3", "-c", _STARTUP_CHECK],
      cwd=backend,
      env=_probe_env(),
      capture_output=True,
      text=True,
      timeout=timeout,
      check=False,
    )
  except subprocess.TimeoutExpired:
    return f"the startup check did not finish within {timeout} seconds"
  except OSError as exc:
    return f"the startup check could not run: {exc!r}"
  if completed.returncode == 0:
    return None
  return (completed.stderr or completed.stdout or "").strip() or (
    f"the startup check exited with status {completed.returncode}"
  )


def validate_restart_source(platform_root: Path | None = None) -> None:
  """Run the candidate startup check before accepting a restart.

  The entrypoint deliberately falls back to the baked platform when an edited
  backend or router cannot import.  A planned restart must catch that condition
  while the healthy worker can still explain and repair it, rather than using
  the fallback as a delayed test result. See ``run_candidate_startup_check``
  for what the check covers. A missing editable backend is valid for a
  baked-only installation; first-boot seeding remains owned by the entrypoint.
  """
  platform_root = platform_root or Path(
    os.environ.get("MOBIUS_PLATFORM_DIR", "/data/platform")
  )
  backend = platform_root / "backend"
  if not (backend / "app").is_dir():
    return

  failure = run_candidate_startup_check(backend)
  if failure is None:
    _refuse_what_the_image_boot_refuses(platform_root)
    return
  if len(failure) > 1200:
    failure = failure[-1200:]
  raise RestartSourceInvalid(
    "Restart stopped: the current platform source failed its startup check. "
    "Ask Möbius to repair it before restarting. "
    f"Details: {failure}"
  )


_PACKAGE_CHECK_PROTOCOL = re.compile(r"^PACKAGE_CHECK_PROTOCOL = (\d+)$", re.MULTILINE)
# The image's own check answers in about a second; a status poll must not
# wait on a stuck one for the startup check's full minute.
PACKAGE_CHECK_TIMEOUT_SECONDS = 20


def image_boot_checks_packages() -> bool:
  """Whether this image's boot has ``check-packages`` and its relaxed rule
  (only a missing official-newer distribution refuses a boot), by the
  explicit ``platform_boot.PACKAGE_CHECK_PROTOCOL`` marker in its baked
  source: importing it would run the served interpreter's view instead."""
  try:
    source = (BAKED_BACKEND / "app" / "platform_boot.py").read_text(encoding="utf-8")
  except (OSError, UnicodeDecodeError):
    return False
  marker = _PACKAGE_CHECK_PROTOCOL.search(source)
  return bool(marker) and int(marker.group(1)) >= 1


def _baked_package_verdict(platform_root: Path) -> tuple[bool, dict | None] | None:
  """The image's own ``platform_boot check-packages`` verdict, or None when
  this image predates that command (or it could not answer)."""
  if not image_boot_checks_packages():
    return None
  try:
    completed = subprocess.run(
      [sys.executable or "python3", "-m", "app.platform_boot", "check-packages",
       str(platform_root)],
      cwd=BAKED_BACKEND, env=_probe_env(), capture_output=True, text=True,
      timeout=PACKAGE_CHECK_TIMEOUT_SECONDS, check=False,
    )
  except (OSError, subprocess.SubprocessError):
    return None
  if completed.returncode not in (0, 1):
    return None  # an older image's usage error: it has no such command
  try:
    gap = json.loads(completed.stdout.strip().splitlines()[-1])
  except (IndexError, ValueError):
    return None
  return completed.returncode == 0, gap if isinstance(gap, dict) else None


class ImagePackageVerdict(NamedTuple):
  allowed: bool
  gap: dict | None
  # False when the image has ``check-packages`` but it could not answer, so
  # the strict rule stood in for this attempt only.
  settled: bool


def image_package_verdict(platform_root: Path) -> ImagePackageVerdict:
  """Whether this image's next boot serves ``platform_root``'s Python
  packages, and the ``PackageGap`` it judged (None when there is none).

  The one verdict restart admission and the status ``restart_blocker``
  share. It predicts the image's boot guard, not the served one: an image
  with ``check-packages`` answers itself (it serves a version-only gap). An
  older image refuses any input from a newer official release, so the
  served gap stands in with that strict rule; relaxing it here would admit a
  restart the old image's boot then refuses. The strict rule also stands in
  when the image's own check fails or times out, which is not ``settled``.
  """
  verdict = _baked_package_verdict(platform_root)
  if verdict is not None:
    return ImagePackageVerdict(*verdict, settled=True)
  from app import platform_update

  settled = not image_boot_checks_packages()
  gap = platform_update.python_package_gap(platform_root)
  if gap is None:
    return ImagePackageVerdict(True, None, settled)
  gap["blocking"] = gap["origin"] == "official_newer"
  return ImagePackageVerdict(not gap["blocking"], gap, settled)


def _refuse_what_the_image_boot_refuses(platform_root: Path) -> None:
  from app import platform_update

  allowed, gap, _settled = image_package_verdict(platform_root)
  if allowed:
    return
  reason = platform_update.describe_package_gap(gap) if gap else (
    "this container image refuses the platform source's Python packages"
  )
  raise RestartSourceInvalid(
    f"Restart stopped: {reason}",
    {"reason": "image_packages_behind_source", "gap": gap},
  )


def _claim_in_process_restart() -> bool:
  """Admit one Settings/platform/card restart for this worker lifetime.

  The winner never releases the latch after admission: losing the HTTP
  acknowledgement must not permit another drain, nonce, timer, or restart.
  """
  global _RESTART_ADMITTED
  with _RESTART_ADMISSION_LOCK:
    if _RESTART_ADMITTED:
      return False
    _RESTART_ADMITTED = True
    return True


def restart_admission_in_progress() -> bool:
  """Whether this worker owns a restart handoff, drain, or supervisor request."""
  with _RESTART_ADMISSION_LOCK:
    return _RESTART_ADMITTED


async def _drain_exact_restart(
  *, cutover: bool = False,
) -> tuple[str, str, list[dict[str, str]]]:
  """Gate admission, bind every live run to one fresh restart nonce, and swap
  in a prepared platform update once every chat is paused."""
  from app import chat, restart_ledger
  from app.broadcast import get_system_broadcast

  boot_id = restart_ledger.current_boot_id()
  get_system_broadcast().publish({
    "type": "server_restarting", "boot_id": boot_id,
  })
  chat.begin_drain()

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
  try:
    from app.platform_update import swap_in_prepared_update
    await asyncio.to_thread(swap_in_prepared_update, cutover=cutover)
  except Exception:
    log.warning("prepared platform update was not swapped in", exc_info=True)
  return boot_id, restart_nonce, restart_runs


async def restart_this_worker(
  ready_path: Path | None = None,
) -> None:
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
  # Last-chance defense for every caller, including future restart surfaces.
  # The synchronous owner-facing paths run this before acknowledging the
  # action; repeating it here closes the short source-change window without
  # draining or interrupting work when the source is invalid.
  try:
    validate_restart_source()
  except RestartSourceInvalid:
    log.error("planned restart rejected by startup preflight", exc_info=True)
    return

  if not _claim_in_process_restart():
    return

  # An approved restart of source that passed its startup check is an explicit
  # retry: a commit that boot set aside for not finishing its start gets new
  # boots.
  from app.platform_boot import forget_unready_boots
  forget_unready_boots()

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


async def prepare_container_cutover(
  cutover_id: str, *, abandoned_timeout: float | None = _CUTOVER_FAILSAFE_SECONDS,
) -> dict[str, object]:
  """Drain once for a Host-owned replacement without self-terminating.

  The root supervisor must first open the exact cutover challenge.  This
  process then parks and nonce-binds active turns, but publishes no shutdown
  sentinel: Docker/Compose owns the stop, so the accepted authorization binds
  to the replacement boot rather than an accidental intermediate restart.
  """
  from app import restart_ledger

  if not restart_ledger.authorized_cutover_challenge(cutover_id):
    raise RuntimeError("the Host did not authorize this cutover")

  boot_id, restart_nonce, restart_runs = await _drain_exact_restart(cutover=True)
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

  if abandoned_timeout is not None:
    watchdog = threading.Timer(abandoned_timeout, _recover_abandoned_cutover)
    watchdog.daemon = True
    watchdog.start()
  return {
    "status": "prepared",
    "cutover_id": cutover_id,
    "boot_id": boot_id,
    "run_count": len(restart_runs),
  }


async def prepare_managed_container_cutover(cutover_id: str) -> dict[str, object]:
  """Prepare a cutover whose provider, rather than this process, stops it.

  Once the account service accepts the operation it durably owns deployment
  and rollback, so a local watchdog must not race Railway's volume-backed
  cutover by restarting the old container mid-deploy.
  """
  return await prepare_container_cutover(cutover_id, abandoned_timeout=None)
