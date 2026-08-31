"""Per-project artifact builders and their build lifecycle.

A project artifact is a buildable output whose source lives in the project
tree and whose build status is persisted on ``Project.artifacts_json``. The
platform keeps website/LaTeX built-ins for blank and existing projects, while
an installed app can contribute a reviewed on-demand build script through its
project template. This module owns that one dispatch boundary, the in-memory
registry of live build tasks, and the common build lifecycle.

Concurrency model (single uvicorn worker, single event loop):
  - ``run_build`` holds the per-project build lock for its whole duration, so a
    project runs one build at a time (a Möbius user pays for their own build
    CPU/RAM — one build per project).
  - Every write to ``Project.artifacts_json`` — the status transitions here and
    the CRUD writes in ``routes/projects.py`` — is a synchronous read-update-
    commit with no ``await`` inside it, run on the one event loop. The loop
    therefore orders them and no update is lost, even while a build holds the
    build lock across its subprocess ``await``.
  - ``_LIVE`` maps ``(project_id, artifact_id)`` to the running task. A stored
    status of ``building`` with no live task is a stale marker from a process
    that died mid-build: reads reconcile it to ``error`` and a rebuild is
    allowed (never a 409).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from app import workspace_files
from app.timeutil import now_naive_utc

log = logging.getLogger(__name__)

# Artifact ids are slugs used verbatim as ``artifacts/<id>/`` path components,
# so the character set is deliberately narrow.
ARTIFACT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

VALID_STATUSES = ("idle", "building", "ok", "error")

BUILTIN_ARTIFACT_TYPES: dict[str, dict[str, Any]] = {
  "website": {
    "id": "website",
    "name": "Website",
    "extensions": ["html", "htm"],
    "preview": "html",
    "script": None,
    "output": "{source}",
  },
  "latex": {
    "id": "latex",
    "name": "PDF",
    "extensions": ["tex"],
    "preview": "pdf",
    "script": None,
    "output": "{stem}.pdf",
  },
}

# tectonic's first cold run downloads its support bundle, which is slow and
# needs network egress. Pin the cache under /data so a warm cache survives
# across builds and container restarts.
TECTONIC_CACHE_DIR = "/data/.cache/tectonic"

# A build that has not finished in this many seconds is killed. Generous
# because a cold tectonic run fetches a multi-megabyte bundle before it can
# typeset anything.
_BUILD_TIMEOUT_SECS = 180

# The on-disk build.log is capped so a pathologically chatty build cannot fill
# the volume. The log-read endpoint tails a smaller window on top of this.
_LOG_MAX_BYTES = 256 * 1024
_LOG_CHUNK = 32 * 1024

# Provider-contributed artifact scripts need an ordinary command runtime, not
# the API worker's credentials.  Keep the small process environment needed to
# locate tools, caches, and temporary files; project-specific capabilities are
# added explicitly by ``_provider_script_env`` below.
_SAFE_SCRIPT_ENV_KEYS = frozenset({
  "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TMP", "TEMP",
  "USER", "LOGNAME", "SHELL", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
})

# In-memory registry of live build tasks, keyed by (project_id, artifact_id).
# A task is removed by its own done-callback. Reads use ``live_task`` which
# also treats a finished-but-not-yet-reaped task as absent.
_LIVE: dict[tuple[str, str], asyncio.Task] = {}


def _now_iso() -> str:
  return now_naive_utc().isoformat()


def slug_artifact_id(name: str) -> str:
  """Derive a valid artifact id from a display name."""
  lowered = (name or "").lower()
  slug = "".join(
    ch if (ch.isalnum() or ch in "-_") else "-" for ch in lowered
  ).strip("-_")
  while "--" in slug:
    slug = slug.replace("--", "-")
  return (slug or "artifact")[:64]


def _clean_artifact_type(value: Any) -> dict[str, Any] | None:
  """Narrow one template declaration before it can select executable code."""
  if not isinstance(value, dict):
    return None
  artifact_type_id = value.get("id")
  name = value.get("name")
  extensions = value.get("extensions")
  preview = value.get("preview")
  script = value.get("script")
  output = value.get("output")
  if not (
    isinstance(artifact_type_id, str)
    and ARTIFACT_ID_RE.match(artifact_type_id)
    and isinstance(name, str)
    and name.strip()
    and isinstance(extensions, list)
    and extensions
    and all(
      isinstance(ext, str) and re.fullmatch(r"[a-z0-9]{1,16}", ext)
      for ext in extensions
    )
    and preview in ("html", "pdf", "image")
    and isinstance(script, str)
    and script.endswith(".sh")
    and not Path(script).is_absolute()
    and "\\" not in script
    and all(part not in ("", ".", "..") for part in Path(script).parts)
    and isinstance(output, str)
    and output
  ):
    return None
  return {
    "id": artifact_type_id,
    "name": name.strip(),
    "extensions": list(dict.fromkeys(extensions)),
    "preview": preview,
    "script": script,
    "output": output,
  }


def template_artifact_types(template: Any) -> list[dict[str, Any]]:
  """Read validated artifact types from a snapshotted project template."""
  raw = template.get("artifact_types") if isinstance(template, dict) else None
  if not isinstance(raw, list):
    return []
  return [clean for value in raw if (clean := _clean_artifact_type(value))]


def resolve_artifact_type(project, builder: str) -> dict[str, Any] | None:
  """Resolve one project-owned builder id to its snapshotted provider contract."""
  for artifact_type in template_artifact_types(
    getattr(project, "template_snapshot_json", None),
  ):
    if artifact_type["id"] == builder:
      return artifact_type
  builtin = BUILTIN_ARTIFACT_TYPES.get(builder)
  return dict(builtin) if builtin is not None else None


def artifact_type_for_source(
  template: Any, source: str, *, preview: str | None = None,
) -> dict[str, Any] | None:
  """Choose the declared type for a source extension and optional preview kind."""
  extension = Path(source).suffix.lower().lstrip(".")
  for artifact_type in template_artifact_types(template):
    if extension not in artifact_type["extensions"]:
      continue
    if preview is None or artifact_type["preview"] == preview:
      return artifact_type
  for artifact_type in BUILTIN_ARTIFACT_TYPES.values():
    if extension in artifact_type["extensions"] and (
      preview is None or artifact_type["preview"] == preview
    ):
      return dict(artifact_type)
  return None


def output_entry(artifact_type: dict[str, Any], source: str) -> str | None:
  """Render and confine one provider-declared output entry template."""
  source_path = Path(source.lstrip("/"))
  rendered = str(artifact_type.get("output") or "").replace(
    "{source}", source_path.as_posix(),
  ).replace("{stem}", source_path.stem)
  candidate = Path(rendered)
  if (
    not rendered
    or candidate.is_absolute()
    or "\\" in rendered
    or any(part in ("", ".", "..") for part in candidate.parts)
  ):
    return None
  return candidate.as_posix()


def default_output_rel(
  artifact_id: str, builder: str, source: str,
  artifact_type: dict[str, Any] | None = None,
) -> str:
  """Project-relative path of the artifact's output entry file.

  The website builder copies the source tree into ``output/`` preserving
  structure, so its entry keeps the source's relative path. The LaTeX builder
  emits ``<stem>.pdf`` from tectonic's ``--outdir`` next to no other structure,
  so its entry is that pdf basename.
  """
  base = f"artifacts/{artifact_id}/output"
  declared = artifact_type or BUILTIN_ARTIFACT_TYPES.get(builder)
  rendered = output_entry(declared, source) if declared else None
  if rendered:
    return f"{base}/{rendered}"
  if builder == "latex":
    return f"{base}/{Path(source).with_suffix('.pdf').name}"
  return f"{base}/{source.lstrip('/')}"


def default_log_rel(artifact_id: str) -> str:
  return f"artifacts/{artifact_id}/build.log"


def new_artifact_entry(
  artifact_id: str, name: str, builder: str, source: str,
  artifact_type: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Build a fresh registry entry in the canonical shape."""
  declared = artifact_type or BUILTIN_ARTIFACT_TYPES.get(builder)
  return {
    "id": artifact_id,
    "name": name,
    "builder": builder,
    "source": source,
    "output_rel": default_output_rel(artifact_id, builder, source, declared),
    "preview": declared.get("preview") if declared else None,
    "type_name": declared.get("name") if declared else None,
    "status": "idle",
    "updated_at": _now_iso(),
    "duration_ms": None,
    "log_rel": default_log_rel(artifact_id),
  }


