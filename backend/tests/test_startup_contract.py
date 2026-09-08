"""Startup contract tests: catch split-generation / dropped-command regressions.

The regression this guards against (the 2026 "restore startup assistant identity
backfill"): a production startup task lazily imports a chat_writer command — e.g.
``_backfill_active_assistant_identities`` does ``from app.chat_writer import
BackfillAssistantIdentity`` — and a bad merge dropped the class. At boot the task
raised ``ImportError``, ``run_startup_tasks`` swallowed it (fail-open), and the
backfill silently never ran while ``/api/ready`` stayed green. Because the whole
served app package boots from ``/data/platform`` (which can carry a half-merged
generation), a symbol another generation removed is exactly this class.

These tests catch it WITHOUT a live boot: statically resolve every symbol the
production startup tasks import from ``app.*``, and verify the ``failed_tasks``
seam actually records a swallowed database-phase failure so it is assertable.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import logging
import textwrap
from types import SimpleNamespace

import pytest

import app.startup as startup
from app.startup import (
  DatabaseBootResult,
  StartupContext,
  StartupTask,
  run_startup_plan,
)


def _startup_action_functions():
  tasks = startup.PROCESS_STARTUP_TASKS + startup.DATABASE_STARTUP_TASKS
  for task in tasks:
    action = getattr(task.action, "func", task.action)  # unwrap partial
    if inspect.isfunction(action):
      yield task.name, action


def test_startup_task_app_imports_all_resolve():
  """Every ``from app.X import Y`` inside a production startup task must resolve.

  A dropped symbol (the BackfillAssistantIdentity class) surfaces here as a
  failing assertion in CI, before it can ship and silently no-op at boot.
  """
  unresolved: list[str] = []
  checked = 0
  for name, fn in _startup_action_functions():
    try:
      source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
      continue
    tree = ast.parse(source)
    for node in ast.walk(tree):
      if not isinstance(node, ast.ImportFrom):
        continue
      module_name = node.module or ""
      if not module_name.startswith("app."):
        continue
      module = importlib.import_module(module_name)
      for alias in node.names:
        if alias.name == "*":
          continue
        checked += 1
        if not hasattr(module, alias.name):
          unresolved.append(f"{name}: {module_name}.{alias.name}")
  assert not unresolved, f"dropped startup imports: {unresolved}"
  # The scan must have actually found imports to verify — a self-check so the
  # test cannot silently pass because it inspected nothing.
  assert checked > 0


def test_reconcile_and_backfill_startup_commands_are_importable():
  """The specific commands startup depends on, checked by name as a tripwire."""
  from app.chat_writer import BackfillAssistantIdentity, ReconcileStartupChat

  assert BackfillAssistantIdentity(chat_id="c").chat_id == "c"
  assert ReconcileStartupChat is not None


def _context():
  return StartupContext(
    app=SimpleNamespace(state=SimpleNamespace()),
    settings=SimpleNamespace(data_dir="/tmp"),
    boot_id="test-boot",
    init_db=DatabaseBootResult,
    install_pm_commit_launcher=lambda _s, _t: False,
    assert_provider_defaults=lambda _n: None,
    logger=logging.getLogger("test.startup.contract"),
  )


@pytest.mark.asyncio
async def test_failed_tasks_seam_records_swallowed_database_phase_failure(
  monkeypatch, caplog,
):
  """A database-phase task that ImportErrors is recorded on failed_tasks and
  surfaced at end of boot — the assertable catch for a dropped command."""
  def dropped_command(_context):
    raise ImportError("cannot import name 'BackfillAssistantIdentity'")

  monkeypatch.setattr(startup, "PROCESS_STARTUP_TASKS", ())
  monkeypatch.setattr(startup, "DATABASE_STARTUP_TASKS", (
    StartupTask("backfill active assistant identities", dropped_command),
    StartupTask("later task", lambda _c: None),
  ))

  ctx = _context()
  with caplog.at_level(logging.ERROR, logger="test.startup.contract"):
    result = await run_startup_plan(ctx)

  # Fail-open: boot still serviceable and later tasks still ran...
  assert result.serviceable is True
  # ...but the swallowed failure is recorded and surfaced, not lost.
  assert ctx.failed_tasks == ["backfill active assistant identities"]
  assert "swallowed task failure" in caplog.text
