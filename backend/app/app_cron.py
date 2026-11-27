"""Shared cron declaration and parsing primitives for installed apps."""

import json
import logging
import os
import re
import shlex
import subprocess
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from fastapi import HTTPException

from app.config import get_settings

log = logging.getLogger(__name__)

BAKED_CRON_SCAFFOLD = Path("/app/scripts/init-cron-scaffold.sh")
_ALLOW_TEST_CRON_ENV = "MOBIUS_ALLOW_TEST_CRON"
# With this set the scaffold writes only the durable declaration and
# register_cron installs the live entry itself (replace_managed_schedule).
# A direct scaffold run leaves it unset and keeps the scaffold's own live write.
_DECLARE_ONLY_ENV = "MOBIUS_CRON_DECLARE_ONLY"
_DECLARED_ENTRY = re.compile(r"""^\s*ENTRY=(?:"([^"]+)"|'([^']+)')\s*$""", re.M)


class CronDeclarationError(HTTPException):
  """One app's schedule declaration is invalid; other apps may proceed."""


class CronInfrastructureError(HTTPException):
  """The shared cron installer is unavailable or failed to execute."""


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


# Platform-owned and id-keyed, like app-runtime/ and app-envs/. Reconciliation
# trusts these files, so they stay outside the app's data tree
# (/data/apps/<id>), which its storage token, its Edit-role agents and Clear
# data all reach.
SCHEDULES_DIRNAME = "app-schedules"


def schedule_state_dir(app_id: int) -> Path:
  """The owner's schedule declaration for one app: choice.json, init-cron.sh."""
  return Path(get_settings().data_dir) / SCHEDULES_DIRNAME / str(int(app_id))


_SCHEDULE_CHOICE_FILE = "choice.json"


@dataclass(frozen=True)
class ScheduleChoice:
  """Who chose an app's current schedule, and what they chose.

  ``source`` is ``"owner"`` for a schedule set through the schedule route
  (by the owner or the app's own settings screen) and ``"manifest"`` for the
  default the platform registered from the app's manifest. ``cron`` is the
  chosen cadence, zone-local when ``timezone`` is set. ``manifest_default`` is
  the manifest default in force when the choice was recorded; it tells a later
  update whether the app has changed its schedule contract since.
  """

  source: str
  cron: str
  job: str
  timezone: str | None = None
  manifest_default: str | None = None


def record_schedule_choice(app_id: int, choice: ScheduleChoice) -> None:
  """Atomically record provenance beside the durable schedule declaration."""
  state_dir = schedule_state_dir(app_id)
  state_dir.mkdir(parents=True, exist_ok=True)
  target = state_dir / _SCHEDULE_CHOICE_FILE
  tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
  tmp.write_text(json.dumps(asdict(choice), sort_keys=True), encoding="utf-8")
  os.replace(tmp, target)


def clear_schedule_choice(app_id: int) -> None:
  (schedule_state_dir(app_id) / _SCHEDULE_CHOICE_FILE).unlink(missing_ok=True)


def _restore_schedule_file(path: Path, data: bytes | None, mode: int | None) -> None:
  if data is None:
    path.unlink(missing_ok=True)
    return
  tmp = path.with_name(f".{path.name}.{os.getpid()}.rollback")
  try:
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
  finally:
    tmp.unlink(missing_ok=True)


@contextmanager
def schedule_choice_rollback(app_id: int):
  """Restore a rejected save, not an accepted install awaiting cron retry.

  The caller holds install_uninstall_lock from validation through rollback.
  Snapshot raw bytes: unreadable or malformed provenance is not absence, and
  a failed snapshot must refuse the save before either durable file changes.
  """
  snapshots = []
  for name in (_SCHEDULE_CHOICE_FILE, "init-cron.sh"):
    path = schedule_state_dir(app_id) / name
    try:
      try:
        data = path.read_bytes()
      except FileNotFoundError:
        data, mode = None, None
      else:
        mode = path.stat().st_mode & 0o7777
    except OSError as exc:
      raise CronInfrastructureError(
        500, "Could not read the current schedule; nothing was changed.",
      ) from exc
    snapshots.append((path, data, mode))
  try:
    yield
  except BaseException:
    for path, data, mode in snapshots:
      try:
        _restore_schedule_file(path, data, mode)
      except OSError:
        log.exception(
          "Could not roll back schedule file %s for app %s", path.name, app_id,
        )
    raise


