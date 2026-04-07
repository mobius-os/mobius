"""Application settings loaded from environment variables."""

import os
from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
  """Application settings."""

  secret_key: str
  domain: str = "localhost"
  database_url: str = "sqlite:////data/db/ultimate.db"
  data_dir: str = "/data"
  frontend_origin: str = "http://localhost:5173"
  api_base_url: str = "http://localhost:8000"

  model_config = SettingsConfigDict(env_file=".env")

  @model_validator(mode="before")
  @classmethod
  def auto_detect_platform_domain(cls, values):
    """On Railway/managed platforms, derive domain and origin automatically."""
    domain = values.get("domain") or values.get("DOMAIN") or "localhost"
    origin = values.get("frontend_origin") or values.get("FRONTEND_ORIGIN") or ""
    railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    port = os.environ.get("PORT", "8000")
    if railway_domain and domain == "localhost":
      values["domain"] = railway_domain
      values["frontend_origin"] = f"https://{railway_domain}"
      # Use localhost for agent subprocess calls (same container),
      # with the correct port that Railway assigned.
      values["api_base_url"] = f"http://localhost:{port}"
    elif domain != "localhost" and (not origin or origin == "http://localhost:5173"):
      values["frontend_origin"] = f"https://{domain}"
      values["api_base_url"] = f"https://{domain}"
    return values

  @field_validator("secret_key")
  @classmethod
  def secret_key_must_be_strong(cls, v: str) -> str:
    """Rejects weak keys at startup before any JWT is signed."""
    if len(v) < 32:
      raise ValueError(
        "SECRET_KEY must be at least 32 characters long. "
        'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
      )
    return v

  @model_validator(mode="after")
  def validate_frontend_origin(self) -> "Settings":
    """Catches the common mistake of leaving DOMAIN blank in .env."""
    if self.frontend_origin in ("https://", "http://", "https:///", "http:///"):
      raise ValueError(
        "FRONTEND_ORIGIN is invalid (got '%s'). "
        "Set DOMAIN=your-domain.com in .env, or set FRONTEND_ORIGIN explicitly "
        "for HTTP-only deployments." % self.frontend_origin
      )
    return self


@lru_cache
def get_settings() -> Settings:
  """Returns the cached application settings singleton."""
  return Settings()
