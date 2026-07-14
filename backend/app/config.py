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

import json
import os
from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _read_build_info() -> dict:
  """Reads optional Docker-baked build metadata.

  deploy-prod.sh still passes BUILD_SHA/BUILD_DATE directly. Managed Docker
  builders (Railway) may expose their own build args without our compose
  wrapper, so the Dockerfile also writes a tiny fallback JSON file.
  """
  path = Path(os.environ.get("MOBIUS_BUILD_INFO_PATH", "/app/build-info.json"))
  try:
    data = json.loads(path.read_text(encoding="utf-8"))
  except Exception:
    return {}
  return data if isinstance(data, dict) else {}


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
  # Commit date (YYYY-MM-DD) of build_sha, baked via the BUILD_DATE build-arg.
  # Surfaced at GET /api/version so Settings can show "version · date".
  build_date: str = "unknown"

  # GitHub OAuth app client id (env GITHUB_OAUTH_CLIENT_ID) for the device
  # flow in routes/github.py. Device flow needs only the client id — no
  # secret — and a client id is public by design, so the Möbius OAuth
  # app's id ships as the default: every instance gets one-tap GitHub
  # sign-in out of the box. Self-hosters can point at their own OAuth app
  # via the env var; empty disables the device flow (classic-PAT connect
  # still works). GitHub caps device-code submissions at 50/hour per
  # client id, shared across every instance using it — a future scaling
  # concern, not a today one.
  github_oauth_client_id: str = "Ov23liMpOLS6qp5YV8Vk"

  # Ensure every settled chat has a current platform-owned summary note. The
  # tool-free publisher (scripts/chat_note.py) runs at turn-end after the reply
  # is sent, so it adds no user-facing latency. No chat agent writes these files.
  ensure_chat_note: bool = True

  model_config = SettingsConfigDict(env_file=".env")

  @model_validator(mode="after")
  def _validate_and_derive(self) -> "Settings":
    """Validates secret_key strength and derives frontend_origin from
    DOMAIN on managed platforms (Railway) or when only DOMAIN is set."""
    build_info = _read_build_info()
    if (self.build_sha or "").strip() in ("", "unknown"):
      railway_sha = (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "").strip()
      baked_sha = str(build_info.get("sha") or "").strip()
      if railway_sha:
        self.build_sha = railway_sha
      elif baked_sha and baked_sha != "unknown":
        self.build_sha = baked_sha
    if (self.build_date or "").strip() in ("", "unknown"):
      baked_date = str(build_info.get("build_date") or "").strip()
      if baked_date and baked_date != "unknown":
        self.build_date = baked_date

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
