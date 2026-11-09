"""Process-wide chat diagnostics and resilient request-session commits."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings


# Per-file bound for the platform's rotating logs.
LOG_FILE_MAX_BYTES = 50 * 1024 * 1024

_chat_log_handler: RotatingFileHandler | None = None


def get_chat_log_handler() -> RotatingFileHandler:
  """Return the one rotating handler shared by chat-adjacent subsystems.

  Sharing the handler is load-bearing: independent handlers rotating the same
  file can race and corrupt it.
  """
  global _chat_log_handler
  if _chat_log_handler is None:
    settings = get_settings()
    log_dir = Path(settings.data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
      log_dir / "chat.log",
      maxBytes=LOG_FILE_MAX_BYTES,
      backupCount=3,
      encoding="utf-8",
    )
    handler.setFormatter(
      logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    _chat_log_handler = handler
  return _chat_log_handler


def get_logger() -> logging.Logger:
  """Return the process chat logger, configured on first use."""
  logger = logging.getLogger("moebius.chat")
  if logger.handlers:
    return logger
  logger.addHandler(get_chat_log_handler())
  logger.setLevel(
    logging.DEBUG if os.getenv("MOEBIUS_CHAT_DEBUG") else logging.INFO
  )
  return logger


def install_platform_warning_log() -> None:
  """Keep the platform's own warnings readable after the fact.

  Records from `app.*` modules (slow database checkouts, dropped work) have no
  handler, so Python's last-resort handler prints them to the container's
  stderr and nowhere else: an agent asked to check the logs cannot read them.
  Send them to the shared chat log as well. Any handler on the chain switches
  the last-resort output off, so it is attached explicitly to keep warnings on
  stderr. Child loggers inherit both handlers; do not attach the shared
  handler to them again or each record is written twice.
  """
  platform_logger = logging.getLogger("app")
  handler = get_chat_log_handler()
  if handler in platform_logger.handlers:
    return
  platform_logger.addHandler(handler)
  platform_logger.addHandler(logging.lastResort)


def safe_commit(db: Session) -> bool:
  """Commit a request session without leaving it poisoned after lock bursts."""
  try:
    db.commit()
    return True
  except OperationalError as exc:
    get_logger().warning("db commit dropped (rolled back): %s", exc)
    try:
      db.rollback()
    except Exception:
      pass
    return False
