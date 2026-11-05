"""Platform warnings must be readable from the shared log, not only stderr."""

import logging

import pytest

from app import chat_logging


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
