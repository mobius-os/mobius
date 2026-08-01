"""Recurring schedule discovery, supervision, and job routes."""

import json
import re
import subprocess
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import app_cron, app_jobs, models, schemas
from app.app_identity import slugify_for_source_dir as _slugify_for_source_dir
from app.app_source_paths import legacy_platform_runtime_dir_for_app
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal, get_current_owner_or_app, get_principal, reject_cross_site,
)
from app.manifest_contract import ManifestContractError, validate_cron_expr
from app.resource_access import live_app_or_404


router = APIRouter()


def _cron_replay_dirs_for_app(app: models.App, source_dir: Path) -> list[Path]:
  runtime_dir = legacy_platform_runtime_dir_for_app(app)
  if runtime_dir is None:
    return [source_dir]
  try:
    if runtime_dir.resolve() == source_dir.resolve():
      return [source_dir]
  except (OSError, RuntimeError):
    pass
  return [source_dir, runtime_dir]


def _read_init_cron_text(replay_dir: Path) -> str:
  init_path = replay_dir / "init-cron.sh"
  try:
    return init_path.read_text() if init_path.is_file() else ""
  except OSError:
    return ""


def _parse_cron_job_line(line: str) -> tuple[str, str] | None:
  """Returns (cron expression, command path) for one runnable crontab line."""
  s = line.strip()
  if not s or s.startswith("#"):
    return None
  first = s.split(None, 1)[0]
  if first.startswith("@"):
    parts = s.split(None, 1)
    if len(parts) != 2:
      return None
    cron, cmd = parts[0], parts[1]
  elif "=" in first:
    return None
  else:
    parts = s.split(None, 5)
    if len(parts) != 6:
      return None
    cron, cmd = " ".join(parts[:5]), parts[5]
  toks = cmd.split()
  while toks and "=" in toks[0] and not toks[0].startswith("/"):
    toks.pop(0)
  if not toks:
    return None
  return cron, toks[0]


def _read_live_crontab() -> str:
  try:
    result = subprocess.run(
      ["crontab", "-u", "mobius", "-l"],
      capture_output=True,
      text=True,
      timeout=10,
      check=False,
    )
  except OSError:
    return ""
  return result.stdout if result.returncode == 0 else ""


def _manifest_schedule(source_dir: Path) -> tuple[str, str] | None:
  try:
    manifest = json.loads((source_dir / "mobius.json").read_text())
  except (OSError, ValueError):
    return None
  if not isinstance(manifest, dict):
    return None
  sched = manifest.get("schedule")
  if not isinstance(sched, dict):
    return None
  cron = sched.get("default")
  if not isinstance(cron, str) or not cron.strip():
    return None
  job = sched.get("job")
  if not isinstance(job, str) or "/" in job or "\\" in job or not job.strip():
    job = "job.sh"
  return cron, job


def _schedule_from_crontab_text(
  source_dir: Path, text: str,
) -> tuple[str, str] | None:
  needle = f"{str(source_dir).rstrip('/')}/"
  for line in text.splitlines():
    entry_match = re.search(r"""^\s*ENTRY=(?:"([^"]+)"|'([^']+)')""", line)
    if entry_match:
      line = entry_match.group(1) or entry_match.group(2) or ""
    parsed = _parse_cron_job_line(line)
    if parsed is None:
      continue
    cron, _ = parsed
    # Managed entries launch Python first, then the common runner, then the
    # real app job.  Resolve that indirection so schedule discovery remains
    # stable after an entry is supervised (and can still migrate old direct
    # entries on the next boot).
    command_path = app_cron.crontab_command_path(line)
    if command_path.startswith(needle):
      return cron, Path(command_path).name
  return None


def _app_schedule(app: models.App, live_crontab: str) -> tuple[str, str] | None:
  if not app.source_dir:
    return None
  source_dir = Path(app.source_dir)
  live = _schedule_from_crontab_text(source_dir, live_crontab)
  if live is not None:
    return live
  for replay_dir in _cron_replay_dirs_for_app(app, source_dir):
    schedule = _schedule_from_crontab_text(
      source_dir, _read_init_cron_text(replay_dir),
    )
    if schedule is not None:
      return schedule
  return _manifest_schedule(source_dir)


