"""Application settings loaded from environment variables.

Served from the editable platform checkout. main.py imports this at module load;
if a local edit breaks it, normal boot falls back to the baked platform and
preserves the checkout for operator repair.

Use /data/shared/agent-settings.json for per-instance settings that do not need
code changes. Source edits require a restart and should be committed in the
platform checkout for rollback.
"""

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse, urlunsplit

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


def _validated_origin(value: str, setting_name: str) -> str:
  """Return a normalized HTTPS origin (HTTP is allowed only on loopback)."""
  normalized = value.strip().rstrip("/")
  parsed = urlparse(normalized)
  is_local_http = (
    parsed.scheme == "http"
    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
  )
  try:
    parsed.port
  except ValueError as exc:
    raise ValueError(f"{setting_name} must be an HTTPS origin.") from exc
  if (
    (parsed.scheme != "https" and not is_local_http)
    or not parsed.hostname
    or parsed.username is not None
    or parsed.password is not None
    or parsed.path not in {"", "/"}
    or parsed.params
    or parsed.query
    or parsed.fragment
  ):
    raise ValueError(f"{setting_name} must be an HTTPS origin.")
  return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "", "", ""))


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

  # Managed deployments receive this complete triplet from their provisioning
  # layer. When absent, Möbius is an ordinary self-hosted installation and
  # keeps the local username/password setup flow. Partial configuration is a
  # startup error: silently falling back to first-owner setup would reopen the
  # ownership race managed sign-in exists to close.
  mobius_sso_issuer: str = ""
  mobius_sso_instance_id: str = ""
  mobius_sso_client_secret: str = ""
  # Authoritative account service used by ordinary self-hosted installations
  # when their owner links a mobius.you identity. Keep this independent from
  # the managed-deployment SSO issuer so the account service can move hosts
  # without a code change or an implicit coupling between the two protocols.
  mobius_account_origin: str = "https://www.mobius.you"
  # Public origin of this installation, bound into self-hosted account grants.
  # Empty derives from FRONTEND_ORIGIN when that is HTTPS or loopback HTTP.
  mobius_account_client_origin: str = ""

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
    sso_values = (
      self.mobius_sso_issuer.strip(),
      self.mobius_sso_instance_id.strip(),
      self.mobius_sso_client_secret,
    )
    if any(sso_values) and not all(sso_values):
      raise ValueError(
        "MOBIUS_SSO_ISSUER, MOBIUS_SSO_INSTANCE_ID, and "
        "MOBIUS_SSO_CLIENT_SECRET must be configured together."
      )
    if all(sso_values):
      self.mobius_sso_issuer = _validated_origin(
        sso_values[0], "MOBIUS_SSO_ISSUER",
      )
      if not re.fullmatch(r"mob_[A-Za-z0-9_-]{3,80}", sso_values[1]):
        raise ValueError("MOBIUS_SSO_INSTANCE_ID is invalid.")
      if len(sso_values[2]) < 32:
        raise ValueError("MOBIUS_SSO_CLIENT_SECRET must be at least 32 characters.")
      self.mobius_sso_instance_id = sso_values[1]
    self.mobius_account_origin = _validated_origin(
      self.mobius_account_origin, "MOBIUS_ACCOUNT_ORIGIN",
    )
    client_origin = self.mobius_account_client_origin.strip()
    if client_origin:
      self.mobius_account_client_origin = _validated_origin(
        client_origin, "MOBIUS_ACCOUNT_CLIENT_ORIGIN",
      )
    else:
      try:
        self.mobius_account_client_origin = _validated_origin(
          self.frontend_origin, "FRONTEND_ORIGIN",
        )
      except ValueError:
        # Non-loopback HTTP remains supported for an entirely local Möbius,
        # but it cannot be entrusted with a cross-origin account grant.
        self.mobius_account_client_origin = ""
    return self

  @property
  def mobius_sso_enabled(self) -> bool:
    return bool(
      self.mobius_sso_issuer
      and self.mobius_sso_instance_id
      and self.mobius_sso_client_secret
    )


@lru_cache
def get_settings() -> Settings:
  """Returns the cached application settings singleton."""
  return Settings()


def agent_scratch_root() -> Path:
  """Root under which each chat gets its own agent scratch directory.

  On the data volume rather than the container's /tmp, because an overlay
  upperdir has no size of its own: statvfs there reports host capacity, no
  quota applies, and it is not a tmpfs so nothing clears it on restart.
  data_dir is a fixed-size volume, so the same bytes land against a limit
  the platform owns and can measure.

  That ceiling is shared with the database, so per-chat directories and their
  removal are what keep scratch from taking durable data down with it;
  `agent_scratch` owns that lifecycle.
  """
  return Path(get_settings().data_dir) / "agent-scratch"