def read_artifacts(project) -> list[dict[str, Any]]:
  """Tolerantly read the artifact registry; never raise on agent-authored data.

  The agent owns the project tree and may hand-edit ``artifacts_json`` into a
  malformed value. A bad top-level value reads as no artifacts, and an entry
  without a usable id is skipped rather than crashing the listing. Individual
  field problems (a missing source, an unknown builder) are surfaced to the
  reader by the view layer, not filtered here.
  """
  raw = getattr(project, "artifacts_json", None)
  if not isinstance(raw, list):
    return []
  entries: list[dict[str, Any]] = []
  for item in raw:
    if not isinstance(item, dict):
      continue
    artifact_id = item.get("id")
    if not isinstance(artifact_id, str) or not ARTIFACT_ID_RE.match(artifact_id):
      continue
    entries.append(item)
  return entries


def live_task(project_id: str, artifact_id: str) -> asyncio.Task | None:
  """Return the running build task for this artifact, or None.

  A task that has finished but whose done-callback has not yet run reads as
  absent so a completed build never blocks a rebuild.
  """
  task = _LIVE.get((project_id, artifact_id))
  if task is None or task.done():
    return None
  return task


def is_build_live(project_id: str, artifact_id: str) -> bool:
  return live_task(project_id, artifact_id) is not None


