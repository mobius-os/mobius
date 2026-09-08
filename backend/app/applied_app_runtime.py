"""Runtime files pinned to the same accepted revision as the compiled app.

The editable source tree is never a runtime fallback. SQLite's runtime_revision
is the publication pointer; preparing a tree does not make it live. Old trees
stay addressable for in-flight jobs; Apply and startup reclaim obsolete trees.
"""

from __future__ import annotations

import json
import fcntl
import hashlib
from dataclasses import dataclass
import os
import re
import shutil
import tempfile
from pathlib import Path

from app import app_git
from app.config import get_settings
from app.manifest_contract import static_asset_entries


_SOURCE_MANIFEST = object()


class AppliedRuntimeUnavailable(RuntimeError):
  """The app has no usable explicitly accepted source revision."""


@dataclass(frozen=True)
class PreparedRuntime:
  root: Path
  revision: str


def _prepared(root: Path) -> PreparedRuntime:
  digest = hashlib.sha256()
  for path in sorted(root.rglob("*")):
    if path.is_file():
      relative = path.relative_to(root).as_posix().encode()
      executable = bool(path.stat().st_mode & 0o111)
      digest.update(relative + b"\0" + (b"x" if executable else b"-") + b"\0")
      digest.update(str(path.stat().st_size).encode() + b"\0")
      with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
          digest.update(chunk)
  return PreparedRuntime(root, digest.hexdigest())


def runtime_parent(app_id: int) -> Path:
  return Path(get_settings().data_dir) / "app-runtime" / str(int(app_id))


def _revision(value: str | None) -> str:
  if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
    raise AppliedRuntimeUnavailable("Apply this app before running its source files.")
  return value


def prepare_runtime(
  source_dir: Path,
  source_commit: str,
  *,
  static_assets: dict[str, bytes] | None = None,
  runtime_manifest: bytes | None | object = _SOURCE_MANIFEST,
) -> PreparedRuntime:
  """Prepare a detached accepted tree; publication is a later cheap rename."""
  revision = _revision(source_commit)
  cache = Path(get_settings().data_dir) / "app-runtime"
  cache.mkdir(parents=True, exist_ok=True)
  staged = Path(tempfile.mkdtemp(prefix=".staged-", dir=cache))
  try:
    app_git.materialize_tree(source_dir, revision, staged)
    if runtime_manifest is None:
      (staged / "mobius.json").unlink(missing_ok=True)
    elif runtime_manifest is not _SOURCE_MANIFEST:
      (staged / "mobius.json").write_bytes(runtime_manifest)
    if static_assets is None:
      # Bootstrap older accepted revisions without ever reading dirty files.
      # Local apply commits declared asset inputs before normalization. Store
      # generated outputs are supplied explicitly on install; bootstrap freezes
      # previously deployed outputs because Git intentionally omits them.
      manifest_path = staged / "mobius.json"
      manifest = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
      static_assets = {}
      for destination, source in static_asset_entries(manifest.get("static_assets") or {}).items():
        path = staged / source
        if path.is_file():
          static_assets[destination] = path.read_bytes()
    for destination, content in static_assets.items():
      target = staged / "static" / destination
      if not target.resolve().is_relative_to(staged.resolve()):
        raise AppliedRuntimeUnavailable("Accepted static asset escapes its runtime tree.")
      target.parent.mkdir(parents=True, exist_ok=True)
      target.write_bytes(content)
    return _prepared(staged)
  except Exception as exc:
    shutil.rmtree(staged)
    if isinstance(exc, AppliedRuntimeUnavailable):
      raise
    raise AppliedRuntimeUnavailable("The accepted app revision could not be materialized.") from exc


def publish_runtime(app, staged: PreparedRuntime) -> Path:
  """Publish immutable bytes, then stage their pointer in the owning DB row."""
  target = runtime_parent(app.id) / staged.revision
  target.parent.mkdir(parents=True, exist_ok=True)
  try:
    staged.root.rename(target)
  except OSError:
    if not target.is_dir():
      raise
    shutil.rmtree(staged.root)
  app.runtime_revision = staged.revision
  return target


def runtime_root(app) -> Path:
  """Read the committed runtime pointer, never lazily adopt editable files."""
  revision = getattr(app, "runtime_revision", None)
  if revision is None:
    raise AppliedRuntimeUnavailable("This app has no frozen runtime baseline; Apply it before running its source files.")
  target = runtime_parent(app.id) / _revision(revision)
  if target.is_dir():
    return target
  if app.source_commit:
    # A lost cache may be reconstructed only if accepted inputs reproduce its
    # exact content address. Ignored Store outputs need explicit reinstallation.
    staged = prepare_runtime(Path(app.source_dir), app.source_commit)
    if staged.revision == revision:
      return publish_runtime(app, staged)
    shutil.rmtree(staged.root)
  raise AppliedRuntimeUnavailable("The frozen runtime files are missing; Apply the app to restore them.")


def _copy_deployed_files(source: Path, target: Path) -> None:
  shutil.copytree(source, target, dirs_exist_ok=True, symlinks=True,
                  ignore=shutil.ignore_patterns(".git"))
  # Match the Git materializer: never keep links into editable or private data.
  for path in target.rglob("*"):
    if path.is_symlink():
      link = os.readlink(path)
      path.unlink()
      path.write_text(link)


