"""Application settings loaded from environment variables."""

import os
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
  """Application settings."""

  secret_key: str
  domain: str = "localhost"
  database_url: str = "sqlite:////data/db/ultimate.db"
  data_dir: str = "/data"
  frontend_origin: str = "http://localhost:5173"
  api_base_url: str = f"http://localhost:{os.environ.get('PORT', '8000')}"

  model_config = SettingsConfigDict(env_file=".env")

  @model_validator(mode="after")
  def _validate_and_derive(self) -> "Settings":
    """Validates secret_key strength and derives frontend_origin from
    DOMAIN on managed platforms (Railway) or when only DOMAIN is set."""
    if len(self.secret_key) < 32:
      raise ValueError(
        "SECRET_KEY must be at least 32 characters long. "
        "Generate one with: "
        'python3 -c "import secrets; print(secrets.token_hex(32))"'
      )

    # Railway: auto-derive domain + origin when running on their platform.
    railway_domain = os.environ.get("RAILWAY_PUBLIC_DOMAIN")
    if railway_domain and self.domain == "localhost":
      self.domain = railway_domain
      self.frontend_origin = f"https://{railway_domain}"
    # Self-hosted: derive origin from domain when not explicitly set.
    elif self.domain != "localhost" and self.frontend_origin == (
      "http://localhost:5173"
    ):
      self.frontend_origin = f"https://{self.domain}"

    # Catch the common mistake of leaving DOMAIN blank.
    if self.frontend_origin in (
      "https://", "http://", "https:///", "http:///",
    ):
      raise ValueError(
        f"FRONTEND_ORIGIN is invalid (got {self.frontend_origin!r}). "
        "Set DOMAIN=your-domain.com in .env, or set FRONTEND_ORIGIN "
        "explicitly for HTTP-only deployments."
      )
    return self


@lru_cache
def get_settings() -> Settings:
  """Returns the cached application settings singleton."""
  return Settings()
