"""Recurring schedule discovery, supervision, and job routes."""

import json
import logging
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import app_cron, app_jobs, models, schemas
from app.config import get_settings
from app.database import get_db
from app.deps import (
  Principal, get_current_owner_or_app, get_principal, reject_cross_site,
)
from app.manifest_contract import ManifestContractError, validate_cron_expr
from app.resource_access import live_app_or_404


router = APIRouter()
log = logging.getLogger(__name__)


def _cron_replay_dirs_for_app(app: models.App, source_dir: Path) -> list[Path]:
  return [source_dir]


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
  """The live crontab, for read-only discovery only.

  Flattening an unreadable spool to "" is safe here and only here: discovery
  then falls through to the durable declaration and the manifest. Mutating
  callers must use ``app_cron.read_crontab()`` and respect its ``None``.
  """
  return app_cron.read_crontab() or ""


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
    if not command_path.startswith(needle):
      continue
    # A line whose job script is gone is DEBRIS, not a declaration. Cron
    # registration is add-only — the scaffold rewrites the line for the path
    # it installs and leaves every other line alone — so an app that renames
    # its job leaves the old entry behind forever. Trusting it here let the
    # dead line win purely because it sorted first, which then (a) reported a
    # phantom job and cadence on /api/apps/schedules, and (b) failed the
    # reconciler's own is_file() guard, so the app's REAL schedule was never
    # re-registered and the whole supervision pass reported a warning.
    # Skipping debris lets discovery fall through to the durable init-cron.sh
    # declaration and then the manifest — both of which state real intent.
    # Legacy unsupervised entries still migrate: their script exists.
    if not Path(command_path).exists():
      continue
    return cron, Path(command_path).name
  return None


def _app_schedule(app: models.App, live_crontab: str) -> tuple[str, str] | None:
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

  source_dir = Path(app.source_dir)
  for replay_dir in _cron_replay_dirs_for_app(app, source_dir):
    declaration = cron_tz.parse_zone_declaration(
      _read_init_cron_text(replay_dir),
    )
    if declaration is not None:
      return declaration
  return None


def _is_orphaned_supervised_entry(line: str, apps_root: Path) -> bool:
  """True when `line` is a supervised app entry whose job script is gone.

  Deliberately narrow. Only a platform-supervised entry (one routed through
  app-job-runner) naming a job directly under the apps root qualifies, so
  unsupervised owner lines, env assignments, comments, and legacy direct
  entries — which migration, not pruning, owns — are never candidates.
  """
  if _parse_cron_job_line(line) is None:
    return False
  if "/app-job-runner.py" not in line:
    return False
  command_path = app_cron.crontab_command_path(line)
  if not command_path:
    return False
  job = Path(command_path)
  try:
    if job.parent.parent != apps_root:
      return False
  except (OSError, ValueError):
    return False
  return not job.exists()


def _prune_orphaned_supervised_entries(apps_root: Path) -> list[str]:
  """Drop live supervised entries whose job script no longer exists.

  Registration converges the entry for the job path it installs, but nothing
  retires the entry for a job path an app has stopped shipping. Such a line is
  provably dead — the runner rejects it on every fire — yet it wakes a Python
  interpreter on its cadence forever (news-2's renamed job.sh fired once a
  minute for weeks) and, before the discovery fix above, could shadow the
  app's real schedule.

  Only the LIVE crontab is rewritten. The durable init-cron.sh declaration is
  left untouched on purpose: this removes a dead materialization, never an
  app's stated intent, so an app whose tree is briefly incomplete simply
  re-arms from its declaration on the next reconciliation. Returns the dropped
  lines. Best-effort — an unreadable spool or a failed write changes nothing.
  """
  current = app_cron.read_crontab()
  if current is None:
    return []
  kept, dropped = [], []
  for line in current.splitlines():
    if _is_orphaned_supervised_entry(line, apps_root):
      dropped.append(line)
    else:
      kept.append(line)
  if not dropped:
    return []
  if not app_cron.write_crontab(("\n".join(kept) + "\n") if kept else ""):
    return []
  return dropped


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
    if schedule is None:
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
  # After every live declaration has been converged, retire the entries no
  # declaration claims any more. Pruning last means a job path this pass just
  # registered is present in the crontab we read, so it can never be mistaken
  # for debris.
  for line in _prune_orphaned_supervised_entries(resolved_root):
    log.info("retired orphaned cron entry (job script gone): %s", line.strip())
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
  """Accept a request to run the app's scheduled job asynchronously.

  Mini-apps cannot shell out themselves — this is the bridge that lets
  a Reports tab's "Generate now" button trigger the same job the cron
  schedule would run. The endpoint returns 202 immediately with an
  `started_at` acceptance timestamp; the job may take 30s+ to complete. A
  request that overlaps another job for the same app is skipped while the
  existing run continues. Callers observe completion by polling the app's
  storage for newly-written output (for example,
  `/api/storage/apps/{id}/reports/<date>.json`).

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
  # No override: this is the owner's system default, which the job then layers
  # its own declared pick on top of.
  choices = resolve_background_agents(get_settings().data_dir)
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
  slug = app.slug
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
