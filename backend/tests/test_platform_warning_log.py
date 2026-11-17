"""Platform warnings must be readable from the shared log, not only stderr."""

import logging

import pytest

from app import chat_logging, startup


class _Capture(logging.Handler):
  def __init__(self):
    super().__init__()
    self.messages: list[str] = []

  def emit(self, record):
    self.messages.append(record.getMessage())


@pytest.fixture
def platform_logger(monkeypatch):
  handler = _Capture()
  monkeypatch.setattr(chat_logging, "_chat_log_handler", handler)
  logger = logging.getLogger("app")
  monkeypatch.setattr(logger, "handlers", [])
  return logger, handler


def test_platform_warnings_reach_the_shared_log_and_still_reach_stderr(platform_logger):
  logger, shared_log = platform_logger
  chat_logging.install_platform_warning_log()
  logging.getLogger("app.database").warning("Database connection checked out for 2300ms")
  assert shared_log.messages == ["Database connection checked out for 2300ms"]
  assert logging.lastResort in logger.handlers


def test_platform_info_records_stay_out_of_the_shared_log(platform_logger):
  _, shared_log = platform_logger
  chat_logging.install_platform_warning_log()
  logging.getLogger("app.database").info("routine")
  assert shared_log.messages == []


def test_installing_twice_does_not_log_each_warning_twice(platform_logger):
  _, shared_log = platform_logger
  chat_logging.install_platform_warning_log()
  chat_logging.install_platform_warning_log()
  logging.getLogger("app.database").warning("once")
  assert shared_log.messages == ["once"]


def test_server_start_installs_the_platform_warning_log_before_startup_work():
  import inspect
  from app import main
  source = inspect.getsource(main.lifespan)
  assert source.index("install_platform_warning_log()") < source.index("run_startup_plan")


@pytest.mark.parametrize("name", [
  "app.providers.models",
  "app.helper_hosts",
  "app.claude_helper_host",
  "app.platform_update",
])
def test_startup_diagnostics_reach_the_shared_log_exactly_once(platform_logger, monkeypatch, name):
  """These loggers also sit under `app`, so wiring both would write each record twice."""
  _, shared_log = platform_logger
  child = logging.getLogger(name)
  monkeypatch.setattr(child, "handlers", [])
  monkeypatch.setattr(child, "level", child.level)
  chat_logging.install_platform_warning_log()
  startup._route_diagnostics_to_chat_log(None)
  child.warning("once only")
  assert shared_log.messages == ["once only"]


def test_startup_gives_the_memory_logger_its_own_shared_handler(platform_logger, monkeypatch):
  """`moebius.memory` sits outside `app`, so it is the one logger wired directly."""
  _, shared_log = platform_logger
  memory = logging.getLogger("moebius.memory")
  monkeypatch.setattr(memory, "handlers", [])
  monkeypatch.setattr(memory, "level", memory.level)
  startup._route_diagnostics_to_chat_log(None)
  memory.info("remembered")
  assert shared_log.messages == ["remembered"]