def effective_status(project_id: str, entry: dict[str, Any]) -> str:
  """Reconcile a stored status against the live task registry.

  A live task always reads as ``building`` (running or queued behind the build
  lock). A stored ``building`` with no live task is stale — the process that
  owned it died — so it reads as ``error`` and a rebuild is allowed.
  """
  artifact_id = entry.get("id")
  if isinstance(artifact_id, str) and is_build_live(project_id, artifact_id):
    return "building"
  stored = entry.get("status")
  if stored == "building":
    return "error"
  if stored in VALID_STATUSES:
    return stored
  return "idle"


def start_build(project_id: str, artifact_id: str) -> asyncio.Task:
  """Schedule ``run_build`` and register it in the live task registry.

  The check-then-register is atomic on the single event loop (no ``await``
  between them), so a concurrent second request for the same artifact returns
  the same task rather than starting a duplicate. Callers gate a 409 on
  ``is_build_live`` before calling this.
  """
  key = (project_id, artifact_id)
  existing = live_task(project_id, artifact_id)
  if existing is not None:
    return existing
  task = asyncio.create_task(run_build(project_id, artifact_id))
  _LIVE[key] = task

  def _reap(finished: asyncio.Task, key: tuple[str, str] = key) -> None:
    # Identity-checked so a newer task for the same key is never dropped.
    if _LIVE.get(key) is finished:
      _LIVE.pop(key, None)

  task.add_done_callback(_reap)
  return task


def _write_status(
  project_id: str, artifact_id: str, *, status: str, duration_ms: int | None,
) -> None:
  """Read-update-commit one artifact's status on ``Project.artifacts_json``.

  Its own short-lived session, a fresh read, a single reassignment, one commit
  — the synchronous shape the single-event-loop ordering relies on. A vanished
  project or artifact is a no-op: a build that outlived its artifact (deleted
  mid-build) must not resurrect it.
  """
  from sqlalchemy.orm.attributes import flag_modified

  from app import models
  from app.database import SessionLocal

  with SessionLocal() as db:
    project = db.query(models.Project).filter(
      models.Project.id == project_id,
      models.Project.deleted_at.is_(None),
    ).first()
    if project is None:
      return
    entries = read_artifacts(project)
    changed = False
    for entry in entries:
      if entry.get("id") == artifact_id:
        entry["status"] = status
        entry["updated_at"] = _now_iso()
        if duration_ms is not None:
          entry["duration_ms"] = duration_ms
        changed = True
        break
    if not changed:
      return
    project.artifacts_json = entries
    flag_modified(project, "artifacts_json")
    project.updated_at = now_naive_utc()
    db.commit()