def _app_zone_declaration(app: models.App) -> tuple[str, str] | None:
  """The schedule's durable (timezone, zone_cron) identity, if declared.

  Read from the init-cron.sh declaration lines the platform appends when a
  schedule is owned in an IANA zone (see app.cron_tz / app_cron.register_cron).
  """
  from app import cron_tz

  if not app.source_dir:
    return None
  source_dir = Path(app.source_dir)
  for replay_dir in _cron_replay_dirs_for_app(app, source_dir):
    declaration = cron_tz.parse_zone_declaration(
      _read_init_cron_text(replay_dir),
    )
    if declaration is not None:
      return declaration
  return None


def reconcile_app_cron_supervision(db: Session) -> tuple[int, list[str]]:
  """Converge every live managed schedule through the common job runner.

  Older ``init-cron.sh`` files wrote ``<source>/fetch.sh`` directly. Boot never
  executes those files; cron is deliberately started only after FastAPI
  lifespan completes. This reconciliation therefore gets a race-free window
  to parse and preserve each effective cadence while rewriting both the live
  crontab entry and its durable declaration via the current
  scaffold. Tombstoned apps are excluded and source trees must be ordinary,
  non-symlink direct children of ``/data/apps``.

  A schedule owned in an IANA timezone (see ``app.cron_tz``) is materialized
  as an every-minute supervised gate. The gate, not a snapshot of today's UTC
  offset, decides the declared wall-clock occurrence at runtime.
  """
  from app import cron_tz

  settings = get_settings()
  apps_root = Path(settings.data_dir) / "apps"
  try:
    resolved_root = apps_root.resolve(strict=True)
  except OSError:
    return 0, [f"apps root unavailable: {apps_root}"]
  live_crontab = _read_live_crontab()
  reconciled = 0
  warnings: list[str] = []
  apps = db.query(models.App).filter(models.App.deleted_at.is_(None)).all()
  for app in apps:
    schedule = _app_schedule(app, live_crontab)
    if schedule is None or not app.source_dir:
      continue
    source_dir = Path(app.source_dir)
    try:
      if source_dir.is_symlink():
        raise ValueError("source directory is a symlink")
      resolved_source = source_dir.resolve(strict=True)
      if resolved_source.parent != resolved_root:
        raise ValueError("source directory is not a direct app child")
    except (OSError, RuntimeError, ValueError) as exc:
      warnings.append(f"app {app.id}: {exc}")
      continue
    cron, job_name = schedule
    job_path = resolved_source / job_name
    try:
      if job_path.is_symlink() or not job_path.is_file():
        raise ValueError(f"job is missing or a symlink: {job_name}")
      declaration = _app_zone_declaration(app)
      if declaration is not None:
        timezone, zone_cron = declaration
        app_cron.register_cron(
          resolved_source.name,
          cron_tz.materialize_zone_cron(zone_cron, timezone),
          job_path, app.id,
          timezone=timezone, zone_cron=zone_cron,
        )
      else:
        app_cron.register_cron(
          resolved_source.name, cron, job_path, app.id,
        )
    except Exception as exc:
      warnings.append(f"app {app.id}: {exc}")
      continue
    reconciled += 1
  return reconciled, warnings


@router.get("/schedules", response_model=list[schemas.AppScheduleOut])
def list_app_schedules(
  db: Session = Depends(get_db),
  _: models.Owner = Depends(get_current_owner_or_app),
):
  """Returns read-only recurring app schedules visible to owners and apps."""
  from app import cron_tz

  live_crontab = _read_live_crontab()
  server_timezone = cron_tz.server_timezone_name()
  rows = []
  apps = (
    db.query(models.App)
    .filter(models.App.deleted_at.is_(None))
    .order_by(models.App.name, models.App.id)
    .all()
  )
  for app in apps:
    schedule = _app_schedule(app, live_crontab)
    if schedule is None:
      continue
    cron, job = schedule
    try:
      declaration = _app_zone_declaration(app)
    except ValueError:
      # Listing remains available for repair, while reconciliation itself
      # fails closed and never installs the unguarded every-minute cadence.
      declaration = None
    rows.append(schemas.AppScheduleOut(
      id=app.id,
      name=app.name,
      slug=app.slug,
      cron=cron,
      job=job,
      timezone=declaration[0] if declaration else None,
      zone_cron=declaration[1] if declaration else None,
      server_timezone=server_timezone,
    ))
  return rows


