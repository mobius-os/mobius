"""Named, ordered startup tasks for the owner-facing service.

Most reconciliation work fails open while database initialization remains
boot-critical. This module makes that ordering and criticality explicit without
turning startup into a plugin system: the task list is fixed application code
and every task is an ordinary function.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from app.database import SessionLocal
from app.memory_observability import record_memory_checkpoint


class StartupState(Protocol):
  media_migration_failed: bool
  reconciliation_failed: bool


class StartupApp(Protocol):
  state: StartupState


class StartupSettings(Protocol):
  data_dir: str


@dataclass
class StartupContext:
  app: StartupApp
  settings: StartupSettings
  boot_id: str
  init_db: Callable[[], None]
  install_pm_commit_launcher: Callable[[Path, Path], bool]
  assert_provider_defaults: Callable[[object], None]
  logger: logging.Logger = field(
    default_factory=lambda: logging.getLogger(__name__)
  )
  restart_authorization: str | None = None
  manual_reconciled_chats: list[str] = field(default_factory=list)
  restart_fallback_chats: list[str] = field(default_factory=list)


TaskAction = Callable[[StartupContext], object | Awaitable[object]]


@dataclass(frozen=True)
class StartupTask:
  name: str
  action: TaskAction
  critical: bool = False
  checkpoint: str | None = None


async def run_startup_tasks(
  context: StartupContext,
  tasks: tuple[StartupTask, ...],
) -> None:
  """Run the fixed boot plan in order with explicit failure semantics."""
  for task in tasks:
    try:
      result = task.action(context)
      if inspect.isawaitable(result):
        await result
    except Exception as exc:
      if task.critical:
        context.logger.critical(
          "critical startup task %s failed: %s",
          task.name,
          exc,
          exc_info=True,
        )
        raise
      context.logger.error(
        "startup task %s failed: %s",
        task.name,
        exc,
        exc_info=True,
      )
    else:
      if task.checkpoint:
        record_memory_checkpoint(task.checkpoint)


def _refresh_commit_launcher(context: StartupContext) -> None:
  context.install_pm_commit_launcher(
    Path(__file__).resolve().parents[1] / "scripts" / "pm-commit",
    Path(context.settings.data_dir) / ".pm-commit",
  )


def _validate_provider_defaults(context: StartupContext) -> None:
  from app.providers import PROVIDER_NAMES

  context.assert_provider_defaults(PROVIDER_NAMES)


def _remove_legacy_auto_resume_setting(context: StartupContext) -> None:
  from app.providers import remove_legacy_global_auto_resume_setting

  if not remove_legacy_global_auto_resume_setting(context.settings.data_dir):
    raise RuntimeError("legacy global auto-resume setting cleanup did not persist")


def _initialize_database(context: StartupContext) -> None:
  context.init_db()


def _purge_expired_chats(context: StartupContext) -> None:
  from app.chat_retention import purge_expired_chat_tombstones

  with SessionLocal() as db:
    purged = purge_expired_chat_tombstones(db)
  if purged:
    context.logger.info("purged %s expired chat tombstone(s)", len(purged))


def _backfill_session_links(context: StartupContext) -> None:
  from app.session_links import backfill_current_session_links

  with SessionLocal() as db:
    count = backfill_current_session_links(db)
  if count:
    context.logger.info("backfilled %s historical chat session link(s)", count)


def _backfill_prompt_snapshots(context: StartupContext) -> None:
  from app.chat import _chat_settings_dict, _custom_system_prompt, _read_skill_text
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
    context.logger.info(
      "captured system prompt snapshots for %s existing chats",
      count,
    )


def _fix_forward_chat_media(context: StartupContext) -> None:
  from app.chat_media import fix_forward_chat_media

  context.app.state.media_migration_failed = False
  try:
    with SessionLocal() as db:
      fix_forward_chat_media(db, context.settings.data_dir)
  except Exception:
    context.app.state.media_migration_failed = True
    raise


def _read_restart_authorization(context: StartupContext) -> None:
  from app.restart_ledger import authorized_restart_nonce

  context.restart_authorization = authorized_restart_nonce()


def _reconcile_startup_chats(context: StartupContext) -> None:
  from app.chat import reconcile_startup_chats

  try:
    with SessionLocal() as db:
      result = reconcile_startup_chats(
        db,
        restart_authorization=context.restart_authorization,
      )
    context.manual_reconciled_chats = result.manual
    context.restart_fallback_chats = result.restart_parks
  except Exception:
    context.app.state.reconciliation_failed = True
    raise


def _reap_staging_bundles(_context: StartupContext) -> None:
  from app.compiler import reap_staging_bundles

  reap_staging_bundles()


async def _reconcile_compiled_bundles(context: StartupContext) -> None:
  from app.compiler import (
    reap_orphaned_bundles,
    reconcile_missing_bundles,
    reconcile_outdated_bundles,
  )

  with SessionLocal() as db:
    healed = await reconcile_missing_bundles(db)
    migrated = await reconcile_outdated_bundles(db)
    removed = reap_orphaned_bundles(db)
  if healed or migrated or removed:
    context.logger.info(
      "compiled-bundle reconciliation: healed=%d migrated=%d reaped=%d",
      len(healed),
      len(migrated),
      len(removed),
    )


def _retire_integrated_provenance(context: StartupContext) -> None:
  from app.app_apply import retire_integrated_app_provenance

  with SessionLocal() as db:
    retired, warnings = retire_integrated_app_provenance(db)
  if retired or warnings:
    context.logger.info(
      "app provenance retirement: retired=%d warnings=%d",
      retired,
      len(warnings),
    )
  for warning in warnings:
    context.logger.warning("app provenance retirement: %s", warning)


def _start_chat_writer(_context: StartupContext) -> None:
  from app.chat_writer import start_writer

  start_writer()


def _initialize_push(_context: StartupContext) -> None:
  from app.push import init_vapid

  init_vapid()


def _notify_reconciled_chats(context: StartupContext) -> None:
  if not context.manual_reconciled_chats:
    return
  from app.chat import notify_after_reconcile

  with SessionLocal() as db:
    notify_after_reconcile(db, context.manual_reconciled_chats)


async def _wake_completed_delegation_parents(context: StartupContext) -> None:
  """Wake parents whose delegation child settled while the process was down.

  Runs after reconcile so a child that will auto-resume is still `resuming`/
  `running` (not wake-eligible) and is skipped; a child that genuinely
  completed while away wakes its parent. Best-effort and retry-latched.
  """
  from app.delegations import wake_parents_for_completed_delegations

  try:
    woken = await wake_parents_for_completed_delegations()
    if woken:
      context.logger.info(
        "woke %d parent chat(s) for completed-while-away delegations", woken,
      )
  except Exception:
    context.logger.warning(
      "delegation parent-wake reconcile skipped", exc_info=True,
    )


async def _reconcile_running_gauntlets(context: StartupContext) -> None:
  """Repair missing slots and release barriers committed before restart."""
  from app.gauntlets import (
    reconcile_running_gauntlets,
    repair_terminal_gauntlet_projections,
  )

  try:
    count = await reconcile_running_gauntlets()
    if count:
      context.logger.info("reconciled %d running Gauntlet(s)", count)
    repaired = await repair_terminal_gauntlet_projections()
    if repaired:
      context.logger.info(
        "repaired %d terminal Gauntlet projection(s)", repaired,
      )
  except Exception:
    context.logger.warning("Gauntlet boot reconcile skipped", exc_info=True)


async def _install_bootstrap_apps(context: StartupContext) -> None:
  from app.bootstrap import ensure_bootstrap_apps_installed

  with SessionLocal() as db:
    await ensure_bootstrap_apps_installed(db)




def _reconcile_app_cron(context: StartupContext) -> None:
  from app.routes.app_schedules import reconcile_app_cron_supervision

  with SessionLocal() as db:
    count, warnings = reconcile_app_cron_supervision(db)
  if count:
    context.logger.info("supervised %d app cron schedule(s)", count)
  for warning in warnings:
    context.logger.warning("app cron supervision skipped: %s", warning)
  if warnings:
    return
  ready = Path(context.settings.data_dir) / "run" / "app-cron-supervision-ready"
  ready.parent.mkdir(parents=True, exist_ok=True)
  ready.write_text(f"{context.boot_id}\n", encoding="utf-8")


def _route_diagnostics_to_chat_log(_context: StartupContext) -> None:
  from app.chat_logging import get_chat_log_handler

  handler = get_chat_log_handler()
  for name, level in (
    ("app.providers.models", logging.WARNING),
    ("moebius.memory", logging.INFO),
  ):
    logger = logging.getLogger(name)
    if handler not in logger.handlers:
      logger.addHandler(handler)
    logger.setLevel(level)


STARTUP_TASKS = (
  StartupTask("refresh pm-commit launcher", _refresh_commit_launcher),
  StartupTask("validate provider defaults", _validate_provider_defaults),
  StartupTask(
    "remove legacy global auto-resume setting",
    _remove_legacy_auto_resume_setting,
  ),
  StartupTask(
    "initialize database",
    _initialize_database,
    critical=True,
    checkpoint="startup_database_initialized",
  ),
  # Transcript migrations below are writer domain commands. Start ownership as
  # soon as the schema exists; later startup failures still fail open exactly
  # as they did when the writer started near the end of the plan.
  StartupTask("start chat writer", _start_chat_writer),
  StartupTask("purge expired chat tombstones", _purge_expired_chats),
  StartupTask("backfill session links", _backfill_session_links),
  StartupTask("backfill prompt snapshots", _backfill_prompt_snapshots),
  StartupTask("fix forward chat media", _fix_forward_chat_media),
  StartupTask("read restart authorization", _read_restart_authorization),
  StartupTask(
    "reconcile startup chats",
    _reconcile_startup_chats,
    checkpoint="startup_state_reconciled",
  ),
  StartupTask("reap staging bundles", _reap_staging_bundles),
  StartupTask(
    "reconcile compiled bundles",
    _reconcile_compiled_bundles,
    checkpoint="startup_bundles_reconciled",
  ),
  StartupTask(
    "retire integrated app provenance",
    _retire_integrated_provenance,
    checkpoint="startup_app_provenance_retired",
  ),
  StartupTask("initialize push", _initialize_push),
  StartupTask("notify reconciled chats", _notify_reconciled_chats),
  StartupTask("reconcile running Gauntlets", _reconcile_running_gauntlets),
  StartupTask(
    "wake completed delegation parents",
    _wake_completed_delegation_parents,
  ),
  StartupTask(
    "install bootstrap apps",
    _install_bootstrap_apps,
    checkpoint="startup_apps_bootstrapped",
  ),
  StartupTask(
    "reconcile app cron supervision",
    _reconcile_app_cron,
    checkpoint="startup_metadata_reconciled",
  ),
  StartupTask(
    "route diagnostics to chat log",
    _route_diagnostics_to_chat_log,
    checkpoint="startup_app_source_ready",
  ),
)
