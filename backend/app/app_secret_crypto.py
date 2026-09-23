"""Shared encryption primitive for app-scoped secrets and trusted runtimes."""

import base64
import hashlib
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import get_settings


def app_secret_fernet() -> Fernet:
  material = f"mobius-app-secret-v1:{get_settings().secret_key}".encode()
  key = base64.urlsafe_b64encode(hashlib.sha256(material).digest())
  return Fernet(key)


def decrypt_app_secret(path: Path) -> str:
  return app_secret_fernet().decrypt(path.read_bytes()).decode()