def read_schedule_choice(app_id: int) -> ScheduleChoice | None:
  try:
    raw = json.loads(
      (schedule_state_dir(app_id) / _SCHEDULE_CHOICE_FILE).read_text(),
    )
    return ScheduleChoice(
      source=str(raw["source"]),
      cron=str(raw["cron"]),
      job=str(raw["job"]),
      timezone=raw.get("timezone") or None,
      manifest_default=raw.get("manifest_default") or None,
    )
  except (OSError, ValueError, TypeError, KeyError):
    return None


def owner_schedule_to_keep(
  app_id: int, default: str, job: str,
) -> ScheduleChoice | None:
  """The owner's schedule choice that survives an accepted manifest.

  An update or local apply keeps the owner's choice while the app's schedule
  contract is unchanged: the same scheduled job, and either the same manifest
  default as when the owner chose, or a wall-time default on the same weekdays
  as the owner's wall time (an app retiming its daily default does not override
  the owner's daily time). A different job or cadence kind returns ``None`` so
  the new default applies.
  """
  from app import cron_tz

  choice = read_schedule_choice(app_id)
  if choice is None or choice.source != "owner" or choice.job != job:
    return None
  if choice.manifest_default == default:
    return choice
  chosen = cron_tz.parse_wall_clock_cron(choice.cron)
  offered = cron_tz.parse_wall_clock_cron(default)
  if chosen and offered and chosen[2] == offered[2]:
    return choice
  return None


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
  """Install one durable, supervised app schedule.

  The scaffold atomically records the complete durable declaration first; for
  an app id this function then materializes that declaration as the app's one
  live entry (``replace_managed_schedule``). A failed durable write therefore
  cannot change live behavior, while a later live-write failure leaves a
  declaration that startup reconciliation can retry safely. Without an app id
  the scaffold installs its standalone entry itself.

  ``app_id`` is passed to the common job runner as the target application.
  ``timezone`` and ``zone_cron`` form one inseparable wall-clock identity: the
  materialized crontab cadence is only a gate, and the runner decides whether
  the source schedule is due in its named IANA zone.

  Tests may inject ``scaffold`` explicitly. Production callers normally omit
  it so the served checkout is preferred over the baked fallback.
  """
  if cron_mutation_blocked_in_test_runtime():
    raise CronInfrastructureError(
      500, "Cron mutation is disabled in the test runtime.",
    )
  if (timezone is None) != (zone_cron is None):
    raise CronDeclarationError(
      500, "Cron registration bug: timezone and zone_cron must be paired.",
    )
  if timezone is not None:
    from app import cron_tz

    if app_id is None:
      raise CronDeclarationError(
        500, "A timezone-owned schedule requires an app id.",
      )
    if not cron_tz.valid_timezone(timezone):
      raise CronDeclarationError(
        500, f"Unknown IANA timezone: {timezone!r}",
      )
    if cron_tz.parse_wall_clock_cron(zone_cron) is None:
      raise CronDeclarationError(
        500, f"Zone-owned schedule must be a fixed wall time: {zone_cron!r}",
      )
  active_scaffold = scaffold or cron_scaffold()
  if not active_scaffold.exists():
    raise CronInfrastructureError(
      500, "init-cron-scaffold.sh missing from image.",
    )
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
  if app_id is not None:
    env["MOBIUS_APP_CRON_STATE_DIR"] = str(schedule_state_dir(app_id))
    env[_DECLARE_ONLY_ENV] = "1"
  try:
    result = subprocess.run(
      command, capture_output=True, text=True, timeout=30, env=env,
    )
  except (OSError, subprocess.SubprocessError) as exc:
    raise CronInfrastructureError(
      500, f"Cron registration could not run: {exc}",
    ) from exc
  if result.returncode != 0:
    raise CronInfrastructureError(
      500,
      f"Cron registration failed: {result.stderr.strip()[:400]}",
    )
  if app_id is not None:
    _install_declared_entry(schedule_state_dir(app_id) / "init-cron.sh")


