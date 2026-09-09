import logging
from types import SimpleNamespace

import pytest

import app.startup as startup
from app.startup import (
  DatabaseBootResult,
  StartupContext,
  StartupTask,
  run_startup_plan,
  run_startup_tasks,
)


def context():
  return StartupContext(
    app=SimpleNamespace(state=SimpleNamespace()),
    settings=SimpleNamespace(data_dir="/tmp"),
    boot_id="test-boot",
    init_db=DatabaseBootResult,
    install_pm_commit_launcher=lambda _source, _target: False,
    assert_provider_defaults=lambda _names: None,
    logger=logging.getLogger("test.startup"),
  )


@pytest.mark.asyncio
async def test_best_effort_startup_failure_is_named_and_does_not_stop_plan(
  caplog,
):
  events = []

  def fail(_context):
    events.append("failed")
    raise RuntimeError("optional unavailable")

  async def continue_boot(_context):
    events.append("continued")

  ctx = context()
  with caplog.at_level(logging.ERROR, logger="test.startup"):
    await run_startup_tasks(ctx, (
      StartupTask("optional repair", fail),
      StartupTask("next repair", continue_boot),
    ))

  assert events == ["failed", "continued"]
  assert "startup task optional repair failed" in caplog.text
  # The swallowed failure is recorded on the context (assertable seam), not
  # only emitted to the log — a contract test can now catch a silently skipped
  # task instead of it vanishing.
  assert ctx.failed_tasks == ["optional repair"]


@pytest.mark.asyncio
async def test_checkpoints_record_only_successful_named_outcomes(monkeypatch):
  checkpoints = []
  monkeypatch.setattr(
    startup,
    "record_memory_checkpoint",
    checkpoints.append,
  )

  def fail(_context):
    raise RuntimeError("not complete")

  await run_startup_tasks(context(), (
    StartupTask("failed", fail, checkpoint="failed_checkpoint"),
    StartupTask("complete", lambda _context: None, checkpoint="complete_checkpoint"),
  ))

  assert checkpoints == ["complete_checkpoint"]


def test_production_startup_plan_has_explicit_unique_order():
  tasks = startup.PROCESS_STARTUP_TASKS + startup.DATABASE_STARTUP_TASKS
  names = [task.name for task in tasks]

  assert len(names) == len(set(names))
  assert startup.PROCESS_STARTUP_TASKS[-1].name == "initialize database"
  assert names.index("sweep Codex provider sessions") < names.index(
    "configure Claude provider retention"
  ) < names.index("initialize database")
  assert startup.DATABASE_STARTUP_TASKS[0].name == "start chat writer"
  assert names.index("initialize database") < names.index("start chat writer")
  assert names.index("start chat writer") < names.index(
    "retire legacy Gauntlet execution"
  )
  assert names.index("retire legacy Gauntlet execution") < names.index(
    "reconcile startup chats"
  )
  assert names.index("retire legacy Gauntlet execution") < names.index(
    "reconcile unstarted delegations"
  )
  assert names.index("start chat writer") < names.index(
    "backfill active assistant identities"
  )
  assert names.index("backfill active assistant identities") < names.index(
    "reconcile startup chats"
  )
  assert names.index("start chat writer") < names.index("fix forward chat media")
  assert names.index("start chat writer") < names.index("reconcile startup chats")
  assert names.index("freeze legacy app runtimes") < names.index("reconcile startup chats")
  assert names.index("freeze legacy app runtimes") < names.index("reconcile app cron supervision")
  assert names.index("initialize push") < names.index("notify reconciled chats")
  assert names.index("install bootstrap apps") < names.index(
    "reconcile app cron supervision"
  )


@pytest.mark.asyncio
async def test_claude_config_failure_cannot_suppress_pre_db_codex_reclaim(
  monkeypatch,
):
  import app.provider_session_retention as retention

  events = []

  def sweep(_data_dir):
    events.append("codex-swept")
    return {
      "status": "completed",
      "reclaimed_bytes": 0,
      "removed_files": 0,
      "errors": 0,
    }

  def fail_claude(_data_dir):
    events.append("claude-failed")
    raise OSError("settings disk full")

  monkeypatch.setattr(retention, "sweep_stale_provider_sessions", sweep)
  monkeypatch.setattr(retention, "ensure_claude_retention_default", fail_claude)
  tasks = tuple(
    task for task in startup.PROCESS_STARTUP_TASKS
    if task.name in {
      "sweep Codex provider sessions",
      "configure Claude provider retention",
    }
  )

  ctx = context()
  await run_startup_tasks(ctx, tasks)

  assert events == ["codex-swept", "claude-failed"]
  assert ctx.failed_tasks == ["configure Claude provider retention"]


