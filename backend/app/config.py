"""Application settings loaded from environment variables.

FROZEN at runtime (chmod 444 root-owned per protected-files.txt).
main.py imports this at module load; if I'm broken the server
can't boot and /recover/chat is unreachable.

To edit me, change the source on the host repo and rebuild the
container image. The agent should not try to edit me in-place at
runtime — the chmod will block it and the error looks like a bug.
Use /data/shared/agent-settings.json for per-instance settings that
don't need code changes.
"""

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
  # Root the owner-facing /api/fs viewer is confined to (reads). Empty falls
  # back to data_dir; ships narrow (`/data`) and can widen later without code.
  # Writes are always pinned to data_dir regardless (the mobius process can
  # only write /data).
  fs_view_root: str = ""
  frontend_origin: str = "http://localhost:5173"
  api_base_url: str = f"http://localhost:{os.environ.get('PORT', '8000')}"
  # Git commit the running image was built from, baked at `docker build` time
  # via the BUILD_SHA build-arg (Dockerfile + deploy-prod.sh). "unknown" for a
  # local `docker compose up` that didn't pass it. Surfaced at GET /api/version
  # so a deploy can verify the SERVED backend matches the intended commit.
  build_sha: str = "unknown"

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