def _manifest_job_name(source_dir: Path) -> str | None:
  """The job script the app's `mobius.json` declares under `schedule.job`.

  This is the source of truth for which script a run-job (and the cron
  schedule) should invoke. The legacy probe below only guesses by filename,
  so when an app renames its job (e.g. tandem's `job.sh` -> `generate.sh`)
  a stale sibling left in the tree shadows the new script. Reading the
  manifest immunizes every app against that race: the declared script wins
  regardless of what else happens to sit in the directory.

  Returns the bare filename only when the manifest names a job that is a
  simple filename with no path separators (the same shape `install._validate_manifest`
  enforces) AND that file actually exists on disk — a manifest that points
  at a since-deleted script should fall through to the legacy probe rather
  than 400. Any read/parse error is non-fatal: older apps have no manifest
  on disk, and the probe is the fallback for them.
  """
  manifest_path = source_dir / "mobius.json"
  try:
    manifest = json.loads(manifest_path.read_text())
  except (OSError, ValueError):
    return None
  if not isinstance(manifest, dict):
    return None
  sched = manifest.get("schedule")
  if not isinstance(sched, dict):
    return None
  job = sched.get("job")
  if not isinstance(job, str) or "/" in job or "\\" in job or not job.strip():
    return None
  return job if (source_dir / job).is_file() else None


@router.post(
  "/{app_id}/run-job",
  status_code=202,
  dependencies=[Depends(reject_cross_site)],
)
def run_app_job(
  app_id: int,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Spawns the app's scheduled job script as a non-blocking subprocess.

  Mini-apps cannot shell out themselves — this is the bridge that lets
  a Reports tab's "Generate now" button trigger the same job the cron
  schedule would run. The endpoint returns 202 immediately with a
  started_at timestamp; the job may take 30s+ to complete. Callers
  observe completion by polling the app's storage for newly-written
  output (e.g. `/api/storage/apps/{id}/reports/<date>.json`).

  The job script lives at `<source_dir>/<job_name>` where source_dir
  is the app's on-disk source tree (per the install-from-manifest
  layout in `app.install`). The manifest's `schedule.job` is the
  source of truth and is tried FIRST — the legacy filename probe
  (fetch.sh / job.sh / build.sh) only runs when no manifest declares
  a job, so a stale sibling script can't shadow the script the app
  actually ships (tandem's old job.sh once won over its new
  generate.sh because the probe order, not the manifest, decided).

  Authorized for the owner OR for an app-scoped token whose `app_id`
  matches the path — the News "run now" button fires from inside the
  mini-app iframe, which only holds an app-scoped token, so requiring
  owner-only here would 403 the very caller the endpoint exists for.
  The app can trigger its own job but not a sibling's. The same
  defense-in-depth CSRF guard the other state-changing endpoints
  (settings, model-prefs) use still applies. Mirrors the self-scope
  check on the icon-write route above.
  """
  from datetime import UTC, datetime
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(
      status_code=403,
      detail="App token can only run its own job.",
    )
  app = live_app_or_404(db, app_id)
  if not app.source_dir:
    raise HTTPException(
      status_code=400, detail="App has no source_dir; cannot locate job.",
    )
  source_dir = Path(app.source_dir)
  # The manifest's schedule.job wins. The legacy probe (fetch.sh
  # app-news convention, job.sh install-from-manifest default,
  # build.sh LaTeX/pipeline apps) is the fallback for apps installed
  # before the manifest convention solidified — first hit wins, in
  # priority order.
  job_path = None
  manifest_job = _manifest_job_name(source_dir)
  if manifest_job is not None:
    job_path = source_dir / manifest_job
  else:
    for candidate in ("fetch.sh", "job.sh", "build.sh"):
      p = source_dir / candidate
      if p.is_file():
        job_path = p
        break
  if job_path is None:
    raise HTTPException(
      status_code=400,
      detail="No job script found (looked for fetch.sh, job.sh, build.sh).",
    )
  # Non-blocking. stdout/stderr go to /dev/null so the subprocess
  # doesn't inherit the FastAPI worker's pipes; the job script itself
  # is expected to log to /data/cron-logs/.
  app_jobs.launch_app_job(app_id, job_path, source_dir)
  return {"started_at": datetime.now(UTC).isoformat()}


@router.get("/{app_id}/job-context")
def get_app_job_context(
  app_id: int,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Return non-secret identity, agent choices and review receipt to a job.

  Jobs should not import platform internals or read owner settings files.  This
  narrow surface lets a short-lived app token inherit the owner's configured
  background provider ordering without receiving credentials or unrelated
  settings.
  """
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(
      status_code=403,
      detail="App token can only read its own job context.",
    )
  app = live_app_or_404(db, app_id)
  from app.background_agents import resolve_background_agents
  choices = resolve_background_agents(get_settings().data_dir, {})
  return {
    "app_id": app_id,
    # The supervisor binds the scheduled script to this exact app before
    # granting its token. This is non-secret durable identity, not owner
    # configuration or a filesystem grant.
    "source_dir": app.source_dir,
    "primary": choices.get("primary"),
    "fallback": choices.get("fallback"),
    # This is the same normalized, non-secret receipt the owner reviewed.
    # Jobs such as Memory may verify their installed data/schedule contract
    # against it; it is not a filesystem sandbox or mount plan.
    "capability_contract": app.capability_contract,
  }