def test_active_assistant_backfill_command_is_available_to_startup():
  """Keep the startup task and its writer-owned command in one release."""
  from app.chat_writer import BackfillAssistantIdentity

  command = BackfillAssistantIdentity(chat_id="legacy-chat")
  assert command.chat_id == "legacy-chat"


@pytest.mark.asyncio
async def test_schema_mismatch_skips_the_entire_database_startup_phase(
  monkeypatch, caplog,
):
  events = []
  startup_context = context()
  startup_context.init_db = lambda: DatabaseBootResult(
    schema_gaps=("apps.paused_capabilities",),
  )
  monkeypatch.setattr(startup, "PROCESS_STARTUP_TASKS", (
    StartupTask("initialize database", startup._initialize_database),
  ))
  monkeypatch.setattr(startup, "DATABASE_STARTUP_TASKS", (
    StartupTask("must not run", lambda _context: events.append("database")),
  ))
  checkpoints = []
  monkeypatch.setattr(startup, "record_memory_checkpoint", checkpoints.append)

  with caplog.at_level(logging.CRITICAL, logger="test.startup"):
    result = await run_startup_plan(startup_context)

  assert result.serviceable is False
  assert startup_context.database_boot.schema_gaps == (
    "apps.paused_capabilities",
  )
  assert events == []
  assert checkpoints == [
    "startup_database_checked",
    "startup_database_degraded",
  ]
  assert "skipped 1 database task" in caplog.text


@pytest.mark.asyncio
async def test_database_initialization_failure_enters_bounded_degraded_boot(
  monkeypatch, caplog,
):
  events = []
  startup_context = context()

  def fail_init():
    raise RuntimeError("broken migration")

  startup_context.init_db = fail_init
  monkeypatch.setattr(startup, "PROCESS_STARTUP_TASKS", (
    StartupTask("initialize database", startup._initialize_database),
  ))
  monkeypatch.setattr(startup, "DATABASE_STARTUP_TASKS", (
    StartupTask("must not run", lambda _context: events.append("database")),
  ))
  checkpoints = []
  monkeypatch.setattr(startup, "record_memory_checkpoint", checkpoints.append)

  with caplog.at_level(logging.CRITICAL, logger="test.startup"):
    result = await run_startup_plan(startup_context)

  assert result == DatabaseBootResult(
    failure_reason="database_initialization_failed",
  )
  assert events == []
  assert checkpoints == [
    "startup_database_checked",
    "startup_database_degraded",
  ]
  assert "database initialization failed: broken migration" in caplog.text


@pytest.mark.asyncio
async def test_schema_safe_boot_runs_the_database_startup_phase(monkeypatch):
  events = []
  monkeypatch.setattr(startup, "PROCESS_STARTUP_TASKS", (
    StartupTask("process", lambda _context: events.append("process")),
  ))
  monkeypatch.setattr(startup, "DATABASE_STARTUP_TASKS", (
    StartupTask("database", lambda _context: events.append("database")),
  ))

  result = await run_startup_plan(context())

  assert result.serviceable is True
  assert events == ["process", "database"]


@pytest.mark.asyncio
async def test_required_execution_cutover_failure_stops_database_recovery(
  monkeypatch, caplog,
):
  events = []

  def fail_cutover(_context):
    events.append("cutover")
    raise RuntimeError("writer did not commit")

  monkeypatch.setattr(startup, "PROCESS_STARTUP_TASKS", ())
  monkeypatch.setattr(startup, "DATABASE_STARTUP_TASKS", (
    StartupTask(
      "retire legacy Gauntlet execution",
      fail_cutover,
      database_failure_reason="legacy_gauntlet_retirement_failed",
    ),
    StartupTask(
      "must not reconcile", lambda _context: events.append("recovery"),
    ),
  ))
  checkpoints = []
  monkeypatch.setattr(startup, "record_memory_checkpoint", checkpoints.append)

  ctx = context()
  with caplog.at_level(logging.CRITICAL, logger="test.startup"):
    result = await run_startup_plan(ctx)

  assert result == DatabaseBootResult(
    failure_reason="legacy_gauntlet_retirement_failed",
  )
  assert events == ["cutover"]
  assert ctx.failed_tasks == ["retire legacy Gauntlet execution"]
  assert checkpoints == ["startup_database_degraded"]
  assert "required startup task did not settle" in caplog.text