def bootstrap_legacy_runtimes(db) -> tuple[int, list[str]]:
  """Freeze pre-isolation deployed files once, before startup admits writers.

  Known commits supply scripts and their siblings. The old static/ directory
  is a deployed-output baseline because Git deliberately excludes generated
  assets. Apps without a recorded commit freeze their full deployed source.
  Neither baseline claims to reconstruct a historical Apply.

  The receipt is created before copying: after any partial failure, later
  boots must not silently promote possibly edited files. Explicit Apply
  repairs a missing baseline through the ordinary accepted-revision path.
  """
  from app import models

  cache = Path(get_settings().data_dir) / "app-runtime"
  cache.mkdir(parents=True, exist_ok=True)
  receipt = cache / "legacy-baseline-migration.json"
  try:
    handle = receipt.open("x", encoding="utf-8")
  except FileExistsError:
    return 0, []
  rows = db.query(models.App).all()
  with handle:
    json.dump({"schema": 1, "app_ids": [app.id for app in rows]}, handle)
    handle.flush()
    os.fsync(handle.fileno())
  migrated = 0
  warnings = []
  for app in rows:
    source = Path(app.source_dir)
    staged = None
    try:
      if getattr(app, "runtime_revision", None) is not None:
        continue
      if not source.is_dir() or source.is_symlink():
        raise AppliedRuntimeUnavailable("source directory is missing or a symlink")
      if app.source_commit:
        staged = prepare_runtime(source, app.source_commit).root
        # Synthetic Store repositories may omit the manifest too. Before
        # isolation these exact deployed declarations owned job discovery.
        deployed_manifest = source / "mobius.json"
        if deployed_manifest.is_file() and not deployed_manifest.is_symlink():
          shutil.copyfile(deployed_manifest, staged / "mobius.json")
        live_static = source / "static"
        if live_static.is_symlink():
          raise AppliedRuntimeUnavailable("deployed static directory is a symlink")
        if live_static.is_dir():
          _copy_deployed_files(live_static, staged / "static")
      else:
        staged = Path(tempfile.mkdtemp(prefix=".legacy-", dir=cache))
        _copy_deployed_files(source, staged)
      # This ignored owner declaration governed the pre-upgrade schedule.
      # Preserve it in the frozen baseline; later API changes write numeric
      # owner data instead and win over this migration-only fallback.
      declaration = source / "init-cron.sh"
      if declaration.is_file() and not declaration.is_symlink():
        shutil.copyfile(declaration, staged / "init-cron.sh")
      previous_updated = app.updated_at
      publish_runtime(app, _prepared(staged))
      staged = None
      # Runtime migration is not a user edit and must not reorder every app.
      from sqlalchemy.orm.attributes import flag_modified
      app.updated_at = previous_updated
      flag_modified(app, "updated_at")
      db.commit()
      migrated += 1
    except Exception as exc:
      db.rollback()
      warnings.append(f"app {app.id} deployed runtime baseline: {exc}")
    finally:
      if staged is not None:
        shutil.rmtree(staged)
  return migrated, warnings


def hold_static_runtime(app_id: int):
  """Pin runtime files until a static HTTP response has finished sending."""
  parent = Path(get_settings().data_dir) / "run" / "app-runtime-readers"
  parent.mkdir(parents=True, exist_ok=True)
  handle = (parent / f"{int(app_id)}.lock").open("a")
  try:
    fcntl.flock(handle, fcntl.LOCK_SH)
    return handle
  except BaseException:
    handle.close()
    raise


def prune_runtime(app, *, previous_revision: str | None = None) -> int:
  """Bound full-tree copies, without removing files an active reader needs.

  The existing single-flight job lock covers job-context lookup AND child
  lifetime, so pruning cannot race a job that has not published its path yet.
  Static responses use a short shared read pin. A busy app defers cleanup to
  the next Apply or startup rather than interrupting running work.
  """
  parent = runtime_parent(app.id)
  if not parent.is_dir():
    return 0
  handles = []
  try:
    for namespace in ("app-job-locks", "app-runtime-readers"):
      lock_dir = Path(get_settings().data_dir) / "run" / namespace
      lock_dir.mkdir(parents=True, exist_ok=True)
      handle = (lock_dir / f"{int(app.id)}.lock").open("a")
      handles.append(handle)
      try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
      except BlockingIOError:
        return 0
    current = getattr(app, "runtime_revision", None)
    if current is None:
      return 0
    current = _revision(current)
    roots = [child for child in parent.iterdir() if child.is_dir() and not child.is_symlink()
             and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}|legacy", child.name)]
    keep = {current}
    if previous_revision is not None:
      keep.add(_revision(previous_revision))
    else:
      older = sorted((root for root in roots if root.name != current),
                     key=lambda root: root.stat().st_mtime_ns, reverse=True)
      if older:
        keep.add(older[0].name)
    removed = 0
    for root in roots:
      if root.name not in keep:
        shutil.rmtree(root)
        removed += 1
    return removed
  finally:
    for handle in reversed(handles):
      handle.close()