def _build_context(
  project_id: str, artifact_id: str,
) -> tuple[Path, str, str, dict[str, Any], Path | None] | None:
  """Resolve the project, provider, and source for one build."""
  from app import models
  from app.config import get_settings
  from app.database import SessionLocal

  with SessionLocal() as db:
    project = db.query(models.Project).filter(
      models.Project.id == project_id,
      models.Project.deleted_at.is_(None),
    ).first()
    if project is None:
      return None
    entry = next(
      (e for e in read_artifacts(project) if e.get("id") == artifact_id), None,
    )
    if entry is None:
      return None
    data_root = Path(get_settings().data_dir).resolve()
    stored = Path(project.root_path)
    root = (stored if stored.is_absolute() else data_root / stored).resolve()
    builder = entry.get("builder")
    source = entry.get("source")
    artifact_type = resolve_artifact_type(project, str(builder))
    if artifact_type is None:
      return None
    app_source = None
    if artifact_type.get("script"):
      app = db.query(models.App).filter(
        models.App.id == project.source_app_id,
        models.App.deleted_at.is_(None),
      ).first()
      if app is not None:
        app_source = Path(app.source_dir).resolve()
  return (
    root,
    str(builder),
    str(source) if isinstance(source, str) else "",
    artifact_type,
    app_source,
  )


def _write_log(log_path: Path, text: str) -> None:
  try:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(text, encoding="utf-8")
  except OSError:
    log.warning("Could not write build log at %s", log_path)


def _append_log(log_path: Path, text: str) -> None:
  try:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as handle:
      handle.write(text)
  except OSError:
    log.warning("Could not append build log at %s", log_path)


async def _drain_to_log(stream: asyncio.StreamReader, log_file) -> None:
  """Copy combined subprocess output to the log file with a byte cap.

  The pipe is always fully drained so the child never blocks on a full buffer,
  but writes stop at ``_LOG_MAX_BYTES`` so one build cannot fill the volume.
  """
  written = 0
  truncated = False
  while True:
    chunk = await stream.read(_LOG_CHUNK)
    if not chunk:
      break
    if written >= _LOG_MAX_BYTES:
      continue
    remaining = _LOG_MAX_BYTES - written
    log_file.write(chunk[:remaining])
    log_file.flush()
    written += min(remaining, len(chunk))
    if written >= _LOG_MAX_BYTES and not truncated:
      truncated = True
      log_file.write(b"\n...build log truncated...\n")
      log_file.flush()


