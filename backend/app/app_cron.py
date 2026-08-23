"""Shared cron declaration and parsing primitives for installed apps."""

import os
import subprocess
from pathlib import Path

from fastapi import HTTPException

from app.config import get_settings

BAKED_CRON_SCAFFOLD = Path("/app/scripts/init-cron-scaffold.sh")
_ALLOW_TEST_CRON_ENV = "MOBIUS_ALLOW_TEST_CRON"


def cron_scaffold(override: Path | None = None) -> Path:
  """Resolve a test override, the served scaffold, or the baked boot floor."""
  if override is not None and override != BAKED_CRON_SCAFFOLD:
    return override
  live = (
    Path(__file__).resolve().parent.parent
    / "scripts"
    / "init-cron-scaffold.sh"
  )
  return live if live.is_file() else BAKED_CRON_SCAFFOLD


def cron_mutation_blocked_in_test_runtime() -> bool:
  """Fail closed when an isolated test could otherwise edit host crontab."""
  return (
    os.environ.get("MOBIUS_TEST_RUNTIME") == "1"
    and os.environ.get(_ALLOW_TEST_CRON_ENV) != "1"
  )


def register_cron(
  slug: str,
  schedule_expr: str,
  job_path: Path,
  app_id: int | None = None,
  timezone: str | None = None,
  zone_cron: str | None = None,
  *,
  scaffold: Path | None = None,
) -> None:
  """Install one durable, supervised app schedule through the scaffold.

  The scaffold atomically records the complete durable declaration before it
  installs the live crontab entry. A failed durable write therefore cannot
  change live behavior, while a later live-write failure leaves a declaration
  that startup reconciliation can retry safely.

  ``app_id`` is passed to the common job runner as the target application.
  ``timezone`` and ``zone_cron`` form one inseparable wall-clock identity: the
  materialized crontab cadence is only a gate, and the runner decides whether
  the source schedule is due in its named IANA zone.

  Tests may inject ``scaffold`` explicitly. Production callers normally omit
  it so the served checkout is preferred over the baked fallback.
  """
  if cron_mutation_blocked_in_test_runtime():
    raise HTTPException(500, "Cron mutation is disabled in the test runtime.")
  if (timezone is None) != (zone_cron is None):
    raise HTTPException(
      500, "Cron registration bug: timezone and zone_cron must be paired.",
    )
  if timezone is not None:
    from app import cron_tz

    if app_id is None:
      raise HTTPException(500, "A timezone-owned schedule requires an app id.")
    if not cron_tz.valid_timezone(timezone):
      raise HTTPException(500, f"Unknown IANA timezone: {timezone!r}")
    if cron_tz.parse_daily_cron(zone_cron) is None:
      raise HTTPException(
        500, f"Zone-owned schedule must be a plain daily cron: {zone_cron!r}",
      )
  active_scaffold = scaffold or cron_scaffold()
  if not active_scaffold.exists():
    raise HTTPException(500, "init-cron-scaffold.sh missing from image.")
  command = [
    str(active_scaffold), slug, schedule_expr, job_path.name,
  ]
  if app_id is not None:
    command.append(str(app_id))
  if timezone is not None:
    command.extend([timezone, zone_cron])

  from app.app_jobs import runner_script

  env = dict(os.environ)
  env["API_BASE_URL"] = get_settings().api_base_url
  env["MOBIUS_APP_JOB_RUNNER"] = str(runner_script())
  result = subprocess.run(
    command, capture_output=True, text=True, timeout=30, env=env,
  )
  if result.returncode != 0:
    raise HTTPException(
      500,
      f"Cron registration failed: {result.stderr.strip()[:400]}",
    )


def read_crontab() -> str | None:
  """The owner's live crontab text, or None when it could not be read.

  ``None`` and ``""`` are deliberately different answers. An empty string
  means the spool is genuinely empty; ``None`` means the read FAILED and the
  crontab may be full of entries we simply cannot see. A caller that rewrites
  from a failed read would drop every one of them, so mutating callers must
  treat ``None`` as "change nothing". Read-only callers may flatten it to "".
  """
  try:
    result = subprocess.run(
      ["crontab", "-u", "mobius", "-l"],
      capture_output=True, text=True, timeout=10, check=False,
    )
  except (OSError, subprocess.SubprocessError):
    return None
  if result.returncode != 0:
    # Only the benign "no crontab for <user>" is real emptiness. Any other
    # failure (unreadable spool, missing binary in the test image) is a read
    # error that must not be mistaken for an empty crontab.
    if "no crontab" in (result.stderr or "").lower():
      return ""
    return None
  return result.stdout


def write_crontab(text: str) -> bool:
  """Replace the owner's crontab wholesale. Returns True when it took."""
  if cron_mutation_blocked_in_test_runtime():
    return False
  try:
    result = subprocess.run(
      ["crontab", "-u", "mobius", "-"],
      input=text, text=True, timeout=10, check=False,
    )
  except (OSError, subprocess.SubprocessError):
    return False
  return result.returncode == 0


def crontab_command_path(line: str) -> str:
  """Return the app executable path from one cron line, or an empty string."""
  stripped = line.strip()
  if not stripped or stripped.startswith("#"):
    return ""
  first = stripped.split(None, 1)[0]
  if first.startswith("@"):
    command = (stripped.split(None, 1) + [""])[1]
  elif "=" in first:
    return ""
  else:
    parts = stripped.split(None, 5)
    command = parts[5] if len(parts) == 6 else ""
  tokens = command.split()
  while tokens and "=" in tokens[0] and not tokens[0].startswith("/"):
    tokens.pop(0)
  if not tokens:
    return ""
  for index, token in enumerate(tokens):
    if token.endswith("/app-job-runner.py") and len(tokens) > index + 2:
      return tokens[-1]
  return tokens[0]