def _install_declared_entry(init_path: Path) -> None:
  """Make the just-written declaration the app's one live crontab entry."""
  try:
    match = _DECLARED_ENTRY.search(init_path.read_text())
  except OSError:
    match = None
  entry = (match.group(1) or match.group(2)) if match else ""
  if not crontab_command_path(entry):
    raise CronInfrastructureError(
      500, "Cron registration wrote no schedule declaration.",
    )
  current = read_crontab()
  if current is None:
    raise CronInfrastructureError(
      500, "Cron registration could not read the live crontab; "
      "the declaration is kept for the next reconciliation.",
    )
  if not write_crontab(replace_managed_schedule(current, entry)):
    raise CronInfrastructureError(
      500, "Cron registration could not write the live crontab; "
      "the declaration is kept for the next reconciliation.",
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


def _crontab_command_tokens(line: str) -> list[str]:
  """Return shell tokens for a cron command, excluding its schedule."""
  stripped = line.strip()
  if not stripped or stripped.startswith("#"):
    return []
  first = stripped.split(None, 1)[0]
  if first.startswith("@"):
    command = (stripped.split(None, 1) + [""])[1]
  elif "=" in first:
    return []
  else:
    parts = stripped.split(None, 5)
    command = parts[5] if len(parts) == 6 else ""
  try:
    # Cron executes through a shell: an unquoted # starts a comment, while a
    # quoted # remains part of the argument it belongs to.
    return shlex.split(command, comments=True, posix=True)
  except ValueError:
    return []


_SHELL_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_PYTHON_EXECUTABLE = re.compile(r"^python(?:3(?:\.\d+)*)?$")


def _supervised_job_path(tokens: list[str]) -> str | None:
  """Return JOB only for the app-job runner's complete supported CLI."""
  index = 0
  while index < len(tokens) and _SHELL_ASSIGNMENT.match(tokens[index]):
    index += 1
  if index < len(tokens) and Path(tokens[index]).name == "env":
    index += 1
    while index < len(tokens) and _SHELL_ASSIGNMENT.match(tokens[index]):
      index += 1
  if (
    index < len(tokens)
    and _PYTHON_EXECUTABLE.fullmatch(Path(tokens[index]).name)
  ):
    index += 1
  if (
    index >= len(tokens)
    or Path(tokens[index]).name != "app-job-runner.py"
  ):
    return None
  argv = tokens[index + 1:]
  if argv[:1] == ["--wait-for-ready"]:
    argv = argv[1:]
  if argv[:1] == ["--scheduled"]:
    argv = argv[1:]
  if argv[:1] == ["--wall-clock"]:
    if len(argv) < 3:
      return None
    argv = argv[3:]
  if len(argv) != 2 or not argv[0].isdigit():
    return None
  return argv[1]


def is_supervised_crontab_entry(line: str) -> bool:
  """Whether cron actually invokes the platform app-job runner."""
  return _supervised_job_path(_crontab_command_tokens(line)) is not None


def is_wall_clock_gate(line: str) -> bool:
  """Whether cron invokes the app-job runner as an every-minute ``--wall-clock``
  gate, whose crontab cadence is not the app's real schedule."""
  tokens = _crontab_command_tokens(line)
  return _supervised_job_path(tokens) is not None and "--wall-clock" in tokens


def crontab_command_path(line: str) -> str:
  """Return the app executable path from one cron line, or an empty string."""
  tokens = _crontab_command_tokens(line)
  if not tokens:
    return ""
  supervised_job = _supervised_job_path(tokens)
  if supervised_job is not None:
    return supervised_job
  while tokens and _SHELL_ASSIGNMENT.match(tokens[0]):
    tokens.pop(0)
  return tokens[0] if tokens else ""


def replace_managed_schedule(current: str, entry: str) -> str:
  """Return ``current`` with ``entry`` as its app's only managed schedule.

  A line is replaced when it runs the same job, or when it is a supervised
  line for the same app whose job has a different name (the app changed its
  job). Every other line, including ones whose arguments merely mention the
  job path, is kept.
  """
  entry_tokens = _crontab_command_tokens(entry)
  job = crontab_command_path(entry)
  # The supported runner CLI ends with APP_ID JOB.
  app_id = entry_tokens[-2] if _supervised_job_path(entry_tokens) else None
  kept = []
  for line in current.splitlines():
    tokens = _crontab_command_tokens(line)
    previous_job = _supervised_job_path(tokens)
    if job and crontab_command_path(line) == job:
      continue
    # Match the job's directory as well as the id: an id reused by another
    # app must never let this app erase that app's schedule.
    if (
      app_id is not None
      and previous_job is not None
      and tokens[-2] == app_id
      and Path(previous_job).parent == Path(job).parent
    ):
      continue
    kept.append(line)
  return "\n".join([*kept, entry]) + "\n"


def crontab_without_app(current: str, source_dir: Path) -> str | None:
  """Return `current` crontab text with every line whose COMMAND runs a
  script under `source_dir` removed — or None if nothing matched, so the
  caller can skip rewriting entirely.

  Matches on the command's executable path (see `crontab_command_path`),
  NOT a whole-line substring: that keeps the news/news-2 prefix safe AND
  avoids dropping an unrelated app whose ARGUMENTS merely reference this
  app's dir (e.g. `... /data/apps/agg/run.sh --feed /data/apps/news/x`).
  Comments, blanks, and `PATH=`/env lines run no command and are preserved;
  `@daily`/`@reboot` shorthand and inline `VAR=val <cmd>` are handled too.
  """
  needle = f"{str(source_dir).rstrip('/')}/"
  kept, dropped = [], False
  for ln in current.splitlines():
    if crontab_command_path(ln).startswith(needle):
      dropped = True
    else:
      kept.append(ln)
  if not dropped:
    return None
  return ("\n".join(kept) + "\n") if kept else ""


def unregister_cron(source_dir: Path) -> None:
  """Remove crontab entries that invoke scripts under `source_dir`.

  Called on app delete so a removed app does not leave a crontab entry
  firing a now-missing script. The spool isn't on the /data volume, so
  an orphan self-clears on the next container restart anyway — this just
  stops it firing (and erroring) in the meantime, and prevents stale
  lines like the `news-2/job.sh` orphan from accumulating across
  reinstalls. Best-effort: every failure is swallowed, exactly like the
  source-tree rmtree this accompanies. Runs `crontab -u mobius` (the
  server runs as mobius, which may edit its own crontab).
  """
  if cron_mutation_blocked_in_test_runtime():
    return
  try:
    listing = subprocess.run(
      ["crontab", "-u", "mobius", "-l"],
      capture_output=True, text=True, timeout=10,
    )
  except (OSError, subprocess.SubprocessError):
    return
  if listing.returncode != 0:
    # No crontab yet, or no crontab binary (as in the test image) —
    # nothing to clean.
    return
  new_crontab = crontab_without_app(listing.stdout, source_dir)
  if new_crontab is None:
    return  # no entry referenced this app — leave the crontab untouched
  try:
    proc = subprocess.run(
      ["crontab", "-u", "mobius", "-"],
      input=new_crontab, text=True, timeout=10, check=False,
    )
  except (OSError, subprocess.SubprocessError):
    return
  if proc.returncode != 0:
    log.warning(
      "cron: failed to rewrite mobius crontab on app delete (rc=%s)",
      proc.returncode,
    )