async def _run_tectonic(
  *, source: str, output_dir: Path, cwd: Path, env: dict, log_path: Path,
) -> int:
  """Run tectonic, streaming combined stdout+stderr to ``build.log``.

  Isolated behind its own function so tests stub the subprocess (no tectonic
  binary, no network in CI). Returns the process exit code.
  """
  cmd = ["tectonic", source, "--outdir", str(output_dir)]
  with open(log_path, "wb") as log_file:
    log_file.write(f"$ {' '.join(cmd)}\n".encode())
    log_file.flush()
    proc = await asyncio.create_subprocess_exec(
      *cmd,
      cwd=str(cwd),
      env=env,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    try:
      await asyncio.wait_for(
        _drain_to_log(proc.stdout, log_file), timeout=_BUILD_TIMEOUT_SECS,
      )
      await asyncio.wait_for(proc.wait(), timeout=_BUILD_TIMEOUT_SECS)
    except asyncio.TimeoutError:
      proc.kill()
      await proc.wait()
      log_file.write(
        f"\ntectonic timed out after {_BUILD_TIMEOUT_SECS}s.\n".encode()
      )
      raise RuntimeError("tectonic build timed out")
    except asyncio.CancelledError:
      proc.kill()
      await proc.wait()
      raise
  return proc.returncode or 0


async def _run_provider_script(
  *, script: Path, root: Path, source: str, output_dir: Path,
  artifact_id: str, log_path: Path,
) -> None:
  """Run one manifest-reviewed on-demand app job for a Project artifact."""
  env = _provider_script_env(
    root=root, source=source, output_dir=output_dir, artifact_id=artifact_id,
  )
  if output_dir.exists():
    shutil.rmtree(output_dir)
  output_dir.mkdir(parents=True, exist_ok=True)
  cmd = ["bash", str(script)]
  with open(log_path, "wb") as log_file:
    log_file.write(f"$ {script.name}\n".encode())
    log_file.flush()
    proc = await asyncio.create_subprocess_exec(
      *cmd,
      cwd=str(root),
      env=env,
      stdout=asyncio.subprocess.PIPE,
      stderr=asyncio.subprocess.STDOUT,
    )
    try:
      await asyncio.wait_for(
        _drain_to_log(proc.stdout, log_file), timeout=_BUILD_TIMEOUT_SECS,
      )
      await asyncio.wait_for(proc.wait(), timeout=_BUILD_TIMEOUT_SECS)
    except asyncio.TimeoutError:
      proc.kill()
      await proc.wait()
      raise RuntimeError(
        f"project builder timed out after {_BUILD_TIMEOUT_SECS}s",
      )
    except asyncio.CancelledError:
      proc.kill()
      await proc.wait()
      raise
  if proc.returncode:
    raise RuntimeError(f"project builder exited with status {proc.returncode}")


def _provider_script_env(
  *, root: Path, source: str, output_dir: Path, artifact_id: str,
) -> dict[str, str]:
  """Build the least-privilege environment for an app artifact script."""
  env = {
    key: value for key, value in os.environ.items()
    if key in _SAFE_SCRIPT_ENV_KEYS
  }
  env.update({
    "PROJECT_ROOT": str(root),
    "PROJECT_SOURCE": source,
    "PROJECT_OUTPUT_DIR": str(output_dir),
    "PROJECT_ARTIFACT_ID": artifact_id,
  })
  return env


async def build_latex(
  *, root: Path, source: str, output_dir: Path, log_path: Path,
) -> None:
  """Compile a LaTeX source to a PDF with tectonic.

  The tectonic cache is pinned under /data so a warm bundle survives restarts;
  the first cold run fetches the bundle (slow, needs egress). Raises on a
  non-zero exit so ``run_build`` records ``error``.
  """
  output_dir.mkdir(parents=True, exist_ok=True)
  env = dict(os.environ)
  cache_dir = Path(TECTONIC_CACHE_DIR)
  try:
    cache_dir.mkdir(parents=True, exist_ok=True)
    env["TECTONIC_CACHE_DIR"] = str(cache_dir)
  except OSError:
    # A read-only or missing /data cache path must not abort the build;
    # tectonic falls back to its own default cache location.
    log.warning("Could not create tectonic cache at %s", cache_dir)
  returncode = await _run_tectonic(
    source=source, output_dir=output_dir, cwd=root, env=env, log_path=log_path,
  )
  if returncode != 0:
    raise RuntimeError(f"tectonic exited with status {returncode}")


async def build_website(
  *, root: Path, source: str, output_dir: Path, log_path: Path,
) -> None:
  """Copy project sources, excluding build output and repository metadata.

  Copying the whole tree — not just the entry file — is what lets relative
  assets (images, fonts, CSS, extra pages) resolve when the built site renders
  in a sandboxed iframe. ``artifacts/`` is excluded so the output never copies
  itself. Git control data is reserved whether represented by a directory or a
  gitfile. Symlinks are omitted recursively; ``symlinks=True`` also guarantees
  a concurrent entry swap can copy only the link, never dereference its target.
  """
  if output_dir.exists():
    shutil.rmtree(output_dir)
  output_dir.mkdir(parents=True, exist_ok=True)
  lines = ["Building website: copying project sources into output.\n"]
  copied = 0
  for child in sorted(root.iterdir(), key=lambda p: p.name):
    if (
      child.name == "artifacts"
      or child.name in workspace_files.GIT_METADATA_NAMES
      or child.is_symlink()
    ):
      continue
    dest = output_dir / child.name
    if child.is_dir():
      shutil.copytree(
        child,
        dest,
        symlinks=True,
        ignore=lambda directory, names: [
          name for name in names
          if (
            name in workspace_files.GIT_METADATA_NAMES
            or (Path(directory) / name).is_symlink()
          )
        ],
      )
    else:
      shutil.copyfile(child, dest, follow_symlinks=False)
    copied += 1
  entry = output_dir / source.lstrip("/")
  if not entry.is_file():
    lines.append(
      f"Warning: entry file '{source}' was not found in the copied output.\n"
    )
  lines.append(f"Copied {copied} top-level entries into output/.\n")
  _write_log(log_path, "".join(lines))


BUILDERS: dict[str, Callable[..., Any]] = {
  "website": build_website,
  "latex": build_latex,
}


def _publish_build_event(project_id: str, artifact_id: str, status: str) -> None:
  """Broadcast an artifact build status change on the system broadcast.

  Modeled on the real system-broadcast payloads already published from
  ``routes/projects.py`` (``project_deleted`` / ``project_recovered`` —
  ``{type, projectId, ...}``) and ``routes/notify.py``'s ``build_phase``
  (``{type, ..., ts}``). The open workspace subscribes to the system broadcast,
  so build status reaches it regardless of which view is active — no polling.
  """
  from app.broadcast import get_system_broadcast

  try:
    get_system_broadcast().publish({
      "type": "project_artifact_build",
      "projectId": str(project_id),
      "artifactId": str(artifact_id),
      "status": status,
      "ts": int(time.time() * 1000),
    })
  except Exception:
    # A broadcast failure must never fail the build it is only reporting on.
    log.warning("Could not publish build event for %s/%s", project_id, artifact_id)


async def run_build(project_id: str, artifact_id: str) -> None:
  """Run one artifact build under the per-project build lock.

  Sets ``building``, runs the builder, then records ``ok``/``error`` with the
  elapsed duration, broadcasting each transition. Holding the lock across the
  whole build enforces one build per project. All errors (unknown builder,
  missing source, builder failure) resolve to ``error`` with a log line rather
  than propagating — the task is fire-and-forget.
  """
  from app.fs_locks import project_build_lock

  async with project_build_lock(project_id):
    context = _build_context(project_id, artifact_id)
    if context is None:
      # The project or artifact vanished between scheduling and running.
      return
    root, builder_name, source, artifact_type, app_source = context
    artifact_dir = root / "artifacts" / artifact_id
    output_dir = artifact_dir / "output"
    log_path = artifact_dir / "build.log"
    try:
      artifact_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
      log.warning("Could not create artifact dir %s", artifact_dir)

    started = time.monotonic()
    _write_status(project_id, artifact_id, status="building", duration_ms=None)
    _publish_build_event(project_id, artifact_id, "building")

    status = "error"
    try:
      source_path = (root / source).resolve() if source else None
      if (
        not source
        or source_path is None
        or not source_path.is_relative_to(root)
        or source_path.is_symlink()
        or not source_path.is_file()
      ):
        _write_log(log_path, f"Source file is missing: {source or '(none)'}\n")
      else:
        script_rel = artifact_type.get("script")
        if script_rel:
          script = (app_source / script_rel).resolve() if app_source else None
          if (
            script is None
            or app_source is None
            or not script.is_relative_to(app_source)
            or not script.is_file()
            or script.is_symlink()
          ):
            raise RuntimeError("artifact provider is unavailable")
          await _run_provider_script(
            script=script,
            root=root,
            source=source,
            output_dir=output_dir,
            artifact_id=artifact_id,
            log_path=log_path,
          )
        else:
          builder = BUILDERS.get(builder_name)
          if builder is None:
            raise RuntimeError(f"unknown builder: {builder_name}")
          await builder(
            root=root, source=source, output_dir=output_dir, log_path=log_path,
          )
        entry = output_entry(artifact_type, source)
        if entry is None or not (output_dir / entry).is_file():
          raise RuntimeError("builder finished without its declared output")
        status = "ok"
    except asyncio.CancelledError:
      _append_log(log_path, "\nBuild cancelled.\n")
      duration_ms = int((time.monotonic() - started) * 1000)
      _write_status(project_id, artifact_id, status="error", duration_ms=duration_ms)
      _publish_build_event(project_id, artifact_id, "error")
      raise
    except Exception as exc:
      _append_log(log_path, f"\nBuild failed: {exc}\n")

    duration_ms = int((time.monotonic() - started) * 1000)
    _write_status(project_id, artifact_id, status=status, duration_ms=duration_ms)
  _publish_build_event(project_id, artifact_id, status)


def reset_for_tests() -> None:
  """Drop any live build tasks between tests."""
  _LIVE.clear()