@router.post(
  "/{app_id}/schedule",
  dependencies=[Depends(reject_cross_site)],
)
def update_app_schedule(
  app_id: int,
  body: schemas.AppScheduleUpdate,
  db: Session = Depends(get_db),
  principal: Principal = Depends(get_principal),
):
  """Updates one app's recurring cron schedule.

  Authorized for the owner OR for the app itself. This is the schedule
  counterpart to run-job: a mini-app settings screen can tune its own
  recurring job, but an app token cannot rewrite a sibling's crontab.
  The scaffold writes both the live crontab and durable init-cron.sh so
  the change survives container restarts.
  """
  if principal.app_id is not None and principal.app_id != app_id:
    raise HTTPException(
      status_code=403,
      detail="App token can only update its own schedule.",
    )
  app = live_app_or_404(db, app_id)
  if not app.source_dir:
    raise HTTPException(
      status_code=400, detail="App has no source_dir; cannot locate job.",
    )
  from app import cron_tz
  try:
    validate_cron_expr(body.cron)
  except ManifestContractError as exc:
    raise HTTPException(status_code=400, detail=str(exc)) from exc
  timezone = (body.timezone or "").strip() or None
  if timezone is not None:
    # A zone-owned schedule is durable data: body.cron is the daily wall time
    # in that zone, and the crontab entry is an every-minute materialization
    # whose supervised gate decides the real due instant.
    if not cron_tz.valid_timezone(timezone):
      raise HTTPException(
        status_code=400, detail=f"Unknown IANA timezone: {timezone!r}",
      )
    if cron_tz.parse_daily_cron(body.cron) is None:
      raise HTTPException(
        status_code=400,
        detail="A timezone-owned schedule must be a plain daily cron "
               "('m h * * *').",
      )
  source_dir = Path(app.source_dir)
  job_name = body.job or "fetch.sh"
  if "/" in job_name or "\\" in job_name or not job_name.strip():
    raise HTTPException(status_code=400, detail="Invalid job filename.")
  job_path = source_dir / job_name
  if not job_path.is_file():
    raise HTTPException(status_code=400, detail="Job script not found.")
  slug = app.slug or _slugify_for_source_dir(app.name)
  if timezone is not None:
    materialized = cron_tz.materialize_zone_cron(body.cron, timezone)
    app_cron.register_cron(
      slug, materialized, job_path, app_id,
      timezone=timezone, zone_cron=body.cron,
    )
    return {
      "cron": materialized, "job": job_name,
      "timezone": timezone, "zone_cron": body.cron,
    }
  app_cron.register_cron(slug, body.cron, job_path, app_id)
  return {"cron": body.cron, "job": job_name, "timezone": None,
          "zone_cron": None}
