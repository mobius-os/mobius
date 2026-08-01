"""Process-wide chat diagnostics and resilient request-session commits."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings


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
      maxBytes=50 * 1024 * 1024,
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
