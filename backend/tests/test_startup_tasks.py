import logging
from types import SimpleNamespace

import pytest

import app.startup as startup
from app.startup import StartupContext, StartupTask, run_startup_tasks


def context():
  return StartupContext(
    app=SimpleNamespace(state=SimpleNamespace()),
    settings=SimpleNamespace(data_dir="/tmp"),
    boot_id="test-boot",
    init_db=lambda: None,
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
  names = [task.name for task in startup.STARTUP_TASKS]

  assert len(names) == len(set(names))
  assert [task.name for task in startup.STARTUP_TASKS if task.critical] == [
    "initialize database",
  ]
  assert names.index("initialize database") < names.index("start chat writer")
  assert names.index("start chat writer") < names.index("fix forward chat media")
  assert names.index("start chat writer") < names.index("reconcile startup chats")
  assert names.index("initialize push") < names.index("notify reconciled chats")
  assert names.index("install bootstrap apps") < names.index(
    "reconcile app cron supervision"
  )
