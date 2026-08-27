import logging
from types import SimpleNamespace

import pytest

import app.startup as startup
from app.startup import (
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
    init_db=lambda: [],
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

  with caplog.at_level(logging.ERROR, logger="test.startup"):
    await run_startup_tasks(context(), (
      StartupTask("optional repair", fail),
      StartupTask("next repair", continue_boot),
    ))

  assert events == ["failed", "continued"]
  assert "startup task optional repair failed" in caplog.text


@pytest.mark.asyncio
async def test_critical_startup_failure_stops_before_later_tasks():
  events = []

  def fail(_context):
    events.append("database")
    raise RuntimeError("database unavailable")

  def must_not_run(_context):
    events.append("later")

  with pytest.raises(RuntimeError, match="database unavailable"):
    await run_startup_tasks(context(), (
      StartupTask("database", fail, critical=True),
      StartupTask("later", must_not_run),
    ))

  assert events == ["database"]


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


def test_production_startup_plan_has_explicit_unique_order_and_criticality():
  tasks = startup.PROCESS_STARTUP_TASKS + startup.DATABASE_STARTUP_TASKS
  names = [task.name for task in tasks]

  assert len(names) == len(set(names))
  assert [task.name for task in tasks if task.critical] == [
    "initialize database",
  ]
  assert startup.PROCESS_STARTUP_TASKS[-1].name == "initialize database"
  assert startup.DATABASE_STARTUP_TASKS[0].name == "start chat writer"
  assert names.index("initialize database") < names.index("start chat writer")
  assert names.index("start chat writer") < names.index(
    "backfill active assistant identities"
  )
  assert names.index("backfill active assistant identities") < names.index(
    "reconcile startup chats"
  )
  assert names.index("start chat writer") < names.index("fix forward chat media")
  assert names.index("start chat writer") < names.index("reconcile startup chats")
  assert names.index("initialize push") < names.index("notify reconciled chats")
  assert names.index("install bootstrap apps") < names.index(
    "reconcile app cron supervision"
  )


@pytest.mark.asyncio
async def test_schema_mismatch_skips_the_entire_database_startup_phase(
  monkeypatch, caplog,
):
  events = []
  startup_context = context()
  startup_context.init_db = lambda: ["apps.paused_capabilities"]
  monkeypatch.setattr(startup, "PROCESS_STARTUP_TASKS", (
    StartupTask("initialize database", startup._initialize_database),
  ))
  monkeypatch.setattr(startup, "DATABASE_STARTUP_TASKS", (
    StartupTask("must not run", lambda _context: events.append("database")),
  ))
  checkpoints = []
  monkeypatch.setattr(startup, "record_memory_checkpoint", checkpoints.append)

  with caplog.at_level(logging.CRITICAL, logger="test.startup"):
    serviceable = await run_startup_plan(startup_context)

  assert serviceable is False
  assert startup_context.schema_gaps == ["apps.paused_capabilities"]
  assert events == []
  assert checkpoints == ["startup_schema_degraded"]
  assert "skipped 1 database startup task" in caplog.text


@pytest.mark.asyncio
async def test_schema_safe_boot_runs_the_database_startup_phase(monkeypatch):
  events = []
  monkeypatch.setattr(startup, "PROCESS_STARTUP_TASKS", (
    StartupTask("process", lambda _context: events.append("process")),
  ))
  monkeypatch.setattr(startup, "DATABASE_STARTUP_TASKS", (
    StartupTask("database", lambda _context: events.append("database")),
  ))

  serviceable = await run_startup_plan(context())

  assert serviceable is True
  assert events == ["process", "database"]
