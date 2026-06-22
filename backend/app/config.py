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

  # Auto memory-search: on a substantive FIRST message of a chat, the platform
  # runs the memory-search subagent (scripts/memory_search.py) and injects its
  # result into the <agent_experience> block — so deep recall happens without
  # the agent remembering to call it (it empirically routes around the
  # instruction). OFF by default: it adds the search's latency (up to the
  # timeout) to the first reply and spends tokens, so it's an owner opt-in.
  auto_memory_search: bool = False
  # Seconds to wait for the auto-search before proceeding without it. A miss
  # never fails the turn — the agent just gets the normal injected block. The
  # subagent traversal takes ~35-45s, so this is the dead latency added to the
  # FIRST reply when the flag is on; that latency (vs the agent narrating while
  # it runs its own search) is the main reason this path is off by default.
  auto_memory_search_timeout: int = 60

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
